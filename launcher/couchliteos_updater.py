#!/usr/bin/python3
"""CouchLiteOS updater (root side): download a release, then replace the system files in place.

Installed as /usr/libexec/couchliteos-updater. The launcher (user couchliteos) only touches
request files below /run/couchliteos; couchliteos-update.service runs `run` as root.

Root never works by path inside a directory that user owns (/run/couchliteos, /var/lib/couchliteos,
/var/log/couchliteos): mount points, filter files and the lock are in root's own WORK_DIR
(/run/couchliteos-update, checked by couchliteos_safefile.root_dir), the log is in
/var/log/couchliteos-update, and the files root leaves for the launcher (the status, installs.json,
config.ini, whatsnew-seen) are written through a descriptor of their directory (safefile.write_atomic).

    run [--no-snapshot]                       check, download, verify, save, install, reboot
    apply-iso PATH [--force] [--no-reboot] [--no-snapshot]
                                              install from an ISO file or device (offline updates)
    apply-root TARGET --status FILE [--force] [--snapshot-done] [--no-snapshot]
                                              the install step itself
    delete-snapshot                           remove the saved previous version (Settings)
    request-restore                           ask for a restore at the next start, then restart (Settings)
    restore                                   put the saved previous version back, then restart (boot)
    find-installs                             live USB: list the installed systems on the disks
    apply-disk DEVICE|--found [--force]       live USB: update the installed system on DEVICE

Before anything is written, `run` and `apply-iso` save the running system as the one snapshot
(couchliteos_snapshot) unless --no-snapshot is given or config.ini says [update] snapshot = off.
`apply-iso` then passes --snapshot-done to the new image's `apply-root`. An older updater (0.2.x)
does not know that flag and passes nothing, so `apply-root` saves TARGET itself first: the update
from 0.2.x to this version can be undone too.

`apply-iso` mounts the ISO's squashfs and runs the NEW image's copy of this program with
`apply-root /mnt` inside a chroot of it (the running disk is bind-mounted at /mnt), so the logic
that does the install is always the newest one. `apply-root` is a stable interface forever.
Progress goes to a small JSON status file the launcher polls.

`restore` works the same way the other way round: it mounts the saved image and runs THIS program's
`apply-root /mnt --restore` inside a chroot of it, so the newest logic puts the old files back.

Standard library only; every system effect goes through Env so tests can use temp directories.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import fnmatch
import hashlib
import json
import math
import os
import pathlib
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator

import couchliteos_snapshot as snapshot
import couchliteos_browser as browser
import couchliteos_safefile as safefile
import couchliteos_update as update

RUN_DIR = pathlib.Path("/run/couchliteos")
WORK_DIR = pathlib.Path("/run/couchliteos-update")  # root's own: mount points, filter files, the lock
CACHE_DIR = pathlib.Path("/var/cache/couchliteos/update")
STATUS_NAME = "update-status.json"
CANCEL_NAME = "update-cancel"
LOCK_NAME = "update.lock"
LOG_REL = "var/log/couchliteos-update/update.log"
MANIFEST_REL = "var/lib/couchliteos-update/etc-manifest"
UPDATER_REL = "usr/libexec/couchliteos-updater"
VERSION_REL = "etc/couchliteos-version"
PROFILE_REL = "usr/share/couchliteos/profile.conf"

PHASES = ("checking", "downloading", "verifying", "saving", "installing", "restarting", "failed", "cancelled", "uptodate")
GIB = 1 << 30
SPACE_MARGIN = 4 * GIB      # free space needed on top of the download
SPACE_FOR_INSTALL = 3 * GIB  # free space needed on the box to copy the new files in
CHUNK = 1 << 20
TIMEOUT = 30                # seconds without data before a download gives up
API_TIMEOUT = 10
SUMS_MAX = 64 * 1024
TRUSTED_PREFIXES = ("https://github.com/", "https://api.github.com/")
RSYNC_OK = (0, 24)          # 24: a source file vanished while copying (a log, a socket)
USER_AGENT = "CouchLiteOS-updater"

MSG_NETWORK = "COULD NOT REACH GITHUB: CHECK SETTINGS > NETWORK"
MSG_LIVE = "UPDATES NEED COUCHLITEOS INSTALLED TO A DISK"
MSG_LAYOUT = "UNSUPPORTED DISK LAYOUT"
MSG_OTHER_BOX = "THIS ISO IS FOR A DIFFERENT KIND OF BOX"
MSG_GENERIC = "UPDATE FAILED: SEE /var/log/couchliteos-update/update.log"
MSG_INSTALL = "INSTALL FAILED: SEE /var/log/couchliteos-update/update.log"
MSG_BOOT_MENU = (
    "INSTALL FAILED WHILE SETTING UP THE BOOT MENU. IF THE BOX WILL NOT START, "
    "REINSTALL FROM THE ISO."
)

# What the installer (GRUB, the maintenance user, locale, network) and the owner wrote into /etc:
# the updater leaves these alone. Patterns are relative to /etc, in rsync's anchored syntax.
ETC_KEEP = (
    "/fstab", "/crypttab", "/hostname", "/hosts", "/machine-id", "/adjtime", "/localtime", "/timezone",
    "/resolv.conf", "/default/grub", "/default/locale", "/default/keyboard", "/default/console-setup",
    "/locale.gen", "/apt/sources.list", "/initramfs-tools/conf.d/resume",
    "/NetworkManager/system-connections/", "/ssh/ssh_host_*",
    "/passwd", "/group", "/shadow", "/gshadow", "/passwd-", "/group-", "/shadow-", "/gshadow-",
    "/subuid", "/subgid", "/sudoers.d/", "/.pwd.lock",
    # Written or edited on the box by the owner (wiki: USB-IP, scripts/couchliteos-tailscale).
    "/couchliteos/tailscale-auth.key", "/couchliteos/usbip-allowlist.conf", "/couchliteos-usbip-client.conf",
    # Changes last, once everything else worked, so a failed update can be tried again.
    "/couchliteos-version",
)
SYSTEM_EXCLUDES = (
    "/dev/", "/proc/", "/sys/", "/run/", "/tmp/", "/mnt/", "/media/", "/lost+found", "/boot/efi/", "/boot/grub/",
    "/boot/couchliteos-previous/",  # the snapshot's boot copy (couchliteos_snapshot)
    "/home/", "/root/", "/usr/local/", "/swapfile", "/etc/", "/var/",
    "/usr/lib/locale/locale-archive",  # generated on the box by locale-gen
)
VAR_EXCLUDES = (
    "/lib/couchliteos/", "/lib/bluetooth/", "/lib/NetworkManager/", "/lib/tailscale/",
    "/lib/systemd/random-seed", "/lib/systemd/timers", "/lib/systemd/backlight", "/lib/systemd/rfkill",
    "/lib/systemd/timesync", "/lib/systemd/coredump", "/lib/systemd/pstore", "/lib/systemd/linger",
    "/log/", "/cache/", "/tmp/", "/spool/", "/mail/", "/backups/", "/lib/dhcp/", "/lib/alsa/",
    "/lib/upower/", "/lib/private/", "/lib/dbus/machine-id", "/lib/couchliteos-update/", "/lib/couchliteos-apps/",
    "/lib/dpkg/",  # has its own run with delete and the protect filter
)
RSYNC_BASE = ("rsync", "-aHAX", "--numeric-ids", "--delay-updates")
STATE_REL = "var/lib/couchliteos"   # the launcher's settings, pairings and home: a restore brings them back
WHATSNEW_REL = "var/lib/couchliteos/whatsnew-seen"
RESTORE_NAME = "restore"  # below WORK_DIR
HELD_REL = "var/lib/couchliteos-update/restore.squashfs"
MSG_NO_SAVED = "THERE IS NO SAVED VERSION TO RESTORE, OR IT IS DAMAGED"
MSG_RESTORE = "RESTORE FAILED: SEE /var/log/couchliteos-update/update.log"
MSG_RESTORE_RETRY = "THE RESTORE STOPPED PART WAY. THE BOX TRIES AGAIN AT THE NEXT START."
MSG_RESTORE_GAVE_UP = (
    "THE RESTORE FAILED TWICE AND WILL NOT BE TRIED AGAIN. THE BOX MAY NEED REINSTALLING FROM THE ISO, "
    "OR CHOOSE THE RESTORE ENTRY IN THE BOOT MENU."
)
MSG_NO_ROOM_OLD_CALLER = "NOT ENOUGH FREE SPACE TO SAVE THE CURRENT VERSION: UPDATING WITHOUT A SAVED COPY"
RESTORE_ATTEMPTS = 2        # restores that stopped part way before the request is dropped
RESTORE_FAILED_PAUSE = 30   # seconds the boot screen shows why a restore failed
REMAP_DIRS = ("var/lib", "var/log", "var/cache", "var/spool", "home", "root")
ACCOUNT_FILES = ("passwd", "group", "shadow", "gshadow")


class UpdateFailed(Exception):
    """The update stopped; `message` is the ALL CAPS text for the launcher screen."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NoRoomToSave(UpdateFailed):
    """Too little free space to save the box first (couchliteos_snapshot.NoRoom)."""


class RestoreIncomplete(UpdateFailed):
    """The restore stopped after it began writing the box: it may be half old, half new."""


class Cancelled(Exception):
    """The owner cancelled during the download."""


# ------------------------------------------------------------------ environment, status, log


