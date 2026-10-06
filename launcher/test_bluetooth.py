import testenv  # noqa: F401  (first: scratch run and state directories)
import json
import pathlib
import unittest
from unittest import mock

import couchliteos_bluetooth as bluetooth


ADAPTER_ON = {
    "ok": True,
    "adapter": {"path": "/org/bluez/hci0", "powered": True, "discovering": False},
    "devices": [],
    "operations": [],
    "prompt": None,
}
DEVICE = {
    "path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
    "address": "AA:BB:CC:DD:EE:FF",
    "alias": "Wireless Controller",
    "paired": True,
    "trusted": True,
    "connected": False,
    "rssi": -40,
    "audio": False,
}


class FakeScreen:
    def __init__(self, keys=()):
        self.keys = list(keys)
        self.drawn = []
        self.frame = []
        self.frames = []  # what was on screen each time a key was read
        self.timeouts = []

    def getmaxyx(self):
        return 30, 100

    def erase(self):
        self.frame = []

    def border(self):
        pass

    def addstr(self, _row, _column, value):
        self.drawn.append(value)
        self.frame.append(value)

    def addnstr(self, _row, _column, value, _length):
        self.drawn.append(value)
        self.frame.append(value)

    def refresh(self):
        pass

    def timeout(self, value):
        self.timeouts.append(value)

    def getch(self):
        self.frames.append(list(self.frame))
        return self.keys.pop(0) if self.keys else 27


class FakeClient:
    def __init__(self, snapshots=()):
        self.snapshots = list(snapshots)
        self.requests = []
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        if not self.snapshots:
            return dict(ADAPTER_ON)
        value = self.snapshots.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def request(self, command, **fields):
        self.requests.append((command, fields))
        if command in {"pair", "connect", "disconnect", "forget", "use_audio"}:
            return {"ok": True, "operation_id": "op-1"}
        return {"ok": True, "accepted": True}


class BluetoothHelpersTest(unittest.TestCase):
    def test_duplicate_aliases_receive_address_suffixes(self):
        devices = [dict(DEVICE), dict(DEVICE, path=DEVICE["path"][:-2] + "11", address="AA:BB:CC:DD:EE:11")]
        self.assertEqual(
            bluetooth.device_labels(devices),
            ["Wireless Controller · EE:FF", "Wireless Controller · EE:11"],
        )

    def test_unnamed_and_mac_only_devices_are_hidden(self):
        self.assertEqual(
            bluetooth.named_devices([
                dict(DEVICE, alias=""),
                dict(DEVICE, alias="AA:BB:CC:DD:EE:FF"),
                dict(DEVICE, alias="aabbccddeeff"),
                DEVICE,
            ]),
            [DEVICE],
        )

    def test_named_paired_device_remains_visible(self):
        self.assertTrue(bluetooth.has_useful_name(DEVICE))

    def test_socket_client_rejects_malformed_response(self):
        connection = mock.Mock()
        connection.recv.side_effect = [b"not json\n"]
        with mock.patch.object(bluetooth.socket, "socket", return_value=connection):
            with self.assertRaisesRegex(bluetooth.BluetoothError, "INVALID RESPONSE"):
                bluetooth.BluetoothClient(pathlib.Path("/tmp/control.sock")).snapshot()
        connection.close.assert_called_once_with()

    def test_socket_client_accepts_snapshot(self):
        connection = mock.Mock()
        connection.recv.side_effect = [(json.dumps(ADAPTER_ON) + "\n").encode()]
        with mock.patch.object(bluetooth.socket, "socket", return_value=connection):
            snapshot = bluetooth.BluetoothClient(pathlib.Path("/tmp/control.sock")).snapshot()
        self.assertTrue(snapshot["adapter"]["powered"])


