import importlib.util
import itertools
import pathlib
import tempfile
import unittest
from unittest import mock

import moonlightos_controllers as controllers
import moonlightos_padcheck as padcheck

# Real /proc/bus/input/devices text (captured on the Debian lab VM): a PC with a keyboard and
# a mouse and nothing else.
KEYBOARD_ONLY = """\
I: Bus=0011 Vendor=0001 Product=0001 Version=ab41
N: Name="AT Translated Set 2 keyboard"
P: Phys=isa0060/serio0/input0
S: Sysfs=/devices/platform/i8042/serio0/input/input0
U: Uniq=
H: Handlers=sysrq kbd leds event0
B: PROP=0
B: EV=120013
B: KEY=402000002 3803078f800d001 feffffdfffefffff fffffffffffffffe
B: MSC=10
B: LED=7

I: Bus=0019 Vendor=0000 Product=0001 Version=0000
N: Name="Power Button"
P: Phys=LNXPWRBN/button/input0
S: Sysfs=/devices/LNXSYSTM:00/LNXPWRBN:00/input/input2
U: Uniq=
H: Handlers=kbd event1
B: PROP=0
B: EV=3
B: KEY=8000 10000000000000 0

I: Bus=0011 Vendor=0002 Product=0006 Version=0000
N: Name="ImExPS/2 Generic Explorer Mouse"
P: Phys=isa0060/serio1/input0
S: Sysfs=/devices/platform/i8042/serio1/input/input3
U: Uniq=
H: Handlers=mouse0 event2
B: PROP=1
B: EV=7
B: KEY=1f0000 0 0 0 0
B: REL=143

I: Bus=0010 Vendor=001f Product=0001 Version=0100
N: Name="PC Speaker"
P: Phys=isa0061/input0
S: Sysfs=/devices/platform/pcspkr/input/input4
U: Uniq=
H: Handlers=kbd event3
B: PROP=0
B: EV=40001
B: SND=6
"""

# An Xbox One pad on USB (xpad driver): BTN_SOUTH is bit 48 of the fifth word from the right.
XBOX_USB = """\
I: Bus=0003 Vendor=045e Product=02ea Version=0301
N: Name="Microsoft X-Box One S pad"
P: Phys=usb-0000:00:14.0-2/input0
S: Sysfs=/devices/pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0/input/input21
U: Uniq=
H: Handlers=event5 js0
B: PROP=0
B: EV=20000b
B: KEY=7cdb000000000000 0 0 0 0
B: ABS=3003f
B: FF=107030000 0
"""

# A DualSense on Bluetooth: BlueZ hands it to the kernel through uhid, so every node sits under
# /devices/virtual/misc/uhid. Only the first node is the pad; the other two are its motion
# sensors and its touchpad (BTN_LEFT and BTN_TOUCH, but no BTN_SOUTH).
DUALSENSE_BLUETOOTH = """\
I: Bus=0005 Vendor=054c Product=0ce6 Version=8111
N: Name="DualSense Wireless Controller"
P: Phys=a0:b1:c2:d3:e4:f5
S: Sysfs=/devices/virtual/misc/uhid/0005:054C:0CE6.0004/input/input25
U: Uniq=e4:d3:c2:b1:a0:f5
H: Handlers=event18 js1
B: PROP=0
B: EV=20000b
B: KEY=7fdb000000000000 0 0 0 0
B: ABS=3003f
B: FF=107030000 0

I: Bus=0005 Vendor=054c Product=0ce6 Version=8111
N: Name="DualSense Wireless Controller Motion Sensors"
P: Phys=a0:b1:c2:d3:e4:f5
S: Sysfs=/devices/virtual/misc/uhid/0005:054C:0CE6.0004/input/input26
U: Uniq=e4:d3:c2:b1:a0:f5
H: Handlers=event19 js2
B: PROP=40
B: EV=19
B: ABS=3f
B: MSC=20

I: Bus=0005 Vendor=054c Product=0ce6 Version=8111
N: Name="DualSense Wireless Controller Touchpad"
P: Phys=a0:b1:c2:d3:e4:f5
S: Sysfs=/devices/virtual/misc/uhid/0005:054C:0CE6.0004/input/input27
U: Uniq=e4:d3:c2:b1:a0:f5
H: Handlers=mouse1 event20
B: PROP=5
B: EV=b
B: KEY=e520 10000 0 0 0 0
B: ABS=260800000000003
"""

