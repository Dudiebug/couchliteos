"""The TV interface's quick menu, button prompt bar, toasts and sounds, as plain Python.

couchliteos-tv.py (GTK) draws them; everything here can be tested without GTK.

Quick menu: a short tap of Guide / PS (or the keyboard's Home key) opens a side panel over
whatever is on screen: VOLUME, BRIGHTNESS, the controllers' battery, the network, ACTIVE
APPLICATIONS (while apps run), HOME and SLEEP / RESTART / TURN OFF. gamepad-nav writes
"guide" into home.request for that tap; the held Home shortcuts (SELECT+START, L3+R3, the
keyboard chord) write "shortcut" and still go straight Home (`home_target`). During a stream
Guide stays with the stream (gamepad-nav sends nothing), as in 0.2.7.

Prompt bar: what the buttons do on the current screen, in the connected pad's own glyphs
(Ⓐ/Ⓑ, ✕/○; controls.detect_family) and always with the keyboard key alongside.
`names_keyboard_keys` is the check that a hint never names only a controller button.

Toasts: a queue of short cards shown one at a time for TOAST_SECONDS at the top right. They
never take focus: nothing here reads keys. `ToastFeed` makes them from the controllers
(connected, disconnected, battery low), the gaming PC status line and the update check.

Sounds: SOUND_NAMES WAVs in SOUNDS_DIR, played with pw-play in a detached process, unless
[appearance] sounds = off in config.ini or an application (a stream) is running.
"""

from __future__ import annotations

import collections
import configparser
import dataclasses
import os
import pathlib
import re
import subprocess
import time
from collections.abc import Callable, Sequence

import couchliteos_theme as theme

RUN = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos"))

# ---------------------------------------------------------------------- Guide / Home

GUIDE = "guide"  # a short tap of Guide / PS, or the keyboard's Home key: the quick menu
SHORTCUT = "shortcut"  # a held Home shortcut (or an older gamepad-nav that writes nothing): Home


def take_request(path: pathlib.Path) -> str | None:
    """GUIDE or SHORTCUT once per press (gamepad-nav leaves home.request), None when there is none."""
    try:
        text = path.read_text(encoding="ascii", errors="replace")[:32] if path.exists() else None
    except OSError:
        text = ""
    if text is None:
        return None
    path.unlink(missing_ok=True)
    return GUIDE if text.strip() == GUIDE else SHORTCUT


def home_target(kind: str, quick_open: bool, mode: str, apps_running: bool) -> str:
    """What a Guide / Home press shows: "quick" (open the quick menu), "close" (close it again),
    "active" (ACTIVE APPLICATIONS) or "home" (the home screen)."""
    if kind == GUIDE:
        return "close" if quick_open else "quick"
    # Held shortcuts go straight Home, as in 0.2.4: the running apps when there are any.
    if mode == "active" and not quick_open:
        return "home"
    return "active" if apps_running else "home"


def front_app_id(run: pathlib.Path = RUN) -> str:
    """The app that was in front when Guide was tapped (app-active, written by its start), so B / Esc
    can go back to it; "" when the launcher already had the controller (launcher-focus)."""
    if (run / "launcher-focus").exists():
        return ""
    try:
        return (run / "app-active").read_text(encoding="ascii", errors="replace").strip()[:64]
    except OSError:
        return ""


# ---------------------------------------------------------------------- prompt bar

# Glyphs of gamepad-nav's BTN_SOUTH, EAST, NORTH and WEST per controls.FAMILY_NAMES family:
# A / Enter opens, B / Esc goes back, X / Triangle is the keyboard (F12), Y / Square closes (Delete).
# Nintendo's south button is labelled B.
GLYPHS = {
    "xbox": ("Ⓐ", "Ⓑ", "Ⓧ", "Ⓨ"),
    "nintendo": ("Ⓑ", "Ⓐ", "Ⓧ", "Ⓨ"),
    "playstation": ("✕", "○", "△", "□"),
    "generic": ("Ⓐ / ✕", "Ⓑ / ○", "Ⓧ / △", "Ⓨ / □"),
}
BUTTONS = ("activate", "back", "keyboard", "close")
KEYBOARD = {"activate": "ENTER", "back": "ESC", "keyboard": "F12", "close": "DELETE", "change": "LEFT / RIGHT"}


