"""STREAM CHECK: parsing, the verdict and tips, the check itself, its screens, and the STREAMING flow.

Only the system boundaries are faked: run_command (ping, ip and nmcli), the Popen under it,
socket connects, /sys/class/net, and the paired hosts. The parsing, the verdict, the tips and
the screens run for real.
"""

import contextlib
import curses
import importlib.util
import pathlib
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import couchliteos_streamcheck as sc
import couchliteos_stream as stream

# --- Real iputils output (Debian 13, `ping -c N -i 0.2 -W 1 -q ADDRESS`).
PING_GOOD = """PING 192.168.1.50 (192.168.1.50) 56(84) bytes of data.

--- 192.168.1.50 ping statistics ---
20 packets transmitted, 20 received, 0% packet loss, time 3802ms
rtt min/avg/max/mdev = 1.234/2.345/4.567/0.789 ms
"""
PING_PARTIAL = """PING 192.168.1.50 (192.168.1.50) 56(84) bytes of data.

--- 192.168.1.50 ping statistics ---
20 packets transmitted, 17 received, 15% packet loss, time 3911ms
rtt min/avg/max/mdev = 1.100/3.000/22.400/4.800 ms
"""
PING_SILENT = """PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.

--- 192.0.2.1 ping statistics ---
20 packets transmitted, 0 received, 100% packet loss, time 3806ms

"""
PING_ERRORS = """PING 192.168.1.50 (192.168.1.50) 56(84) bytes of data.

--- 192.168.1.50 ping statistics ---
20 packets transmitted, 0 received, +20 errors, 100% packet loss, time 3 ms

"""
PING_UNKNOWN_HOST = "ping: nonexistent.invalid: Name or service not known\n"
PING_UNREACHABLE = "ping: connect: Network is unreachable\n"
PING_DENIED = "ping: socket: Operation not permitted\n"
PING_POOR = """PING 192.168.1.50 (192.168.1.50) 56(84) bytes of data.

--- 192.168.1.50 ping statistics ---
20 packets transmitted, 17 received, 15% packet loss, time 3911ms
rtt min/avg/max/mdev = 3.100/18.000/92.400/24.800 ms
"""
ROUTE_WIRED = r"192.168.1.50 dev enp3s0 src 192.168.1.20 uid 1000 \    cache " + "\n"
ROUTE_WIFI = r"192.168.1.50 via 192.168.1.1 dev wlan0 src 192.168.1.20 uid 1000 \    cache " + "\n"
ROUTE_DEFAULT = "default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.20 metric 600\n"
ROUTE_FAILED = 'Error: any valid prefix is expected rather than "gaming-pc.local".\n'
NMCLI_5GHZ = "no:40:2412 MHz:130 Mbit/s\nyes:78:5180 MHz:866 Mbit/s\nno:30:5745 MHz:540 Mbit/s\n"
NMCLI_24GHZ_WEAK = "yes:42:2437 MHz:72 Mbit/s\nno:80:5180 MHz:866 Mbit/s\n"

HOST = stream.Host(
    name="Gaming-PC", uuid="UUID-1", mac=bytes.fromhex("1c1b0d8dbfe9"), local="192.168.1.50", apps=("Desktop",)
)
NO_MAC = HOST.__class__(name="Gaming-PC", uuid="UUID-1", local="192.168.1.50")


def latency(loss=0.0, jitter=1.0, avg=2.0, method="ping"):
    return sc.Latency(sc.PING_OK, method, 20, round(20 * (100 - loss) / 100), loss, avg / 2, avg, avg * 2, jitter)


WIRED = sc.Link("wired", "enp3s0")
WIFI_5 = sc.Link("wifi", "wlan0", sc.Wifi(78, 5180, 866.7))
WIFI_5_WEAK = sc.Link("wifi", "wlan0", sc.Wifi(42, 5180, 300.0))
WIFI_24_WEAK = sc.Link("wifi", "wlan0", sc.Wifi(42, 2437, 72.0))
WIFI_UNREADABLE = sc.Link("wifi", "wlan0", sc.Wifi())
VPN = sc.Link("other", "tailscale0")


def measured(lat, link, can_wake=True, sunshine_up=True):
    word = sc.verdict(lat, link)
    tips = sc.tips_for(sc.MEASURED, word, link, can_wake, sunshine_up)
    return sc.Result(sc.MEASURED, "GAMING-PC", link, lat, word, tips, can_wake, sunshine_up)


def offline(link=WIRED):
    return sc.Result(sc.OFFLINE, "GAMING-PC", link, None, "", sc.tips_for(sc.OFFLINE, "", link, True), False)


def silent(link=WIRED, can_wake=True):
    return sc.Result(sc.SILENT, "GAMING-PC", link, None, "", sc.tips_for(sc.SILENT, "", link, can_wake), can_wake)


class FakeScreen:
    """Curses stub: keys come back one per getch, then -1 (a timeout) forever. Every drawn
    screen is kept as a list of (row, column, text, attribute)."""

    def __init__(self, keys=(), height=24, width=80, on_getch=None, gate=None):
        self.keys = list(keys)
        self.height, self.width = height, width
        self.on_getch = on_getch
        self.gate = gate  # keys are held back until the screen being shown has this text on it
        self.frames = []
        self.cells = []
        self.timeouts = []
        self.reads = 0

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.cells = []

    def border(self, *_args):
        pass

    def keypad(self, *_args):
        pass

    def addstr(self, row, column, text, attr=0):
        self.cells.append((row, column, text, attr))

    def addnstr(self, row, column, text, count, attr=0):
        self.cells.append((row, column, text[:count], attr))

    def refresh(self):
        self.frames.append(list(self.cells))

    def timeout(self, milliseconds):
        self.timeouts.append(milliseconds)

    def getch(self):
        self.reads += 1
        assert self.reads < 20000, "the screen kept waiting for a key"
        if self.on_getch is not None:
            self.on_getch()
        if self.keys and (self.gate is None or (self.frames and self.gate in self.last_text())):
            return self.keys.pop(0)
        time.sleep(0.001)
        return -1

    def last(self):
        return self.frames[-1]

    def last_text(self):
        return "\n".join(text for _row, _column, text, _attr in self.last())

    def all_text(self):
        return "\n".join(text for frame in self.frames for _row, _column, text, _attr in frame)


class Commands:
    """A stand-in for run_command: canned output (or an exception, or a function) per program."""

    def __init__(self, ping=PING_GOOD, route=ROUTE_WIRED, default=ROUTE_DEFAULT, nmcli=""):
        self.answers = {"ping": ping, "route-get": route, "route-default": default, "nmcli": nmcli}
        self.calls = []

    def __call__(self, command, timeout, cancel=None):
        self.calls.append(command)
        key = ("route-default" if "default" in command else "route-get") if command[0] == "ip" else command[0]
        answer = self.answers[key]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if callable(answer):
            return answer(command, timeout, cancel)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def ran(self, program):
        return [command for command in self.calls if command[0] == program]


