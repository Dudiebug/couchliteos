#!/usr/bin/python3
"""Couch-to-game helpers: Wake-on-LAN, paired Moonlight hosts, auto-stream, stream tuning,
per-game stream presets.

Moonlight Qt keeps its paired hosts and stream preferences in a QSettings INI file
(`Moonlight.conf`). This module only reads it, except for the stream keys (PLAN_KEYS) that
`apply_plan` and `restore_global` rewrite, line by line, while Moonlight is not running.
"""

from __future__ import annotations

import configparser
import dataclasses
import ipaddress
import json
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


DATA = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos"))
MOONLIGHT_CONF = DATA / "home/.config/Moonlight Game Streaming Project/Moonlight.conf"
CONFIG = DATA / "config.ini"
RUN = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos"))
HARDWARE_ENV = pathlib.Path(os.environ.get("COUCHLITEOS_HARDWARE_ENV", "/run/couchliteos-hardware/hardware.env"))
STREAM_REQUEST = RUN / "moonlight-stream.request"
SECTION = "streaming"

HTTP_PORT = 47989  # Sunshine / GameStream HTTP port; Moonlight's default
WOL_PORTS = (9, 7)
PROBE_TIMEOUT = 1.0
WAKE_TIMEOUT = 90.0  # a PC that was fully off needs to boot, log in and start Sunshine
POLL_SECONDS = 0.5
RESEND_SECONDS = 5.0

HOST_RE = re.compile(r"[A-Za-z0-9._:-]{1,253}")
APP_MAX = 128
MAC_TEXT_RE = re.compile(r"[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}")


# A stream request older than this is a leftover, not a request. It must outlast the wake wait:
# the launcher writes the request first and may then wait up to WAKE_TIMEOUT for the PC.
REQUEST_MAX_AGE = 180.0
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
    # (app name, Sunshine's app id) for the box art Moonlight caches per id; not part of equality.
    app_ids: tuple[tuple[str, int], ...] = dataclasses.field(default=(), compare=False)

    def app_id(self, app: str) -> int | None:
        return next((number for name, number in self.app_ids if name == app), None)

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
    app_ids: list[tuple[str, int]] = []
    for index in range(1, app_count + 1):
        name = as_text(fields.get(f"apps\\{index}\\name", "")).strip()
        if name and fields.get(f"apps\\{index}\\hidden", "false") != "true":
            apps.append(name)
            try:
                app_ids.append((name, int(fields.get(f"apps\\{index}\\id", ""))))
            except ValueError:
                pass
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
        app_ids=tuple(app_ids),
    )


def parse_hosts(text: str) -> list[Host]:
    """Paired hosts from Moonlight.conf text, read the way Moonlight reads them.

    Moonlight (QSettings) writes each array in its own section: `[hosts]` holds
    `1\\hostname=...` and `size=N`, and `[hostsbackup]` is the same. A `hosts\\1\\hostname`
    key in [General] is read too. Moonlight prefers `hostsbackup` when it is non-empty.
    """
    arrays: dict[str, dict[int, dict[str, str]]] = {"hosts": {}, "hostsbackup": {}}
    sizes: dict[str, int] = {}
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in ";#":
            continue
        if line[0] == "[" and line[-1] == "]":
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if section in arrays:
            name, rest = section, key
        elif section == "General":
            name, _, rest = key.partition("\\")
            if name not in arrays:
                continue
        else:
            continue
        if rest == "size":
            try:
                sizes[name] = int(decode_value(raw))
            except ValueError:
                pass
            continue
        index, _, field = rest.partition("\\")
        if index.isdigit() and field:
            arrays[name].setdefault(int(index), {})[field] = decode_value(raw)
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
    _write_section(path, SECTION, body)


