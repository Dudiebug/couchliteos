import testenv  # noqa: F401  (first: scratch run and state directories)
import curses
import importlib.util
import itertools
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_errors as errors

ENTER, ESC = 10, 27


def ids(failure):
    return [action.id for action in failure.actions]


class ActionMenuTest(unittest.TestCase):
    ACTIONS = (errors.RETRY, errors.SUPPORT, errors.DISMISS)

    def test_starts_on_the_first_action(self):
        menu = errors.ActionMenu(self.ACTIONS)
        self.assertEqual(menu.selected, 0)
        self.assertEqual(menu.current, errors.RETRY)

    def test_arrows_and_vi_keys_move_and_wrap(self):
        menu = errors.ActionMenu(self.ACTIONS)
        self.assertIsNone(menu.handle_key(curses.KEY_DOWN))
        self.assertEqual(menu.selected, 1)
        menu.handle_key(ord("j"))
        self.assertEqual(menu.selected, 2)
        menu.handle_key(curses.KEY_DOWN)
        self.assertEqual(menu.selected, 0, "wraps to the top")
        menu.handle_key(curses.KEY_UP)
        self.assertEqual(menu.selected, 2, "wraps to the bottom")
        menu.handle_key(ord("k"))
        self.assertEqual(menu.selected, 1)

    def test_enter_keys_choose_the_highlighted_action(self):
        for key in (ENTER, 13, curses.KEY_ENTER):
            menu = errors.ActionMenu(self.ACTIONS)
            menu.handle_key(curses.KEY_DOWN)
            self.assertEqual(menu.handle_key(key), "support")

    def test_escape_dismisses_whatever_is_highlighted(self):
        menu = errors.ActionMenu(self.ACTIONS)
        menu.handle_key(curses.KEY_DOWN)
        self.assertEqual(menu.handle_key(ESC), errors.DISMISS.id)

    def test_idle_resize_and_unknown_keys_change_nothing(self):
        menu = errors.ActionMenu(self.ACTIONS)
        for key in (-1, curses.KEY_RESIZE, ord("x"), curses.KEY_LEFT):
            self.assertIsNone(menu.handle_key(key))
        self.assertEqual(menu.selected, 0)

    def test_a_single_button_still_works(self):
        menu = errors.ActionMenu([errors.DISMISS])
        menu.handle_key(curses.KEY_DOWN)
        self.assertEqual(menu.handle_key(ENTER), "dismiss")

    def test_needs_at_least_one_action(self):
        with self.assertRaises(ValueError):
            errors.ActionMenu([])


class AppFailedTest(unittest.TestCase):
    TIMEOUT = "THE APPLICATION DID NOT BECOME READY BEFORE THE STARTUP TIMEOUT"

    def test_app_that_failed_offers_try_again_and_a_support_file(self):
        failure = errors.describe_failure("MOONLIGHT", self.TIMEOUT, app_id="moonlight", online=True)
        self.assertEqual(failure.kind, "app")
        self.assertEqual(failure.title, "MOONLIGHT FAILED TO START")
        self.assertEqual(failure.detail, self.TIMEOUT)
        self.assertEqual(ids(failure), ["retry", "support", "dismiss"])
        self.assertEqual([a.label for a in failure.actions], ["TRY AGAIN", "SAVE SUPPORT FILE", "BACK"])
        self.assertIn("TRY AGAIN", failure.hint)
        self.assertIn("SUPPORT FILE", failure.hint)

    def test_unknown_network_state_is_not_blamed(self):
        failure = errors.describe_failure("FIREFOX", "exited before the application became ready (status 1)",
                                          app_id="firefox", online=None)
        self.assertEqual(failure.kind, "app")

    def test_an_app_that_needs_no_network_is_never_blamed_on_it(self):
        failure = errors.describe_failure("TERMINAL", "boom", app_id="terminal", online=False)
        self.assertEqual(failure.kind, "app")
        self.assertNotIn("network", ids(failure))

    def test_empty_message_still_says_something(self):
        self.assertEqual(errors.describe_failure("X", "").detail, "UNKNOWN ERROR")


