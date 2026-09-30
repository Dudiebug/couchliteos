import importlib.util
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import moonlightos_controllers as controllers


def supply(root, name, **files):
    directory = pathlib.Path(root) / name
    directory.mkdir(parents=True)
    for key, value in files.items():
        (directory / key).write_text(f"{value}\n")
    return directory


def gamepad(root, name, model, capacity=None, **extra):
    files = {"type": "Battery", "scope": "Device", "model_name": model, **extra}
    if capacity is not None:
        files["capacity"] = capacity
    return supply(root, name, **files)


class SysfsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def read(self):
        return controllers.read_sysfs(self.root)

    def test_reads_device_scope_batteries_with_capacity(self):
        gamepad(self.root, "ps-controller-battery-e8:47:3a:12:34:56", "DualSense Wireless Controller", 80,
                status="Discharging")
        gamepad(self.root, "xpadneo-battery-1", "Xbox Wireless Controller", 35)
        found = {item.name: item.percent for item in self.read()}
        self.assertEqual(found, {"DUALSENSE": 80, "XBOX": 35})

    def test_ignores_laptop_battery_mains_and_non_battery_supplies(self):
        supply(self.root, "BAT0", type="Battery", scope="System", capacity=90, model_name="LAPTOP")
        supply(self.root, "BAT1", type="Battery", capacity=90, model_name="NO SCOPE FILE")
        supply(self.root, "AC", type="Mains", scope="Device", online=1)
        supply(self.root, "ups", type="UPS", scope="Device", capacity=50)
        self.assertEqual(self.read(), [])

    def test_missing_directory_is_empty(self):
        self.assertEqual(controllers.read_sysfs(self.root / "absent"), [])

    def test_capacity_level_used_when_there_is_no_percentage(self):
        gamepad(self.root, "xbox-battery", "Xbox Wireless Controller", capacity_level="Low")
        [item] = self.read()
        self.assertIsNone(item.percent)
        self.assertEqual(item.level, "LOW")
        self.assertTrue(item.low)

    def test_charging_status_is_reported_and_suppresses_the_low_warning(self):
        gamepad(self.root, "ps-controller-battery-a", "DualSense Wireless Controller", 9, status="Charging")
        [item] = self.read()
        self.assertTrue(item.charging)
        self.assertFalse(item.low)

    def test_garbage_and_out_of_range_values_are_skipped(self):
        gamepad(self.root, "a", "Xbox Wireless Controller", "abc")
        gamepad(self.root, "b", "Xbox Wireless Controller", 250)
        supply(self.root, "c", type="Battery", scope="Device")  # no capacity at all
        self.assertEqual(self.read(), [])

    def test_zero_percent_that_the_driver_calls_unknown_is_not_a_reading(self):
        # Some drivers publish capacity 0 before the first battery report arrives.
        gamepad(self.root, "a", "Xbox Wireless Controller", 0, capacity_level="Unknown")
        gamepad(self.root, "b", "DualSense Wireless Controller", 0, status="Unknown")
        self.assertEqual(self.read(), [])

    def test_a_genuinely_empty_battery_is_still_reported(self):
        gamepad(self.root, "a", "Xbox Wireless Controller", 0, capacity_level="Critical", status="Discharging")
        [item] = self.read()
        self.assertEqual(item.percent, 0)
        self.assertTrue(item.low)

    def test_keyboards_mice_and_headsets_are_not_controllers(self):
        gamepad(self.root, "hid-aa:bb:cc:dd:ee:01-battery", "Logitech Wireless Keyboard", 50)
        gamepad(self.root, "hid-aa:bb:cc:dd:ee:02-battery", "MX Master Mouse", 50)
        gamepad(self.root, "hid-aa:bb:cc:dd:ee:03-battery", "Gaming Headset", 50)
        self.assertEqual(self.read(), [])

    def test_short_names(self):
        for model, directory, expected in [
            ("Sony Interactive Entertainment DualSense Wireless Controller", "ps-controller-battery-1", "DUALSENSE"),
            ("Xbox Wireless Controller", "x", "XBOX"),
            ("Nintendo Switch Pro Controller", "nintendo_switch_controller_battery_1", "SWITCH PRO"),
            ("Wireless Controller", "sony_controller_battery_e8:47:3a:12:34:56", "PLAYSTATION"),
            ("Controller", "steam-controller-battery-3", "STEAM"),
            ("", "mystery-battery", "CONTROLLER"),
        ]:
            self.assertEqual(controllers.short_name(model, directory), expected, model)

    def test_duplicate_names_are_numbered(self):
        gamepad(self.root, "a", "Xbox Wireless Controller", 50)
        gamepad(self.root, "b", "Xbox Wireless Controller", 40)
        self.assertEqual([item.name for item in self.read()], ["XBOX", "XBOX 2"])


