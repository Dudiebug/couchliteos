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

    def audio_statuses(self, set_default):
        """Status line shown at every draw of the audio output screen."""
        settings = self.module.Settings(Screen([10, 27]), self.launcher())
        statuses = []
        settings.draw = lambda *_args, **_kwargs: statuses.append(settings.status)
        sinks = [self.module.audio.Sink(7, "HDMI OUTPUT", False)]
        with mock.patch.object(self.module.audio, "query_sinks", return_value=sinks), mock.patch.object(
            self.module.audio, "get_volume", return_value=self.module.audio.Volume(50, False)
        ), mock.patch.object(self.module.audio, "ensure_audible", return_value=""), mock.patch.object(
            self.module.audio, "set_default", side_effect=set_default
        ) as chosen:
            settings.run_audio()
        chosen.assert_called_once_with(7)
        return statuses

    def test_audio_output_failure_is_shown(self):
        def refuse(_sink_id):
            raise RuntimeError("wpctl could not switch")

        statuses = self.audio_statuses(refuse)
        self.assertEqual(len(statuses), 2)
        self.assertIn("OUTPUT NOT CHANGED: wpctl could not switch", statuses[-1])

    def test_audio_output_success_is_shown(self):
        statuses = self.audio_statuses(lambda _sink_id: None)
        self.assertIn("DEFAULT OUTPUT: HDMI OUTPUT", statuses[-1])

    def test_progress_helpers(self):
        self.assertEqual(len(self.module.indeterminate_progress_bar(24, 0)), 24)
        self.assertEqual(self.module.format_elapsed(65.9), "01:05")


if __name__ == "__main__":
    unittest.main()
