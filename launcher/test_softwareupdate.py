"""Settings > SOFTWARE UPDATE: every screen, with a fake screen, keys, clock and update service."""

import testenv  # noqa: F401  (first: scratch run and state directories)
import curses
import importlib.util
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

# couchliteos_snapshot -> couchliteos_safefile needs Linux-only os names at import (open flags, and os.chown
# as a default argument). No test here writes through them, so where they are missing (Windows) stand-ins
# are enough. Linux has them: no-op.
for _flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
    if not hasattr(os, _flag):
        setattr(os, _flag, 0)
if not hasattr(os, "chown"):
    os.chown = lambda *_args, **_kwargs: None

import couchliteos_liveslot as liveslot  # noqa: E402
import couchliteos_settings as settings  # noqa: E402
import couchliteos_snapshot as snapshot  # noqa: E402
import couchliteos_softwareupdate as su  # noqa: E402
import couchliteos_update as update  # noqa: E402

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
        # Curses would cut the text at count: a line the screen meant to show must already fit.
        assert len(text) <= count, f"{text!r} ({len(text)}) is cut to {count} columns"
        self.put(row, column, text)

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
        self.saved = None
        self.save_on = True
        self.settings = []

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
            saved=lambda: self.saved, save_first=lambda: self.save_on, set_save_first=self.set_save_first,
            state_dir=self.tmp, config=self.tmp / "config.ini", persistent=lambda: True, stick=lambda: {},
        )
        values.update(overrides)
        return su.SoftwareUpdate(self.screen, **values)

    def keep_awake(self):
        self.awake += 1

    def set_save_first(self, on):
        self.settings.append(on)
        self.save_on = on

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


SAVED = snapshot.Snapshot("0.2.0", "2026-10-02T09:00:00Z", 1_812_000_000, "a" * 64, "6.12.48+deb13-amd64")


class SavedVersionTest(UiTest):
    def test_the_saved_version_is_shown_with_its_date_and_size(self):
        self.saved = SAVED
        self.script = [ESC]
        self.make().run()
        self.assertIn("SAVED VERSION: 0.2.0, SAVED 2 OCT 2026, 1.8 GB", self.screen.frames[0])
        self.assertIn("DELETE SAVED VERSION", self.screen.frames[0])

    def test_without_a_saved_version_there_is_nothing_to_delete(self):
        self.script = [ESC]
        self.make().run()
        self.assertIn("SAVED VERSION: NONE", self.screen.frames[0])
        self.assertNotIn("DELETE SAVED VERSION", self.screen.frames[0])

    def test_a_live_boot_shows_no_saved_version_rows(self):
        self.saved = SAVED
        self.script = [ESC]
        self.make(live=True).run()
        for text in ("SAVED VERSION", "SAVE BEFORE UPDATE"):
            self.assertNotIn(text, self.screen.frames[0])

    def test_save_before_update_turns_off_and_on(self):
        self.script = [DOWN, ENTER, ENTER, ESC]
        self.make().run()
        self.assertEqual(self.settings, [False, True])
        self.assertIn("SAVE BEFORE UPDATE: OFF", self.screen.frames[2])
        self.assertIn("UPDATES NOW INSTALL WITHOUT SAVING THE CURRENT VERSION FIRST", flat(self.screen.frames[2]))
        self.assertIn("SAVE BEFORE UPDATE: ON", self.screen.frames[3])

    def test_a_setting_that_cannot_be_written_says_so(self):
        def failing(_on):
            raise PermissionError(13, "Permission denied")

        self.script = [DOWN, ENTER, ESC]
        self.make(set_save_first=failing).run()
        self.assertIn("COULD NOT SAVE THE SETTING", self.screen.frames[-1])

    def test_delete_asks_first_then_asks_the_service(self):
        self.saved = SAVED
        self.script = [UP, UP, UP, ENTER, ESC]
        self.make().run()
        self.assertEqual(len(self.questions), 1)
        self.assertIn("DELETE THE SAVED VERSION (0.2.0)? THE BOX CAN THEN NOT GO BACK TO IT.", self.questions[0])
        self.assertTrue((self.tmp / "snapshot-delete").exists())
        self.assertIn("DELETING THE SAVED VERSION...", self.screen.frames[-1])

    def test_no_to_delete_changes_nothing(self):
        self.saved = SAVED
        self.answer = False
        self.script = [UP, UP, UP, ENTER, ESC]
        self.make().run()
        self.assertFalse((self.tmp / "snapshot-delete").exists())

    def test_the_richest_screen_fits_80x24(self):
        self.saved = SAVED
        self.script = [ENTER, DOWN, DOWN, DOWN, DOWN, ESC]
        self.make().run()
        self.assertIn("INSTALL UPDATE", self.screen.frames[-1])

    def test_saving_is_shown_with_its_percentage_while_the_service_works(self):
        self.script = [ENTER, ENTER, self.service("saving", 40, "SAVING THE CURRENT VERSION... 40%"),
                       self.service("failed", None, "NOT ENOUGH FREE SPACE"), ENTER, ESC]
        self.make().run()
        self.assertTrue(self.frames_with("SAVING THE CURRENT VERSION... 40%"))
        self.assertTrue(self.frames_with("KEEP THE BOX PLUGGED IN"))


