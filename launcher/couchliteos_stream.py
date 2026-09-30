#!/usr/bin/python3
"""Couch-to-game helpers: Wake-on-LAN, paired Moonlight hosts, auto-stream, stream tuning.

Moonlight Qt keeps its paired hosts and stream preferences in a QSettings INI file
(`Moonlight.conf`). This module only reads it, except for the four stream keys that
`apply_plan` rewrites, line by line, while Moonlight is not running.
"""

from __future__ import annotations

import configparser
import dataclasses
import ipaddress
import math
import os
import pathlib
import re
import socket
import stat
import string
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable


MOONLIGHT_CONF = pathlib.Path(
    "/var/lib/couchliteos/home/.config/Moonlight Game Streaming Project/Moonlight.conf"
)
CONFIG = pathlib.Path("/var/lib/couchliteos/config.ini")
RUN = pathlib.Path("/run/couchliteos")
STREAM_REQUEST = RUN / "moonlight-stream.request"
SECTION = "streaming"

HTTP_PORT = 47989  # Sunshine / GameStream HTTP port; Moonlight's default
WOL_PORTS = (9, 7)
PROBE_TIMEOUT = 1.0
WAKE_TIMEOUT = 30.0
POLL_SECONDS = 0.5
RESEND_SECONDS = 5.0

HOST_RE = re.compile(r"[A-Za-z0-9._:-]{1,253}")
APP_MAX = 128
MAC_TEXT_RE = re.compile(r"[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}")


REQUEST_MAX_AGE = 120.0  # a stream request older than this is a leftover, not a request
PAIR_FIRST = "PAIR A GAMING PC IN MOONLIGHT FIRST"
NO_MAC = "MOONLIGHT HASN'T LEARNED THIS PC'S NETWORK ADDRESS YET; OPEN MOONLIGHT WHILE THE PC IS ON"
NO_NETWORK = "NO NETWORK"


class StreamError(Exception):
    """A streaming action cannot run; the message is meant for the user."""


# --------------------------------------------------------------------------- Qt INI

_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def unescape(value: str) -> str:
    """Undo QSettings' INI escapes (\\xHH, octal, \\n, \\\\ ...); one char per byte."""
    out: list[str] = []
    i, end = 0, len(value)
    while i < end:
        char = value[i]
        if char != "\\" or i + 1 >= end:
            out.append(char)
            i += 1
            continue
        i += 1
        char = value[i]
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
            i += 1
        elif char == "x":
            j = i + 1
            while j < end and value[j] in string.hexdigits:
                j += 1
            if j > i + 1:
                out.append(chr(min(int(value[i + 1:j], 16), 0x10FFFF)))
            i = j
        elif char in "01234567":
            j = i
            while j < end and j < i + 3 and value[j] in "01234567":
                j += 1
            out.append(chr(int(value[i:j], 8)))
            i = j
        else:
            out.append(char)
            i += 1
    return "".join(out)


def decode_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    return unescape(raw)


def byte_array(value: str) -> str | None:
    """The bytes (one char each) inside `@ByteArray(...)`, or None for other values."""
    if value.startswith("@ByteArray(") and value.endswith(")"):
        return value[len("@ByteArray("):-1]
    return None


def as_text(value: str) -> str:
    """Decode a value from the one-char-per-byte domain into real text (UTF-8)."""
    return value.encode("latin-1", "replace").decode("utf-8", "replace")


def mac_bytes(value: str) -> bytes:
    """Six raw MAC bytes from Moonlight's stored form (or an `aa:bb:..` text form)."""
    if MAC_TEXT_RE.fullmatch(value):
        raw = bytes.fromhex(re.sub("[:-]", "", value))
    else:
        raw = value.encode("latin-1", "replace")
    return raw if len(raw) == 6 and any(raw) else b""


# ------------------------------------------------------------------------ hosts


@dataclasses.dataclass(frozen=True)
class Host:
    name: str
    uuid: str = ""
    mac: bytes = b""
    local: str = ""
    local_port: int = HTTP_PORT
    manual: str = ""
    manual_port: int = HTTP_PORT
    remote: str = ""
    ipv6: str = ""
    apps: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.name.upper()

    @property
    def mac_text(self) -> str:
        return ":".join(f"{byte:02X}" for byte in self.mac)

    @property
    def target(self) -> str:
        """What `moonlight stream` is given as the host: an address, else the name."""
        return self.local or self.manual or self.name

    def lan_addresses(self) -> list[tuple[str, int]]:
        found: list[tuple[str, int]] = []
        for address, port in ((self.local, self.local_port), (self.manual, self.manual_port)):
            if address and all(address != item[0] for item in found):
                found.append((address, port))
        return found


