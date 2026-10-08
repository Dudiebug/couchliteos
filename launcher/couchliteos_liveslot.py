#!/usr/bin/python3
"""System updates on a live USB stick.

A stick written with Etcher, Rufus (DD mode) or dd keeps its ISO partition read-only, so an
update goes into a slot on the stick's data area instead:

    live-update/current/{vmlinuz,initrd.img,filesystem.squashfs,SHA256SUMS,version,ok}
    live-update/previous/...   (the system that was current before)

The ISO's GRUB menu (config/live-build/hooks/live/0100-autoboot.hook.binary) boots
`current` when `current/ok` exists, and offers `previous` when `previous/ok` exists;
otherwise the stick boots its own ISO system. `ok` is written last, after every file was
checked, so a slot is either complete or ignored.

Replacement is write-check-rename: the new system goes to `current.new`, then
`previous` -> `previous.old`, `current` -> `previous`, `current.new` -> `current`, and
`previous.old` is removed. A stick pulled out at any step boots a checked system (the new
one, the old one, or the ISO's own), and `recover()` tidies the names up afterwards.

A Ventoy stick boots the ISO file itself, so an update there copies the new ISO next to the
old one, gives it the old one's persistence entry in ventoy/ventoy.json, and deletes the old
ISO only after the new one has reached the launcher once (`ventoy_finish`).

Where the slot lives is one setting, SLOT_DESIGN: on the `persistence` partition itself, or
on a small separate ext4 partition labelled `couchliteos-sys`. The GRUB hook looks for both
labels (couchliteos-sys first), so either layout boots.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
from typing import Callable, Iterable, Iterator

# "persistence": live-update/ sits on the persistence partition (live-boot then uses that
# partition both as the live medium and for persistence).
# "sys": live-update/ sits on its own ext4 partition labelled couchliteos-sys, made by
# couchliteos-persist-setup next to the persistence partition.
SLOT_DESIGN = "persistence"
PERSISTENCE_LABEL = "persistence"
SYS_LABEL = "couchliteos-sys"
SLOT_LABELS = {"persistence": PERSISTENCE_LABEL, "sys": SYS_LABEL}
SLOT_DIR = "live-update"
ISO_VERSION_FILE = "couchliteos-iso-version"  # beside SLOT_DIR: the version of the ISO the stick was written with
SYS_PARTITION_BYTES = 6 << 30  # two systems of about 2 GB each, with room to grow

FILES = ("vmlinuz", "initrd.img", "filesystem.squashfs")
ISO_FILES = {"vmlinuz": "live/vmlinuz", "initrd.img": "live/initrd.img",
             "filesystem.squashfs": "live/filesystem.squashfs"}
OK = "ok"
SUMS = "SHA256SUMS"
VERSION = "version"
CURRENT, PREVIOUS = "current", "previous"
NEW, OLD, SWAP = "current.new", "previous.old", "rollback.tmp"
MARGIN = 256 << 20  # free space kept on the data area after an update
CHUNK = 1 << 20

STATE_FILE = pathlib.Path("/var/lib/couchliteos/live-update.json")
VENTOY_JSON = "ventoy/ventoy.json"
VENTOY_BACKEND = "/couchliteos-persistence.dat"
MOUNT_DIR = pathlib.Path("/run/couchliteos/live-update")


class SlotError(Exception):
    """Updating the stick stopped; `message` is the ALL CAPS text for the launcher."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class VentoyJsonError(SlotError):
    """ventoy/ventoy.json exists but is not something this code may rewrite."""


def _free_bytes(path: pathlib.Path) -> int:
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize


@dataclasses.dataclass
class Env:
    """What the slot code touches outside its arguments, replaceable in tests."""

    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    free_bytes: Callable[[pathlib.Path], int] = _free_bytes
    log: Callable[[str], None] | None = None
    # Called between the renames of a replacement; tests raise from it to simulate a
    # stick pulled out at that step.
    step: Callable[[str], None] = lambda name: None

    def note(self, text: str) -> None:
        if self.log:
            self.log(text)


