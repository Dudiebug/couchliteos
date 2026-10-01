import pathlib
import tempfile
import unittest
from types import SimpleNamespace

import couchliteos_cec as cec


def lines(*rows):
    return "\n".join(rows) + "\n"


# Captured from cec-ctl 1.30.1 on Debian 13 (kernel 6.12) against the vivid
# test driver, which emulates CEC adapters (see the report for how).
INFO_PLAYBACK = lines(
    "Driver Info:",
    "\tDriver Name                : vivid",
    "\tAdapter Name               : vivid-000-vid-out0",
    "\tCapabilities               : 0x000002be",
    "\t\tLogical Addresses",
    "\t\tTransmit",
    "\t\tPassthrough",
    "\t\tRemote Control Support",
    "\t\tMonitor All",
    "\t\tMonitor Pin",
    "\t\tReply Vendor ID",
    "\tDriver version             : 6.12.101",
    "\tAvailable Logical Addresses: 4",
    "\tConnector Info             : None",
    "\tPhysical Address           : 1.1.0.0",
    "\tLogical Address Mask       : 0x0010",
    "\tCEC Version                : 2.0",
    "\tVendor ID                  : 0x000c03 (HDMI)",
    "\tOSD Name                   : 'CouchLiteOS'",
    "\tLogical Addresses          : 1 (Allow RC Passthrough)",
    "",
    "\t  Logical Address          : 4 (Playback Device 1)",
    "\t    Primary Device Type    : Playback",
    "\t    Logical Address Type   : Playback",
    "\t    All Device Types       : Playback",
    "\t    RC TV Profile          : None",
    "\t    Device Features        :",
    "\t\tNone",
    "",
)
INFO_UNCONFIGURED = lines(
    "Driver Info:",
    "\tDriver Name                : vivid",
    "\tAdapter Name               : vivid-000-vid-out0",
    "\tCapabilities               : 0x000002be",
    "\t\tLogical Addresses",
    "\t\tTransmit",
    "\t\tPassthrough",
    "\t\tRemote Control Support",
    "\t\tMonitor All",
    "\t\tMonitor Pin",
    "\t\tReply Vendor ID",
    "\tDriver version             : 6.12.101",
    "\tAvailable Logical Addresses: 4",
    "\tConnector Info             : None",
    "\tPhysical Address           : f.f.f.f",
    "\tLogical Address Mask       : 0x0000",
    "\tCEC Version                : 2.0",
    "\tOSD Name                   : ''",
    "\tLogical Addresses          : 0 ",
    "",
)
# The vivid output above with the capability list of a USB adapter: pulse8-cec
# sets CEC_CAP_PHYS_ADDR, which cec-ctl prints as "Physical Address". Edited by
# hand (no Pulse-Eight hardware in the lab), not captured.
INFO_SETTABLE_PA = (
    INFO_UNCONFIGURED
    .replace("vivid-000-vid-out0", "Pulse-Eight USB-CEC Adapter")
    .replace(": vivid", ": pulse8-cec")
    .replace("\t\tLogical Addresses\n", "\t\tPhysical Address\n\t\tLogical Addresses\n", 1)
)

MONITOR = lines(
    "",
    "Initial Event: State Change: PA: 1.1.0.0, LA mask: 0x0010",
    "Received from TV to all (0 to 15): STANDBY (0x36)",
    "Received from TV to Playback Device 1 (0 to 4): STANDBY (0x36)",
    "Transmitted by Playback Device 1 to TV (4 to 0): FEATURE_ABORT (0x00):",
    "\tabort-msg: 54 (0x36, STANDBY)",
    "\treason: unrecognized-op (0x00)",
    "Received from TV to Playback Device 1 (0 to 4): USER_CONTROL_PRESSED (0x44):",
    "\tui-cmd: up (0x01)",
)

