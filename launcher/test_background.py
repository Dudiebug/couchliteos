import testenv  # noqa: F401  (first: scratch run and state directories)
"""Settings > APPEARANCE > BACKGROUND: the saved style, and what it makes of the wave's mode and
colours (the TV interface applies these; test_tv.py checks it does)."""

import dataclasses
import pathlib
import tempfile
import unittest

import couchliteos_background as background
import couchliteos_theme as theme
import couchliteos_wave as wave


class SavedTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = pathlib.Path(directory.name) / "config.ini"

    def test_wave_and_sparkles_by_default(self):
        self.assertEqual(background.DEFAULT, background.WAVE)
        self.assertEqual(background.load(self.config), background.WAVE)
        self.config.write_text("[appearance]\nbackground = fireworks\n")
        self.assertEqual(background.load(self.config), background.WAVE)
        self.config.write_text("not ini")
        self.assertEqual(background.load(self.config), background.WAVE)

    def test_saved_and_read_back_beside_the_theme(self):
        theme.save_choice("ocean", "teal", self.config)
        for style in (background.PLAIN, background.CALM, background.WAVE):
            background.save(style, self.config)
            self.assertEqual(background.load(self.config), style)
        self.assertEqual(theme.load_choice(self.config), ("ocean", "teal"))
        self.config.write_text("[appearance]\nbackground = Plain \n")
        self.assertEqual(background.load(self.config), background.PLAIN)

    def test_an_unknown_style_is_not_saved(self):
        with self.assertRaises(ValueError):
            background.save("fireworks", self.config)
        self.assertFalse(self.config.exists())

    def test_labels(self):
        self.assertEqual([background.label(style) for style, _label in background.CHOICES],
                         ["WAVE AND SPARKLES", "WAVE", "PLAIN"])
        self.assertEqual(background.label("fireworks"), "WAVE AND SPARKLES")


class WaveTest(unittest.TestCase):
    def setUp(self):
        self.colours = wave.palette(theme.FALLBACK, 14)

    def test_wave_and_sparkles_change_nothing(self):
        self.assertEqual(background.wave_palette(self.colours, background.WAVE), self.colours)
        for how in (wave.GL, wave.STATIC, wave.FLAT):
            self.assertEqual(background.wave_mode(how, background.WAVE), how)
            self.assertEqual(background.wave_mode(how, background.CALM), how)

    def test_wave_has_no_sparkles(self):
        self.assertGreater(self.colours.sparkle, 0)
        calm = background.wave_palette(self.colours, background.CALM)
        self.assertEqual(calm.sparkle, 0.0)
        self.assertEqual((calm.top, calm.bottom, calm.ribbon, calm.alpha),
                         (self.colours.top, self.colours.bottom, self.colours.ribbon, self.colours.alpha))

    def test_plain_is_the_gradient_alone_and_still(self):
        plain = background.wave_palette(self.colours, background.PLAIN)
        self.assertEqual((plain.alpha, plain.sparkle), (0.0, 0.0))
        self.assertEqual((plain.top, plain.bottom), (self.colours.top, self.colours.bottom))
        self.assertEqual(background.wave_mode(wave.GL, background.PLAIN), wave.STATIC)
        self.assertEqual(background.wave_mode(wave.STATIC, background.PLAIN), wave.STATIC)
        self.assertEqual(background.wave_mode(wave.FLAT, background.PLAIN), wave.FLAT, "HIGH CONTRAST stays flat")

    def test_plain_draws_no_ribbon(self):
        # The ribbon's colour makes no difference to the frame: only the gradient is drawn.
        plain = background.wave_palette(self.colours, background.PLAIN)
        red = dataclasses.replace(plain, ribbon=(1.0, 0.0, 0.0))
        self.assertEqual(wave.still_frame(48, 27, plain), wave.still_frame(48, 27, red))
        self.assertNotEqual(wave.still_frame(48, 27, self.colours),
                            wave.still_frame(48, 27, dataclasses.replace(self.colours, ribbon=(1.0, 0.0, 0.0))))


if __name__ == "__main__":
    unittest.main()
