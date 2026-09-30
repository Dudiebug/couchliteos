#!/usr/bin/python3
"""HDMI-CEC TV control: cec-ctl output parsing, decisions, and settings I/O.

Everything here is pure logic or a thin subprocess wrapper so it can be tested
without hardware. The root daemon (moonlightos-cec), the launcher's TV CONTROL
screen, and gamepad-nav's TV remote support all import this module.
"""

from __future__ import annotations

import configparser
import dataclasses
import glob
import os
import pathlib
import re
import subprocess
import tempfile
import time
from typing import Callable

CONFIG = pathlib.Path("/var/lib/moonlightos/config.ini")
SECTION = "cec"
OSD_NAME = "MoonlightOS"
OP_STANDBY = 0x36
TV = 0
BROADCAST = 15
# Ignore a Standby this long after we switched the TV on or asked to suspend.
STANDBY_QUIET_SECONDS = 60.0
INVALID_PHYS_ADDR = "f.f.f.f"
# linux/input.h BUS_CEC: the bus of the input device the kernel's rc-cec keymap uses.
BUS_CEC = 0x1E
POWER_STATE = pathlib.Path("/sys/power/state")

Run = Callable[..., "str | None"]


def run_cec(argv: list[str], timeout: float = 10.0) -> str | None:
    """Run cec-ctl (no shell); its output, or None when it could not run or open the device."""
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.stdout or not result.returncode else None


def cec_ctl(device: str, *args: str) -> list[str]:
    return ["cec-ctl", "-d", device, *args]


# --- settings ---------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Settings:
    turn_tv_on: bool = True
    sleep_on_tv_off: bool = False


def _as_bool(value: str | None, default: bool) -> bool:
    text = (value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def load_settings(path: pathlib.Path = CONFIG) -> Settings:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, configparser.Error):
        return Settings()
    defaults = Settings()
    return Settings(
        _as_bool(parser.get(SECTION, "turn_tv_on", fallback=None), defaults.turn_tv_on),
        _as_bool(parser.get(SECTION, "sleep_on_tv_off", fallback=None), defaults.sleep_on_tv_off),
    )


def save_settings(settings: Settings, path: pathlib.Path = CONFIG) -> None:
    """Rewrite only the [cec] section of config.ini, atomically."""
    block = (
        f"[{SECTION}]\n"
        f"turn_tv_on = {str(settings.turn_tv_on).lower()}\n"
        f"sleep_on_tv_off = {str(settings.sleep_on_tv_off).lower()}\n"
    )
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        lines = []
    start = next((i for i, line in enumerate(lines) if line.strip().lower() == f"[{SECTION}]"), None)
    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines:
            lines.append("\n")
        lines.append(block)
    else:
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines)
        )
        lines[start:end] = [block + ("\n" if end < len(lines) else "")]
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".config.ini.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.writelines(lines)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


# --- adapters ---------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Adapter:
    device: str
    driver: str
    name: str
    caps: frozenset[str]
    phys_addr: str
    la_mask: int

    @property
    def configured(self) -> bool:
        return self.la_mask != 0 and self.phys_addr != INVALID_PHYS_ADDR

    @property
    def can_set_phys_addr(self) -> bool:
        """USB adapters (pulse8-cec) cannot read the EDID themselves; userspace sets it."""
        return "Physical Address" in self.caps


def parse_adapter(device: str, text: str) -> Adapter | None:
    """Parse the driver-info block `cec-ctl -d DEV` prints."""
    fields: dict[str, str] = {}
    caps: set[str] = set()
    in_caps = False
    for line in text.splitlines():
        if in_caps and line.startswith("\t\t"):
            caps.add(line.strip())
            continue
        in_caps = False
        match = re.match(r"\t(\S[^:]*?)\s*:\s*(.*)$", line)
        if match:
            fields.setdefault(match.group(1), match.group(2).strip())
            in_caps = match.group(1) == "Capabilities"
    if "Driver Name" not in fields or "Physical Address" not in fields:
        return None
    try:
        mask = int(fields.get("Logical Address Mask", "0x0"), 16)
    except ValueError:
        mask = 0
    return Adapter(
        device, fields["Driver Name"], fields.get("Adapter Name", ""), frozenset(caps),
        fields["Physical Address"], mask,
    )


def device_nodes(root: pathlib.Path | str = "/dev") -> list[str]:
    """The CEC device nodes (/dev/cec0, /dev/cec1, ...); empty on a PC without CEC hardware."""
    return sorted(glob.glob(os.path.join(str(root), "cec[0-9]*")))


def find_adapters(run: Run = run_cec, devices: list[str] | None = None) -> list[Adapter]:
    if devices is None:
        devices = device_nodes()
    found = []
    for device in devices:
        adapter = parse_adapter(device, run(cec_ctl(device), 5.0) or "")
        if adapter is not None:
            found.append(adapter)
    return found


def pick_adapter(adapters: list[Adapter]) -> Adapter | None:
    """Prefer an adapter that already knows where it is plugged in."""
    return next((a for a in adapters if a.phys_addr != INVALID_PHYS_ADDR), adapters[0] if adapters else None)