TX_OK = lines(
    "",
    "Transmit from Playback Device 1 to TV (4 to 0):",
    "IMAGE_VIEW_ON (0x04)",
    "\tSequence: 32 Tx Timestamp: 6505.611748s",
)
TX_NACK = lines(
    "",
    "Transmit from Playback Device 1 to Audio System (4 to 5):",
    "IMAGE_VIEW_ON (0x04)",
    "\tSequence: 42 Tx Timestamp: 6731.652961s",
    "\tTx, Not Acknowledged (4), Max Retries",
)
TX_ABORTED = lines(
    "",
    "Transmit from Playback Device 1 to TV (4 to 0):",
    "IMAGE_VIEW_ON (0x04)",
    "\tSequence: 44 Tx Timestamp: 6733.407763s",
    "\tTx, Aborted, Max Retries",
)
POWER_ON = lines(
    "",
    "Transmit from Playback Device 1 to TV (4 to 0):",
    "GIVE_DEVICE_POWER_STATUS (0x8f)",
    "    Received from TV (0):",
    "    REPORT_POWER_STATUS (0x90):",
    "\tpwr-state: on (0x00)",
    "\tSequence: 34 Tx Timestamp: 6505.823752s Rx Timestamp: 6505.914749s",
    "\tApproximate response time: 18 ms",
)
POWER_STANDBY = POWER_ON.replace("on (0x00)", "standby (0x01)")
POWER_ABORT = lines(
    "",
    "Transmit from Playback Device 1 to TV (4 to 0):",
    "GIVE_DEVICE_POWER_STATUS (0x8f)",
    "    Received from TV (0):",
    "    FEATURE_ABORT (0x00):",
    "\tabort-msg: 143 (0x8f, GIVE_DEVICE_POWER_STATUS)",
    "\treason: unrecognized-op (0x00)",
    "\tSequence: 47 Tx Timestamp: 6858.502081s Rx Timestamp: 6858.618961s",
    "\tApproximate response time: 20 ms",
    "\tTx, OK, Rx, OK, Feature Abort",
)
OSD_NAME = lines(
    "",
    "Transmit from Playback Device 1 to TV (4 to 0):",
    "GIVE_OSD_NAME (0x46)",
    "    Received from TV (0):",
    "    SET_OSD_NAME (0x47):",
    "\tname: FakeTV",
    "\tSequence: 35 Tx Timestamp: 6505.997688s Rx Timestamp: 6506.211098s",
    "\tApproximate response time: 21 ms",
)
VENDOR = lines(
    "",
    "Transmit from Playback Device 1 to TV (4 to 0):",
    "GIVE_DEVICE_VENDOR_ID (0x8c)",
    "    Received from TV (0):",
    "    DEVICE_VENDOR_ID (0x87):",
    "\tvendor-id: 0x00e091 (LG)",
    "\tSequence: 36 Tx Timestamp: 6506.298995s Rx Timestamp: 6506.437102s",
    "\tApproximate response time: 18 ms",
)
TV_QUERY = VENDOR + OSD_NAME + POWER_ON


class FakeRun:
    """Records argv lists and answers with canned cec-ctl output."""

    def __init__(self, *answers, default=TX_OK):
        self.answers = list(answers)
        self.default = default
        self.calls = []

    def __call__(self, argv, timeout=10.0):
        self.calls.append(list(argv))
        return self.answers.pop(0) if self.answers else self.default


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "config.ini"

    def test_defaults_turn_the_tv_on_but_never_sleep(self):
        self.assertEqual(cec.load_settings(self.path), cec.Settings(True, False))
        self.path.write_text("[display]\noutput = HDMI-A-1\n")
        self.assertEqual(cec.load_settings(self.path), cec.Settings(True, False))

    def test_reads_the_cec_section(self):
        self.path.write_text("[cec]\nturn_tv_on = false\nsleep_on_tv_off = true\n")
        self.assertEqual(cec.load_settings(self.path), cec.Settings(False, True))

    def test_unrecognised_values_fall_back_to_the_defaults(self):
        self.path.write_text("[cec]\nturn_tv_on = maybe\nsleep_on_tv_off = 1\n")
        self.assertEqual(cec.load_settings(self.path), cec.Settings(True, True))

    def test_a_broken_file_gives_the_defaults(self):
        self.path.write_text("not an ini file\n[cec\n")
        self.assertEqual(cec.load_settings(self.path), cec.Settings(True, False))

    def test_save_appends_the_section_and_keeps_everything_else(self):
        self.path.write_text("[display]\noutput = HDMI-A-1\n\n[tailscale]\nenabled = false\n")
        cec.save_settings(cec.Settings(False, True), self.path)
        text = self.path.read_text()
        self.assertIn("[display]\noutput = HDMI-A-1\n", text)
        self.assertIn("[tailscale]\nenabled = false\n", text)
        self.assertIn("[cec]\nturn_tv_on = false\nsleep_on_tv_off = true\n", text)
        self.assertEqual(cec.load_settings(self.path), cec.Settings(False, True))

    def test_save_replaces_only_the_cec_section(self):
        self.path.write_text(
            "[cec]\nturn_tv_on = false\nsleep_on_tv_off = true\n\n[tailscale]\nenabled = true\n"
        )
        cec.save_settings(cec.Settings(True, False), self.path)
        text = self.path.read_text()
        self.assertEqual(text.count("[cec]"), 1)
        self.assertIn("[tailscale]\nenabled = true\n", text)
        self.assertEqual(cec.load_settings(self.path), cec.Settings(True, False))

    def test_the_tv_goes_to_standby_with_the_pc_unless_switched_off(self):
        self.assertTrue(cec.load_settings(self.path).tv_off_on_sleep)
        self.path.write_text("[cec]\ntv_off_on_sleep = false\n")
        self.assertFalse(cec.load_settings(self.path).tv_off_on_sleep)
        self.assertTrue(cec.load_settings(self.path).turn_tv_on)

    def test_save_keeps_the_standby_with_sleep_switch(self):
        cec.save_settings(cec.Settings(True, False, False), self.path)
        self.assertIn("tv_off_on_sleep = false\n", self.path.read_text())
        self.assertEqual(cec.load_settings(self.path), cec.Settings(True, False, False))

    def test_save_creates_a_missing_file_group_readable(self):
        cec.save_settings(cec.Settings(True, True), self.path)
        self.assertEqual(cec.load_settings(self.path), cec.Settings(True, True))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)


