"""Settings > SOFTWARE UPDATE: every screen, with a fake screen, keys, clock and update service."""

import testenv  # noqa: F401  (first: scratch run and state directories)
import curses
import importlib.util
import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

import couchliteos_softwareupdate as su
import couchliteos_update as update

ENTER, ESC, DOWN, UP = 10, 27, curses.KEY_DOWN, curses.KEY_UP
BASE = "https://github.com/Dudiebug/couchliteos/releases/download/v0.2.2/"


def release(version="0.2.2", *, with_iso=True, size=1_900_000_000):
    assets = [update.Asset("SHA256SUMS", BASE + "SHA256SUMS", 200)]
    if with_iso:
        name = f"couchliteos-{version}-amd64.iso"
        assets.append(update.Asset(name, BASE + name, size))
    return update.Release(version, tuple(assets))


class Done(Exception):
    """Raised by the script to end a screen that would otherwise wait forever (RESTARTING...)."""


class TextScreen:
    """A curses window that remembers what was drawn and refuses text that would not fit."""

    def __init__(self, height=24, width=80):
        self.size = (height, width)
        self.rows = {}
        self.frames = []
        self.timeouts = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.rows = {}

    def border(self, *_args):
        pass

    def refresh(self):
        self.frames.append(self.text())

    def timeout(self, milliseconds):
        self.timeouts.append(milliseconds)

    def addstr(self, row, column, text, *_attr):
        self.put(row, column, text)

    def addnstr(self, row, column, text, count, *_attr):
        self.put(row, column, text[:count])

    def put(self, row, column, text):
        height, width = self.size
        assert 0 <= row <= height - 2, f"row {row} is off the screen: {text!r}"  # the last row is the border
        assert 1 <= column and column + len(text) <= width - 1, f"{text!r} runs off the screen at column {column}"
        self.rows[row] = self.rows.get(row, "").ljust(column) + text

    def text(self):
        return "\n".join(self.rows[row].rstrip() for row in sorted(self.rows) if self.rows[row].strip())

    def line_with(self, needle):
        return next(line.strip() for line in self.text().splitlines() if needle in line)


def flat(text):
    """Text with line breaks and runs of blanks as single spaces: wrapping must not matter."""
    return " ".join(text.split())


class UiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.screen = TextScreen()
        self.now = 1000.0
        self.script = []
        self.awake = 0
        self.questions = []
        self.answer = True
        self.apps = False
        self.fetches = []
        self.fetch_result = release()

    # -- fakes ---------------------------------------------------------------------------------

    def read_key(self, _screen):
        self.now += 0.5  # every wait for a key is the 0.5 s the screen polls at
        if not self.script:
            raise AssertionError("the screen asked for more keys than the test scripted")
        item = self.script.pop(0)
        if callable(item):
            item()
            return -1
        return -1 if item is None else item

    def confirm(self, _screen, question):
        self.questions.append(question)
        return self.answer

    def fetch(self, current):
        self.fetches.append((current, self.screen.text()))
        if isinstance(self.fetch_result, Exception):
            raise self.fetch_result
        return self.fetch_result

    def make(self, **overrides):
        values = dict(
            read_key=self.read_key, confirm=self.confirm, apps_running=lambda: self.apps,
            keep_awake=self.keep_awake, current="0.2.1", profile={"PROFILE_NAME": "general", "ISO_SUFFIX": ""},
            live=False, fetch=self.fetch, run_dir=self.tmp, clock=lambda: self.now,
        )
        values.update(overrides)
        return su.SoftwareUpdate(self.screen, **values)

    def keep_awake(self):
        self.awake += 1

    def service(self, phase, percent=None, message="", version="0.2.2"):
        """What couchliteos-update.service writes, as a script item."""
        def write():
            (self.tmp / "update-status.json").write_text(json.dumps(
                {"phase": phase, "percent": percent, "message": message, "version": version}))
        return write

    def frames_with(self, needle):
        return [frame for frame in self.screen.frames if needle in flat(frame)]

    def install_script(self, *after):
        """Keys that check, pick INSTALL UPDATE and answer yes, then what the service does."""
        return [ENTER, ENTER, *after]