def _port(value: str | None) -> int:
    try:
        port = int(value or "")
    except ValueError:
        return HTTP_PORT
    return port if 0 < port < 65536 else HTTP_PORT


def _host(fields: dict[str, str]) -> Host:
    def text(key: str) -> str:
        return as_text(fields.get(key, "")).strip()

    mac = fields.get("mac", "")
    inner = byte_array(mac)
    try:
        app_count = int(fields.get("apps\\size", "0"))
    except ValueError:
        app_count = 0
    apps: list[str] = []
    for index in range(1, app_count + 1):
        name = as_text(fields.get(f"apps\\{index}\\name", "")).strip()
        if name and fields.get(f"apps\\{index}\\hidden", "false") != "true":
            apps.append(name)
    return Host(
        name=text("hostname") or "UNKNOWN",
        uuid=text("uuid"),
        mac=mac_bytes(mac if inner is None else inner),
        local=text("localaddress"),
        local_port=_port(fields.get("localport")),
        manual=text("manualaddress"),
        manual_port=_port(fields.get("manualport")),
        remote=text("remoteaddress"),
        ipv6=text("ipv6address"),
        apps=tuple(apps),
    )


def parse_hosts(text: str) -> list[Host]:
    """Paired hosts from Moonlight.conf text, read the way Moonlight reads them.

    Moonlight prefers the `hostsbackup` array when it is non-empty, else `hosts`.
    """
    arrays: dict[str, dict[int, dict[str, str]]] = {"hosts": {}, "hostsbackup": {}}
    sizes: dict[str, int] = {}
    in_general = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in ";#":
            continue
        if line[0] == "[" and line[-1] == "]":
            in_general = line[1:-1].strip() == "General"
            continue
        if not in_general or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        for name in arrays:
            prefix = f"{name}\\"
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if rest == "size":
                try:
                    sizes[name] = int(decode_value(raw))
                except ValueError:
                    pass
                break
            index, _, field = rest.partition("\\")
            if index.isdigit() and field:
                arrays[name].setdefault(int(index), {})[field] = decode_value(raw)
            break
    for name in ("hostsbackup", "hosts"):
        count = sizes.get(name, 0)
        if count > 0:
            return [_host(arrays[name].get(index, {})) for index in range(1, count + 1)]
    return []


def load_hosts(path: pathlib.Path = MOONLIGHT_CONF) -> list[Host]:
    try:
        return parse_hosts(path.read_bytes().decode("latin-1"))
    except OSError:
        return []


def find_host(hosts: list[Host], ident: str) -> Host | None:
    ident = ident.strip().casefold()
    if not ident:
        return None
    for host in hosts:
        if ident == host.uuid.casefold():
            return host
    for host in hosts:
        if ident == host.name.casefold() or ident in (a.casefold() for a, _p in host.lan_addresses()):
            return host
    return None


# --------------------------------------------------------------------- settings


@dataclasses.dataclass(frozen=True)
class StreamSettings:
    autostart: bool = False
    host: str = ""
    app: str = "Desktop"


def valid_host(value: str) -> bool:
    return bool(HOST_RE.fullmatch(value)) and not value.startswith("-")


def valid_app(value: str) -> bool:
    return 0 < len(value) <= APP_MAX and value.isprintable() and value == value.strip() and not value.startswith("-")


def default_host(hosts: list[Host], settings: StreamSettings) -> Host | None:
    """The chosen host, else the only known host; never guess between several."""
    chosen = find_host(hosts, settings.host)
    if chosen is not None:
        return chosen
    return hosts[0] if len(hosts) == 1 else None


def autostream_host(hosts: list[Host], settings: StreamSettings) -> Host | None:
    """The PC to stream to at startup. A saved choice that is not paired on this
    system (the USB stick moved) is ignored, never swapped for another PC."""
    if settings.host:
        return find_host(hosts, settings.host)
    return hosts[0] if len(hosts) == 1 else None


