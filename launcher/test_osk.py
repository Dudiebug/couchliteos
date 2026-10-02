import io
import os
import pathlib
import subprocess
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


class PanelLayoutTest(unittest.TestCase):
    """Cage docks the keyboard in the bottom 40% of the screen: about 13 terminal rows."""

    # Screens from 600 px to 4K, with the font couchliteos-foot picks for the keyboard panel.
    SCREENS = ((800, 600), (1280, 720), (1366, 768), (1920, 1080), (2560, 1440), (3840, 2160))

    def panel_terminals(self):
        import couchliteos_foot as foot

        for width, height in self.SCREENS:
            size = foot.panel_font_size(width, height)
            yield (width, height), foot.grid(width, foot.panel_height(height), size)

    def test_every_part_has_its_own_row_inside_the_panel(self):
        for height in range(osk.MIN_ROWS, 40):
            places = osk.layout(height)
            rows = [places.text, *places.keys, places.hint]
            self.assertEqual(len(rows), len(set(rows)), height)
            low, high = (1, height - 1) if places.border else (0, height)
            self.assertTrue(all(low <= row < high for row in rows), (height, places))
            self.assertEqual(list(places.keys), sorted(places.keys), height)
            self.assertLess(places.text, places.keys[0], height)
            self.assertLess(places.keys[-1], places.hint, height)
            self.assertEqual(places.title, 0 if places.border else None, height)

    def test_ten_rows_get_the_border_and_title_and_thirteen_get_spacing(self):
        self.assertEqual(osk.MIN_ROWS, 8)
        self.assertFalse(osk.layout(9).border)
        self.assertTrue(osk.layout(10).border)
        places = osk.layout(13)
        self.assertEqual(places.keys[0] - places.text, 2)  # a blank line under the text
        self.assertEqual(places.keys[4] - places.keys[3], 2)  # letters apart from the actions
        self.assertEqual(places.hint - places.keys[-1], 2)
        self.assertEqual(places.hint, 11)  # the bottom border is row 12

    def test_below_eight_rows_the_hint_goes_before_any_key(self):
        places = osk.layout(7)
        self.assertIsNone(places.hint)
        self.assertEqual(places.keys[-1], 6)

    def test_the_foot_font_gives_the_panel_room_for_the_whole_layout(self):
        for screen, (columns, rows) in self.panel_terminals():
            self.assertGreaterEqual(rows, 12, screen)
            self.assertLessEqual(rows, 15, screen)
            self.assertGreaterEqual(columns, 80, screen)

    def test_every_button_is_visible_in_the_docked_panel(self):
        terminals = [terminal for _screen, terminal in self.panel_terminals()]
        for columns, rows in (*terminals, (47, 8), (47, 9), (65, 10), (80, 12)):
            for shift, symbols, masked in ((False, False, False), (True, False, False), (False, True, True)):
                keyboard = osk.Keyboard()
                keyboard.shift, keyboard.symbols, keyboard.masked = shift, symbols, masked
                keyboard.text = "secret"
                keyboard.row, keyboard.column = 5, 3
                screen = DrawScreen(rows, columns)
                osk.draw(screen, keyboard)
                where = (columns, rows, shift, symbols)
                self.assertTrue(all(cell[3] for cell in screen.cells), (where, screen.cells))
                lines = [cell[0] for cell in screen.cells]
                self.assertEqual(len(lines), len(set(lines)), (where, "two lines share a row"))
                self.assertLess(max(lines), rows, where)
                shown = " ".join(cell[2] for cell in screen.cells)
                for label in osk.ACTIONS:
                    self.assertIn(label, shown, where)
                self.assertIn(osk.HINT if columns >= len(osk.HINT) + 2 else osk.SHORT_HINT, shown, where)
                self.assertIn("******" if masked else "secret", shown, where)
                self.assertNotIn("secret" if masked else "******", shown, where)


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


class FakeWlrctl:
    """wlrctl toplevel list/find/focus over (app_id, title) windows, the first one focused."""

    def __init__(self, windows, timeline=None, focusable=True):
        self.windows = list(windows)
        self.timeline = [] if timeline is None else timeline
        self.focusable = focusable  # False: the focus is refused (the keyboard still has it)

    def matches(self, window, terms):
        for term in terms:
            key, _, value = term.partition(":")
            if key == "state" and value == "active" and window != self.windows[0]:
                return False
            if (key == "app_id" and window[0] != value) or (key == "title" and window[1] != value):
                return False
        return True

    def __call__(self, command, **_kwargs):
        self.timeline.append(("wlrctl", tuple(command[2:])))
        action, terms = command[2], command[3:]
        found = [window for window in self.windows if self.matches(window, terms)]
        if action == "list":
            return subprocess.CompletedProcess(command, 0, "".join(f"{a}: {t}\n" for a, t in found), "")
        if action == "focus" and found and self.focusable:
            self.windows.remove(found[0])
            self.windows.insert(0, found[0])
        return subprocess.CompletedProcess(command, 0 if found else 1, "", "")