class MainScreenTest(UiTest):
    def test_the_screen_names_this_box_and_its_profile(self):
        self.script = [ESC]
        self.make().run()
        self.assertIn("SOFTWARE UPDATE", self.screen.frames[0])
        self.assertIn("THIS BOX: COUCHLITEOS 0.2.1 (GENERAL)", self.screen.frames[0])

    def test_an_installed_box_offers_check_and_back(self):
        self.script = [ESC]
        self.make().run()
        frame = self.screen.frames[0]
        self.assertIn("CHECK FOR UPDATES", frame)
        self.assertIn("BACK", frame)
        self.assertNotIn("INSTALL UPDATE", frame)
        self.assertTrue(self.screen.line_with("CHECK FOR UPDATES").startswith(">"))

    def test_live_boot_explains_why_and_offers_only_back(self):
        self.script = [DOWN, ENTER]
        self.make(live=True).run()
        frame = self.screen.frames[0]
        self.assertIn(
            "UPDATES INSTALL ON A BOX THAT RUNS FROM ITS DISK. ON A USB STICK: WRITE THE NEW ISO TO THE ISO STICK "
            "AND KEEP THE PERSISTENCE STICK.", flat(frame))
        self.assertIn(update.RELEASES_TEXT, frame)
        self.assertNotIn("CHECK FOR UPDATES", frame)
        self.assertEqual(self.fetches, [])
        self.assertTrue(self.screen.line_with("BACK").startswith(">"))

    def test_b_goes_back_from_every_screen_state(self):
        for live in (False, True):
            self.script = [ESC]
            self.make(live=live).run()
            self.assertEqual(self.script, [])

    def test_the_main_screen_fits_80x24_in_its_richest_state(self):
        # TextScreen raises when a line does not fit.
        self.script = [ENTER, DOWN, UP, ESC]
        self.make().run()
        self.assertTrue(self.screen.frames)


class CheckTest(UiTest):
    def test_check_draws_checking_first_and_looks_up_this_version(self):
        self.script = [ENTER, ESC]
        self.make().run()
        current, shown = self.fetches[0]
        self.assertEqual(current, "0.2.1")
        self.assertIn("CHECKING...", shown)

    def test_a_newer_release_is_announced_and_install_is_offered_first(self):
        self.script = [ENTER, ESC]
        self.make().run()
        frame = self.screen.frames[-1]
        self.assertIn("COUCHLITEOS 0.2.2 IS AVAILABLE", frame)
        self.assertNotIn("CHECKING...", frame)
        self.assertTrue(self.screen.line_with("INSTALL UPDATE").startswith(">"))
        self.assertIn("CHECK FOR UPDATES", frame)

    def test_the_same_or_an_older_release_is_the_newest_version(self):
        for latest in ("0.2.1", "0.2.0"):
            self.screen = TextScreen()
            self.fetch_result = release(latest)
            self.script = [ENTER, ESC]
            self.make().run()
            self.assertIn("THIS IS THE NEWEST VERSION", self.screen.frames[-1])
            self.assertNotIn("INSTALL UPDATE", self.screen.frames[-1])

    def test_a_failed_lookup_points_at_the_network_settings(self):
        for error in (update.UpdateError("offline"), OSError("unreachable"), ValueError("odd")):
            self.screen = TextScreen()
            self.fetch_result = error
            self.script = [ENTER, ESC]
            self.make().run()
            self.assertIn("COULD NOT REACH GITHUB: CHECK SETTINGS > NETWORK", self.screen.frames[-1])
            self.assertNotIn("INSTALL UPDATE", self.screen.frames[-1])

    def test_a_release_without_a_file_for_this_box_is_not_offered(self):
        self.fetch_result = release(with_iso=False)
        self.script = [ENTER, ESC]
        self.make().run()
        self.assertIn("THIS RELEASE HAS NO FILE FOR THIS BOX", self.screen.frames[-1])
        self.assertNotIn("INSTALL UPDATE", self.screen.frames[-1])

    def test_the_profile_picks_which_file_is_needed(self):
        self.fetch_result = release()  # only the general ISO
        self.script = [ENTER, ESC]
        self.make(profile={"PROFILE_NAME": "nvidia", "ISO_SUFFIX": "nvidia"}).run()
        self.assertIn("THIS RELEASE HAS NO FILE FOR THIS BOX", self.screen.frames[-1])
        self.assertIn("(NVIDIA)", self.screen.frames[-1])

    def test_a_successful_check_tells_the_home_notice_what_it_found(self):
        recorded = []
        for latest in ("0.2.2", "0.2.1"):  # newer, and the same: Home learns both
            self.fetch_result = release(latest)
            self.script = [ENTER, ESC]
            self.make(record=recorded.append).run()
        self.assertEqual(recorded, ["0.2.2", "0.2.1"])

    def test_a_failed_check_records_nothing(self):
        recorded = []
        self.fetch_result = update.UpdateError("offline")
        self.script = [ENTER, ESC]
        self.make(record=recorded.append).run()
        self.assertEqual(recorded, [])

    def test_a_recorder_that_fails_does_not_spoil_the_answer(self):
        def broken(_version):
            raise OSError("disk full")

        self.script = [ENTER, ESC]
        self.make(record=broken).run()
        self.assertIn("COUCHLITEOS 0.2.2 IS AVAILABLE", self.screen.frames[-1])
        self.assertTrue(self.screen.line_with("INSTALL UPDATE").startswith(">"))

    def test_the_default_lookup_gives_github_ten_seconds(self):
        with mock.patch.object(update, "fetch_release", return_value=release()) as fetch:
            self.assertEqual(su.default_fetch("0.2.1"), release())
        fetch.assert_called_once_with("0.2.1", timeout=10)


