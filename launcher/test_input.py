import curses
import os
import pathlib
import signal
import tempfile
import unittest
from unittest import mock

import couchliteos_input as inputprefs
from test_controls import FakeScreen, load_launcher


class TempDir(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.config = self.root / "config.ini"
        self.speed = self.root / "mouse-speed"


class SettingsFileTest(TempDir):
    def test_defaults_without_a_file(self):
        settings = inputprefs.load_settings(self.config, self.speed)
        self.assertEqual(settings, inputprefs.Settings())
        self.assertEqual(settings.home, "select-start")
        self.assertEqual(settings.keyboard_home, inputprefs.DEFAULT_CHORD)
        self.assertEqual(inputprefs.DEFAULT_CHORD, "KEY_LEFTCTRL+KEY_LEFTALT+KEY_H")
        self.assertEqual((settings.pad_speed, settings.mouse_speed), ("normal", "normal"))

    def test_save_and_load_round_trip_every_choice(self):
        for home, _label in inputprefs.HOME_CHOICES:
            for speed, _label in inputprefs.SPEED_CHOICES:
                for keyboard in (inputprefs.DEFAULT_CHORD, "", "KEY_LEFTMETA+KEY_H"):
                    settings = inputprefs.Settings(home=home, keyboard_home=keyboard, pad_speed=speed)
                    inputprefs.save_settings(settings, self.config)
                    self.assertEqual(inputprefs.load_settings(self.config, self.speed), settings)

    def test_saving_keeps_the_other_sections_and_replaces_its_own(self):
        self.config.write_text("[power]\nblank_minutes = 2\n\n[input]\nhome_shortcut = guide\n\n[cec]\nturn_tv_on = false\n")
        inputprefs.save_settings(inputprefs.Settings(home="l3-r3"), self.config)
        text = self.config.read_text()
        self.assertIn("[power]\nblank_minutes = 2\n", text)
        self.assertIn("[cec]\nturn_tv_on = false\n", text)
        self.assertEqual(text.count("[input]"), 1)
        self.assertIn("home_shortcut = l3-r3", text)
        self.assertNotIn("guide", text)
        self.assertEqual(oct(self.config.stat().st_mode & 0o777), oct(0o640))

    def test_unknown_or_broken_values_give_the_defaults(self):
        self.config.write_text("[input]\nhome_shortcut = start\nkeyboard_home = maybe\ncontroller_mouse_speed = 9\n")
        self.assertEqual(inputprefs.load_settings(self.config, self.speed), inputprefs.Settings())
        self.config.write_text("not an ini file at all\n[[[")
        self.assertEqual(inputprefs.load_settings(self.config, self.speed), inputprefs.Settings())

    def test_the_mouse_speed_comes_from_cages_file(self):
        for text, speed in (("-0.5\n", "slow"), ("0.0\n", "normal"), ("0.4\n", "fast"), ("0.8\n", "very-fast"),
                            ("0.45", "fast"), ("junk", "normal"), ("nan", "normal")):
            with self.subTest(text=text):
                self.speed.write_text(text)
                self.assertEqual(inputprefs.load_settings(self.config, self.speed).mouse_speed, speed)
        self.speed.unlink()
        self.assertEqual(inputprefs.load_mouse_speed(self.speed), "normal")


class ChordTest(TempDir):
    def test_old_on_and_off_values_become_the_default_chord_and_off(self):
        for text, chord in (("true", inputprefs.DEFAULT_CHORD), ("on", inputprefs.DEFAULT_CHORD),
                            ("1", inputprefs.DEFAULT_CHORD), ("false", ""), ("off", ""), ("0", ""), ("none", "")):
            with self.subTest(text=text):
                self.config.write_text(f"[input]\nkeyboard_home = {text}\n")
                self.assertEqual(inputprefs.load_settings(self.config, self.speed).keyboard_home, chord)

    def test_a_saved_chord_is_read_back_in_a_fixed_order_and_a_bad_one_gives_the_default(self):
        for text, chord in (("KEY_H+KEY_LEFTMETA", "KEY_LEFTMETA+KEY_H"),
                            ("key_rightalt+key_rightctrl+key_k", "KEY_LEFTCTRL+KEY_LEFTALT+KEY_K"),
                            ("KEY_H", inputprefs.DEFAULT_CHORD), ("KEY_LEFTMETA+KEY_F5", inputprefs.DEFAULT_CHORD),
                            ("junk", inputprefs.DEFAULT_CHORD), ("KEY_LEFTMETA+", inputprefs.DEFAULT_CHORD)):
            with self.subTest(text=text):
                self.config.write_text(f"[input]\nkeyboard_home = {text}\n")
                self.assertEqual(inputprefs.load_settings(self.config, self.speed).keyboard_home, chord)

    def test_off_is_saved_as_off_and_read_back_as_nothing(self):
        inputprefs.save_settings(inputprefs.Settings(keyboard_home=""), self.config)
        self.assertIn("keyboard_home = off", self.config.read_text())
        self.assertEqual(inputprefs.load_settings(self.config, self.speed).keyboard_home, "")

    def test_labels(self):
        self.assertEqual(inputprefs.chord_label(inputprefs.DEFAULT_CHORD), "CTRL+ALT+H")
        self.assertEqual(inputprefs.chord_label("KEY_RIGHTMETA+KEY_LEFTSHIFT+KEY_H"), "SHIFT+SUPER+H")
        self.assertEqual(inputprefs.chord_label("KEY_LEFTALT+KEY_F3"), "ALT+F3")
        self.assertEqual(inputprefs.chord_label(""), "OFF")
        self.assertEqual(inputprefs.chord_label("nonsense"), "OFF")

    def test_what_can_be_the_home_key(self):
        ok = [{"KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_H"}, {"KEY_LEFTMETA", "KEY_H"}, {"KEY_RIGHTALT", "KEY_G"},
              {"KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_H"}, {"KEY_LEFTALT", "KEY_1"},
              {"KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_0"}, {"KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_K"}]
        for chord in ok:
            with self.subTest(chord=chord):
                self.assertIsNone(inputprefs.validate_chord(chord))
        bad = [set(), {"KEY_H"}, {"KEY_LEFTMETA"}, {"KEY_LEFTCTRL", "KEY_LEFTALT"}, {"KEY_LEFTSHIFT", "KEY_H"},
               {"KEY_LEFTCTRL", "KEY_H", "KEY_J"}, {"KEY_LEFTCTRL", "KEY_ENTER"}, {"KEY_LEFTALT", "KEY_ESC"},
               {"KEY_LEFTCTRL", "KEY_BACKSLASH"}, {"KEY_LEFTCTRL", "KEY_LEFTBRACE"},
               {"KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_N"}, {"KEY_LEFTCTRL", "KEY_0"}, {"KEY_LEFTCTRL", "KEY_MINUS"}, {"KEY_LEFTALT", "KEY_F3"}, {"KEY_LEFTCTRL", "KEY_F9"},
               {"KEY_LEFTALT", "KEY_INSERT"},
               {"KEY_LEFTALT", "KEY_FN_F1"}, {"KEY_LEFTMETA", "KEY_F5"}, {"KEY_LEFTALT", "KEY_F12"}, {"KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_F2"},
               {"KEY_LEFTMETA", "KEY_F3"}, {"KEY_LEFTMETA", "KEY_UP"}, {"KEY_LEFTCTRL", "KEY_C"},
               {"KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_M"}, {"KEY_LEFTALT", "KEY_SPACE"}]
        for chord in bad:
            with self.subTest(chord=chord):
                reason = inputprefs.validate_chord(chord)
                self.assertTrue(reason)
                self.assertEqual(reason, reason.upper())
                self.assertLessEqual(len(reason), 60)

    def test_the_capture_gives_the_keys_held_at_the_first_release_once(self):
        capture = inputprefs.ChordCapture()
        self.assertIsNone(capture.feed("KEY_LEFTCTRL", 1))
        self.assertIsNone(capture.feed("KEY_LEFTALT", 1))
        self.assertIsNone(capture.feed("KEY_H", 1))
        self.assertEqual(capture.feed("KEY_H", 0), frozenset({"KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_H"}))
        self.assertIsNone(capture.feed("KEY_LEFTALT", 0), "the rest of the release is not another try")
        self.assertIsNone(capture.feed("KEY_LEFTCTRL", 0))
        self.assertIsNone(capture.feed("KEY_X", 0), "a release without a press")
        self.assertEqual(capture.held, set())
        self.assertIsNone(capture.feed("KEY_LEFTMETA", 1))
        self.assertEqual(capture.feed("KEY_LEFTMETA", 0), frozenset({"KEY_LEFTMETA"}),
                         "a new try after all keys were up")


class FakeKeyboards:
    """What KeyboardSource gives the capture screen: one scripted round of key events per poll."""

    def __init__(self, rounds, found=True):
        self.rounds = list(rounds)
        self.found = found
        self.closed = False
        self.clock = lambda: None  # time passes with every look at the keyboards

    def open(self):
        return self.found

    def poll(self, _timeout):
        self.clock()
        return self.rounds.pop(0) if self.rounds else []

    def close(self):
        self.closed = True


class CaptureScreenTest(TempDir):
    """Settings > CONTROLS > KEYBOARD HOME KEY: hold the keys, let go, confirm."""

    DOWN, UP = 1, 0
    NOTHING = [-1] * 40  # keys the screen sees while the keyboard is being read

    def capture(self, rounds, keys, settings=None, found=True):
        launcher = load_launcher()
        if settings is not None:
            inputprefs.save_settings(settings, self.config)
        screen = FakeScreen(keys)
        menu = launcher.Settings(screen, mock.Mock())
        source = FakeKeyboards(rounds, found)
        self.pages = []
        draw = menu.draw_capture

        def record(lines, hint):
            draw(lines, hint)
            self.pages.append(("\n".join(lines), hint))

        ticks = iter(1000 + x * 0.1 for x in range(100000))
        source.clock = lambda: next(ticks)
        self.request = self.root / "home.request"
        self.request.touch()
        with mock.patch.object(launcher, "read_key", side_effect=lambda window, **_kw: window.getch()), \
                mock.patch.object(launcher, "HOME_REQUEST", self.request), \
                mock.patch.object(launcher.time, "monotonic", side_effect=lambda: next(ticks)), \
                mock.patch.object(inputprefs, "CONFIG", self.config), \
                mock.patch.object(menu, "draw_capture", side_effect=record):
            try:
                self.notice = menu.capture_home_key(source)
            except RuntimeError as stop:  # the screen never closed
                self.notice = str(stop)
        self.source = source
        return inputprefs.load_settings(self.config, self.speed).keyboard_home

    @staticmethod
    def chord(*names):
        """Rounds that press the keys in turn and let go in reverse, like a hand does."""
        return [[(name, 1)] for name in names] + [[(name, 0)] for name in reversed(names)]

    def test_a_chord_is_set_when_confirmed_and_the_old_key_is_forgotten(self):
        rounds = self.chord("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_G")
        saved = self.capture(rounds, self.NOTHING + [10])
        self.assertEqual(saved, "KEY_LEFTCTRL+KEY_LEFTALT+KEY_G")
        self.assertEqual(self.notice, "KEYBOARD HOME KEY  CTRL+ALT+G")
        self.assertTrue(any("SET THE HOME KEY TO CTRL+ALT+G?" in text for text, _hint in self.pages))
        self.assertTrue(self.source.closed)
        self.assertFalse(self.request.exists(), "the old key, pressed while capturing, asked for Home")

    def test_the_chord_is_read_at_the_first_release_and_right_hand_keys_count(self):
        rounds = self.chord("KEY_RIGHTCTRL", "KEY_RIGHTALT", "KEY_K")
        self.assertEqual(self.capture(rounds, self.NOTHING + [10]), "KEY_LEFTCTRL+KEY_LEFTALT+KEY_K")

    def test_not_confirming_keeps_the_old_key(self):
        rounds = self.chord("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_G")
        saved = self.capture(rounds, self.NOTHING + [27])
        self.assertEqual(saved, inputprefs.DEFAULT_CHORD)
        self.assertEqual(self.notice, "NOT CHANGED")

    def test_a_chord_with_super_warns_about_the_remote_pc(self):
        rounds = self.chord("KEY_LEFTMETA", "KEY_H")
        self.assertEqual(self.capture(rounds, self.NOTHING + [10]), "KEY_LEFTMETA+KEY_H")
        confirm = [text for text, _hint in self.pages if "SET THE HOME KEY TO SUPER+H?" in text]
        self.assertTrue(confirm and "SUPER KEY" in confirm[0])

    def test_a_chord_that_cannot_work_says_why_and_keeps_listening(self):
        rounds = self.chord("KEY_LEFTCTRL", "KEY_C") + self.chord("KEY_LEFTALT", "KEY_F5") + self.chord(
            "KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_G")
        saved = self.capture(rounds, self.NOTHING + [10])
        self.assertEqual(saved, "KEY_LEFTCTRL+KEY_LEFTALT+KEY_G", "the third try worked")
        reasons = {inputprefs.validate_chord({"KEY_LEFTCTRL", "KEY_C"}), inputprefs.validate_chord({"KEY_LEFTALT", "KEY_F5"})}
        shown = "\n".join(text for text, _hint in self.pages)
        for reason in reasons:
            self.assertIn(reason, shown)

    def test_a_key_pressed_alone_is_not_a_chord(self):
        saved = self.capture(self.chord("KEY_H"), self.NOTHING + [27])
        self.assertEqual(saved, inputprefs.DEFAULT_CHORD)
        self.assertIn(inputprefs.validate_chord({"KEY_H"}), "\n".join(text for text, _hint in self.pages))

    def test_esc_on_the_keyboard_cancels_without_asking(self):
        saved = self.capture(self.chord("KEY_ESC"), self.NOTHING)
        self.assertEqual((saved, self.notice), (inputprefs.DEFAULT_CHORD, "NOT CHANGED"))

    def test_esc_from_a_pad_cancels_only_when_no_keyboard_key_is_down(self):
        # An ESC byte while Ctrl is held belongs to a chord under way, not to a B press.
        rounds = [[("KEY_LEFTCTRL", 1)], [], [], [("KEY_LEFTCTRL", 0)]]
        keys = [-1, 27, 27, -1] + self.NOTHING + [27]
        saved = self.capture(rounds, keys)
        self.assertEqual((saved, self.notice), (inputprefs.DEFAULT_CHORD, "NOT CHANGED"))


    def test_an_alt_chord_whose_esc_arrives_before_its_key_events_is_not_a_cancel(self):
        # foot writes the ESC of Ctrl+Alt+G at once; the keyboard events are read just after it.
        rounds = [[], [("KEY_LEFTCTRL", 1), ("KEY_LEFTALT", 1), ("KEY_G", 1)],
                  [("KEY_G", 0)], [("KEY_LEFTALT", 0)], [("KEY_LEFTCTRL", 0)]]
        saved = self.capture(rounds, [27] + self.NOTHING + [10])
        self.assertEqual(saved, "KEY_LEFTCTRL+KEY_LEFTALT+KEY_G")

    def test_the_screen_waits_briefly_for_keys_while_capturing_and_goes_back_after(self):
        launcher = load_launcher()
        screen = FakeScreen([])
        waits = []
        screen.timeout = waits.append
        menu = launcher.Settings(screen, mock.Mock())
        with mock.patch.object(launcher, "read_key", side_effect=lambda window, **_kw: window.getch()),                 mock.patch.object(launcher, "HOME_REQUEST", self.root / "home.request"),                 mock.patch.object(inputprefs, "CONFIG", self.config):
            with self.assertRaises(RuntimeError):
                menu.capture_home_key(FakeKeyboards([]))
        self.assertEqual(waits, [menu.CAPTURE_POLL_MS, 1000])
        self.assertLessEqual(menu.CAPTURE_POLL_MS, 100)

    def test_a_keyboard_that_goes_away_ends_the_capture(self):
        source = FakeKeyboards([])
        source.devices = []
        launcher = load_launcher()
        screen = FakeScreen(self.NOTHING)
        menu = launcher.Settings(screen, mock.Mock())
        with mock.patch.object(launcher, "read_key", side_effect=lambda window, **_kw: window.getch()),                 mock.patch.object(launcher, "HOME_REQUEST", self.root / "home.request"),                 mock.patch.object(inputprefs, "CONFIG", self.config):
            self.assertEqual(menu.capture_home_key(source), "KEYBOARD DISCONNECTED")

    def test_y_turns_it_off_and_x_goes_back_to_the_default(self):
        self.assertEqual(self.capture([], self.NOTHING + [curses.KEY_DC]), "")
        self.assertEqual(self.notice, "KEYBOARD HOME KEY  OFF")
        self.assertEqual(self.capture([], self.NOTHING + [curses.KEY_F12],
                                      inputprefs.Settings(keyboard_home="KEY_LEFTMETA+KEY_H")), inputprefs.DEFAULT_CHORD)
        self.assertEqual(self.notice, "KEYBOARD HOME KEY  CTRL+ALT+H")

    def test_delete_and_f12_on_the_keyboard_turn_it_off_and_reset_it_too(self):
        self.assertEqual(self.capture(self.chord("KEY_DELETE"), self.NOTHING), "")
        self.assertEqual(self.notice, "KEYBOARD HOME KEY  OFF")
        self.assertEqual(self.capture(self.chord("KEY_F12"), self.NOTHING,
                                      inputprefs.Settings(keyboard_home="KEY_LEFTMETA+KEY_H")), inputprefs.DEFAULT_CHORD)

    def test_the_screen_names_the_current_key_and_fits_80x24(self):
        self.capture([], self.NOTHING + [27], inputprefs.Settings(keyboard_home="KEY_LEFTMETA+KEY_H"))
        text, hint = self.pages[0]
        self.assertIn("NOW: SUPER+H", text)
        self.assertLessEqual(len(hint), 76)

    def test_without_a_keyboard_it_says_so(self):
        self.assertEqual(self.capture([], [], found=False), inputprefs.DEFAULT_CHORD)
        self.assertEqual(self.notice, "NO KEYBOARD FOUND")


class MouseSpeedTest(TempDir):
    def test_the_steps_are_libinput_acceleration_speeds(self):
        self.assertEqual(inputprefs.MOUSE_SPEEDS, {"slow": -0.5, "normal": 0.0, "fast": 0.4, "very-fast": 0.8})
        self.assertEqual(inputprefs.MOUSE_SPEED_FILE, pathlib.Path("/var/lib/couchliteos/mouse-speed"))

    def test_the_file_holds_the_plain_float(self):
        for speed, text in (("slow", "-0.5\n"), ("normal", "0.0\n"), ("fast", "0.4\n"), ("very-fast", "0.8\n")):
            with self.subTest(speed=speed):
                inputprefs.write_mouse_speed(speed, self.speed)
                self.assertEqual(self.speed.read_text(), text)
                self.assertEqual(float(self.speed.read_text()), inputprefs.MOUSE_SPEEDS[speed])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["mouse-speed"], "no temporary left")

    def test_applying_writes_then_signals(self):
        order = []
        send = mock.Mock(side_effect=lambda: order.append(self.speed.read_text()) or 1)
        self.assertEqual(inputprefs.apply_mouse_speed("fast", self.speed, send), 1)
        self.assertEqual(order, ["0.4\n"], "Cage is told after the file is in place")

    def test_signal_sends_sighup_to_each_cage_and_never_raises(self):
        kill = mock.Mock()
        self.assertEqual(inputprefs.signal_compositor(kill=kill, pids=lambda: [10, 20]), 2)
        self.assertEqual(kill.call_args_list, [mock.call(10, signal.SIGHUP), mock.call(20, signal.SIGHUP)])
        gone = mock.Mock(side_effect=[ProcessLookupError(), PermissionError(), None])
        self.assertEqual(inputprefs.signal_compositor(kill=gone, pids=lambda: [1, 2, 3]), 1)
        self.assertEqual(inputprefs.signal_compositor(kill=kill, pids=lambda: []), 0)
        broken = mock.Mock(side_effect=OSError("no /proc"))
        self.assertEqual(inputprefs.signal_compositor(kill=kill, pids=broken), 0)

    def fake_process(self, proc, pid, comm, exe=b"... /var/lib/couchliteos/mouse-speed ..."):
        entry = proc / str(pid)
        entry.mkdir(parents=True)
        (entry / "comm").write_text(comm + "\n")
        (entry / "exe").write_bytes(exe)
        return entry

    def test_only_this_users_patched_cage_processes_are_found(self):
        proc = self.root / "proc"
        self.fake_process(proc, 42, "cage")
        self.fake_process(proc, 7, "cage")
        self.fake_process(proc, 50, "foot")
        self.fake_process(proc, 60, "cage", exe=b"an unpatched cage: SIGHUP would end the session")
        (proc / "self").mkdir()
        (proc / "uptime").write_text("1 2\n")
        uid = os.stat(proc).st_uid
        self.assertEqual(inputprefs.compositor_pids(proc, uid=uid), [7, 42])
        self.assertEqual(inputprefs.compositor_pids(proc, uid=uid + 1), [], "another user's Cage is left alone")
        self.assertEqual(inputprefs.compositor_pids(self.root / "missing", uid=uid), [])

    def test_the_default_sender_survives_a_machine_without_cage(self):
        kill = mock.Mock()
        with mock.patch.object(inputprefs, "PROC", self.root / "missing"):
            self.assertEqual(inputprefs.signal_compositor(kill=kill), 0)
        kill.assert_not_called()

    def test_the_default_paths_are_looked_up_when_called(self):
        send = mock.Mock(return_value=0)
        with mock.patch.object(inputprefs, "CONFIG", self.config), \
                mock.patch.object(inputprefs, "MOUSE_SPEED_FILE", self.speed), \
                mock.patch.object(inputprefs, "signal_compositor", send):
            inputprefs.change(inputprefs.Settings(), "mouse_speed", 1)
            inputprefs.change(inputprefs.Settings(), "home", 1)
            self.assertEqual(inputprefs.load_settings(), inputprefs.Settings(home="l3-r3", mouse_speed="fast"))
        send.assert_called_once_with()
        self.assertEqual(self.speed.read_text(), "0.4\n")


