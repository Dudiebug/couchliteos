#!/usr/bin/python3
"""Translate gamepad navigation and expose the global controller keyboard chord."""

from __future__ import annotations

import glob
import pathlib
import select
import subprocess
import threading
import time

from evdev import InputDevice, UInput, ecodes

import couchliteos_power as power
import couchliteos_cec as cec

KEYS = [ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_LEFT, ecodes.KEY_RIGHT,
        ecodes.KEY_ENTER, ecodes.KEY_ESC, ecodes.KEY_DELETE, ecodes.KEY_F12,
        ecodes.KEY_F5, ecodes.KEY_F6, ecodes.KEY_F7, ecodes.KEY_F8,
        # controller mouse (pointer mode): browser back, play/pause, page up/down
        ecodes.KEY_BACK, ecodes.KEY_SPACE, ecodes.KEY_PAGEUP, ecodes.KEY_PAGEDOWN]
OSK_ACTIVE = pathlib.Path("/run/couchliteos/osk-active")
START_OSK = pathlib.Path("/run/couchliteos/start-osk")
HOME_REQUEST = pathlib.Path("/run/couchliteos/home.request")
APP_ACTIVE = pathlib.Path("/run/couchliteos/app-active")
# Written by the launcher for apps without controller support (browsers, web apps): the pad drives a mouse.
POINTER_MODE = pathlib.Path("/run/couchliteos/pointer-mode")
# Touched by the launcher while it holds focus (Home pressed) even though an app runs.
LAUNCHER_FOCUS = pathlib.Path("/run/couchliteos/launcher-focus")
CONTROLLER_ID = pathlib.Path("/var/lib/couchliteos/launcher-controller.id")
SLEEP_REQUEST = pathlib.Path("/run/couchliteos/suspend")
# Holding Guide is also how pads are switched off (8BitDo ~3 s, Xbox ~6 s, PlayStation ~10 s), so the
# hold is long, and a hold that began in a game never sleeps the box (see app_owns_pad).
SLEEP_HOLD_SECONDS = 5.0
CEC_NAV, CEC_HOME = cec.remote_key_maps(ecodes)
# Remote keys a running app gets too (the grabbed remote reaches nobody else); the rest are launcher shortcuts.
CEC_APP_KEYS = {ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_LEFT, ecodes.KEY_RIGHT, ecodes.KEY_ENTER, ecodes.KEY_ESC}
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
SUPER_KEYS = frozenset({ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA})


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


# Streams and remote desktops pass the Super key on to the remote PC (its Start menu).
REMOTE_SESSIONS = frozenset({"moonlight", "rdp"})


def remote_session_in_front() -> bool:
    """A stream or remote desktop has the keyboard, so a Super tap belongs to the remote PC."""
    if LAUNCHER_FOCUS.exists():
        return False
    try:
        return APP_ACTIVE.read_text(encoding="ascii").strip() in REMOTE_SESSIONS
    except (OSError, UnicodeDecodeError):
        return False


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
        self.clock = time.monotonic
        self.holds: dict[str, Hold] = {}
        self.sticks: dict[str, dict[int, list]] = {}  # path -> axis -> [-1.0..1.0, -1/0/+1 held]
        self.spans: dict[tuple[str, int], tuple[int, int] | None] = {}
        self.pointer = Pointer()
        self.pointer_on = False
        self.pointer_tick = 0.0
        self.carry = [0.0, 0.0, 0.0, 0.0]  # x, y, wheel, horizontal wheel not yet sent

    def rescan(self) -> None:
        """Follow hot-plug: add pads that appeared, forget those that went away."""
        paths = set(glob.glob("/dev/input/event*"))
        for path in set(self.devices) - paths:
            self.drop(path)
        self.ignored &= paths
        for path in sorted(paths - set(self.devices) - self.ignored):
            dev = None
            try:
                dev = InputDevice(path)
                if not is_gamepad(dev):
                    self.ignored.add(path)
                    dev.close()
                    continue
                if not self.devices:
                    save_identity(dev)
                self.devices[path] = dev
            except OSError:
                if dev is not None:
                    try:
                        dev.close()
                    except OSError:
                        pass

    def drop(self, path: str) -> None:
        self.holds.pop(path, None)
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
        key = key_for_event(event)
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
        if is_home_event(event):
            # The grab hides the pad from watch_home(): Guide must still bring up the menu.
            request_home()
        elif event.code in POINTER_BUTTONS and event.value in (0, 1):
            self.pointer.button(POINTER_BUTTONS[event.code], bool(event.value))
        elif event.value == 1 and event.code == ecodes.BTN_NORTH:
            open_keyboard()  # X / Triangle: type into the page
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
        rates = (
            stick_curve(self.stick_value(ecodes.ABS_X)) * POINTER_SPEED,
            stick_curve(self.stick_value(ecodes.ABS_Y)) * POINTER_SPEED,
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


def key_for_event(event) -> int | None:
    if event.type == ecodes.EV_KEY and event.value == 1:
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
        }.get(event.code)
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


