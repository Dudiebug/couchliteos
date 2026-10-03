import errno
import importlib.util
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock


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
    BTN_TOUCH = 330
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
    KEY_F1 = 59
    KEY_FN = 464
    # The Home shortcut (Settings > CONTROLS): L3/R3 and Ctrl+Alt+H.
    BTN_THUMBL = 317
    BTN_THUMBR = 318
    KEY_LEFTCTRL = 29
    KEY_RIGHTCTRL = 97
    KEY_LEFTALT = 56
    KEY_RIGHTALT = 100
    KEY_H = 35
    KEY_K = 37
    KEY_LEFTSHIFT = 42
    KEY_RIGHTSHIFT = 54


class Stop(Exception):
    """Ends the otherwise endless run() loop from inside a fake select/sleep."""


class FakePad:
    def __init__(self, *codes, dead=False, gamepad=True):
        self.codes = codes
        self.dead = dead
        self.gamepad = gamepad
        self.info = SimpleNamespace(vendor=0x045E, product=0x028E)
        self.uniq = ""
        self.closed = False

    def capabilities(self):
        return {Codes.EV_KEY: [Codes.BTN_SOUTH if self.gamepad else Codes.KEY_ENTER]}

    def read(self):
        if self.dead:
            raise OSError(19, "No such device")
        return [SimpleNamespace(type=Codes.EV_KEY, value=1, code=code) for code in self.codes]

    def read_loop(self):
        # What a one-controller implementation would use; the test ends it here.
        if self.dead:
            raise Stop
        yield from self.read()
        raise Stop

    def grab(self):
        pass

    def ungrab(self):
        pass

    def close(self):
        self.closed = True


