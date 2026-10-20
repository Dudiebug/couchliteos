import testenv  # noqa: F401  (first: scratch run and state directories)
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_display as display


SAMPLE = '''DP-1 "Dell Inc. DELL U2723QE ABC123"
  Enabled: yes
  Modes:
    3840x2160 px, 60.000000 Hz (preferred, current)
    3840x2160 px, 30.000000 Hz
    1920x1080 px, 120.000000 Hz
    1920x1080 px, 60.000000 Hz
HDMI-A-1 "Sony TV XYZ"
  Enabled: no
  Modes:
    1920x1080 px, 60.000000 Hz (preferred)
'''


class DisplayTest(unittest.TestCase):
    def test_parses_only_advertised_output_modes(self):
        outputs = display.parse_wlr_randr(SAMPLE)
        self.assertEqual([item.name for item in outputs], ["DP-1", "HDMI-A-1"])
        self.assertEqual(outputs[0].current_mode.argument, "3840x2160@60Hz")
        self.assertEqual(len(outputs[0].modes), 4)
        self.assertFalse(outputs[1].enabled)

    def test_invalid_resolution_refresh_combination_is_absent(self):
        output = display.parse_wlr_randr(SAMPLE)[0]
        self.assertIsNone(display.find_mode(output, "3840x2160", 120000))
        self.assertIsNotNone(display.find_mode(output, "1920x1080", 120000))

    def test_connector_disappearance_prevents_restore(self):
        saved = {
            "output": "DP-1",
            "identity": display.parse_wlr_randr(SAMPLE)[0].identity,
            "resolution": "1920x1080",
            "refresh_mhz": "120000",
        }
        with mock.patch.object(display, "load_saved_display", return_value=saved), mock.patch.object(
            display, "query_outputs", return_value=[]
        ), mock.patch.object(display, "apply_mode") as apply:
            self.assertFalse(display.restore_saved_mode())
            apply.assert_not_called()

    def test_saved_mode_disappearance_prevents_restore(self):
        output = display.parse_wlr_randr(SAMPLE)[0]
        saved = {
            "output": "DP-1",
            "identity": output.identity,
            "resolution": "2560x1440",
            "refresh_mhz": "60000",
        }
        with mock.patch.object(display, "load_saved_display", return_value=saved), mock.patch.object(
            display, "query_outputs", return_value=[output]
        ), mock.patch.object(display, "apply_mode") as apply:
            self.assertFalse(display.restore_saved_mode())
            apply.assert_not_called()

    def test_changed_display_identity_prevents_restore(self):
        output = display.parse_wlr_randr(SAMPLE)[0]
        saved = {
            "output": "DP-1",
            "identity": "different-display",
            "resolution": "1920x1080",
            "refresh_mhz": "120000",
        }
        with mock.patch.object(display, "load_saved_display", return_value=saved), mock.patch.object(
            display, "query_outputs", return_value=[output]
        ), mock.patch.object(display, "apply_mode") as apply:
            self.assertFalse(display.restore_saved_mode())
            apply.assert_not_called()

    def test_save_preserves_malformed_unrelated_config(self):
        output = display.parse_wlr_randr(SAMPLE)[0]
        mode = display.find_mode(output, "1920x1080", 120000)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.ini"
            path.write_text("orphan malformed line\n[launcher]\nautostart = none\n", encoding="utf-8")
            display.save_display(output, mode, path)
            result = path.read_text(encoding="utf-8")
            self.assertIn("orphan malformed line", result)
            self.assertIn("[launcher]\nautostart = none", result)
            self.assertIn("[display]", result)
            self.assertIn("refresh_mhz = 120000", result)

    def test_apply_runs_dryrun_before_real_change(self):
        output = display.parse_wlr_randr(SAMPLE)[0]
        mode = display.find_mode(output, "1920x1080", 120000)
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(display.subprocess, "run", return_value=completed) as run:
            display.apply_mode(output, mode, dryrun=True)
            self.assertIn("--dryrun", run.call_args.args[0])


