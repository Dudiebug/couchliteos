#!/usr/bin/python3
"""HDMI-CEC TV control: cec-ctl output parsing, decisions, and settings I/O.

Everything here is pure logic or a thin subprocess wrapper so it can be tested
without hardware. The root daemon (couchliteos-cec), the launcher's TV CONTROL
screen, and gamepad-nav's TV remote support all import this module.
"""

from __future__ import annotations

import configparser
import dataclasses
import glob
import math
import os
import pathlib
import re
import subprocess
import tempfile
import time
from typing import Callable

DATA = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos"))
CONFIG = DATA / "config.ini"
SECTION = "cec"
OSD_NAME = "CouchLiteOS"
OP_STANDBY = 0x36
OP_ROUTING_CHANGE = 0x80
OP_ROUTING_INFORMATION = 0x81
OP_ACTIVE_SOURCE = 0x82
OP_SET_STREAM_PATH = 0x86
TV = 0
BROADCAST = 15
# Ignore a Standby this long after we switched the TV on or asked to suspend.
STANDBY_QUIET_SECONDS = 60.0
# The pre-sleep Standby may hold up suspend by about this much (the unit's own timeout is a bit more).
SLEEP_HOOK_SECONDS = 2.0
INVALID_PHYS_ADDR = "f.f.f.f"
# linux/input.h BUS_CEC: the bus of the input device the kernel's rc-cec keymap uses.
BUS_CEC = 0x1E
POWER_STATE = pathlib.Path("/sys/power/state")
# Exists while the TV is showing this box (we are the active source). The root daemon keeps it up to
# date as it follows the bus and the pre-sleep hook reads it: no file means "another input, or not known".
RUN = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos"))
ACTIVE_SOURCE_MARKER = RUN / "cec-active-source"
LOGIND_CAN_SUSPEND = (
    "busctl", "--system", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
    "org.freedesktop.login1.Manager", "CanSuspend",
)

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
    tv_off_on_sleep: bool = True  # Standby to the TV as the PC goes to sleep, if it is showing this box


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
        _as_bool(parser.get(SECTION, "tv_off_on_sleep", fallback=None), defaults.tv_off_on_sleep),
    )