class ParseAdapterTest(unittest.TestCase):
    def test_configured_playback_adapter(self):
        adapter = cec.parse_adapter("/dev/cec1", INFO_PLAYBACK)
        self.assertEqual(adapter.device, "/dev/cec1")
        self.assertEqual(adapter.driver, "vivid")
        self.assertEqual(adapter.name, "vivid-000-vid-out0")
        self.assertEqual(adapter.phys_addr, "1.1.0.0")
        self.assertEqual(adapter.la_mask, 0x10)
        self.assertTrue(adapter.configured)
        self.assertIn("Remote Control Support", adapter.caps)
        self.assertNotIn("None", adapter.caps)
        self.assertFalse(adapter.can_set_phys_addr)

    def test_adapter_without_a_physical_address_is_not_configured(self):
        adapter = cec.parse_adapter("/dev/cec1", INFO_UNCONFIGURED)
        self.assertEqual(adapter.phys_addr, "f.f.f.f")
        self.assertEqual(adapter.la_mask, 0)
        self.assertFalse(adapter.configured)

    def test_usb_adapters_advertise_a_settable_physical_address(self):
        adapter = cec.parse_adapter("/dev/cec0", INFO_SETTABLE_PA)
        self.assertEqual(adapter.driver, "pulse8-cec")
        self.assertTrue(adapter.can_set_phys_addr)

    def test_garbage_is_not_an_adapter(self):
        self.assertIsNone(cec.parse_adapter("/dev/cec0", ""))
        self.assertIsNone(cec.parse_adapter("/dev/cec0", "Failed to open /dev/cec0: Permission denied\n"))


class PickAdapterTest(unittest.TestCase):
    def test_prefers_an_adapter_with_a_valid_physical_address(self):
        cold = cec.parse_adapter("/dev/cec0", INFO_UNCONFIGURED)
        live = cec.parse_adapter("/dev/cec1", INFO_PLAYBACK)
        self.assertIs(cec.pick_adapter([cold, live]), live)
        self.assertIs(cec.pick_adapter([cold]), cold)
        self.assertIsNone(cec.pick_adapter([]))

    def test_find_adapters_reads_every_device_and_skips_unreadable_ones(self):
        run = FakeRun(None, INFO_PLAYBACK)
        found = cec.find_adapters(run, devices=["/dev/cec0", "/dev/cec1"])
        self.assertEqual([a.device for a in found], ["/dev/cec1"])
        self.assertEqual(run.calls, [["cec-ctl", "-d", "/dev/cec0"], ["cec-ctl", "-d", "/dev/cec1"]])


class ParseMessageTest(unittest.TestCase):
    def test_broadcast_and_directed_standby_from_the_tv(self):
        parsed = [cec.parse_message(line) for line in MONITOR.splitlines()]
        self.assertEqual(parsed[2], cec.Message(0, 15, 0x36))
        self.assertEqual(parsed[3], cec.Message(0, 4, 0x36))
        self.assertEqual(parsed[7], cec.Message(0, 4, 0x44))

    def test_only_received_messages_are_messages(self):
        for line in MONITOR.splitlines():
            if not line.startswith("Received from"):
                self.assertIsNone(cec.parse_message(line), line)


class TxOkTest(unittest.TestCase):
    def test_transmit_outcomes(self):
        self.assertTrue(cec.tx_ok(TX_OK))
        self.assertTrue(cec.tx_ok(POWER_ON))
        self.assertTrue(cec.tx_ok(POWER_ABORT))  # a Feature Abort still reached the TV
        self.assertFalse(cec.tx_ok(TX_NACK))
        self.assertFalse(cec.tx_ok(TX_ABORTED))
        self.assertFalse(cec.tx_ok("Failed to open /dev/cec9: No such file or directory\n"))


class TvInfoTest(unittest.TestCase):
    def test_vendor_name_and_power(self):
        self.assertEqual(cec.parse_tv_info(TV_QUERY), cec.TvInfo("LG", "FakeTV", "on"))
        self.assertEqual(cec.parse_power(POWER_STANDBY), "standby")

    def test_a_tv_that_aborts_the_power_query_has_no_power_state(self):
        info = cec.parse_tv_info(VENDOR + OSD_NAME + POWER_ABORT)
        self.assertEqual(info, cec.TvInfo("LG", "FakeTV", ""))

    def test_unknown_vendor_shows_the_id(self):
        info = cec.parse_tv_info(VENDOR.replace(" (LG)", ""))
        self.assertEqual(info.vendor, "0x00e091")

    def test_silence_is_no_information(self):
        self.assertEqual(cec.parse_tv_info(""), cec.TvInfo("", "", ""))


