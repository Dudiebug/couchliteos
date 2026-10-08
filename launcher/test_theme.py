import testenv  # noqa: F401  (first: scratch run and state directories)
"""Themes: parsing and rejects, the contrast check on every built-in, foot/OSC/CSS output,
the accent override, the saved choice, and Settings > APPEARANCE recolouring the running windows."""

import curses
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_background as background
import couchliteos_foot as foot
import couchliteos_month as month
import couchliteos_osk as osk
import couchliteos_theme as theme
import couchliteos_wave as wave
from test_controls import FakeScreen, load_launcher

BUILTIN = pathlib.Path(__file__).resolve().parents[1] / "overlay/usr/share/couchliteos/themes"
GOOD = """[theme]
name = Test  Theme
background = #101010
surface = 202020
text = #F0F0F0
muted = #a0a0a0
accent = #3366ff
focus = #2244aa
warning = #ffcc00
error = #ff4444
wallpaper = /usr/share/backgrounds/x.png
"""


class TempDir(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.user = self.root / "user"
        self.user.mkdir()
        self.config = self.root / "config.ini"

    def write(self, name, text, directory=None):
        path = (directory or self.user) / name
        path.write_text(text)
        return path


class ParseTest(TempDir):
    def test_good_file(self):
        loaded = theme.load(self.write("test.theme", GOOD))
        self.assertEqual(loaded.name, "test")
        self.assertEqual(loaded.label, "TEST THEME")
        self.assertEqual(loaded.colours["text"], "f0f0f0")
        self.assertEqual(loaded.colours["surface"], "202020")
        self.assertEqual(loaded.wallpaper, "/usr/share/backgrounds/x.png")
        self.assertEqual(set(loaded.colours), set(theme.FIELDS))

    def test_label_defaults_to_the_file_name(self):
        text = GOOD.replace("name = Test  Theme\n", "").replace("wallpaper = /usr/share/backgrounds/x.png\n", "")
        loaded = theme.load(self.write("my-room.theme", text))
        self.assertEqual((loaded.label, loaded.wallpaper), ("MY ROOM", ""))

    def test_rejects(self):
        cases = {
            "missing colour": GOOD.replace("error = #ff4444\n", ""),
            "empty colour": GOOD.replace("#ff4444", ""),
            "short colour": GOOD.replace("#ff4444", "#f44"),
            "named colour": GOOD.replace("#ff4444", "red"),
            "bad hex": GOOD.replace("#ff4444", "#ff44gg"),
            "trailing junk": GOOD.replace("#ff4444", "#ff4444 x"),
            "no section": GOOD.replace("[theme]\n", "[colours]\n"),
            "not ini": "background = #000000\n",
            "duplicate key": GOOD + "text = #000000\n",
        }
        for case, text in cases.items():
            with self.subTest(case), self.assertRaises(theme.ThemeError):
                theme.load(self.write("bad.theme", text))

    def test_rejects_unreadable_huge_and_badly_named_files(self):
        with self.assertRaises(theme.ThemeError):
            theme.load(self.user / "missing.theme")
        with self.assertRaises(theme.ThemeError):
            theme.load(self.write("huge.theme", GOOD + "#" * theme.MAX_FILE))
        with self.assertRaises(theme.ThemeError):
            theme.load(self.write("Bad Name.theme", GOOD))
        (self.user / "dir.theme").mkdir()
        with self.assertRaises(theme.ThemeError):
            theme.load(self.user / "dir.theme")


class BuiltinTest(unittest.TestCase):
    def setUp(self):
        self.themes, self.problems = theme.available(BUILTIN, pathlib.Path("/nonexistent"))

    LABELS = {
        "midnight": "MIDNIGHT", "slate": "SLATE", "daylight": "DAYLIGHT", "high-contrast": "HIGH CONTRAST",
        "terminal": "TERMINAL", "ocean": "OCEAN", "crimson": "CRIMSON", "emerald": "EMERALD",
        "amethyst": "AMETHYST", "sunset": "SUNSET", "rose": "ROSE", "gold": "GOLD", "graphite": "GRAPHITE",
        "arctic": "ARCTIC",
    }
    LIGHT = {"daylight", "arctic"}

    def test_the_built_ins_load(self):
        self.assertEqual(self.problems, [])
        self.assertEqual({name: item.label for name, item in self.themes.items()}, self.LABELS)
        self.assertIn(theme.DEFAULT, self.themes)

    def test_two_light_themes(self):
        light = {name for name, item in self.themes.items()
                 if theme._luminance(item.colours["background"]) > 0.4}
        self.assertEqual(light, self.LIGHT)

    def test_no_two_built_ins_look_alike(self):
        # Each built-in has its own accent, and only HIGH CONTRAST and TERMINAL share a background
        # (black): none is another with a new name.
        accents = [item.colours["accent"] for item in self.themes.values()]
        self.assertEqual(len(set(accents)), len(accents))
        backgrounds = [item.colours["background"] for item in self.themes.values()]
        self.assertEqual(len(set(backgrounds)), len(backgrounds) - 1)

    def test_every_built_in_is_readable(self):
        for name, item in self.themes.items():
            colours = item.colours
            with self.subTest(name):
                self.assertGreaterEqual(theme.contrast(colours["text"], colours["background"]), 4.5)
                self.assertGreaterEqual(theme.contrast(theme.on_focus(item), colours["focus"]), 4.5)
                self.assertGreaterEqual(theme.contrast(colours["muted"], colours["background"]), 4.5)
                # Tiles, panels and the quick menu are drawn on the surface colour.
                self.assertGreaterEqual(theme.contrast(colours["text"], colours["surface"]), 4.5)
                self.assertGreaterEqual(theme.contrast(colours["muted"], colours["surface"]), 4.5)
                self.assertGreaterEqual(theme.contrast(colours["warning"], colours["background"]), 4.5)
                self.assertGreaterEqual(theme.contrast(colours["error"], colours["background"]), 4.5)
                # The focused tile's edge: WCAG's 3 to 1 for parts of the picture that are not text.
                self.assertGreaterEqual(theme.contrast(colours["accent"], colours["surface"]), 3.0)

    def test_the_wave_stays_readable_with_each_themes_own_accent(self):
        # test_wave.py checks every ACCENTS preset, test_month.py every month's colour.
        for name, item in self.themes.items():
            text = item.colours["text"]
            for hour in range(24):
                colours = wave.palette(item, hour)
                for part in ("top", "bottom"):
                    self.assertGreaterEqual(theme.contrast(text, colours.hex(part)), theme.MIN_CONTRAST,
                                            (name, hour, part))

    def test_text_is_drawn_in_the_text_colour_on_focus_except_high_contrast_and_terminal(self):
        for name, item in self.themes.items():
            expected = item.colours["background" if name in ("high-contrast", "terminal") else "text"]
            self.assertEqual(theme.on_focus(item), expected, name)

    def test_terminal_is_the_look_before_0_3_0(self):
        # 0.2.x: foot's black background and white text (overlay/etc/xdg/foot/foot.ini), and the
        # selection in reverse video: black text on a white bar.
        colours = self.themes["terminal"].colours
        self.assertEqual((colours["background"], colours["text"], colours["focus"]), ("000000", "ffffff", "ffffff"))
        self.assertEqual(theme.on_focus(self.themes["terminal"]), "000000")

    def test_fallback_matches_the_midnight_file(self):
        self.assertEqual(theme.FALLBACK.colours, self.themes["midnight"].colours)


class ContrastTest(unittest.TestCase):
    def test_wcag_values(self):
        self.assertAlmostEqual(theme.contrast("#000000", "#ffffff"), 21.0)
        self.assertAlmostEqual(theme.contrast("ffffff", "ffffff"), 1.0)
        self.assertAlmostEqual(theme.contrast("#777777", "#ffffff"), 4.48, places=2)
        self.assertEqual(theme.contrast("#123456", "#abcdef"), theme.contrast("#abcdef", "#123456"))


class OutputTest(unittest.TestCase):
    def setUp(self):
        self.midnight = theme.FALLBACK

    def test_foot_options(self):
        options = theme.foot_options(self.midnight)
        self.assertEqual(options[:4], ["-o", "colors.background=0b1020", "-o", "colors.foreground=f1f5f9"])
        self.assertEqual(options[0::2], ["-o"] * (len(options) // 2))
        self.assertIn("colors.regular4=3b82f6", options)
        self.assertIn("colors.bright0=94a3b8", options)

    def test_osc(self):
        text = theme.osc(self.midnight)
        self.assertTrue(text.startswith("\x1b]4;0;rgb:0b/10/20;1;rgb:f8/71/71;"))
        self.assertIn("\x1b]10;rgb:f1/f5/f9\x1b\\", text)
        self.assertTrue(text.endswith("\x1b]11;rgb:0b/10/20\x1b\\"))
        self.assertRegex(text, theme.OSC)

    def test_css(self):
        text = theme.css(self.midnight)
        self.assertIn("  --couchliteos-background: #0b1020;\n", text)
        self.assertIn("  --couchliteos-on-focus: #f1f5f9;\n", text)
        self.assertEqual(text.count("--couchliteos-"), len(theme.FIELDS) + 1)

    def test_accent_override(self):
        self.assertEqual(len(theme.ACCENTS), 8)
        amber = theme.with_accent(self.midnight, "amber")
        self.assertEqual(amber.colours["accent"], "f59e0b")
        self.assertEqual({**amber.colours, "accent": "3b82f6"}, self.midnight.colours)
        self.assertIn("colors.regular4=f59e0b", theme.foot_options(amber))
        self.assertIn("4;rgb:f5/9e/0b", theme.osc(amber))
        self.assertIs(theme.with_accent(self.midnight, ""), self.midnight)
        self.assertIs(theme.with_accent(self.midnight, "chartreuse"), self.midnight)
        self.assertEqual(self.midnight.colours["accent"], "3b82f6", "the theme itself is unchanged")


class AvailableTest(TempDir):
    def test_bad_user_theme_skipped_and_reported(self):
        self.write("mine.theme", GOOD)
        self.write("broken.theme", GOOD.replace("#ff4444", "nope"))
        self.write("midnight.theme", GOOD)
        self.write("notes.txt", "not a theme")
        themes, problems = theme.available(BUILTIN, self.user)
        self.assertIn("mine", themes)
        self.assertNotIn("broken", themes)
        self.assertEqual(themes["midnight"].label, "MIDNIGHT", "a user file cannot replace a built-in")
        self.assertEqual(len(problems), 2)
        self.assertTrue(problems[0].startswith("broken.theme: BAD COLOUR"))
        self.assertEqual(problems[1], "midnight.theme: NAME ALREADY USED")

    def test_missing_directories(self):
        self.assertEqual(theme.available(self.root / "a", self.root / "b"), ({}, []))

    def test_user_dir_follows_xdg_config_home(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/var/lib/couchliteos/home/.config"}):
            self.assertEqual(theme.user_dir(), pathlib.Path("/var/lib/couchliteos/home/.config/couchliteos/themes"))


class InterfaceTest(TempDir):
    def test_tv_unless_classic_is_saved(self):
        self.assertEqual(theme.load_interface(self.config), "tv")
        self.config.write_text("[appearance]\ninterface = something\n")
        self.assertEqual(theme.load_interface(self.config), "tv")
        self.config.write_text("[appearance]\ninterface = Classic\n")
        self.assertEqual(theme.load_interface(self.config), "classic")

    def test_saving_keeps_the_theme(self):
        theme.save_choice("terminal", "", self.config)
        theme.save_interface("classic", self.config)
        self.assertEqual((theme.load_interface(self.config), theme.load_choice(self.config)),
                         ("classic", ("terminal", "")))
        theme.save_interface("tv", self.config)
        self.assertEqual(theme.load_interface(self.config), "tv")

    def test_any_appearance_value(self):
        self.assertEqual(theme.load_value("home", "cross", self.config), "cross")
        self.config.write_text("[appearance]\nhome = Rows \n")
        self.assertEqual(theme.load_value("home", "cross", self.config), "rows")
        self.assertEqual(theme.load_value("motion", path=self.config), "")
        self.config.write_text("not ini")
        self.assertEqual(theme.load_value("home", "cross", self.config), "cross")

    def test_the_session_script_reads_the_same_key(self):
        script = (pathlib.Path(__file__).resolve().parents[1] / "scripts/couchliteos-session").read_text()
        self.assertIn('section == "appearance"', script)
        self.assertIn("interface = classic", script)


class InterfaceScreenTest(unittest.TestCase):
    """Settings > APPEARANCE > INTERFACE in the classic launcher (the TV interface opens the same screen)."""

    def run_screen(self, keys, chosen, running=False):
        import shutil
        import tempfile
        from test_controls import FakeScreen, load_launcher

        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        launcher = load_launcher()
        menu = launcher.Settings(FakeScreen(keys), mock.Mock(any_app_running=mock.Mock(return_value=running)))
        frames = []
        original_draw = menu.draw

        def draw(title, rows, selected, *args, **kwargs):
            frames.append((title, list(rows), menu.status))
            return original_draw(title, rows, selected, *args, **kwargs)

        exited = False
        with mock.patch.object(launcher, "read_key", side_effect=lambda window, **_kw: window.getch()), \
                mock.patch.object(launcher, "RUN", root), \
                mock.patch.object(theme, "CONFIG", root / "config.ini"), \
                mock.patch.object(menu, "draw", side_effect=draw), \
                mock.patch.object(menu, "choose", return_value=chosen):
            try:
                menu.run_appearance()
            except SystemExit as stop:
                exited = stop.code in (0, None)
        return root, frames, exited

    DOWN_TO_INTERFACE = [curses.KEY_DOWN] * 5

    def test_the_interface_row_is_above_back(self):
        _root, frames, _exited = self.run_screen([27], None)
        self.assertEqual(frames[0][1][-2:], ["INTERFACE  TV", "BACK"])

    def test_choosing_classic_saves_it_and_restarts_into_it(self):
        root, _frames, exited = self.run_screen([*self.DOWN_TO_INTERFACE, 10], "classic")
        self.assertTrue(exited)
        self.assertEqual(theme.load_interface(root / "config.ini"), "classic")
        self.assertTrue((root / "switch-interface").exists())

    def test_not_while_an_app_runs(self):
        root, frames, exited = self.run_screen([*self.DOWN_TO_INTERFACE, 10, 27], "classic", running=True)
        self.assertFalse(exited)
        self.assertEqual(theme.load_interface(root / "config.ini"), "tv")
        self.assertIn("CLOSE", frames[-1][2])

    def test_the_same_choice_changes_nothing(self):
        root, _frames, exited = self.run_screen([*self.DOWN_TO_INTERFACE, 10, 27], "tv")
        self.assertFalse(exited)
        self.assertFalse((root / "switch-interface").exists())


class ChoiceTest(TempDir):
    def test_defaults(self):
        self.assertEqual(theme.load_choice(self.config), ("midnight", ""))
        self.config.write_text("[appearance]\ntheme = ../etc\naccent = mauve\n")
        self.assertEqual(theme.load_choice(self.config), ("midnight", ""))

    def test_save_keeps_other_sections(self):
        self.config.write_text("[input]\nhome_shortcut = guide\n\n[appearance]\ntheme = slate\n\n[cec]\non = no\n")
        theme.save_choice("daylight", "green", self.config)
        self.assertEqual(theme.load_choice(self.config), ("daylight", "green"))
        text = self.config.read_text()
        self.assertIn("[input]\nhome_shortcut = guide\n", text)
        self.assertIn("[cec]\non = no\n", text)
        self.assertEqual(text.count("[appearance]"), 1)

    def test_current(self):
        theme.save_choice("high-contrast", "teal", self.config)
        chosen = theme.current(self.config, BUILTIN, self.user)
        self.assertEqual((chosen.name, chosen.colours["accent"]), ("high-contrast", "14b8a6"))
        theme.save_choice("gone", "", self.config)
        self.assertEqual(theme.current(self.config, BUILTIN, self.user).name, "midnight")
        self.assertIsNone(theme.chosen(self.config, self.root / "none", self.user))
        self.assertEqual(theme.current(self.config, self.root / "none", self.user), theme.FALLBACK)
        with mock.patch.object(theme, "available", side_effect=RuntimeError):
            self.assertEqual(theme.current(self.config, BUILTIN, self.user), theme.FALLBACK)


class RunningWindowsTest(TempDir):
    def test_apply_writes_the_terminal_and_the_keyboard_file(self):
        osk_file = self.root / "theme.osc"
        with mock.patch.object(theme.os, "write") as write:
            theme.apply(theme.FALLBACK, 7, osk_file)
        write.assert_called_once_with(7, theme.osc(theme.FALLBACK).encode("ascii"))
        self.assertEqual(theme.read_osc(osk_file), theme.osc(theme.FALLBACK))

    def test_read_osc_rejects_anything_else(self):
        path = self.root / "theme.osc"
        path.write_text("\x1b]4;0;rgb:00/00/00\x1b\\\x1b]2;title\x07")
        self.assertIsNone(theme.read_osc(path))
        path.write_text("plain text")
        self.assertIsNone(theme.read_osc(path))
        path.write_text(theme.osc(theme.FALLBACK))
        self.assertIsNone(theme.read_osc(path, owner_uid=os.getuid() + 1))
        self.assertIsNone(theme.read_osc(self.root / "missing"))

    def test_keyboard_copies_a_new_theme_once(self):
        path = self.root / "theme.osc"
        theme.apply(theme.FALLBACK, None, path)
        with mock.patch.object(theme, "OSK_FILE", path), mock.patch.object(osk.os, "write") as write:
            seen = osk.sync_theme(None, 5)
            self.assertEqual(osk.sync_theme(seen, 5), seen)
            write.assert_called_once_with(5, theme.osc(theme.FALLBACK).encode("ascii"))
            theme.apply(theme.with_accent(theme.FALLBACK, "red"), None, path)
            osk.sync_theme(seen, 5)
            self.assertEqual(write.call_count, 2)

    def test_foot_wrapper_passes_the_colours_and_survives_a_broken_theme(self):
        with mock.patch.object(foot.theme, "chosen", return_value=theme.FALLBACK):
            options = foot.theme_options()
        self.assertEqual(options, theme.foot_options(theme.FALLBACK))
        self.assertEqual(foot.command(None, ["--", "x"], None, options)[1:3], ["-o", "colors.background=0b1020"])
        self.assertEqual(foot.command(None, ["--", "x"])[-2:], ["--", "x"])
        with mock.patch.object(foot.theme, "chosen", side_effect=RuntimeError):
            self.assertEqual(foot.theme_options(), [])
        with mock.patch.object(foot.theme, "chosen", return_value=None):
            self.assertEqual(foot.theme_options(), [], "no theme file: foot.ini's colours")


class AppearanceScreenTest(TempDir):
    """Settings > APPEARANCE: picking a theme saves it and writes the OSC string at once (no restart)."""

    def run_screen(self, keys):
        launcher = load_launcher()
        menu = launcher.Settings(FakeScreen(keys), mock.Mock())
        menu.selected = launcher.SETTINGS_MENU.index("APPEARANCE")
        osk_file = self.root / "theme.osc"
        with mock.patch.object(launcher, "read_key", side_effect=lambda window, **_kw: window.getch()), \
                mock.patch.object(theme, "CONFIG", self.config), \
                mock.patch.object(theme, "BUILTIN_DIR", BUILTIN), \
                mock.patch.object(theme, "OSK_FILE", osk_file), \
                mock.patch.object(theme, "user_dir", return_value=self.user), \
                mock.patch.object(theme.os, "write") as write:
            self.assertTrue(menu.activate())
        return write, osk_file

    def test_menu_has_the_row(self):
        menu = load_launcher().SETTINGS_MENU
        self.assertEqual(menu.index("APPEARANCE"), menu.index("DISPLAY") + 1)

    def test_switching_theme_writes_the_osc_string(self):
        # THEME, A; the list is in file name order, on midnight, with ocean next: down once, A; B.
        write, osk_file = self.run_screen([10, curses.KEY_DOWN, 10, 27])
        self.assertEqual(theme.load_choice(self.config), ("ocean", ""))
        ocean = theme.load(BUILTIN / "ocean.theme")
        write.assert_called_once_with(1, theme.osc(ocean).encode("ascii"))
        self.assertEqual(theme.read_osc(osk_file), theme.osc(ocean))

    def test_switching_accent_writes_the_osc_string(self):
        # ACCENT row, A, THEME DEFAULT -> BLUE -> AMBER, A; B.
        write, _osk_file = self.run_screen([curses.KEY_DOWN, 10, curses.KEY_DOWN, curses.KEY_DOWN, 10, 27])
        self.assertEqual(theme.load_choice(self.config), ("midnight", "amber"))
        write.assert_called_once_with(1, theme.osc(theme.with_accent(theme.FALLBACK, "amber")).encode("ascii"))

    def test_by_month_is_the_last_accent(self):
        # ACCENT row, A, up from THEME DEFAULT wraps to BY MONTH, A; B.
        with mock.patch.object(month, "colour", return_value="dc3b3b"):
            write, _osk_file = self.run_screen([curses.KEY_DOWN, 10, curses.KEY_UP, 10, 27])
        self.assertEqual(theme.load_choice(self.config), ("midnight", "month"))
        self.assertEqual(theme.accent_label("month"), "BY MONTH")
        write.assert_called_once()
        self.assertIn("rgb:dc/3b/3b", write.call_args.args[1].decode("ascii"))

    def test_background_row_saves_the_style(self):
        # BACKGROUND row, A, WAVE AND SPARKLES -> WAVE -> PLAIN, A; B. The colours stay as they are.
        write, _osk_file = self.run_screen([curses.KEY_DOWN, curses.KEY_DOWN, 10,
                                            curses.KEY_DOWN, curses.KEY_DOWN, 10, 27])
        write.assert_not_called()
        self.assertEqual(background.load(self.config), background.PLAIN)
        self.assertEqual(theme.load_choice(self.config), ("midnight", ""))

    def test_sounds_row_turns_the_tv_sounds_off_and_on(self):
        import couchliteos_quick as quick

        write, _osk_file = self.run_screen([curses.KEY_DOWN] * 3 + [10, 27])
        write.assert_not_called()
        self.assertFalse(quick.sounds_enabled(self.config))
        self.assertEqual(theme.load_choice(self.config), ("midnight", ""))
        self.run_screen([curses.KEY_DOWN] * 3 + [10, 27])
        self.assertTrue(quick.sounds_enabled(self.config))

    def test_backing_out_changes_nothing(self):
        write, osk_file = self.run_screen([10, 27, 27])
        write.assert_not_called()
        self.assertFalse(self.config.exists() or osk_file.exists())


if __name__ == "__main__":
    unittest.main()