class InstallTest(UiTest):
    def test_running_apps_block_the_install_before_any_question(self):
        self.apps = True
        self.script = [ENTER, ENTER, ESC]
        self.make().run()
        self.assertEqual(self.questions, [])
        self.assertIn("CLOSE RUNNING APPS FIRST", self.screen.frames[-1])
        self.assertFalse((self.tmp / "update-install").exists())

    def test_the_confirmation_says_what_will_happen(self):
        self.answer = False
        self.script = [ENTER, ENTER, ESC]
        self.make().run()
        question = flat(self.questions[0])
        self.assertIn("COUCHLITEOS 0.2.2", question)
        self.assertIn("DOWNLOADS ABOUT 2 GB, INSTALLS IT, THEN RESTARTS.", question)
        self.assertIn("PAIRINGS, WI-FI, BLUETOOTH AND SETTINGS ARE KEPT.", question)
        self.assertIn("KEEP THE BOX PLUGGED IN UNTIL IT RESTARTS.", question)

    def test_declining_changes_nothing_and_keeps_install_on_offer(self):
        self.answer = False
        self.script = [ENTER, ENTER, ESC]
        self.make().run()
        self.assertFalse((self.tmp / "update-install").exists())
        self.assertIn("INSTALL UPDATE", self.screen.frames[-1])

    def test_yes_asks_the_service_to_install_and_drops_an_old_status(self):
        (self.tmp / "update-status.json").write_text(json.dumps({"phase": "failed", "message": "OLD"}))
        seen = {}

        def at_first_progress_read():
            seen["request"] = (self.tmp / "update-install").exists()
            seen["old_status"] = (self.tmp / "update-status.json").exists()
            raise Done

        self.script = self.install_script(at_first_progress_read)
        with self.assertRaises(Done):
            self.make().run()
        self.assertEqual(seen, {"request": True, "old_status": False})

    def test_a_request_that_cannot_be_written_is_reported(self):
        self.script = self.install_script(ESC)
        ui = self.make(run_dir=self.tmp / "missing" / "dir")
        ui.run()
        self.assertIn("COULD NOT START THE UPDATE", self.screen.frames[-1])

    def test_a_gb_figure_never_rounds_down_to_zero(self):
        self.fetch_result = release(size=300_000_000)
        self.answer = False
        self.script = [ENTER, ENTER, ESC]
        self.make().run()
        self.assertIn("DOWNLOADS ABOUT 1 GB", flat(self.questions[0]))


