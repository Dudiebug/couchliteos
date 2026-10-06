"""couchliteos_display: reading the saved [display] section, wlr-randr errors and mode labels.

test_display.py mocks load_saved_display everywhere; these tests exercise the parser
itself, the save -> load round trip, and the wlr-randr wrappers.
"""

import testenv  # noqa: F401  (first: scratch run and state directories)
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import couchliteos_display as display


SAMPLE = '''DP-1 "Dell Inc. DELL U2723QE ABC123"
  Enabled: yes
  Modes:
    3840x2160 px, 60.000000 Hz (preferred, current)
    1920x1080 px, 59.940000 Hz
HDMI-A-1 "Sony TV XYZ"
  Enabled: no
  Modes:
    1920x1080 px, 60.000000 Hz (preferred)
'''
IDENTITY = display.parse_wlr_randr(SAMPLE)[0].identity


class LoadSavedDisplayTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = pathlib.Path(directory.name) / "config.ini"

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        return display.load_saved_display(self.path)

    def section(self, **overrides):
        values = {"output": "DP-1", "identity": IDENTITY, "resolution": "1920x1080", "refresh_mhz": "59940"}
        values.update(overrides)
        return "[display]\n" + "".join(f"{key} = {value}\n" for key, value in values.items() if value is not None)

    def test_valid_section(self):
        self.assertEqual(
            self.load("[launcher]\nautostart = none\n" + self.section() + "[other]\noutput = HDMI-A-9\n"),
            {"output": "DP-1", "identity": IDENTITY, "resolution": "1920x1080", "refresh_mhz": "59940"},
        )

    def test_missing_file_is_empty(self):
        self.assertEqual(display.load_saved_display(self.path), {})

    def test_section_name_and_keys_are_case_and_space_insensitive(self):
        text = "  [ Display ]  \nOUTPUT=DP-1\n Identity =  %s \nresolution=1920x1080\nRefresh_MHz = 60000\n" % IDENTITY
        self.assertEqual(self.load(text)["refresh_mhz"], "60000")
        self.assertEqual(self.load(text)["output"], "DP-1")

    def test_keys_outside_the_display_section_are_ignored(self):
        text = "output = DP-1\n[launcher]\nidentity = %s\n[display]\nresolution = 1920x1080\nrefresh_mhz = 60000\n" % IDENTITY
        self.assertEqual(self.load(text), {})

    def test_unknown_keys_and_malformed_lines_do_not_matter(self):
        text = self.section() + "colour = blue\nnot a key value line\n"
        self.assertEqual(set(self.load(text)), {"output", "identity", "resolution", "refresh_mhz"})

    def test_every_field_is_required(self):
        for field in ("output", "identity", "resolution", "refresh_mhz"):
            self.assertEqual(self.load(self.section(**{field: None})), {}, field)

    def test_invalid_values_are_rejected(self):
        bad = {
            "output": ["DP 1", "DP-1;rm", "../x", ""],
            "identity": ["abc", "has space", "x" * 513, "abcd==="],
            "resolution": ["1920X1080", "0x1080", "1920x0", "1920x1080x2", "native", "1x1"],
            "refresh_mhz": ["60", "60.0", "0600", "1234567890", "-60000"],
        }
        for field, values in bad.items():
            for value in values:
                self.assertEqual(self.load(self.section(**{field: value})), {}, (field, value))

    def test_boundary_values_are_accepted(self):
        self.assertTrue(self.load(self.section(refresh_mhz="1000", resolution="10x10")))
        self.assertTrue(self.load(self.section(identity="abcd", output="eDP-1.2:3_x")))
        self.assertTrue(self.load(self.section(identity="a" * 512 + "==")))

    def test_invalid_utf8_does_not_crash(self):
        self.path.write_bytes(self.section().encode() + b"\xff\xfe\n")
        self.assertEqual(display.load_saved_display(self.path)["output"], "DP-1")

    @unittest.skipUnless(hasattr(os, "O_DIRECTORY"), "save_display fsyncs its directory (POSIX)")
    def test_save_then_load_round_trip_replaces_an_older_section(self):
        output = display.parse_wlr_randr(SAMPLE)[0]
        mode = display.find_mode(output, "1920x1080", 59940)
        self.path.write_text("[display]\noutput = OLD-1\n[launcher]\nautostart = none\n", encoding="utf-8")
        display.save_display(output, mode, self.path, pending=self.path.with_name("pending"))
        self.assertEqual(
            display.load_saved_display(self.path),
            {"output": "DP-1", "identity": output.identity, "resolution": "1920x1080", "refresh_mhz": "59940"},
        )
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("OLD-1", text)
        self.assertEqual(text.count("[display]"), 1)
        self.assertIn("[launcher]\nautostart = none\n", text)

    def test_save_refuses_an_output_without_identity(self):
        output = display.Output("DP-1", "", True, ())
        with self.assertRaisesRegex(RuntimeError, "identity"):
            display.save_display(output, display.Mode(1920, 1080, 60000), self.path)
        self.assertFalse(self.path.exists())


