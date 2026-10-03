import testenv  # noqa: F401  (first: scratch run and state directories)
import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

import couchliteos_update as update

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
        original = update.State(enabled=False, latest="0.1.14", checked_at=NOW, attempted_at=NOW, prompted="0.1.14")
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

    def test_successful_check_is_not_repeated_within_a_run_for_six_hours(self):
        state = update.State(latest="0.1.13", checked_at=NOW, attempted_at=NOW)
        self.assertFalse(update.due(state, NOW + update.CHECK_SECONDS - 1))
        self.assertTrue(update.due(state, NOW + update.CHECK_SECONDS))
        self.assertEqual(update.CHECK_SECONDS, 6 * 3600)

    def test_the_first_check_of_a_run_ignores_the_last_check(self):
        state = update.State(latest="0.1.13", checked_at=NOW, attempted_at=NOW)
        self.assertFalse(update.due(state, NOW + 10, start=True), "a launcher restart loop must not hammer GitHub")
        self.assertFalse(update.due(state, NOW + update.START_GAP_SECONDS - 1, start=True))
        self.assertTrue(update.due(state, NOW + update.START_GAP_SECONDS, start=True))
        self.assertTrue(update.due(update.State(checked_at=NOW), NOW + 3600, start=True))

    def test_failed_attempt_is_retried_after_ten_minutes(self):
        state = update.State(attempted_at=NOW)
        self.assertEqual(update.RETRY_SECONDS, 600)
        self.assertFalse(update.due(state, NOW + update.RETRY_SECONDS - 1))
        self.assertTrue(update.due(state, NOW + update.RETRY_SECONDS))
        # A failure at start-up retries on the same schedule, not at the 60 s restart gap.
        self.assertFalse(update.due(state, NOW + update.RETRY_SECONDS - 1, start=True, retry=True))
        self.assertTrue(update.due(state, NOW + update.RETRY_SECONDS, start=True, retry=True))

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
        self.assertEqual(request.full_url, "https://api.github.com/repos/Dudiebug/couchliteos/releases/latest")
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["user-agent"], "CouchLiteOS/0.1.13")
        self.assertEqual(set(headers), {"user-agent", "accept"})
        self.assertEqual(opener.call_args.kwargs["timeout"], 5)

    def test_the_old_repository_name_answers_while_the_new_one_does_not_exist(self):
        # Until Dudiebug/moonlightos is renamed on GitHub, Dudiebug/couchliteos is a 404.
        missing = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        opener = mock.Mock(side_effect=[missing, Response({"tag_name": "v0.2.2"})])
        self.assertEqual(update.fetch_latest("0.2.1", opener=opener), "0.2.2")
        urls = [call.args[0].full_url for call in opener.call_args_list]
        self.assertEqual(urls, [
            "https://api.github.com/repos/Dudiebug/couchliteos/releases/latest",
            "https://api.github.com/repos/Dudiebug/moonlightos/releases/latest",  # rename:keep
        ])

    def test_other_http_errors_do_not_fall_back(self):
        opener = mock.Mock(side_effect=urllib.error.HTTPError("u", 500, "Server Error", {}, None))
        with self.assertRaises(update.UpdateError):
            update.fetch_latest("0.2.1", opener=opener)
        self.assertEqual(opener.call_count, 1)

    def test_no_repository_answering_is_an_update_error(self):
        missing = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with self.assertRaises(update.UpdateError):
            update.fetch_latest("0.2.1", opener=mock.Mock(side_effect=[missing, missing]))

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
            "UPDATE AVAILABLE: 0.1.14 — github.com/Dudiebug/couchliteos/releases",
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


ROUTE4_HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
ZERO6 = "0" * 32


def route4(*rows):
    return ROUTE4_HEADER + "".join(
        f"{iface}\t{dest}\t{gateway}\t{flags}\t0\t0\t100\t{mask}\t0\t0\t0\n"
        for iface, dest, gateway, flags, mask in rows
    )


def route6(*rows):
    return "".join(
        f"{dest} {plen} {ZERO6} 00 {gateway} 00000400 00000001 00000000 {flags} {iface}\n"
        for dest, plen, gateway, flags, iface in rows
    )