class StandbyDecisionTest(unittest.TestCase):
    sleepy = cec.Settings(True, True)
    standby = cec.Message(0, 15, cec.OP_STANDBY)

    def decide(self, message=standby, settings=sleepy, now=1000.0, last_tv_on=0.0, last_suspend=None):
        return cec.should_suspend(message, settings, now, last_tv_on, last_suspend)

    def test_tv_standby_suspends_when_enabled(self):
        self.assertTrue(self.decide())
        self.assertTrue(self.decide(cec.Message(0, 4, cec.OP_STANDBY)))

    def test_does_nothing_when_the_setting_is_off(self):
        self.assertFalse(self.decide(settings=cec.Settings(True, False)))

    def test_ignores_other_devices_and_other_messages(self):
        self.assertFalse(self.decide(cec.Message(5, 15, cec.OP_STANDBY)))
        self.assertFalse(self.decide(cec.Message(0, 4, 0x44)))
        self.assertFalse(self.decide(None))

    def test_ignores_standby_right_after_turning_the_tv_on(self):
        self.assertFalse(self.decide(now=1030.0, last_tv_on=1000.0))
        self.assertTrue(self.decide(now=1061.0, last_tv_on=1000.0))

    def test_ignores_repeats_after_a_suspend_request(self):
        self.assertFalse(self.decide(now=1030.0, last_suspend=1000.0))
        self.assertTrue(self.decide(now=1061.0, last_suspend=1000.0))


class StandbyTvTest(unittest.TestCase):
    """The hook that runs before the PC sleeps must be quick and must ask only the TV."""

    def setUp(self):
        self.calls = []
        self.now = 0.0

    def run_cec(self, *answers):
        replies = list(answers)

        def run(argv, timeout=10.0):
            self.calls.append((list(argv), timeout))
            self.now += 0.5
            return replies.pop(0)

        return run

    def standby(self, run, devices=("/dev/cec0",), budget=2.0):
        return cec.standby_tv(list(devices), run, budget, lambda: self.now)

    def test_sends_standby_to_the_tv(self):
        self.assertTrue(self.standby(self.run_cec(TX_OK)))
        self.assertEqual(
            self.calls, [(["cec-ctl", "-d", "/dev/cec0", "--skip-info", "--to", "0", "--standby"], 2.0)]
        )

    def test_a_tv_that_does_not_answer_is_not_retried_and_not_waited_for(self):
        self.assertFalse(self.standby(self.run_cec(TX_NACK)))
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.standby(self.run_cec(None)))

    def test_no_adapter_means_no_command(self):
        self.assertFalse(self.standby(self.run_cec(), devices=()))
        self.assertEqual(self.calls, [])

    def test_the_next_adapter_is_tried_within_the_same_budget(self):
        self.assertTrue(self.standby(self.run_cec(None, TX_OK), devices=("/dev/cec0", "/dev/cec1")))
        self.assertEqual([call[0][2] for call in self.calls], ["/dev/cec0", "/dev/cec1"])
        self.assertEqual([call[1] for call in self.calls], [2.0, 1.5])

    def test_it_stops_when_the_budget_is_spent(self):
        devices = ("/dev/cec0", "/dev/cec1", "/dev/cec2", "/dev/cec3", "/dev/cec4")
        self.assertFalse(self.standby(self.run_cec(None, None, None, None, None), devices=devices))
        self.assertEqual(len(self.calls), 4)


class EdidPathTest(unittest.TestCase):
    def test_uses_the_edid_of_a_connected_connector(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name, status, edid in (
                ("card0-HDMI-A-1", "disconnected", b""),
                ("card0-DP-1", "connected", b"\x00\xff" * 8),
            ):
                (root / name).mkdir()
                (root / name / "status").write_text(status + "\n")
                (root / name / "edid").write_bytes(edid)
            (root / "card0").mkdir()
            self.assertEqual(cec.edid_path(root), str(root / "card0-DP-1" / "edid"))

    def test_no_connected_display_means_no_edid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "card0-HDMI-A-1").mkdir()
            (root / "card0-HDMI-A-1" / "status").write_text("connected\n")
            (root / "card0-HDMI-A-1" / "edid").write_bytes(b"")
            self.assertIsNone(cec.edid_path(root))
            self.assertIsNone(cec.edid_path(root / "missing"))


