#!/usr/bin/python3
"""Translate gamepad navigation and expose the global controller keyboard chord."""

from __future__ import annotations

import functools
import glob
import os
import pathlib
import queue
import select
import subprocess
import threading
import time
from collections.abc import Callable, Iterable

from evdev import InputDevice, UInput, ecodes

import couchliteos_power as power
import couchliteos_cec as cec
import couchliteos_audio as audio
import couchliteos_brightness as brightness
import couchliteos_input as inputprefs
import couchliteos_pads as padprefs

KEYS = [ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_LEFT, ecodes.KEY_RIGHT,
        ecodes.KEY_ENTER, ecodes.KEY_ESC, ecodes.KEY_DELETE, ecodes.KEY_F12,
        ecodes.KEY_F5, ecodes.KEY_F6, ecodes.KEY_F7, ecodes.KEY_F8,
        # controller mouse (pointer mode): browser back, play/pause, page up/down
        ecodes.KEY_BACK, ecodes.KEY_SPACE, ecodes.KEY_PAGEUP, ecodes.KEY_PAGEDOWN]
RUN = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos"))
OSK_ACTIVE = RUN / "osk-active"
START_OSK = RUN / "start-osk"
HOME_REQUEST = RUN / "home.request"
APP_ACTIVE = RUN / "app-active"
# Written by the launcher for apps without controller support (browsers, web apps): the pad drives a mouse.
POINTER_MODE = RUN / "pointer-mode"
# Touched by the launcher while it holds focus (Home pressed) even though an app runs.
LAUNCHER_FOCUS = RUN / "launcher-focus"
CONTROLLER_ID = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos")) / "launcher-controller.id"
SLEEP_REQUEST = RUN / "suspend"
# Holding Guide is also how pads are switched off (8BitDo ~3 s, Xbox ~6 s, PlayStation ~10 s), so the
# hold is long, and a hold that began in a game never sleeps the box (see app_owns_pad).
SLEEP_HOLD_SECONDS = 5.0
CEC_NAV, CEC_HOME = cec.remote_key_maps(ecodes)
# Remote keys a running app gets too (the grabbed remote reaches nobody else); the rest are launcher shortcuts.
CEC_APP_KEYS = {ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_LEFT, ecodes.KEY_RIGHT, ecodes.KEY_ENTER, ecodes.KEY_ESC}
# Apps that stream a PC or a console and read the controller themselves: while one has the screen
# the Guide / PS button belongs to the stream (Steam Big Picture, the PS menu) and the PlayStation
# touchpad is theirs too. Home is then the Select+Start hold or the keyboard shortcut.
STREAM_APPS = frozenset({"moonlight", "chiaki-ng"})
SONY_VENDOR = 0x054C
_last_state_check = 0.0
_last_state = False
# A held direction repeats like a keyboard: a pause, then steady steps.
REPEAT_DELAY = 0.4
REPEAT_INTERVAL = 0.12
# The left stick counts as a D-pad press past STICK_ENGAGE of full deflection and as let
# go again below STICK_RELEASE, so drift and jitter near the edge do not flutter.
STICK_ENGAGE = 0.6
STICK_RELEASE = 0.4
DPAD_BUTTONS = {
    ecodes.BTN_DPAD_UP: ecodes.KEY_UP,
    ecodes.BTN_DPAD_DOWN: ecodes.KEY_DOWN,
    ecodes.BTN_DPAD_LEFT: ecodes.KEY_LEFT,
    ecodes.BTN_DPAD_RIGHT: ecodes.KEY_RIGHT,
}
# axis -> (key for the negative side, key for the positive side); Y grows downward.
HAT_AXES = {
    ecodes.ABS_HAT0X: (ecodes.KEY_LEFT, ecodes.KEY_RIGHT),
    ecodes.ABS_HAT0Y: (ecodes.KEY_UP, ecodes.KEY_DOWN),
}
STICK_AXES = {
    ecodes.ABS_X: (ecodes.KEY_LEFT, ecodes.KEY_RIGHT),
    ecodes.ABS_Y: (ecodes.KEY_UP, ecodes.KEY_DOWN),
}
# Pointer mode: the left stick moves the pointer, the right stick scrolls.
POINTER_AXES = (ecodes.ABS_X, ecodes.ABS_Y, ecodes.ABS_RX, ecodes.ABS_RY)
POINTER_DEADZONE = 0.15
POINTER_SPEED = 1400.0  # pixels per second at full deflection
SCROLL_SPEED = 14.0  # wheel steps per second at full deflection
POINTER_TICK = 1 / 60
POINTER_BUTTONS = {
    ecodes.BTN_SOUTH: ecodes.BTN_LEFT,  # A / Cross clicks (hold to drag)
    ecodes.BTN_WEST: ecodes.BTN_RIGHT,  # Y on Xbox pads, Square on PlayStation pads
}
POINTER_KEYS = {
    ecodes.BTN_EAST: ecodes.KEY_BACK,  # B / Circle: back a page
    ecodes.BTN_START: ecodes.KEY_SPACE,  # play / pause
    ecodes.BTN_SELECT: ecodes.KEY_ESC,  # leave full screen, close a pop-up
    ecodes.BTN_TL: ecodes.KEY_PAGEUP,
    ecodes.BTN_TR: ecodes.KEY_PAGEDOWN,
}
# A keyboard's brightness and volume keys (Apple's Fn layer sends the same codes). Cage does not
# act on them, so gamepad-nav does, the same way the Guide menu's rows do.
BRIGHTNESS_KEYS = {ecodes.KEY_BRIGHTNESSUP: brightness.STEP, ecodes.KEY_BRIGHTNESSDOWN: -brightness.STEP}
VOLUME_KEYS = {ecodes.KEY_VOLUMEUP: audio.VOLUME_STEP, ecodes.KEY_VOLUMEDOWN: -audio.VOLUME_STEP}
MEDIA_KEYS = frozenset({*BRIGHTNESS_KEYS, *VOLUME_KEYS, ecodes.KEY_MUTE})
# The ACPI device a laptop's own brightness keys arrive from; see brightness.kernel_handles_hotkeys.
ACPI_VIDEO_KEYS = "Video Bus"
# Settings > CONTROLS (the launcher saves them in config.ini): re-read when the file changes.
INPUT_SETTINGS = inputprefs.Watcher()
# Settings > CONTROLLERS: the player order and each pad's SWAP A/B and SWAP X/Y, same file.
PAD_SETTINGS = padprefs.Watcher()
# The Home shortcut's held pair, and the buttons that cancel it: Moonlight quits a stream on
# Select+Start+L1+R1, so SELECT+START with a shoulder button held is left to Moonlight.
HOME_COMBOS = {
    inputprefs.HOME_SELECT_START: (frozenset({ecodes.BTN_SELECT, ecodes.BTN_START}),
                                   frozenset({ecodes.BTN_TL, ecodes.BTN_TR})),
    inputprefs.HOME_STICKS: (frozenset({ecodes.BTN_THUMBL, ecodes.BTN_THUMBR}), frozenset()),
}
HOME_COMBO_CODES = frozenset().union(*(pair | cancel for pair, cancel in HOME_COMBOS.values()))
# SELECT and START in the launcher: the VIEW and MENU button shortcuts (see PairTaps).
NAV_TAP_KEYS = {ecodes.BTN_SELECT: ecodes.KEY_F7, ecodes.BTN_START: ecodes.KEY_F8}
# Modifier key codes and the family each belongs to (CTRL, ALT, SHIFT, SUPER), for the keyboard Home key.
MODIFIER_CODES = {getattr(ecodes, name): family for name, family in inputprefs.MODIFIER_FAMILIES.items()
                  if hasattr(ecodes, name)}
