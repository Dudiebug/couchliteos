import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

import moonlightos_update as update

NOW = 1_800_000_000.0
DAY = 24 * 3600.0


class VersionTest(unittest.TestCase):
    def test_numeric_parts_compare_as_numbers(self):
        self.assertTrue(update.is_newer("0.1.10", "0.1.9"))
        self.assertFalse(update.is_newer("0.1.9", "0.1.10"))
        self.assertTrue(update.is_newer("0.2.0", "0.1.13"))
        self.assertTrue(update.is_newer("1.0.0", "0.99.99"))

    def test_same_version_is_not_newer(self):
        self.assertFalse(update.is_newer("0.1.13", "0.1.13"))
        self.assertFalse(update.is_newer("v0.1.13", "0.1.13"))
        self.assertFalse(update.is_newer("0.1", "0.1.0"))

    def test_leading_v_and_whitespace_are_tolerated(self):
        self.assertTrue(update.is_newer("v0.1.14\n", " 0.1.13 "))

    def test_prerelease_sorts_before_its_release(self):
        self.assertTrue(update.is_newer("0.2.0", "0.2.0-rc1"))
        self.assertFalse(update.is_newer("0.2.0-rc1", "0.2.0"))
        self.assertTrue(update.is_newer("0.2.0-rc2", "0.2.0-rc1"))
        self.assertTrue(update.is_newer("0.2.0-rc.10", "0.2.0-rc.2"))
        self.assertTrue(update.is_newer("0.2.0-rc.1", "0.2.0-beta.5"))
        self.assertTrue(update.is_newer("0.2.0-rc.1.1", "0.2.0-rc.1"))

    def test_prerelease_of_a_newer_version_beats_an_older_release(self):
        self.assertTrue(update.is_newer("0.2.0-rc1", "0.1.13"))
        self.assertFalse(update.is_newer("0.1.13", "0.2.0-rc1"))

    def test_build_metadata_is_ignored(self):
        self.assertFalse(update.is_newer("0.1.13+abc", "0.1.13"))

    def test_unparseable_versions_are_never_newer(self):
        for bad in ("", "latest", "nightly-2026", "1.2.3.4.5", "0.1.x", None):
            self.assertFalse(update.is_newer(bad, "0.1.13"), bad)
            self.assertFalse(update.is_newer("9.9.9", bad), bad)


class StateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = pathlib.Path(self._tmp.name) / "update-check.ini"

    def test_missing_file_means_enabled_and_never_checked(self):
        state = update.load_state(self.path)
        self.assertEqual((state.enabled, state.latest, state.checked_at), (True, "", 0.0))

    def test_round_trip_and_permissions(self):
        original = update.State(enabled=False, latest="0.1.14", checked_at=NOW, attempted_at=NOW)
        update.save_state(original, self.path)
        self.assertEqual(update.load_state(self.path), original)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_garbage_is_replaced_by_defaults(self):
        self.path.write_text("[updates]\nenabled = maybe\nlatest = <script>\nchecked_at = soon\n")
        state = update.load_state(self.path)
        self.assertEqual(state, update.State())
        self.path.write_bytes(b"\xff\xfe not an ini file")
        self.assertEqual(update.load_state(self.path), update.State())


class DueTest(unittest.TestCase):
    def test_never_checked_is_due(self):
        self.assertTrue(update.due(update.State(), NOW))

    def test_successful_check_is_not_repeated_for_24_hours(self):
        state = update.State(latest="0.1.13", checked_at=NOW, attempted_at=NOW)
        self.assertFalse(update.due(state, NOW + DAY - 1))
        self.assertTrue(update.due(state, NOW + DAY))

    def test_failed_attempt_is_retried_after_an_hour_not_a_day(self):
        state = update.State(attempted_at=NOW)
        self.assertFalse(update.due(state, NOW + update.RETRY_SECONDS - 1))
        self.assertTrue(update.due(state, NOW + update.RETRY_SECONDS))

    def test_disabled_is_never_due(self):
        self.assertFalse(update.due(update.State(enabled=False), NOW))

    def test_a_clock_that_went_backwards_does_not_block_checks_forever(self):
        state = update.State(checked_at=NOW + 10 * DAY, attempted_at=NOW + 10 * DAY)
        self.assertTrue(update.due(state, NOW))


