#!/usr/bin/python3
"""The PC's own battery (laptops, handhelds, some mini PCs) for the launcher.

UPower is asked over D-Bus (its DisplayDevice combines the system batteries); when it
does not answer, /sys/class/power_supply is read directly (type Battery, scope System;
a laptop battery usually has no scope file at all). A PC without a battery shows
nothing, so desktops look as before. The batteries of connected Bluetooth headphones,
headsets and speakers come from BlueZ's Battery1, through couchliteos-bluetoothd.
"""

from __future__ import annotations

import dataclasses
import pathlib
import threading
from collections.abc import Callable
from typing import Any

import couchliteos_controllers as controllers

NONE = "none"
CHARGING = "charging"
DISCHARGING = "discharging"
FULL = "full"
UNKNOWN = "unknown"

UPOWER = "org.freedesktop.UPower"
DISPLAY_DEVICE = "/org/freedesktop/UPower/devices/DisplayDevice"
UPOWER_DEVICE = "org.freedesktop.UPower.Device"
PROPERTIES = "org.freedesktop.DBus.Properties"
UPOWER_BATTERY = 2  # Type
# UPower State: 1 charging, 2 discharging, 3 empty, 4 fully charged, 5 pending charge
# (plugged in, not charging), 6 pending discharge.
UPOWER_STATES = {1: CHARGING, 2: DISCHARGING, 3: DISCHARGING, 4: FULL, 5: FULL, 6: DISCHARGING}
SYSFS_STATES = {"charging": CHARGING, "discharging": DISCHARGING, "full": FULL, "not charging": FULL}
THRESHOLDS = (20, 10, 5)  # percent; each warns once until the battery charges again
REFRESH_SECONDS = 30.0


@dataclasses.dataclass(frozen=True)
class Status:
    state: str = NONE
    percent: int | None = None
    seconds: int | None = None  # time left: to empty while discharging, to full while charging
    source: str = ""

    @property
    def present(self) -> bool:
        return self.state != NONE

    @property
    def on_battery(self) -> bool:
        return self.state == DISCHARGING


NO_BATTERY = Status()


def from_upower(properties: dict[str, Any]) -> Status:
    """A Status from the DisplayDevice's properties."""
    if not properties.get("IsPresent") or int(properties.get("Type") or 0) != UPOWER_BATTERY:
        return NO_BATTERY
    state = UPOWER_STATES.get(int(properties.get("State") or 0), UNKNOWN)
    try:
        percent = max(0, min(100, round(float(properties.get("Percentage")))))
    except (TypeError, ValueError):
        percent = None
    key = "TimeToEmpty" if state == DISCHARGING else "TimeToFull" if state == CHARGING else ""
    seconds = int(properties.get(key) or 0) if key else 0
    return Status(state, percent, seconds or None, "upower")


def read_upower(get_all: Callable[[], dict[str, Any]] | None = None) -> Status | None:
    """The battery as UPower sees it; None when UPower cannot be asked."""
    try:
        if get_all is None:
            import dbus  # python3-dbus; imported here so the launcher starts without it

            proxy = dbus.SystemBus().get_object(UPOWER, DISPLAY_DEVICE)
            interface = dbus.Interface(proxy, PROPERTIES)

            def get_all() -> dict[str, Any]:
                return interface.GetAll(UPOWER_DEVICE, timeout=2)

        return from_upower(dict(get_all()))
    except Exception:  # no bus, no UPower, or an odd answer: fall back to sysfs
        return None


def _number(path: pathlib.Path) -> int | None:
    text = controllers._read(path)
    return int(text) if text.isdigit() else None


