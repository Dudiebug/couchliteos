#!/usr/bin/python3
"""First-boot setup wizard: each step is done in place and checked before moving on.

The pure logic (step order, resume state, instructions, big digits, result
checks) lives at the top of this module and is unit tested. The curses drawing
and the flows that talk to the system come after it.
"""

from __future__ import annotations

import collections
import curses
import dataclasses
import glob
import ipaddress
import json
import os
import pathlib
import re
import secrets
import select
import subprocess
import tempfile
import textwrap
import time
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import moonlightos_audio as audio
import moonlightos_bluetooth as bluetooth
import moonlightos_display as display


MARKER = pathlib.Path("/var/lib/moonlightos/setup-complete")
STATE = pathlib.Path("/var/lib/moonlightos/setup-state.json")
DONE, SKIPPED, FAILED = "done", "skipped", "failed"
OUTCOMES = (DONE, SKIPPED, FAILED)
TITLES = {
    "network": "NETWORK",
    "controller": "CONTROLLER",
    "display": "DISPLAY AND SOUND",
    "streaming": "STREAMING PC",
    "tv": "TV CONTROL",
    "tailscale": "TAILSCALE",
    "chiaki-ng": "CHIAKI-NG",
    "applications": "MORE APPS",
}


def plan_steps(*, cec_present: bool, tv_available: bool) -> list[str]:
    steps = ["network", "controller", "display", "streaming"]
    if cec_present and tv_available:
        steps.append("tv")
    return steps + ["tailscale", "chiaki-ng", "applications"]


def _atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_complete(path: pathlib.Path = MARKER) -> None:
    _atomic_write(path, "1\n")


def save_state(state: dict[str, str], path: pathlib.Path = STATE) -> None:
    _atomic_write(path, json.dumps({"steps": state}, sort_keys=True) + "\n")


def load_state(path: pathlib.Path = STATE) -> dict[str, str]:
    try:
        steps = json.loads(path.read_text(encoding="utf-8")).get("steps", {})
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(steps, dict):
        return {}
    return {key: value for key, value in steps.items() if key in TITLES and value in OUTCOMES}


def first_unfinished(order: Sequence[str], state: dict[str, str]) -> int:
    return next((index for index, step in enumerate(order) if step not in state), len(order))


def summary(order: Sequence[str], state: dict[str, str]) -> list[tuple[str, str]]:
    return [(TITLES[step], state.get(step, "not done").upper()) for step in order]


# --- big digits -----------------------------------------------------------

BIG_GAP = 2
BIG_ROWS = 5
_GLYPHS = {
    "0": (" ### ", "#   #", "#   #", "#   #", " ### "),
    "1": ("  #  ", " ##  ", "  #  ", "  #  ", " ### "),
    "2": (" ### ", "#   #", "  ## ", " #   ", "#####"),
    "3": ("#### ", "    #", " ### ", "    #", "#### "),
    "4": ("#  # ", "#  # ", "#####", "   # ", "   # "),
    "5": ("#####", "#    ", "#### ", "    #", "#### "),
    "6": (" ### ", "#    ", "#### ", "#   #", " ### "),
    "7": ("#####", "    #", "   # ", "  #  ", " #   "),
    "8": (" ### ", "#   #", " ### ", "#   #", " ### "),
    "9": (" ### ", "#   #", " ####", "    #", " ### "),
}


def can_render_big(text: str) -> bool:
    return bool(text) and all(character in _GLYPHS for character in text)


def render_big(text: str, *, on: str = "#", scale_x: int = 1) -> list[str]:
    if not can_render_big(text):
        raise ValueError("only the digits 0-9 can be drawn large")
    gap = " " * (BIG_GAP * scale_x)
    return [
        gap.join("".join((on if cell == "#" else " ") * scale_x for cell in _GLYPHS[digit][row]) for digit in text)
        for row in range(BIG_ROWS)
    ]


def big_width(text: str, *, scale_x: int = 1) -> int:
    return len(render_big(text, scale_x=scale_x)[0])


# --- controller pairing ---------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ControllerType:
    id: str
    label: str
    instructions: tuple[str, ...]
    hints: tuple[str, ...]


CONTROLLER_TYPES = (
    ControllerType(
        "xbox", "XBOX WIRELESS CONTROLLER",
        ("Hold the pair button on top of the controller", "until the Xbox button flashes fast."),
        ("xbox",),
    ),
    ControllerType(
        "ds4", "DUALSHOCK 4 (PS4)",
        ("Hold Share + PS together", "until the light bar double-flashes."),
        ("wireless controller", "dualshock"),
    ),
    ControllerType(
        "dualsense", "DUALSENSE (PS5)",
        ("Hold Create + PS together", "until the lights flash."),
        ("dualsense", "wireless controller"),
    ),
    ControllerType(
        "switch", "SWITCH PRO CONTROLLER",
        ("Hold the small sync button on top", "until the lights run back and forth."),
        ("pro controller", "joy-con", "nintendo"),
    ),
    ControllerType(
        "other", "8BITDO / OTHER",
        ("Look in the manual for PAIRING MODE.", "It is usually Start + a mode button."),
        ("8bitdo", "controller", "gamepad", "pad", "joy"),
    ),
)


def rank_candidates(devices: list[dict[str, Any]], type_id: str) -> list[dict[str, Any]]:
    hints = next(item.hints for item in CONTROLLER_TYPES if item.id == type_id)

    def key(device: dict[str, Any]) -> tuple[bool, bool, int]:
        alias = str(device.get("alias") or "").casefold()
        return (
            not any(hint in alias for hint in hints),
            not device.get("paired"),
            -int(device.get("rssi") or -127),
        )

    usable = [device for device in bluetooth.named_devices(devices) if not device.get("connected")]
    return sorted(usable, key=key)


EV_KEY = 1
BTN_SOUTH = 304


@dataclasses.dataclass(frozen=True)
class InputDevice:
    name: str
    uniq: str
    event: str
    gamepad: bool


def parse_input_devices(text: str) -> list[InputDevice]:
    devices = []
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if len(line) > 3 and line[1] == ":" and "=" in line:
                key, _, value = line[3:].partition("=")
                fields[key.strip()] = value.strip().strip('"')
        handlers = fields.get("Handlers", "").split()
        event = next((item for item in handlers if re.fullmatch(r"event\d+", item)), "")
        if "Name" in fields and event:
            devices.append(InputDevice(
                fields["Name"], fields.get("Uniq", ""), f"/dev/input/{event}",
                any(re.fullmatch(r"js\d+", item) for item in handlers),
            ))
    return devices


