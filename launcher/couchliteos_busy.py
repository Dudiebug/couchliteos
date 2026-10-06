"""Is the box in use? Background updates (security fixes, app updates) wait while it is.

The box is busy while an application or a stream is open (couchliteos-run-app and the
configured-app runner write /run/couchliteos/app-active, the clients a `<name>-ready` file),
while a CouchLiteOS update is requested or running (its request file, its lock), and while
any other program holds a blocking sleep, idle or shutdown inhibitor, which is how the
updater keeps the box awake. The caller's own inhibitor (the `systemd-inhibit` it runs
under) does not count.

Standard library only; every probe is injectable so the tests need no running system.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from collections.abc import Callable

RUN_DIR = pathlib.Path("/run/couchliteos")
UPDATE_REQUEST = "update-install"
UPDATE_LOCK = "update.lock"
BUSCTL = (
    "busctl", "--json=short", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
    "org.freedesktop.login1.Manager", "ListInhibitors",
)
INHIBIT_WHAT = ("sleep", "idle", "shutdown")


def lock_held(path: pathlib.Path) -> bool:
    """True when another process holds the flock on `path` (the updater's lock)."""
    try:
        import fcntl  # noqa: PLC0415 - Linux only; the rest of the module works anywhere
    except ImportError:
        return False
    try:
        with open(path, "rb") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def parse_inhibitors(text: str) -> list[tuple[str, str, str, str, int, int]]:
    """`busctl --json=short` output of ListInhibitors as (what, who, why, mode, uid, pid) tuples."""
    try:
        data = json.loads(text)
        rows = data["data"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return []
    inhibitors = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, list) and len(row) == 6:
            what, who, why, mode, uid, pid = row
            if all(isinstance(value, str) for value in (what, who, why, mode)) and all(
                isinstance(value, int) for value in (uid, pid)
            ):
                inhibitors.append((what, who, why, mode, uid, pid))
    return inhibitors


def list_inhibitors(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list:
    try:
        result = runner(list(BUSCTL), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                        check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return []
    return parse_inhibitors(result.stdout)


def reason(
    run_dir: pathlib.Path = RUN_DIR,
    *,
    inhibitors: Callable[[], list] | None = None,
    own_pids: tuple[int, ...] | None = None,
    held: Callable[[pathlib.Path], bool] = lock_held,
) -> str:
    """Why the box is busy ("app", "update", "inhibited"), or "" when background work may run."""
    if (run_dir / "app-active").exists():
        return "app"
    try:
        if any(path.name.endswith("-ready") for path in run_dir.iterdir()):
            return "app"
    except OSError:
        pass
    if (run_dir / UPDATE_REQUEST).exists() or held(run_dir / UPDATE_LOCK):
        return "update"
    if own_pids is None:
        own_pids = (os.getpid(), os.getppid())
    for what, _who, _why, mode, _uid, pid in (inhibitors or list_inhibitors)():
        if mode == "block" and pid not in own_pids and any(kind in what.split(":") for kind in INHIBIT_WHAT):
            return "inhibited"
    return ""
