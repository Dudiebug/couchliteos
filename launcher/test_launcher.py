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

    def terminal_app(self):
        return self.module.apps.Application(
            id="terminal", name="TERMINAL", kind="command", command="/bin/bash",
            status_id="terminal", terminal=True,
        )

    def test_home_marks_the_launcher_as_holding_focus_for_the_controller(self):
        launcher = self.launcher()
        launcher.screen = Screen([-1])
        launcher.prepare_session = mock.Mock()
        launcher.setup_wizard = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "home.request").touch()
            seen = []
            launcher.active_applications = mock.Mock(
                side_effect=lambda: seen.append((run / "launcher-focus").exists())
            )
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "HOME_REQUEST", run / "home.request"
            ), mock.patch.object(
                self.module, "LAUNCHER_FOCUS", run / "launcher-focus", create=True
            ), mock.patch.object(self.module.display, "restore_saved_mode"), mock.patch.object(
                self.module.curses, "curs_set"
            ), mock.patch.object(self.module.curses, "use_default_colors"):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    launcher.run()
        self.assertEqual(seen, [True])

    def test_handing_focus_back_to_an_app_clears_the_launcher_focus_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            focus = pathlib.Path(directory) / "launcher-focus"
            focus.touch()
            with mock.patch.object(self.module, "LAUNCHER_FOCUS", focus, create=True), mock.patch.object(
                self.module.subprocess, "run", return_value=mock.Mock(returncode=1)
            ):
                self.assertFalse(self.module.Launcher.focus_app(self.terminal_app()))
                self.assertTrue(focus.exists())  # no window took focus
            with mock.patch.object(self.module, "LAUNCHER_FOCUS", focus, create=True), mock.patch.object(
                self.module.subprocess, "run", return_value=mock.Mock(returncode=0)
            ):
                self.assertTrue(self.module.Launcher.focus_app(self.terminal_app()))
            self.assertFalse(focus.exists())

    def test_starting_an_app_clears_the_marker_and_a_failed_start_restores_it(self):
        launcher = self.launcher()
        launcher.screen = Screen([10])
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            focus = run / "launcher-focus"
            focus.touch()

            def write(_path, _content):
                self.assertFalse(focus.exists())  # the new app gets the controller
                (run / "terminal-ready").touch()

            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "LAUNCHER_FOCUS", focus, create=True
            ), mock.patch.object(self.module.apps, "atomic_write", side_effect=write):
                self.assertTrue(launcher.launch_app(self.terminal_app()))
            self.assertFalse(focus.exists())
            # The failure screen is drawn in the launcher, so it must be navigable.
            with mock.patch.object(self.module, "LAUNCHER_FOCUS", focus, create=True):
                launcher.show_launch_failure("TERMINAL", "boom")
            self.assertTrue(focus.exists())

    def test_app_that_quits_cleanly_before_ready_returns_to_the_launcher_without_an_error(self):
        launcher = self.launcher()
        launcher.screen = Screen([-1] * 5)
        launcher.show_launch_failure = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)

            def write(_path, _content):
                (run / "terminal-status").write_text("exited: status 0\n")

            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module.apps, "atomic_write", side_effect=write
            ):
                self.assertTrue(launcher.launch_app(self.terminal_app()))
        launcher.show_launch_failure.assert_not_called()
        self.assertIn("EXITED", launcher.status)

    def command_app(self, app_id, name):
        return self.module.apps.Application(
            id=app_id, name=name, kind="command", command="/bin/true", status_id=app_id,
        )

    def test_second_configured_app_is_refused_while_another_one_runs(self):
        # One moonlightos-configured-app.service runs one app at a time; a queued
        # request would pop up unasked when the running app closes.
        chrome, terminal = self.command_app("google-chrome", "GOOGLE CHROME"), self.command_app("terminal", "TERMINAL")
        launcher = self.launcher()
        launcher.screen = Screen()
        launcher.show_launch_failure = mock.Mock()
        result = self.module.apps.LoadResult((chrome, terminal), ())
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "google-chrome-ready").touch()
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "application_result", return_value=result
            ):
                self.assertFalse(launcher.launch_app(terminal))
            self.assertFalse((run / "launch-app.request").exists())
        self.assertIn("GOOGLE CHROME", launcher.show_launch_failure.call_args.args[1])

    def test_timed_out_launch_removes_the_unused_request_of_every_app_kind(self):
        launcher = self.launcher()
        launcher.show_launch_failure = mock.Mock()
        moonlight = self.module.apps.Application(
            id="moonlight", name="MOONLIGHT", kind="request", request="start-moonlight", status_id="moonlight",
        )
        for app, request in ((self.terminal_app(), "launch-app.request"), (moonlight, "start-moonlight")):
            launcher.screen = Screen([-1] * 5)
            with tempfile.TemporaryDirectory() as directory:
                run = pathlib.Path(directory)
                clock = iter([0.0, 0.0] + [100.0] * 10)
                with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                    self.module.time, "monotonic", side_effect=lambda: next(clock)
                ):
                    self.assertFalse(launcher.launch_app(app))
                self.assertFalse((run / request).exists(), request)

    def test_user_added_apps_are_resumed_by_the_wayland_app_id_of_their_command(self):
        # UI-created apps store an uppercased name and Chrome kiosk windows are titled
        # by the page, so a title match can never find them.
        web = self.module.apps.Application(
            id="youtube", name="YOUTUBE", kind="command", command="/usr/bin/google-chrome-stable",
            arguments="--ozone-platform=wayland --kiosk --no-first-run https://youtube.com",
            status_id="youtube",
        )
        other = self.module.apps.Application(
            id="steam", name="STEAM", kind="command", command="/usr/bin/steam", status_id="steam",
        )
        for app, expected in ((web, "app_id:google-chrome"), (other, "app_id:steam")):
            with mock.patch.object(
                self.module.subprocess, "run", return_value=mock.Mock(returncode=0)
            ) as run:
                self.assertTrue(self.module.Launcher.focus_app(app))
            self.assertEqual(run.call_args_list[0].args[0], ["wlrctl", "toplevel", "focus", expected])
        with mock.patch.object(self.module.subprocess, "run", return_value=mock.Mock(returncode=1)) as run:
            self.assertFalse(self.module.Launcher.focus_app(web))
        self.assertEqual(
            [call.args[0][-1] for call in run.call_args_list],
            ["app_id:google-chrome", "app_id:google-chrome-stable", "title:YOUTUBE"],
        )
        # Terminal apps are foot windows titled with the app name.
        with mock.patch.object(self.module.subprocess, "run", return_value=mock.Mock(returncode=1)) as run:
            self.module.Launcher.focus_app(self.terminal_app())
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["title:TERMINAL"])

    def active_applications_screen(self, keys):
        class Recording(Screen):
            def __init__(self, keys):
                super().__init__(keys)
                self.text = []

            def addstr(self, _row, _column, text, *_args):
                self.text.append(text)

            addnstr = addstr

        launcher = self.launcher()
        launcher.screen = Recording(keys)
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "terminal-ready").touch()
            result = self.module.apps.LoadResult((self.terminal_app(),), ())
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "application_result", return_value=result
            ), mock.patch.object(self.module, "active_rdp_session", return_value=None):
                launcher.active_applications()
            return launcher.screen.text, sorted(path.name for path in run.glob("close-*"))

    def test_active_applications_name_the_controller_button_that_closes_not_x(self):
        # gamepad-nav sends Delete for Y/Square and F12 (on-screen keyboard) for X/Triangle.
        text, closed = self.active_applications_screen([self.module.curses.KEY_F12, 27])
        self.assertEqual(closed, [])
        hint = next(row for row in text if "CLOSES" in row)
        self.assertIn("Y (XBOX) / SQUARE (PS) CLOSES", hint)
        self.assertNotIn("X CLOSES", hint)
        # The keyboard keys keep working.
        for key in (ord("x"), self.module.curses.KEY_DC):
            _text, closed = self.active_applications_screen([key, 27])
            self.assertEqual(closed, ["close-terminal"])

    def display_preview(self, keys):
        """Run the display-mode confirmation with the given keys; return (events, save mock)."""
        events = []

        class Keys(Screen):
            def getch(self):
                events.append("getch")
                return super().getch()

        launcher = self.launcher()
        settings = self.module.Settings(Keys(keys), launcher)
        old = self.module.display.Mode(1280, 720, 60000, current=True)
        new = self.module.display.Mode(1920, 1080, 60000)
        output = mock.Mock(current_mode=new)
        output.name, output.identity = "HDMI-A-1", "tv"
        settings.output, settings.original_mode = output, old
        settings.resolution, settings.refresh_mhz = "1920x1080", 60000
        settings.refresh_outputs = mock.Mock()
        display = self.module.display
        with mock.patch.object(display, "valid_output_mode", return_value=(output, new)), mock.patch.object(
            display, "apply_mode"
        ), mock.patch.object(display, "save_display") as save, mock.patch.object(display, "log"), mock.patch.object(
            self.module.curses, "flushinp", side_effect=lambda: events.append("flush"), create=True
        ):
            settings.apply_preview()
        return events, save

    def test_display_confirmation_ignores_queued_input_and_defaults_to_revert(self):
        # A double-tapped A must not save a mode nobody has seen: it is reapplied every boot.
        events, save = self.display_preview([10])
        save.assert_not_called()
        self.assertEqual(events[0], "flush")
        # Choosing KEEP explicitly still saves it.
        _events, save = self.display_preview([self.module.curses.KEY_DOWN, 10])
        save.assert_called_once()

    def test_escape_does_not_lag_a_second_behind_the_b_button(self):
        # B is forwarded as a bare Esc; curses waits ESCDELAY (1000 ms) for a sequence.
        screen = Screen()
        with mock.patch.object(self.module.curses, "set_escdelay") as set_escdelay, mock.patch.object(
            self.module, "Launcher"
        ) as launcher:
            self.module.main(screen)
        set_escdelay.assert_called_once_with(25)
        launcher.assert_called_once_with(screen)

    def test_guide_pressed_inside_active_applications_closes_it_and_leaves_no_request(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            request = run / "home.request"
            request.touch()

            class GuideScreen(Screen):
                def getch(self):
                    if self.keys and self.keys[0] == "guide":
                        self.keys.pop(0)
                        request.touch()  # Guide again, while the menu is open
                        return 27
                    return super().getch()

            launcher = self.launcher()
            launcher.screen = GuideScreen([-1, "guide", -1])
            launcher.prepare_session = mock.Mock()
            launcher.setup_wizard = mock.Mock()
            launcher.running_applications = mock.Mock(return_value=[])
            entered = []
            real = launcher.active_applications
            launcher.active_applications = lambda: (entered.append(1), real())[1]
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "HOME_REQUEST", request
            ), mock.patch.object(self.module, "LAUNCHER_FOCUS", run / "launcher-focus", create=True), mock.patch.object(
                self.module.display, "restore_saved_mode"
            ), mock.patch.object(self.module.curses, "curs_set"), mock.patch.object(
                self.module.curses, "use_default_colors"
            ):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    launcher.run()
        self.assertEqual(len(entered), 1)

    def test_a_restarted_launcher_forgets_a_stale_focus_marker(self):
        launcher = self.launcher()
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            focus = run / "launcher-focus"
            focus.touch()
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "LAUNCHER_FOCUS", focus, create=True
            ), mock.patch.dict(self.module.os.environ, {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}):
                launcher.prepare_session()
            self.assertFalse(focus.exists())

    def test_controller_delete_key_corrects_text_already_in_a_field(self):
        # Y/Square arrives as KEY_DC; a prefilled port or host could not be edited before.
        class Keys(Screen):
            def get_wch(self):
                return self.keys.pop(0)

        delete = self.module.curses.KEY_DC
        settings = self.module.ApplicationsSettings(Keys([delete, delete, "\n"]), self.launcher())
        with mock.patch.object(self.module.curses, "curs_set"):
            self.assertEqual(settings.text_input("PORT", "PORT", 5, initial="3389"), "33")
        settings = self.module.ApplicationsSettings(Keys([delete, delete, "9", "\n"]), self.launcher())
        with mock.patch.object(self.module.curses, "curs_set"):
            self.assertEqual(settings.text_input("PORT", "PORT", 5, initial="3389"), "339")

    def form_screen(self, keys):
        """A screen whose getch and get_wch read one queue and that remembers every frame."""
        class FormScreen(Screen):
            def __init__(self, keys):
                super().__init__(keys)
                self.text, self.frames = [], []

            def erase(self):
                self.text = []

            def addstr(self, _row, _column, text, *_args):
                self.text.append(text)

            addnstr = addstr

            def get_wch(self):
                self.frames.append(" ".join(self.text))
                return self.keys.pop(0)

            getch = get_wch

        return FormScreen(keys)

    def test_b_on_a_changed_connection_form_asks_before_discarding_it(self):
        up, down, enter, back = self.module.curses.KEY_UP, self.module.curses.KEY_DOWN, 10, "\x1b"
        # Unchanged: B closes at once (a further key would raise "stop").
        screen = self.form_screen([27])
        self.module.RemoteDesktopSettings(screen, self.launcher()).edit_connection(None)
        self.assertNotIn("DISCARD", " ".join(screen.frames))
        # Changed: a stray A answers NO and the typed name is still there; YES discards.
        screen = self.form_screen([enter, "w", "\n", 27, enter, 27, down, enter])
        with mock.patch.object(self.module.curses, "curs_set"):
            self.module.RemoteDesktopSettings(screen, self.launcher()).edit_connection(None)
        asked = [index for index, frame in enumerate(screen.frames) if "DISCARD CHANGES?" in frame]
        self.assertTrue(asked)
        self.assertIn("DISPLAY NAME   w", screen.frames[asked[0] + 1])
        # The CANCEL button at the end of the form asks too (Up from the top wraps onto it).
        screen = self.form_screen([enter, "w", "\n", up, enter, enter, 27, down, enter])
        with mock.patch.object(self.module.curses, "curs_set"):
            self.module.RemoteDesktopSettings(screen, self.launcher()).edit_connection(None)
        self.assertIn("DISCARD CHANGES?", " ".join(screen.frames))

    def test_b_in_the_add_flows_asks_once_something_was_typed(self):
        down, enter = self.module.curses.KEY_DOWN, 10
        for flow in ("add_web", "add_command"):
            # The first field has nothing to lose.
            screen = self.form_screen(["\x1b"])
            getattr(self.module.ApplicationsSettings(screen, self.launcher()), flow)()
            self.assertNotIn("DISCARD", " ".join(screen.frames))
            # A later field asks; NO keeps the half-typed text, YES abandons the whole flow.
            screen = self.form_screen(["a", "\n", "/", "\x1b", enter, "x", "\x1b", down, enter])
            settings = self.module.ApplicationsSettings(screen, self.launcher())
            settings._write_user = mock.Mock()
            with mock.patch.object(self.module.curses, "curs_set"):
                getattr(settings, flow)()
            settings._write_user.assert_not_called()
            asked = [frame for frame in screen.frames if "DISCARD CHANGES?" in frame]
            self.assertTrue(asked, flow)
            self.assertFalse(screen.keys, flow)

    def power_request(self, action, keys, running=()):
        """Press A on REBOOT/SHUTDOWN, then `keys` on the question; return (files created, text drawn)."""
        class Recording(Screen):
            def __init__(self, keys):
                super().__init__(keys)
                self.text = []

            def addnstr(self, _row, _column, text, *_args):
                self.text.append(text)

        launcher = self.launcher()
        launcher.screen = Recording(keys)
        launcher.running_applications = mock.Mock(return_value=list(running))
        launcher.selected = [item[1] for item in launcher.menu].index(action)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ):
            launcher.activate()
            return sorted(path.name for path in pathlib.Path(directory).iterdir()), " ".join(launcher.screen.text)

    def test_reboot_and_shutdown_ask_first_and_default_to_no(self):
        down = self.module.curses.KEY_DOWN
        for action in ("reboot", "poweroff"):
            self.assertEqual(self.power_request(action, [10])[0], [], action)  # a stray A answers NO
            self.assertEqual(self.power_request(action, [27])[0], [], action)
            self.assertEqual(self.power_request(action, [down, 10])[0], [action])

    def test_the_power_question_names_what_is_still_running(self):
        _files, text = self.power_request("poweroff", [10], running=[self.terminal_app()])
        self.assertIn("TERMINAL", text)
        _files, text = self.power_request("poweroff", [10])
        self.assertNotIn("TERMINAL", text)

    def test_up_from_the_first_button_does_not_wrap_onto_shutdown(self):
        launcher = self.launcher()
        launcher.screen = Screen([self.module.curses.KEY_UP, -1])
        launcher.prepare_session = mock.Mock()
        launcher.setup_wizard = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module.display, "restore_saved_mode"), mock.patch.object(
            self.module.curses, "curs_set"
        ), mock.patch.object(self.module.curses, "use_default_colors"):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                launcher.run()
        self.assertEqual(launcher.selected, 0)

    def test_f12_while_the_keyboard_is_open_does_not_queue_a_second_one(self):
        # The keyboard's session removes start-osk as it opens; a second press in that window
        # used to re-create it and stack another keyboard once the first closed.
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ):
            run = pathlib.Path(directory)
            (run / "osk-active").touch()
            self.module.request_osk(masked=True)
            self.assertFalse((run / "start-osk").exists())
            self.assertFalse((run / "osk-masked").exists(), "a stale mask flag would hide the next keyboard")
            (run / "osk-active").unlink()
            self.module.request_osk(masked=True)
            self.assertTrue((run / "start-osk").exists())
            self.assertTrue((run / "osk-masked").exists())

    def test_progress_helpers(self):
        self.assertEqual(len(self.module.indeterminate_progress_bar(24, 0)), 24)
        self.assertEqual(self.module.format_elapsed(65.9), "01:05")


if __name__ == "__main__":
    unittest.main()
