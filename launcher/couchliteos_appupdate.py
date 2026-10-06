#!/usr/bin/python3
"""App updates between releases (root side): newer Moonlight and chiaki-ng, and Flatpaks.

Run by couchliteos-app-update.service: daily from its timer, or at once from its path unit
when the launcher (or couchliteos-run-app) leaves a request below /run/couchliteos.

    check          read the app manifest and write the status file; installs nothing
    run            the service: handle requests, then check and install (timer runs obey
                   [update] auto_apps), then `flatpak update`
    rollback NAME  go back to the previous version of an app (or to the image's copy)
    prune          drop downloaded copies the image now ships at the same or a newer version
    status         print the status file

The manifest (apps.json, written by tools/apps-manifest.sh from build/applications.lock) is
published with the project on GitHub: {"schema": 1, "apps": [{"name", "version", "url",
"sha256", "min_os", "size"?, "check_args"?}]}. Its addresses and every download must be on
GitHub over HTTPS, the same rule as the CouchLiteOS updater. A version is installed only when
it is newer than both the image's copy (/usr/share/couchliteos/applications.lock) and the
current download, when this CouchLiteOS is at least `min_os`, and when the owner has not rolled
back from it.

Layout, one folder per app below /var/lib/couchliteos/apps/<name>/:
    <version>/       the AppImage's files (unpacked, like the image's /opt/couchliteos/apps/<name>)
    current          symlink to the version couchliteos-run-app starts when it is newer than the image's
    previous         symlink to the version before it; exactly one is kept
    skip             a version the owner (or an automatic rollback) went back from

couchliteos-run-app counts starts that fail before the app is ready in
/var/lib/couchliteos/app-health/<name> ("<version> <failures>"); two in a row for the current
version roll it back automatically. The status file (/var/lib/couchliteos/app-update.json) is
what the SOFTWARE UPDATE screen shows; `rolled_back` lists rollbacks for its toast.

Standard library only; every system effect goes through Env so tests can use temp directories.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator

import couchliteos_busy as busy
import couchliteos_settings as settings
import couchliteos_update as update

# A manifest is published with every release from build/applications.lock; between releases
# a tested app version is published by replacing the latest release's apps.json. Old name
# second, as in couchliteos_update.API_URLS.
MANIFEST_URLS = (
    "https://github.com/Dudiebug/couchliteos/releases/latest/download/apps.json",
    "https://github.com/Dudiebug/moonlightos/releases/latest/download/apps.json",  # rename:keep
)
# The CouchLiteOS updater's check_url rule: only GitHub, only HTTPS.
TRUSTED_PREFIXES = ("https://github.com/", "https://api.github.com/")
APPS_DIR = pathlib.Path("/var/lib/couchliteos/apps")
HEALTH_DIR = pathlib.Path("/var/lib/couchliteos/app-health")
STATE = pathlib.Path("/var/lib/couchliteos/app-update.json")
CACHE_DIR = pathlib.Path("/var/cache/couchliteos/apps")
RUN_DIR = pathlib.Path("/run/couchliteos")
IMAGE_LOCK_REL = "usr/share/couchliteos/applications.lock"
VERSION_REL = "etc/couchliteos-version"
LOG_REL = "var/log/couchliteos/app-update.log"
LOCK_NAME = "app-update.lock"
INSTALL_REQUEST = "app-update-install"   # the owner asked: install even with auto_apps off
ROLLBACK_REQUEST = "app-rollback"        # the owner asked: roll back the app named inside
HEALTH_REQUEST = "app-rollback-check"    # couchliteos-run-app: an app failed to start twice
# The apps this updates, and their program inside the unpacked AppImage.
APPS = {"moonlight": "usr/bin/moonlight", "chiaki-ng": "usr/bin/chiaki"}
FAILED_STARTS = 2
MANIFEST_MAX = 64 * 1024
APP_MAX = 512 * 1024 * 1024
CHUNK = 1 << 20
TIMEOUT = 30
TEST_TIMEOUT = 60
USER_AGENT = "CouchLiteOS-app-update"
TEST_USER = "couchliteos"
ROLLBACK_HISTORY = 5
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SQUASHFS_MAGIC = b"hsqs"
# The `current` and `previous` links; the tests stand in for them where symlinks need privileges.
symlink = os.symlink
readlink = os.readlink

MSG_OK = "UP TO DATE"
MSG_INSTALLED = "APPS UPDATED"
MSG_AVAILABLE = "UPDATES AVAILABLE"
MSG_BUSY = "WAITING: THE BOX IS IN USE"
MSG_DISABLED = "AUTOMATIC APP UPDATES ARE OFF"
MSG_NETWORK = "COULD NOT REACH GITHUB: CHECK SETTINGS > NETWORK"
MSG_FAILED = "APP UPDATE FAILED: SEE /var/log/couchliteos/app-update.log"
MSG_UNSAFE = "THE APP FOLDER IS NOT SAFE TO USE"


class AppUpdateError(Exception):
    """An app update step failed; `message` is the ALL CAPS text for the screen."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ------------------------------------------------------------------ environment


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


