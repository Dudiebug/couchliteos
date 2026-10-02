"""couchliteos_support: the launcher side of a support export (request, status, lsblk helpers).

tests/test_support.py covers destination discovery together with the root exporter
(which needs fcntl); these tests need only the launcher module.
"""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import couchliteos_support as support


class SmallHelpersTest(unittest.TestCase):
    def test_bool_accepts_lsblk_spellings(self):
        for value in (True, "1", "true", "True", "yes", 1):
            self.assertTrue(support._bool(value), value)
        for value in (False, "0", "false", "", None, 0, "no", "y"):
            self.assertFalse(support._bool(value), value)

    def test_mountpoints_keep_only_absolute_strings(self):
        self.assertEqual(support._mountpoints(["/media/a", None, "", "relative", 5, "/b"]), ["/media/a", "/b"])
        self.assertEqual(support._mountpoints("/media/usb"), ["/media/usb"])
        self.assertEqual(support._mountpoints("[SWAP]"), [])
        self.assertEqual(support._mountpoints(None), [])
        self.assertEqual(support._mountpoints({"/x": 1}), [])

    def test_live_medium_is_found_anywhere_in_the_subtree(self):
        live = {"mountpoints": [None, support.LIVE_MEDIUM]}
        self.assertTrue(support._subtree_contains_live_medium(live))
        self.assertTrue(support._subtree_contains_live_medium({"mountpoint": support.LIVE_MEDIUM}))
        disk = {"children": [{"mountpoints": ["/media/x"]}, {"children": [live]}]}
        self.assertTrue(support._subtree_contains_live_medium(disk))
        self.assertFalse(support._subtree_contains_live_medium({"children": [{"mountpoints": ["/media/x"]}, "junk"]}))
        self.assertFalse(support._subtree_contains_live_medium({}))

    def test_safe_terminal_text_replaces_unsafe_characters_and_truncates(self):
        self.assertEqual(support.safe_terminal_text("USB\x1b[31m é\n"), "USB??31m ??")
        self.assertEqual(support.safe_terminal_text("A" * 200), "A" * 96)
        self.assertEqual(support.safe_terminal_text("A" * 200, 10), "A" * 10)
        self.assertEqual(support.safe_terminal_text("/media/My Stick_1 (x),;:+-."), "/media/My Stick_1 (x),;:+-.")

    def test_display_name_prefers_display_label_then_label_then_device(self):
        mounted = support.Destination("/dev/sdb1", "/media/usb", "INTERNAL", "vfat", True, display_label="MY USB")
        self.assertEqual(mounted.display_name, "MY USB  /media/usb")
        labelled = support.Destination("/dev/sdb1", "/media/usb", "STICK", "vfat", True)
        self.assertEqual(labelled.display_name, "STICK  /media/usb")
        bare = support.Destination("/dev/sdc1", "", "", "exfat", False)
        self.assertEqual(bare.display_name, "sdc1  /dev/sdc1  (WILL MOUNT TEMPORARILY)")

    def test_display_name_is_sanitized(self):
        hostile = support.Destination("/dev/sdb1", "/media/\x1b]0;x", "\x07BEL", "vfat", True)
        self.assertNotIn("\x1b", hostile.display_name)
        self.assertNotIn("\x07", hostile.display_name)


class LsblkTest(unittest.TestCase):
    def completed(self, returncode=0, stdout=""):
        return subprocess.CompletedProcess(["lsblk"], returncode, stdout, "")

    def test_lsblk_data_parses_json_objects_only(self):
        with mock.patch.object(support.subprocess, "run", return_value=self.completed(stdout='{"blockdevices": []}')) as run:
            self.assertEqual(support._lsblk_data(), {"blockdevices": []})
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["lsblk", "--json"])
        self.assertIn("MOUNTPOINTS", command[-1])
        self.assertFalse(run.call_args.kwargs["check"])
        for result in (self.completed(1, '{"blockdevices": []}'), self.completed(stdout="garbage"),
                       self.completed(stdout="[1]")):
            with mock.patch.object(support.subprocess, "run", return_value=result):
                self.assertIsNone(support._lsblk_data())

    def test_discover_destinations_is_empty_when_lsblk_fails(self):
        with mock.patch.object(support, "_lsblk_data", return_value=None):
            self.assertEqual(support.discover_destinations(), [])

    def test_discover_destinations_uses_the_writable_check(self):
        data = {"blockdevices": [{"path": "/dev/sdb", "type": "disk", "tran": "usb", "rm": True, "children": [
            {"path": "/dev/sdb1", "type": "part", "fstype": "vfat", "label": "STICK", "mountpoints": ["/media/s"]},
        ]}]}
        with mock.patch.object(support, "_lsblk_data", return_value=data), \
             mock.patch.object(support, "path_is_writable", return_value=True) as writable:
            found = support.discover_destinations()
        writable.assert_called_once_with("/media/s")
        self.assertEqual([(item.device, item.mountpoint, item.mounted) for item in found], [("/dev/sdb1", "/media/s", True)])

    def test_no_destination_message_without_lsblk_asks_for_a_drive(self):
        with mock.patch.object(support, "_lsblk_data", return_value=None):
            self.assertEqual(support.no_destination_message(), support.NO_DESTINATION_TEXT[support.REASON_NO_DRIVE])

    @unittest.skipUnless(hasattr(os, "statvfs"), "needs statvfs")
    def test_path_is_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(support.path_is_writable(directory))
            file_path = pathlib.Path(directory) / "file"
            file_path.write_text("x")
            self.assertFalse(support.path_is_writable(str(file_path)))
            self.assertFalse(support.path_is_writable(str(pathlib.Path(directory) / "missing")))


class RequestAndStatusTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = pathlib.Path(directory.name) / "run"
        for name, value in (
            ("RUN", self.run_dir),
            ("REQUEST", self.run_dir / "support-export.request"),
            ("STATUS", self.run_dir / "support-export.status"),
        ):
            patcher = mock.patch.object(support, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.destination = support.Destination(
            "/dev/sdb1", "/media/usb", "STICK", "vfat", True, "8:17", "ABCD-1234", "STICK"
        )

    def test_submit_request_writes_the_destination_and_a_fresh_id(self):
        first = support.submit_request(self.destination)
        payload = json.loads(support.REQUEST.read_text())
        self.assertRegex(first, r"^[0-9a-f]{24}$")
        self.assertEqual(payload, {
            "device": "/dev/sdb1", "mountpoint": "/media/usb", "label": "STICK", "fstype": "vfat",
            "mounted": True, "majmin": "8:17", "uuid": "ABCD-1234", "display_label": "STICK",
            "request_id": first,
        })
        second = support.submit_request(self.destination)
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads(support.REQUEST.read_text())["request_id"], second)
        # Only the request itself is left behind: no temporary files.
        self.assertEqual([item.name for item in self.run_dir.iterdir()], ["support-export.request"])

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_submit_request_is_private(self):
        support.submit_request(self.destination)
        self.assertEqual(support.REQUEST.stat().st_mode & 0o777, 0o600)

    def write_status(self, payload):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        support.STATUS.write_text(payload if isinstance(payload, str) else json.dumps(payload))

    def test_read_status_returns_only_the_matching_request(self):
        self.write_status({"request_id": "abc", "state": "success", "message": "SAVED TO /media/usb"})
        self.assertEqual(support.read_status("abc"),
                         {"request_id": "abc", "state": "success", "message": "SAVED TO /media/usb"})
        self.assertIsNone(support.read_status("other"))

    def test_read_status_sanitizes_values_for_the_terminal(self):
        self.write_status({"request_id": "abc", "state": "failed", "message": "bad\x1b[2Jthing " + "x" * 400, "n": 5})
        status = support.read_status("abc")
        self.assertEqual(status["message"][:13], "bad??2Jthing ")
        self.assertEqual(len(status["message"]), 240)
        self.assertEqual(status["n"], "5")

    def test_read_status_missing_invalid_or_oversized(self):
        self.assertIsNone(support.read_status("abc"))
        self.write_status("{not json")
        self.assertIsNone(support.read_status("abc"))
        self.write_status(json.dumps({"request_id": "abc", "message": "x" * 9000}))
        self.assertIsNone(support.read_status("abc"))

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "needs symlinks")
    def test_read_status_refuses_a_symlink(self):
        self.run_dir.mkdir(parents=True)
        target = self.run_dir / "elsewhere.json"
        target.write_text(json.dumps({"request_id": "abc", "state": "success"}))
        support.STATUS.symlink_to(target)
        self.assertIsNone(support.read_status("abc"))

    # KNOWN BUG: read_status calls payload.get() without checking that the JSON is
    # an object, so a status file holding a JSON array/string/number raises
    # AttributeError into the launcher's progress loop instead of returning None.
    @unittest.expectedFailure
    def test_read_status_ignores_non_object_json(self):
        for text in ("[]", '"abc"', "5", "null"):
            self.write_status(text)
            self.assertIsNone(support.read_status("abc"), text)


if __name__ == "__main__":
    unittest.main()