class RestoreConfirmationTest(unittest.TestCase):
    """A saved mode that blacks out the TV must not be reapplied on every boot."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pending = pathlib.Path(self.directory.name) / "display-restore.pending"
        self.output = display.parse_wlr_randr(SAMPLE)[0]
        self.saved = {
            "output": "DP-1",
            "identity": self.output.identity,
            "resolution": "1920x1080",
            "refresh_mhz": "120000",
        }
        display._unconfirmed = False
        self.addCleanup(setattr, display, "_unconfirmed", False)

    def restore(self, apply=None):
        with mock.patch.object(display, "load_saved_display", return_value=self.saved), mock.patch.object(
            display, "query_outputs", return_value=[self.output]
        ), mock.patch.object(display, "apply_mode", side_effect=apply) as applied:
            return display.restore_saved_mode(self.pending), applied

    def real_applies(self, applied):
        return [call for call in applied.call_args_list if not call.kwargs.get("dryrun")]

    def test_restore_is_marked_pending_before_the_mode_is_applied(self):
        seen = []

        def apply(_output, _mode, dryrun=False):
            if not dryrun:
                seen.append(self.pending.exists())

        result, applied = self.restore(apply)
        self.assertTrue(result)
        self.assertEqual(seen, [True])
        self.assertEqual(len(self.real_applies(applied)), 1)
        self.assertTrue(self.pending.exists())

    def test_unconfirmed_previous_restore_is_skipped_and_reported(self):
        self.pending.write_text("pending", encoding="utf-8")
        result, applied = self.restore()
        self.assertIsNone(result)
        self.assertEqual(self.real_applies(applied), [])
        self.assertTrue(self.pending.exists())

    def test_first_input_confirms_the_restore(self):
        self.restore()
        display.confirm_restore(self.pending)
        self.assertFalse(self.pending.exists())
        result, applied = self.restore()
        self.assertTrue(result)
        self.assertEqual(len(self.real_applies(applied)), 1)

    def test_confirming_without_a_restore_keeps_an_older_skip_marker(self):
        self.pending.write_text("pending", encoding="utf-8")
        display.confirm_restore(self.pending)
        self.assertTrue(self.pending.exists())

    def test_already_active_mode_is_not_marked_or_skipped(self):
        self.saved.update(resolution="3840x2160", refresh_mhz="60000")
        result, applied = self.restore()
        self.assertTrue(result)
        self.assertEqual(applied.call_args_list, [])
        self.assertFalse(self.pending.exists())

    def test_refused_apply_does_not_leave_a_marker(self):
        def refuse(_output, _mode, dryrun=False):
            if not dryrun:
                raise RuntimeError("wlr-randr failed")

        result, _applied = self.restore(refuse)
        self.assertFalse(result)
        self.assertFalse(self.pending.exists())

    def test_unwritable_marker_skips_the_restore(self):
        self.pending = pathlib.Path(self.directory.name) / "missing" / "display-restore.pending"
        result, applied = self.restore()
        self.assertFalse(result)
        self.assertEqual(self.real_applies(applied), [])

    def test_choosing_a_mode_again_clears_the_skip_marker(self):
        mode = display.find_mode(self.output, "1920x1080", 120000)
        config = pathlib.Path(self.directory.name) / "config.ini"
        self.pending.write_text("pending", encoding="utf-8")
        display.save_display(self.output, mode, config, self.pending)
        self.assertFalse(self.pending.exists())

    def test_launcher_restart_in_the_same_session_can_still_confirm_the_restore(self):
        # The first launcher applied the mode, then restarted before any key press:
        # the mode is now active and the mark is still there.
        self.saved.update(resolution="3840x2160", refresh_mhz="60000")
        self.pending.write_text("pending", encoding="utf-8")
        result, applied = self.restore()
        self.assertTrue(result)
        self.assertEqual(applied.call_args_list, [])
        display.confirm_restore(self.pending)
        self.assertFalse(self.pending.exists())


def mode(width, height, hz, **flags):
    return display.Mode(width, height, round(hz * 1000), **flags)


class NativeModeTest(unittest.TestCase):
    """The preferred mode is not always the screen's size: some 4K TVs prefer 1080p at 120 Hz."""

    def test_preferred_mode_that_is_the_biggest_stays(self):
        modes = display.parse_wlr_randr(SAMPLE)[0].modes
        self.assertEqual(display.native_mode(modes), mode(3840, 2160, 60, preferred=True, current=True))

    def test_4k_tv_preferring_1080p_120_gets_4k_60(self):
        modes = (mode(1920, 1080, 120, preferred=True, current=True), mode(3840, 2160, 30),
                 mode(3840, 2160, 59.94), mode(3840, 2160, 60), mode(3840, 2160, 120),
                 mode(4096, 2160, 60), mode(1920, 1080, 60))
        self.assertEqual(display.native_mode(modes), mode(3840, 2160, 60))

    def test_4k_only_at_30_hz_keeps_the_preferred_mode(self):
        modes = (mode(1920, 1080, 120, preferred=True), mode(3840, 2160, 30), mode(3840, 2160, 24))
        self.assertEqual(display.native_mode(modes), mode(1920, 1080, 120, preferred=True))

    def test_high_refresh_monitor_keeps_its_own_rate(self):
        modes = (mode(2560, 1440, 144, preferred=True), mode(2560, 1440, 60), mode(1920, 1080, 60))
        self.assertEqual(display.native_mode(modes), mode(2560, 1440, 144, preferred=True))

    def test_bigger_mode_only_above_60_hz_takes_the_slowest(self):
        modes = (mode(1920, 1080, 60, preferred=True), mode(3840, 2160, 144), mode(3840, 2160, 120))
        self.assertEqual(display.native_mode(modes), mode(3840, 2160, 120))

    def test_no_preferred_mode_takes_the_biggest(self):
        modes = (mode(1280, 720, 60), mode(1920, 1080, 60))
        self.assertEqual(display.native_mode(modes), mode(1920, 1080, 60))
        self.assertIsNone(display.native_mode(()))


