import importlib.util
import pathlib
import tempfile
import types
import unittest
from unittest import mock


class Screen:
    """Replays keys and remembers every string drawn, so tests can read the screen."""

    def __init__(self, keys=(), on_getch=None):
        self.keys = list(keys)
        self.drawn = []
        self.ever = []  # everything drawn since the start, across redraws
        self.on_getch = on_getch  # called on every getch; with it, running out of keys is "no key pressed"
        self.getch_calls = 0

    def getmaxyx(self):
        return 30, 100

    def erase(self):
        self.drawn.clear()

    def border(self, *_args):
        pass

    def addstr(self, _row, _column, text, *_args):
        self.drawn.append(text)
        self.ever.append(text)

    def addnstr(self, _row, _column, text, *_args):
        self.drawn.append(text)
        self.ever.append(text)

    def refresh(self):
        pass

    def timeout(self, *_args):
        pass

    def keypad(self, *_args):
        pass

    def getch(self):
        self.getch_calls += 1
        if self.on_getch is not None:
            if self.getch_calls > 500:
                raise RuntimeError("the screen never ended")
            self.on_getch()
            return self.keys.pop(0) if self.keys else -1
        if not self.keys:
            raise RuntimeError("the screen asked for more keys than the test sent")
        return self.keys.pop(0)


KEY_UP, KEY_DOWN, KEY_F5, ENTER, ESC = 259, 258, 269, 10, 27


class LauncherCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_cec", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def usable(self):
        cec = self.module.cec
        return cec.Status(cec.Adapter("/dev/cec1", "vivid", "vivid-000-vid-out0", frozenset(), "1.1.0.0", 0x10))


class TvControlScreenTest(LauncherCase):

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

    def test_third_row_toggles_tv_standby_when_the_pc_sleeps(self):
        cec = self.module.cec
        saved, _queried = self.open_screen([KEY_DOWN, KEY_DOWN, ENTER, ESC])
        saved.assert_called_once_with(cec.Settings(True, False, False))

    def test_refresh_asks_the_tv_again_and_back_leaves(self):
        # the rows are the three switches, REFRESH, TEST REMOTE BUTTONS, BACK
        saved, queried = self.open_screen([KEY_DOWN, KEY_DOWN, KEY_DOWN, ENTER, KEY_DOWN, KEY_DOWN, ENTER])
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

    def test_the_standby_switch_is_disabled_on_a_pc_that_cannot_suspend(self):
        saved, _queried = self.open_screen([KEY_DOWN, KEY_DOWN, ENTER, ESC], can_sleep=False)
        saved.assert_not_called()
        self.assertIn("NOT SUPPORTED", self.tv.status)

    def test_without_an_adapter_the_reason_is_shown_and_the_toggles_are_disabled(self):
        cec = self.module.cec
        saved, _queried = self.open_screen([ESC], status=cec.Status(None))
        text = "\n".join(self.screen.drawn)
        self.assertIn("NO CEC ADAPTER FOUND", text)
        self.assertIn("PULSE-EIGHT", text)
        self.assertEqual(text.count("UNAVAILABLE"), 3)
        self.assertNotIn("  ON", text)
        self.assertNotIn(self.module.cec.REMOTE_TEST_ROW, text)  # nothing to test without a CEC adapter

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


    def test_the_remote_test_row_follows_refresh_when_an_adapter_is_present(self):
        cec = self.module.cec
        self.open_screen([ESC])
        text = "\n".join(self.screen.drawn)
        self.assertIn(cec.REMOTE_TEST_ROW, text)
        self.assertLess(text.index("REFRESH"), text.index(cec.REMOTE_TEST_ROW))
        self.assertLess(text.index(cec.REMOTE_TEST_ROW), text.index("BACK"))

    def test_enter_on_the_remote_test_row_opens_the_test(self):
        with mock.patch.object(self.module.Settings, "run_remote_test") as test:
            saved, _queried = self.open_screen([KEY_DOWN] * 4 + [ENTER, ESC])
        test.assert_called_once_with()
        saved.assert_not_called()

    def test_the_remote_test_row_is_shown_for_an_adapter_with_no_tv_but_will_not_open(self):
        cec = self.module.cec
        unlinked = cec.Status(cec.Adapter("/dev/cec1", "vivid", "", frozenset(), "f.f.f.f", 0))
        with mock.patch.object(self.module.Settings, "run_remote_test") as test:
            self.open_screen([KEY_DOWN] * 4 + [ENTER, ESC], status=unlinked)
        test.assert_not_called()
        self.assertIn(cec.REMOTE_TEST_ROW, "\n".join(self.screen.ever))
        self.assertEqual(self.tv.status, "NOT AVAILABLE: TV NOT CONNECTED")

    def test_the_standby_row_explains_that_the_tv_is_only_turned_off_when_it_shows_this_box(self):
        cec = self.module.cec
        self.open_screen([KEY_DOWN, KEY_DOWN, ESC])
        self.assertIn(cec.STANDBY_HINT, self.screen.drawn)
        self.assertIn("ONLY IF THE TV IS SHOWING IT", cec.STANDBY_HINT)

    def test_other_rows_have_no_extra_explanation(self):
        cec = self.module.cec
        self.open_screen([ESC])
        self.assertNotIn(cec.STANDBY_HINT, self.screen.drawn)
        self.assertNotIn(cec.row_hint(cec.REMOTE_TEST_ROW), self.screen.drawn)

    def test_the_remote_test_row_has_its_own_explanation(self):
        cec = self.module.cec
        self.open_screen([KEY_DOWN] * 4 + [ESC])
        self.assertTrue(cec.row_hint(cec.REMOTE_TEST_ROW))
        self.assertIn(cec.row_hint(cec.REMOTE_TEST_ROW), self.screen.drawn)


