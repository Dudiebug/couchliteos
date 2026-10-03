"""The TV interface's quick menu, prompt bar, toasts and sounds (couchliteos_quick), and how the
TV window uses them (the GTK drawing left out)."""

import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import pathlib
import tempfile
import types
import unittest
import wave
from unittest import mock

import couchliteos_controllers as controllers
import couchliteos_quick as quick
import couchliteos_tvlayout as tvlayout

TV_PATH = pathlib.Path(__file__).with_name("couchliteos-tv.py")


class TempDir(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)


class OpenRuleTest(TempDir):
    def test_the_request_says_tap_or_shortcut_and_is_taken_once(self):
        request = self.root / "home.request"
        self.assertIsNone(quick.take_request(request))
        request.write_text("guide\n")
        self.assertEqual(quick.take_request(request), quick.GUIDE)
        self.assertFalse(request.exists())
        self.assertIsNone(quick.take_request(request))
        for text in ("shortcut\n", "", "something else"):  # an older gamepad-nav only touches it
            request.write_text(text)
            self.assertEqual(quick.take_request(request), quick.SHORTCUT, text)

    def test_a_guide_tap_opens_the_quick_menu_and_a_held_shortcut_goes_straight_home(self):
        cases = [
            # kind, quick menu open, mode, apps running -> what shows
            (quick.GUIDE, False, "home", False, "quick"),
            (quick.GUIDE, False, "home", True, "quick"),
            (quick.GUIDE, False, "active", True, "quick"),
            (quick.GUIDE, True, "home", True, "close"),  # a second tap closes it
            (quick.SHORTCUT, False, "home", False, "home"),
            (quick.SHORTCUT, False, "home", True, "active"),  # 0.2.4: the running apps
            (quick.SHORTCUT, False, "active", True, "home"),
            (quick.SHORTCUT, True, "home", True, "active"),
            (quick.SHORTCUT, True, "home", False, "home"),
        ]
        for kind, is_open, mode, running, expected in cases:
            self.assertEqual(quick.home_target(kind, is_open, mode, running), expected, (kind, is_open, mode, running))

    def test_the_app_in_front_is_remembered_only_when_the_launcher_did_not_have_the_pad(self):
        self.assertEqual(quick.front_app_id(self.root), "")
        (self.root / "app-active").write_text("google-chrome\n")
        self.assertEqual(quick.front_app_id(self.root), "google-chrome")
        (self.root / "launcher-focus").touch()
        self.assertEqual(quick.front_app_id(self.root), "")