def autostart_label(hosts: list[Host], settings: StreamSettings) -> str:
    """How the auto-stream switch reads in Settings, including why it cannot be on."""
    if not hosts:
        return f"UNAVAILABLE: {PAIR_FIRST}"
    if settings.autostart and autostream_host(hosts, settings) is None:
        return "ON  (IGNORED: CHOOSE A PC PAIRED ON THIS SYSTEM)"
    return "ON" if settings.autostart else "OFF"


def wake_block(host: Host) -> str:
    """Empty when `host` can be woken; otherwise the reason it cannot."""
    return "" if host.mac else NO_MAC


def link_up(sysfs: pathlib.Path = pathlib.Path("/sys/class/net")) -> bool:
    """True when some network interface other than loopback has a link."""
    try:
        names = [entry.name for entry in sysfs.iterdir() if entry.name != "lo"]
    except OSError:
        return False
    for name in names:
        try:
            if (sysfs / name / "operstate").read_text().strip() == "up":
                return True
        except OSError:
            continue
    return False


def _atomic_write(path: pathlib.Path, data: bytes, default_mode: int = 0o640) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = default_mode
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_settings(path: pathlib.Path = CONFIG) -> StreamSettings:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, UnicodeError, configparser.Error):
        return StreamSettings()
    try:
        autostart = parser.getboolean(SECTION, "autostart", fallback=False)
    except ValueError:
        autostart = False
    host = parser.get(SECTION, "host", fallback="").strip()
    app = parser.get(SECTION, "app", fallback="Desktop").strip()
    return StreamSettings(
        autostart=autostart,
        host=host if host.isprintable() and len(host) <= APP_MAX else "",
        app=app if valid_app(app) else "Desktop",
    )


def save_settings(settings: StreamSettings, path: pathlib.Path = CONFIG) -> None:
    if not valid_app(settings.app):
        raise ValueError("the application name is empty, too long, or starts with '-'")
    if not settings.host.isprintable() or len(settings.host) > APP_MAX:
        raise ValueError("the host is not valid")
    body = [
        f"[{SECTION}]\n",
        f"autostart = {'true' if settings.autostart else 'false'}\n",
        f"host = {settings.host}\n",
        f"app = {settings.app}\n",
    ]
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        lines = []
    header = re.compile(r"\s*\[([^]]+)]\s*\r?\n?")
    start = end = None
    for index, line in enumerate(lines):
        match = header.fullmatch(line)
        if match and match.group(1).strip().lower() == SECTION:
            start = index
            end = next((n for n in range(index + 1, len(lines)) if header.fullmatch(lines[n])), len(lines))
            break
    if start is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        updated = lines + (["\n"] if lines and lines[-1].strip() else []) + body
    else:
        updated = lines[:start] + body + (["\n"] if end < len(lines) else []) + lines[end:]
    _atomic_write(path, "".join(updated).encode("utf-8"))


def write_stream_request(host: str, app: str, path: pathlib.Path = STREAM_REQUEST) -> None:
    """Ask couchliteos-run-app to start `moonlight stream HOST APP` (two plain lines)."""
    if not valid_host(host):
        raise ValueError("the PC's address is not usable as a stream target")
    if not valid_app(app):
        raise ValueError("the application name is not usable")
    _atomic_write(path, f"{host}\n{app}\n".encode("utf-8"))


def take_request(path: pathlib.Path = STREAM_REQUEST) -> tuple[str, str] | None:
    """Read and delete a stream request; (host, app) only if it is fresh and well formed.

    couchliteos-run-app passes the two values to `moonlight stream` as plain arguments,
    so anything that is not a plain host name/address and a printable name is refused.
    """
    try:
        info = path.lstat()
    except OSError:
        return None
    try:
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or time.time() - info.st_mtime > REQUEST_MAX_AGE:
            return None
        lines = path.read_bytes().decode("utf-8").split("\n")
    except (OSError, UnicodeError):
        return None
    finally:
        if not stat.S_ISDIR(info.st_mode):
            try:
                path.unlink()
            except OSError:
                pass
    if len(lines) != 3 or lines[2] or not valid_host(lines[0]) or not valid_app(lines[1]):
        return None
    return lines[0], lines[1]


# ------------------------------------------------------------------ Wake-on-LAN


def magic_packet(mac: bytes) -> bytes:
    if len(mac) != 6:
        raise ValueError("a MAC address is six bytes")
    return b"\xff" * 6 + mac * 16


