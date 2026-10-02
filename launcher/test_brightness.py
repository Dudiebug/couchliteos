"""Screen brightness (couchliteos_brightness) against a fake /sys/class/backlight."""

import os
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_brightness as brightness


class FakeSysfs(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name) / "backlight"
        self.root.mkdir()

    def device(self, name, kind="raw", maximum=255, level=None, status=None):
        """One backlight; `status` gives it a DRM connector parent (connected / disconnected)."""
        device = self.root / name
        device.mkdir()
        if kind is not None:
            (device / "type").write_text(f"{kind}\n")
        if maximum is not None:
            (device / "max_brightness").write_text(f"{maximum}\n")
        (device / "brightness").write_text(f"{maximum if level is None else level}\n")
        if status is not None:
            (device / "device").mkdir()
            (device / "device" / "status").write_text(f"{status}\n")
        return device

    @staticmethod
    def level(device):
        return int((device / "brightness").read_text())


class DeviceChoiceTest(FakeSysfs):
    def test_firmware_then_platform_then_raw_like_systemd(self):
        self.device("intel_backlight", "raw", 120000, status="connected")
        self.device("dell_backlight", "platform", 15)
        self.assertEqual(brightness.find_device(self.root).name, "dell_backlight")
        self.device("acpi_video0", "firmware", 7)
        self.assertEqual(brightness.find_device(self.root).name, "acpi_video0")

    def test_among_raw_ones_the_connected_panel_wins_then_the_finest(self):
        self.device("amdgpu_bl1", "raw", 255)
        self.device("intel_backlight", "raw", 96000, status="disconnected")
        self.assertEqual(brightness.find_device(self.root).name, "intel_backlight", "highest max_brightness")
        self.device("nouveau_bl", "raw", 100, status="connected")
        self.assertEqual(brightness.find_device(self.root).name, "nouveau_bl")

    def test_devices_that_cannot_be_used_are_skipped(self):
        self.device("zero", "raw", 0)
        self.device("unknown_type", "led", 255)
        self.device("no_type", None, 255)
        self.device("no_max", "raw", None)
        (self.root / "garbage").mkdir()
        (self.root / "garbage" / "type").write_text("raw\n")
        (self.root / "garbage" / "max_brightness").write_text("lots\n")
        self.assertIsNone(brightness.find_device(self.root))
        self.device("good", "raw", 10)
        self.assertEqual(brightness.find_device(self.root).name, "good")

    def test_no_backlight_class_or_an_empty_one_means_none(self):
        self.assertIsNone(brightness.find_device(self.root))
        self.assertIsNone(brightness.find_device(self.root / "missing"))
        self.assertIsNone(brightness.get_percent(self.root / "missing"))
        with self.assertRaises(OSError):
            brightness.change(5, self.root)


class CurveTest(FakeSysfs):
    def test_the_curve_is_perceptual_and_round_trips_on_fine_panels(self):
        self.assertEqual(brightness.to_level(100, 1000), 1000)
        self.assertEqual(brightness.to_level(50, 1000), 250, "half as bright looks like a quarter of the power")
        for percent in range(brightness.FLOOR, 101, brightness.STEP):
            self.assertEqual(brightness.to_percent(brightness.to_level(percent, 120000), 120000), percent)

    def test_the_floor_is_never_level_zero(self):
        self.assertEqual(brightness.to_level(brightness.FLOOR, 7), 1)
        self.assertEqual(brightness.to_level(0, 255), 1)

    def test_steps_land_on_the_five_percent_grid(self):
        device = self.device("panel", maximum=10000, level=brightness.to_level(52, 10000))
        self.assertEqual(brightness.change(5, self.root), 55)
        self.assertEqual(brightness.change(-5, self.root), 50)
        self.assertEqual(self.level(device), brightness.to_level(50, 10000))
        self.assertEqual(brightness.get_percent(self.root), 50)

    def test_down_stops_at_the_floor_and_up_at_full(self):
        device = self.device("panel", maximum=10000, level=brightness.to_level(10, 10000))
        for _ in range(4):
            percent = brightness.change(-brightness.STEP, self.root)
        self.assertEqual(percent, brightness.FLOOR)
        self.assertGreater(self.level(device), 0)
        self.device("other", "firmware", 10000, level=brightness.to_level(95, 10000))
        self.assertEqual([brightness.change(5, self.root) for _ in range(3)], [100, 100, 100])

    def test_a_dark_screen_steps_up_from_the_floor(self):
        device = self.device("panel", maximum=255, level=0)
        self.assertEqual(brightness.get_percent(self.root), 0)
        brightness.change(-5, self.root)
        self.assertEqual(self.level(device), 1, "even down lights it to the floor")

    def test_a_coarse_panel_never_loses_a_press(self):
        device = self.device("acpi_video0", "firmware", 15, level=1)
        levels = []
        for _ in range(6):
            brightness.change(5, self.root)
            levels.append(self.level(device))
        self.assertEqual(levels, sorted(set(levels)), "every press moved it up")
        for _ in range(20):
            brightness.change(-5, self.root)
        self.assertEqual(self.level(device), 1)


class AccessTest(FakeSysfs):
    def test_unreadable_brightness_is_unavailable_and_not_changed(self):
        device = self.device("panel")
        (device / "brightness").write_text("\n")
        self.assertIsNone(brightness.get_percent(self.root))
        with self.assertRaises(OSError):
            brightness.change(5, self.root)

    def test_a_backlight_this_user_cannot_write_is_hidden(self):
        self.device("panel", level=64)
        with mock.patch.object(brightness.os, "access", return_value=False) as access:
            self.assertIsNone(brightness.get_percent(self.root))
        access.assert_called_once_with(self.root / "panel" / "brightness", os.W_OK)
        self.assertEqual(brightness.get_percent(self.root), 50)

    def test_a_failed_write_is_reported(self):
        self.device("panel", level=64)
        with mock.patch.object(pathlib.Path, "write_text", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(PermissionError):
                brightness.change(5, self.root)

    def test_no_write_when_nothing_would_change(self):
        self.device("panel")
        with mock.patch.object(pathlib.Path, "write_text") as write:
            self.assertEqual(brightness.change(5, self.root), 100)
        write.assert_not_called()


class HotkeyTest(FakeSysfs):
    def switch(self, value):
        path = self.root.parent / "brightness_switch_enabled"
        path.write_text(f"{value}\n")
        return path

    def test_the_kernel_steps_acpi_video_itself_only_when_its_switch_is_on(self):
        self.device("acpi_video0", "firmware", 15)
        self.assertTrue(brightness.kernel_handles_hotkeys(self.root, self.switch("Y")))
        self.assertFalse(brightness.kernel_handles_hotkeys(self.root, self.switch("N")))
        self.assertFalse(brightness.kernel_handles_hotkeys(self.root, self.root / "no-video-module"))

    def test_a_native_backlight_is_left_to_us(self):
        self.device("intel_backlight", "raw", 96000)
        self.assertFalse(brightness.kernel_handles_hotkeys(self.root, self.switch("Y")))
        self.assertFalse(brightness.kernel_handles_hotkeys(self.root / "missing", self.switch("Y")))


if __name__ == "__main__":
    unittest.main()