OWN_DEVICE_PREFIX = "CouchLiteOS "  # our uinput devices: the pad's keyboard, the on-screen keyboard, the mouse


def app_active() -> bool:
    global _last_state_check, _last_state
    now = time.monotonic()
    if now - _last_state_check < 0.5:
        return _last_state
    _last_state_check = now
    _last_state = APP_ACTIVE.exists()
    return _last_state


def app_owns_pad() -> bool:
    """An app or game is running and the launcher has not taken the controller back (not cached)."""
    return APP_ACTIVE.exists() and not LAUNCHER_FOCUS.exists()


def stream_owns_pad() -> bool:
    """A streaming client (Moonlight, Chiaki) is the app in front: not cached, like app_owns_pad."""
    try:
        app = APP_ACTIVE.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False
    return app in STREAM_APPS and not LAUNCHER_FOCUS.exists()


def effective_home_choice(choice: str) -> str:
    """The Home shortcut in force: Guide is the stream's while one is in front, so a pad that chose
    "Guide only" still gets the Select+Start hold as its way home."""
    if choice == inputprefs.HOME_GUIDE and stream_owns_pad():
        return inputprefs.HOME_SELECT_START
    return choice


def navigation_blocked(active_osk: bool) -> bool:
    """An app owns the controller, unless the launcher was brought back with Home."""
    return app_active() and not active_osk and not LAUNCHER_FOCUS.exists()


def pointer_mode(active_osk: bool) -> bool:
    """The app in front has no controller support and the pad drives a mouse for it."""
    return not active_osk and POINTER_MODE.exists() and app_owns_pad()


def open_keyboard() -> None:
    """Open the buffered keyboard over the app in front; it types into that app when it closes."""
    if OSK_ACTIVE.exists():
        return
    try:
        START_OSK.touch()
    except OSError:
        pass


def stick_curve(value: float) -> float:
    """-1.0..1.0 of full deflection to a pointer speed factor: nothing in the dead zone,
    then quadratic so small pushes are precise and a full push crosses the screen quickly."""
    magnitude = abs(value)
    if magnitude < POINTER_DEADZONE:
        return 0.0
    scaled = min(1.0, (magnitude - POINTER_DEADZONE) / (1 - POINTER_DEADZONE))
    return scaled * scaled * (1 if value > 0 else -1)


def pointer_speed() -> float:
    """Pixels per second at full deflection: POINTER_SPEED scaled by Settings > CONTROLLER MOUSE SPEED."""
    return POINTER_SPEED * inputprefs.PAD_SPEEDS.get(INPUT_SETTINGS.current().pad_speed, 1.0)


class Pointer:
    """A virtual mouse, present only while pointer mode is on: Cage shows a cursor
    whenever a pointer device exists, and the launcher should never have one."""

    def __init__(self, factory=None) -> None:
        self.factory = factory or (lambda: UInput(
            {ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL, ecodes.REL_HWHEEL],
             ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE]},
            name="CouchLiteOS Controller Mouse",
        ))
        self.device = None
        self.held: set[int] = set()

    def open(self) -> None:
        if self.device is None:
            try:
                self.device = self.factory()
            except Exception:  # OSError, or evdev's UInputError: the pad simply stays a pad
                self.device = None

    def close(self) -> None:
        if self.device is None:
            self.held.clear()  # never sent (no device): forget them or the next press is swallowed
            return
        for button in sorted(self.held):
            self.button(button, False)
        try:
            self.device.close()
        except OSError:
            pass
        self.device = None

    def write(self, *events: tuple[int, int, int]) -> None:
        if self.device is None or not events:
            return
        try:
            for event in events:
                self.device.write(*event)
            self.device.syn()
        except OSError:
            pass

    def move(self, dx: int, dy: int) -> None:
        self.write(*[(ecodes.EV_REL, code, value) for code, value in
                     ((ecodes.REL_X, dx), (ecodes.REL_Y, dy)) if value])

    def scroll(self, vertical: int, horizontal: int) -> None:
        self.write(*[(ecodes.EV_REL, code, value) for code, value in
                     ((ecodes.REL_WHEEL, vertical), (ecodes.REL_HWHEEL, horizontal)) if value])

    def button(self, code: int, pressed: bool) -> None:
        if pressed == (code in self.held):
            return
        self.held.add(code) if pressed else self.held.discard(code)
        self.write((ecodes.EV_KEY, code, int(pressed)))


def is_gamepad(dev: InputDevice) -> bool:
    keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
    return ecodes.BTN_GAMEPAD in keys or ecodes.BTN_SOUTH in keys


