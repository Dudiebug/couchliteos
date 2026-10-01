#!/usr/bin/python3
"""CouchLiteOS updater (root side): download a release, then replace the system files in place.

Installed as /usr/libexec/couchliteos-updater. The launcher (user couchliteos) only touches
request files below /run/couchliteos; couchliteos-update.service runs `run` as root.

    run                                       check, download, verify, install, reboot
    apply-iso PATH [--force] [--no-reboot]    install from an ISO file or device (offline updates)
    apply-root TARGET --status FILE [--force] the install step itself

`apply-iso` mounts the ISO's squashfs and runs the NEW image's copy of this program with
`apply-root /mnt` inside a chroot of it (the running disk is bind-mounted at /mnt), so the logic
that does the install is always the newest one. `apply-root` is a stable interface forever.
Progress goes to a small JSON status file the launcher polls.

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

import couchliteos_update as update

RUN_DIR = pathlib.Path("/run/couchliteos")
CACHE_DIR = pathlib.Path("/var/cache/couchliteos/update")
STATUS_NAME = "update-status.json"
CANCEL_NAME = "update-cancel"
LOCK_NAME = "update.lock"
LOG_REL = "var/log/couchliteos/update.log"
MANIFEST_REL = "var/lib/couchliteos-update/etc-manifest"
UPDATER_REL = "usr/libexec/couchliteos-updater"
VERSION_REL = "etc/couchliteos-version"
PROFILE_REL = "usr/share/couchliteos/profile.conf"

PHASES = ("checking", "downloading", "verifying", "installing", "restarting", "failed", "cancelled", "uptodate")
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
MSG_GENERIC = "UPDATE FAILED: SEE /var/log/couchliteos/update.log"
MSG_INSTALL = "INSTALL FAILED: SEE /var/log/couchliteos/update.log"
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
    # Written or edited on the box by the owner (docs/USBIP.md, scripts/couchliteos-tailscale).
    "/couchliteos/tailscale-auth.key", "/couchliteos/usbip-allowlist.conf", "/couchliteos-usbip-client.conf",
    # Changes last, once everything else worked, so a failed update can be tried again.
    "/couchliteos-version",
)
SYSTEM_EXCLUDES = (
    "/dev/", "/proc/", "/sys/", "/run/", "/tmp/", "/mnt/", "/media/", "/lost+found", "/boot/efi/", "/boot/grub/",
    "/home/", "/root/", "/usr/local/", "/swapfile", "/etc/", "/var/",
    "/usr/lib/locale/locale-archive",  # generated on the box by locale-gen
)
VAR_EXCLUDES = (
    "/lib/couchliteos/", "/lib/bluetooth/", "/lib/NetworkManager/", "/lib/tailscale/",
    "/lib/systemd/random-seed", "/lib/systemd/timers", "/lib/systemd/backlight", "/lib/systemd/rfkill",
    "/lib/systemd/timesync", "/lib/systemd/coredump", "/lib/systemd/pstore", "/lib/systemd/linger",
    "/log/", "/cache/", "/tmp/", "/spool/", "/mail/", "/backups/", "/lib/dhcp/", "/lib/alsa/",
    "/lib/upower/", "/lib/private/", "/lib/dbus/machine-id", "/lib/couchliteos-update/",
    "/lib/dpkg/",  # has its own run with delete and the protect filter
)
RSYNC_BASE = ("rsync", "-aHAX", "--numeric-ids", "--delay-updates")
REMAP_DIRS = ("var/lib", "var/log", "var/cache", "var/spool", "home", "root")
ACCOUNT_FILES = ("passwd", "group", "shadow", "gshadow")


class UpdateFailed(Exception):
    """The update stopped; `message` is the ALL CAPS text for the launcher screen."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Cancelled(Exception):
    """The owner cancelled during the download."""


# ------------------------------------------------------------------ environment, status, log


class Log:
    """Append-only update log; never raises."""

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

    def note(self, text: str) -> None:
        if self.log:
            self.log(text)


class Status:
    """The JSON file the launcher polls: {"phase", "percent", "message", "version"}."""

    def __init__(self, path: pathlib.Path, version: str = "") -> None:
        self.path = pathlib.Path(path)
        self.version = version

    def set(self, phase: str, message: str, percent: int | None = None) -> None:
        if phase not in PHASES:
            raise ValueError(phase)
        if percent is not None:
            percent = max(0, min(100, int(percent)))
        text = json.dumps({"phase": phase, "percent": percent, "message": message, "version": self.version})
        try:  # best effort: a status that cannot be written must not stop the update
            write_atomic(self.path, text + "\n", 0o644)
        except OSError:
            pass


