"""The Guide / Home menu (ACTIVE APPLICATIONS): resume, close, type into an app, volume, brightness,
controller mouse."""

import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import couchliteos_pointer as pointer
import couchliteos_quick


class Screen:
    """A curses window stand-in: hands out scripted keys and keeps the text of each frame."""

    def __init__(self, keys=()):
        self.keys = list(keys)
        self.frames = [[]]

    def getmaxyx(self):
        return 30, 100

    def erase(self):
        self.frames.append([])

    def border(self, *_args):
        pass

    def addstr(self, _row, _column, text, *_args):
        self.frames[-1].append(text)

    addnstr = addstr

    def refresh(self):
        pass

    def timeout(self, *_args):
        pass

    def keypad(self, *_args):
        pass

    def getch(self):
        if not self.keys:
            raise AssertionError("ACTIVE APPLICATIONS asked for more keys than the test gave")
        return self.keys.pop(0)

    def last(self):
        return "\n".join(next(frame for frame in reversed(self.frames) if frame))


class GuideFixture(unittest.TestCase):
    """Shared fixtures: a temporary /run/couchliteos, fake wlrctl, audio and backlight, three apps."""

    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_guide", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.curses = cls.module.curses

    def setUp(self):
        module = self.module
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = pathlib.Path(directory.name)
        self.flag = self.run_dir / "pointer-mode"
        self.modes = pointer.Modes(self.flag)
        self.volume = module.audio.Volume(40, False)
        self.brightness = None  # most PCs have no backlight: no BRIGHTNESS row
        self.focus_result = 0
        self.wlrctl = []

        def fake_run(command, *_args, **_kwargs):
            self.wlrctl.append(command[-1])
            return mock.Mock(returncode=self.focus_result)

        for target, attribute, value in (
            (module, "RUN", self.run_dir),
            (module, "HOME_REQUEST", self.run_dir / "home.request"),
            (module, "LAUNCHER_FOCUS", self.run_dir / "launcher-focus"),
            (module, "POINTER_MODES", self.modes),
            (module, "active_rdp_session", mock.Mock(return_value=None)),
            (module.power, "can_suspend", mock.Mock(return_value=True)),
            (module.power, "wake_sources", mock.Mock(return_value=["USB KEYBOARD"])),
            (module.whatsnew, "show_once", mock.Mock()),
            (module.subprocess, "run", mock.Mock(side_effect=fake_run)),
            (module.audio, "get_volume", mock.Mock(side_effect=lambda *_args: self.volume)),
            (module.audio, "change_volume", mock.Mock()),
            (module.audio, "toggle_mute", mock.Mock()),
            (module.audio.MicMonitor, "start", mock.Mock()),  # its thread would run pw-dump through fake_run
            (module.brightness, "get_percent", mock.Mock(side_effect=lambda *_args: self.brightness)),
            (module.brightness, "change", mock.Mock()),
        ):
            patcher = mock.patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.chrome = module.apps.Application(
            id="google-chrome", name="GOOGLE CHROME", kind="request", request="google-chrome.request",
            status_id="google-chrome",
        )
        self.moonlight = module.apps.Application(
            id="moonlight", name="MOONLIGHT", kind="request", request="moonlight.request", status_id="moonlight",
        )
        self.firefox = module.apps.Application(
            id="firefox", name="FIREFOX", kind="request", request="firefox.request", status_id="firefox",
        )
        self.set_apps([self.chrome, self.moonlight, self.firefox])

    def set_apps(self, applications):
        result = self.module.apps.LoadResult(tuple(applications), ())
        patcher = mock.patch.object(self.module, "application_result", return_value=result)
        patcher.start()
        self.addCleanup(patcher.stop)

    def running(self, *applications):
        for app in applications:
            (self.run_dir / f"{app.status_id}-ready").touch()

    def guide(self, keys):
        """Open the Guide menu with these keys; returns (screen, launcher)."""
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(Screen())
        launcher.screen = Screen(keys)
        launcher.active_applications()
        self.assertEqual(launcher.screen.keys, [], "the menu closed before using every key")
        return launcher.screen, launcher

    def rows(self, screen, frame=-1):
        frames = [frame_text for frame_text in screen.frames if frame_text]
        return [text[3:].rstrip() for text in frames[frame] if text[:1] in (">", " ") and text[1:3] == "  "]