class RestoreTest(UiTest):
    ROW = "RESTORE PREVIOUS VERSION (0.2.0, SAVED 2 OCT 2026)"

    def restarting(self):
        return self.service("restarting", 100, "RESTARTING TO RESTORE THE SAVED VERSION...", "0.2.0")

    def test_the_row_names_the_saved_version_and_its_date_next_to_delete(self):
        self.saved = SAVED
        self.script = [ESC]
        screen = self.make()
        screen.run()
        self.assertEqual(screen.rows()[-3:], ["DELETE SAVED VERSION", self.ROW, "BACK"])
        self.assertIn(self.ROW, self.screen.frames[0])
        self.saved = None
        self.assertNotIn(self.ROW, screen.rows())

    def test_restore_warns_that_later_changes_are_lost_then_asks_the_service(self):
        self.saved = SAVED
        self.script = [UP, UP, ENTER, self.restarting(), None]
        with self.assertRaises(AssertionError):  # the box restarts: the screen never comes back
            self.make().run()
        self.assertEqual(len(self.questions), 1)
        self.assertIn("RESTORE COUCHLITEOS 0.2.0, SAVED 2 OCT 2026? EVERYTHING CHANGED SINCE THEN IS LOST",
                      flat(self.questions[0]))
        self.assertTrue((self.tmp / "restore-request").exists())
        self.assertTrue(self.frames_with("RESTORING COUCHLITEOS 0.2.0"))
        self.assertTrue(self.frames_with("RESTARTING..."))

    def test_no_changes_nothing(self):
        self.saved = SAVED
        self.answer = False
        self.script = [UP, UP, ENTER, ESC]
        self.make().run()
        self.assertFalse((self.tmp / "restore-request").exists())

    def test_running_apps_must_be_closed_first(self):
        self.saved = SAVED
        self.apps = True
        self.script = [UP, UP, ENTER, ESC]
        self.make().run()
        self.assertEqual(self.questions, [])
        self.assertFalse((self.tmp / "restore-request").exists())
        self.assertIn("CLOSE RUNNING APPS FIRST", self.screen.frames[-1])

    def test_a_refused_request_is_shown_and_the_update_title_comes_back(self):
        self.saved = SAVED
        self.script = [UP, UP, ENTER,
                       self.service("failed", None, "THERE IS NO SAVED VERSION TO RESTORE, OR IT IS DAMAGED", "0.2.0"),
                       ENTER, ESC]
        screen = self.make()
        screen.run()
        self.assertIn("THERE IS NO SAVED VERSION TO RESTORE", flat(self.screen.frames[-1]))
        self.assertEqual(screen.heading, "UPDATING TO COUCHLITEOS")


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

    def test_a_release_whose_nvidia_iso_is_not_out_yet_says_so(self):
        self.fetch_result = release()  # only the general ISO: the NVIDIA one is published later
        self.script = [ENTER, ESC]
        self.make(profile={"PROFILE_NAME": "nvidia", "ISO_SUFFIX": "nvidia", "RELEASE": "1"}).run()
        self.assertIn("THE NVIDIA VERSION OF 0.2.2 IS NOT READY YET. TRY AGAIN LATER.", flat(self.screen.frames[-1]))
        self.assertNotIn("THIS RELEASE HAS NO FILE FOR THIS BOX", self.screen.frames[-1])
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