def glyphs(family: str) -> dict[str, str]:
    """{action: glyph} for a controls.detect_family() family; an unknown one gets the generic set."""
    return dict(zip(BUTTONS, GLYPHS.get(family, GLYPHS["generic"])))


def prompt(family: str, entries: Sequence[tuple[str, str]], separator: str = "    ") -> str:
    """The prompt bar: each (action, words) as glyph, keyboard key and what it does."""
    marks = glyphs(family)
    parts = []
    for action, words in entries:
        key = KEYBOARD[action]
        parts.append(f"{marks[action]} OR {key}  {words}" if action in marks else f"{key}  {words}")
    return separator.join(parts)


HOME_PROMPT = (("activate", "OPEN"), ("back", "BACK"))
ACTIVE_PROMPT = (("activate", "RESUME"), ("close", "CLOSE"), ("keyboard", "TYPE INTO IT"), ("back", "BACK"))
MOUSE_PROMPT = (("activate", "ON / OFF"), ("back", "BACK"))  # ACTIVE APPLICATIONS' CONTROLLER MOUSE row
QUICK_PROMPT = (("activate", "SELECT"), ("back", "CLOSE"))
QUICK_CHANGE_PROMPT = (("change", "CHANGE"), ("activate", "MUTE"), ("back", "CLOSE"))
QUICK_BRIGHTNESS_PROMPT = (("change", "CHANGE"), ("back", "CLOSE"))

# A controller button named in a hint: "A / CROSS", "B (XBOX)", "SQUARE", "SELECT+START", a glyph.
BUTTON_WORDS = re.compile(
    r"(?<![\w+])(?:[ABXY](?= /| \(| OR |$)|CROSS|CIRCLE|SQUARE|TRIANGLE|SELECT\+START|VIEW\+MENU|L3\+R3|GUIDE|PS BUTTON)\b"
    r"|[ⒶⒷⓍⓎ✕○△□]"
)
KEYBOARD_WORDS = re.compile(r"\b(?:ENTER|ESC|DELETE|BACKSPACE|F\d{1,2}|HOME KEY|CTRL|ALT|SPACE|ARROW KEYS?)\b")


def names_keyboard_keys(hint: str) -> bool:
    """True when every part of `hint` (parts are split by "·" or wide gaps) that names a controller
    button also names the keyboard key that does the same."""
    for part in re.split(r"·|\n|\s{3,}", hint):
        if BUTTON_WORDS.search(part) and not KEYBOARD_WORDS.search(part):
            return False
    return True


# ---------------------------------------------------------------------- quick menu


@dataclasses.dataclass(frozen=True)
class Item:
    key: str  # "volume", "brightness", "battery", "network", "apps", "home", "sleep", "restart", "off"
    label: str  # upper case, as shown
    value: str = ""  # shown on the right
    action: tuple = ()  # what activate() returns; () for rows that only show something
    adjustable: bool = False  # LEFT / RIGHT change it
    selectable: bool = True  # False: shown, skipped by the focus (battery, network)


@dataclasses.dataclass
class Sources:
    """Where the quick menu reads and changes things. Every call may fail: the row then says UNAVAILABLE."""

    volume: Callable[[], object]  # audio.get_volume: .percent, .muted
    change_volume: Callable[[int], object]
    toggle_mute: Callable[[], object]
    brightness: Callable[[], int | None]  # None: no backlight (most TVs and desktops)
    change_brightness: Callable[[int], object]
    battery: Callable[[], str]  # controllers.Monitor.line
    network: Callable[[], str]
    running: Callable[[], int]  # how many applications run
    can_sleep: Callable[[], bool]

    @classmethod
    def system(cls, battery: Callable[[], str], network: Callable[[], str], running: Callable[[], int],
               can_sleep: Callable[[], bool]) -> "Sources":
        import couchliteos_audio as audio
        import couchliteos_brightness as brightness

        return cls(
            volume=audio.get_volume, change_volume=audio.change_volume, toggle_mute=audio.toggle_mute,
            brightness=brightness.get_percent, change_brightness=brightness.change,
            battery=battery, network=network, running=running, can_sleep=can_sleep,
        )