INFO = """Device E8:47:3A:12:34:56 (public)
\tName: 8BitDo Pro 2
\tAlias: 8BitDo Pro 2
\tIcon: input-gaming
\tPaired: yes
\tConnected: yes
\tBattery Percentage: 0x50 (80)
"""


class BluezTest(unittest.TestCase):
    def test_parse_info_reads_a_gaming_device_battery(self):
        item = controllers.parse_bluez_info(INFO)
        self.assertEqual((item.name, item.percent, item.source), ("8BITDO PRO 2", 80, "bluez"))

    def test_parse_info_ignores_non_gaming_and_batteryless_devices(self):
        self.assertIsNone(controllers.parse_bluez_info(INFO.replace("input-gaming", "audio-headset")))
        self.assertIsNone(controllers.parse_bluez_info(INFO.replace("\tBattery Percentage: 0x50 (80)\n", "")))
        self.assertIsNone(controllers.parse_bluez_info(INFO.replace("Connected: yes", "Connected: no")))

    @staticmethod
    def fake_run(devices, infos):
        def run(command, **kwargs):
            assert command[0] == "bluetoothctl" and kwargs.get("timeout")
            if command[1:] == ["devices", "Connected"]:
                return subprocess.CompletedProcess(command, 0, devices, "")
            return subprocess.CompletedProcess(command, 0, infos[command[2]], "")
        return run

    def test_read_bluez_lists_connected_gamepads(self):
        run = self.fake_run("Device E8:47:3A:12:34:56 8BitDo Pro 2\n", {"E8:47:3A:12:34:56": INFO})
        self.assertEqual([item.percent for item in controllers.read_bluez(run=run)], [80])

    def test_read_bluez_skips_devices_the_kernel_already_reports(self):
        run = self.fake_run("Device E8:47:3A:12:34:56 8BitDo Pro 2\n", {"E8:47:3A:12:34:56": INFO})
        seen = {"hid-e8:47:3a:12:34:56-battery"}
        self.assertEqual(controllers.read_bluez(run=run, skip=seen), [])

    def test_read_bluez_ignores_a_missing_or_hung_bluetoothctl(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("bluetoothctl", 3)):
            self.assertEqual(controllers.read_bluez(run=mock.Mock(side_effect=error)), [])

    def test_bluez_info_failure_does_not_hide_other_controllers(self):
        def run(command, **_kwargs):
            if command[1] == "devices":
                return subprocess.CompletedProcess(command, 0, "Device AA:AA:AA:AA:AA:AA a\nDevice E8:47:3A:12:34:56 b\n", "")
            if command[2] == "AA:AA:AA:AA:AA:AA":
                raise subprocess.TimeoutExpired("bluetoothctl", 3)
            return subprocess.CompletedProcess(command, 0, INFO, "")
        self.assertEqual(len(controllers.read_bluez(run=run)), 1)


class FormatTest(unittest.TestCase):
    def test_line_shows_percentages_and_flags_low(self):
        items = [controllers.Battery("DUALSENSE", 80), controllers.Battery("XBOX", 35),
                 controllers.Battery("SWITCH", 12)]
        self.assertEqual(
            controllers.format_line(items), "CONTROLLERS: DUALSENSE 80%  XBOX 35%  SWITCH 12% LOW"
        )

    def test_threshold_is_inclusive_at_15(self):
        self.assertTrue(controllers.Battery("A", 15).low)
        self.assertFalse(controllers.Battery("A", 16).low)

    def test_levels_and_charging_are_labelled(self):
        self.assertEqual(
            controllers.format_line([controllers.Battery("XBOX", None, "HIGH"),
                                     controllers.Battery("PS", 9, charging=True)]),
            "CONTROLLERS: XBOX HIGH  PS 9% CHARGING",
        )

    def test_level_only_controller_shows_the_word_never_a_percentage(self):
        for level in ("LOW", "CRITICAL", "NORMAL"):
            line = controllers.format_line([controllers.Battery("XBOX", None, level)])
            self.assertEqual(line, f"CONTROLLERS: XBOX {level}")
            self.assertNotIn("%", line)

    def test_no_controllers_means_no_line(self):
        self.assertEqual(controllers.format_line([]), "")


