"""The TV interface's Settings, power, update progress and What's New models, and the
`couchliteos-launcher --screen` dispatch they rely on (no GTK needed)."""

import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import json
import pathlib
import tempfile
import types
import unittest
from unittest import mock

import couchliteos_softwareupdate as softwareupdate
import couchliteos_tvlayout as tvlayout
import couchliteos_tvscreens as tvscreens
import couchliteos_whatsnew as whatsnew

_LAUNCHER = []


def load_launcher():
    if not _LAUNCHER:
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_for_tvscreens", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LAUNCHER.append(module)
    return _LAUNCHER[0]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class DispatchTest(unittest.TestCase):
    """Every classic Settings entry is reachable from the TV interface: drawn there, or a --screen."""

    @classmethod
    def setUpClass(cls):
        cls.launcher = load_launcher()

    def test_every_settings_entry_maps_to_the_tv_interface_or_a_classic_screen_in_order(self):
        self.assertEqual([entry.label for entry in tvscreens.ENTRIES], list(self.launcher.SETTINGS_MENU))
        for entry in tvscreens.ENTRIES:
            self.assertIn(entry.kind, (tvscreens.SCREEN, tvscreens.APP, tvscreens.VIEW), entry)
            self.assertTrue(entry.help and entry.help == entry.help.upper(), entry)
            if entry.kind == tvscreens.SCREEN:
                self.assertIn(entry.target, self.launcher.SCREENS, entry)
            elif entry.kind == tvscreens.VIEW:
                self.assertIn(entry.target, ("active", "updates", "help", "home"), entry)

    def test_app_entries_name_shipped_applications(self):
        manifests = pathlib.Path(__file__).resolve().parents[1] / "config/apps.d"
        ids = {line.split("=", 1)[1].strip() for path in manifests.glob("*.ini")
               for line in path.read_text().splitlines() if line.replace(" ", "").startswith("id=")}
        for entry in tvscreens.ENTRIES:
            if entry.kind == tvscreens.APP:
                self.assertIn(entry.target, ids)

    def test_both_sides_list_the_same_screens(self):
        self.assertEqual(set(self.launcher.SCREENS), set(tvscreens.SCREENS))
        for target in tvscreens.TILE_SCREENS.values():
            self.assertIn(target, tvscreens.SCREENS)
        self.assertEqual(self.launcher.UPDATE_WATCH, tvscreens.UPDATE_WATCH)

    def test_each_screen_runs_its_classic_settings_screen(self):
        expected = {
            "display": "run_display", "appearance": "run_appearance", "audio": "run_audio",
            "network": "run_network", "sleep": "run_sleep_settings", "applications": "run_applications",
            "remote-desktop": "run_remote_desktop", "streaming": "run_streaming", "tv-control": "run_tv_control",
            "controls": "run_controls", "support-file": "generate_support_file",
        }
        for name, method in expected.items():
            settings = mock.Mock()
            self.launcher.SCREENS[name](settings, "")
            getattr(settings, method).assert_called_once_with()
        settings = mock.Mock()
        self.launcher.SCREENS["software-update"](settings, "")
        settings.run_software_update.assert_called_once_with(hand_off=True)
        settings = mock.Mock()
        self.launcher.SCREENS["setup-wizard"](settings, "")
        settings.launcher.setup_wizard.assert_called_once_with(force=True)
        settings = mock.Mock()
        self.launcher.SCREENS["connect"](settings, "office-pc")
        settings.launcher.launch_by_id.assert_called_once_with("office-pc")
        for name, attribute in (("bluetooth", "run_bluetooth"), ("controllers", "run")):
            module = self.launcher.bluetooth if name == "bluetooth" else self.launcher.pads
            with mock.patch.object(module, attribute) as run:
                self.launcher.SCREENS[name](mock.Mock(), "")
            run.assert_called_once()

    def test_setup_runs_the_wizard_and_resumes_after_a_restart(self):
        launcher = mock.Mock()
        marker = self.launcher.RUN / "reopen-setup"
        # The CONTROLS screen is not shown here any more: the TV's tour has the buttons.
        with mock.patch.object(self.launcher.controls, "show_once") as controls:
            self.launcher.SCREENS["setup"](types.SimpleNamespace(launcher=launcher), "")
            launcher.setup_wizard.assert_called_once_with()
            controls.assert_not_called()
            marker.touch()
            launcher.reset_mock()
            self.launcher.SCREENS["setup"](types.SimpleNamespace(launcher=launcher), "")
            launcher.setup_wizard.assert_called_once_with(resume=True)
        self.assertFalse(marker.exists())

    def test_the_command_line_takes_only_known_screens(self):
        arguments = self.launcher.parse_arguments(["--screen", "connect", "--app", "office-pc"])
        self.assertEqual((arguments.screen, arguments.app), ("connect", "office-pc"))
        self.assertIsNone(self.launcher.parse_arguments([]).screen)
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self.launcher.parse_arguments(["--screen", "nothing"])

    def test_software_update_for_the_tv_leaves_the_version_to_watch(self):
        settings = object.__new__(self.launcher.Settings)
        settings.screen = mock.Mock()
        settings.launcher = mock.Mock()
        settings.status = ""

        def show(_screen, **injected):
            injected["hand_off"]("0.3.0")

        with mock.patch.object(self.launcher.softwareupdate, "show", side_effect=show):
            settings.run_software_update(hand_off=True)
        self.assertEqual(tvscreens.take_update_watch(self.launcher.RUN), "0.3.0")
        self.assertIsNone(tvscreens.take_update_watch(self.launcher.RUN))
        with mock.patch.object(self.launcher.softwareupdate, "show") as classic:
            settings.run_software_update()
        self.assertIsNone(classic.call_args.kwargs["hand_off"])

    def test_the_child_window_is_the_themed_foot_wrapper_with_its_own_title(self):
        command = tvscreens.child_command("connect", "office-pc")
        self.assertEqual(command[:4], [tvscreens.FOOT, "--fullscreen", "--title", tvscreens.CHILD_TITLE])
        self.assertEqual(command[-4:], ["--screen", "connect", "--app", "office-pc"])
        self.assertNotIn("--app", tvscreens.child_command("display"))
        self.assertNotEqual(tvscreens.CHILD_TITLE, "CouchLiteOS Launcher")  # Guide must find the TV window