def subnet_broadcasts(output: str) -> list[str]:
    """Directed-broadcast addresses from `ip -brief -4 address show up` output."""
    found: list[str] = []
    for line in output.splitlines():
        for item in line.split()[2:]:
            try:
                interface = ipaddress.ip_interface(item)
            except ValueError:
                continue
            if interface.version != 4 or interface.ip.is_loopback or interface.network.prefixlen >= 31:
                continue
            broadcast = str(interface.network.broadcast_address)
            if broadcast not in found:
                found.append(broadcast)
    return found


def wake_targets(host: Host, broadcasts: list[str]) -> list[str]:
    literals = []
    for address, _port_number in host.lan_addresses():
        try:
            if ipaddress.ip_address(address).version == 4:
                literals.append(address)
        except ValueError:
            pass
    targets = ["255.255.255.255", *broadcasts]
    if not broadcasts and literals:
        # No interface data: the host's own /24 is the best guess.
        targets.append(str(ipaddress.ip_network(f"{literals[0]}/24", strict=False).broadcast_address))
    targets.extend(literals)
    return list(dict.fromkeys(targets))


def send_wol(
    mac: bytes, targets: list[str], *, socket_factory: Callable[..., socket.socket] = socket.socket
) -> int:
    """Send the magic packet to every target on ports 9 and 7; return datagrams sent."""
    packet = magic_packet(mac)
    sender = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
    sent = 0
    try:
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in targets:
            for port in WOL_PORTS:
                try:
                    sender.sendto(packet, (target, port))
                    sent += 1
                except OSError:
                    pass
    finally:
        sender.close()
    return sent


