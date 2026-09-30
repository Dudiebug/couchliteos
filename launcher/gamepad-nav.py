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

KEYS = [ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_LEFT, ecodes.KEY_RIGHT,
        ecodes.KEY_ENTER, ecodes.KEY_ESC, ecodes.KEY_DELETE, ecodes.KEY_F12,
        ecodes.KEY_F5, ecodes.KEY_F6, ecodes.KEY_F7, ecodes.KEY_F8]
OSK_ACTIVE = pathlib.Path("/run/moonlightos/osk-active")
START_OSK = pathlib.Path("/run/moonlightos/start-osk")
HOME_REQUEST = pathlib.Path("/run/moonlightos/home.request")
APP_ACTIVE = pathlib.Path("/run/moonlightos/app-active")
# Touched by the launcher while it holds focus (Home pressed) even though an app runs.
LAUNCHER_FOCUS = pathlib.Path("/run/moonlightos/launcher-focus")
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


def find_gamepad() -> InputDevice | None:
    for path in glob.glob("/dev/input/event*"):
        try:
            dev = InputDevice(path)
            keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
            if ecodes.BTN_GAMEPAD in keys or ecodes.BTN_SOUTH in keys:
                serial = (dev.uniq or "*").lower()
                identity = f"{dev.info.vendor:04x}:{dev.info.product:04x}:{serial}\n"
                pathlib.Path("/var/lib/moonlightos/launcher-controller.id").write_text(identity)
                return dev
        except OSError:
            continue
    return None


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
        and event.code in {ecodes.KEY_HOME, ecodes.BTN_MODE}
    )


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
    while True:
        paths = set(glob.glob("/dev/input/event*"))
        for path in set(devices) - paths:
            devices.pop(path).close()
        for path in paths - set(devices):
            try:
                device = InputDevice(path)
                keys = set(device.capabilities().get(ecodes.EV_KEY, []))
                if ecodes.KEY_HOME in keys or ecodes.BTN_MODE in keys:
                    devices[path] = device
                else:
                    device.close()
            except OSError:
                pass
        try:
            readable, _writable, _errors = select.select(list(devices.values()), [], [], 1)
            for device in readable:
                for event in device.read():
                    if is_home_event(event):
                        request_home()
        except OSError:
            for device in devices.values():
                device.close()
            devices.clear()


def run() -> None:
    threading.Thread(target=watch_home, daemon=True).start()
    ui = UInput({ecodes.EV_KEY: KEYS}, name="MoonlightOS Launcher Navigation")
    while True:
        dev = find_gamepad()
        if dev is None:
            time.sleep(2)
            continue
        grabbed = False
        try:
            for event in dev.read_loop():
                active_osk = OSK_ACTIVE.exists()
                if active_osk != grabbed:
                    try:
                        dev.grab() if active_osk else dev.ungrab()
                        grabbed = active_osk
                    except OSError:
                        grabbed = False
                if navigation_blocked(active_osk):
                    continue
                key = key_for_event(event)
                if key:
                    emit(ui, key)
        except OSError:
            time.sleep(1)
        finally:
            if grabbed:
                try:
                    dev.ungrab()
                except OSError:
                    pass


if __name__ == "__main__":
    run()
