import unittest
from unittest import mock

import moonlightos_confirm as confirm

KEY_UP, KEY_DOWN, KEY_LEFT, ENTER = confirm.curses.KEY_UP, confirm.curses.KEY_DOWN, confirm.curses.KEY_LEFT, 10
QUESTION = "TURN OFF BLUETOOTH? BLUETOOTH CONTROLLERS WILL DISCONNECT."


class FakeScreen:
    def __init__(self, keys, size=(30, 100)):
        self.keys = list(keys)
        self.size = size
        self.cells = []  # (row, column, text) of the current frame
        self.frames = []  # the frame on screen each time a key was read

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.cells = []

    def border(self, *_args):
        pass

    def addstr(self, row, column, text, *_args):
        height, width = self.size
        if not (0 <= row < height and column >= 0 and column + len(text) <= width):
            raise confirm.curses.error("outside the screen")
        self.cells.append((row, column, text))

    def addnstr(self, row, column, text, length, *_args):
        self.addstr(row, column, text[:length])

    def refresh(self):
        pass

    def getch(self):
        self.frames.append(list(self.cells))
        if not self.keys:
            raise AssertionError("confirm() kept waiting after every key was used")
        return self.keys.pop(0)

    def text(self, frame=None):
        return " | ".join(cell[2] for cell in (self.cells if frame is None else frame))


def ask(keys, question=QUESTION, size=(30, 100)):
    screen = FakeScreen(keys, size)
    events = []
    real_getch = screen.getch
    screen.getch = lambda: (events.append("getch"), real_getch())[1]
    with mock.patch.object(confirm.curses, "flushinp", side_effect=lambda: events.append("flush"), create=True):
        answer = confirm.confirm(screen, question)
    return answer, screen, events


class ConfirmTest(unittest.TestCase):
    def test_a_press_on_the_default_answers_no(self):
        self.assertIs(ask([ENTER])[0], False)

    def test_escape_and_the_b_button_answer_no(self):
        self.assertIs(ask([27])[0], False)
        self.assertIs(ask([KEY_DOWN, 27])[0], False)

    def test_moving_to_yes_then_enter_answers_yes(self):
        for move in (KEY_UP, KEY_DOWN, KEY_LEFT, confirm.curses.KEY_RIGHT, ord("j"), ord("k")):
            self.assertIs(ask([move, ENTER])[0], True, move)
        self.assertIs(ask([KEY_DOWN, KEY_DOWN, ENTER])[0], False)

    def test_timeouts_resizes_and_other_keys_do_not_answer(self):
        other = [-1, confirm.curses.KEY_RESIZE, confirm.curses.KEY_F12, confirm.curses.KEY_DC, ord("y")]
        self.assertIs(ask([*other, ENTER])[0], False)

    def test_queued_input_is_dropped_before_any_key_is_read(self):
        _answer, _screen, events = ask([ENTER])
        self.assertEqual(events[0], "flush")
        self.assertLess(events.index("flush"), events.index("getch"))

    def test_the_question_and_both_answers_are_shown_with_no_selected(self):
        _answer, screen, _events = ask([ENTER])
        shown = screen.text(screen.frames[0])
        self.assertIn("BLUETOOTH CONTROLLERS WILL DISCONNECT", shown.replace(" | ", " "))
        self.assertIn(">  NO", shown)
        self.assertIn("   YES", shown)
        _answer, screen, _events = ask([KEY_DOWN, ENTER])
        self.assertIn(">  YES", screen.text(screen.frames[1]))

    def test_everything_fits_small_screens(self):
        long_question = "FORGET WIRELESS CONTROLLER? IF IT IS A CONTROLLER IT WILL STOP WORKING UNTIL YOU PAIR IT AGAIN. " * 2
        for size in ((18, 65), (19, 69), (24, 80), (27, 98), (10, 40)):
            for question in (QUESTION, long_question):
                _answer, screen, _events = ask([KEY_DOWN, ENTER], question, size)
                for frame in screen.frames:
                    text = screen.text(frame)
                    self.assertIn("NO", text, (size, question))
                    self.assertIn("YES", text, (size, question))


if __name__ == "__main__":
    unittest.main()
