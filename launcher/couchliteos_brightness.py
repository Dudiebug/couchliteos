"""Screen brightness through /sys/class/backlight (laptops, all-in-ones, built-in panels).

Standard library only: the launcher (the Guide menu's BRIGHTNESS row) and gamepad-nav (a
keyboard's brightness keys) both use it. 70-couchliteos-backlight.rules gives group video write
access to the brightness file, so no root helper is needed; systemd-backlight restores the level
after a reboot. External monitors (DDC/CI) are not handled here.
"""

from __future__ import annotations

import errno
import math
import os
import pathlib

BACKLIGHT = pathlib.Path("/sys/class/backlight")
# Y when the kernel's ACPI video driver moves its own backlight on the laptop's brightness keys.
VIDEO_SWITCH = pathlib.Path("/sys/module/video/parameters/brightness_switch_enabled")
STEP = 5  # percent
FLOOR = 5  # percent: a step down never turns the screen fully dark
# systemd's order: firmware (ACPI) interfaces, then platform drivers, then raw GPU registers.
TYPE_ORDER = ("firmware", "platform", "raw")
# Percent is perceived brightness: the raw level grows with its square, so the dark end gets
# finer steps and the bright end, where the eye barely sees a difference, coarser ones.
GAMMA = 2.0


def _read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _read_int(path: pathlib.Path) -> int | None:
    try:
        return int(_read_text(path))
    except ValueError:
        return None


def _rank(device: pathlib.Path) -> tuple | None:
    """Sort key for a backlight (lowest wins), or None when it cannot be used."""
    maximum = _read_int(device / "max_brightness")
    kind = _read_text(device / "type")
    if not maximum or maximum < 1 or kind not in TYPE_ORDER:
        return None
    # A raw backlight usually hangs off its DRM connector: prefer the one on the connected panel.
    connected = _read_text(device / "device" / "status") == "connected"
    return TYPE_ORDER.index(kind), not connected, -maximum, device.name


def find_device(root: pathlib.Path = BACKLIGHT) -> pathlib.Path | None:
    """The backlight to drive: firmware, then platform, then raw; then the connected panel's,
    then the finest (highest max_brightness). None when there is none."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    ranked = [(rank, device) for device in entries if (rank := _rank(device)) is not None]
    return min(ranked)[1] if ranked else None


def to_percent(level: int, maximum: int) -> int:
    return round(100 * math.pow(max(0, min(level, maximum)) / maximum, 1 / GAMMA))


def to_level(percent: int, maximum: int) -> int:
    """The raw level for `percent`; never 0, so the floor stays lit even on coarse panels."""
    return max(1, round(maximum * math.pow(percent / 100, GAMMA)))


def get_percent(root: pathlib.Path = BACKLIGHT) -> int | None:
    """The screen's brightness in percent; None when there is no backlight this user can change."""
    device = find_device(root)
    if device is None or not os.access(device / "brightness", os.W_OK):
        return None
    level = _read_int(device / "brightness")
    maximum = _read_int(device / "max_brightness")
    if level is None or not maximum:
        return None
    return to_percent(level, maximum)


def change(step: int, root: pathlib.Path = BACKLIGHT) -> int:
    """Move the brightness `step` percent along the curve, onto the STEP grid and never below
    FLOOR; returns the new percent. OSError when there is no backlight or it cannot be written."""
    device = find_device(root)
    if device is None:
        raise OSError(errno.ENODEV, "no adjustable backlight")
    maximum = _read_int(device / "max_brightness")
    level = _read_int(device / "brightness")
    if level is None or not maximum:
        raise OSError(errno.EIO, f"cannot read {device.name} brightness")
    target = STEP * round(to_percent(level, maximum) / STEP)
    new = level
    # A panel with few levels can map neighbouring steps to the same one: keep going until the
    # screen really changes, so a press is never lost.
    for _ in range(100 // STEP + 1):
        target = min(100, max(FLOOR, target + step))
        new = to_level(target, maximum)
        if new != level or target in (FLOOR, 100):
            break
    if new != level:
        (device / "brightness").write_text(str(new), encoding="ascii")
    return to_percent(new, maximum)


def kernel_handles_hotkeys(root: pathlib.Path = BACKLIGHT, switch: pathlib.Path = VIDEO_SWITCH) -> bool:
    """True when the ACPI video driver already moves the chosen backlight on the laptop's own
    brightness keys (they arrive from its "Video Bus" device): acting on them too doubles the step."""
    device = find_device(root)
    return device is not None and device.name.startswith("acpi_video") and _read_text(switch) == "Y"
