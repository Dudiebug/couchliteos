import curses
import importlib.util
import pathlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock

import couchliteos_controls as controls
from test_gamepad_nav import Codes

XBOX_PAD = """I: Bus=0003 Vendor=045e Product=028e Version=0110
N: Name="Microsoft X-Box 360 pad"
P: Phys=usb-0000:00:14.0-1/input0
S: Sysfs=/devices/pci0000:00/0000:00:14.0/usb1/1-1/1-1:1.0/input/input5
U: Uniq=
H: Handlers=event5 js0
B: PROP=0
B: EV=20000b
B: KEY=7cdb000000000000 0 0 0 0
B: ABS=3003f
"""
DUALSENSE = """I: Bus=0005 Vendor=054c Product=0ce6 Version=8111
N: Name="Wireless Controller"
P: Phys=aa:bb:cc:dd:ee:ff
S: Sysfs=/devices/virtual/misc/uhid/0005:054C:0CE6.0003/input/input12
U: Uniq=11:22:33:44:55:66
H: Handlers=event12 js1
B: PROP=0
B: EV=20000b
B: KEY=7fdb000000000000 0 0 0 0
B: ABS=3003f
"""
DUALSENSE_MOTION = """I: Bus=0005 Vendor=054c Product=0ce6 Version=8111
N: Name="Wireless Controller Motion Sensors"
P: Phys=aa:bb:cc:dd:ee:ff
S: Sysfs=/devices/virtual/misc/uhid/0005:054C:0CE6.0003/input/input14
U: Uniq=11:22:33:44:55:66
H: Handlers=event14
B: PROP=40
B: EV=19
B: ABS=3f
"""
KEYBOARD = """I: Bus=0003 Vendor=046d Product=c31c Version=0110
N: Name="Logitech USB Keyboard"
P: Phys=usb-0000:00:14.0-2/input0
S: Sysfs=/devices/pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0/0003:046D:C31C.0001/input/input3
U: Uniq=
H: Handlers=sysrq kbd event3 leds
B: PROP=0
B: EV=120013
B: KEY=1000000000007 ff9f207ac14057ff febeffdfffefffff fffffffffffffffe
B: MSC=10
B: LED=7
"""
MOUSE = """I: Bus=0003 Vendor=046d Product=c077 Version=0111
N: Name="Logitech USB Optical Mouse"
P: Phys=usb-0000:00:14.0-3/input0
S: Sysfs=/devices/pci0000:00/0000:00:14.0/usb1/1-3/1-3:1.0/0003:046D:C077.0002/input/input4
U: Uniq=
H: Handlers=mouse0 event4
B: PROP=0
B: EV=17
B: KEY=ff0000 0 0 0 0
B: REL=903
B: MSC=10
"""
# gamepad-nav's own uinput device is a virtual keyboard: never a pad, and never a pad name.
UINPUT = """I: Bus=0003 Vendor=0000 Product=0000 Version=0000
N: Name="CouchLiteOS Launcher Navigation"
P: Phys=
S: Sysfs=/devices/virtual/input/input9
U: Uniq=
H: Handlers=sysrq kbd event9
B: PROP=0
B: EV=3
B: KEY=1000000000000 0 0 0 0 0
"""
SWITCH_PRO = XBOX_PAD.replace("Microsoft X-Box 360 pad", "Nintendo Co., Ltd. Pro Controller")


def proc(*blocks):
    return "\n".join(blocks)


class FakeScreen:
    def __init__(self, keys=(), size=(24, 80)):
        self.keys = list(keys)
        self.size = size
        self.drawn = []  # (row, column, text) of everything put on the screen, last frame only
        self.frames = 0

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.drawn = []

    def border(self, *_args):
        pass

    def addstr(self, row, column, text, *_attr):
        self.drawn.append((row, column, text))

    def addnstr(self, row, column, text, count, *_attr):
        self.drawn.append((row, column, text[:count]))

    def refresh(self):
        self.frames += 1

    def timeout(self, *_args):
        pass

    def keypad(self, *_args):
        pass

    def getch(self):
        if not self.keys:
            raise RuntimeError("the screen never closed")
        return self.keys.pop(0)


def words(text):
    return text.split()


