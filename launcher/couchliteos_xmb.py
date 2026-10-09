"""The home screen as a PS3-style cross: categories left to right, items top to bottom.

No drawing here. couchliteos_gtk_xmb draws `categories`, sends Left/Right to `move_h`, Up/Down
to `move_v`, A/Enter to `activate` and B/Esc to `back`, and carries out the action tuple that
`activate` returns. The home screen's actions (couchliteos_home) plus:

    ("entry", kind, target)  a Settings entry (couchliteos_tvscreens.Entry kind and target)
    ("power", request)       "suspend", "reboot" or "poweroff", asked first as PowerModel asks
    ("updates",)             CHECK FOR UPDATES: switch the start-up check on or off

Categories, left to right (a boot lands on GAMES, on the last game played):

    POWER       SLEEP, RESTART, SHUT DOWN
    SETTINGS    every Settings entry, with its current value as the detail line: the network's
                state, Wi-Fi, Tailscale, Remote Desktop and System Diagnostics are here
    TV & VIDEO  web browsers and web applications (Netflix, YouTube...), else ADD A STREAMING SERVICE
    GAMES       every paired PC's games, newest first; game streaming apps (chiaki-ng); GAMING PCS
    APPS        the other applications: Moonlight itself (its own list of PCs), saved Remote
                Desktop connections

An application goes where its manifest's `category` says, else by what it is (APP_CATEGORIES,
then a browser or web application is TV & VIDEO). Every item is in exactly one category: an
application that is also a Settings entry (Tailscale, System Diagnostics, the network setup)
is only in SETTINGS. Each category remembers its own item; `reload` keeps the focused item
focused when it still exists. Nothing wraps: a move against an edge returns EDGE (the front
end's bump sound).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Mapping

import couchliteos_apps as apps
import couchliteos_home as home
import couchliteos_tvscreens as tvscreens

POWER, SETTINGS, VIDEO, GAMES, APPS = "power", "settings", "video", "games", "apps"
ORDER = (POWER, SETTINGS, VIDEO, GAMES, APPS)
LABELS = {POWER: "POWER", SETTINGS: "SETTINGS", VIDEO: "TV & VIDEO", GAMES: "GAMES", APPS: "APPS"}
ICONS = {POWER: "power", SETTINGS: "gear", VIDEO: "television", GAMES: "game-controller",
         APPS: "squares-four"}
START = GAMES

MOVED, EDGE, STILL = "moved", "edge", "still"

# Where the built-in applications go (a manifest's own `category` wins).
APP_CATEGORIES = {
    "chiaki-ng": GAMES, "firefox": VIDEO, "google-chrome": VIDEO, "terminal": APPS, "audio-test": APPS,
    "moonlight": APPS,
}
# Applications that are Settings entries (or what a Settings entry opens): only in SETTINGS.
SETTINGS_APPS = {entry.target for entry in tvscreens.ENTRIES if entry.kind == tvscreens.APP} | {"network-setup"}
APP_ICONS = {
    "moonlight": "desktop-tower", "chiaki-ng": "game-controller", "firefox": "browser", "google-chrome": "browser",
    "tailscale": "shield",
    "network-setup": "wifi-high", "terminal": "terminal-window", "system-diagnostics": "stethoscope",
    "audio-test": "speaker-high",
}
BROWSERS = {"firefox", "google-chrome"}
# Settings entries that belong elsewhere on the cross (ACTIVE APPLICATIONS is in POWER), or nowhere
# (BACK has no meaning here).
SETTINGS_SKIP = {"BACK", "ACTIVE APPLICATIONS"}
ENTRY_ICONS = {
    "DISPLAY": "monitor", "APPEARANCE": "paint-brush", "AUDIO": "speaker-high", "BLUETOOTH": "bluetooth",
    "CONTROLLERS": "game-controller", "NETWORK": "globe", "SLEEP & SCREEN": "moon",
    "APPLICATIONS": "squares-four", "REMOTE DESKTOP": "desktop", "STREAMING": "broadcast",
    "TV CONTROL": "television", "SOFTWARE UPDATE": "download", "CHECK FOR UPDATES": "arrows-clockwise",
    "CONTROLS": "keyboard", "SETUP WIZARD": "magic-wand", "GENERATE SUPPORT FILE": "lifebuoy",
    "SYSTEM DIAGNOSTICS": "stethoscope", "HELP": "question", "TAILSCALE": "shield",
}
POWER_ICONS = {"suspend": "moon", "reboot": "arrows-clockwise", "poweroff": "power"}
# Known websites by what they are, for the icon shown until the site's own icon is fetched (one of
# ours, tools/make-icons.py; never anyone's logo) and for the category. Matched on the address's
# host or a parent domain ("www.netflix.com" is netflix.com).
_SERVICES = {
    "film": {
        "netflix.com", "primevideo.com", "disneyplus.com",
        "max.com", "hbomax.com", "hulu.com", "paramountplus.com",
        "peacocktv.com", "tv.apple.com", "crunchyroll.com",
        "plex.tv", "mubi.com", "criterionchannel.com", "starz.com",
        "britbox.com", "nowtv.com", "hotstar.com", "viki.com",
        "stan.com.au", "canalplus.com", "amcplus.com", "shudder.com",
    },
    "play-circle": {
        "youtube.com", "vimeo.com", "dailymotion.com",
        "nebula.tv", "curiositystream.com", "rumble.com",
    },
    "television": {
        "tv.youtube.com", "pluto.tv", "tubitv.com", "sling.com",
        "philo.com", "therokuchannel.roku.com", "bbc.co.uk",
        "itv.com", "channel4.com", "zattoo.com", "freevee.com",
    },
    "broadcast": {"twitch.tv", "kick.com"},
    "music-note": {
        "spotify.com", "music.youtube.com", "soundcloud.com",
        "tidal.com", "deezer.com", "music.apple.com",
        "music.amazon.com", "pandora.com", "bandcamp.com",
    },
    "microphone": {"podcasts.apple.com", "pocketcasts.com"},
    "trophy": {
        "espn.com", "dazn.com", "fubo.tv", "nba.com", "nfl.com",
        "mlb.com", "nhl.com", "f1tv.formula1.com",
    },
    "cloud-game": {
        "xbox.com", "play.geforcenow.com", "luna.amazon.com",
        "boosteroid.com",
    },
    "newspaper": {
        "cnn.com", "nytimes.com", "theguardian.com",
        "news.google.com", "reuters.com", "apnews.com",
    },
    "image": {"photos.google.com", "flickr.com", "icloud.com"},
    "chat": {
        "discord.com", "web.whatsapp.com", "messenger.com",
        "web.telegram.org", "reddit.com",
    },
    "envelope": {
        "mail.google.com", "outlook.live.com", "outlook.office.com",
        "mail.proton.me",
    },
    "bag": {"amazon.com", "ebay.com"},
    "video-camera": {"zoom.us", "meet.google.com", "teams.microsoft.com"},
    "document": {
        "docs.google.com", "drive.google.com", "office.com",
        "notion.so",
    },
}
WEB_SERVICES = {domain: icon for icon, domains in _SERVICES.items() for domain in domains}
# Where a web application goes by its icon: watching and listening are TV & VIDEO.
WEB_GAMES = {"cloud-game"}
WEB_VIDEO = {"film", "play-circle", "television", "broadcast", "music-note", "microphone", "trophy"}
NO_VIDEO = "ADD A STREAMING SERVICE"
MOONLIGHT_DETAIL = "YOUR GAMING PCS IN MOONLIGHT"
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


def web_service(url: str) -> str | None:
    """Our icon for a known website, by the address's host or its parent domains."""
    match = re.match(r"https?://([^/:?#]+)", url or "", re.IGNORECASE)
    if not match:
        return None
    labels = match.group(1).lower().rstrip(".").split(".")
    for start in range(len(labels) - 1):
        found = WEB_SERVICES.get(".".join(labels[start:]))
        if found:
            return found
    return None