def is_playstation_touchpad(dev: InputDevice) -> bool:
    """The "... Touchpad" node hid-playstation / hid-sony create for a DualShock 4 or DualSense."""
    try:
        keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
        return (
            dev.info.vendor == SONY_VENDOR
            and str(dev.name).strip().lower().endswith("touchpad")
            and ecodes.BTN_TOUCH in keys
            and not ({ecodes.BTN_GAMEPAD, ecodes.BTN_SOUTH} & keys)
        )
    except (AttributeError, OSError):
        return False


def save_identity(dev: InputDevice) -> None:
    """Record the launcher controller so USB/IP never exports it (first pad found)."""
    serial = (dev.uniq or "*").lower()
    try:
        CONTROLLER_ID.write_text(f"{dev.info.vendor:04x}:{dev.info.product:04x}:{serial}\n")
    except OSError:
        pass  # the pad still has to work


def stick_direction(value: float, held: int, other: float) -> int:
    """-1, 0 or +1 for one left-stick axis (value is -1.0..1.0 of full deflection).

    Needs a firm push, lets go only below the lower release threshold, and on a diagonal
    only the stronger axis counts so one push is one step.
    """
    sign = 1 if value > 0 else -1
    limit = STICK_RELEASE if held == sign else STICK_ENGAGE
    return sign if abs(value) >= limit and abs(value) >= other else 0


class Hold:
    """The arrows one pad is holding down (newest wins) and when the next repeat is due."""

    def __init__(self) -> None:
        self.sources: dict[int, int] = {}  # input code -> arrow key, oldest first
        self.due = 0.0

    @property
    def key(self) -> int | None:
        return next(reversed(self.sources.values()), None)

    def press(self, source: int, key: int, now: float) -> None:
        self.sources.pop(source, None)
        self.sources[source] = key
        self.due = now + REPEAT_DELAY

    def release(self, source: int, now: float) -> None:
        newest = self.key
        self.sources.pop(source, None)
        if self.key is not None and self.key != newest:
            self.due = now + REPEAT_DELAY  # an older direction is still held: resume it

    def repeat(self, now: float) -> int | None:
        if self.key is None or now < self.due:
            return None
        self.due = now + REPEAT_INTERVAL
        return self.key


