"""Settings > CONTROLS: the Home shortcut and the pointer speeds.

The launcher saves the Home shortcut and the controller mouse speed in the [input] section
of config.ini. gamepad-nav runs as the same user and re-reads the file when it changes
(Watcher), so a choice takes effect at once. Mouse and touchpad speed belongs to Cage: the
launcher writes libinput's acceleration speed to MOUSE_SPEED_FILE and sends Cage SIGHUP,
which makes CouchLiteOS's patched Cage apply it to every pointer (it also reads the file
whenever a mouse is plugged in). That file is the only record of the mouse speed.
"""

from __future__ import annotations

import configparser
import dataclasses
import os
import pathlib
import signal
import tempfile
import time
from collections.abc import Callable

CONFIG = pathlib.Path("/var/lib/couchliteos/config.ini")
SECTION = "input"
# Cage reads this: one float, libinput's pointer acceleration speed (-1.0 to 1.0).
MOUSE_SPEED_FILE = pathlib.Path("/var/lib/couchliteos/mouse-speed")
PROC = pathlib.Path("/proc")
COMPOSITOR = "cage"
# Only a Cage built with the speed patch handles SIGHUP; to any other the signal is fatal (the
# whole session would go), so the binary must mention the file before it is signalled.
RELOAD_MARKER = b"mouse-speed"

HOME_GUIDE = "guide"
HOME_SELECT_START = "select-start"
HOME_STICKS = "l3-r3"
HOME_CHOICES = (
    (HOME_GUIDE, "GUIDE ONLY"),
    (HOME_SELECT_START, "GUIDE OR SELECT+START (HOLD)"),
    (HOME_STICKS, "GUIDE OR L3+R3 (HOLD)"),
)
HOME_HOLD_SECONDS = 1.5
SPEED_CHOICES = (("slow", "SLOW"), ("normal", "NORMAL"), ("fast", "FAST"), ("very-fast", "VERY FAST"))
# Controller mouse: times gamepad-nav's top pointer speed.
PAD_SPEEDS = {"slow": 0.5, "normal": 1.0, "fast": 1.6, "very-fast": 2.4}
# Mice and touchpads: libinput acceleration speed.
MOUSE_SPEEDS = {"slow": -0.5, "normal": 0.0, "fast": 0.4, "very-fast": 0.8}

# The keyboard Home key is a chord of evdev key names joined by "+"; "" means off.
DEFAULT_CHORD = "KEY_LEFTCTRL+KEY_LEFTALT+KEY_H"
MODIFIER_FAMILIES = {
    "KEY_LEFTCTRL": "CTRL", "KEY_RIGHTCTRL": "CTRL",
    "KEY_LEFTALT": "ALT", "KEY_RIGHTALT": "ALT",
    "KEY_LEFTSHIFT": "SHIFT", "KEY_RIGHTSHIFT": "SHIFT",
    "KEY_LEFTMETA": "SUPER", "KEY_RIGHTMETA": "SUPER",
}
# Keys the launcher already uses (the controller sends them too), so they cannot be the Home key.
FIXED_KEYS = frozenset({
    "KEY_ENTER", "KEY_KPENTER", "KEY_ESC", "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT",
    "KEY_F5", "KEY_F6", "KEY_F7", "KEY_F8", "KEY_F12", "KEY_DELETE", "KEY_BACKSPACE", "KEY_TAB", "KEY_SPACE",
})
# Ctrl+letter bytes curses treats specially: interrupt, end of input, Tab, Enter, flow control, suspend.
CTRL_RESERVED = frozenset({"C", "D", "I", "J", "M", "Q", "S", "Z"})
CHORD_ORDER = ("CTRL", "ALT", "SHIFT", "SUPER")
LEFT_KEYS = {"CTRL": "KEY_LEFTCTRL", "ALT": "KEY_LEFTALT", "SHIFT": "KEY_LEFTSHIFT", "SUPER": "KEY_LEFTMETA"}

