import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import os
import pathlib
import tempfile
import time
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
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
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
        # Something that can wake the PC is plugged in unless a test says otherwise.
        patcher = mock.patch.object(self.module.power, "wake_sources", return_value=["USB KEYBOARD"])
        self.wake_sources = patcher.start()
        self.addCleanup(patcher.stop)

    def launcher(self):
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            return self.module.Launcher(Screen())

    def test_ipv4_helpers(self):
        sample = "lo UNKNOWN 127.0.0.1/8\nenp2s0 UP 192.168.50.27/24\n"
        self.assertEqual(self.module.get_ipv4(sample), "192.168.50.27")
        self.assertEqual(self.module.get_ipv4(""), "NO IPV4")

    def test_default_application_order_includes_terminal(self):
        with mock.patch.object(self.module.apps, "installed", return_value=True):  # both browsers installed
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
        self.assertEqual(launcher.menu[0], ("OFFICE  [LB / L1]", "rdp-work-pc"))
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
        launcher.show_failure = mock.Mock(return_value="dismiss")
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
        self.assertIn("DIFFERENT SERVER", launcher.show_failure.call_args.args[0].detail)

    def test_a_session_that_is_still_reconnecting_blocks_another_connection(self):
        connection = self.module.rdp.Connection(id="rdp-work-pc", name="Work", host="10.0.0.9", username="alice")
        launcher, remote, patches = self.remote(connection, "cd" * 32)
        launcher.show_failure = mock.Mock(return_value="dismiss")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), patches[0], patches[1] as probe, patches[2], patches[3]:
            (pathlib.Path(directory) / "rdp-session").write_text("rdp-other\n")
            self.assertFalse(remote.prepare_launch(self.rdp_button()))
        probe.assert_not_called()
        self.assertIn("RECONNECTING", launcher.show_failure.call_args.args[0].detail)

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
        # One couchliteos-configured-app.service runs one app at a time; a queued
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

    # Resuming a stream whose window the exact app id / title do not match.

    def stream_app(self, app_id="moonlight"):
        return self.module.apps.Application(
            id=app_id, name=app_id.upper(), kind="request", request=f"start-{app_id}", status_id=app_id,
        )

    def fake_wlrctl(self, listing, focusable, timeouts=()):
        """subprocess.run stand-in: `list` prints listing; `focus X` succeeds only for X in focusable."""
        calls = []

        def run(command, **_kwargs):
            calls.append(command[2:])
            if command[2] == "list":
                return mock.Mock(returncode=0, stdout=listing)
            if command[-1] in timeouts:
                raise self.module.subprocess.TimeoutExpired(command, 2)
            return mock.Mock(returncode=0 if command[-1] in focusable else 1, stdout="")

        return run, calls

    def test_a_timeout_on_one_match_tries_the_next_instead_of_giving_up(self):
        run, calls = self.fake_wlrctl("", {"title:Moonlight"}, timeouts={"app_id:moonlight"})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "LAUNCHER_FOCUS", pathlib.Path(directory) / "launcher-focus", create=True
        ), mock.patch.object(self.module.subprocess, "run", side_effect=run):
            self.assertTrue(self.module.Launcher.focus_app(self.stream_app()))
        self.assertEqual([call[-1] for call in calls], ["app_id:moonlight", "title:Moonlight"])

    def test_a_stream_window_with_another_name_is_found_in_the_list(self):
        listing = "couchliteos-launcher: CouchLiteOS Launcher\nfoot: TERMINAL\nMoonlight: Moonlight Streaming\n"
        run, calls = self.fake_wlrctl(listing, {"app_id:Moonlight"})
        with tempfile.TemporaryDirectory() as directory:
            focus = pathlib.Path(directory) / "launcher-focus"
            focus.touch()
            with mock.patch.object(self.module, "LAUNCHER_FOCUS", focus, create=True), mock.patch.object(
                self.module.subprocess, "run", side_effect=run
            ):
                self.assertTrue(self.module.Launcher.focus_app(self.stream_app()))
            self.assertFalse(focus.exists())
        self.assertEqual(calls[-1], ["focus", "app_id:Moonlight"])

    def test_the_title_is_used_when_the_window_has_no_app_id(self):
        run, calls = self.fake_wlrctl(": Moonlight (1920x1080) - 60 FPS\n", {"title:Moonlight (1920x1080) - 60 FPS"})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "LAUNCHER_FOCUS", pathlib.Path(directory) / "launcher-focus", create=True
        ), mock.patch.object(self.module.subprocess, "run", side_effect=run):
            self.assertTrue(self.module.Launcher.focus_app(self.stream_app()))

    def test_chiaki_is_found_by_its_own_word_and_the_launcher_is_never_picked(self):
        listing = "couchliteos-launcher: CouchLiteOS Moonlight Chiaki Launcher\nChiaki-Window: Remote Play\n"
        run, calls = self.fake_wlrctl(listing, {"app_id:Chiaki-Window"})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "LAUNCHER_FOCUS", pathlib.Path(directory) / "launcher-focus", create=True
        ), mock.patch.object(self.module.subprocess, "run", side_effect=run):
            self.assertTrue(self.module.Launcher.focus_app(self.stream_app("chiaki-ng")))
        self.assertNotIn(["focus", "app_id:couchliteos-launcher"], calls)

    def test_app_active_follows_the_app_brought_to_the_front(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            active = run / "app-active"
            active.write_text("firefox\n", encoding="ascii")
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "LAUNCHER_FOCUS", run / "launcher-focus", create=True
            ):
                with mock.patch.object(self.module.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
                    self.assertFalse(self.module.Launcher.focus_app(self.stream_app()))
                self.assertEqual(active.read_text(encoding="ascii"), "firefox\n")  # nothing came to the front
                with mock.patch.object(self.module.subprocess, "run", return_value=mock.Mock(returncode=0)):
                    self.assertTrue(self.module.Launcher.focus_app(self.stream_app()))
                    self.assertEqual(active.read_text(encoding="ascii"), "moonlight\n")
                    self.assertEqual(active.stat().st_mode & 0o777, 0o640)
                    # A terminal app leaves it alone, as its start does: gamepad-nav types into it.
                    self.assertTrue(self.module.Launcher.focus_app(self.terminal_app()))
                    self.assertEqual(active.read_text(encoding="ascii"), "moonlight\n")

    def test_no_stream_window_leaves_the_marker_and_logs_what_the_compositor_lists(self):
        listing = "couchliteos-launcher: CouchLiteOS Launcher\nfoot: TERMINAL\n"
        run, _calls = self.fake_wlrctl(listing, set())
        with tempfile.TemporaryDirectory() as directory:
            focus = pathlib.Path(directory) / "launcher-focus"
            focus.touch()
            with mock.patch.object(self.module, "LAUNCHER_FOCUS", focus, create=True), mock.patch.object(
                self.module.subprocess, "run", side_effect=run
            ), mock.patch.object(self.module.display, "log") as log, mock.patch.object(
                self.module.sys, "stderr"
            ) as stderr:
                self.assertFalse(self.module.Launcher.focus_app(self.stream_app()))
            self.assertTrue(focus.exists())
        stderr.write.assert_not_called()  # stderr is the launcher's own screen
        logged, path = log.call_args.args
        self.assertIn("no window for moonlight", logged)
        self.assertIn("foot: TERMINAL", logged)
        self.assertEqual(path, pathlib.Path("/var/log/couchliteos/launcher.log"))

    def test_a_window_whose_app_id_names_the_client_beats_a_tab_that_mentions_it(self):
        listing = "firefox-esr: Moonlight setup guide - Mozilla Firefox\nmoonlight-qt: Moonlight\n"
        run, calls = self.fake_wlrctl(listing, {"app_id:moonlight-qt", "app_id:firefox-esr"})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "LAUNCHER_FOCUS", pathlib.Path(directory) / "launcher-focus", create=True
        ), mock.patch.object(self.module.subprocess, "run", side_effect=run):
            self.assertTrue(self.module.Launcher.focus_app(self.stream_app()))
        self.assertEqual(calls[-1], ["focus", "app_id:moonlight-qt"])
        # With no such window the browser is left alone: Moonlight really is not on screen.
        run, calls = self.fake_wlrctl("firefox-esr: Moonlight setup guide\n", {"app_id:firefox-esr"})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "LAUNCHER_FOCUS", pathlib.Path(directory) / "launcher-focus", create=True
        ), mock.patch.object(self.module.subprocess, "run", side_effect=run), mock.patch.object(
            self.module.display, "log"
        ):
            self.assertFalse(self.module.Launcher.focus_app(self.stream_app()))
        self.assertNotIn(["focus", "app_id:firefox-esr"], calls)

    def test_a_missing_wlrctl_is_not_retried_for_every_match(self):
        with mock.patch.object(self.module.subprocess, "run", side_effect=FileNotFoundError("wlrctl")) as run:
            self.assertFalse(self.module.Launcher.focus_app(self.stream_app()))
        self.assertEqual(run.call_count, 1)

    def test_other_apps_do_not_read_the_window_list(self):
        run, calls = self.fake_wlrctl("firefox: Mozilla Firefox\n", set())
        with mock.patch.object(self.module.subprocess, "run", side_effect=run):
            self.assertFalse(self.module.Launcher.focus_app(self.terminal_app()))
        self.assertNotIn(["list"], [call[:1] for call in calls])

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
        self.assertIn("Y / SQUARE OR DELETE CLOSES", hint)
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
        ), mock.patch.object(self.module.Settings, "restart_for_picture_size"):
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
        launcher.screen = Screen([self.module.curses.KEY_DOWN, 10])  # SLEEP asks first; answer YES
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ):
            launcher.activate()
            self.assertTrue((pathlib.Path(directory) / "suspend").exists())
        self.assertIn("SLEEP", launcher.status)

    def test_sleep_asks_first_and_defaults_to_no(self):
        down = self.module.curses.KEY_DOWN
        self.assertEqual(self.power_request("suspend", [10])[0], [])  # a stray A answers NO
        self.assertEqual(self.power_request("suspend", [27])[0], [])
        self.assertEqual(self.power_request("suspend", [down, 10])[0], ["suspend"])

    def test_sleep_warns_when_nothing_connected_can_wake_the_pc(self):
        for sources, warned in ((["USB KEYBOARD"], False), ([], True)):
            self.wake_sources.return_value = sources
            with mock.patch.object(self.module.confirmation, "confirm", return_value=False) as ask:
                self.power_request("suspend", [])
            self.assertEqual("POWER BUTTON" in ask.call_args.args[1], warned, sources)

    def test_sleep_on_a_pc_that_cannot_sleep_explains_instead_of_asking(self):
        self.can_suspend.return_value = False
        with mock.patch.object(self.module.confirmation, "confirm") as ask:
            files, _text = self.power_request("suspend", [])
        ask.assert_not_called()
        self.assertEqual(files, [])

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
        chrome = self.module.browser.BROWSERS["chrome"]
        patcher = mock.patch.object(self.module.browser, "installed_browsers", return_value=[chrome])
        patcher.start()
        self.addCleanup(patcher.stop)
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

    def add_web_app(self, installed):
        """ADD WEB APPLICATION with these browsers installed; returns (written app or None, settings)."""
        found = [self.module.browser.BROWSERS[name] for name in installed]
        screen = self.form_screen(["TV", "\n", "https://tv.example/", "\n"])
        settings = self.module.ApplicationsSettings(screen, self.launcher())
        settings._write_user = mock.Mock()
        settings.launcher.browser_setup = mock.Mock(return_value=None)
        with mock.patch.object(self.module.browser, "installed_browsers", return_value=found), \
                mock.patch.object(self.module.curses, "curs_set"):
            settings.add_web()
        written = settings._write_user.call_args.args[0] if settings._write_user.called else None
        return written, settings

    def test_a_web_app_opens_in_chrome_when_it_is_installed(self):
        app, _settings = self.add_web_app(["chrome", "firefox"])
        self.assertEqual(app.command, "/usr/bin/google-chrome-stable")
        self.assertEqual(app.arguments, "--ozone-platform=wayland --kiosk --no-first-run https://tv.example/")

    def test_a_web_app_opens_in_firefox_when_only_firefox_is_installed(self):
        app, _settings = self.add_web_app(["firefox"])
        self.assertEqual((app.command, app.arguments), ("/usr/bin/firefox-esr", "--kiosk https://tv.example/"))

    def test_a_web_app_without_a_browser_opens_add_a_web_browser_first(self):
        app, settings = self.add_web_app([])
        self.assertIsNone(app)
        settings.launcher.browser_setup.assert_called_once_with()
        self.assertEqual(settings.status, "ADD A WEB BROWSER FIRST")
        self.assertEqual(len(settings.screen.keys), 4, "no name or address was asked for")

    def test_browser_tiles_are_hidden_until_the_browser_is_installed(self):
        have = set()
        with mock.patch.object(self.module.apps, "installed", side_effect=lambda app: app.binary in ("", *have)):
            labels = [label for label, _action in self.launcher().menu]
            self.assertNotIn("FIREFOX", labels)
            self.assertNotIn("GOOGLE CHROME", labels)
            self.assertIn("TERMINAL", labels)
            have.add("/usr/bin/firefox-esr")
            labels = [label for label, _action in self.launcher().menu]
        self.assertIn("FIREFOX", labels)
        self.assertNotIn("GOOGLE CHROME", labels)

    def test_applications_settings_offers_add_a_web_browser(self):
        down, enter = self.module.curses.KEY_DOWN, 10
        with mock.patch.object(self.module, "application_result",
                               return_value=self.module.apps.LoadResult((), ())):
            launcher = self.launcher()
            launcher.browser_setup = mock.Mock(return_value=None)
            screen = self.form_screen([down, down, enter, 27])
            self.module.ApplicationsSettings(screen, launcher).run()
        self.assertIn("ADD A WEB BROWSER", screen.frames[0])
        launcher.browser_setup.assert_called_once_with()

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

    def test_on_resume_gives_the_tv_and_pad_time_before_auto_streaming(self):
        launcher = self.launcher()
        launcher.autostream = mock.Mock(return_value=False)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module, "focus_launcher"):
            launcher.on_resume()
        launcher.autostream.assert_called_once_with(countdown=15)

    def test_on_resume_puts_the_cursor_on_the_first_menu_item_not_on_sleep(self):
        launcher = self.launcher()
        launcher.selected = [action for _label, action in launcher.menu].index("suspend")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module, "HOME_REQUEST", pathlib.Path(directory) / "home.request"), mock.patch.object(
            self.module, "focus_launcher"
        ):
            launcher.on_resume()
        self.assertEqual(launcher.selected, 0)

    def test_the_first_a_press_after_waking_does_not_choose_sleep_again(self):
        launcher = self.launcher()
        launcher.selected = [action for _label, action in launcher.menu].index("suspend")
        screen = Screen([-1, 10, 10])  # a timeout tick, the A that "wakes" the box, then a real A
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module, "HOME_REQUEST", pathlib.Path(directory) / "home.request"), mock.patch.object(
            self.module, "focus_launcher"
        ), mock.patch.object(self.module, "IDLE_GUARD", launcher.idle):
            (pathlib.Path(directory) / "resumed").touch()
            keys = [self.module.read_key(screen) for _ in range(3)]
        self.assertEqual(keys, [-1, -1, 10])
        self.assertEqual(launcher.selected, 0)

    def test_a_wake_up_seen_inside_active_applications_returns_to_the_main_menu(self):
        # Holding Guide to sleep opens ACTIVE APPLICATIONS first, so the wake-up arrives inside it.
        launcher = self.launcher()
        launcher.running_applications = mock.Mock(return_value=[])
        launcher.autostream = mock.Mock(return_value=False)
        calls = []

        class WakeScreen(Screen):
            def getch(self):
                calls.append(1)
                if len(calls) > 1:
                    raise AssertionError("ACTIVE APPLICATIONS stayed open after the wake-up")
                launcher.on_resume()
                return -1

        launcher.screen = WakeScreen()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module, "HOME_REQUEST", pathlib.Path(directory) / "home.request"), mock.patch.object(
            self.module, "focus_launcher"
        ):
            launcher.active_applications()
            self.assertEqual(len(calls), 1)
            launcher.screen = Screen([-1, 27])  # the next visit is not cut short by the old wake-up
            launcher.active_applications()
            self.assertEqual(launcher.screen.keys, [])

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
        with mock.patch.object(self.module, "RUN", pathlib.Path("/nonexistent-couchliteos-run")):
            launcher.request_sleep()
        self.assertIn("COULD NOT REQUEST SLEEP", launcher.status)

    def test_read_key_goes_through_the_idle_guard(self):
        guard = mock.Mock()
        guard.filter.return_value = -1
        with mock.patch.object(self.module, "IDLE_GUARD", guard):
            self.assertEqual(self.module.read_key(Screen([10])), -1)
        guard.filter.assert_called_once()
        self.assertEqual(guard.filter.call_args.args[1], 10)

    def test_ctrl_alt_h_is_home_not_back(self):
        # foot sends Ctrl+Alt+H as ESC, Ctrl+H; gamepad-nav has left a Home request for it.
        with tempfile.TemporaryDirectory() as directory:
            request = pathlib.Path(directory) / "home.request"
            request.touch()
            screen = Screen([27, 8, 8])
            with mock.patch.object(self.module, "HOME_REQUEST", request), \
                    mock.patch.object(self.module, "_home_chord_until", 0.0), \
                    mock.patch.object(self.module.time, "sleep") as sleep:
                self.assertEqual(self.module.read_key(screen), -1)
                self.assertEqual(self.module.read_key(screen), -1)
                self.assertEqual(self.module.read_key(screen), 8, "only the chord's own Ctrl+H is dropped")
            sleep.assert_not_called()
            self.assertTrue(request.exists(), "the caller's Home check still sees it")

    def use_home_key(self, chord):
        """Make the launcher's Home key settings read `chord`."""
        watcher = mock.Mock(current=lambda: self.module.inputprefs.Settings(keyboard_home=chord))
        return mock.patch.object(self.module, "HOME_SETTINGS", watcher)

    def test_home_chord_bytes_follow_the_chosen_key(self):
        for chord, expected in (
            ("KEY_LEFTCTRL+KEY_LEFTALT+KEY_H", (True, {8})),
            ("KEY_LEFTCTRL+KEY_LEFTALT+KEY_K", (True, {11})),
            ("KEY_LEFTALT+KEY_G", (True, {ord("g"), ord("G")})),
            ("KEY_LEFTMETA+KEY_H", (False, {ord("h"), ord("H")})),
            ("KEY_LEFTCTRL+KEY_LEFTSHIFT+KEY_H", (False, {8})),
            ("KEY_LEFTALT+KEY_F3", (True, set())),
            ("", (False, set())),
        ):
            with self.subTest(chord=chord), self.use_home_key(chord):
                has_esc, tail = self.module.home_chord_bytes()
                self.assertEqual((has_esc, set(tail)), expected)

    def test_super_h_as_the_home_key_drops_only_the_letter_that_follows_a_request(self):
        # foot sends Super+H as a plain h; gamepad-nav has left a Home request for it.
        with tempfile.TemporaryDirectory() as directory, self.use_home_key("KEY_LEFTMETA+KEY_H"):
            request = pathlib.Path(directory) / "home.request"
            request.touch()
            screen = Screen([ord("h"), 27])
            with mock.patch.object(self.module, "HOME_REQUEST", request), \
                    mock.patch.object(self.module, "_home_chord_until", 0.0), \
                    mock.patch.object(self.module.time, "sleep"):
                self.assertEqual(self.module.read_key(screen), -1)
                self.assertEqual(self.module.read_key(screen), 27, "its Esc is not part of it: still back")
            self.assertTrue(request.exists())

    def test_a_typed_h_without_a_home_request_is_a_letter(self):
        with tempfile.TemporaryDirectory() as directory, self.use_home_key("KEY_LEFTMETA+KEY_H"):
            screen = Screen([ord("h")])
            with mock.patch.object(self.module, "HOME_REQUEST", pathlib.Path(directory) / "home.request"), \
                    mock.patch.object(self.module, "_home_chord_until", 0.0), \
                    mock.patch.object(self.module.time, "sleep") as sleep:
                self.assertEqual(self.module.read_key(screen), ord("h"))
            waited = sum(call.args[0] for call in sleep.call_args_list)
            self.assertAlmostEqual(waited, self.module.HOME_CHORD_WAIT)

    def test_with_the_home_key_off_nothing_is_dropped(self):
        with tempfile.TemporaryDirectory() as directory, self.use_home_key(""):
            request = pathlib.Path(directory) / "home.request"
            request.touch()
            screen = Screen([27, 8])
            with mock.patch.object(self.module, "HOME_REQUEST", request), \
                    mock.patch.object(self.module, "_home_chord_until", 0.0), \
                    mock.patch.object(self.module.time, "sleep") as sleep:
                self.assertEqual(self.module.read_key(screen), 27)
                self.assertEqual(self.module.read_key(screen), 8)
            sleep.assert_not_called()

    def test_a_plain_escape_still_goes_back(self):
        with tempfile.TemporaryDirectory() as directory:
            screen = Screen([27, 8])
            with mock.patch.object(self.module, "HOME_REQUEST", pathlib.Path(directory) / "home.request"), \
                    mock.patch.object(self.module, "_home_chord_until", 0.0), \
                    mock.patch.object(self.module.time, "sleep") as sleep:
                self.assertEqual(self.module.read_key(screen), 27)
                self.assertEqual(self.module.read_key(screen), 8)
            waited = sum(call.args[0] for call in sleep.call_args_list)
            self.assertAlmostEqual(waited, self.module.HOME_CHORD_WAIT)
            self.assertLessEqual(self.module.HOME_CHORD_WAIT, 0.1, "Esc must not feel slow")

    def test_a_late_home_request_still_counts_in_setup(self):
        # Seen in a VM: gamepad-nav wrote the request about 0.1 s after foot's ESC, Ctrl+H.
        with tempfile.TemporaryDirectory() as directory:
            request = pathlib.Path(directory) / "home.request"
            waited = []

            def sleep(seconds):
                waited.append(seconds)
                if sum(waited) >= 0.1:
                    request.touch()

            screen = Screen([27, 8, 10])
            with mock.patch.object(self.module, "HOME_REQUEST", request), \
                    mock.patch.object(self.module, "_home_chord_until", 0.0), \
                    mock.patch.object(self.module.time, "sleep", side_effect=sleep):
                choice = self.module.setup_ui(screen).menu("STEP", [], ["FIRST", "SECOND"])
            self.assertEqual(choice, 0, "the chord did not skip the step")
            self.assertLess(sum(waited), self.module.HOME_CHORD_SETUP_WAIT, "stopped once the request appeared")

    def test_ctrl_alt_h_does_not_skip_a_setup_step(self):
        # The setup wizard reads its own keys; the chord's ESC must not count as B (skip).
        with tempfile.TemporaryDirectory() as directory:
            request = pathlib.Path(directory) / "home.request"
            request.touch()
            screen = Screen([27, 8, 10])
            with mock.patch.object(self.module, "HOME_REQUEST", request), \
                    mock.patch.object(self.module, "_home_chord_until", 0.0), \
                    mock.patch.object(self.module.time, "sleep"):
                choice = self.module.setup_ui(screen).menu("STEP", [], ["FIRST", "SECOND"])
            self.assertEqual(choice, 0, "Enter picked the first choice; nothing was skipped")

    def test_b_still_skips_a_setup_step(self):
        with tempfile.TemporaryDirectory() as directory:
            screen = Screen([27])
            with mock.patch.object(self.module, "HOME_REQUEST", pathlib.Path(directory) / "home.request"), \
                    mock.patch.object(self.module, "_home_chord_until", 0.0), \
                    mock.patch.object(self.module.time, "sleep") as sleep:
                self.assertIsNone(self.module.setup_ui(screen).menu("STEP", [], ["FIRST"]))
            waited = sum(call.args[0] for call in sleep.call_args_list)
            self.assertAlmostEqual(waited, self.module.HOME_CHORD_SETUP_WAIT)
            self.assertLessEqual(self.module.HOME_CHORD_SETUP_WAIT, 0.5, "B in setup must not feel slow")

    def test_guide_pressed_in_settings_does_not_take_b_away(self):
        # Only the main menu answers a Guide press made in Settings; until then B / Esc still go back.
        with tempfile.TemporaryDirectory() as directory:
            request = pathlib.Path(directory) / "home.request"
            request.touch()
            old = time.time() - 5
            os.utime(request, (old, old))
            screen = Screen([27, 27])
            with mock.patch.object(self.module, "HOME_REQUEST", request), \
                    mock.patch.object(self.module, "_home_chord_until", 0.0), \
                    mock.patch.object(self.module.time, "sleep"):
                self.assertEqual(self.module.read_key(screen), 27)
                self.assertEqual(self.module.read_key(screen), 27)
            self.assertTrue(request.exists(), "the main menu still opens the Guide menu for it")

    def test_the_keyboard_typing_into_an_app_closes_a_guide_menu_opened_meanwhile(self):
        # Home while the on-screen keyboard was up opened the Guide menu; closing the keyboard typed
        # into the app and put it back in front, so the menu closes and the app gets the controller.
        launcher = self.launcher()
        launcher.running_applications = mock.Mock(return_value=[])
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            refocused = run / "osk-refocused"
            refocused.touch()  # left over from an earlier keyboard: ignored
            calls = []

            class KeyboardClosesScreen(Screen):
                def getch(self):
                    calls.append(1)
                    if len(calls) == 2:
                        refocused.touch()
                    if len(calls) > 2:
                        raise AssertionError("the Guide menu stayed open")
                    return -1

            launcher.screen = KeyboardClosesScreen()
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "HOME_REQUEST", run / "home.request"
            ), mock.patch.object(self.module, "LAUNCHER_FOCUS", run / "launcher-focus"), mock.patch.object(
                self.module, "OSK_REFOCUSED", refocused
            ), mock.patch.object(self.module, "focus_launcher"):
                launcher.active_applications()
            self.assertEqual(len(calls), 2)
            self.assertFalse(refocused.exists())
            self.assertFalse((run / "launcher-focus").exists(), "gamepad-nav stops sending keys to the launcher")

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
        settings.shown = []  # the status line each redraw showed
        settings.draw = mock.Mock(side_effect=lambda *_args: settings.shown.append(settings.status))
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

    def test_idle_sleep_is_off_at_runtime_when_nothing_can_wake_the_pc_but_the_setting_is_kept(self):
        power = self.module.power
        self.wake_sources.return_value = []
        with mock.patch.object(power, "load_settings", return_value=power.Settings(10, 60)), mock.patch.object(
            power, "save_settings"
        ) as save:
            launcher = self.launcher()
        self.assertEqual(launcher.idle.timer.settings, power.Settings(10, 0))  # blanking still works
        save.assert_not_called()

    def test_resume_checks_what_can_wake_the_pc_again_and_idle_sleep_follows(self):
        power = self.module.power
        self.wake_sources.return_value = []
        with mock.patch.object(power, "load_settings", return_value=power.Settings(5, 60)), tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module, "HOME_REQUEST", pathlib.Path(directory) / "home.request"), mock.patch.object(
            self.module, "focus_launcher"
        ):
            launcher = self.launcher()
            self.assertEqual(launcher.idle.timer.settings, power.Settings(5, 0))
            self.wake_sources.return_value = ["BLUETOOTH ADAPTER"]  # plugged in while it slept
            launcher.on_resume()
            self.assertEqual(launcher.idle.timer.settings, power.Settings(5, 60))

    def test_sleep_and_screen_says_why_idle_sleep_is_off_when_nothing_can_wake_the_pc(self):
        settings, _rows, _save = self.sleep_settings_rows(saved=self.module.power.Settings(5, 30))
        reason = "IDLE SLEEP OFF: NO CONTROLLER CAN WAKE THIS PC"
        self.assertEqual(settings.shown[0], reason)
        self.assertEqual(self.module.IDLE_SLEEP_NO_WAKE, reason)
        self.assertLessEqual(len(reason), 76)  # the status line is 76 columns on an 80 column screen

    def test_sleep_and_screen_shows_no_such_reason_when_something_can_wake_the_pc_or_sleep_is_off(self):
        power = self.module.power
        settings, _rows, _save = self.sleep_settings_rows(saved=power.Settings(5, 30), wake=["USB KEYBOARD"])
        self.assertEqual(settings.shown[0], "")
        settings, _rows, _save = self.sleep_settings_rows(saved=power.Settings(5, 0))
        self.assertEqual(settings.shown[0], "")

    def test_the_idle_sleep_reason_stays_up_after_a_change_and_the_saved_sleep_is_not_overwritten(self):
        power = self.module.power
        settings, _rows, save = self.sleep_settings_rows([10, 27], saved=power.Settings(5, 60))
        self.assertEqual([call.args[0] for call in save.call_args_list], [power.Settings(15, 60)])
        self.assertEqual(settings.launcher.idle.apply.call_args.args[0], power.Settings(15, 0))
        self.assertEqual(settings.shown[-1], self.module.IDLE_SLEEP_NO_WAKE)

    def test_choosing_a_sleep_time_with_nothing_to_wake_the_pc_saves_it_but_leaves_it_inactive(self):
        keys = self.module.curses
        power = self.module.power
        settings, _rows, save = self.sleep_settings_rows([keys.KEY_DOWN, 10, 27], saved=power.Settings(5, 60))
        self.assertEqual([call.args[0] for call in save.call_args_list], [power.Settings(5, 15)])
        self.assertEqual(settings.launcher.idle.apply.call_args.args[0], power.Settings(5, 0))

    def test_opening_sleep_and_screen_picks_up_a_wake_source_plugged_in_since_the_launcher_started(self):
        power = self.module.power
        self.wake_sources.return_value = []
        with mock.patch.object(power, "load_settings", return_value=power.Settings(5, 60)):
            launcher = self.launcher()
        self.assertFalse(launcher.can_wake)
        launcher.idle = mock.Mock()
        settings = self.module.Settings(Screen([10, 27]), launcher)
        settings.draw = mock.Mock()
        settings.choose = mock.Mock(return_value=15)
        with mock.patch.object(power, "load_settings", return_value=power.Settings(5, 60)), mock.patch.object(
            power, "save_settings"
        ), mock.patch.object(power, "wake_on_lan", return_value=("none", None)), mock.patch.object(
            power, "wake_sources", return_value=["USB KEYBOARD"]
        ):
            settings.run_sleep_settings()
        self.assertTrue(launcher.can_wake)
        self.assertEqual(launcher.idle.apply.call_args.args[0], power.Settings(15, 60))

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

    # --- setup wizard glue: the wizard logic lives in couchliteos_setup -----

    def run_wizard_glue(self, launcher, **arguments):
        captured = {}

        class FakeWizard:
            def __init__(self, ui, actions, system, **options):
                captured.update(ui=ui, actions=actions, system=system, options=options)

            def run(self, force=False, resume=False):
                captured["force"], captured["resume"] = force, resume
                captured["home_request_seen"] = home_request.exists()

        launcher.reload_applications = mock.Mock()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home_request = pathlib.Path(directory.name) / "home.request"
        home_request.touch()  # Ctrl+Alt+H pressed during setup, which takes no Home request
        with mock.patch.object(self.module.setup, "SetupWizard", FakeWizard), mock.patch.object(
            self.module.bluetooth, "BluetoothClient"
        ), mock.patch.object(self.module, "HOME_REQUEST", home_request):
            launcher.setup_wizard(**arguments)
        captured["reloaded"] = launcher.reload_applications.called
        captured["home_request_left"] = home_request.exists()
        return captured

    def test_wizard_glue_hands_the_wizard_exactly_the_actions_it_calls(self):
        launcher = self.launcher()
        captured = self.run_wizard_glue(launcher, force=True)
        self.assertEqual(
            set(captured["actions"]),
            {"text", "display", "picture_saved", "tone", "launch", "pair_moonlight", "wake_pc", "browser", "applications"},
        )
        self.assertTrue(captured["force"])
        self.assertFalse(captured["resume"])
        self.assertTrue(captured["reloaded"])
        self.assertIn("tailscale", captured["options"]["statuses"])

    def test_a_home_request_left_from_setup_does_not_open_active_applications_after_it(self):
        captured = self.run_wizard_glue(self.launcher())
        self.assertTrue(captured["home_request_seen"])
        self.assertFalse(captured["home_request_left"])

    def test_the_wizard_glue_passes_resume_through(self):
        self.assertTrue(self.run_wizard_glue(self.launcher(), resume=True)["resume"])

    def test_wizard_glue_offers_tv_control_only_when_the_screen_exists(self):
        launcher = self.launcher()
        self.assertNotIn("tv", self.run_wizard_glue(launcher)["actions"])
        launcher.tv_control_screen = mock.Mock()
        actions = self.run_wizard_glue(launcher)["actions"]
        actions["tv"]()
        launcher.tv_control_screen.assert_called_once_with()

    def test_start_up_gives_the_wizard_the_settings_tv_control_screen(self):
        launcher = self.launcher()
        self.module.connect_tv_control(launcher)
        actions = self.run_wizard_glue(launcher)["actions"]
        with mock.patch.object(self.module.Settings, "run_tv_control") as tv_screen:
            actions["tv"]()
        tv_screen.assert_called_once_with()

    def test_main_connects_tv_control_before_the_launcher_runs(self):
        screen = Screen()
        with mock.patch.object(self.module.curses, "set_escdelay"), mock.patch.object(
            self.module, "Launcher"
        ) as launcher, mock.patch.object(self.module, "connect_tv_control") as connect, mock.patch.object(
            self.module.errors, "_EXTRA", []
        ):
            launcher.return_value.run.side_effect = lambda: connect.assert_called_once_with(launcher.return_value)
            self.module.main(screen)
        launcher.return_value.run.assert_called_once_with()

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

    def test_wizard_display_notes_a_new_picture_size_only_when_confirmed(self):
        mode = self.module.display.Mode
        launcher = self.launcher()
        for after, asked, changed in (
            (mode(1280, 720, 60000), ("1280x720", 60000), True),
            (mode(1920, 1080, 60000), ("1280x720", 60000), False),  # rolled back
            (mode(1920, 1080, 50000), ("1920x1080", 50000), False),  # same size, new refresh rate
        ):
            launcher.wizard_picture_changed = False
            settings = self.display_settings(after)
            with mock.patch.object(self.module, "Settings", return_value=settings):
                launcher.wizard_display(*asked)
            self.assertIs(settings.in_wizard, True, "Settings must leave the restart to setup")
            self.assertEqual(launcher.wizard_picture_changed, changed, asked)

    def picture_saved(self, launcher, run_dir):
        with mock.patch.object(self.module, "RUN", run_dir), mock.patch.object(
            self.module.time, "sleep"
        ), mock.patch.object(self.module.setup, "CursesUI") as ui:
            launcher.wizard_picture_saved()
        return ui

    def test_a_new_picture_size_in_setup_restarts_into_setup(self):
        launcher = self.launcher()
        run_dir = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        launcher.wizard_picture_changed = True
        launcher.any_app_running = mock.Mock(return_value=False)
        with self.assertRaises(SystemExit) as stop:
            ui = self.picture_saved(launcher, run_dir)
        self.assertEqual(stop.exception.code, 0)
        self.assertTrue((run_dir / "reopen-setup").exists())
        self.assertEqual(len((run_dir / "screen-restarts").read_text().split()), 1)

    def test_no_restart_in_setup_without_a_new_size_with_an_app_open_or_past_the_limit(self):
        launcher = self.launcher()
        run_dir = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        launcher.any_app_running = mock.Mock(return_value=False)
        launcher.wizard_picture_changed = False
        self.picture_saved(launcher, run_dir)  # must not exit
        launcher.wizard_picture_changed = True
        launcher.any_app_running.return_value = True
        self.picture_saved(launcher, run_dir)
        now = self.module.time.time()
        (run_dir / "screen-restarts").write_text(f"{now - 30}\n{now - 20}\n{now - 10}\n")
        launcher.wizard_picture_changed, launcher.any_app_running.return_value = True, False
        self.picture_saved(launcher, run_dir)
        self.assertFalse((run_dir / "reopen-setup").exists())

    def test_the_restarted_launcher_resumes_setup(self):
        launcher = self.launcher()
        run_dir = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        (run_dir / "reopen-setup").touch()
        launcher.setup_wizard = mock.Mock(side_effect=RuntimeError("stop"))
        with mock.patch.object(self.module, "RUN", run_dir), mock.patch.object(
            self.module.display, "restore_saved_mode"
        ), mock.patch.object(launcher.controllers, "start"), mock.patch.object(
            launcher.pcstatus, "start"
        ), mock.patch.object(launcher.updates, "start"), mock.patch.object(
            self.module.curses, "curs_set"
        ), mock.patch.object(self.module.curses, "use_default_colors"), self.assertRaisesRegex(RuntimeError, "stop"):
            launcher.run()
        launcher.setup_wizard.assert_called_once_with(resume=True)
        self.assertFalse((run_dir / "reopen-setup").exists())

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
        launcher.launch_app.assert_called_once_with(app, wake=False)

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
        self.assertEqual(app.command, "/opt/couchliteos/apps/moonlight/usr/bin/moonlight")
        self.assertEqual(app.arguments, "pair 192.168.1.20 --pin 0427")
        self.assertEqual(app.environment["QT_QPA_PLATFORM"], "xcb")
        self.assertEqual(app.environment["APPDIR"], "/opt/couchliteos/apps/moonlight")
        self.assertEqual(app.environment["LD_LIBRARY_PATH"], "/opt/couchliteos/apps/moonlight/usr/lib")
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

    def draw_home(self, size, selected, *, live_warning="", update="", battery=""):
        """Draw the home screen on a fake terminal; return every (row, text) written, in order."""
        class Recording(Screen):
            def __init__(self):
                super().__init__()
                self.writes = []

            def getmaxyx(self):
                return size

            def addstr(self, row, _column, text, *_args):
                self.writes.append((row, text.strip()))

            addnstr = addstr

        launcher = self.launcher()
        launcher.screen = Recording()
        launcher.status = "STATUS LINE"
        launcher.selected = selected
        launcher.live_warning = live_warning
        launcher.updates = mock.Mock(notice=mock.Mock(return_value=update))
        launcher.controllers = mock.Mock(
            line=mock.Mock(return_value=battery), low=mock.Mock(return_value=False)
        )
        launcher.draw()
        return launcher, launcher.screen.writes

    def test_footer_lines_never_share_a_row_with_the_menu_or_the_status_line(self):
        # 80x24 with a live-mode warning, an update notice and a battery line: three footer rows.
        footer = {"live_warning": "LIVE SESSION WARNING", "update": "UPDATE AVAILABLE",
                  "battery": "CONTROLLERS: PAD 80%"}
        for selected in range(len(self.launcher().menu)):
            with self.subTest(selected=selected):
                launcher, writes = self.draw_home((24, 80), selected, **footer)
                rows: dict[int, list[str]] = {}
                for row, text in writes:
                    rows.setdefault(row, []).append(text)
                self.assertEqual({row: texts for row, texts in rows.items() if len(texts) > 1}, {})
                shown = [text for _row, text in writes]
                for text in ("STATUS LINE", *footer.values()):
                    self.assertIn(text, shown)
                label = launcher.menu[selected][0]
                # The row the cursor is on stays visible (SETTINGS may carry the update suffix).
                self.assertTrue(any(text.startswith(f">  {label}") for text in shown), label)
                self.assertTrue(all(row < 23 for row in rows), "drew on the border row")

    def test_progress_helpers(self):
        self.assertEqual(len(self.module.indeterminate_progress_bar(24, 0)), 24)
        self.assertEqual(self.module.format_elapsed(65.9), "01:05")


if __name__ == "__main__":
    unittest.main()
