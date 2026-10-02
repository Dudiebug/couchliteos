import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

import couchliteos_osk as osk


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
        self.assertEqual(keyboard.text, "1q")
        keyboard.row, keyboard.column = 4, 3
        keyboard.select()
        self.assertTrue(keyboard.shift)
        keyboard.row, keyboard.column = 4, 4
        keyboard.select()
        self.assertTrue(keyboard.symbols)
        keyboard.row, keyboard.column = 5, 0
        keyboard.select()
        self.assertTrue(keyboard.masked)

    def test_starts_lowercase_and_shift_capitalises_only_the_next_letter(self):
        # An RDP password typed with the launcher's all-capitals look in mind must not come out shouted.
        keyboard = osk.Keyboard()
        self.assertFalse(keyboard.shift)
        q, shift = (1, 0), (4, 3)
        keyboard.row, keyboard.column = q
        keyboard.select()
        keyboard.row, keyboard.column = shift
        keyboard.select()
        self.assertEqual(keyboard.rows[1][0], "Q")
        keyboard.row, keyboard.column = q
        keyboard.select()
        self.assertFalse(keyboard.shift)
        keyboard.select()
        self.assertEqual(keyboard.text, "qQq")
        # Pressing SHIFT twice cancels it.
        keyboard.row, keyboard.column = shift
        keyboard.select()
        keyboard.select()
        self.assertFalse(keyboard.shift)

    def test_backspace_clear_and_output_actions(self):
        keyboard = osk.Keyboard()
        keyboard.text = "abc"
        keyboard.row, keyboard.column = 4, 1
        keyboard.select()
        self.assertEqual(keyboard.text, "ab")
        keyboard.column = 2
        keyboard.select()
        self.assertEqual(keyboard.text, "")
        keyboard.row, keyboard.column = 5, 2
        self.assertEqual(keyboard.select(), "type")
        keyboard.column = 3
        self.assertEqual(keyboard.select(), "enter")
        keyboard.column = 1
        self.assertEqual(keyboard.select(), "cancel")

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


class EscapeDelayTest(unittest.TestCase):
    def test_escape_does_not_wait_a_second_for_an_escape_sequence(self):
        screen = mock.Mock()
        screen.get_wch.return_value = "\x1b"
        with mock.patch.object(osk.curses, "set_escdelay") as set_escdelay, mock.patch.object(
            osk.curses, "curs_set"
        ), mock.patch.object(osk, "draw"), mock.patch.object(osk, "consume_mask_request", return_value=False):
            osk.ui(screen)
        set_escdelay.assert_called_once_with(25)


class DrawScreen:
    def __init__(self, height, width):
        self.size = (height, width)
        self.cells = []  # (row, column, text, fully shown)

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.cells = []

    def border(self, *_args):
        pass

    def addnstr(self, row, column, text, length, *_args):
        height, width = self.size
        if not (0 <= row < height and 0 <= column < width):
            raise osk.curses.error("outside the screen")
        self.cells.append((row, column, text[:length], len(text) <= length and column + len(text) <= width))

    def refresh(self):
        pass


class LayoutTest(unittest.TestCase):
    def test_every_button_is_visible_on_720p_768p_and_1080p_terminals(self):
        # foot at size 24 gives about 65x18 at 720p, 69x19 at 768p, 98x27 at 1080p.
        for height, width in ((18, 65), (19, 69), (24, 80), (27, 98)):
            for shift, symbols in ((False, False), (True, False), (False, True)):
                keyboard = osk.Keyboard()
                keyboard.shift, keyboard.symbols = shift, symbols
                for row, column in ((0, 0), (4, 0), (5, 3)):
                    keyboard.row, keyboard.column = row, column
                    screen = DrawScreen(height, width)
                    osk.draw(screen, keyboard)
                    where = (height, width, shift, symbols, row, column)
                    self.assertTrue(all(cell[3] for cell in screen.cells), (where, screen.cells))
                    rows = [cell[0] for cell in screen.cells]
                    self.assertEqual(len(rows), len(set(rows)), (where, "two lines share a row"))
                    shown = " ".join(cell[2] for cell in screen.cells)
                    for label in osk.ACTIONS:
                        self.assertIn(label, shown, where)
                    self.assertIn("CANCELS", shown, where)  # the footer is not overwritten
                    self.assertIn("Y / SQUARE DELETES", shown, where)  # the controller has a delete button