# Settings > CONTROLS rows, in order.
ROW_HOME = "HOME SHORTCUT"
ROW_KEYBOARD = "KEYBOARD HOME KEY"
ROW_PAD_SPEED = "CONTROLLER MOUSE SPEED"
ROW_MOUSE_SPEED = "MOUSE SPEED"
FIELDS = ("home", "keyboard_home", "pad_speed", "mouse_speed")
HELP = {
    "home": "HOLD THE BUTTONS 1.5 S TO OPEN HOME, LIKE GUIDE",
    # Also over a stream or remote desktop.
    "keyboard_home": "OPENS HOME FROM A KEYBOARD, EVEN DURING A STREAM. A / CROSS CHANGES IT",
    "pad_speed": "HOW FAST THE STICK MOVES THE POINTER IN BROWSERS",
    "mouse_speed": "FOR MICE AND TOUCHPADS",
}


@dataclasses.dataclass(frozen=True)
class Settings:
    home: str = HOME_SELECT_START
    keyboard_home: str = DEFAULT_CHORD
    pad_speed: str = "normal"
    mouse_speed: str = "normal"


def parse_chord(text: str | None) -> frozenset[str]:
    """The key names in a saved chord ("KEY_LEFTCTRL+KEY_H"); empty for off or anything unreadable."""
    names = [part.strip().upper() for part in (text or "").split("+")]
    if not all(name.startswith("KEY_") and name[4:].isalnum() for name in names):
        return frozenset()
    return frozenset(names)


def chord_family(name: str) -> str:
    """CTRL, ALT, SHIFT or SUPER for a modifier key, else the key's own name."""
    return MODIFIER_FAMILIES.get(name, name)


def canonical_chord(names) -> str:
    """The stored text for a chord: modifiers in a fixed order (as their left-hand keys), then the key."""
    keys = {chord_family(name) for name in names}
    mods = [LEFT_KEYS[family] for family in CHORD_ORDER if family in keys]
    return "+".join(mods + sorted(name for name in keys if name not in CHORD_ORDER))


def chord_label(text: str | None) -> str:
    """CTRL+ALT+H for a saved chord; OFF when there is none."""
    names = parse_chord(text)
    if not names:
        return "OFF"
    families = {chord_family(name) for name in names}
    mods = [family for family in CHORD_ORDER if family in families]
    return "+".join(mods + sorted(name[4:] for name in families if name not in CHORD_ORDER))


def validate_chord(names) -> str | None:
    """Why a captured chord cannot be the Home key (upper case, for the screen), or None when it can."""
    keys = {name.upper() for name in names}
    mods = {chord_family(name) for name in keys if name in MODIFIER_FAMILIES}
    others = sorted(name for name in keys if name not in MODIFIER_FAMILIES)
    if not others:
        return "HOLD A KEY TOGETHER WITH CTRL, ALT OR SUPER"
    if len(others) > 1:
        return "USE ONLY ONE KEY PLUS CTRL, ALT OR SUPER"
    key = others[0]
    if not mods:
        return "HOLD CTRL, ALT OR SUPER WITH THE KEY"
    if mods == {"SHIFT"}:
        return "SHIFT ALONE IS NOT ENOUGH; ADD CTRL, ALT OR SUPER"
    if key in FIXED_KEYS:
        return f"{key[4:]} IS USED BY THE LAUNCHER; PICK ANOTHER KEY"
    if "CTRL" in mods and key[4:] in CTRL_RESERVED:
        return f"CTRL+{key[4:]} IS USED BY THE TERMINAL; PICK ANOTHER KEY"
    if {"CTRL", "ALT"} <= mods and key[4:].startswith("F") and key[5:].isdigit():
        return "CTRL+ALT+F KEYS SWITCH TEXT CONSOLES"
    if "SUPER" in mods and not (len(key) == 5 and key[4].isalpha()):
        return "WITH SUPER, USE A LETTER KEY"
    return None


