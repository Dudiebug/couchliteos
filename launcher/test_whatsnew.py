import curses
import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_whatsnew as whatsnew
from test_launcher import Screen as LauncherScreen


class Screen:
    """A curses window stand-in that records what was drawn and feeds scripted keys."""

    def __init__(self, keys=(), size=(24, 80), fail_drawing=False):
        self.keys = list(keys)
        self.size = size
        self.fail_drawing = fail_drawing
        self.frame = []
        self.reads = 0

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.frame = []

    def border(self, *_args):
        pass

    def refresh(self):
        pass

    def addstr(self, row, col, text, attr=0):
        if self.fail_drawing:
            raise curses.error("addstr() returned ERR")
        self.frame.append((row, col, text, attr))

    def getch(self):
        self.reads += 1
        if not self.keys:
            raise RuntimeError("stop")  # the screen is still waiting for A or B
        return self.keys.pop(0)

    def text(self):
        return [text for _row, _col, text, _attr in self.frame]


class DecisionTest(unittest.TestCase):
    def test_shown_once_on_upgrade(self):
        # Setup was finished before; the seen marker is missing or older than this version.
        self.assertTrue(whatsnew.should_show("", "0.2.0", True))
        self.assertTrue(whatsnew.should_show("0.1.13\n", "0.2.0", True))

    def test_not_shown_to_new_users(self):
        self.assertFalse(whatsnew.should_show("", "0.2.0", False))
        self.assertFalse(whatsnew.should_show("0.1.13", "0.2.0", False))

    def test_not_shown_again_same_version(self):
        self.assertFalse(whatsnew.should_show("0.2.0\n", "0.2.0", True))
        self.assertFalse(whatsnew.should_show("v0.2.0", "0.2.0", True))
        # A stick moved back to an older system does not nag either.
        self.assertFalse(whatsnew.should_show("0.3.0", "0.2.0", True))

    def test_shown_after_next_version_bump(self):
        self.assertTrue(whatsnew.should_show("0.2.0", "0.2.1", True))
        self.assertTrue(whatsnew.should_show("0.2.0", "0.3.0", True))
        self.assertTrue(whatsnew.should_show("0.2.0-rc.1", "0.2.0", True))

    def test_a_damaged_seen_marker_counts_as_missing(self):
        self.assertTrue(whatsnew.should_show("\x00garbage", "0.2.0", True))

    def test_unreadable_version_shows_nothing(self):
        self.assertFalse(whatsnew.should_show("", "", True))
        self.assertFalse(whatsnew.should_show("0.1.13", "not a version", True))


class ShowOnceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = pathlib.Path(self.directory.name)
        self.version_file = root / "version"
        self.version_file.write_text("0.2.0\n")
        self.setup_marker = root / "state" / "setup-complete"
        self.seen = root / "state" / "whatsnew-seen"
        self.session = root / "run" / "whatsnew-seen"

    def complete_setup(self):
        self.setup_marker.parent.mkdir(parents=True, exist_ok=True)
        self.setup_marker.write_text("1\n")

    def show(self, keys=(10,), **overrides):
        screen = overrides.pop("screen", None) or Screen(keys)
        options = dict(
            markers=(self.seen, self.session),
            setup_marker=self.setup_marker,
            version_files=[self.version_file],
        )
        options.update(overrides)
        return whatsnew.show_once(screen, **options), screen

    def test_upgrader_sees_the_screen_once_and_it_is_marked(self):
        self.complete_setup()
        shown, screen = self.show()
        self.assertTrue(shown)
        self.assertEqual(screen.reads, 1)
        self.assertIn("WHAT'S NEW IN 0.2.0", screen.text())
        self.assertEqual(self.seen.read_text().strip(), "0.2.0")

    def test_new_user_never_sees_it_but_is_marked(self):
        shown, screen = self.show(keys=())
        self.assertFalse(shown)
        self.assertEqual(screen.reads, 0)
        self.assertEqual(screen.frame, [])
        self.assertEqual(self.seen.read_text().strip(), "0.2.0")
        # Finishing the wizard in this same boot does not turn them into an upgrader.
        self.complete_setup()
        shown, screen = self.show(keys=())
        self.assertFalse(shown)

    def test_second_start_of_the_same_version_shows_nothing(self):
        self.complete_setup()
        self.assertTrue(self.show()[0])
        shown, screen = self.show(keys=())
        self.assertFalse(shown)
        self.assertEqual(screen.reads, 0)

    def test_shown_again_after_a_version_bump(self):
        self.complete_setup()
        self.assertTrue(self.show()[0])
        self.version_file.write_text("0.2.1\n")
        shown, screen = self.show()
        self.assertTrue(shown)
        self.assertIn("WHAT'S NEW IN 0.2.1", screen.text())
        self.assertEqual(self.seen.read_text().strip(), "0.2.1")

    def test_a_version_without_notes_shows_nothing_but_is_marked(self):
        self.complete_setup()
        self.seen.write_text("0.2.5\n")
        self.version_file.write_text("0.2.6\n")
        shown, screen = self.show(keys=())
        self.assertFalse(shown)
        self.assertEqual(screen.frame, [])
        self.assertEqual(self.seen.read_text().strip(), "0.2.6")

    def test_an_older_seen_marker_is_replaced(self):
        self.complete_setup()
        self.seen.write_text("0.1.13\n")
        self.assertTrue(self.show()[0])
        self.assertEqual(self.seen.read_text().strip(), "0.2.0")

    def test_unreadable_version_file_shows_nothing_and_marks_nothing(self):
        self.complete_setup()
        for content in (None, "", "garbage\n"):
            with self.subTest(content=content):
                self.version_file.unlink(missing_ok=True)
                if content is not None:
                    self.version_file.write_text(content)
                shown, screen = self.show(keys=())
                self.assertFalse(shown)
                self.assertEqual(screen.reads, 0)
                self.assertFalse(self.seen.exists())
                self.assertFalse(self.session.exists())

    def test_marker_write_failure_does_not_crash_or_loop(self):
        self.complete_setup()
        # The state directory cannot be written (here: a plain file sits where it should be).
        blocked = self.seen.parent.parent / "blocked"
        blocked.write_text("")
        seen = blocked / "whatsnew-seen"
        shown, screen = self.show(markers=(seen, self.session))
        self.assertTrue(shown)
        self.assertEqual(screen.reads, 1)
        # A launcher restart during the same boot does not show it again.
        shown, screen = self.show(keys=(), markers=(seen, self.session))
        self.assertFalse(shown)
        self.assertEqual(screen.reads, 0)

    def test_both_markers_failing_still_shows_the_screen_only_until_a_or_b(self):
        self.complete_setup()
        blocked = self.seen.parent.parent / "blocked"
        blocked.write_text("")
        markers = (blocked / "a", blocked / "b")
        shown, screen = self.show(markers=markers)
        self.assertTrue(shown)
        self.assertEqual(screen.reads, 1)

    def test_marked_before_the_screen_is_shown(self):
        # A crash or power cut while the screen is up must not bring it back at every start.
        self.complete_setup()
        seen_when_read = []

        def read_key():
            seen_when_read.append(self.seen.exists())
            return 10

        self.show(read_key=read_key)
        self.assertEqual(seen_when_read, [True])

    def test_screen_closes_on_a_or_b(self):
        self.complete_setup()
        for key in (10, 13, curses.KEY_ENTER, 27):
            with self.subTest(key=key):
                self.seen.unlink(missing_ok=True)
                self.session.unlink(missing_ok=True)
                shown, screen = self.show(keys=[key])
                self.assertTrue(shown)
                self.assertEqual(screen.reads, 1)

    def test_other_keys_timeouts_and_resizes_keep_the_screen_up(self):
        self.complete_setup()
        keys = [-1, curses.KEY_DOWN, curses.KEY_UP, ord("x"), curses.KEY_F5, curses.KEY_RESIZE, 10]
        shown, screen = self.show(keys=keys)
        self.assertTrue(shown)
        self.assertEqual(screen.reads, 7)
        self.assertEqual(screen.keys, [])

    def test_keys_come_from_the_launchers_reader_when_given(self):
        # The launcher passes its read_key so the idle blanker still filters input.
        self.complete_setup()
        answers = iter([-1, 27])
        screen = Screen(())
        shown, _ = self.show(screen=screen, read_key=lambda: next(answers))
        self.assertTrue(shown)
        self.assertEqual(screen.reads, 0)

    def test_drawing_errors_do_not_crash(self):
        self.complete_setup()
        shown, screen = self.show(screen=Screen([10], fail_drawing=True))
        self.assertTrue(shown)
        self.assertEqual(screen.reads, 1)


