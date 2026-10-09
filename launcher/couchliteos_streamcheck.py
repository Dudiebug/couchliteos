"""STREAM CHECK: will streaming from the gaming PC be smooth, and what to change if not.

Settings > STREAMING > STREAM CHECK pings the chosen gaming PC about 20 times in about 4
seconds, finds out whether the route to it is Ethernet or Wi-Fi (for Wi-Fi also the signal,
the band and the link speed), and turns that into a verdict (GOOD, OK or POOR) and one to
three plain tips.

Windows PCs drop pings by default, and so would be called "not answering" while they stream
fine. So when ping gets no answer but the PC does answer on its Sunshine port, the same
numbers are measured with TCP connects to that port instead. Sunshine is probed on every check: a
good network with no Sunshine on it is not called ready to play.

Everything that decides something is a pure function of text and numbers. The few calls that
touch the system (ping, ip, nmcli, a TCP connect) sit behind run_command and tcp_latency, so
the tests hand them canned answers. The check runs in a thread (Runner) so the screen keeps
drawing and B can cancel; it never blocks the launcher.
"""

from __future__ import annotations

import curses
import dataclasses
import os
import pathlib
import re
import socket
import statistics
import subprocess
import textwrap
import threading
import time
from collections.abc import Callable

import couchliteos_errors as errors
import couchliteos_pcstatus as pcstatus
import couchliteos_stream as stream

TITLE = "STREAM CHECK"
SYSFS = pathlib.Path("/sys/class/net")

# --- What is measured.
PING_COUNT = 20
# 0.2 s is the shortest interval an unprivileged user may ask of iputils ping (older versions
# refuse anything lower; the Debian 13 one allows 2 ms). 20 pings 0.2 s apart take about 4 s.
PING_INTERVAL = 0.2
PING_REPLY_WAIT = 1  # seconds ping waits for the last answer; so a silent PC costs about 5 s
PING_SLACK = 5.0  # a ping that takes longer than its own schedule by this much is stuck
TCP_TIMEOUT = 0.5  # seconds one connect to the Sunshine port may take before it counts as lost
POLL = 0.1  # how often a running command is checked for a cancel

# --- The verdict. These numbers are the only place the thresholds live.
# POOR: more than 1% of the pings lost, or jitter over 10 ms, or an average over 20 ms.
POOR_LOSS_PCT = 1.0
POOR_JITTER_MS = 10.0
POOR_AVG_MS = 20.0
# GOOD needs all of these. Anything between GOOD and POOR is OK. (With 20 pings a single lost
# one is already 5%, so any loss at all is POOR in practice.)
GOOD_LOSS_PCT = 0.0
GOOD_JITTER_MS = 5.0
GOOD_AVG_MS = 10.0
# On Wi-Fi a good ping cannot make up for a weak signal or the crowded 2.4 GHz band, which
# stutter once the stream starts, and so does a link slower than WIFI_SLOW_MBPS: those cap the verdict at OK.
WIFI_WEAK_PCT = 50  # below this signal strength the signal is weak
WIFI_STRONG_PCT = 70  # from this on it is strong
WIFI_SLOW_MBPS = 100  # below this link speed (0 is a driver saying it does not know) the link is slow

GOOD, OK, POOR = "GOOD", "OK", "POOR"
# What a check ends in.
MEASURED, SILENT, NOT_FOUND, OFFLINE = "measured", "silent", "notfound", "offline"
# What a ping run ends in (only "ok" has numbers).
PING_OK, NO_REPLY, UNKNOWN_HOST, NO_ROUTE, DENIED, MISSING, TIMED_OUT, FAILED = (
    "ok", "noreply", "unknownhost", "noroute", "denied", "missing", "timeout", "failed",
)
MAX_TIPS = 3

