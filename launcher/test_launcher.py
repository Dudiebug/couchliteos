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

    def setUp(self):
        # Whether this machine can suspend is decided per test, not by the machine running them.
        patcher = mock.patch.object(self.module.power, "can_suspend", return_value=True)
        self.can_suspend = patcher.start()
        self.addCleanup(patcher.stop)
        # run() must not read or write the real what's-new marker (test_whatsnew covers it).
        patcher = mock.patch.object(self.module.whatsnew, "show_once")
        patcher.start()
        self.addCleanup(patcher.stop)

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
            ["MOONLIGHT", "CHIAKI-NG", "FIREFOX", "GOOGLE CHROME", "TERMINAL", "TAILSCALE", "SETTINGS", "SLEEP", "REBOOT", "SHUTDOWN"],
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
        self.assertEqual(launcher.menu[-len(self.module.FIXED_CONTROLS):], list(self.module.FIXED_CONTROLS))

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
        ) as launcher, mock.patch.object(self.module.errors, "_EXTRA", []):  # main() registers WAKE PC
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

    def test_sleep_control_sits_between_settings_and_the_other_power_controls(self):
        self.assertEqual(
            [action for _label, action in self.module.FIXED_CONTROLS],
            ["settings", "suspend", "reboot", "poweroff"],
        )

    def test_choosing_sleep_asks_systemd_to_suspend_through_the_request_file(self):
        launcher = self.launcher()
        launcher.selected = [action for _label, action in launcher.menu].index("suspend")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ):
            launcher.activate()
            self.assertTrue((pathlib.Path(directory) / "suspend").exists())
        self.assertIn("SLEEP", launcher.status)

    def test_on_resume_drops_stale_requests_refocuses_the_launcher_and_reports(self):
        launcher = self.launcher()
        home = pathlib.Path(tempfile.mkdtemp()) / "home.request"
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module, "HOME_REQUEST", home), mock.patch.object(
            self.module, "focus_launcher"
        ) as focus:
            (pathlib.Path(directory) / "suspend").touch()
            home.touch()  # the long press that put the box to sleep also asked for Home
            launcher.on_resume()
            self.assertFalse((pathlib.Path(directory) / "suspend").exists())
            self.assertFalse(home.exists())
        focus.assert_called_once_with()
        self.assertIn("RESUMED", launcher.status)

    def test_resume_marker_is_consumed_once_and_calls_on_resume(self):
        launcher = self.launcher()
        launcher.on_resume = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ):
            self.assertFalse(launcher.check_resume())
            (pathlib.Path(directory) / "resumed").touch()
            self.assertTrue(launcher.check_resume())
            self.assertFalse(launcher.check_resume())
            self.assertFalse((pathlib.Path(directory) / "resumed").exists())
        launcher.on_resume.assert_called_once_with()

    def test_idle_protection_is_on_normally_and_off_during_the_qemu_smoke_test(self):
        launcher = self.launcher()
        self.assertTrue(launcher.idle.enabled())
        with mock.patch.object(self.module.power, "smoke_test_active", return_value=True):
            self.assertFalse(launcher.idle.enabled())

    def test_idle_guard_asks_the_launcher_whether_an_application_is_running(self):
        launcher = self.launcher()
        launcher.running_applications = mock.Mock(return_value=[object()])
        self.assertTrue(launcher.idle.apps_running())
        launcher.running_applications.return_value = []
        self.assertFalse(launcher.idle.apps_running())

    def test_a_failing_application_check_counts_as_running_so_the_box_never_sleeps_on_it(self):
        launcher = self.launcher()
        launcher.running_applications = mock.Mock(side_effect=ValueError("broken manifest"))
        self.assertTrue(launcher.apps_running())

    def test_unwritable_request_directory_reports_instead_of_crashing(self):
        launcher = self.launcher()
        with mock.patch.object(self.module, "RUN", pathlib.Path("/nonexistent-moonlightos-run")):
            launcher.request_sleep()
        self.assertIn("COULD NOT REQUEST SLEEP", launcher.status)

    def test_read_key_goes_through_the_idle_guard(self):
        guard = mock.Mock()
        guard.filter.return_value = -1
        with mock.patch.object(self.module, "IDLE_GUARD", guard):
            self.assertEqual(self.module.read_key(Screen([10])), -1)
        guard.filter.assert_called_once()
        self.assertEqual(guard.filter.call_args.args[1], 10)

    def test_settings_contains_sleep_and_screen_and_dispatches_to_it(self):
        self.assertIn("SLEEP & SCREEN", self.module.SETTINGS_MENU)
        settings = self.module.Settings(Screen(), self.launcher())
        settings.selected = self.module.SETTINGS_MENU.index("SLEEP & SCREEN")
        with mock.patch.object(self.module.Settings, "run_sleep_settings") as sleep_settings:
            self.assertTrue(settings.activate())
        sleep_settings.assert_called_once_with()

    def test_sleep_and_screen_settings_are_saved_and_applied_to_the_guard(self):
        keys = self.module.curses
        launcher = self.launcher()
        launcher.idle = mock.Mock()
        settings = self.module.Settings(Screen([10, keys.KEY_DOWN, 10, 27]), launcher)
        settings.choose = mock.Mock(side_effect=[15, 60])
        power = self.module.power
        with mock.patch.object(power, "load_settings", return_value=power.Settings(5, 30)), mock.patch.object(
            power, "save_settings"
        ) as save:
            settings.run_sleep_settings()
        self.assertEqual(
            [call.args[0] for call in save.call_args_list], [power.Settings(15, 30), power.Settings(15, 60)]
        )
        self.assertEqual(launcher.idle.apply.call_args_list[-1].args[0], power.Settings(15, 60))
        self.assertEqual(settings.choose.call_args_list[0].args[1][0], ("OFF", 0))
        self.assertEqual(settings.choose.call_args_list[1].args[1][-1], ("2 HOURS", 120))

    def sleep_settings_rows(self, keys=(27,), *, saved=None, wol=("none", None), wake=()):
        """Open SLEEP & SCREEN with the given keys; returns (settings screen, first drawn rows, save mock)."""
        power = self.module.power
        launcher = self.launcher()
        launcher.idle = mock.Mock()
        settings = self.module.Settings(Screen(list(keys)), launcher)
        settings.draw = mock.Mock()
        settings.choose = mock.Mock(return_value=15)
        saved = saved or power.Settings(5, 60)
        with mock.patch.object(power, "load_settings", return_value=saved), mock.patch.object(
            power, "save_settings"
        ) as save, mock.patch.object(power, "wake_on_lan", return_value=wol), mock.patch.object(
            power, "wake_sources", return_value=list(wake)
        ):
            settings.run_sleep_settings()
        return settings, settings.draw.call_args_list[0].args[1], save

    def test_unsupported_hardware_shows_sleep_disabled_and_selecting_it_does_nothing(self):
        self.can_suspend.return_value = False
        launcher = self.launcher()
        labels = [label for label, _action in launcher.menu]
        self.assertEqual(labels[-4:], ["SETTINGS", "SLEEP: NOT SUPPORTED ON THIS PC", "REBOOT", "SHUTDOWN"])
        launcher.selected = labels.index("SLEEP: NOT SUPPORTED ON THIS PC")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ):
            launcher.activate()
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])
        self.assertIn("NOT SUPPORTED", launcher.status)
        self.assertNotIn("GOING TO SLEEP", launcher.status)

    def test_a_sleep_request_from_the_idle_guard_is_refused_on_unsupported_hardware(self):
        self.can_suspend.return_value = False
        launcher = self.launcher()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ):
            launcher.request_sleep()
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])

    def test_supported_hardware_keeps_the_plain_sleep_control(self):
        launcher = self.launcher()
        self.assertEqual([label for label, _action in launcher.menu][-4:], ["SETTINGS", "SLEEP", "REBOOT", "SHUTDOWN"])

    def test_unsupported_hardware_ignores_the_saved_automatic_sleep_but_keeps_blanking(self):
        power = self.module.power
        self.can_suspend.return_value = False
        with mock.patch.object(power, "load_settings", return_value=power.Settings(10, 60)):
            launcher = self.launcher()
        self.assertEqual(launcher.idle.timer.settings, power.Settings(10, 0))

    def test_resume_checks_sleep_support_again_and_the_menu_and_timers_follow(self):
        power = self.module.power
        with mock.patch.object(power, "load_settings", return_value=power.Settings(5, 60)), tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module, "HOME_REQUEST", pathlib.Path(directory) / "home.request"), mock.patch.object(
            self.module, "focus_launcher"
        ):
            launcher = self.launcher()
            self.assertEqual(launcher.idle.timer.settings, power.Settings(5, 60))
            self.can_suspend.return_value = False
            launcher.on_resume()
            self.assertEqual(launcher.menu[-3], ("SLEEP: NOT SUPPORTED ON THIS PC", "suspend"))
            self.assertEqual(launcher.idle.timer.settings, power.Settings(5, 0))
            self.can_suspend.return_value = True
            launcher.on_resume()
            self.assertEqual(launcher.menu[-3], ("SLEEP", "suspend"))
            self.assertEqual(launcher.idle.timer.settings, power.Settings(5, 60))

    def test_sleep_after_row_says_not_supported_and_cannot_be_changed(self):
        keys = self.module.curses
        self.can_suspend.return_value = False
        settings, rows, save = self.sleep_settings_rows([keys.KEY_DOWN, 10, 27])
        self.assertEqual(rows[1], "SLEEP AFTER  NOT SUPPORTED ON THIS PC")
        self.assertEqual(rows[0], "BLANK SCREEN AFTER  5 MIN")  # blanking needs no special hardware
        settings.choose.assert_not_called()
        save.assert_not_called()
        self.assertIn("NOT SUPPORTED", settings.status)

    def test_changing_blanking_on_unsupported_hardware_keeps_the_sleep_value_saved_elsewhere(self):
        power = self.module.power
        self.can_suspend.return_value = False
        settings, _rows, save = self.sleep_settings_rows([10, 27])
        self.assertEqual([call.args[0] for call in save.call_args_list], [power.Settings(15, 60)])
        self.assertEqual(settings.launcher.idle.apply.call_args.args[0], power.Settings(15, 0))

    def test_wake_on_lan_row_shows_the_mac_only_when_the_adapter_can_wake_on_magic_packet(self):
        for wol, expected in (
            (("supported", "D8:BB:C1:01:02:03"), "WAKE-ON-LAN  MAC D8:BB:C1:01:02:03"),
            (("unsupported", None), "WAKE-ON-LAN  NOT SUPPORTED BY THIS NETWORK ADAPTER"),
            (("none", None), "WAKE-ON-LAN  NO WIRED NETWORK ADAPTER"),
            (("unknown", None), "WAKE-ON-LAN  ADAPTER NOT CHECKED"),
        ):
            _settings, rows, _save = self.sleep_settings_rows(wol=wol)
            self.assertEqual(rows[2], expected)

    def test_wake_sources_row_lists_what_can_wake_the_pc(self):
        _settings, rows, _save = self.sleep_settings_rows(wake=["BLUETOOTH ADAPTER", "USB KEYBOARD"])
        self.assertEqual(rows[3], "WAKE FROM  BLUETOOTH ADAPTER, USB KEYBOARD")
        _settings, rows, _save = self.sleep_settings_rows()
        self.assertEqual(rows[3], "WAKE FROM  NO USB BLUETOOTH ADAPTER OR KEYBOARD FOUND")

    def test_information_rows_do_nothing_and_back_leaves_the_screen(self):
        keys = self.module.curses
        down = keys.KEY_DOWN
        settings, rows, save = self.sleep_settings_rows([down, down, 10, down, 10, down, 10])
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[-1], "BACK")
        save.assert_not_called()
        settings.choose.assert_not_called()

    def test_progress_helpers(self):
        self.assertEqual(len(self.module.indeterminate_progress_bar(24, 0)), 24)
        self.assertEqual(self.module.format_elapsed(65.9), "01:05")


if __name__ == "__main__":
    unittest.main()