def send_magic(host: Host) -> int:
    try:
        output = subprocess.run(
            ["ip", "-brief", "-4", "address", "show", "up"],
            text=True, capture_output=True, check=False, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        output = ""
    return send_wol(host.mac, wake_targets(host, subnet_broadcasts(output)))


def probe(
    host: Host,
    *,
    connect: Callable[..., socket.socket] = socket.create_connection,
    timeout: float = PROBE_TIMEOUT,
) -> str:
    """"up" (Sunshine answers), "awake" (the PC refuses the port), "down", or "unknown"."""
    addresses = host.lan_addresses()
    if not addresses:
        return "unknown"
    awake = False
    for address, port in addresses:
        try:
            connection = connect((address, port), timeout=timeout)
        except ConnectionRefusedError:
            awake = True
            continue
        except OSError:
            continue
        try:
            connection.close()
        except OSError:
            pass
        return "up"
    return "awake" if awake else "down"


def wake_and_wait(
    host: Host,
    *,
    force: bool = False,
    tick: Callable[[float], object] | None = None,
    probe_fn: Callable[[Host], str] = probe,
    send: Callable[[Host], object] = send_magic,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    timeout: float = WAKE_TIMEOUT,
) -> str:
    """Wake `host` if it is not answering and wait for it.

    Returns "up" (nothing to do), "nomac", "noaddr" (no LAN address to watch; only
    with force is a packet sent, then "sent"), "woke", "timeout", or "cancelled"
    (`tick(elapsed)` returned true).
    """
    state = probe_fn(host)
    if state in ("up", "awake"):
        return "up"
    if not host.mac:
        return "nomac"
    if state == "unknown" and not force:
        return "noaddr"
    send(host)
    if state == "unknown":
        return "sent"
    start = last_send = clock()
    while True:
        elapsed = clock() - start
        if tick is not None and tick(elapsed):
            return "cancelled"
        if elapsed >= timeout:
            return "timeout"
        sleep(POLL_SECONDS)
        if probe_fn(host) == "up":
            return "woke"
        if clock() - last_send >= RESEND_SECONDS:
            send(host)
            last_send = clock()


# ------------------------------------------------------------------ stream tuning

MIN_BITRATE = 500  # kbps; Moonlight's command-line range is 500..500000
MAX_BITRATE = 500000
LINK_FRACTION_PERMILLE = 600  # cap the stream at 60% of the measured link rate
MAX_PIXELS = 3840 * 2160
BITRATE_TABLE = (
    (640 * 360, 1), (854 * 480, 2), (1280 * 720, 5),
    (1920 * 1080, 10), (2560 * 1440, 20), (3840 * 2160, 40),
)


def default_bitrate(width: int, height: int, fps: int, yuv444: bool = False) -> int:
    """Moonlight Qt's StreamingPreferences::getDefaultBitrate, in kbps."""
    rate_factor = (fps if fps <= 60 else math.sqrt(fps / 60) * 60) / 30
    pixels = width * height
    if pixels <= BITRATE_TABLE[0][0]:
        factor = float(BITRATE_TABLE[0][1])
    elif pixels >= BITRATE_TABLE[-1][0]:
        factor = float(BITRATE_TABLE[-1][1])
    else:
        factor = 0.0
        for (low_pixels, low), (high_pixels, high) in zip(BITRATE_TABLE, BITRATE_TABLE[1:]):
            if pixels <= high_pixels:
                factor = (pixels - low_pixels) / (high_pixels - low_pixels) * (high - low) + low
                break
    if yuv444:
        factor *= 2
    return int(factor * rate_factor + 0.5) * 1000


def choose_bitrate(width: int, height: int, fps: int, link_mbps: float | None) -> int:
    bitrate = default_bitrate(width, height, fps)
    if link_mbps is not None:
        bitrate = min(bitrate, math.floor(link_mbps * LINK_FRACTION_PERMILLE + 1e-9))
    return max(MIN_BITRATE, min(bitrate, MAX_BITRATE))


@dataclasses.dataclass(frozen=True)
class Network:
    link_mbps: float | None = None
    link_kind: str = "unknown"  # "wired", "wifi", or "unknown"
    device: str = ""
    rtt_ms: float | None = None
    loss_pct: float | None = None


@dataclasses.dataclass(frozen=True)
class Plan:
    width: int
    height: int
    fps: int
    bitrate: int
    default_bitrate: int
    capped: bool


def plan_settings(width: int, height: int, refresh_mhz: int, network: Network) -> Plan:
    fps = min(max(round(refresh_mhz / 1000), 24), 240)
    if width * height > MAX_PIXELS:
        scale = math.sqrt(MAX_PIXELS / (width * height))
        width, height = int(width * scale), int(height * scale)
    width, height = width - width % 2, height - height % 2
    bitrate = choose_bitrate(width, height, fps, network.link_mbps)
    default = default_bitrate(width, height, fps)
    return Plan(width, height, fps, bitrate, default, bitrate < default)


def summary_lines(mode: str, plan: Plan, network: Network) -> list[str]:
    """What was measured and what was written, for the result screen."""
    if network.link_mbps is None:
        link = "LINK SPEED UNKNOWN"
    else:
        link = f"{'WIRED' if network.link_kind == 'wired' else 'WI-FI'} {network.link_mbps:g} MBIT/S"
    ping = "PING NOT MEASURED" if network.rtt_ms is None else f"PING {network.rtt_ms:.1f} MS"
    if network.loss_pct is not None:
        ping += f"  LOSS {network.loss_pct:g}%"
    bitrate = f"BITRATE {plan.bitrate / 1000:g} MBPS"
    if plan.capped:
        bitrate += f"  (CAPPED AT 60% OF THE LINK; MOONLIGHT'S DEFAULT IS {plan.default_bitrate / 1000:g} MBPS)"
    lines = [
        f"DISPLAY {mode}",
        f"NETWORK {link}",
        ping,
        f"STREAM {plan.width}x{plan.height} AT {plan.fps} FPS",
        bitrate,
    ]
    if network.loss_pct is not None and network.loss_pct >= 1:
        lines.append(f"WARNING: {network.loss_pct:g}% PACKET LOSS; A WIRED CONNECTION WORKS BEST")
    return lines


def parse_ping(output: str) -> tuple[float | None, float | None]:
    """(average round-trip ms, packet loss percent) from iputils ping output."""
    rtt = re.search(r"=\s*[\d.]+/([\d.]+)/", output)
    loss = re.search(r"([\d.]+)% packet loss", output)
    return (float(rtt.group(1)) if rtt else None, float(loss.group(1)) if loss else None)


def parse_wifi_rate(output: str) -> float | None:
    """The in-use access point's rate from `nmcli -t -f IN-USE,RATE dev wifi list`."""
    for line in output.splitlines():
        if line.startswith("*:"):
            match = re.search(r"(\d+(?:\.\d+)?)\s*Mbit/s", line)
            if match:
                return float(match.group(1))
    return None


def measure_network(
    address: str | None,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sysfs: pathlib.Path = pathlib.Path("/sys/class/net"),
) -> Network:
    """Link rate of the interface that reaches `address`, plus ping round trip and loss."""
    environment = {**os.environ, "LC_ALL": "C"}

    def output(command: list[str], timeout: int = 3) -> str:
        try:
            result = run(
                command, text=True, capture_output=True, check=False, timeout=timeout, env=environment
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout or ""

    if address is not None and not valid_host(address):
        address = None
    match = address and re.search(r"\bdev (\S+)", output(["ip", "-o", "route", "get", address]))
    if not match:
        match = re.search(r"\bdev (\S+)", output(["ip", "-o", "route", "show", "default"]))
    device = match.group(1) if match and re.fullmatch(r"[A-Za-z0-9._-]+", match.group(1)) else ""

    link_mbps: float | None = None
    kind = "unknown"
    if device:
        if (sysfs / device / "wireless").is_dir():
            kind = "wifi"
            link_mbps = parse_wifi_rate(
                output(["nmcli", "-t", "-f", "IN-USE,RATE", "dev", "wifi", "list", "ifname", device, "--rescan", "no"])
            )
        else:
            kind = "wired"
            try:
                speed = int((sysfs / device / "speed").read_text().strip())
            except (OSError, ValueError):
                speed = 0
            link_mbps = float(speed) if speed > 0 else None

    rtt = loss = None
    if address:
        rtt, loss = parse_ping(output(["ping", "-c", "10", "-i", "0.2", "-W", "1", "-q", address], 9))
    return Network(link_mbps, kind, device, rtt, loss)


def moonlight_running(run_dir: pathlib.Path = RUN) -> bool:
    if (run_dir / "moonlight-ready").exists():
        return True
    try:
        return (run_dir / "app-active").read_text(encoding="ascii").strip() == "moonlight"
    except (OSError, UnicodeError):
        return False


def rewrite_conf(text: str, values: dict[str, str]) -> str:
    """Set keys in the [General] section, leaving every other byte of the file alone."""
    lines = text.splitlines(keepends=True)
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    new_lines = {key: f"{key}={value}" for key, value in sorted(values.items())}

    def section(line: str) -> str | None:
        stripped = line.strip()
        return stripped[1:-1].strip() if stripped.startswith("[") and stripped.endswith("]") else None

    start = next((i for i, line in enumerate(lines) if section(line) == "General"), None)
    if start is None:
        block = [f"[General]{eol}", *(f"{line}{eol}" for line in new_lines.values())]
        return "".join(block + ([eol] if lines else []) + lines)
    end = next((i for i in range(start + 1, len(lines)) if section(lines[i]) is not None), len(lines))
    missing = dict(new_lines)
    for index in range(start + 1, end):
        key = lines[index].split("=", 1)[0].strip() if "=" in lines[index] else None
        if key in new_lines:
            ending = lines[index][len(lines[index].rstrip("\r\n")):]
            lines[index] = new_lines[key] + ending
            missing.pop(key, None)
    if missing:
        last = max((i for i in range(start, end) if lines[i].strip()), default=start)
        if not lines[last].endswith("\n"):
            lines[last] += eol
        lines[last + 1:last + 1] = [f"{line}{eol}" for line in missing.values()]
    return "".join(lines)


def apply_plan(plan: Plan, conf: pathlib.Path = MOONLIGHT_CONF, run_dir: pathlib.Path = RUN) -> None:
    """Write the planned resolution, frame rate, and bitrate into Moonlight.conf."""
    if moonlight_running(run_dir):
        raise StreamError("CLOSE MOONLIGHT FIRST; IT WOULD OVERWRITE THE NEW SETTINGS WHEN IT EXITS")
    values = {
        "width": str(plan.width), "height": str(plan.height),
        "fps": str(plan.fps), "bitrate": str(plan.bitrate),
    }
    if plan.bitrate > 100000:
        values["unlockbitrate"] = "true"  # Moonlight's slider stops at 100 Mbps unless unlocked
    try:
        original = conf.read_bytes().decode("latin-1")
    except FileNotFoundError:
        original = ""
    updated = rewrite_conf(original, values)
    if updated != original:
        _atomic_write(conf, updated.encode("latin-1"), 0o644)


def main(argv: list[str]) -> int:
    """`take-request [PATH]`: print the validated host and app for couchliteos-run-app."""
    if argv[:1] != ["take-request"] or len(argv) > 2:
        print("usage: couchliteos_stream.py take-request [PATH]", file=sys.stderr)
        return 2
    request = take_request(pathlib.Path(argv[1]) if len(argv) == 2 else STREAM_REQUEST)
    if request is None:
        return 1
    print(*request, sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
