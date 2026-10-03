#!/usr/bin/python3
"""CouchLiteOS browsers on demand (root side): install Firefox or Google Chrome from the internet.

Installed as /usr/libexec/couchliteos-browser, and as couchliteos_browser.py for the launcher,
which only uses BROWSERS, installed_browsers() and kiosk_command(). Neither browser ships in the
image. The launcher writes the browser's name to /run/couchliteos/browser-install;
couchliteos-browser.service then runs `request` as root.

    install firefox|chrome    apt-get update, download, install; progress in browser-status.json
    remove firefox|chrome     apt-get remove
    request                   read and remove the launcher's request file, then install
    refresh                   after an OS update: install the installed browsers again so their
                              dependencies match the new system (couchliteos-browser-refresh.service)

apt always reads CouchLiteOS's own source lists (APT_DIR), so an install does not depend on what
the Debian installer left in /etc/apt. Standard library only; every system effect goes through
Env so tests can use fakes.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Callable, Iterable, Iterator


@dataclasses.dataclass(frozen=True)
class Browser:
    name: str      # what the request file and the command line say
    label: str     # what the screen says
    package: str
    binary: str


BROWSERS = {
    "firefox": Browser("firefox", "FIREFOX", "firefox-esr", "/usr/bin/firefox-esr"),
    "chrome": Browser("chrome", "GOOGLE CHROME", "google-chrome-stable", "/usr/bin/google-chrome-stable"),
}
RUN_DIR = pathlib.Path("/run/couchliteos")
REQUEST_NAME = "browser-install"
STATUS_NAME = "browser-status.json"
LOCK_NAME = "browser.lock"
REFRESH_REL = "var/lib/couchliteos-update/browser-refresh"  # written by the updater after an update
LOG_REL = "var/log/couchliteos/browser.log"
APT_DIR = "/usr/share/couchliteos/apt"
APT_OPTIONS = (
    "-o", f"Dir::Etc::sourcelist={APT_DIR}/sources.list",
    "-o", f"Dir::Etc::sourceparts={APT_DIR}/sources.list.d",
    # A changed configuration file never stops an unattended install with a question.
    "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold",
)
INSTALL = ("install", "-y", "--no-install-recommends")
PHASES = ("checking", "downloading", "installing", "removing", "done", "failed")
MIN_FREE = 1 << 30  # a browser and its download need about 1 GB (a live USB stick keeps it in memory)
MAX_REQUEST = 64

MSG_NETWORK = "COULD NOT REACH THE INTERNET: CHECK SETTINGS > NETWORK"
MSG_SPACE = "NOT ENOUGH FREE SPACE: A WEB BROWSER NEEDS ABOUT 1 GB"
MSG_BUSY = "ANOTHER INSTALL IS RUNNING: TRY AGAIN IN A MINUTE"
MSG_FAILED = "INSTALL FAILED: SEE /var/log/couchliteos/browser.log"
MSG_UNKNOWN = "UNKNOWN WEB BROWSER"


class BrowserFailed(Exception):
    """The install stopped; `message` is the ALL CAPS text for the launcher screen."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Log:
    """Append-only log; never raises."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    def __call__(self, text: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", errors="replace") as stream:
                stream.write(text.rstrip("\n") + "\n")
        except OSError:
            pass


def _free_bytes(path: pathlib.Path) -> int:
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize


@dataclasses.dataclass
class Env:
    """Everything the tool touches outside its own arguments, replaceable in tests."""

    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    free_bytes: Callable[[pathlib.Path], int] = _free_bytes
    root: pathlib.Path = pathlib.Path("/")
    run_dir: pathlib.Path = RUN_DIR
    euid: Callable[[], int] = os.geteuid
    log: Callable[[str], None] | None = None

    def note(self, text: str) -> None:
        if self.log:
            self.log(text)


class Status:
    """The JSON file the launcher polls: {"phase", "percent", "message", "browser"}."""

    def __init__(self, path: pathlib.Path, browser: str = "") -> None:
        self.path = pathlib.Path(path)
        self.browser = browser

    def set(self, phase: str, message: str, percent: int | None = None) -> None:
        if phase not in PHASES:
            raise ValueError(phase)
        if percent is not None:
            percent = max(0, min(100, int(percent)))
        text = json.dumps({"phase": phase, "percent": percent, "message": message, "browser": self.browser})
        try:  # best effort: a status that cannot be written must not stop the install
            write_atomic(self.path, text + "\n")
        except OSError:
            pass


def write_atomic(path: pathlib.Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


@contextlib.contextmanager
def acquire_lock(path: pathlib.Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise BrowserFailed(MSG_BUSY) from error
        yield


def sh(env: Env, argv: Iterable[object]) -> subprocess.CompletedProcess:
    """Run a command, capturing its output; every command and every failure goes to the log."""
    argv = [str(part) for part in argv]
    result = env.runner(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    env.note(f"$ {shlex.join(argv)} -> exit {result.returncode}")
    output = (result.stdout or "").strip() if isinstance(result.stdout, str) else ""
    if result.returncode != 0 and output:
        env.note(output[-4000:])
    return result


def apt(env: Env, *arguments: str) -> subprocess.CompletedProcess:
    return sh(env, ["apt-get", *APT_OPTIONS, *arguments])


# ------------------------------------------------------------------ what is installed


def browser(name: str) -> Browser:
    try:
        return BROWSERS[name.strip().lower()]
    except KeyError:
        raise BrowserFailed(MSG_UNKNOWN) from None


def installed_browsers(root: pathlib.Path = pathlib.Path("/")) -> list[Browser]:
    """The browsers whose program is on this box, Chrome first (web apps prefer it)."""
    order = (BROWSERS["chrome"], BROWSERS["firefox"])
    return [item for item in order if (root / item.binary.lstrip("/")).exists()]


def kiosk_command(url: str, root: pathlib.Path = pathlib.Path("/")) -> tuple[str, str] | None:
    """(command, arguments) that open `url` full screen as a web app, or None with no browser installed.

    Chrome when it is installed (it opens a second window for each web app), else Firefox.
    """
    for item in installed_browsers(root):
        if item.name == "chrome":
            return item.binary, shlex.join(["--ozone-platform=wayland", "--kiosk", "--no-first-run", url])
        return item.binary, shlex.join(["--kiosk", url])
    return None


# ------------------------------------------------------------------ checks


def refuse_no_network(env: Env) -> None:
    """No default route (IPv4 or IPv6) means no internet; apt would only time out."""
    for family in ("-4", "-6"):
        result = sh(env, ["ip", family, "route", "show", "default"])
        if result.returncode == 0 and (result.stdout or "").strip():
            return
    raise BrowserFailed(MSG_NETWORK)


def refuse_low_space(env: Env) -> None:
    try:
        free = env.free_bytes(env.root)
    except OSError:
        return  # unknown: let apt try, it says so itself when the disk is full
    if free < MIN_FREE:
        env.note(f"free space {free} bytes, need {MIN_FREE}")
        raise BrowserFailed(MSG_SPACE)


def update_lists(env: Env) -> None:
    # The image carries no package lists; an unreachable mirror is an error, not a warning.
    if apt(env, "update", "--error-on=any").returncode != 0:
        raise BrowserFailed(MSG_NETWORK)


# ------------------------------------------------------------------ actions


def install(env: Env, status: Status, item: Browser) -> None:
    status.browser = item.name
    status.set("checking", f"CHECKING BEFORE INSTALLING {item.label}...", 0)
    refuse_no_network(env)
    refuse_low_space(env)
    status.set("downloading", "GETTING THE LIST OF PACKAGES...", 10)
    update_lists(env)
    status.set("downloading", f"DOWNLOADING {item.label}...", 30)
    if apt(env, *INSTALL, "--download-only", item.package).returncode != 0:
        raise BrowserFailed(MSG_NETWORK)
    status.set("installing", f"INSTALLING {item.label}...", 70)
    if apt(env, *INSTALL, item.package).returncode != 0:
        raise BrowserFailed(MSG_FAILED)
    apt(env, "clean")  # the downloaded files are not needed again; on a live stick they use memory
    status.set("done", f"{item.label} IS INSTALLED", 100)


def remove(env: Env, status: Status, item: Browser) -> None:
    status.browser = item.name
    status.set("removing", f"REMOVING {item.label}...", 50)
    if apt(env, "remove", "-y", item.package).returncode != 0:
        raise BrowserFailed("COULD NOT REMOVE " + item.label)
    status.set("done", f"{item.label} IS REMOVED", 100)


def refresh(env: Env) -> None:
    """Install the installed browsers again; the marker goes only once that worked (else next boot tries again)."""
    marker = env.root / REFRESH_REL
    found = installed_browsers(env.root)
    if found:
        update_lists(env)
        if apt(env, *INSTALL, "--reinstall", *(item.package for item in found)).returncode != 0:
            raise BrowserFailed(MSG_FAILED)
        apt(env, "clean")
    marker.unlink(missing_ok=True)


def read_request(path: pathlib.Path) -> str:
    """The browser name the launcher asked for; the file is removed whatever it holds."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise BrowserFailed(MSG_UNKNOWN) from error
    try:
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(MAX_REQUEST + 1)
    finally:
        path.unlink(missing_ok=True)
    try:
        text = raw.decode("ascii") if len(raw) <= MAX_REQUEST else ""
    except UnicodeError:
        text = ""
    return browser(text).name