class RouteTest(unittest.TestCase):
    """The update check may only run when this machine has a way out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.v4 = pathlib.Path(self._tmp.name) / "route"
        self.v6 = pathlib.Path(self._tmp.name) / "ipv6_route"

    def has_route(self, v4=None, v6=None):
        if v4 is not None:
            self.v4.write_text(v4)
        if v6 is not None:
            self.v6.write_text(v6)
        return update.has_default_route(self.v4, self.v6)

    def test_ipv4_default_route_counts(self):
        self.assertTrue(self.has_route(route4(("eth0", "00000000", "0202000A", "0003", "00000000"))))

    def test_only_subnet_and_link_local_routes_is_offline(self):
        self.assertFalse(self.has_route(route4(
            ("eth0", "0002000A", "00000000", "0001", "00FFFFFF"),
            ("eth0", "0000FEA9", "00000000", "0001", "0000FFFF"),
        )))

    def test_default_route_that_is_not_up_is_ignored(self):
        self.assertFalse(self.has_route(route4(("eth0", "00000000", "0202000A", "0002", "00000000"))))

    def test_blackhole_default_route_is_ignored(self):
        self.assertFalse(self.has_route(route4(("eth0", "00000000", "00000000", "0201", "00000000"))))

    def test_ipv6_only_default_route_counts(self):
        self.assertTrue(self.has_route(
            route4(), route6((ZERO6, "00", "fe80000000000000021122fffe334455", "00000003", "wlan0"))
        ))

    def test_ipv6_unreachable_default_on_loopback_is_ignored(self):
        self.assertFalse(self.has_route(route4(), route6((ZERO6, "00", ZERO6, "00200200", "lo"))))

    def test_ipv6_non_default_routes_are_ignored(self):
        self.assertFalse(self.has_route(route4(), route6(
            ("fe800000000000000000000000000000", "40", ZERO6, "00000001", "wlan0"),
        )))

    def test_missing_or_garbage_files_mean_offline(self):
        self.assertFalse(update.has_default_route(self.v4, self.v6))
        self.assertFalse(self.has_route("garbage\nmore garbage\n", "x y z\n"))


class CheckerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = pathlib.Path(self._tmp.name) / "update-check.ini"
        self.now = NOW
        self.online = True

    def checker(self, fetch):
        return update.Checker(
            current="0.1.13", state_path=self.path, fetch=fetch, clock=lambda: self.now,
            online=lambda: self.online,
        )

    def test_check_records_the_result_and_shows_a_notice(self):
        fetch = mock.Mock(return_value="0.1.14")
        checker = self.checker(fetch)
        checker.check_if_due()
        fetch.assert_called_once_with("0.1.13")
        self.assertIn("0.1.14", checker.notice())
        self.assertEqual(update.load_state(self.path).latest, "0.1.14")

    def test_every_start_checks_even_a_few_minutes_after_the_last_check(self):
        fetch = mock.Mock(return_value="0.1.14")
        self.checker(fetch).check_if_due()
        self.now += 5 * 60
        self.checker(fetch).check_if_due()  # a reboot: a new launcher process reads the saved state
        self.assertEqual(fetch.call_count, 2)
        self.now += 3600
        self.checker(fetch).check_if_due()
        self.assertEqual(fetch.call_count, 3)

    def test_a_launcher_restarting_within_a_minute_does_not_ask_again(self):
        fetch = mock.Mock(return_value="0.1.14")
        self.checker(fetch).check_if_due()
        self.now += 20
        self.checker(fetch).check_if_due()
        self.assertEqual(fetch.call_count, 1)

    def test_while_running_it_asks_again_after_six_hours_only(self):
        fetch = mock.Mock(return_value="0.1.14")
        checker = self.checker(fetch)
        checker.check_if_due()
        self.now += update.CHECK_SECONDS - 1
        checker.check_if_due()
        self.assertEqual(fetch.call_count, 1)
        self.now += 1
        checker.check_if_due()
        self.assertEqual(fetch.call_count, 2)

    def test_a_failed_start_check_retries_after_ten_minutes_even_if_the_last_check_was_recent(self):
        self.checker(mock.Mock(return_value="0.1.14")).check_if_due()
        self.now += 3600  # rebooted an hour later; the network is flaky
        fetch = mock.Mock(side_effect=OSError("no route"))
        checker = self.checker(fetch)
        checker.check_if_due()
        self.assertEqual(fetch.call_count, 1)
        self.now += update.RETRY_SECONDS - 1
        checker.check_if_due()
        self.assertEqual(fetch.call_count, 1)
        self.now += 1
        fetch.side_effect = None
        fetch.return_value = "0.1.15"
        checker.check_if_due()
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(checker.available(), "0.1.15")

    def test_available_and_to_offer(self):
        checker = self.checker(mock.Mock(return_value="0.1.14"))
        self.assertEqual((checker.available(), checker.to_offer()), ("", ""))
        checker.check_if_due()
        self.assertEqual((checker.available(), checker.to_offer()), ("0.1.14", "0.1.14"))
        checker.mark_offered("0.1.14")
        self.assertEqual((checker.available(), checker.to_offer()), ("0.1.14", ""))
        self.assertEqual(update.load_state(self.path).prompted, "0.1.14")
        self.assertEqual(self.checker(mock.Mock()).to_offer(), "", "a restart does not ask again")

    def test_a_newer_release_is_offered_again(self):
        checker = self.checker(mock.Mock(return_value="0.1.14"))
        checker.check_if_due()
        checker.mark_offered("0.1.14")
        self.now += update.CHECK_SECONDS
        checker.fetch = mock.Mock(return_value="0.1.15")
        checker.check_if_due()
        self.assertEqual(checker.to_offer(), "0.1.15")

    def test_nothing_is_offered_when_up_to_date_disabled_or_live(self):
        up_to_date = self.checker(mock.Mock(return_value="0.1.13"))
        up_to_date.check_if_due()
        self.assertEqual(up_to_date.to_offer(), "")
        disabled = self.checker(mock.Mock(return_value="0.1.14"))
        disabled.check_if_due()
        disabled.set_enabled(False)
        self.assertEqual((disabled.available(), disabled.to_offer()), ("", ""))
        self.path.unlink()  # the disabled checker saved "off"
        live = update.Checker(
            current="0.1.13", state_path=self.path, fetch=mock.Mock(return_value="0.1.14"),
            clock=lambda: self.now, online=lambda: True, live=lambda: True,
        )
        live.check_if_due()
        self.assertEqual((live.available(), live.to_offer()), ("0.1.14", ""))

    def test_a_check_made_elsewhere_is_recorded_and_counts_as_this_runs_check(self):
        fetch = mock.Mock(return_value="0.1.14")
        checker = self.checker(fetch)
        checker.record("0.1.14")
        self.assertEqual(checker.available(), "0.1.14")
        state = update.load_state(self.path)
        self.assertEqual((state.latest, state.checked_at), ("0.1.14", NOW))
        self.assertEqual(checker.to_offer(), "", "the owner saw it in SOFTWARE UPDATE: Home does not ask again")
        self.assertEqual(state.prompted, "0.1.14")
        checker.check_if_due()
        fetch.assert_not_called()
        checker.record("nightly")  # junk is ignored
        self.assertEqual(checker.available(), "0.1.14")

    def test_failures_are_silent_and_keep_the_previous_notice(self):
        self.checker(mock.Mock(return_value="0.1.14")).check_if_due()
        self.now += 2 * DAY
        checker = self.checker(mock.Mock(side_effect=update.UpdateError("offline")))
        checker.check_if_due()  # must not raise
        self.assertIn("0.1.14", checker.notice())

    def test_no_network_route_means_no_request_and_the_check_stays_due(self):
        fetch = mock.Mock(return_value="0.1.14")
        self.online = False
        checker = self.checker(fetch)
        checker.check_if_due()
        fetch.assert_not_called()
        self.assertEqual(checker.notice(), "")
        self.assertFalse(self.path.exists(), "an offline boot must not write or consume the daily check")
        self.now += 30  # the network comes up shortly afterwards
        self.online = True
        checker.check_if_due()
        fetch.assert_called_once_with("0.1.13")
        self.assertIn("0.1.14", checker.notice())

    def test_offline_keeps_a_notice_that_was_found_earlier(self):
        self.checker(mock.Mock(return_value="0.1.14")).check_if_due()
        self.now += 2 * DAY
        self.online = False
        checker = self.checker(mock.Mock())
        checker.check_if_due()
        self.assertIn("0.1.14", checker.notice())

    def test_the_default_gate_reads_the_routing_tables(self):
        self.assertIs(update.Checker(current="1.0.0", state_path=self.path).online, update.has_default_route)

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
            fetch=mock.Mock(return_value="0.1.14"), clock=lambda: NOW, online=lambda: True,
        )
        checker.check_if_due()
        self.assertIn("0.1.14", checker.notice())


class LauncherUpdateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
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
            launcher.updates = update.Checker(
                current="0.1.13", state_path=path, fetch=mock.Mock(), clock=lambda: NOW, live=lambda: True)
        launcher.controllers = self.module.controllers.Monitor(reader=lambda: [])
        return launcher

    def test_home_shows_the_update_notice(self):
        launcher = self.launcher(update.State(latest="0.1.14", checked_at=NOW))
        texts = [text for text, _attr in launcher.footer_lines()]
        self.assertEqual(texts, ["UPDATE AVAILABLE: 0.1.14 — github.com/Dudiebug/couchliteos/releases"])

    def settings_label(self, launcher):
        for index, (label, action) in enumerate(launcher.menu):
            if action == "settings":
                return launcher.menu[index][0]
        raise AssertionError("no SETTINGS row")

    def drawn_rows(self, launcher):
        rows = []
        with mock.patch.object(self.module, "add_centered", side_effect=lambda _s, _r, text, *_a: rows.append(text)):
            with mock.patch.object(self.module, "draw_border"), mock.patch.object(self.module.listview, "draw_rows") as draw:
                launcher.draw()
        return draw.call_args[0][1], rows

    def test_the_settings_row_says_when_an_update_is_available(self):
        launcher = self.launcher(update.State(latest="0.1.14", checked_at=NOW))
        labels, _rows = self.drawn_rows(launcher)
        self.assertEqual([label for label in labels if "UPDATE" in label], ["SETTINGS  -  UPDATE AVAILABLE"])
        self.assertEqual(self.settings_label(launcher), "SETTINGS", "only the drawn text changes, never the row")
        labels, _rows = self.drawn_rows(self.launcher(update.State(latest="0.1.13", checked_at=NOW)))
        self.assertIn("SETTINGS", labels)
        self.assertFalse([label for label in labels if "UPDATE" in label])

    def test_the_software_update_row_names_the_new_version(self):
        launcher = self.launcher(update.State(latest="0.1.14", checked_at=NOW))
        settings = self.module.Settings(launcher.screen, launcher)
        self.assertEqual(settings.menu_label("SOFTWARE UPDATE"), "SOFTWARE UPDATE  -  0.1.14 AVAILABLE")
        quiet = self.module.Settings(launcher.screen, self.launcher(update.State(latest="0.1.13", checked_at=NOW)))
        self.assertEqual(quiet.menu_label("SOFTWARE UPDATE"), "SOFTWARE UPDATE")
        self.assertLessEqual(len("SOFTWARE UPDATE  -  0.10.123 AVAILABLE"), 76)

    def offering(self, state, *, present=(), running=False, home=False, answer=False, guard=None, confirm_effect=None):
        """Run offer_update() twice on an idle Home; `present` names the /run/couchliteos markers that exist."""
        launcher = self.launcher(state)
        launcher.updates.installed = True
        launcher.draw = mock.Mock()
        run = mock.MagicMock()
        run.__truediv__.side_effect = lambda name: mock.Mock(**{"exists.return_value": name in present})
        home_request = mock.Mock()
        home_request.exists.return_value = home
        with tempfile.TemporaryDirectory() as directory:
            launcher.updates.state_path = pathlib.Path(directory) / "update-check.ini"
            patches = [
                mock.patch.object(self.module, "RUN", run),
                mock.patch.object(self.module, "HOME_REQUEST", home_request),
                mock.patch.object(self.module.Launcher, "any_app_running", return_value=running),
                mock.patch.object(self.module, "IDLE_GUARD", guard),
            ]
            confirm_patch = mock.patch.object(
                self.module.confirmation, "confirm", return_value=answer, side_effect=confirm_effect)
            settings_patch = mock.patch.object(self.module, "Settings")
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                confirm = stack.enter_context(confirm_patch)
                settings = stack.enter_context(settings_patch)
                launcher.offer_update()
                launcher.offer_update()  # a second idle pass must not ask again
        return launcher, confirm, settings

    def test_a_new_release_is_offered_once_on_an_idle_home_screen(self):
        launcher, confirm, settings = self.offering(update.State(latest="0.1.14", checked_at=NOW))
        confirm.assert_called_once()
        self.assertIn("0.1.14", confirm.call_args[0][1])
        self.assertIn("SOFTWARE UPDATE", confirm.call_args[0][1])
        settings.assert_not_called()  # answered NO: later
        self.assertEqual(launcher.updates.to_offer(), "")
        self.assertEqual(launcher.updates.available(), "0.1.14", "the footer and rows keep saying so")
        launcher.draw.assert_called()  # the question's screen is repainted away

    def test_yes_opens_software_update(self):
        launcher, confirm, settings = self.offering(update.State(latest="0.1.14", checked_at=NOW), answer=True)
        confirm.assert_called_once()
        settings.return_value.run_software_update.assert_called_once_with()
        launcher.draw.assert_called()

    def test_the_release_is_marked_offered_before_the_question_is_asked(self):
        seen = []

        def question(_screen, _text):
            seen.append(launcher_box[0].updates.to_offer())
            raise RuntimeError("crash while the question is up")

        launcher_box = []
        original = self.launcher

        def remember(state):
            launcher_box.append(original(state))
            return launcher_box[0]

        with mock.patch.object(self, "launcher", remember), self.assertRaises(RuntimeError):
            self.offering(update.State(latest="0.1.14", checked_at=NOW), confirm_effect=question)
        self.assertEqual(seen, [""], "a crash cannot make the question come back")

    def test_nothing_is_offered_over_an_app_a_stream_or_a_home_request(self):
        state = update.State(latest="0.1.14", checked_at=NOW)
        for label, options in (
            ("app marker", {"present": ("app-active",)}),  # a stream or any app owns the screen
            ("an app showing signs of life", {"running": True}),
            ("a Home request", {"home": True}),
            ("the keyboard is up", {"present": ("osk-active",)}),
            ("the keyboard is starting", {"present": ("start-osk",)}),
        ):
            with self.subTest(label):
                launcher, confirm, _settings = self.offering(state, **options)
                confirm.assert_not_called()
                self.assertEqual(launcher.updates.to_offer(), "0.1.14", "not used up: asked when Home is idle")

    def test_the_question_counts_as_activity_and_waits_for_a_blank_screen_to_wake(self):
        state = update.State(latest="0.1.14", checked_at=NOW)
        awake = mock.Mock()
        awake.timer.blanked = False
        _launcher, confirm, _settings = self.offering(state, guard=awake)
        confirm.assert_called_once()
        awake.keep_awake.assert_called_once_with()  # once: the second pass has nothing to ask
        blank = mock.Mock()
        blank.timer.blanked = True
        launcher, confirm, _settings = self.offering(state, guard=blank)
        confirm.assert_not_called()
        blank.keep_awake.assert_not_called()
        self.assertEqual(launcher.updates.to_offer(), "0.1.14", "not used up: asked when someone is looking")

    def test_nothing_is_offered_when_up_to_date(self):
        _launcher, confirm, _settings = self.offering(update.State(latest="0.1.13", checked_at=NOW))
        confirm.assert_not_called()

    def test_the_main_loop_offers_only_when_idle(self):
        source = pathlib.Path(__file__).with_name("couchliteos-launcher.py").read_text(encoding="utf-8")
        self.assertRegex(source, r"if key == -1:[^\n]*\n\s+self\.offer_update\(\)")

    def test_software_update_screen_reports_its_check_to_home(self):
        launcher = self.launcher(update.State())
        settings = self.module.Settings(launcher.screen, launcher)
        with mock.patch.object(self.module.softwareupdate, "show") as show:
            settings.run_software_update()
        self.assertEqual(show.call_args.kwargs["record"], launcher.updates.record)

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

    def test_turning_it_on_without_a_network_says_it_waits(self):
        launcher = self.launcher(update.State(enabled=False))
        settings = self.module.Settings(launcher.screen, launcher)
        with tempfile.TemporaryDirectory() as directory:
            launcher.updates.state_path = pathlib.Path(directory) / "update-check.ini"
            launcher.updates.online = lambda: False
            settings.toggle_updates()
            self.assertIn("WAITS", settings.status)
            self.assertIn("ONLINE", settings.status)
            launcher.updates.set_enabled(False)
            launcher.updates.online = lambda: True
            settings.toggle_updates()
            self.assertNotIn("WAITS", settings.status)


RELEASE_JSON = {
    "tag_name": "v0.2.2",
    "assets": [
        {"name": "couchliteos-0.2.2-amd64.iso", "size": 1_500_000_000,
         "browser_download_url": "https://github.com/Dudiebug/couchliteos/releases/download/v0.2.2/couchliteos-0.2.2-amd64.iso"},
        {"name": "couchliteos-0.2.2-nvidia-amd64.iso", "size": 1_900_000_000,
         "browser_download_url": "https://github.com/Dudiebug/couchliteos/releases/download/v0.2.2/couchliteos-0.2.2-nvidia-amd64.iso"},
        {"name": "SHA256SUMS", "size": 200,
         "browser_download_url": "https://github.com/Dudiebug/couchliteos/releases/download/v0.2.2/SHA256SUMS"},
        {"name": "broken"},  # no url or size: ignored
        "not an object",
    ],
}


class FetchReleaseTest(unittest.TestCase):
    def test_release_lists_its_assets_and_drops_the_v(self):
        release = update.fetch_release("0.2.1", opener=mock.Mock(return_value=Response(RELEASE_JSON)))
        self.assertEqual(release.version, "0.2.2")
        self.assertEqual([asset.name for asset in release.assets], [
            "couchliteos-0.2.2-amd64.iso", "couchliteos-0.2.2-nvidia-amd64.iso", "SHA256SUMS",
        ])
        self.assertEqual(release.assets[0].size, 1_500_000_000)
        self.assertTrue(release.assets[0].url.startswith("https://github.com/"))

    def test_release_without_assets_is_an_empty_release(self):
        release = update.fetch_release("0.2.1", opener=mock.Mock(return_value=Response({"tag_name": "0.2.2"})))
        self.assertEqual(release, update.Release("0.2.2", ()))

    def test_timeout_is_a_parameter_and_the_new_repository_falls_back_like_fetch_latest(self):
        missing = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        opener = mock.Mock(side_effect=[missing, Response(RELEASE_JSON)])
        self.assertEqual(update.fetch_release("0.2.1", opener=opener, timeout=10).version, "0.2.2")
        self.assertEqual(opener.call_args.kwargs["timeout"], 10)

    def test_bad_answers_raise(self):
        for body in ({"tag_name": "nightly"}, b"not json", b"[]"):
            with self.assertRaises(update.UpdateError, msg=body):
                update.fetch_release("0.2.1", opener=mock.Mock(return_value=Response(body)))


class AssetSelectionTest(unittest.TestCase):
    def setUp(self):
        self.release = update.fetch_release("0.2.1", opener=mock.Mock(return_value=Response(RELEASE_JSON)))

    def test_iso_names_follow_build_sh(self):
        self.assertEqual(update.iso_name("0.2.2", ""), "couchliteos-0.2.2-amd64.iso")
        self.assertEqual(update.iso_name("0.2.2", "nvidia"), "couchliteos-0.2.2-nvidia-amd64.iso")

    def test_general_profile_picks_the_plain_iso_and_nvidia_its_own(self):
        self.assertEqual(update.pick_iso(self.release, "").name, "couchliteos-0.2.2-amd64.iso")
        self.assertEqual(update.pick_iso(self.release, "nvidia").name, "couchliteos-0.2.2-nvidia-amd64.iso")

    def test_a_profile_without_an_asset_is_an_error_not_a_guess(self):
        with self.assertRaises(update.UpdateError):
            update.pick_iso(self.release, "intel")

    def test_sums_asset(self):
        self.assertEqual(update.sums_asset(self.release).name, "SHA256SUMS")
        with self.assertRaises(update.UpdateError):
            update.sums_asset(update.Release("0.2.2", ()))


class ProfileAndLiveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)

    def test_profile_values_are_read_and_unquoted(self):
        path = self.tmp / "profile.conf"
        path.write_text(
            "# comment\nPROFILE_NAME=nvidia\nISO_SUFFIX=nvidia\nPROFILE_DESCRIPTION=\"NVIDIA: GTX 900\"\n"
            "LB_EXTRA_CONFIG='--firmware-binary false'\nEMPTY=\n",
            encoding="utf-8",
        )
        values = update.read_profile(path)
        self.assertEqual(values["PROFILE_NAME"], "nvidia")
        self.assertEqual(values["ISO_SUFFIX"], "nvidia")
        self.assertEqual(values["PROFILE_DESCRIPTION"], "NVIDIA: GTX 900")
        self.assertEqual(values["EMPTY"], "")

    def test_missing_profile_is_empty(self):
        self.assertEqual(update.read_profile(self.tmp / "none"), {})

    def test_live_when_the_medium_is_mounted_or_boot_live_is_on_the_command_line(self):
        cmdline = self.tmp / "cmdline"
        cmdline.write_text("BOOT_IMAGE=/vmlinuz root=/dev/sda2 quiet\n")
        self.assertFalse(update.is_live(self.tmp / "no-medium", cmdline))
        (self.tmp / "medium").mkdir()
        self.assertTrue(update.is_live(self.tmp / "medium", cmdline))
        cmdline.write_text("BOOT_IMAGE=/live/vmlinuz boot=live components quiet\n")
        self.assertTrue(update.is_live(self.tmp / "no-medium", cmdline))

    def test_no_cmdline_and_no_medium_is_not_live(self):
        self.assertFalse(update.is_live(self.tmp / "no-medium", self.tmp / "no-cmdline"))

    def test_a_word_that_merely_contains_boot_live_is_not_live(self):
        cmdline = self.tmp / "cmdline"
        cmdline.write_text("root=/dev/sda2 foo=boot=livecd\n")
        self.assertFalse(update.is_live(self.tmp / "no-medium", cmdline))


class InstalledNoticeTest(unittest.TestCase):
    def test_installed_box_points_at_software_update(self):
        state = update.State(latest="0.2.2", checked_at=NOW)
        text = update.notice(state, "0.2.1", installed=True)
        self.assertEqual(text, "COUCHLITEOS 0.2.2 IS AVAILABLE: SETTINGS > SOFTWARE UPDATE")
        self.assertLessEqual(len(text), 76)  # fits the 80-column home screen

    def test_live_keeps_the_releases_address(self):
        state = update.State(latest="0.2.2", checked_at=NOW)
        self.assertIn(update.RELEASES_TEXT, update.notice(state, "0.2.1", installed=False))

    def test_checker_chooses_the_text_from_live_detection_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "u.ini"
            update.save_state(update.State(latest="0.2.2", checked_at=NOW, attempted_at=NOW), path)
            installed = update.Checker("0.2.1", path, live=lambda: False)
            live = update.Checker("0.2.1", path, live=lambda: True)
            self.assertIn("SETTINGS > SOFTWARE UPDATE", installed.notice())
            self.assertIn(update.RELEASES_TEXT, live.notice())


if __name__ == "__main__":
    unittest.main()
