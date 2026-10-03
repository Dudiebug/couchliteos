"""Root's file writes that never go through a name the couchliteos user could swap.

/run/couchliteos, /var/lib/couchliteos and /var/log/couchliteos belong to the launcher's user,
who can replace any entry in them (a symlink, a renamed directory) between a check and a use.
So root writes there only through a descriptor of the directory: a new temporary file is made
with O_EXCL|O_NOFOLLOW, its mode and owner are set on the descriptor, and it is renamed over the
target name (rename replaces a symlink, it never follows it).

Root's own work (mount points, filter files, locks, logs) lives in directories only root can
write (`root_dir`): /run/couchliteos-update, /var/log/couchliteos-update, /var/lib/couchliteos-update.

Used by couchliteos_updater, couchliteos_snapshot and couchliteos_browser. Standard library only.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import secrets
import stat
from collections.abc import Callable

OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class NotOurs(PermissionError):
    """A root work directory is a symlink, not a directory, someone else's or writable by others."""


def write_atomic(path: pathlib.Path, text: str | bytes, mode: int, owner: tuple[int, int] | None = None,
                 chown: Callable[..., None] = os.chown) -> None:
    """Write `text` to `path` so that readers see the old or the new file, never half of it.

    The directory is opened once (refused when it is itself a symlink) and everything after that
    goes through its descriptor: a symlinked temporary or target is never followed. `chown` is
    called with the temporary's descriptor (os.chown takes one).
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8") if isinstance(text, str) else text
    directory = os.open(path.parent, OPEN_DIR)
    try:
        while True:
            temporary = f".{path.name}.{secrets.token_hex(6)}"
            try:
                descriptor = os.open(temporary, NEW_FILE, 0o600, dir_fd=directory)
                break
            except FileExistsError:
                continue
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), mode)
                if owner is not None:
                    chown(stream.fileno(), *owner)
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
            raise
    finally:
        os.close(directory)


def root_dir(path: pathlib.Path, euid: int, mode: int = 0o700) -> pathlib.Path:
    """Make or check a directory only `euid` can write; raises NotOurs for anything else.

    A missing one is made with `mode`; a real directory of ours with another mode is put right
    (its parent is root's, so nobody else made it). A symlink, a non-directory, someone else's
    directory or one others can write is refused, never used.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, mode)
    found = os.lstat(path)
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != euid:
        raise NotOurs(f"{path} is not a directory of uid {euid}")
    if stat.S_IMODE(found.st_mode) != mode:
        descriptor = os.open(path, OPEN_DIR)
        try:
            if os.fstat(descriptor).st_uid != euid:
                raise NotOurs(f"{path} changed owner")
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)
    return path


def append_line(path: pathlib.Path, text: str, euid: int | None = None, dir_mode: int = 0o755) -> None:
    """Append one line to a log in a root directory (made or checked with `root_dir`), never
    through a symlink and only to a regular file. Raises OSError."""
    path = pathlib.Path(path)
    root_dir(path.parent, os.geteuid() if euid is None else euid, dir_mode)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         0o644)
    with os.fdopen(descriptor, "a", encoding="utf-8", errors="replace") as stream:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise NotOurs(f"{path} is not a regular file")
        stream.write(text.rstrip("\n") + "\n")


def open_lock(path: pathlib.Path):
    """The lock file in a root work directory, opened without following a symlink."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    return os.fdopen(descriptor, "r+")