class NetworkFailedTest(unittest.TestCase):
    def test_offline_pc_and_a_network_app_points_at_network_settings(self):
        failure = errors.describe_failure("MOONLIGHT", "exited before the application became ready (status 1)",
                                          app_id="moonlight", online=False)
        self.assertEqual(failure.kind, "network")
        self.assertEqual(ids(failure), ["network", "retry", "dismiss"])
        self.assertEqual(failure.actions[0].label, "NETWORK SETTINGS")
        self.assertIn("NETWORK SETTINGS", failure.hint)

    def test_remote_desktop_host_that_cannot_be_reached_is_a_network_problem_even_when_online(self):
        failure = errors.describe_failure(
            "OFFICE PC", "COULD NOT REACH 10.0.0.9:3389: [Errno 113] No route to host", app_kind="rdp", online=True
        )
        self.assertEqual(failure.kind, "network")
        self.assertEqual(ids(failure), ["network", "retry", "dismiss"])

    def test_common_connection_failures_are_recognised(self):
        for message in (
            "COULD NOT REACH 10.0.0.9:3389: timed out",
            "NAME OR SERVICE NOT KNOWN",
            "TEMPORARY FAILURE IN NAME RESOLUTION",
            "NETWORK IS UNREACHABLE",
        ):
            self.assertEqual(errors.describe_failure("X", message).kind, "network", message)

    def test_the_network_setup_app_never_offers_to_open_itself(self):
        failure = errors.describe_failure("NETWORK SETUP", "NETWORK IS UNREACHABLE", app_id="network-setup", online=False)
        self.assertNotIn("network", ids(failure))
        self.assertIn("retry", ids(failure))

    def test_bluetooth_problems_point_at_bluetooth_settings(self):
        failure = errors.describe_failure("PAIRING", "BLUETOOTH IS TURNED OFF")
        self.assertEqual(failure.kind, "bluetooth")
        self.assertEqual(ids(failure), ["bluetooth", "retry", "dismiss"])
        self.assertEqual(failure.actions[0].label, "BLUETOOTH SETTINGS")


class SimpleFailureTest(unittest.TestCase):
    def test_retry_is_optional(self):
        self.assertEqual(ids(errors.simple_failure("USB DRIVE NOT FOUND", "PLUG ONE IN", retry=True)), ["retry", "dismiss"])
        self.assertEqual(ids(errors.simple_failure("TIMED OUT", "REBOOT FIRST", retry=False)), ["dismiss"])

    def test_extra_actions_can_be_passed(self):
        failure = errors.simple_failure("NO SOUND", "x", errors.BLUETOOTH, retry=True)
        self.assertEqual(ids(failure), ["bluetooth", "retry", "dismiss"])