class ChordCapture:
    """Settings > CONTROLS > KEYBOARD HOME KEY: the keys held when the first of them is let go.

    Fed (key name, 1 down / 0 up) from the keyboards; gives the chord once, then waits until every
    key is up so the rest of the release does not count as another try.
    """

    def __init__(self) -> None:
        self.held: set[str] = set()
        self.fired = False

    def feed(self, name: str, value: int) -> frozenset[str] | None:
        if value == 1:
            self.held.add(name)
        elif value == 0 and name in self.held:
            chord = None if self.fired else frozenset(self.held)
            self.fired = True
            self.held.discard(name)
            if not self.held:
                self.fired = False
            return chord
        return None


def _choice(value: str | None, choices: tuple[tuple[str, str], ...], default: str) -> str:
    value = (value or "").strip().lower()
    return value if value in dict(choices) else default


def _load_chord(value: str | None, default: str) -> str:
    """The saved chord; the old on/off values of 0.2.4 and earlier map to the default and off."""
    text = (value or "").strip()
    if text.lower() in ("true", "on", "1"):
        return DEFAULT_CHORD
    if text.lower() in ("false", "off", "0", "none"):
        return ""
    names = parse_chord(text)
    if names and validate_chord(names) is None:
        return canonical_chord(names)
    return default


def load_mouse_speed(path: pathlib.Path | None = None) -> str:
    """The step whose acceleration is nearest the file's; NORMAL when it is missing or unreadable."""
    try:
        value = float((MOUSE_SPEED_FILE if path is None else path).read_text(encoding="ascii")[:32].strip())
    except (OSError, ValueError, UnicodeDecodeError):
        return "normal"
    if value != value:  # NaN
        return "normal"
    return min(MOUSE_SPEEDS, key=lambda name: abs(MOUSE_SPEEDS[name] - value))