class PromptTest(unittest.TestCase):
    def test_glyph_selection_table(self):
        self.assertEqual(quick.glyphs("xbox"), {"activate": "Ⓐ", "back": "Ⓑ", "keyboard": "Ⓧ", "close": "Ⓨ"})
        self.assertEqual(quick.glyphs("playstation"), {"activate": "✕", "back": "○", "keyboard": "△", "close": "□"})
        # Nintendo prints B on the south button, which gamepad-nav sends as Enter.
        self.assertEqual(quick.glyphs("nintendo")["activate"], "Ⓑ")
        self.assertEqual(quick.glyphs("nintendo")["back"], "Ⓐ")
        self.assertEqual(quick.glyphs("generic")["activate"], "Ⓐ / ✕")
        self.assertEqual(quick.glyphs("not a family"), quick.glyphs("generic"))

    def test_the_family_comes_from_the_pads_names(self):
        import couchliteos_controls as controls

        for names, family in ((["Xbox Wireless Controller"], "xbox"), (["Sony DualSense Wireless Controller"], "playstation"),
                              (["Nintendo Switch Pro Controller"], "nintendo"), ([], "generic")):
            self.assertIn(controls.family(names), quick.GLYPHS, names)
            self.assertEqual(controls.family(names), family)

    def test_prompts_name_the_glyph_and_the_keyboard_key(self):
        self.assertEqual(quick.prompt("xbox", quick.HOME_PROMPT), "Ⓐ OR ENTER  OPEN    Ⓑ OR ESC  BACK")
        self.assertEqual(quick.prompt("playstation", quick.ACTIVE_PROMPT),
                         "✕ OR ENTER  RESUME    □ OR DELETE  CLOSE    △ OR F12  TYPE INTO IT    ○ OR ESC  BACK")
        self.assertIn("LEFT / RIGHT  CHANGE", quick.prompt("xbox", quick.QUICK_CHANGE_PROMPT))

    def test_every_hint_shown_has_a_keyboard_equivalent(self):
        prompts = [quick.HOME_PROMPT, quick.ACTIVE_PROMPT, quick.QUICK_PROMPT, quick.QUICK_CHANGE_PROMPT,
                   quick.QUICK_BRIGHTNESS_PROMPT, quick.QUICK_PROMPT[:1] + quick.ACTIVE_PROMPT[-1:],
                   quick.MOUSE_PROMPT]
        for family in quick.GLYPHS:
            for entries in prompts:
                for separator in ("    ", "\n"):
                    text = quick.prompt(family, entries, separator)
                    self.assertTrue(quick.names_keyboard_keys(text), text)
        for text in (tvlayout.HOME_HINT, tvlayout.ACTIVE_HINT, tvlayout.FAILURE_HINT, tvlayout.QUESTION_HINT,
                     tvlayout.WAIT_HINT, tvlayout.STREAM_SETTINGS_HINT):
            self.assertTrue(quick.names_keyboard_keys(text), text)
        tv = load_tv()
        self.assertTrue(quick.names_keyboard_keys(tv.BACK_HINT))
        self.assertTrue(quick.names_keyboard_keys(tv.CHOICE_HINT))
        self.assertTrue(quick.names_keyboard_keys(
            "HOLD SELECT+START (VIEW+MENU) OR PRESS THE HOME KEY TO COME BACK HERE"))

    def test_the_check_catches_a_button_without_its_key(self):
        for text in ("A / CROSS RESUMES  ·  Y (XBOX) / SQUARE (PS) CLOSES  ·  B / CIRCLE BACK",
                     "X (XBOX) / TRIANGLE (PS) OPENS THE KEYBOARD AND TYPES INTO THE APP",
                     "HOLD SELECT+START (VIEW+MENU) TO COME BACK TO THE LAUNCHER",
                     "Ⓐ  OPEN    Ⓑ OR ESC  BACK"):
            self.assertFalse(quick.names_keyboard_keys(text), text)
        self.assertTrue(quick.names_keyboard_keys("PICK A PC  ·  NO PC IS PAIRED"))  # "A" the word is not a button


class Sources:
    """Fake volume, backlight and the rest, recording changes."""

    def __init__(self, *, volume=40, brightness=None, running=0, can_sleep=True, battery="CONTROLLERS: XBOX 80%"):
        self.state = {"volume": volume, "muted": False, "brightness": brightness}
        self.changes = []
        self.sources = quick.Sources(
            volume=self.volume, change_volume=lambda step: self.change("volume", step),
            toggle_mute=self.toggle_mute, brightness=lambda: self.state["brightness"],
            change_brightness=lambda step: self.change("brightness", step),
            battery=lambda: battery, network=lambda: "ONLINE", running=lambda: running, can_sleep=lambda: can_sleep,
        )

    def volume(self):
        if self.state["volume"] is None:
            raise OSError("no wpctl")
        return types.SimpleNamespace(percent=self.state["volume"], muted=self.state["muted"])

    def change(self, name, step):
        self.changes.append((name, step))
        self.state[name] += step
        return self.state[name]

    def toggle_mute(self):
        self.state["muted"] = not self.state["muted"]
        return True