class Log:
    """Append-only update log in root's own directory, never through a symlink; never raises."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    def __call__(self, text: str) -> None:
        try:
            safefile.append_line(self.path, f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}")
        except OSError:
            pass


class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub sends asset downloads on to its CDN; that is fine as long as it stays HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(newurl, code, "REDIRECT AWAY FROM HTTPS REFUSED", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(HttpsOnlyRedirect)


def _free_bytes(path: pathlib.Path) -> int:
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize


@dataclasses.dataclass
class Env:
    """Everything the updater touches outside its own arguments, replaceable in tests."""

    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    popen: Callable[..., subprocess.Popen] = subprocess.Popen  # commands whose progress is followed
    lchown: Callable[[str, int, int], None] = os.lchown
    chown: Callable[[str, int, int], None] = os.chown
    free_bytes: Callable[[pathlib.Path], int] = _free_bytes
    opener: Callable[..., object] = dataclasses.field(default_factory=lambda: build_opener().open)
    root: pathlib.Path = pathlib.Path("/")  # the running box: version, profile and live markers live below it
    run_dir: pathlib.Path = RUN_DIR
    cache_dir: pathlib.Path = CACHE_DIR
    clock: Callable[[], float] = time.monotonic
    euid: Callable[[], int] = os.geteuid
    log: Callable[[str], None] | None = None
    work_dir: pathlib.Path = WORK_DIR
    wall_clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep

    def note(self, text: str) -> None:
        if self.log:
            self.log(text)


def own_dir(env: Env, *names: str) -> pathlib.Path:
    """WORK_DIR (or a directory below it), made or checked as root's own (0700, no symlink).

    Mount points and work files never go below /run/couchliteos, which the launcher's user owns.
    """
    try:
        path = safefile.root_dir(env.work_dir, os.geteuid())
        for name in names:
            path = safefile.root_dir(path / name, os.geteuid())
    except OSError as error:
        env.note(f"work directory refused: {error}")
        raise UpdateFailed(MSG_GENERIC) from error
    return path


class Status:
    """The JSON file the launcher polls: {"phase", "percent", "message", "version"}.

    With `echo` each new message is also printed (a restore at boot shows its progress that way).
    """

    def __init__(self, path: pathlib.Path, version: str = "", echo: Callable[[str], None] | None = None) -> None:
        self.path = pathlib.Path(path)
        self.version = version
        self.echo = echo
        self._said = ""

    def set(self, phase: str, message: str, percent: int | None = None) -> None:
        if phase not in PHASES:
            raise ValueError(phase)
        if percent is not None:
            percent = max(0, min(100, int(percent)))
        text = json.dumps({"phase": phase, "percent": percent, "message": message, "version": self.version})
        if self.echo and message != self._said:
            self._said = message
            with contextlib.suppress(Exception):
                self.echo(message)
        try:  # best effort: a status that cannot be written must not stop the update
            write_atomic(self.path, text + "\n", 0o644)
        except OSError:
            pass


def read_status(path: pathlib.Path) -> dict:
    try:
        data = json.loads(snapshot._read_plain(path))  # never through a symlink
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


write_atomic = safefile.write_atomic  # readers see the old or the new file; never through a symlink


@contextlib.contextmanager
def acquire_lock(path: pathlib.Path) -> Iterator[None]:
    """`path` is in root's own directory (own_dir); the file is never opened through a symlink."""
    try:
        stream = safefile.open_lock(path)
    except OSError as error:
        raise UpdateFailed(f"{MSG_GENERIC} ({error.strerror or error})") from error
    with stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise UpdateFailed("AN UPDATE IS ALREADY RUNNING") from error
        yield


def sh(env: Env, argv: Iterable[object], log: Callable[[str], None] | None = None) -> subprocess.CompletedProcess:
    """Run a command, capturing its output; every command and every failure goes to the log."""
    argv = [str(part) for part in argv]
    log = log or env.note
    result = env.runner(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    log(f"$ {' '.join(argv)} -> exit {result.returncode}")
    output = (result.stdout or "").strip() if isinstance(result.stdout, str) else ""
    if result.returncode != 0 and output:
        log(output[-4000:])
    return result


# ------------------------------------------------------------------ refusals


def refuse_live(env: Env) -> None:
    if update.is_live(env.root / "run/live/medium", env.root / "proc/cmdline"):
        raise UpdateFailed(MSG_LIVE)


def refuse_layout(env: Env) -> None:
    """/boot, /usr and /var must live on the root file system: the update replaces them together."""
    for directory in ("/boot", "/usr", "/var"):
        result = sh(env, ["findmnt", "-n", "-o", "TARGET", "--target", directory])
        if result.returncode != 0 or (result.stdout or "").strip() != "/":
            raise UpdateFailed(MSG_LAYOUT)


# ------------------------------------------------------------------ downloading


def check_url(url: str) -> None:
    if not url.startswith(TRUSTED_PREFIXES):
        raise UpdateFailed("THE UPDATE ADDRESS IS NOT ON GITHUB")


def parse_sums(text: str) -> dict[str, str]:
    """`sha256sum` output (text or binary mode lines) as {file name: lowercase hex}."""
    sums = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64}) [ *](.+)$", line.strip())
        if match:
            sums[match.group(2).removeprefix("./")] = match.group(1).lower()
    return sums


def expected_sha(sums: dict[str, str], name: str) -> str:
    if name not in sums:
        raise UpdateFailed("THE CHECKSUM FILE HAS NO ENTRY FOR THIS UPDATE")
    return sums[name]


def _request(url: str, current: str = "", start: int = 0) -> urllib.request.Request:
    headers = {"User-Agent": f"{USER_AGENT}/{current or 'unknown'}"}
    if start:
        headers["Range"] = f"bytes={start}-"
    return urllib.request.Request(url, headers=headers)


def fetch_sums(asset: update.Asset, env: Env) -> dict[str, str]:
    check_url(asset.url)
    try:
        with env.opener(_request(asset.url), timeout=TIMEOUT) as response:
            body = response.read(SUMS_MAX + 1)
    except (OSError, ValueError) as error:  # URLError and HTTPError are OSErrors
        env.note(f"checksum file: {error}")
        raise UpdateFailed(MSG_NETWORK) from error
    if len(body) > SUMS_MAX:
        raise UpdateFailed("THE CHECKSUM FILE IS TOO LARGE")
    return parse_sums(body.decode("utf-8", errors="replace"))