def _write_section(path: pathlib.Path, section: str, body: list[str]) -> None:
    """Replace config.ini's [section] with `body` (its header line first; empty drops it), keeping the rest."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        lines = []
    header = re.compile(r"\s*\[([^]]+)]\s*\r?\n?")
    start = end = None
    for index, line in enumerate(lines):
        match = header.fullmatch(line)
        if match and match.group(1).strip().lower() == section:
            start = index
            end = next((n for n in range(index + 1, len(lines)) if header.fullmatch(lines[n])), len(lines))
            break
    if start is None:
        if not body:
            return
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        updated = lines + (["\n"] if lines and lines[-1].strip() else []) + body
    else:
        updated = lines[:start] + body + (["\n"] if body and end < len(lines) else []) + lines[end:]
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

    Returns "up" (nothing to do), "awake" (the PC is on but Sunshine is not answering,
    at once or when the wait ran out), "nomac", "noaddr" (no LAN address to watch; only
    with force is a packet sent, then "sent"), "woke", "timeout", or "cancelled"
    (`tick(elapsed)` returned true).
    """
    state = probe_fn(host)
    if state in ("up", "awake"):
        return state
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
            return "awake" if state == "awake" else "timeout"
        sleep(POLL_SECONDS)
        state = probe_fn(host)
        if state == "up":
            return "woke"
        if clock() - last_send >= RESEND_SECONDS:
            send(host)
            last_send = clock()


# ------------------------------------------------------------------ stream tuning

MIN_BITRATE = 500  # kbps; Moonlight's command-line range is 500..500000
MAX_BITRATE = 500000
LINK_FRACTION_PERMILLE = 600  # cap the stream at 60% of the measured link rate
MAX_PIXELS = 3840 * 2160
# Software decoding (nouveau, a weak iGPU) cannot keep up beyond 1080p60.
SOFTWARE_MAX_PIXELS = 1920 * 1080
SOFTWARE_MAX_FPS = 60
SOFTWARE_MAX_BITRATE = 20000  # kbps; Moonlight's own default for 1080p60
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
    pc_asleep: bool = False  # the PC did not answer, so it was not pinged


@dataclasses.dataclass(frozen=True)
class Plan:
    width: int
    height: int
    fps: int
    bitrate: int
    default_bitrate: int
    capped: bool
    decode_limited: bool = False  # software decoding forced the mode below the display's
    hdr: bool | None = None  # None leaves Moonlight's own HDR setting alone