class GuideMenuTest(GuideFixture):
    def test_every_hint_names_the_keyboard_key_too(self):
        # 0.3.0: a hint that names a controller button also names the key that does the same.
        self.running(self.chrome, self.moonlight)
        self.brightness = 50
        down = self.curses.KEY_DOWN
        screen, _launcher = self.guide([down] * 6 + [27])
        hints = {text.strip() for frame in screen.frames for text in frame
                 if text.strip() and not (text[:1] in (">", " ") and text[1:3] == "  ")}
        self.assertTrue(any("RESUMES" in hint for hint in hints), hints)
        self.assertTrue(any("CHANGES THE VOLUME" in hint for hint in hints), hints)
        self.assertTrue(any("TURNS IT ON OR OFF" in hint for hint in hints), hints)
        for hint in hints:
            self.assertTrue(couchliteos_quick.names_keyboard_keys(hint), hint)

    # The rows

    def test_rows_list_running_apps_then_volume_mouse_and_return(self):
        self.running(self.chrome, self.moonlight)
        screen, _launcher = self.guide([27])
        rows = self.rows(screen)
        self.assertEqual(len(rows), 5, rows)
        self.assertRegex(rows[0], r"^GOOGLE CHROME +RUNNING$")
        self.assertRegex(rows[1], r"^MOONLIGHT +RUNNING$")
        self.assertEqual(rows[2], "VOLUME  40%")
        self.assertRegex(rows[3], r"^CONTROLLER MOUSE \(GOOGLE CHROME\) +ON$")
        self.assertEqual(rows[4], "RETURN TO MAIN LAUNCHER")

    def test_the_mouse_row_is_for_the_app_last_brought_to_the_front(self):
        self.running(self.chrome, self.moonlight)
        self.modes.front = "moonlight"
        screen, _launcher = self.guide([27])
        self.assertRegex(self.rows(screen)[3], r"^CONTROLLER MOUSE \(MOONLIGHT\) +OFF$")

    def test_a_front_app_that_closed_falls_back_to_the_first_running_one(self):
        self.running(self.moonlight)
        self.modes.front = "google-chrome"
        screen, _launcher = self.guide([27])
        self.assertRegex(self.rows(screen)[2], r"^CONTROLLER MOUSE \(MOONLIGHT\) +OFF$")

    def test_with_nothing_running_there_is_no_mouse_row(self):
        screen, _launcher = self.guide([self.curses.KEY_DOWN, 27])
        self.assertEqual(self.rows(screen), ["VOLUME  40%", "RETURN TO MAIN LAUNCHER"])
        self.assertIn("NO MANAGED APPLICATIONS ARE RUNNING", screen.last())  # under RETURN

    def test_return_to_main_launcher_closes_the_menu(self):
        self.running(self.chrome)
        down = self.curses.KEY_DOWN
        self.guide([down, down, down, 10])
        self.assertEqual(self.wlrctl, [], "nothing was focused")

    # App rows

    def test_a_resumes_the_selected_app_and_hands_gamepad_nav_its_mouse_choice(self):
        self.running(self.chrome, self.moonlight)
        (self.run_dir / "launcher-focus").touch()
        _screen, launcher = self.guide([10])
        self.assertEqual(self.wlrctl[0], "app_id:google-chrome")
        self.assertEqual(launcher.status, "RESUMED GOOGLE CHROME")
        self.assertEqual(self.modes.front, "google-chrome")
        self.assertTrue(self.flag.exists(), "Chrome is driven as a mouse")
        self.assertFalse((self.run_dir / "launcher-focus").exists(), "the app takes the controller back")

    def test_resuming_a_game_turns_the_mouse_off(self):
        self.running(self.chrome, self.moonlight)
        self.flag.touch()
        _screen, launcher = self.guide([self.curses.KEY_DOWN, 10])
        self.assertEqual(launcher.status, "RESUMED MOONLIGHT")
        self.assertFalse(self.flag.exists())

    def test_an_app_that_cannot_be_focused_says_how_to_close_it(self):
        self.running(self.chrome)
        self.focus_result = 1
        screen, _launcher = self.guide([10, 27])
        text = screen.last()
        self.assertIn("COULD NOT FOCUS GOOGLE CHROME", text)
        # gamepad-nav sends F12 (type into the app) for X: the message must name the close button.
        self.assertIn(f"PRESS {self.module.CLOSE_BUTTON} TO CLOSE IT", text)
        self.assertNotIn("PRESS X TO CLOSE", text)
        self.assertIsNone(self.modes.front, "nothing came to the front")
        self.assertFalse(self.flag.exists())

    def test_delete_or_x_closes_the_selected_app(self):
        for key in (self.curses.KEY_DC, ord("x")):
            self.running(self.chrome, self.moonlight)
            screen, _launcher = self.guide([self.curses.KEY_DOWN, key, 27])
            self.assertTrue((self.run_dir / "close-moonlight").exists(), key)
            self.assertFalse((self.run_dir / "close-google-chrome").exists(), key)
            self.assertIn("CLOSING MOONLIGHT", screen.last())
            (self.run_dir / "close-moonlight").unlink()

    def test_x_triangle_focuses_the_selected_app_then_opens_the_keyboard_over_it(self):
        self.running(self.chrome, self.moonlight)
        order = []
        with mock.patch.object(self.module, "request_osk", side_effect=lambda *a: order.append("osk")), \
                mock.patch.object(self.module.Launcher, "focus_app",
                                  side_effect=lambda app: order.append(app.id) or True):
            _screen, launcher = self.guide([self.curses.KEY_DOWN, self.curses.KEY_F12])
        self.assertEqual(order, ["moonlight", "osk"])
        self.assertEqual(launcher.status, "TYPING INTO MOONLIGHT")

    def test_the_keyboard_request_reaches_the_keyboard_service(self):
        self.running(self.chrome)
        self.guide([self.curses.KEY_F12])
        self.assertTrue((self.run_dir / "start-osk").exists())
        self.assertEqual(self.wlrctl[0], "app_id:google-chrome")
        self.assertEqual(self.modes.front, "google-chrome")

    def test_x_triangle_on_the_volume_or_mouse_row_types_into_the_front_app(self):
        self.running(self.chrome, self.moonlight)
        self.modes.front = "moonlight"
        down = self.curses.KEY_DOWN
        for keys in ([down, down, self.curses.KEY_F12], [down, down, down, self.curses.KEY_F12]):
            targets = []
            with mock.patch.object(self.module, "request_osk"), mock.patch.object(
                self.module.Launcher, "focus_app", side_effect=lambda app: targets.append(app.id) or True
            ):
                self.guide(keys)
            self.assertEqual(targets, ["moonlight"], keys)

    def test_typing_into_an_app_that_cannot_be_focused_stays_in_the_menu_without_a_keyboard(self):
        self.running(self.chrome)
        self.focus_result = 1
        screen, launcher = self.guide([self.curses.KEY_F12, 27])
        self.assertFalse((self.run_dir / "start-osk").exists(), "the keyboard would type into the launcher")
        text = screen.last()
        self.assertIn("COULD NOT FOCUS GOOGLE CHROME", text)
        self.assertIn(self.module.CLOSE_BUTTON, text)
        self.assertNotEqual(launcher.status, "TYPING INTO GOOGLE CHROME")

    def test_x_triangle_with_nothing_running_does_not_type_into_the_menu(self):
        screen, _launcher = self.guide([self.curses.KEY_F12, 27])
        self.assertFalse((self.run_dir / "start-osk").exists(), "keys typed here would drive the menu")
        self.assertEqual(self.wlrctl, [])
        self.assertIn("NO APP IS RUNNING TO TYPE INTO", "\n".join(map(str, screen.frames)))

    def test_the_menu_takes_the_pad_back_from_a_running_app(self):
        self.guide([27])
        self.assertTrue((self.run_dir / "launcher-focus").exists())

    def test_a_second_keyboard_is_not_requested_while_one_is_open(self):
        (self.run_dir / "osk-active").touch()
        self.guide([self.curses.KEY_F12, 27])
        self.assertFalse((self.run_dir / "start-osk").exists())

    # VOLUME row

    def test_left_and_right_change_the_volume_and_a_mutes(self):
        audio = self.module.audio
        self.running(self.chrome)
        down, left, right = self.curses.KEY_DOWN, self.curses.KEY_LEFT, self.curses.KEY_RIGHT

        def louder(step):
            self.volume = audio.Volume(self.volume.percent + step, False)

        audio.change_volume.side_effect = louder
        audio.toggle_mute.side_effect = lambda: setattr(self, "volume", audio.Volume(self.volume.percent, True))
        screen, _launcher = self.guide([down, right, right, left, 10, 27])
        self.assertEqual([call.args for call in audio.change_volume.call_args_list],
                         [(audio.VOLUME_STEP,), (audio.VOLUME_STEP,), (-audio.VOLUME_STEP,)])
        audio.toggle_mute.assert_called_once_with()
        self.assertIn(f"VOLUME  {40 + audio.VOLUME_STEP}%  MUTED", self.rows(screen))
        self.assertEqual(self.wlrctl, [], "the volume row never focuses an app")

    def test_the_volume_row_hint_names_the_buttons(self):
        screen, _launcher = self.guide([27])
        self.assertIn("LEFT / RIGHT CHANGES THE VOLUME", screen.last())

    def test_a_failed_volume_change_is_shown(self):
        audio = self.module.audio
        for error in (RuntimeError("no default sink"), OSError("wpctl missing"),
                      subprocess.TimeoutExpired("wpctl", 2)):
            audio.change_volume.side_effect = error
            audio.toggle_mute.side_effect = error
            screen, _launcher = self.guide([self.curses.KEY_RIGHT, 27])
            self.assertIn("VOLUME NOT CHANGED: ", screen.last())
            self.assertIn(str(error).upper(), screen.last())
            screen, _launcher = self.guide([10, 27])
            self.assertIn("VOLUME NOT CHANGED", screen.last(), "mute failed too")

    def test_an_unreadable_volume_says_unavailable(self):
        for error in (RuntimeError("no sink"), OSError("wpctl missing"), subprocess.TimeoutExpired("wpctl", 2)):
            self.module.audio.get_volume.side_effect = error
            screen, _launcher = self.guide([27])
            self.assertEqual(self.rows(screen)[0], "VOLUME  UNAVAILABLE")

    # BRIGHTNESS row

    def test_the_brightness_row_follows_volume_only_when_the_screen_has_a_backlight(self):
        self.running(self.chrome)
        screen, _launcher = self.guide([27])
        rows = self.rows(screen)
        self.assertEqual(len(rows), 4, rows)
        self.assertFalse([row for row in rows if row.startswith("BRIGHTNESS")], "nothing to adjust on this PC")
        self.brightness = 60
        screen, _launcher = self.guide([27])
        rows = self.rows(screen)
        self.assertEqual(rows[1:3], ["VOLUME  40%", "BRIGHTNESS  60%"])
        self.assertRegex(rows[3], r"^CONTROLLER MOUSE \(GOOGLE CHROME\) +ON$")

    def test_left_and_right_change_the_brightness(self):
        brightness = self.module.brightness
        self.brightness = 60
        down, left, right = self.curses.KEY_DOWN, self.curses.KEY_LEFT, self.curses.KEY_RIGHT

        def step(change):
            self.brightness += change

        brightness.change.side_effect = step
        screen, _launcher = self.guide([down, right, right, left, 10, 27])
        self.assertEqual([call.args for call in brightness.change.call_args_list],
                         [(brightness.STEP,), (brightness.STEP,), (-brightness.STEP,)])
        self.assertIn(f"BRIGHTNESS  {60 + brightness.STEP}%", self.rows(screen))
        self.module.audio.toggle_mute.assert_not_called()
        self.assertEqual(self.wlrctl, [], "A on the brightness row does nothing")

    def test_the_brightness_row_hint_names_the_buttons(self):
        self.brightness = 60
        screen, _launcher = self.guide([self.curses.KEY_DOWN, 27])
        self.assertIn("LEFT / RIGHT CHANGES THE SCREEN BRIGHTNESS", screen.last())

    def test_a_failed_brightness_change_is_shown(self):
        self.brightness = 60
        self.module.brightness.change.side_effect = PermissionError(13, "Permission denied")
        screen, _launcher = self.guide([self.curses.KEY_DOWN, self.curses.KEY_RIGHT, 27])
        self.assertIn("BRIGHTNESS NOT CHANGED: PERMISSION DENIED", screen.last())

    def test_the_brightness_row_reads_the_real_backlight(self):
        with mock.patch.object(self.module.brightness, "get_percent", return_value=None):
            self.assertIsNone(self.module.Launcher.brightness_row())
        with mock.patch.object(self.module.brightness, "get_percent", return_value=5):
            self.assertEqual(self.module.Launcher.brightness_row(), "BRIGHTNESS  5%")

    # CONTROLLER MOUSE row

    def test_a_turns_the_controller_mouse_off_and_on_for_the_front_app(self):
        self.running(self.chrome)
        self.modes.apply(self.chrome)
        self.assertTrue(self.flag.exists())
        down = self.curses.KEY_DOWN
        screen, _launcher = self.guide([down, down, 10, 27])
        self.assertFalse(self.modes.enabled(self.chrome))
        self.assertFalse(self.flag.exists(), "gamepad-nav lets go of the pad at once")
        self.assertRegex(self.rows(screen)[2], r"^CONTROLLER MOUSE \(GOOGLE CHROME\) +OFF$")
        self.assertIn("CONTROLLER MOUSE OFF FOR GOOGLE CHROME", screen.last())
        screen, _launcher = self.guide([down, down, self.curses.KEY_RIGHT, 27])
        self.assertTrue(self.modes.enabled(self.chrome))
        self.assertTrue(self.flag.exists())
        self.assertIn("CONTROLLER MOUSE ON FOR GOOGLE CHROME", screen.last())

    def test_the_mouse_can_be_turned_on_for_an_app_with_controller_support(self):
        self.running(self.moonlight)
        self.modes.apply(self.moonlight)
        down = self.curses.KEY_DOWN
        _screen, _launcher = self.guide([down, down, self.curses.KEY_LEFT, 27])
        self.assertTrue(self.modes.enabled(self.moonlight))
        self.assertTrue(self.flag.exists())

    def test_the_mouse_row_ignores_close_and_explains_itself(self):
        self.running(self.chrome)
        down = self.curses.KEY_DOWN
        screen, _launcher = self.guide([down, down, self.curses.KEY_DC, ord("x"), 27])
        self.assertTrue(self.modes.enabled(self.chrome))
        self.assertEqual(list(self.run_dir.glob("close-*")), [])
        self.assertIn("LEFT STICK POINTS", screen.last())

    def test_a_choice_for_an_app_behind_is_used_when_it_comes_forward(self):
        self.running(self.chrome, self.firefox)
        self.modes.apply(self.chrome)
        self.modes.toggle(self.firefox)  # chosen earlier, while Firefox was behind
        self.assertTrue(self.flag.exists())
        self.guide([self.curses.KEY_DOWN, 10])  # resume Firefox
        self.assertEqual(self.modes.front, "firefox")
        self.assertFalse(self.flag.exists())

    # Guide closes the menu

    def test_guide_while_the_menu_is_open_closes_it(self):
        self.running(self.chrome)
        request = self.run_dir / "home.request"

        class GuideScreen(Screen):
            def getch(self):
                request.touch()
                return -1

        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(Screen())
        launcher.screen = GuideScreen()
        launcher.active_applications()
        self.assertFalse(request.exists())
        self.assertEqual(self.wlrctl, [])