class ScriptedPad(FakePad):
    """A pad whose read() hands out one scripted batch of events per call and reports its axis range."""

    def __init__(self, low=-32768, high=32767):
        super().__init__()
        self.batches = []
        self.range = SimpleNamespace(min=low, max=high)

    def absinfo(self, _code):
        return self.range

    def read(self):
        if self.dead:
            raise OSError(19, "No such device")
        return self.batches.pop(0) if self.batches else []


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

    def test_bluetooth_xbox_guide_button_is_a_home_event_and_its_device_is_watched(self):
        # Xbox pads over Bluetooth report Guide as KEY_HOMEPAGE (172), not BTN_MODE.
        guide = SimpleNamespace(type=Codes.EV_KEY, value=1, code=Codes.KEY_HOMEPAGE)
        self.assertTrue(self.module.is_home_event(guide))

        class Stop(Exception):
            pass

        class Device:
            def capabilities(self):
                return {Codes.EV_KEY: [Codes.KEY_HOMEPAGE]}

            def close(self):
                pass

        device = Device()
        watched = []

        def select(devices, *_args):
            watched.extend(devices)
            raise Stop

        with mock.patch.object(self.module.glob, "glob", return_value=["/dev/input/event9"]), mock.patch.object(
            self.module, "InputDevice", return_value=device
        ), mock.patch.object(self.module.select, "select", side_effect=select):
            with self.assertRaises(Stop):
                self.module.watch_home()
        self.assertEqual(watched, [device])

    def test_launcher_focus_marker_lets_the_controller_navigate_while_an_app_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "app-active").write_text("chrome\n")
            focus = run / "launcher-focus"
            with mock.patch.object(self.module, "APP_ACTIVE", run / "app-active", create=True), mock.patch.object(
                self.module, "LAUNCHER_FOCUS", focus, create=True
            ), mock.patch.object(self.module, "_last_state_check", -1e9):
                # An app has focus: controller input belongs to the app.
                self.assertTrue(self.module.navigation_blocked(False))
                # Home showed the launcher while the app keeps running: navigate it.
                focus.touch()
                self.assertFalse(self.module.navigation_blocked(False))
                # The on-screen keyboard always gets the controller.
                focus.unlink()
                self.assertFalse(self.module.navigation_blocked(True))

    def drive_run(self, listings, ready_rounds):
        """Run gamepad-nav's main loop over fake /dev/input listings.

        listings[i] is the {path: device} set glob() reports during round i; ready_rounds[i]
        lists the paths select() reports readable in round i. Returns (keys pressed on the
        navigation keyboard, the device lists select() was asked to watch, identity text).
        """
        module = self.module
        universe = {}
        for listing in listings:
            universe.update(listing)
        state = {"round": 0, "sleeps": 0}
        pressed, watched = [], []

        def fake_glob(_pattern):
            return list(listings[min(state["round"], len(listings) - 1)])

        def fake_select(devices, *_args):
            watched.append(list(devices))
            if state["round"] >= len(ready_rounds):
                raise Stop
            ready = [universe[path] for path in ready_rounds[state["round"]]]
            state["round"] += 1
            return ready, [], []

        def fake_sleep(_seconds):
            state["sleeps"] += 1
            if state["sleeps"] > 20:
                raise Stop

        nav = mock.Mock()
        nav.write.side_effect = lambda _type, code, value: pressed.append(code) if value == 1 else None
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            identity = root / "launcher-controller.id"
            with mock.patch.object(module.glob, "glob", side_effect=fake_glob), mock.patch.object(
                module, "InputDevice", side_effect=lambda path: universe[path]
            ), mock.patch.object(module, "UInput", return_value=nav), mock.patch.object(
                module.select, "select", side_effect=fake_select
            ), mock.patch.object(module.threading, "Thread"), mock.patch.object(
                module.time, "sleep", side_effect=fake_sleep
            ), mock.patch.object(module, "CONTROLLER_ID", identity, create=True), mock.patch.object(
                module, "OSK_ACTIVE", root / "osk-active"
            ), mock.patch.object(module, "APP_ACTIVE", root / "app-active"), mock.patch.object(
                module, "LAUNCHER_FOCUS", root / "launcher-focus"
            ), mock.patch.object(module, "_last_state_check", -1e9):
                with self.assertRaises(Stop):
                    module.run()
            text = identity.read_text() if identity.exists() else None
        return pressed, watched, text

    def test_every_connected_controller_drives_the_launcher(self):
        first, second = FakePad(Codes.BTN_SOUTH), FakePad(Codes.BTN_EAST)
        keyboard = FakePad(Codes.KEY_ENTER, gamepad=False)
        pressed, watched, identity = self.drive_run(
            [{"/dev/input/event3": first, "/dev/input/event4": second, "/dev/input/event5": keyboard}],
            [["/dev/input/event3", "/dev/input/event4"]],
        )
        self.assertEqual(pressed, [Codes.KEY_ENTER, Codes.KEY_ESC])
        self.assertEqual(watched[0], [first, second], "a keyboard must not be treated as a controller")
        self.assertEqual(identity, "045e:028e:*\n")

    def test_a_controller_plugged_in_later_is_picked_up_without_restarting(self):
        first, second = FakePad(Codes.BTN_SOUTH), FakePad(Codes.BTN_EAST)
        pressed, watched, _identity = self.drive_run(
            [{"/dev/input/event3": first}, {"/dev/input/event3": first, "/dev/input/event4": second}],
            [["/dev/input/event3"], ["/dev/input/event4"]],
        )
        self.assertEqual(pressed, [Codes.KEY_ENTER, Codes.KEY_ESC])
        self.assertEqual(watched[1], [first, second])

    def test_an_unplugged_controller_is_dropped_and_the_others_keep_working(self):
        gone, stays = FakePad(Codes.BTN_SOUTH, dead=True), FakePad(Codes.BTN_EAST)
        pressed, watched, _identity = self.drive_run(
            [{"/dev/input/event3": gone, "/dev/input/event4": stays}, {"/dev/input/event4": stays}],
            [["/dev/input/event3", "/dev/input/event4"], ["/dev/input/event4"]],
        )
        self.assertEqual(pressed, [Codes.KEY_ESC, Codes.KEY_ESC])
        self.assertTrue(gone.closed)
        self.assertEqual(watched[1], [stays])

    def test_a_full_or_read_only_disk_does_not_stop_the_controller_working(self):
        # The identity file is only a hint for USB/IP; failing to save it must not skip the pad.
        pad = FakePad(Codes.BTN_SOUTH)
        with mock.patch.object(pathlib.Path, "write_text", side_effect=OSError(errno.ENOSPC, "No space left")):
            pressed, _watched, _identity = self.drive_run(
                [{"/dev/input/event3": pad}], [["/dev/input/event3"]]
            )
        self.assertEqual(pressed, [Codes.KEY_ENTER])

    def test_the_on_screen_keyboard_grabs_every_controller_and_lets_go_again(self):
        pads = self.module.Pads()
        pads.devices = {"/dev/input/event3": mock.Mock(), "/dev/input/event4": mock.Mock()}
        pads.sync_grab(True)
        pads.sync_grab(True)
        for dev in pads.devices.values():
            dev.grab.assert_called_once_with()
        pads.sync_grab(False)
        for dev in pads.devices.values():
            dev.ungrab.assert_called_once_with()
        self.assertEqual(pads.grabbed, set())

    def event(self, code, value):
        return SimpleNamespace(type=Codes.EV_KEY, value=value, code=code)

    def hold(self):
        return self.module.HomeHold()

    def test_holding_home_for_five_seconds_requests_sleep_exactly_once(self):
        hold = self.hold()
        hold.feed(self.event(Codes.BTN_MODE, 1), 100.0)
        self.assertFalse(hold.due(104.9))
        self.assertTrue(hold.due(105.0))
        self.assertFalse(hold.due(105.5))
        hold.feed(self.event(Codes.BTN_MODE, 0), 106.0)
        self.assertFalse(hold.due(110.0))

    def test_the_homepage_key_counts_as_home_for_the_hold_too(self):
        hold = self.hold()
        hold.feed(self.event(Codes.KEY_HOMEPAGE, 1), 40.0)
        self.assertFalse(hold.due(44.9))
        self.assertTrue(hold.due(45.0))
        hold.feed(self.event(Codes.KEY_HOMEPAGE, 1), 50.0)
        hold.feed(self.event(Codes.KEY_HOMEPAGE, 0), 51.0)
        self.assertFalse(hold.due(60.0))

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
        self.assertFalse(hold.due(54.9))
        self.assertTrue(hold.due(55.0))

    def test_other_buttons_and_a_second_press_are_independent(self):
        hold = self.hold()
        hold.feed(self.event(Codes.BTN_SOUTH, 1), 0.0)
        self.assertFalse(hold.due(10.0))
        hold.feed(self.event(Codes.BTN_MODE, 1), 20.0)
        hold.feed(self.event(Codes.BTN_MODE, 0), 21.0)
        hold.feed(self.event(Codes.BTN_MODE, 1), 30.0)
        self.assertFalse(hold.due(34.0))
        self.assertTrue(hold.due(35.0))

    def test_timeout_shrinks_to_the_remaining_hold_and_reset_clears_it(self):
        hold = self.hold()
        self.assertEqual(hold.timeout(5.0), 1.0)
        hold.feed(self.event(Codes.BTN_MODE, 1), 5.0)
        self.assertAlmostEqual(hold.timeout(6.5), 1.0)
        self.assertAlmostEqual(hold.timeout(9.5), 0.5)
        self.assertEqual(hold.timeout(11.0), 0.0)
        hold.reset()
        self.assertEqual(hold.timeout(9.0), 1.0)
        self.assertFalse(hold.due(99.0))

    def markers(self, directory, *, app=False, focus=False):
        """Point the module at a temporary run directory; app/focus say which markers exist."""
        run = pathlib.Path(directory)
        if app:
            (run / "app-active").touch()
        if focus:
            (run / "launcher-focus").touch()
        for name, marker in (("SLEEP_REQUEST", "suspend"), ("APP_ACTIVE", "app-active"), ("LAUNCHER_FOCUS", "launcher-focus")):
            patcher = mock.patch.object(self.module, name, run / marker, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        return run

    def request_sleep_with(self, can_suspend, *, app=False, focus=False):
        with tempfile.TemporaryDirectory() as directory:
            run = self.markers(directory, app=app, focus=focus)
            with mock.patch.object(self.module.power, "can_suspend", return_value=can_suspend):
                self.module.request_sleep()
            return (run / "suspend").exists()

    def test_request_sleep_touches_the_suspend_request(self):
        self.assertTrue(self.request_sleep_with(True))

    def test_long_press_does_not_request_sleep_on_hardware_that_cannot_suspend(self):
        self.assertFalse(self.request_sleep_with(False))

    # HomeHold is wired into watch_home(), the thread that reads every device with a Home/Guide key.
    # Pads.pump() must not feed a second hold, or one long press would ask for sleep twice and the
    # second request could suspend the PC again the moment it wakes.

    def drive_watch_home(self, rounds):
        """Run watch_home() over one fake Guide-button device.

        rounds[i] = (clock reading after select i, events select i delivers). Returns the mocks
        standing in for request_home() and request_sleep()."""
        module = self.module
        state = {"round": 0, "now": 100.0}

        class Device:
            def capabilities(self):
                return {Codes.EV_KEY: [Codes.BTN_MODE]}

            def read(self):
                return [SimpleNamespace(type=Codes.EV_KEY, value=value, code=Codes.BTN_MODE) for value in events]

            def close(self):
                pass

        device = Device()
        events = ()

        def fake_select(devices, _writable, _errors, _timeout):
            nonlocal events
            if state["round"] >= len(rounds):
                raise Stop
            state["now"], events = rounds[state["round"]]
            state["round"] += 1
            return ([device] if events else []), [], []

        with mock.patch.object(module.glob, "glob", return_value=["/dev/input/event9"]), mock.patch.object(
            module, "InputDevice", return_value=device
        ), mock.patch.object(module.select, "select", side_effect=fake_select), mock.patch.object(
            module.time, "monotonic", side_effect=lambda: state["now"]
        ), mock.patch.object(module, "request_home") as home, mock.patch.object(
            module, "request_sleep"
        ) as sleep:
            with self.assertRaises(Stop):
                module.watch_home()
        return home, sleep

    def test_holding_guide_goes_home_then_asks_for_sleep_once(self):
        home, sleep = self.drive_watch_home([(100.0, (1,)), (101.5, ()), (103.2, ()), (110.0, ())])
        home.assert_called_once_with()
        sleep.assert_called_once_with()

    def test_a_short_guide_press_goes_home_and_never_asks_for_sleep(self):
        home, sleep = self.drive_watch_home([(100.0, (1,)), (100.4, (0,)), (110.0, ())])
        home.assert_called_once_with()
        sleep.assert_not_called()

    def test_the_pad_loop_does_not_run_a_second_hold(self):
        pad = FakePad(Codes.BTN_MODE)
        pads = self.module.Pads()
        pads.devices = {"/dev/input/event3": pad}
        with mock.patch.object(self.module.select, "select", return_value=([pad], [], [])), mock.patch.object(
            self.module.time, "monotonic", return_value=500.0
        ), mock.patch.object(self.module, "request_sleep") as sleep, mock.patch.object(
            self.module, "OSK_ACTIVE", pathlib.Path("/nonexistent/osk")
        ):
            pads.pump(mock.Mock())
            pads.pump(mock.Mock())
        sleep.assert_not_called()

    def feed(self, script, low=-32768, high=32767, app_active=False):
        """Pump a pad through scripted rounds with a fake clock.

        script: [(seconds, [(type, code, value), ...] or [], optional hook), ...].
        Returns ([(seconds, key pressed)], [select timeouts], the Pads object).
        """
        module = self.module
        pad = ScriptedPad(low, high)
        pads = module.Pads()
        pads.devices = {"/dev/input/event3": pad}
        clock = {"now": 0.0}
        pads.clock = lambda: clock["now"]
        pressed, timeouts = [], []
        nav = mock.Mock()
        nav.write.side_effect = lambda _type, code, value: pressed.append((clock["now"], code)) if value == 1 else None

        def fake_select(devices, _writers, _errors, timeout):
            timeouts.append(timeout)
            return (devices if pad.batches or pad.dead else []), [], []

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            if app_active:
                (root / "app-active").write_text("chrome\n")
            with mock.patch.object(module.select, "select", side_effect=fake_select), mock.patch.object(
                module, "OSK_ACTIVE", root / "osk-active"
            ), mock.patch.object(module, "APP_ACTIVE", root / "app-active"), mock.patch.object(
                module, "LAUNCHER_FOCUS", root / "launcher-focus"
            ), mock.patch.object(module, "_last_state_check", -1e9), mock.patch.object(module, "_last_state", False):
                for step in script:
                    when, events = step[0], step[1]
                    clock["now"] = when
                    if len(step) > 2:
                        step[2](pad, root)
                    if events:
                        pad.batches.append([SimpleNamespace(type=t, code=c, value=v) for t, c, v in events])
                    pads.pump(nav)
        return pressed, timeouts, pads

    def test_left_stick_moves_like_the_dpad(self):
        x, y = Codes.ABS_X, Codes.ABS_Y
        script = [
            (0.00, [(Codes.EV_ABS, x, -30000)]),  # pushed left
            (0.05, [(Codes.EV_ABS, x, -31000)]),  # a jittery hold is still one press
            (0.10, [(Codes.EV_ABS, x, 0)]),
            (0.20, [(Codes.EV_ABS, y, 30000)]),  # Y grows downward
            (0.30, [(Codes.EV_ABS, y, 0)]),
            (0.40, [(Codes.EV_ABS, x, 30000)]),
            (0.50, [(Codes.EV_ABS, x, 0)]),
            (0.60, [(Codes.EV_ABS, y, -30000)]),
            (0.70, [(Codes.EV_ABS, y, 0)]),
        ]
        pressed, _timeouts, _pads = self.feed(script)
        self.assertEqual(
            [key for _when, key in pressed], [Codes.KEY_LEFT, Codes.KEY_DOWN, Codes.KEY_RIGHT, Codes.KEY_UP]
        )

    def test_a_resting_or_drifting_stick_does_not_move_the_menu(self):
        x = Codes.ABS_X
        script = [(0.1 * i, [(Codes.EV_ABS, x, value)]) for i, value in enumerate((0, 3000, -8000, 15000, -15000, 500))]
        pressed, _timeouts, _pads = self.feed(script)
        self.assertEqual(pressed, [])

    def test_the_stick_needs_a_firm_push_and_does_not_flutter_at_the_edge(self):
        x = Codes.ABS_X
        script = [
            (0.00, [(Codes.EV_ABS, x, 22000)]),  # 67 percent: pressed
            (0.05, [(Codes.EV_ABS, x, 19000)]),  # 58 percent: still held, not a new press
            (0.10, [(Codes.EV_ABS, x, 22000)]),
            (0.15, [(Codes.EV_ABS, x, 10000)]),  # 31 percent: let go
            (0.20, [(Codes.EV_ABS, x, 15000)]),  # 46 percent: not firm enough to press again
        ]
        pressed, _timeouts, _pads = self.feed(script)
        self.assertEqual([key for _when, key in pressed], [Codes.KEY_RIGHT])

    def test_the_stick_works_on_pads_that_report_zero_to_255(self):
        x = Codes.ABS_X
        script = [
            (0.0, [(Codes.EV_ABS, x, 128)]),  # resting at the middle
            (0.1, [(Codes.EV_ABS, x, 100)]),  # a light touch
            (0.2, [(Codes.EV_ABS, x, 20)]),
            (0.3, [(Codes.EV_ABS, x, 128)]),
            (0.4, [(Codes.EV_ABS, x, 250)]),
        ]
        pressed, _timeouts, _pads = self.feed(script, low=0, high=255)
        self.assertEqual([key for _when, key in pressed], [Codes.KEY_LEFT, Codes.KEY_RIGHT])

    def test_a_diagonal_push_moves_one_way_not_two(self):
        x, y = Codes.ABS_X, Codes.ABS_Y
        pressed, _timeouts, _pads = self.feed([(0.0, [(Codes.EV_ABS, x, 20000), (Codes.EV_ABS, y, 30000)])])
        self.assertEqual([key for _when, key in pressed], [Codes.KEY_DOWN])

    def test_holding_the_dpad_repeats_after_a_short_delay_until_released(self):
        module = self.module
        down = Codes.BTN_DPAD_DOWN
        delay, interval = module.REPEAT_DELAY, module.REPEAT_INTERVAL
        first, second = delay + 0.01, delay + interval + 0.02
        script = [
            (0.0, [(Codes.EV_KEY, down, 1)]),
            (delay / 2, []),  # too soon to repeat
            (first, []),
            (second, []),
            (second + 0.01, [(Codes.EV_KEY, down, 0)]),
            (second + 2, []),
            (second + 4, []),
        ]
        pressed, timeouts, _pads = self.feed(script)
        self.assertEqual(pressed, [(0.0, Codes.KEY_DOWN), (first, Codes.KEY_DOWN), (second, Codes.KEY_DOWN)])
        self.assertEqual(timeouts[0], 1)  # nothing held: the usual idle wait
        self.assertAlmostEqual(timeouts[1], delay / 2)  # wakes exactly when the repeat is due
        self.assertEqual(timeouts[-1], 1, "nothing is held once the button is released")

    def test_holding_the_hat_or_the_left_stick_repeats_too(self):
        delay = self.module.REPEAT_DELAY
        for kind, press, release, key in (
            ("hat", (Codes.EV_ABS, Codes.ABS_HAT0Y, 1), (Codes.EV_ABS, Codes.ABS_HAT0Y, 0), Codes.KEY_DOWN),
            ("stick", (Codes.EV_ABS, Codes.ABS_X, -30000), (Codes.EV_ABS, Codes.ABS_X, 0), Codes.KEY_LEFT),
        ):
            script = [(0.0, [press]), (delay + 0.01, []), (delay + 0.02, [release]), (delay + 3, [])]
            pressed, _timeouts, _pads = self.feed(script)
            self.assertEqual(pressed, [(0.0, key), (delay + 0.01, key)], kind)

    def test_the_newest_held_direction_repeats_and_the_older_resumes_when_it_is_let_go(self):
        delay = self.module.REPEAT_DELAY
        up, right = Codes.BTN_DPAD_UP, Codes.BTN_DPAD_RIGHT
        script = [
            (0.0, [(Codes.EV_KEY, up, 1)]),
            (0.1, [(Codes.EV_KEY, right, 1)]),
            (0.1 + delay + 0.01, []),
            (0.6 + delay, [(Codes.EV_KEY, right, 0)]),
            (0.6 + 2 * delay + 0.02, []),
        ]
        pressed, _timeouts, _pads = self.feed(script)
        self.assertEqual(
            pressed,
            [(0.0, Codes.KEY_UP), (0.1, Codes.KEY_RIGHT), (0.1 + delay + 0.01, Codes.KEY_RIGHT),
             (0.6 + 2 * delay + 0.02, Codes.KEY_UP)],
        )

    def test_buttons_that_are_not_directions_do_not_repeat(self):
        script = [(0.0, [(Codes.EV_KEY, Codes.BTN_SOUTH, 1)]), (1.0, []), (2.0, [])]
        pressed, _timeouts, _pads = self.feed(script)
        self.assertEqual(pressed, [(0.0, Codes.KEY_ENTER)])

    def test_repeating_stops_when_an_app_takes_the_controller(self):
        delay = self.module.REPEAT_DELAY

        def app_starts(_pad, root):
            (root / "app-active").write_text("chrome\n")
            self.module._last_state_check = -1e9  # do not wait out the half-second cache

        script = [
            (0.0, [(Codes.EV_KEY, Codes.BTN_DPAD_DOWN, 1)]),
            (delay + 0.01, [], app_starts),
            (delay + 1, []),
        ]
        pressed, _timeouts, _pads = self.feed(script)
        self.assertEqual(pressed, [(0.0, Codes.KEY_DOWN)], "an app's game must not receive menu arrows")

    def test_an_unplugged_pad_stops_repeating(self):
        delay = self.module.REPEAT_DELAY

        def unplugged(pad, _root):
            pad.dead = True

        script = [(0.0, [(Codes.EV_KEY, Codes.BTN_DPAD_DOWN, 1)]), (delay + 0.01, [], unplugged), (delay + 1, [])]
        pressed, timeouts, pads = self.feed(script)
        self.assertEqual(pressed, [(0.0, Codes.KEY_DOWN)])
        self.assertEqual(pads.devices, {})
        self.assertEqual(timeouts[-1], 1)

    def test_the_sleep_hold_is_at_least_five_seconds(self):
        # 8BitDo pads switch off after about 3 s, Xbox after 6 s, PlayStation after 10 s.
        self.assertGreaterEqual(self.module.SLEEP_HOLD_SECONDS, 5.0)
        self.assertGreaterEqual(self.hold().threshold, 5.0)

    def test_a_long_press_does_not_sleep_the_box_while_an_app_has_the_controller(self):
        self.assertFalse(self.request_sleep_with(True, app=True))

    def test_a_long_press_sleeps_when_the_launcher_has_focus_even_with_an_app_running(self):
        self.assertTrue(self.request_sleep_with(True, app=True, focus=True))

    # HomeHold lives in watch_home(), the thread that reads every device with a Home/Guide key.

    def watch_home_rounds(self, rounds, *, app=False, focus=False, focus_after_home=False):
        """Run watch_home() over one fake Guide button.

        rounds[i] = (clock reading after select i, events select i delivers). The markers are the
        ones present when the run starts; focus_after_home makes request_home() bring the launcher
        to the front the way the real one does. Returns the mocks for request_home/request_sleep."""
        state = {"round": 0, "now": 100.0}
        run = self.markers(tempfile.mkdtemp(), app=app, focus=focus)
        self.addCleanup(shutil.rmtree, run, True)
        events = ()

        class Stop(Exception):
            pass

        class Device:
            def capabilities(self):
                return {Codes.EV_KEY: [Codes.BTN_MODE]}

            def read(self):
                return [SimpleNamespace(type=Codes.EV_KEY, value=value, code=Codes.BTN_MODE) for value in events]

            def close(self):
                pass

        device = Device()

        def fake_select(_devices, _writable, _errors, _timeout):
            nonlocal events
            if state["round"] >= len(rounds):
                raise Stop
            state["now"], events = rounds[state["round"]]
            state["round"] += 1
            return ([device] if events else []), [], []

        def take_focus():
            if focus_after_home:
                (run / "launcher-focus").touch()

        module = self.module
        with mock.patch.object(module.glob, "glob", return_value=["/dev/input/event9"]), mock.patch.object(
            module, "InputDevice", return_value=device
        ), mock.patch.object(module.select, "select", side_effect=fake_select), mock.patch.object(
            module.time, "monotonic", side_effect=lambda: state["now"]
        ), mock.patch.object(module, "request_home", side_effect=take_focus) as home, mock.patch.object(
            module.power, "can_suspend", return_value=True
        ):
            with self.assertRaises(Stop):
                module.watch_home()
        return home, (run / "suspend").exists()

    HOLD = [(100.0, (1,)), (102.0, ()), (104.9, ()), (105.1, ()), (112.0, ())]

    def test_holding_guide_in_the_launcher_asks_for_sleep_after_five_seconds(self):
        home, slept = self.watch_home_rounds(self.HOLD)
        home.assert_called_once_with()
        self.assertTrue(slept)

    def test_holding_guide_for_four_and_a_half_seconds_does_not_sleep(self):
        _home, slept = self.watch_home_rounds([(100.0, (1,)), (103.5, ()), (104.5, (0,)), (112.0, ())])
        self.assertFalse(slept)

    def test_holding_guide_mid_game_to_switch_the_pad_off_does_not_sleep_the_box(self):
        # The Guide press also brings the launcher forward, so by the time the hold is up the
        # launcher has focus: what counts is who had the controller when the press began.
        home, slept = self.watch_home_rounds(self.HOLD, app=True, focus_after_home=True)
        home.assert_called_once_with()
        self.assertFalse(slept)

    def test_holding_guide_while_the_launcher_already_has_focus_sleeps_even_with_an_app_running(self):
        _home, slept = self.watch_home_rounds(self.HOLD, app=True, focus=True)
        self.assertTrue(slept)

    def test_a_later_press_with_the_launcher_in_front_is_not_blocked_by_an_earlier_mid_game_press(self):
        # First press: mid-game, released early (launcher takes focus). Second press: launcher in front.
        rounds = [(100.0, (1,)), (101.0, (0,)), (200.0, (1,)), (206.0, ())]
        _home, slept = self.watch_home_rounds(rounds, app=True, focus_after_home=True)
        self.assertTrue(slept)


class FakeMouse:
    """Stands in for the controller-mouse UInput: records (type, code, value) writes and syns."""

    def __init__(self, fail=False):
        self.events = []
        self.syns = 0
        self.closed = False
        self.fail = fail

    def write(self, kind, code, value):
        if self.fail:
            raise OSError(19, "No such device")
        self.events.append((kind, code, value))

    def syn(self):
        self.syns += 1

    def close(self):
        self.closed = True


def key_event(code, value, kind=Codes.EV_KEY):
    return SimpleNamespace(type=kind, code=code, value=value)


class PointerModeTest(unittest.TestCase):
    """Controller mouse (pointer mode) for apps without controller support."""

    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType("evdev")
        fake.InputDevice = object
        fake.UInput = object
        fake.ecodes = Codes
        sys.modules["evdev"] = fake
        path = pathlib.Path(__file__).with_name("gamepad-nav.py")
        spec = importlib.util.spec_from_file_location("gamepad_nav_pointer", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def run_dir(self, *, app=True, pointer=True, focus=False, osk=False):
        """Point every marker gamepad-nav reads at a temporary directory with the given ones present."""
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        run = pathlib.Path(directory)
        for present, name in ((app, "app-active"), (pointer, "pointer-mode"), (focus, "launcher-focus"),
                              (osk, "osk-active")):
            if present:
                (run / name).touch()
        for attribute, name in (("APP_ACTIVE", "app-active"), ("POINTER_MODE", "pointer-mode"),
                                ("LAUNCHER_FOCUS", "launcher-focus"), ("OSK_ACTIVE", "osk-active"),
                                ("START_OSK", "start-osk"), ("HOME_REQUEST", "home.request")):
            patcher = mock.patch.object(self.module, attribute, run / name)
            patcher.start()
            self.addCleanup(patcher.stop)
        for attribute, value in (("_last_state_check", -1e9), ("_last_state", False)):
            patcher = mock.patch.object(self.module, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return run

    def pads(self, mouse=None):
        pads = self.module.Pads()
        mouse = mouse or FakeMouse()
        pads.pointer = self.module.Pointer(factory=lambda: mouse)
        return pads, mouse

    def drive(self, script, *, run=None, mouse=None, low=-32768, high=32767):
        """Pump one pad through [(seconds, [(type, code, value), ...], optional hook(run, pads))].

        Returns (navigation keys pressed as (seconds, key), select timeouts, pads, mouse, pad)."""
        module = self.module
        run = run or self.run_dir()
        pad = ScriptedPad(low, high)
        pads, mouse = self.pads(mouse)
        pads.devices = {"/dev/input/event3": pad}
        clock = {"now": 0.0}
        pads.clock = lambda: clock["now"]
        pressed, timeouts = [], []
        nav = mock.Mock()
        nav.write.side_effect = lambda _type, code, value: pressed.append((clock["now"], code)) if value == 1 else None
        pad.grab = mock.Mock()
        pad.ungrab = mock.Mock()

        def fake_select(devices, _writers, _errors, timeout):
            timeouts.append(timeout)
            return (devices if pad.batches else []), [], []

        with mock.patch.object(module.select, "select", side_effect=fake_select):
            for step in script:
                clock["now"] = step[0]
                if len(step) > 2:
                    step[2](run, pads)
                    module._last_state_check = -1e9
                if step[1]:
                    pad.batches.append([SimpleNamespace(type=t, code=c, value=v) for t, c, v in step[1]])
                pads.pump(nav)
        return pressed, timeouts, pads, mouse, pad

    @staticmethod
    def moved(mouse, code):
        return sum(value for kind, event_code, value in mouse.events if kind == Codes.EV_REL and event_code == code)

    @staticmethod
    def clicks(mouse):
        return [(code, value) for kind, code, value in mouse.events if kind == Codes.EV_KEY]

    # stick_curve

    def test_the_stick_curve_ignores_the_dead_zone_and_is_quadratic_beyond_it(self):
        curve = self.module.stick_curve
        for value in (0.0, 0.05, -0.1, 0.149, -0.149):
            self.assertEqual(curve(value), 0.0, value)
        self.assertAlmostEqual(curve(1.0), 1.0)
        self.assertAlmostEqual(curve(-1.0), -1.0)
        halfway = self.module.POINTER_DEADZONE + (1 - self.module.POINTER_DEADZONE) / 2
        self.assertAlmostEqual(curve(halfway), 0.25)
        self.assertAlmostEqual(curve(-halfway), -0.25)
        self.assertEqual(curve(1.2), 1.0, "a pad reporting past its range does not speed up")
        self.assertEqual(curve(-1.2), -1.0)
        samples = [curve(step / 100) for step in range(0, 101)]
        self.assertEqual(samples, sorted(samples), "a harder push is never slower")

    # Pointer (the virtual mouse)

    def test_the_mouse_device_exists_only_while_open(self):
        made = []
        pointer = self.module.Pointer(factory=lambda: made.append(FakeMouse()) or made[-1])
        self.assertIsNone(pointer.device)
        pointer.move(5, 5)  # nothing to write to: no error
        self.assertEqual(made, [])
        pointer.open()
        pointer.open()
        self.assertEqual(len(made), 1, "opening twice makes one device")
        pointer.close()
        self.assertTrue(made[0].closed)
        self.assertIsNone(pointer.device)
        pointer.close()  # closing again is harmless

    def test_a_mouse_that_cannot_be_created_is_skipped(self):
        pointer = self.module.Pointer(factory=mock.Mock(side_effect=OSError(13, "Permission denied")))
        pointer.open()
        self.assertIsNone(pointer.device)
        pointer.move(3, 3)
        pointer.scroll(1, 0)
        pointer.close()

    def test_move_and_scroll_send_only_the_axes_that_changed_then_one_sync(self):
        mouse = FakeMouse()
        pointer = self.module.Pointer(factory=lambda: mouse)
        pointer.open()
        pointer.move(7, 0)
        self.assertEqual(mouse.events, [(Codes.EV_REL, Codes.REL_X, 7)])
        self.assertEqual(mouse.syns, 1)
        pointer.move(0, 0)
        pointer.scroll(0, 0)
        self.assertEqual(mouse.syns, 1, "no motion means nothing is written")
        pointer.move(-2, 3)
        pointer.scroll(1, -1)
        self.assertEqual(mouse.events[1:], [
            (Codes.EV_REL, Codes.REL_X, -2), (Codes.EV_REL, Codes.REL_Y, 3),
            (Codes.EV_REL, Codes.REL_WHEEL, 1), (Codes.EV_REL, Codes.REL_HWHEEL, -1),
        ])
        self.assertEqual(mouse.syns, 3)

    def test_buttons_are_sent_once_per_change(self):
        mouse = FakeMouse()
        pointer = self.module.Pointer(factory=lambda: mouse)
        pointer.open()
        pointer.button(Codes.BTN_LEFT, True)
        pointer.button(Codes.BTN_LEFT, True)
        pointer.button(Codes.BTN_LEFT, False)
        pointer.button(Codes.BTN_LEFT, False)
        self.assertEqual(self.clicks(mouse), [(Codes.BTN_LEFT, 1), (Codes.BTN_LEFT, 0)])

    def test_closing_releases_held_buttons_so_nothing_stays_pressed(self):
        mouse = FakeMouse()
        pointer = self.module.Pointer(factory=lambda: mouse)
        pointer.open()
        pointer.button(Codes.BTN_RIGHT, True)
        pointer.button(Codes.BTN_LEFT, True)
        pointer.close()
        self.assertEqual(self.clicks(mouse)[2:], [(Codes.BTN_LEFT, 0), (Codes.BTN_RIGHT, 0)])
        self.assertTrue(mouse.closed)
        self.assertEqual(pointer.held, set())

    def test_a_click_held_while_the_mouse_could_not_be_made_is_not_stuck_later(self):
        # The first open fails (uinput busy); A is pressed and the mode turns off and on again.
        mouse = FakeMouse()
        factory = mock.Mock(side_effect=[OSError(16, "Device or resource busy"), mouse])
        pointer = self.module.Pointer(factory=factory)
        pointer.open()
        pointer.button(Codes.BTN_LEFT, True)
        pointer.close()
        pointer.open()
        pointer.button(Codes.BTN_LEFT, True)
        self.assertEqual(self.clicks(mouse), [(Codes.BTN_LEFT, 1)], "the next press must click")

    def test_a_vanished_mouse_does_not_raise(self):
        mouse = FakeMouse(fail=True)
        pointer = self.module.Pointer(factory=lambda: mouse)
        pointer.open()
        pointer.move(1, 1)
        pointer.button(Codes.BTN_LEFT, True)
        mouse.close = mock.Mock(side_effect=OSError(19, "No such device"))
        pointer.close()
        self.assertIsNone(pointer.device)

    # pointer_mode() and open_keyboard()

    def test_pointer_mode_needs_the_flag_and_an_app_holding_the_pad(self):
        run = self.run_dir()
        self.assertTrue(self.module.pointer_mode(False))
        self.assertFalse(self.module.pointer_mode(True), "the on-screen keyboard takes the pad")
        (run / "launcher-focus").touch()
        self.assertFalse(self.module.pointer_mode(False), "the launcher in front is driven by arrows")
        (run / "launcher-focus").unlink()
        (run / "pointer-mode").unlink()
        self.assertFalse(self.module.pointer_mode(False), "a game keeps its controller")
        (run / "pointer-mode").touch()
        (run / "app-active").unlink()
        self.assertFalse(self.module.pointer_mode(False), "no app: the flag is stale")

    def test_open_keyboard_asks_for_the_keyboard_once(self):
        run = self.run_dir()
        self.module.open_keyboard()
        self.assertTrue((run / "start-osk").exists())
        (run / "start-osk").unlink()
        (run / "osk-active").touch()
        self.module.open_keyboard()
        self.assertFalse((run / "start-osk").exists(), "one keyboard at a time")

    def test_open_keyboard_survives_a_missing_run_directory(self):
        with mock.patch.object(self.module, "START_OSK", pathlib.Path("/nonexistent/couchliteos/start-osk")), \
                mock.patch.object(self.module, "OSK_ACTIVE", pathlib.Path("/nonexistent/couchliteos/osk-active")):
            self.module.open_keyboard()

    # Pads in pointer mode

    def test_the_left_stick_moves_the_pointer_at_full_speed_when_pushed_all_the_way(self):
        speed = self.module.POINTER_SPEED
        script = [(0.0, [(Codes.EV_ABS, Codes.ABS_X, 32767), (Codes.EV_ABS, Codes.ABS_Y, -32768)]), (0.05, [])]
        pressed, _timeouts, _pads, mouse, _pad = self.drive(script)
        self.assertEqual(pressed, [], "the stick does not also send arrows")
        self.assertEqual(self.moved(mouse, Codes.REL_X), int(speed * 0.05))
        self.assertEqual(self.moved(mouse, Codes.REL_Y), -int(speed * 0.05))

    def test_a_long_gap_between_ticks_does_not_jump_the_pointer(self):
        script = [(0.0, [(Codes.EV_ABS, Codes.ABS_X, 32767)]), (3.0, [])]
        _pressed, _timeouts, _pads, mouse, _pad = self.drive(script)
        self.assertEqual(self.moved(mouse, Codes.REL_X), int(self.module.POINTER_SPEED * 0.05))

    def test_a_gentle_push_adds_up_fractions_of_a_pixel(self):
        # About 1 percent of full speed: 14 px/s, 0.23 px per 1/60 s tick.
        value = int(32767 * (self.module.POINTER_DEADZONE + 0.1 * (1 - self.module.POINTER_DEADZONE)))
        ticks = [(i / 60, []) for i in range(1, 61)]
        _pressed, _timeouts, _pads, mouse, _pad = self.drive([(0.0, [(Codes.EV_ABS, Codes.ABS_X, value)]), *ticks])
        self.assertIn(self.moved(mouse, Codes.REL_X), range(12, 16), "about 14 pixels in one second")

    def test_a_resting_stick_inside_the_dead_zone_does_not_drift(self):
        script = [(0.0, [(Codes.EV_ABS, Codes.ABS_X, 3000), (Codes.EV_ABS, Codes.ABS_RY, -4000)]), (0.05, []), (0.1, [])]
        _pressed, timeouts, _pads, mouse, _pad = self.drive(script)
        self.assertEqual(mouse.events, [])
        self.assertEqual(timeouts[-1], 1.0, "an idle pointer does not wake 60 times a second")

    def test_the_right_stick_scrolls_and_pushing_it_up_scrolls_up(self):
        script = [(0.0, [(Codes.EV_ABS, Codes.ABS_RY, -32768)])] + [(0.05 * i, []) for i in range(1, 11)]
        _pressed, _timeouts, _pads, mouse, _pad = self.drive(script)
        expected = int(self.module.SCROLL_SPEED * 0.5)
        self.assertIn(self.moved(mouse, Codes.REL_WHEEL), (expected - 1, expected))
        self.assertGreater(self.moved(mouse, Codes.REL_WHEEL), 0)
        self.assertEqual(self.moved(mouse, Codes.REL_X), 0, "scrolling does not move the pointer")
        script = [(0.0, [(Codes.EV_ABS, Codes.ABS_RX, 32767)])] + [(0.05 * i, []) for i in range(1, 11)]
        _pressed, _timeouts, _pads, mouse, _pad = self.drive(script)
        self.assertGreater(self.moved(mouse, Codes.REL_HWHEEL), 0)

    def test_letting_go_of_the_stick_stops_at_once_and_forgets_the_leftover_fraction(self):
        value = int(32767 * 0.5)
        script = [(0.0, [(Codes.EV_ABS, Codes.ABS_X, value)]), (0.01, []), (0.02, [(Codes.EV_ABS, Codes.ABS_X, 0)]),
                  (0.05, []), (0.1, [])]
        _pressed, timeouts, pads, mouse, _pad = self.drive(script)
        self.assertEqual(pads.carry, [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(timeouts[-1], 1.0)

    def test_select_wakes_every_tick_while_the_pointer_moves(self):
        script = [(0.0, [(Codes.EV_ABS, Codes.ABS_X, 32767)]), (0.02, []), (0.04, [])]
        _pressed, timeouts, _pads, _mouse, _pad = self.drive(script)
        self.assertEqual(timeouts[0], 1.0, "nothing known yet")
        self.assertEqual(timeouts[1:], [self.module.POINTER_TICK] * 2)

    def test_a_clicks_left_and_holding_it_drags(self):
        script = [
            (0.0, [(Codes.EV_KEY, Codes.BTN_SOUTH, 1)]),
            (0.1, [(Codes.EV_ABS, Codes.ABS_X, 32767)]),
            (0.15, []),
            (0.2, [(Codes.EV_KEY, Codes.BTN_SOUTH, 2)]),  # autorepeat is not another click
            (0.3, [(Codes.EV_KEY, Codes.BTN_SOUTH, 0)]),
        ]
        pressed, _timeouts, _pads, mouse, _pad = self.drive(script)
        self.assertEqual(pressed, [], "A does not also press Enter")
        self.assertEqual(self.clicks(mouse), [(Codes.BTN_LEFT, 1), (Codes.BTN_LEFT, 0)])
        down = mouse.events.index((Codes.EV_KEY, Codes.BTN_LEFT, 1))
        up = mouse.events.index((Codes.EV_KEY, Codes.BTN_LEFT, 0))
        self.assertTrue(any(event[0] == Codes.EV_REL for event in mouse.events[down:up]), "moved while held")

    def test_the_west_button_right_clicks(self):
        script = [(0.0, [(Codes.EV_KEY, Codes.BTN_WEST, 1)]), (0.1, [(Codes.EV_KEY, Codes.BTN_WEST, 0)])]
        pressed, _timeouts, _pads, mouse, _pad = self.drive(script)
        self.assertEqual(pressed, [], "no Delete for the app behind")
        self.assertEqual(self.clicks(mouse), [(Codes.BTN_RIGHT, 1), (Codes.BTN_RIGHT, 0)])

    def test_the_north_button_opens_the_keyboard_to_type_into_the_page(self):
        run = self.run_dir()
        script = [(0.0, [(Codes.EV_KEY, Codes.BTN_NORTH, 1)]), (0.1, [(Codes.EV_KEY, Codes.BTN_NORTH, 0)])]
        pressed, _timeouts, _pads, mouse, _pad = self.drive(script, run=run)
        self.assertTrue((run / "start-osk").exists())
        self.assertEqual(pressed, [], "no F12 reaches the app")
        self.assertEqual(mouse.events, [])

    def test_the_other_buttons_are_browser_keys(self):
        # START and SELECT act when let go: holding both is the default Home shortcut (see HomeShortcutTest).
        settings = FixedSettings(self.module.inputprefs.Settings())
        for button, key, when in ((Codes.BTN_EAST, Codes.KEY_BACK, 0.0), (Codes.BTN_START, Codes.KEY_SPACE, 0.1),
                                  (Codes.BTN_SELECT, Codes.KEY_ESC, 0.1), (Codes.BTN_TL, Codes.KEY_PAGEUP, 0.0),
                                  (Codes.BTN_TR, Codes.KEY_PAGEDOWN, 0.0)):
            script = [(0.0, [(Codes.EV_KEY, button, 1)]), (0.1, [(Codes.EV_KEY, button, 0)]), (2.0, [])]
            with mock.patch.object(self.module, "INPUT_SETTINGS", settings):
                pressed, _timeouts, _pads, mouse, _pad = self.drive(script)
            self.assertEqual(pressed, [(when, key)], button)
            self.assertEqual(mouse.events, [], button)

    def test_the_navigation_keyboard_can_send_the_browser_keys(self):
        for key in (Codes.KEY_BACK, Codes.KEY_SPACE, Codes.KEY_PAGEUP, Codes.KEY_PAGEDOWN, Codes.KEY_ESC):
            self.assertIn(key, self.module.KEYS)

    def test_the_dpad_still_sends_arrows_that_repeat(self):
        delay = self.module.REPEAT_DELAY
        script = [
            (0.0, [(Codes.EV_KEY, Codes.BTN_DPAD_DOWN, 1)]),
            (delay + 0.01, []),
            (delay + 0.02, [(Codes.EV_KEY, Codes.BTN_DPAD_DOWN, 0)]),
            (delay + 2, []),
            (delay + 3, [(Codes.EV_ABS, Codes.ABS_HAT0X, -1)]),
            (delay + 3.1, [(Codes.EV_ABS, Codes.ABS_HAT0X, 0)]),
        ]
        pressed, _timeouts, _pads, mouse, _pad = self.drive(script)
        self.assertEqual(pressed, [(0.0, Codes.KEY_DOWN), (delay + 0.01, Codes.KEY_DOWN), (delay + 3, Codes.KEY_LEFT)])
        self.assertEqual(mouse.events, [])

    def test_pointer_mode_grabs_the_pad_so_the_app_does_not_also_see_it(self):
        def leave(run, _pads):
            (run / "pointer-mode").unlink()

        script = [(0.0, [(Codes.EV_KEY, Codes.BTN_SOUTH, 1)]), (0.1, []), (0.2, [], leave)]
        _pressed, _timeouts, pads, mouse, pad = self.drive(script)
        pad.grab.assert_called_once_with()
        pad.ungrab.assert_called_once_with()
        self.assertEqual(pads.grabbed, set())
        # The app went: the mouse is gone and A is not left held down.
        self.assertTrue(mouse.closed)
        self.assertEqual(self.clicks(mouse), [(Codes.BTN_LEFT, 1), (Codes.BTN_LEFT, 0)])
        self.assertIsNone(pads.pointer.device)
        self.assertFalse(pads.pointer_on)

    def test_a_game_without_pointer_mode_gets_the_pad_untouched(self):
        run = self.run_dir(pointer=False)
        script = [(0.0, [(Codes.EV_KEY, Codes.BTN_SOUTH, 1), (Codes.EV_ABS, Codes.ABS_X, 32767)]), (0.05, [])]
        pressed, _timeouts, pads, mouse, pad = self.drive(script, run=run)
        self.assertEqual(pressed, [])
        self.assertEqual(mouse.events, [])
        self.assertIsNone(pads.pointer.device, "no mouse device: Cage would show a cursor")
        pad.grab.assert_not_called()

    def test_the_launcher_in_front_is_driven_with_arrows_even_over_a_browser(self):
        run = self.run_dir(focus=True)
        script = [(0.0, [(Codes.EV_KEY, Codes.BTN_SOUTH, 1)]), (0.05, [])]
        pressed, _timeouts, pads, mouse, pad = self.drive(script, run=run)
        self.assertEqual(pressed, [(0.0, Codes.KEY_ENTER)])
        self.assertEqual(mouse.events, [])
        self.assertIsNone(pads.pointer.device)
        pad.grab.assert_not_called()

    def test_the_on_screen_keyboard_takes_the_pad_from_the_mouse(self):
        def keyboard_opens(run, _pads):
            (run / "osk-active").touch()

        script = [(0.0, [(Codes.EV_KEY, Codes.BTN_SOUTH, 1)]), (0.1, [(Codes.EV_KEY, Codes.BTN_SOUTH, 0)]),
                  (0.2, [(Codes.EV_KEY, Codes.BTN_SOUTH, 1)], keyboard_opens)]
        pressed, _timeouts, pads, mouse, pad = self.drive(script)
        self.assertEqual(pressed, [(0.2, Codes.KEY_ENTER)], "A types on the keyboard")
        self.assertTrue(mouse.closed)
        self.assertEqual(pads.grabbed, {"/dev/input/event3"}, "still grabbed, now for the keyboard")
        pad.ungrab.assert_not_called()

    def test_switching_modes_forgets_held_arrows_and_leftover_motion(self):
        pads, _mouse = self.pads()
        pads.hold("/dev/input/event3").press(Codes.BTN_DPAD_DOWN, Codes.KEY_DOWN, 0.0)
        pads.carry = [0.5, 0.5, 0.5, 0.5]
        pads.set_pointer(True, 3.0)
        self.assertEqual(pads.holds, {})
        self.assertEqual(pads.carry, [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(pads.pointer_tick, 3.0)
        pads.carry = [0.5, 0.0, 0.0, 0.0]
        pads.set_pointer(True, 4.0)  # no change: nothing reset
        self.assertEqual(pads.carry, [0.5, 0.0, 0.0, 0.0])
        self.assertEqual(pads.pointer_tick, 3.0)

    def test_the_strongest_push_across_pads_wins(self):
        pads, _mouse = self.pads()
        pads.sticks = {"a": {Codes.ABS_X: [0.3, 0]}, "b": {Codes.ABS_X: [-0.8, 0]}, "c": {Codes.ABS_Y: [0.9, 0]}}
        self.assertEqual(pads.stick_value(Codes.ABS_X), -0.8)
        self.assertEqual(pads.stick_value(Codes.ABS_Y), 0.9)
        self.assertEqual(pads.stick_value(Codes.ABS_RX), 0.0)
        self.assertTrue(pads.pointer_moving())
        pads.sticks = {"a": {Codes.ABS_X: [0.1, 0]}}
        self.assertFalse(pads.pointer_moving())

    def test_record_axis_needs_a_range_and_scales_both_kinds_of_pad(self):
        pads, _mouse = self.pads()
        wide, narrow, broken = ScriptedPad(), ScriptedPad(0, 255), ScriptedPad(0, 0)
        self.assertTrue(pads.record_axis("w", wide, key_event(Codes.ABS_RX, -32768, Codes.EV_ABS)))
        self.assertTrue(pads.record_axis("n", narrow, key_event(Codes.ABS_RX, 255, Codes.EV_ABS)))
        self.assertFalse(pads.record_axis("b", broken, key_event(Codes.ABS_RX, 10, Codes.EV_ABS)))
        self.assertAlmostEqual(pads.sticks["w"][Codes.ABS_RX][0], -1.0)
        self.assertAlmostEqual(pads.sticks["n"][Codes.ABS_RX][0], 1.0)
        self.assertNotIn("b", pads.sticks)

    def test_pointer_mode_with_a_pad_reporting_zero_to_255(self):
        script = [(0.0, [(Codes.EV_ABS, Codes.ABS_X, 0)]), (0.05, [])]
        _pressed, _timeouts, _pads, mouse, _pad = self.drive(script, low=0, high=255)
        self.assertEqual(self.moved(mouse, Codes.REL_X), -int(self.module.POINTER_SPEED * 0.05))

    # watch_home

    def test_guide_still_goes_home_while_the_mouse_has_grabbed_the_pad(self):
        # The grab hides the pad from watch_home(), so the pointer path must ask for home itself.
        run = self.run_dir()
        with mock.patch.object(self.module.subprocess, "run") as run_command:
            _pressed, _timeouts, pads, _mouse, pad = self.drive(
                [(0.0, [(Codes.EV_KEY, Codes.BTN_MODE, 1)]), (0.1, [(Codes.EV_KEY, Codes.BTN_MODE, 0)])], run=run)
        self.assertTrue(pads.pointer_on)
        pad.grab.assert_called()
        self.assertTrue((run / "home.request").exists())
        self.assertEqual(run_command.call_count, 1, "one press, one request")
        self.assertIn("title:CouchLiteOS Launcher", run_command.call_args[0][0])

    def test_without_a_mouse_device_the_pad_stays_a_pad(self):
        run = self.run_dir()
        failing = mock.Mock(side_effect=OSError(16, "Device or resource busy"))
        pads = self.module.Pads()
        pads.pointer = self.module.Pointer(factory=failing)
        pad = ScriptedPad(-32768, 32767)
        pad.grab, pad.ungrab = mock.Mock(), mock.Mock()
        pads.devices = {"/dev/input/event3": pad}
        pads.clock = lambda: 0.0
        with mock.patch.object(self.module.select, "select", return_value=([], [], [])):
            pads.pump(mock.Mock())
            pad.grab.assert_not_called()  # the app still gets its controller
            self.assertEqual(failing.call_count, 1)
            pads.pump(mock.Mock())
            self.assertEqual(failing.call_count, 2, "creating the mouse is tried again")
            failing.side_effect = None
            failing.return_value = FakeMouse()
            pads.pump(mock.Mock())
        self.assertIsNotNone(pads.pointer.device)
        pad.grab.assert_called()

    def test_the_mouse_goes_away_with_the_last_pad(self):
        pads, mouse = self.pads()
        pads.devices = {"/dev/input/event3": mock.Mock()}
        pads.clock = lambda: 0.0
        pads.set_pointer(True, 0.0)
        pads.pointer.button(Codes.BTN_LEFT, True)  # A held when the pad switched itself off
        pads.drop("/dev/input/event3")
        self.assertFalse(pads.pointer_on)
        self.assertTrue(mouse.closed)
        self.assertEqual(self.clicks(mouse)[-1], (Codes.BTN_LEFT, 0), "the held click is let go")

    def test_leaving_mouse_mode_forgets_the_sticks(self):
        pads, _mouse = self.pads()
        pads.sticks = {"/dev/input/event3": {Codes.ABS_RY: [-0.9, 0]}}
        pads.set_pointer(True, 0.0)
        self.assertEqual(pads.sticks, {})
        pads.sticks = {"/dev/input/event3": {Codes.ABS_RY: [-0.9, 0]}}
        pads.set_pointer(False, 1.0)
        self.assertEqual(pads.sticks, {})

    def watch_keyboards(self, rounds, capabilities=(Codes.KEY_LEFTMETA, Codes.KEY_ENTER), name="Keyboard"):
        """watch_home() over one fake keyboard: rounds[i] lists (code, value) events select i delivers.

        Returns (devices select() watched first, request_home mock)."""
        module = self.module
        state = {"round": 0}
        events = []

        class Keyboard:
            def capabilities(self):
                return {Codes.EV_KEY: list(capabilities)}

            def read(self):
                return [key_event(code, value) for code, value in events]

            def close(self):
                pass

        keyboard = Keyboard()
        keyboard.name = name
        watched = []

        def fake_select(devices, _writable, _errors, _timeout):
            nonlocal events
            watched.append(list(devices))
            if state["round"] >= len(rounds):
                raise Stop
            events = rounds[state["round"]]
            state["round"] += 1
            return ([keyboard] if events and devices else []), [], []

        with mock.patch.object(module.glob, "glob", return_value=["/dev/input/event7"]), mock.patch.object(
            module, "InputDevice", return_value=keyboard
        ), mock.patch.object(module.select, "select", side_effect=fake_select), mock.patch.object(
            module, "request_home"
        ) as home, mock.patch.object(module, "request_sleep") as sleep:
            with self.assertRaises(Stop):
                module.watch_home()
        sleep.assert_not_called()
        return watched[0], home, keyboard

    CHORD_KEYS = (Codes.KEY_LEFTCTRL, Codes.KEY_LEFTALT, Codes.KEY_H, Codes.KEY_LEFTMETA, Codes.KEY_ENTER)

    def test_a_lone_super_tap_no_longer_goes_home(self):
        watched, home, keyboard = self.watch_keyboards(
            [[(Codes.KEY_LEFTMETA, 1)], [(Codes.KEY_LEFTMETA, 0)]], capabilities=self.CHORD_KEYS)
        self.assertEqual(watched, [keyboard], "a keyboard with the Home key's keys is watched")
        home.assert_not_called()

    def test_a_keyboard_with_only_a_super_key_is_not_watched_any_more(self):
        watched, home, _keyboard = self.watch_keyboards(
            [[(Codes.KEY_LEFTMETA, 1)], [(Codes.KEY_LEFTMETA, 0)]])
        self.assertEqual(watched, [])
        home.assert_not_called()

    def test_super_used_as_a_shortcut_on_a_keyboard_does_not_go_home(self):
        _watched, home, _keyboard = self.watch_keyboards(
            [[(Codes.KEY_LEFTMETA, 1), (Codes.KEY_ENTER, 1)], [(Codes.KEY_ENTER, 0), (Codes.KEY_LEFTMETA, 0)]],
            capabilities=self.CHORD_KEYS)
        home.assert_not_called()

    def test_a_device_without_home_guide_or_the_home_keys_is_not_watched(self):
        watched, home, _keyboard = self.watch_keyboards([[]], capabilities=(Codes.KEY_ENTER,))
        self.assertEqual(watched, [])
        home.assert_not_called()


class FixedSettings:
    """Stands in for gamepad-nav's INPUT_SETTINGS watcher."""

    def __init__(self, settings):
        self.settings = settings

    def current(self):
        return self.settings


class HomeShortcutTest(unittest.TestCase):
    """Settings > CONTROLS: SELECT+START or L3+R3 held, Ctrl+Alt+H, and the controller mouse speed."""

    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType("evdev")
        fake.InputDevice = object
        fake.UInput = object
        fake.ecodes = Codes
        sys.modules["evdev"] = fake
        path = pathlib.Path(__file__).with_name("gamepad-nav.py")
        spec = importlib.util.spec_from_file_location("gamepad_nav_home", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    # The pointer-mode helpers are shared, not inherited (that would run those tests twice).
    run_dir = PointerModeTest.run_dir
    pads = PointerModeTest.pads
    drive = PointerModeTest.drive
    moved = staticmethod(PointerModeTest.moved)

    def use(self, **choices):
        """Make gamepad-nav see these Settings > CONTROLS choices."""
        settings = self.module.inputprefs.Settings(**choices)
        patcher = mock.patch.object(self.module, "INPUT_SETTINGS", FixedSettings(settings))
        patcher.start()
        self.addCleanup(patcher.stop)

    def combo(self, home="select-start"):
        return self.module.HomeCombo(choice=lambda: home)

    @staticmethod
    def hold(combo, *codes, now=0.0, pad="pad", value=1):
        for code in codes:
            combo.feed(pad, key_event(code, value), now)

    # HomeCombo

    def test_the_hold_is_one_and_a_half_seconds(self):
        self.assertEqual(self.module.inputprefs.HOME_HOLD_SECONDS, 1.5)
        self.assertEqual(self.combo().threshold, 1.5)

    def test_holding_select_and_start_opens_home_once_after_the_hold(self):
        combo = self.combo()
        self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, now=10.0)
        self.assertFalse(combo.due(11.4))
        self.assertAlmostEqual(combo.timeout(11.4), 0.1)
        self.assertTrue(combo.due(11.5))
        self.assertFalse(combo.due(20.0), "one hold, one Home")
        self.assertEqual(combo.timeout(20.0), 1.0)

    def test_a_tap_does_nothing(self):
        combo = self.combo()
        self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, now=0.0)
        self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, now=0.2, value=0)
        self.assertFalse(combo.due(5.0))

    def test_letting_go_of_either_button_early_cancels(self):
        for released in (Codes.BTN_SELECT, Codes.BTN_START):
            with self.subTest(released=released):
                combo = self.combo()
                self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, now=0.0)
                self.hold(combo, released, now=1.0, value=0)
                self.assertFalse(combo.due(1.6))
                self.hold(combo, released, now=2.0)  # pressed again: a new hold starts then
                self.assertFalse(combo.due(3.4))
                self.assertTrue(combo.due(3.5))

    def test_one_button_alone_or_held_repeats_do_not_count(self):
        combo = self.combo()
        self.hold(combo, Codes.BTN_START, now=0.0)
        self.hold(combo, Codes.BTN_SELECT, now=0.5, value=2)  # autorepeat is not a press
        self.assertFalse(combo.due(9.0))

    def test_moonlights_quit_combination_never_opens_home(self):
        # Select+Start+L1+R1 quits a Moonlight stream; however it is pressed, Home stays out of it.
        orders = (
            (Codes.BTN_TL, Codes.BTN_TR, Codes.BTN_SELECT, Codes.BTN_START),
            (Codes.BTN_SELECT, Codes.BTN_START, Codes.BTN_TL, Codes.BTN_TR),
            (Codes.BTN_SELECT, Codes.BTN_TL, Codes.BTN_START, Codes.BTN_TR),
        )
        for order in orders:
            with self.subTest(order=order):
                combo = self.combo()
                for offset, code in enumerate(order):
                    self.hold(combo, code, now=offset * 0.2)
                self.assertFalse(combo.due(5.0))

    def test_a_shoulder_let_go_mid_hold_does_not_restart_it(self):
        combo = self.combo()
        self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, Codes.BTN_TL, now=0.0)
        self.hold(combo, Codes.BTN_TL, now=0.5, value=0)
        self.assertFalse(combo.due(5.0), "the pair must be pressed afresh")
        self.hold(combo, Codes.BTN_START, now=6.0, value=0)
        self.hold(combo, Codes.BTN_START, now=6.1)
        self.assertTrue(combo.due(7.7))

    def test_l3_and_r3_when_chosen_and_then_select_start_does_nothing(self):
        combo = self.combo("l3-r3")
        self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, now=0.0)
        self.assertFalse(combo.due(5.0))
        self.hold(combo, Codes.BTN_THUMBL, Codes.BTN_THUMBR, now=10.0)
        self.assertFalse(combo.due(11.0))
        self.assertTrue(combo.due(11.5))

    def test_select_start_when_chosen_and_then_l3_r3_does_nothing(self):
        combo = self.combo("select-start")
        self.hold(combo, Codes.BTN_THUMBL, Codes.BTN_THUMBR, now=0.0)
        self.assertFalse(combo.due(5.0))

    def test_guide_only_turns_both_holds_off(self):
        combo = self.combo("guide")
        self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, Codes.BTN_THUMBL, Codes.BTN_THUMBR, now=0.0)
        self.assertFalse(combo.due(5.0))
        self.assertEqual(combo.timeout(0.0), 1.0)

    def test_choosing_guide_only_during_a_hold_cancels_it(self):
        choice = {"home": "select-start"}
        combo = self.module.HomeCombo(choice=lambda: choice["home"])
        self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, now=0.0)
        choice["home"] = "guide"
        self.assertFalse(combo.due(2.0))

    def test_holds_are_tracked_per_pad(self):
        combo = self.combo()
        self.hold(combo, Codes.BTN_SELECT, now=0.0, pad="one")
        self.hold(combo, Codes.BTN_START, now=0.0, pad="two")
        self.assertFalse(combo.due(5.0))
        combo.forget("one")
        self.assertNotIn("one", combo.held)

    def test_the_real_key_state_replaces_buttons_let_go_unseen(self):
        # SELECT was released while the pad was grabbed (no event reached watch_home): a lone START must not count.
        combo = self.combo()
        self.hold(combo, Codes.BTN_SELECT, now=0.0)
        combo.feed("pad", key_event(Codes.BTN_START, 1), 10.0, pressed=[Codes.BTN_START])
        self.assertFalse(combo.due(20.0))
        combo.feed("pad", key_event(Codes.BTN_SELECT, 1), 30.0, pressed=[Codes.BTN_START, Codes.BTN_SELECT])
        self.assertTrue(combo.due(31.5))

    def test_the_real_key_state_is_checked_again_before_the_hold_counts(self):
        # The pad was grabbed mid-hold (controller mouse): the release never arrived, nor will another event.
        down = {"pad": [Codes.BTN_SELECT, Codes.BTN_START]}
        combo = self.combo()
        self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, now=0.0)
        down["pad"] = [Codes.BTN_START]
        self.assertFalse(combo.due(1.5, pressed=down.get))
        self.assertEqual(combo.started, {})
        self.assertFalse(combo.due(5.0, pressed=down.get))
        self.hold(combo, Codes.BTN_SELECT, now=6.0)
        down["pad"] = [Codes.BTN_START, Codes.BTN_SELECT, Codes.BTN_TL]
        self.assertFalse(combo.due(7.5, pressed=down.get), "a shoulder went down unseen: Moonlight's quit")
        self.hold(combo, Codes.BTN_TL, Codes.BTN_START, now=8.0, value=0)
        self.hold(combo, Codes.BTN_START, now=8.1)
        self.assertTrue(combo.due(9.7, pressed=lambda _path: None), "no answer: the events seen decide")

    def test_the_hold_still_counts_when_the_real_state_agrees(self):
        combo = self.combo()
        self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, now=0.0)
        self.assertTrue(combo.due(1.5, pressed=lambda _path: [Codes.BTN_SELECT, Codes.BTN_START, Codes.BTN_SOUTH]))

    def test_two_pads_holding_at_once_open_home_once(self):
        for second in (0.0, 0.2):
            with self.subTest(second=second):
                combo = self.combo()
                self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, now=0.0, pad="one")
                self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, now=second, pad="two")
                self.assertTrue(combo.due(1.5))
                self.assertFalse(combo.due(1.5), "the same pass: the menu would open and shut again")
                self.assertFalse(combo.due(1.8))
                self.assertFalse(combo.due(10.0))
                self.hold(combo, Codes.BTN_START, now=11.0, pad="two", value=0)
                self.hold(combo, Codes.BTN_START, now=11.1, pad="two")
                self.assertTrue(combo.due(12.7), "a fresh hold opens it again")

    def test_the_default_choice_is_read_live_from_the_settings(self):
        self.use(home="guide")
        combo = self.module.HomeCombo()
        self.hold(combo, Codes.BTN_SELECT, Codes.BTN_START, now=0.0)
        self.assertFalse(combo.due(2.0))
        self.use(home="l3-r3")
        self.hold(combo, Codes.BTN_THUMBL, Codes.BTN_THUMBR, now=3.0)
        self.assertTrue(combo.due(4.5))

    # HomeChord

    CTRL_ALT_H = "KEY_LEFTCTRL+KEY_LEFTALT+KEY_H"
    SUPER_H = "KEY_LEFTMETA+KEY_H"

    def chord(self, text=CTRL_ALT_H):
        return self.module.HomeChord(chord=lambda: text)

    def press(self, chord, *codes, path="kbd"):
        """Press the codes in turn; the result of the last press."""
        result = False
        for code in codes:
            result = chord.feed(path, key_event(code, 1))
        return result

    def test_ctrl_alt_h_opens_home_with_either_ctrl_and_either_alt(self):
        for ctrl in (Codes.KEY_LEFTCTRL, Codes.KEY_RIGHTCTRL):
            for alt in (Codes.KEY_LEFTALT, Codes.KEY_RIGHTALT):
                with self.subTest(ctrl=ctrl, alt=alt):
                    chord = self.chord()
                    self.assertFalse(chord.feed("kbd", key_event(ctrl, 1)))
                    self.assertFalse(chord.feed("kbd", key_event(alt, 1)))
                    self.assertTrue(chord.feed("kbd", key_event(Codes.KEY_H, 1)))
                    self.assertFalse(chord.feed("kbd", key_event(Codes.KEY_H, 2)), "a held H opens it once")
                    self.assertFalse(chord.feed("kbd", key_event(Codes.KEY_H, 0)))
                    self.assertTrue(chord.feed("kbd", key_event(Codes.KEY_H, 1)), "and again for the next press")

    def test_the_key_alone_or_with_only_some_of_the_modifiers_does_not(self):
        chord = self.chord()
        self.assertFalse(chord.feed("kbd", key_event(Codes.KEY_H, 1)))
        chord.feed("kbd", key_event(Codes.KEY_LEFTCTRL, 1))
        self.assertFalse(chord.feed("kbd", key_event(Codes.KEY_H, 1)))
        chord.feed("kbd", key_event(Codes.KEY_LEFTCTRL, 0))
        chord.feed("kbd", key_event(Codes.KEY_LEFTALT, 1))
        self.assertFalse(chord.feed("kbd", key_event(Codes.KEY_H, 1)))

    def test_an_extra_modifier_is_a_different_shortcut(self):
        chord = self.chord()
        self.assertFalse(self.press(chord, Codes.KEY_LEFTCTRL, Codes.KEY_LEFTALT, Codes.KEY_LEFTSHIFT, Codes.KEY_H))
        chord = self.chord(self.SUPER_H)
        self.assertFalse(self.press(chord, Codes.KEY_LEFTMETA, Codes.KEY_LEFTSHIFT, Codes.KEY_H))

    def test_another_key_does_not_open_home_and_events_that_are_not_keys_are_ignored(self):
        chord = self.chord()
        self.press(chord, Codes.KEY_LEFTCTRL, Codes.KEY_LEFTALT)
        self.assertFalse(chord.feed("kbd", key_event(Codes.KEY_K, 1)))
        self.assertFalse(chord.feed("kbd", key_event(Codes.KEY_H, 1, Codes.EV_REL)))

    def test_super_h_when_it_is_the_chosen_key_and_ctrl_alt_h_then_is_not(self):
        chord = self.chord(self.SUPER_H)
        for code in (Codes.KEY_LEFTMETA, Codes.KEY_RIGHTMETA):
            self.assertTrue(self.press(chord, code, Codes.KEY_H))
            chord.feed("kbd", key_event(Codes.KEY_H, 0))
            chord.feed("kbd", key_event(code, 0))
        self.assertFalse(self.press(chord, Codes.KEY_LEFTCTRL, Codes.KEY_LEFTALT, Codes.KEY_H))

    def test_a_lone_super_tap_is_nothing(self):
        chord = self.chord(self.SUPER_H)
        self.assertFalse(chord.feed("kbd", key_event(Codes.KEY_LEFTMETA, 1)))
        self.assertFalse(chord.feed("kbd", key_event(Codes.KEY_LEFTMETA, 0)))
        self.assertFalse(self.chord().feed("kbd", key_event(Codes.KEY_LEFTMETA, 0)))

    def test_the_chord_counts_only_on_one_keyboard(self):
        chord = self.chord()
        chord.feed("one", key_event(Codes.KEY_LEFTCTRL, 1))
        chord.feed("two", key_event(Codes.KEY_LEFTALT, 1))
        self.assertFalse(chord.feed("two", key_event(Codes.KEY_H, 1)))
        chord.feed("two", key_event(Codes.KEY_LEFTCTRL, 1))
        self.assertTrue(chord.feed("two", key_event(Codes.KEY_H, 1)))
        chord.forget("two")
        self.assertFalse(chord.feed("two", key_event(Codes.KEY_H, 1)), "a keyboard unplugged forgets its keys")

    def test_a_release_without_a_press_and_reset_forget_the_modifiers(self):
        chord = self.chord()
        chord.feed("kbd", key_event(Codes.KEY_LEFTCTRL, 0))
        self.press(chord, Codes.KEY_LEFTCTRL, Codes.KEY_LEFTALT)
        chord.reset()
        self.assertFalse(chord.feed("kbd", key_event(Codes.KEY_H, 1)))

    def test_off_and_a_chord_of_unknown_keys_never_open_home(self):
        for text in ("", "KEY_LEFTCTRL+KEY_NOSUCHKEY", "junk"):
            with self.subTest(text=text):
                self.assertFalse(self.press(self.chord(text), Codes.KEY_LEFTCTRL, Codes.KEY_LEFTALT, Codes.KEY_H))

    def test_the_chord_is_read_live_from_the_settings(self):
        live = self.module.HomeChord()
        self.use(keyboard_home="")
        self.assertFalse(self.press(live, Codes.KEY_LEFTCTRL, Codes.KEY_LEFTALT, Codes.KEY_H))
        self.use(keyboard_home=self.CTRL_ALT_H)
        self.assertTrue(live.feed("kbd", key_event(Codes.KEY_H, 1)))
        self.use(keyboard_home=self.SUPER_H)
        self.assertFalse(live.feed("kbd", key_event(Codes.KEY_H, 1)), "Ctrl and Alt are still held: not Super+H")

    # watch_home: the same Home request as Guide, from devices it only watches (never grabs)

    def watch(self, rounds, capabilities, *, app=True, name="Device", front=None, focus=False):
        """watch_home() over one device; rounds[i] = (clock after select i, [(code, value), ...]),
        optionally with a third item: buttons let go unseen (another fd grabbed the pad), or an
        OSError for select i to raise.

        An app owns the pad by default: the shortcut must work over it, as Guide does.
        Returns (request_home mock, select timeouts, devices watched)."""
        module = self.module
        run = self.run_dir(app=app, pointer=False, focus=focus)
        if front is not None:
            (run / "app-active").write_text(front + "\n")  # which app is in front, as couchliteos-run-app writes it
        state = {"round": 0, "now": 0.0}
        events = []
        down = set()
        timeouts = []

        class Device:
            def capabilities(self):
                return {Codes.EV_KEY: list(capabilities)}

            def read(self):
                for code, value in events:
                    down.add(code) if value else down.discard(code)
                return [key_event(code, value) for code, value in events]

            def active_keys(self):
                return sorted(down)

            def close(self):
                pass

            def grab(self):
                raise AssertionError("watch_home must never grab")

        device = Device()
        device.name = name
        watched = []

        def fake_select(devices, _writable, _errors, timeout):
            nonlocal events
            watched.append(list(devices))
            timeouts.append(timeout)
            if state["round"] >= len(rounds):
                raise Stop
            step = rounds[state["round"]]
            state["round"] += 1
            if isinstance(step, OSError):
                raise step
            state["now"], events = step[:2]
            down.difference_update(*step[2:])
            return ([device] if events and devices else []), [], []

        with mock.patch.object(module.glob, "glob", return_value=["/dev/input/event5"]), mock.patch.object(
            module, "InputDevice", return_value=device
        ), mock.patch.object(module.select, "select", side_effect=fake_select), mock.patch.object(
            module.time, "monotonic", side_effect=lambda: state["now"]
        ), mock.patch.object(module, "request_home") as home, mock.patch.object(module, "request_sleep"):
            with self.assertRaises(Stop):
                module.watch_home()
        self.assertEqual((run / "app-active").exists(), app or front is not None)
        return home, timeouts, watched[0]

    PAD = (Codes.BTN_SOUTH, Codes.BTN_SELECT, Codes.BTN_START, Codes.BTN_TL, Codes.BTN_TR,
           Codes.BTN_THUMBL, Codes.BTN_THUMBR)  # no Guide button at all
    KEYBOARD = (Codes.KEY_LEFTCTRL, Codes.KEY_LEFTALT, Codes.KEY_H, Codes.KEY_ENTER)
    SUPER_KEYBOARD = (Codes.KEY_LEFTMETA, Codes.KEY_H, Codes.KEY_ENTER)  # no Ctrl or Alt keys

    def test_a_pad_without_guide_is_watched_and_the_hold_opens_home_over_a_game(self):
        self.use(home="select-start")
        home, timeouts, watched = self.watch(
            [(0.0, [(Codes.BTN_SELECT, 1), (Codes.BTN_START, 1)]), (1.0, []), (1.5, []), (3.0, [])], self.PAD)
        self.assertEqual(len(watched), 1)
        home.assert_called_once_with()
        self.assertAlmostEqual(timeouts[2], 0.5, msg="select() wakes when the hold is up")

    def test_a_short_press_through_watch_home_does_not_open_home(self):
        self.use(home="select-start")
        home, _timeouts, _watched = self.watch(
            [(0.0, [(Codes.BTN_SELECT, 1), (Codes.BTN_START, 1)]), (0.3, [(Codes.BTN_START, 0)]), (3.0, [])],
            self.PAD)
        home.assert_not_called()

    def test_the_quit_combination_through_watch_home_does_not_open_home(self):
        self.use(home="select-start")
        rounds = [(0.0, [(Codes.BTN_TL, 1), (Codes.BTN_TR, 1), (Codes.BTN_SELECT, 1), (Codes.BTN_START, 1)]),
                  (2.0, []), (4.0, [])]
        home, _timeouts, _watched = self.watch(rounds, self.PAD)
        home.assert_not_called()

    def test_l3_r3_through_watch_home_and_guide_only_ignores_both(self):
        self.use(home="l3-r3")
        home, _timeouts, _watched = self.watch(
            [(0.0, [(Codes.BTN_THUMBL, 1), (Codes.BTN_THUMBR, 1)]), (1.6, [])], self.PAD)
        home.assert_called_once_with()
        self.use(home="guide")
        rounds = [(0.0, [(Codes.BTN_THUMBL, 1), (Codes.BTN_THUMBR, 1), (Codes.BTN_SELECT, 1),
                         (Codes.BTN_START, 1)]), (1.6, []), (5.0, [])]
        home, _timeouts, _watched = self.watch(rounds, self.PAD)
        home.assert_not_called()

    def test_the_home_key_through_watch_home_and_its_off_switch(self):
        rounds = [(0.0, [(Codes.KEY_LEFTCTRL, 1), (Codes.KEY_LEFTALT, 1)]), (0.1, [(Codes.KEY_H, 1)])]
        self.use()
        home, _timeouts, watched = self.watch(rounds, self.KEYBOARD)
        self.assertEqual(len(watched), 1, "a keyboard without Home or Guide is watched for the Home key")
        home.assert_called_once_with()
        self.use(keyboard_home="")
        home, _timeouts, watched = self.watch(rounds, self.KEYBOARD)
        home.assert_not_called()
        self.assertEqual(watched, [], "nothing to watch a keyboard for")

    def test_a_chosen_super_h_goes_through_watch_home(self):
        self.use(keyboard_home=self.SUPER_H)
        rounds = [(0.0, [(Codes.KEY_LEFTMETA, 1)]), (0.1, [(Codes.KEY_H, 1)])]
        home, _timeouts, watched = self.watch(rounds, self.SUPER_KEYBOARD)
        self.assertEqual(len(watched), 1)
        home.assert_called_once_with()
        home, _timeouts, watched = self.watch(rounds, self.KEYBOARD)
        self.assertEqual(watched, [], "this keyboard has no Super key")
        home.assert_not_called()

    def test_our_own_virtual_keyboards_are_not_watched_but_a_vm_keyboard_and_a_virtual_pad_are(self):
        self.use()
        chord_keys = (Codes.KEY_LEFTCTRL, Codes.KEY_LEFTALT, Codes.KEY_H)
        for name, keys, watched_count in (
            ("CouchLiteOS Launcher Navigation", chord_keys, 0),
            ("CouchLiteOS Buffered Keyboard", chord_keys, 0),
            ("QEMU Virtio Keyboard", chord_keys, 1),
            ("CouchLiteOS Virtual Pad", (Codes.BTN_MODE,), 1),
        ):
            with self.subTest(name=name):
                _home, _timeouts, watched = self.watch([], keys, name=name)
                self.assertEqual(len(watched), watched_count)
        self.assertTrue(self.module.is_pad_or_media({Codes.BTN_MODE}))
        self.assertFalse(self.module.is_pad_or_media(set(chord_keys)))

    def test_a_hold_let_go_unseen_does_not_open_home_when_its_time_is_up(self):
        # No event arrives after the release here (the controller mouse grabbed the pad): the timer
        # alone runs out, and the real key state says the pair is no longer held.
        self.use(home="select-start")
        rounds = [(0.0, [(Codes.BTN_SELECT, 1), (Codes.BTN_START, 1)]), (1.0, [], {Codes.BTN_SELECT}), (1.6, []),
                  (3.0, [])]
        home, _timeouts, _watched = self.watch(rounds, self.PAD)
        home.assert_not_called()

    def test_a_read_error_forgets_holds_and_chords_under_way(self):
        self.use(home="select-start")
        rounds = [(0.0, [(Codes.BTN_SELECT, 1), (Codes.BTN_START, 1)]), OSError(19, "No such device"),
                  (1.6, []), (3.0, [])]
        home, _timeouts, _watched = self.watch(rounds, self.PAD)
        home.assert_not_called()
        rounds = [(0.0, [(Codes.KEY_LEFTCTRL, 1), (Codes.KEY_LEFTALT, 1)]), OSError(19, "No such device"),
                  (0.1, [(Codes.KEY_H, 1)])]
        home, _timeouts, _watched = self.watch(rounds, self.KEYBOARD)
        home.assert_not_called()

    def test_the_home_key_opens_home_over_a_stream_too_and_a_super_tap_never_does(self):
        # Deliberate: the keyboard's way home from a stream or remote desktop (Guide is the pad's).
        self.use()
        home, _timeouts, _watched = self.watch(
            [(0.0, [(Codes.KEY_LEFTCTRL, 1), (Codes.KEY_LEFTALT, 1)]), (0.1, [(Codes.KEY_H, 1)]),
             (0.2, [(Codes.KEY_LEFTCTRL, 0), (Codes.KEY_LEFTALT, 0), (Codes.KEY_H, 0)]),
             (0.3, [(Codes.KEY_LEFTMETA, 1), (Codes.KEY_LEFTMETA, 0)])],
            (*self.KEYBOARD, Codes.KEY_LEFTMETA))
        home.assert_called_once_with()  # Ctrl+Alt+H only

    # Guide belongs to the stream (Moonlight, Chiaki): the PC's Steam Big Picture gets it, not Home.

    GUIDE_PAD = (*PAD, Codes.BTN_MODE)
    GUIDE_PRESS = [(0.0, [(Codes.BTN_MODE, 1)]), (0.1, [(Codes.BTN_MODE, 0)])]

    def test_guide_does_not_open_home_while_a_stream_is_in_front(self):
        self.use(home="guide")
        for app in ("moonlight", "chiaki-ng"):
            with self.subTest(app=app):
                home, _timeouts, _watched = self.watch(self.GUIDE_PRESS, self.GUIDE_PAD, front=app)
                home.assert_not_called()

    def test_guide_still_opens_home_over_everything_else(self):
        self.use(home="guide")
        for label, kwargs in (("browser", {"front": "firefox"}), ("no app", {"app": False}),
                              ("launcher in front", {"front": "moonlight", "focus": True}),
                              ("empty marker", {"front": ""})):
            with self.subTest(case=label):
                home, _timeouts, _watched = self.watch(
                    self.GUIDE_PRESS, self.GUIDE_PAD, **({"app": True} | kwargs))
                home.assert_called_once_with()

    def test_the_bluetooth_guide_key_is_the_streams_too(self):
        self.use(home="guide")
        presses = [(0.0, [(Codes.KEY_HOMEPAGE, 1)]), (0.1, [(Codes.KEY_HOMEPAGE, 0)])]
        capabilities = (*self.PAD[1:], Codes.KEY_HOMEPAGE)
        home, _timeouts, _watched = self.watch(presses, capabilities, front="moonlight")
        home.assert_not_called()
        home, _timeouts, _watched = self.watch(presses, capabilities, front="firefox")
        home.assert_called_once_with()

    def test_the_keyboard_home_key_still_goes_home_over_a_stream(self):
        self.use(home="guide")
        press = [(0.0, [(Codes.KEY_HOME, 1)]), (0.1, [(Codes.KEY_HOME, 0)])]
        home, _timeouts, _watched = self.watch(press, (Codes.KEY_HOME, Codes.KEY_ENTER), front="moonlight")
        home.assert_called_once_with()

    def test_the_pad_shortcuts_are_the_way_home_from_a_stream(self):
        hold = [(0.0, [(Codes.BTN_SELECT, 1), (Codes.BTN_START, 1)]), (1.0, []), (1.6, [])]
        # Even with "Guide only" chosen: Guide is the stream's, so the hold must not be dead.
        for choice in ("select-start", "guide"):
            with self.subTest(choice=choice):
                self.use(home=choice)
                home, _timeouts, _watched = self.watch(hold, self.GUIDE_PAD, front="moonlight")
                home.assert_called_once_with()
        self.use(home="l3-r3")
        home, _timeouts, _watched = self.watch(
            [(0.0, [(Codes.BTN_THUMBL, 1), (Codes.BTN_THUMBR, 1)]), (1.6, [])], self.GUIDE_PAD, front="moonlight")
        home.assert_called_once_with()

    def test_guide_only_keeps_its_meaning_outside_a_stream(self):
        # Over a browser the hold is still not a Home shortcut when the choice is Guide only.
        self.use(home="guide")
        hold = [(0.0, [(Codes.BTN_SELECT, 1), (Codes.BTN_START, 1)]), (1.6, []), (3.0, [])]
        home, _timeouts, _watched = self.watch(hold, self.GUIDE_PAD, front="firefox")
        home.assert_not_called()

    def test_stream_owns_pad_reads_the_marker_and_the_launcher_focus(self):
        self.assertEqual(self.module.STREAM_APPS, {"moonlight", "chiaki-ng"})
        for text, focus, expected in (("moonlight\n", False, True), ("chiaki-ng", False, True),
                                      ("firefox\n", False, False), ("", False, False),
                                      ("moonlight\n", True, False)):
            with self.subTest(text=text, focus=focus):
                run = self.run_dir(app=True, pointer=False, focus=focus)
                (run / "app-active").write_text(text)
                self.assertEqual(self.module.stream_owns_pad(), expected)
        self.run_dir(app=False, pointer=False)
        self.assertFalse(self.module.stream_owns_pad(), "no marker, no stream")

    def test_the_effective_home_choice_only_changes_guide_only_in_a_stream(self):
        choice = self.module.effective_home_choice
        self.run_dir(app=True, pointer=False)
        for app, expected in (("moonlight", "select-start"), ("firefox", "guide")):
            (self.module.APP_ACTIVE).write_text(app + "\n")
            with self.subTest(app=app):
                self.assertEqual(choice("guide"), expected)
                self.assertEqual(choice("l3-r3"), "l3-r3")
                self.assertEqual(choice("select-start"), "select-start")

    def test_select_and_start_taps_wait_for_release_in_a_stream_with_guide_only(self):
        # The hold is the way home there, so a tap must not fire F7/F8 (or Esc/Space) on press.
        self.use(home="guide")
        taps = self.module.PairTaps()
        run = self.run_dir(app=True, pointer=False)
        (run / "app-active").write_text("moonlight\n")
        self.assertTrue(taps.deferred())
        (run / "app-active").write_text("firefox\n")
        self.assertFalse(taps.deferred())

    def test_a_touchpad_left_without_a_pad_is_let_go(self):
        # run() with no pad but a grabbed touchpad: it is released before the loop sleeps.
        seen = []

        class OnePass(Exception):
            pass

        def sleep(_seconds):
            raise OnePass

        class StubPads:
            devices = {}

            def rescan(self):
                pass

            def sync_touchpads(self, streaming):
                seen.append(streaming)

        with mock.patch.object(self.module, "Pads", StubPads), mock.patch.object(
            self.module, "UInput"
        ), mock.patch.object(self.module.threading, "Thread"), mock.patch.object(
            self.module.time, "sleep", side_effect=sleep
        ):
            with self.assertRaises(OnePass):
                self.module.run()
        self.assertEqual(seen, [False])

    def test_rescanning_does_not_reopen_a_known_touchpad(self):
        touchpad = self.touchpad()
        opened = []
        devices = {"/dev/input/event2": touchpad}
        pads = self.module.Pads()
        with mock.patch.object(self.module.glob, "glob", return_value=sorted(devices)), mock.patch.object(
            self.module, "InputDevice", side_effect=lambda path: opened.append(path) or devices[path]
        ), mock.patch.object(self.module, "save_identity"):
            pads.rescan()
            pads.rescan()
        self.assertEqual(opened, ["/dev/input/event2"], "one open, not one per rescan")

    def test_a_replugged_touchpad_is_grabbed_again_for_the_next_stream(self):
        first, second = self.touchpad(), self.touchpad()
        pads = self.rescanned({"/dev/input/event2": first})
        pads.sync_touchpads(True)
        pads.drop_touchpad("/dev/input/event2")  # unplugged mid-stream
        first.close.assert_called_once_with()
        self.assertEqual(pads.touchpads, {})
        devices = {"/dev/input/event2": second}  # the same path comes back after a replug
        with mock.patch.object(self.module.glob, "glob", return_value=sorted(devices)), mock.patch.object(
            self.module, "InputDevice", side_effect=lambda path: devices[path]
        ), mock.patch.object(self.module, "save_identity"):
            pads.rescan()
        pads.sync_touchpads(True)
        second.grab.assert_called_once_with()

    def test_guide_with_the_controller_mouse_on_still_goes_home(self):
        # The mouse grabs the pad, so the stream cannot see Guide anyway: it must not be lost.
        self.use()
        run = self.run_dir()
        (run / "app-active").write_text("moonlight\n")
        with mock.patch.object(self.module.subprocess, "run"):
            self.drive([(0.0, [(Codes.EV_KEY, Codes.BTN_MODE, 1)]), (0.1, [(Codes.EV_KEY, Codes.BTN_MODE, 0)])],
                       run=run)
        self.assertTrue((run / "home.request").exists())

    # The PlayStation touchpad is the stream's: grabbed so Cage never turns it into a pointer.

    @staticmethod
    def touchpad(*, vendor=0x054C, name="DualSense Wireless Controller Touchpad", keys=None):
        keys = [Codes.BTN_TOUCH] if keys is None else keys
        pad = mock.Mock()
        pad.name = name
        pad.info = SimpleNamespace(vendor=vendor, product=0x0CE6)
        pad.capabilities.return_value = {Codes.EV_KEY: list(keys)}
        return pad

    def rescanned(self, devices):
        """Pads after rescan() sees these {path: device} nodes."""
        pads = self.module.Pads()
        with mock.patch.object(self.module.glob, "glob", return_value=sorted(devices)), mock.patch.object(
            self.module, "InputDevice", side_effect=lambda path: devices[path]
        ), mock.patch.object(self.module, "save_identity"):
            pads.rescan()
        return pads

    def test_which_nodes_count_as_a_playstation_touchpad(self):
        is_touchpad = self.module.is_playstation_touchpad
        self.assertTrue(is_touchpad(self.touchpad()))
        self.assertTrue(is_touchpad(self.touchpad(name="Wireless Controller Touchpad")))
        self.assertFalse(is_touchpad(self.touchpad(vendor=0x06CB, name="Synaptics TouchPad")), "a laptop's")
        self.assertFalse(is_touchpad(self.touchpad(name="DualSense Wireless Controller")), "not the touchpad node")
        self.assertFalse(is_touchpad(self.touchpad(name="DualSense Wireless Controller Motion Sensors")))
        self.assertFalse(is_touchpad(self.touchpad(keys=[])), "no BTN_TOUCH")
        self.assertFalse(is_touchpad(self.touchpad(keys=[Codes.BTN_TOUCH, Codes.BTN_SOUTH])), "a pad, not a touchpad")
        broken = self.touchpad()
        broken.capabilities.side_effect = OSError(19, "No such device")
        self.assertFalse(is_touchpad(broken))

    def test_rescan_keeps_a_playstation_touchpad_apart_from_the_pads(self):
        pad = FakePad()
        touchpad = self.touchpad()
        laptop = self.touchpad(vendor=0x06CB, name="Synaptics TouchPad")
        pads = self.rescanned({"/dev/input/event1": pad, "/dev/input/event2": touchpad,
                               "/dev/input/event3": laptop})
        self.assertEqual(list(pads.devices), ["/dev/input/event1"])
        self.assertEqual(list(pads.touchpads), ["/dev/input/event2"])
        self.assertIn("/dev/input/event3", pads.ignored)
        touchpad.grab.assert_not_called()

    def test_the_touchpad_is_grabbed_for_a_stream_and_let_go_afterwards(self):
        touchpad = self.touchpad()
        pads = self.rescanned({"/dev/input/event2": touchpad})
        pads.sync_touchpads(False)
        touchpad.grab.assert_not_called()
        pads.sync_touchpads(True)
        pads.sync_touchpads(True)
        touchpad.grab.assert_called_once_with()
        pads.sync_touchpads(False)
        pads.sync_touchpads(False)
        touchpad.ungrab.assert_called_once_with()
        pads.sync_touchpads(True)
        self.assertEqual(touchpad.grab.call_count, 2, "a second stream grabs it again")

    def test_a_busy_touchpad_is_retried_and_one_that_will_not_let_go_is_reopened(self):
        touchpad = self.touchpad()
        touchpad.grab.side_effect = [OSError(16, "Device or resource busy"), None]
        pads = self.rescanned({"/dev/input/event2": touchpad})
        pads.sync_touchpads(True)
        self.assertEqual(pads.touch_grabbed, set())
        pads.sync_touchpads(True)
        self.assertEqual(pads.touch_grabbed, {"/dev/input/event2"})
        touchpad.ungrab.side_effect = OSError(19, "No such device")
        pads.sync_touchpads(False)
        self.assertEqual(pads.touchpads, {})
        touchpad.close.assert_called_once_with()

    def test_pump_grabs_the_touchpad_only_while_a_stream_is_in_front(self):
        self.use()
        touchpad = self.touchpad()
        run = self.run_dir(app=True, pointer=False)
        states = {"moonlight\n": True, "firefox\n": False}
        for text, grabbed in states.items():
            (run / "app-active").write_text(text)
            pads, _mouse = self.pads()
            pads.touchpads = {"/dev/input/event2": touchpad}
            touchpad.reset_mock()
            with mock.patch.object(self.module.select, "select", return_value=([], [], [])), mock.patch.object(
                pads, "rescan"
            ):
                pads.pump(mock.Mock())
            self.assertEqual(touchpad.grab.called, grabbed, text)
        (run / "app-active").write_text("moonlight\n")
        pads, _mouse = self.pads()
        pads.touchpads = {"/dev/input/event2": touchpad}
        with mock.patch.object(self.module.select, "select", return_value=([], [], [])), mock.patch.object(
            pads, "rescan"
        ):
            pads.pump(mock.Mock())
            (run / "launcher-focus").touch()  # Home came forward: the touchpad is a touchpad again
            pads.pump(mock.Mock())
        touchpad.ungrab.assert_called_once_with()

    def test_an_unplugged_touchpad_is_forgotten_and_drop_all_closes_it(self):
        touchpad = self.touchpad()
        pads = self.rescanned({"/dev/input/event2": touchpad})
        with mock.patch.object(self.module.glob, "glob", return_value=[]):
            pads.rescan()
        self.assertEqual(pads.touchpads, {})
        touchpad.close.assert_called_once_with()
        other = self.touchpad()
        pads = self.rescanned({"/dev/input/event2": other})
        pads.drop_all()
        self.assertEqual(pads.touchpads, {})
        other.close.assert_called_once_with()

    def test_devices_watched_for_the_shortcuts(self):
        watches = self.module.watches_home
        self.assertTrue(watches({Codes.BTN_SELECT, Codes.BTN_START}))
        self.assertTrue(watches({Codes.BTN_THUMBL, Codes.BTN_THUMBR}))
        self.assertTrue(watches({Codes.KEY_LEFTCTRL, Codes.KEY_LEFTALT, Codes.KEY_H}, self.CTRL_ALT_H))
        self.assertFalse(watches({Codes.KEY_LEFTCTRL, Codes.KEY_LEFTALT, Codes.KEY_H}, self.SUPER_H))
        self.assertTrue(watches({Codes.KEY_LEFTMETA, Codes.KEY_H}, self.SUPER_H))
        self.assertFalse(watches({Codes.KEY_LEFTMETA}), "a Super key alone is not a reason to watch a keyboard")
        self.assertFalse(watches({Codes.KEY_LEFTCTRL, Codes.KEY_LEFTALT, Codes.KEY_H}, ""))
        self.assertTrue(watches({Codes.BTN_MODE}))
        self.assertFalse(watches({Codes.BTN_SELECT, Codes.KEY_H, Codes.KEY_ENTER}))

    # Pointer mode grabs the pad, so it watches for the hold itself.

    def test_the_hold_opens_home_while_the_mouse_has_grabbed_the_pad(self):
        self.use(home="select-start")
        run = self.run_dir()
        with mock.patch.object(self.module.subprocess, "run") as run_command:
            pressed, timeouts, pads, _mouse, pad = self.drive(
                [(0.0, [(Codes.EV_KEY, Codes.BTN_SELECT, 1), (Codes.EV_KEY, Codes.BTN_START, 1)]),
                 (1.0, []), (1.5, []), (3.0, [(Codes.EV_KEY, Codes.BTN_SELECT, 0), (Codes.EV_KEY, Codes.BTN_START, 0)])],
                run=run)
        self.assertEqual(pressed, [], "no Esc (leaves full screen) or Space (pauses) on the way")
        self.assertTrue(pads.pointer_on)
        pad.grab.assert_called()
        self.assertTrue((run / "home.request").exists())
        self.assertEqual(run_command.call_count, 1, "one hold, one request")
        self.assertAlmostEqual(timeouts[1], 0.5, msg="select() wakes when the hold is up")

    def test_a_tap_or_the_quit_combination_in_mouse_mode_does_not_go_home(self):
        self.use(home="select-start")
        for script in (
            [(0.0, [(Codes.EV_KEY, Codes.BTN_SELECT, 1), (Codes.EV_KEY, Codes.BTN_START, 1)]),
             (0.2, [(Codes.EV_KEY, Codes.BTN_SELECT, 0)]), (3.0, [])],
            [(0.0, [(Codes.EV_KEY, code, 1) for code in (Codes.BTN_TL, Codes.BTN_TR, Codes.BTN_SELECT,
                                                          Codes.BTN_START)]), (3.0, [])],
        ):
            run = self.run_dir()
            with mock.patch.object(self.module.subprocess, "run"):
                pressed, _timeouts, _pads, _mouse, _pad = self.drive(script, run=run)
            self.assertFalse((run / "home.request").exists())
            self.assertNotIn(Codes.KEY_ESC, [key for _when, key in pressed])
            self.assertNotIn(Codes.KEY_SPACE, [key for _when, key in pressed])

    # SELECT and START on their own: their keys wait for the release while they are the Home hold.

    def tap_script(self, *steps):
        return [(when, [(Codes.EV_KEY, code, value) for code, value in events]) for when, events in steps]

    TAPS = (
        # (script, keys expected in the launcher, in pointer mode)
        ([(0.0, [(Codes.BTN_SELECT, 1)]), (0.2, [(Codes.BTN_SELECT, 0)])],
         [(0.2, Codes.KEY_F7)], [(0.2, Codes.KEY_ESC)]),
        ([(0.0, [(Codes.BTN_START, 1)]), (0.2, [(Codes.BTN_START, 0)])],
         [(0.2, Codes.KEY_F8)], [(0.2, Codes.KEY_SPACE)]),
        # SELECT held, START tapped, SELECT let go: neither was a tap.
        ([(0.0, [(Codes.BTN_SELECT, 1)]), (0.2, [(Codes.BTN_START, 1)]), (0.3, [(Codes.BTN_START, 0)]),
          (0.5, [(Codes.BTN_SELECT, 0)])], [], []),
        # The Home hold, let go: no key either side.
        ([(0.0, [(Codes.BTN_START, 1), (Codes.BTN_SELECT, 1)]), (2.0, []),
          (3.0, [(Codes.BTN_SELECT, 0), (Codes.BTN_START, 0)])], [], []),
    )

    def test_select_and_start_act_on_release_and_only_for_a_lone_tap(self):
        self.use(home="select-start")
        for index, (steps, launcher, pointer) in enumerate(self.TAPS):
            for mode, expected in (("launcher", launcher), ("pointer", pointer)):
                with self.subTest(script=index, mode=mode):
                    run = self.run_dir(app=mode == "pointer", pointer=mode == "pointer")
                    with mock.patch.object(self.module.subprocess, "run"):
                        pressed, _timeouts, pads, _mouse, _pad = self.drive(self.tap_script(*steps), run=run)
                    self.assertEqual(pads.pointer_on, mode == "pointer")
                    self.assertEqual(pressed, expected)

    def test_with_another_home_shortcut_select_and_start_act_on_press(self):
        steps = [(0.0, [(Codes.BTN_SELECT, 1)]), (0.2, [(Codes.BTN_START, 1)]),
                 (0.3, [(Codes.BTN_START, 0), (Codes.BTN_SELECT, 0)])]
        for home in ("guide", "l3-r3"):
            self.use(home=home)
            for mode, keys in (("launcher", (Codes.KEY_F7, Codes.KEY_F8)), ("pointer", (Codes.KEY_ESC, Codes.KEY_SPACE))):
                with self.subTest(home=home, mode=mode):
                    run = self.run_dir(app=mode == "pointer", pointer=mode == "pointer")
                    pressed, _timeouts, _pads, _mouse, _pad = self.drive(self.tap_script(*steps), run=run)
                    self.assertEqual(pressed, [(0.0, keys[0]), (0.2, keys[1])])

    def test_pair_taps_are_tracked_per_pad_and_forgotten(self):
        taps = self.module.PairTaps(deferred=lambda: True)
        self.assertIsNone(taps.feed("one", key_event(Codes.BTN_SELECT, 1)))
        self.assertIsNone(taps.feed("two", key_event(Codes.BTN_START, 1)), "another pad's START is no pair")
        self.assertIsNone(taps.feed("two", key_event(Codes.BTN_START, 2)), "autorepeat is ignored")
        self.assertEqual(taps.feed("two", key_event(Codes.BTN_START, 0)), Codes.BTN_START)
        self.assertEqual(taps.feed("one", key_event(Codes.BTN_SELECT, 0)), Codes.BTN_SELECT)
        self.assertIsNone(taps.feed("one", key_event(Codes.BTN_SOUTH, 1)))
        taps.feed("one", key_event(Codes.BTN_SELECT, 1))
        taps.forget("one")
        self.assertIsNone(taps.feed("one", key_event(Codes.BTN_SELECT, 0)), "a pad gone forgets its press")
        taps.feed("two", key_event(Codes.BTN_START, 1))
        taps.reset()
        self.assertIsNone(taps.feed("two", key_event(Codes.BTN_START, 0)))

    def test_a_button_let_go_while_an_app_had_the_pad_is_forgotten(self):
        # Home over an app: SELECT goes down in the launcher, the app takes the pad back before it
        # is let go. That press must neither be sent later nor spoil the next START tap.
        self.use(home="select-start")
        run = self.run_dir(app=True, pointer=False, focus=True)

        def app_has_it(run, _pads):
            (run / "launcher-focus").unlink()

        def launcher_has_it(run, _pads):
            (run / "launcher-focus").touch()

        script = [(0.0, [(Codes.EV_KEY, Codes.BTN_SELECT, 1)]),
                  (0.2, [(Codes.EV_KEY, Codes.BTN_SELECT, 0)], app_has_it),
                  (0.4, [(Codes.EV_KEY, Codes.BTN_START, 1)], launcher_has_it),
                  (0.5, [(Codes.EV_KEY, Codes.BTN_START, 0)])]
        pressed, _timeouts, _pads, _mouse, _pad = self.drive(script, run=run)
        self.assertEqual(pressed, [(0.5, Codes.KEY_F8)])

    def test_leaving_mouse_mode_forgets_a_hold(self):
        pads, _mouse = self.pads()
        pads.set_pointer(True, 0.0)
        pads.combo.feed("/dev/input/event3", key_event(Codes.BTN_SELECT, 1), 0.0)
        pads.set_pointer(False, 0.5)
        self.assertEqual(pads.combo.held, {})

    # Controller mouse speed

    def test_the_speed_steps(self):
        self.assertEqual(self.module.inputprefs.PAD_SPEEDS,
                         {"slow": 0.5, "normal": 1.0, "fast": 1.6, "very-fast": 2.4})
        for speed, factor in self.module.inputprefs.PAD_SPEEDS.items():
            with self.subTest(speed=speed):
                self.use(pad_speed=speed)
                self.assertAlmostEqual(self.module.pointer_speed(), self.module.POINTER_SPEED * factor)

    def test_the_stick_moves_the_pointer_at_the_chosen_speed(self):
        script = [(0.0, [(Codes.EV_ABS, Codes.ABS_X, 32767)]), (0.05, [])]
        for speed, factor in self.module.inputprefs.PAD_SPEEDS.items():
            with self.subTest(speed=speed):
                self.use(pad_speed=speed)
                _pressed, _timeouts, _pads, mouse, _pad = self.drive(script)
                self.assertEqual(self.moved(mouse, Codes.REL_X), int(self.module.POINTER_SPEED * factor * 0.05))

    def test_a_new_speed_takes_effect_without_a_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config = pathlib.Path(directory) / "config.ini"
            clock = {"now": 0.0}
            watcher = self.module.inputprefs.Watcher(config, interval=0.5, clock=lambda: clock["now"])
            with mock.patch.object(self.module, "INPUT_SETTINGS", watcher):
                self.assertEqual(self.module.pointer_speed(), self.module.POINTER_SPEED)
                self.module.inputprefs.save_settings(self.module.inputprefs.Settings(pad_speed="very-fast"), config)
                clock["now"] = 1.0
                self.assertAlmostEqual(self.module.pointer_speed(), self.module.POINTER_SPEED * 2.4)