class Pads:
    """Every gamepad driving the launcher, keyed by /dev/input path."""

    def __init__(self) -> None:
        self.devices: dict[str, InputDevice] = {}
        self.grabbed: set[str] = set()
        self.ignored: set[str] = set()  # nodes already seen to be something else
        self.idents: dict[str, str] = {}  # path -> the pad's id in Settings > CONTROLLERS
        self.touchpads: dict[str, InputDevice] = {}  # PlayStation pad touchpads, held back from the pointer during a stream
        self.touch_grabbed: set[str] = set()
        self.clock = time.monotonic
        self.holds: dict[str, Hold] = {}
        self.sticks: dict[str, dict[int, list]] = {}  # path -> axis -> [-1.0..1.0, -1/0/+1 held]
        self.spans: dict[tuple[str, int], tuple[int, int] | None] = {}
        self.pointer = Pointer()
        self.pointer_on = False
        self.pointer_tick = 0.0
        self.carry = [0.0, 0.0, 0.0, 0.0]  # x, y, wheel, horizontal wheel not yet sent
        self.combo = HomeCombo()  # the mouse grabs the pads, hiding the Home shortcut from watch_home()
        self.taps = PairTaps()  # SELECT and START act when let go while they are the Home hold

    def rescan(self) -> None:
        """Follow hot-plug: add pads that appeared, forget those that went away."""
        paths = set(glob.glob("/dev/input/event*"))
        for path in set(self.devices) - paths:
            self.drop(path)
        for path in set(self.touchpads) - paths:
            self.drop_touchpad(path)
        self.ignored &= paths
        added = False
        for path in sorted(paths - set(self.devices) - set(self.touchpads) - self.ignored):
            dev = None
            try:
                dev = InputDevice(path)
                if is_playstation_touchpad(dev):
                    self.touchpads[path] = dev
                    continue
                if not is_gamepad(dev):
                    self.ignored.add(path)
                    dev.close()
                    continue
                if not self.devices:
                    save_identity(dev)
                self.devices[path] = dev
                self.idents[path] = padprefs.pad_id(dev)
                added = True
            except OSError:
                if dev is not None:
                    try:
                        dev.close()
                    except OSError:
                        pass
        if added:
            self.light_players()

    def light_players(self) -> None:
        """Set the player lights in the Settings > CONTROLLERS order when a pad arrives."""
        try:
            found = [padprefs.from_device(path, dev) for path, dev in self.devices.items()]
            padprefs.set_player_leds(padprefs.ordered(found, PAD_SETTINGS.current().order))
        except Exception:  # lights are a nicety: never stop the pad working over them
            pass

    def drop(self, path: str) -> None:
        self.holds.pop(path, None)
        self.idents.pop(path, None)
        self.combo.forget(path)
        self.taps.forget(path)
        self.sticks.pop(path, None)
        for key in [key for key in self.spans if key[0] == path]:
            del self.spans[key]
        dev = self.devices.pop(path, None)
        if dev is None:
            return
        try:
            if path in self.grabbed:
                dev.ungrab()
            dev.close()
        except OSError:
            pass
        self.grabbed.discard(path)
        if not self.devices:
            # The last pad went (idle timeout, flat battery): no cursor or held click without one.
            self.set_pointer(False, self.clock())

    def drop_all(self) -> None:
        for path in list(self.devices):
            self.drop(path)
        for path in list(self.touchpads):
            self.drop_touchpad(path)

    def drop_touchpad(self, path: str) -> None:
        dev = self.touchpads.pop(path, None)
        self.touch_grabbed.discard(path)
        if dev is not None:
            try:
                dev.close()  # closing the descriptor also lets go of the grab
            except OSError:
                pass

    def sync_touchpads(self, streaming: bool) -> None:
        """While a stream is in front the PlayStation touchpad is grabbed, so the compositor does not
        move the pointer with it; the streaming client reads it from the pad itself. Otherwise it is a
        plain touchpad again (browser, desktop, the launcher)."""
        for path, dev in list(self.touchpads.items()):
            if streaming == (path in self.touch_grabbed):
                continue
            try:
                dev.grab() if streaming else dev.ungrab()
            except OSError:
                if streaming:
                    continue  # busy or gone: try again on the next pass
                self.drop_touchpad(path)  # could not let go: reopen it with a fresh descriptor
                continue
            self.touch_grabbed.add(path) if streaming else self.touch_grabbed.discard(path)

    def sync_grab(self, active_osk: bool) -> None:
        """The on-screen keyboard grabs every pad so games never see its input."""
        for path, dev in self.devices.items():
            if active_osk == (path in self.grabbed):
                continue
            try:
                dev.grab() if active_osk else dev.ungrab()
            except OSError:
                self.grabbed.discard(path)
                continue
            self.grabbed.add(path) if active_osk else self.grabbed.discard(path)

    def axis_span(self, path: str, dev: InputDevice, code: int) -> tuple[int, int] | None:
        """(minimum, maximum) the pad reports for an axis; Xbox pads use +-32768, PlayStation pads 0-255."""
        if (path, code) not in self.spans:
            span = None
            try:
                info = dev.absinfo(code)
                if info is not None and info.max > info.min:
                    span = (info.min, info.max)
            except OSError:
                pass
            self.spans[(path, code)] = span
        return self.spans[(path, code)]

    def hold(self, path: str) -> Hold:
        return self.holds.setdefault(path, Hold())

    def record_axis(self, path: str, dev: InputDevice, event) -> bool:
        """Store a stick axis as -1.0..1.0 of full deflection; False when the pad gives no range."""
        span = self.axis_span(path, dev, event.code)
        if span is None:
            return False
        low, high = span
        value = (event.value - (low + high) / 2) / ((high - low) / 2)
        self.sticks.setdefault(path, {}).setdefault(event.code, [0.0, 0])[0] = value
        return True

    def handle(self, path: str, dev: InputDevice, event, ui: UInput, now: float) -> bool:
        """Translate one event; True when a left-stick axis moved (judged once per batch)."""
        if event.type == ecodes.EV_ABS and event.code in STICK_AXES:
            return self.record_axis(path, dev, event)
        arrow = arrow_for_event(event)
        if arrow is not None:
            source, key = arrow
            if key is None:
                self.hold(path).release(source, now)
            else:
                emit(ui, key)
                self.hold(path).press(source, key, now)
            return False
        if event.type == ecodes.EV_KEY and event.code in PairTaps.BUTTONS:
            tapped = self.taps.feed(path, event)
            if tapped is not None:
                emit(ui, NAV_TAP_KEYS[tapped])
            return False
        # Only here, in the launcher: a stream or an app reads the pad itself, unswapped.
        key = key_for_event(event, PAD_SETTINGS.current().swaps(self.idents.get(path, "")))
        if key:
            emit(ui, key)
        return False

    def handle_pointer(self, path: str, dev: InputDevice, event, ui: UInput, now: float) -> None:
        """Pointer mode: sticks move and scroll, A and Y click, the D-pad still sends arrows."""
        if event.type == ecodes.EV_ABS and event.code in POINTER_AXES:
            self.record_axis(path, dev, event)
            return
        arrow = arrow_for_event(event)
        if arrow is not None:
            source, key = arrow
            if key is None:
                self.hold(path).release(source, now)
            else:
                emit(ui, key)
                self.hold(path).press(source, key, now)
            return
        if event.type != ecodes.EV_KEY:
            return
        self.combo.feed(path, event, now)
        if is_home_event(event):
            # The grab hides the pad from watch_home(): Guide must still bring up the menu.
            request_home(guide=True)
        elif event.code in POINTER_BUTTONS and event.value in (0, 1):
            self.pointer.button(POINTER_BUTTONS[event.code], bool(event.value))
        elif event.value == 1 and event.code == ecodes.BTN_NORTH:
            open_keyboard()  # X / Triangle: type into the page
        elif event.code in PairTaps.BUTTONS:
            tapped = self.taps.feed(path, event)
            if tapped is not None:
                emit(ui, POINTER_KEYS[tapped])
        elif event.value == 1 and event.code in POINTER_KEYS:
            emit(ui, POINTER_KEYS[event.code])

    def stick_value(self, code: int) -> float:
        """The strongest push on one axis across every pad."""
        values = [axes[code][0] for axes in self.sticks.values() if code in axes]
        return max(values, key=abs, default=0.0)

    def pointer_moving(self) -> bool:
        return any(stick_curve(self.stick_value(code)) for code in POINTER_AXES)

    def tick_pointer(self, now: float) -> None:
        """Send the motion and scrolling the sticks asked for since the last tick."""
        elapsed = min(0.05, max(0.0, now - self.pointer_tick))
        self.pointer_tick = now
        speed = pointer_speed()
        rates = (
            stick_curve(self.stick_value(ecodes.ABS_X)) * speed,
            stick_curve(self.stick_value(ecodes.ABS_Y)) * speed,
            -stick_curve(self.stick_value(ecodes.ABS_RY)) * SCROLL_SPEED,  # pushed up scrolls up
            stick_curve(self.stick_value(ecodes.ABS_RX)) * SCROLL_SPEED,
        )
        steps = []
        for index, rate in enumerate(rates):
            if not rate:
                self.carry[index] = 0.0
                steps.append(0)
                continue
            self.carry[index] += rate * elapsed
            whole = int(self.carry[index])
            self.carry[index] -= whole
            steps.append(whole)
        self.pointer.move(steps[0], steps[1])
        self.pointer.scroll(steps[2], steps[3])

    def set_pointer(self, on: bool, now: float) -> None:
        if on == self.pointer_on:
            if on and self.pointer.device is None:
                self.pointer.open()  # creating the mouse failed before: try again
            return
        self.pointer_on = on
        self.carry = [0.0, 0.0, 0.0, 0.0]
        self.sticks.clear()  # a stick held across the switch must not keep moving or scrolling
        self.pointer_tick = now
        self.holds.clear()
        self.combo.reset()
        self.taps.reset()
        self.pointer.open() if on else self.pointer.close()

    def update_stick(self, path: str, ui: UInput, now: float) -> None:
        axes = self.sticks.get(path, {})
        for code in STICK_AXES:
            axes.setdefault(code, [0.0, 0])
        horizontal, vertical = (axes[code] for code in STICK_AXES)
        for code, entry, other in (
            (ecodes.ABS_X, horizontal, abs(vertical[0])), (ecodes.ABS_Y, vertical, abs(horizontal[0]))
        ):
            direction = stick_direction(entry[0], entry[1], other)
            if direction == entry[1]:
                continue
            entry[1] = direction
            if direction == 0:
                self.hold(path).release(code, now)
            else:
                key = STICK_AXES[code][direction > 0]
                emit(ui, key)
                self.hold(path).press(code, key, now)

    def repeat_timeout(self, now: float) -> float:
        """How long select() may sleep: a second when idle, less when a repeat is due sooner
        or a stick is moving the pointer."""
        moving = [POINTER_TICK] if self.pointer_on and self.pointer_moving() else []
        moving += [self.combo.timeout(now)] if self.pointer_on and self.combo.started else []
        return min([1.0, *moving,
                    *(max(0.0, hold.due - now) for hold in self.holds.values() if hold.key is not None)])

    def pump(self, ui: UInput) -> None:
        """Wait up to a second for input from any pad and translate it."""
        try:
            readable, _writable, _errors = select.select(
                list(self.devices.values()), [], [], self.repeat_timeout(self.clock())
            )
        except (OSError, ValueError):  # a pad vanished under select
            self.drop_all()
            time.sleep(1)
            return
        now = self.clock()
        self.sync_touchpads(stream_owns_pad())
        active_osk = OSK_ACTIVE.exists()
        pointer = pointer_mode(active_osk)
        self.set_pointer(pointer, now)
        # Without a virtual mouse (uinput failed) the pad stays a pad rather than going dead.
        pointer = pointer and self.pointer.device is not None
        # The keyboard and the mouse grab every pad so the app never also sees the presses.
        self.sync_grab(active_osk or pointer)
        blocked = navigation_blocked(active_osk) and not pointer
        if blocked:
            self.holds.clear()  # an app owns the controller now; do not keep stepping its menus
            self.taps.reset()  # nor send a SELECT/START pressed before it on release
        for path, dev in list(self.devices.items()):
            if dev not in readable:
                continue
            stick_moved = False
            try:
                for event in dev.read():
                    if pointer:
                        self.handle_pointer(path, dev, event, ui, now)
                    elif not blocked:
                        stick_moved = self.handle(path, dev, event, ui, now) or stick_moved
            except BlockingIOError:
                pass
            except OSError:
                self.drop(path)
                continue
            if stick_moved:
                self.update_stick(path, ui, now)
        if pointer:
            self.tick_pointer(now)
            if self.combo.due(now):
                request_home()
        if not blocked:
            for hold in self.holds.values():
                key = hold.repeat(now)
                if key:
                    emit(ui, key)