def save_settings(settings: Settings, path: pathlib.Path = CONFIG) -> None:
    """Rewrite only the [cec] section of config.ini, atomically."""
    block = (
        f"[{SECTION}]\n"
        f"turn_tv_on = {str(settings.turn_tv_on).lower()}\n"
        f"sleep_on_tv_off = {str(settings.sleep_on_tv_off).lower()}\n"
        f"tv_off_on_sleep = {str(settings.tv_off_on_sleep).lower()}\n"
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
    """Register as a Playback device called CouchLiteOS; returns the adapter as it now is."""
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


# --- which input the TV shows -----------------------------------------------

# cec-ctl prints a message's opcode on one line and its parameters on the lines after, indented with a tab:
#   Received from Playback Device 2 to all (8 to 15): ACTIVE_SOURCE (0x82):
#   \tphys-addr: 2.0.0.0
# ("Transmitted by ..." for what this adapter sent). Events are single lines:
#   Event: State Change: PA: 1.1.0.0, LA mask: 0x0010      ("Initial Event: ..." when the monitor starts)
_HEADER_RE = re.compile(r"^(?:Received from|Transmitted by) .+? \(\d+ to \d+\):\s*\S+ \(0x([0-9a-fA-F]{2})\)")
_PARAMETER_RE = re.compile(r"^\s*((?:orig-|new-)?phys-addr):\s*(\S+)")
_STATE_CHANGE_RE = re.compile(r"Event: State Change: PA: ([0-9a-fA-F.]+),")
_ADDRESS_RE = re.compile(r"[0-9a-f](?:\.[0-9a-f]){3}")
_SOURCE_OPCODES = frozenset({OP_ROUTING_CHANGE, OP_ROUTING_INFORMATION, OP_ACTIVE_SOURCE, OP_SET_STREAM_PATH})


def _address(value: object) -> str | None:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if _ADDRESS_RE.fullmatch(text) else None


def _header_opcode(line: str) -> int | None:
    match = _HEADER_RE.match(line)
    return int(match.group(1), 16) if match else None


def parse_source(text: str, our_physical_address: str) -> bool | None:
    """Is the TV showing this box? `text` is one `cec-ctl --monitor` message: its first line and the
    parameter lines after it.

    True: Active Source, Routing Information, Set Stream Path or Routing Change names our physical address.
    False: they name another one (another input is now showing). None: anything else, a first line without
    its address yet, or no usable address of ours. Never raises."""
    ours = _address(our_physical_address)
    if ours is None or ours == INVALID_PHYS_ADDR or not isinstance(text, str):
        return None
    opcode = None
    shown: dict[str, str | None] = {}
    for line in text.splitlines():
        header = _HEADER_RE.match(line)
        if header:
            opcode, shown = int(header.group(1), 16), {}
        elif opcode is not None and (parameter := _PARAMETER_RE.match(line)):
            shown.setdefault(parameter.group(1).lower(), _address(parameter.group(2)))
    if opcode not in _SOURCE_OPCODES:
        return None
    address = shown.get("new-phys-addr" if opcode == OP_ROUTING_CHANGE else "phys-addr")
    return None if address is None else address == ours


class SourceTracker:
    """Feeds `cec-ctl --monitor` lines to parse_source, which needs a message's lines together.

    feed() answers as soon as a message's address line arrives: True (the TV shows this box), False (it
    shows another input, or this box can no longer be known to be shown: the adapter's state changed) or
    None (nothing to say). A line that begins a new message or event ends the one before it."""

    def __init__(self, phys_addr: str) -> None:
        self.phys_addr = phys_addr
        self.block: list[str] = []

    def feed(self, line: str) -> bool | None:
        text = line.rstrip("\r\n")
        if not text.strip():
            return None
        if text[0].isspace():
            if not self.block:
                return None
            self.block.append(text)
        else:
            self.block = [text] if _header_opcode(text) in _SOURCE_OPCODES else []
            changed = _STATE_CHANGE_RE.search(text)
            if changed:
                self.phys_addr = _address(changed.group(1)) or INVALID_PHYS_ADDR
                # The monitor's first line only says where we are; a change later means the TV or the
                # cable came or went, and what it shows is unknown.
                return None if "Initial" in text else False
            if not self.block:
                return None
        result = parse_source("\n".join(self.block), self.phys_addr)
        if result is not None:
            self.block = []
        return result


def mark_active_source(
    active: bool, marker: pathlib.Path = ACTIVE_SOURCE_MARKER, log: Callable[[str], None] = lambda message: None
) -> bool:
    """Create (the TV shows this box) or remove (it does not, or unknown) the marker; False when that failed.

    Best effort: without the marker the pre-sleep hook leaves the TV alone."""
    try:
        if active:
            # The daemon is root and the directory belongs to the launcher's user: never follow a link there.
            os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644))
        else:
            marker.unlink(missing_ok=True)
    except OSError as error:
        log(f"could not {'create' if active else 'remove'} {marker}: {error}")
        return False
    return True


def should_standby_on_sleep(settings: Settings, marker: pathlib.Path = ACTIVE_SOURCE_MARKER) -> bool:
    """Standby for the TV as the PC sleeps: the setting is on and the TV is showing this box.

    A TV on another input (or a state not known) is left alone: someone may be watching it."""
    return settings.tv_off_on_sleep and os.path.exists(marker)


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