class SysfsCase(unittest.TestCase):
    """A fake /sys/class/net: wired (enp3s0), wlan0 (wireless) and tailscale0 (a tunnel)."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sysfs = pathlib.Path(directory.name)
        (self.sysfs / "enp3s0" / "device").mkdir(parents=True)
        (self.sysfs / "wlan0" / "wireless").mkdir(parents=True)
        (self.sysfs / "wlan0" / "device").mkdir()
        (self.sysfs / "tailscale0").mkdir()
        patcher = mock.patch.object(sc, "SYSFS", self.sysfs)
        patcher.start()
        self.addCleanup(patcher.stop)


# ----------------------------------------------------------------------- ping text


class ParsePingTest(unittest.TestCase):
    def test_a_normal_run_gives_all_the_numbers(self):
        result = sc.parse_ping(PING_GOOD)
        self.assertEqual(result.status, sc.PING_OK)
        self.assertEqual((result.sent, result.received, result.loss_pct), (20, 20, 0.0))
        self.assertEqual((result.min_ms, result.avg_ms, result.max_ms, result.jitter_ms), (1.234, 2.345, 4.567, 0.789))
        self.assertEqual(result.method, "ping")

    def test_partial_loss_is_counted(self):
        result = sc.parse_ping(PING_PARTIAL)
        self.assertEqual(result.status, sc.PING_OK)
        self.assertEqual((result.sent, result.received, result.loss_pct), (20, 17, 15.0))
        self.assertEqual((result.avg_ms, result.max_ms, result.jitter_ms), (3.0, 22.4, 4.8))

    def test_a_pc_that_never_answers(self):
        for output in (PING_SILENT, PING_ERRORS):
            with self.subTest(output=output[-60:]):
                result = sc.parse_ping(output)
                self.assertEqual(result.status, sc.NO_REPLY)
                self.assertEqual((result.sent, result.received), (20, 0))
                self.assertEqual(result.loss_pct, 100.0)

    def test_an_unknown_host(self):
        self.assertEqual(sc.parse_ping(PING_UNKNOWN_HOST).status, sc.UNKNOWN_HOST)
        self.assertEqual(
            sc.parse_ping("ping: gaming-pc.local: Temporary failure in name resolution\n").status, sc.UNKNOWN_HOST
        )

    def test_other_failures_are_told_apart(self):
        self.assertEqual(sc.parse_ping(PING_UNREACHABLE).status, sc.NO_ROUTE)
        self.assertEqual(sc.parse_ping(PING_DENIED).status, sc.DENIED)
        self.assertEqual(sc.parse_ping("ping: Lacking privilege for raw socket.\n").status, sc.DENIED)
        self.assertEqual(sc.parse_ping("").status, sc.FAILED)
        self.assertEqual(sc.parse_ping("something else entirely").status, sc.FAILED)

    def test_other_ping_flavours(self):
        busybox = "10 packets transmitted, 10 packets received, 0% packet loss\nround-trip min/avg/max = 1.0/2.0/3.0 ms\n"
        result = sc.parse_ping(busybox)
        self.assertEqual((result.status, result.avg_ms, result.jitter_ms), (sc.PING_OK, 2.0, 0.0))
        fractional = PING_GOOD.replace("0% packet loss", "0.5% packet loss")
        self.assertEqual(sc.parse_ping(fractional).loss_pct, 0.5)

    def test_answers_without_round_trip_times_are_not_numbers(self):
        text = "20 packets transmitted, 20 received, 0% packet loss, time 3802ms\n"
        self.assertEqual(sc.parse_ping(text).status, sc.FAILED)


class PingRunTest(unittest.TestCase):
    def test_the_command_is_twenty_pings_four_seconds_long_and_cannot_be_an_option(self):
        commands = Commands()
        with mock.patch.object(sc, "run_command", commands):
            result = sc.ping("192.168.1.50")
        self.assertEqual(result.status, sc.PING_OK)
        self.assertEqual(commands.calls, [["ping", "-c", "20", "-i", "0.2", "-W", "1", "-q", "192.168.1.50"]])
        self.assertEqual(sc.PING_COUNT * sc.PING_INTERVAL, 4.0)
        self.assertEqual(sc.PING_INTERVAL, 0.2)  # the shortest an unprivileged user may ask of older iputils

    def test_a_ping_that_would_take_longer_than_its_schedule_times_out(self):
        seen = []
        with mock.patch.object(sc, "run_command", lambda command, timeout, cancel=None: seen.append(timeout) or PING_GOOD):
            sc.ping("192.168.1.50")
        self.assertGreater(seen[0], 5.0)  # 4 s of pings and the 1 s wait for the last answer
        self.assertLess(seen[0], 15.0)

    def test_the_ping_binary_missing(self):
        with mock.patch.object(sc, "run_command", Commands(ping=sc.CommandError("missing"))):
            self.assertEqual(sc.ping("192.168.1.50").status, sc.MISSING)

    def test_a_timeout(self):
        with mock.patch.object(sc, "run_command", Commands(ping=sc.CommandError("timeout"))):
            self.assertEqual(sc.ping("192.168.1.50").status, sc.TIMED_OUT)

    def test_any_other_failure(self):
        with mock.patch.object(sc, "run_command", Commands(ping=sc.CommandError("failed"))):
            self.assertEqual(sc.ping("192.168.1.50").status, sc.FAILED)

    def test_a_cancel_gets_through(self):
        with mock.patch.object(sc, "run_command", Commands(ping=sc.Cancelled())):
            with self.assertRaises(sc.Cancelled):
                sc.ping("192.168.1.50")

    def test_an_address_that_ping_could_read_as_an_option_is_never_run(self):
        commands = Commands()
        with mock.patch.object(sc, "run_command", commands):
            for address in ("-f", "-c1", "a b", ""):
                self.assertEqual(sc.ping(address).status, sc.FAILED)
        self.assertEqual(commands.calls, [])

    def test_unknown_host_and_silence_come_through_as_such(self):
        with mock.patch.object(sc, "run_command", Commands(ping=PING_UNKNOWN_HOST)):
            self.assertEqual(sc.ping("nonexistent.invalid").status, sc.UNKNOWN_HOST)
        with mock.patch.object(sc, "run_command", Commands(ping=PING_SILENT)):
            self.assertEqual(sc.ping("192.0.2.1").status, sc.NO_REPLY)


class FakeProcess:
    """What subprocess.Popen returns: communicate() gives the script's steps, then times out."""

    def __init__(self, script):
        self.script = list(script)
        self.killed = self.reaped = False

    def communicate(self, timeout=None):
        if timeout is None:
            self.reaped = True
            return "", None
        step = self.script.pop(0) if self.script else subprocess.TimeoutExpired("fake", timeout)
        if isinstance(step, BaseException):
            raise step
        return step, None

    def kill(self):
        self.killed = True


class RunCommandTest(unittest.TestCase):
    def popen(self, process=None, error=None):
        return mock.patch.object(subprocess, "Popen", side_effect=error, return_value=process)

    def test_it_returns_the_output_of_a_command_run_in_the_c_locale_without_a_shell(self):
        process = FakeProcess([subprocess.TimeoutExpired("fake", 0.1), "hello\n"])
        with self.popen(process) as popen:
            self.assertEqual(sc.run_command(["ip", "route"], 3), "hello\n")
        arguments, options = popen.call_args
        self.assertEqual(arguments[0], ["ip", "route"])
        self.assertEqual(options["env"]["LC_ALL"], "C")
        self.assertEqual(options["stderr"], subprocess.STDOUT)
        self.assertNotIn("shell", options)
        self.assertFalse(process.killed)

    def test_a_program_that_is_not_installed(self):
        with self.popen(error=FileNotFoundError("ping")):
            with self.assertRaises(sc.CommandError) as caught:
                sc.run_command(["ping"], 3)
        self.assertEqual(caught.exception.kind, "missing")

    def test_a_program_that_cannot_be_started(self):
        with self.popen(error=PermissionError("no")):
            with self.assertRaises(sc.CommandError) as caught:
                sc.run_command(["ping"], 3)
        self.assertEqual(caught.exception.kind, "failed")

    def test_a_command_that_runs_too_long_is_killed(self):
        process = FakeProcess([])
        clock = mock.Mock(monotonic=mock.Mock(side_effect=iter(range(0, 1000, 10))))  # every look at the clock is 10 s later
        with self.popen(process), mock.patch.object(sc, "time", clock):
            with self.assertRaises(sc.CommandError) as caught:
                sc.run_command(["ping"], 5)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertTrue(process.killed and process.reaped)

    def test_b_kills_a_running_command(self):
        process = FakeProcess([])
        cancel = threading.Event()
        cancel.set()
        with self.popen(process):
            with self.assertRaises(sc.Cancelled):
                sc.run_command(["ping"], 30, cancel)
        self.assertTrue(process.killed and process.reaped)


# ------------------------------------------------------------- route and Wi-Fi


class RouteTest(SysfsCase):
    def test_the_device_is_read_from_the_route(self):
        self.assertEqual(sc.parse_route_device(ROUTE_WIRED), "enp3s0")
        self.assertEqual(sc.parse_route_device(ROUTE_WIFI), "wlan0")  # via a gateway
        self.assertEqual(sc.parse_route_device(ROUTE_DEFAULT), "wlan0")
        self.assertEqual(sc.parse_route_device("local 127.0.0.1 dev lo table local src 127.0.0.1"), "lo")

    def test_nothing_usable_in_the_output(self):
        self.assertEqual(sc.parse_route_device(ROUTE_FAILED), "")
        self.assertEqual(sc.parse_route_device(""), "")
        self.assertEqual(sc.parse_route_device("10.0.0.1 dev eth0;reboot src 10.0.0.2"), "")

    def test_the_route_to_the_pc_decides_not_the_default_route(self):
        commands = Commands(route=ROUTE_WIRED, default=ROUTE_DEFAULT)
        with mock.patch.object(sc, "run_command", commands):
            self.assertEqual(sc.route_device("192.168.1.50"), "enp3s0")
        self.assertEqual(commands.calls, [["ip", "-o", "route", "get", "192.168.1.50"]])

    def test_a_name_that_ip_cannot_route_falls_back_to_the_default_route(self):
        commands = Commands(route=ROUTE_FAILED, default=ROUTE_DEFAULT)
        with mock.patch.object(sc, "run_command", commands):
            self.assertEqual(sc.route_device("gaming-pc.local"), "wlan0")
        self.assertEqual(commands.calls[-1], ["ip", "-o", "route", "show", "default"])

    def test_without_ip_there_is_no_device(self):
        with mock.patch.object(sc, "run_command", Commands(route=sc.CommandError("missing"), default=sc.CommandError("missing"))):
            self.assertEqual(sc.route_device("192.168.1.50"), "")

    def test_an_address_that_ip_could_read_as_an_option_is_not_asked(self):
        commands = Commands(default=ROUTE_DEFAULT)
        with mock.patch.object(sc, "run_command", commands):
            self.assertEqual(sc.route_device("-6"), "wlan0")
        self.assertEqual(commands.calls, [["ip", "-o", "route", "show", "default"]])

    def test_link_kinds(self):
        (self.sysfs / "wlp3s0" / "phy80211").mkdir(parents=True)  # a Wi-Fi card without the wireless/ directory
        for device, kind in (
            ("wlan0", "wifi"), ("wlp3s0", "wifi"), ("enp3s0", "wired"), ("tailscale0", "other"),
            ("gone0", "other"), ("", "unknown"),
        ):
            with self.subTest(device=device):
                self.assertEqual(sc.link_kind(device), kind)


