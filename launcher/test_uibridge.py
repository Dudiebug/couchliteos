import curses
import json
import unittest

import couchliteos_listview as listview
import couchliteos_uibridge as bridge


class Keys(bridge.KeySource):
    def __init__(self, keys):
        super().__init__(-1)
        self.pending = list(keys)

    def read(self, timeout):
        return self.pending.pop(0) if self.pending else None

    def drain(self):
        self.pending.clear()


def window(keys=()):
    sent = []
    return bridge.Window(sent.append, Keys(keys)), sent


class WindowTest(unittest.TestCase):
    def test_a_frame_is_sent_once_per_change(self):
        screen, sent = window()
        screen.erase()
        screen.addstr(2, 40, "DISPLAY")
        screen.refresh()
        screen.refresh()
        self.assertEqual(len(sent), 1)
        self.assertEqual(json.loads(sent[0])["ops"], [[2, 40, "DISPLAY", 0]])
        screen.addnstr(3, 1, "RESOLUTION", 3)
        screen.refresh()
        self.assertEqual(json.loads(sent[1])["ops"][-1], [3, 1, "RES", 0])

    def test_reading_a_key_shows_the_frame_first(self):
        screen, sent = window([curses.KEY_DOWN, "a"])
        screen.addstr(1, 1, "X")
        self.assertEqual(screen.getch(), curses.KEY_DOWN)
        self.assertEqual(len(sent), 1)
        self.assertEqual(screen.getch(), ord("a"))
        self.assertEqual(screen.getch(), -1)

    def test_get_wch_raises_without_input_as_curses_does(self):
        screen, _sent = window(["é"])
        self.assertEqual(screen.get_wch(), "é")
        with self.assertRaises(curses.error):
            screen.get_wch()

    def test_get_wch_gives_enter_and_escape_as_characters_as_curses_does(self):
        # Text fields (ADD WEB APPLICATION) compare with "\n" and "\x1b": as numbers, A and B did nothing.
        screen, _sent = window([10, 27, 9, curses.KEY_BACKSPACE, curses.KEY_F12])
        self.assertEqual([screen.get_wch() for _ in range(5)], ["\n", "\x1b", "\t", curses.KEY_BACKSPACE, curses.KEY_F12])

    def test_a_qr_code_is_sent_as_its_url(self):
        screen, sent = window()
        screen.report_qr("http://192.168.1.5:4000/t/abc", 5)
        screen.refresh()
        self.assertEqual(json.loads(sent[0])["qr"], [5, "http://192.168.1.5:4000/t/abc"])
        screen.erase()
        screen.refresh()
        self.assertNotIn("qr", json.loads(sent[1]))

    def test_unknown_window_calls_do_nothing(self):
        screen, _sent = window()
        self.assertIsNone(screen.attron(curses.A_BOLD))

    def test_listview_hands_over_the_whole_list(self):
        screen, _sent = window()
        rows = [f"ROW {number}" for number in range(40)]
        listview.draw_rows(screen, rows, 30, 5, 25, 10)
        self.assertEqual(screen.lists, [[5, 10, rows, 30]])
        self.assertEqual(screen.ops, [])

    def test_keys_round_trip(self):
        for key in (curses.KEY_UP, 10, 27, "x", " "):
            self.assertEqual(bridge.decode_key(bridge.encode_key(key)[:-1]), key)
        self.assertIsNone(bridge.decode_key("bogus"))


class InstallTest(unittest.TestCase):
    def test_curses_functions_work_without_a_terminal(self):
        saved = {name: getattr(curses, name, None) for name in ("curs_set", "flushinp", "wrapper", "color_pair")}
        try:
            screen, _sent = window(["a"])
            bridge.install(screen)
            curses.curs_set(0)
            curses.flushinp()
            self.assertEqual(screen.getch(), -1)  # flushed
            self.assertIs(curses.wrapper(lambda win, value: (win, value), 3)[0], screen)
        finally:
            for name, value in saved.items():
                if value is not None:
                    setattr(curses, name, value)


def frame(ops=(), lists=(), rows=30, cols=100):
    return {"rows": rows, "cols": cols, "boxed": True, "ops": [list(op) for op in ops], "lists": [list(item) for item in lists]}