# --- What the screens say (ALL CAPS, at most 76 columns a line).
PAIR_FIRST = "PAIR A GAMING PC FIRST.\nSETTINGS > STREAMING > PAIR A GAMING PC."
NO_NETWORK = "NO NETWORK. CONNECT ETHERNET OR WI-FI, THEN TRY AGAIN: SETTINGS > NETWORK."
NO_ADDRESS = "NO ADDRESS IS KNOWN FOR THIS GAMING PC YET. OPEN MOONLIGHT WHILE IT IS ON, THEN TRY AGAIN."
CHECK_FAILED = "THE CHECK COULD NOT RUN. TRY AGAIN."
CHECKING = "CHECKING... (5 TO 15 SECONDS)"  # a PC that ignores ping takes about 10: ping, then the port
SMOOTHER = "SMOOTHER STREAM"  # the row in Settings > STREAMING (the launcher's SMOOTHER_ROW starts with it)
CHECK_HINT = "B / CIRCLE CANCELS"
RESULT_HINT = "A / CROSS SELECTS  ·  LEFT/RIGHT MOVES A BUTTON  ·  B / CIRCLE GOES BACK"
SPINNER = "|/-\\"

NO_SUNSHINE = "THE NETWORK LOOKS FINE, BUT SUNSHINE IS NOT ANSWERING"  # the headline of a GOOD result without Sunshine
TIP_NOTHING = "NOTHING TO CHANGE. START MOONLIGHT AND PLAY."
TIP_ETHERNET = "USE A NETWORK CABLE (ETHERNET) FROM THIS PC TO YOUR ROUTER. IT IS THE MOST RELIABLE FIX."
TIP_5GHZ = "SWITCH THIS PC TO YOUR 5 GHZ WI-FI NETWORK: SETTINGS > NETWORK > JOIN A WI-FI NETWORK."
TIP_CLOSER = "MOVE THE ROUTER OR THIS PC CLOSER TOGETHER. THE WI-FI SIGNAL IS WEAK."
TIP_GAMING_PC = "IF THE GAMING PC IS ON WI-FI, USE A CABLE THERE TOO. OR TRY ANOTHER CABLE OR ROUTER PORT."
TIP_OTHER = "THIS PC REACHES THE GAMING PC OVER {device}, NOT OVER A CABLE OR WI-FI. A DIRECT HOME CONNECTION STREAMS BEST."
TIP_SMOOTHER = f"CHOOSE SETTINGS > STREAMING > {SMOOTHER}. IT LOWERS THE QUALITY ONE STEP; REPEAT IF NEEDED."
TIP_WAKE = "IT MAY BE ASLEEP: CHOOSE WAKE PC BELOW."
TIP_TURN_ON = "TURN THE GAMING PC ON. TO WAKE IT FROM HERE NEXT TIME, OPEN MOONLIGHT ONCE WHILE IT IS ON."
TIP_SUNSHINE = "IF IT IS ON, START SUNSHINE ON IT AND CHECK THAT IT IS ON THE SAME NETWORK AS THIS PC."
TIP_NOT_FOUND = "THIS PC CANNOT FIND THE GAMING PC'S ADDRESS. CHECK THAT BOTH ARE ON THE SAME NETWORK."
TIP_LEARN = "OPEN MOONLIGHT WHILE THE GAMING PC IS ON, SO IT LEARNS THE ADDRESS AGAIN."

# What a screen returns when a button is chosen.
WAKE, AGAIN, BACK = "wake", "again", "back"
LABELS = {WAKE: "WAKE PC", AGAIN: "RUN AGAIN", BACK: "BACK"}


class Cancelled(Exception):
    """The user pressed B while a command was running."""