class DeviceNamesTest(unittest.TestCase):
    def test_pad_names_keeps_only_real_gamepads(self):
        text = proc(XBOX_PAD, KEYBOARD, MOUSE, UINPUT, DUALSENSE_MOTION)
        self.assertEqual(controls.pad_names(text), ["Microsoft X-Box 360 pad"])

    def test_pad_names_finds_a_bluetooth_pad_under_uhid(self):
        # bluetoothd creates Bluetooth HID devices through uhid, which sysfs puts under
        # /devices/virtual/misc/uhid: "virtual" alone must not hide a real pad.
        self.assertEqual(controls.pad_names(proc(KEYBOARD, DUALSENSE)), ["Wireless Controller"])

    def test_pad_names_skips_uinput_pads(self):
        fake = XBOX_PAD.replace("/devices/pci0000:00/0000:00:14.0/usb1/1-1/1-1:1.0/input/input5", "/devices/virtual/input/input9")
        self.assertEqual(controls.pad_names(fake), [])

    def test_pad_names_empty_or_garbage_text(self):
        self.assertEqual(controls.pad_names(""), [])
        self.assertEqual(controls.pad_names("N: Name=\nB: KEY=zz\n\n\n"), [])

    def test_family_from_device_names(self):
        cases = {
            ("Microsoft X-Box 360 pad",): "xbox",
            ("Xbox Wireless Controller",): "xbox",
            ("Sony Interactive Entertainment DualSense Wireless Controller",): "playstation",
            ("Wireless Controller",): "playstation",
            ("PLAYSTATION(R)3 Controller",): "playstation",
            ("Nintendo Co., Ltd. Pro Controller",): "nintendo",
            ("Logitech Gamepad F310",): "generic",
            ("Xbox Wireless Controller", "Wireless Controller"): "generic",
            ("Xbox Wireless Controller", "Logitech Gamepad F310"): "generic",
            ("Xbox Wireless Controller", "Microsoft X-Box 360 pad"): "xbox",
            (): "generic",
        }
        for names, expected in cases.items():
            with self.subTest(names=names):
                self.assertEqual(controls.family(list(names)), expected)

    def test_detect_family_reads_the_proc_file_and_falls_back_to_generic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "devices"
            path.write_text(proc(KEYBOARD, SWITCH_PRO))
            self.assertEqual(controls.detect_family(path), "nintendo")
            self.assertEqual(controls.detect_family(pathlib.Path(directory) / "missing"), "generic")


class RowsTest(unittest.TestCase):
    def label_text(self, family, can_sleep=True):
        return "\n".join(" | ".join(label) for label, _text in controls.table(family, can_sleep))

    def test_xbox_rows_use_xbox_names(self):
        text = "\n".join(controls.rows("xbox", True))
        for name in ("A ", "B ", "X ", "Y ", "LB", "RB", "VIEW", "MENU", "GUIDE"):
            self.assertIn(name, text)
        self.assertNotIn("CROSS", text)
        self.assertNotIn("(L1)", text)

    def test_playstation_rows_use_cross_circle(self):
        text = "\n".join(controls.rows("playstation", True))
        for name in ("CROSS", "CIRCLE", "TRIANGLE", "SQUARE", "L1", "R1", "SHARE", "OPTIONS", "PS BUTTON"):
            self.assertIn(name, text)
        self.assertNotIn("VIEW", text)
        self.assertNotIn("GUIDE", text)

    def test_nintendo_rows_use_nintendo_names(self):
        text = "\n".join(controls.rows("nintendo", True))
        for name in ("MINUS", "PLUS", "HOME"):
            self.assertIn(name, text)
        self.assertNotIn("CROSS", text)

    def test_generic_rows_show_both_names(self):
        text = "\n".join(controls.rows("generic", True))
        for both in ("A (CROSS)", "B (CIRCLE)", "X (TRIANGLE)", "Y (SQUARE)", "LB (L1)", "RB (R1)",
                     "VIEW (SHARE)", "MENU (OPTIONS)", "GUIDE (PS BUTTON)"):
            self.assertIn(both, text)

    def test_every_row_says_what_the_button_does(self):
        rows = controls.rows("xbox", True)
        self.assertRegex(rows[0], r"^A\s+SELECT")
        self.assertRegex(rows[1], r"^B\s+BACK")
        self.assertRegex(rows[2], r"^X\s+ON-SCREEN KEYBOARD")
        self.assertRegex(rows[3], r"^Y\s+DELETE A LETTER")

    def test_sleep_row_only_when_supported(self):
        for family in ("xbox", "playstation", "nintendo", "generic"):
            with self.subTest(family=family):
                self.assertTrue(any("SLEEP" in row for row in controls.rows(family, True)))
                self.assertFalse(any("SLEEP" in row for row in controls.rows(family, False)))
        self.assertIn("HOLD GUIDE 3 S", "\n".join(controls.rows("xbox", True)))
        self.assertIn("HOLD PS BUTTON 3 S", "\n".join(controls.rows("playstation", True)))

    def test_every_row_fits_76_columns(self):
        for family in ("xbox", "playstation", "nintendo", "generic"):
            for can_sleep in (True, False):
                with self.subTest(family=family, can_sleep=can_sleep):
                    lines = controls.rows(family, can_sleep) + [controls.TITLE, controls.footer(family)]
                    self.assertTrue(lines)
                    for line in lines:
                        self.assertLessEqual(len(line), 76, line)

    def test_rows_are_upper_case_ascii(self):
        for family in ("xbox", "playstation", "nintendo", "generic"):
            for row in controls.rows(family, True) + [controls.footer(family)]:
                self.assertTrue(row.isascii(), row)
                self.assertEqual(row, row.upper())


