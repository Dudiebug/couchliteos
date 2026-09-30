import contextlib
import dataclasses
import importlib.util
import itertools
import os
import pathlib
import socket
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import couchliteos_stream as stream


# Shaped like a real Moonlight Qt 6.1 QSettings INI file. Keys are sorted, the MAC
# is a raw 6-byte @ByteArray with QSettings escapes, and a value that contains ","
# "=" or ";" is wrapped in double quotes.
SAMPLE = r'''[General]
audiocfg=0
bitrate=20000
defaultver=2
fps=60
height=1080
hosts\1\apps\1\appcollector=false
hosts\1\apps\1\directlaunch=false
hosts\1\apps\1\hdr=false
hosts\1\apps\1\hidden=false
hosts\1\apps\1\id=881448767
hosts\1\apps\1\name=Desktop
hosts\1\apps\2\appcollector=false
hosts\1\apps\2\directlaunch=false
hosts\1\apps\2\hdr=false
hosts\1\apps\2\hidden=false
hosts\1\apps\2\id=1093255277
hosts\1\apps\2\name=Steam Big Picture
hosts\1\apps\size=2
hosts\1\customname=false
hosts\1\hostname=GAMING-PC
hosts\1\ipv6address=
hosts\1\ipv6port=47984
hosts\1\localaddress=192.168.1.50
hosts\1\localport=47989
hosts\1\mac=@ByteArray(\x1c\x1b\r\x8d\xbf\xe9)
hosts\1\manualaddress=
hosts\1\manualport=47989
hosts\1\nvidiasw=false
hosts\1\remoteaddress=203.0.113.9
hosts\1\remoteport=47989
hosts\1\srvcert="@ByteArray(-----BEGIN CERTIFICATE-----\nMIIDXTCCAkWgAwIBAgIJAJC1HiIAZAiIMA0GCSqGSIb3DQEBCwUAMEUxCzAJBgNV\n-----END CERTIFICATE-----\n)"
hosts\1\uuid=6F1A2E0B-1D2C-4E5F-9A8B-7C6D5E4F3A2B
hosts\2\apps\size=0
hosts\2\customname=true
hosts\2\hostname=Living Room Mini
hosts\2\localaddress=192.168.1.77
hosts\2\localport=48989
hosts\2\mac="@ByteArray(,=\\A\n\xff)"
hosts\2\manualaddress=mini.lan
hosts\2\manualport=47989
hosts\2\uuid=AAAAAAAA-0000-0000-0000-000000000002
hosts\size=2
mdns=true
unlockbitrate=false
vsync=true
width=1920

[Other]
hosts\9\hostname=not-a-host
'''


class ParseTest(unittest.TestCase):
    def test_parses_hosts_with_addresses_apps_and_escaped_mac(self):
        hosts = stream.parse_hosts(SAMPLE)
        self.assertEqual([host.name for host in hosts], ["GAMING-PC", "Living Room Mini"])
        first = hosts[0]
        self.assertEqual(first.mac, bytes([0x1C, 0x1B, 0x0D, 0x8D, 0xBF, 0xE9]))
        self.assertEqual(first.mac_text, "1C:1B:0D:8D:BF:E9")
        self.assertEqual((first.local, first.local_port), ("192.168.1.50", 47989))
        self.assertEqual(first.remote, "203.0.113.9")
        self.assertEqual(first.uuid, "6F1A2E0B-1D2C-4E5F-9A8B-7C6D5E4F3A2B")
        self.assertEqual(first.apps, ("Desktop", "Steam Big Picture"))
        self.assertEqual(first.target, "192.168.1.50")

    def test_quoted_mac_with_special_bytes_and_custom_port(self):
        second = stream.parse_hosts(SAMPLE)[1]
        self.assertEqual(second.mac, bytes([0x2C, 0x3D, 0x5C, 0x41, 0x0A, 0xFF]))
        self.assertEqual((second.local, second.local_port), ("192.168.1.77", 48989))
        self.assertEqual(second.manual, "mini.lan")
        self.assertEqual(second.lan_addresses(), [("192.168.1.77", 48989), ("mini.lan", 47989)])
        self.assertEqual(second.apps, ())

    def test_other_sections_and_garbage_are_ignored(self):
        self.assertEqual(stream.parse_hosts(""), [])
        self.assertEqual(stream.parse_hosts("[General]\nwidth=1\nhosts\\size=x\n"), [])
        self.assertEqual(len(stream.parse_hosts(SAMPLE.replace("\n", "\r\n"))), 2)

    def test_unescape_matches_qsettings_writer(self):
        self.assertEqual(stream.unescape(r"\x1c\x1b\r\x8d"), "\x1c\x1b\r\x8d")
        self.assertEqual(stream.unescape(r"a\\b\"c\,d\;e"), 'a\\b"c,d;e')
        # QSettings escapes a hex digit that follows \x or \0 so \x greedy parsing is safe.
        self.assertEqual(stream.unescape(r"\x1c\x41"), "\x1cA")
        self.assertEqual(stream.unescape(r"\0\x31"), "\x001")
        self.assertEqual(stream.unescape(r"\101"), "A")

    def test_mac_text_form_is_accepted(self):
        self.assertEqual(stream.mac_bytes("aa:bb:cc:dd:ee:ff"), bytes.fromhex("aabbccddeeff"))
        self.assertEqual(stream.mac_bytes(b"\xaa\xbb\xcc\xdd\xee\xff".decode("latin-1")), bytes.fromhex("aabbccddeeff"))
        self.assertEqual(stream.mac_bytes(""), b"")
        self.assertEqual(stream.mac_bytes("abc"), b"")

    def test_backup_copy_wins_like_moonlight(self):
        text = (
            "[General]\nhosts\\1\\hostname=Main\nhosts\\size=1\n"
            "hostsbackup\\1\\hostname=Backup\nhostsbackup\\size=1\n"
        )
        self.assertEqual([host.name for host in stream.parse_hosts(text)], ["Backup"])
        text = text.replace("hostsbackup\\size=1", "hostsbackup\\size=0")
        self.assertEqual([host.name for host in stream.parse_hosts(text)], ["Main"])

    def test_load_hosts_reads_file_and_tolerates_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "Moonlight.conf"
            self.assertEqual(stream.load_hosts(path), [])
            path.write_bytes(SAMPLE.encode("latin-1"))
            self.assertEqual(len(stream.load_hosts(path)), 2)

    def test_find_and_default_host(self):
        hosts = stream.parse_hosts(SAMPLE)
        self.assertEqual(stream.find_host(hosts, hosts[1].uuid), hosts[1])
        self.assertEqual(stream.find_host(hosts, "gaming-pc"), hosts[0])
        self.assertEqual(stream.find_host(hosts, "192.168.1.77"), hosts[1])
        self.assertIsNone(stream.find_host(hosts, "nope"))
        self.assertIsNone(stream.find_host(hosts, ""))
        chosen = stream.StreamSettings(host=hosts[1].uuid)
        self.assertEqual(stream.default_host(hosts, chosen), hosts[1])
        # Two known hosts and none chosen is ambiguous: never wake both.
        self.assertIsNone(stream.default_host(hosts, stream.StreamSettings()))
        self.assertEqual(stream.default_host(hosts[:1], stream.StreamSettings()), hosts[0])
        self.assertIsNone(stream.default_host([], stream.StreamSettings()))