class RowsAndCycleTest(TempDir):
    def test_rows(self):
        self.assertEqual(inputprefs.rows(inputprefs.Settings()), [
            "HOME SHORTCUT  GUIDE OR SELECT+START (HOLD)",
            "KEYBOARD HOME KEY  CTRL+ALT+H",
            "CONTROLLER MOUSE SPEED  NORMAL",
            "MOUSE SPEED  NORMAL",
        ])
        rows = inputprefs.rows(inputprefs.Settings("guide", "", "very-fast", "slow"))
        self.assertEqual(rows[0], "HOME SHORTCUT  GUIDE (SELECT+START IN A STREAM)")
        self.assertEqual(rows[1], "KEYBOARD HOME KEY  OFF")
        self.assertEqual(rows[2], "CONTROLLER MOUSE SPEED  VERY FAST")
        self.assertEqual(rows[3], "MOUSE SPEED  SLOW")
        self.assertEqual([label for _value, label in inputprefs.HOME_CHOICES],
                         ["GUIDE (SELECT+START IN A STREAM)", "GUIDE OR SELECT+START (HOLD)", "GUIDE OR L3+R3 (HOLD)"])
        for text in [*rows, *inputprefs.HELP.values()]:
            self.assertLessEqual(len(text), 76)
            self.assertEqual(text, text.upper())

    def test_cycling_wraps_both_ways(self):
        settings = inputprefs.Settings()
        self.assertEqual(inputprefs.cycle(settings, "home").home, "l3-r3")
        self.assertEqual(inputprefs.cycle(settings, "home", -1).home, "guide")
        self.assertEqual(inputprefs.cycle(inputprefs.Settings(home="l3-r3"), "home").home, "guide")
        self.assertEqual(inputprefs.cycle(settings, "pad_speed").pad_speed, "fast")
        self.assertEqual(inputprefs.cycle(inputprefs.Settings(pad_speed="slow"), "pad_speed", -1).pad_speed, "very-fast")
        self.assertEqual(inputprefs.cycle(settings, "keyboard_home"), settings, "the Home key is captured, not cycled")
        self.assertEqual(inputprefs.cycle(settings, "mouse_speed").mouse_speed, "fast")

    def test_change_saves_the_controller_choices_to_config_and_the_mouse_speed_to_cage(self):
        send = mock.Mock(return_value=1)
        settings, message = inputprefs.change(inputprefs.Settings(), "pad_speed", 1, self.config, self.speed, send)
        self.assertEqual(message, "CONTROLLER MOUSE SPEED  FAST")
        self.assertEqual(inputprefs.load_settings(self.config, self.speed).pad_speed, "fast")
        self.assertFalse(self.speed.exists())
        send.assert_not_called()
        settings, message = inputprefs.change(settings, "mouse_speed", -1, self.config, self.speed, send)
        self.assertEqual(settings.mouse_speed, "slow")
        self.assertEqual(self.speed.read_text(), "-0.5\n")
        send.assert_called_once_with()
        self.assertEqual(inputprefs.load_settings(self.config, self.speed), settings)

    def test_a_mouse_speed_no_cage_heard_says_when_it_applies(self):
        _settings, message = inputprefs.change(
            inputprefs.Settings(), "mouse_speed", 1, self.config, self.speed, lambda: 0)
        self.assertEqual(message, "MOUSE SPEED  FAST (FROM THE NEXT RESTART)")

    def test_a_failed_save_keeps_the_old_choice(self):
        blocked = self.root / "file"
        blocked.write_text("")
        settings, message = inputprefs.change(
            inputprefs.Settings(), "home", 1, blocked / "config.ini", blocked / "mouse-speed", lambda: 1)
        self.assertEqual(settings, inputprefs.Settings())
        self.assertTrue(message.startswith("COULD NOT SAVE"))
        settings, message = inputprefs.change(
            inputprefs.Settings(), "mouse_speed", 1, blocked / "config.ini", blocked / "mouse-speed", lambda: 1)
        self.assertEqual(settings.mouse_speed, "normal")
        self.assertTrue(message.startswith("COULD NOT SAVE"))