class QuickMenuTest(unittest.TestCase):
    def menu(self, **options):
        fake = Sources(**options)
        menu = quick.QuickMenu(fake.sources)
        menu.open()
        return fake, menu

    def keys(self, menu):
        return [item.key for item in menu.items]

    def test_rows(self):
        _fake, menu = self.menu(brightness=60, running=2)
        self.assertEqual(self.keys(menu), ["apps", "volume", "brightness", "battery", "network", "home", "sleep", "restart", "off"])
        self.assertEqual(menu.items[0].value, "2 RUNNING")
        self.assertEqual(menu.focused().key, "apps")
        self.assertEqual(menu.items[3].value, "XBOX 80%")
        _fake, menu = self.menu(can_sleep=False, battery="")
        self.assertEqual(self.keys(menu), ["volume", "network", "home", "restart", "off"])
        self.assertEqual(menu.focused().key, "volume")

    def test_focus_skips_rows_that_only_show_something_and_never_wraps(self):
        _fake, menu = self.menu()
        self.assertEqual(menu.focused().key, "volume")
        self.assertFalse(menu.move(-1))
        self.assertTrue(menu.move(1))
        self.assertEqual(menu.focused().key, "home")  # battery and network skipped
        while menu.move(1):
            pass
        self.assertEqual(menu.focused().key, "off")

    def test_left_right_change_volume_and_brightness_and_a_mutes(self):
        fake, menu = self.menu(brightness=50)
        self.assertTrue(menu.adjust(1))
        self.assertEqual(menu.focused().value, "45%")
        menu.move(1)
        self.assertTrue(menu.adjust(-1))
        self.assertEqual(menu.focused().value, "45%")
        self.assertEqual(fake.changes, [("volume", quick.STEP), ("brightness", -quick.STEP)])
        menu.move(-1)
        self.assertEqual(menu.activate(), ())
        self.assertEqual(menu.focused().value, "45%  MUTED")
        menu.move(1)
        menu.move(1)
        self.assertFalse(menu.adjust(1), "HOME is not a slider")

    def test_actions(self):
        _fake, menu = self.menu(running=1)
        found = {}
        while True:
            found[menu.focused().key] = menu.activate() if menu.focused().key != "volume" else None
            if not menu.move(1):
                break
        self.assertEqual(found["apps"], ("active",))
        self.assertEqual(found["home"], ("home",))
        self.assertEqual(found["sleep"], ("power", "suspend"))
        self.assertEqual(found["restart"], ("power", "reboot"))
        self.assertEqual(found["off"], ("power", "poweroff"))
        self.assertEqual(set(quick.POWER_QUESTIONS), {"reboot", "poweroff"})

    def test_sleep_asks_first_and_says_when_nothing_can_wake_this_pc(self):
        self.assertEqual(quick.power_question("suspend", True), ("SLEEP", "SLEEP NOW?"))
        title, question = quick.power_question("suspend", False)
        self.assertEqual(title, "SLEEP")
        self.assertIn("NOTHING CONNECTED CAN WAKE THIS PC: USE ITS POWER BUTTON", question)
        for request in ("reboot", "poweroff"):
            self.assertEqual(quick.power_question(request, True), quick.POWER_QUESTIONS[request])

    def test_a_broken_source_shows_unavailable(self):
        fake, menu = self.menu(volume=None)
        volume = menu.items[self.keys(menu).index("volume")]
        self.assertEqual((volume.value, volume.selectable), ("UNAVAILABLE", False))
        fake.sources.network = mock.Mock(side_effect=RuntimeError)
        menu.refresh()
        self.assertEqual(menu.items[self.keys(menu).index("network")].value, "UNKNOWN")

    def test_extra_rows_come_first_and_their_action_is_returned(self):
        # R1 puts the stream preset and SHOW STATS here while a stream runs.
        fake = Sources()
        menu = quick.QuickMenu(fake.sources, extra=lambda: [quick.Item("stats", "SHOW STATS", "OFF", ("stats",))])
        menu.open()
        self.assertEqual(menu.focused().key, "stats")
        self.assertEqual(menu.activate(), ("stats",))

    def test_refresh_keeps_the_focused_row(self):
        fake, menu = self.menu()
        menu.move(1)
        fake.sources.running = lambda: 1  # an app started: a row appears above
        menu.refresh()
        self.assertEqual(menu.focused().key, "home")

    def test_the_prompt_follows_the_row(self):
        _fake, menu = self.menu(brightness=10)
        self.assertIn("MUTE", menu.prompt("xbox"))
        menu.move(1)
        self.assertNotIn("MUTE", menu.prompt("xbox"))
        self.assertIn("CHANGE", menu.prompt("xbox"))
        menu.move(1)
        self.assertEqual(menu.prompt("xbox"), "Ⓐ OR ENTER  SELECT\nⒷ OR ESC  CLOSE")


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ToastsTest(unittest.TestCase):
    def test_one_at_a_time_for_four_seconds_then_gone(self):
        clock = Clock()
        toasts = quick.Toasts(clock)
        self.assertEqual(toasts.current(), "")
        toasts.push("controller connected: xbox")
        toasts.push("Update")
        self.assertEqual(toasts.current(), "CONTROLLER CONNECTED: XBOX")
        clock.now += quick.TOAST_SECONDS - 0.1
        self.assertEqual(toasts.current(), "CONTROLLER CONNECTED: XBOX")
        clock.now += 0.2
        self.assertEqual(toasts.current(), "UPDATE")
        clock.now += quick.TOAST_SECONDS
        self.assertEqual(toasts.current(), "")
        self.assertEqual(quick.TOAST_SECONDS, 4.0)

    def test_repeats_are_dropped_and_the_queue_is_short(self):
        clock = Clock()
        toasts = quick.Toasts(clock, limit=2)
        for text in ("ONE", "ONE", "TWO", "THREE"):
            toasts.push(text)
        self.assertEqual(list(toasts.waiting), ["TWO", "THREE"])
        self.assertEqual(toasts.current(), "TWO")
        toasts.push("TWO")
        self.assertEqual(list(toasts.waiting), ["THREE"])

    def test_a_toast_never_takes_focus(self):
        self.assertFalse(quick.Toasts.focusable)
        toasts = quick.Toasts(Clock())
        self.assertFalse([name for name in dir(toasts) if "key" in name or "focus" in name.replace("focusable", "")])
        # The TV window shows it, but its keys still go to the screen or menu underneath.
        tv = bare_tv()
        tv.toasts = toasts
        toasts.push("CONTROLLER CONNECTED: XBOX")
        tv.quick_key_or_open("home")
        tv.open_quick.assert_called_once_with()
        self.assertEqual(toasts.current(), "CONTROLLER CONNECTED: XBOX")