class WakeOnLanTest(unittest.TestCase):
    MAC = bytes.fromhex("1c1b0d8dbfe9")

    def test_magic_packet_bytes(self):
        packet = stream.magic_packet(self.MAC)
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:], self.MAC * 16)
        with self.assertRaises(ValueError):
            stream.magic_packet(b"\x01\x02")

    def test_subnet_broadcasts_from_ip_brief(self):
        output = (
            "lo UNKNOWN 127.0.0.1/8\n"
            "enp2s0 UP 192.168.50.27/24 fe80::1/64\n"
            "wlan0 UP 10.1.2.3/16\n"
            "tailscale0 UNKNOWN 100.64.0.5/32\n"
        )
        self.assertEqual(stream.subnet_broadcasts(output), ["192.168.50.255", "10.1.255.255"])
        self.assertEqual(stream.subnet_broadcasts(""), [])

    def test_wake_targets_include_global_subnet_and_host(self):
        host = stream.Host(name="PC", mac=self.MAC, local="192.168.1.50")
        self.assertEqual(
            stream.wake_targets(host, ["192.168.1.255"]),
            ["255.255.255.255", "192.168.1.255", "192.168.1.50"],
        )
        # Without interface data the host's own /24 is the best guess.
        self.assertEqual(
            stream.wake_targets(host, []),
            ["255.255.255.255", "192.168.1.255", "192.168.1.50"],
        )

    def test_send_wol_uses_broadcast_socket_on_ports_9_and_7(self):
        sockets = []

        class FakeSocket:
            def __init__(self, *args):
                self.args, self.options, self.sent, self.closed = args, [], [], False
                sockets.append(self)

            def setsockopt(self, *args):
                self.options.append(args)

            def sendto(self, data, address):
                self.sent.append((data, address))
                return len(data)

            def close(self):
                self.closed = True

        count = stream.send_wol(self.MAC, ["255.255.255.255", "192.168.1.255"], socket_factory=FakeSocket)
        fake = sockets[0]
        self.assertEqual(count, 4)
        self.assertEqual(fake.args, (socket.AF_INET, socket.SOCK_DGRAM))
        self.assertIn((socket.SOL_SOCKET, socket.SO_BROADCAST, 1), fake.options)
        self.assertEqual(
            [address for _data, address in fake.sent],
            [("255.255.255.255", 9), ("255.255.255.255", 7), ("192.168.1.255", 9), ("192.168.1.255", 7)],
        )
        self.assertTrue(all(data == stream.magic_packet(self.MAC) for data, _address in fake.sent))
        self.assertTrue(fake.closed)

    def test_send_wol_survives_unreachable_targets(self):
        class FailingSocket:
            def __init__(self, *_args):
                pass

            def setsockopt(self, *_args):
                pass

            def sendto(self, _data, address):
                if address[0] == "192.168.1.255":
                    raise OSError(101, "Network is unreachable")
                return 102

            def close(self):
                pass

        self.assertEqual(
            stream.send_wol(self.MAC, ["192.168.1.255", "255.255.255.255"], socket_factory=FailingSocket), 2
        )


class ProbeTest(unittest.TestCase):
    HOST = stream.Host(name="PC", mac=b"\x01" * 6, local="192.168.1.50", manual="pc.lan")

    def test_connect_to_sunshine_http_port(self):
        connect = mock.MagicMock()
        self.assertEqual(stream.probe(self.HOST, connect=connect), "up")
        connect.assert_called_once_with(("192.168.1.50", 47989), timeout=1.0)

    def test_refused_means_awake_but_timeout_means_down(self):
        refuse = mock.Mock(side_effect=ConnectionRefusedError())
        self.assertEqual(stream.probe(self.HOST, connect=refuse), "awake")
        down = mock.Mock(side_effect=socket.timeout())
        self.assertEqual(stream.probe(self.HOST, connect=down), "down")
        unreachable = mock.Mock(side_effect=OSError(113, "No route to host"))
        self.assertEqual(stream.probe(self.HOST, connect=unreachable), "down")
        self.assertEqual(down.call_count, 2)  # local and manual addresses

    def test_any_answering_address_is_up(self):
        def connect(address, timeout):
            if address[0] == "pc.lan":
                return mock.MagicMock()
            raise socket.timeout()

        self.assertEqual(stream.probe(self.HOST, connect=connect), "up")

    def test_no_lan_address_is_unknown(self):
        host = stream.Host(name="PC", remote="203.0.113.9")
        self.assertEqual(stream.probe(host, connect=mock.Mock()), "unknown")


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WakeAndWaitTest(unittest.TestCase):
    HOST = stream.Host(name="GAMING-PC", mac=bytes.fromhex("1c1b0d8dbfe9"), local="192.168.1.50")

    def run_wake(self, states, host=None, **kwargs):
        clock = Clock()
        sent = []
        states = list(states)

        def probe(_host):
            state = states.pop(0) if len(states) > 1 else states[0]
            return state

        result = stream.wake_and_wait(
            host or self.HOST,
            probe_fn=probe,
            send=lambda host: sent.append((host.mac, host.name)),
            clock=clock,
            sleep=clock.sleep,
            **kwargs,
        )
        return result, sent, clock

    def test_reachable_host_is_left_alone(self):
        for state in ("up", "awake"):
            result, sent, _clock = self.run_wake([state])
            self.assertEqual(result, "up")
            self.assertEqual(sent, [])

    def test_host_without_mac_is_not_woken(self):
        host = stream.Host(name="PC", local="192.168.1.50")
        result, sent, _clock = self.run_wake(["down"], host)
        self.assertEqual(result, "nomac")
        self.assertEqual(sent, [])

    def test_unknown_address_is_skipped_unless_forced(self):
        result, sent, _clock = self.run_wake(["unknown"])
        self.assertEqual((result, sent), ("noaddr", []))
        result, sent, _clock = self.run_wake(["unknown"], force=True)
        self.assertEqual(result, "sent")
        self.assertEqual(len(sent), 1)

    def test_wakes_then_reports_when_host_answers(self):
        ticks = []
        result, sent, clock = self.run_wake(["down", "down", "down", "down", "awake", "up"], tick=ticks.append)
        self.assertEqual(result, "woke")
        self.assertEqual(sent[0], (self.HOST.mac, "GAMING-PC"))
        self.assertLess(clock.now - 100.0, 10.0)
        self.assertTrue(ticks)
        self.assertEqual(ticks[0], 0.0)

    def test_gives_up_after_thirty_seconds_and_resends(self):
        result, sent, clock = self.run_wake(["down"])
        self.assertEqual(result, "timeout")
        self.assertGreaterEqual(clock.now - 100.0, 30.0)
        self.assertLess(clock.now - 100.0, 32.0)
        self.assertGreater(len(sent), 2)

    def test_tick_can_cancel(self):
        result, sent, clock = self.run_wake(["down"], tick=lambda elapsed: elapsed >= 3)
        self.assertEqual(result, "cancelled")
        self.assertLess(clock.now - 100.0, 5.0)
        self.assertTrue(sent)


