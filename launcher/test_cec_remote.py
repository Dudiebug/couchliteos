import importlib.util
import pathlib
import sys
import types
import unittest
from types import SimpleNamespace


class Codes:
    EV_KEY = 1
    EV_REL = 2
    EV_ABS = 3
    BTN_LEFT = 272
    BTN_RIGHT = 273
    BTN_MIDDLE = 274
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
    ABS_X = 0
    ABS_Y = 1
    ABS_RX = 3
    ABS_RY = 4
    REL_X = 0
    REL_Y = 1
    REL_HWHEEL = 6
    REL_WHEEL = 8
    ABS_HAT0X = 16
    ABS_HAT0Y = 17
    KEY_UP = 103
    KEY_DOWN = 108
    KEY_LEFT = 105
    KEY_RIGHT = 106
    KEY_ENTER = 28
    KEY_ESC = 1
    KEY_HOME = 102
    KEY_HOMEPAGE = 172
    KEY_DELETE = 111
    KEY_F5 = 63
    KEY_F6 = 64
    KEY_F7 = 65
    KEY_F8 = 66
    KEY_F12 = 88
    KEY_BACK = 158
    KEY_SPACE = 57
    KEY_PAGEUP = 104
    KEY_PAGEDOWN = 109
    KEY_LEFTMETA = 125
    KEY_RIGHTMETA = 126
    # Keyboard brightness and volume keys (gamepad-nav acts on them).
    KEY_MUTE = 113
    KEY_VOLUMEDOWN = 114
    KEY_VOLUMEUP = 115
    KEY_BRIGHTNESSDOWN = 224
    KEY_BRIGHTNESSUP = 225
    # The Home shortcut (Settings > CONTROLS): L3/R3 and Ctrl+Alt+H.
    BTN_THUMBL = 317
    BTN_THUMBR = 318
    KEY_LEFTCTRL = 29
    KEY_RIGHTCTRL = 97
    KEY_LEFTALT = 56
    KEY_RIGHTALT = 100
    KEY_H = 35
    # Keys the kernel's rc-cec keymap reports for a TV remote.
    KEY_OK = 352
    KEY_EXIT = 174
    KEY_BACK = 158
    KEY_CLEAR = 355
    KEY_MENU = 139
    KEY_ROOT_MENU = 0x2FA
    KEY_RED = 398
    KEY_GREEN = 399
    KEY_YELLOW = 400
    KEY_BLUE = 401
    KEY_PLAYPAUSE = 164


def press(code, value=1):
    return SimpleNamespace(type=Codes.EV_KEY, value=value, code=code)


class CecRemoteNavTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType("evdev")
        fake.InputDevice = object
        fake.UInput = object
        fake.ecodes = Codes
        sys.modules["evdev"] = fake
        path = pathlib.Path(__file__).with_name("gamepad-nav.py")
        spec = importlib.util.spec_from_file_location("gamepad_nav_cec", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.emitted = []
        self.homes = []
        self.app_running = False
        self.osk_open = False
        self.launcher_has_focus = False
        module = self.module
        self.saved = (module.emit, module.request_home, module.app_active, module.OSK_ACTIVE, module.LAUNCHER_FOCUS)
        module.emit = lambda ui, key: self.emitted.append(key)
        module.request_home = lambda: self.homes.append(True)
        module.app_active = lambda: self.app_running
        module.OSK_ACTIVE = SimpleNamespace(exists=lambda: self.osk_open)
        module.LAUNCHER_FOCUS = SimpleNamespace(exists=lambda: self.launcher_has_focus)
        self.addCleanup(self.restore)

    def restore(self):
        module = self.module
        module.emit, module.request_home, module.app_active, module.OSK_ACTIVE, module.LAUNCHER_FOCUS = self.saved

    def test_remote_keys_are_translated_to_launcher_keys(self):
        for remote, launcher in (
            (Codes.KEY_UP, Codes.KEY_UP),
            (Codes.KEY_RIGHT, Codes.KEY_RIGHT),
            (Codes.KEY_OK, Codes.KEY_ENTER),
            (Codes.KEY_EXIT, Codes.KEY_ESC),
            (Codes.KEY_BACK, Codes.KEY_ESC),
            (Codes.KEY_RED, Codes.KEY_F5),
            (Codes.KEY_BLUE, Codes.KEY_F8),
        ):
            self.emitted.clear()
            self.module.handle_cec_event(None, press(remote))
            self.assertEqual(self.emitted, [launcher])
            self.assertIn(launcher, self.module.KEYS)

    def test_home_like_remote_keys_ask_for_the_launcher(self):
        for code in (Codes.KEY_HOME, Codes.KEY_ROOT_MENU, Codes.KEY_MENU):
            self.module.handle_cec_event(None, press(code))
        self.assertEqual(len(self.homes), 3)
        self.assertEqual(self.emitted, [])

    def test_home_works_while_an_app_is_running(self):
        self.app_running = True
        self.module.handle_cec_event(None, press(Codes.KEY_ROOT_MENU))
        self.assertEqual(len(self.homes), 1)

    def test_navigation_reaches_the_running_app(self):
        # The remote's device is grabbed, so nobody else would ever see these keys.
        self.app_running = True
        for remote, sent in (
            (Codes.KEY_UP, Codes.KEY_UP),
            (Codes.KEY_DOWN, Codes.KEY_DOWN),
            (Codes.KEY_LEFT, Codes.KEY_LEFT),
            (Codes.KEY_RIGHT, Codes.KEY_RIGHT),
            (Codes.KEY_OK, Codes.KEY_ENTER),
            (Codes.KEY_EXIT, Codes.KEY_ESC),
            (Codes.KEY_BACK, Codes.KEY_ESC),
        ):
            self.emitted.clear()
            self.module.handle_cec_event(None, press(remote))
            self.assertEqual(self.emitted, [sent])

    def test_launcher_shortcut_keys_stay_out_of_a_running_app(self):
        self.app_running = True
        for code in (Codes.KEY_RED, Codes.KEY_GREEN, Codes.KEY_YELLOW, Codes.KEY_BLUE, Codes.KEY_CLEAR):
            self.module.handle_cec_event(None, press(code))
        self.assertEqual(self.emitted, [])

    def test_launcher_shortcut_keys_reach_the_launcher_when_it_has_the_focus_over_a_running_app(self):
        # Home brought the launcher forward while the app keeps running behind it: the same test as the gamepad's
        self.app_running = True
        self.launcher_has_focus = True
        for remote, sent in (
            (Codes.KEY_RED, Codes.KEY_F5),
            (Codes.KEY_GREEN, Codes.KEY_F6),
            (Codes.KEY_YELLOW, Codes.KEY_F7),
            (Codes.KEY_BLUE, Codes.KEY_F8),
            (Codes.KEY_CLEAR, Codes.KEY_DELETE),
        ):
            self.emitted.clear()
            self.module.handle_cec_event(None, press(remote))
            self.assertEqual(self.emitted, [sent])

    def test_the_focus_makes_the_remote_agree_with_the_gamepad(self):
        for app_running in (False, True):
            for focus in (False, True):
                for osk in (False, True):
                    self.app_running, self.launcher_has_focus, self.osk_open = app_running, focus, osk
                    self.emitted.clear()
                    self.module.handle_cec_event(None, press(Codes.KEY_RED))
                    blocked = self.module.navigation_blocked(self.osk_open)
                    self.assertEqual(self.emitted, [] if blocked else [Codes.KEY_F5], (app_running, focus, osk))

    def test_navigation_still_reaches_the_launcher_and_a_running_app_whoever_has_the_focus(self):
        self.app_running = True
        for focus in (False, True):
            self.launcher_has_focus = focus
            self.emitted.clear()
            self.module.handle_cec_event(None, press(Codes.KEY_UP))
            self.module.handle_cec_event(None, press(Codes.KEY_OK))
            self.module.handle_cec_event(None, press(Codes.KEY_BACK))
            self.assertEqual(self.emitted, [Codes.KEY_UP, Codes.KEY_ENTER, Codes.KEY_ESC])

    def test_the_on_screen_keyboard_still_gets_navigation_while_an_app_runs(self):
        self.app_running = True
        self.osk_open = True
        self.module.handle_cec_event(None, press(Codes.KEY_DOWN))
        self.assertEqual(self.emitted, [Codes.KEY_DOWN])

    def test_releases_repeats_and_unmapped_keys_are_ignored(self):
        self.module.handle_cec_event(None, press(Codes.KEY_OK, value=0))
        self.module.handle_cec_event(None, press(Codes.KEY_OK, value=2))
        self.module.handle_cec_event(None, press(Codes.KEY_PLAYPAUSE))
        self.module.handle_cec_event(None, SimpleNamespace(type=Codes.EV_ABS, value=1, code=Codes.KEY_OK))
        self.assertEqual(self.emitted, [])
        self.assertEqual(self.homes, [])


if __name__ == "__main__":
    unittest.main()