class CommandError(Exception):
    """A command could not give an answer; `kind` is "missing", "timeout" or "failed"."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


# ---------------------------------------------------------------------- results


@dataclasses.dataclass(frozen=True)
class Latency:
    """How the pings (or, as a fallback, the connects to the Sunshine port) went."""

    status: str = NO_REPLY
    method: str = "ping"  # "ping", or "port" when the PC ignores ping
    sent: int = 0
    received: int = 0
    loss_pct: float = 100.0
    min_ms: float = 0.0
    avg_ms: float = 0.0
    max_ms: float = 0.0
    jitter_ms: float = 0.0  # ping's mdev: how far the round trips stray from their average


@dataclasses.dataclass(frozen=True)
class Wifi:
    """What NetworkManager knows about the access point in use; None for anything it did not say."""

    signal: int | None = None  # percent
    freq_mhz: int | None = None
    rate_mbps: float | None = None

    @property
    def band(self) -> str:
        return band_name(self.freq_mhz)

    @property
    def weak(self) -> bool:
        return self.signal is not None and self.signal < WIFI_WEAK_PCT

    @property
    def slow(self) -> bool:
        return self.rate_mbps is not None and 0 < self.rate_mbps < WIFI_SLOW_MBPS


@dataclasses.dataclass(frozen=True)
class Link:
    kind: str = "unknown"  # "wired", "wifi", "other" (VPN, bridge...) or "unknown"
    device: str = ""
    wifi: Wifi | None = None  # only for "wifi"


@dataclasses.dataclass(frozen=True)
class Result:
    state: str  # MEASURED, SILENT (nothing answers), NOT_FOUND (the name does not resolve) or OFFLINE (no route)
    pc: str  # the PC's name, as the screen shows it
    link: Link
    latency: Latency | None = None
    verdict: str = ""  # GOOD, OK or POOR; only when measured
    tips: tuple[str, ...] = ()
    can_wake: bool = False  # a MAC address is known, so WAKE PC can be offered
    sunshine_up: bool = True  # Sunshine answered on its port (the network is judged either way)

    @property
    def headline(self) -> str:
        """The verdict in plain words."""
        if self.verdict == GOOD and not self.sunshine_up:
            return NO_SUNSHINE
        return {
            GOOD: "STREAMING SHOULD BE SMOOTH",
            OK: "STREAMING SHOULD WORK, BUT MAY STUTTER NOW AND THEN",
            POOR: "STREAMING WILL PROBABLY STUTTER",
        }.get(self.verdict) or {
            SILENT: "THE GAMING PC IS NOT ANSWERING",
            OFFLINE: "THIS PC HAS NO ROUTE TO THE GAMING PC",
        }.get(self.state, "THE GAMING PC CANNOT BE FOUND")

    @property
    def word(self) -> str:
        """The big word on the screen."""
        return self.verdict or {SILENT: "NO ANSWER", OFFLINE: "NO NETWORK"}.get(self.state, "NOT FOUND")


# ---------------------------------------------------------------------- parsing

SUMMARY_RE = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received.*?([\d.]+)% packet loss")
RTT_RE = re.compile(
    r"(?:rtt|round-trip) min/avg/max(?:/mdev)? = ([\d.]+)/([\d.]+)/([\d.]+)(?:/([\d.]+))? ms"
)
PING_ERRORS = (
    (UNKNOWN_HOST, re.compile(r"name or service not known|temporary failure in name resolution|unknown host|bad address", re.I)),
    (NO_ROUTE, re.compile(r"network is unreachable|no route to host", re.I)),
    (DENIED, re.compile(r"operation not permitted|permission denied|lacking privilege", re.I)),
)
DEVICE_RE = re.compile(r"\bdev (\S+)")
DEVICE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


def parse_ping(output: str) -> Latency:
    """The numbers in iputils `ping -q` output, or why there are none."""
    summary = SUMMARY_RE.search(output)
    if summary is None:
        for status, pattern in PING_ERRORS:
            if pattern.search(output):
                return Latency(status=status)
        return Latency(status=FAILED)
    sent, received = int(summary.group(1)), int(summary.group(2))
    loss = min(max(float(summary.group(3)), 0.0), 100.0)
    rtt = RTT_RE.search(output)
    if received == 0:
        return Latency(status=NO_REPLY, sent=sent)
    if rtt is None:
        return Latency(status=FAILED, sent=sent)
    low, average, high = (float(rtt.group(index)) for index in (1, 2, 3))
    jitter = float(rtt.group(4)) if rtt.group(4) else 0.0
    return Latency(PING_OK, "ping", sent, received, loss, low, average, high, jitter)


def parse_route_device(output: str) -> str:
    """The interface named in `ip -o route get ADDRESS` (or `route show default`) output."""
    match = DEVICE_RE.search(output)
    return match.group(1) if match and DEVICE_NAME_RE.fullmatch(match.group(1)) else ""


def split_terse(line: str) -> list[str]:
    """Fields of one `nmcli -t` line: ":" separates them, "\\:" and "\\\\" are literal."""
    fields = [""]
    escaped = False
    for char in line:
        if escaped:
            fields[-1] += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("")
        else:
            fields[-1] += char
    return fields


def parse_wifi(output: str) -> Wifi:
    """The access point in use from `nmcli -t -f ACTIVE,SIGNAL,FREQ,RATE dev wifi list` output."""
    for line in output.splitlines():
        fields = split_terse(line)
        if len(fields) < 4 or fields[0] != "yes":
            continue
        signal = re.match(r"\d+", fields[1].strip())
        freq = re.match(r"\d+", fields[2].strip())
        rate = re.match(r"\d+(?:\.\d+)?", fields[3].strip())
        return Wifi(
            int(signal.group()) if signal else None,
            int(freq.group()) if freq else None,
            float(rate.group()) if rate else None,
        )
    return Wifi()


def band_name(freq_mhz: int | None) -> str:
    """"2.4", "5" or "6" (GHz) for a frequency in MHz; "" when it is none of them."""
    if freq_mhz is None:
        return ""
    if 2400 <= freq_mhz <= 2500:
        return "2.4"
    if 4900 <= freq_mhz < 5925:
        return "5"
    if 5925 <= freq_mhz <= 7125:
        return "6"
    return ""


def signal_label(signal: int | None) -> str:
    if signal is None:
        return "UNKNOWN"
    word = "STRONG" if signal >= WIFI_STRONG_PCT else "WEAK" if signal < WIFI_WEAK_PCT else "FAIR"
    return f"{signal}% ({word})"


# --------------------------------------------------------------------- the system


def run_command(command: list[str], timeout: float, cancel: threading.Event | None = None) -> str:
    """Run `command` and return what it printed (stdout and stderr, C locale).

    Raises Cancelled as soon as `cancel` is set (the command is killed), and CommandError
    ("missing" when the program is not installed, "timeout", or "failed")."""
    environment = {**os.environ, "LC_ALL": "C"}
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            env=environment,
        )
    except FileNotFoundError:
        raise CommandError("missing") from None
    except OSError:
        raise CommandError("failed") from None
    deadline = time.monotonic() + timeout
    while True:
        try:
            output, _unused = process.communicate(timeout=POLL)
            return output or ""
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None and cancel.is_set():
            _stop(process)
            raise Cancelled
        if time.monotonic() >= deadline:
            _stop(process)
            raise CommandError("timeout")


def _stop(process: subprocess.Popen) -> None:
    try:
        process.kill()
        process.communicate()
    except (OSError, subprocess.SubprocessError):
        pass


def ping(address: str, cancel: threading.Event | None = None, *, count: int = PING_COUNT) -> Latency:
    """Ping `address` `count` times PING_INTERVAL apart. Only Cancelled is raised."""
    if not stream.valid_host(address):
        return Latency(status=FAILED)
    command = [
        "ping", "-c", str(count), "-i", f"{PING_INTERVAL:g}", "-W", str(PING_REPLY_WAIT), "-q", address,
    ]
    # No ping -w: with -c it would keep sending until the deadline when nothing answers.
    try:
        output = run_command(command, count * PING_INTERVAL + PING_REPLY_WAIT + PING_SLACK, cancel)
    except CommandError as error:
        return Latency(status={"missing": MISSING, "timeout": TIMED_OUT}.get(error.kind, FAILED))
    return parse_ping(output)


def latency_from_samples(samples_ms: list[float], sent: int, method: str) -> Latency:
    """The same numbers ping prints, for `samples_ms` answers out of `sent` tries."""
    if not samples_ms:
        return Latency(status=NO_REPLY, method=method, sent=sent)
    received = len(samples_ms)
    return Latency(
        PING_OK, method, sent, received, (sent - received) * 100.0 / sent,
        min(samples_ms), statistics.fmean(samples_ms), max(samples_ms), statistics.pstdev(samples_ms),
    )


def tcp_latency(
    address: str,
    port: int,
    cancel: threading.Event | None = None,
    *,
    count: int = PING_COUNT,
    connect: Callable[..., socket.socket] = socket.create_connection,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
) -> Latency:
    """Round trips to a PC that ignores ping: connects to its Sunshine port, PING_INTERVAL apart.

    A refused connection is an answer too (the PC is on, only Sunshine is not listening)."""
    samples: list[float] = []
    for index in range(count):
        if cancel is not None and cancel.is_set():
            raise Cancelled
        started = clock()
        try:
            connection = connect((address, port), timeout=TCP_TIMEOUT)
        except ConnectionRefusedError:
            samples.append((clock() - started) * 1000)
        except OSError:
            pass  # no answer in time: lost
        else:
            samples.append((clock() - started) * 1000)
            try:
                connection.close()
            except OSError:
                pass
        rest = PING_INTERVAL - (clock() - started)
        if rest > 0 and index < count - 1:
            sleep(rest)
    return latency_from_samples(samples, count, "port")


def route_device(address: str, cancel: threading.Event | None = None) -> str:
    """The interface the route to `address` leaves by; the default route's when that is not known."""
    commands = [["ip", "-o", "route", "show", "default"]]
    if stream.valid_host(address):
        commands.insert(0, ["ip", "-o", "route", "get", address])
    for command in commands:
        try:
            device = parse_route_device(run_command(command, 3, cancel))
        except CommandError:
            continue
        if device:
            return device
    return ""