def emit(ui: UInput, key: int) -> None:
    ui.write(ecodes.EV_KEY, key, 1)
    ui.write(ecodes.EV_KEY, key, 0)
    ui.syn()


def arrow_for_event(event) -> tuple[int, int | None] | None:
    """(input code, arrow key now held, or None once let go) for the D-pad buttons and the hat."""
    if event.type == ecodes.EV_KEY and event.code in DPAD_BUTTONS and event.value in (0, 1):
        return event.code, DPAD_BUTTONS[event.code] if event.value else None
    if event.type == ecodes.EV_ABS and event.code in HAT_AXES:
        negative, positive = HAT_AXES[event.code]
        return event.code, (positive if event.value > 0 else negative) if event.value else None
    return None


def key_for_event(event, swaps: dict[int, int] | None = None) -> int | None:
    """The launcher key for a press; `swaps` (Settings > CONTROLLERS SWAP A/B, SWAP X/Y) reads one
    button as another first."""
    if event.type == ecodes.EV_KEY and event.value == 1:
        code = (swaps or {}).get(event.code, event.code)
        return {
            ecodes.BTN_SOUTH: ecodes.KEY_ENTER,
            ecodes.BTN_EAST: ecodes.KEY_ESC,
            ecodes.BTN_WEST: ecodes.KEY_DELETE,
            # BTN_NORTH is X on Xbox pads and Triangle on PlayStation pads; it opens
            # the buffered keyboard (F12) for launcher text fields.
            ecodes.BTN_NORTH: ecodes.KEY_F12,
            # Launcher button shortcuts, configured per button in Settings.
            ecodes.BTN_TL: ecodes.KEY_F5,
            ecodes.BTN_TR: ecodes.KEY_F6,
            ecodes.BTN_SELECT: ecodes.KEY_F7,
            ecodes.BTN_START: ecodes.KEY_F8,
            ecodes.BTN_DPAD_UP: ecodes.KEY_UP,
            ecodes.BTN_DPAD_DOWN: ecodes.KEY_DOWN,
            ecodes.BTN_DPAD_LEFT: ecodes.KEY_LEFT,
            ecodes.BTN_DPAD_RIGHT: ecodes.KEY_RIGHT,
        }.get(code)
    if event.type == ecodes.EV_ABS:
        if event.code == ecodes.ABS_HAT0X and event.value:
            return ecodes.KEY_RIGHT if event.value > 0 else ecodes.KEY_LEFT
        if event.code == ecodes.ABS_HAT0Y and event.value:
            return ecodes.KEY_DOWN if event.value > 0 else ecodes.KEY_UP
    return None


def is_home_event(event) -> bool:
    return (
        event.type == ecodes.EV_KEY
        and event.value == 1
        and event.code in {ecodes.KEY_HOME, ecodes.KEY_HOMEPAGE, ecodes.BTN_MODE}
    )


def is_guide_press(event) -> bool:
    """The pad's Guide / PS button going down: BTN_MODE, or KEY_HOMEPAGE (how Bluetooth Xbox pads
    send it). The keyboard's Home key (KEY_HOME) is not one."""
    return (
        event.type == ecodes.EV_KEY and event.value == 1 and event.code in {ecodes.KEY_HOMEPAGE, ecodes.BTN_MODE}
    )