STEP = 5  # percent per LEFT / RIGHT, as the volume and brightness keys


class QuickMenu:
    """The rows and the focus. `extra` adds rows at the top (R1: the stream preset and SHOW STATS
    while a stream runs); their actions come back from activate() for the front end to carry out."""

    def __init__(self, sources: Sources, extra: Callable[[], list[Item]] = list) -> None:
        self.sources = sources
        self.extra = extra
        self.items: list[Item] = []
        self.index = 0

    def _value(self, source: Callable[[], object]) -> object:
        try:
            return source()
        except Exception:  # noqa: BLE001 - a broken source shows UNAVAILABLE, never closes the menu
            return None

    def build(self) -> list[Item]:
        items = list(self._value(self.extra) or [])
        running = self._value(self.sources.running) or 0
        if running:
            items.append(Item("apps", "APPLICATIONS", f"{running} RUNNING", ("active",)))
        volume = self._value(self.sources.volume)
        if volume is None:
            items.append(Item("volume", "VOLUME", "UNAVAILABLE", selectable=False))
        else:
            items.append(Item("volume", "VOLUME", f"{volume.percent}%" + ("  MUTED" if volume.muted else ""),
                              ("mute",), adjustable=True))
        level = self._value(self.sources.brightness)
        if isinstance(level, int):
            items.append(Item("brightness", "BRIGHTNESS", f"{level}%", adjustable=True))
        battery = self._value(self.sources.battery)
        if battery:
            items.append(Item("battery", "CONTROLLERS", str(battery).removeprefix("CONTROLLERS:").strip(), selectable=False))
        items.append(Item("network", "NETWORK", str(self._value(self.sources.network) or "UNKNOWN"), selectable=False))
        items.append(Item("home", "HOME", "", ("home",)))
        if self._value(self.sources.can_sleep):
            items.append(Item("sleep", "SLEEP", "", ("power", "suspend")))
        items.append(Item("restart", "RESTART", "", ("power", "reboot")))
        items.append(Item("off", "TURN OFF", "", ("power", "poweroff")))
        return items

    def refresh(self) -> None:
        """Read every row again; the focus stays on the same row."""
        before = self.focused()
        self.items = self.build()
        keys = [item.key for item in self.items]
        if before is not None and before.key in keys and self.items[keys.index(before.key)].selectable:
            self.index = keys.index(before.key)
        else:
            self.index = self._nearest(min(self.index, len(self.items) - 1))

    def open(self) -> None:
        """Opened again: read the rows, focus the first one that does something."""
        self.items = self.build()
        self.index = self._nearest(0)

    def _nearest(self, start: int) -> int:
        for index in list(range(start, len(self.items))) + list(range(start - 1, -1, -1)):
            if self.items[index].selectable:
                return index
        return 0

    def focused(self) -> Item | None:
        return self.items[self.index] if 0 <= self.index < len(self.items) else None

    def move(self, step: int) -> bool:
        """Up / down by one selectable row; nothing wraps. True when the focus moved."""
        index = self.index + step
        while 0 <= index < len(self.items) and not self.items[index].selectable:
            index += step
        if 0 <= index < len(self.items):
            self.index = index
            return True
        return False

    def adjust(self, step: int) -> bool:
        """LEFT / RIGHT on VOLUME or BRIGHTNESS: change it by STEP percent. True when it changed."""
        item = self.focused()
        if item is None or not item.adjustable:
            return False
        change = self.sources.change_volume if item.key == "volume" else self.sources.change_brightness
        if self._value(lambda: change(STEP if step > 0 else -STEP)) is None:
            return False
        self.refresh()
        return True

    def activate(self) -> tuple:
        """A / Enter: mute is done here; anything else is returned for the front end:
        ("active",), ("home",), ("power", "suspend" | "reboot" | "poweroff"), or an extra row's action."""
        item = self.focused()
        if item is None:
            return ()
        if item.action == ("mute",):
            self._value(self.sources.toggle_mute)
            self.refresh()
            return ()
        return item.action

    def prompt(self, family: str) -> str:
        """The panel's prompt, one button a line."""
        item = self.focused()
        entries = QUICK_PROMPT
        if item is not None and item.key == "volume" and item.adjustable:
            entries = QUICK_CHANGE_PROMPT
        elif item is not None and item.key == "brightness":
            entries = QUICK_BRIGHTNESS_PROMPT
        return prompt(family, entries, "\n")