class Response:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FetchTest(unittest.TestCase):
    def test_request_is_anonymous_with_a_versioned_user_agent_and_5s_timeout(self):
        opener = mock.Mock(return_value=Response({"tag_name": "0.1.14"}))
        self.assertEqual(update.fetch_latest("0.1.13", opener=opener), "0.1.14")
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/Dudiebug/moonlightos/releases/latest")
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["user-agent"], "MoonlightOS/0.1.13")
        self.assertEqual(set(headers), {"user-agent", "accept"})
        self.assertEqual(opener.call_args.kwargs["timeout"], 5)

    def test_bad_answers_raise(self):
        for body in ({"tag_name": "nightly"}, {"name": "x"}, {"tag_name": 5}, b"not json", b"[]"):
            with self.assertRaises(update.UpdateError, msg=body):
                update.fetch_latest("0.1.13", opener=mock.Mock(return_value=Response(body)))

    def test_network_errors_raise_update_error(self):
        for error in (urllib.error.URLError("offline"), TimeoutError(), OSError("x")):
            with self.assertRaises(update.UpdateError):
                update.fetch_latest("0.1.13", opener=mock.Mock(side_effect=error))

    def test_oversized_answers_are_rejected(self):
        with self.assertRaises(update.UpdateError):
            update.fetch_latest("0.1.13", opener=mock.Mock(return_value=Response(b"x" * (update.MAX_BYTES + 1))))


class NoticeTest(unittest.TestCase):
    def test_notice_text_for_a_newer_release(self):
        state = update.State(latest="0.1.14", checked_at=NOW)
        self.assertEqual(
            update.notice(state, "0.1.13"),
            "UPDATE AVAILABLE: 0.1.14 — github.com/Dudiebug/moonlightos/releases",
        )

    def test_no_notice_when_current_equal_older_disabled_or_unknown(self):
        self.assertEqual(update.notice(update.State(latest="0.1.13"), "0.1.13"), "")
        self.assertEqual(update.notice(update.State(latest="0.1.12"), "0.1.13"), "")
        self.assertEqual(update.notice(update.State(latest=""), "0.1.13"), "")
        self.assertEqual(update.notice(update.State(latest="0.1.14", enabled=False), "0.1.13"), "")
        self.assertEqual(update.notice(update.State(latest="0.1.14"), ""), "")


class InstalledVersionTest(unittest.TestCase):
    def test_first_readable_file_wins_and_junk_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "bad").write_text("not a version\n")
            (root / "good").write_text("0.1.13\n")
            self.assertEqual(update.installed_version([root / "missing", root / "bad", root / "good"]), "0.1.13")
            self.assertEqual(update.installed_version([root / "missing"]), "")

    def test_source_tree_version_file_exists_as_a_fallback(self):
        repo_version = pathlib.Path(__file__).resolve().parents[1] / "VERSION"
        self.assertIn(repo_version, update.VERSION_FILES)


class CheckerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = pathlib.Path(self._tmp.name) / "update-check.ini"
        self.now = NOW

    def checker(self, fetch):
        return update.Checker(
            current="0.1.13", state_path=self.path, fetch=fetch, clock=lambda: self.now,
        )

    def test_check_records_the_result_and_shows_a_notice(self):
        fetch = mock.Mock(return_value="0.1.14")
        checker = self.checker(fetch)
        checker.check_if_due()
        fetch.assert_called_once_with("0.1.13")
        self.assertIn("0.1.14", checker.notice())
        self.assertEqual(update.load_state(self.path).latest, "0.1.14")

    def test_at_most_one_successful_request_per_day_even_across_restarts(self):
        fetch = mock.Mock(return_value="0.1.14")
        self.checker(fetch).check_if_due()
        self.now += DAY / 2
        self.checker(fetch).check_if_due()  # a new launcher process reads the saved state
        self.assertEqual(fetch.call_count, 1)
        self.now += DAY
        self.checker(fetch).check_if_due()
        self.assertEqual(fetch.call_count, 2)

    def test_failures_are_silent_and_keep_the_previous_notice(self):
        self.checker(mock.Mock(return_value="0.1.14")).check_if_due()
        self.now += 2 * DAY
        checker = self.checker(mock.Mock(side_effect=update.UpdateError("offline")))
        checker.check_if_due()  # must not raise
        self.assertIn("0.1.14", checker.notice())

    def test_unexpected_errors_do_not_escape_the_background_thread(self):
        checker = self.checker(mock.Mock(side_effect=RuntimeError("boom")))
        checker.check_if_due()
        self.assertEqual(checker.notice(), "")

    def test_disabled_makes_no_requests_and_hides_the_notice(self):
        fetch = mock.Mock(return_value="0.1.14")
        checker = self.checker(fetch)
        checker.check_if_due()
        checker.set_enabled(False)
        self.assertEqual(checker.notice(), "")
        self.now += 5 * DAY
        checker.check_if_due()
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(update.load_state(self.path).enabled)

    def test_reenabling_checks_again_when_due(self):
        fetch = mock.Mock(return_value="0.1.14")
        checker = self.checker(fetch)
        checker.set_enabled(False)
        checker.set_enabled(True)
        checker.check_if_due()
        self.assertEqual(fetch.call_count, 1)

    def test_unwritable_state_does_not_break_the_checker(self):
        checker = update.Checker(
            current="0.1.13", state_path=pathlib.Path("/proc/nonexistent/x.ini"),
            fetch=mock.Mock(return_value="0.1.14"), clock=lambda: NOW,
        )
        checker.check_if_due()
        self.assertIn("0.1.14", checker.notice())


class LauncherUpdateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("moonlightos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_update_under_test", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def launcher(self, state):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (30, 100)
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(screen)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "update-check.ini"
            update.save_state(state, path)
            launcher.updates = update.Checker(current="0.1.13", state_path=path, fetch=mock.Mock(), clock=lambda: NOW)
        launcher.controllers = self.module.controllers.Monitor(reader=lambda: [])
        return launcher

    def test_home_shows_the_update_notice(self):
        launcher = self.launcher(update.State(latest="0.1.14", checked_at=NOW))
        texts = [text for text, _attr in launcher.footer_lines()]
        self.assertEqual(texts, ["UPDATE AVAILABLE: 0.1.14 — github.com/Dudiebug/moonlightos/releases"])

    def test_home_is_quiet_when_up_to_date(self):
        self.assertEqual(self.launcher(update.State(latest="0.1.13", checked_at=NOW)).footer_lines(), [])

    def test_settings_menu_has_the_toggle_and_it_flips_the_setting(self):
        self.assertIn("CHECK FOR UPDATES", self.module.SETTINGS_MENU)
        launcher = self.launcher(update.State())
        settings = self.module.Settings(launcher.screen, launcher)
        with tempfile.TemporaryDirectory() as directory:
            launcher.updates.state_path = pathlib.Path(directory) / "update-check.ini"
            self.assertEqual(settings.menu_label("CHECK FOR UPDATES"), "CHECK FOR UPDATES  ON")
            settings.toggle_updates()
            self.assertFalse(launcher.updates.enabled)
            self.assertEqual(settings.menu_label("CHECK FOR UPDATES"), "CHECK FOR UPDATES  OFF")
            self.assertEqual(settings.menu_label("DISPLAY"), "DISPLAY")
            settings.toggle_updates()
            self.assertTrue(launcher.updates.enabled)


if __name__ == "__main__":
    unittest.main()