class ToastFeedTest(unittest.TestCase):
    def test_changes_become_toasts_and_the_first_look_is_silent(self):
        state = {"pads": ["Xbox Wireless Controller"], "low": [], "pc": "", "update": ""}
        toasts = quick.Toasts(Clock())
        feed = quick.ToastFeed(toasts, pads=lambda: state["pads"], low=lambda: state["low"],
                               pc_line=lambda: state["pc"], update=lambda: state["update"])
        feed.poll()
        self.assertEqual(list(toasts.waiting), [])
        state["pads"] = ["Xbox Wireless Controller", "Sony DualSense"]
        state["low"] = [controllers.Battery("PAD 2", 10)]
        state["pc"] = "GAMING PC: TOWER READY"
        state["update"] = "0.3.1"
        feed.poll()
        self.assertEqual(list(toasts.waiting), [
            "CONTROLLER CONNECTED: SONY DUALSENSE", "PAD 2 BATTERY LOW", "GAMING PC: TOWER READY", "COUCHLITEOS 0.3.1 IS AVAILABLE",
        ])
        feed.poll()  # nothing changed: nothing new
        self.assertEqual(len(toasts.waiting), 4)
        state["pads"] = ["Sony DualSense"]
        state["pc"] = "GAMING PC: TOWER ASLEEP - STARTING MOONLIGHT WAKES IT"
        feed.poll()
        self.assertEqual(toasts.waiting[-1], "CONTROLLER DISCONNECTED: XBOX WIRELESS CONTROLLER")

    def test_a_failing_source_makes_no_toast(self):
        toasts = quick.Toasts(Clock())
        feed = quick.ToastFeed(toasts, pads=mock.Mock(side_effect=OSError), low=list, pc_line=str, update=str)
        feed.poll()
        feed.poll()
        self.assertEqual(list(toasts.waiting), [])