def find_controller(devices: list[InputDevice], address: str) -> InputDevice | None:
    matches = [item for item in devices if item.uniq and item.uniq.casefold() == address.casefold()]
    return next((item for item in matches if item.gamepad), matches[0] if matches else None)


def new_gamepads(before: list[InputDevice], after: list[InputDevice]) -> list[InputDevice]:
    known = {item.event for item in before}
    return [item for item in after if item.gamepad and item.event not in known]


def _readable(device: Any, seconds: float) -> bool:
    return bool(select.select([device], [], [], seconds)[0])


def wait_for_a_button(
    device: Any, timeout: float, *, clock: Callable[[], float] = time.monotonic,
    wait: Callable[[Any, float], bool] = _readable, south: int = BTN_SOUTH, key_event: int = EV_KEY,
) -> bool:
    started = clock()
    try:
        while clock() - started < timeout:
            if not wait(device, 0.25):
                continue
            try:
                events = list(device.read())
            except BlockingIOError:
                continue
            except OSError:
                return False
            if any(item.type == key_event and item.code == south and item.value == 1 for item in events):
                return True
        return False
    finally:
        device.close()


# --- network --------------------------------------------------------------

def split_terse(line: str) -> list[str]:
    fields, current, escaped = [], [], False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    fields.append("".join(current))
    return fields


NetworkDevice = collections.namedtuple("NetworkDevice", "name kind state connection", defaults=("",))


def parse_devices(output: str) -> list[NetworkDevice]:
    rows = (split_terse(line) for line in output.splitlines() if line.strip())
    return [
        NetworkDevice(*row[:3], *(["" if row[3] == "--" else row[3]] if len(row) > 3 else []))
        for row in rows if len(row) >= 3
    ]


def wired_connected(devices: list[NetworkDevice]) -> bool:
    return any(item.kind == "ethernet" and item.state.split()[:1] == ["connected"] for item in devices)


def wifi_device(devices: list[NetworkDevice]) -> str | None:
    return next((item.name for item in devices if item.kind == "wifi"), None)


def _wifi_up(devices: list[NetworkDevice]) -> NetworkDevice | None:
    return next((item for item in devices if item.kind == "wifi" and item.state.split()[:1] == ["connected"]), None)


def wifi_connected(devices: list[NetworkDevice]) -> bool:
    return _wifi_up(devices) is not None


def wifi_connection_name(devices: list[NetworkDevice]) -> str:
    device = _wifi_up(devices)
    return device.connection if device else ""


@dataclasses.dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    signal: int
    security: str
    in_use: bool = False

    @property
    def secured(self) -> bool:
        return self.security.strip() not in ("", "--")

    @property
    def supported(self) -> bool:
        return not {"802.1X", "WEP", "OWE"} & set(self.security.split())

    @property
    def key_mgmt(self) -> str:
        words = set(self.security.split())
        return "sae" if "WPA3" in words and not {"WPA2", "WPA1"} & words else "wpa-psk"


def parse_wifi_list(output: str) -> list[WifiNetwork]:
    best: dict[str, WifiNetwork] = {}
    for line in output.splitlines():
        fields = split_terse(line)
        if len(fields) < 4 or not fields[1]:
            continue
        signal = int(fields[2]) if fields[2].isdigit() else 0
        network = WifiNetwork(fields[1], signal, fields[3], fields[0].strip() == "*")
        old = best.get(network.ssid)
        if old is None or network.signal > old.signal:
            best[network.ssid] = dataclasses.replace(network, in_use=network.in_use or bool(old and old.in_use))
        elif network.in_use:
            best[network.ssid] = dataclasses.replace(old, in_use=True)
    return sorted(best.values(), key=lambda item: -item.signal)


def valid_passphrase(text: str) -> bool:
    if len(text) == 64:
        return all(character in "0123456789abcdefABCDEF" for character in text)
    return 8 <= len(text) <= 63 and all(32 <= ord(character) < 127 for character in text)


def wifi_settings(network: WifiNetwork, password: str, uuid: str) -> dict[str, dict[str, Any]]:
    settings: dict[str, dict[str, Any]] = {
        "connection": {"id": network.ssid, "uuid": uuid, "type": "802-11-wireless", "autoconnect": True},
        "802-11-wireless": {"ssid": network.ssid.encode("utf-8"), "mode": "infrastructure"},
        "ipv4": {"method": "auto"},
        "ipv6": {"method": "auto"},
    }
    if network.secured:
        settings["802-11-wireless-security"] = {"key-mgmt": network.key_mgmt, "psk": password}
    return settings


def parse_gateway(text: str) -> str:
    match = re.search(r"^default\s+via\s+(\S+)", text, re.MULTILINE)
    if not match:
        return ""
    try:
        return str(ipaddress.ip_address(match.group(1)))
    except ValueError:
        return ""


@dataclasses.dataclass(frozen=True)
class Connectivity:
    gateway: str
    lan: bool
    dns: bool
    internet: bool
    portal: bool

    @property
    def headline(self) -> str:
        if self.internet:
            return "ONLINE"
        if self.portal:
            return "LOGIN PAGE BLOCKS THE INTERNET"
        if self.lan:
            return "LOCAL NETWORK ONLY"
        return "ROUTER DOES NOT ANSWER" if self.gateway else "NO LOCAL NETWORK"

    @property
    def usable(self) -> bool:
        # Many routers ignore ping, so a missed ping alone never makes the network unusable.
        return self.lan or self.internet or self.portal

    def lines(self) -> list[str]:
        def word(flag: bool) -> str:
            return "OK" if flag else "FAILED"

        where = f" ({self.gateway})" if self.gateway else ""
        router = "OK" if self.lan else "NO PING ANSWER (THAT IS OK)" if self.usable else "FAILED"
        return [
            f"LOCAL NETWORK{where}: {router}",
            f"NAME LOOKUP: {word(self.dns)}",
            f"INTERNET: {word(self.internet)}",
        ]