def link_kind(device: str, sysfs: pathlib.Path | None = None) -> str:
    """"wifi", "wired" (real hardware that is not Wi-Fi), "other" (a tunnel, bridge...) or "unknown"."""
    if not device:
        return "unknown"
    base = (sysfs or SYSFS) / device
    if (base / "wireless").is_dir() or (base / "phy80211").exists():
        return "wifi"
    # Network cards have a "device" link to their PCI or USB device; tunnels and bridges do not.
    return "wired" if (base / "device").exists() else "other"


def read_wifi(device: str, cancel: threading.Event | None = None) -> Wifi:
    """Signal, frequency and rate of the access point `device` is joined to (cached, no new scan)."""
    command = [
        "nmcli", "-t", "-f", "ACTIVE,SIGNAL,FREQ,RATE", "dev", "wifi", "list", "ifname", device,
        "--rescan", "no",
    ]
    try:
        return parse_wifi(run_command(command, 4, cancel))
    except CommandError:
        return Wifi()


def find_link(address: str, cancel: threading.Event | None = None) -> Link:
    """Ethernet or Wi-Fi (with its details) for the route to `address`."""
    device = route_device(address, cancel)
    kind = link_kind(device)
    return Link(kind, device, read_wifi(device, cancel) if kind == "wifi" else None)