class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub sends release downloads on to its CDN; that is fine as long as it stays HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(newurl, code, "REDIRECT AWAY FROM HTTPS REFUSED", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_test_run(env: "Env", app_dir: pathlib.Path, binary: pathlib.Path, args: tuple[str, ...]) -> bool:
    """The new copy must load: its libraries resolve (ldd), and `check_args` (if any) exit 0.

    Both run as the couchliteos user (setpriv), never as root."""
    environment = [f"APPDIR={app_dir}", f"LD_LIBRARY_PATH={app_dir / 'usr/lib'}", "QT_QPA_PLATFORM=offscreen"]
    prefix = ["setpriv", f"--reuid={TEST_USER}", f"--regid={TEST_USER}", "--init-groups", "--no-new-privs",
              "env", *environment]
    try:
        result = env.runner([*prefix, "ldd", str(binary)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, check=False, timeout=TEST_TIMEOUT)
        output = result.stdout if isinstance(result.stdout, str) else ""
        if result.returncode != 0 or "not found" in output:
            env.note(f"test run: ldd {binary} -> exit {result.returncode}\n{output.strip()[-4000:]}")
            return False
        if args:
            result = env.runner([*prefix, str(binary), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, check=False, timeout=TEST_TIMEOUT)
            if result.returncode != 0:
                env.note(f"test run: {binary} {' '.join(args)} -> exit {result.returncode}")
                return False
    except (OSError, subprocess.SubprocessError) as error:
        env.note(f"test run: {error}")
        return False
    return True


@dataclasses.dataclass
class Env:
    """Everything the app updater touches outside its own arguments, replaceable in tests."""

    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    opener: Callable[..., object] = dataclasses.field(
        default_factory=lambda: urllib.request.build_opener(HttpsOnlyRedirect).open)
    root: pathlib.Path = pathlib.Path("/")
    apps_dir: pathlib.Path = APPS_DIR
    health_dir: pathlib.Path = HEALTH_DIR
    cache_dir: pathlib.Path = CACHE_DIR
    run_dir: pathlib.Path = RUN_DIR
    state: pathlib.Path = STATE
    config: pathlib.Path = settings.CONFIG
    manifest_urls: tuple[str, ...] = MANIFEST_URLS
    clock: Callable[[], float] = time.time
    euid: Callable[[], int] = getattr(os, "geteuid", lambda: 0)
    owner_uid: int | None = 0  # who must own apps_dir; None skips the check (tests on other systems)
    busy: Callable[[pathlib.Path], str] = busy.reason
    test_run: Callable[..., bool] = _default_test_run
    flatpak: Callable[["Env"], dict] | None = None
    log: Callable[[str], None] | None = None

    def note(self, text: str) -> None:
        if self.log:
            self.log(text)


# ------------------------------------------------------------------ small helpers


def check_url(url: str) -> None:
    if not isinstance(url, str) or not url.startswith(TRUSTED_PREFIXES):
        raise AppUpdateError("THE APP ADDRESS IS NOT ON GITHUB")


def valid_version(text: object) -> bool:
    return isinstance(text, str) and update.parse_version(text) is not None and "/" not in text and text not in (".", "..")


def newer(candidate: str, than: str) -> bool:
    """`candidate` is a version and newer than `than`; anything beats no version at all."""
    if not valid_version(candidate):
        return False
    return not valid_version(than) or update.is_newer(candidate, than)


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
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def read_json(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def acquire_lock(path: pathlib.Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl  # noqa: PLC0415 - Linux only
    except ImportError:
        yield
        return
    with open(path, "a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise AppUpdateError("AN APP UPDATE IS ALREADY RUNNING") from error
        yield


def sh(env: Env, argv: Iterable[object]) -> subprocess.CompletedProcess:
    argv = [str(part) for part in argv]
    result = env.runner(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    env.note(f"$ {' '.join(argv)} -> exit {result.returncode}")
    output = result.stdout if isinstance(result.stdout, str) else ""
    if result.returncode != 0 and output.strip():
        env.note(output.strip()[-4000:])
    return result


# ------------------------------------------------------------------ manifest and versions


@dataclasses.dataclass(frozen=True)
class Entry:
    name: str
    version: str
    url: str
    sha256: str
    min_os: str = ""
    size: int | None = None
    check_args: tuple[str, ...] = ()


def parse_manifest(text: str, note: Callable[[str], None] = lambda _text: None) -> dict[str, Entry]:
    """{name: Entry} of the valid entries for apps this box knows; anything else is left out."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise AppUpdateError("THE APP LIST IS DAMAGED") from error
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("apps"), list):
        raise AppUpdateError("THE APP LIST IS DAMAGED")
    entries: dict[str, Entry] = {}
    for item in data["apps"]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name not in APPS:
            note(f"manifest: unknown app {name!r} left out")
            continue
        version, url, sha = item.get("version"), item.get("url"), item.get("sha256")
        min_os, size, check_args = item.get("min_os", ""), item.get("size"), item.get("check_args", [])
        problems = []
        if not valid_version(version):
            problems.append("version")
        if not isinstance(url, str) or not url.startswith(TRUSTED_PREFIXES):
            problems.append("url (not on GitHub)")
        if not isinstance(sha, str) or not SHA_RE.match(sha.lower()):
            problems.append("sha256")
        if min_os not in ("", None) and not valid_version(min_os):
            problems.append("min_os")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= APP_MAX):
            problems.append("size")
        if not isinstance(check_args, list) or not all(
            isinstance(arg, str) and re.fullmatch(r"--?[A-Za-z0-9-]{1,32}", arg) for arg in check_args
        ):
            problems.append("check_args")
        if problems:
            note(f"manifest: {name} left out, bad {', '.join(problems)}")
            continue
        entries[name] = Entry(name, version, url, sha.lower(), min_os or "", size, tuple(check_args))
    return entries


def fetch_manifest(env: Env) -> dict[str, Entry]:
    last: Exception | None = None
    for url in env.manifest_urls:
        check_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with env.opener(request, timeout=TIMEOUT) as response:
                body = response.read(MANIFEST_MAX + 1)
        except (OSError, ValueError) as error:  # URLError and HTTPError are OSErrors
            env.note(f"manifest {url}: {error}")
            last = error
            continue
        if len(body) > MANIFEST_MAX:
            raise AppUpdateError("THE APP LIST IS TOO LARGE")
        return parse_manifest(body.decode("utf-8", errors="replace"), env.note)
    raise AppUpdateError(MSG_NETWORK) from last


def image_versions(path: pathlib.Path) -> dict[str, str]:
    """{name: version} of build/applications.lock as installed into the image."""
    versions: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return versions
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("|")
        if len(fields) >= 2 and fields[0].strip() in APPS and valid_version(fields[1].strip()):
            versions[fields[0].strip()] = fields[1].strip()
    return versions


def os_version(env: Env) -> str:
    return update.installed_version([env.root / VERSION_REL])


# ------------------------------------------------------------------ the per-app folder


def link_version(link: pathlib.Path) -> str:
    """The version a `current`/`previous` symlink names, or "" when it is missing or odd."""
    try:
        target = readlink(link)
    except OSError:
        return ""
    name = pathlib.PurePosixPath(target.replace("\\", "/")).name
    return name if valid_version(name) and (link.parent / name).is_dir() else ""


def set_link(app_dir: pathlib.Path, name: str, version: str) -> None:
    """Point `name` at `version` atomically: a new symlink renamed over the old one."""
    temporary = app_dir / f".{name}.new"
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    symlink(version, temporary)
    os.replace(temporary, app_dir / name)


def drop_link(app_dir: pathlib.Path, name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        (app_dir / name).unlink()


def read_skip(app_dir: pathlib.Path) -> str:
    try:
        value = (app_dir / "skip").read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return ""
    return value if valid_version(value) else ""


def tidy(app_dir: pathlib.Path) -> None:
    """Keep only the versions `current` and `previous` name, and nothing half-made."""
    keep = {link_version(app_dir / "current"), link_version(app_dir / "previous")} - {""}
    for name in ("current", "previous"):
        if os.path.lexists(app_dir / name) and not link_version(app_dir / name):
            drop_link(app_dir, name)
    try:
        children = list(app_dir.iterdir())
    except OSError:
        return
    for child in children:
        if child.name in ("current", "previous", "skip") or child.name in keep:
            continue
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child, ignore_errors=True)


def activate(app_dir: pathlib.Path, version: str) -> None:
    """Make `version` current; the old current becomes the one previous version kept."""
    old = link_version(app_dir / "current")
    set_link(app_dir, "current", version)
    if old and old != version:
        set_link(app_dir, "previous", old)
    tidy(app_dir)


def rollback(app_dir: pathlib.Path) -> tuple[str, str]:
    """Go back one version: to `previous`, or to the image's copy when there is none.

    The version gone back from is deleted and remembered in `skip`, so it is not installed
    again; a newer version in the manifest is. Returns (from, to); to "" is the image's copy."""
    bad = link_version(app_dir / "current")
    if not bad:
        raise AppUpdateError("NOTHING TO ROLL BACK")
    good = link_version(app_dir / "previous")
    if good:
        set_link(app_dir, "current", good)
    else:
        drop_link(app_dir, "current")
    drop_link(app_dir, "previous")
    (app_dir / "skip").write_text(bad + "\n", encoding="ascii")
    tidy(app_dir)
    return bad, good


def prune(apps_dir: pathlib.Path, images: dict[str, str]) -> list[str]:
    """Drop downloaded versions the image ships at the same or a newer version.

    Run after a full update: the new image may carry the app the box had downloaded."""
    dropped = []
    for name in APPS:
        app_dir = apps_dir / name
        image = images.get(name, "")
        if not app_dir.is_dir() or app_dir.is_symlink() or not image:
            continue
        for link in ("current", "previous"):
            version = link_version(app_dir / link)
            if version and not update.is_newer(version, image):
                drop_link(app_dir, link)
                dropped.append(f"{name} {version}")
        skip = read_skip(app_dir)
        if skip and not update.is_newer(skip, image):
            (app_dir / "skip").unlink()
        tidy(app_dir)
    return dropped


def read_health(health_dir: pathlib.Path, name: str) -> tuple[str, int]:
    try:
        fields = (health_dir / name).read_text(encoding="ascii", errors="replace").split()
    except OSError:
        return "", 0
    if len(fields) != 2 or not valid_version(fields[0]) or not fields[1].isdigit():
        return "", 0
    return fields[0], int(fields[1])


# ------------------------------------------------------------------ download and unpack


def _request(url: str, start: int = 0) -> urllib.request.Request:
    headers = {"User-Agent": USER_AGENT}
    if start:
        headers["Range"] = f"bytes={start}-"
    return urllib.request.Request(url, headers=headers)


def _hash_file(path: pathlib.Path, digest) -> None:
    with open(path, "rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)


def download(entry: Entry, env: Env) -> pathlib.Path:
    """Fetch the AppImage to the cache (resuming a partial file) and return the verified file."""
    check_url(entry.url)
    env.cache_dir.mkdir(parents=True, exist_ok=True)
    final = env.cache_dir / f"{entry.name}-{entry.version}.AppImage"
    part = final.with_name(final.name + ".part")
    for path in sorted(env.cache_dir.glob(f"{entry.name}-*")):  # other versions only take up room
        if path not in (final, part):
            with contextlib.suppress(OSError):
                path.unlink()
    if final.exists():
        digest = hashlib.sha256()
        _hash_file(final, digest)
        if digest.hexdigest() == entry.sha256:
            return final
        final.unlink()
    offset = part.stat().st_size if part.exists() else 0
    if offset > (entry.size or APP_MAX):
        part.unlink()
        offset = 0
    digest = hashlib.sha256()
    if offset:
        _hash_file(part, digest)
    response = None
    for attempt in (1, 2):
        try:
            response = env.opener(_request(entry.url, offset), timeout=TIMEOUT)
        except urllib.error.HTTPError as error:
            if error.code == 416 and offset and attempt == 1:  # the partial file does not fit the file
                part.unlink(missing_ok=True)
                offset, digest = 0, hashlib.sha256()
                continue
            env.note(f"download {entry.url}: {error}")
            raise AppUpdateError("DOWNLOAD FAILED: CHECK THE NETWORK AND TRY AGAIN") from error
        except (OSError, ValueError) as error:
            env.note(f"download {entry.url}: {error}")
            raise AppUpdateError("DOWNLOAD FAILED: CHECK THE NETWORK AND TRY AGAIN") from error
        break
    done = offset
    with response:
        if offset and getattr(response, "status", 206) != 206:  # the server sent everything again
            offset = done = 0
            digest = hashlib.sha256()
        try:
            with open(part, "ab" if offset else "wb") as stream:
                while chunk := response.read(CHUNK):
                    stream.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if done > (entry.size or APP_MAX):
                        raise AppUpdateError("THE DOWNLOAD IS LARGER THAN EXPECTED")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            env.note(f"download interrupted: {error}")
            raise AppUpdateError("DOWNLOAD STOPPED: CHECK THE NETWORK AND TRY AGAIN") from error
    if entry.size is not None and done < entry.size:
        raise AppUpdateError("DOWNLOAD STOPPED: CHECK THE NETWORK AND TRY AGAIN")
    if digest.hexdigest() != entry.sha256:
        part.unlink(missing_ok=True)
        raise AppUpdateError("THE DOWNLOAD IS DAMAGED: ITS CHECKSUM DOES NOT MATCH")
    os.replace(part, final)
    return final


def squashfs_offset(path: pathlib.Path) -> int:
    """Where an AppImage's squashfs starts: right after its ELF runtime's section headers.

    Read from the ELF header instead of running `--appimage-offset`, so nothing downloaded is
    executed as root."""
    with open(path, "rb") as stream:
        header = stream.read(64)
        if len(header) < 52 or header[:4] != b"\x7fELF" or header[4] not in (1, 2) or header[5] not in (1, 2):
            raise AppUpdateError("THE DOWNLOAD IS NOT AN APPIMAGE")
        order = "<" if header[5] == 1 else ">"
        if header[4] == 2:
            shoff, = struct.unpack_from(order + "Q", header, 0x28)
            shentsize, shnum = struct.unpack_from(order + "HH", header, 0x3A)
        else:
            shoff, = struct.unpack_from(order + "I", header, 0x20)
            shentsize, shnum = struct.unpack_from(order + "HH", header, 0x2E)
        offset = shoff + shentsize * shnum
        stream.seek(offset)
        if stream.read(4) != SQUASHFS_MAGIC:
            raise AppUpdateError("THE DOWNLOAD IS NOT AN APPIMAGE")
    return offset


def unpack(appimage: pathlib.Path, destination: pathlib.Path, env: Env) -> None:
    offset = squashfs_offset(appimage)
    if destination.exists():
        shutil.rmtree(destination)
    result = sh(env, ["unsquashfs", "-quiet", "-no-xattrs", "-offset", offset, "-dest", destination, appimage])
    if result.returncode != 0:
        shutil.rmtree(destination, ignore_errors=True)
        raise AppUpdateError("THE APP COULD NOT BE UNPACKED")


def install(entry: Entry, env: Env) -> None:
    """Download, check, unpack, test-run, then swap `current` to the new version."""
    app_dir = env.apps_dir / entry.name
    app_dir.mkdir(mode=0o755, exist_ok=True)
    appimage = download(entry, env)
    staging = app_dir / f".{entry.version}.new"
    unpack(appimage, staging, env)
    binary = staging / APPS[entry.name]
    if not binary.is_file() or not os.access(binary, os.X_OK):
        shutil.rmtree(staging, ignore_errors=True)
        raise AppUpdateError("THE NEW APP VERSION HAS NO PROGRAM")
    target = app_dir / entry.version
    if target.exists() and entry.version not in (link_version(app_dir / "current"), link_version(app_dir / "previous")):
        shutil.rmtree(target)
    if target.exists():  # the version is already kept (as previous): use it again
        shutil.rmtree(staging, ignore_errors=True)
    else:
        os.replace(staging, target)
    if not env.test_run(env, target, target / APPS[entry.name], entry.check_args):
        if entry.version not in (link_version(app_dir / "current"), link_version(app_dir / "previous")):
            shutil.rmtree(target, ignore_errors=True)
        raise AppUpdateError("THE NEW APP VERSION DID NOT START")
    activate(app_dir, entry.version)
    with contextlib.suppress(OSError):
        (env.health_dir / entry.name).unlink()  # a new version starts with a clean count
    with contextlib.suppress(OSError):
        appimage.unlink()


# ------------------------------------------------------------------ status and the service


def check_apps_dir(env: Env) -> None:
    """The app folder is root's: refuse a symlink or anything another user could swap."""
    try:
        env.apps_dir.mkdir(mode=0o755, exist_ok=True)
        info = os.lstat(env.apps_dir)
    except OSError as error:
        raise AppUpdateError(MSG_UNSAFE) from error
    if not stat.S_ISDIR(info.st_mode):
        raise AppUpdateError(MSG_UNSAFE)
    if env.owner_uid is not None and (info.st_uid != env.owner_uid or info.st_mode & 0o022):
        raise AppUpdateError(MSG_UNSAFE)


def app_rows(env: Env, entries: dict[str, Entry] | None, images: dict[str, str], os_now: str) -> list[dict]:
    rows = []
    for name in APPS:
        app_dir = env.apps_dir / name
        image = images.get(name, "")
        current = link_version(app_dir / "current")
        previous = link_version(app_dir / "previous")
        active = current if newer(current, image) else image
        row = {"name": name, "image": image, "current": current, "previous": previous, "active": active,
               "available": "", "state": "uptodate", "message": ""}
        entry = (entries or {}).get(name)
        if entries is None:
            row["state"] = "unknown"
        elif entry and newer(entry.version, active):
            if entry.version == read_skip(app_dir):
                row["state"] = "skipped"
            elif entry.min_os and not (os_now and not update.is_newer(entry.min_os, os_now)):
                row["state"], row["message"] = "needs_os", f"NEEDS COUCHLITEOS {entry.min_os}"
                row["available"] = entry.version
            else:
                row["state"], row["available"] = "available", entry.version
        rows.append(row)
    return rows


def _record_rollback(status: dict, name: str, bad: str, good: str, automatic: bool, now: int) -> None:
    history = [item for item in status.get("rolled_back", []) if isinstance(item, dict)]
    history.append({"name": name, "from": bad, "to": good, "automatic": automatic, "at": now})
    status["rolled_back"] = history[-ROLLBACK_HISTORY:]


def auto_rollbacks(env: Env, status: dict, now: int) -> None:
    """Roll back a current version that failed to start FAILED_STARTS times in a row."""
    for name in APPS:
        app_dir = env.apps_dir / name
        current = link_version(app_dir / "current")
        version, failures = read_health(env.health_dir, name)
        if current and version == current and failures >= FAILED_STARTS:
            bad, good = rollback(app_dir)
            env.note(f"{name} {bad} failed to start {failures} times; rolled back to {good or 'the image copy'}")
            _record_rollback(status, name, bad, good, True, now)
            with contextlib.suppress(OSError):
                (env.health_dir / name).unlink()


def flatpak_update(env: Env) -> dict:
    if env.flatpak is not None:
        return env.flatpak(env)
    if not shutil.which("flatpak"):
        return {"result": "none", "message": ""}
    result = sh(env, ["flatpak", "update", "--noninteractive", "--system", "-y"])
    if result.returncode != 0:
        return {"result": "failed", "message": "FLATPAK UPDATE FAILED"}
    return {"result": "ok", "message": ""}


def run(env: Env, *, command: str = "run", manual: bool | None = None, target: str = "") -> int:
    """One pass of `command` (run, check, rollback, prune). Writes the status file; returns the exit status."""
    previous = read_json(env.state)
    now = int(env.clock())
    status = {
        "result": "ok", "message": MSG_OK, "attempted_at": now,
        "checked_at": previous.get("checked_at", 0) if isinstance(previous.get("checked_at"), int) else 0,
        "apps": [], "flatpak": previous.get("flatpak", {"result": "none", "message": ""}),
        "rolled_back": previous.get("rolled_back", []) if isinstance(previous.get("rolled_back"), list) else [],
    }
    images = image_versions(env.root / IMAGE_LOCK_REL)
    os_now = os_version(env)
    entries: dict[str, Entry] | None = None
    outcomes: dict[str, tuple[str, str]] = {}  # name: (state, message) of an install that failed
    code = 0
    install_request = env.run_dir / INSTALL_REQUEST
    rollback_request = env.run_dir / ROLLBACK_REQUEST
    health_only = False
    if command == "run":
        if manual is None:
            manual = install_request.exists()
        install_request.unlink(missing_ok=True)
        health_only = (env.run_dir / HEALTH_REQUEST).exists() and not manual
        (env.run_dir / HEALTH_REQUEST).unlink(missing_ok=True)
        try:
            target = rollback_request.read_text(encoding="ascii", errors="replace").strip()
        except OSError:
            target = ""
        rollback_request.unlink(missing_ok=True)
    try:
        check_apps_dir(env)
        dropped = prune(env.apps_dir, images)
        if dropped:
            env.note(f"the image now ships {', '.join(dropped)}: downloaded copies dropped")
        auto_rollbacks(env, status, now)
        if target:
            if target not in APPS:
                raise AppUpdateError("UNKNOWN APP")
            bad, good = rollback(env.apps_dir / target)
            env.note(f"{target}: rolled back from {bad} to {good or 'the image copy'}")
            _record_rollback(status, target, bad, good, False, now)
            status["message"] = "ROLLED BACK"
        if command in ("rollback", "prune") or (command == "run" and (target or health_only) and not manual):
            status["result"] = "ok"
        elif command == "run" and not manual and not settings.get_bool(
                settings.read_section("update", env.config), "auto_apps", True):
            status["result"], status["message"] = "disabled", MSG_DISABLED
        elif command == "run" and (reason := env.busy(env.run_dir)):
            env.note(f"box busy ({reason}); trying again on the next run")
            status["result"], status["message"] = "busy", MSG_BUSY
        else:
            entries = fetch_manifest(env)
            status["checked_at"] = now
            rows = app_rows(env, entries, images, os_now)
            if command == "check":
                pending = [row for row in rows if row["state"] == "available"]
                status["message"] = MSG_AVAILABLE if pending else MSG_OK
            else:
                installed = failed = 0
                for row in rows:
                    if row["state"] != "available":
                        continue
                    if reason := env.busy(env.run_dir):  # a stream started: stop between apps
                        env.note(f"box busy ({reason}); the other apps wait for the next run")
                        status["result"], status["message"] = "busy", MSG_BUSY
                        break
                    try:
                        install(entries[row["name"]], env)
                        installed += 1
                        env.note(f"{row['name']} {row['available']} installed")
                    except AppUpdateError as error:
                        failed += 1
                        env.note(f"{row['name']} {row['available']}: {error.message}")
                        outcomes[row["name"]] = ("failed", error.message)
                if status["result"] != "busy":
                    status["flatpak"] = flatpak_update(env)
                    if failed:
                        status["result"], status["message"], code = "failed", MSG_FAILED, 1
                    elif status["flatpak"].get("result") == "failed":
                        status["result"], status["message"], code = "failed", status["flatpak"]["message"], 1
                    else:
                        status["message"] = MSG_INSTALLED if installed else MSG_OK
    except AppUpdateError as error:
        env.note(f"app update: {error.message}")
        status["result"], status["message"], code = "failed", error.message, 1
    except Exception:  # noqa: BLE001 - the screen gets plain words, the log gets the traceback
        env.note(traceback.format_exc())
        status["result"], status["message"], code = "failed", MSG_FAILED, 1
    rows = app_rows(env, entries, images, os_now)
    if entries is None and isinstance(previous.get("apps"), list):  # keep what the last check knew
        known = {row.get("name"): row for row in previous["apps"] if isinstance(row, dict)}
        for row in rows:
            old = known.get(row["name"], {})
            if old.get("available") and newer(old["available"], row["active"]):
                row["available"], row["state"] = old["available"], old.get("state", "available")
    for row in rows:
        if row["name"] in outcomes:
            row["state"], row["message"] = outcomes[row["name"]]
    status["apps"] = rows
    try:
        write_json(env.state, status)
    except OSError as error:
        env.note(f"status file: {error}")
    return code


def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    parser = argparse.ArgumentParser(prog="couchliteos-app-update", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="read the app manifest; install nothing")
    commands.add_parser("run", help="handle requests, then check and install (the service)")
    back = commands.add_parser("rollback", help="go back to the previous version of an app")
    back.add_argument("name", choices=sorted(APPS))
    commands.add_parser("prune", help="drop downloaded copies the image supersedes")
    commands.add_parser("status", help="print the status file")
    args = parser.parse_args(argv)
    env = env or Env()
    if args.command == "status":
        print(json.dumps(read_json(env.state), indent=2, sort_keys=True))
        return 0
    if env.euid() != 0:
        print("app updates must run as root", file=sys.stderr)
        return 2
    if env.log is None:
        env.log = Log(env.root / LOG_REL)
    try:
        with acquire_lock(env.run_dir / LOCK_NAME):
            return run(env, command=args.command, target=getattr(args, "name", ""))
    except AppUpdateError as error:  # another run holds the lock: leave its status alone
        print(error.message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