class SettingsTest(unittest.TestCase):
    def test_defaults_are_off(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = stream.load_settings(pathlib.Path(directory) / "config.ini")
        self.assertEqual(settings, stream.StreamSettings(autostart=False, host="", app="Desktop"))

    def test_round_trip_preserves_other_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.ini"
            path.write_text("[display]\noutput = DP-1\n\n[streaming]\nold = 1\n\n[tailscale]\nhost_address_mode = auto\n")
            stream.save_settings(stream.StreamSettings(True, "6F1A2E0B", "Steam Big Picture"), path)
            text = path.read_text()
            self.assertIn("[display]\noutput = DP-1\n", text)
            self.assertIn("[tailscale]\nhost_address_mode = auto\n", text)
            self.assertNotIn("old = 1", text)
            self.assertEqual(
                stream.load_settings(path), stream.StreamSettings(True, "6F1A2E0B", "Steam Big Picture")
            )
            stream.save_settings(stream.StreamSettings(False, "x", "Desktop"), path)
            self.assertFalse(stream.load_settings(path).autostart)
            self.assertEqual(path.read_text().count("[streaming]"), 1)

    def test_invalid_values_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.ini"
            path.write_text("[streaming]\nautostart = yes please\napp = --evil\nhost = a\n")
            settings = stream.load_settings(path)
            self.assertFalse(settings.autostart)
            self.assertEqual(settings.app, "Desktop")
            path.write_text("not an ini [[[")
            self.assertEqual(stream.load_settings(path), stream.StreamSettings())

    def test_save_rejects_unsafe_app_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.ini"
            for bad in ("", "-rf", "a\nb", "x" * 200):
                with self.assertRaises(ValueError):
                    stream.save_settings(stream.StreamSettings(True, "h", bad), path)

    def test_stream_request_is_two_safe_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "moonlight-stream.request"
            stream.write_stream_request("192.168.1.50", "Steam Big Picture", path)
            self.assertEqual(path.read_text(), "192.168.1.50\nSteam Big Picture\n")
            for host, app in (("-x", "Desktop"), ("a b", "Desktop"), ("h;rm", "Desktop"), ("h", "--x"), ("h", "a\nb"), ("h", "")):
                with self.assertRaises(ValueError, msg=(host, app)):
                    stream.write_stream_request(host, app, path)


class BitrateTest(unittest.TestCase):
    def test_default_bitrate_matches_moonlight_table(self):
        default = stream.default_bitrate
        self.assertEqual(default(1280, 720, 60), 10000)
        self.assertEqual(default(1920, 1080, 30), 10000)
        self.assertEqual(default(1920, 1080, 60), 20000)
        self.assertEqual(default(2560, 1440, 60), 40000)
        self.assertEqual(default(3840, 2160, 60), 80000)
        # Above 60 fps the factor grows with the square root.
        self.assertEqual(default(1920, 1080, 120), 28000)
        self.assertEqual(default(2560, 1440, 144), 62000)
        # Between table entries the factor is interpolated.
        self.assertEqual(default(1600, 900, 60), 15000)  # 5 + 0.45 * 5 = 7.25, times 2
        self.assertGreater(default(1600, 900, 60), default(1280, 720, 60))
        self.assertLess(default(1600, 900, 60), default(1920, 1080, 60))
        self.assertEqual(default(320, 200, 30), 1000)
        self.assertEqual(default(1920, 1080, 60, True), 40000)

    def test_bitrate_is_capped_to_sixty_percent_of_the_link(self):
        self.assertEqual(stream.choose_bitrate(1920, 1080, 60, 1000), 20000)
        self.assertEqual(stream.choose_bitrate(3840, 2160, 60, 100), 60000)
        self.assertEqual(stream.choose_bitrate(3840, 2160, 60, 30), 18000)
        self.assertEqual(stream.choose_bitrate(1920, 1080, 60, None), 20000)
        self.assertEqual(stream.choose_bitrate(3840, 2160, 120, 0.1), 500)
        self.assertEqual(stream.choose_bitrate(3840, 2160, 240, 100000), 160000)

    def test_plan_uses_display_mode_and_link(self):
        network = stream.Network(link_mbps=100.0, link_kind="wired", device="enp2s0", rtt_ms=0.6, loss_pct=0.0)
        plan = stream.plan_settings(3840, 2160, 59940, network)
        self.assertEqual((plan.width, plan.height, plan.fps), (3840, 2160, 60))
        self.assertEqual(plan.default_bitrate, 80000)
        self.assertEqual(plan.bitrate, 60000)
        self.assertTrue(plan.capped)
        fast = stream.plan_settings(1920, 1080, 143981, stream.Network())
        self.assertEqual((fast.fps, fast.bitrate, fast.capped), (144, stream.default_bitrate(1920, 1080, 144), False))

    def test_plan_clamps_oversized_displays(self):
        plan = stream.plan_settings(5120, 2880, 60000, stream.Network())
        self.assertEqual((plan.width, plan.height), (3840, 2160))
        odd = stream.plan_settings(1921, 1081, 60000, stream.Network())
        self.assertEqual((odd.width % 2, odd.height % 2), (0, 0))


class NetworkMeasurementTest(unittest.TestCase):
    PING = (
        "PING 192.168.1.50 (192.168.1.50) 56(84) bytes of data.\n\n"
        "--- 192.168.1.50 ping statistics ---\n"
        "10 packets transmitted, 9 received, 10% packet loss, time 1810ms\n"
        "rtt min/avg/max/mdev = 0.412/0.531/0.822/0.120 ms\n"
    )

    def test_parse_ping(self):
        self.assertEqual(stream.parse_ping(self.PING), (0.531, 10.0))
        dead = "10 packets transmitted, 0 received, 100% packet loss, time 9200ms\n"
        self.assertEqual(stream.parse_ping(dead), (None, 100.0))
        self.assertEqual(stream.parse_ping("garbage"), (None, None))

    def test_parse_nmcli_rate(self):
        self.assertEqual(stream.parse_wifi_rate(" :130 Mbit/s\n*:866 Mbit/s\n :54 Mbit/s\n"), 866.0)
        self.assertIsNone(stream.parse_wifi_rate(" :130 Mbit/s\n"))
        self.assertIsNone(stream.parse_wifi_rate(""))

    def sysfs(self, directory, name, speed=None, wireless=False):
        device = pathlib.Path(directory) / name
        device.mkdir(parents=True)
        if speed is not None:
            (device / "speed").write_text(f"{speed}\n")
        if wireless:
            (device / "wireless").mkdir()
        return device

    def fake_run(self, table):
        def run(command, **_kwargs):
            for prefix, output in table.items():
                if command[: len(prefix)] == list(prefix):
                    if isinstance(output, Exception):
                        raise output
                    return subprocess.CompletedProcess(command, 0, output, "")
            return subprocess.CompletedProcess(command, 1, "", "")

        return run

    def test_wired_link_speed_and_ping(self):
        with tempfile.TemporaryDirectory() as directory:
            self.sysfs(directory, "enp2s0", 1000)
            run = self.fake_run({
                ("ip", "-o", "route", "get"): "192.168.1.50 dev enp2s0 src 192.168.1.20 uid 1000 \\    cache\n",
                ("ping",): self.PING,
            })
            network = stream.measure_network("192.168.1.50", run=run, sysfs=pathlib.Path(directory))
        self.assertEqual(network, stream.Network(1000.0, "wired", "enp2s0", 0.531, 10.0))

    def test_wifi_rate_comes_from_nmcli(self):
        with tempfile.TemporaryDirectory() as directory:
            self.sysfs(directory, "wlan0", wireless=True)
            run = self.fake_run({
                ("ip", "-o", "route", "show", "default"): "default via 192.168.1.1 dev wlan0 proto dhcp metric 600\n",
                ("nmcli",): "*:433 Mbit/s\n :54 Mbit/s\n",
            })
            network = stream.measure_network(None, run=run, sysfs=pathlib.Path(directory))
        self.assertEqual((network.link_mbps, network.link_kind, network.device), (433.0, "wifi", "wlan0"))
        self.assertIsNone(network.rtt_ms)

    def test_unknown_when_tools_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.fake_run({("ip",): FileNotFoundError("ip"), ("ping",): subprocess.TimeoutExpired("ping", 8)})
            network = stream.measure_network("192.168.1.50", run=run, sysfs=pathlib.Path(directory))
        self.assertEqual(network, stream.Network())

    def test_link_down_speed_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            self.sysfs(directory, "enp2s0", -1)
            run = self.fake_run({("ip", "-o", "route", "get"): "192.168.1.50 dev enp2s0 src 192.168.1.20\n"})
            network = stream.measure_network("192.168.1.50", run=run, sysfs=pathlib.Path(directory))
        self.assertIsNone(network.link_mbps)


class ConfRewriteTest(unittest.TestCase):
    VALUES = {"width": "3840", "height": "2160", "fps": "60", "bitrate": "60000"}

    def test_rewrite_changes_only_the_requested_keys(self):
        updated = stream.rewrite_conf(SAMPLE, self.VALUES)
        old, new = SAMPLE.splitlines(), updated.splitlines()
        self.assertEqual(len(old), len(new))
        self.assertEqual(
            [(a, b) for a, b in zip(old, new) if a != b],
            [("bitrate=20000", "bitrate=60000"), ("height=1080", "height=2160"), ("width=1920", "width=3840")],
        )
        # Every other line, including the escaped MAC and certificate, is byte-identical.
        self.assertEqual(stream.parse_hosts(updated), stream.parse_hosts(SAMPLE))
        self.assertIn(r"hosts\1\mac=@ByteArray(\x1c\x1b\r\x8d\xbf\xe9)", updated)
        self.assertTrue(updated.endswith("hosts\\9\\hostname=not-a-host\n"))

    def test_rewrite_is_idempotent_and_keeps_line_endings(self):
        once = stream.rewrite_conf(SAMPLE, self.VALUES)
        self.assertEqual(stream.rewrite_conf(once, self.VALUES), once)
        crlf = SAMPLE.replace("\n", "\r\n")
        self.assertNotIn("\n", stream.rewrite_conf(crlf, self.VALUES).replace("\r\n", ""))

    def test_missing_keys_are_added_to_general_section(self):
        text = "[General]\nhosts\\size=0\nvsync=true\n\n[Other]\nwidth=5\n"
        updated = stream.rewrite_conf(text, self.VALUES)
        general, other = updated.split("[Other]")
        for key, value in self.VALUES.items():
            self.assertIn(f"{key}={value}\n", general)
        self.assertEqual(other, "\nwidth=5\n")
        self.assertIn("vsync=true\n", general)

    def test_empty_file_gets_a_general_section(self):
        self.assertEqual(
            stream.rewrite_conf("", {"width": "1", "fps": "2"}), "[General]\nfps=2\nwidth=1\n"
        )

    def test_write_conf_refuses_while_moonlight_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            conf = root / "config" / "Moonlight Game Streaming Project" / "Moonlight.conf"
            run = root / "run"
            run.mkdir()
            plan = stream.plan_settings(1920, 1080, 60000, stream.Network())
            (run / "moonlight-ready").touch()
            with self.assertRaises(stream.StreamError):
                stream.apply_plan(plan, conf, run)
            self.assertFalse(conf.exists())
            (run / "moonlight-ready").unlink()
            (run / "app-active").write_text("moonlight\n")
            with self.assertRaises(stream.StreamError):
                stream.apply_plan(plan, conf, run)
            (run / "app-active").write_text("firefox\n")
            stream.apply_plan(plan, conf, run)
            self.assertIn("width=1920\n", conf.read_text())

    def test_apply_plan_preserves_file_and_unlocks_high_bitrates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            conf = root / "Moonlight.conf"
            conf.write_bytes(SAMPLE.encode("latin-1"))
            plan = stream.plan_settings(3840, 2160, 120000, stream.Network())
            self.assertGreater(plan.bitrate, 100000)
            stream.apply_plan(plan, conf, root)
            text = conf.read_bytes().decode("latin-1")
            self.assertIn("unlockbitrate=true\n", text)
            self.assertIn(f"bitrate={plan.bitrate}\n", text)
            self.assertIn("fps=120\n", text)
            self.assertEqual(stream.parse_hosts(text), stream.parse_hosts(SAMPLE))
            self.assertEqual([path.name for path in root.iterdir()], ["Moonlight.conf"])


class RequestTest(unittest.TestCase):
    """couchliteos-run-app reads the stream request through take_request; it never sees a shell."""

    def test_take_request_validates_and_always_consumes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "moonlight-stream.request"
            path.write_text("192.168.1.50\nSteam Big Picture\n")
            self.assertEqual(stream.take_request(path), ("192.168.1.50", "Steam Big Picture"))
            self.assertFalse(path.exists())
            for content in ("-x\nDesktop\n", "h\n--x\n", "h\n", "h;rm\nDesktop\n", "h\nDesktop\nextra\n", "\xff\xfe"):
                path.write_text(content, encoding="latin-1")
                self.assertIsNone(stream.take_request(path), content)
                self.assertFalse(path.exists(), content)
            self.assertIsNone(stream.take_request(path))

    def test_take_request_ignores_symlinks_large_and_stale_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "target"
            target.write_text("h\nDesktop\n")
            link = root / "link"
            link.symlink_to(target)
            self.assertIsNone(stream.take_request(link))
            self.assertTrue(target.exists())
            big = root / "big"
            big.write_text("h\n" + "x" * 5000 + "\n")
            self.assertIsNone(stream.take_request(big))
            stale = root / "stale"
            stale.write_text("h\nDesktop\n")
            old = time.time() - 3600
            os.utime(stale, (old, old))
            self.assertIsNone(stream.take_request(stale))
            self.assertFalse(stale.exists())

    def test_command_line_prints_two_lines_for_run_app(self):
        script = pathlib.Path(stream.__file__)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "req"
            path.write_text("pc.lan\nDesktop\n")
            result = subprocess.run(["python3", str(script), "take-request", str(path)], text=True, capture_output=True)
            self.assertEqual((result.returncode, result.stdout), (0, "pc.lan\nDesktop\n"))
            path.write_text("-bad\nDesktop\n")
            result = subprocess.run(["python3", str(script), "take-request", str(path)], text=True, capture_output=True)
            self.assertEqual((result.returncode, result.stdout), (1, ""))


class SummaryTest(unittest.TestCase):
    def test_summary_lines_show_what_was_measured(self):
        network = stream.Network(100.0, "wired", "enp2s0", 0.531, 0.0)
        plan = stream.plan_settings(3840, 2160, 59940, network)
        text = "\n".join(stream.summary_lines("3840x2160@59.94Hz", plan, network))
        for expected in ("3840x2160@59.94Hz", "WIRED 100 MBIT/S", "0.5 MS", "LOSS 0%", "60 MBPS", "80 MBPS", "CAPPED"):
            self.assertIn(expected, text)

    def test_summary_lines_when_nothing_could_be_measured(self):
        plan = stream.plan_settings(1920, 1080, 60000, stream.Network())
        text = "\n".join(stream.summary_lines("1920x1080@60Hz", plan, stream.Network()))
        self.assertIn("LINK SPEED UNKNOWN", text)
        self.assertIn("20 MBPS", text)
        self.assertNotIn("CAPPED", text)

    def test_summary_warns_about_a_lossy_path(self):
        network = stream.Network(1000.0, "wired", "enp2s0", 4.0, 6.0)
        plan = stream.plan_settings(1920, 1080, 60000, network)
        self.assertIn("PACKET LOSS", "\n".join(stream.summary_lines("x", plan, network)))


class GateTest(unittest.TestCase):
    def test_link_up_needs_a_non_loopback_interface_that_is_up(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = pathlib.Path(directory)
            self.assertFalse(stream.link_up(sysfs))
            for name, state in (("lo", "up"), ("enp2s0", "down"), ("wlan0", "dormant"), ("tailscale0", "unknown")):
                (sysfs / name).mkdir()
                (sysfs / name / "operstate").write_text(state + "\n")
            self.assertFalse(stream.link_up(sysfs))
            (sysfs / "enp2s0" / "operstate").write_text("up\n")
            self.assertTrue(stream.link_up(sysfs))

    def test_link_up_without_sysfs_is_false(self):
        self.assertFalse(stream.link_up(pathlib.Path("/nonexistent/net")))

    A = stream.Host(name="A", uuid="U1")
    B = stream.Host(name="B", uuid="U2")

    def test_autostream_host_ignores_a_saved_pc_that_is_not_paired_here(self):
        self.assertEqual(stream.autostream_host([self.A, self.B], stream.StreamSettings(True, "U1")), self.A)
        self.assertIsNone(stream.autostream_host([self.B], stream.StreamSettings(True, "GONE")))
        self.assertEqual(stream.autostream_host([self.B], stream.StreamSettings(True, "")), self.B)
        self.assertIsNone(stream.autostream_host([self.A, self.B], stream.StreamSettings(True, "")))
        self.assertIsNone(stream.autostream_host([], stream.StreamSettings(True, "U1")))

    def test_autostart_label_explains_why_it_is_unavailable(self):
        label = stream.autostart_label
        self.assertIn("PAIR A GAMING PC IN MOONLIGHT FIRST", label([], stream.StreamSettings()))
        self.assertIn("PAIR A GAMING PC IN MOONLIGHT FIRST", label([], stream.StreamSettings(True, "U1")))
        self.assertIn("IGNORED", label([self.B], stream.StreamSettings(True, "GONE")))
        self.assertIn("IGNORED", label([self.A, self.B], stream.StreamSettings(True, "")))
        self.assertEqual(label([self.A], stream.StreamSettings(True, "U1")), "ON")
        self.assertEqual(label([self.A], stream.StreamSettings(False, "U1")), "OFF")

    def test_wake_block_explains_a_missing_mac(self):
        self.assertIn("NETWORK ADDRESS", stream.wake_block(self.A))
        self.assertEqual(stream.wake_block(stream.Host(name="C", mac=b"\x01" * 6)), "")


class Screen:
    """Curses stub: keys come back one per getch, then -1 (timeout) forever."""

    def __init__(self, keys=()):
        self.keys = list(keys)
        self.drawn = []

    def getmaxyx(self):
        return 30, 100

    def erase(self):
        pass

    def border(self, *_args):
        pass

    def addstr(self, _row, _column, text, *_args):
        self.drawn.append(text)

    def addnstr(self, _row, _column, text, *_args):
        self.drawn.append(text)

    def refresh(self):
        pass

    def timeout(self, *_args):
        pass

    def keypad(self, *_args):
        pass

    def getch(self):
        return self.keys.pop(0) if self.keys else -1

    def text(self):
        return "\n".join(self.drawn)


def changed(host, **fields):
    return dataclasses.replace(host, **fields)


class LauncherTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_couch", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        # run() must not read or write the real what's-new marker (test_whatsnew covers it).
        patcher = mock.patch.object(cls.module.whatsnew, "show_once")
        patcher.start()
        cls.addClassCleanup(patcher.stop)

    HOST = stream.Host(
        name="Gaming-PC", uuid="UUID-1", mac=bytes.fromhex("1c1b0d8dbfe9"),
        local="192.168.1.50", apps=("Desktop", "Steam Big Picture"),
    )
    ON = stream.StreamSettings(True, "UUID-1", "Steam Big Picture")

    def launcher(self, keys=()):
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            return self.module.Launcher(Screen(keys))

    def app(self, app_id="moonlight", request="start-moonlight"):
        return self.module.apps.Application(
            id=app_id, name=app_id.upper(), kind="request", request=request, status_id=app_id,
        )

    def world(self, run, hosts=(HOST,), settings=None, link=True):
        """Patch the launcher's view of /run, the paired hosts, our settings, and the network link."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(self.module, "RUN", run))
        stack.enter_context(mock.patch.object(self.module, "HOME_REQUEST", run / "home.request"))
        if callable(link):
            stack.enter_context(mock.patch.object(self.module.stream, "link_up", side_effect=link))
        else:
            stack.enter_context(mock.patch.object(self.module.stream, "link_up", return_value=link))
        stack.enter_context(mock.patch.object(self.module.stream, "load_hosts", return_value=list(hosts)))
        stack.enter_context(
            mock.patch.object(self.module.stream, "load_settings", return_value=settings or stream.StreamSettings())
        )
        return stack


class WakeBeforeMoonlightTest(LauncherTestCase):
    def launch(self, launcher, app, run, hosts=(LauncherTestCase.HOST,)):
        order = []
        launcher.wake_host = mock.Mock(side_effect=lambda *a, **k: order.append("wake") or "woke")

        def request(name):
            order.append("request")
            (run / f"{app.status_id}-ready").touch()

        launcher.request = mock.Mock(side_effect=request)
        with self.world(run, hosts):
            launcher.launch_app(app)
        return order

    def test_moonlight_launch_wakes_the_default_host_first(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            order = self.launch(launcher, self.app(), pathlib.Path(directory))
        self.assertEqual(order, ["wake", "request"])
        self.assertEqual(launcher.wake_host.call_args.args[0], self.HOST)
        self.assertFalse(launcher.wake_host.call_args.kwargs.get("force"))

    def test_other_apps_do_not_wake(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            order = self.launch(launcher, self.app("chiaki-ng", "start-chiaki"), pathlib.Path(directory))
        self.assertEqual(order, ["request"])

    def test_several_paired_pcs_and_none_chosen_wakes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            two = (self.HOST, changed(self.HOST, name="Other", uuid="UUID-2"))
            order = self.launch(launcher, self.app(), pathlib.Path(directory), two)
        self.assertEqual(order, ["request"])

    def test_no_paired_pc_wakes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            order = self.launch(launcher, self.app(), pathlib.Path(directory), ())
        self.assertEqual(order, ["request"])

    def test_a_wake_failure_never_blocks_moonlight(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher = self.launcher()
            launcher.wake_host = mock.Mock(side_effect=OSError("Network is unreachable"))
            launcher.request = mock.Mock(side_effect=lambda name: (run / "moonlight-ready").touch())
            with self.world(run):
                self.assertTrue(launcher.launch_app(self.app()))
        launcher.request.assert_called_once_with("start-moonlight")

    def test_a_running_moonlight_is_resumed_without_waking(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "moonlight-ready").touch()
            launcher = self.launcher()
            launcher.wake_host = mock.Mock()
            launcher.focus_app = mock.Mock(return_value=True)
            with self.world(run):
                self.assertTrue(launcher.launch_app(self.app()))
        launcher.wake_host.assert_not_called()

    def test_wake_host_shows_progress_and_any_key_skips_the_wait(self):
        launcher = self.launcher([27])
        seen = []

        def fake_wake(host, **kwargs):
            seen.append((host, kwargs["force"], kwargs["tick"](3.0)))
            return "cancelled"

        with mock.patch.object(self.module.stream, "wake_and_wait", side_effect=fake_wake):
            self.assertEqual(launcher.wake_host(self.HOST, force=True), "cancelled")
        self.assertEqual(seen, [(self.HOST, True, True)])
        self.assertIn("WAKING GAMING-PC...", launcher.screen.text())

    def test_wake_host_keeps_waiting_when_no_key_is_pressed(self):
        launcher = self.launcher()
        seen = []

        def fake_wake(host, **kwargs):
            seen.append(kwargs["tick"](1.0))
            return "woke"

        with mock.patch.object(self.module.stream, "wake_and_wait", side_effect=fake_wake):
            self.assertEqual(launcher.wake_host(self.HOST), "woke")
        self.assertEqual(seen, [False])


    def test_wake_host_without_any_link_does_not_wait_and_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            with self.world(pathlib.Path(directory), link=False), mock.patch.object(
                self.module.stream, "wake_and_wait", side_effect=AssertionError("must not wait")
            ):
                self.assertEqual(launcher.wake_host(self.HOST, force=True), "nonetwork")

    def test_moonlight_still_starts_without_a_link_and_the_screen_says_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher = self.launcher()
            launcher.request = mock.Mock(side_effect=lambda name: (run / "moonlight-ready").touch())
            with self.world(run, link=False), mock.patch.object(
                self.module.stream, "wake_and_wait", side_effect=AssertionError("must not wait")
            ):
                self.assertTrue(launcher.launch_app(self.app()))
        launcher.request.assert_called_once_with("start-moonlight")
        self.assertIn("NO NETWORK", launcher.screen.text())


class LaunchFailureTest(LauncherTestCase):
    """Moonlight problems offer WAKE PC on the shared error screen (couchliteos_errors)."""

    def setUp(self):
        patcher = mock.patch.object(self.module.errors, "_EXTRA", [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def failed_launch(self, app_id, hosts=(LauncherTestCase.HOST,)):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher = self.launcher()
            launcher.show_launch_failure = mock.Mock(return_value="dismiss")
            launcher.wake_before_moonlight = mock.Mock()
            launcher.request = mock.Mock()
            clock = itertools.chain([0.0], itertools.repeat(100.0))
            with self.world(run, hosts), mock.patch.object(self.module.time, "monotonic", side_effect=lambda: next(clock)):
                self.assertFalse(launcher.launch_app(self.app(app_id, "start-x")))
        return launcher.show_launch_failure.call_args

    def dialog(self, app_id, keys):
        """The real failure screen for this app, driven by `keys`; wake_pc is replaced by a mock."""
        launcher = self.launcher(keys)
        launcher.wake_pc = mock.Mock()
        self.module.register_failure_actions(launcher)  # what main() does at start-up
        result = launcher.show_launch_failure("APP", "exited before ready", app=self.app(app_id))
        return launcher, result

    def test_start_up_registers_the_wake_pc_button_and_its_handler(self):
        screen = Screen()
        with mock.patch.object(self.module.curses, "set_escdelay"), mock.patch.object(
            self.module, "Launcher"
        ) as launcher:
            self.module.main(screen)
        self.assertIn("wake-pc", [action.id for action in self.module.errors.actions_for("app", "moonlight")])
        self.assertNotIn("wake-pc", [action.id for action in self.module.errors.actions_for("app", "chiaki-ng")])
        launcher.return_value.failure_actions.__setitem__.assert_called_once_with(
            "wake-pc", launcher.return_value.wake_from_failure
        )
        launcher.return_value.run.assert_called_once_with()

    def test_a_failed_start_hands_the_app_to_the_failure_screen(self):
        self.assertEqual(self.failed_launch("moonlight").kwargs["app"].id, "moonlight")
        self.assertEqual(self.failed_launch("chiaki-ng").kwargs["app"].id, "chiaki-ng")

    def test_moonlight_failure_offers_wake_pc_as_the_first_choice(self):
        launcher, result = self.dialog("moonlight", [10, 27])
        launcher.wake_pc.assert_called_once_with()
        self.assertIn("WAKE PC", launcher.screen.text())
        self.assertEqual(result, "dismiss")  # woken or not, the screen comes back; ESC leaves it

    def test_other_apps_do_not_offer_wake_pc(self):
        launcher, _result = self.dialog("chiaki-ng", [27])
        self.assertNotIn("WAKE PC", launcher.screen.text())

    def test_back_and_escape_do_not_wake(self):
        for keys in ([self.module.curses.KEY_UP, 10], [27]):  # UP wraps to the last button, BACK
            launcher, result = self.dialog("moonlight", keys)
            launcher.wake_pc.assert_not_called()
            self.assertEqual(result, "dismiss")

    def test_the_wake_button_runs_couchs_wake_and_never_retries_by_itself(self):
        launcher = self.launcher()
        launcher.wake_pc = mock.Mock()
        self.module.register_failure_actions(launcher)
        self.assertFalse(launcher.failure_actions["wake-pc"](self.app()))
        launcher.wake_pc.assert_called_once_with()

    def test_without_a_known_mac_the_wake_flow_explains_itself(self):
        # Replaces "WAKE PC is not offered until a MAC is known": feat/easy's error screen always
        # offers the registered button on Moonlight failures, so the flow says why it cannot help.
        launcher = self.launcher([10])
        with tempfile.TemporaryDirectory() as directory, self.world(pathlib.Path(directory), ()):
            launcher.failure_actions["wake-pc"] = launcher.wake_from_failure
            launcher.failure_actions["wake-pc"](self.app())
        self.assertIn(stream.PAIR_FIRST, launcher.screen.text())
        launcher = self.launcher([10])
        with tempfile.TemporaryDirectory() as directory, self.world(pathlib.Path(directory), (changed(self.HOST, mac=b""),)):
            launcher.wake_from_failure(self.app())
        self.assertIn(stream.NO_MAC, launcher.screen.text())

    def test_plain_dialog_has_back_and_no_wake(self):
        launcher, _result = self.dialog("chiaki-ng", [27])
        self.assertNotIn("WAKE PC", launcher.screen.text())
        self.assertIn("BACK", launcher.screen.text())


class AutostreamTest(LauncherTestCase):
    def autostream(self, launcher, run, settings, hosts=(LauncherTestCase.HOST,), complete=True, link=True):
        marker = run / "setup-complete"
        if complete:
            marker.touch()
        launcher.app_by_id = mock.Mock(return_value=self.app())
        clock = (i * 0.25 for i in itertools.count())
        with self.world(run, hosts, settings, link), mock.patch.object(self.module.setup, "MARKER", marker), \
                mock.patch.object(self.module.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(self.module.curses, "flushinp"):
            return launcher.autostream()

    def test_off_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            launcher.launch_app = mock.Mock()
            self.assertFalse(self.autostream(launcher, pathlib.Path(directory), stream.StreamSettings()))
        launcher.launch_app.assert_not_called()

    def test_counts_down_then_streams_the_chosen_app(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher = self.launcher()
            seen = []

            def launch(app):
                seen.append((app.id, (run / "moonlight-stream.request").read_text()))
                return True

            launcher.launch_app = mock.Mock(side_effect=launch)
            self.assertTrue(self.autostream(launcher, run, self.ON))
            self.assertEqual(seen, [("moonlight", "192.168.1.50\nSteam Big Picture\n")])
            self.assertFalse((run / "moonlight-stream.request").exists())
        self.assertIn("STARTING STREAM", launcher.screen.text())
        self.assertIn("PRESS ANY BUTTON TO CANCEL", launcher.screen.text())

    def test_the_countdown_lasts_about_five_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            launcher.launch_app = mock.Mock(return_value=True)
            self.autostream(launcher, pathlib.Path(directory), self.ON)
        text = launcher.screen.text()
        self.assertIn("5", text)
        self.assertNotIn("6", text.replace("192.168.1.50", ""))

    def test_a_failed_launch_leaves_no_stream_request_behind(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher = self.launcher()
            launcher.launch_app = mock.Mock(return_value=False)
            self.assertFalse(self.autostream(launcher, run, self.ON))
            self.assertFalse((run / "moonlight-stream.request").exists())

    def test_any_key_cancels_the_countdown(self):
        for key in (ord("x"), 10, 27, self.module.curses.KEY_F12, self.module.curses.KEY_F5):
            with tempfile.TemporaryDirectory() as directory:
                launcher = self.launcher([key])
                launcher.launch_app = mock.Mock()
                self.assertFalse(self.autostream(launcher, pathlib.Path(directory), self.ON), key)
            launcher.launch_app.assert_not_called()
            self.assertIn("CANCELLED", launcher.status)

    def test_a_terminal_resize_does_not_cancel_the_countdown(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher([self.module.curses.KEY_RESIZE])
            launcher.launch_app = mock.Mock(return_value=True)
            self.assertTrue(self.autostream(launcher, pathlib.Path(directory), self.ON))

    def test_the_home_button_cancels_and_is_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "home.request").touch()
            launcher = self.launcher()
            launcher.launch_app = mock.Mock()
            with mock.patch.object(self.module, "HOME_REQUEST", run / "home.request"):
                self.assertFalse(self.autostream(launcher, run, self.ON))
            self.assertFalse((run / "home.request").exists())
        launcher.launch_app.assert_not_called()

    def test_waits_for_setup_to_be_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            launcher.launch_app = mock.Mock()
            self.assertFalse(self.autostream(launcher, pathlib.Path(directory), self.ON, complete=False))
        launcher.launch_app.assert_not_called()

    def test_skips_when_an_app_is_already_running(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "app-active").write_text("moonlight\n")
            launcher = self.launcher()
            launcher.launch_app = mock.Mock()
            self.assertFalse(self.autostream(launcher, run, self.ON))
        launcher.launch_app.assert_not_called()

    def test_an_unpaired_host_says_so_and_does_not_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            launcher.launch_app = mock.Mock()
            self.assertFalse(self.autostream(launcher, pathlib.Path(directory), self.ON, hosts=()))
        launcher.launch_app.assert_not_called()
        self.assertIn("STREAMING", launcher.status)

    def test_a_saved_pc_that_is_gone_is_ignored_and_the_setting_is_kept(self):
        other = changed(self.HOST, name="Other", uuid="UUID-2")
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            launcher.launch_app = mock.Mock()
            with mock.patch.object(self.module.stream, "save_settings") as save:
                self.assertFalse(self.autostream(launcher, pathlib.Path(directory), self.ON, hosts=(other,)))
            save.assert_not_called()
        launcher.launch_app.assert_not_called()

    def test_no_network_skips_autostream_and_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            launcher.launch_app = mock.Mock()
            self.assertFalse(self.autostream(launcher, pathlib.Path(directory), self.ON, link=False))
        launcher.launch_app.assert_not_called()
        self.assertIn("NO NETWORK", launcher.status)

    def test_a_link_that_appears_shortly_after_boot_is_waited_for(self):
        answers = iter([False, False, False, True])
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            launcher.launch_app = mock.Mock(return_value=True)
            self.assertTrue(
                self.autostream(launcher, pathlib.Path(directory), self.ON, link=lambda *a: next(answers, True))
            )
        self.assertIn("WAITING FOR THE NETWORK", launcher.screen.text())

    def test_a_bad_settings_file_never_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.launcher()
            with mock.patch.object(self.module.stream, "load_settings", side_effect=OSError("denied")):
                self.assertFalse(launcher.autostream())

    def test_run_calls_autostream_after_the_setup_wizard(self):
        launcher = self.launcher()
        calls = []
        launcher.prepare_session = mock.Mock()
        launcher.setup_wizard = mock.Mock(side_effect=lambda: calls.append("setup"))
        launcher.autostream = mock.Mock(side_effect=lambda: calls.append("autostream"))
        launcher.screen.getch = mock.Mock(side_effect=RuntimeError("stop"))
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module.display, "restore_saved_mode"), mock.patch.object(
            self.module.curses, "curs_set"
        ), mock.patch.object(self.module.curses, "use_default_colors"):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                launcher.run()
        self.assertEqual(calls, ["setup", "autostream"])

    def resume(self, launcher, run):
        with self.world(run), mock.patch.object(self.module, "focus_launcher"), mock.patch.object(
            launcher, "refresh_sleep_support"
        ):
            launcher.on_resume()

    def test_resume_from_sleep_starts_the_chosen_stream_after_the_launcher_is_ready(self):
        launcher = self.launcher()
        calls = []
        launcher.refresh_sleep_support = mock.Mock(side_effect=lambda: calls.append("sleep-support"))
        launcher.autostream = mock.Mock(side_effect=lambda: calls.append("autostream"))
        with tempfile.TemporaryDirectory() as directory, self.world(pathlib.Path(directory)), mock.patch.object(
            self.module, "focus_launcher", side_effect=lambda: calls.append("focus")
        ):
            launcher.on_resume()
        self.assertEqual(calls, ["sleep-support", "focus", "autostream"])

    def test_resume_with_autostart_off_just_reports_the_resume(self):
        launcher = self.launcher()
        launcher.launch_app = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            self.resume(launcher, pathlib.Path(directory))
        launcher.launch_app.assert_not_called()
        self.assertIn("RESUMED", launcher.status)

    def test_resume_with_autostart_on_counts_down_and_streams(self):
        launcher = self.launcher()
        launcher.launch_app = mock.Mock(return_value=True)
        launcher.app_by_id = mock.Mock(return_value=self.app())
        clock = (i * 0.25 for i in itertools.count())
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            marker = run / "setup-complete"
            marker.touch()
            with self.world(run, settings=self.ON), mock.patch.object(self.module.setup, "MARKER", marker), \
                    mock.patch.object(self.module.time, "monotonic", side_effect=lambda: next(clock)), \
                    mock.patch.object(self.module.curses, "flushinp"), \
                    mock.patch.object(self.module, "focus_launcher"), \
                    mock.patch.object(launcher, "refresh_sleep_support"):
                launcher.on_resume()
        launcher.launch_app.assert_called_once()


class StreamingSettingsTest(LauncherTestCase):
    def streaming(self):
        launcher = self.launcher()
        settings = self.module.StreamingSettings(Screen(), launcher)
        settings.message = mock.Mock()
        settings.menu = mock.Mock()
        settings.text_input = mock.Mock()
        return launcher, settings

    def test_streaming_is_a_settings_entry(self):
        self.assertIn("STREAMING", self.module.SETTINGS_MENU)
        settings = self.module.Settings(Screen(), self.launcher())
        settings.selected = self.module.SETTINGS_MENU.index("STREAMING")
        with mock.patch.object(self.module.Settings, "run_streaming") as run:
            self.assertTrue(settings.activate())
        run.assert_called_once_with()

    def test_wake_pc_wakes_the_default_host_and_reports(self):
        launcher, settings = self.streaming()
        launcher.wake_host = mock.Mock(return_value="woke")
        with self.world(pathlib.Path("/nonexistent")):
            settings.wake_pc()
        self.assertEqual(launcher.wake_host.call_args.args[0], self.HOST)
        self.assertTrue(launcher.wake_host.call_args.kwargs["force"])
        self.assertIn("GAMING-PC IS AWAKE", settings.message.call_args.args[1])

    def test_wake_pc_asks_which_pc_when_several_are_paired(self):
        launcher, settings = self.streaming()
        other = changed(self.HOST, name="Other", uuid="UUID-2")
        settings.menu.return_value = 1
        launcher.wake_host = mock.Mock(return_value="timeout")
        with self.world(pathlib.Path("/nonexistent"), (self.HOST, other)):
            settings.wake_pc()
        self.assertEqual(launcher.wake_host.call_args.args[0], other)
        self.assertIn("MAY STILL BE STARTING", settings.message.call_args.args[1])

    def test_wake_pc_explains_a_missing_mac_and_a_missing_pc(self):
        launcher, settings = self.streaming()
        launcher.wake_host = mock.Mock(return_value="woke")
        with self.world(pathlib.Path("/nonexistent"), (changed(self.HOST, mac=b""),)):
            settings.wake_pc()
        launcher.wake_host.assert_not_called()
        self.assertIn("NETWORK ADDRESS", settings.message.call_args.args[1])
        launcher.wake_host.reset_mock()
        with self.world(pathlib.Path("/nonexistent"), ()):
            settings.wake_pc()
        launcher.wake_host.assert_not_called()
        self.assertIn("PAIR A GAMING PC IN MOONLIGHT FIRST", settings.message.call_args.args[1])

    def test_wake_pc_says_no_network_when_there_is_no_link(self):
        launcher, settings = self.streaming()
        launcher.wake_host = mock.Mock(return_value="nonetwork")
        with self.world(pathlib.Path("/nonexistent")):
            settings.wake_pc()
        self.assertIn("NO NETWORK", settings.message.call_args.args[1])

    def test_rows_show_unavailable_features_with_a_reason(self):
        _launcher, settings = self.streaming()
        rows = "\n".join(settings.rows([], stream.StreamSettings()))
        self.assertIn("PAIR A GAMING PC IN MOONLIGHT FIRST", rows)
        self.assertIn("WAKE PC  UNAVAILABLE", rows)
        rows = "\n".join(settings.rows([changed(self.HOST, mac=b"")], stream.StreamSettings()))
        self.assertIn("WAKE PC  UNAVAILABLE", rows)
        rows = "\n".join(settings.rows([self.HOST], self.ON))
        self.assertIn("ON", rows)
        self.assertIn("Steam Big Picture", rows)
        self.assertNotIn("UNAVAILABLE", rows)

    def test_launcher_wake_pc_is_the_same_flow(self):
        launcher = self.launcher()
        with mock.patch.object(self.module.StreamingSettings, "wake_pc") as wake:
            launcher.wake_pc()
        wake.assert_called_once_with()

    def test_toggle_autostart_saves_settings_and_picks_the_only_pc(self):
        _launcher, settings = self.streaming()
        with mock.patch.object(self.module.stream, "save_settings") as save:
            settings.toggle_autostart(stream.StreamSettings(), [self.HOST])
            save.assert_called_once_with(stream.StreamSettings(True, "UUID-1", "Desktop"))
            save.reset_mock()
            settings.toggle_autostart(self.ON, [self.HOST])
            save.assert_called_once_with(stream.StreamSettings(False, "UUID-1", "Steam Big Picture"))

    def test_toggle_autostart_without_any_paired_pc_stays_off(self):
        _launcher, settings = self.streaming()
        with mock.patch.object(self.module.stream, "save_settings") as save:
            settings.toggle_autostart(stream.StreamSettings(), [])
        save.assert_not_called()
        self.assertIn("PAIR A GAMING PC IN MOONLIGHT FIRST", settings.message.call_args.args[1])

    def test_toggle_autostart_on_asks_which_pc_when_several_are_paired(self):
        _launcher, settings = self.streaming()
        other = changed(self.HOST, name="Other", uuid="UUID-2")
        with mock.patch.object(self.module.stream, "save_settings") as save:
            settings.menu.return_value = 1
            settings.toggle_autostart(stream.StreamSettings(), [self.HOST, other])
            save.assert_called_once_with(stream.StreamSettings(True, "UUID-2", "Desktop"))
            save.reset_mock()
            settings.menu.return_value = None
            settings.toggle_autostart(stream.StreamSettings(), [self.HOST, other])
            save.assert_not_called()

    def test_toggle_autostart_cannot_switch_on_for_a_pc_that_is_gone(self):
        _launcher, settings = self.streaming()
        with mock.patch.object(self.module.stream, "save_settings") as save:
            settings.menu.return_value = None  # the PC chooser is dismissed
            settings.toggle_autostart(stream.StreamSettings(False, "GONE", "Desktop"), [self.HOST])
        save.assert_not_called()

    def test_choose_app_uses_the_cached_apps_or_a_typed_name(self):
        _launcher, settings = self.streaming()
        with mock.patch.object(self.module.stream, "save_settings") as save:
            settings.menu.return_value = 1
            settings.choose_app(stream.StreamSettings(True, "UUID-1", "Desktop"), self.HOST)
            save.assert_called_once_with(stream.StreamSettings(True, "UUID-1", "Steam Big Picture"))
            save.reset_mock()
            settings.menu.return_value = 2  # TYPE A NAME
            settings.text_input.return_value = "  Emulation Station "
            settings.choose_app(stream.StreamSettings(True, "UUID-1", "Desktop"), self.HOST)
            save.assert_called_once_with(stream.StreamSettings(True, "UUID-1", "Emulation Station"))
            save.reset_mock()
            settings.text_input.return_value = "-bad"
            settings.choose_app(stream.StreamSettings(True, "UUID-1", "Desktop"), self.HOST)
            save.assert_not_called()

    def optimize(self, settings, run, outputs, network=None):
        with self.world(run), mock.patch.object(
            self.module.display, "query_outputs", return_value=outputs
        ), mock.patch.object(
            self.module.stream, "measure_network", return_value=network or stream.Network()
        ) as measure, mock.patch.object(self.module.stream, "apply_plan") as apply:
            settings.optimize()
        return measure, apply

    def test_optimize_writes_the_display_mode_and_a_measured_bitrate(self):
        _launcher, settings = self.streaming()
        display = self.module.display
        output = display.Output("DP-1", "TV", True, (display.Mode(3840, 2160, 59940, True, True),))
        network = stream.Network(100.0, "wired", "enp2s0", 0.5, 0.0)
        with tempfile.TemporaryDirectory() as directory:
            measure, apply = self.optimize(settings, pathlib.Path(directory), [output], network)
        measure.assert_called_once_with("192.168.1.50")
        plan = apply.call_args.args[0]
        self.assertEqual((plan.width, plan.height, plan.fps, plan.bitrate), (3840, 2160, 60, 60000))
        self.assertIn("60 MBPS", settings.message.call_args.args[1])

    def test_optimize_refuses_while_moonlight_runs(self):
        _launcher, settings = self.streaming()
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "moonlight-ready").touch()
            measure, apply = self.optimize(settings, run, [])
        measure.assert_not_called()
        apply.assert_not_called()
        self.assertIn("CLOSE MOONLIGHT", settings.message.call_args.args[1])

    def test_optimize_reports_a_missing_display_and_writes_nothing(self):
        _launcher, settings = self.streaming()
        with tempfile.TemporaryDirectory() as directory:
            measure, apply = self.optimize(settings, pathlib.Path(directory), [])
        measure.assert_not_called()
        apply.assert_not_called()
        self.assertIn("NO DISPLAY MODE", settings.message.call_args.args[1])

    def test_optimize_needs_a_network_link_and_writes_nothing_without_one(self):
        _launcher, settings = self.streaming()
        display = self.module.display
        output = display.Output("DP-1", "TV", True, (display.Mode(1920, 1080, 60000, True, True),))
        with tempfile.TemporaryDirectory() as directory, self.world(pathlib.Path(directory), link=False), \
                mock.patch.object(display, "query_outputs", return_value=[output]), \
                mock.patch.object(self.module.stream, "measure_network") as measure, \
                mock.patch.object(self.module.stream, "apply_plan") as apply:
            settings.optimize()
        measure.assert_not_called()
        apply.assert_not_called()
        self.assertIn("NO NETWORK", settings.message.call_args.args[1])

    def test_optimize_reports_a_write_failure(self):
        _launcher, settings = self.streaming()
        display = self.module.display
        output = display.Output("DP-1", "TV", True, (display.Mode(1920, 1080, 60000, True, True),))
        with tempfile.TemporaryDirectory() as directory, self.world(pathlib.Path(directory)), mock.patch.object(
            display, "query_outputs", return_value=[output]
        ), mock.patch.object(self.module.stream, "measure_network", return_value=stream.Network()), mock.patch.object(
            self.module.stream, "apply_plan", side_effect=PermissionError("denied")
        ):
            settings.optimize()
        self.assertIn("NOT CHANGED", settings.message.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
