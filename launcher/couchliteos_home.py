"""The home screen as plain Python: rows of tiles, the focus, and what a press does.

No drawing here. A front end (the GTK home screen) shows `rows`, sends the arrow keys to
`move`, A/Enter to `activate` and B/Esc to `back`, and carries out the action tuple that
`activate` returns:

    ("stream", host, app)   stream `app` (a name) from `host` (a stream.Host)
    ("app", app_id)         start the application with that apps.d id
    ("settings",)           open Settings
    ("hosts",)              pair and manage gaming PCs (Moonlight)
    ("power",)              the power menu

Rows, top to bottom:
    GAMES   every paired PC's apps in one list, most recently played first
    APPS    enabled, visible applications whose program is installed (not Moonlight: its
            games are in GAMES and pairing is HOSTS)
    SYSTEM  SETTINGS, HOSTS, POWER

The focus is (row, column). Up and down skip empty rows and each row remembers its own
column, as console carousels do. Nothing wraps. `reload` keeps the focus on the same
tile when it still exists, so a re-read after a stream stays put.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import time
from collections.abc import Callable

import couchliteos_apps as apps
import couchliteos_recent as recent
import couchliteos_stream as stream

GAMES, APPS, SYSTEM = "games", "apps", "system"
ROW_TITLES = {GAMES: "GAMES", APPS: "APPS", SYSTEM: "SYSTEM"}
SYSTEM_TILES = (("SETTINGS", ("settings",)), ("HOSTS", ("hosts",)), ("POWER", ("power",)))
NO_GAMES = "ADD A GAMING PC"  # the GAMES row is never blank: with no PC paired it offers pairing
STREAMED_APP = "moonlight"  # its games are tiles of their own; pairing is HOSTS
# Programs started through a request file (couchliteos-run-app): the tile shows only when it is there.
REQUEST_BINARIES = {
    "start-moonlight": "opt/couchliteos/apps/moonlight/usr/bin/moonlight",
    "start-chiaki": "opt/couchliteos/apps/chiaki-ng/usr/bin/chiaki",
    "start-firefox": "usr/bin/firefox-esr",
}

Action = tuple


@dataclasses.dataclass(frozen=True)
class Tile:
    key: str  # stable identity across reloads ("game:<host>/<app>", "app:<id>", "system:<name>")
    label: str  # upper case, as shown
    action: Action
    detail: str = ""  # a game's PC name (upper case)
    played: float | None = None  # when a game was last played (epoch seconds), None if never


@dataclasses.dataclass(frozen=True)
class Row:
    name: str  # GAMES, APPS or SYSTEM
    title: str
    tiles: tuple[Tile, ...]


@dataclasses.dataclass(frozen=True)
class Status:
    """The top bar. Every field is "" (or False) when its source has nothing or fails."""

    clock: str  # "21:04"
    network: str  # "ONLINE" / "OFFLINE", or what the front end's network source says
    battery: str  # the controllers' battery line
    battery_low: bool
    update: str  # the newer release on offer, "" when none


def installed(app: apps.Application, root: pathlib.Path = pathlib.Path("/")) -> bool:
    """Whether the program behind a tile is on this system (browsers are installed on demand)."""
    if app.kind == "command":
        return os.access(root / app.command.lstrip("/"), os.X_OK)
    if app.kind == "request" and app.request in REQUEST_BINARIES:
        return os.access(root / REQUEST_BINARIES[app.request], os.X_OK)
    return True  # remote desktop connections and requests this table does not know


def game_key(host: str, app: str) -> str:
    return f"game:{host}/{app}"


def link_status() -> str:
    return "ONLINE" if stream.link_up() else "OFFLINE"


def _text(source: Callable[[], object]) -> str:
    try:
        value = source()
    except Exception:  # noqa: BLE001 - the top bar must never take the home screen down
        return ""
    return value if isinstance(value, str) else ""


class HomeModel:
    def __init__(
        self,
        hosts: Callable[[], list[stream.Host]] = stream.load_hosts,
        applications: Callable[[], apps.LoadResult] = apps.visible_applications,
        installed: Callable[[apps.Application], bool] = installed,
        history: Callable[[], dict[recent.Key, float]] = recent.load,
        controllers: object | None = None,  # controllers.Monitor: line(), low()
        updates: object | None = None,  # update.Checker: available()
        network: Callable[[], str] = link_status,
        clock: Callable[[], time.struct_time] = time.localtime,
    ) -> None:
        self.hosts = hosts
        self.applications = applications
        self.installed = installed
        self.history = history
        self.controllers = controllers
        self.updates = updates
        self.network = network
        self.clock = clock
        self.rows: list[Row] = []
        self.errors: tuple[str, ...] = ()  # apps.d entries that failed to load
        self.row = 0
        self.columns: dict[str, int] = {}  # each row's own column
        self.reload()
        self.focus_last_played()  # a boot lands on the last game

    # ------------------------------------------------------------------ building

    def _games(self) -> tuple[Tile, ...]:
        try:
            hosts = list(self.hosts())
        except (OSError, ValueError):
            hosts = []
        try:
            history = self.history()
        except (OSError, ValueError):
            history = {}
        games: dict[recent.Key, stream.Host] = {}  # (host key, app) -> host, in host order
        for host in hosts:
            for app in host.apps:
                games.setdefault((recent.host_key(host), app), host)
        tiles = tuple(
            Tile(game_key(*key), key[1].upper(), ("stream", games[key], key[1]), detail=games[key].label, played=history.get(key))
            for key in recent.ordered(games, history=history)
        )
        return tiles or (Tile("game:none", NO_GAMES, ("hosts",)),)

    def _apps(self) -> tuple[Tile, ...]:
        try:
            result = self.applications()
        except (OSError, ValueError):
            result = apps.LoadResult((), ())
        self.errors = tuple(result.errors)
        tiles: list[Tile] = []
        for app in result.applications:
            if not (app.enabled and app.visible) or app.id == STREAMED_APP:
                continue
            try:
                if not self.installed(app):
                    continue
            except OSError:
                continue
            tiles.append(Tile(f"app:{app.id}", app.name.upper(), ("app", app.id)))
        return tuple(tiles)

    def reload(self) -> None:
        """Read the hosts, applications and recent games again; the focused tile stays focused."""
        before = self.focused()
        self.rows = [
            Row(GAMES, ROW_TITLES[GAMES], self._games()),
            Row(APPS, ROW_TITLES[APPS], self._apps()),
            Row(SYSTEM, ROW_TITLES[SYSTEM], tuple(Tile(f"system:{label.lower()}", label, action) for label, action in SYSTEM_TILES)),
        ]
        for row in self.rows:
            self.columns[row.name] = min(self.columns.get(row.name, 0), max(0, len(row.tiles) - 1))
        if before is not None and self.focus_key(before.key):
            return
        self.row = min(self.row, len(self.rows) - 1)
        if not self.rows[self.row].tiles:  # the row emptied (an app was removed): nearest row with tiles
            self.row = min(
                (index for index, row in enumerate(self.rows) if row.tiles),
                key=lambda index: (abs(index - self.row), index),
            )

    # ------------------------------------------------------------------ focus

    @property
    def focus(self) -> tuple[int, int]:
        """(row index, column) of the focused tile."""
        return self.row, self.columns[self.rows[self.row].name]

    def focused(self) -> Tile | None:
        if not self.rows:
            return None
        row = self.rows[self.row]
        return row.tiles[self.columns[row.name]] if row.tiles else None

    def focus_key(self, key: str) -> bool:
        """Focus the tile with this key; False (focus unchanged) when there is none."""
        for index, row in enumerate(self.rows):
            for column, tile in enumerate(row.tiles):
                if tile.key == key:
                    self.row, self.columns[row.name] = index, column
                    return True
        return False

    def focus_game(self, host_uuid: str, app: str) -> bool:
        return self.focus_key(game_key(host_uuid, app))

    def focus_last_played(self) -> bool:
        """Focus the most recently played game (after a stream, at boot), else the first game.
        True when the focused game has been played."""
        self.row, self.columns[GAMES] = 0, 0  # GAMES is ordered newest first and never empty
        return self.rows[0].tiles[0].played is not None

    def move(self, dx: int, dy: int) -> bool:
        """Move the focus by one step per sign; True when it moved (for the move sound)."""
        before = self.focus
        if dy:
            step = 1 if dy > 0 else -1
            index = self.row + step
            while 0 <= index < len(self.rows) and not self.rows[index].tiles:
                index += step
            if 0 <= index < len(self.rows):
                self.row = index
        if dx:
            row = self.rows[self.row]
            column = self.columns[row.name] + (1 if dx > 0 else -1)
            self.columns[row.name] = max(0, min(column, len(row.tiles) - 1))
        return self.focus != before

    def back(self) -> bool:
        """B/Esc on the home screen: return to the start of GAMES. True when the focus moved."""
        before = self.focus
        self.row, self.columns[GAMES] = 0, 0
        return self.focus != before

    def activate(self) -> Action | None:
        tile = self.focused()
        return tile.action if tile is not None else None

    # ------------------------------------------------------------------ top bar

    def status(self) -> Status:
        def clock() -> str:
            return time.strftime("%H:%M", self.clock())

        def low() -> bool:
            try:
                return bool(self.controllers.low()) if self.controllers is not None else False
            except Exception:  # noqa: BLE001
                return False

        controllers, updates = self.controllers, self.updates
        return Status(
            clock=_text(clock),
            network=_text(self.network),
            battery=_text(controllers.line) if controllers is not None else "",
            battery_low=low(),
            update=_text(updates.available) if updates is not None else "",
        )