class ConfigureAdapterTest(unittest.TestCase):
    def test_registers_as_a_playback_device_named_couchliteos(self):
        run = FakeRun("", INFO_PLAYBACK)
        adapter = cec.configure_adapter(cec.parse_adapter("/dev/cec1", INFO_UNCONFIGURED), run, edid=None)
        self.assertEqual(
            run.calls[0],
            ["cec-ctl", "-d", "/dev/cec1", "--skip-info", "--playback", "--osd-name", "CouchLiteOS"],
        )
        self.assertEqual(run.calls[1], ["cec-ctl", "-d", "/dev/cec1"])
        self.assertTrue(adapter.configured)

    def test_adapters_that_need_it_get_the_physical_address_from_the_display_edid(self):
        run = FakeRun("", INFO_PLAYBACK)
        usb = cec.parse_adapter("/dev/cec0", INFO_SETTABLE_PA)
        cec.configure_adapter(usb, run, edid="/sys/class/drm/card0-HDMI-A-1/edid")
        self.assertEqual(
            run.calls[0],
            ["cec-ctl", "-d", "/dev/cec0", "--skip-info", "--playback", "--osd-name", "CouchLiteOS",
             "--phys-addr-from-edid", "/sys/class/drm/card0-HDMI-A-1/edid"],
        )

    def test_a_physical_address_the_kernel_already_has_is_left_alone(self):
        run = FakeRun("", INFO_PLAYBACK)
        usb = cec.parse_adapter("/dev/cec0", INFO_SETTABLE_PA.replace("f.f.f.f", "2.0.0.0"))
        cec.configure_adapter(usb, run, edid="/sys/class/drm/card0-HDMI-A-1/edid")
        self.assertNotIn("--phys-addr-from-edid", run.calls[0])

    def test_unreadable_adapter_gives_none(self):
        run = FakeRun(None)
        self.assertIsNone(cec.configure_adapter(cec.parse_adapter("/dev/cec1", INFO_UNCONFIGURED), run, edid=None))


class TurnTvOnTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.messages = []

    def turn_on(self, run, tries=3):
        return cec.turn_tv_on(
            "/dev/cec1", "1.1.0.0", run, sleep=self.sleeps.append, log=self.messages.append, tries=tries
        )

    def test_wakes_the_tv_then_switches_to_this_input(self):
        run = FakeRun(TX_OK, POWER_ON, TX_OK)
        self.assertTrue(self.turn_on(run))
        self.assertEqual(
            run.calls,
            [
                ["cec-ctl", "-d", "/dev/cec1", "--skip-info", "--to", "0", "--image-view-on"],
                ["cec-ctl", "-d", "/dev/cec1", "--skip-info", "--to", "0", "--give-device-power-status"],
                ["cec-ctl", "-d", "/dev/cec1", "--skip-info", "--active-source", "phys-addr=1.1.0.0"],
            ],
        )

    def test_waits_while_the_tv_is_still_starting(self):
        run = FakeRun(TX_OK, POWER_STANDBY, POWER_STANDBY, POWER_ON, TX_OK)
        self.assertTrue(self.turn_on(run))
        self.assertEqual(len(run.calls), 5)
        self.assertEqual(len(self.sleeps), 2)

    def test_switches_input_even_if_the_tv_never_reports_on(self):
        run = FakeRun(TX_OK, POWER_STANDBY, POWER_STANDBY, POWER_STANDBY, TX_OK)
        self.assertTrue(self.turn_on(run, tries=3))
        self.assertIn("--active-source", run.calls[-1])

    def test_retries_a_tv_that_does_not_acknowledge(self):
        run = FakeRun(TX_NACK, TX_OK, POWER_ON, TX_OK)
        self.assertTrue(self.turn_on(run))
        self.assertEqual(run.calls[0], run.calls[1])
        self.assertEqual(len(self.sleeps), 1)

    def test_gives_up_quietly_when_nobody_answers(self):
        run = FakeRun(default=TX_NACK)
        self.assertFalse(self.turn_on(run, tries=3))
        self.assertEqual(len(run.calls), 3)
        self.assertTrue(all("--image-view-on" in call for call in run.calls))
        self.assertTrue(self.messages)


