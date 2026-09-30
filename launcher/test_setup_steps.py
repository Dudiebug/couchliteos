"""Wizard steps after the network: controller scan cleanup, sound test, redo, streaming PC.

Shares the fakes in test_setup.py. Only `base.<name>` is used so that the test
classes in that module are not collected a second time from here.
"""

import unittest
from unittest import mock

import moonlightos_audio as audio
import moonlightos_setup as setup
import test_setup as base

MAX_COLUMNS = 76


class ScanCleanupTest(base.WizardTestCase):
    """Bluetooth discovery must not be left running once the wizard is done with it."""

    def run_controller(self, ui, bluetooth, system=None):
        system = system or base.FakeSystem(after_pairing=[base.PAD])
        bluetooth.system = system
        return self.wizard(ui, system, bluetooth).step_controller()

    @staticmethod
    def scan_commands(bluetooth):
        return [name for name, _fields in bluetooth.requests if name in ("start_scan", "stop_scan", "pair")]

    def assertScanStopped(self, bluetooth):
        commands = self.scan_commands(bluetooth)
        self.assertIn("start_scan", commands)
        self.assertEqual(commands[-1], "stop_scan", commands)

    def test_scanning_stops_before_pairing_starts_and_stays_stopped(self):
        bluetooth = base.FakeBluetooth([base.XBOX], states=["working", "completed"])
        self.assertEqual(self.run_controller(base.FakeUI("PAIR", "XBOX", "OK"), bluetooth), "done")
        commands = self.scan_commands(bluetooth)
        self.assertLess(commands.index("stop_scan"), commands.index("pair"), commands)
        self.assertScanStopped(bluetooth)

    def test_a_pairing_the_service_rejects_still_ends_with_the_scan_stopped(self):
        class Rejecting(base.FakeBluetooth):
            def request(self, command, **fields):
                super().request(command, **fields)
                return None if command == "pair" else {"ok": True}

        bluetooth = Rejecting([base.XBOX])
        ui = base.FakeUI("PAIR", "XBOX", "CONTINUE ANYWAY")
        self.assertEqual(self.run_controller(ui, bluetooth), "failed")
        self.assertScanStopped(bluetooth)

    def test_a_failed_pairing_ends_with_the_scan_stopped(self):
        bluetooth = base.FakeBluetooth([base.XBOX], states=["failed"])
        ui = base.FakeUI("PAIR", "XBOX", "CONTINUE ANYWAY")
        self.assertEqual(self.run_controller(ui, bluetooth), "failed")
        self.assertScanStopped(bluetooth)

    def test_a_controller_that_never_answers_the_a_button_ends_with_the_scan_stopped(self):
        bluetooth = base.FakeBluetooth([base.XBOX])
        system = base.FakeSystem(after_pairing=[base.PAD], pressed=False)
        ui = base.FakeUI("PAIR", "XBOX", "CONTINUE ANYWAY")
        self.assertEqual(self.run_controller(ui, bluetooth, system), "failed")
        self.assertScanStopped(bluetooth)

    def test_nothing_found_and_back_ends_with_the_scan_stopped(self):
        bluetooth = base.FakeBluetooth([])
        ui = base.FakeUI("PAIR", "XBOX", "BACK", "SKIP")
        self.assertEqual(self.run_controller(ui, bluetooth, base.FakeSystem()), "skipped")
        self.assertScanStopped(bluetooth)

    def test_an_error_while_searching_still_stops_the_scan(self):
        class Breaking(base.FakeUI):
            def wait(self, title, lines, done, timeout, *, big=None):
                raise RuntimeError("screen lost")

        bluetooth = base.FakeBluetooth([base.XBOX])
        with self.assertRaises(RuntimeError):
            self.run_controller(Breaking("PAIR", "XBOX"), bluetooth)
        self.assertScanStopped(bluetooth)


if __name__ == "__main__":
    unittest.main()