class MonitorTest(unittest.TestCase):
    def test_refresh_keeps_the_last_good_value_when_reading_fails(self):
        reader = mock.Mock(side_effect=[[controllers.Battery("XBOX", 50)], OSError("gone")])
        monitor = controllers.Monitor(reader=reader)
        monitor.refresh()
        monitor.refresh()
        self.assertEqual(monitor.line(), "CONTROLLERS: XBOX 50%")

    def test_low_reports_only_unplugged_low_batteries(self):
        monitor = controllers.Monitor(reader=lambda: [controllers.Battery("A", 10), controllers.Battery("B", 90)])
        monitor.refresh()
        self.assertEqual([item.name for item in monitor.low()], ["A"])

    def test_read_all_combines_sysfs_then_bluez_with_dedupe(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as bluetooth:
            (pathlib.Path(bluetooth) / "hci0").mkdir()
            gamepad(directory, "hid-e8:47:3a:12:34:56-battery", "8BitDo Pro 2", 70)
            run = BluezTest.fake_run("Device E8:47:3A:12:34:56 8BitDo Pro 2\n", {"E8:47:3A:12:34:56": INFO})
            items = controllers.read_all(root=pathlib.Path(directory), run=run, bluetooth_root=pathlib.Path(bluetooth))
        self.assertEqual([(item.percent, item.source) for item in items], [(70, "sysfs")])

    def test_bluetoothctl_is_not_run_on_a_pc_without_a_bluetooth_adapter(self):
        run = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as bluetooth:
            self.assertFalse(controllers.bluetooth_present(pathlib.Path(bluetooth)))
            self.assertEqual(controllers.read_all(pathlib.Path(directory), run, pathlib.Path(bluetooth)), [])
        run.assert_not_called()
        self.assertFalse(controllers.bluetooth_present(pathlib.Path("/nonexistent/bluetooth")))

    def test_an_adapter_turns_the_bluez_lookup_on(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as bluetooth:
            (pathlib.Path(bluetooth) / "hci0").mkdir()
            (pathlib.Path(bluetooth) / "hci0:11").mkdir()  # a connection handle, not an adapter
            self.assertTrue(controllers.bluetooth_present(pathlib.Path(bluetooth)))
            run = BluezTest.fake_run("Device E8:47:3A:12:34:56 8BitDo Pro 2\n", {"E8:47:3A:12:34:56": INFO})
            items = controllers.read_all(pathlib.Path(directory), run, pathlib.Path(bluetooth))
        self.assertEqual([item.percent for item in items], [80])


class LauncherFooterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("moonlightos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_under_test", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def launcher(self, batteries):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (30, 100)
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(screen)
        launcher.controllers = controllers.Monitor(reader=lambda: batteries)
        # Hermetic: never read the host's saved update state.
        launcher.updates = self.module.update.Checker(current="0.1.13", state_path=pathlib.Path("/nonexistent/update-check.ini"))
        launcher.controllers.refresh()
        return launcher

    def test_footer_shows_controllers_and_highlights_low(self):
        launcher = self.launcher([controllers.Battery("XBOX", 12)])
        [(text, attr)] = [item for item in launcher.footer_lines() if "XBOX" in item[0]]
        self.assertEqual(text, "CONTROLLERS: XBOX 12% LOW")
        self.assertTrue(attr & self.module.curses.A_REVERSE)

    def test_footer_is_quiet_without_controllers(self):
        self.assertEqual(self.launcher([]).footer_lines(), [])


if __name__ == "__main__":
    unittest.main()