class RemoteTestScreenTest(LauncherCase):
    """TEST REMOTE BUTTONS: the screen that names each key as it arrives."""

    START = 1000.0

    def run_test(self, keys=(), step=1.0, home_request=False):
        """Run the screen on a fake clock that advances `step` seconds on every wait for a key."""
        clock = [self.START]
        screen = Screen(keys, on_getch=lambda: clock.__setitem__(0, clock[0] + step))
        tv = self.module.Settings(screen, mock.Mock())
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        home = pathlib.Path(folder.name) / "home.request"
        if home_request:
            home.touch()
        for patch in (
            mock.patch.object(self.module, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda _seconds: None)),
            mock.patch.object(self.module, "HOME_REQUEST", home),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        tv.run_remote_test()
        self.screen, self.home = screen, home
        return screen

    def last_button(self, screen=None):
        """The 'LAST BUTTON' values drawn so far, oldest first (a frame that repeats the one before is dropped)."""
        marker = "LAST BUTTON  "
        shown = [text.split(marker)[1] for text in (screen or self.screen).ever if marker in text]
        return [value for index, value in enumerate(shown) if index == 0 or value != shown[index - 1]]

    def test_it_closes_after_ten_seconds_with_no_button(self):
        screen = self.run_test()
        self.assertEqual(screen.getch_calls, 10)

    def test_a_button_starts_the_ten_seconds_again(self):
        screen = self.run_test([-1] * 5 + [KEY_UP])
        self.assertEqual(screen.getch_calls, 16)  # not 10: UP came at the 6th second and the timer restarted

    def test_back_twice_in_a_row_leaves_at_once(self):
        screen = self.run_test([ESC, ESC])
        self.assertEqual(screen.getch_calls, 2)

    def test_back_with_another_button_between_does_not_leave(self):
        screen = self.run_test([ESC, KEY_UP, ESC])
        self.assertEqual(screen.getch_calls, 13)  # only the quiet ten seconds after the last button ended it
        self.assertEqual(self.last_button(screen)[-3:], ["BACK", "UP", "BACK"])

    def test_one_back_does_not_leave(self):
        screen = self.run_test([ESC])
        self.assertEqual(screen.getch_calls, 11)

    def test_each_button_is_named(self):
        curses = self.module.curses
        screen = self.run_test([KEY_UP, ENTER, ESC, curses.KEY_F5, curses.KEY_DC])
        names = self.last_button(screen)
        for name in ("UP", "OK", "BACK", "RED → F5", "CLEAR → DELETE"):
            self.assertIn(name, names)

    def test_the_screen_says_what_to_do_and_how_to_leave(self):
        cec = self.module.cec
        screen = self.run_test()
        text = "\n".join(screen.ever)
        self.assertIn("PRESS BUTTONS ON THE TV REMOTE", text)
        self.assertIn("PRESS BACK TWICE TO LEAVE", text)
        self.assertIn("CLOSES IN 10 S", text)
        self.assertIn("CLOSES IN 1 S", text)
        self.assertIn(cec.REMOTE_TEST_ROW, text)  # the title

    def test_an_unknown_keycode_shows_the_raw_code_and_does_not_crash(self):
        screen = self.run_test([9999])
        self.assertIn("UNKNOWN KEY 9999", self.last_button(screen))

    def test_earlier_buttons_stay_on_the_screen(self):
        screen = self.run_test([KEY_UP, ENTER])
        self.assertTrue(any("LAST BUTTON  OK" in text for text in screen.ever))
        self.assertTrue(any("EARLIER  UP" in text for text in screen.ever))

    def test_the_home_button_leaves_a_request_file_and_is_named_not_acted_on(self):
        screen = self.run_test(home_request=True)
        self.assertIn("HOME", self.last_button(screen))
        self.assertFalse(self.home.exists())  # consumed, so the launcher does not go Home when this screen ends

    def test_a_resize_is_not_a_button(self):
        screen = self.run_test([self.module.curses.KEY_RESIZE])
        self.assertEqual(set(self.last_button(screen)), {"-"})
        self.assertEqual(screen.getch_calls, 10)

    def test_the_screen_goes_back_to_the_slow_poll_even_if_drawing_fails(self):
        timeouts = []
        screen = Screen()
        screen.timeout = timeouts.append
        tv = self.module.Settings(screen, mock.Mock())
        with mock.patch.object(tv, "draw", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                tv.run_remote_test()
        self.assertEqual(timeouts, [100, 1000])

    def test_every_key_the_launcher_names_is_a_key_the_remote_can_become(self):
        cec = self.module.cec
        self.assertEqual(set(self.module.REMOTE_TEST_KEYS.values()), set(cec.REMOTE_LABELS) - {cec.HOME_KEY})


if __name__ == "__main__":
    unittest.main()