class ProgressTest(UiTest):
    def run_install(self, *after, expect=Done):
        self.script = self.install_script(*after, self.finish)
        if expect:
            with self.assertRaises(expect):
                self.make().run()
        else:
            self.make().run()

    def finish(self):
        raise Done

    def test_each_phase_shows_its_message_and_a_bar(self):
        self.run_install(
            self.service("checking", None, "CHECKING FOR UPDATES..."),
            self.service("downloading", 40, "DOWNLOADING: 760 OF 1900 MB"),
            self.service("verifying", None, "CHECKING THE DOWNLOAD..."),
            self.service("installing", 50, "COPYING SYSTEM FILES..."),
            self.service("restarting", 100, "RESTARTING..."),
        )
        for text in ("CHECKING FOR UPDATES...", "DOWNLOADING: 760 OF 1900 MB", "CHECKING THE DOWNLOAD...",
                     "COPYING SYSTEM FILES...", "RESTARTING..."):
            self.assertTrue(self.frames_with(text), text)
        downloading = self.frames_with("DOWNLOADING: 760 OF 1900 MB")[-1]
        self.assertIn("40%", downloading)
        self.assertRegex(downloading, r"\[=+ +\]")

    def test_the_version_comes_from_the_status(self):
        # The service looked the release up itself; if a newer one appeared since the check, show that.
        self.run_install(self.service("downloading", 5, "DOWNLOADING: 95 OF 1900 MB", version="0.2.3"))
        self.assertTrue(self.frames_with("COUCHLITEOS 0.2.3"))

    def test_it_polls_twice_a_second_and_keeps_the_screen_awake(self):
        self.run_install(self.service("downloading", 1, "DOWNLOADING"), None, None)
        self.assertIn(500, self.screen.timeouts)
        self.assertGreaterEqual(self.awake, 3)

    def test_the_normal_poll_rate_comes_back_afterwards(self):
        self.run_install(self.service("failed", None, "BOOM"), ENTER, ESC, expect=None)
        self.assertEqual(self.screen.timeouts[-1], 1000)

    def test_b_while_downloading_cancels_and_waits_for_the_service(self):
        self.run_install(
            self.service("downloading", 10, "DOWNLOADING: 190 OF 1900 MB"), ESC,
            lambda: self.assertTrue((self.tmp / "update-cancel").exists()),
            self.service("cancelled", None, "UPDATE CANCELLED"), ESC, expect=None,
        )
        self.assertTrue(self.frames_with("CANCELLING..."))
        self.assertIn("UPDATE CANCELLED", self.screen.frames[-1])
        self.assertIn("INSTALL UPDATE", self.screen.frames[-1], "a retry resumes the partial download")

    def test_b_while_installing_only_asks_to_wait(self):
        self.run_install(self.service("installing", 50, "COPYING SYSTEM FILES..."), ESC, None)
        self.assertTrue(self.frames_with("INSTALLING: PLEASE WAIT"))
        self.assertFalse((self.tmp / "update-cancel").exists())

    def test_b_during_the_checks_around_the_download_is_ignored_too(self):
        self.run_install(self.service("verifying", None, "CHECKING THE DOWNLOAD..."), ESC, None)
        self.assertFalse((self.tmp / "update-cancel").exists())
        self.assertTrue(self.frames_with("PLEASE WAIT"))

    def test_a_failure_shows_the_message_and_waits_for_a(self):
        self.run_install(
            self.service("failed", None, "NOT ENOUGH FREE SPACE: NEED 6 GB"), None, ENTER, ESC, expect=None)
        failed = self.frames_with("NOT ENOUGH FREE SPACE: NEED 6 GB")
        self.assertIn("PRESS A", failed[0])
        self.assertIn("NOT ENOUGH FREE SPACE: NEED 6 GB", self.screen.frames[-1], "the menu keeps the reason")
        self.assertIn("INSTALL UPDATE", self.screen.frames[-1])

    def test_b_also_leaves_a_failure(self):
        self.run_install(self.service("failed", None, "DOWNLOAD FAILED"), ESC, ESC, expect=None)
        self.assertEqual(self.script, [self.finish], "B left the failure and then the menu")

    def test_restarting_ignores_the_buttons(self):
        self.run_install(self.service("restarting", 100, "RESTARTING..."), ESC, ENTER, DOWN)
        self.assertTrue(self.frames_with("RESTARTING..."))

    def test_a_box_that_does_not_restart_says_so(self):
        ticks = [None] * 200
        self.run_install(self.service("restarting", 100, "RESTARTING..."), *ticks[:190], ENTER, ESC, expect=None)
        self.assertTrue(self.frames_with("THE BOX DID NOT RESTART"))

    def test_up_to_date_from_the_service_is_reported(self):
        self.run_install(self.service("uptodate", None, "THIS IS THE NEWEST VERSION"), ESC, expect=None)
        self.assertIn("THIS IS THE NEWEST VERSION", self.screen.frames[-1])
        self.assertNotIn("INSTALL UPDATE", self.screen.frames[-1])

    def test_no_status_within_15_seconds_means_the_service_did_not_start(self):
        self.run_install(*([None] * 29))
        self.assertFalse(self.frames_with("THE UPDATE SERVICE DID NOT START"))

    def test_the_15_second_wait_ends_in_a_failure_screen(self):
        self.run_install(*([None] * 31), ENTER, ESC, expect=None)
        self.assertTrue(self.frames_with("THE UPDATE SERVICE DID NOT START"))
        self.assertIn("PRESS A", self.frames_with("THE UPDATE SERVICE DID NOT START")[0])

    def test_a_status_after_the_wait_is_not_needed_once_one_arrived(self):
        # 15 s is only the wait for the first status; a slow phase later must not trip it.
        self.run_install(self.service("checking", None, "CHECKING FOR UPDATES..."), *([None] * 60))
        self.assertFalse(self.frames_with("DID NOT START"))

    def test_a_half_written_status_is_ignored(self):
        self.run_install(lambda: (self.tmp / "update-status.json").write_text('{"phase": "down'),
                         self.service("installing", 20, "UPDATING SETTINGS FILES..."))
        self.assertTrue(self.frames_with("UPDATING SETTINGS FILES..."))

    def test_unknown_phases_and_odd_values_do_not_crash_the_screen(self):
        self.run_install(
            self.service("future-phase", "lots", ["not", "text"]),
            self.service("downloading", 250, "DOWNLOADING"),
            self.service("downloading", -5, "DOWNLOADING"),
        )

    def test_a_long_message_wraps_inside_the_screen(self):
        message = "INSTALL FAILED WHILE SETTING UP THE BOOT MENU. IF THE BOX WILL NOT START, REINSTALL FROM THE ISO."
        self.run_install(self.service("failed", None, message), ENTER, ESC, expect=None)
        self.assertTrue(self.frames_with(message))


