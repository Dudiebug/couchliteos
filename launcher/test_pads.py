import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import pathlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock

import couchliteos_controllers as controllers
import couchliteos_pads as pads
from test_gamepad_nav import Codes, FakePad, Stop

MAC_A = "aa:bb:cc:dd:ee:01"
MAC_B = "aa:bb:cc:dd:ee:02"


class Device:
    """A pad as evdev opens it: records rumble commands, and refuses to be grabbed."""

    def __init__(self, name="Xbox Wireless Controller", uniq="", phys="usb-0000:00:14.0-2/input0",
                 bustype=0x03, vendor=0x045E, product=0x0B12, keys=(Codes.BTN_SOUTH,), rumble=True, events=()):
        self.name = name
        self.uniq = uniq
        self.phys = phys
        self.info = SimpleNamespace(bustype=bustype, vendor=vendor, product=product)
        self.keys = list(keys)
        self.can_rumble = rumble
        self.commands = []
        self.closed = False
        self.events = list(events)

    def capabilities(self):
        caps = {pads.EV_KEY: self.keys}
        if self.can_rumble:
            caps[pads.EV_FF] = [pads.FF_RUMBLE]
        return caps

    def upload_effect(self, effect):
        self.commands.append(("upload", effect))
        return 7

    def write(self, kind, code, value):
        self.commands.append(("write", kind, code, value))

    def erase_effect(self, effect_id):
        self.commands.append(("erase", effect_id))

    def grab(self):
        raise AssertionError("Settings > CONTROLLERS must never grab a pad")

    def absinfo(self, _code):
        return SimpleNamespace(min=-32768, max=32767)

    def read(self):
        events, self.events = self.events, []
        return events

    def fileno(self):
        return 0

    def close(self):
        self.closed = True


def opener_for(devices):
    return lambda path: devices[path]


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = pathlib.Path(self._tmp.name) / "config.ini"

    def test_the_id_is_the_pads_uniq_else_its_usb_port(self):
        self.assertEqual(pads.pad_id(Device(uniq="AA:BB:CC:DD:EE:01")), MAC_A)
        self.assertEqual(pads.pad_id(Device()), "045e:0b12:usb-0000:00:14.0-2/input0")

    def test_saving_keeps_the_other_sections_and_reads_back(self):
        self.config.write_text("[input]\nhome_shortcut = guide\n\n[cec]\nenabled = true\n")
        saved = pads.Settings((MAC_B, MAC_A), frozenset({MAC_A}), frozenset({MAC_B}))
        pads.save(saved, self.config)
        pads.save(saved, self.config)  # a second save replaces the section, never adds another
        text = self.config.read_text()
        self.assertEqual(text.count("[controllers]"), 1)
        self.assertIn("home_shortcut = guide", text)
        self.assertIn("[cec]\nenabled = true", text)
        self.assertEqual(pads.load(self.config), saved)

    def test_nothing_saved_or_unreadable_gives_no_order_and_no_swaps(self):
        self.assertEqual(pads.load(self.config), pads.Settings())
        self.config.write_text("[controllers\norder = x")
        self.assertEqual(pads.load(self.config), pads.Settings())

    def test_the_swap_table(self):
        settings = pads.Settings((), frozenset({MAC_A}), frozenset({MAC_A, MAC_B}))
        self.assertEqual(settings.swaps(MAC_A), {
            Codes.BTN_SOUTH: Codes.BTN_EAST, Codes.BTN_EAST: Codes.BTN_SOUTH,
            Codes.BTN_NORTH: Codes.BTN_WEST, Codes.BTN_WEST: Codes.BTN_NORTH,
        })
        self.assertEqual(settings.swaps(MAC_B), {Codes.BTN_NORTH: Codes.BTN_WEST, Codes.BTN_WEST: Codes.BTN_NORTH})
        self.assertEqual(settings.swaps("045e:0b12:usb-1"), {})
        self.assertEqual(settings.swaps(""), {})
        toggled = pads.toggle(settings, MAC_A, "swap_ab")
        self.assertNotIn(MAC_A, toggled.swap_ab)
        self.assertIn(MAC_A, pads.toggle(toggled, MAC_A, "swap_ab").swap_ab)


class ListTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = pathlib.Path(self._tmp.name) / "config.ini"

    def listing(self, devices, settings=None, batteries=()):
        return pads.list_pads(opener_for(devices), devices, lambda: list(batteries),
                              settings or pads.load(self.config), pathlib.Path(self._tmp.name) / "none")

    def test_only_real_pads_are_listed_with_their_connection_and_battery(self):
        devices = {
            "/dev/input/event3": Device("DualSense Wireless Controller", MAC_A, bustype=0x05),
            "/dev/input/event4": Device("AT Keyboard", keys=(Codes.KEY_ENTER,)),
            "/dev/input/event5": Device("CouchLiteOS Launcher Navigation"),
            "/dev/input/event6": Device("Xbox Wireless Controller"),
        }
        battery = controllers.Battery("PLAYSTATION", 40, ident=f"ps-controller-battery-{MAC_A}")
        found = self.listing(devices, batteries=[battery])
        self.assertEqual([(p.path, p.bus, p.player) for p in found],
                         [("/dev/input/event3", "BLUETOOTH", 1), ("/dev/input/event6", "USB", 2)])
        self.assertEqual(found[0].battery, battery)
        self.assertIsNone(found[1].battery)
        self.assertEqual(pads.row(found[0]), "PLAYER 1  DUALSENSE  BLUETOOTH  40%")
        self.assertTrue(all(device.closed for device in devices.values()))

    def test_the_order_survives_a_reconnect(self):
        first = {"/dev/input/event3": Device("Pad A", MAC_A), "/dev/input/event4": Device("Pad B", MAC_B)}
        listed = self.listing(first)
        self.assertEqual([p.ident for p in listed], [MAC_A, MAC_B])
        pads.save(pads.move(pads.load(self.config), listed, 1, -1), self.config)  # B: MOVE UP
        self.assertEqual([p.ident for p in self.listing(first)], [MAC_B, MAC_A])
        # B goes away: A is alone, player 1. B comes back on another node, and a new pad joins.
        alone = self.listing({"/dev/input/event3": Device("Pad A", MAC_A)})
        self.assertEqual([(p.ident, p.player) for p in alone], [(MAC_A, 1)])
        again = self.listing({
            "/dev/input/event3": Device("Pad A", MAC_A),
            "/dev/input/event7": Device("New pad", "aa:bb:cc:dd:ee:03"),
            "/dev/input/event9": Device("Pad B", MAC_B),
        })
        self.assertEqual([(p.ident, p.player) for p in again],
                         [(MAC_B, 1), (MAC_A, 2), ("aa:bb:cc:dd:ee:03", 3)])

    def test_moving_with_a_pad_away_keeps_its_place_in_the_saved_order(self):
        settings = pads.Settings((MAC_B, "gone", MAC_A))
        listed = pads.ordered([pads.Pad("/a", "A", MAC_A), pads.Pad("/b", "B", MAC_B)], settings.order)
        moved = pads.move(settings, listed, 0, 1)
        self.assertEqual(moved.order, (MAC_A, MAC_B, "gone"))
        self.assertEqual(pads.move(settings, listed, 0, -1), settings)  # the first cannot go up


