"""Settings > CONTROLLERS: the connected pads, their player order, IDENTIFY, TEST and the button swaps.

Pads are read from evdev (watched, never grabbed: gamepad-nav and the streaming clients keep
them). The player order and the per-pad swaps are saved in the [controllers] section of
config.ini, keyed by the pad's unique id: its `uniq` (the MAC address for Bluetooth and most
USB PlayStation pads), else vendor:product:phys (the USB port), so a pad keeps its place
after a reconnect. The order sets the player lights and the numbers shown here; the order a
game sees in a stream is Moonlight's own. The swaps are applied by gamepad-nav to the keys it
sends the launcher (key_for_event); a stream reads the pad itself and gets it unchanged.
"""

from __future__ import annotations

import configparser
import curses
import dataclasses
import glob
import pathlib
import re
import select
import threading
import time
from collections.abc import Callable, Iterable

import couchliteos_controllers as controllers
import couchliteos_input as inputprefs
import couchliteos_listview as listview

CONFIG = inputprefs.CONFIG
SECTION = "controllers"
INPUT_NODES = "/dev/input/event*"
SYS_INPUT = pathlib.Path("/sys/class/input")
LEDS = pathlib.Path("/sys/class/leds")
OWN_DEVICE_PREFIX = "CouchLiteOS "  # gamepad-nav's keyboard, the on-screen keyboard, the mouse

# evdev codes (linux/input-event-codes.h), so this module loads without python3-evdev.
EV_KEY = 0x01
EV_ABS = 0x03
EV_FF = 0x15
FF_RUMBLE = 0x50
BTN_SOUTH = 0x130  # also BTN_GAMEPAD: what makes an input device a pad
BTN_EAST = 0x131
BTN_NORTH = 0x133
BTN_WEST = 0x134
BTN_TL, BTN_TR, BTN_TL2, BTN_TR2 = 0x136, 0x137, 0x138, 0x139
BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR = 0x13A, 0x13B, 0x13C, 0x13D, 0x13E
BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT = 0x220, 0x221, 0x222, 0x223
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11
BUSES = {0x03: "USB", 0x05: "BLUETOOTH"}

# gamepad-nav's key_for_event reads these per pad: A/B (south/east) and X/Y (north/west) swapped.
SWAP_AB = {BTN_SOUTH: BTN_EAST, BTN_EAST: BTN_SOUTH}
SWAP_XY = {BTN_NORTH: BTN_WEST, BTN_WEST: BTN_NORTH}

RUMBLE_SECONDS = 0.3
BLINKS = 3
BLINK_SECONDS = 0.15
# xpad's LED (xpad0, ...) takes a pattern number: 1 blinks all four, 6-9 light quadrant 1-4 steadily.
XPAD_BLINK = 1
XPAD_PLAYER_ON = 6
# DualSense's five player lights, as the PS5 lights them for players 1-4.
FIVE_LIGHTS = {1: {3}, 2: {2, 4}, 3: {1, 3, 5}, 4: {1, 2, 4, 5}}
PLAYER_LED = re.compile(r"player-?(\d+)$")
XPAD_LED = re.compile(r"xpad\d+$")

ORDER_NOTE = "THE ORDER SETS LIGHTS AND NUMBERS HERE. A STREAM USES MOONLIGHT'S OWN ORDER"
SWAP_NOTE = "FOR NINTENDO LAYOUTS. COUCHLITEOS MENUS ONLY: A STREAM GETS THE PAD AS IT IS"
LIST_HINT = "A / CROSS OR ENTER SELECTS  ·  B / CIRCLE OR ESC GOES BACK"
TEST_HINT = "HOLD B / CIRCLE 2 S, OR PRESS ESC ON A KEYBOARD, TO GO BACK"
TEST_HOLD_SECONDS = 2.0
# An Esc within this long of the tested pad's own input came from that pad (gamepad-nav), not a keyboard.
TEST_QUIET_SECONDS = 1.0
TEST_POLL_MS = 50
ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
ROWS = ("MOVE UP", "MOVE DOWN", "IDENTIFY", "TEST", "SWAP A/B", "SWAP X/Y", "BACK")


@dataclasses.dataclass(frozen=True)
class Settings:
    order: tuple[str, ...] = ()
    swap_ab: frozenset[str] = frozenset()
    swap_xy: frozenset[str] = frozenset()

    def swaps(self, ident: str) -> dict[int, int]:
        """The button codes gamepad-nav reads as others for this pad (none for most pads)."""
        table: dict[int, int] = {}
        if ident and ident in self.swap_ab:
            table.update(SWAP_AB)
        if ident and ident in self.swap_xy:
            table.update(SWAP_XY)
        return table


