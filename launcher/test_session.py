import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import couchliteos_apps as apps
import couchliteos_pointer as pointer
import couchliteos_session as session
import couchliteos_stream as stream


def app(app_id, kind="request", **fields):
    fields.setdefault("status_id", app_id)
    if kind == "request":
        fields.setdefault("request", f"start-{app_id}")
    return apps.Application(id=app_id, name=app_id.upper(), kind=kind, **fields)


MOONLIGHT = app("moonlight")
TERMINAL = app("terminal", kind="command", command="/bin/bash", terminal=True)
TOOL = app("tool", kind="command", command="/usr/bin/tool")


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeSession(session.Session):
    """The screen parts recorded instead of drawn; launch_wait moves a fake clock on."""

    def __init__(self, run, applications=(MOONLIGHT, TERMINAL, TOOL), clock=None, on_wait=None):
        self.run = run
        self.result = apps.LoadResult(tuple(applications), ())
        self.clock = clock
        self.on_wait = on_wait
        self.frames, self.failures, self.waits = [], [], 0
        self.failure_answer = "dismiss"
        self.modes = pointer.Modes(run / "pointer-mode")

    @property
    def run_dir(self):
        return self.run

    @property
    def pointer_modes(self):
        return self.modes

    def application_result(self):
        return self.result

    def draw_launching(self, label, frame):
        self.frames.append((label, frame))

    def launch_wait(self):
        self.waits += 1
        if self.clock is not None:
            self.clock.now += 0.1
        if self.on_wait is not None:
            self.on_wait(self)

    def show_launch_failure(self, label, message, app=None, *, retry=True):
        self.failures.append((label, message))
        return self.failure_answer


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run = pathlib.Path(directory.name)
        self.clock = Clock()
        patcher = mock.patch.object(session.time, "monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, **kwargs):
        return FakeSession(self.run, clock=self.clock, **kwargs)


class LaunchTest(SessionTestCase):
    def test_a_request_app_touches_its_request_and_waits_for_ready(self):
        def ready(s):
            self.assertTrue((self.run / "start-moonlight").exists())
            (self.run / "moonlight-ready").touch()

        s = self.session(on_wait=ready)
        self.assertTrue(s.launch_app(MOONLIGHT))
        self.assertEqual(s.status, "MOONLIGHT STARTED")
        self.assertEqual(s.frames[0], ("MOONLIGHT", "|"))

    def test_a_command_app_is_asked_for_with_launch_app_request(self):
        def ready(s):
            self.assertEqual((self.run / "launch-app.request").read_text(), "terminal\n")
            (self.run / "terminal-ready").touch()

        self.assertTrue(self.session(on_wait=ready).launch_app(TERMINAL))

    def test_starting_hands_the_controller_to_the_app(self):
        (self.run / "launcher-focus").touch()
        s = self.session(on_wait=lambda s: (self.run / "terminal-ready").touch())
        self.assertTrue(s.launch_app(TERMINAL))
        self.assertFalse((self.run / "launcher-focus").exists())
        self.assertEqual(s.modes.front, "terminal")

    def test_an_app_that_quits_before_ready_is_not_an_error(self):
        s = self.session(on_wait=lambda s: (self.run / "terminal-status").write_text("exited: 0\n"))
        self.assertTrue(s.launch_app(TERMINAL))
        self.assertEqual((s.status, s.failures), ("TERMINAL EXITED", []))

    def test_a_lasting_failure_is_shown_once_and_the_start_fails(self):
        s = self.session(on_wait=lambda s: (self.run / "terminal-status").write_text("failed: no such file\n"))
        self.assertFalse(s.launch_app(TERMINAL))
        self.assertEqual(s.failures, [("TERMINAL", "no such file")])
        self.assertEqual(s.status, "TERMINAL FAILED TO START")
        self.assertGreaterEqual(s.waits * 0.1, session.FAILED_FOR - 0.1)

    def test_try_again_starts_it_again(self):
        attempts = []

        def wait(s):
            if len(attempts) == 0:
                (self.run / "terminal-status").write_text("failed: boom\n")
            else:
                (self.run / "terminal-ready").touch()

        s = self.session(on_wait=wait)
        original = s.show_launch_failure

        def answer(*args, **kwargs):
            attempts.append(1)
            original(*args, **kwargs)
            return "retry"

        s.show_launch_failure = answer
        self.assertTrue(s.launch_app(TERMINAL))
        self.assertEqual(len(attempts), 1)

    def test_a_timeout_takes_the_request_back(self):
        s = self.session()
        self.assertFalse(s.launch_app(MOONLIGHT))
        self.assertFalse((self.run / "start-moonlight").exists())
        self.assertEqual(s.status, "MOONLIGHT START TIMED OUT")
        self.assertIn("DID NOT BECOME READY", s.failures[0][1])
        self.assertFalse(self.session().launch_app(TERMINAL, quiet=True))
        self.assertFalse((self.run / "launch-app.request").exists())

    def test_a_running_app_is_brought_to_the_front_instead(self):
        (self.run / "moonlight-ready").touch()
        s = self.session()
        s.focus_app = mock.Mock(return_value=True)
        self.assertTrue(s.launch_app(MOONLIGHT))
        self.assertEqual(s.status, "RESUMED MOONLIGHT")
        s.focus_app.return_value = False
        self.assertFalse(s.launch_app(MOONLIGHT))
        self.assertIn("HAS NO WINDOW", s.status)

    def test_one_command_app_at_a_time(self):
        (self.run / "terminal-ready").touch()
        s = self.session()
        self.assertFalse(s.launch_app(TOOL))
        self.assertIn("TERMINAL IS STILL RUNNING", s.failures[0][1])
        self.assertFalse((self.run / "launch-app.request").exists())

    def test_hooks_can_stop_a_start(self):
        s = self.session()
        s.wake_before_moonlight = mock.Mock(return_value=False)
        self.assertFalse(s.launch_app(MOONLIGHT))
        self.assertFalse((self.run / "start-moonlight").exists())
        self.assertTrue(s.launch_app(MOONLIGHT, wake=False) is False)  # no ready: timed out, but it was asked
        s.wake_before_moonlight.assert_called_once()
        s.prepare_remote_desktop = mock.Mock(return_value=False)
        self.assertFalse(s.launch_app(app("work-pc", kind="rdp", connection="work-pc")))

    def test_launch_by_id(self):
        s = self.session(on_wait=lambda s: (self.run / "moonlight-ready").touch())
        self.assertTrue(s.launch_by_id("moonlight"))
        self.assertFalse(s.launch_by_id("missing"))
        self.assertEqual(s.status, "MISSING IS NOT AVAILABLE: CHECK SETTINGS > APPLICATIONS")

    def test_moonlight_by_itself_opens_its_list_of_pcs(self):
        seen = []

        def ready(s):
            seen.append((self.run / session.MOONLIGHT_BROWSE).exists())
            (self.run / "moonlight-ready").touch()

        self.assertTrue(self.session(on_wait=ready).launch_app(MOONLIGHT))
        self.assertEqual(seen[0], True)  # couchliteos-run-app takes it

    def test_moonlight_that_never_starts_leaves_no_browse_request(self):
        self.assertFalse(self.session().launch_app(MOONLIGHT))
        self.assertFalse((self.run / session.MOONLIGHT_BROWSE).exists())


class StreamTest(SessionTestCase):
    HOST = stream.Host(name="Gaming-PC", uuid="UUID-1", local="192.168.1.50", apps=("Desktop",))

    def test_a_stream_writes_its_request_records_the_game_and_cleans_up(self):
        seen = []

        def ready(s):
            seen.append((self.run / stream.STREAM_REQUEST.name).read_text())
            (self.run / "moonlight-ready").touch()

        (self.run / session.MOONLIGHT_BROWSE).touch()  # left over from an earlier start
        s = self.session(on_wait=ready)
        with mock.patch.object(session.recent, "record") as record:
            self.assertTrue(s.start_stream(self.HOST, "Desktop", by_hand=True))
        self.assertEqual(seen[0], "192.168.1.50\nDesktop\n")
        self.assertFalse((self.run / session.MOONLIGHT_BROWSE).exists())  # a stream, not the list of PCs
        record.assert_called_once_with("UUID-1", "Desktop")
        self.assertFalse((self.run / stream.STREAM_REQUEST.name).exists())
        self.assertIsNone(s.pending_stream)

    def test_no_moonlight_no_stream(self):
        s = self.session(applications=(TERMINAL,))
        with mock.patch.object(session.recent, "record") as record:
            self.assertFalse(s.start_stream(self.HOST, "Desktop"))
        record.assert_not_called()
        self.assertEqual(s.status, "MOONLIGHT IS UNAVAILABLE")

    def test_a_bad_app_name_is_reported_in_the_streams_words(self):
        s = self.session()
        self.assertFalse(s.start_stream(self.HOST, "bad\nname", by_hand=True))
        self.assertTrue(s.status.startswith("STREAM NOT STARTED"))
        self.assertFalse(s.start_stream(self.HOST, "bad\nname"))
        self.assertTrue(s.status.startswith("AUTO-STREAM NOT STARTED"))

    def test_autostream_target(self):
        s = self.session()
        settings = stream.StreamSettings(autostart=True, host="UUID-1", app="Desktop")
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(session, "SETUP_MARKER", pathlib.Path(directory) / "setup-complete"), \
                mock.patch.object(session.stream, "load_settings", return_value=settings), \
                mock.patch.object(session.stream, "load_hosts", return_value=[self.HOST]):
            self.assertIsNone(s.autostream_target())  # setup not complete
            (pathlib.Path(directory) / "setup-complete").touch()
            self.assertEqual(s.autostream_target(), (self.HOST, "Desktop"))
            (self.run / "app-active").touch()
            self.assertIsNone(s.autostream_target())  # something already runs
            (self.run / "app-active").unlink()
            with mock.patch.object(session.stream, "load_hosts", return_value=[]):
                self.assertIsNone(s.autostream_target())
            self.assertIn("NOT PAIRED", s.status)


class FocusTest(SessionTestCase):
    def wlrctl(self, focusable, listing=""):
        def run(command, **_kwargs):
            if command[2] == "list":
                return mock.Mock(returncode=0, stdout=listing)
            return mock.Mock(returncode=0 if command[-1] in focusable else 1, stdout="")
        return mock.patch.object(session.subprocess, "run", side_effect=run)

    def test_a_window_found_takes_the_controller_and_names_the_front_app(self):
        (self.run / "launcher-focus").touch()
        s = self.session()
        with self.wlrctl({"title:Moonlight"}):
            self.assertTrue(s.focus_app(MOONLIGHT))
        self.assertFalse((self.run / "launcher-focus").exists())
        self.assertEqual((self.run / "app-active").read_text(), "moonlight\n")
        self.assertEqual(s.modes.front, "moonlight")

    def test_a_stream_window_with_another_name_is_found_in_the_list(self):
        listing = "couchliteos-launcher: CouchLiteOS Launcher\nMoonlight: Moonlight Streaming\n"
        with self.wlrctl({"app_id:Moonlight"}, listing):
            self.assertTrue(self.session().focus_app(MOONLIGHT))
        with self.wlrctl(set(), listing), mock.patch.object(session.display, "log") as log:
            self.assertFalse(self.session().focus_app(MOONLIGHT))
        self.assertEqual(log.call_args.args[1], session.LOG)

    def test_user_apps_are_matched_by_their_binary(self):
        chrome = app("kiosk", kind="command", command="/usr/bin/google-chrome-stable")
        with self.wlrctl({"app_id:google-chrome"}):
            self.assertTrue(self.session().focus_app(chrome))

    def test_flathub_apps_are_matched_by_their_flatpak_id_not_by_flatpak(self):
        kodi = apps.flatpak_application("tv.kodi.Kodi", "Kodi", set())
        tried = []

        def run(command, **_kwargs):
            tried.append(command[-1])
            return mock.Mock(returncode=0 if command[-1] == "app_id:tv.kodi.Kodi" else 1, stdout="")

        with mock.patch.object(session.subprocess, "run", side_effect=run):
            self.assertTrue(self.session().focus_app(kodi))
        self.assertNotIn("app_id:flatpak", tried)
        with self.wlrctl({"app_id:kodi"}):  # a program that names its window itself
            self.assertTrue(self.session().focus_app(kodi))

    def test_no_wlrctl_is_no_window(self):
        with mock.patch.object(session.subprocess, "run", side_effect=FileNotFoundError("wlrctl")) as run:
            self.assertFalse(self.session().focus_app(MOONLIGHT))
        self.assertEqual(run.call_count, 1)

    def test_focus_launcher_names_the_window_title_both_front_ends_use(self):
        with mock.patch.object(session.subprocess, "run") as run:
            session.focus_launcher()
        self.assertEqual(run.call_args.args[0], ["wlrctl", "toplevel", "focus", "title:CouchLiteOS Launcher"])


class RunningTest(SessionTestCase):
    def test_running_applications_and_close(self):
        s = self.session()
        self.assertEqual(s.running_applications(), [])
        self.assertFalse(s.any_app_running())
        (self.run / "launcher-ready").touch()
        self.assertFalse(s.any_app_running())  # the home screen itself does not count
        (self.run / "terminal-ready").touch()
        self.assertEqual([item.id for item in s.running_applications()], ["terminal"])
        self.assertTrue(s.any_app_running())
        s.close_app(TERMINAL)
        self.assertTrue((self.run / "close-terminal").exists())

    def test_the_every_second_check_parses_the_manifests_only_after_a_change(self):
        apps._LOADED.clear()
        self.addCleanup(apps._LOADED.clear)
        with mock.patch.object(apps, "SETTLED", -1):
            first = session.application_result()
            with mock.patch.object(session.apps, "load_applications") as load:
                second = session.application_result()
                session.application_result()
        load.assert_not_called()
        self.assertEqual(first, second)

    def test_read_app_status_takes_the_first_line_and_ignores_a_huge_file(self):
        (self.run / "x-status").write_text("failed: one\ntwo\n")
        self.assertEqual(self.session().read_app_status("x"), "failed: one")
        (self.run / "x-status").write_text("y" * 600)
        self.assertEqual(self.session().read_app_status("x"), "")


class TickTest(SessionTestCase):
    def test_a_home_request_is_taken_once(self):
        s = self.session()
        self.assertFalse(s.take_home_request())
        (self.run / "home.request").touch()
        self.assertTrue(s.take_home_request())
        self.assertFalse(s.take_home_request())

    def test_a_resume_drops_a_stale_sleep_request_and_home_press(self):
        s = self.session()
        self.assertFalse(s.take_resumed())
        for name in ("resumed", "suspend", "home.request"):
            (self.run / name).touch()
        self.assertTrue(s.take_resumed())
        self.assertEqual(list(self.run.iterdir()), [])

    def test_an_update_is_offered_only_on_an_idle_home_screen(self):
        s = self.session()
        self.assertTrue(s.may_offer_update())
        self.assertFalse(s.may_offer_update(blanked=True))
        for name in ("home.request", "app-active", "moonlight-ready", "osk-active", "start-osk"):
            (self.run / name).touch()
            self.assertFalse(s.may_offer_update(), name)
            (self.run / name).unlink()

    def test_prepare_session_writes_the_display_for_the_app_units(self):
        (self.run / "launcher-focus").touch()
        s = self.session()
        with mock.patch.dict(os.environ, {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}):
            s.prepare_session()
        self.assertEqual((self.run / "session.env").read_text(), "DISPLAY=:0\nWAYLAND_DISPLAY=wayland-0\n")
        self.assertFalse((self.run / "launcher-focus").exists())
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "../x"}), self.assertRaises(RuntimeError):
            s.prepare_session()


class ClassicLauncherDelegatesTest(unittest.TestCase):
    """The curses launcher runs this module's code, not a copy of it."""

    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_for_session", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_the_launcher_is_a_session(self):
        launcher = self.module.Launcher
        self.assertTrue(issubclass(launcher, session.Session))
        for name in ("launch_app", "start_stream", "launch_by_id", "app_by_id", "running_applications",
                     "request", "close_app", "prepare_session", "may_offer_update"):
            self.assertIs(getattr(launcher, name), getattr(session.Session, name), name)
        self.assertIs(self.module.POINTER_MODES, session.POINTER_MODES)

    def test_the_static_helpers_use_the_launchers_run_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "moonlight-ready").touch()
            with mock.patch.object(self.module, "RUN", run):
                self.assertTrue(self.module.Launcher.any_app_running())
                (run / "x-status").write_text("exited: 0\n")
                self.assertEqual(self.module.Launcher.read_app_status("x"), "exited: 0")


if __name__ == "__main__":
    unittest.main()