class HomeHold:
    """Watches Home/Guide for a long press (a short press is handled on press as before).

    Self-contained: call feed() for every key event, due() each time round the event loop
    (act when it returns True), timeout() for the select() wait, and reset() when a pad goes away.
    """

    CODES = frozenset({ecodes.KEY_HOME, ecodes.BTN_MODE, ecodes.KEY_HOMEPAGE})

    def __init__(self, threshold: float = SLEEP_HOLD_SECONDS) -> None:
        self.threshold = threshold
        self.pressed: dict[int, float] = {}

    def feed(self, event, now: float) -> None:
        if event.type != ecodes.EV_KEY or event.code not in self.CODES:
            return
        if event.value == 1:
            self.pressed[event.code] = now
        elif event.value == 0:
            self.pressed.pop(event.code, None)

    def due(self, now: float) -> bool:
        """True once per press, when it has been held for the threshold."""
        for code, started in list(self.pressed.items()):
            if now - started >= self.threshold:
                del self.pressed[code]
                return True
        return False

    def timeout(self, now: float, default: float = 1.0) -> float:
        if not self.pressed:
            return default
        remaining = self.threshold - (now - min(self.pressed.values()))
        return max(0.0, min(default, remaining))

    def reset(self) -> None:
        self.pressed.clear()


def media_action(event, device):
    """What a brightness, volume or mute key does, as a call to make; None for any other event.

    Brightness and volume step again while the key is held (autorepeat); mute toggles once a press.
    """
    if event.type != ecodes.EV_KEY or event.code not in MEDIA_KEYS or event.value not in (1, 2):
        return None
    if event.code in BRIGHTNESS_KEYS:
        if device.name == ACPI_VIDEO_KEYS and brightness.kernel_handles_hotkeys():
            return None  # the kernel has already stepped it
        return functools.partial(brightness.change, BRIGHTNESS_KEYS[event.code])
    if event.code in VOLUME_KEYS:
        # Only sets it: nothing shows the level, and reading it back would double the wpctl calls.
        return functools.partial(audio.step_volume, VOLUME_KEYS[event.code])
    return audio.toggle_mute if event.value == 1 else None


# Key actions run on their own thread: wpctl can be slow, and Home must not wait for it. A key
# held while a change is stuck queues a few steps, not a long tail.
MEDIA_QUEUE: queue.Queue = queue.Queue(maxsize=4)


class MediaRepeat:
    """Paces a held brightness or volume key like a held D-pad direction.

    The keyboard autorepeats every ~33 ms, faster than wpctl runs and too fast to stop on the
    level wanted, so a held key steps after REPEAT_DELAY and then at most every REPEAT_INTERVAL,
    and not at all while earlier steps are still waiting: letting go stops it at once instead of
    running on through a backlog. Presses always count. allow() every event, per keyboard.
    """

    def __init__(self, delay: float = REPEAT_DELAY, interval: float = REPEAT_INTERVAL,
                 busy: Callable[[], bool] | None = None) -> None:
        self.delay = delay
        self.interval = interval
        self.busy = busy or (lambda: not MEDIA_QUEUE.empty())
        self.due: dict[tuple[object, int], float] = {}  # (keyboard, key) -> when it may step again

    def allow(self, path: object, event, now: float) -> bool:
        """False for an autorepeat of a media key that comes too soon (or while steps are queued)."""
        if event.type != ecodes.EV_KEY or event.code not in MEDIA_KEYS:
            return True
        key = (path, event.code)
        if event.value == 1:
            self.due[key] = now + self.delay
        elif event.value == 0:
            self.due.pop(key, None)
        elif key not in self.due:
            self.due[key] = now + self.delay  # held since before this keyboard was watched
            return False
        elif now < self.due[key] or self.busy():
            return False
        else:
            self.due[key] = now + self.interval
        return True

    def forget(self, path: object) -> None:
        for key in [key for key in self.due if key[0] == path]:
            del self.due[key]

    def reset(self) -> None:
        self.due.clear()


def queue_media(action) -> None:
    try:
        MEDIA_QUEUE.put_nowait(action)
    except queue.Full:
        pass


def apply_media() -> None:
    while True:
        action = MEDIA_QUEUE.get()
        try:
            action()
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"media key not applied: {error}", flush=True)


class HomeCombo:
    """Holding SELECT+START (or L3+R3, chosen in Settings > CONTROLS) for 1.5 s opens Home, like Guide.

    For pads without a usable Guide button. A tap does nothing, letting go of either button
    early cancels, and a hold that has SELECT+START plus L1 or R1 is Moonlight's quit and never
    counts (nor does it once the shoulder is let go: the pair has to be pressed afresh). Tracked
    per pad: feed() every key event, due() each time round the event loop (open Home when it
    returns True), timeout() for the select() wait, forget()/reset() when a pad goes away.
    """

    def __init__(self, threshold: float = inputprefs.HOME_HOLD_SECONDS,
                 choice: Callable[[], str] | None = None) -> None:
        self.threshold = threshold
        self.choice = choice or (lambda: INPUT_SETTINGS.current().home)
        self.held: dict[object, set[int]] = {}
        self.started: dict[object, float] = {}
        self.spent: set[object] = set()  # pads whose current hold may not open Home

    def combo(self) -> tuple[frozenset[int], frozenset[int]] | None:
        return HOME_COMBOS.get(effective_home_choice(self.choice()))

    def feed(self, path: object, event, now: float, pressed: Iterable[int] | None = None) -> None:
        """`pressed`: the pad's real key state when it is known (it may have been held while grabbed)."""
        if event.type != ecodes.EV_KEY or event.code not in HOME_COMBO_CODES or event.value not in (0, 1):
            return
        held = self.held.setdefault(path, set())
        if pressed is not None:
            held.clear()
            held.update(set(pressed) & HOME_COMBO_CODES)
        if event.value:
            held.add(event.code)
        else:
            held.discard(event.code)
        combo = self.combo()
        if combo is None or not combo[0] <= held:
            self.started.pop(path, None)
            self.spent.discard(path)
        elif combo[1] & held:
            self.started.pop(path, None)
            self.spent.add(path)
        elif path not in self.spent:
            self.started.setdefault(path, now)

    def due(self, now: float, pressed: Callable[[object], Iterable[int] | None] | None = None) -> bool:
        """True once per hold, when the pair has been held for the threshold.

        `pressed(path)`: the pad's real key state, checked before a hold counts. The pad may have
        been grabbed mid-hold, and then the release never arrived here. Holds that overlap (two
        pads at once) open Home once: every hold under way is spent with the one that fires.
        """
        combo = self.combo()
        for path, started in list(self.started.items()):
            if combo is not None and now - started >= self.threshold and pressed is not None:
                keys = pressed(path)
                if keys is not None:
                    held = self.held.setdefault(path, set())
                    held.clear()
                    held.update(set(keys) & HOME_COMBO_CODES)
            held = self.held.get(path, set())
            if combo is None or not combo[0] <= held:
                del self.started[path]  # let go unseen, or the setting changed under the hold
                self.spent.discard(path)
            elif combo[1] & held:
                del self.started[path]  # a shoulder went down unseen: Moonlight's quit
                self.spent.add(path)
            elif now - started >= self.threshold:
                self.spent.update(self.started)
                self.started.clear()
                return True
        return False

    def timeout(self, now: float, default: float = 1.0) -> float:
        if not self.started:
            return default
        return max(0.0, min(default, self.threshold - (now - min(self.started.values()))))

    def forget(self, path: object) -> None:
        self.held.pop(path, None)
        self.started.pop(path, None)
        self.spent.discard(path)

    def reset(self) -> None:
        self.held.clear()
        self.started.clear()
        self.spent.clear()