def standby_tv(
    devices: list[str], run: Run = run_cec, budget: float = SLEEP_HOOK_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Standby to the TV as the PC goes to sleep: one try per adapter, `budget` seconds in all."""
    deadline = clock() + budget
    for device in devices:
        left = deadline - clock()
        if left <= 0:
            break
        output = run(cec_ctl(device, "--skip-info", "--to", str(TV), "--standby"), left)
        if output is not None and tx_ok(output):
            return True
    return False


def watch(
    lines,
    load: Callable[[], Settings],
    now: Callable[[], float],
    suspend: Callable[[], None],
    log: Callable[[str], None],
    last_tv_on: float,
    phys_addr: str | None = None,
    marker: pathlib.Path = ACTIVE_SOURCE_MARKER,
) -> None:
    """Suspend when the TV says Standby and the setting is on; `lines` is `cec-ctl --monitor` output.

    Given this box's physical address it also keeps `marker` in step with whether the TV shows this box:
    created when we become the active source, removed when another input does, or when the TV goes to
    standby or the adapter's state changes (what it shows is then unknown)."""
    last_suspend = None
    tracker = SourceTracker(phys_addr) if phys_addr else None
    marked = None  # what the marker was last set to; None until the first answer

    def mark(active: bool) -> None:
        nonlocal marked
        if active != marked and mark_active_source(active, marker, log):
            marked = active

    for line in lines:
        if tracker is not None:
            seen = tracker.feed(line)
            if seen is not None:
                mark(seen)
        message = parse_message(line)
        # Reading the settings asks logind whether the PC can suspend, so only do it for a Standby.
        if message is None or message.opcode != OP_STANDBY:
            continue
        if tracker is not None and message.source == TV:
            mark(False)
        if should_suspend(message, load(), now(), last_tv_on, last_suspend):
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


# --- TEST REMOTE BUTTONS ----------------------------------------------------

REMOTE_TEST_ROW = "TEST REMOTE BUTTONS"
REMOTE_TEST_SECONDS = 10.0  # the screen closes after this long without a button
REMOTE_TEST_BACKS = 2  # ... or when BACK is pressed this many times in a row (BACK is under test too)
HOME_KEY = "HOME"  # stands for _REMOTE_HOME: gamepad-nav turns those into a request file, not a key
# Launcher keys that several remote keys end up as are named by the one a person presses most.
_KEY_TITLES = {"KEY_ENTER": "OK", "KEY_ESC": "BACK"}
BACK_KEY = "KEY_ESC"


def _reverse_map() -> dict[str, str]:
    """Launcher key (a _REMOTE_NAV value) -> what to call the remote key that sent it: UP, OK, RED -> F5."""
    labels = {}
    for target in dict.fromkeys(_REMOTE_NAV.values()):
        name = target.removeprefix("KEY_")
        first = next(source for source, value in _REMOTE_NAV.items() if value == target).removeprefix("KEY_")
        labels[target] = _KEY_TITLES.get(target) or (first if first == name else f"{first} → {name}")
    if _REMOTE_HOME:
        labels[HOME_KEY] = "HOME"
    return labels


REMOTE_LABELS = _reverse_map()


def describe_key(launcher_key: str | None, code: int) -> str:
    """The name of a key that arrived; `launcher_key` is its KEY_* name (HOME_KEY for Home), None if we have none."""
    return REMOTE_LABELS.get(launcher_key or "") or f"UNKNOWN KEY {code}"


class RemoteTest:
    """TEST REMOTE BUTTONS: the keys seen so far, and when the screen is over.

    The launcher only sees what gamepad-nav forwards, so this names the keys the TV got through to the
    launcher, as the launcher key they became. It ends after `seconds` with no key, or `backs` BACKs in a row."""

    HISTORY = 4  # the last one and three before it fit an 80-column screen even with unknown codes

    def __init__(self, now: float, seconds: float = REMOTE_TEST_SECONDS, backs: int = REMOTE_TEST_BACKS) -> None:
        self.seconds = seconds
        self.backs = backs
        self.last_key = now
        self.back_run = 0
        self.seen: list[str] = []

    def press(self, launcher_key: str | None, code: int, now: float) -> str:
        label = describe_key(launcher_key, code)
        self.last_key = now
        self.back_run = self.back_run + 1 if launcher_key == BACK_KEY else 0
        self.seen = (self.seen + [label])[-self.HISTORY:]
        return label

    def seconds_left(self, now: float) -> int:
        return max(0, math.ceil(self.seconds - (now - self.last_key)))

    def done(self, now: float) -> bool:
        return self.back_run >= self.backs or self.seconds_left(now) <= 0

    def rows(self) -> list[str]:
        return [
            "PRESS BUTTONS ON THE TV REMOTE. EACH ONE IS NAMED HERE.",
            "A BUTTON THAT IS NEVER NAMED IS BLOCKED BY THE TV.",
            "",
            f"LAST BUTTON  {self.seen[-1] if self.seen else '-'}",
            f"EARLIER  {'  '.join(reversed(self.seen[:-1])) or '-'}",
        ]

    def hint(self, now: float) -> str:
        return f"PRESS BACK TWICE TO LEAVE  ·  CLOSES IN {self.seconds_left(now)} S WITH NO BUTTON"


STANDBY_ROW = "TV STANDBY WHEN PC SLEEPS"
STANDBY_HINT = "TURNS THE TV OFF WHEN THIS BOX SLEEPS (ONLY IF THE TV IS SHOWING IT)"
_ROW_HINTS = {
    STANDBY_ROW: STANDBY_HINT,
    REMOTE_TEST_ROW: "SEE WHICH BUTTONS OF YOUR TV REMOTE THE TV PASSES ON",
}


def row_hint(row: str) -> str:
    """The footer for a TV CONTROL row, or "" for the usual one."""
    return _ROW_HINTS.get(row.split("  ")[0], "")


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


def suspend_supported(state: pathlib.Path = POWER_STATE, run=subprocess.run) -> bool:
    """Whether this PC can suspend: the same answer as the main SLEEP entry (couchliteos_power).

    logind's CanSuspend "yes"/"challenge" means yes and "na" means no; anything else (the launcher's
    non-root call usually gets "no": no polkit) leaves /sys/power/state to decide: "mem" or "freeze"."""
    try:
        result = run(LOGIND_CAN_SUSPEND, capture_output=True, text=True, timeout=3, check=False)
        answer = re.fullmatch(r's "(\w+)"\s*', result.stdout or "") if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        answer = None
    if answer and answer.group(1) in ("yes", "challenge"):
        return True
    if answer and answer.group(1) == "na":
        return False
    try:
        return bool({"mem", "freeze"} & set(state.read_text().split()))
    except OSError:
        return False


def effective_settings(settings: Settings, can_sleep: bool) -> Settings:
    """What the daemon acts on: a sleep switch saved on another PC means nothing here."""
    return settings if can_sleep else dataclasses.replace(settings, sleep_on_tv_off=False)


def toggle_rows(settings: Settings, usable: bool = True, can_sleep: bool = True) -> list[str]:
    def word(value: bool) -> str:
        return ("ON" if value else "OFF") if usable else "UNAVAILABLE"

    def sleepy(value: bool) -> str:
        return word(value) if can_sleep or not usable else "NOT SUPPORTED ON THIS PC"

    return [
        f"TURN TV ON AT START/WAKE  {word(settings.turn_tv_on)}",
        f"SLEEP WHEN TV TURNS OFF  {sleepy(settings.sleep_on_tv_off)}",
        f"{STANDBY_ROW}  {sleepy(settings.tv_off_on_sleep)}",
    ]


def toggled(settings: Settings, index: int, usable: bool = True, can_sleep: bool = True) -> Settings:
    """The settings after flipping toggle `index` (0 TV on, 1 sleep on TV off, 2 TV standby on sleep).

    Unchanged when that switch is unavailable here."""
    if not usable or (index > 0 and not can_sleep):
        return settings
    if index == 0:
        return dataclasses.replace(settings, turn_tv_on=not settings.turn_tv_on)
    if index == 1:
        return dataclasses.replace(settings, sleep_on_tv_off=not settings.sleep_on_tv_off)
    return dataclasses.replace(settings, tv_off_on_sleep=not settings.tv_off_on_sleep)
