#!/usr/bin/python3
"""OS security fixes between releases (root side): apt-get update, then unattended-upgrade.

Run by couchliteos-security-update.service: daily from its timer, or at once when the
launcher leaves /run/couchliteos/security-update-install. The origins and the held packages
are in /etc/apt/apt.conf.d/52couchliteos-unattended (build/unattended-upgrades.sh): Debian
security for trixie, Google Chrome and Tailscale only, the build's pinned packages held, and
never an automatic restart.

    run       one check-and-apply pass (the service)
    status    print the status file

A timer run is skipped when [update] auto_security is off; a run the owner asked for is not.
Every run is skipped while the box is in use (couchliteos_busy) and on a live system whose
/usr is not kept by persistence. The result goes to /var/lib/couchliteos/security-update.json:
{"result", "message", "checked_at", "attempted_at", "upgraded", "restart_needed",
"restart_packages"}; restart_needed mirrors /run/reboot-required, and the box never restarts
on its own.

Standard library only; every system effect goes through Env so tests can use temp directories.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable

import couchliteos_busy as busy
import couchliteos_settings as settings
import couchliteos_update as update

RUN_DIR = pathlib.Path("/run/couchliteos")
STATE = pathlib.Path("/var/lib/couchliteos/security-update.json")
LOG_REL = "var/log/couchliteos/security-update.log"
REQUEST_NAME = "security-update-install"
RESULTS = ("ok", "failed", "busy", "disabled", "live")
UPGRADED_RE = re.compile(r"Packages that will be upgraded: (.*)$", re.MULTILINE)
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*(:[a-z0-9]+)?$")
APT_ENV = {"DEBIAN_FRONTEND": "noninteractive", "APT_LISTCHANGES_FRONTEND": "none"}

MSG_OK = "UP TO DATE"
MSG_FIXED = "SECURITY FIXES INSTALLED"
MSG_RESTART = "RESTART TO FINISH THE SECURITY FIXES"
MSG_BUSY = "WAITING: THE BOX IS IN USE"
MSG_DISABLED = "AUTOMATIC SECURITY UPDATES ARE OFF"
MSG_LIVE = "SECURITY FIXES COME WITH THE NEXT LIVE USB UPDATE"
MSG_NETWORK = "COULD NOT REACH THE UPDATE SERVERS: CHECK SETTINGS > NETWORK"
MSG_FAILED = "SECURITY UPDATE FAILED: SEE /var/log/couchliteos/security-update.log"


class Log:
    """Append-only log; never raises."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    def __call__(self, text: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", errors="replace") as stream:
                stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            pass


@dataclasses.dataclass
class Env:
    """Everything a run touches outside its own arguments, replaceable in tests."""

    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    root: pathlib.Path = pathlib.Path("/")
    run_dir: pathlib.Path = RUN_DIR
    state: pathlib.Path = STATE
    config: pathlib.Path = settings.CONFIG
    clock: Callable[[], float] = time.time
    euid: Callable[[], int] = getattr(os, "geteuid", lambda: 0)
    busy: Callable[[pathlib.Path], str] = busy.reason
    log: Callable[[str], None] | None = None

    def note(self, text: str) -> None:
        if self.log:
            self.log(text)