class ExtensionPointTest(unittest.TestCase):
    """The lead adds a WAKE PC button for Moonlight host errors with one register_action call."""

    WAKE = errors.Action("WAKE PC", "wake-pc")

    def setUp(self):
        patcher = mock.patch.object(errors, "_EXTRA", [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_registered_action_shows_for_the_chosen_app_and_comes_first(self):
        errors.register_action(self.WAKE, app_ids=("moonlight",))
        failure = errors.describe_failure("MOONLIGHT", "boom", app_id="moonlight", online=True)
        self.assertEqual(ids(failure), ["wake-pc", "retry", "support", "dismiss"])
        other = errors.describe_failure("FIREFOX", "boom", app_id="firefox", online=True)
        self.assertNotIn("wake-pc", ids(other))

    def test_registered_action_can_follow_an_error_kind(self):
        errors.register_action(self.WAKE, kinds=("network",))
        failure = errors.describe_failure("OFFICE PC", "COULD NOT REACH 10.0.0.9:3389: timed out", app_kind="rdp")
        self.assertEqual(ids(failure), ["wake-pc", "network", "retry", "dismiss"])
        self.assertNotIn("wake-pc", ids(errors.describe_failure("X", "boom")))

    def test_registering_twice_does_not_duplicate_the_button(self):
        errors.register_action(self.WAKE, app_ids=("moonlight",))
        errors.register_action(self.WAKE, app_ids=("moonlight",))
        failure = errors.describe_failure("MOONLIGHT", "boom", app_id="moonlight")
        self.assertEqual(ids(failure).count("wake-pc"), 1)


class Screen:
    """Records what a dialog draws and feeds it scripted keys."""

    def __init__(self, keys=(), size=(30, 100)):
        self.keys = list(keys)
        self.size = size
        self.frames = [[]]

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.frames.append([])

    def border(self, *_args):
        pass

    def addstr(self, _row, _column, text, *_attr):
        self.frames[-1].append(text)

    def addnstr(self, _row, _column, text, count, *_attr):
        self.frames[-1].append(text[:count])

    def refresh(self):
        pass

    def timeout(self, *_args):
        pass

    def keypad(self, *_args):
        pass

    def getch(self):
        if not self.keys:
            raise AssertionError("the dialog asked for more keys than the test supplied")
        return self.keys.pop(0)

    @property
    def text(self):
        return "\n".join(self.frames[-1])


class ShowTest(unittest.TestCase):
    FAILURE = errors.describe_failure("MOONLIGHT", "THE APPLICATION DID NOT BECOME READY", app_id="moonlight", online=True)

    def test_draws_title_message_hint_and_every_button(self):
        screen = Screen([ESC])
        errors.show(screen, self.FAILURE)
        for needle in ("MOONLIGHT FAILED TO START", "DID NOT BECOME READY", "TRY AGAIN", "SAVE SUPPORT FILE", "BACK"):
            self.assertIn(needle, screen.text)

    def test_marker_follows_the_selection(self):
        screen = Screen([curses.KEY_DOWN, ESC])
        errors.show(screen, self.FAILURE)
        self.assertIn(">  SAVE SUPPORT FILE", screen.text)
        self.assertNotIn(">  TRY AGAIN", screen.text)

    def test_returns_the_chosen_action(self):
        self.assertEqual(errors.show(Screen([ENTER]), self.FAILURE), "retry")
        self.assertEqual(errors.show(Screen([curses.KEY_DOWN, ENTER]), self.FAILURE), "support")
        self.assertEqual(errors.show(Screen([curses.KEY_UP, ENTER]), self.FAILURE), "dismiss")

    def test_escape_dismisses_and_idle_ticks_are_ignored(self):
        self.assertEqual(errors.show(Screen([-1, -1, curses.KEY_RESIZE, ESC]), self.FAILURE), "dismiss")

    def test_uses_the_launchers_key_reader_when_given(self):
        seen = []

        def read_key(screen):
            seen.append(screen)
            return ENTER

        screen = Screen()
        self.assertEqual(errors.show(screen, self.FAILURE, read_key=read_key), "retry")
        self.assertEqual(seen, [screen])

    def test_a_tiny_screen_does_not_crash(self):
        self.assertEqual(errors.show(Screen([ESC], size=(6, 20)), self.FAILURE), "dismiss")


class LauncherErrorsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_errors_under_test", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def launcher(self, keys=(), online=True, bluetooth=True):
        summary = "10.0.0.7  ONLINE" if online else "NO IPV4  OFFLINE"
        with mock.patch.object(self.module, "network_summary", return_value=summary):
            launcher = self.module.Launcher(Screen(keys))
        launcher.updates = self.module.update.Checker(
            current="0.1.13", state_path=pathlib.Path("/nonexistent/update-check.ini"))
        self.network = mock.patch.object(self.module, "network_summary", return_value=summary)
        self.network.start()
        self.addCleanup(self.network.stop)
        adapter = mock.patch.object(self.module.controllers, "bluetooth_present", return_value=bluetooth)
        adapter.start()  # the PC under test has (or has not) a Bluetooth adapter, whatever the VM has
        self.addCleanup(adapter.stop)
        return launcher

    def moonlight(self):
        return self.module.apps.Application(
            id="moonlight", name="MOONLIGHT", kind="request", request="start-moonlight", status_id="moonlight",
        )

    # --- app failed -------------------------------------------------------------------------

    def test_failed_app_dialog_offers_retry_and_support_file_and_returns_the_choice(self):
        launcher = self.launcher([ENTER])
        self.assertEqual(launcher.show_launch_failure("MOONLIGHT", "boom", app=self.moonlight()), "retry")
        launcher = self.launcher([ESC])
        self.assertEqual(launcher.show_launch_failure("MOONLIGHT", "boom", app=self.moonlight()), "dismiss")

    def test_save_support_file_runs_the_support_export_and_shows_the_dialog_again(self):
        launcher = self.launcher([curses.KEY_DOWN, ENTER, ESC])
        with mock.patch.object(self.module.Settings, "generate_support_file") as export:
            self.assertEqual(launcher.show_launch_failure("MOONLIGHT", "boom", app=self.moonlight()), "dismiss")
        export.assert_called_once_with()

    def test_choosing_try_again_relaunches_the_app(self):
        launcher = self.launcher([-1] * 40)
        app = self.moonlight()
        launcher.show_launch_failure = mock.Mock(side_effect=["retry", "dismiss"])
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher.request = mock.Mock(
                side_effect=lambda _name: (run / "moonlight-status").write_text("failed: boom\n")
            )
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module.time, "monotonic", side_effect=itertools.count(0.0, 1.0)
            ):
                self.assertFalse(launcher.launch_app(app))
        self.assertEqual(launcher.request.call_count, 2)
        self.assertEqual(launcher.show_launch_failure.call_count, 2)
        self.assertEqual(launcher.show_launch_failure.call_args.kwargs["app"], app)
        self.assertIn("FAILED", launcher.status)

    def test_a_dismissed_failure_does_not_relaunch(self):
        launcher = self.launcher([-1] * 20)
        launcher.show_launch_failure = mock.Mock(return_value="dismiss")
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            launcher.request = mock.Mock(
                side_effect=lambda _name: (run / "moonlight-status").write_text("failed: boom\n")
            )
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module.time, "monotonic", side_effect=itertools.count(0.0, 1.0)
            ):
                self.assertFalse(launcher.launch_app(self.moonlight()))
        self.assertEqual(launcher.request.call_count, 1)

    # --- network ----------------------------------------------------------------------------

    def test_offline_pc_failure_offers_network_settings_which_opens_the_network_app(self):
        launcher = self.launcher([ENTER, ESC], online=False)
        launcher.launch_by_id = mock.Mock(return_value=True)
        self.assertEqual(launcher.show_launch_failure("MOONLIGHT", "boom", app=self.moonlight()), "dismiss")
        launcher.launch_by_id.assert_called_once_with("network-setup")

    def test_online_pc_failure_does_not_offer_network_settings(self):
        launcher = self.launcher()
        with mock.patch.object(self.module.errors, "show", return_value="dismiss") as show:
            launcher.show_launch_failure("MOONLIGHT", "boom", app=self.moonlight())
        self.assertEqual(ids(show.call_args.args[1]), ["retry", "support", "dismiss"])

    def test_offline_pc_failure_lists_network_settings_first(self):
        launcher = self.launcher(online=False)
        with mock.patch.object(self.module.errors, "show", return_value="dismiss") as show:
            launcher.show_launch_failure("MOONLIGHT", "boom", app=self.moonlight())
        self.assertEqual(ids(show.call_args.args[1]), ["network", "retry", "dismiss"])

    def remote_desktop(self, launcher):
        connection = self.module.rdp.Connection(
            id="rdp-work-pc", name="Work", host="10.0.0.9", username="alice", certificate="ab" * 32)
        application = self.module.apps.Application(
            id="rdp-work-pc", name="WORK", kind="rdp", connection="rdp-work-pc", status_id="rdp-work-pc")
        return connection, application, self.module.RemoteDesktopSettings(launcher.screen, launcher)

    def test_unreachable_remote_desktop_host_offers_network_settings_and_try_again(self):
        launcher = self.launcher()
        connection, application, remote = self.remote_desktop(launcher)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module.rdp, "get_connection", return_value=connection), mock.patch.object(
            self.module.rdp, "probe_certificate", side_effect=OSError("timed out")
        ), mock.patch.object(self.module.errors, "show", return_value="dismiss") as show:
            self.assertFalse(remote.prepare_launch(application))
        failure = show.call_args.args[1]
        self.assertEqual(ids(failure), ["network", "retry", "dismiss"])
        self.assertIn("10.0.0.9:3389", failure.detail)

    def test_try_again_on_an_unreachable_host_probes_again(self):
        launcher = self.launcher()
        connection, application, remote = self.remote_desktop(launcher)
        remote.text_input = mock.Mock(return_value="Fake-Typed-Password")
        probe = mock.Mock(side_effect=[OSError("timed out"), "ab" * 32])
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module.rdp, "get_connection", return_value=connection), mock.patch.object(
            self.module.rdp, "probe_certificate", probe
        ), mock.patch.object(self.module.errors, "show", return_value="retry"):
            self.assertTrue(remote.prepare_launch(application))
        self.assertEqual(probe.call_count, 2)

    # --- Remote Desktop problems that only Settings can fix ---------------------------------------

    def prepare_with_problem(self, launcher, *, connection, password_result=None):
        """prepare_launch with the saved connection gone (connection=None) or its saved password refused."""
        stored, application, remote = self.remote_desktop(launcher)
        remote.password_request = mock.Mock(return_value=password_result)
        found = stored if connection is None else connection
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module, "active_rdp_session", return_value=None), mock.patch.object(
            self.module.rdp, "get_connection", return_value=None if connection is None else found
        ), mock.patch.object(self.module.rdp, "probe_certificate", return_value="ab" * 32), mock.patch.object(
            self.module.errors, "show", return_value="dismiss"
        ) as show:
            self.assertFalse(remote.prepare_launch(application))
        return show.call_args.args[1]

    def test_a_deleted_saved_connection_points_at_remote_desktop_settings_online_or_not(self):
        for online in (True, False):
            with self.subTest(online=online):
                failure = self.prepare_with_problem(self.launcher(online=online), connection=None)
                self.assertEqual(ids(failure), ["dismiss"], "no network button, no TRY AGAIN that cannot work")
                self.assertIn("NO LONGER EXISTS", failure.detail)
                self.assertIn("SETTINGS > REMOTE DESKTOP", failure.hint)
                self.assertNotIn("NETWORK", failure.detail + failure.hint)
                self.assertNotIn("TRY AGAIN", failure.hint)

    def test_a_refused_saved_password_points_at_remote_desktop_settings_online_or_not(self):
        saved = self.module.rdp.Connection(
            id="rdp-work-pc", name="Work", host="10.0.0.9", username="alice", certificate="ab" * 32,
            save_password=True,
        )
        for online in (True, False):
            with self.subTest(online=online):
                failure = self.prepare_with_problem(
                    self.launcher(online=online), connection=saved,
                    password_result=(False, "the saved password belongs to different server settings"),
                )
                self.assertEqual(ids(failure), ["support", "dismiss"])
                self.assertIn("DIFFERENT SERVER SETTINGS", failure.detail)
                self.assertIn("SETTINGS > REMOTE DESKTOP", failure.hint)
                self.assertNotIn("NETWORK", failure.hint)
                self.assertNotIn("TRY AGAIN", failure.hint)

    # --- another Remote Desktop session is open -------------------------------------------------

    def prepare_with_session(self, launcher, sessions, choices, *, other_ready=True):
        """Run prepare_launch for rdp-work-pc. `sessions` are the ids active_rdp_session reports, one per look;
        `choices` are the error-screen buttons pressed. Returns (result, the errors.show mock)."""
        _connection, application, remote = self.remote_desktop(launcher)
        remote.text_input = mock.Mock(return_value="Fake-Typed-Password")
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            if other_ready:
                (run / "rdp-home-pc-ready").touch()
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(
                self.module, "active_rdp_session", side_effect=sessions
            ), mock.patch.object(
                self.module.rdp, "get_connection", return_value=_connection
            ), mock.patch.object(
                self.module.rdp, "probe_certificate", return_value="ab" * 32
            ), mock.patch.object(self.module.rdp, "write_handoff"), mock.patch.object(
                self.module.errors, "show", side_effect=choices
            ) as show:
                return remote.prepare_launch(application), show

    def test_open_remote_desktop_session_offers_active_applications_not_try_again(self):
        launcher = self.launcher()
        result, show = self.prepare_with_session(launcher, ["rdp-home-pc"], ["dismiss"])
        self.assertFalse(result)
        failure = show.call_args.args[1]
        self.assertEqual(ids(failure), ["active-apps", "dismiss"])
        self.assertEqual(failure.actions[0].label, "ACTIVE APPLICATIONS")
        self.assertNotIn("TRY AGAIN", failure.detail + failure.hint)

    def test_closing_the_other_session_carries_on_with_the_connection(self):
        launcher = self.launcher()
        launcher.active_applications = mock.Mock()
        result, show = self.prepare_with_session(launcher, ["rdp-home-pc", None, None], ["active-apps"])
        self.assertTrue(result, "no other session is left, so the launch goes on to the password prompt")
        launcher.active_applications.assert_called_once_with()
        self.assertEqual(show.call_count, 1)

    def test_the_other_session_still_open_brings_the_screen_back(self):
        launcher = self.launcher()
        launcher.active_applications = mock.Mock()
        result, show = self.prepare_with_session(
            launcher, ["rdp-home-pc", "rdp-home-pc"], ["active-apps", "dismiss"]
        )
        self.assertFalse(result)
        self.assertEqual(show.call_count, 2)

    def test_a_session_that_is_only_starting_or_reconnecting_is_waited_for_not_listed(self):
        launcher = self.launcher()
        result, show = self.prepare_with_session(launcher, ["rdp-home-pc"], ["dismiss"], other_ready=False)
        self.assertFalse(result)
        failure = show.call_args.args[1]
        self.assertEqual(ids(failure), ["retry", "dismiss"], "it is not in ACTIVE APPLICATIONS yet")
        self.assertIn("WAIT", failure.hint)

    # --- the WAKE PC hook ---------------------------------------------------------------------

    def test_a_registered_wake_pc_action_runs_its_handler_and_can_request_a_retry(self):
        launcher = self.launcher([ENTER])
        app = self.moonlight()
        wake = mock.Mock(return_value=True)
        launcher.failure_actions["wake-pc"] = wake
        with mock.patch.object(errors, "_EXTRA", []):
            errors.register_action(errors.Action("WAKE PC", "wake-pc"), app_ids=("moonlight",))
            self.assertEqual(launcher.show_launch_failure("MOONLIGHT", "boom", app=app), "retry")
        wake.assert_called_once_with(app)

    def test_an_action_without_a_handler_does_nothing_and_keeps_the_dialog_open(self):
        launcher = self.launcher([ENTER, ESC])
        with mock.patch.object(errors, "_EXTRA", []):
            errors.register_action(errors.Action("WAKE PC", "wake-pc"), app_ids=("moonlight",))
            self.assertEqual(launcher.show_launch_failure("MOONLIGHT", "boom", app=self.moonlight()), "dismiss")

    # --- support export ------------------------------------------------------------------------

    def test_missing_usb_drive_offers_try_again_and_rechecks(self):
        launcher = self.launcher()
        settings = self.module.Settings(launcher.screen, launcher)
        with mock.patch.object(
            self.module.support, "discover_destinations", side_effect=[[], []]
        ) as discover, mock.patch.object(self.module.errors, "show", side_effect=["retry", "dismiss"]) as show:
            settings.generate_support_file()
        self.assertEqual(discover.call_count, 2)
        failure = show.call_args.args[1]
        self.assertEqual(failure.title, "USB DRIVE NOT FOUND")
        self.assertEqual(ids(failure), ["retry", "dismiss"])
        self.assertIn("USB", failure.detail)

    def test_support_export_timeout_does_not_offer_to_try_again(self):
        launcher = self.launcher()
        settings = self.module.Settings(launcher.screen, launcher)
        with mock.patch.object(self.module.errors, "show", return_value="dismiss") as show:
            settings.show_error("SUPPORT EXPORT TIMED OUT", "REBOOT COUCHLITEOS BEFORE TRYING AGAIN.", retry=False)
        self.assertEqual(ids(show.call_args.args[1]), ["dismiss"])

    # --- audio: nothing to play on -------------------------------------------------------------

    def test_no_sound_output_offers_bluetooth_settings_once(self):
        launcher = self.launcher([-1, ESC])
        settings = self.module.Settings(launcher.screen, launcher)
        with mock.patch.object(self.module.audio, "query_sinks", return_value=[]), mock.patch.object(
            self.module.errors, "show", return_value="dismiss"
        ) as show:
            settings.run_audio()
        self.assertEqual(show.call_count, 1, "asked once, not on every redraw")
        self.assertEqual(ids(show.call_args.args[1]), ["bluetooth", "retry", "dismiss"])

    def test_choosing_bluetooth_settings_from_the_audio_dialog_opens_bluetooth(self):
        launcher = self.launcher([ESC])
        settings = self.module.Settings(launcher.screen, launcher)
        with mock.patch.object(self.module.audio, "query_sinks", return_value=[]), mock.patch.object(
            self.module.errors, "show", side_effect=["bluetooth", "dismiss"]
        ), mock.patch.object(self.module.bluetooth, "run_bluetooth") as run_bluetooth:
            settings.run_audio()
        run_bluetooth.assert_called_once_with(launcher.screen)

    def test_audio_query_failure_offers_try_again_and_a_support_file(self):
        launcher = self.launcher([ESC])
        settings = self.module.Settings(launcher.screen, launcher)
        with mock.patch.object(self.module.audio, "query_sinks", side_effect=RuntimeError("no pipewire")), mock.patch.object(
            self.module.errors, "show", return_value="dismiss"
        ) as show:
            settings.run_audio()
        self.assertEqual(ids(show.call_args.args[1]), ["retry", "support", "dismiss"])
        self.assertIn("no pipewire".upper(), show.call_args.args[1].detail)

    # --- no Bluetooth adapter on this PC ---------------------------------------------------------

    def test_no_sound_output_on_a_pc_without_bluetooth_offers_no_bluetooth_button(self):
        launcher = self.launcher([-1, ESC], bluetooth=False)
        settings = self.module.Settings(launcher.screen, launcher)
        with mock.patch.object(self.module.audio, "query_sinks", return_value=[]), mock.patch.object(
            self.module.errors, "show", return_value="dismiss"
        ) as show:
            settings.run_audio()
        failure = show.call_args.args[1]
        self.assertEqual(ids(failure), ["retry", "dismiss"])
        self.assertNotIn("BLUETOOTH", failure.detail + failure.hint, "nothing to pair on a PC without an adapter")
        self.assertIn("TV OR SPEAKERS", failure.detail)

    def test_a_bluetooth_failure_on_a_pc_without_bluetooth_says_so_instead_of_offering_settings(self):
        launcher = self.launcher(bluetooth=False)
        with mock.patch.object(self.module.errors, "show", return_value="dismiss") as show:
            launcher.show_launch_failure("PAIRING", "BLUETOOTH IS TURNED OFF")
        failure = show.call_args.args[1]
        self.assertEqual(ids(failure), ["retry", "dismiss"])
        self.assertNotIn("BLUETOOTH SETTINGS", failure.hint)
        self.assertIn("NO BLUETOOTH ADAPTER", failure.hint)

    def test_a_bluetooth_failure_on_a_pc_with_bluetooth_still_offers_settings(self):
        launcher = self.launcher(bluetooth=True)
        with mock.patch.object(self.module.errors, "show", return_value="dismiss") as show:
            launcher.show_launch_failure("PAIRING", "BLUETOOTH IS TURNED OFF")
        self.assertEqual(ids(show.call_args.args[1]), ["bluetooth", "retry", "dismiss"])

    # --- status-line wording -----------------------------------------------------------------

    def test_a_missing_app_says_where_to_turn_it_on(self):
        launcher = self.launcher()
        with mock.patch.object(self.module, "application_result", return_value=self.module.apps.LoadResult((), ())):
            self.assertFalse(launcher.launch_by_id("chiaki-ng"))
        self.assertIn("SETTINGS > APPLICATIONS", launcher.status)

    def test_resuming_an_app_that_will_not_focus_says_how_to_recover(self):
        launcher = self.launcher([ENTER, ESC])
        with mock.patch.object(launcher, "running_applications", return_value=[self.moonlight()]), mock.patch.object(
            launcher, "focus_app", return_value=False
        ):
            launcher.active_applications()
        self.assertIn("COULD NOT FOCUS MOONLIGHT", launcher.screen.text)
        self.assertIn("CLOSE IT", launcher.screen.text)

    def test_a_running_app_that_cannot_be_shown_says_how_to_close_it(self):
        launcher = self.launcher()
        with tempfile.TemporaryDirectory() as directory:
            run = pathlib.Path(directory)
            (run / "moonlight-ready").touch()
            with mock.patch.object(self.module, "RUN", run), mock.patch.object(launcher, "focus_app", return_value=False):
                self.assertFalse(launcher.launch_app(self.moonlight()))
        self.assertIn("ACTIVE APPLICATIONS", launcher.status)