class SysfsTest(unittest.TestCase):
    """Two pads' lights in a fake /sys: a DualSense (lights named after its input device) and an
    Xbox pad on xpad (light on the same USB interface as the input device)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        self.sys_input = root / "class/input"
        self.leds = root / "class/leds"
        self.sys_input.mkdir(parents=True)
        self.leds.mkdir(parents=True)
        hid = root / "devices/hid0"
        usb = root / "devices/usb1"
        for parent, node, event in ((hid, "input12", "event3"), (usb, "input20", "event6")):
            (parent / node).mkdir(parents=True)
            (parent / node / "device").symlink_to(parent)
            (self.sys_input / node).symlink_to(parent / node)
            (self.sys_input / event).mkdir()
            (self.sys_input / event / "device").symlink_to(parent / node)
        for number in range(1, 6):
            self.led(f"input12:white:player-{number}", hid)
        self.led("input12:rgb:indicator", hid)
        self.led("xpad0", usb, maximum=15)
        self.led("input99::capslock", root / "devices/keyboard")  # someone else's
        self.dualsense = pads.Pad("/dev/input/event3", "DualSense", MAC_A, "USB", "input12", player=1)
        self.xbox = pads.Pad("/dev/input/event6", "Xbox", "045e:0b12:usb-1", "USB", "input20", player=2)

    def led(self, name, parent, maximum=1, value=0):
        directory = self.leds / name
        directory.mkdir()
        parent.mkdir(parents=True, exist_ok=True)
        (directory / "device").symlink_to(parent)
        (directory / "max_brightness").write_text(f"{maximum}\n")
        (directory / "brightness").write_text(f"{value}\n")

    def brightness(self, name):
        return int((self.leds / name / "brightness").read_text())

    def test_the_input_node_is_found_from_the_event_node(self):
        self.assertEqual(pads.input_node("/dev/input/event3", self.sys_input), "input12")
        self.assertEqual(pads.input_node("/dev/input/event8", self.sys_input), "")

    def test_each_pad_gets_only_its_own_lights(self):
        own = {led.name for led in pads.leds_for(self.dualsense, self.leds, self.sys_input)}
        self.assertEqual(own, {f"input12:white:player-{n}" for n in range(1, 6)} | {"input12:rgb:indicator"})
        self.assertEqual([led.name for led in pads.leds_for(self.xbox, self.leds, self.sys_input)], ["xpad0"])

    def test_player_lights_follow_the_order(self):
        written = pads.set_player_leds([self.dualsense, self.xbox], self.leds, self.sys_input)
        self.assertEqual(written, 6)
        self.assertEqual([self.brightness(f"input12:white:player-{n}") for n in range(1, 6)], [0, 0, 1, 0, 0])
        self.assertEqual(self.brightness("xpad0"), 7)  # quadrant 2
        self.assertEqual(self.brightness("input99::capslock"), 0)
        swapped = pads.ordered([self.xbox, self.dualsense], [self.xbox.ident, self.dualsense.ident])
        pads.set_player_leds(swapped, self.leds, self.sys_input)
        self.assertEqual([self.brightness(f"input12:white:player-{n}") for n in range(1, 6)], [0, 1, 0, 1, 0])
        self.assertEqual(self.brightness("xpad0"), 6)

    def test_identify_rumbles_and_blinks_only_the_chosen_pad(self):
        devices = {"/dev/input/event3": Device(uniq=MAC_A), "/dev/input/event6": Device()}
        pads.set_player_leds([self.dualsense, self.xbox], self.leds, self.sys_input)
        writes = []
        real_write = pads._write
        with mock.patch.object(pads, "_write", side_effect=lambda led, value: writes.append(led.name)
                               or real_write(led, value)):
            text = pads.identify(self.xbox, opener_for(devices), self.leds, self.sys_input,
                                 sleep=lambda _s: None, effect=lambda: "effect")
        self.assertEqual(text, "PLAYER 2: RUMBLE AND LIGHTS")
        self.assertEqual(devices["/dev/input/event3"].commands, [])
        self.assertEqual(devices["/dev/input/event6"].commands, [
            ("upload", "effect"), ("write", pads.EV_FF, 7, 1), ("write", pads.EV_FF, 7, 0), ("erase", 7),
        ])
        self.assertTrue(devices["/dev/input/event6"].closed)
        self.assertEqual(set(writes), {"xpad0"})
        self.assertEqual(self.brightness("xpad0"), 7)  # put back after the blink
        self.assertEqual(self.brightness("input12:white:player-3"), 1)

    def test_a_pad_without_rumble_or_lights_says_so(self):
        devices = {"/dev/input/event8": Device(rumble=False)}
        pad = pads.Pad("/dev/input/event8", "Generic", "0079:0006:usb-2", player=1)
        text = pads.identify(pad, opener_for(devices), self.leds, self.sys_input, sleep=lambda _s: None)
        self.assertEqual(text, "THIS CONTROLLER CANNOT RUMBLE OR FLASH ITS LIGHTS HERE")
        self.assertEqual(devices["/dev/input/event8"].commands, [])


class Screen:
    def __init__(self, keys):
        self.keys = list(keys)
        self.lines = []

    def getmaxyx(self):
        return 30, 100

    def erase(self):
        self.lines = []

    def border(self):
        pass

    def addstr(self, _row, _column, text):
        self.lines.append(text)

    def addnstr(self, _row, _column, text, _limit):
        self.lines.append(text)

    def refresh(self):
        pass

    def timeout(self, _ms):
        pass

    def getch(self):
        if not self.keys:
            raise Stop
        return self.keys.pop(0)


class MenuTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = pathlib.Path(self._tmp.name) / "config.ini"
        self.found = [pads.Pad("/dev/input/event3", "Pad A", MAC_A), pads.Pad("/dev/input/event4", "Pad B", MAC_B)]

    def menu(self, keys, **extra):
        screen = Screen(keys)
        listing = lambda: pads.ordered(self.found, pads.load(self.config).order)  # noqa: E731
        return pads.Menu(screen, pads=listing, config=self.config, root=pathlib.Path(self._tmp.name),
                         sys_input=pathlib.Path(self._tmp.name), **extra), screen

    def test_move_down_and_the_swaps_are_saved(self):
        down, enter = 258, 10
        # Pad A > MOVE DOWN; then SWAP A/B (pad screen keeps its row), then BACK twice.
        menu, screen = self.menu([enter, down, enter, down, down, down, enter, 27, 27])
        menu.run()
        saved = pads.load(self.config)
        self.assertEqual(saved.order, (MAC_B, MAC_A))
        self.assertEqual(saved.swap_ab, frozenset({MAC_A}))

    def test_the_screen_says_games_in_a_stream_use_moonlights_order(self):
        menu, screen = self.menu([27])
        menu.run()
        self.assertIn(pads.ORDER_NOTE, screen.lines)
        self.assertIn("MOONLIGHT", pads.ORDER_NOTE)
        self.assertTrue(all(line == line.upper() for line in screen.lines))

    def test_identify_runs_off_the_screen_thread(self):
        jobs = []
        menu, _screen = self.menu([10, 258, 258, 10, 27, 27], background=jobs.append)
        menu.run()
        self.assertEqual(len(jobs), 1)

    def test_test_shows_live_input_and_leaves_on_a_held_b(self):
        device = Device(events=[
            SimpleNamespace(type=pads.EV_ABS, code=pads.ABS_X, value=32767),
            SimpleNamespace(type=pads.EV_KEY, code=Codes.BTN_TL, value=1),
            SimpleNamespace(type=pads.EV_KEY, code=Codes.BTN_EAST, value=1),
        ])
        times = iter([0.0, 0.5, 1.0, 2.5])
        menu, screen = self.menu([-1] * 4, opener=lambda _path: device, clock=lambda: next(times))
        with mock.patch.object(pads.select, "select", side_effect=lambda r, *_a: (r, [], [])):
            note = menu.test(pads.Pad("/dev/input/event3", "Xbox Wireless Controller", MAC_A, player=1), pads.Settings())
        self.assertEqual(note, "")
        self.assertIn("LEFT STICK    X +100%   Y    0%", [line.strip() for line in screen.lines])
        self.assertIn("BUTTONS       B  LB", [line.strip() for line in screen.lines])
        self.assertTrue(device.closed)

    def test_a_keyboard_esc_leaves_test_but_the_pads_own_esc_does_not(self):
        device = Device(events=[SimpleNamespace(type=pads.EV_KEY, code=Codes.BTN_EAST, value=1)])
        times = iter([10.0, 10.2, 12.0])
        menu, _screen = self.menu([27, -1, 27], opener=lambda _path: device, clock=lambda: next(times))
        with mock.patch.object(pads.select, "select", side_effect=lambda r, *_a: (r, [], [])), \
                mock.patch.object(device, "read", side_effect=[device.events, [SimpleNamespace(
                    type=pads.EV_KEY, code=Codes.BTN_EAST, value=0)], []]):
            note = menu.test(pads.Pad("/dev/input/event3", "Pad", MAC_A, player=1), pads.Settings())
        self.assertEqual(note, "")


class GamepadNavTest(unittest.TestCase):
    """gamepad-nav applies the swaps to the launcher only; a stream gets the pad as it is."""

    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType("evdev")
        fake.InputDevice = object
        fake.UInput = object
        fake.ecodes = Codes
        sys.modules["evdev"] = fake
        path = pathlib.Path(__file__).with_name("gamepad-nav.py")
        spec = importlib.util.spec_from_file_location("gamepad_nav_pads", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_key_for_event_reads_the_swapped_buttons(self):
        swaps = pads.Settings((), frozenset({MAC_A}), frozenset({MAC_A})).swaps(MAC_A)
        press = lambda code: SimpleNamespace(type=Codes.EV_KEY, value=1, code=code)  # noqa: E731
        expected = {Codes.BTN_SOUTH: Codes.KEY_ESC, Codes.BTN_EAST: Codes.KEY_ENTER,
                    Codes.BTN_NORTH: Codes.KEY_DELETE, Codes.BTN_WEST: Codes.KEY_F12, Codes.BTN_TL: Codes.KEY_F5}
        for code, key in expected.items():
            self.assertEqual(self.module.key_for_event(press(code), swaps), key)
        self.assertEqual(self.module.key_for_event(press(Codes.BTN_SOUTH)), Codes.KEY_ENTER)

    def drive(self, app):
        """Run gamepad-nav with one pad (MAC_A, SWAP A/B on) pressing A; (keys sent, grab calls)."""
        module = self.module
        pad = FakePad(Codes.BTN_SOUTH)
        pad.uniq = MAC_A.upper()
        pad.grabs = 0
        pad.grab = lambda: setattr(pad, "grabs", pad.grabs + 1)
        rounds = {"n": 0}
        pressed = []

        def fake_select(devices, *_args):
            rounds["n"] += 1
            if rounds["n"] > 1:
                raise Stop
            return list(devices), [], []

        nav = mock.Mock()
        nav.write.side_effect = lambda _type, code, value: pressed.append(code) if value == 1 else None
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            config = root / "config.ini"
            pads.save(pads.Settings((), frozenset({MAC_A})), config)
            if app:
                (root / "app-active").write_text("moonlight\n")
            with mock.patch.object(module.glob, "glob", return_value=["/dev/input/event3"]), mock.patch.object(
                module, "InputDevice", return_value=pad
            ), mock.patch.object(module, "UInput", return_value=nav), mock.patch.object(
                module.select, "select", side_effect=fake_select
            ), mock.patch.object(module.threading, "Thread"), mock.patch.object(
                module, "CONTROLLER_ID", root / "id", create=True
            ), mock.patch.object(module, "OSK_ACTIVE", root / "osk-active"), mock.patch.object(
                module, "APP_ACTIVE", root / "app-active"
            ), mock.patch.object(module, "LAUNCHER_FOCUS", root / "launcher-focus"), mock.patch.object(
                module, "POINTER_MODE", root / "pointer-mode"
            ), mock.patch.object(module, "_last_state_check", -1e9), mock.patch.object(
                module, "PAD_SETTINGS", pads.Watcher(config)
            ), mock.patch.object(module.padprefs, "set_player_leds") as lights:
                with self.assertRaises(Stop):
                    module.run()
        lights.assert_called_once()
        return pressed, pad.grabs

    def test_the_launcher_gets_the_swapped_button(self):
        pressed, grabs = self.drive(app=False)
        self.assertEqual(pressed, [Codes.KEY_ESC])
        self.assertEqual(grabs, 0)

    def test_a_stream_keeps_the_raw_pad(self):
        # Moonlight reads the pad itself: gamepad-nav neither sends keys nor grabs it.
        pressed, grabs = self.drive(app=True)
        self.assertEqual(pressed, [])
        self.assertEqual(grabs, 0)


if __name__ == "__main__":
    unittest.main()
