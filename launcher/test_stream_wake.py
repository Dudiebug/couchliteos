"""couchliteos_stream: the WAKE PC entry point, the take-request CLI and small validators."""

import testenv  # noqa: F401  (first: scratch run and state directories)
import contextlib
import io
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import couchliteos_stream as stream


MAC = bytes.fromhex("0a1b2c3d4e5f")
IP_BRIEF = (
    "lo               UNKNOWN        127.0.0.1/8\n"
    "enp3s0           UP             192.168.50.20/24\n"
)


class SendMagicTest(unittest.TestCase):
    def test_wakes_on_every_subnet_the_appliance_is_on(self):
        host = stream.Host("PC", mac=MAC, local="192.168.50.10")
        completed = subprocess.CompletedProcess([], 0, IP_BRIEF, "")
        with mock.patch.object(stream.subprocess, "run", return_value=completed) as run, \
             mock.patch.object(stream, "send_wol", return_value=6) as send:
            self.assertEqual(stream.send_magic(host), 6)
        self.assertEqual(run.call_args.args[0], ["ip", "-brief", "-4", "address", "show", "up"])
        self.assertFalse(run.call_args.kwargs["check"])
        self.assertIn("timeout", run.call_args.kwargs)
        send.assert_called_once_with(MAC, ["255.255.255.255", "192.168.50.255", "192.168.50.10"])

    def test_without_ip_falls_back_to_the_hosts_own_slash_24(self):
        host = stream.Host("PC", mac=MAC, local="10.0.7.9")
        for error in (FileNotFoundError("ip"), subprocess.TimeoutExpired(["ip"], 3)):
            with mock.patch.object(stream.subprocess, "run", side_effect=error), \
                 mock.patch.object(stream, "send_wol", return_value=4) as send:
                self.assertEqual(stream.send_magic(host), 4)
            send.assert_called_once_with(MAC, ["255.255.255.255", "10.0.7.255", "10.0.7.9"])

    def test_host_known_only_by_name_still_gets_the_global_broadcast(self):
        host = stream.Host("PC", mac=MAC, manual="gaming-pc.lan")
        completed = subprocess.CompletedProcess([], 1, "", "error")
        with mock.patch.object(stream.subprocess, "run", return_value=completed), \
             mock.patch.object(stream, "send_wol", return_value=2) as send:
            stream.send_magic(host)
        send.assert_called_once_with(MAC, ["255.255.255.255"])

    def test_magic_packet_layout(self):
        packet = stream.magic_packet(MAC)
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:], MAC * 16)
        for bad in (b"", MAC[:5], MAC + b"\x00"):
            with self.assertRaises(ValueError):
                stream.magic_packet(bad)


class MoonlightRunningTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = pathlib.Path(directory.name)

    def test_nothing_running(self):
        self.assertFalse(stream.moonlight_running(self.run_dir))
        self.assertFalse(stream.moonlight_running(self.run_dir / "missing"))

    def test_ready_marker(self):
        (self.run_dir / "moonlight-ready").touch()
        self.assertTrue(stream.moonlight_running(self.run_dir))

    def test_active_app_marker(self):
        active = self.run_dir / "app-active"
        active.write_text("moonlight\n")
        self.assertTrue(stream.moonlight_running(self.run_dir))
        active.write_text("firefox\n")
        self.assertFalse(stream.moonlight_running(self.run_dir))
        active.write_bytes(b"moonlight\xff")
        self.assertFalse(stream.moonlight_running(self.run_dir))


class TakeRequestCommandTest(unittest.TestCase):
    def main(self, argv):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = stream.main(argv)
        return code, output.getvalue(), errors.getvalue()

    def test_prints_host_and_app_on_two_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "moonlight-stream.request"
            path.write_bytes(b"pc.lan\nSteam Big Picture\n")
            self.assertEqual(self.main(["take-request", str(path)]), (0, "pc.lan\nSteam Big Picture\n", ""))
            self.assertFalse(path.exists())

    def test_missing_or_invalid_request_exits_1_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "moonlight-stream.request"
            self.assertEqual(self.main(["take-request", str(path)]), (1, "", ""))
            path.write_bytes(b"-oProxyCommand=x\nDesktop\n")
            self.assertEqual(self.main(["take-request", str(path)]), (1, "", ""))
            self.assertFalse(path.exists())

    def test_stale_request_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "moonlight-stream.request"
            path.write_bytes(b"pc.lan\nDesktop\n")
            old = time.time() - stream.REQUEST_MAX_AGE - 5
            os.utime(path, (old, old))
            self.assertEqual(self.main(["take-request", str(path)])[0], 1)

    def test_default_path(self):
        with mock.patch.object(stream, "take_request", return_value=None) as take:
            self.assertEqual(self.main(["take-request"])[0], 1)
        take.assert_called_once_with(stream.STREAM_REQUEST)

    def test_usage_errors(self):
        for argv in ([], ["take"], ["take-request", "a", "b"], ["--help"]):
            code, output, errors = self.main(argv)
            self.assertEqual((code, output), (2, ""), argv)
            self.assertIn("usage:", errors)


class ValidatorTest(unittest.TestCase):
    def test_valid_host(self):
        for value in ("pc", "pc.lan", "192.168.1.5", "fe80::1", "a" * 253, "gaming_pc"):
            self.assertTrue(stream.valid_host(value), value)
        for value in ("", "-pc", "pc lan", "pc;rm", "a" * 254, "pc\n", "$(x)", "pc/1"):
            self.assertFalse(stream.valid_host(value), value)

    def test_valid_app(self):
        for value in ("Desktop", "Steam Big Picture", "a" * stream.APP_MAX, "Café ★"):
            self.assertTrue(stream.valid_app(value), value)
        for value in ("", " Desktop", "Desktop ", "-x", "a" * (stream.APP_MAX + 1), "Desk\ttop", "a\nb"):
            self.assertFalse(stream.valid_app(value), value)

    def test_wake_block(self):
        self.assertEqual(stream.wake_block(stream.Host("PC", mac=MAC)), "")
        self.assertEqual(stream.wake_block(stream.Host("PC")), stream.NO_MAC)

    def test_qt_value_helpers(self):
        self.assertEqual(stream.decode_value('  "a\\x41;b"  '), "aA;b")
        self.assertEqual(stream.decode_value('"'), '"')
        self.assertEqual(stream.byte_array("@ByteArray(abc)"), "abc")
        self.assertIsNone(stream.byte_array("@ByteArray(abc"))
        self.assertIsNone(stream.byte_array("abc"))
        self.assertEqual(stream.as_text("Caf\xc3\xa9"), "Café")
        self.assertEqual(stream.as_text("\xff"), "�")


if __name__ == "__main__":
    unittest.main()