LAUNCHER = ("foot", "CouchLiteOS Launcher")
CHROME = ("google-chrome", "Sign in - Google Chrome")


class InjectTest(unittest.TestCase):
    """inject() waits for the compositor to open the new keyboard before typing, and for
    the last release to be read before the keyboard goes away."""

    def run_inject(self, text, enter=False, payload=True, target=None, wlrctl=None):
        timeline = [] if wlrctl is None else wlrctl.timeline  # ("open"/"write"/"syn"/"close"/"sleep"/"wlrctl", detail)

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
                refocused = pathlib.Path(directory) / "osk-refocused"
                if wlrctl is None:
                    result = osk.inject(path, refocused=refocused)
                else:
                    result = osk.inject(path, target, wlrctl, refocused=refocused)
                self.refocused = refocused.exists()
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

    def test_the_text_goes_to_the_window_the_keyboard_was_opened_for(self):
        # Home raised the launcher while the keyboard was open: closing the keyboard focused it.
        wlrctl = FakeWlrctl([LAUNCHER, CHROME])
        target = {"app_id": CHROME[0], "title": CHROME[1]}
        result, timeline, _codes = self.run_inject("ab", target=target, wlrctl=wlrctl)
        self.assertEqual(result, 0)
        self.assertEqual(wlrctl.windows[0], CHROME)
        focus = timeline.index(("wlrctl", ("focus", "app_id:google-chrome", "title:Sign in - Google Chrome")))
        active = timeline.index(
            ("wlrctl", ("find", "app_id:google-chrome", "title:Sign in - Google Chrome", "state:active"))
        )
        opened = timeline.index(("open", "CouchLiteOS Buffered Keyboard"))
        self.assertLess(focus, active)
        self.assertLess(active, opened, "nothing is typed before the window is back in front")
        self.assertEqual(sum(kind == "write" for kind, _detail in timeline), 4)
        self.assertTrue(self.refocused, "a Guide menu opened by Home meanwhile hands the controller back")

    def test_typing_back_into_the_launcher_leaves_its_menu_alone(self):
        wlrctl = FakeWlrctl([CHROME, LAUNCHER])
        result, _timeline, _codes = self.run_inject("a", target={"app_id": LAUNCHER[0], "title": LAUNCHER[1]},
                                                    wlrctl=wlrctl)
        self.assertEqual(result, 0)
        self.assertEqual(wlrctl.windows[0], LAUNCHER)
        self.assertFalse(self.refocused)

    def test_a_window_whose_title_changed_is_found_by_its_app_id(self):
        wlrctl = FakeWlrctl([LAUNCHER, ("google-chrome", "Inbox - Google Chrome")])
        result, timeline, _codes = self.run_inject("a", target={"app_id": CHROME[0], "title": CHROME[1]}, wlrctl=wlrctl)
        self.assertEqual(result, 0)
        self.assertIn(("wlrctl", ("focus", "app_id:google-chrome")), timeline)
        self.assertEqual(wlrctl.windows[0][0], "google-chrome")
        self.assertIn(("open", "CouchLiteOS Buffered Keyboard"), timeline)

    def test_an_app_id_other_windows_share_is_not_enough(self):
        # The launcher and terminal apps are all foot windows: app_id:foot could focus the launcher.
        wlrctl = FakeWlrctl([LAUNCHER, ("foot", "htop")])
        result, timeline, _codes = self.run_inject("a", target={"app_id": "foot", "title": "vim"}, wlrctl=wlrctl)
        self.assertEqual(result, 0)
        self.assertEqual(wlrctl.windows[0], LAUNCHER)
        self.assertNotIn(("wlrctl", ("focus", "app_id:foot")), timeline)
        self.assertFalse(any(kind == "open" for kind, _detail in timeline), "nothing typed into the launcher")

    def test_nothing_is_typed_once_the_window_has_closed(self):
        wlrctl = FakeWlrctl([LAUNCHER])
        with mock.patch("sys.stderr"):
            result, timeline, _codes = self.run_inject("secret", target={"app_id": CHROME[0], "title": CHROME[1]},
                                                       wlrctl=wlrctl)
        self.assertEqual(result, 0)
        self.assertFalse(any(kind in {"open", "write"} for kind, _detail in timeline))
        self.assertFalse(self.refocused, "the launcher keeps the controller")

    def test_nothing_is_typed_while_the_window_cannot_get_the_focus(self):
        wlrctl = FakeWlrctl([LAUNCHER, CHROME], focusable=False)
        with mock.patch("sys.stderr"):
            _result, timeline, _codes = self.run_inject("a", target={"app_id": CHROME[0], "title": CHROME[1]},
                                                        wlrctl=wlrctl)
        self.assertFalse(any(kind == "open" for kind, _detail in timeline))
        # Both matches (app_id and title, then the app_id alone) are tried, each for a while.
        polls = [detail for kind, detail in timeline if kind == "sleep"]
        self.assertEqual(polls, [osk.REFOCUS_POLL] * osk.REFOCUS_TRIES * 2)
        self.assertLessEqual(sum(polls), 2.5, "gives up within a couple of seconds")

    def test_without_a_target_the_focused_window_gets_the_text(self):
        wlrctl = FakeWlrctl([LAUNCHER, CHROME])
        _result, timeline, _codes = self.run_inject("a", target=None, wlrctl=wlrctl)
        self.assertFalse(any(kind == "wlrctl" for kind, _detail in timeline))
        self.assertIn(("open", "CouchLiteOS Buffered Keyboard"), timeline)
        self.assertFalse(self.refocused)

    def test_the_launcher_watches_the_same_file(self):
        self.assertEqual(osk.REFOCUSED, pathlib.Path("/run/couchliteos/osk-refocused"))


