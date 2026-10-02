"""couchliteos_rdp: session request files, password-service status and small validators."""

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_rdp as rdp


POSIX_FILES = hasattr(os, "O_NOFOLLOW") and hasattr(os, "fchown") and hasattr(os, "getuid")
REQUEST_ID = "0123456789abcdef01234567"


class ValidatorTest(unittest.TestCase):
    def test_connection_ids(self):
        for value in ("rdp-work", "rdp-a1-b2", "rdp-" + "x" * 28):
            self.assertEqual(rdp.validate_id(value), value)
        for value in ("work", "rdp_work", "RDP-work", "rdp-" + "x" * 29, "", "rdp-work\n", "../rdp-x", "-rdp"):
            with self.assertRaisesRegex(rdp.RdpError, "invalid connection id"):
                rdp.validate_id(value)

    def test_display_names(self):
        self.assertEqual(rdp.validate_name("  Work PC  "), "Work PC")
        with self.assertRaisesRegex(rdp.RdpError, "required"):
            rdp.validate_name("   ")
        with self.assertRaisesRegex(rdp.RdpError, "too long"):
            rdp.validate_name("x" * 65)
        with self.assertRaisesRegex(rdp.RdpError, "unsupported character"):
            rdp.validate_name("Work\x1bPC")

    def test_new_connection_ids_are_valid_and_unique(self):
        first = rdp.connection_id("Work PC", set())
        self.assertEqual(first, "rdp-work-pc")
        self.assertEqual(rdp.connection_id("Work PC", {first}), "rdp-work-pc-2")
        self.assertEqual(rdp.connection_id("!!!", set()), "rdp-connection")
        long_id = rdp.connection_id("A" * 80, set())
        self.assertEqual(rdp.validate_id(long_id), long_id)

    def test_fingerprint_lines_group_upper_case_pairs(self):
        value = "ab" * 32
        lines = rdp.fingerprint_lines(value)
        self.assertEqual(lines, [":".join(["AB"] * 8)] * 4)
        self.assertEqual(rdp.fingerprint_lines("AB:" * 31 + "AB", per_line=16), [":".join(["AB"] * 16)] * 2)
        self.assertEqual(rdp.fingerprint_lines(""), [])
        with self.assertRaises(rdp.RdpError):
            rdp.fingerprint_lines("abc")

    def test_display_size_accepts_only_wxh(self):
        self.assertEqual(rdp._display_size("1920x1080"), "1920x1080")
        for value in (None, "", "native", "1920x1080 ", "1920X1080", "0x0"):
            self.assertIsNone(rdp._display_size(value), value)


class SecretStatusTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.status = pathlib.Path(directory.name) / "rdp-secret.status"

    def write(self, payload):
        self.status.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")

    def test_matching_status_is_returned_as_strings(self):
        self.write({"request_id": REQUEST_ID, "state": "success", "message": None, "extra": "dropped"})
        self.assertEqual(
            rdp.read_secret_status(REQUEST_ID, self.status),
            {"request_id": REQUEST_ID, "state": "success", "message": "None"},
        )

    def test_missing_fields_become_empty(self):
        self.write({"request_id": REQUEST_ID})
        self.assertEqual(
            rdp.read_secret_status(REQUEST_ID, self.status), {"request_id": REQUEST_ID, "state": "", "message": ""}
        )

    def test_other_request_missing_or_invalid_file_is_none(self):
        self.assertIsNone(rdp.read_secret_status(REQUEST_ID, self.status))
        self.write({"request_id": "f" * 24, "state": "success"})
        self.assertIsNone(rdp.read_secret_status(REQUEST_ID, self.status))
        for text in ("{", "[]", '"x"', "null"):
            self.write(text)
            self.assertIsNone(rdp.read_secret_status(REQUEST_ID, self.status), text)
        self.status.write_bytes(b"\xff\xfe")
        self.assertIsNone(rdp.read_secret_status(REQUEST_ID, self.status))

    def test_wait_returns_the_final_state(self):
        self.write({"request_id": REQUEST_ID, "state": "failed", "message": "DISK FULL"})
        self.assertEqual(rdp.wait_secret_status(REQUEST_ID, 1.0, self.status), (False, "DISK FULL"))
        self.write({"request_id": REQUEST_ID, "state": "success", "message": "SAVED"})
        self.assertEqual(rdp.wait_secret_status(REQUEST_ID, 1.0, self.status), (True, "SAVED"))

    def test_wait_polls_until_the_service_answers(self):
        states = iter([None, {"request_id": REQUEST_ID, "state": "working", "message": ""},
                       {"request_id": REQUEST_ID, "state": "success", "message": "OK"}])
        with mock.patch.object(rdp, "read_secret_status", side_effect=lambda *_: next(states)), \
             mock.patch.object(rdp.time, "sleep") as sleep:
            self.assertEqual(rdp.wait_secret_status(REQUEST_ID, 10.0, self.status), (True, "OK"))
        self.assertEqual(sleep.call_count, 2)

    def test_wait_times_out(self):
        clock = iter([0.0, 0.0, 5.0, 11.0])
        with mock.patch.object(rdp.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(rdp.time, "sleep"):
            self.assertEqual(
                rdp.wait_secret_status(REQUEST_ID, 10.0, self.status), (False, "the password service did not respond")
            )


class SessionFilesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run = pathlib.Path(directory.name)

    def test_clear_session_state_removes_handoff_and_session(self):
        for name in (rdp.HANDOFF.name, rdp.SESSION.name, rdp.REQUEST.name):
            (self.run / name).write_text("x")
        rdp.clear_session_state(self.run)
        self.assertEqual(sorted(item.name for item in self.run.iterdir()), [rdp.REQUEST.name])
        rdp.clear_session_state(self.run)  # nothing left to remove is fine

    def test_clear_session_state_can_keep_the_password_handoff(self):
        for name in (rdp.HANDOFF.name, rdp.SESSION.name):
            (self.run / name).write_text("x")
        rdp.clear_session_state(self.run, keep_handoff=True)
        self.assertEqual(sorted(item.name for item in self.run.iterdir()), [rdp.HANDOFF.name])

    def test_session_request_rejects_invalid_ids_before_writing(self):
        path = self.run / "rdp.request"
        with self.assertRaises(rdp.RdpError):
            rdp.write_session_request("../../etc/passwd", path)
        self.assertFalse(path.exists())

    @unittest.skipUnless(POSIX_FILES, "needs POSIX ownership and O_NOFOLLOW")
    def test_session_request_round_trip(self):
        path = self.run / "rdp.request"
        rdp.write_session_request("rdp-work-pc", path)
        self.assertEqual(path.read_text(), "rdp-work-pc\n")
        self.assertEqual(rdp.read_connection_id(path), "rdp-work-pc")

    @unittest.skipUnless(POSIX_FILES, "needs POSIX ownership and O_NOFOLLOW")
    def test_read_connection_id_validation(self):
        path = self.run / "rdp.request"
        self.assertIsNone(rdp.read_connection_id(path))
        path.write_text("not-an-rdp-id\n")
        with self.assertRaisesRegex(rdp.RdpError, "invalid connection id"):
            rdp.read_connection_id(path)
        path.write_text("rdp-" + "x" * 80)
        with self.assertRaisesRegex(rdp.RdpError, "bounded regular file"):
            rdp.read_connection_id(path)
        path.write_text("rdp-work\n")
        with self.assertRaisesRegex(rdp.RdpError, "unexpected owner"):
            rdp.read_connection_id(path, owner_uid=os.getuid() + 1)

    @unittest.skipUnless(POSIX_FILES, "needs POSIX ownership and O_NOFOLLOW")
    def test_read_connection_id_refuses_symlinks(self):
        target = self.run / "target"
        target.write_text("rdp-work\n")
        link = self.run / "rdp.request"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            rdp.read_connection_id(link)


if __name__ == "__main__":
    unittest.main()