class GamepadNavMappingTest(unittest.TestCase):
    """The help must describe what gamepad-nav really does, so a remap breaks these."""

    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType("evdev")
        fake.InputDevice = object
        fake.UInput = object
        fake.ecodes = Codes
        with mock.patch.dict(sys.modules, {"evdev": fake}):
            path = pathlib.Path(__file__).with_name("gamepad-nav.py")
            spec = importlib.util.spec_from_file_location("gamepad_nav_for_controls", path)
            cls.nav = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.nav)

    def key(self, code):
        return self.nav.key_for_event(SimpleNamespace(type=Codes.EV_KEY, value=1, code=code))

    # what each evdev button is called on each family's pad
    BUTTON_NAMES = {
        "xbox": {"BTN_SOUTH": "A", "BTN_EAST": "B", "BTN_NORTH": "X", "BTN_WEST": "Y",
                 "BTN_TL": "LB", "BTN_TR": "RB", "BTN_SELECT": "VIEW", "BTN_START": "MENU"},
        "playstation": {"BTN_SOUTH": "CROSS", "BTN_EAST": "CIRCLE", "BTN_NORTH": "TRIANGLE",
                        "BTN_WEST": "SQUARE", "BTN_TL": "L1", "BTN_TR": "R1", "BTN_SELECT": "SHARE",
                        "BTN_START": "OPTIONS"},
    }

    def described_as(self, family, button, can_sleep=True):
        """The description text of the row whose label names this button."""
        name = self.BUTTON_NAMES[family][button]
        found = [
            text for label, text in controls.table(family, can_sleep)
            if any(name in words(line) for line in label)
        ]
        self.assertEqual(len(found), 1, f"{family} {button} ({name}) must be on exactly one row")
        return found[0]

    def test_select_back_keyboard_delete_match_the_mapping(self):
        expected = {
            "BTN_SOUTH": (Codes.KEY_ENTER, "SELECT"),
            "BTN_EAST": (Codes.KEY_ESC, "BACK"),
            "BTN_NORTH": (Codes.KEY_F12, "KEYBOARD"),
            "BTN_WEST": (Codes.KEY_DELETE, "DELETE"),
        }
        for family in self.BUTTON_NAMES:
            for button, (key, meaning) in expected.items():
                with self.subTest(family=family, button=button):
                    self.assertEqual(self.key(getattr(Codes, button)), key)
                    self.assertIn(meaning, self.described_as(family, button))

    def test_shoulders_view_menu_are_app_shortcuts_f5_to_f8(self):
        for family in self.BUTTON_NAMES:
            for button, key in (("BTN_TL", Codes.KEY_F5), ("BTN_TR", Codes.KEY_F6),
                                ("BTN_SELECT", Codes.KEY_F7), ("BTN_START", Codes.KEY_F8)):
                with self.subTest(family=family, button=button):
                    self.assertEqual(self.key(getattr(Codes, button)), key)
                    self.assertIn("SHORTCUT", self.described_as(family, button))

    def test_the_shortcut_buttons_are_the_launchers_shortcut_keys(self):
        launcher = load_launcher()
        self.assertEqual(
            sorted(tag.split(" / ")[0] for tag in launcher.SHORTCUT_TAGS.values()), sorted(["LB", "RB", "VIEW", "MENU"])
        )
        self.assertEqual(
            sorted(launcher.SHORTCUT_KEYS), sorted([curses.KEY_F5, curses.KEY_F6, curses.KEY_F7, curses.KEY_F8])
        )

    def test_dpad_and_left_stick_move_and_holding_repeats(self):
        for family in self.BUTTON_NAMES:
            move = [(" ".join(label), text) for label, text in controls.table(family, True) if "D-PAD" in " ".join(label)]
            self.assertEqual(len(move), 1)
            self.assertIn("LEFT STICK", move[0][0])
            self.assertIn("HOLD", move[0][1])
        self.assertIn(Codes.ABS_X, self.nav.STICK_AXES)
        self.assertIn(Codes.ABS_Y, self.nav.STICK_AXES)
        self.assertIn(Codes.BTN_DPAD_UP, self.nav.DPAD_BUTTONS)
        self.assertGreater(self.nav.REPEAT_DELAY, 0)
        self.assertGreater(self.nav.REPEAT_INTERVAL, 0)

    def test_guide_is_the_home_button(self):
        self.assertTrue(self.nav.is_home_event(SimpleNamespace(type=Codes.EV_KEY, value=1, code=Codes.BTN_MODE)))
        guide = [text for label, text in controls.table("xbox", True) if "GUIDE" in " ".join(label) and "HOLD" not in " ".join(label)]
        self.assertEqual(len(guide), 1)

    def test_sleep_hold_matches_gamepad_nav(self):
        if not hasattr(self.nav, "SLEEP_HOLD_SECONDS"):
            self.skipTest("the sleep feature is not in this tree")  # runs once it is merged
        self.assertEqual(controls.SLEEP_HOLD_SECONDS, int(self.nav.SLEEP_HOLD_SECONDS))