def centered(row, text, cols=100):
    return (row, (cols - len(text)) // 2, text, 0)


class ParseTest(unittest.TestCase):
    def test_title_list_and_hint(self):
        view = bridge.parse(frame(
            [centered(3, "DISPLAY"), centered(27, "A / CROSS CHOOSES   B / CIRCLE GOES BACK")],
            [(10, 30, ["RESOLUTION  1920X1080", "REFRESH RATE  60 HZ", "BACK"], 1)],
        ))
        self.assertEqual(view.title, "DISPLAY")
        self.assertEqual(view.hint, ("A / CROSS CHOOSES   B / CIRCLE GOES BACK",))
        (block,) = view.blocks
        self.assertEqual(block.rows[0], bridge.Row("RESOLUTION", "1920X1080"))
        self.assertEqual(block.selected, 1)
        self.assertIs(view.focus, block)

    def test_a_list_without_a_cursor_is_a_paragraph(self):
        view = bridge.parse(frame([centered(3, "NETWORK")], [(10, 4, ["CONNECTED TO", "HOME WIFI"], None)]))
        self.assertEqual(view.blocks, (bridge.TextBlock(("CONNECTED TO", "HOME WIFI"), True),))

    def test_rows_drawn_with_the_classic_cursor_are_a_list(self):
        view = bridge.parse(frame([
            centered(2, "BLUETOOTH"),
            (8, 6, "   PAIR A NEW DEVICE", 0),
            (9, 6, ">  XBOX CONTROLLER  CONNECTED", 0),
            (10, 6, "   BACK", 0),
            (11, 9, "v  MORE", 0),
        ]))
        (block,) = view.blocks
        self.assertEqual([row.label for row in block.rows], ["PAIR A NEW DEVICE", "XBOX CONTROLLER", "BACK"])
        self.assertEqual(block.rows[1].value, "CONNECTED")
        self.assertEqual(block.selected, 1)

    def test_reverse_video_marks_the_focused_row(self):
        view = bridge.parse(frame([
            centered(2, "SETUP"),
            (10, 20, "   ENGLISH", 0),
            (11, 20, "   DEUTSCH", curses.A_REVERSE),
        ]))
        self.assertEqual(view.blocks[0].selected, 1)

    def test_progress_bars_become_progress(self):
        view = bridge.parse(frame([centered(2, "SOFTWARE UPDATE"), (12, 10, "[##########          ] 50%", 0)]))
        (block,) = view.blocks
        self.assertIsInstance(block, bridge.ProgressBlock)
        self.assertAlmostEqual(block.fraction, 0.5)

    def test_rules_and_more_markers_are_dropped(self):
        view = bridge.parse(frame([centered(2, "HELP"), (4, 1, "-" * 98, 0), (6, 4, "SOME TEXT", 0)]))
        self.assertEqual(view.blocks, (bridge.TextBlock(("SOME TEXT",), False),))

    def test_columns_on_one_row_are_kept_apart(self):
        view = bridge.parse(frame([centered(2, "AUDIO"), (8, 4, "OUTPUT", 0), (8, 30, "TV SPEAKERS", 0)]))
        self.assertEqual(view.blocks[0].lines, ("OUTPUT" + " " * 20 + "TV SPEAKERS",))

    def test_lines_at_one_column_stay_left_aligned(self):
        text = "- " + "X" * 60
        view = bridge.parse(frame([centered(2, "NEWS"), (8, 19, text, 0), (9, 19, "- SHORT", 0)]))
        self.assertEqual(view.blocks, (bridge.TextBlock((text, "- SHORT"), False),))

    def test_a_qr_code_is_a_picture_and_its_text_drawing_is_dropped(self):
        code = ["\u2588\u2580\u2580\u2580\u2580\u2580\u2588 \u2584\u2588", "\u2588 \u2588\u2588\u2588 \u2588\u2584 \u2588"]
        ops = [centered(1, "TYPE ON PHONE"), centered(3, "http://x/t/abc")] + [(5 + n, 40, line, 0) for n, line in enumerate(code)]
        with_qr = frame(ops)
        with_qr["qr"] = [5, "http://x/t/abc"]
        view = bridge.parse(with_qr)
        self.assertIn(bridge.QrBlock("http://x/t/abc"), view.blocks)
        self.assertFalse(any(isinstance(block, bridge.TextBlock) and "\u2588" in "".join(block.lines) for block in view.blocks))

    def test_split_row(self):
        self.assertEqual(bridge.split_row("SOFTWARE UPDATE  -  0.3.1 AVAILABLE"), bridge.Row("SOFTWARE UPDATE", "0.3.1 AVAILABLE"))
        self.assertEqual(bridge.split_row("PAIR A NEW DEVICE"), bridge.Row("PAIR A NEW DEVICE"))
        self.assertEqual(bridge.split_row("UPDATE CHANNEL: BETA"), bridge.Row("UPDATE CHANNEL", "BETA"))
        self.assertEqual(bridge.split_row("NOTE: THIS IS A LONG SENTENCE THAT GOES ON."),
                         bridge.Row("NOTE: THIS IS A LONG SENTENCE THAT GOES ON."))

    def test_visible_keeps_the_focus_in_the_middle(self):
        self.assertEqual(bridge.visible(5, 4, 8), range(5))
        self.assertEqual(bridge.visible(40, 20, 9), range(16, 25))
        self.assertEqual(bridge.visible(40, 39, 9), range(31, 40))


class KeyForTest(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(bridge.key_for("Down"), curses.KEY_DOWN)
        self.assertEqual(bridge.key_for("Return"), 10)
        self.assertEqual(bridge.key_for("Escape"), 27)
        self.assertEqual(bridge.key_for("F12"), curses.KEY_F0 + 12)
        self.assertEqual(bridge.key_for("a", "a"), "a")
        self.assertIsNone(bridge.key_for("Shift_L", ""))


if __name__ == "__main__":
    unittest.main()
