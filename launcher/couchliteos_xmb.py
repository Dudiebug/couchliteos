"""The home screen as a PS3-style cross: categories left to right, items top to bottom.

No drawing here. couchliteos_gtk_xmb draws `categories`, sends Left/Right to `move_h`, Up/Down
to `move_v`, A/Enter to `activate` and B/Esc to `back`, and carries out the action tuple that
`activate` returns. The home screen's actions (couchliteos_home) plus:

    ("entry", kind, target)  a Settings entry (couchliteos_tvscreens.Entry kind and target)
    ("power", request)       "suspend", "reboot" or "poweroff", asked first as PowerModel asks
    ("updates",)             CHECK FOR UPDATES: switch the start-up check on or off

Categories, left to right (a boot lands on GAMES, on the last game played):

    POWER       SLEEP, RESTART, SHUT DOWN
    SETTINGS    every Settings entry, with its current value as the detail line
    TV & VIDEO  web browsers and web applications (Netflix, YouTube...), else ADD A STREAMING SERVICE
    GAMES       every paired PC's games, newest first; game streaming apps (chiaki-ng); GAMING PCS
    APPS        the other applications
    NETWORK     the network's state, Wi-Fi, Tailscale, Remote Desktop connections

An application goes where its manifest's `category` says, else by what it is (APP_CATEGORIES,
then a browser or web application is TV & VIDEO, a Remote Desktop connection is NETWORK).
Each category remembers its own item; `reload` keeps the focused item focused when it still
exists. Nothing wraps: a move against an edge returns EDGE (the front end's bump sound).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Mapping

import couchliteos_apps as apps
import couchliteos_home as home
import couchliteos_tvscreens as tvscreens

POWER, SETTINGS, VIDEO, GAMES, APPS, NETWORK = "power", "settings", "video", "games", "apps", "network"
ORDER = (POWER, SETTINGS, VIDEO, GAMES, APPS, NETWORK)
LABELS = {POWER: "POWER", SETTINGS: "SETTINGS", VIDEO: "TV & VIDEO", GAMES: "GAMES", APPS: "APPS",
          NETWORK: "NETWORK"}
ICONS = {POWER: "power", SETTINGS: "gear", VIDEO: "television", GAMES: "game-controller",
         APPS: "squares-four", NETWORK: "globe"}
START = GAMES

MOVED, EDGE, STILL = "moved", "edge", "still"

# Where the built-in applications go (a manifest's own `category` wins).
APP_CATEGORIES = {
    "chiaki-ng": GAMES, "firefox": VIDEO, "google-chrome": VIDEO, "tailscale": NETWORK,
    "network-setup": NETWORK, "terminal": APPS, "system-diagnostics": APPS, "audio-test": APPS,
}
APP_ICONS = {
    "chiaki-ng": "game-controller", "firefox": "browser", "google-chrome": "browser", "tailscale": "shield",
    "network-setup": "wifi-high", "terminal": "terminal-window", "system-diagnostics": "stethoscope",
    "audio-test": "speaker-high",
}
BROWSERS = {"firefox", "google-chrome"}
# Settings entries that belong elsewhere on the cross, or nowhere (BACK has no meaning here).
SETTINGS_SKIP = {"BACK", "TAILSCALE", "ACTIVE APPLICATIONS"}
ENTRY_ICONS = {
    "DISPLAY": "monitor", "APPEARANCE": "paint-brush", "AUDIO": "speaker-high", "BLUETOOTH": "bluetooth",
    "CONTROLLERS": "game-controller", "NETWORK": "wifi-high", "SLEEP & SCREEN": "moon",
    "APPLICATIONS": "squares-four", "REMOTE DESKTOP": "desktop", "STREAMING": "broadcast",
    "TV CONTROL": "television", "SOFTWARE UPDATE": "download", "CHECK FOR UPDATES": "arrows-clockwise",
    "CONTROLS": "keyboard", "SETUP WIZARD": "magic-wand", "GENERATE SUPPORT FILE": "lifebuoy",
    "SYSTEM DIAGNOSTICS": "stethoscope",
}
POWER_ICONS = {"suspend": "moon", "reboot": "arrows-clockwise", "poweroff": "power"}
NO_VIDEO = "ADD A STREAMING SERVICE"
WEB_URL_RE = re.compile(r"https?://[^\s'\"]+")

Action = tuple


@dataclasses.dataclass(frozen=True)
class Item:
    key: str  # stable across reloads
    label: str  # upper case, as shown
    action: Action | None
    detail: str = ""  # the second line: a game's PC, a setting's value
    icon: str = ""  # a bundled icon name (couchliteos_icons)
    app: str = ""  # the apps.d id, for the application's own icon and artwork
    url: str = ""  # a web application's address, for the site's own icon
    game: tuple[str, str] | None = None  # (host label, app name) of a game, for its artwork
    played: float | None = None


@dataclasses.dataclass(frozen=True)
class Category:
    key: str
    label: str
    icon: str
    items: tuple[Item, ...]


def web_url(app: apps.Application) -> str:
    """The address a web application opens (its kiosk browser command), else ""."""
    if app.kind != "command":
        return ""
    match = WEB_URL_RE.search(app.arguments)
    return match.group(0) if match else ""


def category_of(app: apps.Application) -> str:
    if app.category in (VIDEO, GAMES, APPS, NETWORK):
        return app.category
    if app.id in APP_CATEGORIES:
        return APP_CATEGORIES[app.id]
    if app.kind == "rdp":
        return NETWORK
    if web_url(app):
        return VIDEO
    return APPS


def icon_of(app: apps.Application) -> str:
    if app.icon:
        return app.icon
    if app.id in APP_ICONS:
        return APP_ICONS[app.id]
    if app.kind == "rdp":
        return "desktop"
    if web_url(app):
        return "play-circle"
    return "app-window"


class XmbModel:
    """`home` supplies the games, applications and top bar; `settings` the Settings values;
    `power` the power choices (re-made by the front end when what is running changes)."""

    def __init__(
        self,
        home_model: home.HomeModel,
        settings: tvscreens.SettingsModel | None = None,
        power: Callable[[], tvscreens.PowerModel] | None = None,
        applications: Callable[[], apps.LoadResult] | None = None,
        network: Callable[[], str] | None = None,
    ) -> None:
        self.home = home_model
        self.settings = settings or tvscreens.SettingsModel()
        self.power = power or (lambda: tvscreens.PowerModel(can_sleep=True, can_wake=True))
        self.applications = applications or home_model.applications
        self.network = network or home_model.network
        self.categories: list[Category] = []
        self.column = ORDER.index(START)
        self.rows: dict[str, int] = {}  # each category's own focused item
        self.reload(reread_home=False)
        self.focus_last_played()

    # ------------------------------------------------------------------ building

    def _apps(self) -> dict[str, list[Item]]:
        placed: dict[str, list[Item]] = {key: [] for key in ORDER}
        try:
            result = self.applications()
        except (OSError, ValueError):
            return placed
        for app in result.applications:
            if not (app.enabled and app.visible) or app.id == home.STREAMED_APP:
                continue
            try:
                if not self.home.installed(app):
                    continue
            except OSError:
                continue
            placed[category_of(app)].append(Item(
                f"app:{app.id}", app.name.upper(), ("app", app.id), icon=icon_of(app), app=app.id,
                url=web_url(app), detail="WEB" if web_url(app) else "",
            ))
        return placed

    def _games(self, extra: list[Item]) -> tuple[Item, ...]:
        games = next((row.tiles for row in self.home.rows if row.name == home.GAMES), ())
        items = [
            Item(tile.key, tile.label, tile.action, detail=tile.detail, icon="game-controller",
                 game=(tile.detail, tile.action[2]) if tile.action[0] == "stream" else None, played=tile.played)
            for tile in games if tile.key != "game:none"
        ]
        items += extra
        items.append(Item("games:hosts", "GAMING PCS", ("hosts",), detail="PAIR, WAKE AND MANAGE",
                          icon="desktop-tower"))
        if not any(item.game for item in items):
            items.insert(0, Item("game:none", home.NO_GAMES, ("hosts",), icon="plus-circle"))
        return tuple(items)

    def _settings(self) -> tuple[Item, ...]:
        return tuple(
            Item(f"entry:{entry.target}", entry.label,
                 ("updates",) if entry.target == "updates" else ("entry", entry.kind, entry.target),
                 detail=self.settings.values.get(entry.label, "") or entry.help,
                 icon=ENTRY_ICONS.get(entry.label, "gear"))
            for entry in self.settings.entries if entry.label not in SETTINGS_SKIP
        )

    def _power(self) -> tuple[Item, ...]:
        try:
            model = self.power()
        except Exception:  # noqa: BLE001 - the power column must always be there
            model = tvscreens.PowerModel(can_sleep=False, can_wake=False)
        items = [
            Item(f"power:{choice.request}", choice.label,
                 ("power", choice.request) if choice.request and (choice.request != "suspend" or model.can_sleep) else None,
                 detail=choice.question, icon=POWER_ICONS[choice.request])
            for choice in model.choices if choice.request
        ]
        items.append(Item("power:active", "ACTIVE APPLICATIONS", ("entry", tvscreens.VIEW, "active"),
                          detail="BRING BACK OR CLOSE A RUNNING APPLICATION", icon="stack"))
        return tuple(items)

    def _network(self, extra: list[Item]) -> tuple[Item, ...]:
        try:
            state = self.network() or ""
        except Exception:  # noqa: BLE001
            state = ""
        status = Item("network:status", "INTERNET", ("entry", tvscreens.SCREEN, "network"),
                      detail=state.upper(), icon="globe")
        wifi = Item("network:wifi", "WI-FI AND WIRED", ("entry", tvscreens.SCREEN, "network"),
                    detail="CONNECT TO A NETWORK", icon="wifi-high")
        remote = Item("network:remote", "REMOTE DESKTOP", ("entry", tvscreens.SCREEN, "remote-desktop"),
                      detail="SAVED REMOTE DESKTOP CONNECTIONS", icon="desktop")
        return (status, wifi, *extra, remote)

    def reload(self, *, reread_home: bool = True) -> None:
        """Read everything again; the focused item stays focused while it exists."""
        before = self.focused()
        if reread_home:
            self.home.reload()
        placed = self._apps()
        video = tuple(placed[VIDEO]) or (
            Item("video:add", NO_VIDEO, ("entry", tvscreens.SCREEN, "applications"),
                 detail="ADD A WEB APPLICATION OR A BROWSER", icon="plus-circle"),)
        items = {
            POWER: self._power(),
            SETTINGS: self._settings(),
            VIDEO: video,
            GAMES: self._games(placed[GAMES]),
            APPS: tuple(placed[APPS]) or (Item("apps:add", "ADD AN APPLICATION",
                                               ("entry", tvscreens.SCREEN, "applications"), icon="plus-circle"),),
            NETWORK: self._network(placed[NETWORK]),
        }
        self.categories = [Category(key, LABELS[key], ICONS[key], items[key]) for key in ORDER]
        for category in self.categories:
            self.rows[category.key] = min(self.rows.get(category.key, 0), len(category.items) - 1)
        if before is not None:
            self.focus_key(before.key, category=self.categories[self.column].key)

    # ------------------------------------------------------------------ focus

    @property
    def category(self) -> Category:
        return self.categories[self.column]

    @property
    def row(self) -> int:
        return self.rows[self.category.key]

    def focused(self) -> Item | None:
        if not self.categories:
            return None
        items = self.category.items
        return items[self.row] if items else None

    def focus_key(self, key: str, category: str | None = None) -> bool:
        """Focus the item with this key (in `category` first: one item can be in two); False when none."""
        order = sorted(range(len(self.categories)), key=lambda index: self.categories[index].key != category)
        for index in order:
            for row, item in enumerate(self.categories[index].items):
                if item.key == key:
                    self.column, self.rows[self.categories[index].key] = index, row
                    return True
        return False

    def focus_last_played(self) -> bool:
        """GAMES, first item: the most recently played game. True when it has been played."""
        self.column = ORDER.index(GAMES)
        self.rows[GAMES] = 0
        first = self.focused()
        return first is not None and first.played is not None

    def move_h(self, dx: int) -> str:
        column = self.column + (1 if dx > 0 else -1)
        if not 0 <= column < len(self.categories):
            return EDGE
        self.column = column
        return MOVED

    def move_v(self, dy: int) -> str:
        key = self.category.key
        row = self.rows[key] + (1 if dy > 0 else -1)
        if not 0 <= row < len(self.category.items):
            return EDGE
        self.rows[key] = row
        return MOVED

    def back(self) -> str:
        """B/Esc: the top of the category, then GAMES' first item."""
        if self.row:
            self.rows[self.category.key] = 0
            return MOVED
        if self.category.key != GAMES:
            self.column = ORDER.index(GAMES)
            return MOVED
        return STILL

    def activate(self) -> Action | None:
        item = self.focused()
        return item.action if item is not None else None

    def status(self) -> home.Status:
        return self.home.status()

    def settings_values(self, values: Mapping[str, str]) -> None:
        """New Settings values (read on a worker): the detail lines change, the focus stays."""
        self.settings.values = dict(values)
        before = self.focused()
        self.categories[ORDER.index(SETTINGS)] = Category(SETTINGS, LABELS[SETTINGS], ICONS[SETTINGS], self._settings())
        if before is not None:
            self.focus_key(before.key, category=self.category.key)
