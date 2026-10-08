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
        for name in ("u_size", "u_time", "u_top", "u_bottom", "u_ribbon", "u_alpha", "u_sparkle", "u_fade"):
            self.assertRegex(wave.fragment(), rf"uniform \w+ {name};")
        self.assertTrue(wave.fragment().startswith("#version 330 core\n"))
        es = wave.fragment("300 es")
        self.assertTrue(es.startswith("#version 300 es\nprecision highp float;\n"))
        self.assertTrue(wave.vertex("300 es").startswith("#version 300 es\n"))

    def test_braces_balance(self):
        for source in (wave.fragment(), wave.vertex()):
            self.assertEqual(source.count("{"), source.count("}"))
            self.assertEqual(source.count("("), source.count(")"))
        self.assertIsNone(re.search(r"\d\.(?!\d)", wave.fragment()))  # every float literal is "1.0", not "1."


class SparkleTest(unittest.TestCase):
    WIDTH, HEIGHT = 96, 54

    def waves(self, time=wave.STILL_TIME):
        return [wave.ribbon_y((px + 0.5) / self.WIDTH, time, 0) for px in range(self.WIDTH)]

    def brute(self, time, min_size=0.0):
        waves, out = self.waves(time), {}
        for py in range(self.HEIGHT):
            for px in range(self.WIDTH):
                value = sum(wave.sparkle((px + 0.5) / self.WIDTH, (py + 0.5) / self.HEIGHT, time,
                                         self.WIDTH / self.HEIGHT, layer, waves[px], min_size) for layer in wave.LAYERS)
                if value > 0:
                    out[py * self.WIDTH + px] = value
        return out

    def test_the_quick_field_is_every_pixel_worked_out(self):
        for time in (0.0, wave.STILL_TIME, 1234.5):
            field = wave.sparkle_field(self.WIDTH, self.HEIGHT, time, self.waves(time), 0.8 / self.HEIGHT)
            brute = self.brute(time, 0.8 / self.HEIGHT)
            self.assertTrue(brute, time)
            self.assertEqual(set(field), set(brute), time)
            for index, value in brute.items():
                self.assertAlmostEqual(field[index], value)

    def test_a_sparkle_glows_only_inside_its_cell(self):
        # So the shader only looks at a pixel's own cell: on every cell's edge, nothing.
        for layer in wave.LAYERS:
            for time in (0.0, 17.3, 400.0):
                left, top = layer.drift[0] * time, layer.drift[1] * time
                for cx in range(-3, 12):
                    for cy in range(-4, 4):
                        for k in range(9):
                            along = (k + 0.5) / 9
                            for gx, gy in ((cx + along, cy), (cx, cy + along)):
                                x, y = (left + gx * layer.cell) / 1.75, 0.6 + top + gy * layer.cell
                                self.assertEqual(wave.sparkle(x, y, time, 1.75, layer, 0.6), 0.0)

    def test_there_are_a_few_and_they_drift_and_twinkle(self):
        first = wave.sparkle_field(self.WIDTH, self.HEIGHT, 100.0, self.waves(100.0))
        later = wave.sparkle_field(self.WIDTH, self.HEIGHT, 103.0, self.waves(103.0))
        self.assertTrue(first)
        self.assertNotEqual(set(first), set(later))
        self.assertLess(len(first), self.WIDTH * self.HEIGHT * 0.1)  # dots, not a haze
        self.assertTrue(all(value <= sum(layer.brightness for layer in wave.LAYERS) * 1.3 for value in first.values()))
        glints = [wave._sparkle_in(3, 1, time / 10)[2] for time in range(300)]
        self.assertAlmostEqual(min(glints), wave.SPARKLE_GLINT, places=2)
        self.assertGreater(max(glints), 0.95)

    def test_they_ride_the_wave(self):
        for time in (0.0, wave.STILL_TIME, 300.0):
            waves = self.waves(time)
            field = wave.sparkle_field(self.WIDTH, self.HEIGHT, time, waves)
            self.assertTrue(field)
            for index in field:
                py, px = divmod(index, self.WIDTH)
                self.assertLessEqual(abs((py + 0.5) / self.HEIGHT - waves[px]), wave.SPARKLE_REACH)
            # Closer to the ribbon, brighter: most of the light is within one band of it.
            near = sum(v for i, v in field.items() if abs((i // self.WIDTH + 0.5) / self.HEIGHT - waves[i % self.WIDTH]) < wave.SPARKLE_BAND)
            self.assertGreater(near, sum(field.values()) * 0.6)

    def test_the_sheet_is_brighter_at_its_edges_and_nothing_outside(self):
        self.assertAlmostEqual(wave.sheet(0.5, 0.4, 0.6), wave.SHEET_ALPHA * 0.35)
        self.assertGreater(wave.sheet(0.59, 0.4, 0.6), wave.sheet(0.5, 0.4, 0.6))
        self.assertEqual(wave.sheet(0.3, 0.4, 0.6), 0.0)
        self.assertEqual(wave.sheet(0.7, 0.4, 0.6), 0.0)
        self.assertLessEqual(max(wave.sheet(y / 1000, 0.4, 0.6) for y in range(1000)), wave.SHEET_ALPHA)
        self.assertIn("float y1 = ribbon_y(uv.x, u_time, 1);", wave.fragment())
        self.assertIn(f"return {wave._float(wave.SHEET_ALPHA)} * inside * (0.35 + 0.65 * pow(abs(2.0 * s - 1.0), 3.0));",
                      wave.fragment())

    def test_the_hash_is_the_shaders(self):
        values = [wave.cell_hash(x, y) for x in range(-20, 20) for y in range(-5, 5)]
        self.assertTrue(all(0 <= value < 1 for value in values))
        self.assertGreater(len({round(value, 3) for value in values}), 300)
        source = wave.fragment()
        self.assertIn("vec3 h = fract(vec3(p.xyx) * 0.1031);", source)
        self.assertIn("h += dot(h, h.yzx + 33.33);", source)
        for layer in wave.LAYERS:
            self.assertIn(f"{wave._float(layer.cell)}, {wave._float(layer.density)}, {wave._float(layer.size)}, "
                          f"vec2({wave._float(layer.drift[0])}, {wave._float(layer.drift[1])})", source)
        self.assertIn(f"exp(-(above * above) / {wave._float(wave.SPARKLE_BAND ** 2)})", source)
        self.assertIn(f"if (abs(above) > {wave._float(wave.SPARKLE_REACH)}) return 0.0;", source)

    def test_dark_themes_sparkle_more_and_flat_has_none(self):
        dark = wave.palette(theme.FALLBACK, 12)
        themes, _ = theme.available(BUILTIN, pathlib.Path("/nonexistent"))
        light = wave.palette(themes["daylight"], 12)
        self.assertGreater(dark.sparkle, light.sparkle)
        self.assertGreater(light.sparkle, 0)
        flat = wave.Palette((0.5, 0.5, 0.5), (0.5, 0.5, 0.5), (1.0, 1.0, 1.0), 0.0)
        self.assertEqual(flat.sparkle, 0.0)


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

    def test_the_still_frame_has_sparkles(self):
        width, height = 160, 90
        plain = wave.Palette(self.COLOURS.top, self.COLOURS.bottom, self.COLOURS.ribbon, 0.3)
        sparkling = wave.Palette(self.COLOURS.top, self.COLOURS.bottom, self.COLOURS.ribbon, 0.3, 0.9)
        a, b = wave.still_frame(width, height, plain), wave.still_frame(width, height, sparkling)
        brighter = [i for i in range(0, len(a), 3) if sum(b[i:i + 3]) > sum(a[i:i + 3]) + 30]
        self.assertTrue(10 < len(brighter) < width * height * 0.05, len(brighter))
        self.assertFalse([i for i in range(0, len(a), 3) if sum(b[i:i + 3]) < sum(a[i:i + 3])])

    def test_flat_is_the_gradient_alone(self):
        flat = wave.Palette((0.5, 0.5, 0.5), (0.5, 0.5, 0.5), (1.0, 1.0, 1.0), 0.0)
        self.assertEqual(set(wave.still_frame(8, 8, flat)), {128})


if __name__ == "__main__":
    unittest.main()
