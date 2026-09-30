import importlib.util
import pathlib
import sys
import types
import unittest
from types import SimpleNamespace


class Codes:
    EV_KEY = 1
    EV_ABS = 3
    BTN_GAMEPAD = 304
    BTN_SOUTH = 304
    BTN_EAST = 305
    BTN_NORTH = 307
    BTN_WEST = 308
    BTN_TL = 310
    BTN_TR = 311
    BTN_SELECT = 314
    BTN_START = 315
    BTN_MODE = 316
    BTN_DPAD_UP = 544
    BTN_DPAD_DOWN = 545
    BTN_DPAD_LEFT = 546
    BTN_DPAD_RIGHT = 547
    ABS_HAT0X = 16
    ABS_HAT0Y = 17
    KEY_UP = 103
    KEY_DOWN = 108
    KEY_LEFT = 105
    KEY_RIGHT = 106
    KEY_ENTER = 28
    KEY_ESC = 1
    KEY_HOME = 102
    KEY_DELETE = 111
    KEY_F5 = 63
    KEY_F6 = 64
    KEY_F7 = 65
    KEY_F8 = 66
    KEY_F12 = 88


class GamepadMappingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType("evdev")
        fake.InputDevice = object
        fake.UInput = object
        fake.ecodes = Codes
        sys.modules["evdev"] = fake
        path = pathlib.Path(__file__).with_name("gamepad-nav.py")
        spec = importlib.util.spec_from_file_location("gamepad_nav", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_south_and_east_buttons_map_to_select_and_back(self):
        south = SimpleNamespace(type=Codes.EV_KEY, value=1, code=Codes.BTN_SOUTH)
        east = SimpleNamespace(type=Codes.EV_KEY, value=1, code=Codes.BTN_EAST)
        self.assertEqual(self.module.key_for_event(south), Codes.KEY_ENTER)
        self.assertEqual(self.module.key_for_event(east), Codes.KEY_ESC)

    def test_west_button_maps_to_close_action(self):
        west = SimpleNamespace(type=Codes.EV_KEY, value=1, code=Codes.BTN_WEST)
        self.assertEqual(self.module.key_for_event(west), Codes.KEY_DELETE)

    def test_north_button_opens_the_keyboard_and_shoulders_are_shortcuts(self):
        expected = {
            Codes.BTN_NORTH: Codes.KEY_F12,
            Codes.BTN_TL: Codes.KEY_F5,
            Codes.BTN_TR: Codes.KEY_F6,
            Codes.BTN_SELECT: Codes.KEY_F7,
            Codes.BTN_START: Codes.KEY_F8,
        }
        for code, key in expected.items():
            event = SimpleNamespace(type=Codes.EV_KEY, value=1, code=code)
            self.assertEqual(self.module.key_for_event(event), key)
            self.assertIn(key, self.module.KEYS)

    def test_dpad_and_hat_map_to_arrows(self):
        dpad = SimpleNamespace(type=Codes.EV_KEY, value=1, code=Codes.BTN_DPAD_DOWN)
        hat = SimpleNamespace(type=Codes.EV_ABS, value=-1, code=Codes.ABS_HAT0X)
        self.assertEqual(self.module.key_for_event(dpad), Codes.KEY_DOWN)
        self.assertEqual(self.module.key_for_event(hat), Codes.KEY_LEFT)

    def test_release_is_not_reemitted(self):
        release = SimpleNamespace(type=Codes.EV_KEY, value=0, code=Codes.BTN_SOUTH)
        self.assertIsNone(self.module.key_for_event(release))

    def test_keyboard_and_controller_home_are_global_events(self):
        mode = SimpleNamespace(type=Codes.EV_KEY, value=1, code=Codes.BTN_MODE)
        home = SimpleNamespace(type=Codes.EV_KEY, value=1, code=Codes.KEY_HOME)
        release = SimpleNamespace(type=Codes.EV_KEY, value=0, code=Codes.KEY_HOME)
        self.assertTrue(self.module.is_home_event(mode))
        self.assertTrue(self.module.is_home_event(home))
        self.assertFalse(self.module.is_home_event(release))

    def event(self, code, value):
        return SimpleNamespace(type=Codes.EV_KEY, value=value, code=code)

    def hold(self):
        return self.module.HomeHold()

    def test_holding_home_for_three_seconds_requests_sleep_exactly_once(self):
        hold = self.hold()
        hold.feed(self.event(Codes.BTN_MODE, 1), 100.0)
        self.assertFalse(hold.due(102.9))
        self.assertTrue(hold.due(103.0))
        self.assertFalse(hold.due(103.5))
        hold.feed(self.event(Codes.BTN_MODE, 0), 104.0)
        self.assertFalse(hold.due(110.0))

    def test_a_short_press_never_requests_sleep(self):
        hold = self.hold()
        hold.feed(self.event(Codes.KEY_HOME, 1), 10.0)
        hold.feed(self.event(Codes.KEY_HOME, 0), 11.5)
        self.assertFalse(hold.due(20.0))

    def test_keyboard_autorepeat_does_not_restart_or_shorten_the_hold(self):
        hold = self.hold()
        hold.feed(self.event(Codes.KEY_HOME, 1), 50.0)
        for tick in range(1, 25):
            hold.feed(self.event(Codes.KEY_HOME, 2), 50.0 + tick * 0.1)
        self.assertFalse(hold.due(52.9))
        self.assertTrue(hold.due(53.0))

    def test_other_buttons_and_a_second_press_are_independent(self):
        hold = self.hold()
        hold.feed(self.event(Codes.BTN_SOUTH, 1), 0.0)
        self.assertFalse(hold.due(10.0))
        hold.feed(self.event(Codes.BTN_MODE, 1), 20.0)
        hold.feed(self.event(Codes.BTN_MODE, 0), 21.0)
        hold.feed(self.event(Codes.BTN_MODE, 1), 30.0)
        self.assertFalse(hold.due(32.0))
        self.assertTrue(hold.due(33.0))

    def test_timeout_shrinks_to_the_remaining_hold_and_reset_clears_it(self):
        hold = self.hold()
        self.assertEqual(hold.timeout(5.0), 1.0)
        hold.feed(self.event(Codes.BTN_MODE, 1), 5.0)
        self.assertAlmostEqual(hold.timeout(6.5), 1.0)
        self.assertAlmostEqual(hold.timeout(7.5), 0.5)
        self.assertEqual(hold.timeout(9.0), 0.0)
        hold.reset()
        self.assertEqual(hold.timeout(9.0), 1.0)
        self.assertFalse(hold.due(99.0))

    def test_request_sleep_touches_the_suspend_request(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            request = pathlib.Path(directory) / 'suspend'
            old, self.module.SLEEP_REQUEST = self.module.SLEEP_REQUEST, request
            try:
                self.module.request_sleep()
            finally:
                self.module.SLEEP_REQUEST = old
            self.assertTrue(request.exists())


if __name__ == "__main__":
    unittest.main()
