import testenv  # noqa: F401  (first: scratch run and state directories)
"""ACCENT > BY MONTH: the twelve colours, the days the colour moves on, the wave staying readable
in every month on every built-in theme, and the saved choice."""

import dataclasses
import datetime
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_month as month
import couchliteos_theme as theme
import couchliteos_wave as wave

BUILTIN = pathlib.Path(__file__).resolve().parents[1] / "overlay/usr/share/couchliteos/themes"


def day(month_number, day_number, year=2026):
    return datetime.date(year, month_number, day_number)


def today(date):
    """Stands in for the datetime module in couchliteos_month: today is `date`."""
    return mock.Mock(date=mock.Mock(today=mock.Mock(return_value=date)))


class PaletteTest(unittest.TestCase):
    def test_twelve_different_colours(self):
        self.assertEqual(len(month.COLOURS), 12)
        self.assertEqual(len(set(month.COLOURS)), 12)
        for colour in month.COLOURS:
            self.assertEqual(theme.parse_colour(colour), colour)

    def test_the_console_order(self):
        # Grey in January, red in December; blue in August, as on the PS3.
        red, green, blue = (int(month.COLOURS[11][at:at + 2], 16) for at in (0, 2, 4))
        self.assertGreater(red, 2 * max(green, blue))
        red, green, blue = (int(month.COLOURS[7][at:at + 2], 16) for at in (0, 2, 4))
        self.assertGreater(blue, max(red, green))
        red, green, blue = (int(month.COLOURS[0][at:at + 2], 16) for at in (0, 2, 4))
        self.assertLess(max(red, green, blue) - min(red, green, blue), 24)


class SwitchTest(unittest.TestCase):
    def test_last_months_colour_until_the_blend(self):
        for number in (1, 11):
            self.assertEqual(month.colour(day(5, number)), month.COLOURS[3])  # April's pink in early May

    def test_this_months_colour_from_the_fifteenth(self):
        for number in (15, 20, 31):
            self.assertEqual(month.colour(day(5, number)), month.COLOURS[4])

    def test_the_three_days_before_blend_in_order(self):
        april, may = month.COLOURS[3], month.COLOURS[4]
        blend = [month.colour(day(5, number)) for number in (12, 13, 14)]
        self.assertEqual(len(set(blend + [april, may])), 5)
        # Each step is nearer May's colour than the one before.
        distance = [sum(abs(int(colour[at:at + 2], 16) - int(may[at:at + 2], 16)) for at in (0, 2, 4))
                    for colour in [april, *blend, may]]
        self.assertEqual(distance, sorted(distance, reverse=True))
        self.assertEqual(distance[-1], 0)
        self.assertEqual(month.colour(day(5, 13)), month._mix(april, may, 0.5))

    def test_january_comes_from_december(self):
        self.assertEqual(month.colour(day(1, 3)), month.COLOURS[11])
        self.assertEqual(month.colour(day(1, 15)), month.COLOURS[0])
        self.assertEqual(month.colour(day(12, 15)), month.COLOURS[11])

    def test_today_by_default(self):
        with mock.patch.object(month, "datetime", today(day(8, 30))):
            self.assertEqual(month.colour(), month.COLOURS[7])


class ReadableTest(unittest.TestCase):
    def test_the_wave_stays_readable_in_every_month_on_every_theme(self):
        themes, problems = theme.available(BUILTIN, pathlib.Path("/nonexistent"))
        self.assertEqual(problems, [])
        colours = set(month.COLOURS) | {month.colour(day(number, 13)) for number in range(1, 13)}
        for item in themes.values():
            text = item.colours["text"]
            for colour in colours:
                look = dataclasses.replace(item, colours={**item.colours, "accent": colour})
                for hour in range(0, 24, 2):
                    palette = wave.palette(look, hour)
                    for part in ("top", "bottom"):
                        where = (item.name, colour, hour, part)
                        self.assertGreaterEqual(theme.contrast(text, palette.hex(part)), theme.MIN_CONTRAST, where)
                        under_ribbon = wave._mix(getattr(palette, part), palette.ribbon,
                                                 min(1.0, wave.MAX_GLOW * palette.alpha))
                        self.assertGreaterEqual(theme.contrast(text, wave._hex(under_ribbon)), wave.LARGE_CONTRAST,
                                                where + ("ribbon",))


class ChoiceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = pathlib.Path(directory.name) / "config.ini"

    def test_saved_and_read_back(self):
        theme.save_choice("ocean", month.NAME, self.config)
        self.assertEqual(theme.load_choice(self.config), ("ocean", month.NAME))

    def test_with_accent_uses_todays_colour(self):
        with mock.patch.object(month, "colour", return_value="dc3b3b"):
            looks = theme.with_accent(theme.FALLBACK, month.NAME)
        self.assertEqual(looks.colours["accent"], "dc3b3b")
        self.assertEqual({**looks.colours, "accent": theme.FALLBACK.colours["accent"]}, theme.FALLBACK.colours)

    def test_current_follows_the_month(self):
        theme.save_choice("midnight", month.NAME, self.config)
        for number, expected in ((2, month.COLOURS[0]), (9, month.COLOURS[8])):
            with mock.patch.object(month, "datetime", today(day(number, 20 if number == 9 else 1))):
                chosen = theme.current(self.config, BUILTIN, pathlib.Path("/nonexistent"))
            self.assertEqual(chosen.colours["accent"], expected)

    def test_labels(self):
        self.assertEqual(theme.accent_label(month.NAME), "BY MONTH")
        self.assertEqual(theme.accent_label("teal"), "TEAL")
        self.assertEqual(theme.accent_label(""), "THEME DEFAULT")


if __name__ == "__main__":
    unittest.main()
