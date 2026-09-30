#!/usr/bin/python3
"""Battery levels of connected game controllers for the launcher home screen.

The kernel drivers for PlayStation, Xbox, Switch and Steam controllers
(hid-sony, hid-playstation, hid-microsoft, xpadneo, hid-nintendo, hid-steam)
publish a power_supply device with type Battery and scope Device. Bluetooth
controllers that only expose BlueZ's Battery1 interface are read through
`bluetoothctl info`, but only for devices the kernel did not already report.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import subprocess
import threading
from collections.abc import Callable

POWER_SUPPLY = pathlib.Path("/sys/class/power_supply")
BLUETOOTH_ADAPTERS = pathlib.Path("/sys/class/bluetooth")
LOW_PERCENT = 15
REFRESH_SECONDS = 30.0
BLUETOOTHCTL_TIMEOUT = 3
MAX_NAME = 20
# Keyboards, mice and headsets also publish Device-scope batteries.
NOT_A_CONTROLLER = re.compile(
    r"keyboard|mouse|trackpad|touchpad|trackball|headset|headphone|earbud|stylus|\bpen\b", re.I
)
NAME_NOISE = re.compile(
    r"\b(wireless|controller|gamepad|bluetooth|sony|interactive|entertainment|microsoft|nintendo|corp\.?|inc\.?|co\.?|ltd\.?)\b",
    re.I,
)
# Used when the driver's model name says nothing ("Wireless Controller").
NAME_HINTS = (
    ("ps-controller", "PLAYSTATION"), ("sony", "PLAYSTATION"), ("nintendo", "SWITCH"),
    ("steam", "STEAM"), ("xpadneo", "XBOX"), ("xbox", "XBOX"), ("microsoft", "XBOX"),
)
LEVEL_WORDS = {"CRITICAL", "LOW", "NORMAL", "HIGH", "FULL"}
MAC_RE = re.compile(r"(?:[0-9a-f]{2}[:_-]){5}[0-9a-f]{2}", re.I)


@dataclasses.dataclass(frozen=True)
class Battery:
    name: str
    percent: int | None = None
    level: str = ""
    charging: bool = False
    source: str = "sysfs"
    ident: str = ""

    @property
    def low(self) -> bool:
        if self.charging:
            return False
        if self.percent is not None:
            return self.percent <= LOW_PERCENT
        return self.level in {"CRITICAL", "LOW"}


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def short_name(model: str, directory: str = "") -> str:
    name = " ".join(NAME_NOISE.sub(" ", "".join(c for c in model if c.isprintable())).split())
    if not name:
        lowered = directory.lower()
        name = next((label for hint, label in NAME_HINTS if hint in lowered), "CONTROLLER")
    return name.upper()[:MAX_NAME].strip()


def _numbered(items: list[Battery]) -> list[Battery]:
    seen: dict[str, int] = {}
    result = []
    for item in items:
        seen[item.name] = seen.get(item.name, 0) + 1
        if seen[item.name] > 1:
            item = dataclasses.replace(item, name=f"{item.name} {seen[item.name]}")
        result.append(item)
    return result


def read_sysfs(root: pathlib.Path = POWER_SUPPLY) -> list[Battery]:
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        if _read(entry / "type") != "Battery" or _read(entry / "scope") != "Device":
            continue
        model = _read(entry / "model_name")
        if NOT_A_CONTROLLER.search(f"{model} {entry.name}"):
            continue
        capacity = _read(entry / "capacity")
        percent = int(capacity) if capacity.isdigit() and int(capacity) <= 100 else None
        level = _read(entry / "capacity_level").upper()
        status = _read(entry / "status").lower()
        if percent == 0 and (level == "UNKNOWN" or status == "unknown"):
            continue  # drivers publish 0 before the first real report arrives
        level = level if level in LEVEL_WORDS and percent is None else ""
        if percent is None and not level:
            continue
        found.append(Battery(
            short_name(model, entry.name), percent, level,
            status == "charging", "sysfs", entry.name.lower(),
        ))
    return _numbered(found)


def parse_bluez_info(text: str) -> Battery | None:
    fields = {}
    for line in text.splitlines()[1:]:
        key, _sep, value = line.strip().partition(":")
        fields[key.strip()] = value.strip()
    if fields.get("Connected") != "yes" or not re.search(r"gaming|joystick", fields.get("Icon", "")):
        return None
    match = re.search(r"\((\d{1,3})\)", fields.get("Battery Percentage", ""))
    if not match or int(match.group(1)) > 100:
        return None
    return Battery(
        short_name(fields.get("Name", "")), int(match.group(1)), source="bluez",
    )


def _macs(value: str) -> set[str]:
    """MAC addresses in a sysfs name, in either byte order."""
    macs: set[str] = set()
    for found in MAC_RE.findall(value):
        parts = re.split(r"[:_-]", found.lower())
        macs.add(":".join(parts))
        macs.add(":".join(reversed(parts)))
    return macs


def read_bluez(
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run, skip: set[str] | None = None
) -> list[Battery]:
    known: set[str] = set()
    for name in skip or ():
        known |= _macs(name)
    try:
        listing = run(
            ["bluetoothctl", "devices", "Connected"], text=True, capture_output=True,
            check=False, timeout=BLUETOOTHCTL_TIMEOUT,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] != "Device" or not MAC_RE.fullmatch(parts[1]):
            continue
        if parts[1].lower() in known:
            continue
        try:
            info = run(
                ["bluetoothctl", "info", parts[1]], text=True, capture_output=True,
                check=False, timeout=BLUETOOTHCTL_TIMEOUT,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        item = parse_bluez_info(info)
        if item:
            found.append(item)
    return found


def bluetooth_present(root: pathlib.Path = BLUETOOTH_ADAPTERS) -> bool:
    """True when this PC has a Bluetooth adapter (hci0, hci1, ...; not connection handles)."""
    try:
        return any(re.fullmatch(r"hci\d+", entry.name) for entry in root.iterdir())
    except OSError:
        return False


def read_all(
    root: pathlib.Path = POWER_SUPPLY,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    bluetooth_root: pathlib.Path = BLUETOOTH_ADAPTERS,
) -> list[Battery]:
    items = read_sysfs(root)
    if not bluetooth_present(bluetooth_root):
        return items  # no adapter on this PC: do not start bluetoothctl every 30 seconds
    return _numbered(items + read_bluez(run, {item.ident for item in items}))


def format_line(items: list[Battery] | tuple[Battery, ...]) -> str:
    """One compact, plain-ASCII footer line, for example `CONTROLLERS: XBOX 35% LOW`."""
    parts = []
    for item in items:
        text = f"{item.name} {item.percent}%" if item.percent is not None else f"{item.name} {item.level}"
        if item.charging:
            text += " CHARGING"
        elif item.low and item.percent is not None:
            text += " LOW"  # a level word (LOW, CRITICAL) already says it
        parts.append(text)
    return "CONTROLLERS: " + "  ".join(parts) if parts else ""


class Monitor:
    """Keeps the latest battery readings, refreshed off the UI thread."""

    def __init__(
        self, reader: Callable[[], list[Battery]] = read_all, interval: float = REFRESH_SECONDS
    ) -> None:
        self.reader = reader
        self.interval = interval
        self._items: tuple[Battery, ...] = ()
        self._stop = threading.Event()

    def refresh(self) -> None:
        try:
            self._items = tuple(self.reader())
        except Exception:  # a failed read must never reach the launcher
            pass

    def line(self) -> str:
        return format_line(self._items)

    def low(self) -> list[Battery]:
        return [item for item in self._items if item.low]

    def start(self) -> None:
        def loop() -> None:
            while True:
                self.refresh()
                if self._stop.wait(self.interval):
                    return

        threading.Thread(target=loop, name="controller-batteries", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