class RemoteKeyMapsTest(unittest.TestCase):
    ecodes = SimpleNamespace(
        KEY_UP=103, KEY_DOWN=108, KEY_LEFT=105, KEY_RIGHT=106, KEY_ENTER=28, KEY_ESC=1,
        KEY_DELETE=111, KEY_F5=63, KEY_F6=64, KEY_F7=65, KEY_F8=66,
        KEY_OK=352, KEY_EXIT=174, KEY_BACK=158, KEY_RED=398, KEY_GREEN=399, KEY_YELLOW=400,
        KEY_BLUE=401, KEY_CLEAR=355, KEY_ROOT_MENU=0x2fa, KEY_MENU=139, KEY_HOME=102,
    )

    def test_arrows_select_back_and_colours_drive_the_launcher(self):
        nav, _home = cec.remote_key_maps(self.ecodes)
        e = self.ecodes
        self.assertEqual(nav[e.KEY_UP], e.KEY_UP)
        self.assertEqual(nav[e.KEY_RIGHT], e.KEY_RIGHT)
        self.assertEqual(nav[e.KEY_OK], e.KEY_ENTER)
        self.assertEqual(nav[e.KEY_ENTER], e.KEY_ENTER)
        self.assertEqual(nav[e.KEY_EXIT], e.KEY_ESC)
        self.assertEqual(nav[e.KEY_BACK], e.KEY_ESC)
        self.assertEqual(nav[e.KEY_CLEAR], e.KEY_DELETE)
        self.assertEqual(
            [nav[e.KEY_RED], nav[e.KEY_GREEN], nav[e.KEY_YELLOW], nav[e.KEY_BLUE]],
            [e.KEY_F5, e.KEY_F6, e.KEY_F7, e.KEY_F8],
        )

    def test_home_menu_and_root_menu_request_the_launcher(self):
        nav, home = cec.remote_key_maps(self.ecodes)
        e = self.ecodes
        self.assertEqual(home, {e.KEY_HOME, e.KEY_ROOT_MENU, e.KEY_MENU})
        self.assertFalse(home & set(nav))

    def test_codes_this_evdev_does_not_know_are_skipped(self):
        nav, home = cec.remote_key_maps(SimpleNamespace(KEY_UP=103, KEY_ESC=1))
        self.assertEqual(nav, {103: 103})
        self.assertEqual(home, set())

    def test_the_cec_input_bus_is_recognised(self):
        self.assertTrue(cec.is_cec_bus(0x1E))
        self.assertFalse(cec.is_cec_bus(0x03))


class StatusTest(unittest.TestCase):
    def test_no_adapter(self):
        status = cec.query_status(FakeRun(), devices=[])
        self.assertIsNone(status.adapter)
        self.assertFalse(status.usable)
        text = "\n".join(cec.status_lines(status))
        self.assertIn("NO CEC ADAPTER FOUND", cec.status_lines(status)[0])
        self.assertIn("PULSE-EIGHT", text)
        self.assertIn("DISPLAYPORT", text)

    def test_adapter_and_tv_details(self):
        run = FakeRun(INFO_PLAYBACK, TV_QUERY)
        status = cec.query_status(run, devices=["/dev/cec1"])
        self.assertEqual(run.calls[1][:4], ["cec-ctl", "-d", "/dev/cec1", "--skip-info"])
        self.assertEqual(status.tv, cec.TvInfo("LG", "FakeTV", "on"))
        text = "\n".join(cec.status_lines(status))
        for expected in ("/dev/cec1", "vivid", "1.1.0.0", "LG", "FAKETV", "ON"):
            self.assertIn(expected.upper(), text.upper())

    def test_an_unconfigured_adapter_is_not_asked_about_the_tv(self):
        run = FakeRun(INFO_UNCONFIGURED)
        status = cec.query_status(run, devices=["/dev/cec1"])
        self.assertEqual(len(run.calls), 1)
        self.assertIsNone(status.tv)
        self.assertIn("NOT CONNECTED", "\n".join(cec.status_lines(status)))

    def test_a_silent_tv_is_reported_as_such(self):
        status = cec.query_status(FakeRun(INFO_PLAYBACK, ""), devices=["/dev/cec1"])
        self.assertIn("NO ANSWER", "\n".join(cec.status_lines(status)))

    def test_toggle_rows(self):
        rows = cec.toggle_rows(cec.Settings(True, False))
        self.assertEqual(
            rows,
            ["TURN TV ON AT START/WAKE  ON", "SLEEP WHEN TV TURNS OFF  OFF", "TV STANDBY WHEN PC SLEEPS  ON"],
        )
        self.assertTrue(cec.query_status(FakeRun(INFO_PLAYBACK, TV_QUERY), devices=["/dev/cec1"]).usable)