class HomeChord:
    """The keyboard Home key (Settings > CONTROLS, Ctrl+Alt+H until it is changed) opens Home.

    The modifiers held must be exactly the chord's (either Ctrl, either Alt ...) and all on one keyboard.
    It fires as the chord's key goes down, once a press, and also over a stream or remote desktop:
    it is the keyboard's way home from one (Guide is the pad's). A lone Super tap does nothing.
    """

    def __init__(self, chord: Callable[[], str] | None = None) -> None:
        self.chord = chord or (lambda: INPUT_SETTINGS.current().keyboard_home)
        self.held: dict[object, set[int]] = {}

    def feed(self, path: object, event) -> bool:
        """True when this event is the last key of the chord going down; `path` names the keyboard."""
        if event.type != ecodes.EV_KEY:
            return False
        if event.code in MODIFIER_CODES:
            if event.value == 1:
                self.held.setdefault(path, set()).add(event.code)
            elif event.value == 0:
                self.held.get(path, set()).discard(event.code)
            return False
        if event.value != 1:
            return False
        names = inputprefs.parse_chord(self.chord())
        if not names or event.code not in chord_key_codes(names):
            return False
        wanted = {inputprefs.chord_family(name) for name in names if name in inputprefs.MODIFIER_FAMILIES}
        return {MODIFIER_CODES[code] for code in self.held.get(path, set())} == wanted

    def forget(self, path: object) -> None:
        self.held.pop(path, None)

    def reset(self) -> None:
        self.held.clear()


class PairTaps:
    """SELECT's and START's own keys (F7/F8 in the launcher, Esc/Space for the controller mouse)
    wait for the button to be let go while SELECT+START is the Home hold.

    Sent on press, starting the hold would first leave full screen, pause the video or launch
    the button's app. So a button counts only as a tap: let go without the other one having
    been pressed meanwhile (which also rules out the hold having fired). With another Home
    shortcut chosen, they act on press as before. Tracked per pad.
    """

    BUTTONS = frozenset({ecodes.BTN_SELECT, ecodes.BTN_START})

    def __init__(self, deferred: Callable[[], bool] | None = None) -> None:
        self.deferred = deferred or (
            lambda: effective_home_choice(INPUT_SETTINGS.current().home) == inputprefs.HOME_SELECT_START)
        self.down: dict[object, set[int]] = {}
        self.armed: dict[object, set[int]] = {}  # pressed alone so far: a tap when let go

    def feed(self, path: object, event) -> int | None:
        """The button (BTN_SELECT or BTN_START) whose own key is due now, or None."""
        if event.type != ecodes.EV_KEY or event.code not in self.BUTTONS or event.value not in (0, 1):
            return None
        down = self.down.setdefault(path, set())
        armed = self.armed.setdefault(path, set())
        if event.value:
            together = bool(down - {event.code})
            down.add(event.code)
            if not self.deferred():
                armed.clear()
                return event.code
            if together:
                armed.clear()  # SELECT+START: the Home hold, or Moonlight's quit, not two taps
            else:
                armed.add(event.code)
            return None
        down.discard(event.code)
        if event.code in armed:
            armed.discard(event.code)
            return event.code
        return None

    def forget(self, path: object) -> None:
        self.down.pop(path, None)
        self.armed.pop(path, None)

    def reset(self) -> None:
        self.down.clear()
        self.armed.clear()


def pressed_keys(device) -> list[int] | None:
    """The keys a device has down right now (EVIOCGKEY), or None when it cannot say."""
    try:
        return list(device.active_keys())
    except (AttributeError, OSError):
        return None


def chord_key_codes(names) -> set[int]:
    """The codes of the chord's own (non-modifier) keys; empty when a name is unknown here."""
    codes = {getattr(ecodes, name, None) for name in names if name not in inputprefs.MODIFIER_FAMILIES}
    return codes if None not in codes else set()


def chord_codes(chord: str) -> set[int]:
    """Every key the chord needs a keyboard to have: its key plus one key of each modifier family."""
    names = inputprefs.parse_chord(chord)
    key = chord_key_codes(names)
    if not key:
        return set()
    return key | {min(code for code, family in MODIFIER_CODES.items() if family == inputprefs.chord_family(name))
                  for name in names if name in inputprefs.MODIFIER_FAMILIES}


def is_pad_or_media(keys: set[int]) -> bool:
    """Keys that make a device worth watching whatever its bus: Home/Guide, a pad pair, media keys."""
    return watches_home(keys, "")


def is_own_device(device) -> bool:
    """One of our own uinput devices; a VM's virtual keyboard is a real keyboard here."""
    return str(getattr(device, "name", "")).startswith(OWN_DEVICE_PREFIX)


def watches_home(keys: set[int], chord: str | None = None) -> bool:
    """watch_home() reads devices with Home/Guide, a Home shortcut pair, the keys of the keyboard
    Home chord, or brightness/volume keys."""
    chord = INPUT_SETTINGS.current().keyboard_home if chord is None else chord
    pairs = (pair for pair, _cancel in HOME_COMBOS.values())
    needed = chord_codes(chord)
    return bool({ecodes.KEY_HOME, ecodes.KEY_HOMEPAGE, ecodes.BTN_MODE, *MEDIA_KEYS} & keys) or any(
        pair <= keys for pair in pairs
    ) or (bool(needed) and needed <= keys)