def check_connectivity(
    *, gateway: Callable[[], str], ping: Callable[[str], bool],
    resolves: Callable[[], bool], fetch: Callable[[], str],
) -> Connectivity:
    address = gateway()
    lan = bool(address) and ping(address)
    dns = resolves()
    page = fetch()
    return Connectivity(address, lan, dns, page == "ok", page == "portal")


# --- display, sound, streaming -------------------------------------------

def display_choices(modes: Sequence[Any]) -> list[tuple[str, str, int]]:
    usable = [mode for mode in modes if mode.width >= 1280 and mode.height >= 720] or list(modes)
    ordered = sorted(usable, key=lambda mode: (not mode.preferred, -mode.width * mode.height, -mode.refresh_mhz))
    choices, seen = [], set()
    for mode in ordered:
        key = (mode.resolution, mode.refresh_mhz)
        if key in seen:
            continue
        seen.add(key)
        flags = [name for name, on in (("NATIVE", mode.preferred), ("CURRENT", mode.current)) if on]
        label = f"{mode.resolution}  {mode.refresh} HZ" + (f"  ({', '.join(flags)})" if flags else "")
        choices.append((label, mode.resolution, mode.refresh_mhz))
    return choices


_VIDEO_SINK = re.compile(r"hdmi|display\s*port|\bdp\b", re.IGNORECASE)


def sound_candidates(sinks: Sequence[Any]) -> list[Any]:
    video = [sink for sink in sinks if _VIDEO_SINK.search(sink.name)]
    video.sort(key=lambda sink: not sink.default)
    return video + [sink for sink in sinks if sink not in video]


def new_pairing_pin() -> str:
    return f"{secrets.randbelow(10000):04d}"


_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def valid_host(text: str) -> str | None:
    return text if len(text) <= 253 and _HOST.fullmatch(text) else None


def moonlight_pair_arguments(host: str, pin: str) -> str:
    return f"pair {host} --pin {pin}"


def paired_hosts(text: str) -> list[str]:
    names: dict[int, str] = {}
    paired: set[int] = set()
    for line in text.splitlines():
        match = re.match(r"(\d+)\\(hostname|srvcert)=(.*)$", line)
        if not match:
            continue
        index, field, value = int(match.group(1)), match.group(2), match.group(3)
        if field == "hostname":
            names[index] = value
        elif re.fullmatch(r"@ByteArray\(.+\)", value):
            paired.add(index)
    return [names[index] for index in sorted(paired) if names.get(index)]


# --- hardware gates: each one answers "can this PC do it, and if not why" --

NO_BLUETOOTH_HINT = "PLUG IN A CONTROLLER BY USB"
NO_SOUND = "NO SOUND OUTPUT FOUND — NOTHING TO TEST ON THIS PC"
SAVE_FAILED = "COULD NOT SAVE SETUP PROGRESS: DISK FULL OR READ-ONLY"


def bluetooth_gate(snapshot: dict[str, Any] | None) -> tuple[bool, str]:
    if snapshot is None:
        return False, "BLUETOOTH SERVICE NOT RUNNING"
    if not isinstance(snapshot.get("adapter"), dict):
        return False, "NO BLUETOOTH ADAPTER"
    return True, ""


def network_mode(devices: list[NetworkDevice]) -> str:
    if wired_connected(devices):
        return "wired"
    if wifi_connected(devices):
        return "wifi-up"
    return "wifi" if wifi_device(devices) else "none"


def display_gate(modes: Sequence[Any]) -> tuple[bool, str]:
    choices = display_choices(modes)
    if not choices:
        return False, "NO DISPLAY MODES AVAILABLE"
    if len(choices) == 1:
        return False, f"ONLY ONE DISPLAY MODE IS AVAILABLE: {choices[0][0].split('  (')[0]}"
    return True, ""


def sound_gate(sinks: Sequence[Any]) -> tuple[bool, str]:
    return (True, "") if sinks else (False, NO_SOUND)


def combine(*outcomes: str) -> str:
    if FAILED in outcomes:
        return FAILED
    return DONE if DONE in outcomes else SKIPPED


def controller_matches(device: dict[str, Any], type_id: str) -> bool:
    hints = next(item.hints for item in CONTROLLER_TYPES if item.id == type_id)
    alias = str(device.get("alias") or "").casefold()
    return any(hint in alias for hint in hints)


# --- the real system, one small method per boundary -----------------------

NM = "org.freedesktop.NetworkManager"
CHECK_URL = "http://network-test.debian.org/nm"
CHECK_HOST = "network-test.debian.org"