class RemoteSessionBusyTest(unittest.TestCase):
    def test_open_session_offers_active_applications_and_names_the_buttons(self):
        failure = errors.remote_session_busy("WORK PC", open_now=True)
        self.assertEqual(failure.title, "WORK PC FAILED TO START")
        self.assertEqual(ids(failure), ["active-apps", "dismiss"])
        for needle in ("ACTIVE APPLICATIONS", "A/ENTER", "Y (XBOX) / SQUARE (PS)", "B/ESC"):
            self.assertIn(needle, failure.hint)

    def test_session_that_is_starting_is_waited_for(self):
        failure = errors.remote_session_busy("WORK PC", open_now=False)
        self.assertEqual(ids(failure), ["retry", "dismiss"])
        self.assertIn("TRY AGAIN", failure.hint)

    def test_both_screens_fit_80_columns_with_nothing_cut_off(self):
        for open_now in (True, False):
            failure = errors.remote_session_busy("A VERY LONG CONNECTION NAME", open_now=open_now)
            screen = Screen([ESC], size=(24, 80))
            errors.show(screen, failure)
            lines = screen.frames[-1]
            self.assertTrue(all(len(line) <= 76 for line in lines), lines)
            text = " ".join(line.strip() for line in lines)
            self.assertIn(failure.detail, text)
            self.assertIn(failure.hint, text)