def write_json(path: pathlib.Path, data: dict) -> None:
    """Readers (the launcher) see the old or the new file, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_json(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def auto_enabled(config: pathlib.Path) -> bool:
    return settings.get_bool(settings.read_section("update", config), "auto_security", True)


def usr_persistent(root: pathlib.Path) -> bool:
    """On a live system: does a persistence volume keep the whole root (`/ union`) or /usr?"""
    for conf in sorted((root / "run/live/persistence").glob("*/persistence.conf")):
        try:
            lines = conf.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if fields and fields[0] in ("/", "/usr") and any("union" in option.split(",") for option in fields[1:]):
                return True
    return False


def live_without_usr(root: pathlib.Path) -> bool:
    return update.is_live(root / "run/live/medium", root / "proc/cmdline") and not usr_persistent(root)


def restart_state(root: pathlib.Path) -> tuple[bool, list[str]]:
    needed = (root / "run/reboot-required").exists()
    try:
        names = (root / "run/reboot-required.pkgs").read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        names = []
    return needed, sorted({name for name in names if PACKAGE_RE.match(name)})


def upgraded_packages(output: str) -> list[str]:
    names: set[str] = set()
    for match in UPGRADED_RE.finditer(output or ""):
        names.update(name for name in match.group(1).split() if PACKAGE_RE.match(name))
    return sorted(names)


def sh(env: Env, argv: list[str]) -> subprocess.CompletedProcess:
    result = env.runner(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
                        env={**os.environ, **APT_ENV})
    env.note(f"$ {' '.join(argv)} -> exit {result.returncode}")
    output = result.stdout if isinstance(result.stdout, str) else ""
    if output.strip():
        env.note(output.strip()[-8000:])
    return result


def run(env: Env, *, manual: bool | None = None) -> int:
    """One pass. Returns the service's exit status: 1 only when apt itself failed."""
    request = env.run_dir / REQUEST_NAME
    if manual is None:
        manual = request.exists()
    request.unlink(missing_ok=True)
    previous = read_json(env.state)
    now = int(env.clock())
    status = {
        "result": "ok",
        "message": MSG_OK,
        "checked_at": previous.get("checked_at", 0) if isinstance(previous.get("checked_at"), int) else 0,
        "attempted_at": now,
        "upgraded": [],
        "restart_needed": False,
        "restart_packages": [],
    }

    def finish(result: str, message: str, code: int = 0) -> int:
        status["result"], status["message"] = result, message
        status["restart_needed"], status["restart_packages"] = restart_state(env.root)
        if status["restart_needed"] and result == "ok":
            status["message"] = MSG_RESTART
        env.note(f"security update: {result}: {status['message']}")
        try:
            write_json(env.state, status)
        except OSError as error:
            env.note(f"status file: {error}")
        return code

    try:
        if not manual and not auto_enabled(env.config):
            return finish("disabled", MSG_DISABLED)
        if live_without_usr(env.root):
            return finish("live", MSG_LIVE)
        if reason := env.busy(env.run_dir):
            env.note(f"box busy ({reason}); trying again on the next run")
            return finish("busy", MSG_BUSY)
        if sh(env, ["apt-get", "-q", "update"]).returncode != 0:
            return finish("failed", MSG_NETWORK, 1)
        if reason := env.busy(env.run_dir):  # a stream may have started during the list download
            env.note(f"box busy ({reason}); trying again on the next run")
            return finish("busy", MSG_BUSY)
        result = sh(env, ["unattended-upgrade", "-v"])
        status["checked_at"] = now
        status["upgraded"] = upgraded_packages(result.stdout if isinstance(result.stdout, str) else "")
        if result.returncode != 0:
            return finish("failed", MSG_FAILED, 1)
        return finish("ok", MSG_FIXED if status["upgraded"] else MSG_OK)
    except Exception:  # noqa: BLE001 - the screen gets plain words, the log gets the traceback
        env.note(traceback.format_exc())
        return finish("failed", MSG_FAILED, 1)


def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    parser = argparse.ArgumentParser(prog="couchliteos-security-update", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="check for and apply security fixes (the service)")
    commands.add_parser("status", help="print the status file")
    args = parser.parse_args(argv)
    env = env or Env()
    if args.command == "status":
        print(json.dumps(read_json(env.state), indent=2, sort_keys=True))
        return 0
    if env.euid() != 0:
        print("security updates must run as root", file=sys.stderr)
        return 2
    if env.log is None:
        env.log = Log(env.root / LOG_REL)
    return run(env)


if __name__ == "__main__":
    sys.exit(main())
