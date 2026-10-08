import testenv  # noqa: F401  (first: scratch run and state directories)
import pathlib
import re
import unittest

import couchliteos_motion as motion
import couchliteos_theme as theme
import couchliteos_wave as wave

BUILTIN = pathlib.Path(__file__).resolve().parents[1] / "overlay/usr/share/couchliteos/themes"


def every_look():
    themes, _ = theme.available(BUILTIN, pathlib.Path("/nonexistent"))
    for item in themes.values():
        for accent, _ in theme.ACCENTS:
            yield theme.with_accent(item, accent)


class ModeTest(unittest.TestCase):
    def test_gl_animates_and_everything_else_is_still_or_flat(self):
        self.assertEqual(wave.mode("ngl", motion.FULL, "midnight"), wave.GL)
        self.assertEqual(wave.mode(None, motion.REDUCED, "midnight"), wave.GL)
        self.assertEqual(wave.mode("cairo", motion.FULL, "midnight"), wave.STATIC)
        self.assertEqual(wave.mode("ngl", motion.OFF, "midnight"), wave.STATIC)
        self.assertEqual(wave.mode("ngl", motion.FULL, "midnight", failed_before=True), wave.STATIC)
        self.assertEqual(wave.mode("ngl", motion.FULL, "high-contrast"), wave.FLAT)


class PaletteTest(unittest.TestCase):
    def test_text_stays_readable_on_every_theme_accent_and_hour(self):
        for look in every_look():
            text = look.colours["text"]
            for hour in range(0, 24, 3):
                colours = wave.palette(look, hour)
                for part in ("top", "bottom"):
                    self.assertGreaterEqual(theme.contrast(text, colours.hex(part)), theme.MIN_CONTRAST,
                                            (look.name, look.colours["accent"], hour, part))
                    under_ribbon = wave._mix(getattr(colours, part), colours.ribbon,
                                             min(1.0, wave.MAX_GLOW * colours.alpha))
                    self.assertGreaterEqual(theme.contrast(text, wave._hex(under_ribbon)), wave.LARGE_CONTRAST,
                                            (look.name, look.colours["accent"], hour, part, "ribbon"))

    def test_the_accent_tints_the_top(self):
        midnight = theme.FALLBACK
        blue = wave.palette(theme.with_accent(midnight, "blue"))
        red = wave.palette(theme.with_accent(midnight, "red"))
        self.assertNotEqual(blue.top, red.top)
        self.assertGreater(blue.top[2], red.top[2])

    def test_a_dark_theme_is_brighter_by_day_than_at_night(self):
        def luminance(colour):
            return theme._luminance(wave._hex(colour))
        noon, night = wave.palette(theme.FALLBACK, 14), wave.palette(theme.FALLBACK, 3)
        self.assertGreater(luminance(noon.top), luminance(night.top))
        self.assertGreater(luminance(noon.bottom), luminance(night.bottom))

    def test_daylight_follows_the_clock(self):
        self.assertAlmostEqual(wave.daylight(3), 0.0)
        self.assertAlmostEqual(wave.daylight(15), 1.0)
        self.assertAlmostEqual(wave.daylight(9), wave.daylight(21))


class RibbonTest(unittest.TestCase):
    def test_ribbons_stay_in_the_lower_middle_of_the_screen(self):
        for ribbon in range(len(wave.RIBBON_OFFSETS)):
            for step in range(0, 600, 7):
                for x in (0, 0.25, 0.5, 0.75, 1):
                    self.assertTrue(0.4 < wave.ribbon_y(x, step / 10, ribbon) < 0.8)

    def test_ribbons_move_slowly(self):
        # Under a third of a screen height in a second, anywhere: a calm background, not a busy one.
        for x in (0, 0.5, 1):
            for t in (0, 10, 33.3):
                self.assertLess(abs(wave.ribbon_y(x, t + 1, 0) - wave.ribbon_y(x, t, 0)), 0.05)

    def test_still_points_span_the_screen_in_pixels(self):
        points = wave.ribbon_points(1920, 1080, segments=8)
        self.assertEqual(len(points), 9)
        self.assertEqual(points[0][0], 0)
        self.assertEqual(points[-1][0], 1920)
        self.assertAlmostEqual(points[4][1], 1080 * wave.ribbon_y(0.5, wave.STILL_TIME, 0))


