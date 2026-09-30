"""Couch-usability tests: status messages, cursor identity, scrolling, hints, RDP connect."""

import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock


class Screen:
    """Fake curses window. Keys may be ints or (key, seconds_to_advance_the_clock)."""

    def __init__(self, keys=(), size=(30, 100), clock=None):
        self.keys = list(keys)
        self.size = size
        self.clock = clock
        self.rows = {}

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.rows = {}

    def border(self, *_args):
        pass

    def addstr(self, row, column, text, *_args):
        self.rows[(row, column)] = text

    def addnstr(self, row, column, text, length, *_args):
        self.rows[(row, column)] = text[:length]

    def refresh(self):
        pass

    def timeout(self, *_args):
        pass

    def keypad(self, *_args):
        pass

    def text(self):
        return [value for _key, value in sorted(self.rows.items())]

    def getch(self):
        if not self.keys:
            raise RuntimeError("stop")
        key = self.keys.pop(0)
        if isinstance(key, tuple):
            key, seconds = key
            self.clock.now += seconds
        return key


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def load_launcher():
    path = pathlib.Path(__file__).with_name("moonlightos-launcher.py")
    spec = importlib.util.spec_from_file_location("launcher_ux2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MainScreenStatusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_launcher()

    def run_main_loop(self, keys, on_activate):
        """Run Launcher.run() on `keys`; return the status shown at each redraw."""
        clock = Clock()
        summaries = iter(f"SUMMARY {number}" for number in range(1, 100))
        with mock.patch.object(self.module, "network_summary", side_effect=lambda: next(summaries)), mock.patch.object(
            self.module.time, "monotonic", clock
        ):
            launcher = self.module.Launcher(Screen(keys, clock=clock))
        launcher.prepare_session = mock.Mock()
        launcher.setup_wizard = mock.Mock()
        launcher.activate = lambda: on_activate(launcher)
        shown = []
        launcher.draw = lambda: shown.append(launcher.status)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module.time, "monotonic", clock), mock.patch.object(
            self.module, "network_summary", side_effect=lambda: next(summaries)
        ), mock.patch.object(self.module.display, "restore_saved_mode"), mock.patch.object(
            self.module.curses, "curs_set"
        ), mock.patch.object(self.module.curses, "use_default_colors"):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                launcher.run()
        return shown[2:]  # run() draws twice before the first key

    @staticmethod
    def fail_after_a_long_launch(launcher):
        launcher.status = "MOONLIGHT FAILED TO START"

    def test_a_failure_message_survives_the_network_refresh_that_follows_a_slow_launch(self):
        # A failed launch takes ~18 s, so the 5 s summary refresh is already due.
        enter = self.module.curses.KEY_ENTER
        shown = self.run_main_loop([(enter, 18), (-1, 1), (-1, 1)], self.fail_after_a_long_launch)
        self.assertEqual(shown, ["MOONLIGHT FAILED TO START"] * 3)

    def test_the_message_stays_until_a_button_is_pressed_then_the_summary_returns(self):
        enter = self.module.curses.KEY_ENTER
        down = self.module.curses.KEY_DOWN
        shown = self.run_main_loop([(enter, 18), (-1, 6), (-1, 6), (down, 0)], self.fail_after_a_long_launch)
        self.assertEqual(shown[:3], ["MOONLIGHT FAILED TO START"] * 3)
        self.assertTrue(shown[3].startswith("SUMMARY"), shown)

    def test_the_message_goes_after_fifteen_seconds_without_a_press(self):
        enter = self.module.curses.KEY_ENTER
        shown = self.run_main_loop([(enter, 1), (-1, 10), (-1, 10)], self.fail_after_a_long_launch)
        self.assertEqual(shown[0], "MOONLIGHT FAILED TO START")
        self.assertEqual(shown[1], "MOONLIGHT FAILED TO START")  # 10 s in
        self.assertTrue(shown[2].startswith("SUMMARY"), shown)  # 20 s in

    def test_the_network_summary_still_refreshes_when_nothing_is_showing(self):
        shown = self.run_main_loop([(-1, 6), (-1, 6)], lambda _launcher: None)
        self.assertEqual(shown[0], "SUMMARY 2")
        self.assertEqual(shown[1], "SUMMARY 3")


if __name__ == "__main__":
    unittest.main()