class WatcherTest(TempDir):
    def test_rereads_only_when_the_file_changes_and_at_most_every_interval(self):
        clock = {"now": 0.0}
        loads = []

        def load(path):
            loads.append(path)
            return inputprefs.load_settings(path, self.speed)

        watcher = inputprefs.Watcher(self.config, interval=0.5, clock=lambda: clock["now"], load=load)
        self.assertEqual(watcher.current(), inputprefs.Settings())
        inputprefs.save_settings(inputprefs.Settings(home="guide"), self.config)
        clock["now"] = 0.2
        self.assertEqual(watcher.current().home, "select-start", "not looked at again yet")
        clock["now"] = 0.6
        self.assertEqual(watcher.current().home, "guide")
        clock["now"] = 2.0
        watcher.current()
        self.assertEqual(len(loads), 1, "an unchanged file is not parsed again")
        inputprefs.save_settings(inputprefs.Settings(home="l3-r3", keyboard_home="KEY_LEFTMETA+KEY_H"), self.config)
        clock["now"] = 3.0
        self.assertEqual(watcher.current(), inputprefs.Settings(home="l3-r3", keyboard_home="KEY_LEFTMETA+KEY_H"))
        self.config.unlink()
        clock["now"] = 4.0
        self.assertEqual(watcher.current(), inputprefs.Settings(), "a removed file means the defaults")

    def test_a_clock_that_goes_backwards_still_rereads(self):
        clock = {"now": 100.0}
        watcher = inputprefs.Watcher(self.config, interval=0.5, clock=lambda: clock["now"])
        watcher.current()
        inputprefs.save_settings(inputprefs.Settings(pad_speed="slow"), self.config)
        clock["now"] = 1.0
        self.assertEqual(watcher.current().pad_speed, "slow")