class MediaKeyTest(unittest.TestCase):
    """A keyboard's brightness, volume and mute keys, read by watch_home() like the Home key chord."""

    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType("evdev")
        fake.InputDevice = object
        fake.UInput = object
        fake.ecodes = Codes
        sys.modules["evdev"] = fake
        path = pathlib.Path(__file__).with_name("gamepad-nav.py")
        spec = importlib.util.spec_from_file_location("gamepad_nav_media", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        """Every brightness and volume change lands in self.changes as (what, step) instead."""
        module = self.module
        self.changes = []
        self.kernel_steps = False
        for target, attribute, value in (
            (module.brightness, "change", lambda step: self.changes.append(("brightness", step))),
            (module.brightness, "kernel_handles_hotkeys", lambda: self.kernel_steps),
            (module.audio, "step_volume", lambda step: self.changes.append(("volume", step))),
            (module.audio, "toggle_mute", lambda: self.changes.append(("mute", None))),
        ):
            patcher = mock.patch.object(target, attribute, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def act(self, code, value=1, name="USB Keyboard", kind=Codes.EV_KEY):
        """Run what one key event does; True when it did something."""
        action = self.module.media_action(key_event(code, value, kind), SimpleNamespace(name=name))
        if action is None:
            return False
        action()
        return True

    def test_brightness_and_volume_keys_step_by_the_guide_menus_steps(self):
        step, volume = self.module.brightness.STEP, self.module.audio.VOLUME_STEP
        for code in (Codes.KEY_BRIGHTNESSUP, Codes.KEY_BRIGHTNESSDOWN, Codes.KEY_VOLUMEUP, Codes.KEY_VOLUMEDOWN,
                     Codes.KEY_MUTE):
            self.assertTrue(self.act(code), code)
        self.assertEqual(self.changes, [("brightness", step), ("brightness", -step), ("volume", volume),
                                        ("volume", -volume), ("mute", None)])
        self.assertEqual((step, volume), (5, 5))

    def test_holding_brightness_or_volume_keeps_stepping_but_mute_toggles_once(self):
        for code in (Codes.KEY_BRIGHTNESSDOWN, Codes.KEY_VOLUMEUP):
            self.assertTrue(self.act(code, 2), code)
        self.assertFalse(self.act(Codes.KEY_MUTE, 2), "a held mute key would flicker")
        self.assertEqual([what for what, _step in self.changes], ["brightness", "volume"])

    def test_releases_and_other_keys_do_nothing(self):
        for code in (Codes.KEY_BRIGHTNESSUP, Codes.KEY_VOLUMEUP, Codes.KEY_MUTE):
            self.assertFalse(self.act(code, 0), code)
        for code in (Codes.KEY_ENTER, Codes.KEY_LEFTMETA, Codes.KEY_HOME, Codes.KEY_F1):
            self.assertFalse(self.act(code), code)
        self.assertFalse(self.act(Codes.KEY_VOLUMEUP, 1, kind=Codes.EV_REL), "not a key at all")
        self.assertEqual(self.changes, [])

    def test_the_laptops_own_keys_are_left_to_the_kernel_when_it_steps_the_backlight(self):
        self.kernel_steps = True
        self.assertFalse(self.act(Codes.KEY_BRIGHTNESSUP, name="Video Bus"))
        self.assertTrue(self.act(Codes.KEY_BRIGHTNESSUP, name="AT Translated Set 2 keyboard"), "a real keyboard")
        self.kernel_steps = False
        self.assertTrue(self.act(Codes.KEY_BRIGHTNESSUP, name="Video Bus"))
        self.assertEqual(len(self.changes), 2)

    def test_the_queue_runs_actions_in_order_and_survives_a_failure(self):
        module = self.module
        ran = []

        def stop():
            raise Stop

        with mock.patch.object(module, "MEDIA_QUEUE", module.queue.Queue(maxsize=4)), \
                mock.patch("builtins.print") as printed:
            module.queue_media(lambda: ran.append(1))
            module.queue_media(mock.Mock(side_effect=RuntimeError("no default sink")))
            module.queue_media(lambda: ran.append(2))
            module.queue_media(stop)
            module.queue_media(lambda: ran.append("dropped: the queue is full"))
            with self.assertRaises(Stop):
                module.apply_media()
        self.assertEqual(ran, [1, 2])
        self.assertIn("no default sink", printed.call_args.args[0])

    def watch_media(self, rounds, *, plugged_at=0, name="USB Keyboard", capabilities=(Codes.KEY_ENTER,),
                    times=None, backlog=None):
        """watch_home() over a keyboard that appears at select round `plugged_at`: rounds[i]
        lists the (code, value) events select i delivers, at times[i] seconds when given.
        Actions run at once, in order, unless `backlog` (a queue nothing drains) is given.

        Returns the devices select() watched each round."""
        module = self.module
        state = {"round": 0}
        clock = mock.patch.object(module.time, "monotonic",
                                  side_effect=lambda: times[max(0, state["round"] - 1)] if times else 0.0)
        if backlog is None:
            queued = mock.patch.object(module, "queue_media", side_effect=lambda action: action())
        else:
            queued = mock.patch.object(module, "MEDIA_QUEUE", backlog)
        events = []
        keyboard = SimpleNamespace(name=name, capabilities=lambda: {Codes.EV_KEY: list(capabilities)},
                                   read=lambda: [key_event(code, value) for code, value in events],
                                   close=lambda: None)
        watched = []

        def fake_select(devices, _writable, _errors, _timeout):
            nonlocal events
            watched.append(list(devices))
            if state["round"] >= len(rounds):
                raise Stop
            events = rounds[state["round"]]
            state["round"] += 1
            return ([keyboard] if events and devices else []), [], []

        def fake_glob(_pattern):
            return ["/dev/input/event9"] if state["round"] >= plugged_at else []

        with mock.patch.object(module.glob, "glob", side_effect=fake_glob), mock.patch.object(
            module, "InputDevice", return_value=keyboard
        ), mock.patch.object(module.select, "select", side_effect=fake_select), mock.patch.object(
            module, "request_home"
        ) as home, mock.patch.object(module, "request_sleep"), queued, clock:
            with self.assertRaises(Stop):
                module.watch_home()
        home.assert_not_called()
        return watched

    def test_a_media_keys_only_device_is_watched(self):
        # Many USB keyboards report these keys from a separate "Consumer Control" device without Super.
        watched = self.watch_media([[(Codes.KEY_VOLUMEUP, 1), (Codes.KEY_VOLUMEUP, 0)], [(Codes.KEY_MUTE, 1)]],
                                   name="USB Keyboard Consumer Control",
                                   capabilities=(Codes.KEY_VOLUMEUP, Codes.KEY_VOLUMEDOWN, Codes.KEY_MUTE))
        self.assertEqual(len(watched[0]), 1)
        self.assertEqual(self.changes, [("volume", 5), ("mute", None)])

    def test_a_keyboard_plugged_in_later_is_picked_up(self):
        watched = self.watch_media([[], [], [(Codes.KEY_BRIGHTNESSUP, 1)], [(Codes.KEY_BRIGHTNESSUP, 2)]],
                                   plugged_at=2, capabilities=(Codes.KEY_BRIGHTNESSUP, Codes.KEY_BRIGHTNESSDOWN),
                                   times=[0.0, 0.0, 0.0, 1.0])
        self.assertEqual([len(devices) for devices in watched], [0, 0, 1, 1, 1])
        self.assertEqual(self.changes, [("brightness", 5), ("brightness", 5)])

    # A held key: the keyboard autorepeats every ~33 ms, far faster than wpctl runs.

    HELD = 0.033

    def held(self, code, seconds):
        """Press `code`, autorepeat it every HELD seconds for `seconds`, let go: (rounds, times)."""
        count = round(seconds / self.HELD)
        rounds = [[(code, 1)], *([[(code, 2)]] * count), [(code, 0)]]
        times = [0.0, *(index * self.HELD for index in range(1, count + 1)), seconds + 0.01]
        return rounds, times

    def test_a_held_key_steps_after_the_repeat_delay_then_at_the_repeat_interval(self):
        module = self.module
        self.assertEqual((module.REPEAT_DELAY, module.REPEAT_INTERVAL), (0.4, 0.12))
        for code, what in ((Codes.KEY_VOLUMEUP, "volume"), (Codes.KEY_BRIGHTNESSDOWN, "brightness")):
            with self.subTest(what=what):
                self.changes.clear()
                rounds, times = self.held(code, 1.2)
                self.watch_media(rounds, times=times, capabilities=(code,))
                # The press, then at 0.43, 0.56, 0.69, 0.83, 0.96 and 1.09 s: not 36 autorepeats' worth.
                self.assertEqual([change[0] for change in self.changes], [what] * 7)

    def test_volume_keys_keep_working_over_a_stream(self):
        # Deliberate: they set this box's (the TV's) volume, not the remote PC's.
        self.watch_media([[(Codes.KEY_VOLUMEDOWN, 1), (Codes.KEY_VOLUMEDOWN, 0)]],
                         capabilities=(Codes.KEY_VOLUMEDOWN,))
        self.assertEqual(self.changes, [("volume", -5)])

    def test_steps_still_waiting_hold_back_the_repeats_so_letting_go_stops_at_once(self):
        stuck = self.module.queue.Queue(maxsize=4)  # wpctl hangs: nothing takes the steps
        rounds, times = self.held(Codes.KEY_VOLUMEDOWN, 2.0)
        self.watch_media(rounds, times=times, backlog=stuck, capabilities=(Codes.KEY_VOLUMEDOWN,))
        self.assertEqual(stuck.qsize(), 1, "only the press: no tail of queued repeats after letting go")

    def test_the_repeat_pacing(self):
        busy = {"now": False}
        pace = self.module.MediaRepeat(busy=lambda: busy["now"])

        def key(value):
            return key_event(Codes.KEY_VOLUMEUP, value)

        self.assertTrue(pace.allow("kbd", key(1), 0.0), "a press always counts")
        self.assertFalse(pace.allow("kbd", key(2), 0.39))
        self.assertTrue(pace.allow("kbd", key(2), 0.4))
        self.assertFalse(pace.allow("kbd", key(2), 0.5))
        busy["now"] = True
        self.assertFalse(pace.allow("kbd", key(2), 0.6), "due, but earlier steps are still queued")
        self.assertTrue(pace.allow("kbd", key(1), 0.6), "a fresh press is never dropped here")
        busy["now"] = False
        self.assertFalse(pace.allow("kbd", key(2), 0.7), "the press restarted the delay")
        self.assertTrue(pace.allow("kbd", key(2), 1.0))
        self.assertTrue(pace.allow("other", key(1), 1.0), "paced per keyboard")
        self.assertTrue(pace.allow("kbd", key(0), 1.1))
        self.assertFalse(pace.allow("kbd", key(2), 5.0), "held from before it was watched: wait the delay")
        self.assertTrue(pace.allow("kbd", key(2), 5.4))
        self.assertTrue(pace.allow("kbd", key_event(Codes.KEY_ENTER, 2), 5.41), "other keys are not paced")
        pace.forget("other")
        self.assertEqual(list(pace.due), [("kbd", Codes.KEY_VOLUMEUP)])
        pace.reset()
        self.assertEqual(pace.due, {})

    def test_apples_fn_layer_sends_the_brightness_keys(self):
        # hid_apple: Fn+F1/F2 (or F1/F2 alone, depending on fnmode) arrive as the brightness codes.
        rounds = [[(Codes.KEY_FN, 1), (Codes.KEY_BRIGHTNESSDOWN, 1)], [(Codes.KEY_BRIGHTNESSDOWN, 0), (Codes.KEY_FN, 0)],
                  [(Codes.KEY_F1, 1), (Codes.KEY_F1, 0)]]
        self.watch_media(rounds, name="Apple Inc. Magic Keyboard",
                         capabilities=(Codes.KEY_LEFTMETA, Codes.KEY_FN, Codes.KEY_F1, Codes.KEY_BRIGHTNESSDOWN))
        self.assertEqual(self.changes, [("brightness", -5)])


if __name__ == "__main__":
    unittest.main()