class TargetTest(unittest.TestCase):
    def test_the_focused_window_is_the_target(self):
        self.assertEqual(osk.active_toplevel(FakeWlrctl([CHROME, LAUNCHER])), {"app_id": CHROME[0], "title": CHROME[1]})
        self.assertEqual(osk.active_toplevel(FakeWlrctl([("foot", "ONE: first")])), {"app_id": "foot", "title": "ONE: first"})

    def test_no_target_when_it_is_not_known(self):
        self.assertIsNone(osk.active_toplevel(FakeWlrctl([])))
        self.assertIsNone(osk.active_toplevel(FakeWlrctl([("couchliteos-osk", "COUCHLITEOS KEYBOARD"), CHROME])))
        self.assertIsNone(osk.active_toplevel(mock.Mock(side_effect=FileNotFoundError("wlrctl"))))
        self.assertIsNone(osk.active_toplevel(mock.Mock(side_effect=subprocess.TimeoutExpired("wlrctl", 5))))
        failed = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "no compositor"))
        self.assertIsNone(osk.active_toplevel(failed))

    def test_the_target_survives_the_command_line(self):
        target = {"app_id": "", "title": "Moonlight \"PC\" : 1"}
        with mock.patch.object(osk, "active_toplevel", return_value=target), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(osk.main(["--target"]), 0)
        with mock.patch.object(osk, "inject", return_value=0) as inject:
            osk.main(["--inject", stdout.getvalue().strip()])
        inject.assert_called_once_with(target=target)

    def test_inject_without_a_known_target_types_into_the_focused_window(self):
        for argv in (["--inject"], ["--inject", ""], ["--inject", "not json"], ["--inject", '{"app_id": 1}'],
                     ["--inject", '{"app_id": "", "title": ""}'], ["--inject", "[]"]):
            with self.subTest(argv=argv), mock.patch.object(osk, "inject", return_value=0) as inject:
                osk.main(argv)
                inject.assert_called_once_with(target=None)

    def test_target_prints_nothing_when_unknown(self):
        with mock.patch.object(osk, "active_toplevel", return_value=None), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(osk.main(["--target"]), 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_the_session_remembers_the_target_before_the_keyboard_opens(self):
        session = (pathlib.Path(__file__).resolve().parent.parent / "scripts/couchliteos-osk-session").read_text()
        remember = session.index("target=$(/usr/libexec/couchliteos-osk --target) || target=\n")
        self.assertLess(remember, session.index("/usr/libexec/couchliteos-foot --app-id=couchliteos-osk"))
        self.assertTrue(session.rstrip().endswith('exec /usr/libexec/couchliteos-osk --inject "$target"'))


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
