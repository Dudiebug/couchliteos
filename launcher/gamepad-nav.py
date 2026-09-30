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

import moonlightos_power as power

KEYS = [ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_LEFT, ecodes.KEY_RIGHT,
        ecodes.KEY_ENTER, ecodes.KEY_ESC, ecodes.KEY_DELETE, ecodes.KEY_F12,
        ecodes.KEY_F5, ecodes.KEY_F6, ecodes.KEY_F7, ecodes.KEY_F8]
OSK_ACTIVE = pathlib.Path("/run/moonlightos/osk-active")
START_OSK = pathlib.Path("/run/moonlightos/start-osk")
HOME_REQUEST = pathlib.Path("/run/moonlightos/home.request")
APP_ACTIVE = pathlib.Path("/run/moonlightos/app-active")
# Touched by the launcher while it holds focus (Home pressed) even though an app runs.
LAUNCHER_FOCUS = pathlib.Path("/run/moonlightos/launcher-focus")
CONTROLLER_ID = pathlib.Path("/var/lib/moonlightos/launcher-controller.id")
SLEEP_REQUEST = pathlib.Path("/run/moonlightos/suspend")
SLEEP_HOLD_SECONDS = 3.0
_last_state_check = 0.0
_last_state = False


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


class Pads:
    """Every gamepad driving the launcher, keyed by /dev/input path."""

    def __init__(self) -> None:
        self.devices: dict[str, InputDevice] = {}
        self.grabbed: set[str] = set()
        self.ignored: set[str] = set()  # nodes already seen to be something else

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

    def pump(self, ui: UInput) -> None:
        """Wait up to a second for input from any pad and translate it."""
        try:
            readable, _writable, _errors = select.select(list(self.devices.values()), [], [], 1)
        except (OSError, ValueError):  # a pad vanished under select
            self.drop_all()
            time.sleep(1)
            return
        active_osk = OSK_ACTIVE.exists()
        self.sync_grab(active_osk)
        for path, dev in list(self.devices.items()):
            if dev not in readable:
                continue
            try:
                for event in dev.read():
                    if navigation_blocked(active_osk):
                        continue
                    key = key_for_event(event)
                    if key:
                        emit(ui, key)
            except BlockingIOError:
                pass
            except OSError:
                self.drop(path)


def emit(ui: UInput, key: int) -> None:
    ui.write(ecodes.EV_KEY, key, 1)
    ui.write(ecodes.EV_KEY, key, 0)
    ui.syn()


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
            ["wlrctl", "toplevel", "focus", "title:MoonlightOS Launcher"],
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


def run() -> None:
    threading.Thread(target=watch_home, daemon=True).start()
    ui = UInput({ecodes.EV_KEY: KEYS}, name="MoonlightOS Launcher Navigation")
    pads = Pads()
    while True:
        pads.rescan()
        if not pads.devices:
            time.sleep(2)
            continue
        pads.pump(ui)


if __name__ == "__main__":
    run()