class WifiTest(SysfsCase):
    def test_the_active_access_point_is_read(self):
        self.assertEqual(sc.parse_wifi(NMCLI_5GHZ), sc.Wifi(78, 5180, 866.0))
        self.assertEqual(sc.parse_wifi(NMCLI_24GHZ_WEAK), sc.Wifi(42, 2437, 72.0))

    def test_without_an_active_access_point_or_with_junk_nothing_is_known(self):
        self.assertEqual(sc.parse_wifi("no:40:2412 MHz:130 Mbit/s\n"), sc.Wifi())
        self.assertEqual(sc.parse_wifi(""), sc.Wifi())
        self.assertEqual(sc.parse_wifi("yes:::\n"), sc.Wifi())
        self.assertEqual(sc.parse_wifi("Error: NetworkManager is not running.\n"), sc.Wifi())

    def test_a_rate_with_a_fraction(self):
        self.assertEqual(sc.parse_wifi("yes:60:5180 MHz:866.7 Mbit/s\n").rate_mbps, 866.7)

    def test_terse_escapes(self):
        self.assertEqual(sc.split_terse(r"a\:b:c"), ["a:b", "c"])
        self.assertEqual(sc.split_terse(r"a\\b:c"), ["a\\b", "c"])
        self.assertEqual(sc.split_terse("::"), ["", "", ""])

    def test_frequency_to_band(self):
        for freq, band in (
            (2400, "2.4"), (2412, "2.4"), (2484, "2.4"), (2500, "2.4"), (2501, ""), (2399, ""),
            (4900, "5"), (5180, "5"), (5885, "5"), (5924, "5"), (4899, ""),
            (5925, "6"), (5955, "6"), (7115, "6"), (7125, "6"), (7126, ""),
            (0, ""), (None, ""),
        ):
            with self.subTest(freq=freq):
                self.assertEqual(sc.band_name(freq), band)
                self.assertEqual(sc.Wifi(50, freq, 100.0).band, band)

    def test_weak_and_the_signal_words(self):
        self.assertTrue(sc.Wifi(49).weak)
        self.assertFalse(sc.Wifi(50).weak)
        self.assertFalse(sc.Wifi(None).weak)
        for signal, text in ((100, "100% (STRONG)"), (70, "70% (STRONG)"), (69, "69% (FAIR)"), (50, "50% (FAIR)"),
                             (49, "49% (WEAK)"), (None, "UNKNOWN")):
            with self.subTest(signal=signal):
                self.assertEqual(sc.signal_label(signal), text)

    def test_the_nmcli_command_reads_the_cache_for_one_device(self):
        commands = Commands(nmcli=NMCLI_5GHZ)
        with mock.patch.object(sc, "run_command", commands):
            self.assertEqual(sc.read_wifi("wlan0"), sc.Wifi(78, 5180, 866.0))
        self.assertEqual(commands.calls, [[
            "nmcli", "-t", "-f", "ACTIVE,SIGNAL,FREQ,RATE", "dev", "wifi", "list", "ifname", "wlan0", "--rescan", "no",
        ]])

    def test_nmcli_missing_or_stuck_means_nothing_is_known(self):
        for error in ("missing", "timeout", "failed"):
            with self.subTest(error=error), mock.patch.object(sc, "run_command", Commands(nmcli=sc.CommandError(error))):
                self.assertEqual(sc.read_wifi("wlan0"), sc.Wifi())

    def test_a_wifi_route_has_wifi_details(self):
        commands = Commands(route=ROUTE_WIFI, nmcli=NMCLI_24GHZ_WEAK)
        with mock.patch.object(sc, "run_command", commands):
            link = sc.find_link("192.168.1.50")
        self.assertEqual(link, sc.Link("wifi", "wlan0", sc.Wifi(42, 2437, 72.0)))

    def test_a_wired_route_never_asks_nmcli(self):
        commands = Commands(route=ROUTE_WIRED, nmcli=NMCLI_5GHZ)
        with mock.patch.object(sc, "run_command", commands):
            link = sc.find_link("192.168.1.50")
        self.assertEqual(link, sc.Link("wired", "enp3s0", None))
        self.assertEqual(commands.ran("nmcli"), [])

    def test_a_tunnel_is_neither(self):
        commands = Commands(route="10.1.2.3 dev tailscale0 src 100.64.0.1 uid 1000 \\    cache \n", nmcli=NMCLI_5GHZ)
        with mock.patch.object(sc, "run_command", commands):
            link = sc.find_link("10.1.2.3")
        self.assertEqual(link, sc.Link("other", "tailscale0", None))
        self.assertEqual(commands.ran("nmcli"), [])

    def test_no_route_at_all(self):
        with mock.patch.object(sc, "run_command", Commands(route=ROUTE_FAILED, default="")):
            self.assertEqual(sc.find_link("192.168.1.50"), sc.Link("unknown", "", None))


# ----------------------------------------------------------- verdict and tips


class VerdictTest(unittest.TestCase):
    def test_the_thresholds_are_the_documented_ones(self):
        self.assertEqual((sc.POOR_LOSS_PCT, sc.POOR_JITTER_MS, sc.POOR_AVG_MS), (1.0, 10.0, 20.0))
        self.assertEqual((sc.GOOD_LOSS_PCT, sc.GOOD_JITTER_MS, sc.GOOD_AVG_MS), (0.0, 5.0, 10.0))

    def test_good_up_to_and_including_its_limits(self):
        self.assertEqual(sc.verdict(latency(0.0, 0.0, 0.5), WIRED), sc.GOOD)
        self.assertEqual(sc.verdict(latency(0.0, 5.0, 2.0), WIRED), sc.GOOD)
        self.assertEqual(sc.verdict(latency(0.0, 1.0, 10.0), WIRED), sc.GOOD)

    def test_just_past_good_is_ok(self):
        self.assertEqual(sc.verdict(latency(0.0, 5.1, 2.0), WIRED), sc.OK)
        self.assertEqual(sc.verdict(latency(0.0, 1.0, 10.1), WIRED), sc.OK)
        self.assertEqual(sc.verdict(latency(0.5, 1.0, 2.0), WIRED), sc.OK)

    def test_ok_up_to_and_including_the_poor_limits(self):
        self.assertEqual(sc.verdict(latency(1.0, 1.0, 2.0), WIRED), sc.OK)
        self.assertEqual(sc.verdict(latency(0.0, 10.0, 2.0), WIRED), sc.OK)
        self.assertEqual(sc.verdict(latency(0.0, 1.0, 20.0), WIRED), sc.OK)

    def test_just_past_the_poor_limits_is_poor(self):
        self.assertEqual(sc.verdict(latency(1.1, 1.0, 2.0), WIRED), sc.POOR)
        self.assertEqual(sc.verdict(latency(0.0, 10.1, 2.0), WIRED), sc.POOR)
        self.assertEqual(sc.verdict(latency(0.0, 1.0, 20.1), WIRED), sc.POOR)

    def test_one_lost_ping_in_twenty_is_poor(self):
        self.assertEqual(sc.verdict(latency(5.0, 1.0, 2.0), WIRED), sc.POOR)

    def test_the_worst_number_decides(self):
        self.assertEqual(sc.verdict(latency(0.0, 12.0, 30.0), WIRED), sc.POOR)
        self.assertEqual(sc.verdict(latency(0.0, 6.0, 25.0), WIRED), sc.POOR)

    def test_a_weak_or_2_4_ghz_wifi_caps_a_good_ping_at_ok(self):
        self.assertEqual(sc.verdict(latency(), WIFI_5), sc.GOOD)
        self.assertEqual(sc.verdict(latency(), WIFI_24_WEAK), sc.OK)
        self.assertEqual(sc.verdict(latency(), WIFI_5_WEAK), sc.OK)
        self.assertEqual(sc.verdict(latency(), sc.Link("wifi", "wlan0", sc.Wifi(90, 2412, 100.0))), sc.OK)
        self.assertEqual(sc.verdict(latency(), sc.Link("wifi", "wlan0", sc.Wifi(50, 5180, 100.0))), sc.GOOD)
        self.assertEqual(sc.verdict(latency(), sc.Link("wifi", "wlan0", sc.Wifi(60, 6000, 100.0))), sc.GOOD)

    def test_wifi_details_that_could_not_be_read_do_not_count_against_the_pc(self):
        self.assertEqual(sc.verdict(latency(), WIFI_UNREADABLE), sc.GOOD)
        self.assertEqual(sc.verdict(latency(), sc.Link("wifi", "wlan0", None)), sc.GOOD)

    def test_a_slow_wifi_link_caps_a_good_ping_at_ok(self):
        slow = lambda mbps: sc.Link("wifi", "wlan0", sc.Wifi(78, 5180, mbps))
        self.assertEqual(sc.verdict(latency(), slow(24.0)), sc.OK)
        self.assertEqual(sc.verdict(latency(), slow(99.9)), sc.OK)
        self.assertEqual(sc.verdict(latency(), slow(100.0)), sc.GOOD)
        self.assertEqual(sc.verdict(latency(), slow(0.0)), sc.GOOD)  # 0 is how a driver says "no idea"
        self.assertEqual(sc.verdict(latency(10.0), slow(24.0)), sc.POOR)  # never makes a bad ping better
        self.assertEqual(sc.verdict(latency(), sc.Link("wired", "enp3s0", sc.Wifi(78, 5180, 24.0))), sc.GOOD)

    def test_wifi_never_makes_a_bad_ping_better(self):
        self.assertEqual(sc.verdict(latency(10.0), WIFI_24_WEAK), sc.POOR)
        self.assertEqual(sc.verdict(latency(10.0), WIFI_5), sc.POOR)

    def test_a_tunnel_is_judged_by_the_ping_alone(self):
        self.assertEqual(sc.verdict(latency(), VPN), sc.GOOD)

    def test_measuring_the_sunshine_port_is_judged_the_same_way(self):
        self.assertEqual(sc.verdict(latency(0.0, 12.0, 2.0, method="port"), WIRED), sc.POOR)