# ------------------------------------------------------------- verdict and tips


def verdict(latency: Latency, link: Link) -> str:
    """GOOD, OK or POOR for numbers that were measured; the thresholds are at the top of this file."""
    if (
        latency.loss_pct > POOR_LOSS_PCT
        or latency.jitter_ms > POOR_JITTER_MS
        or latency.avg_ms > POOR_AVG_MS
    ):
        return POOR
    if (
        latency.loss_pct > GOOD_LOSS_PCT
        or latency.jitter_ms > GOOD_JITTER_MS
        or latency.avg_ms > GOOD_AVG_MS
        or wifi_limits(link)
    ):
        return OK
    return GOOD


def wifi_limits(link: Link) -> bool:
    """Wi-Fi that is weak, slow or on 2.4 GHz: fine for a ping, shaky for a stream."""
    return (
        link.kind == "wifi" and link.wifi is not None
        and (link.wifi.band == "2.4" or link.wifi.weak or link.wifi.slow)
    )


def tips_for(
    state: str, verdict_word: str, link: Link, can_wake: bool, sunshine_up: bool = True
) -> tuple[str, ...]:
    """One to three plain tips, the most useful first."""
    if state == OFFLINE:
        return (NO_NETWORK,)
    if state == SILENT:
        return (TIP_WAKE if can_wake else TIP_TURN_ON, TIP_SUNSHINE)
    if state == NOT_FOUND:
        return (TIP_NOT_FOUND, TIP_WAKE if can_wake else TIP_LEARN)
    if verdict_word == GOOD:
        return (TIP_NOTHING if sunshine_up else TIP_SUNSHINE,)
    found: list[str] = []
    if link.kind == "wifi":
        if link.wifi is not None and link.wifi.band == "2.4":
            found.append(TIP_5GHZ)
        if link.wifi is not None and link.wifi.weak:
            found.append(TIP_CLOSER)
        found.append(TIP_ETHERNET)
    elif link.kind == "wired":
        found.append(TIP_GAMING_PC)
    elif link.kind == "other":
        found.append(TIP_OTHER.format(device=link.device.upper()))
    found.append(TIP_SMOOTHER)
    return tuple(found[:MAX_TIPS])