class BetweenReleasesTest(UiTest):
    """Security fixes and app updates (couchliteos-security-update, couchliteos-app-update)."""

    def press(self, label, *after):
        """Keys that move from the first row to the row starting with `label`, then ENTER."""
        index = next(i for i, row in enumerate(self.make().rows()) if row.startswith(label))
        return [DOWN] * index + [ENTER, *after]

    def write_state(self, name, data):
        (self.tmp / name).write_text(json.dumps(data))

    def test_the_last_results_are_shown(self):
        self.write_state("security-update.json", {"result": "ok", "message": "RESTART TO FINISH THE SECURITY FIXES",
                                                  "restart_needed": True})
        self.write_state("app-update.json", {"result": "ok", "message": "APPS UPDATED", "apps": []})
        self.script = [ESC]
        self.make().run()
        self.assertIn("SECURITY FIXES: RESTART TO FINISH THE SECURITY FIXES", self.screen.frames[0])
        self.assertIn("APPS: APPS UPDATED", self.screen.frames[0])

    def test_never_checked_says_so(self):
        self.script = [ESC]
        self.make().run()
        self.assertIn("SECURITY FIXES: NOT CHECKED YET", self.screen.frames[0])
        self.assertIn("APPS: NOT CHECKED YET", self.screen.frames[0])

    def test_update_now_asks_both_services(self):
        self.script = self.press("INSTALL SECURITY FIXES AND APP UPDATES NOW", ESC)
        self.make().run()
        self.assertTrue((self.tmp / "security-update-install").exists())
        self.assertTrue((self.tmp / "app-update-install").exists())
        self.assertIn("STARTED", self.screen.frames[-1])

    def test_the_automatic_switches_write_the_update_section(self):
        self.script = self.press("AUTOMATIC SECURITY FIXES: ON", ESC)
        self.make().run()
        self.assertIn("AUTOMATIC SECURITY FIXES: OFF", self.screen.frames[-1])
        self.script = self.press("AUTOMATIC APP UPDATES: ON", ESC)
        self.make().run()
        self.assertIn("AUTOMATIC APP UPDATES: OFF", self.screen.frames[-1])
        values = settings.read_section("update", self.tmp / "config.ini")
        self.assertEqual((values.get("auto_security"), values.get("auto_apps")), ("off", "off"))

    def test_a_downloaded_app_can_be_rolled_back(self):
        self.write_state("app-update.json", {"message": "UP TO DATE", "apps": [
            {"name": "moonlight", "image": "6.1.0", "current": "6.2.0", "active": "6.2.0"},
            {"name": "chiaki-ng", "image": "1.9.0", "current": "1.8.0", "active": "1.9.0"}]})  # the image is newer
        self.script = [ESC]
        self.make().run()
        self.assertIn("ROLL BACK MOONLIGHT 6.2.0", self.screen.frames[0])
        self.assertNotIn("ROLL BACK CHIAKI", self.screen.frames[0])
        self.script = self.press("ROLL BACK MOONLIGHT", ESC)
        self.make().run()
        self.assertTrue(any("MOONLIGHT" in question for question in self.questions))
        self.assertEqual((self.tmp / "app-rollback").read_text(), "moonlight\n")

    def test_a_refused_rollback_asks_nothing(self):
        self.write_state("app-update.json", {"apps": [
            {"name": "moonlight", "image": "6.1.0", "current": "6.2.0", "active": "6.2.0"}]})
        self.answer = False
        self.script = self.press("ROLL BACK MOONLIGHT", ESC)
        self.make().run()
        self.assertFalse((self.tmp / "app-rollback").exists())

    def test_not_on_the_live_stick(self):
        self.script = [ESC]
        self.make(live=True).run()
        self.assertNotIn("SECURITY FIXES", self.screen.frames[0])
        self.assertNotIn("AUTOMATIC", self.screen.frames[0])