@dataclasses.dataclass(frozen=True)
class Pad:
    path: str
    name: str
    ident: str
    bus: str = ""  # USB, BLUETOOTH, or "" for anything else
    node: str = ""  # the kernel input device ("input12") that the pad's lights belong to
    battery: controllers.Battery | None = None
    player: int = 0


def _ids(value: str | None) -> list[str]:
    return [part.strip().lower() for part in (value or "").split(",") if part.strip()]


def load(path: pathlib.Path | None = None) -> Settings:
    """The saved order and swaps; nothing saved, or unreadable, gives none."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string((CONFIG if path is None else path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, configparser.Error):
        return Settings()
    order = tuple(dict.fromkeys(_ids(parser.get(SECTION, "order", fallback=None))))
    return Settings(
        order, frozenset(_ids(parser.get(SECTION, "swap_ab", fallback=None))),
        frozenset(_ids(parser.get(SECTION, "swap_xy", fallback=None))),
    )


def save(settings: Settings, path: pathlib.Path | None = None) -> None:
    """Rewrite only the [controllers] section of config.ini, atomically."""
    path = CONFIG if path is None else path
    block = (
        f"[{SECTION}]\n"
        f"order = {', '.join(settings.order)}\n"
        f"swap_ab = {', '.join(sorted(settings.swap_ab))}\n"
        f"swap_xy = {', '.join(sorted(settings.swap_xy))}\n"
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
    inputprefs._write_atomically(path, "".join(lines), 0o640)


class Watcher:
    """gamepad-nav's view of the swaps: re-read when config.ini changes, looked at most every `interval`."""

    def __init__(
        self, path: pathlib.Path | None = None, interval: float = 0.5, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = CONFIG if path is None else path
        self.interval = interval
        self.clock = clock
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
            self.settings = load(self.path) if stamp is not None else Settings()
        return self.settings


def pad_id(device) -> str:
    """The pad's `uniq` (a MAC address on most wireless pads), else vendor:product:phys (its USB port)."""
    uniq = str(getattr(device, "uniq", "") or "").strip().lower()
    if uniq:
        return re.sub(r"[\s,]+", "_", uniq)
    phys = re.sub(r"[\s,]+", "_", str(getattr(device, "phys", "") or "").strip().lower())
    return f"{device.info.vendor:04x}:{device.info.product:04x}:{phys}"


def is_pad(device) -> bool:
    keys = set(device.capabilities().get(EV_KEY, []))
    return BTN_SOUTH in keys and not str(getattr(device, "name", "")).startswith(OWN_DEVICE_PREFIX)


def input_node(path: str, sys_input: pathlib.Path = SYS_INPUT) -> str:
    """"input12" for /dev/input/event5: the kernel input device the event node belongs to."""
    link = sys_input / pathlib.Path(path).name / "device"
    try:
        return link.resolve(strict=True).name if link.exists() else ""
    except OSError:
        return ""


def _evdev_open(path: str):
    import evdev  # python3-evdev, already part of the image; loaded only when a pad is opened

    return evdev.InputDevice(path)


def ordered(pads: Iterable[Pad], order: Iterable[str]) -> list[Pad]:
    """Pads in the saved order (unknown ones last, as found), numbered from player 1."""
    rank = {ident: index for index, ident in enumerate(order)}
    found = sorted(pads, key=lambda pad: rank.get(pad.ident, len(rank)))  # stable: unknown ones keep theirs
    return [dataclasses.replace(pad, player=number) for number, pad in enumerate(found, 1)]


def battery_for(pad: Pad, batteries: Iterable[controllers.Battery]) -> controllers.Battery | None:
    """The battery whose power_supply name carries this pad's MAC address."""
    return next((item for item in batteries if pad.ident in controllers._macs(item.ident)), None)


def from_device(path: str, device, sys_input: pathlib.Path = SYS_INPUT) -> Pad:
    info = getattr(device, "info", None)
    return Pad(
        path, str(getattr(device, "name", "") or "CONTROLLER"), pad_id(device),
        BUSES.get(getattr(info, "bustype", 0), ""), input_node(path, sys_input),
    )


def list_pads(
    opener: Callable[[str], object] | None = None,
    paths: Iterable[str] | None = None,
    batteries: Callable[[], list[controllers.Battery]] = controllers.read_sysfs,
    settings: Settings | None = None,
    sys_input: pathlib.Path = SYS_INPUT,
) -> list[Pad]:
    """The connected pads in player order, each with its battery when the kernel reports one."""
    opener = opener or _evdev_open
    found = []
    for path in sorted(glob.glob(INPUT_NODES) if paths is None else paths):
        try:
            device = opener(path)
        except OSError:
            continue
        try:
            if is_pad(device):
                found.append(from_device(path, device, sys_input))
        except OSError:
            pass
        finally:
            try:
                device.close()
            except OSError:
                pass
    try:
        levels = batteries()
    except Exception:  # a failed read must never empty the list
        levels = []
    found = [dataclasses.replace(pad, battery=battery_for(pad, levels)) for pad in found]
    return ordered(found, (load() if settings is None else settings).order)


def move(settings: Settings, pads: list[Pad], index: int, step: int) -> Settings:
    """Move the pad at `index` one place up (step -1) or down (+1); pads not connected keep their places after."""
    target = index + step
    if not (0 <= index < len(pads) and 0 <= target < len(pads)):
        return settings
    ids = [pad.ident for pad in pads]
    ids[index], ids[target] = ids[target], ids[index]
    return dataclasses.replace(settings, order=tuple(ids + [i for i in settings.order if i not in ids]))


def toggle(settings: Settings, ident: str, field: str) -> Settings:
    """SWAP A/B (field "swap_ab") or SWAP X/Y ("swap_xy") on or off for one pad."""
    current = getattr(settings, field)
    return dataclasses.replace(settings, **{field: current - {ident} if ident in current else current | {ident}})


def leds_for(pad: Pad, root: pathlib.Path = LEDS, sys_input: pathlib.Path = SYS_INPUT) -> list[pathlib.Path]:
    """The pad's lights: named after its input device (hid-playstation) or on the same parent device
    (xpad, hid-nintendo)."""
    if not pad.node:
        return []
    try:
        parent = (sys_input / pad.node / "device").resolve(strict=True)
    except OSError:
        parent = None
    found = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith(f"{pad.node}:"):
            found.append(entry)
            continue
        try:
            if parent is not None and (entry / "device").resolve(strict=True) == parent:
                found.append(entry)
        except OSError:
            pass
    return found


def _write(led: pathlib.Path, value: int) -> bool:
    try:
        (led / "brightness").write_text(f"{value}\n")
        return True
    except OSError:  # no write access (no udev rule yet) or the pad went away
        return False


def player_values(leds: list[pathlib.Path], player: int) -> dict[pathlib.Path, int]:
    """The brightness to write to each light for a player number."""
    values: dict[pathlib.Path, int] = {}
    numbered = {}
    for led in leds:
        if XPAD_LED.fullmatch(led.name):
            if 1 <= player <= 4:
                values[led] = XPAD_PLAYER_ON + player - 1
            continue
        match = PLAYER_LED.search(led.name)
        if match:
            numbered[int(match.group(1))] = led
    if numbered:
        count = len(numbered)
        if count == 5 and player in FIVE_LIGHTS:
            lit = FIVE_LIGHTS[player]
        elif 1 <= player <= count:
            lit = {player}
        else:
            lit = set(numbered)  # more players than lights: all of them
        for number, led in numbered.items():
            values[led] = _max_brightness(led) if number in lit else 0
    return values


def _max_brightness(led: pathlib.Path) -> int:
    try:
        return max(1, int((led / "max_brightness").read_text().strip()))
    except (OSError, ValueError):
        return 1


def set_player_leds(
    pads: Iterable[Pad], root: pathlib.Path = LEDS, sys_input: pathlib.Path = SYS_INPUT,
) -> int:
    """Light each pad's player lights for its place in the order; how many lights were written."""
    written = 0
    for pad in pads:
        for led, value in player_values(leds_for(pad, root, sys_input), pad.player).items():
            written += _write(led, value)
    return written


def _rumble_effect():
    from evdev import ecodes, ff

    rumble = ff.Rumble(strong_magnitude=0xC000, weak_magnitude=0xC000)
    return ff.Effect(
        ecodes.FF_RUMBLE, -1, 0, ff.Trigger(0, 0), ff.Replay(int(RUMBLE_SECONDS * 1000), 0),
        ff.EffectType(ff_rumble_effect=rumble),
    )


def rumble(
    path: str, opener: Callable[[str], object] | None = None, sleep: Callable[[float], None] = time.sleep,
    effect: Callable[[], object] = _rumble_effect,
) -> bool:
    """A 300 ms rumble on the pad at `path`; False when it cannot rumble."""
    try:
        device = (opener or _evdev_open)(path)
    except OSError:
        return False
    try:
        if FF_RUMBLE not in device.capabilities().get(EV_FF, []):
            return False
        effect_id = device.upload_effect(effect())
        device.write(EV_FF, effect_id, 1)
        sleep(RUMBLE_SECONDS)
        device.write(EV_FF, effect_id, 0)
        device.erase_effect(effect_id)
        return True
    except (OSError, ImportError):
        return False
    finally:
        try:
            device.close()
        except OSError:
            pass


def blink(leds: list[pathlib.Path], sleep: Callable[[float], None] = time.sleep) -> bool:
    """Flash the lights BLINKS times, then put back what they showed; False when none could be written."""
    saved = {}
    for led in leds:
        try:
            saved[led] = int((led / "brightness").read_text().strip())
        except (OSError, ValueError):
            pass
    if not saved:
        return False
    lit = False
    for _round in range(BLINKS):
        for led in saved:
            lit = _write(led, 0) or lit
        sleep(BLINK_SECONDS)
        for led in saved:
            _write(led, XPAD_BLINK if XPAD_LED.fullmatch(led.name) else _max_brightness(led))
        sleep(BLINK_SECONDS)
    for led, value in saved.items():
        _write(led, value)
    return lit


def identify(
    pad: Pad, opener: Callable[[str], object] | None = None, root: pathlib.Path = LEDS,
    sys_input: pathlib.Path = SYS_INPUT, sleep: Callable[[float], None] = time.sleep,
    effect: Callable[[], object] = _rumble_effect,
) -> str:
    """Rumble and blink the pad; what to tell the user."""
    shook = rumble(pad.path, opener, sleep, effect)
    flashed = blink(leds_for(pad, root, sys_input), sleep)
    if shook and flashed:
        return f"PLAYER {pad.player}: RUMBLE AND LIGHTS"
    if shook:
        return f"PLAYER {pad.player}: RUMBLE"
    if flashed:
        return f"PLAYER {pad.player}: LIGHTS"
    return "THIS CONTROLLER CANNOT RUMBLE OR FLASH ITS LIGHTS HERE"


def connection(pad: Pad) -> str:
    text = pad.bus or "CONNECTED"
    if pad.battery is not None:
        item = pad.battery
        text += f"  {item.percent}%" if item.percent is not None else f"  {item.level}"
        text += "  CHARGING" if item.charging else ("  LOW" if item.low and item.percent is not None else "")
    return text


def row(pad: Pad) -> str:
    return f"PLAYER {pad.player}  {controllers.short_name(pad.name)}  {connection(pad)}"


class Reading:
    """TEST: what the pad is doing now, from its events."""

    def __init__(self, device) -> None:
        self.device = device
        self.axes: dict[int, int] = {}
        self.held: set[int] = set()
        self.spans: dict[int, tuple[int, int] | None] = {}

    def feed(self, event) -> None:
        if event.type == EV_KEY:
            self.held.add(event.code) if event.value else self.held.discard(event.code)
        elif event.type == EV_ABS:
            self.axes[event.code] = event.value

    def span(self, code: int) -> tuple[int, int] | None:
        if code not in self.spans:
            try:
                info = self.device.absinfo(code)
                self.spans[code] = (info.min, info.max) if info is not None and info.max > info.min else None
            except (OSError, AttributeError):
                self.spans[code] = None
        return self.spans[code]

    def stick(self, code: int) -> str:
        span = self.span(code)
        if span is None or code not in self.axes:
            return "   0%"
        low, high = span
        return f"{round((self.axes[code] - (low + high) / 2) / ((high - low) / 2) * 100):+4d}%"

    def trigger(self, code: int, button: int) -> str:
        span = self.span(code)
        if span is not None and code in self.axes:
            low, high = span
            return f"{round((self.axes[code] - low) / (high - low) * 100):3d}%"
        return "100%" if button in self.held else "  0%"

    def dpad(self) -> str:
        words = []
        x, y = self.axes.get(ABS_HAT0X, 0), self.axes.get(ABS_HAT0Y, 0)
        for active, word in ((y < 0 or BTN_DPAD_UP in self.held, "UP"), (y > 0 or BTN_DPAD_DOWN in self.held, "DOWN"),
                             (x < 0 or BTN_DPAD_LEFT in self.held, "LEFT"),
                             (x > 0 or BTN_DPAD_RIGHT in self.held, "RIGHT")):
            if active:
                words.append(word)
        return " ".join(words) or "-"

    def lines(self, names: dict[int, str]) -> list[str]:
        buttons = [names.get(code, f"BUTTON {code}") for code in sorted(self.held)
                   if code not in (BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT)]
        return [
            f"LEFT STICK    X {self.stick(ABS_X)}   Y {self.stick(ABS_Y)}",
            f"RIGHT STICK   X {self.stick(ABS_RX)}   Y {self.stick(ABS_RY)}",
            f"TRIGGERS      {names.get(BTN_TL2, 'LT')} {self.trigger(ABS_Z, BTN_TL2)}   "
            f"{names.get(BTN_TR2, 'RT')} {self.trigger(ABS_RZ, BTN_TR2)}",
            f"D-PAD         {self.dpad()}",
            f"BUTTONS       {'  '.join(buttons) or '-'}",
        ]


def button_names(name: str) -> dict[int, str]:
    """Button codes to the names printed on this family of pad (as on CONTROLLER BUTTONS)."""
    import couchliteos_controls as controls  # only the launcher needs it, not gamepad-nav

    kind = controls.family([name])
    south, east, north, west, left, right, view, menu, guide = controls.FAMILY_NAMES[kind]
    triggers = {"xbox": ("LT", "RT"), "playstation": ("L2", "R2"), "nintendo": ("ZL", "ZR")}.get(kind, ("LT", "RT"))
    return {
        BTN_SOUTH: south, BTN_EAST: east, BTN_NORTH: north, BTN_WEST: west, BTN_TL: left, BTN_TR: right,
        BTN_TL2: triggers[0], BTN_TR2: triggers[1], BTN_SELECT: view, BTN_START: menu, BTN_MODE: guide,
        BTN_THUMBL: "L3", BTN_THUMBR: "R3",
    }


class Menu:
    """Settings > CONTROLLERS and each pad's own screen."""

    def __init__(
        self, screen: curses.window, read_key: Callable[[curses.window], int] | None = None,
        opener: Callable[[str], object] | None = None, pads: Callable[[], list[Pad]] | None = None,
        config: pathlib.Path | None = None, root: pathlib.Path = LEDS, sys_input: pathlib.Path = SYS_INPUT,
        clock: Callable[[], float] = time.monotonic, background: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.screen = screen
        self.read_key = read_key or (lambda window: window.getch())
        self.opener = opener
        self.config = config
        self.root = root
        self.sys_input = sys_input
        self.clock = clock
        self._pads = pads or (lambda: list_pads(self.opener, settings=self.settings(), sys_input=self.sys_input))
        self.background = background or (lambda work: threading.Thread(target=work, daemon=True).start())
        self.notice = ""

    def settings(self) -> Settings:
        return load(self.config)

    def pads(self) -> list[Pad]:
        try:
            return self._pads()
        except Exception:  # never let a strange device take the launcher down
            return []

    def draw(self, title: str, rows: list[str], selected: int | None, status: str, hint: str = LIST_HINT,
             details: list[str] | None = None) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height >= 8 and width >= 24:
            try:
                self.screen.border()
            except curses.error:
                pass
        top = max(2, height // 8)
        _centered(self.screen, top, title)
        for offset, line in enumerate(details or []):
            _centered(self.screen, top + 2 + offset, line)
        first = max(top + 3 + len(details or []), height // 3)
        left = max(2, (width - max((len(text) for text in rows), default=1) - 3) // 2)
        listview.draw_rows(self.screen, rows, selected, first, height - 4, left)
        _centered(self.screen, height - 3, status or hint)
        self.screen.refresh()

    def run(self) -> None:
        selected = 0
        while True:
            pads = self.pads()
            rows = [row(pad) for pad in pads] + ["BACK"]
            selected = min(selected, len(rows) - 1)
            status = self.notice or ("NO CONTROLLER FOUND" if not pads else ORDER_NOTE)
            self.notice = ""
            self.draw("CONTROLLERS", rows, selected, status)
            key = self.read_key(self.screen)
            selected = _move(selected, key, len(rows))
            if key == 27 or (key in ENTER_KEYS and selected == len(rows) - 1):
                return
            if key in ENTER_KEYS:
                selected = self.pad_screen(pads[selected].ident, selected)

    def save(self, settings: Settings) -> bool:
        try:
            save(settings, self.config)
            return True
        except OSError as error:
            self.notice = f"COULD NOT SAVE: {error}".upper()
            return False

    def pad_screen(self, ident: str, place: int) -> int:
        """One pad's rows; the pad's place in the list when leaving."""
        selected = 0
        while True:
            pads = self.pads()
            index = next((i for i, pad in enumerate(pads) if pad.ident == ident), None)
            if index is None:
                self.notice = "CONTROLLER DISCONNECTED"
                return place
            pad = pads[index]
            place = index
            settings = self.settings()
            rows = list(ROWS)
            rows[4] = f"SWAP A/B  {'ON' if ident in settings.swap_ab else 'OFF'}"
            rows[5] = f"SWAP X/Y  {'ON' if ident in settings.swap_xy else 'OFF'}"
            status = self.notice or {0: ORDER_NOTE, 1: ORDER_NOTE, 4: SWAP_NOTE, 5: SWAP_NOTE}.get(selected, "")
            self.notice = ""
            self.draw(f"PLAYER {pad.player}", rows, selected, status,
                      details=[pad.name.upper()[:60], connection(pad)])
            key = self.read_key(self.screen)
            selected = _move(selected, key, len(rows))
            if key == 27 or (key in ENTER_KEYS and selected == len(rows) - 1):
                return place
            if key not in ENTER_KEYS:
                continue
            if selected in (0, 1):
                changed = move(settings, pads, index, -1 if selected == 0 else 1)
                if changed != settings and self.save(changed):
                    set_player_leds(ordered(pads, changed.order), self.root, self.sys_input)
                    self.notice = f"NOW PLAYER {[p.ident for p in ordered(pads, changed.order)].index(ident) + 1}"
            elif selected == 2:
                self.notice = "IDENTIFYING..."
                self.background(lambda: identify(pad, self.opener, self.root, self.sys_input))
            elif selected == 3:
                self.notice = self.test(pad, settings)
            elif selected in (4, 5):
                field = "swap_ab" if selected == 4 else "swap_xy"
                if self.save(toggle(settings, ident, field)):
                    self.notice = SWAP_NOTE

    def test(self, pad: Pad, settings: Settings) -> str:
        """TEST: the live sticks, triggers and buttons until B / Circle is held or a keyboard's Esc."""
        try:
            device = (self.opener or _evdev_open)(pad.path)
        except OSError:
            return "CONTROLLER DISCONNECTED"
        reading = Reading(device)
        names = button_names(pad.name)
        back = BTN_SOUTH if pad.ident in settings.swap_ab else BTN_EAST  # what gamepad-nav sends as Esc
        back_down: float | None = None
        last_input = float("-inf")
        self.screen.timeout(TEST_POLL_MS)
        try:
            while True:
                try:
                    ready = select.select([device], [], [], 0)[0]
                    events = list(device.read()) if ready else []
                except BlockingIOError:
                    events = []
                except (OSError, ValueError):
                    return "CONTROLLER DISCONNECTED"
                now = self.clock()
                for event in events:
                    reading.feed(event)
                    if event.type in (EV_KEY, EV_ABS):
                        last_input = now
                    if event.type == EV_KEY and event.code == back:
                        back_down = now if event.value else None
                if back_down is not None and now - back_down >= TEST_HOLD_SECONDS:
                    return ""
                self.draw(f"TEST PLAYER {pad.player}", reading.lines(names), None, TEST_HINT)
                key = self.read_key(self.screen)
                if key == 27 and now - last_input >= TEST_QUIET_SECONDS:
                    return ""  # a keyboard's Esc (gamepad-nav's Esc from this pad comes with its own input)
        finally:
            self.screen.timeout(1000)
            try:
                device.close()
            except OSError:
                pass


def _centered(screen: curses.window, row_number: int, text: str) -> None:
    height, width = screen.getmaxyx()
    text = text[: max(0, width - 4)]
    if 0 <= row_number < height:
        try:
            screen.addstr(row_number, max(1, (width - len(text)) // 2), text)
        except curses.error:
            pass


def _move(selected: int, key: int, count: int) -> int:
    if key in (curses.KEY_UP, ord("k")):
        return (selected - 1) % count
    if key in (curses.KEY_DOWN, ord("j")):
        return (selected + 1) % count
    return selected


def run(screen: curses.window, read_key: Callable[[curses.window], int] | None = None) -> None:
    Menu(screen, read_key).run()