class System:
    def __init__(
        self,
        *,
        input_devices_path: pathlib.Path = pathlib.Path("/proc/bus/input/devices"),
        config_root: pathlib.Path = pathlib.Path("/var/lib/moonlightos/home/.config"),
    ) -> None:
        self.input_devices_path = input_devices_path
        self.config_root = config_root

    @staticmethod
    def run(args: list[str], timeout: float = 6) -> Any:
        environment = dict(os.environ, LC_ALL="C")
        try:
            return subprocess.run(
                args, text=True, capture_output=True, check=False, timeout=timeout, env=environment
            )
        except (OSError, subprocess.SubprocessError):
            return None

    @staticmethod
    def cec_present() -> bool:
        return bool(glob.glob("/dev/cec*"))

    def network_devices(self) -> list[NetworkDevice]:
        result = self.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])
        return parse_devices(result.stdout) if result and result.returncode == 0 else []

    def wifi_interface(self) -> str | None:
        return wifi_device(self.network_devices())

    def wifi_networks(self) -> list[WifiNetwork]:
        self.run(["nmcli", "device", "wifi", "rescan"])
        time.sleep(3)
        result = self.run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list"])
        return parse_wifi_list(result.stdout) if result and result.returncode == 0 else []

    def connect_wifi(self, network: WifiNetwork, password: str) -> tuple[bool, str]:
        """Join a network. The password only travels over the NetworkManager D-Bus
        API: it is never placed on a command line, in the environment, or in a log."""
        interface = self.wifi_interface()
        if not interface:
            return False, "NO WI-FI ADAPTER FOUND"
        try:
            return self._activate(wifi_settings(network, password, str(uuid.uuid4())), interface)
        except Exception:
            return False, "COULD NOT CONNECT: NETWORKMANAGER REFUSED THE REQUEST"

    @staticmethod
    def _activate(settings: dict[str, dict[str, Any]], interface: str) -> tuple[bool, str]:
        import dbus  # python3-dbus, already part of the image

        bus = dbus.SystemBus()
        manager = dbus.Interface(bus.get_object(NM, "/org/freedesktop/NetworkManager"), NM)
        device = manager.GetDeviceByIpIface(interface)
        store = dbus.Interface(
            bus.get_object(NM, "/org/freedesktop/NetworkManager/Settings"), NM + ".Settings"
        )
        name = settings["connection"]["id"]
        # Saved profiles for this network are only replaced once the new one works:
        # a typo in the password must not cost the user a connection that already works.
        earlier = []
        for path in store.ListConnections():
            profile = dbus.Interface(bus.get_object(NM, path), NM + ".Settings.Connection")
            try:
                known = profile.GetSettings().get("connection", {})
            except dbus.DBusException:
                continue
            if str(known.get("id")) == name and str(known.get("type")) == "802-11-wireless":
                earlier.append(profile)

        typed = dbus.Dictionary(
            {
                section: dbus.Dictionary(
                    {key: dbus.ByteArray(value) if isinstance(value, bytes) else value for key, value in body.items()},
                    signature="sv",
                )
                for section, body in settings.items()
            },
            signature="sa{sv}",
        )
        profile_path, active_path = manager.AddAndActivateConnection(typed, device, "/")
        new_profile = dbus.Interface(bus.get_object(NM, profile_path), NM + ".Settings.Connection")
        activated = False
        try:
            properties = dbus.Interface(bus.get_object(NM, active_path), "org.freedesktop.DBus.Properties")
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                try:
                    state = int(properties.Get(NM + ".Connection.Active", "State"))
                except dbus.DBusException:
                    break  # NetworkManager already removed the failed activation
                if state == 2:
                    activated = True
                    break
                if state in (3, 4):
                    break
                time.sleep(0.5)
        finally:
            for doomed in earlier if activated else [new_profile]:
                try:
                    doomed.Delete()
                except dbus.DBusException:
                    pass
        if activated:
            return True, ""
        return False, "COULD NOT CONNECT: CHECK THE PASSWORD AND THE SIGNAL"

    def gateway(self) -> str:
        result = self.run(["ip", "-4", "route", "show", "default"])
        return parse_gateway(result.stdout) if result and result.returncode == 0 else ""

    def ping(self, host: str) -> bool:
        result = self.run(["ping", "-c", "1", "-W", "2", host])
        return bool(result and result.returncode == 0)

    def resolves(self) -> bool:
        result = self.run(["getent", "hosts", CHECK_HOST])
        return bool(result and result.returncode == 0)

    @staticmethod
    def fetch_check() -> str:
        try:
            with urllib.request.urlopen(CHECK_URL, timeout=4) as response:
                body = response.read(200).decode("utf-8", "replace")
        except (OSError, ValueError):
            return ""
        return "ok" if "NetworkManager is online" in body else "portal"

    def connectivity(self) -> Connectivity:
        return check_connectivity(
            gateway=self.gateway, ping=self.ping, resolves=self.resolves, fetch=self.fetch_check
        )

    def input_devices(self) -> list[InputDevice]:
        try:
            return parse_input_devices(self.input_devices_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return []

    @staticmethod
    def confirm_button(device: InputDevice, timeout: float) -> bool:
        import evdev  # python3-evdev, already part of the image

        try:
            opened = evdev.InputDevice(device.event)
        except OSError:
            return False
        return wait_for_a_button(opened, timeout)

    @staticmethod
    def display_modes() -> list[Any]:
        try:
            output = display.active_output(display.query_outputs())
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return []
        return list(output.modes) if output else []

    @staticmethod
    def sinks() -> list[Any]:
        try:
            return audio.query_sinks()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return []

    @staticmethod
    def set_default_sink(sink_id: int) -> None:
        audio.set_default(sink_id)

    def paired_hosts(self) -> list[str]:
        hosts: list[str] = []
        for path in sorted(self.config_root.glob("*/Moonlight.conf")):
            try:
                hosts += paired_hosts(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        return hosts


# --- the wizard: what happens on each screen --------------------------------

SKIP = "SKIP THIS STEP"
OK = "OK"
OPTIONAL = {
    "tailscale": ("REACH YOUR GAMING PC FROM ANYWHERE WITH TAILSCALE.", "OPEN TAILSCALE SETUP", "tailscale"),
    "chiaki-ng": ("STREAM FROM A PLAYSTATION ON YOUR NETWORK WITH CHIAKI-NG.", "OPEN CHIAKI-NG", "chiaki-ng"),
}


class SetupWizard:
    def __init__(
        self,
        ui: Any,
        actions: dict[str, Callable[..., Any]],
        system: Any,
        *,
        bluetooth_client: Any = None,
        statuses: dict[str, Callable[[], str]] | None = None,
        marker: pathlib.Path = MARKER,
        state_path: pathlib.Path = STATE,
    ) -> None:
        self.ui = ui
        self.actions = actions
        self.system = system
        self.bluetooth = bluetooth_client
        self.statuses = statuses or {}
        self.marker = marker
        self.state_path = state_path
        self.state: dict[str, str] = {}
        self.save_failed = False

    # small helpers
    def pick(self, title: str, lines: list[str], choices: list[str], *, big: str | None = None) -> str | None:
        index = self.ui.menu(title, lines, choices, big=big)
        return None if index is None else choices[index]

    def notice(self, title: str, lines: list[str], *, big: str | None = None) -> None:
        self.ui.menu(title, lines, [OK], big=big)

    def status_line(self, step: str) -> str:
        try:
            return self.statuses[step]()
        except Exception:
            return ""

    def save_progress(self) -> bool:
        try:
            save_state(self.state, self.state_path)
            return True
        except OSError:
            self.tell_save_failed()
            return False

    def tell_save_failed(self) -> None:
        if not self.save_failed:
            self.save_failed = True
            self.notice("SETUP", [SAVE_FAILED, "SETUP WILL ASK AGAIN THE NEXT TIME THE LAUNCHER STARTS."])

    def mark_complete(self) -> None:
        try:
            write_complete(self.marker)
        except OSError:
            self.tell_save_failed()

    # the whole run
    def run(self, *, force: bool = False) -> bool:
        if self.marker.exists() and not force:
            return False
        self.state = {} if force else load_state(self.state_path)
        if force:
            self.save_progress()
        order = plan_steps(
            cec_present=self.system.cec_present(), tv_available=callable(self.actions.get("tv"))
        )
        index = first_unfinished(order, self.state)
        answer = self.welcome(resuming=bool(self.state) and index < len(order), title=TITLES.get(order[index], "") if index < len(order) else "")
        if answer == "exit":
            return True
        if answer == "skip":
            self.mark_complete()
            return True
        if answer == "restart":
            self.state, index = {}, 0
            self.save_progress()
        while True:
            while index < len(order):
                step = order[index]
                self.state[step] = getattr(self, "step_" + step.replace("-", "_"))()
                self.save_progress()
                index += 1
            choice = self.done_screen(order)
            if choice == "FINISH":
                self.mark_complete()
                return True
            if choice is not None:
                index = next(i for i, step in enumerate(order) if self.state.get(step) != DONE)

    def welcome(self, *, resuming: bool, title: str) -> str:
        lines = [
            "THIS SETS UP NETWORK, CONTROLLER, PICTURE, SOUND, AND YOUR GAMING PC.",
            "IT TAKES A FEW MINUTES. EVERY STEP CAN BE SKIPPED WITH B (ESC).",
            "USE A WIRED CONTROLLER OR A KEYBOARD UNTIL YOUR WIRELESS ONE IS PAIRED.",
            "RUN IT AGAIN ANY TIME FROM SETTINGS > SETUP WIZARD.",
        ]
        if resuming:
            lines.insert(0, f"SETUP WAS NOT FINISHED. IT CONTINUES AT: {title}")
        choices = (["CONTINUE SETUP", "START OVER"] if resuming else ["START SETUP"]) + ["SKIP SETUP", "EXIT TO LAUNCHER"]
        choice = self.pick("WELCOME BACK" if resuming else "WELCOME TO MOONLIGHTOS", lines, choices)
        return {"START SETUP": "go", "CONTINUE SETUP": "go", "START OVER": "restart", "SKIP SETUP": "skip"}.get(choice or "", "exit")

    def done_screen(self, order: list[str]) -> str | None:
        lines = [f"{title}: {status}" for title, status in summary(order, self.state)]
        if callable(self.actions.get("tv")) and "tv" not in order:
            lines.append("TV CONTROL: NO CEC ADAPTER FOUND")
        lines += ["", "RUN SETUP AGAIN ANY TIME FROM SETTINGS > SETUP WIZARD."]
        choices = ["FINISH"]
        if any(self.state.get(step) != DONE for step in order):
            choices.append("REDO SKIPPED OR FAILED STEPS")
        choice = self.pick("SETUP COMPLETE", lines, choices)
        return None if choice is None else ("FINISH" if choice == "FINISH" else "redo")

    # 1. network
    def step_network(self) -> str:
        offered_keep = False
        while True:
            devices = self.system.network_devices()
            mode = network_mode(devices)
            if mode == "wired":
                return self.network_result("CONNECTED BY CABLE")
            if mode == "wifi-up" and not offered_keep:
                offered_keep = True
                name = wifi_connection_name(devices)
                choice = self.pick(
                    "NETWORK",
                    [f"THIS PC IS ALREADY ON WI-FI{': ' + name if name else ''}.",
                     "PRESS A TO KEEP IT, OR CHOOSE A DIFFERENT NETWORK. B SKIPS."],
                    ["KEEP THIS NETWORK", "CHOOSE ANOTHER NETWORK", SKIP],
                )
                if choice == "KEEP THIS NETWORK":
                    return self.network_result(f"CONNECTED TO WI-FI{': ' + name if name else ''}")
                if choice != "CHOOSE ANOTHER NETWORK":
                    return SKIPPED
            if mode == "none":
                choice = self.pick(
                    "NETWORK",
                    ["NO WI-FI ADAPTER AND NO CABLE FOUND.", "PLUG IN A NETWORK CABLE, THEN CHECK AGAIN."],
                    ["CHECK AGAIN", SKIP],
                )
                if choice != "CHECK AGAIN":
                    return SKIPPED
                continue
            outcome = self.wifi_flow()
            if outcome:
                return outcome

    def wifi_flow(self) -> str | None:
        self.ui.status("NETWORK", ["LOOKING FOR WI-FI NETWORKS..."])
        networks = self.system.wifi_networks()
        labels = [
            f"{item.ssid}  {item.signal}%" + ("" if item.secured else "  (OPEN)") + ("  (CONNECTED)" if item.in_use else "")
            for item in networks
        ]
        index = self.ui.menu(
            "NETWORK",
            ["NO CABLE FOUND. CHOOSE YOUR WI-FI NETWORK,", "OR PLUG IN A CABLE AND CHOOSE SCAN AGAIN."],
            labels + ["SCAN AGAIN", SKIP],
        )
        if index is None or index == len(labels) + 1:
            return SKIPPED
        if index == len(labels):
            return None
        network = networks[index]
        if not network.supported:
            self.notice("NETWORK", [f"{network.ssid}: NOT SUPPORTED HERE.", "WORK, SCHOOL AND WEP NETWORKS NEED A CABLE OR A PHONE HOTSPOT."])
            return None
        if not self.join(network):
            return None
        return self.network_result(f"CONNECTED TO {network.ssid}")

    def join(self, network: WifiNetwork) -> bool:
        while True:
            password = ""
            if network.secured:
                typed = self.actions["text"]("WI-FI PASSWORD", f"PASSWORD FOR {network.ssid}", 64, masked=True)
                if typed is None:
                    return False
                if not valid_passphrase(typed):
                    self.notice("WI-FI PASSWORD", ["THE PASSWORD MUST BE 8 TO 63 CHARACTERS."])
                    continue
                password = typed
            self.ui.status("NETWORK", [f"CONNECTING TO {network.ssid}..."])
            ok, message = self.system.connect_wifi(network, password)
            password = ""
            if ok:
                return True
            if self.pick("COULD NOT CONNECT", [message], ["TRY AGAIN", "CHOOSE ANOTHER NETWORK"]) != "TRY AGAIN":
                return False

    def network_result(self, connected: str) -> str:
        while True:
            self.ui.status("NETWORK", [connected, "CHECKING THE CONNECTION..."])
            result = self.system.connectivity()
            lines = [connected, result.headline, ""] + result.lines()
            if result.usable:
                again = not result.internet
                choice = self.pick("NETWORK", lines, ["CONTINUE"] + (["CHECK AGAIN"] if again else []))
                if choice == "CHECK AGAIN":
                    continue
                return DONE
            if self.pick("NETWORK", lines, ["TRY AGAIN", "CONTINUE ANYWAY"]) != "TRY AGAIN":
                return FAILED

    # 2. controller
    def bluetooth_snapshot(self) -> dict[str, Any] | None:
        try:
            return self.bluetooth.snapshot() if self.bluetooth else None
        except bluetooth.BluetoothError:
            return None

    def bluetooth_request(self, command: str, **fields: object) -> dict[str, Any] | None:
        try:
            return self.bluetooth.request(command, **fields) if self.bluetooth else None
        except bluetooth.BluetoothError:
            return None

    def gamepads(self) -> list[InputDevice]:
        return [item for item in self.system.input_devices() if item.gamepad]

    def step_controller(self) -> str:
        usable, reason = bluetooth_gate(self.bluetooth_snapshot())
        if not usable:
            return self.usb_controller(f"{reason} — {NO_BLUETOOTH_HINT}")
        lines = ["PAIR A WIRELESS CONTROLLER SO YOU CAN PLAY WITHOUT A KEYBOARD."]
        pads = self.gamepads()
        choices = ["PAIR A WIRELESS CONTROLLER"]
        if pads:
            lines.append("CONNECTED NOW: " + ", ".join(sorted({item.name for item in pads}))[:70])
            choices.append("THIS CONTROLLER IS ENOUGH")
        while True:
            choice = self.pick("CONTROLLER", lines, choices + [SKIP])
            if choice == "THIS CONTROLLER IS ENOUGH":
                return DONE
            if choice != "PAIR A WIRELESS CONTROLLER":
                return SKIPPED
            outcome = self.pair_controller()
            if outcome:
                return outcome

    def usb_controller(self, reason: str) -> str:
        while True:
            pads = self.gamepads()
            if not pads:
                found = self.ui.wait(
                    "CONTROLLER", [reason, "", "WAITING FOR A USB CONTROLLER...", "(B SKIPS)"],
                    lambda: bool(self.gamepads()), 120,
                )
                if found:
                    continue
                if found is None or self.pick("CONTROLLER", [reason], ["KEEP WAITING", SKIP]) != "KEEP WAITING":
                    return SKIPPED
                continue
            return self.confirm_pad(pads[0], [reason])

    def confirm_pad(self, device: InputDevice, lines: list[str]) -> str:
        while True:
            self.ui.status("CONTROLLER", lines + ["", "PRESS A ON THE NEW CONTROLLER NOW.", "(WAITING 20 SECONDS)"])
            pressed = self.system.confirm_button(device, 20)
            self.ui.flush()
            if pressed:
                self.notice("CONTROLLER", ["YOUR CONTROLLER WORKS."])
                return DONE
            if self.pick("CONTROLLER", ["THE A BUTTON WAS NOT PRESSED."], ["TRY AGAIN", "CONTINUE ANYWAY"]) != "TRY AGAIN":
                return FAILED

    def pair_controller(self) -> str | None:
        index = self.ui.menu(
            "CONTROLLER TYPE", ["WHICH CONTROLLER ARE YOU PAIRING?"], [item.label for item in CONTROLLER_TYPES]
        )
        if index is None:
            return None
        kind = CONTROLLER_TYPES[index]
        while True:
            outcome = self.find_and_pair(kind)
            if outcome != "again":
                return outcome

    def candidates(self, kind: ControllerType, *, strict: bool) -> list[dict[str, Any]]:
        snapshot = self.bluetooth_snapshot() or {}
        ranked = rank_candidates(list(snapshot.get("devices") or []), kind.id)
        return [item for item in ranked if controller_matches(item, kind.id)] if strict else ranked

    def find_and_pair(self, kind: ControllerType) -> str | None:
        snapshot = self.bluetooth_snapshot()
        if not bluetooth_gate(snapshot)[0]:
            return self.usb_controller(f"NO BLUETOOTH ADAPTER — {NO_BLUETOOTH_HINT}")
        if not (snapshot or {}).get("adapter", {}).get("powered"):
            self.bluetooth_request("set_power", powered=True)
        self.bluetooth_request("start_scan")
        lines = ["PUT THE CONTROLLER IN PAIRING MODE:", *kind.instructions, "", "SEARCHING...  (B CANCELS)"]
        found = self.ui.wait("CONTROLLER", lines, lambda: bool(self.candidates(kind, strict=True)), 60)
        device = None
        if found:
            device = self.candidates(kind, strict=True)[0]
        elif found is False:
            others = self.candidates(kind, strict=False)
            labels = bluetooth.device_labels(others)
            index = self.ui.menu(
                "CONTROLLER",
                ["I CANNOT SEE YOUR CONTROLLER YET.", "IT MUST BE IN PAIRING MODE:", *kind.instructions, "", "PICK IT HERE IF IT IS LISTED."],
                labels + ["SEARCH AGAIN", "BACK"],
            )
            if index is not None and index == len(labels):
                self.bluetooth_request("stop_scan")
                return "again"
            if index is not None and index < len(labels):
                device = others[index]
        if device is None:
            self.bluetooth_request("stop_scan")
            return None
        if not self.pair(device):
            choice = self.pick("PAIRING FAILED", ["THE CONTROLLER DID NOT PAIR.", "PUT IT BACK IN PAIRING MODE AND TRY AGAIN."], ["TRY AGAIN", "CONTINUE ANYWAY"])
            return "again" if choice == "TRY AGAIN" else FAILED
        address = str(device.get("address") or "")
        arrived = self.ui.wait(
            "CONTROLLER", ["PAIRED. WAITING FOR THE CONTROLLER TO CONNECT..."],
            lambda: find_controller(self.system.input_devices(), address) is not None, 15,
        )
        pad = find_controller(self.system.input_devices(), address) if arrived else None
        if pad is None:
            self.notice("CONTROLLER", ["PAIRED, BUT THE CONTROLLER DID NOT SHOW UP AS AN INPUT DEVICE.", "TURN IT OFF AND ON, THEN RERUN SETUP."])
            return FAILED
        return self.confirm_pad(pad, ["PAIRED."])

    def pair(self, device: dict[str, Any]) -> bool:
        response = self.bluetooth_request("pair", device=str(device.get("path") or ""))
        if not response:
            return False
        operation_id = str(response.get("operation_id") or "")
        handled = ""
        while True:
            snapshot = self.bluetooth_snapshot()
            if snapshot is None:
                return False
            operation = next((item for item in snapshot.get("operations") or [] if str(item.get("id")) == operation_id), {})
            state = str(operation.get("state") or "")
            if state in ("completed", "paired"):
                return True
            if state in ("failed", "cancelled"):
                return False
            lines, big = ["PAIRING...  (B CANCELS)"], None
            prompt = snapshot.get("prompt")
            if isinstance(prompt, dict) and str(prompt.get("operation_id") or "") == operation_id:
                kind, prompt_id, code = str(prompt.get("kind")), str(prompt.get("id") or ""), str(prompt.get("passkey") or "")
                if kind == "display_passkey":
                    lines, big = ["TYPE THIS NUMBER ON THE DEVICE:"], (code if can_render_big(code) else None)
                    if big is None:
                        lines.append(code)
                elif prompt_id != handled:
                    handled = prompt_id
                    if kind == "confirmation":
                        answer = self.pick(
                            "CONFIRM THE NUMBER", ["DOES THE DEVICE SHOW THE SAME NUMBER?"],
                            ["YES, IT MATCHES", "NO"], big=code if can_render_big(code) else None,
                        )
                        self.bluetooth_request("agent_reply", prompt_id=prompt_id, accepted=bool(answer and answer.startswith("YES")))
                    elif kind == "authorization":
                        self.bluetooth_request("agent_reply", prompt_id=prompt_id, accepted=True)
                    elif kind == "pin_code":
                        self.bluetooth_request("agent_reply", prompt_id=prompt_id, accepted=True, value="0000")
                    else:
                        self.bluetooth_request("cancel_pairing", operation_id=operation_id)
                        return False
            if self.ui.wait("PAIRING", lines, lambda: False, 0.5, big=big) is None:
                self.bluetooth_request("cancel_pairing", operation_id=operation_id)
                return False

    # 3. display and sound
    def step_display(self) -> str:
        modes, sinks = self.system.display_modes(), self.system.sinks()
        shows, display_reason = display_gate(modes)
        plays, sound_reason = sound_gate(sinks)
        notes = [reason for ok, reason in ((shows, display_reason), (plays, sound_reason)) if not ok]
        if notes:
            self.notice("DISPLAY AND SOUND", notes)
        return combine(
            self.choose_display(modes) if shows else SKIPPED,
            self.choose_sound(sinks) if plays else SKIPPED,
        )

    def choose_display(self, modes: Sequence[Any]) -> str:
        choices = display_choices(modes)
        labels = [label for label, _resolution, _mhz in choices]
        note: list[str] = []
        while True:
            index = self.ui.menu(
                "DISPLAY AND SOUND",
                ["PICK THE PICTURE SIZE THAT FITS YOUR TV OR MONITOR. NATIVE IS USUALLY BEST.",
                 "YOU GET 15 SECONDS TO CONFIRM. IF THE PICTURE IS BAD IT GOES BACK BY ITSELF.", *note],
                labels + ["KEEP CURRENT (NO CHANGE)"],
            )
            if index is None:
                return SKIPPED
            if index == len(labels):
                return DONE
            _label, resolution, refresh = choices[index]
            if self.actions["display"](resolution, refresh):
                return DONE
            note = ["NOT CHANGED: THE PICTURE WAS NOT CONFIRMED."]

    def choose_sound(self, sinks: Sequence[Any]) -> str:
        candidates = sound_candidates(sinks)
        position = 0
        while True:
            if position >= len(candidates):
                choice = self.pick("NO TONE HEARD", ["NO OUTPUT PLAYED A TONE YOU COULD HEAR.", "CHECK THE CABLE AND THE VOLUME."], ["START OVER", "CONTINUE"])
                if choice != "START OVER":
                    return FAILED
                position = 0
                continue
            sink = candidates[position]
            try:
                self.system.set_default_sink(sink.id)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                position += 1
                continue
            self.ui.status("DISPLAY AND SOUND", ["PLAYING A TEST TONE ON:", sink.name])
            self.actions["tone"]()
            self.ui.flush()
            last = position == len(candidates) - 1
            choice = self.pick(
                "DID YOU HEAR THE TONE?", [sink.name],
                ["YES, I HEARD IT", "NO, I HEARD NOTHING" if last else "NO, TRY ANOTHER OUTPUT", "PLAY AGAIN", "SKIP SOUND"],
            )
            if choice == "YES, I HEARD IT":
                return DONE
            if choice is None or choice == "SKIP SOUND":
                return SKIPPED
            if choice != "PLAY AGAIN":
                position += 1

    # 4. streaming PC
    def step_streaming(self) -> str:
        lines = [
            "MOONLIGHT TALKS TO SUNSHINE ON YOUR GAMING PC. BOTH MUST BE ON THE SAME NETWORK.",
            "PAIRING NEEDS A 4 DIGIT PIN TYPED INTO SUNSHINE'S WEB PAGE ON THE PC.",
        ]
        while True:
            choice = self.pick(
                "STREAMING PC", lines,
                ["FIND MY GAMING PC IN MOONLIGHT", "PAIR WITH A PIN SHOWN HERE", SKIP],
            )
            if choice not in ("FIND MY GAMING PC IN MOONLIGHT", "PAIR WITH A PIN SHOWN HERE"):
                return SKIPPED
            paired = self.pair_in_moonlight() if choice.startswith("FIND") else self.pair_with_pin()
            if paired is None:
                continue
            if paired:
                return DONE
            again = self.pick("NOT PAIRED YET", ["NO PAIRED GAMING PC WAS FOUND."], ["TRY AGAIN", "CONTINUE WITHOUT PAIRING"])
            if again != "TRY AGAIN":
                return FAILED

    def pair_in_moonlight(self) -> bool | None:
        choice = self.pick(
            "FIND MY GAMING PC",
            [
                "MOONLIGHT OPENS NEXT AND LISTS GAMING PCS IT FINDS. CHOOSE YOURS.",
                "MOONLIGHT THEN SHOWS A PIN. ON THE GAMING PC, OPEN SUNSHINE'S WEB PAGE",
                "(HTTPS://LOCALHOST:47990), CLICK THE PIN TAB, AND TYPE THAT PIN.",
                "WHEN IT SAYS PAIRED, CLOSE MOONLIGHT OR PRESS THE HOME BUTTON.",
            ],
            ["OPEN MOONLIGHT", "BACK"],
        )
        if choice != "OPEN MOONLIGHT":
            return None
        self.actions["launch"]("moonlight")
        return bool(self.system.paired_hosts())

    def pair_with_pin(self) -> bool | None:
        while True:
            typed = self.actions["text"]("GAMING PC", "ADDRESS OR NAME OF YOUR GAMING PC (LIKE 192.168.1.20)", 253, masked=False)
            if typed is None:
                return None
            host = valid_host(typed.strip())
            if host:
                break
            self.notice("GAMING PC", ["THAT IS NOT A VALID ADDRESS OR NAME.", "USE LETTERS, NUMBERS, DOTS AND DASHES ONLY."])
        pin = new_pairing_pin()
        choice = self.pick(
            f"PAIR WITH {host}",
            [
                f"ON THE GAMING PC OPEN HTTPS://{host}:47990 (SUNSHINE), CLICK THE PIN TAB,",
                "AND TYPE THIS NUMBER. PRESS START PAIRING, THEN ENTER THE PIN AT ONCE.",
            ],
            ["START PAIRING", "BACK"],
            big=pin,
        )
        if choice != "START PAIRING":
            return None
        # The launcher reports a failed start when moonlight exits before it counts as ready,
        # which is also what a quickly typed PIN looks like. Only the saved pairing is trusted.
        self.actions["pair_moonlight"](host, pin)
        return bool(self.system.paired_hosts())

    # 5. optional steps
    def open_step(self, step: str, intro: str, label: str, action: Callable[[], Any]) -> str:
        lines = [intro]
        status = self.status_line(step)
        if status:
            lines.append(status)
        if self.pick(TITLES[step], lines, [label, SKIP]) != label:
            return SKIPPED
        return FAILED if action() is False else DONE

    def step_tv(self) -> str:
        return self.open_step("tv", "USE THE TV REMOTE AND CONTROL POWER AND INPUT FROM HERE.", "OPEN TV CONTROL", self.actions["tv"])

    def step_tailscale(self) -> str:
        intro, label, app = OPTIONAL["tailscale"]
        return self.open_step("tailscale", intro, label, lambda: self.actions["launch"](app))

    def step_chiaki_ng(self) -> str:
        intro, label, app = OPTIONAL["chiaki-ng"]
        return self.open_step("chiaki-ng", intro, label, lambda: self.actions["launch"](app))

    def step_applications(self) -> str:
        return self.open_step("applications", "ADD MORE APPS, LIKE A WEB BROWSER OR A REMOTE DESKTOP.", "OPEN APPLICATIONS", self.actions["applications"])


# --- curses drawing (thin: no decisions are made here) ----------------------

SPINNER = "|/-\\"
ENTER = (curses.KEY_ENTER, 10, 13)
MENU_FOOTER = "A / ENTER: SELECT   ·   B / ESC: SKIP OR BACK"


def big_lines(text: str, width: int) -> list[str]:
    if can_render_big(text):
        for scale in (2, 1):
            if big_width(text, scale_x=scale) + 4 <= width:
                return render_big(text, on="█", scale_x=scale)
    return [" ".join(text)]


class CursesUI:
    def __init__(self, screen: curses.window) -> None:
        self.screen = screen

    def flush(self) -> None:
        curses.flushinp()

    def put(self, row: int, column: int, text: str, attribute: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if 0 <= row < height and 0 <= column < width - 1:
            try:
                self.screen.addnstr(row, column, text, width - column - 1, attribute)
            except curses.error:
                pass

    def center(self, row: int, text: str, attribute: int = 0) -> None:
        width = self.screen.getmaxyx()[1]
        self.put(row, max(1, (width - len(text)) // 2), text[: max(1, width - 3)], attribute)

    def draw(
        self, title: str, lines: list[str], choices: list[str], selected: int | None,
        big: str | None, footer: str,
    ) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        try:
            self.screen.border()
        except curses.error:
            pass
        self.center(2, title, curses.A_BOLD)
        row = 4
        for line in lines:
            for part in textwrap.wrap(line, max(10, width - 8)) or [""]:
                self.center(row, part)
                row += 1
        if big:
            row += 1
            for piece in big_lines(big, width):
                self.center(row, piece, curses.A_BOLD)
                row += 1
        row += 1
        room = max(1, height - row - 3)
        offset = 0 if selected is None else min(max(0, selected - room + 1), max(0, len(choices) - room))
        left = max(2, (width - max((len(item) for item in choices), default=1) - 3) // 2)
        for index, choice in enumerate(choices[offset:offset + room], start=offset):
            mark = ">" if index == selected else " "
            self.put(row + index - offset, left, f"{mark}  {choice}", curses.A_BOLD if index == selected else 0)
        self.center(height - 2, footer)
        self.screen.refresh()

    def menu(self, title: str, lines: list[str], choices: list[str], *, big: str | None = None, selected: int = 0) -> int | None:
        selected = min(max(0, selected), len(choices) - 1)
        self.screen.timeout(1000)
        while True:
            self.draw(title, lines, choices, selected, big, MENU_FOOTER)
            key = self.screen.getch()
            if key == curses.KEY_UP:
                selected = (selected - 1) % len(choices)
            elif key == curses.KEY_DOWN:
                selected = (selected + 1) % len(choices)
            elif key in ENTER:
                return selected
            elif key == 27:
                return None

    def status(self, title: str, lines: list[str], *, big: str | None = None) -> None:
        self.draw(title, lines, [], None, big, "PLEASE WAIT")

    def wait(
        self, title: str, lines: list[str], done: Callable[[], bool], timeout: float, *, big: str | None = None,
    ) -> bool | None:
        deadline = time.monotonic() + timeout
        frame = 0
        self.screen.timeout(200)
        try:
            while not done():
                if time.monotonic() >= deadline:
                    return False
                self.draw(title, lines + [f"{SPINNER[frame % len(SPINNER)]}  PLEASE WAIT"], [], None, big, "B / ESC: CANCEL")
                frame += 1
                if self.screen.getch() == 27:
                    return None
            return True
        finally:
            self.screen.timeout(1000)