def edid_path(root: pathlib.Path | str = "/sys/class/drm") -> str | None:
    """EDID of a connected display, for adapters that need their physical address set."""
    for connector in sorted(pathlib.Path(root).glob("card*-*")):
        try:
            if (connector / "status").read_text().strip() != "connected":
                continue
            if (connector / "edid").read_bytes():
                return str(connector / "edid")
        except OSError:
            continue
    return None


def configure_adapter(adapter: Adapter, run: Run = run_cec, edid: str | None = None) -> Adapter | None:
    """Register as a Playback device called MoonlightOS; returns the adapter as it now is."""
    argv = cec_ctl(adapter.device, "--skip-info", "--playback", "--osd-name", OSD_NAME)
    if edid and adapter.can_set_phys_addr and adapter.phys_addr == INVALID_PHYS_ADDR:
        argv += ["--phys-addr-from-edid", edid]
    if run(argv, 10.0) is None:
        return None
    return parse_adapter(adapter.device, run(cec_ctl(adapter.device), 5.0) or "")


def bring_up(run: Run = run_cec, devices: list[str] | None = None, edid: str | None = None) -> Adapter | None:
    """A registered adapter that can reach a TV, or None (no hardware, no HDMI link, unreadable)."""
    adapter = pick_adapter(find_adapters(run, devices))
    if adapter is None:
        return None
    if adapter.phys_addr == INVALID_PHYS_ADDR and not (edid and adapter.can_set_phys_addr):
        return None  # no link to a TV and nothing we could set one from: leave the adapter alone
    adapter = configure_adapter(adapter, run, edid)
    return adapter if adapter is not None and adapter.configured else None


# --- messages ---------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Message:
    source: int
    dest: int
    opcode: int


_MESSAGE_RE = re.compile(r"^Received from .+? \((\d+) to (\d+)\): \S+ \(0x([0-9a-f]{2})\)")


def parse_message(line: str) -> Message | None:
    """A `cec-ctl --monitor` line for a message that reached us."""
    match = _MESSAGE_RE.match(line)
    return Message(int(match.group(1)), int(match.group(2)), int(match.group(3), 16)) if match else None


def tx_ok(output: str) -> bool:
    """cec-ctl exits 0 even when nobody acknowledged; the Tx status line tells."""
    return "\tSequence:" in output and not re.search(r"^\s*Tx, (?!OK)", output, re.M)


def parse_power(output: str) -> str:
    match = re.search(r"pwr-state:\s*(\S+)", output)
    return match.group(1) if match else ""


@dataclasses.dataclass(frozen=True)
class TvInfo:
    vendor: str
    osd_name: str
    power: str


def parse_tv_info(output: str) -> TvInfo:
    vendor = re.search(r"vendor-id:\s*(0x[0-9a-fA-F]+)(?:\s*\(([^)]*)\))?", output)
    name = re.search(r"^\s*name:\s*(.+?)\s*$", output, re.M)
    return TvInfo(
        (vendor.group(2) or vendor.group(1)) if vendor else "",
        name.group(1)[:32] if name else "",
        parse_power(output),
    )


def should_suspend(
    message: Message | None,
    settings: Settings,
    now: float,
    last_tv_on: float,
    last_suspend: float | None,
) -> bool:
    if message is None or not settings.sleep_on_tv_off:
        return False
    if message.source != TV or message.opcode != OP_STANDBY:
        return False
    if now - last_tv_on < STANDBY_QUIET_SECONDS:
        return False
    return last_suspend is None or now - last_suspend >= STANDBY_QUIET_SECONDS


def turn_tv_on(
    device: str,
    phys_addr: str,
    run: Run = run_cec,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda message: None,
    tries: int = 5,
) -> bool:
    """Image View On, wait for the TV to report on, then make this the active source."""
    for attempt in range(tries):
        output = run(cec_ctl(device, "--skip-info", "--to", str(TV), "--image-view-on"), 10.0)
        if output is not None and tx_ok(output):
            break
        log(f"TV did not acknowledge Image View On (attempt {attempt + 1} of {tries})")
        if attempt < tries - 1:
            sleep(2)
    else:
        return False
    # A TV that is still starting ignores Active Source, so wait for power state "on".
    for attempt in range(tries):
        power = run(cec_ctl(device, "--skip-info", "--to", str(TV), "--give-device-power-status"), 5.0)
        if parse_power(power or "") == "on":
            break
        if attempt < tries - 1:
            sleep(2)
    run(cec_ctl(device, "--skip-info", "--active-source", f"phys-addr={phys_addr}"), 10.0)
    return True


def watch(
    lines,
    load: Callable[[], Settings],
    now: Callable[[], float],
    suspend: Callable[[], None],
    log: Callable[[str], None],
    last_tv_on: float,
) -> None:
    """Suspend when the TV says Standby and the setting is on; `lines` is `cec-ctl --monitor` output."""
    last_suspend = None
    for line in lines:
        if should_suspend(parse_message(line), load(), now(), last_tv_on, last_suspend):
            last_suspend = now()
            log("TV went to standby; asking systemd to suspend")
            suspend()