POWER_QUESTIONS = {
    "reboot": ("RESTART", "RESTART THIS PC NOW? RUNNING APPLICATIONS ARE CLOSED."),
    "poweroff": ("TURN OFF", "TURN THIS PC OFF NOW? RUNNING APPLICATIONS ARE CLOSED."),
}


def power_question(request: str, can_wake: bool) -> tuple[str, str] | None:
    """(title, question) the quick menu asks before `request`; SLEEP asks as the classic home and
    the TV POWER screen do: without a wake source only the power button wakes this PC."""
    if request == "suspend":
        return ("SLEEP", "SLEEP NOW?" if can_wake else
                "SLEEP NOW? NOTHING CONNECTED CAN WAKE THIS PC: USE ITS POWER BUTTON TO WAKE IT.")
    return POWER_QUESTIONS.get(request)

# ---------------------------------------------------------------------- toasts

TOAST_SECONDS = 4.0
TOAST_LIMIT = 5  # waiting toasts kept; older ones are dropped, not shown late


class Toasts:
    """Cards shown one at a time for TOAST_SECONDS each. No keys reach them: a toast never takes focus
    and never changes what has it."""

    focusable = False

    def __init__(self, clock: Callable[[], float] = time.monotonic, seconds: float = TOAST_SECONDS,
                 limit: int = TOAST_LIMIT) -> None:
        self.clock = clock
        self.seconds = seconds
        self.waiting: collections.deque[str] = collections.deque(maxlen=limit)
        self.showing = ""
        self.until = 0.0

    def push(self, text: str) -> None:
        text = text.upper().strip()
        if text and text != self.showing and text not in self.waiting:
            self.waiting.append(text)

    def current(self) -> str:
        """The toast to show now ("" for none); moves on to the next one when its time is up."""
        now = self.clock()
        if self.showing and now >= self.until:
            self.showing = ""
        if not self.showing and self.waiting:
            self.showing = self.waiting.popleft()
            self.until = now + self.seconds
        return self.showing


class ToastFeed:
    """Turns changes in the controllers, the gaming PC and the update check into toasts. The first
    poll only takes note of how things are: nothing already connected is announced at start."""

    def __init__(
        self, toasts: Toasts, pads: Callable[[], list[str]], low: Callable[[], list],
        pc_line: Callable[[], str], update: Callable[[], str],
    ) -> None:
        self.toasts = toasts
        self.sources = {"pads": pads, "low": low, "pc": pc_line, "update": update}
        self.last: dict[str, object] | None = None

    def _read(self, name: str, default: object) -> object:
        try:
            return self.sources[name]()
        except Exception:  # noqa: BLE001 - a failed source means no toast, never a crash
            return default

    def poll(self) -> None:
        now = {
            "pads": collections.Counter(name.upper()[:32] for name in self._read("pads", []) or []),
            "low": {getattr(item, "name", str(item)).upper() for item in self._read("low", []) or []},
            "pc": str(self._read("pc", "") or ""),
            "update": str(self._read("update", "") or ""),
        }
        last, self.last = self.last, now
        if last is None:
            return
        for name in (now["pads"] - last["pads"]).elements():
            self.toasts.push(f"CONTROLLER CONNECTED: {name}")
        for name in (last["pads"] - now["pads"]).elements():
            self.toasts.push(f"CONTROLLER DISCONNECTED: {name}")
        for name in sorted(now["low"] - last["low"]):
            self.toasts.push(f"{name} BATTERY LOW")
        if now["pc"].endswith("READY") and now["pc"] != last["pc"]:
            self.toasts.push(now["pc"])
        if now["update"] and now["update"] != last["update"]:
            self.toasts.push(f"COUCHLITEOS {now['update']} IS AVAILABLE")


