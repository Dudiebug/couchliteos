import errno
import importlib.util
import pathlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock


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
    KEY_HOMEPAGE = 172
    KEY_DELETE = 111
    KEY_F5 = 63
    KEY_F6 = 64
    KEY_F7 = 65
    KEY_F8 = 66
    KEY_F12 = 88


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

    def test_holding_home_for_three_seconds_requests_sleep_exactly_once(self):
        hold = self.hold()
        hold.feed(self.event(Codes.BTN_MODE, 1), 100.0)
        self.assertFalse(hold.due(102.9))
        self.assertTrue(hold.due(103.0))
        self.assertFalse(hold.due(103.5))
        hold.feed(self.event(Codes.BTN_MODE, 0), 104.0)
        self.assertFalse(hold.due(110.0))

    def test_the_homepage_key_counts_as_home_for_the_hold_too(self):
        hold = self.hold()
        hold.feed(self.event(Codes.KEY_HOMEPAGE, 1), 40.0)
        self.assertFalse(hold.due(42.9))
        self.assertTrue(hold.due(43.0))
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

    def request_sleep_with(self, can_suspend):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            request = pathlib.Path(directory) / 'suspend'
            old, self.module.SLEEP_REQUEST = self.module.SLEEP_REQUEST, request
            try:
                with mock.patch.object(self.module.power, "can_suspend", return_value=can_suspend) as check:
                    self.module.request_sleep()
            finally:
                self.module.SLEEP_REQUEST = old
            check.assert_called_once_with()
            return request.exists()

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

    def test_holding_guide_for_three_seconds_goes_home_then_asks_for_sleep_once(self):
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


if __name__ == "__main__":
    unittest.main()