def is_hold_press(event) -> bool:
    return event.type == ecodes.EV_KEY and event.value == 1 and event.code in HomeHold.CODES


def request_sleep() -> None:
    # Checked when it happens: the stick may have moved to a PC that cannot suspend, and a
    # game that has the controller must not be put to sleep under the player.
    if not app_owns_pad() and power.can_suspend():
        SLEEP_REQUEST.touch()


def request_home(guide: bool = False) -> None:
    """Ask the launcher for Home. A tap of Guide / PS, the Home key or a remote's Home key (`guide`)
    opens the TV interface's quick menu; the held shortcuts and the keyboard chord go straight Home."""
    try:
        HOME_REQUEST.write_text("guide\n" if guide else "shortcut\n", encoding="ascii")
        subprocess.run(
            ["wlrctl", "toplevel", "focus", "title:CouchLiteOS Launcher"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def watch_home() -> None:
    devices: dict[str, InputDevice] = {}
    hold = HomeHold()
    combo = HomeCombo()  # SELECT+START or L3+R3 held: watched, never grabbed, so it works over any app
    chord = HomeChord()  # the keyboard Home key: also watched, never grabbed
    media = MediaRepeat()
    in_game = False  # the hold under way began while an app had the controller

    def pressed(path: object) -> list[int] | None:
        # EVIOCGKEY answers even while another fd (the controller mouse) has the pad grabbed.
        device = devices.get(path) if isinstance(path, str) else None
        return pressed_keys(device) if device is not None else None

    while True:
        paths = set(glob.glob("/dev/input/event*"))
        for path in set(devices) - paths:
            gone = devices.pop(path)
            combo.forget(path)
            chord.forget(path)
            media.forget(path)
            gone.close()
            hold.reset()  # the button may have been released while disconnected
        for path in paths - set(devices):
            try:
                device = InputDevice(path)
                keys = set(device.capabilities().get(ecodes.EV_KEY, []))
                if watches_home(keys) and not (is_own_device(device) and not is_pad_or_media(keys)):
                    devices[path] = device
                else:
                    device.close()
            except OSError:
                pass
        try:
            now = time.monotonic()
            readable, _writable, _errors = select.select(
                list(devices.values()), [], [], min(hold.timeout(now), combo.timeout(now))
            )
            for device in readable:
                path = next((key for key, value in devices.items() if value is device), id(device))
                for event in device.read():
                    if is_hold_press(event):
                        # Looked at before request_home() brings the launcher forward: the Guide
                        # press itself takes the focus, so later the launcher always "has" it.
                        in_game = app_owns_pad()
                    # The keyboard Home key is not held back from a stream or remote desktop in
                    # front: it is the keyboard's way home from one (Guide is the pad's).
                    # A stream in front keeps the Guide / PS button (Steam Big Picture, the PS menu):
                    # Home is then the Select+Start hold or the keyboard shortcut.
                    chorded = chord.feed(path, event)
                    guide_goes_home = is_home_event(event) and not (is_guide_press(event) and stream_owns_pad())
                    if chorded:
                        request_home()
                    elif guide_goes_home:
                        request_home(guide=True)
                    # Deliberately also during a stream or remote desktop: the volume and
                    # brightness keys control this box (the TV's sound), not the remote PC.
                    paced = media.allow(path, event, time.monotonic())
                    action = media_action(event, device)
                    if action is not None and paced:
                        queue_media(action)
                    hold.feed(event, time.monotonic())
                    if event.type == ecodes.EV_KEY and event.code in HOME_COMBO_CODES:
                        combo.feed(path, event, time.monotonic(), pressed_keys(device))
            if combo.due(time.monotonic(), pressed):
                request_home()
            if hold.due(time.monotonic()) and not in_game:
                request_sleep()
        except OSError:
            hold.reset()
            combo.reset()
            chord.reset()
            media.reset()
            for device in devices.values():
                device.close()
            devices.clear()


def handle_cec_event(ui: UInput, event) -> None:
    """A TV remote key (HDMI-CEC) drives the launcher like the gamepad does."""
    if event.type != ecodes.EV_KEY or event.value != 1:
        return
    if event.code in CEC_HOME:
        request_home(guide=True)
    elif event.code in CEC_NAV:
        key = CEC_NAV[event.code]
        # The launcher's shortcut keys (colour keys, Clear) go to it whenever it has the focus, even with an
        # app running behind it: the same test as the gamepad's.
        if key in CEC_APP_KEYS or not navigation_blocked(OSK_ACTIVE.exists()):
            emit(ui, key)


def watch_cec(ui: UInput) -> None:
    devices: dict[str, InputDevice] = {}
    other: set[str] = set()
    while True:
        paths = set(glob.glob("/dev/input/event*"))
        other &= paths
        for path in set(devices) - paths:
            devices.pop(path).close()
        for path in paths - set(devices) - other:
            try:
                device = InputDevice(path)
            except OSError:
                continue
            if not cec.is_cec_bus(device.info.bustype):
                other.add(path)
                device.close()
                continue
            try:
                device.grab()  # the raw keys must not also reach the compositor
            except OSError:
                pass
            devices[path] = device
        try:
            readable, _writable, _errors = select.select(list(devices.values()), [], [], 1)
            for device in readable:
                for event in device.read():
                    handle_cec_event(ui, event)
        except OSError:
            for device in devices.values():
                device.close()
            devices.clear()


def run() -> None:
    threading.Thread(target=apply_media, daemon=True).start()
    threading.Thread(target=watch_home, daemon=True).start()
    ui = UInput({ecodes.EV_KEY: KEYS}, name="CouchLiteOS Launcher Navigation")
    pads = Pads()
    threading.Thread(target=watch_cec, args=(ui,), daemon=True).start()
    while True:
        pads.rescan()
        if not pads.devices:
            pads.sync_touchpads(False)  # no pad left to stream with: a leftover touchpad is a plain one
            time.sleep(2)
            continue
        pads.pump(ui)


if __name__ == "__main__":
    run()