class NoBluetoothAdapterTest(unittest.TestCase):
    def test_without_bluetooth_drops_the_button_and_the_hint_that_points_at_it(self):
        failure = errors.describe_failure("PAIRING", "BLUETOOTH IS TURNED OFF").without_bluetooth()
        self.assertEqual(ids(failure), ["retry", "dismiss"])
        self.assertNotIn("BLUETOOTH SETTINGS", failure.hint)
        self.assertIn("NO BLUETOOTH ADAPTER", failure.hint)
        self.assertIn("TRY AGAIN", failure.hint)

    def test_the_replacement_hint_does_not_promise_try_again_when_there_is_no_such_button(self):
        failure = errors.describe_failure("PAIRING", "BLUETOOTH IS TURNED OFF", retry=False).without_bluetooth()
        self.assertEqual(ids(failure), ["dismiss"])
        self.assertNotIn("TRY AGAIN", failure.hint)

    def test_failures_without_the_button_are_returned_unchanged(self):
        failure = errors.describe_failure("MOONLIGHT", "boom", app_id="moonlight", online=True)
        self.assertIs(failure.without_bluetooth(), failure)

    def test_a_failure_that_only_carries_the_button_loses_just_the_button(self):
        failure = errors.simple_failure("NO SOUND", "x", errors.BLUETOOTH, hint="CHECK THE TV.", retry=True)
        trimmed = failure.without_bluetooth()
        self.assertEqual(ids(trimmed), ["retry", "dismiss"])
        self.assertEqual(trimmed.hint, "CHECK THE TV.")

    def test_no_sound_screen_names_bluetooth_only_when_there_is_an_adapter(self):
        with_adapter = errors.no_sound_output(bluetooth=True)
        self.assertEqual(ids(with_adapter), ["bluetooth", "retry", "dismiss"])
        self.assertIn("BLUETOOTH SPEAKER", with_adapter.detail)
        without = errors.no_sound_output(bluetooth=False)
        self.assertEqual(ids(without), ["retry", "dismiss"])
        self.assertNotIn("BLUETOOTH", without.detail)