# ---------------------------------------------------------------------- sounds

SOUNDS_DIR = pathlib.Path("/usr/share/couchliteos/sounds")
REPO_SOUNDS = pathlib.Path(__file__).resolve().parents[1] / "overlay/usr/share/couchliteos/sounds"
SOUND_NAMES = ("move", "select", "back", "category", "edge", "open", "close", "notify", "startup")
SOUND_KEYS = {"up": "move", "down": "move", "left": "move", "right": "move", "activate": "select", "back": "back"}
SOUND_VOLUME = "0.5"  # quiet: pw-play's own volume, on top of the system one


def sounds_enabled(path: pathlib.Path | None = None) -> bool:
    """[appearance] sounds in config.ini; anything but "off" (or no file) is on."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string((theme.CONFIG if path is None else path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, configparser.Error):
        return True
    return parser.get(theme.SECTION, "sounds", fallback="on").strip().lower() != "off"


def save_sounds(on: bool, path: pathlib.Path | None = None) -> None:
    """Settings > APPEARANCE > SOUNDS. Raises OSError."""
    theme.save_values({"sounds": "on" if on else "off"}, path)


def spawn_detached(command: list[str]) -> None:
    subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )


class Sounds:
    def __init__(
        self, *, muted: Callable[[], bool], enabled: Callable[[], bool] = sounds_enabled,
        directory: pathlib.Path | None = None, spawn: Callable[[list[str]], None] = spawn_detached,
    ) -> None:
        self.muted = muted
        self.enabled = enabled
        self.directory = directory or (SOUNDS_DIR if SOUNDS_DIR.is_dir() else REPO_SOUNDS)
        self.spawn = spawn

    def command(self, name: str) -> list[str]:
        return ["pw-play", "--volume", SOUND_VOLUME, str(self.directory / f"{name}.wav")]

    def play(self, name: str) -> bool:
        """Play a sound unless sounds are off or an application (a stream) runs. True when started."""
        try:
            if name not in SOUND_NAMES or not self.enabled() or self.muted():
                return False
            self.spawn(self.command(name))
        except Exception:  # noqa: BLE001 - no PipeWire, no pw-play: silence, never a crash
            return False
        return True

    def for_key(self, action: str | None) -> bool:
        return self.play(SOUND_KEYS[action]) if action in SOUND_KEYS else False


# ---------------------------------------------------------------------- style


def stylesheet(colours: theme.Theme, layout) -> str:
    """CSS for the quick menu panel and the toast card (added next to tvlayout.stylesheet); `layout`
    is a tvlayout.Layout. The panel reaches the screen edge, its text stays inside the safe margin."""
    scale, fonts = layout.px, layout.fonts
    c = {field: f"#{value}" for field, value in colours.colours.items()}
    on_focus = f"#{theme.on_focus(colours)}"
    radius = scale(14)
    return f"""
.tv-quick {{ background-color: {c['surface']}; border-left: {scale(4)}px solid {c['accent']};
  padding: {layout.margin_y}px {layout.margin_x}px {layout.margin_y}px {scale(32)}px; }}
.tv-quick .tv-title {{ font-size: {fonts['title']}px; font-weight: bold; color: {c['text']}; }}
.tv-quick-row {{ font-size: {fonts['body']}px; padding: {scale(8)}px {scale(16)}px; border-radius: {radius}px; }}
.tv-quick-row label {{ color: {c['text']}; }}
.tv-quick-row.tv-info label {{ color: {c['muted']}; }}
.tv-quick-row.tv-focused {{ background-color: {c['focus']}; }}
.tv-quick-row.tv-focused label {{ color: {on_focus}; }}
.tv-quick .tv-prompt {{ font-size: {fonts['prompt']}px; color: {c['muted']}; }}
.tv-toast {{ background-color: {c['surface']}; color: {c['text']}; font-size: {fonts['body']}px;
  border: {scale(3)}px solid {c['accent']}; border-radius: {radius}px; padding: {scale(16)}px {scale(28)}px; }}
"""
