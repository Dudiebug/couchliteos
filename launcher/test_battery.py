"""The PC battery: UPower states, the sysfs fallback, warnings, the footer and Bluetooth batteries."""

import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_battery as battery
import couchliteos_power as power


def upower(state, percent=50.0, present=True, kind=2, empty=0, full=0):
    return {"IsPresent": present, "Type": kind, "State": state, "Percentage": percent,
            "TimeToEmpty": empty, "TimeToFull": full}


class UPowerTest(unittest.TestCase):
    def read(self, properties):
        return battery.read_upower(lambda: properties)

    def test_states(self):
        self.assertEqual(self.read(upower(2, 54.4, empty=8100)), battery.Status("discharging", 54, 8100, "upower"))
        self.assertEqual(self.read(upower(1, 80, full=1800)), battery.Status("charging", 80, 1800, "upower"))
        self.assertEqual(self.read(upower(4, 100)).state, "full")
        self.assertEqual(self.read(upower(5, 79)).state, "full", "plugged in, not charging")
        self.assertEqual(self.read(upower(0, 50)).state, "unknown")

    def test_no_battery(self):
        self.assertEqual(self.read(upower(0, 0, present=False, kind=0)), battery.NO_BATTERY)
        self.assertEqual(self.read(upower(2, 40, kind=3)), battery.NO_BATTERY, "a UPS is not the PC's battery")
        self.assertFalse(battery.NO_BATTERY.present)

    def test_no_upower_falls_back_to_sysfs(self):
        def broken():
            raise RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")

        self.assertIsNone(battery.read_upower(broken))
        fallback = battery.Status("discharging", 30, None, "sysfs")
        self.assertEqual(battery.read_status(lambda: None, lambda: fallback), fallback)
        upower_status = battery.Status("full", 100, None, "upower")
        self.assertEqual(battery.read_status(lambda: upower_status, lambda: fallback), upower_status)


class SysfsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def supply(self, name, **values):
        folder = self.root / name
        folder.mkdir()
        for key, value in values.items():
            (folder / key).write_text(f"{value}\n")

    def test_laptop_battery_without_scope_is_read_with_time_left(self):
        self.supply("AC", type="Mains", online=0)
        self.supply("BAT0", type="Battery", status="Discharging", capacity=42,
                    energy_now=21000000, energy_full=50000000, power_now=10500000)
        self.assertEqual(battery.read_sysfs(self.root), battery.Status("discharging", 42, 7200, "sysfs"))

    def test_controller_batteries_are_not_the_pc_battery(self):
        self.supply("ps-controller-battery-aa", type="Battery", scope="Device", status="Discharging", capacity=20)
        self.assertEqual(battery.read_sysfs(self.root), battery.NO_BATTERY)

    def test_states_and_two_batteries(self):
        self.supply("BAT0", type="Battery", scope="System", status="Charging", capacity=60,
                    charge_now=3000000, charge_full=5000000, current_now=2000000)
        self.supply("BAT1", type="Battery", status="Full", capacity=100)
        status = battery.read_sysfs(self.root)
        self.assertEqual((status.state, status.percent, status.seconds), ("charging", 80, 3600))

    def test_full_not_charging_and_unknown(self):
        self.supply("BAT0", type="Battery", status="Not charging", capacity=80)
        self.assertEqual(battery.read_sysfs(self.root).state, "full")
        (self.root / "BAT0" / "status").write_text("Unknown\n")
        self.assertEqual(battery.read_sysfs(self.root).state, "unknown")

    def test_missing_directory_is_no_battery(self):
        self.assertEqual(battery.read_sysfs(self.root / "missing"), battery.NO_BATTERY)