def load_settings(path: pathlib.Path | None = None, speed_file: pathlib.Path | None = None) -> Settings:
    """The saved choices; anything missing, unreadable or unknown gives its default."""
    defaults = Settings()
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string((CONFIG if path is None else path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, configparser.Error):
        parser = configparser.ConfigParser(interpolation=None)
    keyboard = parser.get(SECTION, "keyboard_home", fallback=None)
    return Settings(
        home=_choice(parser.get(SECTION, "home_shortcut", fallback=None), HOME_CHOICES, defaults.home),
        keyboard_home=_load_chord(keyboard, defaults.keyboard_home),
        pad_speed=_choice(parser.get(SECTION, "controller_mouse_speed", fallback=None), SPEED_CHOICES,
                          defaults.pad_speed),
        mouse_speed=load_mouse_speed(speed_file),
    )


def _write_atomically(path: pathlib.Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save_settings(settings: Settings, path: pathlib.Path | None = None) -> None:
    """Rewrite only the [input] section of config.ini, atomically (the mouse speed is not in it)."""
    path = CONFIG if path is None else path
    block = (
        f"[{SECTION}]\n"
        f"home_shortcut = {settings.home}\n"
        f"keyboard_home = {settings.keyboard_home or 'off'}\n"
        f"controller_mouse_speed = {settings.pad_speed}\n"
    )
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        lines = []
    start = next((i for i, line in enumerate(lines) if line.strip().lower() == f"[{SECTION}]"), None)
    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(block)
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        lines[start:end] = [block + ("\n" if end < len(lines) else "")]
    _write_atomically(path, "".join(lines), 0o640)


def mouse_speed_text(speed: str) -> str:
    return f"{MOUSE_SPEEDS[speed]:.1f}\n"


def write_mouse_speed(speed: str, path: pathlib.Path | None = None) -> None:
    """The plain float Cage reads, e.g. "0.4\\n"; NORMAL is written too ("0.0\\n")."""
    _write_atomically(MOUSE_SPEED_FILE if path is None else path, mouse_speed_text(speed), 0o644)


def compositor_pids(proc: pathlib.Path = PROC, name: str = COMPOSITOR, uid: int | None = None) -> list[int]:
    """Running `name` processes owned by `uid` (this user) whose binary has the speed patch."""
    uid = os.getuid() if uid is None else uid
    found = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text(encoding="utf-8", errors="replace").strip() != name:
                continue
            if entry.stat().st_uid != uid:
                continue
            if RELOAD_MARKER not in (entry / "exe").read_bytes():
                continue
        except OSError:  # gone meanwhile, or not ours to look at
            continue
        found.append(int(entry.name))
    return sorted(found)


def signal_compositor(
    kill: Callable[[int, int], None] = os.kill, pids: Callable[[], list[int]] | None = None,
) -> int:
    """SIGHUP each running Cage of this user so it re-applies the speed; how many were told. Never raises."""
    told = 0
    try:
        targets = pids() if pids is not None else compositor_pids(PROC)
    except OSError:
        return 0
    for pid in targets:
        try:
            kill(pid, signal.SIGHUP)
            told += 1
        except OSError:  # ProcessLookupError, PermissionError
            pass
    return told


def apply_mouse_speed(
    speed: str, path: pathlib.Path | None = None, send: Callable[[], int] | None = None,
) -> int:
    """Save the speed for Cage and tell it; how many Cages were told. OSError only when the file cannot be written."""
    write_mouse_speed(speed, path)
    return (send or signal_compositor)()


def label(field: str, settings: Settings) -> str:
    value = getattr(settings, field)
    if field == "keyboard_home":
        return chord_label(value)
    return dict(HOME_CHOICES if field == "home" else SPEED_CHOICES)[value]


def rows(settings: Settings) -> list[str]:
    """The Settings > CONTROLS rows for the four choices, in FIELDS order."""
    names = (ROW_HOME, ROW_KEYBOARD, ROW_PAD_SPEED, ROW_MOUSE_SPEED)
    return [f"{name}  {label(field, settings)}" for name, field in zip(names, FIELDS)]


def cycle(settings: Settings, field: str, step: int = 1) -> Settings:
    """The next (step 1) or previous (step -1) choice for one field, wrapping round."""
    if field == "keyboard_home":  # chosen by capture, not cycled
        return settings
    values = [value for value, _label in (HOME_CHOICES if field == "home" else SPEED_CHOICES)]
    index = values.index(getattr(settings, field)) if getattr(settings, field) in values else 0
    return dataclasses.replace(settings, **{field: values[(index + step) % len(values)]})


def change(
    settings: Settings,
    field: str,
    step: int = 1,
    path: pathlib.Path | None = None,
    speed_file: pathlib.Path | None = None,
    send: Callable[[], int] | None = None,
) -> tuple[Settings, str]:
    """Cycle one choice and save it; (the settings now in force, what to tell the user)."""
    changed = cycle(settings, field, step)
    note = ""
    try:
        if field == "mouse_speed":
            if not apply_mouse_speed(changed.mouse_speed, speed_file, send):
                note = " (FROM THE NEXT RESTART)"
        else:
            save_settings(changed, path)
    except OSError as error:
        return settings, f"COULD NOT SAVE: {error}".upper()
    return changed, rows(changed)[FIELDS.index(field)] + note


class Watcher:
    """gamepad-nav's view of the settings: re-read when config.ini changes, looked at most every `interval`."""

    def __init__(
        self,
        path: pathlib.Path | None = None,
        interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        load: Callable[[pathlib.Path], Settings] | None = None,
    ) -> None:
        self.path = CONFIG if path is None else path
        self.interval = interval
        self.clock = clock
        self.load = load or (lambda config: load_settings(config, pathlib.Path(os.devnull)))
        self.checked: float | None = None
        self.stamp: tuple[int, int, int] | None = None
        self.settings = Settings()

    def current(self) -> Settings:
        now = self.clock()
        if self.checked is not None and 0 <= now - self.checked < self.interval:
            return self.settings
        self.checked = now
        try:
            info = self.path.stat()
            stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            stamp = None
        if stamp != self.stamp:
            self.stamp = stamp
            self.settings = self.load(self.path) if stamp is not None else Settings()
        return self.settings
