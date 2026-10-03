"""The one saved copy of the installed system, made before every update so it can be undone.

    /var/lib/couchliteos/snapshot/previous.squashfs   the whole root, zstd (root only, 0600)
    /var/lib/couchliteos/snapshot/previous.json       version, date, size, sha256, kernel (0644)
    /boot/couchliteos-previous/                       vmlinuz, initrd.img and modules/ (squashfs,
                                                      loop and what they need, in modules/load-order)

The snapshot directory is 0711, not 0700: the launcher (user couchliteos) reads previous.json by
name for Settings > SOFTWARE UPDATE but cannot list the directory or read the image. Because its
parent /var/lib/couchliteos belongs to that user, every root use first checks the directory is
still ours (`trusted_dir`), so a directory put there by the user is never written to or restored.

One slot: the new snapshot is staged next to the old one as `.new` files, `previous.json.new` is
written last as the commit mark, then the image, the boot copy and the json are moved into place in
that order. A crash before the mark leaves the old snapshot; after it, `recover` finishes the move.
Either way exactly one valid snapshot is left.

Every system effect goes through the updater's Env (runner, popen, free_bytes, euid, note), and
`root` is the system being saved, so the same code saves the running box ("/") or a disk mounted
elsewhere (the live ISO updating an installed system). Standard library only.
"""

from __future__ import annotations

import configparser
import contextlib
import dataclasses
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable

import couchliteos_update as update

SNAP_REL = "var/lib/couchliteos/snapshot"
BOOT_REL = "boot/couchliteos-previous"
CONFIG_REL = "var/lib/couchliteos/config.ini"
IMAGE, INFO = "previous.squashfs", "previous.json"
VERSION_RELS = ("etc/couchliteos-version", "etc/moonlightos-version")
MODULES = ("squashfs", "loop")  # what the restore needs to open the image on any kernel
LOAD_ORDER = "load-order"
DIR_MODE, IMAGE_MODE, INFO_MODE = 0o711, 0o600, 0o644

GIB = 1 << 30
SNAPSHOT_ESTIMATE = 5 * GIB // 2  # a 0.2.x system is about 1.6 to 2 GB as zstd
SPACE_FOR_INSTALL = 3 * GIB      # couchliteos_updater.SPACE_FOR_INSTALL: still free after the snapshot
SPACE_MARGIN = GIB
KEEP_OLD, DELETE_OLD_FIRST, REFUSE = "keep-old", "delete-old-first", "refuse"

# Relative to the saved root, in mksquashfs -wildcards syntax. The first list keeps the directory
# and drops what is in it: a restore chroots into the image and mounts /proc, /dev, /run and /mnt.
EMPTIED = ("dev", "proc", "sys", "run", "tmp", "mnt", "media", "var/log", "var/cache", "var/tmp")
LEFT_OUT = (SNAP_REL, BOOT_REL, "var/lib/couchliteos/home/.cache", "swapfile", "lost+found")
COMPRESS = ("-comp", "zstd", "-Xcompression-level", "6")
GENTLY = ("nice", "-n", "19", "ionice", "-c", "3")

MSG_FAILED = "COULD NOT SAVE THE CURRENT VERSION: SEE /var/log/couchliteos/update.log"
MSG_NOT_OURS = "COULD NOT SAVE THE CURRENT VERSION: THE SNAPSHOT FOLDER IS NOT THE SYSTEM'S"
SAVING = "SAVING THE CURRENT VERSION..."


