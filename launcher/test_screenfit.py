"""Screen edges (TV overscan) and text size: settings file, pure math, the foot wrapper hooks, and the menu rows."""

import curses
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import couchliteos_foot as foot
import couchliteos_screenfit as screenfit
import test_ux2

TV = '''HDMI-A-1 "Sony TV XYZ"
  Enabled: yes
  Modes:
    {mode} px, 60.000000 Hz (preferred)
    {mode} px, 60.000000 Hz (current)
'''
RESOLUTIONS = [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)]


def tv(width, height):
    return TV.format(mode=f"{width}x{height}")


def completed(text):
    return mock.Mock(returncode=0, stdout=text, stderr="")


class SettingsFileTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name) / "screen.json"

    def test_defaults_when_file_missing_or_corrupt(self):
        defaults = screenfit.Settings()
        self.assertEqual((defaults.edges, defaults.text), (0, "auto"))
        self.assertEqual(screenfit.load(self.path), defaults)  # missing
        for content in (b"", b"{", b"[]", b"null", b"42", b'"text"', b"\xff\xfe\x00", b"[" * 100000, b"{" * 100000):
            self.path.write_bytes(content)
            self.assertEqual(screenfit.load(self.path), defaults, content[:10])
        self.path.unlink()
        self.path.mkdir()  # unreadable: a directory where the file should be
        self.assertEqual(screenfit.load(self.path), defaults)

    def test_unknown_values_ignored(self):
        cases = [
            ({"edges": 5, "text": "huge"}, (0, "auto")),
            ({"edges": 99, "text": "larger"}, (0, "larger")),
            ({"edges": 4, "text": "bogus"}, (4, "auto")),
            ({"edges": "4", "text": ["larger"]}, (0, "auto")),
            ({"edges": 4.0}, (0, "auto")),
            ({"edges": True}, (0, "auto")),
            ({"edges": -2, "text": None}, (0, "auto")),
            ({"edges": 6, "text": "smaller", "extra": 1}, (6, "smaller")),
        ]
        for data, expected in cases:
            self.path.write_text(json.dumps(data))
            loaded = screenfit.load(self.path)
            self.assertEqual((loaded.edges, loaded.text), expected, data)

    def test_save_then_load_round_trip_leaves_no_temporary_files(self):
        screenfit.save(screenfit.Settings(edges=4, text="smaller"), self.path)
        self.assertEqual(screenfit.load(self.path), screenfit.Settings(edges=4, text="smaller"))
        self.assertEqual(json.loads(self.path.read_text()), {"edges": 4, "text": "smaller"})
        self.assertEqual([item.name for item in self.path.parent.iterdir()], ["screen.json"])

    def test_the_default_path_sits_next_to_the_other_settings(self):
        self.assertEqual(screenfit.PATH, pathlib.Path("/var/lib/couchliteos/screen.json"))
        with mock.patch.object(screenfit, "PATH", self.path):
            screenfit.save(screenfit.Settings(edges=2))
            self.assertEqual(screenfit.load().edges, 2)


class MathTest(unittest.TestCase):
    def test_pad_pixels_percentages_1080p_and_4k(self):
        expected = {
            (1920, 1080): {0: (0, 0), 2: (38, 22), 4: (77, 43), 6: (115, 65)},
            (3840, 2160): {0: (0, 0), 2: (77, 43), 4: (154, 86), 6: (230, 130)},
        }
        for (width, height), by_percent in expected.items():
            for percent, pad in by_percent.items():
                self.assertEqual(screenfit.pad_pixels(width, height, percent), pad, (width, height, percent))

    def test_no_value_can_push_the_edges_past_six_percent(self):
        for bad in (7, 50, 100, -1, "4", None, 4.0, True):
            self.assertEqual(screenfit.pad_pixels(1920, 1080, bad), (0, 0), bad)

    def test_text_scale(self):
        self.assertEqual(
            [screenfit.text_scale(choice) for choice in ("smaller", "auto", "larger")], [0.8, 1.0, 1.25]
        )
        for bad in ("huge", "", None, 3):
            self.assertEqual(screenfit.text_scale(bad), 1.0)


class FootHooksTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(foot.sys, "stderr")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_for(self, width, height):
        return mock.Mock(return_value=completed(tv(width, height)))

    def choose(self, width, height, settings, environ=None):
        return foot.choose(
            environ or {}, run=self.run_for(width, height), saved={}, sleep=lambda _s: None, settings=settings
        )

    def test_zero_edges_adds_no_option(self):
        self.assertEqual(foot.command(24, ["--fullscreen"]), ["/usr/bin/foot", "--font", "monospace:size=24", "--fullscreen"])
        self.assertEqual(foot.command(24, ["--fullscreen"], None), foot.command(24, ["--fullscreen"]))
        for settings in (None, screenfit.Settings(), screenfit.Settings(edges=0, text="auto")):
            size, pad = self.choose(1920, 1080, settings)
            self.assertEqual((size, pad), (24, None), settings)
        with mock.patch.object(foot.screenfit, "load", return_value=screenfit.Settings()), mock.patch.object(
            foot, "choose", wraps=foot.choose
        ), mock.patch.object(foot.display, "load_saved_display", return_value={}), mock.patch.object(
            foot, "detect_resolution", return_value=(1920, 1080)
        ), mock.patch.object(foot.os, "execv") as execv:
            foot.main(["--fullscreen"])
        execv.assert_called_once_with("/usr/bin/foot", ["/usr/bin/foot", "--font", "monospace:size=24", "--fullscreen"])

    def test_edges_become_one_pad_option_before_the_callers_arguments(self):
        self.assertEqual(
            foot.command(24, ["--fullscreen", "--", "/bin/true"], (89, 65)),
            ["/usr/bin/foot", "--font", "monospace:size=24", "-o", "main.pad=89x65", "--fullscreen", "--", "/bin/true"],
        )
        # The pad is foot.ini's own 12 px plus the edge, on each side: 1080p at 4% is 12+77 by 12+43.
        _size, pad = self.choose(1920, 1080, screenfit.Settings(edges=4))
        self.assertEqual(pad, (89, 55))
        _size, pad = self.choose(3840, 2160, screenfit.Settings(edges=6))
        self.assertEqual(pad, (242, 142))

    def test_auto_text_still_fits_the_grid_inside_the_edges(self):
        for width, height in RESOLUTIONS:
            for edges in screenfit.EDGE_CHOICES:
                size, pad = self.choose(width, height, screenfit.Settings(edges=edges))
                columns, rows = foot.grid(width, height, size, (pad[0] - foot.BASE_PAD, pad[1] - foot.BASE_PAD) if pad else (0, 0))
                self.assertGreaterEqual(columns, 80, (width, height, edges))
                self.assertGreaterEqual(rows, 24, (width, height, edges))

    def test_edges_shrink_the_font_so_the_same_text_fits_the_smaller_area(self):
        plain, _pad = self.choose(1920, 1080, screenfit.Settings())
        inside, _pad = self.choose(1920, 1080, screenfit.Settings(edges=6))
        self.assertLess(inside, plain)

    def test_text_choice_scales_the_automatic_size(self):
        small, _ = self.choose(1920, 1080, screenfit.Settings(text="smaller"))
        auto, _ = self.choose(1920, 1080, screenfit.Settings(text="auto"))
        large, _ = self.choose(1920, 1080, screenfit.Settings(text="larger"))
        # 30 would leave fewer than 24 rows at 1080p; LARGER stops at the biggest 80x24 font.
        self.assertEqual((small, auto, large), (19, 24, 28))

    def test_text_scale_clamped_to_foot_limits(self):
        tiny, _ = self.choose(200, 100, screenfit.Settings(text="smaller"))
        self.assertEqual(tiny, foot.MIN_SIZE)
        huge, _ = self.choose(15360, 8640, screenfit.Settings(text="larger"))
        self.assertEqual(huge, foot.MAX_SIZE)

    def test_larger_text_keeps_a_usable_terminal_on_the_common_tvs(self):
        for width, height in RESOLUTIONS:
            size, _pad = self.choose(width, height, screenfit.Settings(text="larger"))
            columns, rows = foot.grid(width, height, size)
            self.assertGreaterEqual(columns, 80, (width, height))
            self.assertGreaterEqual(rows, 24, (width, height))
            auto, _pad = self.choose(width, height, screenfit.Settings(text="auto"))
            self.assertGreaterEqual(size, auto, (width, height))

    def test_env_override_still_wins(self):
        size, pad = self.choose(1920, 1080, screenfit.Settings(text="larger"), {"COUCHLITEOS_FONT_SIZE": "30"})
        self.assertEqual((size, pad), (30, None))
        run = mock.Mock()  # nothing to measure when the edges are off
        self.assertEqual(
            foot.choose({"COUCHLITEOS_FONT_SIZE": "30"}, run=run, saved={}, settings=screenfit.Settings(text="smaller")),
            (30, None),
        )
        run.assert_not_called()
        # The size is forced; the edges still apply.
        size, pad = self.choose(1920, 1080, screenfit.Settings(edges=2), {"COUCHLITEOS_FONT_SIZE": "30"})
        self.assertEqual((size, pad), (30, (50, 34)))

    def test_choose_size_keeps_todays_result_and_ignores_settings(self):
        run = self.run_for(1920, 1080)
        self.assertEqual(foot.choose_size({}, run=run, saved={}, sleep=lambda _s: None), 24)

    def test_unknown_screen_leaves_fonts_and_padding_alone(self):
        run = mock.Mock(return_value=mock.Mock(returncode=1, stdout="", stderr=""))
        result = foot.choose({}, run=run, saved={}, sleep=lambda _s: None, settings=screenfit.Settings(edges=6, text="larger"))
        self.assertEqual(result, (None, None))

    def execute(self, **patches):
        """Run foot.main() for a 1080p screen with the given screenfit patches; return execv's argv."""
        with mock.patch.object(foot.display, "load_saved_display", return_value={}), mock.patch.object(
            foot, "detect_resolution", return_value=(1920, 1080)
        ), mock.patch.object(foot.os, "execv") as execv, mock.patch.dict(foot.os.environ, {}, clear=False):
            foot.os.environ.pop("COUCHLITEOS_FONT_SIZE", None)
            managers = [mock.patch.object(foot.screenfit, name, value) for name, value in patches.items()]
            for manager in managers:
                manager.start()
            try:
                foot.main(["--fullscreen"])
            finally:
                for manager in managers:
                    manager.stop()
        self.assertEqual(execv.call_count, 1)
        return execv.call_args[0][1]

    TODAY = ["/usr/bin/foot", "--font", "monospace:size=24", "--fullscreen"]

    def test_foot_command_never_raises_on_bad_settings(self):
        boom = mock.Mock(side_effect=RuntimeError("bad settings"))
        for patches in (
            {"load": boom},
            {"load": mock.Mock(side_effect=OSError("unreadable"))},
            {"load": mock.Mock(side_effect=RecursionError())},
            {"load": mock.Mock(return_value=object())},  # not a Settings at all
            {"load": mock.Mock(return_value=None)},
            {"load": mock.Mock(return_value=mock.Mock(edges="x", text=3))},
            {"load": mock.Mock(return_value=screenfit.Settings(edges=4)), "pad_pixels": boom},
            {"load": mock.Mock(return_value=screenfit.Settings(text="larger")), "text_scale": boom},
            {"load": mock.Mock(return_value=screenfit.Settings(edges=4)), "pad_pixels": mock.Mock(return_value="junk")},
            {"load": mock.Mock(return_value=screenfit.Settings(text="larger")), "text_scale": mock.Mock(return_value="junk")},
        ):
            self.assertEqual(self.execute(**patches), self.TODAY, patches)

    def test_foot_still_starts_when_the_module_is_missing_or_the_file_is_corrupt(self):
        with mock.patch.object(foot, "screenfit", None):
            with mock.patch.object(foot.display, "load_saved_display", return_value={}), mock.patch.object(
                foot, "detect_resolution", return_value=(1920, 1080)
            ), mock.patch.object(foot.os, "execv") as execv:
                foot.main(["--fullscreen"])
        self.assertEqual(execv.call_args[0][1], self.TODAY)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "screen.json"
            path.write_text("{ this is not json")
            self.assertEqual(self.execute(PATH=path), self.TODAY)

    def test_foot_imports_and_starts_when_the_screenfit_module_is_missing_or_broken(self):
        for source in (None, "raise RuntimeError('broken')", "this is not python (", "import no_such_module_xyz"):
            with tempfile.TemporaryDirectory() as directory:
                if source is not None:
                    pathlib.Path(directory, "couchliteos_screenfit.py").write_text(source)
                spec = importlib.util.spec_from_file_location("foot_without_screenfit", pathlib.Path(foot.__file__))
                module = importlib.util.module_from_spec(spec)
                with mock.patch.dict(sys.modules), mock.patch.object(sys, "path", [directory, *sys.path]):
                    sys.modules.pop("couchliteos_screenfit", None)
                    if source is None:
                        sys.modules["couchliteos_screenfit"] = None  # makes `import` raise ImportError
                    spec.loader.exec_module(module)
            self.assertIsNone(module.screenfit, source)
            with mock.patch.object(module.display, "load_saved_display", return_value={}), mock.patch.object(
                module, "detect_resolution", return_value=(1920, 1080)
            ), mock.patch.object(module.os, "execv") as execv:
                module.main(["--fullscreen"])
            self.assertEqual(execv.call_args[0][1], self.TODAY, source)

    def test_a_failure_while_choosing_falls_back_to_the_plain_font_command(self):
        real = foot.choose
        calls = []

        def choose(environ, **kwargs):
            calls.append(kwargs.get("settings"))
            if kwargs.get("settings") is not None:
                raise RuntimeError("new code failed")
            return real(environ, **kwargs)

        with mock.patch.object(foot, "choose", choose), mock.patch.object(
            foot.screenfit, "load", return_value=screenfit.Settings(edges=6, text="larger")
        ), mock.patch.object(foot.display, "load_saved_display", return_value={}), mock.patch.object(
            foot, "detect_resolution", return_value=(1920, 1080)
        ), mock.patch.object(foot.os, "execv") as execv:
            foot.main(["--fullscreen"])
        self.assertEqual(execv.call_args[0][1], self.TODAY)
        self.assertEqual(len(calls), 2)

    def test_saved_settings_reach_foot(self):
        argv = self.execute(load=mock.Mock(return_value=screenfit.Settings(edges=4, text="larger")))
        # 1080p, 4% edges: 77x43 extra, font from the smaller area times 1.25.
        self.assertEqual(argv[:2], ["/usr/bin/foot", "--font"])
        self.assertIn("-o", argv)
        self.assertEqual(argv[argv.index("-o") + 1], "main.pad=89x55")
        self.assertEqual(argv[-1], "--fullscreen")


class LauncherRowsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_screenfit", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.run_dir = pathlib.Path(self.directory.name) / "run"
        self.run_dir.mkdir()
        self.path = pathlib.Path(self.directory.name) / "screen.json"
        for patcher in (
            mock.patch.object(screenfit, "PATH", self.path),
            mock.patch.object(self.module, "RUN", self.run_dir),
            mock.patch.object(self.module.time, "sleep"),
            mock.patch.object(self.module, "network_summary", return_value="OFFLINE"),
            mock.patch.object(self.module.display, "query_outputs", return_value=[]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def settings(self, keys=()):
        screen = test_ux2.Screen(keys)
        return self.module.Settings(screen, self.module.Launcher(screen)), screen

    @staticmethod
    def shown(screen):
        return "\n".join(screen.text())

    def test_display_menu_has_the_two_new_rows_before_back(self):
        settings, screen = self.settings([27])
        settings.run_display()
        text = self.shown(screen)
        self.assertIn("SCREEN EDGES  0%", text)
        self.assertIn("TEXT SIZE  AUTO", text)
        self.assertLess(text.index("APPLY DISPLAY MODE"), text.index("SCREEN EDGES  0%"))
        self.assertLess(text.index("TEXT SIZE  AUTO"), text.index("BACK"))

    def test_rows_show_the_saved_values(self):
        screenfit.save(screenfit.Settings(edges=4, text="larger"))
        settings, screen = self.settings([27])
        settings.run_display()
        self.assertIn("SCREEN EDGES  4%", self.shown(screen))
        self.assertIn("TEXT SIZE  LARGER", self.shown(screen))

    def test_the_edge_row_shows_its_help_line(self):
        down = curses.KEY_DOWN
        settings, screen = self.settings([down, down, down])
        with self.assertRaises(RuntimeError):  # the fake screen runs out of keys
            settings.run_display()
        self.assertIn(self.module.SCREEN_EDGES_HELP, self.shown(screen))
        self.assertEqual(self.module.SCREEN_EDGES_HELP, "PICK THE SMALLEST EDGE WHERE YOU CAN SEE THE WHOLE BORDER.")

    def pick(self, row, choices_down):
        """Open the row at `row`, move `choices_down` entries down in its list, and press A."""
        down, enter = curses.KEY_DOWN, 10
        return [down] * row + [enter] + [down] * choices_down + [enter]

    def test_apply_restarts_only_when_no_app_running(self):
        # No app or stream running: the launcher saves, says so, and exits so systemd restarts it.
        settings, screen = self.settings(self.pick(3, 2))
        with self.assertRaises(SystemExit) as stop:
            settings.run_display()
        self.assertEqual(stop.exception.code, 0)
        self.assertEqual(screenfit.load(), screenfit.Settings(edges=4))
        self.assertIn("RESTARTING THE LAUNCHER TO APPLY. THIS TAKES A FEW SECONDS.", self.shown(screen))

    def test_the_restarted_launcher_reopens_display_instead_of_auto_streaming(self):
        settings, _screen = self.settings(self.pick(3, 2))
        with self.assertRaises(SystemExit):
            settings.run_display()
        self.assertTrue((self.run_dir / "reopen-display").exists())

    def test_a_fourth_restart_within_a_minute_waits_for_the_next_start(self):
        # systemd stops restarting the launcher after 5 starts a minute and the TV would go dark.
        now = self.module.time.time()
        (self.run_dir / "screen-restarts").write_text(f"{now - 100}\n{now - 30}\n{now - 20}\n")
        settings, _screen = self.settings(self.pick(3, 2))
        with self.assertRaises(SystemExit):  # the one 100 s ago no longer counts
            settings.run_display()
        settings, screen = self.settings(self.pick(3, 1) + [27])
        settings.run_display()  # must NOT exit
        self.assertEqual(screenfit.load().edges, 6, "still saved")
        self.assertIn("APPLIES THE NEXT TIME", self.shown(screen))

    def test_an_app_running_means_save_and_apply_next_start(self):
        for ready in ("moonlight-ready", "chiaki-ng-ready", "rdp-ready", "abc123-ready"):
            (self.run_dir / ready).touch()
            self.addCleanup((self.run_dir / ready).unlink, missing_ok=True)
            settings, screen = self.settings(self.pick(4, 2) + [27])  # TEXT SIZE: AUTO -> LARGER, then leave
            settings.run_display()  # must NOT exit
            self.assertEqual(screenfit.load(), screenfit.Settings(text="larger"), ready)
            self.assertIn("SAVED. IT APPLIES THE NEXT TIME COUCHLITEOS STARTS.", self.shown(screen), ready)
            (self.run_dir / ready).unlink()
            self.path.unlink()

    def test_the_launchers_own_ready_file_is_not_an_app(self):
        (self.run_dir / "launcher-ready").touch()
        settings, _screen = self.settings(self.pick(3, 1))
        with self.assertRaises(SystemExit):
            settings.run_display()

    def test_if_running_apps_cannot_be_checked_nothing_restarts(self):
        settings, screen = self.settings(self.pick(3, 1) + [27])
        with mock.patch.object(self.module, "RUN", self.run_dir / "missing" / "deeper"), mock.patch.object(
            pathlib.Path, "glob", side_effect=OSError("cannot list")
        ):
            settings.run_display()
        self.assertEqual(screenfit.load().edges, 2)
        self.assertIn("APPLIES THE NEXT TIME", self.shown(screen))

    def test_picking_the_current_value_changes_nothing(self):
        settings, screen = self.settings(self.pick(3, 0) + [27])  # 0% is already the value
        settings.run_display()
        self.assertFalse(self.path.exists())

    def test_cancelling_the_list_changes_nothing(self):
        settings, _screen = self.settings([curses.KEY_DOWN] * 3 + [10, curses.KEY_DOWN, 27, 27])
        settings.run_display()
        self.assertFalse(self.path.exists())

    def test_a_failed_save_says_so_and_does_not_restart(self):
        settings, screen = self.settings(self.pick(3, 1) + [27])
        with mock.patch.object(screenfit, "save", side_effect=OSError("disk full")):
            settings.run_display()
        self.assertIn("NOT SAVED: DISK FULL", self.shown(screen))

    def test_every_new_line_fits_76_columns_and_names_the_buttons(self):
        lines = [
            self.module.SCREEN_EDGES_HELP,
            self.module.TEXT_SIZE_HELP,
            self.module.SCREEN_CHOICE_HINT,
            self.module.SCREEN_RESTART_MESSAGE,
            self.module.SCREEN_NEXT_START_MESSAGE,
        ]
        for line in lines:
            self.assertLessEqual(len(line), 76, line)
            self.assertEqual(line, line.upper())
        self.assertIn("A / CROSS", self.module.SCREEN_CHOICE_HINT)
        self.assertIn("B / CIRCLE", self.module.SCREEN_CHOICE_HINT)


if __name__ == "__main__":
    unittest.main()