class ChannelRowTest(UiTest):
    """UPDATE CHANNEL: STABLE / BETA, saved as config.ini [update] channel."""

    def press(self, label, *after, **overrides):
        index = next(i for i, row in enumerate(self.make(**overrides).rows()) if row.startswith(label))
        return [DOWN] * index + [ENTER, *after]

    def saved_channel(self):
        return settings.read_section("update", self.tmp / "config.ini").get("channel")

    def test_a_release_box_starts_on_stable_and_the_row_switches_and_saves(self):
        self.script = [ESC]
        self.make().run()
        self.assertIn("UPDATE CHANNEL: STABLE", self.screen.frames[0])
        self.script = self.press("UPDATE CHANNEL: STABLE", ESC)
        self.make().run()
        self.assertIn("UPDATE CHANNEL: BETA", self.screen.frames[-1])
        self.assertEqual(self.saved_channel(), "beta")
        self.script = self.press("UPDATE CHANNEL: BETA", ESC)
        self.make().run()
        self.assertIn("UPDATE CHANNEL: STABLE", self.screen.frames[-1])
        self.assertEqual(self.saved_channel(), "stable")

    def test_a_beta_box_starts_on_beta(self):
        self.assertIn("UPDATE CHANNEL: BETA", self.make(current="0.3.0-beta").rows())

    def test_switching_keeps_the_other_update_settings(self):
        (self.tmp / "config.ini").write_text("[update]\nauto_apps = off\nsnapshot = off\n")
        self.script = self.press("UPDATE CHANNEL: STABLE", ESC)
        self.make().run()
        values = settings.read_section("update", self.tmp / "config.ini")
        self.assertEqual(values, {"auto_apps": "off", "snapshot": "off", "channel": "beta"})

    def test_switching_forgets_what_the_last_check_found(self):
        # CHECK finds 0.2.2 (INSTALL UPDATE becomes the first row), then the channel row is switched.
        index = self.make().rows().index("UPDATE CHANNEL: STABLE") + 1
        self.script = [ENTER] + [DOWN] * index + [ENTER, ESC]
        self.make().run()
        self.assertIn("UPDATE CHANNEL: BETA", self.screen.frames[-1])
        self.assertNotIn("INSTALL UPDATE", self.screen.frames[-1])

    def test_not_on_the_live_stick(self):
        self.assertFalse([row for row in self.make(live=True).rows() if "CHANNEL" in row])


class StorageTest(UiTest):
    """Live stick without persistence: SET UP STORAGE ON THIS STICK (couchliteos-persist-setup)."""

    def test_offered_only_on_a_stick_that_keeps_nothing(self):
        self.script = [ESC]
        self.make(live=True, persistent=lambda: False).run()
        self.assertIn("SET UP STORAGE ON THIS STICK", self.screen.frames[0])
        self.script = [ESC]
        self.make(live=True, persistent=lambda: True).run()
        self.assertNotIn("SET UP STORAGE", self.screen.frames[-1])
        self.script = [ESC]
        self.make(live=False, persistent=lambda: False).run()
        self.assertNotIn("SET UP STORAGE", self.screen.frames[-1])

    def test_asks_first_then_asks_the_root_service(self):
        self.script = [ENTER, ESC]
        self.make(live=True, persistent=lambda: False).run()
        self.assertEqual(len(self.questions), 1)
        self.assertIn("THE ISO ON IT STAYS", self.questions[0])
        self.assertTrue((self.tmp / "persist-setup-request").exists())

    def test_no_means_nothing_is_asked(self):
        self.answer = False
        self.script = [ENTER, ESC]
        self.make(live=True, persistent=lambda: False).run()
        self.assertFalse((self.tmp / "persist-setup-request").exists())

    def test_the_services_progress_is_shown(self):
        (self.tmp / "persist-setup.json").write_text(json.dumps(
            {"phase": "done", "message": "STORAGE IS READY: RESTART TO USE IT", "percent": 100}))
        self.script = [ESC]
        self.make(live=True, persistent=lambda: False).run()
        self.assertIn("STORAGE IS READY: RESTART TO USE IT", self.screen.frames[0])

    def test_the_mountinfo_check(self):
        mountinfo = self.tmp / "mountinfo"
        mountinfo.write_text("22 1 0:20 / / rw - overlay overlay rw\n"
                             "30 22 8:1 / /run/live/medium ro - iso9660 /dev/sdb1 ro\n")  # every live boot
        self.assertFalse(su.has_persistence(mountinfo))
        mountinfo.write_text("40 22 8:3 /home /home rw - ext4 /dev/sdb3 rw\n"
                             "41 22 8:3 / /run/live/persistence/sdb3 rw - ext4 /dev/sdb3 rw\n")
        self.assertTrue(su.has_persistence(mountinfo))
        self.assertFalse(su.has_persistence(self.tmp / "missing"))


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
        # Nothing was cut (addnstr above refuses it): the 90-character failure is all there, wrapped.
        self.assertTrue(any(frame.count("X") == 90 for frame in self.screen.frames), self.screen.frames[-1])
        self.assertTrue(any("1881 OF 1900 MB" in frame for frame in self.screen.frames))