def video_decode(
    hardware_env: pathlib.Path = HARDWARE_ENV,
    config: pathlib.Path = CONFIG,
    moonlight_conf: pathlib.Path = MOONLIGHT_CONF,
) -> str:
    """"software", "hardware" or "auto": how this PC decodes the stream, as couchliteos-run-app decides.

    A decoder set in config.ini [moonlight] wins; else hwdetect's COUCHLITEOS_VIDEO_DECODE
    hint (nouveau has no usable decoder); else software chosen inside Moonlight (videodec=2)."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config, encoding="utf-8")
        configured = parser.get("moonlight", "decoder", fallback="auto").strip()
    except (OSError, ValueError, configparser.Error):  # ValueError: not UTF-8
        configured = "auto"
    if configured in ("software", "hardware"):
        return configured
    try:
        hint = re.findall(r"(?m)^COUCHLITEOS_VIDEO_DECODE=(.*)$", hardware_env.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        hint = []
    if hint and hint[-1].strip() == "software":
        return "software"
    try:
        chosen = re.findall(r"(?m)^videodec=(\d+)\s*$", moonlight_conf.read_bytes().decode("latin-1"))
    except OSError:
        chosen = []
    return "software" if chosen and chosen[-1] == "2" else "auto"


def plan_settings(width: int, height: int, refresh_mhz: int, network: Network, decode: str = "auto") -> Plan:
    fps = min(max(round(refresh_mhz / 1000), 24), 240)
    max_pixels, max_fps = MAX_PIXELS, 240
    if decode == "software":
        max_pixels, max_fps = SOFTWARE_MAX_PIXELS, SOFTWARE_MAX_FPS
    limited = width * height > max_pixels or fps > max_fps
    fps = min(fps, max_fps)
    if width * height > max_pixels:
        scale = math.sqrt(max_pixels / (width * height))
        width, height = int(width * scale), int(height * scale)
    width, height = width - width % 2, height - height % 2
    bitrate = choose_bitrate(width, height, fps, network.link_mbps)
    default = default_bitrate(width, height, fps)
    if decode == "software":
        bitrate = min(bitrate, SOFTWARE_MAX_BITRATE)
    return Plan(width, height, fps, bitrate, default, bitrate < default, limited and decode == "software")


def summary_lines(mode: str, plan: Plan, network: Network) -> list[str]:
    """What was measured and what was written, for the result screen."""
    if network.link_mbps is None:
        link = "LINK SPEED UNKNOWN"
    else:
        link = f"{'WIRED' if network.link_kind == 'wired' else 'WI-FI'} {network.link_mbps:g} MBIT/S"
    # No reply at all means a sleeping PC or one that drops pings; only replies say anything about the network.
    replies = network.rtt_ms is not None
    ping = f"PING {network.rtt_ms:.1f} MS" if replies else "PING NOT MEASURED"
    if network.pc_asleep:
        ping = "PC NOT ANSWERING: PING NOT MEASURED"
    if replies and network.loss_pct is not None:
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
    if plan.decode_limited:
        lines.append("SOFTWARE VIDEO DECODING: STREAM LIMITED TO 1920x1080 AT 60 FPS")
    if network.pc_asleep:
        lines.append("WAKE THE PC AND OPTIMIZE AGAIN TO MEASURE THE PING")
    if replies and network.loss_pct is not None and network.loss_pct >= 1:
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
    pc_asleep: bool = False,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sysfs: pathlib.Path = pathlib.Path("/sys/class/net"),
) -> Network:
    """Link rate of the interface that reaches `address`, plus ping round trip and loss.

    A `pc_asleep` PC is not pinged: every probe would be lost and say nothing about the network."""
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
    if address and not pc_asleep:
        rtt, loss = parse_ping(output(["ping", "-c", "10", "-i", "0.2", "-W", "1", "-q", address], 9))
    return Network(link_mbps, kind, device, rtt, loss, pc_asleep)


def moonlight_running(run_dir: pathlib.Path = RUN) -> bool:
    if (run_dir / "moonlight-ready").exists():
        return True
    try:
        return (run_dir / "app-active").read_text(encoding="ascii").strip() == "moonlight"
    except (OSError, UnicodeError):
        return False


def rewrite_conf(text: str, values: dict[str, str | None]) -> str:
    """Set keys in the [General] section (None removes one), leaving every other byte of the file alone."""
    lines = text.splitlines(keepends=True)
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    new_lines = {key: f"{key}={value}" for key, value in sorted(values.items()) if value is not None}
    removed = {key for key, value in values.items() if value is None}

    def section(line: str) -> str | None:
        stripped = line.strip()
        return stripped[1:-1].strip() if stripped.startswith("[") and stripped.endswith("]") else None

    start = next((i for i, line in enumerate(lines) if section(line) == "General"), None)
    if start is not None and removed:
        end = next((i for i in range(start + 1, len(lines)) if section(lines[i]) is not None), len(lines))
        lines = lines[:start + 1] + [
            line for line in lines[start + 1:end]
            if "=" not in line or line.split("=", 1)[0].strip() not in removed
        ] + lines[end:]
    if start is None:
        if not new_lines:
            return text
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
    try:
        original = conf.read_bytes().decode("latin-1")
    except FileNotFoundError:
        original = ""
    updated = rewrite_conf(original, plan_values(plan))
    if updated != original:
        _atomic_write(conf, updated.encode("latin-1"), 0o644)


def plan_values(plan: Plan) -> dict[str, str | None]:
    """The Moonlight.conf [General] keys a plan sets."""
    values: dict[str, str | None] = {
        "width": str(plan.width), "height": str(plan.height),
        "fps": str(plan.fps), "bitrate": str(plan.bitrate),
    }
    if plan.bitrate > 100000:
        values["unlockbitrate"] = "true"  # Moonlight's slider stops at 100 Mbps unless unlocked
    if plan.hdr is not None:
        values["hdr"] = "true" if plan.hdr else "false"
    return values


SMOOTHER_FLOOR = 5000  # kbps; below this the picture gets worse than the stutter it cures


def general_values(text: str) -> dict[str, str]:
    """key=value pairs of Moonlight.conf's [General] section."""
    values: dict[str, str] = {}
    in_general = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_general = line[1:-1].strip() == "General"
        elif in_general and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def current_bitrate(values: dict[str, str]) -> int:
    """The bitrate Moonlight streams at: the saved one, else its default for the saved mode."""
    def number(key: str, default: int) -> int:
        try:
            value = int(values.get(key, ""))
        except ValueError:
            return default
        return value if value > 0 else default

    bitrate = number("bitrate", 0)
    if MIN_BITRATE <= bitrate <= MAX_BITRATE:
        return bitrate
    return default_bitrate(number("width", 1920), number("height", 1080), number("fps", 60))