_LAUNCHER = []


def load_launcher():
    if not _LAUNCHER:
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_for_controls", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LAUNCHER.append(module)
    return _LAUNCHER[0]


class SleepSupportTest(unittest.TestCase):
    def test_sleep_supported_follows_power_can_suspend(self):
        for answer in (True, False):
            fake = types.SimpleNamespace(can_suspend=lambda answer=answer: answer)
            with mock.patch.dict(sys.modules, {"couchliteos_power": fake}):
                self.assertIs(controls.sleep_supported(), answer)

    def test_sleep_supported_is_false_without_the_power_module_or_when_it_fails(self):
        with mock.patch.dict(sys.modules, {"couchliteos_power": None}):  # import raises ImportError
            self.assertFalse(controls.sleep_supported())
        def broken():
            raise OSError("no busctl")
        with mock.patch.dict(sys.modules, {"couchliteos_power": types.SimpleNamespace(can_suspend=broken)}):
            self.assertFalse(controls.sleep_supported())


class ScreenTest(unittest.TestCase):
    def shown(self, keys, **kwargs):
        screen = FakeScreen(keys)
        with tempfile.TemporaryDirectory() as directory:
            missing = pathlib.Path(directory) / "devices"
            controls.show(screen, can_sleep=kwargs.pop("can_sleep", True), proc=missing, **kwargs)
        return screen

    def test_screen_closes_on_a_or_b(self):
        for key in (10, 13, curses.KEY_ENTER, 27):
            with self.subTest(key=key):
                screen = self.shown([-1, curses.KEY_DOWN, key])
                self.assertEqual(screen.keys, [])
                self.assertGreaterEqual(screen.frames, 3)  # timeouts and other keys only redraw

    def test_screen_fits_80x24(self):
        for family_text, can_sleep in ((XBOX_PAD, True), (DUALSENSE, True), (SWITCH_PRO, False), ("", True), ("", False)):
            with tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / "devices"
                path.write_text(family_text)
                screen = FakeScreen([10])
                # getch is reached after the first frame, so the frame is still in screen.drawn
                controls.show(screen, can_sleep=can_sleep, proc=path)
                self.assertTrue(screen.drawn)
                for row, column, text in screen.drawn:
                    self.assertTrue(0 <= row < 24, (row, text))
                    self.assertLessEqual(column + len(text), 80 - 1, (column, text))

    def test_screen_shows_title_rows_and_the_close_hint_for_the_detected_pad(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "devices"
            path.write_text(XBOX_PAD)
            screen = FakeScreen([27])
            controls.show(screen, can_sleep=False, proc=path)
        text = "\n".join(item[2] for item in screen.drawn)
        self.assertIn("CONTROLLER BUTTONS", text)
        self.assertIn("SELECT / OK", text)
        self.assertIn("PRESS A OR B TO CLOSE", text)
        self.assertNotIn("SLEEP", text)

    def test_screen_clips_instead_of_crashing_on_a_tiny_terminal(self):
        screen = FakeScreen([10], size=(10, 30))
        controls.show(screen, can_sleep=True, proc=pathlib.Path("/nonexistent"))
        for row, column, text in screen.drawn:
            self.assertTrue(0 <= row < 10)
            self.assertLessEqual(column + len(text), 30)


class ShowOnceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = pathlib.Path(self.directory.name)
        self.setup_marker = root / "setup-complete"
        self.marker = root / "controls-shown"
        self.devices = root / "devices"

    def run_once(self, screen=None):
        screen = screen or FakeScreen([10])
        with mock.patch.object(controls, "show") as show:
            controls.show_once(screen, marker=self.marker, setup_marker=self.setup_marker)
        return show

    def test_show_once_only_after_setup_complete(self):
        self.assertFalse(controls.should_show_once(self.marker, self.setup_marker))
        self.run_once().assert_not_called()
        self.assertFalse(self.marker.exists())
        self.setup_marker.write_text("1\n")
        self.assertTrue(controls.should_show_once(self.marker, self.setup_marker))

    def test_show_once_writes_marker_and_never_shows_twice(self):
        self.setup_marker.write_text("1\n")
        self.run_once().assert_called_once()
        self.assertTrue(self.marker.exists())
        self.assertFalse(controls.should_show_once(self.marker, self.setup_marker))
        self.run_once().assert_not_called()

    def test_marker_is_written_before_the_screen_so_a_crash_cannot_loop_it(self):
        self.setup_marker.write_text("1\n")
        def crash(*_args, **_kwargs):
            self.assertTrue(self.marker.exists())
            raise curses.error("terminal too small")
        with mock.patch.object(controls, "show", side_effect=crash):
            controls.show_once(FakeScreen(), marker=self.marker, setup_marker=self.setup_marker)

    def test_marker_write_failure_does_not_crash(self):
        self.setup_marker.write_text("1\n")
        with mock.patch.object(controls.setup, "write_complete", side_effect=OSError("read-only")):
            controls.mark_shown(self.marker)  # must not raise
            with mock.patch.object(controls, "show") as show:
                controls.show_once(FakeScreen(), marker=self.marker, setup_marker=self.setup_marker)
            show.assert_called_once()  # the worst case is that the screen shows again

    def test_marker_lives_next_to_the_setup_marker(self):
        self.assertEqual(controls.SHOWN, controls.setup.MARKER.parent / "controls-shown")


class LauncherHookTest(unittest.TestCase):
    def test_settings_menu_has_the_row_just_before_the_setup_wizard(self):
        menu = load_launcher().SETTINGS_MENU
        self.assertEqual(menu.index("CONTROLLER BUTTONS") + 1, menu.index("SETUP WIZARD"))

    def test_settings_row_opens_the_screen(self):
        launcher = load_launcher()
        settings = launcher.Settings(FakeScreen(), mock.Mock())
        settings.selected = launcher.SETTINGS_MENU.index("CONTROLLER BUTTONS")
        with mock.patch.object(launcher.controls, "show") as show:
            self.assertTrue(settings.activate())
        show.assert_called_once_with(settings.screen)

    def test_launcher_runs_the_one_time_screen_right_after_the_wizard(self):
        launcher = load_launcher()
        order = []
        with mock.patch.object(launcher, "network_summary", return_value="OFFLINE"):
            app = launcher.Launcher(FakeScreen())
        app.prepare_session = lambda: None
        app.draw = lambda: None
        app.setup_wizard = lambda: order.append("wizard")
        with mock.patch.object(launcher.controls, "show_once", side_effect=lambda screen: order.append("controls")), \
                mock.patch.object(launcher.display, "restore_saved_mode"), \
                mock.patch.object(launcher, "read_key", side_effect=RuntimeError("stop")), \
                mock.patch.object(launcher.curses, "curs_set"), mock.patch.object(launcher.curses, "use_default_colors"), \
                tempfile.TemporaryDirectory() as run, mock.patch.object(launcher, "RUN", pathlib.Path(run)):
            with self.assertRaises(RuntimeError):
                app.run()
        self.assertEqual(order, ["wizard", "controls"])


if __name__ == "__main__":
    unittest.main()