class TipsTest(unittest.TestCase):
    def tips(self, lat, link, can_wake=True):
        return sc.tips_for(sc.MEASURED, sc.verdict(lat, link), link, can_wake)

    def test_a_good_connection_has_nothing_to_change(self):
        self.assertEqual(self.tips(latency(), WIRED), (sc.TIP_NOTHING,))
        self.assertEqual(self.tips(latency(), WIFI_5), (sc.TIP_NOTHING,))

    def test_a_good_ping_with_sunshine_not_answering_points_to_sunshine_not_to_playing(self):
        self.assertEqual(sc.tips_for(sc.MEASURED, sc.GOOD, WIRED, True, False), (sc.TIP_SUNSHINE,))
        self.assertEqual(sc.tips_for(sc.MEASURED, sc.GOOD, WIRED, True, True), (sc.TIP_NOTHING,))

    def test_a_slow_wifi_link_says_cable(self):
        tips = self.tips(latency(), sc.Link("wifi", "wlan0", sc.Wifi(78, 5180, 24.0)))
        self.assertEqual(tips, (sc.TIP_ETHERNET, sc.TIP_SMOOTHER))

    def test_no_route_says_to_check_the_network_settings(self):
        self.assertEqual(sc.tips_for(sc.OFFLINE, "", WIRED, True), (sc.NO_NETWORK,))
        self.assertIn("SETTINGS > NETWORK", sc.NO_NETWORK)

    def test_2_4_ghz_wifi_says_to_switch_to_5_ghz(self):
        tips = self.tips(latency(), sc.Link("wifi", "wlan0", sc.Wifi(80, 2437, 72.0)))
        self.assertEqual(tips, (sc.TIP_5GHZ, sc.TIP_ETHERNET, sc.TIP_SMOOTHER))
        self.assertIn("5 GHZ", sc.TIP_5GHZ)

    def test_a_weak_signal_says_to_move_closer(self):
        self.assertEqual(self.tips(latency(10.0), WIFI_5_WEAK), (sc.TIP_CLOSER, sc.TIP_ETHERNET, sc.TIP_SMOOTHER))
        self.assertIn("CLOSER", sc.TIP_CLOSER)

    def test_never_more_than_three_tips_and_the_cable_beats_the_smoother_stream(self):
        tips = self.tips(latency(10.0), WIFI_24_WEAK)
        self.assertEqual(tips, (sc.TIP_5GHZ, sc.TIP_CLOSER, sc.TIP_ETHERNET))

    def test_strong_5_ghz_wifi_that_stutters_says_cable_then_smoother_stream(self):
        self.assertEqual(self.tips(latency(10.0), WIFI_5), (sc.TIP_ETHERNET, sc.TIP_SMOOTHER))
        self.assertIn("CABLE", sc.TIP_ETHERNET)

    def test_a_cable_that_stutters_looks_at_the_other_end_then_smoother_stream(self):
        self.assertEqual(self.tips(latency(10.0), WIRED), (sc.TIP_GAMING_PC, sc.TIP_SMOOTHER))
        self.assertNotIn(sc.TIP_ETHERNET, self.tips(latency(10.0), WIRED))

    def test_a_tunnel_names_itself(self):
        tips = self.tips(latency(10.0), VPN)
        self.assertIn("TAILSCALE0", tips[0])
        self.assertEqual(tips[1], sc.TIP_SMOOTHER)

    def test_an_unknown_route_only_offers_the_smoother_stream(self):
        self.assertEqual(self.tips(latency(10.0), sc.Link()), (sc.TIP_SMOOTHER,))

    def test_the_smoother_tip_names_the_real_menu(self):
        self.assertIn("SETTINGS > STREAMING > SMOOTHER STREAM", sc.TIP_SMOOTHER)

    def test_a_silent_pc_with_a_known_address_says_wake_pc(self):
        tips = sc.tips_for(sc.SILENT, "", WIRED, True)
        self.assertEqual(tips, (sc.TIP_WAKE, sc.TIP_SUNSHINE))
        self.assertIn("WAKE PC", tips[0])

    def test_a_silent_pc_that_cannot_be_woken_says_to_turn_it_on(self):
        tips = sc.tips_for(sc.SILENT, "", WIRED, False)
        self.assertEqual(tips, (sc.TIP_TURN_ON, sc.TIP_SUNSHINE))
        self.assertNotIn("CHOOSE WAKE PC", " ".join(tips))

    def test_a_pc_that_cannot_be_found(self):
        self.assertEqual(sc.tips_for(sc.NOT_FOUND, "", WIRED, True), (sc.TIP_NOT_FOUND, sc.TIP_WAKE))
        self.assertEqual(sc.tips_for(sc.NOT_FOUND, "", WIRED, False), (sc.TIP_NOT_FOUND, sc.TIP_LEARN))

    def test_every_case_has_one_to_three_tips_in_capitals(self):
        links = (WIRED, WIFI_5, WIFI_5_WEAK, WIFI_24_WEAK, WIFI_UNREADABLE, VPN, sc.Link())
        for link in links:
            for lat in (latency(), latency(2.0), latency(0, 7), latency(20.0, 40, 90)):
                for wake in (True, False):
                    tips = self.tips(lat, link, wake)
                    self.assertTrue(1 <= len(tips) <= sc.MAX_TIPS, (link, lat, tips))
        for text in (sc.TIP_NOTHING, sc.TIP_ETHERNET, sc.TIP_5GHZ, sc.TIP_CLOSER, sc.TIP_GAMING_PC, sc.TIP_SMOOTHER,
                     sc.TIP_WAKE, sc.TIP_TURN_ON, sc.TIP_SUNSHINE, sc.TIP_NOT_FOUND, sc.TIP_LEARN):
            self.assertEqual(text, text.upper())
            self.assertLessEqual(len(text), 100)


class UnavailableTest(unittest.TestCase):
    def test_no_paired_pc(self):
        message = sc.unavailable([], None, True)
        self.assertIn("PAIR A GAMING PC FIRST", message)
        self.assertEqual(sc.unavailable([], None, False), message)

    def test_no_network(self):
        self.assertIn("NO NETWORK", sc.unavailable([HOST], HOST, False))

    def test_a_pc_without_an_address(self):
        message = sc.unavailable([stream.Host(name="X", remote="1.2.3.4")], stream.Host(name="X", remote="1.2.3.4"), True)
        self.assertIn("NO ADDRESS", message)

    def test_a_manual_address_is_enough_and_so_is_a_pc_still_to_be_chosen(self):
        manual = stream.Host(name="X", manual="gaming-pc.local")
        self.assertEqual(sc.unavailable([manual], manual, True), "")
        self.assertEqual(sc.unavailable([HOST, manual], None, True), "")

    def test_all_good(self):
        self.assertEqual(sc.unavailable([HOST], HOST, True), "")