def step_down(kbps: int) -> int | None:
    """A quarter less, rounded down to 0.5 Mbps, never below the floor; None when already there."""
    if kbps <= SMOOTHER_FLOOR:
        return None
    return max(SMOOTHER_FLOOR, kbps * 3 // 4 // 500 * 500)


def lower_bitrate(conf: pathlib.Path = MOONLIGHT_CONF, run_dir: pathlib.Path = RUN) -> tuple[int, int | None]:
    """Lower only Moonlight's bitrate one step: (old, new), new None when already at the floor."""
    if moonlight_running(run_dir):
        raise StreamError("CLOSE MOONLIGHT FIRST; IT WOULD OVERWRITE THE NEW SETTINGS WHEN IT EXITS")
    try:
        original = conf.read_bytes().decode("latin-1")
    except FileNotFoundError:
        original = ""
    old = current_bitrate(general_values(original))
    new = step_down(old)
    if new is not None:
        _atomic_write(conf, rewrite_conf(original, {"bitrate": str(new)}).encode("latin-1"), 0o644)
    return old, new


# ------------------------------------------------------------- per-game presets

# A game's own stream settings live in config.ini [stream-games] as `<pc>/<app> = PRESET` or
# `= CUSTOM 1920x1080 60 20000 hdr`. Before Moonlight starts, the settings a game's plan replaced
# in Moonlight.conf are put back (GLOBAL_SAVED), then the next game's plan is written: one game's
# plan never carries over to the next game, or to Moonlight opened from its tile.
GAMES_SECTION = "stream-games"
GLOBAL_SAVED = DATA / "stream-global.json"
DEFAULT, PERFORMANCE, BALANCED, QUALITY, CUSTOM = "DEFAULT", "PERFORMANCE", "BALANCED", "QUALITY", "CUSTOM"
PRESETS = (PERFORMANCE, BALANCED, QUALITY)
CHOICES = (DEFAULT, *PRESETS, CUSTOM)  # DEFAULT: no settings of its own (Settings > STREAMING)
# PERFORMANCE streams 720p, or 1080p on a display above 1080p with a hardware decoder: fewer pixels to
# encode and decode is less latency.
PERFORMANCE_HEIGHTS = (720, 1080)
PERFORMANCE_FPS = 120
QUALITY_FPS = 60
QUALITY_BITRATE_PERCENT = 150  # of Moonlight's default for the mode
CUSTOM_RESOLUTIONS = ((1280, 720), (1920, 1080), (2560, 1440), (3840, 2160))
CUSTOM_FPS = (30, 60, 90, 120)
CUSTOM_BITRATES = (0, 5000, 10000, 20000, 30000, 50000, 80000, 150000)  # kbps; 0: Moonlight's default
PLAN_KEYS = ("width", "height", "fps", "bitrate", "unlockbitrate", "hdr")  # what a plan may write
SAVED_VALUE_RE = re.compile(r"[A-Za-z0-9.]{0,16}")
KEY_SAFE_RE = re.compile(r"[^A-Za-z0-9 ._()!&'+,@-]")
Mode = tuple[int, int, int]  # width, height, refresh in mHz: the display mode the stream fits


@dataclasses.dataclass(frozen=True)
class GameSettings:
    preset: str = BALANCED  # PERFORMANCE, BALANCED, QUALITY or CUSTOM
    width: int = 1920  # the CUSTOM fields
    height: int = 1080
    fps: int = 60
    bitrate: int = 0  # kbps; 0 is Moonlight's default for the mode
    hdr: bool = False

    def text(self) -> str:
        """The config.ini value."""
        if self.preset != CUSTOM:
            return self.preset
        return f"{CUSTOM} {self.width}x{self.height} {self.fps} {self.bitrate} {'hdr' if self.hdr else 'sdr'}"


def parse_game(text: str) -> GameSettings | None:
    words = text.split()
    if len(words) == 1 and words[0].upper() in PRESETS:
        return GameSettings(words[0].upper())
    match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", words[1]) if len(words) == 5 and words[0].upper() == CUSTOM else None
    if match is None or not words[2].isdigit() or not words[3].isdigit() or words[4] not in ("hdr", "sdr"):
        return None
    bitrate = int(words[3])
    if not 24 <= int(words[2]) <= 240 or (bitrate and not MIN_BITRATE <= bitrate <= MAX_BITRATE):
        return None
    return GameSettings(CUSTOM, int(match.group(1)), int(match.group(2)), int(words[2]), bitrate, words[4] == "hdr")


def game_key(host_key: str, app: str) -> str:
    """`<pc>/<app>` with what config.ini cannot hold in a key (= : % [ ...) written as %XX."""
    def escape(text: str) -> str:
        return KEY_SAFE_RE.sub(lambda match: "".join(f"%{byte:02X}" for byte in match.group().encode()), text)
    return f"{escape(host_key)}/{escape(app)}"


def _games_parser(path: pathlib.Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keys keep their case
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, UnicodeError, configparser.Error):
        pass
    return parser


def game_settings(host_key: str, app: str, path: pathlib.Path = CONFIG) -> GameSettings | None:
    """The game's own stream settings, None when it uses the global ones."""
    parser = _games_parser(path)
    if not parser.has_section(GAMES_SECTION):
        return None
    return parse_game(parser.get(GAMES_SECTION, game_key(host_key, app), fallback=""))


def save_game(host_key: str, app: str, settings: GameSettings | None, path: pathlib.Path = CONFIG) -> None:
    """Save a game's own settings; None goes back to the global ones."""
    parser = _games_parser(path)
    games = dict(parser.items(GAMES_SECTION)) if parser.has_section(GAMES_SECTION) else {}
    key = game_key(host_key, app)
    games.pop(key, None)
    if settings is not None:
        if parse_game(settings.text()) != settings:
            raise ValueError("the stream settings are not valid")
        games[key] = settings.text()
    body = [f"[{GAMES_SECTION}]\n", *(f"{name} = {value}\n" for name, value in games.items())] if games else []
    _write_section(path, GAMES_SECTION, body)


def _fit(width: int, height: int, max_width: int, max_height: int, max_pixels: int) -> tuple[int, int]:
    """`width` x `height` scaled down (aspect kept, even sizes) to fit the box and the pixel count."""
    scale = min(1.0, max_width / width, max_height / height, math.sqrt(max_pixels / (width * height)))
    width, height = int(width * scale + 1e-9), int(height * scale + 1e-9)
    return width - width % 2, height - height % 2


def _limits(mode: Mode, decode: str) -> tuple[int, int]:
    """(most pixels, highest fps) a stream may use on this display with this decoder."""
    width, height, refresh_mhz = mode
    display_fps = min(max(round(refresh_mhz / 1000), 24), 240)
    if decode == "software":
        return min(width * height, SOFTWARE_MAX_PIXELS), min(display_fps, SOFTWARE_MAX_FPS)
    return min(width * height, MAX_PIXELS), display_fps


def _plan(width: int, height: int, fps: int, wanted: int, mode: Mode, decode: str, link_mbps: float | None,
          hdr: bool | None) -> Plan:
    default = default_bitrate(width, height, fps)
    bitrate = wanted
    if link_mbps is not None:
        bitrate = min(bitrate, math.floor(link_mbps * LINK_FRACTION_PERMILLE + 1e-9))
    if decode == "software":
        bitrate = min(bitrate, SOFTWARE_MAX_BITRATE)
    bitrate = max(MIN_BITRATE, min(bitrate, MAX_BITRATE))
    limited = decode == "software" and (width * height < mode[0] * mode[1] or fps < round(mode[2] / 1000))
    return Plan(width, height, fps, bitrate, default, bitrate < min(wanted, MAX_BITRATE), limited,
                hdr and decode != "software" if hdr is not None else None)


def preset_plan(preset: str, mode: Mode, decode: str = "auto", link_mbps: float | None = None) -> Plan:
    """A preset on this display and decoder: never a mode or rate either cannot do.

    PERFORMANCE: 720p (1080p on a bigger display with a hardware decoder), up to 120 fps, SDR.
    BALANCED: what OPTIMIZE STREAM SETTINGS picks (the display's mode, Moonlight's bitrate).
    QUALITY: the display's full resolution, 60 fps, a higher bitrate, HDR with a hardware decoder."""
    width, height, _refresh = mode
    if preset == BALANCED:
        return plan_settings(*mode, Network(link_mbps), decode)
    max_pixels, max_fps = _limits(mode, decode)
    if preset == PERFORMANCE:
        cap = PERFORMANCE_HEIGHTS[decode != "software" and height > PERFORMANCE_HEIGHTS[1]]
        size = _fit(width, height, width, cap, max_pixels)
        fps = min(PERFORMANCE_FPS, max_fps)
        return _plan(*size, fps, default_bitrate(*size, fps), mode, decode, link_mbps, False)
    if preset == QUALITY:
        size = _fit(width, height, width, height, max_pixels)
        fps = min(QUALITY_FPS, max_fps)
        wanted = default_bitrate(*size, fps) * QUALITY_BITRATE_PERCENT // 100
        return _plan(*size, fps, wanted, mode, decode, link_mbps, True)
    raise ValueError(f"unknown preset {preset}")


def game_plan(settings: GameSettings, mode: Mode | None, decode: str = "auto",
              link_mbps: float | None = None) -> Plan | None:
    """The plan for a game's own settings; None when a preset needs the display mode and there is none."""
    if settings.preset != CUSTOM:
        return preset_plan(settings.preset, mode, decode, link_mbps) if mode is not None else None
    mode = mode or (settings.width, settings.height, settings.fps * 1000)
    max_pixels, max_fps = _limits(mode, decode)
    size = _fit(settings.width, settings.height, mode[0], mode[1], max_pixels)
    fps = min(settings.fps, max_fps)
    return _plan(*size, fps, settings.bitrate or default_bitrate(*size, fps), mode, decode, link_mbps, settings.hdr)


def plan_for(
    host_key: str, app: str, *, mode: Callable[[], Mode | None], decode: Callable[[], str] = lambda: video_decode(),
    config: pathlib.Path = CONFIG,
) -> Plan | None:
    """This game's plan; None for the global settings (Moonlight.conf as Settings > STREAMING left it).

    `mode` and `decode` are asked only when the game has settings of its own."""
    settings = game_settings(host_key, app, config)
    return game_plan(settings, mode(), decode()) if settings is not None else None


def describe(plan: Plan) -> str:
    text = f"{plan.width}x{plan.height} AT {plan.fps} FPS, {plan.bitrate / 1000:g} MBPS"
    return text + (", HDR" if plan.hdr else "")


def _read_saved(saved: pathlib.Path) -> dict | None:
    try:
        data = json.loads(saved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    for name in ("saved", "written"):
        values = data.get(name)
        if not isinstance(values, dict) or not all(
            key in PLAN_KEYS and (value is None or (isinstance(value, str) and SAVED_VALUE_RE.fullmatch(value)))
            for key, value in values.items()
        ):
            return {}
    return data




def restore_global(conf: pathlib.Path = MOONLIGHT_CONF, saved: pathlib.Path = GLOBAL_SAVED,
                   run_dir: pathlib.Path = RUN) -> bool:
    """Put back the Moonlight.conf values the last game's plan replaced. A value changed since
    (OPTIMIZE, SMOOTHER STREAM, Moonlight's own settings) is kept. True when anything was put back."""
    data = _read_saved(saved)
    if data is None:
        return False
    if moonlight_running(run_dir):
        raise StreamError("CLOSE MOONLIGHT FIRST; IT WOULD OVERWRITE THE NEW SETTINGS WHEN IT EXITS")
    changed = False
    if data:
        try:
            original = conf.read_bytes().decode("latin-1")
        except FileNotFoundError:
            original = ""
        current = general_values(original)
        written = data["written"]
        back = {key: value for key, value in data["saved"].items() if key in written and current.get(key) == written[key]}
        updated = rewrite_conf(original, back)
        if updated != original:
            _atomic_write(conf, updated.encode("latin-1"), 0o644)
            changed = True
    saved.unlink(missing_ok=True)
    return changed


def apply_game_plan(plan: Plan, preset: str, conf: pathlib.Path = MOONLIGHT_CONF,
                    saved: pathlib.Path = GLOBAL_SAVED, run_dir: pathlib.Path = RUN) -> None:
    """Write a game's plan, first keeping the values it replaces for restore_global."""
    if moonlight_running(run_dir):
        raise StreamError("CLOSE MOONLIGHT FIRST; IT WOULD OVERWRITE THE NEW SETTINGS WHEN IT EXITS")
    try:
        current = general_values(conf.read_bytes().decode("latin-1"))
    except FileNotFoundError:
        current = {}
    values = plan_values(plan)
    record = {"preset": preset, "saved": {key: current.get(key) for key in values}, "written": values}
    _atomic_write(saved, json.dumps(record, indent=1).encode("utf-8"), 0o640)
    apply_plan(plan, conf, run_dir)


def prepare_stream(
    game: tuple[str, str] | None, *, mode: Callable[[], Mode | None], decode: Callable[[], str] = lambda: video_decode(),
    config: pathlib.Path = CONFIG, conf: pathlib.Path = MOONLIGHT_CONF, saved: pathlib.Path = GLOBAL_SAVED,
    run_dir: pathlib.Path = RUN,
) -> str:
    """Before Moonlight starts: the global settings back, then `game`'s (pc key, app) own plan.

    Returns the preset in force ("" for the global settings)."""
    restore_global(conf, saved, run_dir)
    settings = game_settings(*game, config) if game is not None else None
    plan = game_plan(settings, mode(), decode()) if settings is not None else None
    if plan is None:
        return ""
    apply_game_plan(plan, settings.preset, conf, saved, run_dir)
    return settings.preset


def active_preset(saved: pathlib.Path = GLOBAL_SAVED) -> str:
    """The preset the running (or last) stream was started with; "" for the global settings."""
    data = _read_saved(saved)
    preset = data.get("preset") if data else None
    return preset if preset in (*PRESETS, CUSTOM) else ""


class SettingsForm:
    """STREAM SETTINGS for one game: PRESET, then (CUSTOM) RESOLUTION, FPS, BITRATE, HDR, then SAVE.
    LEFT / RIGHT change the focused row, A / Enter on SAVE ends it."""

    def __init__(self, current: GameSettings | None) -> None:
        current = current or GameSettings(DEFAULT)
        self.preset = current.preset
        self.custom = current if current.preset == CUSTOM else GameSettings(CUSTOM)
        self.index = 0

    def rows(self) -> list[tuple[str, str, str]]:
        """(key, label, value) per row."""
        rows = [("preset", "PRESET", self.preset)]
        if self.preset == CUSTOM:
            custom = self.custom
            rows += [
                ("resolution", "RESOLUTION", f"{custom.width}x{custom.height}"),
                ("fps", "FRAME RATE", f"{custom.fps} FPS"),
                ("bitrate", "BITRATE", f"{custom.bitrate / 1000:g} MBPS" if custom.bitrate else "AUTOMATIC"),
                ("hdr", "HDR", "ON" if custom.hdr else "OFF"),
            ]
        return rows + [("save", "SAVE", "")]

    def move(self, step: int) -> None:
        self.index = max(0, min(len(self.rows()) - 1, self.index + step))

    def adjust(self, step: int) -> bool:
        """LEFT / RIGHT on the focused row; True when something changed."""
        key = self.rows()[self.index][0]
        custom = self.custom

        def step_in(options: tuple, current: object) -> object:
            at = options.index(current) if current in options else 0
            return options[max(0, min(len(options) - 1, at + step))]

        if key == "preset":
            self.preset = step_in(CHOICES, self.preset)
        elif key == "resolution":
            self.custom = dataclasses.replace(custom, **dict(zip(
                ("width", "height"), step_in(CUSTOM_RESOLUTIONS, (custom.width, custom.height)))))
        elif key == "fps":
            self.custom = dataclasses.replace(custom, fps=step_in(CUSTOM_FPS, custom.fps))
        elif key == "bitrate":
            self.custom = dataclasses.replace(custom, bitrate=step_in(CUSTOM_BITRATES, custom.bitrate))
        elif key == "hdr":
            self.custom = dataclasses.replace(custom, hdr=step > 0)
        else:
            return False
        return True

    def settings(self) -> GameSettings | None:
        """What SAVE stores: None for DEFAULT."""
        if self.preset == DEFAULT:
            return None
        return self.custom if self.preset == CUSTOM else GameSettings(self.preset)


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