class ControlsScreenTest(TempDir):
    """Settings > CONTROLS in the launcher: the rows render and LEFT/RIGHT or A cycles them."""

    def run_screen(self, keys, settings=None):
        launcher = load_launcher()
        if settings is not None:
            inputprefs.save_settings(settings, self.config)
        screen = FakeScreen(keys)
        frames = []
        menu = launcher.Settings(screen, mock.Mock())
        draw = menu.draw

        def record(*args, **kwargs):
            draw(*args, **kwargs)
            frames.append([item[2] for item in screen.drawn])

        send = mock.Mock(return_value=1)
        with mock.patch.object(launcher, "read_key", side_effect=lambda window: window.getch()), \
                mock.patch.object(menu, "draw", side_effect=record), \
                mock.patch.object(inputprefs, "CONFIG", self.config), \
                mock.patch.object(inputprefs, "MOUSE_SPEED_FILE", self.speed), \
                mock.patch.object(inputprefs, "signal_compositor", send), \
                mock.patch.object(launcher.controls, "show") as show, \
                mock.patch.object(launcher.controls, "show_keys") as show_keys, \
                mock.patch.object(menu, "capture_home_key", return_value="CAPTURED") as capture:
            menu.run_controls()
        self.show_keys, self.capture = show_keys, capture
        return frames, show, send

    def test_the_rows_render_on_80x24(self):
        frames, _show, _send = self.run_screen([27])
        text = "\n".join(frames[0])
        for row in ("CONTROLS", "HOME SHORTCUT  GUIDE OR SELECT+START (HOLD)",
                    "KEYBOARD HOME KEY  CTRL+ALT+H", "CONTROLLER MOUSE SPEED  NORMAL",
                    "MOUSE SPEED  NORMAL", "CONTROLLER BUTTONS", "KEYBOARD KEYS", "BACK", inputprefs.HELP["home"]):
            self.assertIn(row, text)

    def test_left_right_and_a_cycle_each_row_and_save(self):
        keys = [curses.KEY_RIGHT, curses.KEY_RIGHT,  # HOME SHORTCUT: L3+R3, then GUIDE (SELECT+START IN A STREAM)
                curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT,  # the Home key is not cycled
                curses.KEY_DOWN, curses.KEY_LEFT,  # controller mouse: SLOW
                curses.KEY_DOWN, curses.KEY_RIGHT,  # mouse: FAST
                27]
        frames, _show, send = self.run_screen(keys)
        saved = inputprefs.load_settings(self.config, self.speed)
        self.assertEqual(saved, inputprefs.Settings("guide", inputprefs.DEFAULT_CHORD, "slow", "fast"))
        self.capture.assert_not_called()
        self.assertEqual(self.speed.read_text(), "0.4\n")
        send.assert_called_once_with()
        last = "\n".join(frames[-1])
        self.assertIn("HOME SHORTCUT  GUIDE (SELECT+START IN A STREAM)", last)
        self.assertIn("MOUSE SPEED  FAST", last)
        self.assertIn("HOME SHORTCUT  GUIDE OR L3+R3 (HOLD)", "\n".join(frames[1]))

    def test_controller_buttons_opens_the_help_and_back_leaves(self):
        # UP wraps to BACK, UP again is KEYBOARD KEYS, UP again is CONTROLLER BUTTONS.
        _frames, show, _send = self.run_screen(
            [curses.KEY_UP, curses.KEY_UP, curses.KEY_UP, 10, curses.KEY_DOWN, curses.KEY_DOWN, 10])
        show.assert_called_once()
        self.show_keys.assert_not_called()

    def test_keyboard_keys_opens_its_page(self):
        _frames, show, _send = self.run_screen([curses.KEY_UP, curses.KEY_UP, 10, curses.KEY_DOWN, 10])
        self.show_keys.assert_called_once()
        show.assert_not_called()

    def test_enter_on_the_home_key_row_opens_capture_and_shows_its_notice(self):
        frames, _show, _send = self.run_screen([curses.KEY_DOWN, 10, 27])
        self.capture.assert_called_once_with()
        self.assertIn("CAPTURED", "\n".join(frames[-1]))


if __name__ == "__main__":
    unittest.main()
