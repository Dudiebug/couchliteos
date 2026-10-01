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
    ABS_X = 0
    ABS_Y = 1
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


if __name__ == "__main__":
    unittest.main()
