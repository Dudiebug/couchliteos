import errno
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import moonlightos_setup as setup


class StepPlanTest(unittest.TestCase):
    def test_required_steps_come_first_in_the_documented_order(self):
        order = setup.plan_steps(cec_present=False, tv_available=False)
        self.assertEqual(
            order, ["network", "controller", "display", "streaming", "tailscale", "chiaki-ng", "applications"]
        )

    def test_tv_step_needs_both_a_cec_device_and_a_tv_screen(self):
        self.assertNotIn("tv", setup.plan_steps(cec_present=True, tv_available=False))
        self.assertNotIn("tv", setup.plan_steps(cec_present=False, tv_available=True))
        order = setup.plan_steps(cec_present=True, tv_available=True)
        self.assertEqual(order.index("tv"), order.index("streaming") + 1)

    def test_every_step_has_a_title(self):
        for step in setup.plan_steps(cec_present=True, tv_available=True):
            self.assertTrue(setup.TITLES[step])


class StateTest(unittest.TestCase):
    def test_round_trip_is_private_and_resumes_at_the_first_unfinished_step(self):
        order = ["network", "controller", "display"]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "setup-state.json"
            setup.save_state({"network": "done", "controller": "skipped"}, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            state = setup.load_state(path)
        self.assertEqual(state, {"network": "done", "controller": "skipped"})
        self.assertEqual(setup.first_unfinished(order, state), 2)

    def test_a_failed_step_counts_as_decided_but_a_missing_one_does_not(self):
        order = ["network", "controller"]
        self.assertEqual(setup.first_unfinished(order, {"network": "failed"}), 1)
        self.assertEqual(setup.first_unfinished(order, {}), 0)
        self.assertEqual(setup.first_unfinished(order, {"network": "done", "controller": "done"}), 2)

    def test_damaged_or_hostile_state_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "setup-state.json"
            self.assertEqual(setup.load_state(path), {})
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(setup.load_state(path), {})
            path.write_text(json.dumps({"steps": {"network": "done", "evil": "done", "display": "maybe"}}))
            self.assertEqual(setup.load_state(path), {"network": "done"})
            path.write_text(json.dumps(["network"]))
            self.assertEqual(setup.load_state(path), {})

    def test_summary_lists_every_step_with_a_status(self):
        rows = setup.summary(["network", "display"], {"network": "done"})
        self.assertEqual(rows, [("NETWORK", "DONE"), ("DISPLAY AND SOUND", "NOT DONE")])
        rows = setup.summary(["network", "display"], {"network": "failed", "display": "skipped"})
        self.assertEqual(rows, [("NETWORK", "FAILED"), ("DISPLAY AND SOUND", "SKIPPED")])


class MarkerTest(unittest.TestCase):
    def test_marker_keeps_the_existing_format_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / "state" / "setup-complete"
            setup.write_complete(marker)
            self.assertEqual(marker.read_text(), "1\n")
            self.assertEqual(marker.stat().st_mode & 0o777, 0o640)
            self.assertEqual(marker.parent.stat().st_mode & 0o777, 0o750)


class BigDigitsTest(unittest.TestCase):
    def test_every_digit_is_five_rows_of_equal_width_and_distinct(self):
        seen = set()
        for digit in "0123456789":
            rows = setup.render_big(digit)
            self.assertEqual(len(rows), 5)
            self.assertEqual(len({len(row) for row in rows}), 1)
            self.assertTrue(any("#" in row for row in rows))
            seen.add(tuple(rows))
        self.assertEqual(len(seen), 10)

    def test_rendering_spaces_digits_and_scales_horizontally(self):
        one = setup.render_big("1")
        two = setup.render_big("12")
        self.assertEqual(len(two[0]), 2 * len(one[0]) + setup.BIG_GAP)
        wide = setup.render_big("1", scale_x=2)
        self.assertEqual(len(wide[0]), 2 * len(one[0]))
        self.assertEqual(wide[1], "".join(ch * 2 for ch in one[1]))

    def test_glyph_characters_can_be_replaced(self):
        rows = setup.render_big("0", on="█")
        self.assertIn("█", "".join(rows))
        self.assertNotIn("#", "".join(rows))

    def test_the_zero_glyph_looks_like_a_zero(self):
        self.assertEqual(
            setup.render_big("0"),
            [" ### ", "#   #", "#   #", "#   #", " ### "],
        )

    def test_only_digits_can_be_drawn_and_width_is_reported(self):
        self.assertTrue(setup.can_render_big("048291"))
        self.assertFalse(setup.can_render_big("12a"))
        self.assertFalse(setup.can_render_big(""))
        with self.assertRaises(ValueError):
            setup.render_big("1x")
        self.assertEqual(setup.big_width("482917", scale_x=2), len(setup.render_big("482917", scale_x=2)[0]))


class ControllerTableTest(unittest.TestCase):
    def test_instruction_table_covers_the_supported_controllers(self):
        labels = " ".join(item.label for item in setup.CONTROLLER_TYPES).upper()
        for word in ("XBOX", "DUALSHOCK", "DUALSENSE", "SWITCH PRO", "8BITDO"):
            self.assertIn(word, labels)
        text = {item.id: " ".join(item.instructions).upper() for item in setup.CONTROLLER_TYPES}
        self.assertIn("PAIR BUTTON", text["xbox"])
        self.assertIn("SHARE + PS", text["ds4"])
        self.assertIn("CREATE + PS", text["dualsense"])
        self.assertIn("SYNC BUTTON", text["switch"])
        self.assertIn("PAIRING MODE", text["other"])
        for item in setup.CONTROLLER_TYPES:
            self.assertTrue(item.instructions)

    def test_candidates_hide_connected_and_unnamed_devices_and_rank_name_matches_first(self):
        devices = [
            {"path": "/a", "alias": "Living Room TV", "paired": False, "connected": False, "rssi": -30},
            {"path": "/b", "alias": "Xbox Wireless Controller", "paired": False, "connected": False, "rssi": -70},
            {"path": "/c", "alias": "AA-BB-CC-DD-EE-FF", "paired": False, "connected": False, "rssi": -20},
            {"path": "/d", "alias": "Xbox Wireless Controller", "paired": True, "connected": True, "rssi": -10},
            {"path": "/e", "alias": "Xbox Wireless Controller", "paired": True, "connected": False, "rssi": -60},
        ]
        ranked = setup.rank_candidates(devices, "xbox")
        self.assertEqual([item["path"] for item in ranked], ["/e", "/b", "/a"])

    def test_dualsense_and_ds4_both_match_the_generic_playstation_name(self):
        pad = {"path": "/p", "alias": "Wireless Controller", "paired": False, "connected": False, "rssi": -50}
        other = {"path": "/o", "alias": "Keyboard K380", "paired": False, "connected": False, "rssi": -40}
        for kind in ("ds4", "dualsense"):
            self.assertEqual(setup.rank_candidates([other, pad], kind)[0]["path"], "/p")


class InputDeviceTest(unittest.TestCase):
    SAMPLE = """I: Bus=0005 Vendor=045e Product=0b13 Version=0517
N: Name="Xbox Wireless Controller"
P: Phys=a0:b1:c2:d3:e4:f5
S: Sysfs=/devices/virtual/misc/uhid/0005:045E:0B13.0001/input/input7
U: Uniq=aa:bb:cc:dd:ee:ff
H: Handlers=event7 js0
B: PROP=0

I: Bus=0011 Vendor=0001 Product=0001 Version=ab41
N: Name="AT Translated Set 2 keyboard"
P: Phys=isa0060/serio0/input0
U: Uniq=
H: Handlers=sysrq kbd event3 leds
B: PROP=0

I: Bus=0003 Vendor=054c Product=09cc Version=8111
N: Name="Wireless Controller"
U: Uniq=
H: Handlers=event9 js1
"""

    def test_gamepads_are_the_devices_with_a_joystick_handler(self):
        devices = setup.parse_input_devices(self.SAMPLE)
        self.assertEqual([item.name for item in devices if item.gamepad],
                         ["Xbox Wireless Controller", "Wireless Controller"])
        self.assertEqual(devices[0].event, "/dev/input/event7")
        self.assertEqual(devices[0].uniq, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(devices[1].event, "/dev/input/event3")
        self.assertFalse(devices[1].gamepad)

    def test_a_paired_controller_is_found_by_its_bluetooth_address(self):
        devices = setup.parse_input_devices(self.SAMPLE)
        found = setup.find_controller(devices, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(found.event, "/dev/input/event7")
        self.assertIsNone(setup.find_controller(devices, "11:22:33:44:55:66"))

    def test_new_gamepads_are_those_not_present_before(self):
        before = setup.parse_input_devices(self.SAMPLE)
        after = setup.parse_input_devices(
            self.SAMPLE + "\nN: Name=\"Pro Controller\"\nU: Uniq=11:22:33:44:55:66\nH: Handlers=event12 js2\n"
        )
        new = setup.new_gamepads(before, after)
        self.assertEqual([item.name for item in new], ["Pro Controller"])


class ButtonWatchTest(unittest.TestCase):
    class Event:
        def __init__(self, kind, code, value):
            self.type, self.code, self.value = kind, code, value

    class Device:
        def __init__(self, batches):
            self.batches = list(batches)
            self.closed = False

        def read(self):
            return self.batches.pop(0) if self.batches else []

        def fileno(self):
            return 99

        def close(self):
            self.closed = True

    def test_a_press_is_the_south_button_going_down(self):
        event = self.Event
        device = self.Device([[event(3, 0, 5)], [event(1, 304, 0)], [event(1, 305, 1)], [event(1, 304, 1)]])
        clock = iter(range(100))
        pressed = setup.wait_for_a_button(
            device, 10, clock=lambda: next(clock), wait=lambda _device, _seconds: True,
            south=304, key_event=1,
        )
        self.assertTrue(pressed)
        self.assertTrue(device.closed)

    def test_no_press_before_the_deadline_is_a_timeout(self):
        device = self.Device([])
        clock = iter(range(100))
        self.assertFalse(setup.wait_for_a_button(
            device, 3, clock=lambda: next(clock), wait=lambda _device, _seconds: False,
            south=304, key_event=1,
        ))
        self.assertTrue(device.closed)


class NetworkParsingTest(unittest.TestCase):
    def test_terse_lines_split_on_unescaped_colons_only(self):
        self.assertEqual(setup.split_terse(r"a\:b:c\\d::e"), ["a:b", "c\\d", "", "e"])

    def test_devices_and_wired_detection(self):
        output = (
            "enp2s0:ethernet:connected\nwlan0:wifi:disconnected\n"
            "lo:loopback:connected (externally)\np2p-dev-wlan0:wifi-p2p:disconnected\n"
        )
        devices = setup.parse_devices(output)
        self.assertTrue(setup.wired_connected(devices))
        self.assertEqual(setup.wifi_device(devices), "wlan0")
        offline = setup.parse_devices("enp2s0:ethernet:unavailable\nwlan0:wifi:connected\n")
        self.assertFalse(setup.wired_connected(offline))
        self.assertIsNone(setup.wifi_device(setup.parse_devices("enp2s0:ethernet:connected\n")))

    def test_wifi_list_is_deduplicated_sorted_and_classified(self):
        output = "\n".join([
            "*:HomeNet:71:WPA2",
            " :HomeNet:40:WPA2",
            " :Cafe Guest:88:",
            " ::65:WPA2",
            " :Office\\:5G:55:WPA2 802.1X",
            " :Modern:60:WPA3",
            " :Mixed:30:WPA2 WPA3",
        ])
        networks = setup.parse_wifi_list(output)
        self.assertEqual([n.ssid for n in networks], ["Cafe Guest", "HomeNet", "Modern", "Office:5G", "Mixed"])
        by_name = {n.ssid: n for n in networks}
        self.assertTrue(by_name["HomeNet"].in_use)
        self.assertEqual(by_name["HomeNet"].signal, 71)
        self.assertFalse(by_name["Cafe Guest"].secured)
        self.assertTrue(by_name["HomeNet"].secured)
        self.assertEqual(by_name["Modern"].key_mgmt, "sae")
        self.assertEqual(by_name["Mixed"].key_mgmt, "wpa-psk")
        self.assertEqual(by_name["HomeNet"].key_mgmt, "wpa-psk")
        self.assertFalse(by_name["Office:5G"].supported)
        self.assertTrue(by_name["HomeNet"].supported)

    def test_passphrase_rules(self):
        self.assertFalse(setup.valid_passphrase("short"))
        self.assertTrue(setup.valid_passphrase("eight888"))
        self.assertTrue(setup.valid_passphrase("x" * 63))
        self.assertFalse(setup.valid_passphrase("x" * 64))
        self.assertTrue(setup.valid_passphrase("ab" * 32))
        self.assertFalse(setup.valid_passphrase("café café café"))

    def test_wifi_settings_carry_the_password_only_in_the_security_section(self):
        network = setup.WifiNetwork("HomeNet", 70, "WPA2")
        settings = setup.wifi_settings(network, "correct horse", "11111111-2222-3333-4444-555555555555")
        self.assertEqual(settings["connection"]["type"], "802-11-wireless")
        self.assertEqual(settings["connection"]["uuid"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(settings["802-11-wireless"]["ssid"], b"HomeNet")
        self.assertEqual(settings["802-11-wireless-security"], {"key-mgmt": "wpa-psk", "psk": "correct horse"})
        rest = {key: value for key, value in settings.items() if key != "802-11-wireless-security"}
        self.assertNotIn("correct horse", repr(rest))

    def test_open_networks_have_no_security_section(self):
        settings = setup.wifi_settings(setup.WifiNetwork("Cafe", 70, ""), "", "u")
        self.assertNotIn("802-11-wireless-security", settings)
        self.assertNotIn("security", settings["802-11-wireless"])

    def test_default_gateway_is_read_from_the_routing_table(self):
        self.assertEqual(
            setup.parse_gateway("default via 192.168.1.1 dev enp2s0 proto dhcp metric 100\n"), "192.168.1.1"
        )
        self.assertEqual(setup.parse_gateway(""), "")
        self.assertEqual(setup.parse_gateway("default dev ppp0 scope link\n"), "")


class ConnectivityTest(unittest.TestCase):
    def check(self, gateway="192.168.1.1", ping=True, dns=True, http="ok"):
        return setup.check_connectivity(
            gateway=lambda: gateway, ping=lambda _host: ping, resolves=lambda: dns, fetch=lambda: http,
        )

    def test_all_good(self):
        result = self.check()
        self.assertTrue(result.lan and result.dns and result.internet)
        self.assertEqual(result.gateway, "192.168.1.1")
        self.assertEqual(result.headline, "ONLINE")
        self.assertTrue(result.usable)

    def test_no_gateway_means_no_local_network(self):
        result = self.check(gateway="", dns=False, http="")
        self.assertFalse(result.lan)
        self.assertEqual(result.headline, "NO LOCAL NETWORK")
        self.assertFalse(result.usable)

    def test_unreachable_gateway(self):
        result = self.check(ping=False, dns=False, http="")
        self.assertFalse(result.lan)
        self.assertFalse(result.usable)

    def test_lan_without_internet_is_usable_for_streaming(self):
        result = self.check(dns=False, http="")
        self.assertTrue(result.lan)
        self.assertFalse(result.internet)
        self.assertEqual(result.headline, "LOCAL NETWORK ONLY")
        self.assertTrue(result.usable)

    def test_a_login_page_is_reported_separately(self):
        result = self.check(http="portal")
        self.assertFalse(result.internet)
        self.assertEqual(result.headline, "LOGIN PAGE BLOCKS THE INTERNET")

    def test_lines_show_every_check(self):
        lines = self.check(dns=False, http="").lines()
        self.assertEqual(len(lines), 3)
        self.assertIn("192.168.1.1", lines[0])
        self.assertIn("OK", lines[0])
        self.assertIn("FAILED", lines[1])
        self.assertIn("FAILED", lines[2])


class DisplayAndSoundLogicTest(unittest.TestCase):
    def modes(self):
        import moonlightos_display as display

        return (
            display.Mode(1920, 1080, 60000, current=True),
            display.Mode(3840, 2160, 60000, preferred=True),
            display.Mode(3840, 2160, 30000),
            display.Mode(1280, 720, 60000),
            display.Mode(720, 480, 60000),
            display.Mode(1920, 1080, 119880),
        )

    def test_choices_put_native_first_label_flags_and_drop_tiny_modes(self):
        choices = setup.display_choices(self.modes())
        labels = [label for label, _resolution, _mhz in choices]
        self.assertTrue(labels[0].startswith("3840x2160  60 HZ"))
        self.assertIn("NATIVE", labels[0])
        self.assertTrue(any("CURRENT" in label and label.startswith("1920x1080  60 HZ") for label in labels))
        self.assertFalse(any(label.startswith("720x480") for label in labels))
        self.assertEqual(choices[0][1:], ("3840x2160", 60000))
        self.assertEqual(len(labels), len(set(labels)))

    def test_choices_fall_back_to_every_mode_when_all_are_small(self):
        import moonlightos_display as display

        choices = setup.display_choices((display.Mode(800, 600, 60000, current=True),))
        self.assertEqual(len(choices), 1)

    def sinks(self, *names, default=None):
        import moonlightos_audio as audio

        return [audio.Sink(index + 1, name, name == default) for index, name in enumerate(names)]

    def test_hdmi_and_displayport_outputs_are_tried_before_everything_else(self):
        sinks = self.sinks(
            "Built-in Audio Analog Stereo", "Navi HDMI/DP Audio Digital Stereo (HDMI 3)",
            "USB Headset", "Built-in Audio Digital Stereo (DisplayPort)",
        )
        ordered = setup.sound_candidates(sinks)
        self.assertEqual([sink.id for sink in ordered], [2, 4, 1, 3])

    def test_a_default_hdmi_output_is_tried_first(self):
        sinks = self.sinks("HDMI 1", "HDMI 2", default="HDMI 2")
        self.assertEqual([sink.id for sink in setup.sound_candidates(sinks)], [2, 1])

    def test_no_video_output_means_the_order_is_unchanged(self):
        sinks = self.sinks("Analog", "USB")
        self.assertEqual([sink.id for sink in setup.sound_candidates(sinks)], [1, 2])


class StreamingLogicTest(unittest.TestCase):
    def test_pairing_pin_is_four_digits_and_padded(self):
        with mock.patch.object(setup.secrets, "randbelow", return_value=42):
            self.assertEqual(setup.new_pairing_pin(), "0042")
        for _ in range(50):
            self.assertRegex(setup.new_pairing_pin(), r"^[0-9]{4}$")

    def test_host_names_are_validated_before_reaching_a_command_line(self):
        for good in ("192.168.1.20", "gaming-pc", "gaming-pc.lan", "GAMING_PC"):
            self.assertEqual(setup.valid_host(good), good)
        for bad in ("", "a b", "host;rm", "-rf", "x" * 254, "$(id)", "host/path", "a\nb"):
            self.assertIsNone(setup.valid_host(bad))

    def test_pair_command_uses_moonlights_pin_option(self):
        self.assertEqual(setup.moonlight_pair_arguments("192.168.1.20", "0042"), "pair 192.168.1.20 --pin 0042")

    def test_moonlight_config_shows_which_hosts_are_paired(self):
        text = "\n".join([
            "[General]", "width=1920",
            "[hosts]",
            "1\\hostname=DESKTOP-ABC",
            "1\\srvcert=@ByteArray(-----BEGIN CERTIFICATE-----\\nMIIB\\n-----END CERTIFICATE-----)",
            "2\\hostname=OLD-PC",
            "2\\srvcert=@ByteArray()",
            "3\\hostname=NAS",
            "size=3",
        ])
        self.assertEqual(setup.paired_hosts(text), ["DESKTOP-ABC"])
        self.assertEqual(setup.paired_hosts(""), [])
        self.assertEqual(setup.paired_hosts("[General]\nwidth=1\n"), [])



# --- wizard flows (fake UI, fake system, fake Bluetooth service) ----------

import moonlightos_audio as audio
import moonlightos_bluetooth as bt
import moonlightos_display as display


class FakeUI:
    """Scripted answers: an int picks that row, a string picks the row containing it, None is Esc."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.screens = []
        self.flushed = 0
        self.waits = []

    def menu(self, title, lines, choices, *, big=None, selected=0):
        self.screens.append({"title": title, "lines": list(lines), "choices": list(choices), "big": big})
        if not self.answers:
            raise AssertionError(f"no scripted answer for screen {title!r} {choices!r}")
        answer = self.answers.pop(0)
        if isinstance(answer, str):
            return next(index for index, choice in enumerate(choices) if answer in choice)
        return answer

    def status(self, title, lines, *, big=None):
        self.screens.append({"title": title, "lines": list(lines), "choices": [], "big": big})

    def wait(self, title, lines, done, timeout, *, big=None):
        self.screens.append({"title": title, "lines": list(lines), "choices": [], "big": big})
        self.waits.append(title)
        return bool(done())

    def flush(self):
        self.flushed += 1

    def titles(self):
        return [screen["title"] for screen in self.screens]

    def text(self):
        return "\n".join(line for screen in self.screens for line in screen["lines"] + screen["choices"])


class FakeSystem:
    def __init__(self, **values):
        self.cec = False
        self.devices = [setup.NetworkDevice("enp2s0", "ethernet", "unavailable"), setup.NetworkDevice("wlan0", "wifi", "disconnected")]
        self.networks = []
        self.connect_results = [(True, "")]
        self.connected = []
        self.results = [setup.Connectivity("192.168.1.1", True, True, True, False)]
        self.inputs = []
        self.after_pairing = []
        self.pressed = True
        self.modes = []
        self.sink_list = []
        self.default_sinks = []
        self.hosts = []
        self.__dict__.update(values)

    def cec_present(self):
        return self.cec

    def network_devices(self):
        return self.devices

    def wifi_networks(self):
        self.scans = getattr(self, "scans", 0) + 1
        return self.networks

    def connect_wifi(self, network, password):
        self.connected.append((network.ssid, password))
        return self.connect_results.pop(0) if len(self.connect_results) > 1 else self.connect_results[0]

    def connectivity(self):
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def input_devices(self):
        return self.after_pairing if getattr(self, "paired_now", False) else self.inputs

    def confirm_button(self, device, timeout):
        self.confirmed = device
        return self.pressed

    def display_modes(self):
        return self.modes

    def sinks(self):
        return self.sink_list

    def set_default_sink(self, sink_id):
        self.default_sinks.append(sink_id)

    def paired_hosts(self):
        return self.hosts


class FakeBluetooth:
    def __init__(self, devices=(), states=("completed",), prompt=None, powered=True, adapter=True, down=False):
        self.down = down
        self.started = False
        self.devices = list(devices)
        self.states = list(states)
        self.prompt = prompt
        self.powered = powered
        self.adapter = adapter
        self.requests = []
        self.system = None

    def request(self, command, **fields):
        self.requests.append((command, fields))
        if command == "pair":
            self.started = True
            if self.system is not None:
                self.system.paired_now = True
        return {"ok": True, "operation_id": "op1"}

    def snapshot(self):
        if self.down:
            raise bt.BluetoothError("BLUETOOTH SERVICE UNAVAILABLE")
        operations, prompt = [], None
        if self.started:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            operations = [{"id": "op1", "state": state, "error": ""}]
            if state == "working" and self.prompt:
                prompt = dict(self.prompt, operation_id="op1", id="p1")
        return {
            "adapter": {"powered": self.powered} if self.adapter else None,
            "devices": self.devices,
            "operations": operations,
            "prompt": prompt,
        }

    def sent(self, command):
        return [fields for name, fields in self.requests if name == command]


XBOX = {"path": "/dev/xbox", "address": "AA:BB:CC:DD:EE:FF", "alias": "Xbox Wireless Controller",
        "paired": False, "connected": False, "rssi": -50}
PAD = setup.InputDevice("Xbox Wireless Controller", "aa:bb:cc:dd:ee:ff", "/dev/input/event7", True)
HOME_NET = setup.WifiNetwork("HomeNet", 80, "WPA2")


class WizardTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = pathlib.Path(directory.name)
        self.marker = self.dir / "setup-complete"
        self.state_path = self.dir / "setup-state.json"
        self.calls = []
        self.texts = []

    def actions(self, **extra):
        def text(title, prompt, limit, *, masked=False):
            self.calls.append(("text", title, masked))
            return self.texts.pop(0) if self.texts else None

        actions = {
            "text": text,
            "display": lambda resolution, mhz: self.calls.append(("display", resolution, mhz)) or True,
            "tone": lambda: self.calls.append(("tone",)) or True,
            "launch": lambda app_id: self.calls.append(("launch", app_id)) or True,
            "pair_moonlight": lambda host, pin: self.calls.append(("pair", host, pin)) or True,
            "applications": lambda: self.calls.append(("applications",)),
        }
        actions.update(extra)
        return actions

    def wizard(self, ui, system=None, bluetooth=None, **extra):
        self.ui = ui
        self.system = system or FakeSystem()
        return setup.SetupWizard(
            ui, self.actions(**extra), self.system, bluetooth_client=bluetooth,
            marker=self.marker, state_path=self.state_path,
        )

    def state(self):
        return setup.load_state(self.state_path)


class WizardLifecycleTest(WizardTestCase):
    SKIP_ALL = ["START SETUP"] + [None] * 7

    def test_a_finished_setup_does_not_run_again_unless_forced(self):
        self.marker.write_text("1\n")
        ui = FakeUI()
        self.assertFalse(self.wizard(ui).run())
        self.assertEqual(ui.screens, [])

    def test_welcome_explains_the_wizard_and_skip_setup_writes_the_marker(self):
        ui = FakeUI("SKIP SETUP")
        self.assertTrue(self.wizard(ui).run())
        self.assertTrue(self.marker.exists())
        screen = ui.screens[0]
        self.assertEqual(screen["title"], "WELCOME TO MOONLIGHTOS")
        text = " ".join(screen["lines"]).upper()
        self.assertIn("SKIP", text)
        self.assertIn("SETTINGS > SETUP WIZARD", text)

    def test_exit_and_escape_on_the_welcome_leave_setup_for_the_next_boot(self):
        for answer in ("EXIT TO LAUNCHER", None):
            with self.subTest(answer=answer):
                ui = FakeUI(answer)
                self.assertTrue(self.wizard(ui).run())
                self.assertFalse(self.marker.exists())

    def test_escape_on_the_done_screen_never_leaves_setup_unsaved(self):
        ui = FakeUI(*self.SKIP_ALL, None, None, "FINISH")
        self.assertTrue(self.wizard(ui).run())
        self.assertTrue(self.marker.exists())
        self.assertEqual(ui.titles().count("SETUP COMPLETE"), 3)

    def test_finish_writes_the_marker_and_the_summary_lists_each_step(self):
        ui = FakeUI(*self.SKIP_ALL, "FINISH")
        self.wizard(ui).run()
        done = [screen for screen in ui.screens if screen["title"] == "SETUP COMPLETE"][0]
        text = "\n".join(done["lines"])
        for title in ("NETWORK", "CONTROLLER", "DISPLAY AND SOUND", "STREAMING PC", "TAILSCALE", "CHIAKI-NG", "MORE APPS"):
            self.assertIn(title, text)
        self.assertIn("SKIPPED", text)

    def test_skipping_a_step_is_remembered(self):
        self.wizard(FakeUI(*self.SKIP_ALL, "FINISH")).run()
        self.assertEqual(set(self.state().values()), {"skipped"})
        self.assertEqual(len(self.state()), 7)

    def test_leaving_midway_resumes_at_the_first_unfinished_step(self):
        setup.save_state({"network": "done", "controller": "skipped"}, self.state_path)
        ui = FakeUI("CONTINUE SETUP", None, None, None, None, None, "FINISH")
        self.wizard(ui).run()
        self.assertEqual(ui.titles()[0], "WELCOME BACK")
        steps = [title for title in ui.titles() if title in setup.TITLES.values()]
        self.assertEqual(steps[0], "DISPLAY AND SOUND")
        self.assertNotIn("NETWORK", steps)

    def test_start_over_forgets_earlier_progress(self):
        setup.save_state({"network": "done"}, self.state_path)
        ui = FakeUI("START OVER", *([None] * 7), "FINISH")
        self.wizard(ui).run()
        self.assertEqual([t for t in ui.titles() if t in setup.TITLES.values()][0], "NETWORK")

    def test_rerun_from_settings_starts_fresh_even_with_a_marker(self):
        self.marker.write_text("1\n")
        setup.save_state({"network": "done"}, self.state_path)
        ui = FakeUI(*self.SKIP_ALL, "FINISH")
        self.assertTrue(self.wizard(ui).run(force=True))
        self.assertEqual(ui.titles()[0], "WELCOME TO MOONLIGHTOS")
        self.assertEqual(self.state()["network"], "skipped")

    def test_the_done_screen_can_redo_skipped_steps(self):
        ui = FakeUI(*self.SKIP_ALL, "REDO", None, None, None, None, None, None, None, "FINISH")
        self.wizard(ui).run()

        self.assertGreaterEqual(ui.titles().count("NETWORK"), 2)

    def test_the_tv_step_needs_a_cec_device_and_a_tv_screen(self):
        skip_all = ["START SETUP"] + [None] * 8
        ui = FakeUI(*skip_all, "FINISH")
        self.wizard(ui, FakeSystem(cec=True), tv=lambda: None).run()
        self.assertIn("TV CONTROL", ui.titles())
        ui = FakeUI(*self.SKIP_ALL, "FINISH")
        self.wizard(ui, FakeSystem(cec=True)).run()
        self.assertNotIn("TV CONTROL", ui.titles())
        ui = FakeUI(*self.SKIP_ALL, "FINISH")
        self.wizard(ui, FakeSystem(cec=False), tv=lambda: None).run()
        self.assertNotIn("TV CONTROL", ui.titles())

    def test_the_done_screen_says_why_tv_control_is_missing(self):
        ui = FakeUI(*self.SKIP_ALL, "FINISH")
        self.wizard(ui, FakeSystem(cec=False), tv=lambda: None).run()
        done = [screen for screen in ui.screens if screen["title"] == "SETUP COMPLETE"][0]
        self.assertIn("TV CONTROL: NO CEC ADAPTER FOUND", "\n".join(done["lines"]))

    def test_the_tv_screen_is_opened_and_counts_as_done(self):
        opened = []
        ui = FakeUI("START SETUP", None, None, None, None, "OPEN", None, None, None, "FINISH")
        self.wizard(ui, FakeSystem(cec=True), tv=lambda: opened.append(1)).run()
        self.assertEqual(opened, [1])
        self.assertEqual(self.state()["tv"], "done")


class NetworkFlowTest(WizardTestCase):
    def run_network(self, ui, system):
        return self.wizard(ui, system).step_network()

    def test_a_cable_is_detected_and_checked_without_asking_anything(self):
        system = FakeSystem(devices=[setup.NetworkDevice("enp2s0", "ethernet", "connected")])
        ui = FakeUI("CONTINUE")
        self.assertEqual(self.run_network(ui, system), "done")
        text = ui.text()
        self.assertIn("CONNECTED BY CABLE", text)
        self.assertIn("ONLINE", text)
        self.assertEqual([call for call in self.calls if call[0] == "text"], [])

    def test_wifi_password_is_asked_masked_and_only_given_to_the_connect_call(self):
        self.texts = ["hunter2-hunter2"]
        system = FakeSystem(networks=[HOME_NET])
        ui = FakeUI("HomeNet", "CONTINUE")
        self.assertEqual(self.run_network(ui, system), "done")
        self.assertEqual(system.connected, [("HomeNet", "hunter2-hunter2")])
        self.assertIn(("text", "WI-FI PASSWORD", True), self.calls)
        self.assertNotIn("hunter2", ui.text())
        self.assertIn("CONNECTED TO HomeNet", ui.text())

    def test_an_open_network_needs_no_password(self):
        system = FakeSystem(networks=[setup.WifiNetwork("Cafe", 70, "")])
        ui = FakeUI("Cafe", "CONTINUE")
        self.assertEqual(self.run_network(ui, system), "done")
        self.assertEqual(system.connected, [("Cafe", "")])
        self.assertEqual([call for call in self.calls if call[0] == "text"], [])

    def test_a_wrong_password_is_reported_and_can_be_retyped(self):
        self.texts = ["wrong-password", "right-password"]
        system = FakeSystem(networks=[HOME_NET], connect_results=[(False, "COULD NOT JOIN: CHECK THE PASSWORD"), (True, "")])
        ui = FakeUI("HomeNet", "TRY AGAIN", "CONTINUE")
        self.assertEqual(self.run_network(ui, system), "done")
        self.assertIn("CHECK THE PASSWORD", ui.text())
        self.assertEqual([item[1] for item in system.connected], ["wrong-password", "right-password"])

    def test_too_short_passwords_are_rejected_before_connecting(self):
        self.texts = ["short", "long-enough-pass"]
        system = FakeSystem(networks=[HOME_NET])
        ui = FakeUI("HomeNet", "OK", "CONTINUE")
        self.assertEqual(self.run_network(ui, system), "done")
        self.assertEqual([item[1] for item in system.connected], ["long-enough-pass"])

    def test_work_networks_are_refused_politely(self):
        system = FakeSystem(networks=[setup.WifiNetwork("Corp", 90, "WPA2 802.1X")])
        ui = FakeUI("Corp", "OK", None)
        self.assertEqual(self.run_network(ui, system), "skipped")
        self.assertEqual(system.connected, [])
        self.assertIn("NOT SUPPORTED", ui.text())

    def test_escape_on_the_password_goes_back_to_the_network_list(self):
        self.texts = [None]
        ui = FakeUI("HomeNet", None)
        self.assertEqual(self.run_network(ui, FakeSystem(networks=[HOME_NET])), "skipped")

    def test_no_local_network_is_a_failure_the_user_may_continue_past(self):
        system = FakeSystem(
            devices=[setup.NetworkDevice("enp2s0", "ethernet", "connected")],
            results=[setup.Connectivity("", False, False, False, False)],
        )
        ui = FakeUI("CONTINUE ANYWAY")
        self.assertEqual(self.run_network(ui, system), "failed")
        self.assertIn("NO LOCAL NETWORK", ui.text())

    def test_lan_without_internet_is_still_done_and_says_so(self):
        system = FakeSystem(
            devices=[setup.NetworkDevice("enp2s0", "ethernet", "connected")],
            results=[setup.Connectivity("192.168.1.1", True, False, False, False)],
        )
        ui = FakeUI("CONTINUE")
        self.assertEqual(self.run_network(ui, system), "done")
        self.assertIn("LOCAL NETWORK ONLY", ui.text())

    def test_no_wifi_device_means_no_wifi_list_is_ever_offered(self):
        system = FakeSystem(devices=[setup.NetworkDevice("enp2s0", "ethernet", "unavailable")], networks=[HOME_NET])
        ui = FakeUI("SKIP")
        self.assertEqual(self.run_network(ui, system), "skipped")
        self.assertFalse(getattr(system, "scans", 0))
        self.assertIn("NO WI-FI ADAPTER", ui.text())
        self.assertIn("PLUG IN A NETWORK CABLE", ui.text())


class ControllerFlowTest(WizardTestCase):
    def run_controller(self, ui, bluetooth, system=None):
        system = system or FakeSystem(after_pairing=[PAD])
        bluetooth.system = system
        return self.wizard(ui, system, bluetooth).step_controller()

    def test_instructions_for_the_chosen_controller_are_shown_large_and_clear(self):
        ui = FakeUI("PAIR", "XBOX", None, "SKIP")
        self.run_controller(ui, FakeBluetooth([]), FakeSystem())
        text = ui.text().upper()
        self.assertIn("PAIR BUTTON", text)
        self.assertIn("XBOX BUTTON FLASHES FAST", text)
        self.assertIn("XBOX WIRELESS CONTROLLER", text)
        self.assertIn("DUALSENSE", text)

    def test_a_controller_is_found_paired_and_confirmed_with_the_a_button(self):
        bluetooth = FakeBluetooth([XBOX], states=["working", "completed"])
        ui = FakeUI("PAIR", "XBOX", "OK")
        self.assertEqual(self.run_controller(ui, bluetooth), "done")
        self.assertEqual(bluetooth.sent("pair"), [{"device": "/dev/xbox"}])
        self.assertIn(("start_scan", {}), bluetooth.requests)
        self.assertIn("PRESS A", ui.text().upper())

    def test_the_bluetooth_adapter_is_switched_on_when_needed(self):
        bluetooth = FakeBluetooth([XBOX], powered=False)
        ui = FakeUI("PAIR", "XBOX", "OK")
        self.run_controller(ui, bluetooth)
        self.assertEqual(bluetooth.sent("set_power"), [{"powered": True}])

    def test_a_pairing_number_is_drawn_large(self):
        prompt = {"kind": "display_passkey", "passkey": "482917"}
        bluetooth = FakeBluetooth([XBOX], states=["working", "working", "completed"], prompt=prompt)
        ui = FakeUI("PAIR", "XBOX", "OK")
        self.assertEqual(self.run_controller(ui, bluetooth), "done")
        self.assertIn("482917", [screen["big"] for screen in ui.screens])

    def test_a_confirmation_code_is_large_and_the_answer_goes_back(self):
        prompt = {"kind": "confirmation", "passkey": "123456"}
        bluetooth = FakeBluetooth([XBOX], states=["working", "completed"], prompt=prompt)
        ui = FakeUI("PAIR", "XBOX", "YES", "OK")
        self.assertEqual(self.run_controller(ui, bluetooth), "done")
        self.assertIn("123456", [screen["big"] for screen in ui.screens])
        self.assertEqual(bluetooth.sent("agent_reply"), [{"prompt_id": "p1", "accepted": True}])

    def test_a_failed_pairing_can_be_left_as_failed(self):
        bluetooth = FakeBluetooth([XBOX], states=["failed"])
        ui = FakeUI("PAIR", "XBOX", "CONTINUE ANYWAY")
        self.assertEqual(self.run_controller(ui, bluetooth), "failed")

    def test_no_a_press_is_a_failure_and_the_stray_enter_is_flushed(self):
        bluetooth = FakeBluetooth([XBOX])
        system = FakeSystem(after_pairing=[PAD], pressed=False)
        ui = FakeUI("PAIR", "XBOX", "CONTINUE ANYWAY")
        self.assertEqual(self.run_controller(ui, bluetooth, system), "failed")
        self.assertGreaterEqual(ui.flushed, 1)

    def test_the_a_press_is_watched_on_the_device_with_the_paired_address(self):
        bluetooth = FakeBluetooth([XBOX])
        system = FakeSystem(after_pairing=[PAD])
        self.run_controller(FakeUI("PAIR", "XBOX", "OK"), bluetooth, system)
        self.assertEqual(system.confirmed, PAD)

    def test_a_controller_that_is_already_connected_is_enough(self):
        system = FakeSystem(inputs=[PAD])
        ui = FakeUI("ENOUGH")
        self.assertEqual(self.wizard(ui, system, FakeBluetooth()).step_controller(), "done")
        self.assertIn("Xbox Wireless Controller", ui.text())

    HINT = "NO BLUETOOTH ADAPTER — PLUG IN A CONTROLLER BY USB"

    def test_without_an_adapter_pairing_is_not_offered_and_a_usb_pad_press_is_accepted(self):
        bluetooth = FakeBluetooth(adapter=False)
        system = FakeSystem(inputs=[PAD])
        ui = FakeUI("OK")
        self.assertEqual(self.wizard(ui, system, bluetooth).step_controller(), "done")
        self.assertIn(self.HINT, ui.text())
        self.assertNotIn("PAIR A WIRELESS CONTROLLER", ui.text())
        self.assertEqual(bluetooth.requests, [])
        self.assertEqual(system.confirmed, PAD)

    def test_without_an_adapter_or_a_pad_it_waits_for_one_and_can_be_skipped(self):
        ui = FakeUI("SKIP")
        wizard = self.wizard(ui, FakeSystem(), FakeBluetooth(adapter=False))
        self.assertEqual(wizard.step_controller(), "skipped")
        self.assertEqual(ui.waits, ["CONTROLLER"])
        self.assertIn(self.HINT, ui.text())

    def test_a_stopped_bluetooth_service_or_no_client_is_treated_the_same(self):
        for bluetooth in (FakeBluetooth(down=True), None):
            with self.subTest(bluetooth=bluetooth):
                ui = FakeUI("SKIP")
                self.assertEqual(self.wizard(ui, FakeSystem(), bluetooth).step_controller(), "skipped")
                self.assertIn("PLUG IN A CONTROLLER BY USB", ui.text())

    def test_a_usb_pad_press_that_never_comes_can_be_continued_past(self):
        system = FakeSystem(inputs=[PAD], pressed=False)
        ui = FakeUI("CONTINUE ANYWAY")
        self.assertEqual(self.wizard(ui, system, FakeBluetooth(adapter=False)).step_controller(), "failed")


class DisplayAndSoundFlowTest(WizardTestCase):
    MODES = (
        display.Mode(1920, 1080, 60000, current=True),
        display.Mode(3840, 2160, 60000, preferred=True),
    )

    def run_display(self, ui, **values):
        values.setdefault("modes", self.MODES)
        values.setdefault("sink_list", [audio.Sink(1, "Analog"), audio.Sink(2, "HDMI 1")])
        return self.wizard(ui, FakeSystem(**values)).step_display()

    def test_the_chosen_mode_goes_through_the_existing_preview_and_the_tone_is_confirmed(self):
        ui = FakeUI("3840x2160", "YES")
        self.assertEqual(self.run_display(ui), "done")
        self.assertIn(("display", "3840x2160", 60000), self.calls)
        self.assertIn(("tone",), self.calls)

    def test_sound_follows_the_display_so_hdmi_is_tried_first_and_made_the_default(self):
        ui = FakeUI("3840x2160", "YES")
        self.run_display(ui)
        self.assertEqual(self.system.default_sinks, [2])

    def test_a_silent_output_moves_on_to_the_next_one(self):
        ui = FakeUI("3840x2160", "NO", "YES")
        self.assertEqual(self.run_display(ui), "done")
        self.assertEqual(self.system.default_sinks, [2, 1])
        self.assertEqual(self.calls.count(("tone",)), 2)

    def test_when_nothing_plays_the_step_fails_but_can_continue(self):
        ui = FakeUI("3840x2160", "NO", "NO", "CONTINUE")
        self.assertEqual(self.run_display(ui), "failed")

    def test_a_rolled_back_mode_returns_to_the_list(self):
        results = iter([False, True])
        ui = FakeUI("3840x2160", "1920x1080", "YES")
        wizard = self.wizard(ui, FakeSystem(modes=self.MODES, sink_list=[audio.Sink(1, "HDMI")]),
                             display=lambda resolution, mhz: self.calls.append(("display", resolution)) or next(results))
        self.assertEqual(wizard.step_display(), "done")
        self.assertEqual([call for call in self.calls if call[0] == "display"], [("display", "3840x2160"), ("display", "1920x1080")])
        self.assertIn("NOT CHANGED", ui.text())

    def test_keeping_the_current_mode_still_checks_the_sound(self):
        ui = FakeUI("KEEP CURRENT", "YES")
        self.assertEqual(self.run_display(ui), "done")
        self.assertEqual([call for call in self.calls if call[0] == "display"], [])

    def test_skipping_both_skips_the_step(self):
        self.assertEqual(self.run_display(FakeUI(None, None)), "skipped")

    def test_without_a_sound_output_the_sound_part_is_explained_and_skipped(self):
        ui = FakeUI("OK", "KEEP CURRENT")
        self.assertEqual(self.run_display(ui, sink_list=[]), "done")
        self.assertIn("NO SOUND OUTPUT", ui.text())
        self.assertEqual(self.system.default_sinks, [])
        self.assertNotIn(("tone",), self.calls)

    def test_a_single_advertised_mode_is_reported_and_the_wizard_moves_on(self):
        only = (display.Mode(1920, 1080, 60000, current=True, preferred=True),)
        ui = FakeUI("OK", "YES")
        self.assertEqual(self.run_display(ui, modes=only), "done")
        self.assertIn("ONLY ONE DISPLAY MODE", ui.text())
        self.assertEqual([call for call in self.calls if call[0] == "display"], [])

    def test_no_display_query_result_is_reported_and_the_sound_is_still_checked(self):
        ui = FakeUI("OK", "YES")
        self.assertEqual(self.run_display(ui, modes=()), "done")
        self.assertIn("NO DISPLAY MODES", ui.text())
        self.assertIn(("tone",), self.calls)


class GatePureFunctionsTest(unittest.TestCase):
    def test_bluetooth_needs_a_running_service_and_an_adapter(self):
        self.assertEqual(setup.bluetooth_gate(None), (False, "BLUETOOTH SERVICE NOT RUNNING"))
        self.assertEqual(setup.bluetooth_gate({"adapter": None}), (False, "NO BLUETOOTH ADAPTER"))
        self.assertEqual(setup.bluetooth_gate({"adapter": {"powered": False}}), (True, ""))
        self.assertEqual(setup.bluetooth_gate({"adapter": {"powered": True}}), (True, ""))

    def test_the_network_mode_is_wired_wifi_or_none(self):
        cable = setup.NetworkDevice("enp2s0", "ethernet", "connected")
        unplugged = setup.NetworkDevice("enp2s0", "ethernet", "unavailable")
        wifi = setup.NetworkDevice("wlan0", "wifi", "disconnected")
        self.assertEqual(setup.network_mode([cable, wifi]), "wired")
        self.assertEqual(setup.network_mode([unplugged, wifi]), "wifi")
        self.assertEqual(setup.network_mode([unplugged]), "none")
        self.assertEqual(setup.network_mode([]), "none")

    def test_the_display_choice_is_offered_only_with_a_real_choice(self):
        one = (display.Mode(1920, 1080, 60000, current=True),)
        two = one + (display.Mode(1280, 720, 60000),)
        self.assertEqual(setup.display_gate(()), (False, "NO DISPLAY MODES AVAILABLE"))
        offered, reason = setup.display_gate(one)
        self.assertFalse(offered)
        self.assertIn("ONLY ONE DISPLAY MODE", reason)
        self.assertIn("1920x1080", reason)
        self.assertEqual(setup.display_gate(two), (True, ""))

    def test_sound_needs_an_output(self):
        self.assertEqual(setup.sound_gate([]), (False, "NO SOUND OUTPUT FOUND — NOTHING TO TEST ON THIS PC"))
        self.assertEqual(setup.sound_gate([audio.Sink(1, "HDMI")]), (True, ""))

    def test_parts_combine_into_one_step_outcome(self):
        self.assertEqual(setup.combine("done", "skipped"), "done")
        self.assertEqual(setup.combine("done", "failed"), "failed")
        self.assertEqual(setup.combine("skipped", "skipped"), "skipped")
        self.assertEqual(setup.combine("skipped", "failed"), "failed")


class StreamingFlowTest(WizardTestCase):
    def test_moonlight_is_opened_to_find_the_pc_and_the_pairing_is_checked(self):
        system = FakeSystem(hosts=["DESKTOP-ABC"])
        ui = FakeUI("FIND MY GAMING PC", "OPEN MOONLIGHT")
        self.assertEqual(self.wizard(ui, system).step_streaming(), "done")
        self.assertIn(("launch", "moonlight"), self.calls)
        text = ui.text().upper()
        self.assertIn("SUNSHINE", text)
        self.assertIn("PIN", text)

    def test_no_pairing_after_moonlight_closes_is_a_failure(self):
        ui = FakeUI("FIND MY GAMING PC", "OPEN MOONLIGHT", "CONTINUE WITHOUT")
        self.assertEqual(self.wizard(ui, FakeSystem()).step_streaming(), "failed")

    def test_the_pin_is_made_here_shown_large_and_passed_to_moonlight(self):
        self.texts = ["192.168.1.20"]
        ui = FakeUI("PAIR WITH A PIN", "START PAIRING")
        system = FakeSystem(hosts=["DESKTOP-ABC"])
        with mock.patch.object(setup, "new_pairing_pin", return_value="0427"):
            self.assertEqual(self.wizard(ui, system).step_streaming(), "done")
        self.assertIn(("pair", "192.168.1.20", "0427"), self.calls)
        shown = [screen for screen in ui.screens if screen["big"] == "0427"]
        self.assertEqual(len(shown), 1)
        self.assertIn("47990", " ".join(shown[0]["lines"]))
        self.assertIn("192.168.1.20", " ".join(shown[0]["lines"]))

    def test_pairing_that_finished_before_the_client_was_ready_still_counts(self):
        # moonlight exits at once when the PIN was typed quickly; the launcher then
        # reports a failed start even though the host is paired.
        self.texts = ["192.168.1.20"]
        ui = FakeUI("PAIR WITH A PIN", "START PAIRING")
        system = FakeSystem(hosts=["DESKTOP-ABC"])
        wizard = self.wizard(ui, system, pair_moonlight=lambda host, pin: False)
        self.assertEqual(wizard.step_streaming(), "done")

    def test_an_unsafe_host_name_is_refused_and_asked_again(self):
        self.texts = ["bad host;rm", "gaming-pc"]
        ui = FakeUI("PAIR WITH A PIN", "OK", "START PAIRING")
        system = FakeSystem(hosts=["PC"])
        self.assertEqual(self.wizard(ui, system).step_streaming(), "done")
        self.assertEqual([call[1] for call in self.calls if call[0] == "pair"], ["gaming-pc"])

    def test_skipping_is_always_possible(self):
        self.assertEqual(self.wizard(FakeUI(None), FakeSystem()).step_streaming(), "skipped")
        self.assertEqual(self.wizard(FakeUI("SKIP"), FakeSystem()).step_streaming(), "skipped")


class OptionalStepsTest(WizardTestCase):
    def test_tailscale_and_chiaki_open_the_existing_apps(self):
        wizard = self.wizard(FakeUI("OPEN", "OPEN"))
        self.assertEqual(wizard.step_tailscale(), "done")
        self.assertEqual(wizard.step_chiaki_ng(), "done")
        self.assertEqual([call for call in self.calls if call[0] == "launch"], [("launch", "tailscale"), ("launch", "chiaki-ng")])

    def test_more_apps_opens_the_applications_screen(self):
        self.assertEqual(self.wizard(FakeUI("OPEN")).step_applications(), "done")
        self.assertIn(("applications",), self.calls)

    def test_an_app_that_fails_to_start_is_a_failed_step(self):
        wizard = self.wizard(FakeUI("OPEN"), launch=lambda app_id: False)
        self.assertEqual(wizard.step_tailscale(), "failed")

    def test_skip_and_escape_skip(self):
        wizard = self.wizard(FakeUI("SKIP", None))
        self.assertEqual(wizard.step_tailscale(), "skipped")
        self.assertEqual(wizard.step_chiaki_ng(), "skipped")

    def test_the_launchers_status_text_is_shown(self):
        ui = FakeUI("SKIP")
        wizard = self.wizard(ui)
        wizard.statuses = {"tailscale": lambda: "TAILSCALE DISCONNECTED"}
        wizard.step_tailscale()
        self.assertIn("TAILSCALE DISCONNECTED", ui.text())


# --- the real system backend, with only the boundaries mocked -------------

class SystemBackendTest(unittest.TestCase):
    def completed(self, stdout="", returncode=0):
        return mock.Mock(stdout=stdout, stderr="", returncode=returncode)

    def test_devices_and_wifi_come_from_nmcli_in_terse_c_locale(self):
        system = setup.System()
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            if args[:3] == ["nmcli", "-t", "-f"] and "device" in args and "wifi" not in args:
                return self.completed("enp2s0:ethernet:connected\n")
            return self.completed("*:HomeNet:71:WPA2\n")

        with mock.patch.object(setup.subprocess, "run", side_effect=run), mock.patch.object(setup.time, "sleep"):
            self.assertTrue(setup.wired_connected(system.network_devices()))
            self.assertEqual([item.ssid for item in system.wifi_networks()], ["HomeNet"])
        self.assertTrue(all(kwargs["env"]["LC_ALL"] == "C" and kwargs["timeout"] for _args, kwargs in calls))

    def test_connecting_hands_the_password_to_dbus_and_never_to_a_command(self):
        system = setup.System()
        seen = {}
        network = setup.WifiNetwork("HomeNet", 80, "WPA2")
        with mock.patch.object(setup.subprocess, "run") as run, \
                mock.patch.object(system, "_activate", side_effect=lambda settings, ifname: seen.update(settings=settings, ifname=ifname) or (True, "")), \
                mock.patch.object(setup.System, "wifi_interface", return_value="wlan0"):
            self.assertEqual(system.connect_wifi(network, "correct horse battery"), (True, ""))
        run.assert_not_called()
        self.assertEqual(seen["settings"]["802-11-wireless-security"]["psk"], "correct horse battery")
        self.assertEqual(seen["ifname"], "wlan0")

    def test_failure_messages_never_contain_the_password(self):
        system = setup.System()
        with mock.patch.object(system, "_activate", side_effect=RuntimeError("boom correct horse battery")), \
                mock.patch.object(setup.System, "wifi_interface", return_value="wlan0"):
            ok, message = system.connect_wifi(setup.WifiNetwork("HomeNet", 80, "WPA2"), "correct horse battery")
        self.assertFalse(ok)
        self.assertNotIn("correct horse battery", message)

    def test_connectivity_uses_the_route_ping_lookup_and_a_bounded_fetch(self):
        system = setup.System()
        seen = []

        def run(args, **kwargs):
            seen.append(args)
            if args[0] == "ip":
                return self.completed("default via 192.168.1.1 dev enp2s0\n")
            return self.completed("")

        body = mock.MagicMock()
        body.__enter__.return_value.read.return_value = b"NetworkManager is online\n"
        with mock.patch.object(setup.subprocess, "run", side_effect=run), \
                mock.patch.object(setup.urllib.request, "urlopen", return_value=body) as urlopen:
            result = system.connectivity()
        self.assertTrue(result.lan and result.dns and result.internet)
        self.assertIn(["ping", "-c", "1", "-W", "2", "192.168.1.1"], seen)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 4)

    def test_a_different_page_is_a_login_portal_and_no_answer_is_no_internet(self):
        system = setup.System()
        body = mock.MagicMock()
        body.__enter__.return_value.read.return_value = b"<html>please log in</html>"
        with mock.patch.object(setup.urllib.request, "urlopen", return_value=body):
            self.assertEqual(system.fetch_check(), "portal")
        with mock.patch.object(setup.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(system.fetch_check(), "")

    def test_cec_devices_are_found_by_their_device_nodes(self):
        with mock.patch.object(setup.glob, "glob", return_value=["/dev/cec0"]):
            self.assertTrue(setup.System().cec_present())
        with mock.patch.object(setup.glob, "glob", return_value=[]):
            self.assertFalse(setup.System().cec_present())

    def test_input_devices_and_moonlight_pairings_are_read_from_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "devices").write_text(InputDeviceTest.SAMPLE)
            config = root / "config" / "Moonlight Game Streaming Project"
            config.mkdir(parents=True)
            (config / "Moonlight.conf").write_text("1\\hostname=PC\n1\\srvcert=@ByteArray(abc)\n")
            system = setup.System(input_devices_path=root / "devices", config_root=root / "config")
            self.assertEqual(len(system.input_devices()), 3)
            self.assertEqual(system.paired_hosts(), ["PC"])
            self.assertEqual(setup.System(config_root=root / "missing").paired_hosts(), [])

    def test_sound_and_display_reuse_the_existing_modules(self):
        system = setup.System()
        with mock.patch.object(setup.audio, "query_sinks", return_value=[audio.Sink(1, "HDMI")]), \
                mock.patch.object(setup.audio, "set_default") as set_default:
            self.assertEqual(system.sinks()[0].name, "HDMI")
            system.set_default_sink(1)
        set_default.assert_called_once_with(1)
        output = display.Output("HDMI-A-1", "TV", True, (display.Mode(1920, 1080, 60000, current=True),))
        with mock.patch.object(setup.display, "query_outputs", return_value=[output]), \
                mock.patch.object(setup.display, "active_output", return_value=output):
            self.assertEqual(system.display_modes(), list(output.modes))
        with mock.patch.object(setup.display, "query_outputs", side_effect=RuntimeError("no compositor")):
            self.assertEqual(system.display_modes(), [])


class WizardSaveFailureTest(WizardTestCase):
    MESSAGE = "COULD NOT SAVE SETUP PROGRESS: DISK FULL OR READ-ONLY"
    FULL = OSError(errno.ENOSPC, "No space left on device")

    def test_a_full_disk_when_finishing_is_reported_and_the_launcher_carries_on(self):
        ui = FakeUI("START SETUP", *([None] * 7), "FINISH", "OK")
        with mock.patch.object(setup, "write_complete", side_effect=self.FULL):
            self.assertTrue(self.wizard(ui).run())
        self.assertIn(self.MESSAGE, ui.text())
        self.assertFalse(self.marker.exists())

    def test_a_read_only_disk_when_skipping_setup_is_reported_too(self):
        ui = FakeUI("SKIP SETUP", "OK")
        with mock.patch.object(setup, "write_complete", side_effect=OSError(errno.EROFS, "Read-only file system")):
            self.assertTrue(self.wizard(ui).run())
        self.assertIn(self.MESSAGE, ui.text())

    def test_progress_that_cannot_be_saved_is_reported_once_and_setup_still_finishes(self):
        ui = FakeUI("START SETUP", None, "OK", *([None] * 6), "FINISH")
        with mock.patch.object(setup, "save_state", side_effect=self.FULL):
            self.assertTrue(self.wizard(ui).run())
        self.assertEqual(ui.text().count(self.MESSAGE), 1)
        self.assertTrue(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