def read_sysfs(root: pathlib.Path = controllers.POWER_SUPPLY) -> Status:
    """The system batteries in /sys/class/power_supply, combined."""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return NO_BATTERY
    percents: list[int] = []
    states: list[str] = []
    seconds: list[int] = []
    for entry in entries:
        if controllers._read(entry / "type") != "Battery":
            continue
        if controllers._read(entry / "scope") not in ("", "System"):
            continue  # a controller's or a mouse's battery
        if controllers._read(entry / "present") == "0":
            continue
        capacity = _number(entry / "capacity")
        if capacity is not None and capacity <= 100:
            percents.append(capacity)
        state = SYSFS_STATES.get(controllers._read(entry / "status").lower(), UNKNOWN)
        states.append(state)
        now = _number(entry / "energy_now") or _number(entry / "charge_now")
        full = _number(entry / "energy_full") or _number(entry / "charge_full")
        rate = _number(entry / "power_now") or _number(entry / "current_now")
        if rate and now is not None:
            if state == DISCHARGING:
                seconds.append(now * 3600 // rate)
            elif state == CHARGING and full:
                seconds.append(max(0, full - now) * 3600 // rate)
    if not states:
        return NO_BATTERY
    if CHARGING in states:
        state = CHARGING
    elif DISCHARGING in states:
        state = DISCHARGING
    elif all(item == FULL for item in states):
        state = FULL
    else:
        state = UNKNOWN
    percent = round(sum(percents) / len(percents)) if percents else None
    return Status(state, percent, max(seconds) if seconds and state in (CHARGING, DISCHARGING) else None, "sysfs")


def read_status(
    upower: Callable[[], Status | None] = read_upower,
    sysfs: Callable[[], Status] = read_sysfs,
) -> Status:
    status = upower()
    return status if status is not None else sysfs()


def bluetooth_batteries(devices: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """(NAME, percent) of each connected Bluetooth audio device that reports its battery."""
    import couchliteos_bluetooth as bluetooth

    found = []
    for device in devices:
        battery = device.get("battery")
        if not device.get("connected") or not isinstance(battery, int) or isinstance(battery, bool):
            continue
        if not bluetooth.is_audio(device) or not 0 <= battery <= 100:
            continue
        name = bluetooth.safe_text(device.get("alias") or "", controllers.MAX_NAME).strip().upper()
        found.append((name or bluetooth.kind_label(device) or "BLUETOOTH", battery))
    return found


def read_bluetooth() -> list[tuple[str, int]]:
    if not controllers.bluetooth_present():
        return []
    import couchliteos_bluetooth as bluetooth

    try:
        return bluetooth_batteries(list(bluetooth.BluetoothClient().snapshot().get("devices") or []))
    except Exception:
        return []


def duration(seconds: int) -> str:
    minutes = max(0, seconds) // 60
    return f"{minutes // 60}:{minutes % 60:02d}"


def status_text(status: Status) -> str:
    """BATTERY 54%  2:15 LEFT, BATTERY 80%  CHARGING, BATTERY FULL; "" without a battery."""
    if not status.present:
        return ""
    text = f"BATTERY {status.percent}%" if status.percent is not None else "BATTERY"
    if status.state == CHARGING:
        text += "  CHARGING"
        if status.seconds:
            text += f"  FULL IN {duration(status.seconds)}"
    elif status.state == FULL:
        text += "  PLUGGED IN" if status.percent is not None and status.percent < 95 else "  FULL"
    elif status.state == DISCHARGING and status.seconds:
        text += f"  {duration(status.seconds)} LEFT"
    elif status.state == UNKNOWN and status.percent is None:
        text += " UNKNOWN"
    return text


def format_line(status: Status, bluetooth: list[tuple[str, int]] | tuple = ()) -> str:
    parts = [status_text(status)] if status.present else []
    parts += [f"{name} {percent}%" for name, percent in bluetooth]
    return "  ·  ".join(part for part in parts if part)


def warning_text(level: int) -> str:
    if level <= 5:
        return f"BATTERY {level}%: PLUG IN THE CHARGER NOW OR THE PC WILL TURN OFF"
    return f"BATTERY LOW ({level}%): PLUG IN THE CHARGER"


class Warnings:
    """Each threshold (20%, 10%, 5%) warns once; charging starts them over."""

    def __init__(self, thresholds: tuple[int, ...] = THRESHOLDS) -> None:
        self.thresholds = thresholds
        self.warned: set[int] = set()

    def check(self, status: Status) -> int | None:
        if status.state in (CHARGING, FULL):
            self.warned.clear()
            return None
        if status.state != DISCHARGING or status.percent is None:
            return None
        crossed = [level for level in self.thresholds if status.percent <= level]
        fresh = [level for level in crossed if level not in self.warned]
        self.warned.update(crossed)
        return min(fresh) if fresh else None


class Monitor:
    """Keeps the latest battery reading, refreshed off the UI thread."""

    def __init__(
        self,
        reader: Callable[[], Status] = read_status,
        bluetooth: Callable[[], list[tuple[str, int]]] = read_bluetooth,
        interval: float = REFRESH_SECONDS,
    ) -> None:
        self.reader = reader
        self.bluetooth = bluetooth
        self.interval = interval
        self.status = NO_BATTERY
        self.devices: tuple[tuple[str, int], ...] = ()
        self.warnings = Warnings()
        self._warning: int | None = None
        self._source_changed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.awake = threading.Event()  # cleared while the TV interface rests: no polling then
        self.awake.set()

    def refresh(self) -> None:
        try:
            status = self.reader()
        except Exception:  # a failed read must never reach the launcher
            status = self.status
        try:
            devices = tuple(self.bluetooth())
        except Exception:
            devices = ()
        with self._lock:
            if status.on_battery != self.status.on_battery:
                self._source_changed = True
            self.status, self.devices = status, devices
            level = self.warnings.check(status)
            if level is not None:
                self._warning = level

    def line(self) -> str:
        return format_line(self.status, self.devices)

    def low(self) -> bool:
        return self.status.on_battery and self.status.percent is not None and self.status.percent <= THRESHOLDS[0]

    def on_battery(self) -> bool:
        return self.status.on_battery

    def take_warning(self) -> int | None:
        """The threshold just crossed, once; None otherwise."""
        with self._lock:
            level, self._warning = self._warning, None
        return level

    def take_source_change(self) -> bool:
        """True once after the PC went from mains to battery or back."""
        with self._lock:
            changed, self._source_changed = self._source_changed, False
        return changed

    def start(self) -> None:
        def loop() -> None:
            while True:
                self.awake.wait()
                self.refresh()
                if self._stop.wait(self.interval):
                    return

        threading.Thread(target=loop, name="pc-battery", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