class ScreenTest(unittest.TestCase):
    def draw(self, size=(24, 80), version="0.2.5", seen=""):
        screen = Screen(size=size)
        whatsnew.draw(screen, version, seen)
        return screen

    def test_shows_everything_inside_80x24(self):
        screen = self.draw()
        for row, col, text, _attr in screen.frame:
            self.assertTrue(0 < row < 23, (row, text))
            self.assertTrue(col >= 1 and col + len(text) <= 79, (col, text))
        text = "\n".join(screen.text())
        self.assertIn("WHAT'S NEW IN 0.2.5", text)
        self.assertIn(whatsnew.RENAME_NOTICE, text)
        for feature in whatsnew.FEATURES:
            self.assertIn(feature, text)
        self.assertIn("PRESS A OR B TO CONTINUE", text)
        # Nothing is drawn on top of anything else.
        rows = [row for row, _col, _text, _attr in screen.frame]
        self.assertEqual(len(rows), len(set(rows)))

    def test_every_line_fits_76_columns(self):
        lines = [whatsnew.RENAME_NOTICE, whatsnew.MORE]
        for release, features in whatsnew.RELEASES:
            lines += [f"NEW IN {release}:", *("- " + feature for feature in features)]
        for line in lines:
            self.assertLessEqual(len(line), 76, line)
        for _row, col, text, _attr in self.draw().frame:
            self.assertLessEqual(len(text), 76, text)

    def test_small_screens_are_clipped_and_never_raise(self):
        for size in ((10, 40), (6, 30), (24, 60), (3, 10), (1, 1)):
            with self.subTest(size=size):
                screen = self.draw(size)
                height, width = size
                for row, col, text, _attr in screen.frame:
                    self.assertTrue(0 <= row < height, (row, text))
                    self.assertTrue(col + len(text) <= width, (col, text))

    def test_footer_is_visible_on_a_short_screen(self):
        self.assertIn("PRESS A OR B TO CONTINUE", self.draw((12, 80)).text())

    def test_the_title_is_bold(self):
        attrs = {text: attr for _row, _col, text, attr in self.draw().frame}
        self.assertTrue(attrs["WHAT'S NEW IN 0.2.5"] & curses.A_BOLD)

    def test_an_upgrade_from_0_2_4_shows_only_0_2_5_without_the_rename(self):
        text = "\n".join(self.draw(seen="0.2.4").text())
        self.assertNotIn(whatsnew.RENAME_NOTICE, text)
        for feature in whatsnew.FEATURES:
            self.assertIn(feature, text)
        self.assertNotIn("BRIGHTNESS AND VOLUME KEYS", text)  # 0.2.4 notes were seen already
        self.assertNotIn("NEW IN 0.2.5:", text)  # one release needs no heading
        self.assertNotIn(whatsnew.EARLIER, text)

    def test_a_long_upgrade_shows_only_this_release_and_points_to_the_rest(self):
        screen = self.draw(seen="0.1.13")
        text = "\n".join(screen.text())
        self.assertIn(whatsnew.RENAME_NOTICE, text)
        for feature in whatsnew.FEATURES:
            self.assertIn(feature, text)
        for _release, features in whatsnew.RELEASES[1:]:
            for feature in features:
                self.assertNotIn(feature, text)
        self.assertEqual(text.count(whatsnew.EARLIER), 1)
        rows = [row for row, _col, _text, _attr in screen.frame]
        self.assertEqual(len(rows), len(set(rows)))
        self.assertTrue(all(row < 21 for row, _c, line, _a in screen.frame if line != whatsnew.FOOTER))