class BarTest(unittest.TestCase):
    def test_the_bar_is_exactly_as_wide_as_asked_and_fills_with_the_percentage(self):
        self.assertEqual(su.bar(0, 12), "[" + " " * 10 + "]")
        self.assertEqual(su.bar(50, 12), "[" + "=" * 5 + " " * 5 + "]")
        self.assertEqual(su.bar(100, 12), "[" + "=" * 10 + "]")

    def test_out_of_range_percentages_are_clamped(self):
        self.assertEqual(su.bar(-20, 12), su.bar(0, 12))
        self.assertEqual(su.bar(400, 12), su.bar(100, 12))

    def test_without_a_percentage_a_segment_moves_along_the_bar(self):
        frames = [su.bar(None, 20, frame) for frame in range(40)]
        self.assertTrue(all(len(frame) == 20 and "=" in frame for frame in frames))
        self.assertGreater(len(set(frames)), 5)


class StatusReadTest(unittest.TestCase):
    def test_only_a_json_object_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "s.json"
            self.assertEqual(su.read_status(path), {})
            for text, expected in (("[1]", {}), ("{bad", {}), ('{"phase": "failed"}', {"phase": "failed"})):
                path.write_text(text)
                self.assertEqual(su.read_status(path), expected)


class FitTest(UiTest):
    def test_every_screen_fits_a_small_terminal(self):
        # 24 rows x 80 columns is the smallest the launcher supports (720p at large text).
        self.script = [ENTER, DOWN, UP, ESC]
        self.make().run()
        self.screen = TextScreen(24, 80)
        self.script = self.install_script(
            self.service("downloading", 99, "DOWNLOADING: 1881 OF 1900 MB"), ESC, self.service("failed", None, "X" * 90),
            ENTER, ESC)
        self.make().run()


class WiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_softwareupdate_under_test", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def settings(self):
        screen = TextScreen()
        launcher = mock.Mock()
        return self.module.Settings(screen, launcher), launcher

    def test_settings_has_a_software_update_row_next_to_the_update_check(self):
        menu = self.module.SETTINGS_MENU
        self.assertEqual(menu.index("SOFTWARE UPDATE") + 1, menu.index("CHECK FOR UPDATES"))

    def test_the_row_opens_the_screen_with_the_launchers_input_and_app_check(self):
        settings, launcher = self.settings()
        settings.selected = self.module.SETTINGS_MENU.index("SOFTWARE UPDATE")
        with mock.patch.object(self.module.softwareupdate, "show") as show:
            self.assertTrue(settings.activate())
        args, kwargs = show.call_args
        self.assertIs(args[0], settings.screen)
        self.assertIs(kwargs["read_key"], self.module.read_key)
        self.assertEqual(kwargs["apps_running"], launcher.apps_running)
        self.assertTrue(callable(kwargs["keep_awake"]))

    def test_a_crash_in_the_screen_is_a_status_line_not_a_dead_launcher(self):
        settings, _launcher = self.settings()
        settings.selected = self.module.SETTINGS_MENU.index("SOFTWARE UPDATE")
        with mock.patch.object(self.module.softwareupdate, "show", side_effect=RuntimeError("boom")):
            settings.activate()
        self.assertEqual(settings.status, "COULD NOT OPEN SOFTWARE UPDATE")


if __name__ == "__main__":
    unittest.main()