TV_1080P_PREFERRED = '''HDMI-A-1 "Some TV 4K"
  Enabled: yes
  Modes:
    1920x1080 px, 120.000000 Hz (preferred, current)
    3840x2160 px, 60.000000 Hz
    1920x1080 px, 60.000000 Hz
'''


class NativeAtBootTest(unittest.TestCase):
    """With no saved mode, the screen's native mode replaces the compositor's choice once."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pending = pathlib.Path(self.directory.name) / "display-restore.pending"
        display._unconfirmed = False
        self.addCleanup(setattr, display, "_unconfirmed", False)

    def restore(self, text=TV_1080P_PREFERRED, apply=None):
        with mock.patch.object(display, "load_saved_display", return_value={}), mock.patch.object(
            display, "query_outputs", return_value=display.parse_wlr_randr(text)
        ), mock.patch.object(display, "apply_mode", side_effect=apply) as applied,                 mock.patch.object(display, "log"):
            result = display.restore_saved_mode(self.pending)
        return result, [call.args[1] for call in applied.call_args_list if not call.kwargs.get("dryrun")]

    def test_switches_to_4k_and_marks_it_pending(self):
        result, applied = self.restore()
        self.assertTrue(result)
        self.assertEqual(applied, [mode(3840, 2160, 60)])
        self.assertTrue(self.pending.exists())
        display.confirm_restore(self.pending)
        self.assertFalse(self.pending.exists())

    def test_unconfirmed_native_mode_is_not_tried_again_quietly(self):
        self.pending.write_text("pending", encoding="utf-8")
        result, applied = self.restore()
        self.assertIs(result, False)
        self.assertEqual(applied, [])

    def test_native_mode_already_active_changes_nothing(self):
        result, applied = self.restore(SAMPLE)
        self.assertTrue(result)
        self.assertEqual(applied, [])
        self.assertFalse(self.pending.exists())

    def test_refused_mode_leaves_no_marker(self):
        def refuse(_output, _mode, dryrun=False):
            if not dryrun:
                raise RuntimeError("wlr-randr failed")

        result, _applied = self.restore(apply=refuse)
        self.assertFalse(result)
        self.assertFalse(self.pending.exists())


if __name__ == "__main__":
    unittest.main()


class RestoreAtBootTest(unittest.TestCase):
    def run_boot(self, results, missing):
        calls, slept = [], []

        def restore():
            calls.append(1)
            display._missing = missing[len(calls) - 1]
            return results[len(calls) - 1]

        with mock.patch.object(display, "restore_saved_mode", restore), \
                mock.patch.object(display, "log"):
            result = display.restore_at_boot(attempts=3, delay=0.5, sleep=slept.append)
        return result, len(calls), slept

    def test_retries_while_the_screen_is_not_ready(self):
        self.assertEqual(self.run_boot([False, False, True], [True, True, False]), (True, 3, [0.5, 0.5]))

    def test_gives_up_after_the_attempts(self):
        self.assertEqual(self.run_boot([False] * 3, [True] * 3), (False, 3, [0.5, 0.5]))

    def test_no_retry_for_other_failures_or_skips(self):
        self.assertEqual(self.run_boot([False], [False]), (False, 1, []))
        self.assertEqual(self.run_boot([None], [False]), (None, 1, []))
        self.assertEqual(self.run_boot([True], [False]), (True, 1, []))