class GateTest(unittest.TestCase):
    """Hardware gating: nothing is offered or done without a CEC adapter that reaches a TV."""

    def test_an_adapter_that_knows_where_it_is_plugged_in_is_usable_even_before_registering(self):
        adapter = cec.parse_adapter("/dev/cec1", INFO_PLAYBACK.replace("0x0010", "0x0000"))
        self.assertFalse(adapter.configured)
        self.assertTrue(cec.Status(adapter).usable)

    def test_no_physical_address_means_no_path_to_a_tv(self):
        self.assertFalse(cec.Status(cec.parse_adapter("/dev/cec1", INFO_UNCONFIGURED)).usable)
        self.assertFalse(cec.Status(None).usable)

    def test_unusable_rows_are_shown_unavailable_whatever_was_saved_elsewhere(self):
        saved_elsewhere = cec.Settings(turn_tv_on=True, sleep_on_tv_off=True)
        rows = cec.toggle_rows(saved_elsewhere, usable=False)
        self.assertEqual(
            rows,
            [
                "TURN TV ON AT START/WAKE  UNAVAILABLE",
                "SLEEP WHEN TV TURNS OFF  UNAVAILABLE",
                "TV STANDBY WHEN PC SLEEPS  UNAVAILABLE",
            ],
        )

    def test_toggling_flips_one_setting_and_keeps_the_other(self):
        settings = cec.Settings(turn_tv_on=True, sleep_on_tv_off=False)
        self.assertEqual(cec.toggled(settings, 0, usable=True), cec.Settings(False, False))
        self.assertEqual(cec.toggled(settings, 1, usable=True), cec.Settings(True, True))
        self.assertEqual(cec.toggled(settings, 2, usable=True), cec.Settings(True, False, False))

    def test_toggling_without_a_usable_adapter_changes_nothing(self):
        settings = cec.Settings(turn_tv_on=True, sleep_on_tv_off=True)
        self.assertIs(cec.toggled(settings, 0, usable=False), settings)
        self.assertIs(cec.toggled(settings, 1, usable=False), settings)
        self.assertIs(cec.toggled(settings, 2, usable=False), settings)

    def test_bring_up_without_any_cec_device_runs_nothing(self):
        run = FakeRun()
        self.assertIsNone(cec.bring_up(run, devices=[], edid=None))
        self.assertEqual(run.calls, [])

    def test_bring_up_registers_and_returns_the_configured_adapter(self):
        run = FakeRun(INFO_UNCONFIGURED.replace("f.f.f.f", "1.1.0.0"), "", INFO_PLAYBACK)
        adapter = cec.bring_up(run, devices=["/dev/cec1"], edid=None)
        self.assertEqual((adapter.device, adapter.phys_addr), ("/dev/cec1", "1.1.0.0"))
        self.assertIn("--playback", run.calls[1])

    def test_bring_up_leaves_an_adapter_alone_while_it_has_no_link_to_a_tv(self):
        run = FakeRun(INFO_UNCONFIGURED, "", INFO_UNCONFIGURED)
        self.assertIsNone(cec.bring_up(run, devices=["/dev/cec1"], edid=None))
        self.assertEqual(run.calls, [["cec-ctl", "-d", "/dev/cec1"]])

    def test_bring_up_sets_the_address_of_a_usb_adapter_when_a_display_is_connected(self):
        run = FakeRun(INFO_SETTABLE_PA, "", INFO_PLAYBACK)
        adapter = cec.bring_up(run, devices=["/dev/cec0"], edid="/sys/class/drm/card0-HDMI-A-1/edid")
        self.assertIsNotNone(adapter)
        self.assertIn("--phys-addr-from-edid", run.calls[1])

    def test_bring_up_waits_for_a_usb_adapter_until_a_display_is_connected(self):
        run = FakeRun(INFO_SETTABLE_PA)
        self.assertIsNone(cec.bring_up(run, devices=["/dev/cec0"], edid=None))
        self.assertEqual(len(run.calls), 1)

    def test_bring_up_gives_up_when_the_adapter_cannot_be_read(self):
        self.assertIsNone(cec.bring_up(FakeRun(None), devices=["/dev/cec1"], edid=None))

    def test_device_nodes_are_only_cec_numbered_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.assertEqual(cec.device_nodes(root), [])
            for name in ("cec0", "cec1", "cecfoo", "video0"):
                (root / name).touch()
            self.assertEqual(cec.device_nodes(root), [str(root / "cec0"), str(root / "cec1")])


class WatchTest(unittest.TestCase):
    """The daemon's monitor loop with a fake clock, settings file and suspend."""

    def setUp(self):
        self.suspended = []
        self.logged = []
        self.settings = cec.Settings(True, True)
        self.clock = 1000.0

    def tick(self):
        return self.clock

    def watch(self, feed, last_tv_on=0.0):
        cec.watch(feed, lambda: self.settings, self.tick, lambda: self.suspended.append(self.clock),
                  self.logged.append, last_tv_on)

    def test_broadcast_and_directed_standby_suspend_only_once(self):
        self.watch(MONITOR.splitlines())
        self.assertEqual(self.suspended, [1000.0])
        self.assertTrue(any("suspend" in entry for entry in self.logged))

    def test_nothing_happens_with_the_setting_off(self):
        self.settings = cec.Settings(True, False)
        self.watch(MONITOR.splitlines())
        self.assertEqual(self.suspended, [])

    def test_the_setting_is_read_again_for_every_message(self):
        def feed():
            yield "Received from TV to all (0 to 15): STANDBY (0x36)"
            self.settings = cec.Settings(True, False)
            yield "Received from TV to all (0 to 15): STANDBY (0x36)"
            self.settings = cec.Settings(True, True)
            self.clock += 120
            yield "Received from TV to all (0 to 15): STANDBY (0x36)"

        self.watch(feed())
        self.assertEqual(self.suspended, [1000.0, 1120.0])

    def test_standby_just_after_switching_the_tv_on_is_ignored(self):
        self.watch(MONITOR.splitlines(), last_tv_on=990.0)
        self.assertEqual(self.suspended, [])

    def test_the_settings_are_only_read_for_a_tv_standby(self):
        reads = []

        def load():
            reads.append(1)
            return self.settings

        cec.watch(
            [
                "Received from TV to Playback Device 1 (0 to 4): USER_CONTROL_PRESSED (0x44):",
                "Received from Audio System to all (5 to 15): REPORT_POWER_STATUS (0x90):",
                "some other line of cec-ctl output",
            ],
            load, self.tick, lambda: None, self.logged.append, 0.0,
        )
        self.assertEqual(reads, [])

    def test_a_remote_key_press_is_not_a_standby(self):
        self.watch(["Received from TV to Playback Device 1 (0 to 4): USER_CONTROL_PRESSED (0x44):"])
        self.assertEqual(self.suspended, [])