class WlrRandrTest(unittest.TestCase):
    def completed(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(["wlr-randr"], returncode, stdout, stderr)

    def test_query_outputs_parses_the_command_output(self):
        with mock.patch.object(display.subprocess, "run", return_value=self.completed(stdout=SAMPLE)) as run:
            outputs = display.query_outputs()
        self.assertEqual([item.name for item in outputs], ["DP-1", "HDMI-A-1"])
        self.assertEqual(run.call_args.args[0], ["wlr-randr"])
        self.assertFalse(run.call_args.kwargs["check"])

    def test_query_outputs_raises_with_the_compositor_error(self):
        with mock.patch.object(display.subprocess, "run", return_value=self.completed(1, stderr=" no compositor \n")):
            with self.assertRaisesRegex(RuntimeError, "^no compositor$"):
                display.query_outputs()
        with mock.patch.object(display.subprocess, "run", return_value=self.completed(1)):
            with self.assertRaisesRegex(RuntimeError, "wlr-randr failed"):
                display.query_outputs()

    def test_apply_mode_reports_stdout_when_stderr_is_empty(self):
        output = display.parse_wlr_randr(SAMPLE)[0]
        mode = display.find_mode(output, "1920x1080", 59940)
        with mock.patch.object(display.subprocess, "run", return_value=self.completed(1, stdout="bad mode")) as run:
            with self.assertRaisesRegex(RuntimeError, "bad mode"):
                display.apply_mode(output, mode)
        self.assertEqual(run.call_args.args[0], ["wlr-randr", "--output", "DP-1", "--mode", "1920x1080@59.94Hz"])

    def test_valid_output_mode_requires_enabled_output_name_identity_and_mode(self):
        outputs = display.parse_wlr_randr(SAMPLE)
        dp, hdmi = outputs
        wanted = display.Mode(1920, 1080, 59940)
        with mock.patch.object(display, "query_outputs", return_value=outputs):
            found = display.valid_output_mode("DP-1", dp.identity, wanted)
            self.assertEqual((found[0].name, found[1].refresh_mhz), ("DP-1", 59940))
            self.assertIsNone(display.valid_output_mode("DP-1", "", wanted))
            self.assertIsNone(display.valid_output_mode("DP-1", hdmi.identity, wanted))
            self.assertIsNone(display.valid_output_mode("DP-1", dp.identity, display.Mode(1920, 1080, 60000)))
            # HDMI-A-1 advertises 1080p60 but is disabled.
            self.assertIsNone(display.valid_output_mode("HDMI-A-1", hdmi.identity, display.Mode(1920, 1080, 60000)))


class ModeLabelTest(unittest.TestCase):
    def test_refresh_labels(self):
        self.assertEqual(display.Mode(1920, 1080, 60000).refresh, "60")
        self.assertEqual(display.Mode(1920, 1080, 59940).refresh, "59.94")
        self.assertEqual(display.Mode(1920, 1080, 23976).argument, "1920x1080@23.976Hz")
        self.assertEqual(display.Mode(1280, 720, 50000).resolution, "1280x720")

    def test_identity_is_url_safe_base64_of_the_description(self):
        self.assertEqual(display.Output("DP-1", "", True, ()).identity, "")
        identity = display.Output("DP-1", "Sony TV ÿ?>", True, ()).identity
        self.assertRegex(identity, r"^[A-Za-z0-9_-]+={0,2}$")

    def test_active_output_is_the_enabled_one_with_a_current_mode(self):
        outputs = display.parse_wlr_randr(SAMPLE)
        self.assertEqual(display.active_output(outputs).name, "DP-1")
        self.assertIsNone(display.active_output(outputs[1:]))


class LogTest(unittest.TestCase):
    def test_log_line_is_single_line_and_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "logs" / "display.log"
            display.log("mode\nfaked line\x1b[2J", path)
            display.log("second", path)
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith(" mode?faked line?[2J"))
        self.assertTrue(lines[1].endswith(" second"))

    def test_log_never_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = pathlib.Path(directory) / "file"
            blocker.write_text("x")
            display.log("message", blocker / "display.log")


if __name__ == "__main__":
    unittest.main()