class RemoteSettingsProblemTest(unittest.TestCase):
    def test_a_missing_connection_says_where_to_go_and_fits_80_columns(self):
        failure = errors.connection_missing("A VERY LONG CONNECTION NAME")
        self.assertEqual(ids(failure), ["dismiss"])
        self.assertEqual(failure.title, "A VERY LONG CONNECTION NAME FAILED TO START")
        for needle in ("B/ESC", "SETTINGS > REMOTE DESKTOP", "A/ENTER"):
            self.assertIn(needle, failure.hint)
        screen = Screen([ESC], size=(24, 80))
        errors.show(screen, failure)
        self.assertTrue(all(len(line) <= 76 for line in screen.frames[-1]), screen.frames[-1])
        self.assertIn(failure.hint, " ".join(line.strip() for line in screen.frames[-1]))

    def test_a_refused_saved_password_keeps_the_support_file_button(self):
        failure = errors.saved_password_unusable("WORK", "the password service did not respond")
        self.assertEqual(ids(failure), ["support", "dismiss"])
        self.assertIn("PASSWORD SERVICE DID NOT RESPOND", failure.detail)
        self.assertIn("SETTINGS > REMOTE DESKTOP", failure.hint)
        self.assertNotIn("TRY AGAIN", failure.hint)