class HandOffTest(unittest.TestCase):
    def test_install_hands_off_and_the_screen_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            handed = []
            screen = softwareupdate.SoftwareUpdate(
                mock.Mock(), read_key=lambda _screen: 10, apps_running=lambda: False,
                confirm=lambda *_args: True, current="0.2.7", profile={}, live=False, run_dir=run,
                hand_off=handed.append,
            )
            screen.release, screen.asset = mock.Mock(version="0.3.0"), mock.Mock(size=2e9)
            screen.rows = lambda: [softwareupdate.INSTALL]
            screen.draw_main = mock.Mock()
            with mock.patch.object(screen, "progress", side_effect=AssertionError("watched here")):
                screen.run()
            self.assertEqual(handed, ["0.3.0"])
            self.assertTrue((run / softwareupdate.REQUEST).exists())


class SettingsModelTest(unittest.TestCase):
    def test_two_panes_the_focused_entry_its_value_and_help(self):
        model = tvscreens.SettingsModel({"DISPLAY": lambda: "1920x1080 @ 60 HZ", "AUDIO": lambda: 1 / 0})
        model.refresh()
        self.assertEqual(model.rows()[0], "DISPLAY")
        self.assertEqual(model.detail(), ("DISPLAY", "1920X1080 @ 60 HZ", tvscreens.ENTRIES[0].help))
        self.assertEqual(model.activate(), (tvscreens.SCREEN, "display"))
        model.move(2)
        self.assertEqual(model.detail()[:2], ("AUDIO", ""))  # a value that fails is blank, not fatal

    def test_focus_stops_at_the_ends_and_the_list_scrolls_with_it(self):
        model = tvscreens.SettingsModel()
        self.assertFalse(model.move(-1))
        self.assertTrue(model.move(100))
        self.assertEqual(model.focused().label, "BACK")
        self.assertEqual(model.activate(), (tvscreens.VIEW, "home"))
        shown = model.visible(8)
        self.assertEqual(len(shown), 8)
        self.assertIn(model.focus, shown)
        self.assertEqual(shown[-1], len(tvscreens.ENTRIES) - 1)

    def test_values_come_from_the_launcher_state(self):
        updates = types.SimpleNamespace(enabled=True, available=lambda: "0.3.0", current="0.2.7")
        sources = tvscreens.value_sources(
            updates=updates, controllers=types.SimpleNamespace(line=lambda: ""), running=lambda: ["A", "B"],
            applications=lambda: 4, can_sleep=lambda: False, network=lambda: "ONLINE",
        )
        model = tvscreens.SettingsModel(sources)
        model.refresh()
        self.assertEqual(model.values["CHECK FOR UPDATES"], "ON")
        self.assertEqual(model.values["SOFTWARE UPDATE"], "0.3.0 AVAILABLE")
        self.assertEqual(model.values["ACTIVE APPLICATIONS"], "2 RUNNING")
        self.assertEqual(model.values["APPLICATIONS"], "4 APPLICATIONS")
        self.assertEqual(model.values["CONTROLLERS"], "NO CONTROLLER CONNECTED")
        self.assertIn("SLEEP AFTER NOT SUPPORTED", model.values["SLEEP & SCREEN"])
        updates.available = lambda: ""
        self.assertEqual(tvscreens.update_value(updates), "THIS BOX: COUCHLITEOS 0.2.7")

    def test_check_for_updates_switches_with_the_classic_words(self):
        checker = types.SimpleNamespace(enabled=True, online=lambda: False)
        checker.set_enabled = lambda value: setattr(checker, "enabled", value)
        self.assertEqual(tvscreens.toggle_updates(checker), "UPDATE CHECK OFF: NOTHING IS SENT")
        self.assertEqual(tvscreens.toggle_updates(checker), "UPDATE CHECK ON: WAITS UNTIL THIS PC IS ONLINE")
        checker.online = lambda: True
        checker.enabled = False
        self.assertIn("EVERY START", tvscreens.toggle_updates(checker))

    def test_every_settings_row_fits_the_screen(self):
        for height in (720, 1080, 2160):
            layout = tvlayout.Layout(height * 16 // 9, height)
            slots = tvscreens.list_slots(layout)
            self.assertGreaterEqual(slots, 5)
            self.assertLessEqual(slots * layout.px(58), layout.height - 2 * layout.margin_y)


class PowerModelTest(unittest.TestCase):
    def test_choices_and_questions(self):
        model = tvscreens.PowerModel(True, True, ["MOONLIGHT"])
        self.assertEqual(model.rows(), ["SLEEP", "RESTART", "SHUT DOWN", "BACK"])
        self.assertEqual(model.focused().question, "SLEEP NOW?")
        model.move(1)
        self.assertEqual(model.focused().request, "reboot")
        self.assertEqual(model.focused().question, "RESTART NOW? MOONLIGHT WILL BE CLOSED.")
        model.move(5)
        self.assertEqual(model.focused().request, "")
        self.assertEqual(tvscreens.PowerModel.done("poweroff"), "SHUTTING DOWN...")

    def test_a_pc_that_cannot_sleep_or_be_woken_says_so(self):
        self.assertEqual(tvscreens.PowerModel(False, True).rows()[0], "SLEEP: NOT SUPPORTED ON THIS PC")
        self.assertIn("POWER BUTTON", tvscreens.PowerModel(True, False).focused().question)


class UpdateProgressTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.run_dir = pathlib.Path(temporary.name)
        self.clock = Clock()
        self.progress = tvscreens.UpdateProgress("0.3.0", self.run_dir, self.clock)

    def status(self, **state):
        (self.run_dir / softwareupdate.STATUS).write_text(json.dumps(state))

    def test_phases_use_the_classic_texts_and_bar(self):
        view = self.progress.poll()
        self.assertEqual((view.title, view.text, view.percent), ("UPDATING TO COUCHLITEOS 0.3.0",
                                                                   "STARTING THE UPDATE SERVICE...", None))
        self.status(phase="downloading", percent=42, message="Downloading")
        view = self.progress.poll()
        self.assertEqual((view.text, view.percent), ("DOWNLOADING", 42))
        self.assertIn("ESC", view.hint)
        self.status(phase="restarting")
        self.assertEqual(self.progress.poll().text, softwareupdate.PHASE_TEXT["restarting"])

    def test_b_cancels_a_download_and_only_explains_later(self):
        self.status(phase="downloading", percent=5)
        self.progress.poll()
        self.progress.back()
        self.assertTrue((self.run_dir / softwareupdate.CANCEL).exists())
        self.assertEqual(self.progress.poll().hint, "CANCELLING...")
        self.status(phase="installing")
        self.progress.poll()
        self.progress.back()
        self.assertEqual(self.progress.poll().hint, "INSTALLING: PLEASE WAIT")
        self.status(phase="cancelled")
        self.assertEqual(self.progress.poll().finished, softwareupdate.CANCELLED)

    def test_failures_wait_for_a_press(self):
        self.clock.now += softwareupdate.START_WAIT
        view = self.progress.poll()
        self.assertEqual((view.text, view.bar, view.finished), (softwareupdate.NOT_STARTED, False, ""))
        self.assertEqual(view.hint, tvscreens.CONTINUE_HINT)
        self.progress.dismiss()
        self.assertEqual(self.progress.poll().finished, softwareupdate.NOT_STARTED)

    def test_a_service_message_and_a_restart_that_never_comes(self):
        self.status(phase="failed", message="NOT ENOUGH SPACE")
        self.assertEqual(self.progress.poll().text, "NOT ENOUGH SPACE")
        self.progress.back()
        self.assertEqual(self.progress.poll().finished, "NOT ENOUGH SPACE")
        progress = tvscreens.UpdateProgress("0.3.0", self.run_dir, self.clock)
        self.status(phase="restarting")
        progress.poll()
        self.clock.now += softwareupdate.RESTART_WAIT
        self.assertEqual(progress.poll().text, softwareupdate.NOT_RESTARTED)

    def test_an_update_under_way_is_watched_again_after_a_restart(self):
        self.assertFalse(tvscreens.update_running(self.run_dir))
        self.status(phase="failed")
        self.assertFalse(tvscreens.update_running(self.run_dir))
        self.status(phase="downloading")
        self.assertTrue(tvscreens.update_running(self.run_dir))
        (self.run_dir / softwareupdate.STATUS).unlink()
        (self.run_dir / softwareupdate.REQUEST).touch()
        self.assertTrue(tvscreens.update_running(self.run_dir))


class StartTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = pathlib.Path(temporary.name)

    def test_setup_is_due_until_complete_and_after_a_restart(self):
        run, setup = self.base / "run", self.base / "setup-complete"
        run.mkdir()
        self.assertTrue(tvscreens.setup_due(run, setup))
        setup.touch()
        self.assertFalse(tvscreens.setup_due(run, setup))  # the tour, not --screen setup, has the buttons
        (run / tvscreens.REOPEN_SETUP).touch()
        self.assertTrue(tvscreens.setup_due(run, setup))

    def test_markers_are_taken_once(self):
        (self.base / tvscreens.REOPEN_DISPLAY).touch()
        self.assertTrue(tvscreens.take_reopen_display(self.base))
        self.assertFalse(tvscreens.take_reopen_display(self.base))
        (self.base / tvscreens.SWITCH_INTERFACE).touch()
        self.assertTrue(tvscreens.take_switch_interface(self.base))
        self.assertFalse(tvscreens.take_switch_interface(self.base))
        tvscreens.write_update_watch("0.3.0", self.base)
        self.assertEqual(tvscreens.take_update_watch(self.base), "0.3.0")
        self.assertIsNone(tvscreens.take_update_watch(self.base))


class WhatsNewTest(unittest.TestCase):
    def test_lines_for_an_upgrade(self):
        version = whatsnew.RELEASES[0][0]
        title, lines = tvscreens.whats_new(version, whatsnew.RELEASES[1][0])  # from the release before
        self.assertEqual(title, f"WHAT'S NEW IN {version}")
        self.assertEqual(lines, [f"·  {feature}" for feature in whatsnew.FEATURES])
        _title, lines = tvscreens.whats_new(version, "")
        self.assertEqual(lines[0], whatsnew.RENAME_NOTICE)
        self.assertEqual(lines[-1], whatsnew.EARLIER)

    def test_due_marks_the_version_seen_and_skips_new_users(self):
        version_file, seen, setup = self.paths()
        version_file.write_text("0.2.7\n")
        seen.write_text("0.2.6\n")
        self.assertIsNone(whatsnew.due(markers=(seen,), setup_marker=setup, version_files=(version_file,)))
        self.assertEqual(seen.read_text().strip(), "0.2.7")  # a new user only records it
        seen.write_text("0.2.6\n")
        setup.touch()
        self.assertEqual(whatsnew.due(markers=(seen,), setup_marker=setup, version_files=(version_file,)),
                         ("0.2.7", "0.2.6"))
        self.assertIsNone(whatsnew.due(markers=(seen,), setup_marker=setup, version_files=(version_file,)))

    def paths(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = pathlib.Path(temporary.name)
        return base / "VERSION", base / "seen", base / "setup-complete"


if __name__ == "__main__":
    unittest.main()