# gamepad-nav's own virtual keyboard (python-evdev uinput defaults; KEY_UP/DOWN/LEFT/RIGHT,
# ENTER, ESC, DELETE, F5-F8 and F12): it lives under /devices/virtual/input.
UINPUT_NAV = """\
I: Bus=0003 Vendor=0001 Product=0001 Version=0001
N: Name="MoonlightOS Launcher Navigation"
P: Phys=
S: Sysfs=/devices/virtual/input/input33
U: Uniq=
H: Handlers=sysrq kbd event21
B: PROP=0
B: EV=3
B: KEY=8000000010000002 968001000007 0 0
"""

# The TV remote the kernel builds for a CEC adapter (bus 0x1e): arrow keys, Enter, Esc.
CEC_REMOTE = """\
I: Bus=001e Vendor=0000 Product=0000 Version=0000
N: Name="Pulse-Eight HDMI CEC"
P: Phys=
S: Sysfs=/devices/pci0000:00/0000:00:14.0/usb1/1-3/1-3:1.0/rc/rc0/input24
U: Uniq=
H: Handlers=kbd event22
B: PROP=0
B: EV=100013
B: KEY=8000000010000002 14a000000000 0 0
B: MSC=10
"""

# Some other program's software controller (it still claims BTN_SOUTH): not a real pad.
UINPUT_PAD = XBOX_USB.replace(
    'N: Name="Microsoft X-Box One S pad"', 'N: Name="Microsoft X-Box 360 pad"'
).replace(
    "S: Sysfs=/devices/pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0/input/input21",
    "S: Sysfs=/devices/virtual/input/input40",
).replace("Phys=usb-0000:00:14.0-2/input0", "Phys=")

PC = KEYBOARD_ONLY + "\n" + UINPUT_NAV + "\n" + CEC_REMOTE


class ParserTest(unittest.TestCase):
    def test_detects_xbox_usb_pad(self):
        self.assertEqual(padcheck.gamepads(PC + "\n" + XBOX_USB), ["Microsoft X-Box One S pad"])

    def test_detects_dualsense_bluetooth_but_not_its_sensors_or_touchpad(self):
        self.assertEqual(padcheck.gamepads(PC + "\n" + DUALSENSE_BLUETOOTH), ["DualSense Wireless Controller"])

    def test_bluetooth_pad_under_uhid_is_not_mistaken_for_a_software_device(self):
        # /devices/virtual/misc/uhid is where the kernel puts EVERY BlueZ HID device.
        self.assertIn("/devices/virtual/misc/uhid/", DUALSENSE_BLUETOOTH)
        self.assertEqual(len(padcheck.gamepads(DUALSENSE_BLUETOOTH)), 1)

    def test_detects_every_pad_when_there_are_several(self):
        self.assertEqual(
            padcheck.gamepads(XBOX_USB + "\n" + DUALSENSE_BLUETOOTH),
            ["Microsoft X-Box One S pad", "DualSense Wireless Controller"],
        )

    def test_ignores_keyboard_mouse_and_power_button(self):
        self.assertEqual(padcheck.gamepads(KEYBOARD_ONLY), [])

    def test_ignores_virtual_uinput_devices(self):
        # gamepad-nav's own keyboard, and a software pad that does claim BTN_SOUTH.
        self.assertEqual(padcheck.gamepads(UINPUT_NAV), [])
        self.assertEqual(padcheck.gamepads(UINPUT_PAD), [])
        self.assertEqual(padcheck.gamepads(KEYBOARD_ONLY + "\n" + UINPUT_PAD + "\n" + UINPUT_NAV), [])

    def test_ignores_the_launchers_own_devices_even_when_reported_elsewhere(self):
        odd = UINPUT_PAD.replace("Microsoft X-Box 360 pad", "MoonlightOS Buffered Keyboard").replace(
            "/devices/virtual/input/input40", "/devices/platform/x/input/input40")
        self.assertEqual(padcheck.gamepads(odd), [])

    def test_ignores_the_cec_remote(self):
        self.assertEqual(padcheck.gamepads(CEC_REMOTE), [])
        # Even a keymap that claims BTN_SOUTH is not a pad when it sits on the CEC bus.
        self.assertEqual(padcheck.gamepads(XBOX_USB.replace("Bus=0003", "Bus=001e")), [])

    def test_needs_the_btn_south_bit_in_the_right_word(self):
        self.assertEqual(padcheck.gamepads(XBOX_USB.replace("7cdb000000000000", "7cda000000000000")), [])
        # The same bit one word over is BTN_SOUTH + 64 (not a button the pad has).
        self.assertEqual(padcheck.gamepads(XBOX_USB.replace("KEY=7cdb000000000000 0 0 0 0", "KEY=7cdb000000000000 0 0 0 0 0")), [])
        # A key bitmap that is too short to reach bit 304 cannot hold a pad button.
        self.assertEqual(padcheck.gamepads(XBOX_USB.replace("KEY=7cdb000000000000 0 0 0 0", "KEY=7cdb000000000000")), [])

    def test_garbage_and_empty_text_give_no_pads(self):
        for text in ("", "\n\n", "hello\nworld", "N: Name=\nB: KEY=zzzz 0 0 0 0\n\n", "B: KEY=\n", "\x00\x01"):
            self.assertEqual(padcheck.gamepads(text), [], repr(text))

    def test_a_block_with_a_bad_bitmap_does_not_hide_a_good_pad(self):
        broken = XBOX_USB.replace("KEY=7cdb000000000000 0 0 0 0", "KEY=nothex 0 0 0 0").replace("X-Box One S", "Broken")
        self.assertEqual(padcheck.gamepads(broken + "\n" + DUALSENSE_BLUETOOTH), ["DualSense Wireless Controller"])

    def test_text_without_a_final_blank_line_is_still_parsed(self):
        self.assertEqual(padcheck.gamepads(XBOX_USB.rstrip("\n")), ["Microsoft X-Box One S pad"])


class ReadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def test_reads_and_parses_the_file(self):
        path = self.root / "devices"
        path.write_text(PC + "\n" + XBOX_USB)
        self.assertEqual(padcheck.read(path), ["Microsoft X-Box One S pad"])

    def test_a_pc_with_no_pad_reads_as_an_empty_list_not_none(self):
        path = self.root / "devices"
        path.write_text(KEYBOARD_ONLY)
        self.assertEqual(padcheck.read(path), [])

    def test_unreadable_file_is_none(self):
        self.assertIsNone(padcheck.read(self.root / "absent"))
        self.assertIsNone(padcheck.read(self.root))  # a directory

    def test_file_with_binary_junk_is_still_read(self):
        path = self.root / "devices"
        path.write_bytes(b"\xff\xfe\x00" + XBOX_USB.encode())
        self.assertIsInstance(padcheck.read(path), list)

    def test_default_path_is_the_kernel_device_list_only(self):
        self.assertEqual(str(padcheck.DEVICES), "/proc/bus/input/devices")


class BannerTest(unittest.TestCase):
    def test_banner_hidden_during_grace_period(self):
        for seconds in (0, 1, padcheck.GRACE_SECONDS - 0.1):
            self.assertEqual(padcheck.banner([], seconds), "", seconds)

    def test_banner_shown_after_grace_period_when_no_pad(self):
        self.assertEqual(padcheck.GRACE_SECONDS, 15)
        self.assertEqual(
            padcheck.banner([], 15),
            "NO CONTROLLER FOUND. PLUG ONE IN BY USB, OR PAIR: SETTINGS > BLUETOOTH",
        )
        self.assertTrue(padcheck.banner([], 3600))

    def test_banner_hidden_when_a_pad_is_present(self):
        self.assertEqual(padcheck.banner(["Xbox Wireless Controller"], 3600), "")

    def test_unreadable_proc_shows_no_banner(self):
        self.assertEqual(padcheck.banner(None, 3600), "")

    def test_banner_fits_76_columns_and_names_the_bluetooth_setting(self):
        text = padcheck.banner([], 3600)
        self.assertLessEqual(len(text), 76)
        self.assertIn("SETTINGS > BLUETOOTH", text)
        self.assertTrue(text.isascii())