def run(env: Env, argv: Iterable[object], input_text: str | None = None) -> subprocess.CompletedProcess:
    argv = [str(part) for part in argv]
    result = env.runner(argv, input=input_text, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, check=False)
    env.note(f"$ {' '.join(argv)} -> exit {result.returncode}")
    return result


# ------------------------------------------------------------------ small file helpers


def _fsync_dir(path: pathlib.Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_synced(path: pathlib.Path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_hashed(source: pathlib.Path, destination: pathlib.Path,
                progress: Callable[[int], None] | None = None) -> str:
    """Copy and fsync; return the sha256 of what was read from `source`."""
    digest = hashlib.sha256()
    with open(source, "rb") as reader, open(destination, "wb") as writer:
        for block in iter(lambda: reader.read(CHUNK), b""):
            digest.update(block)
            writer.write(block)
            if progress:
                progress(len(block))
        writer.flush()
        os.fsync(writer.fileno())
    return digest.hexdigest()


def _remove(path: pathlib.Path) -> None:
    if path.is_dir() and not path.is_symlink():
        # The ok marker goes first, so a half-deleted slot is never taken for a good one.
        with contextlib.suppress(FileNotFoundError):
            (path / OK).unlink()
        shutil.rmtree(path)
    else:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def parse_sums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            sums[parts[1].lstrip("*").removeprefix("./")] = parts[0].lower()
    return sums


# ------------------------------------------------------------------ the A/B slot


def slot_is_good(slot: pathlib.Path) -> bool:
    """A slot counts only with its ok marker and every file present."""
    return (slot / OK).is_file() and all((slot / name).is_file() for name in FILES)


def verify_slot(slot: pathlib.Path) -> bool:
    """Re-read every file and compare it with the slot's SHA256SUMS."""
    try:
        sums = parse_sums((slot / SUMS).read_text(encoding="ascii"))
        return all(name in sums and sha256_file(slot / name) == sums[name] for name in FILES)
    except (OSError, UnicodeError):
        return False


def recover(base: pathlib.Path) -> list[str]:
    """Finish or undo a replacement that a power cut or a pulled stick interrupted.

    `base` is the live-update directory. Returns what was done, for the log.
    """
    done: list[str] = []
    current, previous = base / CURRENT, base / PREVIOUS
    new, old, swap = base / NEW, base / OLD, base / SWAP
    if swap.exists():  # an interrupted rollback: current and previous trade places
        if not current.exists() and previous.exists():
            os.replace(previous, current)
        if not current.exists():
            os.replace(swap, current)
            done.append("undid an interrupted rollback")
        elif not previous.exists():
            os.replace(swap, previous)
            done.append("finished an interrupted rollback")
        else:
            _remove(swap)
    if new.exists():
        if not current.exists() and slot_is_good(new):
            os.replace(new, current)  # the old current already became previous
            done.append("finished an interrupted update")
        else:
            _remove(new)  # never got its ok marker, or current is still in place
            done.append("removed an unfinished update")
    if old.exists():
        if not previous.exists():
            os.replace(old, previous)
            done.append("restored the previous system")
        else:
            _remove(old)
    for slot in (current, previous):
        if slot.exists() and not slot_is_good(slot):
            _remove(slot)
            done.append(f"removed incomplete {slot.name}")
    if done:
        _fsync_dir(base)
    return done


def slot_version(slot: pathlib.Path) -> str:
    try:
        return (slot / VERSION).read_text(encoding="utf-8").strip() if slot_is_good(slot) else ""
    except OSError:
        return ""


def slot_state(base: pathlib.Path) -> dict[str, str]:
    return {name: slot_version(base / name) for name in (CURRENT, PREVIOUS)}


def space_needed(sizes: Iterable[int]) -> int:
    return sum(sizes) + MARGIN


def install(base: pathlib.Path, sources: dict[str, pathlib.Path], version: str, env: Env,
            expected: dict[str, str] | None = None,
            progress: Callable[[int], None] | None = None) -> None:
    """Install kernel, initrd and squashfs from `sources` as the new `current`.

    `expected` (optional) holds sha256 sums the sources must match, for example from the
    ISO's own sha256sum.txt. Every copy is read back and compared before `ok` is written.
    """
    if set(sources) != set(FILES):
        raise ValueError(f"sources must be exactly {FILES}")
    base.mkdir(parents=True, exist_ok=True)
    for line in recover(base):
        env.note(f"live-update: {line}")
    # The old previous is replaced, so its space counts as free.
    reclaim = sum((base / PREVIOUS / name).stat().st_size for name in FILES
                  if (base / PREVIOUS / name).is_file())
    needed = space_needed(path.stat().st_size for path in sources.values())
    free = env.free_bytes(base) + reclaim
    if free < needed:
        missing = -(-(needed - free) // (1 << 20))
        raise SlotError(f"NOT ENOUGH SPACE ON THE STICK: {missing} MB MORE NEEDED")
    new = base / NEW
    _remove(new)
    new.mkdir()
    sums: dict[str, str] = {}
    for name in FILES:
        got = copy_hashed(sources[name], new / name, progress)
        if expected and expected.get(name) and expected[name].lower() != got:
            _remove(new)
            raise SlotError("THE UPDATE FILES ARE DAMAGED: TRY AGAIN")
        sums[name] = got
    _write_synced(new / SUMS, "".join(f"{sums[name]}  {name}\n" for name in FILES))
    _write_synced(new / VERSION, version + "\n")
    _fsync_dir(new)
    if not verify_slot(new):  # read back from the stick, not from the page cache's word
        _remove(new)
        raise SlotError("THE STICK DID NOT KEEP THE UPDATE: IT MAY BE FAILING")
    _write_synced(new / OK, version + "\n")
    _fsync_dir(new)
    env.step("written")
    current, previous, old = base / CURRENT, base / PREVIOUS, base / OLD
    if current.exists():
        if previous.exists():
            os.replace(previous, old)
            _fsync_dir(base)
            env.step("previous-aside")
        os.replace(current, previous)
        _fsync_dir(base)
        env.step("current-to-previous")
    os.replace(new, current)
    _fsync_dir(base)
    env.step("new-to-current")
    if old.exists():
        _remove(old)
        _fsync_dir(base)


def rollback(base: pathlib.Path, env: Env) -> None:
    """Make `previous` the system GRUB starts; the failed one becomes `previous`."""
    recover(base)
    current, previous, swap = base / CURRENT, base / PREVIOUS, base / SWAP
    if not slot_is_good(previous):
        raise SlotError("NO EARLIER SYSTEM IS KEPT ON THIS STICK")
    if current.exists():
        os.replace(current, swap)
        _fsync_dir(base)
        env.step("current-aside")
    os.replace(previous, current)
    _fsync_dir(base)
    env.step("previous-to-current")
    if swap.exists():
        os.replace(swap, previous)
        _fsync_dir(base)


def clear(base: pathlib.Path) -> None:
    """Forget every installed update: the stick boots its own ISO system again."""
    for name in (CURRENT, NEW, PREVIOUS, OLD, SWAP):
        if (base / name).exists():
            _remove(base / name)
    _fsync_dir(base)


def iso_sums(mounted_iso: pathlib.Path) -> dict[str, str]:
    """The live files' sums from the ISO's own sha256sum.txt (live-build writes it)."""
    try:
        sums = parse_sums((mounted_iso / "sha256sum.txt").read_text(encoding="ascii"))
    except (OSError, UnicodeError):
        return {}
    return {name: sums[path] for name, path in ISO_FILES.items() if path in sums}


@contextlib.contextmanager
def mounted(env: Env, source: pathlib.Path | str, options: str, mountpoint: pathlib.Path) -> Iterator[pathlib.Path]:
    mountpoint.mkdir(parents=True, exist_ok=True)
    result = run(env, ["mount", "-o", options, source, mountpoint])
    if result.returncode != 0:
        raise SlotError("COULD NOT OPEN THE STICK'S DATA AREA")
    try:
        yield mountpoint
    finally:
        run(env, ["umount", mountpoint])


def install_from_iso(base: pathlib.Path, iso: pathlib.Path, version: str, env: Env,
                     progress: Callable[[int], None] | None = None,
                     mountpoint: pathlib.Path = MOUNT_DIR / "iso") -> None:
    """Loop-mount a downloaded (already checksum-checked) ISO and install its live system."""
    with mounted(env, iso, "loop,ro", mountpoint) as root:
        sources = {name: root / path for name, path in ISO_FILES.items()}
        missing = [path for path in sources.values() if not path.is_file()]
        if missing:
            raise SlotError("THIS ISO HAS NO LIVE SYSTEM")
        install(base, sources, version, env, expected=iso_sums(root), progress=progress)


# ------------------------------------------------------------------ where the slot is


def find_slot_device(env: Env) -> tuple[str, str]:
    """(device, label) of the partition that holds live-update/, preferring couchliteos-sys."""
    for label in (SYS_LABEL, PERSISTENCE_LABEL):
        result = run(env, ["blkid", "-t", f"LABEL={label}", "-o", "device"])
        devices = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        if result.returncode == 0 and devices:
            return devices[0], label
    raise SlotError("NO STORAGE IS SET UP ON THIS STICK")


def mount_target(mountinfo_text: str, device_name: str) -> tuple[str, bool] | None:
    """Where `device_name` (as in /proc/self/mountinfo field 10) is mounted, and whether rw."""
    for line in mountinfo_text.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields, tail = left.split(), right.split()
        if len(fields) > 5 and len(tail) > 1 and tail[1] == device_name:
            target = fields[4].replace("\\040", " ")
            return target, "rw" in fields[5].split(",")
    return None


@contextlib.contextmanager
def slot_base(env: Env, mountinfo: pathlib.Path = pathlib.Path("/proc/self/mountinfo")) -> Iterator[pathlib.Path]:
    """Yield the writable live-update directory, mounting or remounting as needed.

    With the "persistence" design live-boot already has the partition mounted rw under
    /run/live/persistence. A couchliteos-sys partition is the live medium (mounted ro) when
    the stick booted an update, so it is remounted rw for the write and ro again afterwards.
    """
    device, _label = find_slot_device(env)
    try:
        text = mountinfo.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    found = mount_target(text, device)
    if found and found[1]:
        yield pathlib.Path(found[0]) / SLOT_DIR
    elif found:
        if run(env, ["mount", "-o", "remount,rw", found[0]]).returncode != 0:
            raise SlotError("COULD NOT WRITE TO THE STICK")
        try:
            yield pathlib.Path(found[0]) / SLOT_DIR
        finally:
            run(env, ["mount", "-o", "remount,ro", found[0]])
    else:
        with mounted(env, device, "rw,noatime", MOUNT_DIR / "slot") as root:
            yield root / SLOT_DIR


# ------------------------------------------------------------------ Ventoy


VENTOY_DM = "couchliteos-ventoy"


@contextlib.contextmanager
def ventoy_partition(env: Env, device: str, sectors: int, mountpoint: pathlib.Path,
                     writable: bool) -> Iterator[pathlib.Path]:
    """Mount Ventoy's data partition (where the ISO files are) while booted from it.

    Ventoy's own device-mapper map of the booted ISO holds the stick, so the partition
    itself cannot be mounted. A linear map over the partition can (Ventoy's documented
    workaround, doc_linux_remount): mount that instead and remove it afterwards.
    """
    mapper = pathlib.Path("/dev/mapper") / VENTOY_DM
    created = False
    if run(env, ["dmsetup", "info", VENTOY_DM]).returncode != 0:
        table = f"0 {sectors} linear {device} 0\n"
        if run(env, ["dmsetup", "create", VENTOY_DM], input_text=table).returncode != 0:
            raise SlotError("COULD NOT OPEN THE VENTOY PARTITION")
        created = True
        run(env, ["udevadm", "settle"])
    try:
        with mounted(env, mapper, "rw,noatime" if writable else "ro", mountpoint) as root:
            yield root
    finally:
        if created:
            run(env, ["dmsetup", "remove", VENTOY_DM])


def load_ventoy_json(path: pathlib.Path) -> dict:
    """The parsed ventoy.json, {} when absent. Refuses anything it could not rewrite safely."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise VentoyJsonError("COULD NOT READ VENTOY/VENTOY.JSON") from error
    try:
        data = json.loads(raw.decode("utf-8-sig")) if raw.strip() else {}
    except (UnicodeError, ValueError) as error:
        raise VentoyJsonError("VENTOY/VENTOY.JSON IS NOT VALID JSON: FIX OR REMOVE IT") from error
    if not isinstance(data, dict):
        raise VentoyJsonError("VENTOY/VENTOY.JSON IS NOT A JSON OBJECT")
    if "persistence" in data and not isinstance(data["persistence"], list):
        raise VentoyJsonError("VENTOY/VENTOY.JSON: PERSISTENCE IS NOT A LIST")
    return data


def save_ventoy_json(path: pathlib.Path, data: dict) -> None:
    """Replace ventoy.json in one rename (the Ventoy partition is usually exFAT)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".ventoy.", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, indent=4, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def with_persistence(data: dict, image: str, backend: str, autosel: int | None = 1) -> dict:
    """A copy of `data` whose persistence[] maps `image` to `backend`.

    Every other key and every other entry stays as it was. An existing entry for the same
    image gets the backend (keeping its own extra keys); otherwise one entry is appended.
    autosel=1 makes Ventoy boot with the data file without asking each time.
    """
    result = dict(data)
    entries = [dict(entry) if isinstance(entry, dict) else entry for entry in data.get("persistence", [])]
    for entry in entries:
        if isinstance(entry, dict) and entry.get("image") == image:
            entry["backend"] = backend
            break
    else:
        entry = {"image": image, "backend": backend}
        if autosel is not None:
            entry["autosel"] = autosel
        entries.append(entry)
    result["persistence"] = entries
    return result


def without_persistence(data: dict, image: str) -> dict:
    result = dict(data)
    if "persistence" in data:
        result["persistence"] = [entry for entry in data["persistence"]
                                 if not (isinstance(entry, dict) and entry.get("image") == image)]
    return result


def repoint_default(data: dict, old_image: str, new_image: str) -> dict:
    """Follow the update in control[] VTOY_DEFAULT_IMAGE, but only if it named the old ISO."""
    control = data.get("control")
    if not isinstance(control, list):
        return data
    result = dict(data)
    result["control"] = [
        {**item, "VTOY_DEFAULT_IMAGE": new_image}
        if isinstance(item, dict) and item.get("VTOY_DEFAULT_IMAGE") == old_image else item
        for item in control
    ]
    return result


def image_path(ventoy_root: pathlib.Path, iso: pathlib.Path) -> str:
    """The form ventoy.json uses: an absolute path from the Ventoy partition's root."""
    return "/" + iso.relative_to(ventoy_root).as_posix()


def find_ventoy_iso(ventoy_root: pathlib.Path, size: int, max_depth: int = 4) -> list[pathlib.Path]:
    """ISO files on the Ventoy partition whose size equals the booted image's.

    Ventoy maps the booted ISO as /dev/mapper/ventoy, which has exactly the file's size.
    CouchLiteOS ISOs sort first when several files match.
    """
    found: list[pathlib.Path] = []
    for directory, subdirs, files in os.walk(ventoy_root):
        depth = len(pathlib.Path(directory).relative_to(ventoy_root).parts)
        subdirs[:] = [name for name in subdirs if depth < max_depth - 1
                      and name not in ("ventoy", "System Volume Information", "$RECYCLE.BIN")]
        for name in files:
            path = pathlib.Path(directory) / name
            if name.lower().endswith(".iso"):
                with contextlib.suppress(OSError):
                    if path.stat().st_size == size:
                        found.append(path)
    return sorted(found, key=lambda path: (not path.name.lower().startswith("couchliteos"), str(path)))


def ventoy_install_iso(ventoy_root: pathlib.Path, current_iso: pathlib.Path, new_iso: pathlib.Path,
                       name: str, version: str, env: Env, expected_sha: str = "",
                       state_file: pathlib.Path = STATE_FILE,
                       progress: Callable[[int], None] | None = None) -> pathlib.Path:
    """Copy a downloaded ISO next to the running one and give it the same persistence.

    The old ISO and its ventoy.json entry stay until `ventoy_finish` sees the new system
    reach the launcher, so a new ISO that fails to start leaves the old one in Ventoy's menu.
    """
    if "/" in name or not name.lower().endswith(".iso"):
        raise ValueError("name must be a plain .iso file name")
    config = ventoy_root / VENTOY_JSON
    data = load_ventoy_json(config)  # refuse before copying gigabytes
    destination = current_iso.parent / name
    if destination == current_iso:
        raise SlotError("THIS VERSION IS ALREADY ON THE STICK")
    needed = new_iso.stat().st_size + MARGIN
    if env.free_bytes(ventoy_root) < needed:
        missing = -(-(needed - env.free_bytes(ventoy_root)) // (1 << 20))
        raise SlotError(f"NOT ENOUGH SPACE ON THE STICK: {missing} MB MORE NEEDED")
    part = destination.with_name(destination.name + ".part")
    got = copy_hashed(new_iso, part, progress)
    if (expected_sha and expected_sha.lower() != got) or sha256_file(part) != got:
        _remove(part)
        raise SlotError("THE STICK DID NOT KEEP THE UPDATE: IT MAY BE FAILING")
    os.replace(part, destination)
    _fsync_dir(destination.parent)
    old_image, new_image = image_path(ventoy_root, current_iso), image_path(ventoy_root, destination)
    backend = VENTOY_BACKEND
    for entry in data.get("persistence", []):
        if isinstance(entry, dict) and entry.get("image") == old_image and entry.get("backend"):
            backend = entry["backend"]
            break
    save_ventoy_json(config, repoint_default(with_persistence(data, new_image, backend), old_image, new_image))
    state = {"mode": "ventoy", "old_iso": old_image, "new_iso": new_image, "version": version}
    state_file.parent.mkdir(parents=True, exist_ok=True)
    _write_synced(state_file, json.dumps(state) + "\n")
    return destination


def read_state(state_file: pathlib.Path = STATE_FILE) -> dict:
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def ventoy_finish(ventoy_root: pathlib.Path, running_image: str, launcher_ready: bool,
                  state_file: pathlib.Path = STATE_FILE) -> str:
    """After a good boot of the new ISO, delete the old ISO and its persistence entry.

    Returns what happened: "", "waiting" (not yet booted into the new one) or "done".
    """
    state = read_state(state_file)
    if state.get("mode") != "ventoy":
        return ""
    old, new = state.get("old_iso", ""), state.get("new_iso", "")
    if not (launcher_ready and running_image == new and old and old != new):
        return "waiting"
    config = ventoy_root / VENTOY_JSON
    data = load_ventoy_json(config)
    save_ventoy_json(config, without_persistence(data, old))
    target = ventoy_root / old.lstrip("/")
    if target.resolve().is_relative_to(ventoy_root.resolve()):
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
    with contextlib.suppress(FileNotFoundError):
        state_file.unlink()
    return "done"