def _hash_file(path: pathlib.Path, digest) -> None:
    with open(path, "rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)


def clean_cache(cache_dir: pathlib.Path) -> None:
    for path in _listdir(cache_dir):
        if path.name.endswith((".iso", ".iso.part")):
            with contextlib.suppress(OSError):
                path.unlink()


def _listdir(path: pathlib.Path) -> list[pathlib.Path]:
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


def download_iso(asset: update.Asset, expected: str, env: Env, status: Status) -> pathlib.Path:
    """Fetch the asset to the cache (resuming a partial file) and return the verified ISO."""
    check_url(asset.url)
    cache = env.cache_dir
    cache.mkdir(parents=True, exist_ok=True)
    final = cache / asset.name
    part = cache / (asset.name + ".part")
    cancel = env.run_dir / CANCEL_NAME
    for path in _listdir(cache):  # files of other versions only take up room
        if path.name.endswith((".iso", ".iso.part")) and path not in (final, part):
            with contextlib.suppress(OSError):
                path.unlink()
    if final.exists():
        digest = hashlib.sha256()
        _hash_file(final, digest)
        if digest.hexdigest() == expected.lower():
            status.set("verifying", "CHECKING THE DOWNLOAD...")
            return final
        final.unlink()
    offset = part.stat().st_size if part.exists() else 0
    if offset > asset.size:
        part.unlink()
        offset = 0
    need = asset.size - offset + SPACE_MARGIN
    if env.free_bytes(cache) < need:
        raise UpdateFailed(f"NOT ENOUGH FREE SPACE: NEED {math.ceil(need / GIB)} GB")

    total_mb = asset.size // 1_000_000
    digest = hashlib.sha256()
    if offset:
        _hash_file(part, digest)
    last = float("-inf")
    for attempt in (1, 2):
        try:
            response = env.opener(_request(asset.url, start=offset), timeout=TIMEOUT)
        except urllib.error.HTTPError as error:
            if error.code == 416 and offset and attempt == 1:  # the partial file does not fit the file
                part.unlink(missing_ok=True)
                offset, digest = 0, hashlib.sha256()
                continue
            env.note(f"download: {error}")
            raise UpdateFailed("DOWNLOAD FAILED: CHECK THE NETWORK AND TRY AGAIN") from error
        except (OSError, ValueError) as error:
            env.note(f"download: {error}")
            raise UpdateFailed("DOWNLOAD FAILED: CHECK THE NETWORK AND TRY AGAIN") from error
        break
    done = offset
    with response:
        if offset and getattr(response, "status", 206) != 206:  # the server sent everything again
            offset = done = 0
            digest = hashlib.sha256()
        try:
            with open(part, "ab" if offset else "wb") as stream:
                while True:
                    now = env.clock()
                    if now - last >= 1.0:
                        last = now
                        percent = done * 100 // asset.size if asset.size else 0
                        status.set("downloading", f"DOWNLOADING: {done // 1_000_000} OF {total_mb} MB", percent)
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    stream.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if cancel.exists():
                        stream.flush()
                        raise Cancelled
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            env.note(f"download interrupted: {error}")
            raise UpdateFailed("DOWNLOAD STOPPED: CHECK THE NETWORK AND TRY AGAIN") from error
    if done < asset.size:
        raise UpdateFailed("DOWNLOAD STOPPED: CHECK THE NETWORK AND TRY AGAIN")
    status.set("verifying", "CHECKING THE DOWNLOAD...")
    if digest.hexdigest() != expected.lower():
        part.unlink(missing_ok=True)
        raise UpdateFailed("THE DOWNLOAD IS DAMAGED. TRY AGAIN.")
    os.replace(part, final)
    return final


# ------------------------------------------------------------------ dpkg


def parse_dpkg_status(text: str) -> dict[tuple[str, str], str]:
    """Installed packages of a dpkg status file: {(Package, Architecture): stanza text}."""
    packages: dict[tuple[str, str], str] = {}
    for block in re.split(r"\n(?:[ \t]*\n)+", text):
        stanza = block.strip("\n")
        fields = {}
        for line in stanza.splitlines():
            if line[:1] not in (" ", "\t") and ": " in line:
                key, _, value = line.partition(": ")
                fields.setdefault(key, value.strip())
        if fields.get("Status") == "install ok installed" and "Package" in fields:
            packages[(fields["Package"], fields.get("Architecture", ""))] = stanza
    return packages


def extra_packages(box: dict, image: dict) -> dict:
    """What the box has that the image does not (GRUB, shim, firmware added by the installer)."""
    return {key: value for key, value in box.items() if key not in image}


def info_files(info: pathlib.Path, package: str, architecture: str) -> list[str]:
    """Names in /var/lib/dpkg/info that belong to the package (`pkg.ext` or `pkg:arch.ext`).

    `stem.` followed by an extension with no dot keeps python3 from claiming python3.13's files.
    """
    stems = (package, f"{package}:{architecture}")
    try:
        names = sorted(os.listdir(info))
    except OSError:
        return []
    return [
        name for name in names
        if any(name.startswith(stem + ".") and "." not in name[len(stem) + 1:] for stem in stems)
    ]


def protected_paths(target: pathlib.Path, image: pathlib.Path, extras: Iterable[tuple[str, str]]) -> list[str]:
    """Absolute paths rsync must not delete: the extras' files, dpkg's records of them, and the
    directories above them that only the box has."""
    info = target / "var/lib/dpkg/info"
    protected: set[str] = set()
    for package, architecture in extras:
        for name in info_files(info, package, architecture):
            protected.add(f"/var/lib/dpkg/info/{name}")
            if not name.endswith(".list"):
                continue
            try:
                lines = (info / name).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.startswith("/"):
                    continue
                path = posixpath.normpath(line)
                if path in ("/", "."):
                    continue
                full = target / path.lstrip("/")
                if full.is_symlink() or not full.is_dir():
                    protected.add(path)
                parent = posixpath.dirname(path)
                while parent not in ("/", ""):
                    if not os.path.lexists(image / parent.lstrip("/")):
                        protected.add(parent)
                    parent = posixpath.dirname(parent)
    return sorted(protected)


def filter_rules(paths: Iterable[str], root: str = "/") -> list[str]:
    """`P /path` rsync filter lines for the absolute paths below `root`, written relative to it.

    rsync only reads backslash escapes in a pattern that has a wildcard, so the backslash is doubled
    only then. A path with a line break (or trailing blank) cannot be written as a rule and is skipped.
    """
    base = root.rstrip("/")
    rules = []
    for path in paths:
        if "\n" in path or "\r" in path:
            continue
        if base:
            if not path.startswith(base + "/"):
                continue
            path = path[len(base):]
        if path in ("", "/") or path.endswith((" ", "\t")):
            continue
        if re.search(r"[*?\[]", path):
            path = re.sub(r"([*?\[])", r"\\\1", path.replace("\\", "\\\\"))
        rules.append(f"P {path}")
    return rules


def merge_dpkg_status(image_text: str, extras: Iterable[str]) -> str:
    text = image_text if image_text.endswith("\n") or not image_text else image_text + "\n"
    extras = list(extras)
    if not extras:
        return image_text
    return text + "\n" + "\n\n".join(extras) + "\n"


# ------------------------------------------------------------------ accounts


@dataclasses.dataclass
class Accounts:
    files: dict[str, str]
    uid_map: dict[int, int]
    gid_map: dict[int, int]
    skipped: list[str]
    shadow_gid: int | None


def _rows(text: str, minimum: int, numeric: tuple[int, ...] = ()) -> list[list[str]]:
    rows = []
    for line in (text or "").splitlines():
        fields = line.rstrip("\r").split(":")
        if line.strip() and not line.startswith("#") and len(fields) >= minimum and all(
            fields[index].isdigit() for index in numeric
        ):
            rows.append(fields)
    return rows


def _members(row: list[str]) -> list[str]:
    return [name for name in row[3].split(",") if name]


def merge_accounts(image: dict[str, str], target: dict[str, str], renames: dict[str, str] | None = None) -> Accounts:
    """The image's users and groups, plus everything only the box has (maintenance user, its groups).

    Passwords stay as the box has them. A box-only account whose id the image now uses is skipped.
    `renames` maps an old group name to the image's new one: its files get the new gid (apply-disk).
    """
    renames = renames or {}
    skipped: list[str] = []
    image_users, box_users = _rows(image["passwd"], 7, (2,)), _rows(target["passwd"], 7, (2,))
    users = [list(row) for row in image_users]
    names = {row[0]: int(row[2]) for row in users}
    taken = set(names.values())
    uid_map: dict[int, int] = {}
    for row in box_users:
        name, uid = row[0], int(row[2])
        if name in names:
            if names[name] != uid:
                uid_map.setdefault(uid, names[name])
        elif uid in taken:
            skipped.append(f"user {name} (uid {uid} is taken)")
        else:
            users.append(row)
            names[name] = uid
            taken.add(uid)

    image_groups, box_groups = _rows(image["group"], 4, (2,)), _rows(target["group"], 4, (2,))
    box_by_name = {row[0]: row for row in box_groups}
    groups = [list(row) for row in image_groups]
    gids = {row[0]: int(row[2]) for row in groups}
    taken_gids = set(gids.values())
    gid_map: dict[int, int] = {}
    for row in groups:
        if row[0] in box_by_name:
            old = box_by_name[row[0]]
            if int(old[2]) != int(row[2]):
                gid_map.setdefault(int(old[2]), int(row[2]))
            members = _members(row) + [name for name in _members(old) if name not in _members(row)]
            row[3] = ",".join(name for name in members if name in names)
    for row in box_groups:
        gid = int(row[2])
        if row[0] in gids:
            continue
        renamed = renames.get(row[0])
        if renamed in gids:
            gid_map.setdefault(gid, gids[renamed])
            new_row = next(group for group in groups if group[0] == renamed)
            members = _members(new_row) + [name for name in _members(row) if name not in _members(new_row)]
            new_row[3] = ",".join(name for name in members if name in names)
            continue
        if gid in taken_gids:
            skipped.append(f"group {row[0]} (gid {gid} is taken)")
            continue
        row = list(row)
        row[3] = ",".join(name for name in _members(row) if name in names)
        groups.append(row)
        gids[row[0]] = gid
        taken_gids.add(gid)

    def by_name(text: str) -> dict[str, str]:
        return {line.split(":")[0]: line.rstrip("\r") for line in (text or "").splitlines() if ":" in line}

    image_shadow, box_shadow = by_name(image["shadow"]), by_name(target["shadow"])
    shadow = [
        box_shadow.get(row[0]) or image_shadow.get(row[0]) or f"{row[0]}:!:0:0:99999:7:::" for row in users
    ]
    image_gshadow, box_gshadow = by_name(image["gshadow"]), by_name(target["gshadow"])
    gshadow = []
    for row in groups:
        fields = (box_gshadow.get(row[0]) or image_gshadow.get(row[0]) or f"{row[0]}:!::").split(":")
        fields = (fields + ["", "", "", ""])[:4]
        fields[3] = row[3]
        gshadow.append(":".join(fields))

    def text(lines: Iterable[str]) -> str:
        lines = list(lines)
        return "\n".join(lines) + "\n" if lines else ""

    return Accounts(
        files={
            "passwd": text(":".join(row) for row in users),
            "group": text(":".join(row) for row in groups),
            "shadow": text(shadow),
            "gshadow": text(gshadow),
        },
        uid_map=uid_map, gid_map=gid_map, skipped=skipped, shadow_gid=gids.get("shadow"),
    )


def read_accounts(root: pathlib.Path) -> dict[str, str]:
    accounts = {}
    for name in ACCOUNT_FILES:
        try:
            accounts[name] = (root / "etc" / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            accounts[name] = ""
    return accounts


def write_accounts(target: pathlib.Path, accounts: Accounts, chown: Callable[[str, int, int], None] = os.chown) -> None:
    for name in ACCOUNT_FILES:
        secret = name in ("shadow", "gshadow")
        owner = (0, accounts.shadow_gid) if secret and accounts.shadow_gid is not None else None
        write_atomic(target / "etc" / name, accounts.files[name], 0o640 if secret else 0o644, owner, chown)


def _entries(top: pathlib.Path) -> Iterator[str]:
    yield str(top)
    for directory, directories, files in os.walk(top):
        for name in directories + files:
            yield os.path.join(directory, name)


def remap_ids(
    target: pathlib.Path, uid_map: dict[int, int], gid_map: dict[int, int],
    lchown: Callable[[str, int, int], None] = os.lchown, dirs: Iterable[str] = REMAP_DIRS,
) -> int:
    """Give files below the state directories the new id of a user or group whose id changed."""
    if not uid_map and not gid_map:
        return 0
    changed = 0
    for rel in dirs:
        top = target / rel
        if top.is_symlink() or not top.is_dir():
            continue
        for path in _entries(top):
            try:
                info = os.lstat(path)
            except OSError:
                continue
            uid, gid = uid_map.get(info.st_uid, info.st_uid), gid_map.get(info.st_gid, info.st_gid)
            if (uid, gid) == (info.st_uid, info.st_gid):
                continue
            lchown(path, uid, gid)
            changed += 1
            if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o6000:
                os.chmod(path, stat.S_IMODE(info.st_mode))  # chown dropped setuid/setgid
    return changed


# ------------------------------------------------------------------ /etc


def etc_kept(rel: str) -> bool:
    """True for a path (relative to /etc) the update never overwrites or deletes."""
    for pattern in ETC_KEEP:
        pattern = pattern.lstrip("/")
        if pattern.endswith("/"):
            if rel.startswith(pattern):
                return True
        elif fnmatch.fnmatchcase(rel, pattern):
            return True
    return False


def _files_below(top: pathlib.Path) -> list[str]:
    """Paths relative to `top` of everything that is not a real directory (symlinks included)."""
    found = []
    for directory, directories, files in os.walk(top):
        for name in directories:
            if os.path.islink(os.path.join(directory, name)):
                found.append(os.path.relpath(os.path.join(directory, name), top))
        found += [os.path.relpath(os.path.join(directory, name), top) for name in files]
    return sorted(found)


def etc_manifest(image_etc: pathlib.Path) -> list[str]:
    return _files_below(image_etc)


def read_manifest(path: pathlib.Path) -> set[str] | None:
    try:
        return {line for line in path.read_text(encoding="utf-8").splitlines() if line}
    except OSError:
        return None


def write_manifest(path: pathlib.Path, rels: Iterable[str]) -> None:
    write_atomic(path, "".join(f"{rel}\n" for rel in sorted(set(rels))), 0o644)


def _safe_rel(rel: str) -> bool:
    return bool(rel) and not rel.startswith("/") and ".." not in pathlib.PurePosixPath(rel).parts


def stale_etc_files(target_etc: pathlib.Path, image_etc: pathlib.Path, manifest: set[str] | None) -> list[str]:
    """Files an earlier update put in /etc that the new image no longer has.

    Without a manifest (the first update) only what the image certainly owns is considered: the
    /etc/couchliteos directory and our own couchliteos-* units, never what the installer or the
    owner enabled next to them.
    """
    if manifest is None:
        candidates = [
            rel for rel in _files_below(target_etc)
            if rel.startswith("couchliteos/")
            or (rel.startswith("systemd/system/") and posixpath.basename(rel).startswith("couchliteos-"))
        ]
    else:
        candidates = [rel for rel in manifest if _safe_rel(rel) and os.path.lexists(target_etc / rel)]
    return sorted(
        rel for rel in candidates
        if _safe_rel(rel) and not os.path.lexists(image_etc / rel) and not etc_kept(rel)
    )


def remove_stale(target_etc: pathlib.Path, rels: Iterable[str]) -> None:
    for rel in rels:
        if not _safe_rel(rel):
            continue
        path = target_etc / rel
        with contextlib.suppress(OSError):
            path.unlink()
        parent = path.parent
        while parent != target_etc:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


# ------------------------------------------------------------------ rsync


def _rsync_dir(base: pathlib.Path, rel: str) -> str:
    return str(base).rstrip("/") + rel


def rsync_steps(
    image: pathlib.Path, target: pathlib.Path, filter_system: pathlib.Path, filter_dpkg: pathlib.Path,
    restore: bool = False,
) -> list[tuple[str, list[str]]]:
    """The four rsync runs, source = the image, destination = the box. Nothing deletes except
    `system` and `dpkg`, and those only after the copy and never what the protect filter names.

    A restore adds a fifth, `state`: the launcher's settings as they were when the image was saved
    (deleting what came later). The snapshot is not below it (couchliteos_snapshot.SNAP_REL)."""

    def excludes(patterns: Iterable[str]) -> list[str]:
        return [f"--exclude={pattern}" for pattern in patterns]

    base = list(RSYNC_BASE)
    return [
        ("system", [*base, "--delete-after", f"--filter=merge {filter_system}", *excludes(SYSTEM_EXCLUDES),
                    _rsync_dir(image, "/"), _rsync_dir(target, "/")]),
        ("etc", [*base, *excludes(ETC_KEEP), _rsync_dir(image, "/etc/"), _rsync_dir(target, "/etc/")]),
        ("var", [*base, *excludes(VAR_EXCLUDES), _rsync_dir(image, "/var/"), _rsync_dir(target, "/var/")]),
        ("dpkg", [*base, "--delete-after", f"--filter=merge {filter_dpkg}", "--exclude=/status",
                  _rsync_dir(image, "/var/lib/dpkg/"), _rsync_dir(target, "/var/lib/dpkg/")]),
    ] + ([
        ("state", [*base, "--delete-after",
                   _rsync_dir(image, f"/{STATE_REL}/"), _rsync_dir(target, f"/{STATE_REL}/")]),
    ] if restore else [])


# ------------------------------------------------------------------ apply-root


def _read_version(path: pathlib.Path) -> str:
    return update.installed_version([path])


def preflight(target: pathlib.Path, image: pathlib.Path, env: Env, force: bool, legacy: bool = False) -> str:
    """Refuse before anything is written; returns the version being installed.

    `legacy` (apply-disk) also takes a box of 0.2.0 or older: MoonlightOS names, or no version file.
    """
    if legacy:
        found = box_identity(target)
        if found is None:
            raise UpdateFailed(MSG_NO_INSTALL)
        installed, theirs = found
        if legacy_conflicts(target):
            raise UpdateFailed(MSG_DISK_LAYOUT)
    else:
        installed = _read_version(target / VERSION_REL)
        if not installed:
            raise UpdateFailed("THIS DISK IS NOT A COUCHLITEOS BOX")
        theirs = update.read_profile(target / PROFILE_REL).get("PROFILE_NAME")
    new = _read_version(image / VERSION_REL)
    if not new:
        raise UpdateFailed("THIS UPDATE HAS NO VERSION")
    ours = update.read_profile(image / PROFILE_REL).get("PROFILE_NAME")
    if not ours or ours != theirs:
        raise UpdateFailed(MSG_OTHER_BOX)
    if not force and installed and not update.is_newer(new, installed):
        raise UpdateFailed("THIS UPDATE IS NOT NEWER THAN THE BOX")
    if env.free_bytes(target) < SPACE_FOR_INSTALL:
        raise UpdateFailed(f"NOT ENOUGH FREE SPACE: NEED {SPACE_FOR_INSTALL // GIB} GB")
    return new


@contextlib.contextmanager
def chroot_mounts(env: Env, root: pathlib.Path, log: Callable[[str], None]) -> Iterator[None]:
    """proc, sys, dev and run inside `root`, unmounted again (newest first) even after a failure.

    The bind mounts are made slaves so that taking them down can never reach the real /dev or /sys.
    """
    with contextlib.ExitStack() as stack:
        for argv, mountpoint, slave in (
            (["mount", "-t", "proc", "proc", f"{root}/proc"], f"{root}/proc", False),
            (["mount", "--rbind", "/sys", f"{root}/sys"], f"{root}/sys", True),
            (["mount", "--rbind", "/dev", f"{root}/dev"], f"{root}/dev", True),
            (["mount", "--bind", "/run", f"{root}/run"], f"{root}/run", True),
        ):
            if sh(env, argv, log).returncode != 0:
                raise UpdateFailed(MSG_BOOT_MENU)
            stack.callback(unmount, env, mountpoint, log)
            if slave and sh(env, ["mount", "--make-rslave", mountpoint], log).returncode != 0:
                raise UpdateFailed(MSG_BOOT_MENU)
        yield


def unmount(env: Env, mountpoint: str, log: Callable[[str], None] | None = None) -> None:
    log = log or env.note
    if sh(env, ["umount", "-R", mountpoint], log).returncode != 0:
        if sh(env, ["umount", "-R", "-l", mountpoint], log).returncode != 0:
            log(f"could not unmount {mountpoint}")


def apply_root(
    target: pathlib.Path, status: Status, env: Env, *, force: bool = False, image: pathlib.Path = pathlib.Path("/"),
    save_first: bool = True, save: Callable[..., None] | None = None,
    legacy: bool = False, grub_install: list[str] | None = None, restore: bool = False,
) -> None:
    """Replace the system files of the box at `target` with those of the image at `image`.

    With `save_first` (the caller did not save the box: an updater older than 0.3.0), `target` is
    saved as the snapshot before anything of it is written. `legacy` and `grub_install` are for
    apply-disk: a box of 0.2.0 or older is moved to the new names first (`migrate_legacy`), and
    `grub_install` (a command run in the target) puts the boot loader back too.

    With `restore` the image is the saved snapshot: any version goes, the launcher's state comes
    back too (the `state` step), and the box ends up as it was, so nothing only the box has is
    kept (the snapshot already holds what the installer added). /etc files in ETC_KEEP stay as they
    are now. Then the automatic update check pauses and What's New counts as seen. A restore is
    never `legacy`: the snapshot was saved by 0.3.0 or later, so the box already has the new names.
    """
    if restore and legacy:
        raise ValueError("a restore is never a legacy update")
    target, image = pathlib.Path(target), pathlib.Path(image)
    new_version = preflight(target, image, env, force or restore, legacy)
    log = Log(target / LOG_REL)
    status.version = new_version
    kind = "restore" if restore else "update"
    log(f"{kind} to {new_version} from {_read_version(target / VERSION_REL)} (force={force}, save={save_first})")
    if save_first:
        # Inside the new image the default log is on its read-only squashfs: note into the box's log.
        try:
            (save or save_snapshot)(dataclasses.replace(env, log=log), status, target)
        except NoRoomToSave as error:
            # The caller is an updater older than 0.3.0: its box has no SAVE BEFORE UPDATE switch to
            # turn off, so refusing here would leave it unable to ever update. Go on without a copy.
            log(f"snapshot: {error.message}")
            log(MSG_NO_ROOM_OLD_CALLER)
            status.set("installing", MSG_NO_ROOM_OLD_CALLER)

    def step(message: str, percent: int) -> None:
        log(message)
        status.set("installing", message, percent)

    step("PREPARING THE UPDATE...", 2)
    if legacy:
        migrate_legacy(target, log, env.chown)
    image_packages = parse_dpkg_status(_read(image / "var/lib/dpkg/status"))
    box_packages = parse_dpkg_status(_read(target / "var/lib/dpkg/status"))
    extras = {} if restore else extra_packages(box_packages, image_packages)
    log(f"packages only the box has: {len(extras)}")
    protected = protected_paths(target, image, extras)
    work = pathlib.Path(tempfile.mkdtemp(prefix="update-filter-", dir=own_dir(env)))
    try:
        filter_system, filter_dpkg = work / "system.rules", work / "dpkg.rules"
        outside = [path for path in protected if not path.startswith(("/etc/", "/var/"))]
        filter_system.write_text("".join(f"{rule}\n" for rule in filter_rules(outside)), encoding="utf-8")
        filter_dpkg.write_text(
            "".join(f"{rule}\n" for rule in filter_rules(protected, "/var/lib/dpkg")), encoding="utf-8")

        # Accounts first, while every file still carries the ids the box had; the copy below then
        # gives image-owned files the new ids, and running this again after a failure changes nothing.
        accounts = merge_accounts(read_accounts(image), read_accounts(target), LEGACY_GROUPS if legacy else None)
        for line in accounts.skipped:
            log(f"accounts: skipped {line}")
        step("KEEPING YOUR ACCOUNTS...", 5)
        log(f"id remap: users {accounts.uid_map} groups {accounts.gid_map}")
        remap_ids(target, accounts.uid_map, accounts.gid_map, lchown=env.lchown)
        write_accounts(target, accounts, chown=env.chown)

        messages = {
            "system": ("COPYING SYSTEM FILES...", 10), "etc": ("UPDATING SETTINGS FILES...", 60),
            "var": ("UPDATING SYSTEM DATA...", 70), "dpkg": ("UPDATING THE PACKAGE LIST...", 75),
            "state": ("RESTORING YOUR SETTINGS...", 80),
        }
        for name, argv in rsync_steps(image, target, filter_system, filter_dpkg, restore):
            step(*messages[name])
            if sh(env, argv, log).returncode not in RSYNC_OK:
                raise UpdateFailed(f"INSTALL FAILED WHILE COPYING FILES ({name.upper()}). {MSG_INSTALL[15:]}")
            if name == "etc":
                _tidy_etc(target, image, log)
                if legacy:
                    remove_legacy_units(target, log)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    write_atomic(
        target / "var/lib/dpkg/status",
        merge_dpkg_status(_read(image / "var/lib/dpkg/status"), list(extras.values())), 0o644)
    _boot_menu(target, env, log, step, grub_install)
    write_atomic(target / VERSION_REL, new_version + "\n", 0o644)
    if restore:
        try:
            _after_restore(target, new_version, env, log)
        except OSError as error:  # the files are back; only the courtesies are missing
            log(f"restore: could not pause the update check or mark What's New: {error}")
    # Browsers are not in the image (the box installed them), so they are kept as extras;
    # couchliteos-browser-refresh.service installs them again on the new system at the next start.
    packages = {item.package for item in browser.BROWSERS.values()}
    if any(name in packages for name, _architecture in extras):
        write_atomic(target / browser.REFRESH_REL, "", 0o644)
        log("browsers installed: refresh marked for the next start")
    step("SAVING TO THE DISK...", 98)
    sh(env, ["sync"], log)
    log(f"{kind} to {new_version} done")
    status.set("restarting", "RESTARTING...", 100)


def _after_restore(target: pathlib.Path, version: str, env: Env, log: Callable[[str], None]) -> None:
    """Pause the automatic update check for a week (it would offer the same update at once) and
    mark the restored version's What's New as seen. Both files are the launcher's own, in its own
    directory: written through a descriptor of it (safefile.write_atomic), owned by that user."""
    until = int(env.wall_clock() + update.PAUSE_SECONDS)
    snapshot.set_option("paused_until", str(until), target)
    state = os.lstat(target / STATE_REL)
    write_atomic(target / WHATSNEW_REL, f"{version}\n", 0o644, (state.st_uid, state.st_gid), env.chown)
    log(f"restore: update check paused until {time.strftime('%Y-%m-%d %H:%M', time.gmtime(until))} UTC")


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tidy_etc(target: pathlib.Path, image: pathlib.Path, log: Callable[[str], None]) -> None:
    """Delete /etc files the new image dropped, then remember the image's /etc for next time."""
    manifest_path = target / MANIFEST_REL
    stale = stale_etc_files(target / "etc", image / "etc", read_manifest(manifest_path))
    for rel in stale:
        log(f"removing stale /etc/{rel}")
    remove_stale(target / "etc", stale)
    write_manifest(manifest_path, etc_manifest(image / "etc"))


def _boot_menu(target: pathlib.Path, env: Env, log: Callable[[str], None], step,
               grub_install: list[str] | None = None) -> None:
    kernels = sorted(path.name.removeprefix("vmlinuz-") for path in (target / "boot").glob("vmlinuz-*"))
    if not kernels:
        log("no kernel in /boot after the copy")
        raise UpdateFailed(MSG_BOOT_MENU)
    step("SETTING UP THE BOOT MENU...", 85)
    with chroot_mounts(env, target, log):
        commands = [["ldconfig"]]
        for kernel in kernels:
            mode = "-u" if (target / "boot" / f"initrd.img-{kernel}").exists() else "-c"
            commands.append(["update-initramfs", mode, "-k", kernel])
        if grub_install:  # apply-disk on a box whose GRUB predates the boot check
            commands.append(grub_install)
        commands.append(["update-grub"])
        for command in commands:
            if sh(env, ["chroot", str(target), *command], log).returncode != 0:
                raise UpdateFailed(MSG_BOOT_MENU)


# ------------------------------------------------------------------ the snapshot before an update


def save_snapshot(env: Env, status: Status, root: pathlib.Path | None = None) -> None:
    """Save `root` (default: this box) as the one snapshot, unless config.ini turned that off.

    Runs before anything of the box is written, so a failure leaves the box as it was.
    """
    root = env.root if root is None else pathlib.Path(root)
    if not snapshot.enabled(root):
        env.note("snapshot: off in config.ini")
        return
    try:
        snapshot.create(env, status, root)
    except snapshot.NoRoom as error:
        raise NoRoomToSave(error.message) from error
    except snapshot.SnapshotFailed as error:
        raise UpdateFailed(error.message) from error


# ------------------------------------------------------------------ apply-iso (the handover)


def _mount(env: Env, stack: contextlib.ExitStack, argv: list[str], mountpoint: pathlib.Path, message: str,
           rslave: bool = False) -> None:
    """Mount and register the unmount on `stack`; a failure raises UpdateFailed(message)."""
    if sh(env, ["mount", *argv, str(mountpoint)]).returncode != 0:
        raise UpdateFailed(message)
    stack.callback(unmount, env, str(mountpoint))
    if rslave and sh(env, ["mount", "--make-rslave", str(mountpoint)]).returncode != 0:
        raise UpdateFailed(message)


def _enter(env: Env, stack: contextlib.ExitStack, new: pathlib.Path, message: str) -> None:
    """Make the image mounted at `new` a chroot whose /mnt is this box."""
    _mount(env, stack, ["--bind", "/"], new / "mnt", message, rslave=True)
    _mount(env, stack, ["-t", "proc", "proc"], new / "proc", message)
    _mount(env, stack, ["--rbind", "/sys"], new / "sys", message, rslave=True)
    _mount(env, stack, ["--rbind", "/dev"], new / "dev", message, rslave=True)
    _mount(env, stack, ["--bind", "/run"], new / "run", message, rslave=True)


def is_block_device(path: pathlib.Path) -> bool:
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def handles_snapshot_done(updater: pathlib.Path) -> bool:
    """Whether an image's updater takes --snapshot-done; an older one (a downgrade) would refuse it."""
    try:
        return b"--snapshot-done" in updater.read_bytes()
    except OSError:
        return False


def apply_iso(
    source: pathlib.Path, env: Env, status: Status, *, force: bool = False, save_first: bool = True,
    save: Callable[[Env, Status], None] | None = None,
) -> None:
    """Open the ISO, save this box (unless `save_first` is off), then run the ISO's own updater
    against it (see the module docstring)."""
    status.set("installing", "INSTALLING THE UPDATE... KEEP THE BOX PLUGGED IN.")
    refuse_live(env)
    refuse_layout(env)
    iso_dir, new = own_dir(env, "update", "iso"), own_dir(env, "update", "root")

    with contextlib.ExitStack() as stack:
        def mount(argv: list[str], mountpoint: pathlib.Path) -> None:
            _mount(env, stack, argv, mountpoint, "COULD NOT OPEN THE UPDATE FILE")

        mount(["-o", "ro" if is_block_device(source) else "ro,loop", str(source)], iso_dir)
        mount(["-t", "squashfs", "-o", "ro,loop", str(iso_dir / "live/filesystem.squashfs")], new)
        if not (new / UPDATER_REL).exists():
            raise UpdateFailed("THIS ISO CANNOT UPDATE A BOX")
        if update.read_profile(new / PROFILE_REL).get("PROFILE_NAME") != \
                update.read_profile(env.root / PROFILE_REL).get("PROFILE_NAME"):
            raise UpdateFailed(MSG_OTHER_BOX)
        if save_first:
            (save or save_snapshot)(env, status)
            status.set("installing", "INSTALLING THE UPDATE... KEEP THE BOX PLUGGED IN.")
        _enter(env, stack, new, "COULD NOT OPEN THE UPDATE FILE")
        argv = ["chroot", str(new), f"/{UPDATER_REL}", "apply-root", "/mnt", "--status", str(status.path)]
        if force:
            argv.append("--force")
        if handles_snapshot_done(new / UPDATER_REL):  # saved above (or skipped on purpose): not again
            argv.append("--snapshot-done")
        if sh(env, argv).returncode != 0:
            reported = read_status(status.path)
            message = reported.get("message") if reported.get("phase") == "failed" else None
            raise UpdateFailed(message if isinstance(message, str) and message else MSG_INSTALL)


# ------------------------------------------------------------------ restore (the saved previous version)

# This program and the modules it imports, copied out so the snapshot's python3 runs today's logic.
TOOL = (pathlib.Path(__file__), pathlib.Path(update.__file__), pathlib.Path(snapshot.__file__),
        pathlib.Path(browser.__file__), pathlib.Path(safefile.__file__))


def load_squashfs(env: Env) -> None:
    """squashfs and loop for the mount. Booted from the GRUB entry, the kernel is the saved one and
    this disk may no longer have its modules: then the boot copy's are loaded, in their order."""
    if sh(env, ["modprobe", "-a", *snapshot.MODULES]).returncode == 0:
        return
    for path in snapshot.load_order(env.root):
        sh(env, ["insmod", path])  # one already loaded answers "File exists": the mount says if it worked


def request_restore(env: Env, status: Status) -> None:
    """Settings > RESTORE PREVIOUS VERSION: leave the request for the boot service (then restart)."""
    refuse_live(env)
    saved = snapshot.info(env.root)
    if saved is None or not snapshot.request(env, env.root):
        raise UpdateFailed(MSG_NO_SAVED)
    status.version = saved.version
    sh(env, ["sync"])
    status.set("restarting", "RESTARTING TO RESTORE THE SAVED VERSION...", 100)


def restore(env: Env, status: Status) -> str:
    """Put the saved version back over this box; returns its version. The caller restarts.

    The image is mounted from root's own hard link of it (snapshot.hold), and today's updater is
    copied to root's own directory in /run (WORK_DIR/restore), which the chroot sees through its
    bind of /run. A failure once that chroot runs raises RestoreIncomplete: files may be written.
    """
    refuse_live(env)
    refuse_layout(env)
    status.set("installing", "CHECKING THE SAVED VERSION...")
    held = env.root / HELD_REL
    held.parent.mkdir(parents=True, exist_ok=True)
    saved = snapshot.hold(env, env.root, held)
    if saved is None:
        raise UpdateFailed(MSG_NO_SAVED)
    status.version = saved.version
    env.note(f"restore: {saved.version} saved {saved.date}")
    work = own_dir(env, RESTORE_NAME)
    new, tool = work / "root", work / "tool"
    try:
        shutil.rmtree(tool, ignore_errors=True)
        for directory in (new, tool):
            safefile.root_dir(directory, os.geteuid())
        for source in TOOL:
            name = "couchliteos_updater.py" if source == TOOL[0] else source.name
            shutil.copyfile(source, tool / name)
        load_squashfs(env)
        with contextlib.ExitStack() as stack:
            message = "COULD NOT OPEN THE SAVED VERSION"
            _mount(env, stack, ["-t", "squashfs", "-o", "ro,loop", str(held)], new, message)
            _enter(env, stack, new, message)
            argv = ["chroot", str(new), "python3", f"{tool}/couchliteos_updater.py", "apply-root", "/mnt",
                    "--status", str(status.path), "--force", "--snapshot-done", "--restore"]
            # Not captured: its progress lines go to the same screen as ours.
            code = env.runner(argv, check=False).returncode
            env.note(f"$ {' '.join(argv)} -> exit {code}")
            if code != 0:
                reported = read_status(status.path)
                failed = reported.get("message") if reported.get("phase") == "failed" else None
                raise RestoreIncomplete(failed if isinstance(failed, str) and failed else MSG_RESTORE)
    finally:
        shutil.rmtree(tool, ignore_errors=True)
        with contextlib.suppress(OSError):
            held.unlink()
    return saved.version


def restore_at_boot(env: Env, status: Status) -> int:
    """couchliteos-restore.service: restore, drop the request, restart. On failure say why and let
    the start go on (a power cut leaves the request, and the next start finishes the restore).

    A failure before anything was written drops the request, so a restore that cannot work does
    not block every start. One after the copy began (RestoreIncomplete: the box may be half old,
    half new) keeps it for the next start, counted next to it; after RESTORE_ATTEMPTS such tries it
    is dropped and the screen says the box may need reinstalling."""
    status.echo = status.echo or (lambda text: print(text, flush=True))
    status.echo("RESTORING THE SAVED VERSION. KEEP THE BOX PLUGGED IN.")
    done: list[str] = []
    incomplete: list[RestoreIncomplete] = []

    def attempt() -> None:
        try:
            done.append(restore(env, status))
        except RestoreIncomplete as error:
            incomplete.append(error)
            raise

    code = _guarded(env, status, attempt)
    retry = False
    if incomplete and snapshot.requested(env.root):
        try:
            tries = snapshot.count_attempt(env, env.root)
        except OSError as error:
            env.note(f"restore: could not count the try: {error}")
            tries = RESTORE_ATTEMPTS
        env.note(f"restore: stopped part way, try {tries} of {RESTORE_ATTEMPTS}")
        retry = 0 < tries < RESTORE_ATTEMPTS
    if not retry:
        with contextlib.suppress(OSError):
            snapshot.clear_request(env, env.root)
    if code == 0:
        status.echo(f"RESTORED COUCHLITEOS {done[0]}. RESTARTING...")
        sh(env, ["sync"])
        reboot(env)
    elif retry:
        status.echo(MSG_RESTORE_RETRY)
        env.sleep(RESTORE_FAILED_PAUSE)
    elif incomplete:
        status.echo(MSG_RESTORE_GAVE_UP)
        env.sleep(RESTORE_FAILED_PAUSE)
    else:
        status.echo("THE BOX STARTS AS IT IS. TRY AGAIN FROM SETTINGS > SOFTWARE UPDATE OR THE BOOT MENU.")
        env.sleep(RESTORE_FAILED_PAUSE)
    return code
# ------------------------------------------------------------------ apply-disk (an installed system, from the ISO)
#
# Booted from the new ISO, the live system updates the system installed on a disk of the box,
# including 0.2.0 (no updater, no version file) and MoonlightOS 0.1.x (the old names). The image is
# the stick's pristine squashfs, mounted again: the live root carries this session's changes.

LEGACY_VERSION_REL = "etc/moonlightos-version"  # rename:keep
LEGACY_PROFILE_REL = "usr/share/moonlightos/profile.conf"  # rename:keep
# The path table of scripts/couchliteos-migrate, relative to the target.
LEGACY_PATHS = (
    ("var/lib/moonlightos", "var/lib/couchliteos"),  # rename:keep
    ("var/log/moonlightos", "var/log/couchliteos"),  # rename:keep
)
LEGACY_GROUPS = {"moonlightos": "couchliteos"}  # rename:keep
LEGACY_USER = "moonlightos"  # rename:keep
LEGACY_UNIT_PREFIX = "moonlightos-"  # rename:keep
LEGACY_GRUB_REL = "etc/default/grub.d/20-moonlightos.cfg"  # rename:keep
LEGACY_CONFIG_REL = "var/lib/couchliteos/config.ini"
UNIT_DIRS = ("lib/systemd/system", "usr/lib/systemd/system", "etc/systemd/system")
BOOTCHECK_REL = "etc/grub.d/01_couchliteos_bootcheck"
LIVE_IMAGE_REL = "run/live/medium/live/filesystem.squashfs"
INSTALLS_NAME = "installs.json"
DISK_DIR, DISK_IMAGE_DIR, PROBE_DIR = "disk", "disk-image", "probe"
PROBE_FS = ("ext4", "ext3", "ext2", "btrfs", "xfs")
SEPARATE = ("/usr", "/var", "/boot")  # mount points the update cannot replace together with /
OLDER = "OLDER THAN 0.2.1"
PHASES = PHASES + ("updated",)  # apply-disk finished: the stick comes out before the restart

MSG_NOT_LIVE = "UPDATING THE INSTALLED SYSTEM NEEDS THE COUCHLITEOS USB STICK"
MSG_NO_INSTALL = "NO INSTALLED COUCHLITEOS OR MOONLIGHTOS SYSTEM FOUND ON THAT DISK"  # rename:keep
MSG_DISK_LAYOUT = (
    "THE INSTALLED SYSTEM HAS A LAYOUT THIS UPDATE DOES NOT KNOW. NOTHING WAS CHANGED. "
    "USE INSTALL INSTEAD (IT ERASES THE DISK)."
)
MSG_DISK_NEWER = "THE INSTALLED SYSTEM IS NEWER THAN THIS USB STICK"
MSG_DISK_DONE = "UPDATE DONE. REMOVE THE USB STICK, THEN RESTART: REBOOT OR POWER > RESTART ON THE HOME SCREEN."


@dataclasses.dataclass(frozen=True)
class Install:
    device: str   # the partition holding the root file system
    version: str  # OLDER when the box has no version file
    profile: str  # PROFILE_NAME ("general", "nvidia"), "" when the file does not say
    disk: str     # the whole disk the partition is on, "" when unknown


def box_identity(root: pathlib.Path) -> tuple[str, str] | None:
    """(version or "", profile name) of the system at `root`, or None when it is not one of ours.

    The couchliteos names win; a MoonlightOS box (0.1.x) has only the old ones.
    """
    root = pathlib.Path(root)
    profile = next((root / rel for rel in (PROFILE_REL, LEGACY_PROFILE_REL) if (root / rel).is_file()), None)
    if profile is None:
        return None
    version = update.installed_version([root / VERSION_REL, root / LEGACY_VERSION_REL])
    return version, update.read_profile(profile).get("PROFILE_NAME", "")


def _probe_options(fstype: str) -> str:
    return "ro,noload" if fstype in ("ext3", "ext4") else "ro"  # noload: not even the journal is replayed


def find_installs(env: Env) -> list[Install]:
    """Every partition holding an installed system: each one not in use is mounted read-only in turn."""
    result = sh(env, ["lsblk", "-J", "-p", "-o", "NAME,TYPE,FSTYPE,MOUNTPOINT"])
    try:
        tree = json.loads(result.stdout or "")["blockdevices"] if result.returncode == 0 else []
    except (ValueError, KeyError, TypeError):
        tree = []
    candidates: list[tuple[str, str, str]] = []

    def walk(nodes: object, disk: str) -> None:
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict) or not isinstance(node.get("name"), str):
                continue
            kind, name = node.get("type"), node["name"]
            if kind == "part" and node.get("fstype") in PROBE_FS and not node.get("mountpoint"):
                candidates.append((name, node["fstype"], disk))
            walk(node.get("children"), name if kind == "disk" else disk)

    walk(tree, "")
    probe = own_dir(env, PROBE_DIR) if candidates else None
    found = []
    for device, fstype, disk in candidates:
        if sh(env, ["mount", "-o", _probe_options(fstype), device, str(probe)]).returncode != 0:
            continue
        try:
            identity = box_identity(probe)
        finally:
            unmount(env, str(probe))
        if identity is not None:
            found.append(Install(device, identity[0] or OLDER, identity[1], disk))
    return found


def write_installs(path: pathlib.Path, installs: Iterable[Install]) -> None:
    """The list the live launcher reads (couchliteos_update.read_installs)."""
    write_atomic(path, json.dumps([dataclasses.asdict(item) for item in installs]) + "\n", 0o644)


def target_layout(target: pathlib.Path) -> str:
    """Refuse a disk whose fstab mounts /usr, /var or /boot separately (refuse_layout, for a disk that
    is not running). Returns the fstab source of /boot/efi, "" for a BIOS box."""
    try:
        lines = (target / "etc/fstab").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        raise UpdateFailed(MSG_DISK_LAYOUT) from error
    esp = ""
    for line in lines:
        fields = line.split()
        if len(fields) < 2 or fields[0].startswith("#"):
            continue
        point = fields[1].replace("\\040", " ").rstrip("/") or "/"
        if point in SEPARATE:
            raise UpdateFailed(MSG_DISK_LAYOUT)
        if point == "/boot/efi":
            esp = fields[0]
    return esp


def legacy_conflicts(target: pathlib.Path) -> list[str]:
    """What stops the old data directories from moving: a symlink, or a name in both old and new."""
    problems = []
    for old, new in LEGACY_PATHS:
        source, destination = target / old, target / new
        if not os.path.lexists(source):
            continue
        if source.is_symlink() or not source.is_dir() or destination.is_symlink():
            problems.append(f"/{old}")
        elif destination.is_dir():
            problems += [f"/{new}/{name}" for name in sorted(set(os.listdir(source)) & set(os.listdir(destination)))]
        elif os.path.lexists(destination):
            problems.append(f"/{new}")
    return problems


def move_legacy_dirs(target: pathlib.Path, log: Callable[[str], None]) -> None:
    """Rename /var/{lib,log}/moonlightos to the couchliteos names; safe to run again.

    The new directory may already exist (something made it before the move; the snapshot and the
    update log are in root's own -update directories, not here): its entries join the old tree
    first, so the moved directory keeps the owner and mode the box gave it. A name in both stops
    the move part way with every file still in one of the two trees (legacy_conflicts finds it).
    """
    for old, new in LEGACY_PATHS:
        source, destination = target / old, target / new
        if source.is_symlink() or not source.is_dir():
            continue
        if destination.is_dir() and not destination.is_symlink():
            for entry in sorted(destination.iterdir()):
                if os.path.lexists(source / entry.name):
                    raise UpdateFailed(MSG_DISK_LAYOUT)
                os.rename(entry, source / entry.name)
            destination.rmdir()
        os.rename(source, destination)
        log(f"legacy: moved /{old} to /{new}")


def rewrite_legacy_config(path: pathlib.Path, chown: Callable[[str, int, int], None] = os.chown) -> bool:
    """Values in config.ini that name an old path name the new one; owner and mode stay."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        info = path.stat()
    except OSError:
        return False
    new = text
    for old, renamed in LEGACY_PATHS:
        new = re.sub(re.escape(f"/{old}") + r"(?![\w.-])", f"/{renamed}", new)
    if new == text:
        return False
    write_atomic(path, new, stat.S_IMODE(info.st_mode), (info.st_uid, info.st_gid), chown)
    return True


def migrate_legacy(target: pathlib.Path, log: Callable[[str], None],
                   chown: Callable[[str, int, int], None] = os.chown) -> None:
    """Before the copy: the old data paths, config values and GRUB settings take the new names."""
    move_legacy_dirs(target, log)
    if rewrite_legacy_config(target / LEGACY_CONFIG_REL, chown):
        log("legacy: old paths rewritten in config.ini")
    if os.path.lexists(target / LEGACY_GRUB_REL):
        (target / LEGACY_GRUB_REL).unlink()
        log(f"legacy: removed /{LEGACY_GRUB_REL}")
    if any(line.startswith(f"{LEGACY_USER}:") for line in read_accounts(target)["passwd"].splitlines()):
        log(f"legacy: the old {LEGACY_USER} user has the uid couchliteos has, so couchliteos keeps its files")


def _unit_exists(target: pathlib.Path, name: str) -> bool:
    names = [name]
    instance = re.match(r"^(.+@)[^@]+(\.[a-z]+)$", name)
    if instance:
        names.append(instance.group(1) + instance.group(2))  # the template of an instance
    for directory in UNIT_DIRS:
        for unit in names:
            path = target / directory / unit
            if path.is_file() and not (directory == "etc/systemd/system" and path.is_symlink()):
                return True
    return False


def stale_legacy_units(target: pathlib.Path) -> list[str]:
    """moonlightos-* links in /etc/systemd/system (enablements, aliases) whose unit is gone,
    relative to /etc."""
    stale = []
    for directory, directories, files in os.walk(target / "etc/systemd/system"):
        for name in directories + files:
            path = pathlib.Path(directory, name)
            if name.startswith(LEGACY_UNIT_PREFIX) and path.is_symlink() and not _unit_exists(target, name):
                stale.append(str(path.relative_to(target / "etc")))
    return sorted(stale)


def remove_legacy_units(target: pathlib.Path, log: Callable[[str], None]) -> None:
    """After the copy (the old unit files are gone by then): drop what still enables them."""
    stale = stale_legacy_units(target)
    for rel in stale:
        log(f"legacy: removing /etc/{rel}")
    remove_stale(target / "etc", stale)


def grub_install_argv(env: Env, device: str, esp: str) -> list[str]:
    """The grub-install command for the target's boot loader: UEFI into its ESP, else the disk's MBR."""
    if esp:
        return ["grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi"]
    result = sh(env, ["lsblk", "-n", "-p", "-o", "PKNAME", device])
    disk = (result.stdout or "").split() if result.returncode == 0 else []
    if not disk or not disk[0].startswith("/dev/"):
        raise UpdateFailed(MSG_DISK_LAYOUT)
    return ["grub-install", "--target=i386-pc", disk[0]]


def apply_disk(
    device: str, env: Env, status: Status, *, force: bool = False,
    save: Callable[..., None] | None = None, apply: Callable[..., None] | None = None,
) -> None:
    """Update the system installed on `device` from this live USB stick (see the section comment).

    Every refusal (no install, a separate /usr, /var or /boot, another kind of box, a newer box)
    comes before anything on the disk is written. The disk is saved as the snapshot first.
    """
    status.set("installing", "OPENING THE INSTALLED SYSTEM...")
    if not update.is_live(env.root / "run/live/medium", env.root / "proc/cmdline"):
        raise UpdateFailed(MSG_NOT_LIVE)
    target, image = own_dir(env, DISK_DIR), own_dir(env, DISK_IMAGE_DIR)
    with contextlib.ExitStack() as stack:
        def mount(argv: list[str], mountpoint: pathlib.Path, message: str) -> None:
            mountpoint.mkdir(parents=True, exist_ok=True)
            if sh(env, ["mount", *argv, str(mountpoint)]).returncode != 0:
                raise UpdateFailed(message)
            stack.callback(unmount, env, str(mountpoint))

        mount([device], target, "COULD NOT OPEN THE INSTALLED SYSTEM")
        found = box_identity(target)
        if found is None:
            raise UpdateFailed(MSG_NO_INSTALL)
        esp = target_layout(target)
        mount(["-t", "squashfs", "-o", "ro,loop", str(env.root / LIVE_IMAGE_REL)], image,
              "COULD NOT OPEN THE NEW SYSTEM ON THE USB STICK")
        new = preflight(target, image, env, True, legacy=True)
        if not force and found[0] and update.is_newer(found[0], new):
            raise UpdateFailed(MSG_DISK_NEWER)
        # A GRUB from before the boot check (0.2.0 and older) is installed again, not only configured.
        grub = None if (target / BOOTCHECK_REL).exists() else grub_install_argv(env, device, esp)
        if esp:
            mount([esp], target / "boot/efi", "COULD NOT OPEN THE BOOT PARTITION OF THE INSTALLED SYSTEM")
        log = Log(target / LOG_REL)
        log(f"apply-disk {device}: {found[0] or OLDER} ({found[1]}) to {new} from the USB stick")
        disk_env = dataclasses.replace(env, log=log)
        try:
            (save or save_snapshot)(disk_env, status, target)
        except NoRoomToSave as error:
            # The old install has no SAVE BEFORE UPDATE switch to turn off: go on without a copy.
            log(f"snapshot: {error.message}")
            log(MSG_NO_ROOM_OLD_CALLER)
            status.set("installing", MSG_NO_ROOM_OLD_CALLER)
        (apply or apply_root)(target, status, disk_env, force=True, image=image, save_first=False,
                              legacy=True, grub_install=grub)
    status.set("updated", MSG_DISK_DONE, 100)


def only_install(env: Env) -> str:
    """The device of the one installed system on the disks (apply-disk --found, the service)."""
    installs = find_installs(env)
    if not installs:
        raise UpdateFailed("NO INSTALLED SYSTEM FOUND ON THE DISKS")
    if len(installs) > 1:
        raise UpdateFailed("MORE THAN ONE INSTALLED SYSTEM FOUND: DISCONNECT THE OTHER DISKS AND TRY AGAIN")
    return installs[0].device


# ------------------------------------------------------------------ run (the service) and the CLI


def reboot(env: Env) -> None:
    sh(env, ["systemctl", "reboot"])


def run(
    env: Env, status: Status, apply: Callable[..., None] | None = None, *, save_first: bool = True,
    save: Callable[[Env, Status], None] | None = None,
) -> int:
    """Check, download, verify, save, install, restart. Returns the exit status of the service."""
    apply = apply or (lambda iso, env, status: apply_iso(iso, env, status, save_first=False))
    (env.run_dir / CANCEL_NAME).unlink(missing_ok=True)  # a cancel from an earlier run must not count
    try:
        status.set("checking", "CHECKING FOR UPDATES...")
        refuse_live(env)
        refuse_layout(env)
        current = _read_version(env.root / VERSION_REL)
        if not current:
            raise UpdateFailed("THIS BOX HAS NO VERSION FILE")
        profile = update.read_profile(env.root / PROFILE_REL)
        try:
            release = update.fetch_release(current, env.opener, timeout=API_TIMEOUT)
        except update.UpdateError as error:
            env.note(f"release lookup: {error}")
            raise UpdateFailed(MSG_NETWORK) from error
        status.version = release.version
        if not update.is_newer(release.version, current):
            status.set("uptodate", "THIS IS THE NEWEST VERSION")
            return 0
        try:
            asset = update.pick_iso(release, profile.get("ISO_SUFFIX", ""))
            sums = fetch_sums(update.sums_asset(release), env)
        except update.UpdateError as error:
            env.note(f"release assets: {error}")
            raise UpdateFailed(update.not_ready(release, profile) or "THIS RELEASE HAS NO FILE FOR THIS BOX") from error
        iso = download_iso(asset, expected_sha(sums, asset.name), env, status)
        if save_first:
            (save or save_snapshot)(env, status)
        apply(iso, env, status)
        clean_cache(env.cache_dir)
        status.set("restarting", "RESTARTING...", 100)
        reboot(env)
        return 0
    except Cancelled:
        status.set("cancelled", "UPDATE CANCELLED")
        return 0
    except UpdateFailed as error:
        env.note(f"update failed: {error.message}")
        status.set("failed", error.message)
        return 1
    except Exception:  # noqa: BLE001 - the screen gets plain words, the log gets the traceback
        env.note(traceback.format_exc())
        status.set("failed", MSG_GENERIC)
        return 1


def _guarded(env: Env, status: Status, action: Callable[[], None]) -> int:
    try:
        action()
    except Cancelled:
        status.set("cancelled", "UPDATE CANCELLED")
        return 0
    except UpdateFailed as error:
        env.note(f"update failed: {error.message}")
        status.set("failed", error.message)
        return 1
    except Exception:  # noqa: BLE001
        env.note(traceback.format_exc())
        status.set("failed", MSG_GENERIC)
        return 1
    return 0


def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    parser = argparse.ArgumentParser(prog="couchliteos-updater", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    service = commands.add_parser("run", help="check for, download and install the newest release (the service)")
    service.add_argument("--no-snapshot", action="store_true", help="do not save the current version first")
    iso = commands.add_parser("apply-iso", help="install from an ISO file or device")
    iso.add_argument("path")
    iso.add_argument("--force", action="store_true", help="allow the same or an older version")
    iso.add_argument("--no-reboot", action="store_true")
    iso.add_argument("--no-snapshot", action="store_true", help="do not save the current version first")
    commands.add_parser("delete-snapshot", help="remove the saved previous version")
    commands.add_parser("request-restore", help="restore the saved previous version at the next start")
    commands.add_parser("restore", help="restore the saved previous version now (the boot service)")
    commands.add_parser("find-installs", help="live USB: list the installed systems on the disks")
    disk = commands.add_parser("apply-disk", help="live USB: update the installed system on a disk")
    disk.add_argument("device", nargs="?", help="the partition holding the installed root")
    disk.add_argument("--found", action="store_true", help="the one installed system find-installs sees")
    disk.add_argument("--force", action="store_true", help="allow an installed system newer than the stick")
    root = commands.add_parser("apply-root", help="install step; runs inside the new image")
    root.add_argument("target")
    root.add_argument("--status", required=True)
    root.add_argument("--force", action="store_true")
    root.add_argument("--snapshot-done", action="store_true", help="the caller already saved TARGET (0.3.0 and later)")
    root.add_argument("--no-snapshot", action="store_true", help="do not save TARGET first")
    root.add_argument("--restore", action="store_true", help="the image is the saved previous version")
    args = parser.parse_args(argv)
    if args.command == "apply-disk" and bool(args.device) == args.found:
        parser.error("apply-disk takes a DEVICE or --found")
    env = env or Env()
    if env.euid() != 0:
        print("the updater must run as root", file=sys.stderr)
        return 2
    if env.log is None:
        env.log = Log(env.root / LOG_REL)

    if args.command == "apply-root":
        echo = (lambda text: print(text, flush=True)) if args.restore else None
        status = Status(pathlib.Path(args.status), echo=echo)
        return _guarded(env, status, lambda: apply_root(
            pathlib.Path(args.target), status, env, force=args.force,
            save_first=not (args.snapshot_done or args.no_snapshot), restore=args.restore))

    status = Status(env.run_dir / STATUS_NAME)
    try:
        with acquire_lock(own_dir(env) / LOCK_NAME):
            if args.command == "delete-snapshot":  # leaves the update status alone: no update ran
                try:
                    return 0 if snapshot.delete(env, env.root) else 1
                except OSError as error:
                    env.note(f"snapshot: delete failed: {error}")
                    return 1
            if args.command == "run":
                return run(env, status, save_first=not args.no_snapshot)
            if args.command == "restore":
                return restore_at_boot(env, status)
            if args.command == "request-restore":
                code = _guarded(env, status, lambda: request_restore(env, status))
                if code == 0:
                    reboot(env)
                return code
            if args.command == "find-installs":
                write_installs(env.run_dir / INSTALLS_NAME, find_installs(env))
                return 0
            if args.command == "apply-disk":
                code = _guarded(env, status, lambda: apply_disk(
                    args.device or only_install(env), env, status, force=args.force))
                if code == 0:  # the live launcher stops offering what is now done
                    write_installs(env.run_dir / INSTALLS_NAME, find_installs(env))
                return code
            code = _guarded(env, status, lambda: apply_iso(
                pathlib.Path(args.path), env, status, force=args.force, save_first=not args.no_snapshot))
            if code == 0 and not args.no_reboot:
                reboot(env)
            return code
    except UpdateFailed as error:  # another update holds the lock: leave its status alone
        print(error.message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