class SoundsTest(TempDir):
    def sounds(self, *, muted=False, enabled=True):
        played = []
        sounds = quick.Sounds(muted=lambda: muted, enabled=lambda: enabled, directory=self.root, spawn=played.append)
        return sounds, played

    def test_keys_play_their_sound_with_pw_play(self):
        sounds, played = self.sounds()
        for name in ("up", "activate", "back", "close", None):
            sounds.for_key(name)
        self.assertEqual(played, [["pw-play", "--volume", quick.SOUND_VOLUME, str(self.root / f"{name}.wav")]
                                  for name in ("move", "select", "back")])

    def test_muted_during_a_stream_and_when_off(self):
        for options in ({"muted": True}, {"enabled": False}):
            sounds, played = self.sounds(**options)
            self.assertFalse(sounds.play("select"))
            self.assertEqual(played, [], options)

    def test_no_pw_play_is_silence_not_a_crash(self):
        sounds = quick.Sounds(muted=lambda: False, enabled=lambda: True, spawn=mock.Mock(side_effect=FileNotFoundError))
        self.assertFalse(sounds.play("move"))

    def test_the_setting_is_kept_with_the_theme(self):
        import couchliteos_theme as theme

        config = self.root / "config.ini"
        self.assertTrue(quick.sounds_enabled(config))
        theme.save_choice("slate", "teal", config)
        quick.save_sounds(False, config)
        self.assertFalse(quick.sounds_enabled(config))
        theme.save_choice("daylight", "", config)  # changing the theme keeps SOUNDS OFF
        self.assertFalse(quick.sounds_enabled(config))
        self.assertEqual(theme.load_choice(config), ("daylight", ""))
        quick.save_sounds(True, config)
        self.assertTrue(quick.sounds_enabled(config))
        self.assertEqual(theme.load_choice(config), ("daylight", ""))
        self.assertEqual(config.read_text().count("[appearance]"), 1)

    def test_the_sound_files_are_short_quiet_wavs(self):
        directory = quick.REPO_SOUNDS
        for name in quick.SOUND_NAMES:
            path = directory / f"{name}.wav"
            self.assertLess(path.stat().st_size, 16 * 1024, name)
            with wave.open(str(path)) as sound:
                self.assertLess(sound.getnframes() / sound.getframerate(), 0.3, name)
                self.assertEqual(sound.getnchannels(), 1)


def load_tv():
    spec = importlib.util.spec_from_file_location("couchliteos_tv_for_quick_tests", TV_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bare_tv(running=()):
    """A Tv without GTK: the quick-menu logic with the drawing mocked out."""
    module = load_tv()
    tv = object.__new__(module.Tv)
    tv.quick_open = False
    tv.quick_front = ""
    tv.quick = mock.Mock()
    tv.key_home_at = float("-inf")
    tv.mode = "home"
    tv.status = ""
    tv.running_applications = mock.Mock(return_value=list(running))
    for name in ("open_quick", "open_active", "show", "render_home", "render_bar", "render_quick", "set_launcher_focus",
                 "request", "ask", "focus_app"):
        setattr(tv, name, mock.Mock())
    return tv


class TvQuickMenuTest(unittest.TestCase):
    def test_a_guide_tap_opens_it_and_a_held_shortcut_goes_home(self):
        tv = bare_tv(running=["app"])
        tv.home_request_arrived(quick.GUIDE)
        tv.open_quick.assert_called_once_with()
        tv.open_active.assert_not_called()
        tv = bare_tv(running=["app"])
        tv.home_request_arrived(quick.SHORTCUT)
        tv.open_active.assert_called_once_with()
        tv.open_quick.assert_not_called()
        tv = bare_tv()
        tv.home_request_arrived(quick.SHORTCUT)
        tv.show.assert_called_once_with("home")

    def test_the_home_key_is_one_press_not_two(self):
        # The window gets the key and gamepad-nav writes home.request for it too.
        tv = bare_tv()
        tv.quick_key_or_open("home")
        tv.open_quick.assert_called_once_with()
        tv.home_request_arrived(quick.GUIDE)
        tv.open_quick.assert_called_once_with()

    def test_power_rows_ask_first(self):
        tv = bare_tv()
        tv.close_quick = mock.Mock()
        tv.can_wake = False
        tv.ask.return_value = "no"
        tv.quick_action(("power", "poweroff"))
        tv.request.assert_not_called()
        tv.quick_action(("power", "suspend"))
        tv.request.assert_not_called()
        self.assertIn("NOTHING CONNECTED CAN WAKE THIS PC", tv.ask.call_args.args[1])
        tv.ask.return_value = "yes"
        tv.quick_action(("power", "reboot"))
        tv.request.assert_called_once_with("reboot")
        tv.quick_action(("power", "suspend"))
        tv.request.assert_called_with("suspend")
        self.assertEqual(tv.ask.call_count, 4)

    def test_back_returns_to_the_app_guide_was_tapped_over(self):
        app = types.SimpleNamespace(id="google-chrome", name="GOOGLE CHROME")
        tv = bare_tv(running=[app])
        tv.quick_panel = mock.Mock()
        tv.place_toast = mock.Mock()
        tv.quick_open = True
        tv.quick_front = "google-chrome"
        tv.quick_key_or_open("back")
        tv.focus_app.assert_called_once_with(app)
        self.assertFalse(tv.quick_open)


if __name__ == "__main__":
    unittest.main()
