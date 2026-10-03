"""Recently played games: which (host, app) streamed last, kept across restarts.

The home screen's GAMES row puts these first, most recent first, and lands on the
newest one at boot and after a stream. The store is one small JSON file, rewritten
atomically on each start of a stream:

    {"version": 1, "games": [{"host": UUID, "app": NAME, "played": EPOCH}, ...]}

`games` is most recent first and never longer than LIMIT. A missing, unreadable or
damaged file reads as "nothing played yet": the order only ever falls back to the
hosts' own order, it never stops the launcher.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import time
from collections.abc import Callable, Iterable
from typing import TypeVar

import couchliteos_stream as stream

PATH = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos")) / "recent.json"
LIMIT = 200
VERSION = 1
MAX_BYTES = 256 * 1024  # 200 entries of the longest names fit with room to spare
HOST_MAX = 253

Key = tuple[str, str]  # (host uuid, app name)
T = TypeVar("T")


def _valid(host: object, app: object) -> bool:
    return (
        isinstance(host, str) and isinstance(app, str)
        and 0 < len(host) <= HOST_MAX and host.isprintable()
        and 0 < len(app) <= stream.APP_MAX and app.isprintable()
    )


def host_key(host: stream.Host) -> str:
    """A PC's identity in the list: Moonlight's uuid for it, else its name."""
    return host.uuid or host.name


def load(path: pathlib.Path = PATH) -> dict[Key, float]:
    """{(host, app): when it was last played}, most recent first. Bad entries are skipped."""
    try:
        if path.is_symlink() or path.stat().st_size > MAX_BYTES:
            return {}
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    games = data.get("games") if isinstance(data, dict) else None
    if not isinstance(games, list):
        return {}
    found: dict[Key, float] = {}
    for entry in games:
        if not isinstance(entry, dict):
            continue
        host, app, played = entry.get("host"), entry.get("app"), entry.get("played")
        if not _valid(host, app) or isinstance(played, bool) or not isinstance(played, (int, float)):
            continue
        if not math.isfinite(played) or (host, app) in found:
            continue
        found[(host, app)] = float(played)
        if len(found) >= LIMIT:
            break
    # Stored newest first; sort anyway so a hand-edited file still orders by time.
    return dict(sorted(found.items(), key=lambda item: -item[1]))


def record(host_uuid: str, app: str, path: pathlib.Path = PATH, now: Callable[[], float] = time.time) -> None:
    """Mark `app` on `host_uuid` as played now. Raises ValueError for unusable names, OSError on disk errors."""
    if not _valid(host_uuid, app):
        raise ValueError("the host or application name is not usable")
    history = load(path)
    history.pop((host_uuid, app), None)
    entries = [{"host": host_uuid, "app": app, "played": float(now())}]
    entries += [{"host": host, "app": name, "played": played} for (host, name), played in history.items()]
    body = json.dumps({"version": VERSION, "games": entries[:LIMIT]}, ensure_ascii=True, indent=1)
    stream._atomic_write(path, (body + "\n").encode("ascii"))


def ordered(
    games: Iterable[T], key: Callable[[T], Key] = lambda game: game, history: dict[Key, float] | None = None,
    path: pathlib.Path = PATH,
) -> list[T]:
    """`games` with the played ones first, most recent first; the rest keep their order.

    `key` gives each game's (host uuid, app name). `history` is a `load()` result; it is
    read from `path` when not given."""
    if history is None:
        history = load(path)
    items = list(games)
    rank = {game_key: index for index, game_key in enumerate(history)}  # history is newest first
    return sorted(items, key=lambda game: rank.get(key(game), len(rank)))