class EveryFailureKeepsItsPromisesTest(unittest.TestCase):
    """A hint must never send the user to a button the screen does not have (a dead end)."""

    PROMISES = (
        ("TRY AGAIN", "retry"),
        ("NETWORK SETTINGS", "network"),
        ("BLUETOOTH SETTINGS", "bluetooth"),
        ("SUPPORT FILE", "support"),
        ("ACTIVE APPLICATIONS", "active-apps"),
    )

    def every_failure(self):
        messages = (
            "boom",
            "BLUETOOTH IS TURNED OFF",
            "NETWORK IS UNREACHABLE",
            "COULD NOT REACH 10.0.0.9:3389: timed out",
        )
        for message, app_id, app_kind, online, retry in itertools.product(
            messages, ("", "moonlight", "network-setup", "terminal"), ("", "rdp"), (True, False, None), (True, False)
        ):
            label = f"{message!r} app={app_id!r}/{app_kind!r} online={online} retry={retry}"
            failure = errors.describe_failure(
                "X", message, app_id=app_id, app_kind=app_kind, online=online, retry=retry
            )
            yield label, failure
            yield label + " (no bluetooth adapter)", failure.without_bluetooth()
        for kind, retry in itertools.product(("network", "bluetooth", "app"), (True, False)):
            yield f"problem {kind} retry={retry}", errors.problem("X", "boom", kind=kind, retry=retry)

    def test_hints_only_name_buttons_that_are_on_the_screen(self):
        checked = 0
        for label, failure in self.every_failure():
            for words, action_id in self.PROMISES:
                if words in failure.hint:
                    self.assertIn(action_id, ids(failure), f"{label}: hint says {words}: {failure.hint}")
            checked += 1
        self.assertGreater(checked, 200)

    def test_every_screen_fits_80_columns_with_nothing_cut_off(self):
        for label, failure in self.every_failure():
            screen = Screen([ESC], size=(24, 80))
            errors.show(screen, failure)
            lines = screen.frames[-1]
            self.assertTrue(all(len(line) <= 76 for line in lines), label)
            text = " ".join(line.strip() for line in lines)
            self.assertIn(failure.detail, text, label)
            self.assertIn(failure.hint, text, label)

    def test_every_screen_ends_with_back_and_has_no_repeated_button(self):
        for label, failure in self.every_failure():
            self.assertEqual(ids(failure)[-1], "dismiss", label)
            self.assertEqual(len(set(ids(failure))), len(ids(failure)), label)

    def test_a_hint_without_try_again_still_says_what_to_do(self):
        offline = errors.describe_failure("X", "boom", app_id="moonlight", online=False, retry=False)
        self.assertIn("OPEN NETWORK SETTINGS", offline.hint)
        self.assertNotIn("TRY AGAIN", offline.hint)
        rdp = errors.describe_failure("X", "boom", app_kind="rdp", online=True, retry=False)
        self.assertIn("SETTINGS > REMOTE DESKTOP", rdp.hint)
        app = errors.describe_failure("X", "boom", app_id="terminal", online=True, retry=False)
        self.assertEqual(app.hint, "SAVE A SUPPORT FILE TO A USB DRIVE SO IT CAN BE DIAGNOSED.")
        self.assertEqual(ids(app), ["support", "dismiss"])


if __name__ == "__main__":
    unittest.main()