def unavailable(hosts: list[stream.Host], host: stream.Host | None, link_up: bool) -> str:
    """Why the check cannot run, in words for the screen; "" when it can."""
    if not hosts:
        return PAIR_FIRST
    if not link_up:
        return NO_NETWORK
    if host is not None and not host.probe_addresses():
        return NO_ADDRESS
    return ""


# -------------------------------------------------------------------- the check


def run_check(host: stream.Host, cancel: threading.Event | None = None) -> Result | None:
    """Measure the path to `host` (which has an address); None when `cancel` was set.

    Sunshine is probed first, on every address Moonlight knows at once (about a second), and the
    path is measured to the first address that answered (else the first LAN address).
    Takes about 5 seconds: the pings, plus the link look-up, which is quick (about 10 for a PC that
    ignores ping: the silent pings, then the port)."""
    seen: list[tuple[str, int, str]] = []
    probed = stream.probe(host, seen=seen)
    address, port = stream.measure_address(host, seen)
    pc = pcstatus.label(host.name)
    can_wake = bool(host.mac)
    try:
        if cancel is not None and cancel.is_set():
            raise Cancelled
        link = find_link(address, cancel)
        ping(address, cancel, count=1)  # warm-up: the first answer is slow (ARP, Wi-Fi power save); not measured
        latency = ping(address, cancel)
        sunshine = "unknown" if latency.status == UNKNOWN_HOST else probed
        if latency.status not in (PING_OK, UNKNOWN_HOST) and sunshine in ("up", "awake"):
            # The PC is on but ignores ping (Windows does by default): measure Sunshine's port instead.
            latency = tcp_latency(address, port, cancel)
    except Cancelled:
        return None
    if cancel is not None and cancel.is_set():
        return None
    if latency.status == PING_OK:
        word = verdict(latency, link)
        tips = tips_for(MEASURED, word, link, can_wake, sunshine == "up")
        return Result(MEASURED, pc, link, latency, word, tips, can_wake, sunshine == "up")
    state = {UNKNOWN_HOST: NOT_FOUND, NO_ROUTE: OFFLINE}.get(latency.status, SILENT)
    return Result(state, pc, link, None, "", tips_for(state, "", link, can_wake), can_wake and state != OFFLINE)