@unittest.skipUnless(hasattr(os, "getuid"), "the launcher imports pwd: Linux only")
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


def make_slot(path, version):
    """A complete update slot, written the way couchliteos_liveslot writes one (ok last)."""
    path.mkdir(parents=True)
    for name in liveslot.FILES:
        (path / name).write_text("x")
    (path / liveslot.VERSION).write_text(version + "\n")
    (path / liveslot.OK).write_text(version + "\n")


class StickTest(UiTest):
    """A live stick with storage: its own update slots (couchliteos_liveslot) show on this screen."""

    UPDATED = {"current": "0.3.2", "previous": "0.3.1", "iso": "0.3.1"}

    def stick_rows(self, stick):
        return self.make(live=True, stick=stick).rows()

    def test_a_stick_with_no_update_says_it_runs_from_the_stick_and_names_its_iso(self):
        self.script = [ESC]
        self.make(live=True).run()
        frame = flat(self.screen.frames[0])
        self.assertIn("RUNNING FROM STICK", frame)
        self.assertIn("ISO 0.2.1", frame, "with no update on the stick, the running system is the ISO's")
        self.assertNotIn("UPDATED SYSTEM", frame)
        self.assertNotIn("ROLL BACK", frame)

    def test_an_updated_stick_shows_the_slot_version_and_the_iso_it_came_from(self):
        self.script = [ESC]
        self.make(live=True, stick=lambda: self.UPDATED).run()
        frame = flat(self.screen.frames[0])
        self.assertIn("RUNNING FROM STICK", frame)
        self.assertIn("UPDATED SYSTEM 0.3.2", frame)
        self.assertIn("ISO 0.3.1", frame)

    def test_an_updated_stick_that_does_not_record_its_iso_says_unknown(self):
        self.script = [ESC]
        self.make(live=True, stick=lambda: {"current": "0.3.2", "previous": "", "iso": ""}).run()
        self.assertIn("ISO UNKNOWN", flat(self.screen.frames[0]))

    def test_a_stick_that_cannot_read_its_slots_still_says_it_runs_from_the_stick(self):
        def broken():
            raise OSError("gone")

        self.script = [ESC]
        self.make(live=True, stick=broken).run()
        frame = flat(self.screen.frames[0])
        self.assertIn("RUNNING FROM STICK", frame)
        self.assertIn("ISO 0.2.1", frame)
        self.assertNotIn("UPDATED SYSTEM", frame)

    def test_roll_back_is_listed_only_with_a_previous_slot(self):
        self.assertEqual(self.stick_rows(lambda: self.UPDATED)[-2:], ["ROLL BACK", "BACK"])
        only_current = {"current": "0.3.2", "previous": "", "iso": "0.3.1"}
        self.assertNotIn("ROLL BACK", self.stick_rows(lambda: only_current))
        self.assertNotIn("ROLL BACK", self.stick_rows(lambda: {}))

    def test_roll_back_asks_first_then_asks_the_updater_for_rollback_live(self):
        self.script = [ENTER, ESC]  # the first row on a stick with a previous slot
        self.make(live=True, stick=lambda: self.UPDATED).run()
        self.assertEqual(len(self.questions), 1)
        self.assertIn("GO BACK TO COUCHLITEOS 0.3.1", flat(self.questions[0]))
        self.assertEqual(su.LIVE_ROLLBACK, "rollback-live")
        self.assertTrue((self.tmp / "rollback-live").exists())
        self.assertIn("ROLLING BACK TO COUCHLITEOS 0.3.1", flat(self.screen.frames[-1]))

    def test_no_to_roll_back_changes_nothing(self):
        self.answer = False
        self.script = [ENTER, ESC]
        self.make(live=True, stick=lambda: self.UPDATED).run()
        self.assertEqual(len(self.questions), 1)
        self.assertFalse((self.tmp / "rollback-live").exists())

    def test_without_persistence_the_stick_keeps_the_explanation(self):
        self.script = [ESC]
        self.make(live=True, persistent=lambda: False, stick=lambda: self.UPDATED).run()
        frame = self.screen.frames[0]
        self.assertIn(flat(su.LIVE_TEXT), flat(frame))
        self.assertIn("SET UP STORAGE ON THIS STICK", frame)
        for text in ("RUNNING FROM STICK", "UPDATED SYSTEM", "ROLL BACK"):
            self.assertNotIn(text, frame)


