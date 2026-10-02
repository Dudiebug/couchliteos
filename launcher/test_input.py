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
        self.assertTrue(settings.keyboard_home)
        self.assertEqual((settings.pad_speed, settings.mouse_speed), ("normal", "normal"))

    def test_save_and_load_round_trip_every_choice(self):
        for home, _label in inputprefs.HOME_CHOICES:
            for speed, _label in inputprefs.SPEED_CHOICES:
                for keyboard in (True, False):
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
            "KEYBOARD HOME SHORTCUT (CTRL+ALT+H)  ON",
            "CONTROLLER MOUSE SPEED  NORMAL",
            "MOUSE SPEED  NORMAL",
        ])
        rows = inputprefs.rows(inputprefs.Settings("guide", False, "very-fast", "slow"))
        self.assertEqual(rows[0], "HOME SHORTCUT  GUIDE ONLY")
        self.assertEqual(rows[1], "KEYBOARD HOME SHORTCUT (CTRL+ALT+H)  OFF")
        self.assertEqual(rows[2], "CONTROLLER MOUSE SPEED  VERY FAST")
        self.assertEqual(rows[3], "MOUSE SPEED  SLOW")
        self.assertEqual([label for _value, label in inputprefs.HOME_CHOICES],
                         ["GUIDE ONLY", "GUIDE OR SELECT+START (HOLD)", "GUIDE OR L3+R3 (HOLD)"])
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
        self.assertFalse(inputprefs.cycle(settings, "keyboard_home").keyboard_home)
        self.assertFalse(inputprefs.cycle(settings, "keyboard_home", -1).keyboard_home)
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
        inputprefs.save_settings(inputprefs.Settings(home="l3-r3", keyboard_home=False), self.config)
        clock["now"] = 3.0
        self.assertEqual(watcher.current(), inputprefs.Settings(home="l3-r3", keyboard_home=False))
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
                mock.patch.object(launcher.controls, "show") as show:
            menu.run_controls()
        return frames, show, send

    def test_the_rows_render_on_80x24(self):
        frames, _show, _send = self.run_screen([27])
        text = "\n".join(frames[0])
        for row in ("CONTROLS", "HOME SHORTCUT  GUIDE OR SELECT+START (HOLD)",
                    "KEYBOARD HOME SHORTCUT (CTRL+ALT+H)  ON", "CONTROLLER MOUSE SPEED  NORMAL",
                    "MOUSE SPEED  NORMAL", "CONTROLLER BUTTONS", "BACK", inputprefs.HELP["home"]):
            self.assertIn(row, text)

    def test_left_right_and_a_cycle_each_row_and_save(self):
        keys = [curses.KEY_RIGHT, curses.KEY_RIGHT,  # HOME SHORTCUT: L3+R3, then GUIDE ONLY
                curses.KEY_DOWN, 10,  # keyboard shortcut off
                curses.KEY_DOWN, curses.KEY_LEFT,  # controller mouse: SLOW
                curses.KEY_DOWN, curses.KEY_RIGHT,  # mouse: FAST
                27]
        frames, _show, send = self.run_screen(keys)
        saved = inputprefs.load_settings(self.config, self.speed)
        self.assertEqual(saved, inputprefs.Settings("guide", False, "slow", "fast"))
        self.assertEqual(self.speed.read_text(), "0.4\n")
        send.assert_called_once_with()
        last = "\n".join(frames[-1])
        self.assertIn("HOME SHORTCUT  GUIDE ONLY", last)
        self.assertIn("MOUSE SPEED  FAST", last)
        self.assertIn("HOME SHORTCUT  GUIDE OR L3+R3 (HOLD)", "\n".join(frames[1]))

    def test_controller_buttons_opens_the_help_and_back_leaves(self):
        _frames, show, _send = self.run_screen([curses.KEY_UP, curses.KEY_UP, 10, curses.KEY_DOWN, 10])
        show.assert_called_once()


if __name__ == "__main__":
    unittest.main()