class Runner:
    """Runs a check in a thread, so the screen can keep drawing and listening for B."""

    def __init__(
        self, host: stream.Host, check: Callable[[stream.Host, threading.Event], Result | None] | None = None
    ) -> None:
        self.host = host
        self.pc = pcstatus.label(host.name)
        self.result: Result | None = None
        self.cancel = threading.Event()
        self._check = check or run_check
        self._done = threading.Event()

    def _run(self) -> None:
        try:
            self.result = self._check(self.host, self.cancel)
        except Exception:  # whatever went wrong, the launcher only sees "no result"
            self.result = None
        finally:
            self._done.set()

    def start(self) -> None:
        threading.Thread(target=self._run, name="stream-check", daemon=True).start()

    @property
    def finished(self) -> bool:
        return self._done.is_set()


# ---------------------------------------------------------------------- screens

Row = tuple[str, int, bool]  # text, curses attribute, centered?
BAR = curses.A_REVERSE | curses.A_BOLD


def _put(screen: "curses.window", row: int, column: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if not 0 < row < height - 1 or column >= width - 1:
        return
    try:
        screen.addnstr(row, max(1, column), text, max(1, width - max(1, column) - 1), attr)
    except curses.error:
        pass


def _frame(screen: "curses.window", title: str) -> tuple[int, int]:
    screen.erase()
    height, width = screen.getmaxyx()
    if height >= 8 and width >= 24:
        try:
            screen.border(ord("|"), ord("|"), ord("-"), ord("-"), ord("+"), ord("+"), ord("+"), ord("+"))
        except curses.error:
            pass
    _put(screen, 1 if height < 20 else 2, max(1, (width - len(title)) // 2), title[: max(0, width - 4)])
    return height, width


def draw_checking(screen: "curses.window", pc: str, frame: int) -> None:
    height, width = _frame(screen, TITLE)
    middle = max(5, height // 2 - 1)
    for row, text in ((middle, CHECKING), (middle + 1, SPINNER[frame % len(SPINNER)]), (middle + 3, f"GAMING PC: {pc}")):
        _put(screen, row, (width - len(text)) // 2, text)
    _put(screen, height - 3, (width - len(CHECK_HINT)) // 2, CHECK_HINT)
    screen.refresh()


def wait(
    screen: "curses.window",
    runner: Runner,
    read_key: Callable[["curses.window"], int] | None = None,
    home_pressed: Callable[[], bool] = lambda: False,
) -> bool:
    """Start `runner` and show CHECKING... until it is done. False when B (or Home) cancelled it."""
    read = read_key or (lambda window: window.getch())
    runner.start()
    screen.timeout(100)
    try:
        frame = 0
        while not runner.finished:
            draw_checking(screen, runner.pc, frame)
            frame += 1
            if read(screen) == errors.ESCAPE or home_pressed():
                runner.cancel.set()
                return False
        return True
    finally:
        screen.timeout(1000)


def spaced(word: str) -> str:
    """"NO ANSWER" as "N O   A N S W E R": big and wide on a TV."""
    return "   ".join(" ".join(part) for part in word.split())


def metric_rows(result: Result) -> list[Row]:
    rows: list[Row] = []
    latency = result.latency
    if latency is not None:
        rows.append((
            f"LATENCY  {latency.avg_ms:.1f} MS    JITTER  {latency.jitter_ms:.1f} MS    "
            f"PACKET LOSS  {latency.loss_pct:g}%", 0, False,
        ))
        if latency.method == "port":
            rows.append(("MEASURED WITH SUNSHINE'S PORT: THE GAMING PC IGNORES PING", 0, False))
    link = result.link
    names = {"wired": "ETHERNET", "wifi": "WI-FI", "unknown": "UNKNOWN"}
    rows.append((f"CONNECTION  {names.get(link.kind) or f'OTHER ({link.device.upper()})'}", 0, False))
    if link.kind == "wifi":  # the Wi-Fi rows exist only when the route really goes over Wi-Fi
        wifi = link.wifi or Wifi()
        speed = f"{wifi.rate_mbps:g} MBIT/S" if wifi.rate_mbps is not None else "UNKNOWN"
        rows.append((f"WI-FI SIGNAL  {signal_label(wifi.signal)}", 0, False))
        rows.append((f"WI-FI BAND  {(wifi.band + ' GHZ') if wifi.band else 'UNKNOWN'}    LINK SPEED  {speed}", 0, False))
    return rows


def tip_rows(result: Result, width: int) -> list[Row]:
    rows: list[Row] = []
    for number, tip in enumerate(result.tips, 1):
        lines = textwrap.wrap(
            tip, width=max(20, min(width - 14, 66)), initial_indent=f"{number}. ", subsequent_indent="   ",
            break_on_hyphens=False,  # never "WI-" then "FI"
        )
        rows += [(line, 0, False) for line in lines]
    return rows


def top_row(height: int) -> int:
    """The first row under the title. Short screens (under 20 rows) give up the blank row above it."""
    return 2 if height < 20 else 3


def layout(result: Result, width: int, height: int) -> list[Row]:
    """The rows between the title and the buttons, trimmed to fit: gaps go first, then the
    verdict's three-row banner (it shrinks to one row), then the tips' last lines.
    At 80x24 everything fits, with the banner, even for Wi-Fi with three tips."""
    metrics = metric_rows(result)
    tips = [("WHAT TO DO", curses.A_BOLD, False), *tip_rows(result, width)]
    room = height - 4 - top_row(height)  # the buttons are on row height - 4
    word = spaced(result.word)
    wide = len(word) + 8
    gap: Row = ("", 0, False)
    for banner_rows, gaps in ((3, 3), (3, 2), (3, 0), (1, 0)):
        banner = (
            [(" " * wide, BAR, True), (f"{word:^{wide}}", BAR, True), (" " * wide, BAR, True)]
            if banner_rows == 3 else [(f"  {word}  ", BAR, True)]
        )
        spacer = [gap] * gaps
        rows = [*banner, (result.headline, curses.A_BOLD, True), *spacer[:1], *metrics, *spacer[1:2], *tips, *spacer[2:]]
        if len(rows) <= room:
            return rows
    return rows[: max(1, room)]


def button_ids(result: Result) -> list[str]:
    return ([WAKE] if result.state != MEASURED and result.can_wake else []) + [AGAIN, BACK]


def draw_result(screen: "curses.window", result: Result, buttons: list[str], selected: int) -> None:
    height, width = _frame(screen, f"{TITLE}: {result.pc}")
    rows = layout(result, width, height)
    widest = max((len(text) for text, _attr, centered in rows if not centered), default=0)
    left = max(2, (width - widest) // 2)
    for offset, (text, attr, centered) in enumerate(rows):
        _put(screen, top_row(height) + offset, (width - len(text)) // 2 if centered else left, text, attr)
    shown = [f"[ {LABELS[button]} ]" for button in buttons]
    column = (width - (sum(len(text) for text in shown) + 3 * (len(shown) - 1))) // 2
    for index, text in enumerate(shown):
        _put(screen, height - 4, column, text, BAR if index == selected else 0)
        column += len(text) + 3
    _put(screen, height - 3, (width - len(RESULT_HINT)) // 2, RESULT_HINT)
    screen.refresh()


def show_result(
    screen: "curses.window", result: Result, read_key: Callable[["curses.window"], int] | None = None
) -> str:
    """Show the result until a button is chosen; return WAKE, AGAIN or BACK (B / Escape)."""
    read = read_key or (lambda window: window.getch())
    buttons = button_ids(result)
    selected = 0
    screen.timeout(1000)
    while True:
        draw_result(screen, result, buttons, selected)
        key = read(screen)
        if key in (curses.KEY_LEFT, curses.KEY_UP, ord("h"), ord("k")):
            selected = (selected - 1) % len(buttons)
        elif key in (curses.KEY_RIGHT, curses.KEY_DOWN, ord("l"), ord("j")):
            selected = (selected + 1) % len(buttons)
        elif key in errors.ENTER_KEYS:
            return buttons[selected]
        elif key == errors.ESCAPE:
            return BACK