class BluetoothMenuTest(unittest.TestCase):
    def test_no_adapter_screen_returns_with_escape(self):
        screen = FakeScreen([27])
        client = FakeClient([dict(ADAPTER_ON, adapter=None)])
        bluetooth.BluetoothMenu(screen, client).run()
        self.assertIn("NO BLUETOOTH ADAPTER FOUND", screen.drawn)

    def test_powered_off_screen_can_turn_adapter_on(self):
        off = dict(ADAPTER_ON, adapter={"path": "/org/bluez/hci0", "powered": False})
        screen = FakeScreen([10, 27])
        client = FakeClient([off, ADAPTER_ON])
        bluetooth.BluetoothMenu(screen, client).run()
        self.assertIn(("set_power", {"powered": True}), client.requests)

    def test_powered_on_screen_lists_connected_state(self):
        screen = FakeScreen([27])
        client = FakeClient([dict(ADAPTER_ON, devices=[dict(DEVICE, connected=True)])])
        bluetooth.BluetoothMenu(screen, client).run()
        self.assertTrue(any("Wireless Controller" in value and "CONNECTED" in value for value in screen.drawn))

    def test_powered_on_screen_does_not_list_mac_only_device(self):
        mac_only = dict(DEVICE, alias=DEVICE["address"], paired=False)
        screen = FakeScreen([27])
        bluetooth.BluetoothMenu(screen, FakeClient([dict(ADAPTER_ON, devices=[mac_only])])).run()
        self.assertFalse(any(DEVICE["address"] in value for value in screen.drawn))

    def test_scan_runs_until_escape_and_has_no_countdown(self):
        screen = FakeScreen([-1] * 200 + [27])
        client = FakeClient([ADAPTER_ON])
        bluetooth.BluetoothMenu(screen, client).run()
        self.assertEqual(client.requests[0][0], "start_scan")
        self.assertEqual(client.requests[-1][0], "stop_scan")
        self.assertGreater(client.snapshot_calls, 15)
        self.assertTrue(any("SCANNING" in value for value in screen.drawn))
        self.assertFalse(any("15S" in value for value in screen.drawn))

    def test_rescan_stops_then_starts_discovery(self):
        screen = FakeScreen([10, 27])
        client = FakeClient([ADAPTER_ON, ADAPTER_ON])
        bluetooth.BluetoothMenu(screen, client).run()
        commands = [command for command, _fields in client.requests]
        self.assertEqual(commands[:3], ["start_scan", "stop_scan", "start_scan"])
        self.assertEqual(commands[-1], "stop_scan")

    def test_pair_success_finishes_without_external_screen(self):
        completed = dict(
            ADAPTER_ON,
            operations=[{"id": "op-1", "type": "pair", "state": "completed", "device": DEVICE["path"]}],
        )
        screen = FakeScreen([10])
        client = FakeClient([completed])
        bluetooth.BluetoothMenu(screen, client)._pair(dict(DEVICE, paired=False))
        self.assertIn(("pair", {"device": DEVICE["path"]}), client.requests)

    def test_pair_confirmation_can_be_rejected(self):
        prompt = {
            "id": "prompt-1",
            "kind": "confirmation",
            "passkey": "123456",
            "operation_id": "op-1",
        }
        working = dict(
            ADAPTER_ON,
            operations=[{"id": "op-1", "type": "pair", "state": "working", "device": DEVICE["path"]}],
            prompt=prompt,
        )
        screen = FakeScreen([27])
        client = FakeClient([working])
        success, _operation = bluetooth.BluetoothMenu(screen, client)._wait_operation(
            "op-1", "PAIRING", cancellable_pairing=True
        )
        self.assertFalse(success)
        self.assertIn(
            ("agent_reply", {"prompt_id": "prompt-1", "accepted": False}), client.requests
        )

    def test_display_passkey_stays_on_screen_while_the_device_waits_for_it(self):
        prompt = {
            "id": "prompt-1",
            "kind": "display_passkey",
            "passkey": "123456",
            "operation_id": "op-1",
        }
        working = dict(
            ADAPTER_ON,
            operations=[{"id": "op-1", "type": "pair", "state": "working", "device": DEVICE["path"]}],
        )
        screen = FakeScreen([-1, -1, 27])
        client = FakeClient([dict(working, prompt=prompt), dict(working, prompt=prompt), working])
        success, _operation = bluetooth.BluetoothMenu(screen, client)._wait_operation(
            "op-1", "PAIRING", cancellable_pairing=True
        )
        self.assertFalse(success)
        self.assertEqual(len(screen.frames), 3)
        for frame in screen.frames[:2]:
            self.assertTrue(any("123456" in value for value in frame), frame)
        self.assertFalse(any("123456" in value for value in screen.frames[2]))
        self.assertTrue(any("PLEASE WAIT" in value for value in screen.frames[2]))
        self.assertIn(("cancel_pairing", {"operation_id": "op-1"}), client.requests)

    def test_pair_timeout_is_reported(self):
        failed = dict(
            ADAPTER_ON,
            operations=[{
                "id": "op-1",
                "type": "pair",
                "state": "failed",
                "device": DEVICE["path"],
                "error": "PAIRING TIMED OUT",
            }],
        )
        screen = FakeScreen([27])
        client = FakeClient([failed])
        success, _operation = bluetooth.BluetoothMenu(screen, client)._wait_operation(
            "op-1", "PAIRING", cancellable_pairing=True
        )
        self.assertFalse(success)
        self.assertTrue(any("PAIRING TIMED OUT" in value for value in screen.drawn))

    def test_adapter_removal_during_operation_is_recoverable(self):
        screen = FakeScreen([27])
        client = FakeClient([dict(ADAPTER_ON, adapter=None)])
        success, _operation = bluetooth.BluetoothMenu(screen, client)._wait_operation("op-1", "CONNECTING")
        self.assertFalse(success)
        self.assertIn("BLUETOOTH ADAPTER REMOVED", screen.drawn)

    def test_service_unavailable_always_offers_back(self):
        screen = FakeScreen([bluetooth.curses.KEY_DOWN, 10])
        client = FakeClient([bluetooth.BluetoothError("unavailable")])
        bluetooth.BluetoothMenu(screen, client).run()
        self.assertIn("BLUETOOTH SERVICE UNAVAILABLE", screen.drawn)
        self.assertTrue(any("BACK" in value for value in screen.drawn))

    def requests_after(self, keys, snapshots=(ADAPTER_ON,), device_screen=False):
        """Commands sent when the given keys are pressed, plus everything drawn."""
        class BoundedScreen(FakeScreen):
            def getch(self):
                if len(self.frames) > 40:
                    raise AssertionError("menu never returned")
                return super().getch()

        screen = BoundedScreen(keys)
        client = FakeClient(list(snapshots))
        menu = bluetooth.BluetoothMenu(screen, client)
        menu._device_screen(DEVICE["path"]) if device_screen else menu.run()
        return [command for command, _fields in client.requests], screen.drawn

    def test_turning_bluetooth_off_asks_first_and_defaults_to_no(self):
        # A Bluetooth controller user reaches this row with one Up Up; it disconnects their pad
        # and the off state survives a reboot.
        up, down, enter = bluetooth.curses.KEY_UP, bluetooth.curses.KEY_DOWN, 10
        commands, drawn = self.requests_after([up, up, enter, enter])
        self.assertNotIn("set_power", commands)
        self.assertIn("BLUETOOTH CONTROLLERS WILL DISCONNECT", " ".join(drawn))
        commands, _drawn = self.requests_after([up, up, enter, down, enter])
        self.assertIn("set_power", commands)
        commands, _drawn = self.requests_after([up, up, enter, 27])
        self.assertNotIn("set_power", commands)

    def test_forgetting_a_device_asks_first_and_defaults_to_no(self):
        down, enter = bluetooth.curses.KEY_DOWN, 10
        paired = dict(ADAPTER_ON, devices=[DEVICE])
        done = dict(ADAPTER_ON, operations=[{"id": "op-1", "state": "completed"}])
        commands, drawn = self.requests_after([down, enter, enter], [paired] * 3, device_screen=True)
        self.assertNotIn("forget", commands)
        self.assertIn("PAIR IT AGAIN", " ".join(drawn))
        commands, _drawn = self.requests_after([down, enter, down, enter], [paired, paired, done], device_screen=True)
        self.assertIn("forget", commands)

    def test_f12_while_the_keyboard_is_open_does_not_queue_a_second_one(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            with mock.patch.object(bluetooth, "START_OSK", run / "start-osk"), mock.patch.object(
                bluetooth, "OSK_ACTIVE", run / "osk-active", create=True
            ):
                menu = bluetooth.BluetoothMenu(FakeScreen([bluetooth.curses.KEY_F12] * 2), FakeClient([ADAPTER_ON]))
                (run / "osk-active").touch()
                self.assertEqual(menu._getch(), -1)
                self.assertFalse((run / "start-osk").exists())
                (run / "osk-active").unlink()
                self.assertEqual(menu._getch(), -1)
                self.assertTrue((run / "start-osk").exists())

    def test_device_screen_changes_action_for_connection_state(self):
        disconnected = dict(ADAPTER_ON, devices=[DEVICE])
        screen = FakeScreen([27])
        bluetooth.BluetoothMenu(screen, FakeClient([disconnected]))._device_screen(DEVICE["path"])
        self.assertTrue(any("CONNECT" in value for value in screen.drawn))

    def test_no_adapter_screen_says_what_to_do(self):
        screen = FakeScreen([27])
        bluetooth.BluetoothMenu(screen, FakeClient([dict(ADAPTER_ON, adapter=None)])).run()
        self.assertIn("NO BLUETOOTH ADAPTER FOUND", screen.drawn)
        self.assertTrue(any("PLUG IN" in value and "ADAPTER" in value for value in screen.drawn))

    def test_request_errors_say_to_try_again(self):
        class Refusing(FakeClient):
            def request(self, command, **fields):
                raise bluetooth.BluetoothError("ANOTHER BLUETOOTH ACTION IS ALREADY RUNNING FOR THIS DEVICE")

        menu = bluetooth.BluetoothMenu(FakeScreen(), Refusing())
        with mock.patch.object(menu, "message") as message:
            self.assertIsNone(menu._request("pair", device="x"))
        title, text = message.call_args.args
        self.assertEqual(title, "BLUETOOTH ERROR")
        self.assertIn("ALREADY RUNNING", text)
        self.assertIn("TRY AGAIN", text)

    def test_service_unavailable_message_says_to_wait_or_reboot(self):
        menu = bluetooth.BluetoothMenu(FakeScreen(), FakeClient([bluetooth.BluetoothError("BLUETOOTH SERVICE UNAVAILABLE")]))
        with mock.patch.object(menu, "message") as message:
            menu._device_screen(DEVICE["path"])
        self.assertEqual(message.call_args.args[0], "BLUETOOTH SERVICE UNAVAILABLE")
        self.assertIn("REBOOT", message.call_args.args[1])


class DeviceKindTest(unittest.TestCase):
    def test_kind_from_the_bluez_icon(self):
        for icon, kind, label in (
            ("audio-headphones", "headphones", "HEADPHONES"),
            ("audio-headset", "headset", "HEADSET"),
            ("audio-card", "speaker", "SPEAKER"),
            ("input-gaming", "controller", "CONTROLLER"),
            ("input-keyboard", "keyboard", "KEYBOARD"),
        ):
            with self.subTest(icon=icon):
                device = dict(DEVICE, icon=icon)
                self.assertEqual(bluetooth.device_kind(device), kind)
                self.assertEqual(bluetooth.kind_label(device), label)
        self.assertEqual(bluetooth.kind_icon(dict(DEVICE, icon="audio-headset")), "audio-headset")

    def test_kind_from_the_class_of_device_without_an_icon(self):
        self.assertEqual(bluetooth.device_kind(dict(DEVICE, device_class=0x240404)), "headset")
        self.assertEqual(bluetooth.device_kind(dict(DEVICE, device_class=0x240418)), "headphones")
        self.assertEqual(bluetooth.device_kind(dict(DEVICE, device_class=0x240414)), "speaker")
        self.assertEqual(bluetooth.device_kind(dict(DEVICE, device_class=0x002508)), "controller")
        self.assertEqual(bluetooth.device_kind(dict(DEVICE, device_class=0x002540)), "keyboard")
        self.assertEqual(bluetooth.device_kind(dict(DEVICE)), "other")
        self.assertEqual(bluetooth.device_kind(dict(DEVICE, audio=True)), "speaker", "audio profiles, no class")
        self.assertEqual(bluetooth.kind_label(dict(DEVICE)), "")

    def test_audio_devices_and_battery_text(self):
        self.assertTrue(bluetooth.is_audio(dict(DEVICE, icon="audio-headphones")))
        self.assertFalse(bluetooth.is_audio(dict(DEVICE, icon="input-gaming")))
        self.assertEqual(bluetooth.battery_text(dict(DEVICE, battery=80)), "80%")
        self.assertEqual(bluetooth.battery_text(dict(DEVICE, battery=None)), "")
        self.assertEqual(bluetooth.battery_text(DEVICE), "")

    def test_list_rows_show_what_each_device_is(self):
        headphones = dict(DEVICE, alias="WH-1000XM5", icon="audio-headphones", connected=True)
        screen = FakeScreen([27])
        bluetooth.BluetoothMenu(screen, FakeClient([dict(ADAPTER_ON, devices=[headphones])])).run()
        self.assertTrue(any("WH-1000XM5" in row and "HEADPHONES" in row and "CONNECTED" in row for row in screen.drawn))

    def test_audio_device_screen_shows_type_battery_codec_and_use_for_audio(self):
        headphones = dict(DEVICE, alias="WH-1000XM5", icon="audio-headphones", connected=True, battery=70)
        screen = FakeScreen([27])
        with mock.patch.object(bluetooth, "audio_codec", return_value="LDAC") as codec:
            bluetooth.BluetoothMenu(screen, FakeClient([dict(ADAPTER_ON, devices=[headphones])]))._device_screen(DEVICE["path"])
        codec.assert_called_with("AA:BB:CC:DD:EE:FF")
        drawn = " ".join(screen.drawn)
        for text in ("HEADPHONES", "70%", "LDAC", "USE FOR AUDIO"):
            self.assertIn(text, drawn)

    def test_a_controller_has_no_codec_or_audio_action(self):
        pad = dict(DEVICE, icon="input-gaming", connected=True)
        screen = FakeScreen([27])
        with mock.patch.object(bluetooth, "audio_codec") as codec:
            bluetooth.BluetoothMenu(screen, FakeClient([dict(ADAPTER_ON, devices=[pad])]))._device_screen(DEVICE["path"])
        codec.assert_not_called()
        self.assertNotIn("USE FOR AUDIO", screen.drawn)

    def test_a_pairing_step_the_device_started_is_shown_and_answered(self):
        prompt = {"id": "prompt-1", "kind": "confirmation", "passkey": "123456", "operation_id": "",
                  "device_alias": "Speaker"}
        client = FakeClient([dict(ADAPTER_ON, prompt=prompt), dict(ADAPTER_ON, prompt=prompt)])
        screen = FakeScreen([10, -1, 27])  # CONFIRM, then the same prompt is not asked twice
        bluetooth.BluetoothMenu(screen, client).run()
        self.assertIn("123456", screen.drawn)
        self.assertIn(("agent_reply", {"prompt_id": "prompt-1", "accepted": True}), client.requests)
        self.assertEqual(sum(1 for command, _fields in client.requests if command == "agent_reply"), 1)

    def test_a_code_to_type_on_the_device_is_shown(self):
        prompt = {"id": "prompt-2", "kind": "display_passkey", "passkey": "0042", "operation_id": "",
                  "device_alias": "Keyboard"}
        screen = FakeScreen([10, 27])
        bluetooth.BluetoothMenu(screen, FakeClient([dict(ADAPTER_ON, prompt=prompt)])).run()
        self.assertTrue(any("0042" in row and "KEYBOARD" in row.upper() for row in screen.drawn))

    def test_codec_is_empty_when_pipewire_cannot_say(self):
        with mock.patch("couchliteos_audio.query_graph", return_value=None):
            self.assertEqual(bluetooth.audio_codec("AA:BB:CC:DD:EE:FF"), "")


if __name__ == "__main__":
    unittest.main()