class SleepGateTest(unittest.TestCase):
    """Suspend-on-standby needs a PC that can suspend; /sys/power/state says whether it can."""

    def state(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = pathlib.Path(directory.name) / "state"
        if text is not None:
            path.write_text(text)
        return path

    @staticmethod
    def logind(answer, returncode=0):
        """A stand-in for `busctl ... CanSuspend`; answer None is a bus that cannot be reached."""
        def run(argv, **_kwargs):
            if answer is None:
                raise OSError("no busctl")
            return SimpleNamespace(returncode=returncode, stdout=f's "{answer}"\n')
        return run

    def supported(self, text, answer="no"):
        return cec.suspend_supported(self.state(text), self.logind(answer))

    def test_suspend_to_ram_listed(self):
        self.assertTrue(self.supported("freeze mem disk"))

    def test_no_suspend_to_ram(self):
        self.assertFalse(self.supported("disk"))
        self.assertFalse(self.supported(""))
        self.assertFalse(self.supported(None))

    def test_the_same_states_as_the_main_sleep_entry_count(self):
        # couchliteos_power.suspend_supported: "mem" or "freeze" (suspend-to-idle) is enough
        self.assertTrue(self.supported("freeze disk"))
        self.assertTrue(self.supported("mem"))

    def test_logind_yes_or_challenge_decides_whatever_the_state_file_says(self):
        for answer in ("yes", "challenge"):
            self.assertTrue(self.supported("", answer))
            self.assertTrue(self.supported(None, answer))

    def test_logind_na_means_this_pc_cannot_suspend(self):
        self.assertFalse(self.supported("freeze mem disk", "na"))

    def test_a_logind_that_cannot_answer_leaves_the_state_file_to_decide(self):
        for run in (self.logind(None), self.logind("yes", returncode=1), self.logind("maybe")):
            self.assertTrue(cec.suspend_supported(self.state("mem"), run))
            self.assertFalse(cec.suspend_supported(self.state("disk"), run))

    def test_logind_is_asked_with_a_short_timeout_and_no_shell(self):
        seen = {}

        def run(argv, **kwargs):
            seen.update(argv=argv, **kwargs)
            return SimpleNamespace(returncode=0, stdout='s "yes"\n')

        cec.suspend_supported(self.state("mem"), run)
        self.assertEqual(seen["argv"][0], "busctl")
        self.assertIn("CanSuspend", seen["argv"])
        self.assertLessEqual(seen["timeout"], 3)

    def test_the_sleep_row_says_why_it_is_off(self):
        rows = cec.toggle_rows(cec.Settings(True, True), usable=True, can_sleep=False)
        self.assertEqual(rows[0], "TURN TV ON AT START/WAKE  ON")
        self.assertEqual(rows[1], "SLEEP WHEN TV TURNS OFF  NOT SUPPORTED ON THIS PC")
        self.assertEqual(rows[2], "TV STANDBY WHEN PC SLEEPS  NOT SUPPORTED ON THIS PC")

    def test_the_sleep_switch_cannot_be_turned_on_without_suspend(self):
        settings = cec.Settings(True, False)
        self.assertIs(cec.toggled(settings, 1, usable=True, can_sleep=False), settings)
        self.assertIs(cec.toggled(settings, 2, usable=True, can_sleep=False), settings)
        self.assertEqual(cec.toggled(settings, 0, usable=True, can_sleep=False), cec.Settings(False, False))

    def test_a_saved_sleep_setting_is_ignored_where_suspend_is_missing(self):
        saved_elsewhere = cec.Settings(True, True)
        self.assertEqual(cec.effective_settings(saved_elsewhere, can_sleep=False), cec.Settings(True, False))
        self.assertIs(cec.effective_settings(saved_elsewhere, can_sleep=True), saved_elsewhere)


if __name__ == "__main__":
    unittest.main()