class SnapshotFailed(Exception):
    """Saving stopped; `message` is the ALL CAPS text for the launcher screen. Nothing was replaced."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclasses.dataclass(frozen=True)
class Snapshot:
    version: str
    date: str     # UTC, 2026-10-03T14:05:00Z
    size: int     # bytes of previous.squashfs
    sha256: str
    kernel: str   # the release the boot copy holds

    def describe(self) -> str:
        """"0.2.7, SAVED 3 OCT 2026, 1.8 GB" for the settings screens."""
        try:
            day = time.strftime("%d %b %Y", time.strptime(self.date, "%Y-%m-%dT%H:%M:%SZ")).lstrip("0").upper()
        except ValueError:
            day = "ON AN UNKNOWN DATE"
        return f"{self.version or 'UNKNOWN VERSION'}, SAVED {day}, {self.size / 1e9:.1f} GB"


# ------------------------------------------------------------------ paths and reading (any user)


def paths(root: pathlib.Path = pathlib.Path("/")) -> dict[str, pathlib.Path]:
    snap, boot = pathlib.Path(root) / SNAP_REL, pathlib.Path(root) / BOOT_REL
    return {
        "dir": snap, "image": snap / IMAGE, "info": snap / INFO,
        "image_new": snap / (IMAGE + ".new"), "info_new": snap / (INFO + ".new"),
        "boot": boot, "boot_new": boot.with_name(boot.name + ".new"), "boot_old": boot.with_name(boot.name + ".old"),
    }


def _parse(text: str) -> Snapshot | None:
    try:
        data = json.loads(text)
        snapshot = Snapshot(
            version=str(data["version"]), date=str(data["date"]), size=int(data["size"]),
            sha256=str(data["sha256"]).lower(), kernel=str(data["kernel"]),
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return snapshot if re.fullmatch(r"[0-9a-f]{64}", snapshot.sha256) and snapshot.size > 0 else None


def info(root: pathlib.Path = pathlib.Path("/")) -> Snapshot | None:
    """The saved snapshot, or None when there is none (or it is half replaced). Cheap: no hashing."""
    where = paths(root)
    try:
        snapshot = _parse(where["info"].read_text(encoding="utf-8"))
        if snapshot is None or where["image"].stat().st_size != snapshot.size:
            return None
    except OSError:
        return None
    return snapshot


def verify(root: pathlib.Path = pathlib.Path("/")) -> Snapshot | None:
    """`info`, plus the image's hash and a complete boot copy: what a restore checks first (root)."""
    snapshot = info(root)
    where = paths(root)
    if snapshot is None or not all((where["boot"] / name).is_file() for name in ("vmlinuz", "initrd.img")):
        return None
    try:
        digest = _sha256(where["image"])
    except OSError:
        return None
    return snapshot if digest == snapshot.sha256 else None


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ the SAVE BEFORE UPDATE setting


