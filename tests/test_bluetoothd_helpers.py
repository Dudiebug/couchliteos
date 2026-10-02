"""couchliteos-bluetoothd: input validation, saved power, error text and the --snapshot CLI."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import socket
import sys
import tempfile
import unittest
from unittest import mock


PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "couchliteos-bluetoothd"


def load_module():
    loader = importlib.machinery.SourceFileLoader("couchliteos_bluetoothd_helpers", str(PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


bluetoothd = load_module()

ADAPTER_PATH = "/org/bluez/hci0"
DEVICE_PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


class NamedError(Exception):
    def __init__(self, name, text="raw error"):
        super().__init__(text)
        self.name = name

    def get_dbus_name(self):
        return self.name


class TextTest(unittest.TestCase):
    def test_safe_text_replaces_control_characters_and_newlines(self):
        self.assertEqual(bluetoothd.safe_text("Pad\r\nX\x1b[2J\t"), "Pad??X?[2J?")

    def test_safe_text_truncates_and_stringifies(self):
        self.assertEqual(bluetoothd.safe_text("x" * 500), "x" * 160)
        self.assertEqual(bluetoothd.safe_text("abcdef", 3), "abc")
        self.assertEqual(bluetoothd.safe_text(42), "42")
        self.assertEqual(bluetoothd.safe_text(None), "None")

    def test_safe_text_keeps_printable_unicode(self):
        self.assertEqual(bluetoothd.safe_text("Café Pad"), "Café Pad")

    def test_plain_converts_nested_dbus_like_values(self):
        class Opaque:
            def __str__(self):
                return "opaque"

        value = {1: (True, 2.5, None, [Opaque()]), "k": {"n": "v"}}
        self.assertEqual(
            bluetoothd.plain(value), {"1": [True, 2.5, None, ["opaque"]], "k": {"n": "v"}}
        )


class DevicePathTest(unittest.TestCase):
    def test_address_from_a_valid_device_path(self):
        self.assertEqual(bluetoothd.address_from_path(DEVICE_PATH), "AA:BB:CC:DD:EE:FF")
        self.assertEqual(
            bluetoothd.address_from_path("/org/bluez/hci12/dev_01_23_45_67_89_AB"), "01:23:45:67:89:AB"
        )

    def test_address_from_invalid_paths_is_empty(self):
        for path in (
            ADAPTER_PATH,
            "/org/bluez/hci0/dev_aa_bb_cc_dd_ee_ff",  # BlueZ always uses upper case
            "/org/bluez/hci0/dev_AA_BB_CC_DD_EE",
            "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/sep1",
            "/org/bluez/hciX/dev_AA_BB_CC_DD_EE_FF",
            "",
        ):
            self.assertEqual(bluetoothd.address_from_path(path), "", path)

    def test_validate_device_path_accepts_only_device_object_paths(self):
        self.assertEqual(bluetoothd.validate_device_path(DEVICE_PATH), DEVICE_PATH)
        for value in (None, 7, ["x"], ADAPTER_PATH, DEVICE_PATH + "\n", "../" + DEVICE_PATH):
            with self.assertRaisesRegex(ValueError, "INVALID BLUETOOTH DEVICE IDENTIFIER"):
                bluetoothd.validate_device_path(value)


class PreferenceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name) / "state" / "bluetooth-enabled"

    def test_missing_file_has_no_preference(self):
        self.assertIsNone(bluetoothd.read_preference(self.path))

    def test_reads_one_and_zero_with_surrounding_whitespace(self):
        self.path.parent.mkdir()
        self.path.write_text(" 1\n")
        self.assertTrue(bluetoothd.read_preference(self.path))
        self.path.write_text("0")
        self.assertIs(bluetoothd.read_preference(self.path), False)

    def test_anything_else_has_no_preference(self):
        self.path.parent.mkdir()
        for text in ("true", "", "2", "10", "yes\n"):
            self.path.write_text(text)
            self.assertIsNone(bluetoothd.read_preference(self.path), text)

    # KNOWN BUG: read_preference catches OSError only, so a corrupt (non-ASCII)
    # preference file raises UnicodeDecodeError out of _apply_saved_power, i.e.
    # out of the BlueZ InterfacesAdded / GetManagedObjects callbacks. Remove the
    # decorator once read_preference also treats UnicodeError as "no preference".
    @unittest.expectedFailure
    def test_corrupt_non_ascii_file_has_no_preference(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"\xff\xfe")
        self.assertIsNone(bluetoothd.read_preference(self.path))

    @unittest.skipUnless(hasattr(os, "O_DIRECTORY"), "needs POSIX directory fsync")
    def test_write_then_read_round_trip_creates_the_directory(self):
        bluetoothd.write_preference(False, self.path)
        self.assertEqual(self.path.read_text(), "0\n")
        self.assertIs(bluetoothd.read_preference(self.path), False)
        bluetoothd.write_preference(True, self.path)
        self.assertTrue(bluetoothd.read_preference(self.path))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(item.name for item in self.path.parent.iterdir()), ["bluetooth-enabled"])


class ErrorTextTest(unittest.TestCase):
    def test_bluez_error_name(self):
        self.assertEqual(bluetoothd.bluez_error_name(NamedError("org.bluez.Error.Failed")), "org.bluez.Error.Failed")
        self.assertEqual(bluetoothd.bluez_error_name(ValueError("x")), "")
        self.assertEqual(bluetoothd.bluez_error_name(None), "")

    def test_known_errors_are_translated(self):
        self.assertEqual(
            bluetoothd.describe_error(NamedError("org.bluez.Error.AlreadyConnected")),
            "THE DEVICE IS ALREADY CONNECTED",
        )
        self.assertEqual(bluetoothd.describe_error(NamedError("org.bluez.Error.Blocked")), bluetoothd.BLOCKED_TEXT)

    def test_pairing_hint_only_for_pairing_errors_and_only_when_asked(self):
        failed = NamedError("org.bluez.Error.AuthenticationFailed")
        self.assertEqual(bluetoothd.describe_error(failed), "AUTHENTICATION FAILED")
        self.assertEqual(
            bluetoothd.describe_error(failed, hint=True), f"AUTHENTICATION FAILED. {bluetoothd.PAIRING_HINT}"
        )
        busy = NamedError("org.bluez.Error.InProgress")
        self.assertEqual(bluetoothd.describe_error(busy, hint=True), "ANOTHER BLUETOOTH ACTION IS IN PROGRESS")

    def test_unknown_errors_keep_their_sanitized_text(self):
        error = NamedError("org.freedesktop.DBus.Error.Weird", "bad\nthing " + "y" * 200)
        text = bluetoothd.describe_error(error)
        self.assertTrue(text.startswith("bad?thing "))
        self.assertEqual(len(text), 120)
        self.assertEqual(bluetoothd.describe_error(RuntimeError("plain")), "plain")


class SavedPowerTest(unittest.TestCase):
    """A saved on/off choice is re-applied whenever BlueZ (re)announces the adapter."""

    def controller(self, saved):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        preference = pathlib.Path(directory.name) / "bluetooth-enabled"
        if saved is not None:
            preference.write_text("1\n" if saved else "0\n")
        dbus = mock.Mock()
        dbus.Boolean = bool
        controller = bluetoothd.BluetoothController(mock.Mock(), dbus, mock.Mock(), preference_path=preference)
        controller._set_property = mock.Mock()
        return controller

    def announce(self, controller, powered):
        controller.interfaces_added(ADAPTER_PATH, {bluetoothd.ADAPTER: {"Powered": powered}})

    def test_saved_off_powers_down_a_powered_adapter(self):
        controller = self.controller(saved=False)
        self.announce(controller, powered=True)
        controller._set_property.assert_called_once()
        path, interface, name, value = controller._set_property.call_args.args[:4]
        self.assertEqual((path, interface, name, value), (ADAPTER_PATH, bluetoothd.ADAPTER, "Powered", False))

    def test_saved_on_powers_up_an_unpowered_adapter(self):
        controller = self.controller(saved=True)
        self.announce(controller, powered=False)
        self.assertIs(controller._set_property.call_args.args[3], True)

    def test_matching_state_is_left_alone(self):
        controller = self.controller(saved=True)
        self.announce(controller, powered=True)
        controller._set_property.assert_not_called()

    def test_no_saved_choice_is_left_alone(self):
        controller = self.controller(saved=None)
        self.announce(controller, powered=False)
        controller._set_property.assert_not_called()

    def test_a_new_device_does_not_reapply_power(self):
        controller = self.controller(saved=True)
        controller.interfaces_added(DEVICE_PATH, {bluetoothd.DEVICE: {"Address": "AA:BB:CC:DD:EE:FF"}})
        controller._set_property.assert_not_called()
        self.assertIn(DEVICE_PATH, controller.objects)

    def test_restore_failure_is_recorded(self):
        controller = self.controller(saved=True)
        controller._set_property.side_effect = lambda *args: args[5](NamedError("org.bluez.Error.NotReady"))
        with contextlib.redirect_stderr(io.StringIO()) as log:
            self.announce(controller, powered=False)
        self.assertEqual(controller.last_error, "COULD NOT RESTORE BLUETOOTH POWER: BLUETOOTH IS NOT READY YET")
        self.assertIn(controller.last_error, log.getvalue())

    def test_replace_objects_applies_the_saved_choice(self):
        controller = self.controller(saved=False)
        controller._replace_objects({ADAPTER_PATH: {bluetoothd.ADAPTER: {"Powered": True}}})
        self.assertIs(controller._set_property.call_args.args[3], False)


class FakeClient:
    def __init__(self, chunks=(), connect_error=None):
        self.chunks = list(chunks)
        self.connect_error = connect_error
        self.sent = b""
        self.closed = False

    def settimeout(self, _value):
        pass

    def connect(self, _path):
        if self.connect_error:
            raise self.connect_error

    def sendall(self, data):
        self.sent += data

    def recv(self, _size):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class SnapshotCommandTest(unittest.TestCase):
    def snapshot(self, client):
        output = io.StringIO()
        with mock.patch.object(bluetoothd.socket, "AF_UNIX", 1, create=True), \
             mock.patch.object(bluetoothd.socket, "socket", return_value=client), \
             contextlib.redirect_stdout(output):
            code = bluetoothd.print_snapshot(pathlib.Path("control.sock"))
        self.assertTrue(client.closed)
        return code, output.getvalue()

    def test_prints_adapter_and_devices(self):
        response = {
            "ok": True,
            "adapter": {"address": "11:22:33:44:55:66", "powered": True, "discovering": False},
            "devices": [
                {"alias": "Pad\x1b", "paired": True, "connected": False},
                {"alias": "", "paired": False, "connected": True},
            ],
        }
        data = json.dumps(response).encode() + b"\n"
        client = FakeClient([data[:10], data[10:]])
        code, output = self.snapshot(client)
        self.assertEqual(code, 0)
        self.assertEqual(client.sent, b'{"command":"snapshot"}\n')
        self.assertEqual(
            output.splitlines(),
            [
                "Adapter: 11:22:33:44:55:66",
                "Powered: yes",
                "Discovering: no",
                "Known devices:",
                "- Pad?: paired=yes connected=no",
                "- Unknown device: paired=no connected=yes",
            ],
        )

    def test_no_adapter(self):
        code, output = self.snapshot(FakeClient([b'{"ok":true,"adapter":null,"devices":[]}\n']))
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines(), ["Adapter: not found", "Known devices:"])

    def test_service_down(self):
        code, output = self.snapshot(FakeClient(connect_error=FileNotFoundError("no socket")))
        self.assertEqual(code, 1)
        self.assertIn("Bluetooth service unavailable: no socket", output)

    def test_invalid_response(self):
        for chunks in ([b"not json\n"], [b"\xff\xfe\n"], []):
            code, output = self.snapshot(FakeClient(chunks))
            self.assertEqual(code, 1, chunks)
            self.assertEqual(output, "Bluetooth service returned invalid data\n")

    def test_non_object_response_is_reported_without_crashing(self):
        code, output = self.snapshot(FakeClient([b"[1, 2]\n"]))
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines(), ["Adapter: not found", "Known devices:"])

    def test_main_routes_snapshot_flag_without_loading_dbus(self):
        with mock.patch.object(sys, "argv", ["couchliteos-bluetoothd", "--snapshot"]), \
             mock.patch.object(bluetoothd, "print_snapshot", return_value=0) as snapshot:
            self.assertEqual(bluetoothd.main(), 0)
        snapshot.assert_called_once_with()

    def test_main_reports_missing_runtime_dependencies(self):
        error = io.StringIO()
        with mock.patch.object(sys, "argv", ["couchliteos-bluetoothd"]), \
             mock.patch.dict(sys.modules, {"dbus": None}), \
             contextlib.redirect_stderr(error):
            self.assertEqual(bluetoothd.main(), 1)
        self.assertIn("Bluetooth runtime dependency missing", error.getvalue())


if __name__ == "__main__":
    unittest.main()
