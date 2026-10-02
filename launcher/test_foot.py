import unittest
from unittest import mock

import couchliteos_display as display
import couchliteos_foot as foot

TV = '''HDMI-A-1 "Sony TV XYZ"
  Enabled: yes
  Modes:
    3840x2160 px, 60.000000 Hz (preferred)
    1920x1080 px, 60.000000 Hz (current)
    1280x720 px, 60.000000 Hz
'''


class FontSizeTest(unittest.TestCase):
    RESOLUTIONS = [(1280, 720), (1366, 768), (1920, 1080), (2560, 1440), (3840, 2160), (1024, 768), (1280, 1024)]

    def test_the_four_common_tv_resolutions(self):
        self.assertEqual(
            [foot.font_size(w, h) for w, h in [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)]],
            [16, 24, 32, 49],
        )

    def test_every_common_screen_gets_at_least_an_80x24_terminal(self):
        for width, height in self.RESOLUTIONS:
            columns, rows = foot.grid(width, height, foot.font_size(width, height))
            self.assertGreaterEqual(columns, 80, (width, height))
            self.assertGreaterEqual(rows, 24, (width, height))

    def test_text_is_not_left_tiny_on_a_big_screen(self):
        # About 28 rows on a 16:9 screen at every resolution, so text looks the same size from the sofa.
        for width, height in [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)]:
            _columns, rows = foot.grid(width, height, foot.font_size(width, height))
            self.assertTrue(26 <= rows <= 30, (width, height, rows))

    def test_a_bigger_screen_never_gets_a_smaller_font(self):
        sizes = [foot.font_size(int(h * 16 / 9), h) for h in range(480, 4400, 40)]
        self.assertEqual(sizes, sorted(sizes))

    def test_a_narrow_screen_is_limited_by_its_width(self):
        self.assertLess(foot.font_size(1024, 768), foot.font_size(1920, 1080))

    def test_absurd_sizes_are_clamped(self):
        self.assertEqual(foot.font_size(200, 100), foot.MIN_SIZE)
        self.assertEqual(foot.font_size(15360, 8640), foot.MAX_SIZE)


class ResolutionTest(unittest.TestCase):
    def outputs(self):
        return display.parse_wlr_randr(TV)

    def test_the_current_mode_is_used(self):
        self.assertEqual(foot.target_resolution(self.outputs(), {}), (1920, 1080))

    def test_no_active_output_gives_nothing(self):
        self.assertIsNone(foot.target_resolution([], {}))
        self.assertIsNone(foot.target_resolution(display.parse_wlr_randr(TV.replace("yes", "no")), {}))

    def test_the_saved_mode_wins_because_the_launcher_restores_it_after_foot_starts(self):
        output = self.outputs()[0]
        saved = {"output": "HDMI-A-1", "identity": output.identity, "resolution": "1280x720"}
        self.assertEqual(foot.target_resolution([output], saved), (1280, 720))

    def test_a_saved_mode_for_another_screen_is_ignored(self):
        output = self.outputs()[0]
        for saved in (
            {"output": "DP-1", "identity": output.identity, "resolution": "1280x720"},
            {"output": "HDMI-A-1", "identity": "b3RoZXI=", "resolution": "1280x720"},
            {"output": "HDMI-A-1", "identity": output.identity, "resolution": "800x600"},
        ):
            self.assertEqual(foot.target_resolution([output], saved), (1920, 1080), saved)


class ChooseSizeTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(foot.sys, "stderr")
        patcher.start()
        self.addCleanup(patcher.stop)

    def completed(self, text="", code=0):
        return mock.Mock(returncode=code, stdout=text, stderr="")

    def test_reads_the_resolution_from_wlr_randr(self):
        run = mock.Mock(return_value=self.completed(TV))
        self.assertEqual(foot.choose_size({}, run=run, saved={}, sleep=lambda _s: None), 24)

    def test_retries_while_the_compositor_has_no_output_yet(self):
        run = mock.Mock(side_effect=[self.completed("", 1), self.completed(TV)])
        self.assertEqual(foot.choose_size({}, run=run, saved={}, sleep=lambda _s: None), 24)
        self.assertEqual(run.call_count, 2)

    def test_gives_up_quietly_so_foot_still_starts_with_its_default_font(self):
        for run in (
            mock.Mock(return_value=self.completed("", 1)),
            mock.Mock(side_effect=FileNotFoundError("wlr-randr")),
            mock.Mock(side_effect=foot.subprocess.TimeoutExpired("wlr-randr", 2)),
            mock.Mock(return_value=self.completed("nonsense")),
        ):
            self.assertIsNone(foot.choose_size({}, run=run, saved={}, sleep=lambda _s: None))

    def test_an_environment_override_wins_when_it_is_sane(self):
        run = mock.Mock()
        self.assertEqual(foot.choose_size({"COUCHLITEOS_FONT_SIZE": "30"}, run=run, saved={}), 30)
        run.assert_not_called()
        for bad in ("", "abc", "0", "500", "-3", "12.5"):
            run = mock.Mock(return_value=self.completed(TV))
            self.assertEqual(
                foot.choose_size({"COUCHLITEOS_FONT_SIZE": bad}, run=run, saved={}, sleep=lambda _s: None), 24, bad
            )