class ContentTest(unittest.TestCase):
    def test_rename_notice_names_both_products(self):
        self.assertIn("MOONLIGHTOS IS NOW CALLED COUCHLITEOS", whatsnew.RENAME_NOTICE)  # rename:keep

    def test_features_are_one_constant_tuple_so_the_lead_can_trim_it(self):
        self.assertIsInstance(whatsnew.FEATURES, tuple)
        for _release, features in whatsnew.RELEASES:
            self.assertTrue(all(isinstance(line, str) and line == line.upper() and len(line) <= 66 for line in features))

    def test_releases_are_newest_first_and_the_current_version_has_notes(self):
        versions = [whatsnew.update.parse_version(release) for release, _features in whatsnew.RELEASES]
        self.assertEqual(versions, sorted(versions, reverse=True))
        current = (pathlib.Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
        self.assertEqual(whatsnew.RELEASES[0][0], current)

    def test_0_2_5_says_the_super_key_no_longer_opens_home_and_where_to_set_the_key(self):
        release, features = whatsnew.RELEASES[0]
        self.assertEqual(release, "0.2.5")
        text = " ".join(features)
        self.assertIn("SUPER", text)
        self.assertIn("KEYBOARD HOME KEY", text)
        self.assertIn("SETTINGS > CONTROLS", text)

    def test_notes_are_only_the_installed_release(self):
        self.assertEqual(whatsnew.notes("0.2.3", "0.2.2"), (False, [whatsnew.RELEASES[2]]))
        renamed, releases = whatsnew.notes("0.2.3", "0.2.0")
        self.assertFalse(renamed)
        self.assertEqual([r for r, _f in releases], ["0.2.3"])
        self.assertTrue(whatsnew.skipped_releases("0.2.3", "0.2.0"))
        self.assertFalse(whatsnew.skipped_releases("0.2.3", "0.2.2"))
        renamed, releases = whatsnew.notes("0.2.3", "0.1.13")
        self.assertTrue(renamed)
        self.assertEqual([r for r, _f in releases], ["0.2.3"])
        self.assertTrue(whatsnew.notes("0.2.3", "")[0])  # no marker: from before the rename
        # A release without its own notes shows none: never the previous release's.
        self.assertEqual(whatsnew.notes("0.2.6", "0.2.5"), (False, []))
        self.assertEqual(whatsnew.notes("0.2.5.1", "0.2.5"), (False, []))
        # A pre-release of 0.2.5 already shows the 0.2.5 notes.
        self.assertEqual(whatsnew.notes("0.2.5-rc.1", "0.2.4")[1], [whatsnew.RELEASES[0]])
        # A future version's notes are not shown on an older system.
        self.assertEqual(whatsnew.notes("0.2.2", "0.2.1")[1][0][0], "0.2.2")

    def test_find_gaming_pcs_is_placed_where_it_really_is(self):
        # FIND GAMING PCS is inside STREAMING > PAIR A GAMING PC, not a row of its own.
        line = next(line for _r, features in whatsnew.RELEASES for line in features if "FIND GAMING PCS" in line)
        self.assertIn("SETTINGS > STREAMING", line)
        self.assertIn("PAIR A / ANOTHER GAMING PC", line)  # the row says ANOTHER once a PC is paired
        self.assertLessEqual(len(line), 74)

    def test_every_new_feature_is_listed(self):
        text = "\n".join(line for _r, features in whatsnew.RELEASES for line in features)
        for needle in ("TYPE INTO AN APP", "CONTROLLER MOUSE", "SUPER KEY", "SETUP WIZARD", "HOLD THE GUIDE", "TV REMOTE", "GAMING PC WAKES", "FIND GAMING PCS", "STREAM CHECK", "SCREEN EDGES",
                       "SOFTWARE UPDATE: SETTINGS > SOFTWARE UPDATE"):
            self.assertIn(needle, text)

    def test_markers_live_with_the_other_state(self):
        self.assertEqual(whatsnew.SEEN.name, "whatsnew-seen")
        self.assertEqual(whatsnew.SEEN.parent, whatsnew.setup.MARKER.parent)
        self.assertEqual(whatsnew.SESSION_SEEN.parent, pathlib.Path("/run/couchliteos"))

    def test_the_version_comes_from_the_same_files_as_the_update_check(self):
        self.assertEqual(whatsnew.VERSION_FILES, whatsnew.update.VERSION_FILES)


class LauncherHookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).with_name("couchliteos-launcher.py")
        spec = importlib.util.spec_from_file_location("launcher_whatsnew", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_run_shows_whats_new_before_the_wizard_and_with_the_launchers_key_reader(self):
        # Before the wizard: afterwards a new user who just finished setup would look like an upgrader.
        calls = []
        with mock.patch.object(self.module, "network_summary", return_value="OFFLINE"):
            launcher = self.module.Launcher(LauncherScreen())
        launcher.prepare_session = mock.Mock()
        launcher.setup_wizard = mock.Mock(side_effect=lambda: calls.append("wizard"))
        launcher.autostream = mock.Mock(side_effect=lambda: calls.append("autostream"))
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.module, "RUN", pathlib.Path(directory)
        ), mock.patch.object(self.module.display, "restore_saved_mode"), mock.patch.object(
            self.module.curses, "curs_set"
        ), mock.patch.object(self.module.curses, "use_default_colors"), mock.patch.object(
            self.module.whatsnew, "show_once", side_effect=lambda *args: calls.append(("whatsnew", args))
        ):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                launcher.run()
        self.assertEqual(calls[1:], ["wizard", "autostream"])
        screen, reader = calls[0][1]
        self.assertEqual((calls[0][0], screen), ("whatsnew", launcher.screen))
        # show_once calls its reader with no arguments; it must still read the launcher's screen.
        with mock.patch.object(self.module, "read_key", return_value=27) as read_key:
            self.assertEqual(reader(), 27)
        read_key.assert_called_once_with(launcher.screen)


if __name__ == "__main__":
    unittest.main()