def _guarded(env: Env, status: Status | None, action: Callable[[], None]) -> int:
    try:
        action()
    except BrowserFailed as error:
        env.note(f"failed: {error.message}")
        if status is not None:
            status.set("failed", error.message)
        return 1
    except Exception:  # noqa: BLE001 - the screen gets plain words, the log gets the traceback
        env.note(traceback.format_exc())
        if status is not None:
            status.set("failed", MSG_FAILED)
        return 1
    return 0


def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    parser = argparse.ArgumentParser(prog="couchliteos-browser", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "remove"):
        commands.add_parser(name).add_argument("browser", choices=sorted(BROWSERS))
    commands.add_parser("request", help="install what the launcher asked for (the service)")
    commands.add_parser("refresh", help="install the installed browsers again after an OS update")
    args = parser.parse_args(argv)
    env = env or Env()
    if env.euid() != 0:
        print("couchliteos-browser must run as root", file=sys.stderr)
        return 2
    if env.log is None:
        env.log = Log(env.root / LOG_REL)
    os.environ.setdefault("DEBIAN_FRONTEND", "noninteractive")
    status = None if args.command == "refresh" else Status(env.run_dir / STATUS_NAME)

    def action() -> None:
        # The request goes first, even when another install holds the lock: the path unit
        # would otherwise start this service again the moment it ends.
        name = read_request(env.run_dir / REQUEST_NAME) if args.command == "request" else getattr(args, "browser", "")
        with acquire_lock(env.run_dir / LOCK_NAME):
            if args.command == "refresh":
                refresh(env)
            elif args.command == "remove":
                remove(env, status, browser(name))
            else:
                install(env, status, browser(name))

    return _guarded(env, status, action)


if __name__ == "__main__":
    sys.exit(main())