# --- TV remote (kernel rc-cec input device) --------------------------------

# rc-cec key -> key the launcher understands (gamepad-nav's uinput keys).
_REMOTE_NAV = {
    "KEY_UP": "KEY_UP", "KEY_DOWN": "KEY_DOWN", "KEY_LEFT": "KEY_LEFT", "KEY_RIGHT": "KEY_RIGHT",
    "KEY_OK": "KEY_ENTER", "KEY_ENTER": "KEY_ENTER",
    "KEY_EXIT": "KEY_ESC", "KEY_BACK": "KEY_ESC",  # TVs send Exit for their Back button
    "KEY_CLEAR": "KEY_DELETE",
    "KEY_RED": "KEY_F5", "KEY_GREEN": "KEY_F6", "KEY_YELLOW": "KEY_F7", "KEY_BLUE": "KEY_F8",
}
_REMOTE_HOME = ("KEY_HOME", "KEY_ROOT_MENU", "KEY_MENU")


def remote_key_maps(ecodes) -> tuple[dict[int, int], set[int]]:
    """(remote key code -> launcher key code, remote key codes that mean Home)."""
    nav = {}
    for source, target in _REMOTE_NAV.items():
        code, key = getattr(ecodes, source, None), getattr(ecodes, target, None)
        if code is not None and key is not None:
            nav[code] = key
    return nav, {getattr(ecodes, name) for name in _REMOTE_HOME if hasattr(ecodes, name)}


def is_cec_bus(bustype: int) -> bool:
    return bustype == BUS_CEC


# --- launcher screen --------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Status:
    adapter: Adapter | None
    tv: TvInfo | None = None  # None: not asked

    @property
    def usable(self) -> bool:
        """An adapter that knows where it is plugged in; without it the TV cannot be reached."""
        return self.adapter is not None and self.adapter.phys_addr != INVALID_PHYS_ADDR


def query_status(run: Run = run_cec, devices: list[str] | None = None) -> Status:
    adapter = pick_adapter(find_adapters(run, devices))
    if adapter is None or not adapter.configured:
        return Status(adapter)
    query = cec_ctl(
        adapter.device, "--skip-info", "--to", str(TV),
        "--give-device-vendor-id", "--give-osd-name", "--give-device-power-status",
    )
    return Status(adapter, parse_tv_info(run(query, 8.0) or ""))


_POWER_LABELS = {"on": "ON", "standby": "STANDBY", "to-on": "TURNING ON", "to-standby": "TURNING OFF"}


def status_lines(status: Status) -> list[str]:
    adapter = status.adapter
    if adapter is None:
        return [
            "NO CEC ADAPTER FOUND",
            "NEEDS A PULSE-EIGHT USB-CEC ADAPTER OR A",
            "DISPLAYPORT-TO-HDMI ADAPTER WITH CEC TUNNELING",
        ]
    lines = [f"DEVICE  {adapter.device}  ({adapter.driver})"]
    if adapter.phys_addr == INVALID_PHYS_ADDR:
        return lines + ["TV  NOT CONNECTED (NO HDMI LINK)"]
    lines.append(f"PHYSICAL ADDRESS  {adapter.phys_addr}")
    if status.tv is None:
        return lines + ["TV  ADAPTER NOT READY"]
    if not (status.tv.vendor or status.tv.osd_name or status.tv.power):
        return lines + ["TV  NO ANSWER"]
    power = _POWER_LABELS.get(status.tv.power, "POWER UNKNOWN")
    return lines + [f"TV  {status.tv.vendor}  {status.tv.osd_name}  {power}".upper()]


def suspend_supported(state: pathlib.Path = POWER_STATE) -> bool:
    """Whether this PC can suspend to RAM (`systemctl suspend` needs "mem" in /sys/power/state)."""
    try:
        return "mem" in state.read_text().split()
    except OSError:
        return False


def effective_settings(settings: Settings, can_sleep: bool) -> Settings:
    """What the daemon acts on: a sleep switch saved on another PC means nothing here."""
    return settings if can_sleep else dataclasses.replace(settings, sleep_on_tv_off=False)


def toggle_rows(settings: Settings, usable: bool = True, can_sleep: bool = True) -> list[str]:
    def word(value: bool) -> str:
        return ("ON" if value else "OFF") if usable else "UNAVAILABLE"

    sleep = word(settings.sleep_on_tv_off) if can_sleep or not usable else "NOT SUPPORTED ON THIS PC"
    return [f"TURN TV ON AT START/WAKE  {word(settings.turn_tv_on)}", f"SLEEP WHEN TV TURNS OFF  {sleep}"]


def toggled(settings: Settings, index: int, usable: bool = True, can_sleep: bool = True) -> Settings:
    """The settings after flipping toggle `index`; unchanged when that switch is unavailable here."""
    if not usable or (index == 1 and not can_sleep):
        return settings
    if index == 0:
        return dataclasses.replace(settings, turn_tv_on=not settings.turn_tv_on)
    return dataclasses.replace(settings, sleep_on_tv_off=not settings.sleep_on_tv_off)