class ReadKeyTest(GuideFixture):
    """read_key() and the places that bring an app forward."""

    def test_f12_opens_the_launcher_keyboard_unless_the_caller_wants_it(self):
        screen = Screen([self.curses.KEY_F12, self.curses.KEY_F12, 10])
        with mock.patch.object(self.module, "IDLE_GUARD", None):
            self.assertEqual(self.module.read_key(screen, keyboard=False), self.curses.KEY_F12)
            self.assertFalse((self.run_dir / "start-osk").exists())
            self.assertEqual(self.module.read_key(screen), -1)
            self.assertTrue((self.run_dir / "start-osk").exists())
            self.assertEqual(self.module.read_key(screen, keyboard=False), 10)

    def test_focus_app_hands_gamepad_nav_the_choice_only_when_it_worked(self):
        self.focus_result = 1
        self.assertFalse(self.module.Launcher.focus_app(self.chrome))
        self.assertIsNone(self.modes.front)
        self.assertFalse(self.flag.exists())
        self.focus_result = 0
        self.assertTrue(self.module.Launcher.focus_app(self.chrome))
        self.assertEqual(self.modes.front, "google-chrome")
        self.assertTrue(self.flag.exists())

    def test_launching_an_app_sets_its_mouse_before_it_starts(self):
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(Screen())
        launcher.screen = Screen([-1] * 5)
        seen = []

        def start(name):
            seen.append((name, self.flag.exists(), self.modes.front))
            (self.run_dir / "google-chrome-ready").touch()

        launcher.request = start
        self.assertTrue(launcher.launch_app(self.chrome))
        self.assertEqual(seen, [("google-chrome.request", True, "google-chrome")])
        self.flag.unlink()
        (self.run_dir / "google-chrome-ready").unlink()
        launcher.request = lambda _name: (self.run_dir / "moonlight-ready").touch()
        with mock.patch.object(launcher, "wake_before_moonlight", return_value=True):
            self.assertTrue(launcher.launch_app(self.moonlight))
        self.assertEqual(self.modes.front, "moonlight")
        self.assertFalse(self.flag.exists())

    def test_resuming_through_launch_app_applies_the_choice_too(self):
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(Screen())
        self.running(self.firefox)
        self.assertTrue(launcher.launch_app(self.firefox))
        self.assertEqual(launcher.status, "RESUMED FIREFOX")
        self.assertEqual(self.modes.front, "firefox")
        self.assertTrue(self.flag.exists())


if __name__ == "__main__":
    unittest.main()