class StickStateTest(unittest.TestCase):
    """What the screen reads from a stick: the mount table, then the slot files. Nothing is mounted."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mountinfo = self.tmp / "mountinfo"
        self.part = self.tmp / "run/live/persistence/sdb3"

    def mounted_at(self, point):
        self.mountinfo.write_text("22 1 0:20 / / rw - overlay overlay rw\n"
                                  f"41 22 8:3 / {point} rw - ext4 /dev/sdb3 rw\n")

    def read(self):
        return su.stick_state(self.mountinfo, self.tmp)

    def test_a_stick_with_no_slot_has_no_stick_state(self):
        self.mounted_at("/run/live/persistence/sdb3")
        self.assertEqual(self.read(), {})

    def test_the_current_and_previous_slots_and_the_iso_version_are_read(self):
        make_slot(self.part / "live-update/current", "0.3.2")
        make_slot(self.part / "live-update/previous", "0.3.1")
        (self.part / su.ISO_VERSION_FILE).write_text("0.3.1\n")
        self.mounted_at("/run/live/persistence/sdb3")
        self.assertEqual(self.read(), {"current": "0.3.2", "previous": "0.3.1", "iso": "0.3.1"})

    def test_a_slot_without_its_ok_marker_is_not_shown_as_updated(self):
        make_slot(self.part / "live-update/current", "0.3.2")
        (self.part / "live-update/current" / liveslot.OK).unlink()
        self.mounted_at("/run/live/persistence/sdb3")
        self.assertEqual(self.read(), {"current": "", "previous": "", "iso": ""})

    def test_a_slot_on_the_live_medium_is_found_too(self):
        make_slot(self.tmp / "run/live/medium/live-update/current", "0.3.2")
        self.mounted_at("/run/live/medium")
        self.assertEqual(self.read()["current"], "0.3.2")

    def test_a_stick_booted_from_its_iso_has_no_slot_on_the_medium(self):
        (self.tmp / "run/live/medium").mkdir(parents=True)  # the ISO itself: no live-update directory
        self.mounted_at("/run/live/medium")
        self.assertEqual(self.read(), {})

    def test_roll_back_names_the_updater_command_as_its_request_file(self):
        self.assertEqual(su.LIVE_ROLLBACK, "rollback-live")
        self.assertEqual(su.STICK_ROLL_BACK, "ROLL BACK")


if __name__ == "__main__":
    unittest.main()