class TextTest(unittest.TestCase):
    def test_footer_text(self):
        self.assertEqual(battery.status_text(battery.Status("discharging", 54, 8100)), "BATTERY 54%  2:15 LEFT")
        self.assertEqual(battery.status_text(battery.Status("charging", 80, 1800)), "BATTERY 80%  CHARGING  FULL IN 0:30")
        self.assertEqual(battery.status_text(battery.Status("full", 100)), "BATTERY 100%  FULL")
        self.assertEqual(battery.status_text(battery.Status("full", 79)), "BATTERY 79%  PLUGGED IN")
        self.assertEqual(battery.status_text(battery.Status("unknown")), "BATTERY UNKNOWN")
        self.assertEqual(battery.status_text(battery.NO_BATTERY), "")

    def test_no_battery_and_no_device_shows_nothing(self):
        self.assertEqual(battery.format_line(battery.NO_BATTERY, []), "")

    def test_bluetooth_batteries_join_the_line(self):
        self.assertEqual(battery.format_line(battery.NO_BATTERY, [("WH-1000XM5", 70)]), "WH-1000XM5 70%")
        self.assertEqual(battery.format_line(battery.Status("discharging", 54), [("WH-1000XM5", 70)]),
                         "BATTERY 54%  ·  WH-1000XM5 70%")


class BluetoothBatteryTest(unittest.TestCase):
    def device(self, **values):
        return {"alias": "WH-1000XM5", "connected": True, "icon": "audio-headphones", "battery": 70, **values}

    def test_shown_when_reported(self):
        self.assertEqual(battery.bluetooth_batteries([self.device()]), [("WH-1000XM5", 70)])

    def test_hidden_when_not_reported_disconnected_or_not_audio(self):
        hidden = [
            self.device(battery=None),
            self.device(connected=False),
            self.device(icon="input-gaming", alias="Wireless Controller"),  # the controller footer shows it
            self.device(battery=True),
            self.device(battery=140),
        ]
        self.assertEqual(battery.bluetooth_batteries(hidden), [])

    def test_no_adapter_means_no_lookup(self):
        with mock.patch.object(battery.controllers, "bluetooth_present", return_value=False), \
                mock.patch("couchliteos_bluetooth.BluetoothClient") as client:
            self.assertEqual(battery.read_bluetooth(), [])
        client.assert_not_called()

    def test_service_down_means_none_shown(self):
        import couchliteos_bluetooth

        with mock.patch.object(battery.controllers, "bluetooth_present", return_value=True), \
                mock.patch.object(couchliteos_bluetooth.BluetoothClient, "snapshot",
                                  side_effect=couchliteos_bluetooth.BluetoothError("down")):
            self.assertEqual(battery.read_bluetooth(), [])


class WarningsTest(unittest.TestCase):
    def test_each_threshold_warns_once(self):
        warnings = battery.Warnings()
        levels = [warnings.check(battery.Status("discharging", percent)) for percent in (25, 20, 19, 15, 10, 9, 5, 4, 3)]
        self.assertEqual(levels, [None, 20, None, None, 10, None, 5, None, None])

    def test_a_jump_warns_for_the_lowest_level_only(self):
        warnings = battery.Warnings()
        self.assertEqual(warnings.check(battery.Status("discharging", 8)), 10)
        self.assertIsNone(warnings.check(battery.Status("discharging", 7)))
        self.assertEqual(warnings.check(battery.Status("discharging", 5)), 5)

    def test_charging_starts_them_over(self):
        warnings = battery.Warnings()
        warnings.check(battery.Status("discharging", 18))
        self.assertIsNone(warnings.check(battery.Status("discharging", 17)))
        self.assertIsNone(warnings.check(battery.Status("charging", 17)))
        self.assertEqual(warnings.check(battery.Status("discharging", 17)), 20)

    def test_unknown_or_no_battery_never_warns(self):
        warnings = battery.Warnings()
        self.assertIsNone(warnings.check(battery.Status("unknown", 3)))
        self.assertIsNone(warnings.check(battery.NO_BATTERY))
        self.assertIsNone(warnings.check(battery.Status("discharging", None)))

    def test_warning_text(self):
        self.assertIn("20%", battery.warning_text(20))
        self.assertIn("PLUG IN THE CHARGER NOW", battery.warning_text(5))