# ---------------------------------------------------------- the port fallback


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class TcpLatencyTest(unittest.TestCase):
    def run_tcp(self, outcomes, cancel=None, closer=None):
        clock = Clock()
        outcomes = list(outcomes)
        opened = []

        class Connection:
            def close(self):
                if closer:
                    raise closer
                opened.append("closed")

        def connect(address, timeout):
            self.assertEqual(address, ("192.168.1.50", 47989))
            self.assertEqual(timeout, sc.TCP_TIMEOUT)
            outcome = outcomes.pop(0)
            if outcome == "refused":
                clock.now += 0.002
                raise ConnectionRefusedError
            if outcome == "timeout":
                clock.now += sc.TCP_TIMEOUT
                raise socket.timeout
            clock.now += outcome
            return Connection()

        result = sc.tcp_latency(
            "192.168.1.50", 47989, cancel, count=len(outcomes), connect=connect, clock=clock, sleep=clock.sleep
        )
        return result, clock, opened

    def test_twenty_connects_become_the_same_numbers_as_ping(self):
        result, clock, opened = self.run_tcp([0.004] * 20)
        self.assertEqual((result.status, result.method, result.sent, result.received), (sc.PING_OK, "port", 20, 20))
        self.assertEqual(result.loss_pct, 0.0)
        for value in (result.min_ms, result.avg_ms, result.max_ms):
            self.assertAlmostEqual(value, 4.0, places=3)
        self.assertAlmostEqual(result.jitter_ms, 0.0, places=3)
        self.assertEqual(len(opened), 20)  # every connection is closed again
        self.assertEqual(len(clock.slept), 19)  # nothing to wait for after the last one
        self.assertAlmostEqual(clock.now, 19 * sc.PING_INTERVAL + 0.004, places=3)  # about 4 s

    def test_a_refused_connection_is_an_answer(self):
        result, _clock, _opened = self.run_tcp(["refused"] * 10)
        self.assertEqual((result.status, result.received, result.loss_pct), (sc.PING_OK, 10, 0.0))

    def test_a_timeout_is_a_lost_packet(self):
        result, _clock, _opened = self.run_tcp([0.004] * 15 + ["timeout"] * 5)
        self.assertEqual((result.received, result.loss_pct), (15, 25.0))

    def test_no_answer_at_all(self):
        result, _clock, _opened = self.run_tcp(["timeout"] * 20)
        self.assertEqual((result.status, result.method, result.sent, result.received), (sc.NO_REPLY, "port", 20, 0))

    def test_an_error_while_closing_does_not_matter(self):
        result, _clock, _opened = self.run_tcp([0.004] * 3, closer=OSError("reset"))
        self.assertEqual(result.received, 3)

    def test_jitter_is_the_deviation_of_the_round_trips(self):
        result = sc.latency_from_samples([1.0, 3.0], 2, "port")
        self.assertEqual((result.min_ms, result.avg_ms, result.max_ms, result.jitter_ms), (1.0, 2.0, 3.0, 1.0))

    def test_b_stops_it(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(sc.Cancelled):
            self.run_tcp([0.004] * 5, cancel)


# ------------------------------------------------------------------ the check


class RunCheckTest(SysfsCase):
    def check(self, host=HOST, link=WIRED, ping=None, probe="up", tcp=None, cancel=None):
        patches = [
            mock.patch.object(sc, "find_link", return_value=link),
            mock.patch.object(sc, "ping", return_value=ping or latency(), side_effect=None),
            mock.patch.object(sc.stream, "probe", return_value=probe),
            mock.patch.object(sc, "tcp_latency", return_value=tcp or latency(method="port")),
        ]
        with contextlib.ExitStack() as stack:
            mocks = [stack.enter_context(patch) for patch in patches]
            result = sc.run_check(host, cancel)
        self.mocks = dict(zip(("link", "ping", "probe", "tcp"), mocks))
        return result

    def test_a_good_wired_connection(self):
        result = self.check()
        self.assertEqual((result.state, result.verdict, result.pc), (sc.MEASURED, sc.GOOD, "GAMING-PC"))
        self.assertEqual(result.tips, (sc.TIP_NOTHING,))
        self.assertEqual(result.link, WIRED)
        self.assertEqual(result.latency.method, "ping")
        # one warm-up ping (its answer is slow: ARP, Wi-Fi power save) that stays out of the numbers, then the run
        self.assertEqual(
            self.mocks["ping"].call_args_list,
            [mock.call("192.168.1.50", None, count=1), mock.call("192.168.1.50", None)],
        )
        self.mocks["probe"].assert_called_once_with(HOST)  # a PC that answers ping may still have no Sunshine
        self.mocks["tcp"].assert_not_called()

    def test_a_good_ping_while_sunshine_is_not_answering_is_not_called_ready_to_play(self):
        for probe in ("awake", "down", "unknown"):
            with self.subTest(probe=probe):
                result = self.check(probe=probe)
                self.assertEqual((result.state, result.verdict), (sc.MEASURED, sc.GOOD))  # the network is not blamed
                self.assertEqual(result.tips, (sc.TIP_SUNSHINE,))
                self.assertNotIn(sc.TIP_NOTHING, result.tips)
                self.assertEqual(result.headline, sc.NO_SUNSHINE)
                self.assertIn("NETWORK LOOKS FINE", sc.NO_SUNSHINE)
                self.assertIn("SUNSHINE IS NOT ANSWERING", sc.NO_SUNSHINE)
                self.mocks["probe"].assert_called_once_with(HOST)
                self.mocks["tcp"].assert_not_called()

    def test_a_bad_ping_keeps_its_verdict_and_tips_while_sunshine_is_not_answering(self):
        result = self.check(link=WIFI_24_WEAK, ping=latency(15.0, 14.0, 18.0), probe="down")
        self.assertEqual(result.verdict, sc.POOR)
        self.assertEqual(result.tips, (sc.TIP_5GHZ, sc.TIP_CLOSER, sc.TIP_ETHERNET))
        self.assertEqual(result.headline, "STREAMING WILL PROBABLY STUTTER")

    def test_a_pc_that_ignores_ping_and_refuses_the_port_is_not_called_ready_to_play_either(self):
        result = self.check(ping=sc.Latency(status=sc.NO_REPLY), probe="awake", tcp=latency(0.0, 1.0, 3.0, "port"))
        self.assertEqual((result.state, result.verdict, result.latency.method), (sc.MEASURED, sc.GOOD, "port"))
        self.assertEqual((result.tips, result.headline), ((sc.TIP_SUNSHINE,), sc.NO_SUNSHINE))

    def test_a_poor_wifi_connection(self):
        result = self.check(link=WIFI_24_WEAK, ping=latency(15.0, 14.0, 18.0))
        self.assertEqual((result.state, result.verdict), (sc.MEASURED, sc.POOR))
        self.assertEqual(result.tips, (sc.TIP_5GHZ, sc.TIP_CLOSER, sc.TIP_ETHERNET))

    def test_the_first_lan_address_and_port_are_used(self):
        manual = stream.Host(name="Pc", manual="10.0.0.7", manual_port=47990)
        self.check(manual, ping=sc.Latency(status=sc.NO_REPLY))
        self.assertEqual(
            self.mocks["ping"].call_args_list, [mock.call("10.0.0.7", None, count=1), mock.call("10.0.0.7", None)]
        )
        self.mocks["tcp"].assert_called_once_with("10.0.0.7", 47990, None)

    def test_a_pc_that_ignores_ping_is_measured_through_its_sunshine_port(self):
        for status in (sc.NO_REPLY, sc.MISSING, sc.TIMED_OUT, sc.DENIED, sc.FAILED, sc.NO_ROUTE):
            for probe in ("up", "awake"):
                with self.subTest(status=status, probe=probe):
                    result = self.check(ping=sc.Latency(status=status), probe=probe, tcp=latency(0.0, 1.0, 3.0, "port"))
                    self.assertEqual((result.state, result.verdict, result.latency.method), (sc.MEASURED, sc.GOOD, "port"))
                    self.mocks["tcp"].assert_called_once_with("192.168.1.50", stream.HTTP_PORT, None)

    def test_a_pc_nothing_reaches_is_silent_and_can_be_woken(self):
        result = self.check(ping=sc.Latency(status=sc.NO_REPLY), probe="down")
        self.assertEqual((result.state, result.verdict, result.latency), (sc.SILENT, "", None))
        self.assertTrue(result.can_wake)
        self.assertEqual(result.tips, (sc.TIP_WAKE, sc.TIP_SUNSHINE))
        self.assertEqual((result.word, result.headline), ("NO ANSWER", "THE GAMING PC IS NOT ANSWERING"))
        self.assertEqual(result.link, WIRED)  # what is known about the route is still shown
        self.mocks["tcp"].assert_not_called()

    def test_no_route_to_the_pc_points_to_the_network_settings_not_to_wake_pc(self):
        result = self.check(ping=sc.Latency(status=sc.NO_ROUTE), probe="down")
        self.assertEqual((result.state, result.verdict, result.latency), (sc.OFFLINE, "", None))
        self.assertEqual(result.tips, (sc.NO_NETWORK,))
        self.assertEqual((result.word, result.headline), ("NO NETWORK", "THIS PC HAS NO ROUTE TO THE GAMING PC"))
        self.assertFalse(result.can_wake)  # WAKE PC would only wake a PC that cannot be reached anyway
        self.assertEqual(sc.button_ids(result), [sc.AGAIN, sc.BACK])
        self.mocks["tcp"].assert_not_called()

    def test_a_silent_pc_without_a_mac_cannot_be_woken(self):
        result = self.check(NO_MAC, ping=sc.Latency(status=sc.NO_REPLY), probe="down")
        self.assertFalse(result.can_wake)
        self.assertEqual(result.tips, (sc.TIP_TURN_ON, sc.TIP_SUNSHINE))

    def test_a_pc_that_ignored_ping_and_then_stopped_answering_the_port_is_silent(self):
        result = self.check(ping=sc.Latency(status=sc.NO_REPLY), probe="up", tcp=sc.Latency(status=sc.NO_REPLY, method="port"))
        self.assertEqual(result.state, sc.SILENT)

    def test_a_name_nobody_knows(self):
        result = self.check(ping=sc.Latency(status=sc.UNKNOWN_HOST), probe="up")
        self.assertEqual((result.state, result.word), (sc.NOT_FOUND, "NOT FOUND"))
        self.assertEqual(result.headline, "THE GAMING PC CANNOT BE FOUND")
        self.assertEqual(result.tips, (sc.TIP_NOT_FOUND, sc.TIP_WAKE))
        self.mocks["probe"].assert_not_called()

    def test_cancelling_during_a_command_gives_no_result(self):
        with mock.patch.object(sc, "find_link", side_effect=sc.Cancelled):
            self.assertIsNone(sc.run_check(HOST, threading.Event()))

    def test_cancelling_after_the_last_command_gives_no_result(self):
        cancel = threading.Event()
        cancel.set()
        self.assertIsNone(self.check(cancel=cancel))

    def test_end_to_end_through_the_commands(self):
        commands = Commands(ping=PING_POOR, route=ROUTE_WIFI, nmcli=NMCLI_5GHZ)
        with mock.patch.object(sc, "run_command", commands), mock.patch.object(sc.stream, "probe", return_value="up"):
            result = sc.run_check(HOST)
        self.assertEqual((result.state, result.verdict), (sc.MEASURED, sc.POOR))
        self.assertEqual(result.link, sc.Link("wifi", "wlan0", sc.Wifi(78, 5180, 866.0)))
        self.assertEqual(result.latency.loss_pct, 15.0)

    def test_one_warm_up_ping_goes_first_and_only_the_run_is_measured(self):
        commands = Commands(ping=[PING_UNREACHABLE, PING_GOOD])  # the warm-up's answer, whatever it is, is not used
        with mock.patch.object(sc, "run_command", commands), mock.patch.object(sc.stream, "probe", return_value="up"):
            result = sc.run_check(HOST)
        self.assertEqual(
            commands.ran("ping"),
            [["ping", "-c", "1", "-i", "0.2", "-W", "1", "-q", "192.168.1.50"],
             ["ping", "-c", "20", "-i", "0.2", "-W", "1", "-q", "192.168.1.50"]],
        )
        self.assertEqual((result.state, result.verdict, result.latency.jitter_ms), (sc.MEASURED, sc.GOOD, 0.789))

    def test_cancelling_the_warm_up_gives_no_result(self):
        with mock.patch.object(sc, "run_command", Commands(ping=sc.Cancelled())):
            self.assertIsNone(sc.run_check(HOST, threading.Event()))


class RunnerTest(unittest.TestCase):
    def test_the_result_arrives_and_the_runner_says_so(self):
        release = threading.Event()
        runner = sc.Runner(HOST, lambda host, cancel: release.wait(5) and measured(latency(), WIRED))
        self.assertFalse(runner.finished)
        runner.start()
        self.assertFalse(runner.finished)
        release.set()
        self.assertTrue(runner._done.wait(5))
        self.assertTrue(runner.finished)
        self.assertEqual(runner.result.verdict, sc.GOOD)
        self.assertEqual(runner.pc, "GAMING-PC")

    def test_an_error_becomes_no_result_never_an_exception_in_the_launcher(self):
        def broken(host, cancel):
            raise RuntimeError("boom")

        runner = sc.Runner(HOST, broken)
        runner.start()
        self.assertTrue(runner._done.wait(5))
        self.assertIsNone(runner.result)

    def test_cancel_reaches_the_check(self):
        seen = []

        def check(host, cancel):
            seen.append(cancel)
            cancel.wait(5)
            return None

        runner = sc.Runner(HOST, check)
        runner.start()
        runner.cancel.set()
        self.assertTrue(runner._done.wait(5))
        self.assertIs(seen[0], runner.cancel)


# ----------------------------------------------------------------- the screens


class ScreenTest(unittest.TestCase):
    def draw(self, result, height=24, width=80, selected=0):
        screen = FakeScreen(height=height, width=width)
        sc.draw_result(screen, result, sc.button_ids(result), selected)
        return screen

    def assertFits(self, screen):
        for row, column, text, _attr in screen.last():
            self.assertTrue(1 <= row <= screen.height - 2, (row, text))
            self.assertTrue(1 <= column and column + len(text) <= screen.width - 1, (column, text))

    def rows(self, screen):
        return {text.strip(): row for row, _column, text, _attr in screen.last()}

    def test_the_worst_case_fits_80_by_24_with_everything_on_it(self):
        result = measured(latency(15.0, 14.0, 28.0, method="port"), WIFI_24_WEAK)
        self.assertEqual(len(result.tips), 3)
        screen = self.draw(result)
        self.assertFits(screen)
        text = screen.last_text()
        for piece in ("P O O R", "STREAMING WILL PROBABLY STUTTER", "LATENCY  28.0 MS", "JITTER  14.0 MS",
                      "PACKET LOSS  15%", "SUNSHINE'S PORT", "CONNECTION  WI-FI", "WI-FI SIGNAL  42% (WEAK)",
                      "WI-FI BAND  2.4 GHZ    LINK SPEED  72 MBIT/S", "WHAT TO DO", "[ RUN AGAIN ]", "[ BACK ]"):
            self.assertIn(piece, text)
        shown = [row_text for _row, _column, row_text, _attr in screen.last()]
        for number in (1, 2, 3):
            self.assertTrue(any(row_text.startswith(f"{number}. ") for row_text in shown), number)
        for tip in result.tips:  # no tip is cut short
            for word in tip.split():
                self.assertIn(word, text)

    def test_a_typical_wifi_result_keeps_the_big_verdict_banner(self):
        screen = self.draw(measured(latency(0.0, 7.0, 4.0), WIFI_5_WEAK))
        self.assertFits(screen)
        bars = [text for _row, _column, text, attr in screen.last() if attr & curses.A_REVERSE and not text.strip()]
        self.assertEqual(len(bars), 2)  # a blank bar above and one below the word
        self.assertIn("O K", screen.last_text())

    def test_the_verdict_is_large_spaced_and_highlighted(self):
        screen = self.draw(measured(latency(), WIRED))
        word = [(text, attr) for _row, _column, text, attr in screen.last() if "G O O D" in text]
        self.assertEqual(len(word), 1)
        self.assertTrue(word[0][1] & curses.A_REVERSE and word[0][1] & curses.A_BOLD)
        self.assertIn("STREAMING SHOULD BE SMOOTH", screen.last_text())

    def test_the_order_is_verdict_numbers_connection_tips_buttons(self):
        screen = self.draw(measured(latency(10.0, 3.0, 5.0), WIFI_5))
        rows = self.rows(screen)
        order = [
            rows["P O O R"], rows["STREAMING WILL PROBABLY STUTTER"],
            next(row for text, row in rows.items() if text.startswith("LATENCY")),
            rows["CONNECTION  WI-FI"], next(row for text, row in rows.items() if text.startswith("WI-FI SIGNAL")),
            next(row for text, row in rows.items() if text.startswith("WI-FI BAND")),
            rows["WHAT TO DO"], next(row for text, row in rows.items() if text.startswith("1. ")),
        ]
        self.assertEqual(order, sorted(order))
        self.assertEqual(len(set(order)), len(order))
        buttons = next(row for row, _column, text, _attr in screen.last() if "[ RUN AGAIN ]" in text)
        self.assertGreater(buttons, order[-1])

    def test_ethernet_has_no_wifi_rows(self):
        screen = self.draw(measured(latency(), WIRED))
        self.assertIn("CONNECTION  ETHERNET", screen.last_text())
        self.assertNotIn("WI-FI", screen.last_text())
        self.assertNotIn("GHZ", screen.last_text())

    def test_a_tunnel_has_no_wifi_rows_either(self):
        text = self.draw(measured(latency(), VPN)).last_text()
        self.assertIn("CONNECTION  OTHER (TAILSCALE0)", text)
        self.assertNotIn("WI-FI SIGNAL", text)

    def test_wifi_details_that_could_not_be_read_say_unknown(self):
        text = self.draw(measured(latency(), WIFI_UNREADABLE)).last_text()
        self.assertIn("WI-FI SIGNAL  UNKNOWN", text)
        self.assertIn("WI-FI BAND  UNKNOWN    LINK SPEED  UNKNOWN", text)

    def test_a_silent_pc_says_so_and_offers_wake_pc_first(self):
        screen = self.draw(silent())
        self.assertFits(screen)
        text = screen.last_text()
        self.assertIn("N O   A N S W E R", text)
        self.assertIn("THE GAMING PC IS NOT ANSWERING", text)
        self.assertNotIn("LATENCY", text)
        self.assertIn("[ WAKE PC ]", text)
        self.assertEqual(sc.button_ids(silent()), [sc.WAKE, sc.AGAIN, sc.BACK])

    def test_sunshine_not_answering_shows_the_good_network_and_the_sunshine_tip(self):
        screen = self.draw(measured(latency(), WIRED, sunshine_up=False))
        self.assertFits(screen)
        text = screen.last_text()
        self.assertIn("G O O D", text)
        self.assertIn(sc.NO_SUNSHINE, text)
        self.assertIn("START SUNSHINE", text)
        self.assertNotIn("NOTHING TO CHANGE", text)
        self.assertNotIn("STREAMING SHOULD BE SMOOTH", text)

    def test_no_route_says_no_network_and_points_to_settings_network(self):
        screen = self.draw(offline())
        self.assertFits(screen)
        text = screen.last_text()
        flat = " ".join(text.split())  # a tip may wrap between "SETTINGS" and "> NETWORK"
        self.assertIn("N O   N E T W O R K", text)
        self.assertIn("SETTINGS > NETWORK", flat)
        self.assertNotIn("ASLEEP", text)
        self.assertNotIn("WAKE PC", text)

    def test_a_silent_pc_without_a_mac_has_no_wake_button(self):
        self.assertEqual(sc.button_ids(silent(can_wake=False)), [sc.AGAIN, sc.BACK])
        self.assertNotIn("[ WAKE PC ]", self.draw(silent(can_wake=False)).last_text())

    def test_a_measured_result_never_offers_wake_pc(self):
        self.assertEqual(sc.button_ids(measured(latency(), WIRED)), [sc.AGAIN, sc.BACK])

    def test_the_selected_button_is_highlighted(self):
        screen = self.draw(silent(), selected=1)
        marks = {text: attr for _row, _column, text, attr in screen.last() if text.startswith("[ ")}
        self.assertFalse(marks["[ WAKE PC ]"] & curses.A_REVERSE)
        self.assertTrue(marks["[ RUN AGAIN ]"] & curses.A_REVERSE)

    def test_every_result_fits_every_screen_size_the_launcher_may_have(self):
        results = [
            measured(latency(), WIRED), measured(latency(15.0, 14.0, 28.0, "port"), WIFI_24_WEAK),
            measured(latency(0.0, 7.0), WIFI_5_WEAK), measured(latency(15.0), VPN), silent(), silent(can_wake=False),
            sc.Result(sc.NOT_FOUND, "X" * 24, sc.Link(), None, "", sc.tips_for(sc.NOT_FOUND, "", sc.Link(), True), True),
            measured(latency(), WIRED, sunshine_up=False), offline(),
            measured(latency(), sc.Link("wifi", "wlan0", sc.Wifi(78, 5180, 24.0))),
        ]
        for height, width in ((24, 80), (30, 100), (45, 160), (20, 80), (18, 80), (24, 70)):
            for result in results:
                with self.subTest(size=(height, width), state=result.state, verdict=result.verdict):
                    screen = self.draw(result, height, width)
                    self.assertFits(screen)
                    self.assertIn(f"[ {sc.LABELS[sc.BACK]} ]", screen.last_text())  # the way out is always on screen
                    self.assertIn(sc.RESULT_HINT[:20], screen.last_text())

    def test_a_tiny_screen_does_not_crash(self):
        for height, width in ((5, 20), (8, 24), (1, 1)):
            self.draw(measured(latency(), WIFI_5), height, width)
            self.draw(silent(), height, width)

    def test_the_hint_names_controller_buttons_not_keys(self):
        self.assertIn("A / CROSS", sc.RESULT_HINT)
        self.assertIn("B / CIRCLE", sc.RESULT_HINT)
        self.assertIn("B / CIRCLE", sc.CHECK_HINT)

    def test_spaced_words(self):
        self.assertEqual(sc.spaced("GOOD"), "G O O D")
        self.assertEqual(sc.spaced("NO ANSWER"), "N O   A N S W E R")

    def test_every_screen_text_is_in_capitals(self):
        for result in (measured(latency(), WIRED), measured(latency(15.0), WIFI_24_WEAK), silent()):
            for _row, _column, text, _attr in self.draw(result).last():
                self.assertEqual(text, text.upper())
        for text in (sc.CHECKING, sc.NO_NETWORK, sc.NO_ADDRESS, sc.PAIR_FIRST, sc.CHECK_FAILED, sc.CHECK_HINT,
                     sc.NO_SUNSHINE):
            self.assertEqual(text, text.upper())
        self.assertLessEqual(len(sc.CHECKING), 76)
        self.assertLessEqual(len(sc.NO_SUNSHINE), 76)


class ButtonsTest(unittest.TestCase):
    def show(self, keys, result):
        screen = FakeScreen(keys)
        return sc.show_result(screen, result, read_key=lambda window: window.getch()), screen

    def test_a_selects_the_first_button_and_b_goes_back(self):
        self.assertEqual(self.show([10], measured(latency(), WIRED))[0], sc.AGAIN)
        self.assertEqual(self.show([curses.KEY_ENTER], measured(latency(), WIRED))[0], sc.AGAIN)
        self.assertEqual(self.show([27], measured(latency(), WIRED))[0], sc.BACK)

    def test_the_d_pad_moves_between_buttons_and_wraps(self):
        result = silent()
        self.assertEqual(self.show([10], result)[0], sc.WAKE)
        self.assertEqual(self.show([curses.KEY_RIGHT, 10], result)[0], sc.AGAIN)
        self.assertEqual(self.show([curses.KEY_RIGHT, curses.KEY_RIGHT, 10], result)[0], sc.BACK)
        self.assertEqual(self.show([curses.KEY_RIGHT, curses.KEY_RIGHT, curses.KEY_RIGHT, 10], result)[0], sc.WAKE)
        self.assertEqual(self.show([curses.KEY_LEFT, 10], result)[0], sc.BACK)
        self.assertEqual(self.show([curses.KEY_DOWN, 10], result)[0], sc.AGAIN)
        self.assertEqual(self.show([curses.KEY_UP, 10], result)[0], sc.BACK)

    def test_other_keys_change_nothing(self):
        self.assertEqual(self.show([ord("x"), -1, curses.KEY_RESIZE, 10], silent())[0], sc.WAKE)

    def test_it_redraws_while_it_waits(self):
        _choice, screen = self.show([-1, -1, 27], measured(latency(), WIRED))
        self.assertEqual(len(screen.frames), 3)
        self.assertEqual(screen.timeouts[0], 1000)


class WaitTest(unittest.TestCase):
    def runner(self, check):
        return sc.Runner(HOST, check)

    def test_it_says_checking_five_to_fifteen_seconds_until_the_result_is_there(self):
        release = threading.Event()
        runner = self.runner(lambda host, cancel: release.wait(5) and measured(latency(), WIRED))
        screen = FakeScreen(on_getch=release.set)
        self.assertTrue(sc.wait(screen, runner, lambda window: window.getch()))
        self.assertIn("CHECKING... (5 TO 15 SECONDS)", screen.all_text())  # a PC that ignores ping takes about 10
        self.assertNotIn("ABOUT 5 SECONDS", screen.all_text())
        self.assertIn("GAMING PC: GAMING-PC", screen.all_text())
        self.assertIn("B / CIRCLE CANCELS", screen.all_text())
        self.assertEqual(runner.result.verdict, sc.GOOD)
        self.assertEqual(screen.timeouts, [100, 1000])  # polls quickly, and leaves the launcher's timeout as it was

    def test_b_cancels_it(self):
        runner = self.runner(lambda host, cancel: cancel.wait(5) and None)
        screen = FakeScreen([27])
        self.assertFalse(sc.wait(screen, runner, lambda window: window.getch()))
        self.assertTrue(runner.cancel.is_set())
        self.assertTrue(runner._done.wait(5))
        self.assertEqual(screen.timeouts[-1], 1000)

    def test_the_home_button_cancels_it_too(self):
        runner = self.runner(lambda host, cancel: cancel.wait(5) and None)
        pressed = iter([False, True])
        self.assertFalse(sc.wait(FakeScreen(), runner, lambda window: window.getch(), lambda: next(pressed)))
        self.assertTrue(runner.cancel.is_set())
        self.assertTrue(runner._done.wait(5))

    def test_other_keys_do_not_cancel_it(self):
        release = threading.Event()
        runner = self.runner(lambda host, cancel: release.wait(5) and measured(latency(), WIRED))
        screen = FakeScreen([10, curses.KEY_DOWN, ord("a")], on_getch=lambda: screen.reads > 3 and release.set())
        self.assertTrue(sc.wait(screen, runner, lambda window: window.getch()))
        self.assertFalse(runner.cancel.is_set())

    def test_the_animation_moves(self):
        release = threading.Event()
        runner = self.runner(lambda host, cancel: release.wait(5) and None)
        screen = FakeScreen(on_getch=lambda: screen.reads > 6 and release.set())
        sc.wait(screen, runner, lambda window: window.getch())
        spinners = {text for frame in screen.frames for _r, _c, text, _a in frame if text in tuple(sc.SPINNER)}
        self.assertGreater(len(spinners), 2)


# ------------------------------------------------------ Settings > STREAMING flow


class LauncherFlowTest(SysfsCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_streamcheck", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        patcher = mock.patch.object(cls.module.whatsnew, "show_once")
        patcher.start()
        cls.addClassCleanup(patcher.stop)

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = pathlib.Path(directory.name)

    def launcher(self):
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(FakeScreen())
        launcher.wake_host = mock.Mock(return_value="woke")
        return launcher

    def flow(self, keys=(), hosts=(HOST,), commands=None, probe="down", link=True, settings=None, menu=None,
             gate="[ BACK ]"):
        """Run Settings > STREAMING > STREAM CHECK on a 80x24 screen; return (settings, screen, commands).

        The keys are pressed on the result screen (the gate), not while CHECKING... is showing."""
        screen = FakeScreen(keys, gate=gate)
        page = self.module.StreamingSettings(screen, self.launcher())
        page.message = mock.Mock()
        page.menu = mock.Mock(return_value=menu)
        commands = commands or Commands()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.module, "HOME_REQUEST", self.run_dir / "home.request"))
            stack.enter_context(mock.patch.object(stream, "link_up", return_value=link))
            stack.enter_context(mock.patch.object(stream, "load_hosts", return_value=list(hosts)))
            stack.enter_context(
                mock.patch.object(stream, "load_settings", return_value=settings or stream.StreamSettings())
            )
            stack.enter_context(mock.patch.object(stream, "probe", return_value=probe))
            stack.enter_context(mock.patch.object(sc, "run_command", commands))
            page.stream_check()
            for thread in threading.enumerate():  # a cancelled check winds down before the fakes go away
                if thread.name == "stream-check":
                    thread.join(5)
        return page, screen, commands

    def test_the_row_names_the_tips_point_to_exist(self):
        self.assertIn("STREAM CHECK", self.module.StreamingSettings.rows(mock.Mock(), [HOST], stream.StreamSettings()))
        self.assertTrue(self.module.SMOOTHER_ROW.startswith(sc.SMOOTHER))
        self.assertIn(sc.SMOOTHER, sc.TIP_SMOOTHER)
        self.assertIn("STREAMING", self.module.SETTINGS_MENU)
        self.assertIn("NETWORK", self.module.SETTINGS_MENU)
        self.assertIn(f"SETTINGS > NETWORK > {self.module.netmenu.JOIN}", sc.TIP_5GHZ)  # the 5 GHz tip's path is real
        self.assertEqual(sc.TITLE, "STREAM CHECK")

    def test_no_paired_pc_says_to_pair_one_first_and_runs_nothing(self):
        page, screen, commands = self.flow(hosts=())
        title, message = page.message.call_args.args
        self.assertEqual(title, "STREAM CHECK")
        self.assertIn("PAIR A GAMING PC FIRST", message)
        self.assertEqual(commands.calls, [])
        self.assertEqual(screen.frames, [])

    def test_no_network_runs_nothing(self):
        page, _screen, commands = self.flow(link=False)
        self.assertIn("NO NETWORK", page.message.call_args.args[1])
        self.assertEqual(commands.calls, [])

    def test_a_pc_without_an_address_runs_nothing(self):
        page, _screen, commands = self.flow(hosts=(stream.Host(name="X", remote="1.2.3.4"),))
        self.assertIn("NO ADDRESS", page.message.call_args.args[1])
        self.assertEqual(commands.calls, [])

    def test_a_good_wired_connection(self):
        def slow(command, timeout, cancel):  # a real ping takes seconds: CHECKING... must be on screen meanwhile
            time.sleep(0.05)
            return PING_GOOD

        page, screen, commands = self.flow(
            [27], commands=Commands(ping=slow, route=ROUTE_WIRED, nmcli=NMCLI_5GHZ), probe="up"
        )
        text = screen.last_text()
        for piece in ("STREAM CHECK: GAMING-PC", "G O O D", "STREAMING SHOULD BE SMOOTH", "LATENCY  2.3 MS",
                      "JITTER  0.8 MS", "PACKET LOSS  0%", "CONNECTION  ETHERNET", "NOTHING TO CHANGE",
                      "[ RUN AGAIN ]", "[ BACK ]"):
            self.assertIn(piece, text)
        self.assertNotIn("WI-FI", text)
        self.assertNotIn("WAKE PC", text)
        self.assertIn("CHECKING... (5 TO 15 SECONDS)", screen.all_text())  # shown while it ran
        self.assertEqual(commands.ran("nmcli"), [])
        self.assertEqual(
            commands.ran("ping"),
            [["ping", "-c", "1", "-i", "0.2", "-W", "1", "-q", "192.168.1.50"],  # the warm-up
             ["ping", "-c", "20", "-i", "0.2", "-W", "1", "-q", "192.168.1.50"]],
        )
        page.message.assert_not_called()

    def test_a_good_ping_but_no_sunshine_says_so_instead_of_start_moonlight_and_play(self):
        _page, screen, _commands = self.flow([27], probe="down")
        text = screen.last_text()
        for piece in ("G O O D", sc.NO_SUNSHINE, "START SUNSHINE", "LATENCY  2.3 MS"):
            self.assertIn(piece, text)
        self.assertNotIn("START MOONLIGHT AND PLAY", text)
        self.assertNotIn("STREAMING SHOULD BE SMOOTH", text)

    def test_no_route_to_the_pc_points_to_settings_network_and_offers_no_wake_pc(self):
        _page, screen, _commands = self.flow([27], commands=Commands(ping=PING_UNREACHABLE), probe="down")
        text = screen.last_text()
        flat = " ".join(text.split())  # a tip may wrap between "SETTINGS" and "> NETWORK"
        for piece in ("N O   N E T W O R K", "[ RUN AGAIN ]", "[ BACK ]"):
            self.assertIn(piece, text)
        self.assertIn("SETTINGS > NETWORK", flat)
        self.assertNotIn("ASLEEP", text)
        self.assertNotIn("WAKE PC", text)

    def test_a_poor_wifi_connection(self):
        commands = Commands(ping=PING_POOR, route=ROUTE_WIFI, nmcli=NMCLI_24GHZ_WEAK)
        _page, screen, _commands = self.flow([27], commands=commands)
        text = screen.last_text()
        for piece in ("P O O R", "STREAMING WILL PROBABLY STUTTER", "LATENCY  18.0 MS", "JITTER  24.8 MS",
                      "PACKET LOSS  15%", "CONNECTION  WI-FI", "WI-FI SIGNAL  42% (WEAK)",
                      "WI-FI BAND  2.4 GHZ    LINK SPEED  72 MBIT/S", "5 GHZ", "CLOSER", "NETWORK CABLE"):
            self.assertIn(piece, text)
        self.assertFalse(any(row > 22 or row < 1 for row, _column, _text, _attr in screen.last()))

    def test_a_poor_strong_5_ghz_wifi_points_to_the_smoother_stream_row(self):
        commands = Commands(ping=PING_POOR, route=ROUTE_WIFI, nmcli=NMCLI_5GHZ)
        _page, screen, _commands = self.flow([27], commands=commands)
        text = screen.last_text()
        self.assertIn("SETTINGS > STREAMING > SMOOTHER STREAM", text.replace("\n", " "))
        self.assertIn("WI-FI BAND  5 GHZ    LINK SPEED  866 MBIT/S", text)

    def test_an_unreachable_pc_with_a_mac_offers_wake_pc_and_checks_again_after_it(self):
        commands = Commands(ping=[PING_SILENT, PING_SILENT, PING_GOOD])  # (warm-up, run) of each of the two checks
        page, screen, commands = self.flow([10, 27], commands=commands)  # A on WAKE PC, then B on the new result
        first = screen.frames[[index for index, frame in enumerate(screen.frames)
                               if "N O   A N S W E R" in " ".join(cell[2] for cell in frame)][0]]
        text = " ".join(cell[2] for cell in first)
        for piece in ("THE GAMING PC IS NOT ANSWERING", "[ WAKE PC ]", "[ RUN AGAIN ]", "[ BACK ]", "CHOOSE WAKE PC BELOW"):
            self.assertIn(piece, text)
        self.assertEqual(self.wake_calls(page), [(HOST, True)])  # the existing wake flow, forced
        self.assertIn("GAMING-PC IS AWAKE", page.message.call_args.args[1])
        self.assertEqual(page.message.call_args.args[0], "WAKE PC")
        self.assertEqual(len(commands.ran("ping")), 4)  # it measured again by itself
        self.assertIn("G O O D", screen.last_text())

    def wake_calls(self, page):
        return [(call.args[0], call.kwargs.get("force")) for call in page.launcher.wake_host.call_args_list]

    def test_an_unreachable_pc_without_a_mac_does_not_offer_wake_pc(self):
        page, screen, _commands = self.flow([27], hosts=(NO_MAC,), commands=Commands(ping=PING_SILENT))
        text = screen.last_text()
        self.assertIn("N O   A N S W E R", text)
        self.assertIn("TURN THE GAMING PC ON", text)
        self.assertNotIn("[ WAKE PC ]", text)
        self.assertIn("[ RUN AGAIN ]", text)
        page.launcher.wake_host.assert_not_called()

    def test_a_pc_that_ignores_ping_is_measured_through_sunshine(self):
        # Windows drops pings: the check must not call such a PC silent. (tcp_latency is slow by design.)
        with mock.patch.object(sc, "tcp_latency", return_value=latency(0.0, 1.0, 3.0, "port")):
            _page, screen, _commands = self.flow([27], commands=Commands(ping=PING_SILENT), probe="up")
        text = screen.last_text()
        self.assertIn("G O O D", text)
        self.assertIn("SUNSHINE'S PORT", text)
        self.assertNotIn("N O   A N S W E R", text)

    def test_run_again_measures_again(self):
        commands = Commands(ping=[PING_GOOD, PING_GOOD, PING_POOR])  # (warm-up, run) of each of the two checks
        _page, screen, commands = self.flow([10, 27], commands=commands)  # A on RUN AGAIN, then B
        self.assertEqual(len(commands.ran("ping")), 4)
        self.assertIn("P O O R", screen.last_text())

    def test_back_leaves_after_one_check(self):
        page, _screen, commands = self.flow([curses.KEY_RIGHT, 10])  # RUN AGAIN is first; BACK is the second button
        self.assertEqual(len(commands.ran("ping")), 2)  # the warm-up and the run
        page.message.assert_not_called()

    def test_b_cancels_the_check_itself(self):
        finished = threading.Event()
        seen = []

        def blocked_ping(command, timeout, cancel):
            seen.append(cancel)
            cancel.wait(10)
            finished.set()
            raise sc.Cancelled

        page, screen, _commands = self.flow([27], commands=Commands(ping=blocked_ping), gate=None)
        self.assertTrue(finished.wait(5))
        self.assertTrue(seen[0].is_set())
        page.message.assert_not_called()  # no error, no result: back in the menu
        self.assertNotIn("[ BACK ]", screen.all_text())

    def test_the_home_button_cancels_it_and_is_consumed(self):
        (self.run_dir / "home.request").touch()
        finished = threading.Event()

        def blocked_ping(command, timeout, cancel):
            cancel.wait(10)
            finished.set()
            raise sc.Cancelled

        page, _screen, _commands = self.flow([], commands=Commands(ping=blocked_ping))
        self.assertTrue(finished.wait(5))
        self.assertFalse((self.run_dir / "home.request").exists())
        page.message.assert_not_called()

    def test_a_check_that_breaks_says_so_instead_of_crashing(self):
        page, _screen, _commands = self.flow(commands=Commands(ping=RuntimeError("boom"), route=RuntimeError("boom")))
        self.assertEqual(page.message.call_args.args, ("STREAM CHECK", sc.CHECK_FAILED))

    def test_with_several_pcs_none_chosen_it_asks_which(self):
        other = stream.Host(name="Other", uuid="UUID-2", mac=b"\x01" * 6, local="192.168.1.60")
        page, _screen, commands = self.flow([27], hosts=(HOST, other), menu=1)
        page.menu.assert_called_once()
        self.assertEqual(page.menu.call_args.args[0], "CHECK WHICH PC?")
        self.assertEqual(commands.ran("ping")[0][-1], "192.168.1.60")

    def test_with_several_pcs_backing_out_of_the_choice_checks_nothing(self):
        other = stream.Host(name="Other", uuid="UUID-2", local="192.168.1.60")
        _page, screen, commands = self.flow(hosts=(HOST, other), menu=None)
        self.assertEqual(commands.calls, [])
        self.assertEqual(screen.frames, [])

    def test_the_chosen_pc_is_checked_without_asking(self):
        other = stream.Host(name="Other", uuid="UUID-2", mac=b"\x01" * 6, local="192.168.1.60")
        page, _screen, commands = self.flow([27], hosts=(HOST, other), settings=stream.StreamSettings(False, "UUID-2", "Desktop"))
        page.menu.assert_not_called()
        self.assertEqual(commands.ran("ping")[0][-1], "192.168.1.60")

    def test_wake_pc_still_wakes_the_chosen_pc_the_same_way(self):
        launcher = self.launcher()
        launcher.wake_host = mock.Mock(return_value="timeout")
        page = self.module.StreamingSettings(FakeScreen(), launcher)
        page.message = mock.Mock()
        with mock.patch.object(stream, "load_hosts", return_value=[HOST]), mock.patch.object(
            stream, "load_settings", return_value=stream.StreamSettings()
        ):
            page.wake_pc()
        launcher.wake_host.assert_called_once_with(HOST, force=True)
        self.assertIn(f"WITHIN {int(stream.WAKE_TIMEOUT)} SECONDS", page.message.call_args.args[1])

    def test_a_host_that_cannot_be_woken_is_told_why_not_woken(self):
        launcher = self.launcher()
        page = self.module.StreamingSettings(FakeScreen(), launcher)
        page.message = mock.Mock()
        page.wake(NO_MAC)
        launcher.wake_host.assert_not_called()
        self.assertEqual(page.message.call_args.args, ("WAKE PC", stream.wake_block(NO_MAC)))


if __name__ == "__main__":
    unittest.main()