def read_status(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_atomic(path: pathlib.Path, text: str, mode: int, owner: tuple[int, int] | None = None,
                 chown: Callable[[str, int, int], None] = os.chown) -> None:
    """Write `text` to `path` so that readers see the old or the new file, never half of it."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if owner is not None:
            chown(temporary, *owner)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def acquire_lock(path: pathlib.Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as stream:
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


def merge_accounts(image: dict[str, str], target: dict[str, str]) -> Accounts:
    """The image's users and groups, plus everything only the box has (maintenance user, its groups).

    Passwords stay as the box has them. A box-only account whose id the image now uses is skipped.
    """
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
    image: pathlib.Path, target: pathlib.Path, filter_system: pathlib.Path, filter_dpkg: pathlib.Path
) -> list[tuple[str, list[str]]]:
    """The four rsync runs, source = the image, destination = the box. Nothing deletes except
    `system` and `dpkg`, and those only after the copy and never what the protect filter names."""

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
    ]


# ------------------------------------------------------------------ apply-root


def _read_version(path: pathlib.Path) -> str:
    return update.installed_version([path])


def preflight(target: pathlib.Path, image: pathlib.Path, env: Env, force: bool) -> str:
    """Refuse before anything is written; returns the version being installed."""
    installed = _read_version(target / VERSION_REL)
    if not installed:
        raise UpdateFailed("THIS DISK IS NOT A COUCHLITEOS BOX")
    new = _read_version(image / VERSION_REL)
    if not new:
        raise UpdateFailed("THIS UPDATE HAS NO VERSION")
    ours = update.read_profile(image / PROFILE_REL).get("PROFILE_NAME")
    theirs = update.read_profile(target / PROFILE_REL).get("PROFILE_NAME")
    if not ours or ours != theirs:
        raise UpdateFailed(MSG_OTHER_BOX)
    if not force and not update.is_newer(new, installed):
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
    target: pathlib.Path, status: Status, env: Env, *, force: bool = False, image: pathlib.Path = pathlib.Path("/")
) -> None:
    """Replace the system files of the box at `target` with those of the image at `image`."""
    target, image = pathlib.Path(target), pathlib.Path(image)
    new_version = preflight(target, image, env, force)
    log = Log(target / LOG_REL)
    status.version = new_version
    log(f"update to {new_version} from {_read_version(target / VERSION_REL)} (force={force})")

    def step(message: str, percent: int) -> None:
        log(message)
        status.set("installing", message, percent)

    step("PREPARING THE UPDATE...", 2)
    image_packages = parse_dpkg_status(_read(image / "var/lib/dpkg/status"))
    box_packages = parse_dpkg_status(_read(target / "var/lib/dpkg/status"))
    extras = extra_packages(box_packages, image_packages)
    log(f"packages only the box has: {len(extras)}")
    protected = protected_paths(target, image, extras)
    env.run_dir.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix="update-filter-", dir=env.run_dir))
    try:
        filter_system, filter_dpkg = work / "system.rules", work / "dpkg.rules"
        outside = [path for path in protected if not path.startswith(("/etc/", "/var/"))]
        filter_system.write_text("".join(f"{rule}\n" for rule in filter_rules(outside)), encoding="utf-8")
        filter_dpkg.write_text(
            "".join(f"{rule}\n" for rule in filter_rules(protected, "/var/lib/dpkg")), encoding="utf-8")

        # Accounts first, while every file still carries the ids the box had; the copy below then
        # gives image-owned files the new ids, and running this again after a failure changes nothing.
        accounts = merge_accounts(read_accounts(image), read_accounts(target))
        for line in accounts.skipped:
            log(f"accounts: skipped {line}")
        step("KEEPING YOUR ACCOUNTS...", 5)
        log(f"id remap: users {accounts.uid_map} groups {accounts.gid_map}")
        remap_ids(target, accounts.uid_map, accounts.gid_map, lchown=env.lchown)
        write_accounts(target, accounts, chown=env.chown)

        messages = {
            "system": ("COPYING SYSTEM FILES...", 10), "etc": ("UPDATING SETTINGS FILES...", 60),
            "var": ("UPDATING SYSTEM DATA...", 70), "dpkg": ("UPDATING THE PACKAGE LIST...", 75),
        }
        for name, argv in rsync_steps(image, target, filter_system, filter_dpkg):
            step(*messages[name])
            if sh(env, argv, log).returncode not in RSYNC_OK:
                raise UpdateFailed(f"INSTALL FAILED WHILE COPYING FILES ({name.upper()}). {MSG_INSTALL[15:]}")
            if name == "etc":
                _tidy_etc(target, image, log)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    write_atomic(
        target / "var/lib/dpkg/status",
        merge_dpkg_status(_read(image / "var/lib/dpkg/status"), list(extras.values())), 0o644)
    _boot_menu(target, env, log, step)
    write_atomic(target / VERSION_REL, new_version + "\n", 0o644)
    step("SAVING TO THE DISK...", 98)
    sh(env, ["sync"], log)
    log(f"update to {new_version} done")
    status.set("restarting", "RESTARTING...", 100)


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


def _boot_menu(target: pathlib.Path, env: Env, log: Callable[[str], None], step) -> None:
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
        commands.append(["update-grub"])
        for command in commands:
            if sh(env, ["chroot", str(target), *command], log).returncode != 0:
                raise UpdateFailed(MSG_BOOT_MENU)


# ------------------------------------------------------------------ apply-iso (the handover)


def is_block_device(path: pathlib.Path) -> bool:
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def apply_iso(source: pathlib.Path, env: Env, status: Status, *, force: bool = False) -> None:
    """Open the ISO and run its own updater against this box (see the module docstring)."""
    status.set("installing", "INSTALLING THE UPDATE... KEEP THE BOX PLUGGED IN.")
    refuse_live(env)
    refuse_layout(env)
    work = env.run_dir / "update"
    iso_dir, new = work / "iso", work / "root"
    for directory in (iso_dir, new):
        directory.mkdir(parents=True, exist_ok=True)

    with contextlib.ExitStack() as stack:
        def mount(argv: list[str], mountpoint: pathlib.Path, rslave: bool = False) -> None:
            if sh(env, ["mount", *argv, str(mountpoint)]).returncode != 0:
                raise UpdateFailed("COULD NOT OPEN THE UPDATE FILE")
            stack.callback(unmount, env, str(mountpoint))
            if rslave and sh(env, ["mount", "--make-rslave", str(mountpoint)]).returncode != 0:
                raise UpdateFailed("COULD NOT OPEN THE UPDATE FILE")

        mount(["-o", "ro" if is_block_device(source) else "ro,loop", str(source)], iso_dir)
        mount(["-t", "squashfs", "-o", "ro,loop", str(iso_dir / "live/filesystem.squashfs")], new)
        if not (new / UPDATER_REL).exists():
            raise UpdateFailed("THIS ISO CANNOT UPDATE A BOX")
        if update.read_profile(new / PROFILE_REL).get("PROFILE_NAME") != \
                update.read_profile(env.root / PROFILE_REL).get("PROFILE_NAME"):
            raise UpdateFailed(MSG_OTHER_BOX)
        mount(["--bind", "/"], new / "mnt", rslave=True)
        mount(["-t", "proc", "proc"], new / "proc")
        mount(["--rbind", "/sys"], new / "sys", rslave=True)
        mount(["--rbind", "/dev"], new / "dev", rslave=True)
        mount(["--bind", "/run"], new / "run", rslave=True)
        argv = ["chroot", str(new), f"/{UPDATER_REL}", "apply-root", "/mnt", "--status", str(status.path)]
        if force:
            argv.append("--force")
        if sh(env, argv).returncode != 0:
            reported = read_status(status.path)
            message = reported.get("message") if reported.get("phase") == "failed" else None
            raise UpdateFailed(message if isinstance(message, str) and message else MSG_INSTALL)


# ------------------------------------------------------------------ run (the service) and the CLI


def reboot(env: Env) -> None:
    sh(env, ["systemctl", "reboot"])


def run(env: Env, status: Status, apply: Callable[..., None] | None = None) -> int:
    """Check, download, verify, install, restart. Returns the exit status of the service."""
    apply = apply or apply_iso
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
            raise UpdateFailed("THIS RELEASE HAS NO FILE FOR THIS BOX") from error
        iso = download_iso(asset, expected_sha(sums, asset.name), env, status)
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
    commands.add_parser("run", help="check for, download and install the newest release (the service)")
    iso = commands.add_parser("apply-iso", help="install from an ISO file or device")
    iso.add_argument("path")
    iso.add_argument("--force", action="store_true", help="allow the same or an older version")
    iso.add_argument("--no-reboot", action="store_true")
    root = commands.add_parser("apply-root", help="install step; runs inside the new image")
    root.add_argument("target")
    root.add_argument("--status", required=True)
    root.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    env = env or Env()
    if env.euid() != 0:
        print("the updater must run as root", file=sys.stderr)
        return 2
    if env.log is None:
        env.log = Log(env.root / LOG_REL)

    if args.command == "apply-root":
        status = Status(pathlib.Path(args.status))
        return _guarded(env, status, lambda: apply_root(pathlib.Path(args.target), status, env, force=args.force))

    status = Status(env.run_dir / STATUS_NAME)
    try:
        with acquire_lock(env.run_dir / LOCK_NAME):
            if args.command == "run":
                return run(env, status)
            code = _guarded(env, status, lambda: apply_iso(pathlib.Path(args.path), env, status, force=args.force))
            if code == 0 and not args.no_reboot:
                reboot(env)
            return code
    except UpdateFailed as error:  # another update holds the lock: leave its status alone
        print(error.message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
