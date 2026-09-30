import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock


class Screen:
    def __init__(self, keys=()):
        self.keys = list(keys)

    def getmaxyx(self):
        return 30, 100

    def erase(self):
        pass

    def border(self, *_args):
        pass

    def addstr(self, *_args):
        pass

    def addnstr(self, *_args):
        pass

    def refresh(self):
        pass

    def timeout(self, *_args):
        pass

    def keypad(self, *_args):
        pass

    def getch(self):
        if not self.keys:
            raise RuntimeError("stop")
        return self.keys.pop(0)


class LauncherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("moonlightos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def launcher(self):
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            return self.module.Launcher(Screen())

    def test_ipv4_helpers(self):
        sample = "lo UNKNOWN 127.0.0.1/8\nenp2s0 UP 192.168.50.27/24\n"
        self.assertEqual(self.module.get_ipv4(sample), "192.168.50.27")
        self.assertEqual(self.module.get_ipv4(""), "NO IPV4")

    def test_default_application_order_includes_terminal(self):
        launcher = self.launcher()
        self.assertEqual(
            [label for label, _action in launcher.menu],
            ["MOONLIGHT", "CHIAKI-NG", "FIREFOX", "GOOGLE CHROME", "TERMINAL", "TAILSCALE", "SETTINGS", "REBOOT", "SHUTDOWN"],
        )

    def test_custom_order_and_disabled_visibility(self):
        application = self.module.apps.Application
        result = self.module.apps.LoadResult(
            (
                application(id="disabled", name="DISABLED", kind="command", command="/bin/true", status_id="disabled", enabled=False, order=1),
                application(id="custom", name="CUSTOM", kind="command", command="/bin/true", status_id="custom", order=2),
            ),
            (),
        )
        with mock.patch.object(self.module, "application_result", return_value=result), mock.patch.object(
            self.module, "network_summary", return_value="OFFLINE"
        ):
            launcher = self.module.Launcher(Screen())
        self.assertEqual(launcher.menu[0], ("CUSTOM", "custom"))
        self.assertNotIn("DISABLED", [label for label, _action in launcher.menu])
        self.assertEqual(launcher.menu[-3:], list(self.module.FIXED_CONTROLS))

    def test_invalid_manifests_do_not_crash_startup(self):
        result = self.module.apps.LoadResult((), ("bad.ini: invalid",))
        with mock.patch.object(self.module, "application_result", return_value=result), mock.patch.object(
            self.module, "network_summary", return_value="OFFLINE"
        ):
            launcher = self.module.Launcher(Screen())
        self.assertIn("INVALID", launcher.status)

    def test_request_application_retains_request_marker(self):
        app = self.module.apps.Application(
            id="moonlight", name="MOONLIGHT", kind="request", request="start-moonlight",
            status_id="moonlight",
        )
        launcher = self.launcher()
        launcher.screen = Screen()
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher.request = mock.Mock(side_effect=lambda _name: (run / "moonlight-ready").touch())
            with mock.patch.object(self.module, "RUN", run):
                self.assertTrue(launcher.launch_app(app))
        launcher.request.assert_called_once_with("start-moonlight")

    def test_command_application_uses_configured_request(self):
        app = self.module.apps.Application(
            id="terminal", name="TERMINAL", kind="command", command="/bin/bash",
            status_id="terminal", terminal=True,
        )
        launcher = self.launcher()
        launcher.screen = Screen()
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)

            def write(_path, content):
                self.assertEqual(content, "terminal\n")
                (run / "terminal-ready").touch()

            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module.apps, "atomic_write", side_effect=write
            ):
                self.assertTrue(launcher.launch_app(app))

    def test_application_settings_reload_launcher_menu(self):
        launcher = self.launcher()
        launcher.selected = len(launcher.applications)
        launcher.reload_applications = mock.Mock()
        with mock.patch.object(self.module.Settings, "run"):
            launcher.activate()
        launcher.reload_applications.assert_called_once_with()

    def test_launcher_ready_precedes_first_boot_wizard(self):
        launcher = self.launcher()
        launcher.screen = Screen()
        launcher.prepare_session = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher.setup_wizard = mock.Mock(side_effect=lambda: self.assertTrue((run / "launcher-ready").exists()))
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module.display, "restore_saved_mode"
            ), mock.patch.object(self.module.curses, "curs_set"), mock.patch.object(
                self.module.curses, "use_default_colors"
            ):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    launcher.run()
        launcher.setup_wizard.assert_called_once_with()

    def test_settings_contains_applications_and_setup(self):
        self.assertIn("DISPLAY", self.module.SETTINGS_MENU)
        self.assertIn("AUDIO", self.module.SETTINGS_MENU)
        self.assertIn("ACTIVE APPLICATIONS", self.module.SETTINGS_MENU)
        self.assertIn("TAILSCALE", self.module.SETTINGS_MENU)
        self.assertIn("APPLICATIONS", self.module.SETTINGS_MENU)
        self.assertIn("SETUP WIZARD", self.module.SETTINGS_MENU)
        self.assertFalse(hasattr(self.module.Launcher, "terminal_command"))

    def test_settings_contains_remote_desktop_and_dispatches_by_label(self):
        self.assertIn("REMOTE DESKTOP", self.module.SETTINGS_MENU)
        launcher = self.launcher()
        settings = self.module.Settings(Screen(), launcher)
        settings.selected = self.module.SETTINGS_MENU.index("REMOTE DESKTOP")
        with mock.patch.object(self.module.Settings, "run_remote_desktop") as remote:
            self.assertTrue(settings.activate())
        remote.assert_called_once_with()
        settings.selected = self.module.SETTINGS_MENU.index("BACK")
        self.assertFalse(settings.activate())

    def rdp_button(self, **changes):
        base = self.module.apps.Application(
            id="rdp-work-pc", name="OFFICE", kind="rdp", connection="rdp-work-pc",
            status_id="rdp-work-pc", shortcut="lb",
        )
        return self.module.apps.dataclasses.replace(base, **changes)

    def test_shortcut_key_launches_the_assigned_button_and_labels_show_it(self):
        result = self.module.apps.LoadResult((self.rdp_button(),), ())
        with mock.patch.object(self.module, "application_result", return_value=result), mock.patch.object(
            self.module, "network_summary", return_value="OFFLINE"
        ):
            launcher = self.module.Launcher(Screen([self.module.curses.KEY_F5]))
        self.assertEqual(launcher.menu[0], ("OFFICE  [LB]", "rdp-work-pc"))
        launcher.launch_app = mock.Mock(return_value=True)
        launcher.prepare_session = mock.Mock()
        launcher.setup_wizard = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module.display, "restore_saved_mode"), mock.patch.object(
            self.module.curses, "curs_set"
        ), mock.patch.object(self.module.curses, "use_default_colors"):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                launcher.run()
        launcher.launch_app.assert_called_once_with(self.rdp_button())

    def test_rdp_launch_writes_its_own_request_after_preparation(self):
        launcher = self.launcher()
        launcher.screen = Screen()
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)

            def write(path, content):
                self.assertEqual((path.name, content), ("rdp.request", "rdp-work-pc\n"))
                (run / "rdp-work-pc-ready").touch()

            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module.RemoteDesktopSettings, "prepare_launch", return_value=True
            ) as prepare, mock.patch.object(self.module.apps, "atomic_write", side_effect=write):
                self.assertTrue(launcher.launch_app(self.rdp_button()))
            prepare.assert_called_once_with(self.rdp_button())
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module.RemoteDesktopSettings, "prepare_launch", return_value=False
            ), mock.patch.object(self.module.apps, "atomic_write") as never:
                (run / "rdp-work-pc-ready").unlink()
                self.assertFalse(launcher.launch_app(self.rdp_button()))
            never.assert_not_called()

    def test_unpinned_session_is_listed_in_active_applications(self):
        launcher = self.launcher()
        connection = self.module.rdp.Connection(
            id="rdp-lab", name="Lab", host="lab.example", username="alice", certificate="ab" * 32,
        )
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "rdp-session").write_text("rdp-lab\n")
            (run / "rdp-lab-ready").touch()
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module.rdp, "get_connection", return_value=connection
            ):
                running = launcher.running_applications()
        self.assertEqual([(app.id, app.kind, app.status_id) for app in running], [("rdp-lab", "rdp", "rdp-lab")])

    def remote(self, connection, fingerprint):
        launcher = self.launcher()
        launcher.show_launch_failure = mock.Mock()
        remote = self.module.RemoteDesktopSettings(Screen(), launcher)
        patches = [
            mock.patch.object(self.module.rdp, "get_connection", return_value=connection),
            mock.patch.object(self.module.rdp, "probe_certificate", return_value=fingerprint),
            mock.patch.object(self.module.rdp, "upsert_connection"),
            mock.patch.object(self.module, "rdp_log"),
        ]
        return launcher, remote, patches

    def test_first_connection_pins_the_accepted_fingerprint_and_hands_off_the_password(self):
        connection = self.module.rdp.Connection(id="rdp-work-pc", name="Work", host="10.0.0.9", username="alice")
        launcher, remote, patches = self.remote(connection, "cd" * 32)
        remote.confirm_certificate = mock.Mock(return_value=True)
        remote.text_input = mock.Mock(return_value="Fake-Typed-Password")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), patches[0], patches[1], patches[2] as upsert, patches[3]:
            self.assertTrue(remote.prepare_launch(self.rdp_button()))
            handoff = pathlib.Path(directory) / "rdp-session.secret"
            self.assertEqual(handoff.read_text(), "rdp-work-pc\nFake-Typed-Password")
            self.assertEqual(handoff.stat().st_mode & 0o777, 0o600)
        self.assertEqual(upsert.call_args.args[0].certificate, "cd" * 32)
        self.assertTrue(remote.text_input.call_args.kwargs["masked"])

    def test_changed_fingerprint_is_blocked_without_asking_for_a_password(self):
        connection = self.module.rdp.Connection(
            id="rdp-work-pc", name="Work", host="10.0.0.9", username="alice", certificate="ab" * 32,
        )
        launcher, remote, patches = self.remote(connection, "cd" * 32)
        remote.message = mock.Mock()
        remote.text_input = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), patches[0], patches[1], patches[2] as upsert, patches[3] as log:
            self.assertFalse(remote.prepare_launch(self.rdp_button()))
            self.assertFalse((pathlib.Path(directory) / "rdp-session.secret").exists())
        upsert.assert_not_called()
        remote.text_input.assert_not_called()
        self.assertIn("CERTIFICATE CHANGED", remote.message.call_args.args[0])
        self.assertIn("changed", log.call_args.args[0])
        self.assertIn("BLOCKED", launcher.status)

    def test_saved_password_is_staged_by_the_root_helper_without_a_prompt(self):
        connection = self.module.rdp.Connection(
            id="rdp-work-pc", name="Work", host="10.0.0.9", username="alice", certificate="ab" * 32,
            save_password=True,
        )
        launcher, remote, patches = self.remote(connection, "ab" * 32)
        remote.text_input = mock.Mock()
        remote.password_request = mock.Mock(return_value=(True, "password handed to the session"))
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), patches[0], patches[1], patches[2], patches[3]:
            stale = pathlib.Path(directory) / "rdp-session.secret"
            stale.write_text("stale")
            self.assertTrue(remote.prepare_launch(self.rdp_button()))
            self.assertFalse(stale.exists())
            remote.password_request.return_value = (False, "the saved password belongs to different server settings")
            self.assertFalse(remote.prepare_launch(self.rdp_button()))
        remote.password_request.assert_called_with("stage", "rdp-work-pc")
        remote.text_input.assert_not_called()
        self.assertIn("DIFFERENT SERVER", launcher.show_launch_failure.call_args.args[1])

    def test_a_session_that_is_still_reconnecting_blocks_another_connection(self):
        connection = self.module.rdp.Connection(id="rdp-work-pc", name="Work", host="10.0.0.9", username="alice")
        launcher, remote, patches = self.remote(connection, "cd" * 32)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), patches[0], patches[1] as probe, patches[2], patches[3]:
            (pathlib.Path(directory) / "rdp-session").write_text("rdp-other\n")
            self.assertFalse(remote.prepare_launch(self.rdp_button()))
        probe.assert_not_called()
        self.assertIn("RECONNECTING", launcher.show_launch_failure.call_args.args[1])

    def test_timed_out_rdp_launch_removes_the_unused_request_and_password(self):
        launcher = self.launcher()
        launcher.screen = Screen([-1] * 5)
        launcher.show_launch_failure = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)

            def prepare(_app):
                (run / "rdp-session.secret").write_text("rdp-work-pc\nFake")
                return True

            clock = iter([0.0, 0.0] + [100.0] * 10)
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module.RemoteDesktopSettings, "prepare_launch", side_effect=prepare
            ), mock.patch.object(self.module.time, "monotonic", side_effect=lambda: next(clock)):
                self.assertFalse(launcher.launch_app(self.rdp_button()))
            self.assertFalse((run / "rdp.request").exists())
            self.assertFalse((run / "rdp-session.secret").exists())

    # --- setup wizard glue: the wizard logic lives in moonlightos_setup -----

    def run_wizard_glue(self, launcher, **arguments):
        captured = {}

        class FakeWizard:
            def __init__(self, ui, actions, system, **options):
                captured.update(ui=ui, actions=actions, system=system, options=options)

            def run(self, force=False):
                captured["force"] = force

        launcher.reload_applications = mock.Mock()
        with mock.patch.object(self.module.setup, "SetupWizard", FakeWizard), mock.patch.object(
            self.module.bluetooth, "BluetoothClient"
        ):
            launcher.setup_wizard(**arguments)
        captured["reloaded"] = launcher.reload_applications.called
        return captured

    def test_wizard_glue_hands_the_wizard_exactly_the_actions_it_calls(self):
        launcher = self.launcher()
        captured = self.run_wizard_glue(launcher, force=True)
        self.assertEqual(
            set(captured["actions"]),
            {"text", "display", "tone", "launch", "pair_moonlight", "applications"},
        )
        self.assertTrue(captured["force"])
        self.assertTrue(captured["reloaded"])
        self.assertIn("tailscale", captured["options"]["statuses"])

    def test_wizard_glue_offers_tv_control_only_when_the_screen_exists(self):
        launcher = self.launcher()
        self.assertNotIn("tv", self.run_wizard_glue(launcher)["actions"])
        launcher.tv_control_screen = mock.Mock()
        actions = self.run_wizard_glue(launcher)["actions"]
        actions["tv"]()
        launcher.tv_control_screen.assert_called_once_with()

    def test_wizard_text_entry_opens_the_keyboard_and_keeps_passwords_masked(self):
        launcher = self.launcher()
        actions = self.run_wizard_glue(launcher)["actions"]
        with mock.patch.object(self.module, "request_osk") as osk, mock.patch.object(
            self.module.ApplicationsSettings, "text_input", return_value="secret-pass"
        ) as text_input:
            self.assertEqual(actions["text"]("WI-FI PASSWORD", "PASSWORD FOR HomeNet", 64, masked=True), "secret-pass")
            osk.assert_called_once_with(True)
            text_input.assert_called_once_with("WI-FI PASSWORD", "PASSWORD FOR HomeNet", 64, masked=True)
            osk.reset_mock()
            actions["text"]("GAMING PC", "ADDRESS", 253)
            osk.assert_called_once_with(False)

    def display_settings(self, current_after_preview):
        mode = self.module.display.Mode
        settings = mock.Mock()
        settings.refresh_outputs.return_value = True
        settings.output = mock.Mock(current_mode=mode(1920, 1080, 60000))

        def preview():
            settings.output = mock.Mock(current_mode=current_after_preview)

        settings.apply_preview.side_effect = preview
        return settings

    def test_wizard_display_reuses_the_preview_and_reports_a_confirmed_mode(self):
        mode = self.module.display.Mode
        settings = self.display_settings(mode(1280, 720, 60000))
        launcher = self.launcher()
        with mock.patch.object(self.module, "Settings", return_value=settings):
            self.assertTrue(launcher.wizard_display("1280x720", 60000))
        self.assertEqual((settings.resolution, settings.refresh_mhz), ("1280x720", 60000))
        settings.apply_preview.assert_called_once_with()

    def test_wizard_display_reports_a_rolled_back_mode_as_not_confirmed(self):
        mode = self.module.display.Mode
        settings = self.display_settings(mode(1920, 1080, 60000))
        launcher = self.launcher()
        with mock.patch.object(self.module, "Settings", return_value=settings):
            self.assertFalse(launcher.wizard_display("1280x720", 60000))

    def test_wizard_display_fails_cleanly_without_an_active_output(self):
        settings = mock.Mock()
        settings.refresh_outputs.return_value = False
        launcher = self.launcher()
        with mock.patch.object(self.module, "Settings", return_value=settings):
            self.assertFalse(launcher.wizard_display("1280x720", 60000))
        settings.apply_preview.assert_not_called()

    def waiting_application(self):
        return self.module.apps.Application(
            id="tailscale", name="TAILSCALE", kind="request", request="start-tailscale", status_id="tailscale",
        )

    def test_launch_and_wait_returns_when_the_application_exits(self):
        launcher = self.launcher()
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "tailscale-ready").touch()
            app = self.waiting_application()
            launcher.app_by_id = mock.Mock(return_value=app)
            launcher.launch_app = mock.Mock(return_value=True)
            polls = []

            def getch():
                polls.append(1)
                if len(polls) == 3:
                    (run / "tailscale-ready").unlink()
                return -1

            launcher.screen = Screen()
            launcher.screen.getch = getch
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "HOME_REQUEST", run / "home.request"
            ):
                self.assertTrue(launcher.launch_and_wait("tailscale"))
        self.assertEqual(len(polls), 3)
        launcher.launch_app.assert_called_once_with(app)

    def test_launch_and_wait_reports_an_unknown_or_failed_application(self):
        launcher = self.launcher()
        launcher.screen = Screen()
        launcher.app_by_id = mock.Mock(return_value=None)
        self.assertFalse(launcher.launch_and_wait("nothing"))
        launcher.app_by_id = mock.Mock(return_value=self.rdp_button())
        launcher.launch_app = mock.Mock(return_value=False)
        self.assertFalse(launcher.launch_and_wait("rdp-work-pc"))

    def test_launch_and_wait_shows_the_home_menu_when_home_is_pressed(self):
        launcher = self.launcher()
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "tailscale-ready").touch()
            home = run / "home.request"
            home.touch()
            launcher.app_by_id = mock.Mock(return_value=self.waiting_application())
            launcher.launch_app = mock.Mock(return_value=True)
            launcher.active_applications = mock.Mock(side_effect=lambda: (run / "tailscale-ready").unlink())
            launcher.screen = Screen()
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(self.module, "HOME_REQUEST", home):
                self.assertTrue(launcher.launch_and_wait("tailscale"))
            self.assertFalse(home.exists())
        launcher.active_applications.assert_called_once_with()

    def waiting_launcher(self, run, keys):
        launcher = self.launcher()
        (run / "tailscale-ready").touch()
        launcher.app_by_id = mock.Mock(return_value=self.waiting_application())
        launcher.launch_app = mock.Mock(return_value=True)
        launcher.screen = Screen()
        pending = list(keys)

        def getch():
            if (run / "close-tailscale").exists():
                (run / "tailscale-ready").unlink(missing_ok=True)
            return pending.pop(0) if pending else -1

        launcher.screen.getch = getch
        return launcher

    def test_launch_and_wait_shows_the_given_lines_and_large_code(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher = self.waiting_launcher(run, [27])
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "HOME_REQUEST", run / "home.request"
            ), mock.patch.object(self.module.setup, "CursesUI") as ui:
                launcher.launch_and_wait("tailscale", lines=["TYPE THIS"], big="0427", patience=60)
        ui.return_value.status.assert_called_with("SETUP", ["TYPE THIS"], big="0427")

    def test_escape_cancels_a_patient_wait_by_closing_the_application(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher = self.waiting_launcher(run, [-1, 27])
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "HOME_REQUEST", run / "home.request"
            ):
                self.assertTrue(launcher.launch_and_wait("tailscale", patience=60))
            self.assertTrue((run / "close-tailscale").exists())

    def test_a_wait_that_runs_out_of_patience_closes_the_application(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher = self.waiting_launcher(run, [])
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "HOME_REQUEST", run / "home.request"
            ):
                self.assertTrue(launcher.launch_and_wait("tailscale", patience=0))
            self.assertTrue((run / "close-tailscale").exists())

    def test_escape_does_not_close_an_application_that_was_not_started_patiently(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher = self.waiting_launcher(run, [27, 27])
            polls = []
            original = launcher.screen.getch

            def getch():
                value = original()
                polls.append(value)
                if len(polls) == 2:
                    (run / "tailscale-ready").unlink()
                return value

            launcher.screen.getch = getch
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "HOME_REQUEST", run / "home.request"
            ):
                launcher.launch_and_wait("tailscale")
            self.assertFalse((run / "close-tailscale").exists())

    def test_moonlight_pairing_runs_as_a_temporary_hidden_app_and_cleans_up(self):
        launcher = self.launcher()
        written = []
        with tempfile.TemporaryDirectory() as directory:
            user_dir = pathlib.Path(directory)
            real_write = self.module.apps.write_user_application

            def write(app):
                written.append(app)
                return real_write(app, system_dir=pathlib.Path("/nonexistent"), user_dir=user_dir)

            def delete(app_id):
                (user_dir / f"{app_id}.ini").unlink()

            def launch(_app_id, **_options):
                self.assertTrue((user_dir / "moonlight-pair.ini").exists())
                return True

            launcher.launch_and_wait = mock.Mock(side_effect=launch)
            with mock.patch.object(self.module.apps, "write_user_application", side_effect=write), mock.patch.object(
                self.module.apps, "delete_user_application", side_effect=delete
            ):
                self.assertTrue(launcher.pair_moonlight("192.168.1.20", "0427"))
            self.assertEqual(list(user_dir.iterdir()), [])
        app = written[0]
        self.assertEqual(app.id, "moonlight-pair")
        self.assertEqual(app.kind, "command")
        self.assertFalse(app.visible)
        self.assertEqual(app.command, "/opt/moonlightos/apps/moonlight/usr/bin/moonlight")
        self.assertEqual(app.arguments, "pair 192.168.1.20 --pin 0427")
        self.assertEqual(app.environment["QT_QPA_PLATFORM"], "xcb")
        self.assertEqual(app.environment["APPDIR"], "/opt/moonlightos/apps/moonlight")
        self.assertEqual(app.environment["LD_LIBRARY_PATH"], "/opt/moonlightos/apps/moonlight/usr/lib")
        launcher.launch_and_wait.assert_called_once()
        call = launcher.launch_and_wait.call_args
        self.assertEqual(call.args, ("moonlight-pair",))
        self.assertEqual(call.kwargs["big"], "0427")
        self.assertIsNotNone(call.kwargs["patience"])
        self.assertNotIn("0427", launcher.status)

    def test_moonlight_pairing_removes_the_pin_even_when_the_launch_fails(self):
        launcher = self.launcher()
        launcher.launch_and_wait = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.object(self.module.apps, "write_user_application"), mock.patch.object(
            self.module.apps, "delete_user_application"
        ) as delete:
            with self.assertRaises(RuntimeError):
                launcher.pair_moonlight("192.168.1.20", "0427")
        delete.assert_called_once_with("moonlight-pair")

    def test_moonlight_pairing_reports_an_unwritable_manifest(self):
        launcher = self.launcher()
        launcher.launch_and_wait = mock.Mock()
        with mock.patch.object(
            self.module.apps, "write_user_application", side_effect=OSError("read-only")
        ), mock.patch.object(self.module.apps, "delete_user_application"):
            self.assertFalse(launcher.pair_moonlight("192.168.1.20", "0427"))
        launcher.launch_and_wait.assert_not_called()

    def test_progress_helpers(self):
        self.assertEqual(len(self.module.indeterminate_progress_bar(24, 0)), 24)
        self.assertEqual(self.module.format_elapsed(65.9), "01:05")


if __name__ == "__main__":
    unittest.main()
