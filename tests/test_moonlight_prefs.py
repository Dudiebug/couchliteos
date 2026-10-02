import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PREFS = ROOT / "scripts" / "couchliteos-moonlight-prefs"

# Moonlight's own enums: videocfg 0 auto, 1 H.264, 2 HEVC, 4 AV1;
# videodec 0 auto, 1 hardware, 2 software.
SOFTWARE_H264 = ("1", "2")
AUTO = ("0", "0")


class MoonlightPrefsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temporary.name)
        self.conf = base / "Moonlight Game Streaming Project" / "Moonlight.conf"
        self.marker = base / "couchliteos-decode-seed"

    def tearDown(self):
        self.temporary.cleanup()

    def prefs(self, hint, codec="auto", decoder="auto", resolution=("1920", "1080"), fps="60"):
        return subprocess.run(
            [sys.executable, str(PREFS), str(self.conf), str(self.marker), hint,
             resolution[0], resolution[1], fps, codec, decoder],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )

    def write_conf(self, text):
        self.conf.parent.mkdir(parents=True, exist_ok=True)
        self.conf.write_text(text)

    def values(self):
        found = {}
        for line in self.conf.read_text().splitlines():
            key, _, value = line.partition("=")
            found[key] = value
        return found

    def pair(self):
        values = self.values()
        return values.get("videocfg"), values.get("videodec")

    def test_usage(self):
        result = subprocess.run([sys.executable, str(PREFS)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 64)

    def test_software_hint_seeds_a_missing_file_and_remembers_it(self):
        result = self.prefs("software", codec="H.264", decoder="software", resolution=("1280", "720"), fps="30")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pair(), SOFTWARE_H264)
        values = self.values()
        self.assertEqual((values["width"], values["height"], values["fps"]), ("1280", "720", "30"))
        self.assertEqual(self.marker.read_text().split(), list(SOFTWARE_H264))

    def test_automatic_settings_leave_a_missing_file_missing(self):
        self.assertEqual(self.prefs("auto").returncode, 0)
        self.assertFalse(self.conf.exists())
        self.assertFalse(self.marker.exists())

    def test_configured_codec_seeds_once_without_a_hint_marker(self):
        self.assertEqual(self.prefs("auto", codec="HEVC", decoder="hardware").returncode, 0)
        self.assertEqual(self.pair(), ("2", "1"))
        self.assertFalse(self.marker.exists())

    def test_stick_moves_from_a_software_pc_to_a_hardware_pc(self):
        self.prefs("software", codec="H.264", decoder="software")
        self.conf.write_text(self.conf.read_text() + "\n[hosts]\n1\\localaddress=10.0.0.5\n")
        result = self.prefs("auto")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pair(), AUTO)
        self.assertFalse(self.marker.exists())
        text = self.conf.read_text()
        self.assertIn("width=1920", text)
        self.assertIn("1\\localaddress=10.0.0.5", text)

    def test_stick_first_used_on_a_hardware_pc_gets_the_hint_on_a_software_pc(self):
        self.write_conf("[General]\nwidth=1920\nheight=1080\nvideocfg=0\nvideodec=0\nvsync=true\n")
        result = self.prefs("software", codec="H.264", decoder="software")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pair(), SOFTWARE_H264)
        self.assertEqual(self.values()["vsync"], "true")
        self.assertEqual(self.marker.read_text().split(), list(SOFTWARE_H264))

    def test_missing_video_keys_count_as_automatic(self):
        self.write_conf("[General]\nwidth=1920\n")
        self.prefs("software", codec="H.264", decoder="software")
        self.assertEqual(self.pair(), SOFTWARE_H264)
        self.assertEqual(self.values()["width"], "1920")

    def test_file_without_a_general_section_gets_one(self):
        self.write_conf("[hosts]\nsize=0\n")
        self.prefs("software", codec="H.264", decoder="software")
        self.assertEqual(self.pair(), SOFTWARE_H264)
        self.assertIn("[hosts]", self.conf.read_text())

    def test_round_trip_between_machines(self):
        for hint, expected in (("software", SOFTWARE_H264), ("auto", AUTO), ("software", SOFTWARE_H264), ("auto", AUTO)):
            decode = ("H.264", "software") if hint == "software" else ("auto", "auto")
            self.assertEqual(self.prefs(hint, *decode).returncode, 0)
            if hint == "auto" and not self.conf.exists():
                continue
            self.assertEqual(self.pair(), expected)

    def test_hint_is_idempotent(self):
        self.prefs("software", codec="H.264", decoder="software")
        first = self.conf.read_text()
        self.prefs("software", codec="H.264", decoder="software")
        self.assertEqual(self.conf.read_text(), first)
        self.assertEqual(first.count("videocfg"), 1)

    def test_a_choice_made_in_moonlight_is_kept_on_a_hardware_pc(self):
        self.prefs("software", codec="H.264", decoder="software")
        self.write_conf("[General]\nvideocfg=2\nvideodec=1\n")  # HEVC, hardware
        self.prefs("auto")
        self.assertEqual(self.pair(), ("2", "1"))

    def test_a_hardware_or_automatic_decoder_is_undone_on_a_software_pc(self):
        # The user's report: Moonlight set back to automatic on a nouveau iMac, so
        # streams opened with "no functioning hardware accelerated video decoder".
        for decoder in ("0", "1"):
            self.prefs("software", codec="H.264", decoder="software")
            self.write_conf(f"[General]\nvideocfg=0\nvideodec={decoder}\n")
            self.prefs("software", codec="H.264", decoder="software")
            self.assertEqual(self.pair(), SOFTWARE_H264, decoder)
            self.assertEqual(tuple(self.marker.read_text().split()), SOFTWARE_H264)

    def test_a_codec_chosen_with_software_decoding_is_kept_on_a_software_pc(self):
        self.prefs("software", codec="H.264", decoder="software")
        self.write_conf("[General]\nvideocfg=2\nvideodec=2\n")  # HEVC, software
        self.assertEqual(self.prefs("software", codec="H.264", decoder="software").stdout, "")
        self.assertEqual(self.pair(), ("2", "2"))

    def test_settings_of_unknown_origin(self):
        # No marker: a PC without a decoder still gets the software decoder ...
        self.write_conf("[General]\nvideocfg=2\nvideodec=1\n")
        self.prefs("software", codec="H.264", decoder="software")
        self.assertEqual(self.pair(), SOFTWARE_H264)
        # ... but a PC with one never undoes settings CouchLiteOS did not write.
        self.marker.unlink()
        self.write_conf("[General]\nvideocfg=1\nvideodec=2\n")
        self.prefs("auto")
        self.assertEqual(self.pair(), SOFTWARE_H264)
        self.assertFalse(self.marker.exists())

    def test_a_deleted_file_forgets_the_marker(self):
        self.prefs("software", codec="H.264", decoder="software")
        self.conf.unlink()
        self.prefs("auto")
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.conf.exists())

    def test_reports_what_changed(self):
        self.write_conf("[General]\nvideocfg=0\nvideodec=0\n")
        self.assertIn("software H.264", self.prefs("software", "H.264", "software").stdout)
        self.assertIn("automatic", self.prefs("auto").stdout)
        self.assertEqual(self.prefs("auto").stdout, "")


if __name__ == "__main__":
    unittest.main()