class ShaderTest(unittest.TestCase):
    def test_the_shader_uses_the_same_waves_as_python(self):
        source = wave.fragment()
        for amplitude, frequency, speed, phase in wave.WAVES:
            self.assertIn(f"{wave._float(amplitude)} * sin(x * {wave._float(frequency)} + t * "
                          f"{wave._float(speed)} + k * {wave._float(phase)})", source)
        self.assertIn(f"return {wave._float(wave.BASE)} + OFFSETS[i]", source)

    def test_uniforms_and_versions(self):
        for name in ("u_size", "u_time", "u_top", "u_bottom", "u_ribbon", "u_alpha", "u_fade"):
            self.assertRegex(wave.fragment(), rf"uniform \w+ {name};")
        self.assertTrue(wave.fragment().startswith("#version 330 core\n"))
        es = wave.fragment("300 es")
        self.assertTrue(es.startswith("#version 300 es\nprecision mediump float;\n"))
        self.assertTrue(wave.vertex("300 es").startswith("#version 300 es\n"))

    def test_braces_balance(self):
        for source in (wave.fragment(), wave.vertex()):
            self.assertEqual(source.count("{"), source.count("}"))
            self.assertEqual(source.count("("), source.count(")"))
        self.assertIsNone(re.search(r"\d\.(?!\d)", wave.fragment()))  # every float literal is "1.0", not "1."


class ClockTest(unittest.TestCase):
    def test_time_only_moves_while_shown_and_a_stall_is_a_pause(self):
        clock = wave.Clock(start=0)
        self.assertEqual(clock.frame(100.0), 0)
        self.assertAlmostEqual(clock.frame(100.05), 0.05)
        self.assertAlmostEqual(clock.frame(105.0), 0.15)  # a 5 s stall moves it 0.1 s
        clock.pause()
        self.assertAlmostEqual(clock.frame(500.0), 0.15)

    def test_at_most_fps_frames_a_second(self):
        clock = wave.Clock()
        self.assertTrue(clock.due(0))
        clock.frame(0)
        self.assertFalse(clock.due(0.01))
        self.assertTrue(clock.due(1 / wave.FPS))


class StillTest(unittest.TestCase):
    COLOURS = wave.Palette((0.2, 0.3, 0.6), (0.0, 0.0, 0.1), (1.0, 1.0, 1.0), 0.3)

    def pixel(self, data, width, x, y):
        at = (y * width + x) * 3
        return tuple(data[at:at + 3])

    def test_size_gradient_and_a_ribbon_where_the_shader_puts_it(self):
        width, height = 64, 36
        data = wave.still_frame(width, height, self.COLOURS)
        self.assertEqual(len(data), width * height * 3)
        self.assertGreater(sum(self.pixel(data, width, 0, 0)), sum(self.pixel(data, width, width - 1, height - 1)))
        middle = round(wave.ribbon_y(32.5 / width, wave.STILL_TIME, 0) * height - 0.5)
        bare = wave.still_frame(width, height, wave.Palette(self.COLOURS.top, self.COLOURS.bottom, self.COLOURS.ribbon, 0.0))
        self.assertGreater(sum(self.pixel(data, width, 32, middle)), sum(self.pixel(bare, width, 32, middle)) + 60)

    def test_flat_is_the_gradient_alone(self):
        flat = wave.Palette((0.5, 0.5, 0.5), (0.5, 0.5, 0.5), (1.0, 1.0, 1.0), 0.0)
        self.assertEqual(set(wave.still_frame(8, 8, flat)), {128})


if __name__ == "__main__":
    unittest.main()
