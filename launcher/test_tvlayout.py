import testenv  # noqa: F401  (first: scratch run and state directories)
import re
import unittest

import couchliteos_power as power
import couchliteos_theme as theme
import couchliteos_tvlayout as tvlayout


class KeysTest(unittest.TestCase):
    def test_every_key_gamepad_nav_sends_does_something(self):
        # D-pad, A (Enter), B (Esc), Y (Delete), X (F12), LB/RB/VIEW/MENU (F5-F8)
        for key in ("Up", "Down", "Left", "Right", "Return", "Escape", "Delete", "F12", "F5", "F6", "F7", "F8"):
            self.assertIsNotNone(tvlayout.action(key), key)
        self.assertEqual(tvlayout.action("Return"), "activate")
        self.assertEqual(tvlayout.action("Escape"), "back")
        self.assertEqual(tvlayout.action("F7"), "shortcut:view")

    def test_other_keys_are_ignored(self):
        for key in ("a", "Tab", "", None):
            self.assertIsNone(tvlayout.action(key))

    def test_hints_name_the_keyboard_key_with_each_button(self):
        for hint in (tvlayout.HOME_HINT, tvlayout.ACTIVE_HINT, tvlayout.FAILURE_HINT, tvlayout.QUESTION_HINT):
            self.assertEqual(hint, hint.upper())
            self.assertIn("ENTER", hint)
            self.assertIn("ESC", hint)


class LayoutTest(unittest.TestCase):
    def test_no_text_under_28_px_at_1080p(self):
        fonts = tvlayout.Layout(1920, 1080).fonts
        self.assertGreaterEqual(min(fonts.values()), 28)

    def test_sizes_scale_with_the_height(self):
        full, small, large = tvlayout.Layout(1920, 1080), tvlayout.Layout(1280, 720), tvlayout.Layout(3840, 2160)
        self.assertEqual(small.fonts["bar"], 19)
        self.assertEqual(large.fonts["bar"], 56)
        self.assertLess(small.tile_height("games"), full.tile_height("games"))

    def test_a_five_percent_safe_margin(self):
        layout = tvlayout.Layout(1920, 1080)
        self.assertEqual((layout.margin_x, layout.margin_y), (96, 54))

    def test_six_tiles_fit_across_inside_the_margins(self):
        for width, height in ((1920, 1080), (1280, 720), (1024, 768), (3840, 2160)):
            layout = tvlayout.Layout(width, height)
            across = tvlayout.MAX_TILES * layout.tile_width + (tvlayout.MAX_TILES - 1) * layout.gap
            self.assertLessEqual(across, width - 2 * layout.margin_x)

    def test_the_rows_fit_the_screen_height(self):
        layout = tvlayout.Layout(1920, 1080)
        rows = sum(layout.tile_height(row) for row in ("games", "apps", "system"))
        self.assertLess(rows, 0.65 * (1080 - 2 * layout.margin_y))  # room for the bar, titles and prompts
        self.assertGreater(layout.tile_height("games"), layout.tile_height("apps"))
        self.assertGreater(layout.tile_height("apps"), layout.tile_height("system"))


class CarouselTest(unittest.TestCase):
    def test_a_short_row_shows_everything(self):
        self.assertEqual(list(tvlayout.visible(4, 3)), [0, 1, 2, 3])

    def test_at_most_six_tiles_and_the_focus_is_always_on_screen(self):
        for count in range(0, 20):
            for focus in range(count):
                shown = tvlayout.visible(count, focus)
                self.assertLessEqual(len(shown), tvlayout.MAX_TILES)
                self.assertIn(focus, shown)

    def test_a_long_row_keeps_the_focus_in_a_fixed_column(self):
        columns = {list(tvlayout.visible(20, focus)).index(focus) for focus in range(1, 15)}
        self.assertEqual(columns, {tvlayout.ANCHOR})
        self.assertEqual(list(tvlayout.visible(20, 19)), list(range(14, 20)))  # the end does not run past the row


class StylesheetTest(unittest.TestCase):
    def test_the_theme_colours_are_written_in(self):
        css = tvlayout.stylesheet(theme.FALLBACK, tvlayout.Layout(1920, 1080))
        for field in ("background", "surface", "text", "accent", "focus", "muted"):
            self.assertIn("#" + theme.FALLBACK.colours[field], css)
        self.assertNotIn("var(", css)  # GTK before 4.16 has no CSS variables

    def test_every_font_size_is_at_least_28_px_at_1080p(self):
        css = tvlayout.stylesheet(theme.FALLBACK, tvlayout.Layout(1920, 1080))
        sizes = [int(size) for size in re.findall(r"font-size: (\d+)px", css)]
        self.assertTrue(sizes)
        self.assertGreaterEqual(min(sizes), 28)

    def test_text_on_the_focus_colour_stays_readable(self):
        high = theme.Theme("high-contrast", "HIGH CONTRAST", {
            **theme.FALLBACK_COLOURS, "background": "000000", "text": "ffffff", "focus": "ffff00"})
        css = tvlayout.stylesheet(high, tvlayout.Layout(1920, 1080))
        self.assertIn(".tv-tile.tv-focused label { color: #000000; }", css)

    def test_initial(self):
        self.assertEqual(tvlayout.initial("living room mini"), "L")
        self.assertEqual(tvlayout.initial("  [steam]"), "S")
        self.assertEqual(tvlayout.initial(""), "?")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class IdleWatchTest(unittest.TestCase):
    def watch(self, blank=2, sleep=0, running=False, enabled=True):
        self.clock = Clock()
        return tvlayout.IdleWatch(power.Settings(blank=blank, sleep=sleep), apps_running=lambda: running,
                                  clock=self.clock, enabled=lambda: enabled)

    def test_blank_after_the_timeout_and_the_first_key_only_wakes(self):
        watch = self.watch(blank=2)
        self.clock.now = 119
        self.assertIsNone(watch.tick())
        self.clock.now = 121
        self.assertEqual(watch.tick(), power.BLANK)
        self.assertTrue(watch.blanked)
        self.assertTrue(watch.key())  # swallowed: it only wakes the screen
        self.assertFalse(watch.blanked)
        self.assertFalse(watch.key())  # the next one acts

    def test_sleep_is_asked_for_after_its_own_timeout(self):
        watch = self.watch(blank=0, sleep=15)
        self.clock.now = 15 * 60 + 1
        self.assertEqual(watch.tick(), power.SLEEP)
        self.assertIsNone(watch.tick())  # once

    def test_nothing_blanks_while_an_app_runs(self):
        watch = self.watch(blank=2, running=True)
        for now in range(0, 600, 30):
            self.clock.now = now
            self.assertIsNone(watch.tick())

    def test_a_key_resets_the_timer(self):
        watch = self.watch(blank=2)
        self.clock.now = 100
        watch.key()
        self.clock.now = 200
        self.assertIsNone(watch.tick())

    def test_the_first_key_after_a_resume_is_dropped(self):
        watch = self.watch(blank=2)
        watch.resumed()
        self.assertTrue(watch.key())
        self.assertFalse(watch.key())

    def test_off_during_the_smoke_test(self):
        watch = self.watch(blank=2, enabled=False)
        self.clock.now = 10_000
        self.assertIsNone(watch.tick())
        self.assertFalse(watch.blanked)


if __name__ == "__main__":
    unittest.main()