def category_of(app: apps.Application) -> str:
    """VIDEO, GAMES or APPS. A manifest's `category = network` (the category before SETTINGS took
    the network in) is APPS."""
    if app.category in (VIDEO, GAMES, APPS):
        return app.category
    if app.id in APP_CATEGORIES:
        return APP_CATEGORIES[app.id]
    if app.kind == "rdp":
        return APPS
    if web_url(app):
        service = web_service(web_url(app))
        if service in WEB_GAMES:
            return GAMES
        return VIDEO if service is None or service in WEB_VIDEO else APPS
    return APPS


def icon_of(app: apps.Application) -> str:
    if app.icon:
        return app.icon
    if app.id in APP_ICONS:
        return APP_ICONS[app.id]
    if app.kind == "rdp":
        return "desktop"
    if web_url(app):
        return web_service(web_url(app)) or "play-circle"
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
    ) -> None:
        self.home = home_model
        self.settings = settings or tvscreens.SettingsModel()
        self.power = power or (lambda: tvscreens.PowerModel(can_sleep=True, can_wake=True))
        self.applications = applications or home_model.applications
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
            if not (app.enabled and app.visible) or app.id in SETTINGS_APPS:
                continue
            try:
                if not self.home.installed(app):
                    continue
            except OSError:
                continue
            url = web_url(app)
            detail = "WEB" if url else ""
            if app.id == home.STREAMED_APP:
                detail = MOONLIGHT_DETAIL
            elif app.kind == "rdp":
                detail = "REMOTE DESKTOP"
            placed[category_of(app)].append(Item(
                f"app:{app.id}", app.name.upper(), ("app", app.id), icon=icon_of(app), app=app.id,
                url=url, detail=detail,
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
        """Focus the item with this key (in `category` first); False when none."""
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