class MonitorTest(unittest.TestCase):
    def test_reading_warning_and_power_source_change(self):
        readings = iter([battery.Status("full", 100), battery.Status("discharging", 19), battery.Status("discharging", 18)])
        monitor = battery.Monitor(lambda: next(readings), lambda: [])
        monitor.refresh()
        self.assertEqual(monitor.line(), "BATTERY 100%  FULL")
        self.assertFalse(monitor.on_battery())
        self.assertIsNone(monitor.take_warning())
        monitor.refresh()
        self.assertTrue(monitor.on_battery())
        self.assertTrue(monitor.low())
        self.assertTrue(monitor.take_source_change())
        self.assertFalse(monitor.take_source_change())
        self.assertEqual(monitor.take_warning(), 20)
        self.assertIsNone(monitor.take_warning(), "taken once")
        monitor.refresh()
        self.assertIsNone(monitor.take_warning())

    def test_no_battery_shows_nothing(self):
        monitor = battery.Monitor(lambda: battery.NO_BATTERY, lambda: [])
        monitor.refresh()
        self.assertEqual(monitor.line(), "")
        self.assertFalse(monitor.low())

    def test_a_failed_read_keeps_the_last_reading(self):
        monitor = battery.Monitor(lambda: battery.Status("discharging", 50), mock.Mock(side_effect=OSError))
        monitor.refresh()
        monitor.reader = mock.Mock(side_effect=RuntimeError)
        monitor.refresh()
        self.assertEqual(monitor.line(), "BATTERY 50%")


class PowerSettingsTest(unittest.TestCase):
    def test_battery_blank_delay_is_saved_and_used_only_on_battery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.ini"
            path.write_text("[audio]\nmic = default\n", encoding="utf-8")
            self.assertEqual(power.load_settings(path).battery_blank, power.DEFAULT_BATTERY_BLANK)
            power.save_settings(power.Settings(blank=10, sleep=30, battery_blank=1), path)
            settings = power.load_settings(path)
            self.assertEqual(settings.battery_blank, 1)
            self.assertIn("[audio]\nmic = default\n", path.read_text(encoding="utf-8"))
        self.assertEqual(power.effective_settings(settings, True, True).blank, 10)
        self.assertEqual(power.effective_settings(settings, True, True, on_battery=True).blank, 1)
        self.assertEqual(power.effective_settings(settings, False, True, on_battery=True).sleep, 0)


class LauncherFooterTest(unittest.TestCase):
    """The home screen footer: the battery only when the PC has one, MIC LIVE, audio notices."""

    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_battery_under_test", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def launcher(self, status, mic="none", devices=()):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (30, 100)
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(screen)
        launcher.controllers = self.module.controllers.Monitor(reader=lambda: [])
        launcher.updates = self.module.update.Checker(current="0.1.13", state_path=pathlib.Path("/nonexistent/update-check.ini"))
        launcher.padcheck.footer = lambda _attr: []
        launcher.battery = battery.Monitor(lambda: status, lambda: list(devices))
        launcher.battery.refresh()
        launcher.mic = self.module.audio.MicMonitor(lambda: mic)
        launcher.mic.refresh()
        launcher.idle = mock.Mock()
        return launcher

    def lines(self, launcher):
        with mock.patch.object(self.module.audio, "read_notice", return_value=""):
            return launcher.footer_lines()

    def test_a_desktop_shows_no_battery(self):
        self.assertEqual(self.lines(self.launcher(battery.NO_BATTERY)), [])

    def test_battery_line_low_warning_and_power_source(self):
        launcher = self.launcher(battery.Status("discharging", 9, 1200))
        [(text, attr)] = self.lines(launcher)
        self.assertEqual(text, "BATTERY 9%  0:20 LEFT")
        self.assertTrue(attr & self.module.curses.A_REVERSE)
        self.assertIn("BATTERY LOW (10%)", launcher.status)
        launcher.idle.apply.assert_called_once()
        self.assertEqual(launcher.idle.apply.call_args.args[0].blank, power.load_settings(pathlib.Path("/nonexistent")).battery_blank)

    def test_bluetooth_headphones_battery_and_mic_live(self):
        launcher = self.launcher(battery.NO_BATTERY, mic="live", devices=[("WH-1000XM5", 60)])
        self.assertEqual([text for text, _attr in self.lines(launcher)], ["WH-1000XM5 60%", "MIC LIVE"])

    def test_audio_notice_is_shown(self):
        launcher = self.launcher(battery.NO_BATTERY)
        with mock.patch.object(self.module.audio, "read_notice", return_value="AUDIO: WH-1000XM5"):
            self.assertEqual([text for text, _attr in launcher.footer_lines()], ["AUDIO: WH-1000XM5"])


if __name__ == "__main__":
    unittest.main()