class SuperTap:
    """The Super (Windows) key pressed and let go on its own opens Home, like Guide.

    Super held with another key (a shortcut) does not; tracked per keyboard.
    """

    def __init__(self) -> None:
        self.armed: set[object] = set()

    def feed(self, path: object, event) -> bool:
        """True when this event completes a lone Super tap; `path` names the keyboard."""
        if event.type != ecodes.EV_KEY:
            return False
        if event.code in SUPER_KEYS:
            if event.value == 1:
                self.armed.add(path)
            elif event.value == 2:
                self.armed.discard(path)  # held down: not a tap
            elif event.value == 0 and path in self.armed:
                self.armed.discard(path)
                return True
            return False
        if event.value == 1:
            self.armed.discard(path)
        return False

    def reset(self) -> None:
        self.armed.clear()


def is_hold_press(event) -> bool:
    return event.type == ecodes.EV_KEY and event.value == 1 and event.code in HomeHold.CODES


def request_sleep() -> None:
    # Checked when it happens: the stick may have moved to a PC that cannot suspend, and a
    # game that has the controller must not be put to sleep under the player.
    if not app_owns_pad() and power.can_suspend():
        SLEEP_REQUEST.touch()


def request_home() -> None:
    try:
        HOME_REQUEST.touch()
        subprocess.run(
            ["wlrctl", "toplevel", "focus", "title:CouchLiteOS Launcher"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def watch_home() -> None:
    devices: dict[str, InputDevice] = {}
    hold = HomeHold()
    tap = SuperTap()
    in_game = False  # the hold under way began while an app had the controller
    while True:
        paths = set(glob.glob("/dev/input/event*"))
        for path in set(devices) - paths:
            gone = devices.pop(path)
            tap.armed.discard(id(gone))
            gone.close()
            hold.reset()  # the button may have been released while disconnected
        for path in paths - set(devices):
            try:
                device = InputDevice(path)
                keys = set(device.capabilities().get(ecodes.EV_KEY, []))
                if {ecodes.KEY_HOME, ecodes.KEY_HOMEPAGE, ecodes.BTN_MODE, *SUPER_KEYS} & keys:
                    devices[path] = device
                else:
                    device.close()
            except OSError:
                pass
        try:
            readable, _writable, _errors = select.select(
                list(devices.values()), [], [], hold.timeout(time.monotonic())
            )
            for device in readable:
                for event in device.read():
                    if is_hold_press(event):
                        # Looked at before request_home() brings the launcher forward: the Guide
                        # press itself takes the focus, so later the launcher always "has" it.
                        in_game = app_owns_pad()
                    tapped = tap.feed(id(device), event) and not remote_session_in_front()
                    if tapped or is_home_event(event):
                        request_home()
                    hold.feed(event, time.monotonic())
            if hold.due(time.monotonic()) and not in_game:
                request_sleep()
        except OSError:
            hold.reset()
            tap.reset()
            for device in devices.values():
                device.close()
            devices.clear()


def handle_cec_event(ui: UInput, event) -> None:
    """A TV remote key (HDMI-CEC) drives the launcher like the gamepad does."""
    if event.type != ecodes.EV_KEY or event.value != 1:
        return
    if event.code in CEC_HOME:
        request_home()
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
    threading.Thread(target=watch_home, daemon=True).start()
    ui = UInput({ecodes.EV_KEY: KEYS}, name="CouchLiteOS Launcher Navigation")
    pads = Pads()
    threading.Thread(target=watch_cec, args=(ui,), daemon=True).start()
    while True:
        pads.rescan()
        if not pads.devices:
            time.sleep(2)
            continue
        pads.pump(ui)


if __name__ == "__main__":
    run()
