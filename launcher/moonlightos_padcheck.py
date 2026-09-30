"""NO CONTROLLER FOUND banner for the launcher's home screen.

The only input is the kernel's device list, /proc/bus/input/devices. The pad nodes
themselves are never opened (gamepad-nav owns them). When the list cannot be read there
is no banner, so a failure here can never raise a false alarm.
"""

from __future__ import annotations

import pathlib
import time
from typing import Callable

import moonlightos_cec as cec

DEVICES = pathlib.Path("/proc/bus/input/devices")
BTN_SOUTH = 0x130  # the same button gamepad-nav's is_gamepad() looks for (BTN_GAMEPAD)
WORD_BITS = 64  # /proc prints the key bitmap as unpadded hex words of the kernel's long size
# Bluetooth pads reconnect a little after boot, so nothing is said before this.
GRACE_SECONDS = 15
REFRESH_SECONDS = 5
TEXT = "NO CONTROLLER FOUND. PLUG ONE IN BY USB, OR PAIR: SETTINGS > BLUETOOTH"
# uinput devices (gamepad-nav's keyboard, other programs' software pads) sit under
# /devices/virtual/input. BlueZ gives the kernel EVERY Bluetooth pad through uhid, under
# /devices/virtual/misc/uhid, so "virtual" alone would hide every real Bluetooth pad.
SOFTWARE_DEVICE_PATH = "/devices/virtual/input/"
OWN_DEVICE_PREFIX = "moonlightos"  # the launcher's own uinput keyboards (any case)


def has_btn_south(key_words: list[str]) -> bool:
    """Is bit BTN_SOUTH set in a "B: KEY=" bitmap? Words are hex, highest word first."""
    index = BTN_SOUTH // WORD_BITS
    if len(key_words) <= index:
        return False
    return bool(int(key_words[-1 - index], 16) >> (BTN_SOUTH % WORD_BITS) & 1)


def gamepads(proc_text: str) -> list[str]:
    """Names of the real gamepads in the text of /proc/bus/input/devices."""
    pads: list[str] = []
    for block in proc_text.split("\n\n"):
        name = sysfs = ""
        bus = -1
        keys: list[str] = []
        for line in block.splitlines():
            if line.startswith("I: "):
                for part in line[3:].split():
                    if part.startswith("Bus="):
                        try:
                            bus = int(part[4:], 16)
                        except ValueError:
                            pass
            elif line.startswith("N: Name="):
                name = line[8:].strip().strip('"')
            elif line.startswith("S: Sysfs="):
                sysfs = line[9:].strip()
            elif line.startswith("B: KEY="):
                keys = line[7:].split()
        try:
            pad = has_btn_south(keys)
        except ValueError:  # a garbled bitmap: not a pad, and no reason to stop
            pad = False
        if pad and not (
            sysfs.startswith(SOFTWARE_DEVICE_PATH) or cec.is_cec_bus(bus)
            or name.lower().startswith(OWN_DEVICE_PREFIX)
        ):
            pads.append(name)
    return pads


def read(path: pathlib.Path = DEVICES) -> list[str] | None:
    """Pad names from the kernel's list, or None when it cannot be read."""
    try:
        return gamepads(path.read_text(errors="replace"))
    except OSError:
        return None


def banner(pads: list[str] | None, seconds_since_start: float) -> str:
    """The warning line, or "" while in the grace period, with a pad, or with no reading."""
    if pads is None or pads or seconds_since_start < GRACE_SECONDS:
        return ""
    return TEXT


class Monitor:
    """The banner for the launcher; the file is read at most every REFRESH_SECONDS."""

    def __init__(
        self, read: Callable[[], list[str] | None] = read, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._read = read
        self._clock = clock
        self.started = clock()
        self._checked = float("-inf")
        self._text = ""

    def line(self) -> str:
        now = self._clock()
        if now - self._checked >= REFRESH_SECONDS:
            self._checked = now
            try:
                self._text = banner(self._read(), now - self.started)
            except Exception:  # never let this reach the home screen
                self._text = ""
        return self._text

    def footer(self, attr: int) -> list[tuple[str, int]]:
        """The banner as a footer line (text, curses attribute), or no lines."""
        text = self.line()
        return [(text, attr)] if text else []