def enabled(root: pathlib.Path = pathlib.Path("/")) -> bool:
    """config.ini [update] snapshot; on unless it says off."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string((pathlib.Path(root) / CONFIG_REL).read_text(encoding="utf-8", errors="replace"))
        value = parser.get("update", "snapshot", fallback="on").strip().lower()
    except (OSError, configparser.Error):
        return True
    return value not in ("off", "false", "no", "0")


def set_enabled(on: bool, root: pathlib.Path = pathlib.Path("/")) -> None:
    """Rewrite only the `snapshot` line of [update], keeping every other line of config.ini."""
    path = pathlib.Path(root) / CONFIG_REL
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        lines = []
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    setting = f"snapshot = {'on' if on else 'off'}\n"
    section, start = "", None
    for index, line in enumerate(lines):
        header = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if header:
            section = header.group(1).strip().lower()
            if section == "update" and start is None:
                start = index
        elif section == "update" and re.match(r"^\s*snapshot\s*[=:]", line):
            lines[index] = setting
            break
    else:
        if start is None:
            lines += (["\n"] if lines and lines[-1].strip() else []) + ["[update]\n", setting]
        else:
            lines.insert(start + 1, setting)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".config.ini.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.writelines(lines)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


# ------------------------------------------------------------------ space


def space_plan(free: int, old_size: int) -> str:
    """keep-old: the new image fits next to the old one; delete-old-first: only without it; refuse."""
    need = SNAPSHOT_ESTIMATE + SPACE_FOR_INSTALL + SPACE_MARGIN
    if free >= need:
        return KEEP_OLD
    if old_size > 0 and free + old_size >= need:
        return DELETE_OLD_FIRST
    return REFUSE


def space_needed_gb() -> int:
    return math.ceil((SNAPSHOT_ESTIMATE + SPACE_FOR_INSTALL + SPACE_MARGIN) / GIB)


# ------------------------------------------------------------------ root side


def _sh(env, argv: Iterable[object]) -> subprocess.CompletedProcess:
    argv = [str(part) for part in argv]
    result = env.runner(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    env.note(f"$ {' '.join(argv)} -> exit {result.returncode}")
    output = result.stdout.strip() if isinstance(result.stdout, str) else ""
    if result.returncode != 0 and output:
        env.note(output[-4000:])
    return result


def trusted_dir(env, root: pathlib.Path, create: bool = False) -> bool:
    """The snapshot directory is a real directory owned by us (root) that nobody else can write.

    With `create`, a missing one is made, and a wrong mode on our own directory is put right.
    """
    directory = paths(root)["dir"]
    try:
        found = os.lstat(directory)
    except FileNotFoundError:
        if not create:
            return False
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=DIR_MODE)
        found = os.lstat(directory)
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != env.euid():
        return False
    if stat.S_IMODE(found.st_mode) != DIR_MODE:
        if not create:
            return False
        os.chmod(directory, DIR_MODE)
    return True


def _rename(source: pathlib.Path, destination: pathlib.Path) -> None:
    os.replace(source, destination)  # a seam for the tests' injected crashes


def _remove(path: pathlib.Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _commit(where: dict[str, pathlib.Path]) -> None:
    """Move a staged snapshot into place: image, boot copy, then the json. Safe to run again."""
    if where["image_new"].exists():
        _rename(where["image_new"], where["image"])
    if where["boot_new"].exists():
        if where["boot"].exists():
            if where["boot_old"].exists():
                _remove(where["boot_old"])
            _rename(where["boot"], where["boot_old"])
        _rename(where["boot_new"], where["boot"])
    if where["boot_old"].exists():
        _remove(where["boot_old"])
    _rename(where["info_new"], where["info"])


def recover(env, root: pathlib.Path = pathlib.Path("/")) -> None:
    """Finish or undo a replacement a crash interrupted, so one valid snapshot (or none) is left."""
    where = paths(root)
    if not trusted_dir(env, root):
        return
    staged = where["info_new"].exists() and _parse(_read(where["info_new"])) is not None
    if staged:
        env.note("snapshot: finishing an interrupted replacement")
        _commit(where)
        return
    for name in ("info_new", "image_new", "boot_new"):
        if os.path.lexists(where[name]):
            env.note(f"snapshot: removing the unfinished {where[name].name}")
            _remove(where[name])
    if where["boot_old"].exists():
        if where["boot"].exists():
            _remove(where["boot_old"])
        else:
            _rename(where["boot_old"], where["boot"])


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def delete(env, root: pathlib.Path = pathlib.Path("/")) -> bool:
    """Remove the saved snapshot and its boot copy (DELETE SAVED VERSION). False if it is not ours."""
    where = paths(root)
    if not trusted_dir(env, root):
        return False
    # The json first: without it nothing counts as a snapshot, whatever a crash leaves behind.
    for name in ("info", "info_new", "image", "image_new", "boot", "boot_new", "boot_old"):
        if os.path.lexists(where[name]):
            _remove(where[name])
    env.note("snapshot: deleted")
    return True


def excludes() -> list[str]:
    patterns = []
    for rel in EMPTIED:
        patterns += [f"{rel}/*", f"{rel}/.*"]  # `*` does not match a leading dot
    return patterns + list(LEFT_OUT)


def mksquashfs_argv(root: pathlib.Path, output: pathlib.Path) -> list[str]:
    return [*GENTLY, "mksquashfs", str(root), str(output), "-noappend", *COMPRESS,
            "-one-file-system", "-percentage", "-wildcards", "-e", *excludes()]


def _version_key(name: str) -> list:
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r"(\d+)", name)]


def pick_kernel(root: pathlib.Path, running: str | None = None) -> str:
    """The running kernel if this root has it, else the root's newest; "" when /boot has none."""
    boot = pathlib.Path(root) / "boot"
    running = os.uname().release if running is None else running
    releases = [path.name.removeprefix("vmlinuz-") for path in boot.glob("vmlinuz-*")]
    releases = [release for release in releases if (boot / f"initrd.img-{release}").is_file()]
    if running in releases:
        return running
    return max(releases, key=_version_key) if releases else ""


def module_files(env, root: pathlib.Path, kernel: str) -> list[pathlib.Path]:
    """The .ko files of MODULES and their dependencies, in load order (built-in ones need none)."""
    root = pathlib.Path(root)
    found: list[pathlib.Path] = []
    for module in MODULES:
        argv = ["modprobe", "--show-depends", "-S", kernel]
        if root != pathlib.Path("/"):
            argv += ["-d", str(root)]
        result = _sh(env, [*argv, module])
        if result.returncode != 0:
            raise SnapshotFailed(MSG_FAILED)
        for line in (result.stdout or "").splitlines():
            words = line.split()
            if len(words) < 2 or words[0] != "insmod":
                continue  # "builtin loop"
            path = pathlib.Path(words[1])
            if not str(path).startswith(str(root).rstrip("/") + "/"):
                path = root / str(path).lstrip("/")
            if path not in found:
                found.append(path)
    return found


