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


class SoundRestoreTest(base.WizardTestCase):
    """The tone test moves the default output around; only a heard tone may keep the move."""

    @staticmethod
    def sinks(default):
        names = {1: "Analog", 2: "HDMI 1", 3: "USB Headset"}
        return [audio.Sink(number, name, number == default) for number, name in names.items()]

    def run_sound(self, ui, default=3, system=None, **extra):
        sinks = self.sinks(default)
        system = system or base.FakeSystem(sink_list=sinks)
        return self.wizard(ui, system, **extra).choose_sound(sinks)

    # Candidates are tried HDMI first (2), then the rest in list order (1, 3).

    def test_skipping_the_sound_test_puts_the_original_output_back(self):
        for answer in ("SKIP SOUND", None):
            with self.subTest(answer=answer):
                outcome = self.run_sound(base.FakeUI("NO, TRY", answer))
                self.assertEqual(outcome, "skipped")
                self.assertEqual(self.system.default_sinks, [2, 1, 3])

    def test_skipping_on_the_first_output_puts_the_original_output_back(self):
        self.assertEqual(self.run_sound(base.FakeUI("SKIP SOUND")), "skipped")
        self.assertEqual(self.system.default_sinks, [2, 3])

    def test_hearing_nothing_anywhere_puts_the_original_output_back(self):
        ui = base.FakeUI("NO, TRY", "NO, TRY", "NO, I HEARD NOTHING", "CONTINUE")
        self.assertEqual(self.run_sound(ui, default=1), "failed")
        self.assertEqual(self.system.default_sinks, [2, 1, 3, 1])

    def test_a_heard_tone_keeps_that_output(self):
        self.assertEqual(self.run_sound(base.FakeUI("NO, TRY", "YES")), "done")
        self.assertEqual(self.system.default_sinks, [2, 1])

    def test_playing_again_never_goes_back_to_the_original_output(self):
        self.assertEqual(self.run_sound(base.FakeUI("PLAY AGAIN", "YES")), "done")
        self.assertEqual(set(self.system.default_sinks), {2})

    def test_an_error_while_playing_the_tone_still_puts_the_original_output_back(self):
        def tone():
            raise RuntimeError("no player")

        with self.assertRaises(RuntimeError):
            self.run_sound(base.FakeUI(), tone=tone)
        self.assertEqual(self.system.default_sinks, [2, 3])

    def test_no_known_default_means_nothing_is_restored(self):
        self.assertEqual(self.run_sound(base.FakeUI("SKIP SOUND"), default=None), "skipped")
        self.assertEqual(self.system.default_sinks, [2])

    def test_a_restore_that_fails_does_not_crash_the_wizard(self):
        class Stubborn(base.FakeSystem):
            def set_default_sink(self, sink_id):
                super().set_default_sink(sink_id)
                if len(self.default_sinks) > 1:
                    raise RuntimeError("wpctl failed")

        system = Stubborn(sink_list=self.sinks(3))
        self.assertEqual(self.run_sound(base.FakeUI("SKIP SOUND"), system=system), "skipped")
        self.assertEqual(system.default_sinks, [2, 3])

    def test_the_display_and_sound_step_restores_too(self):
        modes = base.DisplayAndSoundFlowTest.MODES
        system = base.FakeSystem(modes=modes, sink_list=self.sinks(3))
        ui = base.FakeUI("KEEP CURRENT", "SKIP SOUND")
        self.assertEqual(self.wizard(ui, system).step_display(), "done")
        self.assertEqual(system.default_sinks[-1], 3)


if __name__ == "__main__":
    unittest.main()
