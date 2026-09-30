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


SMALL_SCREENS = ((18, 65), (19, 69), (24, 80), (27, 98))


class ScrollingScreensTest(unittest.TestCase):
    """Every row a cursor can land on must be drawn, even on 720p/768p terminals."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_launcher()

    def app(self, number):
        return self.module.apps.Application(
            id=f"app{number}", name=f"APP {number}", kind="command", command="/bin/true",
            status_id=f"app{number}", order=number,
        )

    def launcher(self, screen, extra_apps=0):
        result = self.module.apps.LoadResult(tuple(self.app(number) for number in range(extra_apps)), ())
        with mock.patch.object(self.module, "network_summary", return_value="10.0.0.5  ONLINE"), mock.patch.object(
            self.module, "application_result", return_value=result
        ):
            return self.module.Launcher(screen)

    def assert_on_screen(self, screen, text, context):
        height, _width = screen.size
        drawn = {row: value for (row, _column), value in screen.rows.items()}
        self.assertTrue(any(text in value for value in drawn.values()), f"{text!r} not drawn: {context}")
        self.assertTrue(all(row < height - 1 for row in drawn), f"drew on the border or off-screen: {context}")

    def test_the_main_menu_shows_the_cursor_row_and_the_status_line_on_small_screens(self):
        for size in SMALL_SCREENS:
            for extra in (0, 12):
                screen = Screen(size=size)
                launcher = self.launcher(screen, extra)
                for selected in range(len(launcher.menu)):
                    launcher.selected = selected
                    launcher.draw()
                    label = launcher.menu[selected][0]
                    self.assert_on_screen(screen, f">  {label}", (size, extra, selected))
                    self.assert_on_screen(screen, "10.0.0.5  ONLINE", (size, extra, selected))

    def test_the_main_menu_says_when_entries_are_hidden(self):
        screen = Screen(size=(18, 65))
        launcher = self.launcher(screen, 6)
        launcher.draw()
        self.assertTrue(any("v  MORE" in value for value in screen.text()), screen.text())
        launcher.selected = len(launcher.menu) - 1
        launcher.draw()
        self.assertTrue(any("^  MORE" in value for value in screen.text()), screen.text())
        self.assertFalse(any("v  MORE" in value for value in screen.text()), screen.text())

    def test_a_tall_screen_keeps_the_grouping_gaps_and_needs_no_markers(self):
        screen = Screen(size=(40, 100))
        launcher = self.launcher(screen, 3)
        launcher.draw()
        self.assertFalse(any("MORE" in value for value in screen.text()))
        rows = {value.lstrip("> "): row for (row, _column), value in screen.rows.items()}
        self.assertEqual(rows["REBOOT"] - rows["SETTINGS"], 2)  # the blank row between groups

    def test_settings_and_choosers_show_the_cursor_row_on_small_screens(self):
        for size in SMALL_SCREENS:
            screen = Screen(size=size)
            settings = self.module.Settings(screen, self.launcher(screen))
            for selected, label in enumerate(self.module.SETTINGS_MENU):
                settings.selected = selected
                settings.draw()
                self.assert_on_screen(screen, f">  {label}", (size, selected))
            resolutions = [f"{1000 + number}x{number}" for number in range(20)]
            for selected, label in enumerate(resolutions):
                settings.draw("RESOLUTION", resolutions, selected)
                self.assert_on_screen(screen, f">  {label}", (size, selected))

    def test_the_application_and_remote_desktop_lists_show_the_cursor_row(self):
        for size in SMALL_SCREENS:
            screen = Screen(size=size)
            settings = self.module.RemoteDesktopSettings(screen, self.launcher(screen))
            rows = [f"PC {number}" for number in range(25)]
            for selected, label in enumerate(rows):
                settings.draw("REMOTE DESKTOP", rows, selected)
                self.assert_on_screen(screen, f">  {label}", (size, selected))


class BluetoothScrollTest(unittest.TestCase):
    def test_the_bluetooth_list_shows_the_cursor_row_on_small_screens(self):
        import moonlightos_bluetooth as bluetooth

        for size in SMALL_SCREENS:
            screen = Screen(size=size)
            menu = bluetooth.BluetoothMenu(screen, mock.Mock())
            rows = ["RESCAN"] + [f"DEVICE {number}" for number in range(14)] + ["TURN BLUETOOTH OFF", "BACK"]
            for selected, label in enumerate(rows):
                menu.draw("BLUETOOTH", rows, selected, details=["ON  ·  SCANNING"])
                drawn = screen.text()
                self.assertTrue(any(f">  {label}" in value for value in drawn), (size, selected, drawn))
                self.assertTrue(all(row < size[0] - 1 for row, _column in screen.rows), (size, selected))


class BluetoothCursorTest(unittest.TestCase):
    XBOX = {
        "path": "/org/bluez/hci0/dev_11", "address": "11:22:33:44:55:66", "alias": "Xbox Wireless Controller",
        "paired": False, "trusted": False, "connected": False, "rssi": -40, "audio": False,
    }
    APPLE = dict(XBOX, path="/org/bluez/hci0/dev_AA", address="AA:BB:CC:DD:EE:FF", alias="Apple TV")

    def snapshot(self, *devices, operations=()):
        return {
            "ok": True, "adapter": {"path": "/org/bluez/hci0", "powered": True, "discovering": True},
            "devices": list(devices), "operations": list(operations), "prompt": None,
        }

    def run_menu(self, snapshots, keys):
        import moonlightos_bluetooth as bluetooth

        class Client:
            def __init__(self):
                self.requests, self.left = [], list(snapshots)

            def snapshot(self):
                return self.left.pop(0) if self.left else self_outer.snapshot()

            def request(self, command, **fields):
                self.requests.append((command, fields))
                return {"ok": True, "operation_id": "op-1"}

        self_outer = self
        client = Client()

        class Keys(Screen):
            def getch(self):
                return self.keys.pop(0) if self.keys else 27

        bluetooth.BluetoothMenu(Keys(keys), client).run()
        return client.requests

    def test_a_device_that_appears_above_the_cursor_does_not_steal_the_selection(self):
        import moonlightos_bluetooth as bluetooth

        down, enter = bluetooth.curses.KEY_DOWN, 10
        done = self.snapshot(operations=[{"id": "op-1", "state": "completed"}])
        requests = self.run_menu(
            [self.snapshot(self.XBOX), self.snapshot(self.APPLE, self.XBOX), done],
            [down, enter, enter],  # select the Xbox, then PAIR on the confirm screen
        )
        self.assertIn(("pair", {"device": self.XBOX["path"]}), requests)
        self.assertNotIn(("pair", {"device": self.APPLE["path"]}), requests)

    def test_the_cursor_does_not_slide_onto_a_power_button_when_its_device_leaves(self):
        import moonlightos_bluetooth as bluetooth

        down, enter = bluetooth.curses.KEY_DOWN, 10
        both = self.snapshot(self.APPLE, self.XBOX)
        # The Xbox is under the cursor, then drops off the list: the cursor must not
        # end up on TURN BLUETOOTH OFF, which one more press would obey.
        requests = self.run_menu(
            [both, both, self.snapshot(self.APPLE), self.snapshot(self.APPLE)],
            [down, down, -1, enter],
        )
        self.assertNotIn("set_power", [command for command, _fields in requests])


class RdpMenuCursorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_launcher()

    def test_forgetting_the_password_does_not_leave_the_cursor_on_a_neighbouring_action(self):
        module = self.module
        state = {"connection": module.rdp.Connection(
            id="rdp-pc", name="PC", host="10.0.0.2", username="me", save_password=True, certificate="ab" * 32,
        )}

        def forget(_self, connection):
            state["connection"] = module.dataclasses.replace(connection, save_password=False)

        down, enter = module.curses.KEY_DOWN, 10
        # FORGET SAVED PASSWORD is the fourth row; its row disappears once it is done,
        # so a second press must not land on FORGET CERTIFICATE / DELETE CONNECTION.
        screen = Screen([down, down, down, enter, enter, 27])
        launcher = mock.Mock()
        settings = module.RemoteDesktopSettings(screen, launcher)
        settings.yes_no = mock.Mock(return_value=False)
        with mock.patch.object(module.rdp, "get_connection", side_effect=lambda _id: state["connection"]), mock.patch.object(
            module, "application_result", return_value=module.apps.LoadResult((), ())
        ), mock.patch.object(module.RemoteDesktopSettings, "forget_password", forget), mock.patch.object(
            module.RemoteDesktopSettings, "pin"
        ) as pin:
            settings.connection_menu("rdp-pc")
        self.assertFalse(state["connection"].save_password)
        settings.yes_no.assert_not_called()  # no certificate or delete question appeared
        pin.assert_called_once()  # the cursor moved up to the row above, PIN TO LAUNCHER

    def test_the_cursor_stays_on_its_row_when_rows_appear_below_it(self):
        module = self.module
        connection = module.rdp.Connection(id="rdp-pc", name="PC", host="10.0.0.2", username="me")
        pinned = []
        down, enter = module.curses.KEY_DOWN, 10
        screen = Screen([down, down, enter, enter, 27])
        settings = module.RemoteDesktopSettings(screen, mock.Mock())

        def pin(_self, item):
            pinned.append(item)

        def result():
            apps = (module.apps.Application(
                id="rdp-pc", name="PC", kind="rdp", connection="rdp-pc", status_id="rdp-pc"),) if pinned else ()
            return module.apps.LoadResult(apps, ())

        unpinned = []
        with mock.patch.object(module.rdp, "get_connection", return_value=connection), mock.patch.object(
            module, "application_result", side_effect=result
        ), mock.patch.object(module.RemoteDesktopSettings, "pin", pin), mock.patch.object(
            module.apps, "delete_user_application", side_effect=lambda *a, **k: unpinned.append(1)
        ), mock.patch.object(module.RemoteDesktopSettings, "_system_dir", return_value=pathlib.Path("/nonexistent")):
            settings.connection_menu("rdp-pc")
        # PIN TO LAUNCHER was chosen; the same row is now UNPIN FROM LAUNCHER, so the
        # second press toggles it back: one pin, one unpin.
        self.assertEqual((len(pinned), len(unpinned)), (1, 1))


if __name__ == "__main__":
    unittest.main()
