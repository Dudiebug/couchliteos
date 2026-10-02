"""couchliteos_firmware: the launcher side of VIDEO DECODER FIRMWARE."""

import json
import pathlib
import tempfile
import unittest

import couchliteos_firmware as firmware


class FirmwareSettingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temporary.name)
        self.hardware = self.base / "hardware.env"
        self.store = self.base / "store"

    def tearDown(self):
        self.temporary.cleanup()

    def test_offered_only_on_nouveau(self):
        self.assertFalse(firmware.applies(self.hardware))  # no hardware.env yet
        self.hardware.write_text("COUCHLITEOS_GPU_DRIVER=i915\n")
        self.assertFalse(firmware.applies(self.hardware))
        self.hardware.write_text("COUCHLITEOS_GPU_DRIVER=nouveau\nCOUCHLITEOS_GPU_ID=10de:0fe9\n")
        self.assertTrue(firmware.applies(self.hardware))

    def test_state_label(self):
        self.hardware.write_text("COUCHLITEOS_GPU_DRIVER=nouveau\nCOUCHLITEOS_GPU_ID=10de:0fe9\n")
        self.assertEqual(firmware.state_label(self.store, self.hardware), "OFF (SOFTWARE DECODING)")
        (self.store / "nouveau").mkdir(parents=True)
        (self.store / "nouveau" / "nve0_bsp").write_bytes(b"x")
        self.assertEqual(firmware.state_label(self.store, self.hardware), "SAVED, NOT WORKING YET")
        (self.store / "verified").write_text("10de:1234\n")
        self.assertEqual(firmware.state_label(self.store, self.hardware), "SAVED, NOT WORKING YET")
        (self.store / "verified").write_text("10de:1234\n10de:0fe9\n")
        self.assertEqual(firmware.state_label(self.store, self.hardware), "ON")

    def test_submit_writes_the_request_and_clears_an_old_status(self):
        (self.base / "nvidia-firmware.status").write_text('{"state": "done", "message": "OLD"}')
        firmware.submit("install", self.base)
        self.assertEqual((self.base / "nvidia-firmware.request").read_text(), "install\n")
        self.assertIsNone(firmware.read_status(self.base))
        with self.assertRaises(ValueError):
            firmware.submit("rm -rf", self.base)

    def test_read_status(self):
        status = self.base / "nvidia-firmware.status"
        status.write_text(json.dumps({"state": "running", "message": "DOWNLOADING"}))
        self.assertEqual(firmware.read_status(self.base), ("running", "DOWNLOADING"))
        for junk in ("not json", "[]", '{"state": "odd", "message": "x"}', '{"state": "done", "message": 5}'):
            status.write_text(junk)
            self.assertIsNone(firmware.read_status(self.base), junk)

    def test_about_text_says_what_happens(self):
        for words in ("NVIDIA.COM", "325.15", "LICENSE", "SOFTWARE DECODING"):
            self.assertIn(words, firmware.ABOUT)


if __name__ == "__main__":
    unittest.main()
