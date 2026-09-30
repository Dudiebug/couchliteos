import importlib.util
import pathlib
import unittest
from unittest import mock


class Screen:
    """Replays keys and remembers every string drawn, so tests can read the screen."""

    def __init__(self, keys=()):
        self.keys = list(keys)
        self.drawn = []

    def getmaxyx(self):
        return 30, 100

    def erase(self):
        self.drawn.clear()

    def border(self, *_args):
        pass

    def addstr(self, _row, _column, text, *_args):
        self.drawn.append(text)

    def addnstr(self, _row, _column, text, *_args):
        self.drawn.append(text)

    def refresh(self):
        pass

    def timeout(self, *_args):
        pass

    def keypad(self, *_args):
        pass

    def getch(self):
        if not self.keys:
            raise RuntimeError("the screen asked for more keys than the test sent")
        return self.keys.pop(0)


KEY_DOWN, ENTER, ESC = 258, 10, 27


class TvControlScreenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("moonlightos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_cec", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def usable(self):
        cec = self.module.cec
        return cec.Status(cec.Adapter("/dev/cec1", "vivid", "vivid-000-vid-out0", frozenset(), "1.1.0.0", 0x10))

    def open_screen(self, keys, settings=None, status=None, save_error=None, can_sleep=True):
        cec = self.module.cec
        screen = Screen(keys)
        tv = self.module.Settings(screen, mock.Mock())
        patches = (
            mock.patch.object(cec, "load_settings", return_value=settings or cec.Settings()),
            mock.patch.object(cec, "save_settings", side_effect=save_error),
            mock.patch.object(cec, "query_status", return_value=status or self.usable()),
            mock.patch.object(cec, "suspend_supported", return_value=can_sleep),
        )
        _loaded, saved, queried, _sleep = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        tv.run_tv_control()
        self.tv, self.screen = tv, screen
        return saved, queried

    def test_menu_lists_tv_control_and_opens_the_screen(self):
        self.assertIn("TV CONTROL", self.module.SETTINGS_MENU)
        settings = self.module.Settings(Screen(), mock.Mock())
        settings.selected = self.module.SETTINGS_MENU.index("TV CONTROL")
        with mock.patch.object(self.module.Settings, "run_tv_control") as screen:
            self.assertTrue(settings.activate())
        screen.assert_called_once_with()

    def test_enter_toggles_turn_tv_on_and_saves_it(self):
        cec = self.module.cec
        saved, _queried = self.open_screen([ENTER, ESC])
        saved.assert_called_once_with(cec.Settings(turn_tv_on=False, sleep_on_tv_off=False))

    def test_second_row_toggles_sleep_when_tv_turns_off(self):
        cec = self.module.cec
        saved, _queried = self.open_screen([KEY_DOWN, ENTER, ESC])
        saved.assert_called_once_with(cec.Settings(turn_tv_on=True, sleep_on_tv_off=True))

    def test_refresh_asks_the_tv_again_and_back_leaves(self):
        saved, queried = self.open_screen([KEY_DOWN, KEY_DOWN, ENTER, KEY_DOWN, ENTER])
        self.assertEqual(queried.call_count, 2)
        saved.assert_not_called()

    def test_escape_leaves_without_saving(self):
        saved, queried = self.open_screen([ESC])
        saved.assert_not_called()
        self.assertEqual(queried.call_count, 1)

    def test_a_failed_save_is_reported_not_raised(self):
        self.open_screen([ENTER, ESC], save_error=OSError("read-only"))
        self.assertIn("NOT SAVED", self.tv.status)

    def test_the_sleep_switch_is_disabled_on_a_pc_that_cannot_suspend(self):
        saved, _queried = self.open_screen([KEY_DOWN, ENTER, ESC], can_sleep=False)
        saved.assert_not_called()
        self.assertIn("NOT SUPPORTED", self.tv.status)

    def test_without_an_adapter_the_reason_is_shown_and_the_toggles_are_disabled(self):
        cec = self.module.cec
        saved, _queried = self.open_screen([ESC], status=cec.Status(None))
        text = "\n".join(self.screen.drawn)
        self.assertIn("NO CEC ADAPTER FOUND", text)
        self.assertIn("PULSE-EIGHT", text)
        self.assertEqual(text.count("UNAVAILABLE"), 2)
        self.assertNotIn("  ON", text)

    def test_without_an_adapter_the_toggles_cannot_be_switched_or_saved_over(self):
        cec = self.module.cec
        elsewhere = cec.Settings(turn_tv_on=False, sleep_on_tv_off=True)
        saved, _queried = self.open_screen(
            [ENTER, KEY_DOWN, ENTER, ESC], settings=elsewhere, status=cec.Status(None)
        )
        saved.assert_not_called()
        self.assertIn("NO CEC ADAPTER", self.tv.status)

    def test_an_adapter_with_no_link_to_a_tv_is_disabled_too(self):
        cec = self.module.cec
        unlinked = cec.Status(cec.Adapter("/dev/cec1", "vivid", "", frozenset(), "f.f.f.f", 0))
        saved, _queried = self.open_screen([ENTER, ESC], status=unlinked)
        saved.assert_not_called()
        self.assertIn("NOT CONNECTED", "\n".join(self.screen.drawn) + self.tv.status)


if __name__ == "__main__":
    unittest.main()
