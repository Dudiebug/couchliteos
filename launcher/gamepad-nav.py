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
        ecodes.KEY_F5, ecodes.KEY_F6, ecodes.KEY_F7, ecodes.KEY_F8]
OSK_ACTIVE = pathlib.Path("/run/couchliteos/osk-active")
START_OSK = pathlib.Path("/run/couchliteos/start-osk")
HOME_REQUEST = pathlib.Path("/run/couchliteos/home.request")
APP_ACTIVE = pathlib.Path("/run/couchliteos/app-active")
# Touched by the launcher while it holds focus (Home pressed) even though an app runs.
LAUNCHER_FOCUS = pathlib.Path("/run/couchliteos/launcher-focus")
CONTROLLER_ID = pathlib.Path("/var/lib/couchliteos/launcher-controller.id")
SLEEP_REQUEST = pathlib.Path("/run/couchliteos/suspend")
SLEEP_HOLD_SECONDS = 3.0
CEC_NAV, CEC_HOME = cec.remote_key_maps(ecodes)
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


def app_active() -> bool:
    global _last_state_check, _last_state
    now = time.monotonic()
    if now - _last_state_check < 0.5:
        return _last_state
    _last_state_check = now
    _last_state = APP_ACTIVE.exists()
    return _last_state


def navigation_blocked(active_osk: bool) -> bool:
    """An app owns the controller, unless the launcher was brought back with Home."""
    return app_active() and not active_osk and not LAUNCHER_FOCUS.exists()


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

    def handle(self, path: str, dev: InputDevice, event, ui: UInput, now: float) -> bool:
        """Translate one event; True when a left-stick axis moved (judged once per batch)."""
        if event.type == ecodes.EV_ABS and event.code in STICK_AXES:
            span = self.axis_span(path, dev, event.code)
            if span is None:
                return False
            low, high = span
            value = (event.value - (low + high) / 2) / ((high - low) / 2)
            self.sticks.setdefault(path, {}).setdefault(event.code, [0.0, 0])[0] = value
            return True
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
        """How long select() may sleep: a second when idle, less when a repeat is due sooner."""
        return min([1.0, *(max(0.0, hold.due - now) for hold in self.holds.values() if hold.key is not None)])

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
        self.sync_grab(active_osk)
        blocked = navigation_blocked(active_osk)
        if blocked:
            self.holds.clear()  # an app owns the controller now; do not keep stepping its menus
        for path, dev in list(self.devices.items()):
            if dev not in readable:
                continue
            stick_moved = False
            try:
                for event in dev.read():
                    if not blocked:
                        stick_moved = self.handle(path, dev, event, ui, now) or stick_moved
            except BlockingIOError:
                pass
            except OSError:
                self.drop(path)
                continue
            if stick_moved:
                self.update_stick(path, ui, now)
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


def request_sleep() -> None:
    # Checked when it happens: the stick may have moved to a PC that cannot suspend.
    if power.can_suspend():
        SLEEP_REQUEST.touch()


def request_home() -> None:
    HOME_REQUEST.touch()
    try:
        subprocess.run(
            ["wlrctl", "toplevel", "focus", "title:CouchLiteOS Launcher"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def watch_home() -> None:
    devices: dict[str, InputDevice] = {}
    hold = HomeHold()
    while True:
        paths = set(glob.glob("/dev/input/event*"))
        for path in set(devices) - paths:
            devices.pop(path).close()
            hold.reset()  # the button may have been released while disconnected
        for path in paths - set(devices):
            try:
                device = InputDevice(path)
                keys = set(device.capabilities().get(ecodes.EV_KEY, []))
                if {ecodes.KEY_HOME, ecodes.KEY_HOMEPAGE, ecodes.BTN_MODE} & keys:
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
                    if is_home_event(event):
                        request_home()
                    hold.feed(event, time.monotonic())
            if hold.due(time.monotonic()):
                request_sleep()
        except OSError:
            hold.reset()
            for device in devices.values():
                device.close()
            devices.clear()


def handle_cec_event(ui: UInput, event) -> None:
    """A TV remote key (HDMI-CEC) drives the launcher like the gamepad does."""
    if event.type != ecodes.EV_KEY or event.value != 1:
        return
    if event.code in CEC_HOME:
        request_home()
    elif event.code in CEC_NAV and (not app_active() or OSK_ACTIVE.exists()):
        emit(ui, CEC_NAV[event.code])


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