class KeyboardPanelTest(unittest.TestCase):
    def test_the_panel_is_cages_bottom_forty_percent(self):
        # Cage: (int) (height * CAGE_OSK_PANEL_SHARE), at least CAGE_OSK_PANEL_MIN_HEIGHT.
        self.assertEqual([foot.panel_height(h) for h in (480, 600, 720, 1080, 2160)], [240, 240, 288, 432, 864])
        self.assertEqual(foot.panel_height(200), 200)

    def test_the_keyboard_font_gives_the_panel_about_thirteen_rows(self):
        self.assertEqual(
            [foot.panel_font_size(w, h) for w, h in [(1280, 720), (1920, 1080), (3840, 2160)]], [13, 20, 41]
        )
        for width, height in FontSizeTest.RESOLUTIONS:
            columns, rows = foot.grid(width, foot.panel_height(height), foot.panel_font_size(width, height))
            self.assertGreaterEqual(rows, foot.OSK_ROWS, (width, height))
            self.assertGreaterEqual(columns, 80, (width, height))

    def test_only_the_keyboard_app_id_gets_the_panel_font(self):
        with mock.patch.object(foot, "detect_resolution", return_value=(1920, 1080)), mock.patch.object(
            foot, "load_settings", return_value=None
        ), mock.patch.object(foot.display, "load_saved_display", return_value={}), mock.patch.object(
            foot.os, "execv"
        ) as execv:
            foot.main(["--app-id=couchliteos-osk", "--title", "COUCHLITEOS KEYBOARD", "--", "/bin/true"])
            foot.main(["--fullscreen", "--title", "CouchLiteOS Launcher"])
        self.assertEqual(execv.call_args_list[0][0][1][:3], ["/usr/bin/foot", "--font", "monospace:size=20"])
        self.assertEqual(execv.call_args_list[1][0][1][:3], ["/usr/bin/foot", "--font", "monospace:size=24"])

    def test_an_unknown_screen_or_a_forced_size_is_left_alone(self):
        self.assertEqual(foot.choose({}, run=mock.Mock(), saved={}, panel=True, sleep=lambda _s: None)[0], None)
        with mock.patch.object(foot, "detect_resolution", return_value=(1920, 1080)):
            self.assertEqual(foot.choose_size({}, saved={}, panel=True), 20)
            self.assertEqual(foot.choose_size({"COUCHLITEOS_FONT_SIZE": "12"}, saved={}, panel=True), 12)


class CommandTest(unittest.TestCase):
    def test_the_font_goes_before_the_callers_arguments(self):
        self.assertEqual(
            foot.command(24, ["--fullscreen", "--title", "T", "--", "/bin/true"]),
            ["/usr/bin/foot", "--font", "monospace:size=24", "--fullscreen", "--title", "T", "--", "/bin/true"],
        )

    def test_without_a_size_foot_keeps_its_configured_font(self):
        self.assertEqual(foot.command(None, ["--fullscreen"]), ["/usr/bin/foot", "--fullscreen"])

    def test_main_replaces_itself_with_foot(self):
        with mock.patch.object(foot, "choose_size", return_value=16), mock.patch.object(foot.os, "execv") as execv:
            foot.main(["--fullscreen"])
        execv.assert_called_once_with("/usr/bin/foot", ["/usr/bin/foot", "--font", "monospace:size=16", "--fullscreen"])


if __name__ == "__main__":
    unittest.main()
