import os
import pathlib
import tempfile
import unittest

import moonlightos_osk as osk


class Codes:
    KEY_LEFTSHIFT = 42
    KEY_ENTER = 28

    def __getattr__(self, name):
        return abs(hash(name)) % 10000 + 100


class KeyboardTest(unittest.TestCase):
    def test_grid_navigation_buffer_shift_symbols_and_mask(self):
        keyboard = osk.Keyboard()
        keyboard.select()
        self.assertEqual(keyboard.text, "1")
        keyboard.row, keyboard.column = 1, 0
        keyboard.select()
        self.assertEqual(keyboard.text, "1Q")
        keyboard.row, keyboard.column = 4, 3
        keyboard.select()
        self.assertFalse(keyboard.shift)
        keyboard.row, keyboard.column = 4, 4
        keyboard.select()
        self.assertTrue(keyboard.symbols)
        keyboard.row, keyboard.column = 4, 5
        keyboard.select()
        self.assertTrue(keyboard.masked)

    def test_backspace_clear_and_output_actions(self):
        keyboard = osk.Keyboard()
        keyboard.text = "abc"
        keyboard.row, keyboard.column = 4, 1
        keyboard.select()
        self.assertEqual(keyboard.text, "ab")
        keyboard.column = 2
        keyboard.select()
        self.assertEqual(keyboard.text, "")
        keyboard.column = 7
        self.assertEqual(keyboard.select(), "type")
        keyboard.column = 8
        self.assertEqual(keyboard.select(), "enter")

    def test_payload_is_atomic_private_and_removed_after_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "payload.json"
            osk.atomic_payload("Hello!", True, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(osk.load_payload(path), ("Hello!", True))
            self.assertFalse(path.exists())

    def test_malformed_and_wrong_owner_payloads_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "payload.json"
            path.write_text("not json")
            with self.assertRaises(ValueError):
                osk.load_payload(path)
            self.assertFalse(path.exists())
            path.write_text('{"text":"ok","enter":false}')
            with self.assertRaisesRegex(ValueError, "owner"):
                osk.load_payload(path, owner_uid=os.getuid() + 1)
            self.assertFalse(path.exists())

    def test_character_mapping_supports_shifted_punctuation(self):
        codes = Codes()
        events = osk.character_events("aA1!?", True, codes)
        self.assertFalse(events[0][1])
        self.assertTrue(events[1][1])
        self.assertTrue(events[3][1])
        self.assertEqual(events[-1], (codes.KEY_ENTER, False))
        with self.assertRaisesRegex(ValueError, "unsupported"):
            osk.character_events("é", False, codes)


class MaskRequestTest(unittest.TestCase):
    def test_mask_request_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / "osk-masked"
            self.assertFalse(osk.consume_mask_request(marker))
            marker.touch()
            self.assertTrue(osk.consume_mask_request(marker))
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