class MonitorTest(unittest.TestCase):
    def monitor(self, reads):
        """A Monitor on a fake clock; reads is a list of read() results, the last one repeats."""
        self.now = 100.0
        self.read = mock.Mock(side_effect=lambda: reads[min(self.read.call_count - 1, len(reads) - 1)])
        return padcheck.Monitor(read=self.read, clock=lambda: self.now)

    def advance(self, seconds):
        self.now += seconds

    def test_nothing_at_start_and_during_the_bluetooth_grace_period(self):
        monitor = self.monitor([[]])
        self.assertEqual(monitor.line(), "")
        self.advance(14)
        self.assertEqual(monitor.line(), "")

    def test_banner_appears_after_15_seconds_without_a_pad(self):
        monitor = self.monitor([[]])
        monitor.line()
        self.advance(15)
        self.assertIn("SETTINGS > BLUETOOTH", monitor.line())

    def test_banner_goes_on_the_next_five_second_tick_after_a_pad_appears(self):
        monitor = self.monitor([[], ["DualSense Wireless Controller"]])
        self.advance(20)
        self.assertTrue(monitor.line())
        self.advance(1)  # the launcher redraws every second; the file is not re-read each time
        self.assertTrue(monitor.line())
        self.advance(4)  # next tick
        self.assertEqual(monitor.line(), "")

    def test_reads_the_file_at_most_every_five_seconds(self):
        monitor = self.monitor([[]])
        self.advance(30)
        for _ in range(4):
            monitor.line()
            self.advance(1)
        self.assertEqual(self.read.call_count, 1)
        self.advance(5)
        monitor.line()
        self.assertEqual(self.read.call_count, 2)

    def test_unreadable_file_never_shows_a_banner(self):
        monitor = self.monitor([None])
        self.advance(60)
        self.assertEqual(monitor.line(), "")

    def test_a_failing_reader_never_reaches_the_launcher(self):
        monitor = padcheck.Monitor(read=mock.Mock(side_effect=RuntimeError("boom")), clock=lambda: 1000.0)
        monitor.started = 0.0
        self.assertEqual(monitor.line(), "")

    def test_footer_is_a_bold_line_or_nothing(self):
        monitor = self.monitor([[]])
        self.assertEqual(monitor.footer(7), [])
        self.advance(20)
        self.assertEqual(monitor.footer(7), [(padcheck.banner([], 3600), 7)])


class LauncherFooterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("moonlightos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_padcheck_under_test", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def launcher(self, batteries, pads):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (30, 100)
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(screen)
        launcher.controllers = controllers.Monitor(reader=lambda: batteries)
        launcher.controllers.refresh()
        # Hermetic: never read the host's saved update state or its /proc, and run 60 s in.
        launcher.updates = self.module.update.Checker(current="0.1.13", state_path=pathlib.Path("/nonexistent/update-check.ini"))
        launcher.padcheck = padcheck.Monitor(read=lambda: pads, clock=itertools.chain([0.0], itertools.repeat(60.0)).__next__)
        return launcher

    def test_the_launcher_owns_a_monitor_that_starts_quiet(self):
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(mock.Mock(getmaxyx=lambda: (30, 100)))
        self.assertIsInstance(launcher.padcheck, padcheck.Monitor)
        self.assertEqual(launcher.padcheck.line(), "")

    def test_footer_shows_a_bold_banner_when_no_pad_and_no_battery_line(self):
        launcher = self.launcher([], [])
        [(text, attr)] = launcher.footer_lines()
        self.assertEqual(text, padcheck.banner([], 3600))
        self.assertTrue(attr & self.module.curses.A_BOLD)

    def test_footer_has_no_banner_when_a_pad_is_connected(self):
        self.assertEqual(self.launcher([], ["Xbox Wireless Controller"]).footer_lines(), [])

    def test_footer_has_no_banner_when_the_proc_file_cannot_be_read(self):
        self.assertEqual(self.launcher([], None).footer_lines(), [])

    def test_banner_never_adds_a_row_beside_the_battery_line(self):
        launcher = self.launcher([controllers.Battery("XBOX", 12)], [])
        self.assertEqual([text for text, _attr in launcher.footer_lines()], ["CONTROLLERS: XBOX 12% LOW"])

    def test_the_settings_menu_has_the_bluetooth_entry_the_banner_names(self):
        self.assertIn("BLUETOOTH", self.module.SETTINGS_MENU)
        self.assertIn("SETTINGS", [label for label, _action in self.module.FIXED_CONTROLS])


if __name__ == "__main__":
    unittest.main()
