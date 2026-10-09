import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_settings as settings


class SectionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name) / "config.ini"

    def test_missing_file_and_section_read_as_empty(self):
        self.assertEqual(settings.read_section("ui", self.path), {})
        self.path.write_text("[power]\nblank_minutes = 2\n")
        self.assertEqual(settings.read_section("ui", self.path), {})

    def test_write_creates_the_file_with_private_mode(self):
        settings.write_section("ui", {"interface": "classic"}, self.path)
        self.assertEqual(self.path.read_text(), "[ui]\ninterface = classic\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_other_sections_survive_a_replace(self):
        self.path.write_text("[display]\noutput = HDMI-A-1\n\n[ui]\ninterface = tv\nold = 1\n\n[moonlight]\nfps = 60\n")
        settings.write_section("ui", {"interface": "classic"}, self.path)
        text = self.path.read_text()
        self.assertIn("[display]\noutput = HDMI-A-1\n", text)
        self.assertIn("[ui]\ninterface = classic\n\n[moonlight]\nfps = 60\n", text)
        self.assertNotIn("old", text)
        self.assertEqual(text.count("[ui]"), 1)

    def test_a_file_without_a_final_newline_gets_one_before_the_new_section(self):
        self.path.write_text("[power]\nblank_minutes = 2")
        settings.write_section("ui", {"interface": "tv"}, self.path)
        self.assertEqual(self.path.read_text(), "[power]\nblank_minutes = 2\n\n[ui]\ninterface = tv\n")

    def test_update_merges_and_keeps_existing_keys(self):
        settings.write_section("theme", {"name": "slate", "accent": "amber"}, self.path)
        result = settings.update_section("theme", {"name": "midnight"}, self.path)
        self.assertEqual(result, {"name": "midnight", "accent": "amber"})
        self.assertEqual(settings.read_section("theme", self.path), result)

    def test_an_unwritable_hand_edited_entry_does_not_block_saving_the_section(self):
        # A tab in a value or a space in a key: write_section refuses those, so update_section drops them.
        self.path.write_text("[update]\nnote = a\tb\nmy key = 1\nauto = off\n")
        result = settings.update_section("update", {"auto": "on"}, self.path)
        self.assertEqual(result, {"auto": "on"})
        self.assertEqual(settings.read_section("update", self.path), {"auto": "on"})
        with self.assertRaises(ValueError):  # a bad new value is still refused
            settings.update_section("update", {"auto": "a\tb"}, self.path)

    def test_bad_keys_values_and_sections_are_refused_and_nothing_is_written(self):
        for section, values in (
            ("ui", {"Bad Key": "x"}),
            ("ui", {"interface": "a\nb = c"}),
            ("ui", {"interface": " padded"}),
            ("UI", {"interface": "tv"}),
            ("ui]", {"interface": "tv"}),
        ):
            with self.subTest(section=section, values=values):
                with self.assertRaises(ValueError):
                    settings.write_section(section, values, self.path)
        self.assertFalse(self.path.exists())

    def test_a_failed_write_leaves_the_old_file_and_no_temporary(self):
        self.path.write_text("[ui]\ninterface = tv\n")
        with mock.patch("couchliteos_settings.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                settings.write_section("ui", {"interface": "classic"}, self.path)
        self.assertEqual(self.path.read_text(), "[ui]\ninterface = tv\n")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["config.ini"])

    def test_unreadable_file_reads_as_empty(self):
        self.path.write_bytes(b"[ui\n\xff\xfe garbage")
        self.assertEqual(settings.read_section("ui", self.path), {})

    def test_a_line_that_is_not_a_setting_is_skipped_and_the_rest_still_read(self):
        self.path.write_text("stray\n[ui]\ninterface = tv\noops no equals\n  indented after the bad line\n"
                             "home = rows\n= no key\n[audio]\nsink = hdmi\n")
        self.assertEqual(settings.read_section("ui", self.path), {"interface": "tv", "home": "rows"})
        self.assertEqual(settings.read_section("audio", self.path), {"sink": "hdmi"})
        self.assertEqual(settings.sections("", self.path), ["ui", "audio"])

    def test_a_value_that_goes_on_over_indented_lines_is_still_read(self):
        self.path.write_text("[ui]\nnote = one\n  two\nhome = rows\n")
        self.assertEqual(settings.read_section("ui", self.path), {"note": "one\ntwo", "home": "rows"})

    def test_update_never_drops_keys_because_of_a_bad_line(self):
        self.path.write_text("[theme]\nname = slate\nbroken line\naccent = amber\n\n[audio]\noops\nsink = hdmi\n")
        result = settings.update_section("theme", {"name": "midnight"}, self.path)
        self.assertEqual(result, {"name": "midnight", "accent": "amber"})
        self.assertEqual(settings.read_section("theme", self.path), result)
        self.assertIn("[audio]\noops\nsink = hdmi\n", self.path.read_text())  # another section is left as it was

    def test_update_does_not_write_over_a_file_it_cannot_read(self):
        self.path.write_bytes(b"[theme]\nname = slate\xff\naccent = amber\n")
        with self.assertRaises(OSError):
            settings.update_section("theme", {"name": "midnight"}, self.path)
        self.assertEqual(self.path.read_bytes(), b"[theme]\nname = slate\xff\naccent = amber\n")

    def test_sections_with_colons_round_trip_and_list_by_prefix(self):
        settings.write_section("stream:default", {"preset": "balanced"}, self.path)
        settings.write_section("stream:pc-1:doom-1a2b", {"app": "DOOM: Eternal", "preset": "performance"}, self.path)
        settings.write_section("ui", {"interface": "tv"}, self.path)
        self.assertEqual(settings.read_section("stream:pc-1:doom-1a2b", self.path),
                         {"app": "DOOM: Eternal", "preset": "performance"})
        self.assertEqual(settings.sections("stream:", self.path), ["stream:default", "stream:pc-1:doom-1a2b"])
        self.assertEqual(settings.sections("stream:", self.path / "missing"), [])

    def test_delete_section_keeps_the_others(self):
        self.path.write_text("[ui]\ninterface = tv\n\n[stream:pc:a]\npreset = quality\n\n[audio]\nsink = hdmi\n")
        self.assertTrue(settings.delete_section("stream:pc:a", self.path))
        self.assertEqual(self.path.read_text(), "[ui]\ninterface = tv\n\n[audio]\nsink = hdmi\n")
        self.assertFalse(settings.delete_section("stream:pc:a", self.path))
        self.assertTrue(settings.delete_section("audio", self.path))
        self.assertEqual(self.path.read_text(), "[ui]\ninterface = tv\n")

    def test_section_part_is_always_a_valid_section_piece(self):
        for text, expected in (
            ("Steam Big Picture", "steam-big-picture"),
            ("DOOM: Eternal]", "doom-eternal"),
            ("Pokémon ™", "pok-mon"),
            ("[x]\nkey = 1", "x-key-1"),
            ("::", "-"),
            ("", "-"),
        ):
            with self.subTest(text=text):
                part = settings.section_part(text)
                self.assertEqual(part, expected)
                settings.write_section(f"stream:{part}", {"ok": "1"}, self.path)
        self.assertEqual(len(settings.section_part("a" * 200)), 48)


class ValueHelpersTest(unittest.TestCase):
    def test_bool(self):
        self.assertTrue(settings.get_bool({"x": "ON"}, "x", False))
        self.assertFalse(settings.get_bool({"x": "no"}, "x", True))
        self.assertTrue(settings.get_bool({"x": "maybe"}, "x", True))
        self.assertFalse(settings.get_bool({}, "x", False))

    def test_choice(self):
        self.assertEqual(settings.get_choice({"i": "CLASSIC"}, "i", ("tv", "classic"), "tv"), "classic")
        self.assertEqual(settings.get_choice({"i": "web"}, "i", ("tv", "classic"), "tv"), "tv")

    def test_int_out_of_range_or_junk_uses_the_default(self):
        self.assertEqual(settings.get_int({"n": "7"}, "n", 3, 0, 10), 7)
        self.assertEqual(settings.get_int({"n": "70"}, "n", 3, 0, 10), 3)
        self.assertEqual(settings.get_int({"n": "x"}, "n", 3, 0, 10), 3)


if __name__ == "__main__":
    unittest.main()