class ControllerDeleteTest(unittest.TestCase):
    def test_delete_key_from_the_controller_erases_the_last_character(self):
        # Y/Square reaches the keyboard window as KEY_DC.
        typed = []
        keys = ["a", "b", osk.curses.KEY_DC] + [osk.curses.KEY_DOWN] * 5 + [osk.curses.KEY_RIGHT] * 2 + ["\n"]
        screen = mock.Mock()
        screen.get_wch.side_effect = keys
        with mock.patch.object(osk.curses, "set_escdelay"), mock.patch.object(osk.curses, "curs_set"), mock.patch.object(
            osk, "draw"
        ), mock.patch.object(osk, "consume_mask_request", return_value=False), mock.patch.object(
            osk, "atomic_payload", side_effect=lambda text, enter: typed.append((text, enter))
        ):
            osk.ui(screen)
        self.assertEqual(typed, [("a", False)])


class InjectTest(unittest.TestCase):
    """inject() waits for the compositor to open the new keyboard before typing, and for
    the last release to be read before the keyboard goes away."""

    def run_inject(self, text, enter=False, payload=True):
        timeline = []  # ("open"/"write"/"syn"/"close"/"sleep", detail)

        class Device:
            def __init__(self, capabilities, name):
                timeline.append(("open", name))
                self.capabilities = capabilities

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                timeline.append(("close", None))

            def write(self, _kind, code, value):
                timeline.append(("write", (code, value)))

            def syn(self):
                timeline.append(("syn", None))

        codes = Codes()
        codes.EV_KEY = 1
        fake = types.ModuleType("evdev")
        fake.UInput = Device
        fake.ecodes = codes
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "payload.json"
            if payload:
                osk.atomic_payload(text, enter, path)
            with mock.patch.dict(sys.modules, {"evdev": fake}), mock.patch.object(
                osk.time, "sleep", side_effect=lambda seconds: timeline.append(("sleep", seconds))
            ):
                result = osk.inject(path)
            self.assertFalse(path.exists(), "the payload (maybe a password) is never left behind")
        return result, timeline, codes

    def test_typing_waits_for_the_new_keyboard_to_settle_first(self):
        result, timeline, _codes = self.run_inject("ab")
        self.assertEqual(result, 0)
        self.assertEqual(timeline[0], ("open", "CouchLiteOS Buffered Keyboard"))
        self.assertEqual(timeline[1], ("sleep", osk.DEVICE_SETTLE))
        self.assertGreaterEqual(osk.DEVICE_SETTLE, 0.2, "long enough for udev and libinput")
        first_write = next(index for index, (kind, _detail) in enumerate(timeline) if kind == "write")
        self.assertGreater(first_write, 1)

    def test_the_keyboard_stays_a_moment_after_the_last_key(self):
        _result, timeline, codes = self.run_inject("a", enter=True)
        self.assertEqual(timeline[-1], ("close", None))
        self.assertEqual(timeline[-2], ("sleep", 0.1))
        last_write = max(index for index, (kind, _detail) in enumerate(timeline) if kind == "write")
        self.assertEqual(timeline[last_write], ("write", (codes.KEY_ENTER, 0)), "Enter is released before the wait")
        self.assertLess(last_write, len(timeline) - 2)

    def test_every_key_is_pressed_and_released_with_shift_around_capitals(self):
        _result, timeline, codes = self.run_inject("aA")
        writes = [detail for kind, detail in timeline if kind == "write"]
        a = codes.KEY_A
        self.assertEqual(writes, [(a, 1), (a, 0), (codes.KEY_LEFTSHIFT, 1), (a, 1), (a, 0), (codes.KEY_LEFTSHIFT, 0)])
        sleeps = [detail for kind, detail in timeline if kind == "sleep"]
        self.assertEqual(sleeps[0], osk.DEVICE_SETTLE)
        self.assertEqual(sleeps[-1], 0.1)
        self.assertEqual(len(sleeps), 4, "settle, one short pause per character, the final wait")

    def test_no_payload_creates_no_device_and_does_not_wait(self):
        result, timeline, _codes = self.run_inject("", payload=False)
        self.assertEqual(result, 0)
        self.assertEqual(timeline, [])


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
