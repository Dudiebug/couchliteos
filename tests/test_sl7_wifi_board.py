import hashlib
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "config" / "profiles" / "snapdragon" / "overlay" / "usr" / "libexec" / "couchliteos-sl7-wifi-board"
SOURCE = "usr/lib/firmware/ath12k/WCN7850/hw2.0/board-2.bin"
TARGET = "usr/lib/firmware/updates/ath12k/WCN7850/hw2.0/board-2.bin"
NAME = (b"bus=pci,vendor=17cb,device=1107,subsystem-vendor=17cb,subsystem-device=1107,"
        b"qmi-chip-id=2,qmi-board-id=255")
DONOR = NAME.replace(b"subsystem-device=1107", b"subsystem-device=3378")
OTHER = NAME.replace(b"qmi-board-id=255", b"qmi-board-id=44")


def element(ie_id, payload):
    return struct.pack("<II", ie_id, len(payload)) + payload + b"\0" * (-len(payload) % 4)


def board(*names, data):
    return element(0, b"".join(element(0, name) for name in names) + element(1, data))


def board_file(*boards, regdb=b"regdb"):
    return b"QCA-ATH12K-BOARD\0" + b"\0" * 3 + b"".join(boards) + element(1, regdb)


def lookup(blob, name):
    """ath12k's search: a matching name, then the board data that follows it in its entry."""
    pos = 20
    while pos < len(blob):
        ie_id, length = struct.unpack_from("<II", blob, pos)
        payload = blob[pos + 8:pos + 8 + length]
        pos += 8 + length + (-length % 4)
        if ie_id != 0:
            continue
        sub, matched = 0, False
        while sub < len(payload):
            sub_id, sub_length = struct.unpack_from("<II", payload, sub)
            value = payload[sub + 8:sub + 8 + sub_length]
            sub += 8 + sub_length + (-sub_length % 4)
            matched = matched or (sub_id == 0 and value == name)
            if sub_id == 1 and matched:
                return value
    return None


class Sl7WifiBoardTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        (self.root / "proc/device-tree").mkdir(parents=True)
        self.machine(b"microsoft,romulus13\0qcom,x1e80100\0")
        self.source = self.root / SOURCE
        self.target = self.root / TARGET
        self.source.parent.mkdir(parents=True)
        # Odd lengths: every element is padded to 4 bytes.
        self.original = board_file(board(OTHER, data=b"other-data!"), board(DONOR, b"second-name", data=b"donor-data"))
        self.source.write_bytes(self.original)

    def tearDown(self):
        self.temporary.cleanup()

    def machine(self, compatible):
        (self.root / "proc/device-tree/compatible").write_bytes(compatible)

    def run_helper(self):
        return subprocess.run([sys.executable, "-I", str(HELPER), str(self.root)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)

    def test_names_the_surface_card_with_the_donor_board_data(self):
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        patched = self.target.read_bytes()
        self.assertEqual(lookup(patched, NAME), b"donor-data")
        self.assertEqual(lookup(patched, DONOR), b"donor-data")
        self.assertEqual(lookup(patched, b"second-name"), b"donor-data")
        self.assertEqual(lookup(patched, OTHER), b"other-data!")
        self.assertTrue(patched.endswith(element(1, b"regdb")))
        self.assertEqual(self.source.read_bytes(), self.original, "Debian's file is never changed")
        stamp = self.target.with_name("board-2.bin.couchliteos-source-sha256")
        self.assertEqual(stamp.read_text().strip(), hashlib.sha256(self.original).hexdigest())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)

    def test_second_run_keeps_the_copy(self):
        self.run_helper()
        before = self.target.stat().st_mtime_ns
        result = self.run_helper()
        self.assertEqual(result.returncode, 0)
        self.assertIn("already made", result.stdout)
        self.assertEqual(self.target.stat().st_mtime_ns, before)

    def test_new_debian_file_is_patched_again(self):
        self.run_helper()
        updated = board_file(board(DONOR, data=b"newer-donor"))
        self.source.write_bytes(updated)
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertEqual(lookup(self.target.read_bytes(), NAME), b"newer-donor")

    def test_debian_naming_the_card_removes_the_copy(self):
        self.run_helper()
        self.source.write_bytes(board_file(board(NAME, data=b"debian-own")))
        result = self.run_helper()
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.with_name("board-2.bin.couchliteos-source-sha256").exists())

    def test_other_machines_are_left_alone(self):
        self.machine(b"qcom,x1e80100-crd\0qcom,x1e80100\0")
        result = self.run_helper()
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())

    def test_fifteen_inch_model(self):
        self.machine(b"microsoft,romulus15\0qcom,x1e80100\0")
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertEqual(lookup(self.target.read_bytes(), NAME), b"donor-data")

    def test_no_donor_leaves_debian_file_in_use(self):
        self.source.write_bytes(board_file(board(OTHER, data=b"other")))
        result = self.run_helper()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no board data for", result.stderr)
        self.assertFalse(self.target.exists())

    def test_not_a_board_file(self):
        self.source.write_bytes(b"not a board file")
        self.assertEqual(self.run_helper().returncode, 1)
        self.assertFalse(self.target.exists())

    def test_truncated_board_file(self):
        self.source.write_bytes(self.original[:-8])
        self.assertEqual(self.run_helper().returncode, 1)
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