def _stage_boot(env, root: pathlib.Path, where: dict[str, pathlib.Path], kernel: str) -> None:
    boot, staged = pathlib.Path(root) / "boot", where["boot_new"]
    modules = module_files(env, root, kernel)
    staged.mkdir(mode=0o755)
    shutil.copyfile(boot / f"vmlinuz-{kernel}", staged / "vmlinuz")
    shutil.copyfile(boot / f"initrd.img-{kernel}", staged / "initrd.img")
    (staged / "modules").mkdir()
    for path in modules:
        shutil.copyfile(path, staged / "modules" / path.name)
    (staged / "modules" / LOAD_ORDER).write_text("".join(f"{path.name}\n" for path in modules), encoding="utf-8")


def _squash(env, status, root: pathlib.Path, output: pathlib.Path) -> None:
    argv = mksquashfs_argv(root, output)
    env.note(f"$ {' '.join(argv[:9])} ...")
    tail: list[str] = []
    shown = 0
    status.set("saving", f"{SAVING} 0%", 0)
    process = env.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    with process:
        for line in process.stdout:
            line = line.strip()
            if line.isdigit():
                percent = min(100, int(line))
                if percent != shown:
                    shown = percent
                    status.set("saving", f"{SAVING} {percent}%", percent)
            elif line:
                tail = (tail + [line])[-20:]
        code = process.wait()
    env.note(f"mksquashfs -> exit {code}")
    if code != 0:
        env.note("\n".join(tail))
        raise SnapshotFailed(MSG_FAILED)


def create(env, status, root: pathlib.Path = pathlib.Path("/"), *, now: Callable[[], float] = time.time,
           running: str | None = None) -> Snapshot:
    """Save `root` as the one snapshot, replacing the old one only once the new one is complete.

    Raises SnapshotFailed with nothing replaced (the old snapshot stays, unless there was only room
    after deleting it). Progress goes to `status` as the "saving" phase.
    """
    root = pathlib.Path(root)
    where = paths(root)
    if not trusted_dir(env, root, create=True):
        raise SnapshotFailed(MSG_NOT_OURS)
    recover(env, root)
    old = info(root)
    plan = space_plan(env.free_bytes(root), old.size if old else 0)
    env.note(f"snapshot: plan {plan}, old {old.version if old else 'none'}")
    if plan == REFUSE:
        raise SnapshotFailed(
            f"NOT ENOUGH FREE SPACE TO SAVE THE CURRENT VERSION: NEED {space_needed_gb()} GB. "
            "FREE SOME SPACE OR TURN OFF SAVE BEFORE UPDATE IN SETTINGS > SOFTWARE UPDATE.")
    if plan == DELETE_OLD_FIRST:
        delete(env, root)
    kernel = pick_kernel(root, running)
    if not kernel:
        env.note("snapshot: no kernel with an initrd in /boot")
        raise SnapshotFailed(MSG_FAILED)
    version = update.installed_version([root / rel for rel in VERSION_RELS])
    started = time.monotonic()
    try:
        _squash(env, status, root, where["image_new"])
        os.chmod(where["image_new"], IMAGE_MODE)
        if _sh(env, ["unsquashfs", "-s", where["image_new"]]).returncode != 0:
            raise SnapshotFailed(MSG_FAILED)
        size = where["image_new"].stat().st_size
        digest = _sha256(where["image_new"])
        _stage_boot(env, root, where, kernel)
        snapshot = Snapshot(
            version=version, date=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())),
            size=size, sha256=digest, kernel=kernel)
        _write_info(where["info_new"], snapshot)  # the commit mark: from here on `recover` rolls forward
    except BaseException as error:
        for name in ("info_new", "image_new", "boot_new"):
            with contextlib.suppress(OSError):
                if os.path.lexists(where[name]):
                    _remove(where[name])
        if isinstance(error, OSError):
            env.note(f"snapshot: {error}")
            raise SnapshotFailed(MSG_FAILED) from error
        raise
    _commit(where)
    _sh(env, ["sync"])
    env.note(f"snapshot: saved {version} ({size} bytes, kernel {kernel}) in {time.monotonic() - started:.0f} s")
    return snapshot


def _write_info(path: pathlib.Path, snapshot: Snapshot) -> None:
    text = json.dumps(dataclasses.asdict(snapshot), indent=1) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, INFO_MODE)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
