import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import pathlib
import re
import tempfile
import unittest

import couchliteos_controls as controls
import couchliteos_help as tvhelp
import couchliteos_icons as icons
import couchliteos_input as inputprefs
import couchliteos_quick as quick
import couchliteos_theme as theme
import couchliteos_tvlayout as tvlayout
import couchliteos_tvscreens as tvscreens

HERE = pathlib.Path(__file__).resolve().parent
FAMILIES = tuple(controls.FAMILY_NAMES)
CHIP_BUTTONS = {*quick.BUTTONS, "move", "guide", "home", "hold-x", "phone"}
# Every place a card or a topic sends someone ("SETTINGS > STREAMING > STREAM CHECK"), past
# Settings' own rows: each must be a real menu row, named as the classic screens name it.
LAUNCHER_TEXT = "\n".join(path.read_text(encoding="utf-8") for path in sorted(HERE.glob("couchliteos*.py"))
                          if path.name != "couchliteos_help.py")

try:
    import curses
except ImportError:  # Windows: the classic screen is Linux-only, like the launcher
    curses = None


def every_card():
    for cross in (True, False):
        yield from tvhelp.tour_cards(cross)


def every_topic():
    for cross in (True, False):
        yield from tvhelp.topics(cross)


WORDS = r"[A-Z0-9&'-]+(?: [A-Z0-9&'-]+)*"


def places(text: str) -> list[list[str]]:
    """The "SETTINGS > A > B" paths in `text`, as [A, B]. The last part runs on into the sentence
    ("SETTINGS > BLUETOOTH AND PUT THE PAD..."): `known_start` finds where the name ends."""
    return [match.group(1).split(" > ") for match in re.finditer(rf"SETTINGS > ({WORDS}(?: > {WORDS})*)", text)]


def known_start(part: str, known) -> str:
    """The longest run of `part`'s first words that `known` accepts, or ""."""
    words = part.split()
    for end in range(len(words), 0, -1):
        if known(" ".join(words[:end])):
            return " ".join(words[:end])
    return ""


class TourCardsTest(unittest.TestCase):
    def test_five_short_cards_each_with_a_title_lines_and_a_bundled_icon(self):
        for cross in (True, False):
            cards = tvhelp.tour_cards(cross)
            self.assertTrue(4 <= len(cards) <= 6, len(cards))
            self.assertEqual(len({card.key for card in cards}), len(cards))
        fallback = icons.bundled("no-such-icon")
        for card in every_card():
            self.assertTrue(card.title.strip(), card)
            self.assertTrue(card.lines and all(line.strip() for line in card.lines), card)
            self.assertLessEqual(len(card.lines), 3, f"{card.key}: a card is short")
            for line in card.lines:
                self.assertLessEqual(len(line), 120, line)
                shown = line.format(guide="XBOX BUTTON")
                self.assertEqual(shown, shown.upper(), line)
            self.assertNotEqual(icons.bundled(card.icon), fallback, f"{card.key}: no icon {card.icon}")

    def test_the_cards_cover_moving_streaming_apps_buttons_and_help(self):
        self.assertEqual([card.key for card in tvhelp.tour_cards()], ["move", "stream", "apps", "buttons", "help"])
        last = tvhelp.tour_cards()[-1]
        self.assertTrue(any("HELP" in line for line in last.lines), "the last card says where HELP is")

    def test_cross_and_rows_cards_name_their_own_home(self):
        cross, rows = (" ".join(" ".join(card.lines) for card in tvhelp.tour_cards(flag)) for flag in (True, False))
        self.assertIn("CATEGORY", cross)
        self.assertNotIn("CATEGORY", rows)
        self.assertIn("ROW", rows)

    def test_every_chip_has_keys_for_every_family_with_the_keyboard_key(self):
        settings = (inputprefs.Settings(), inputprefs.Settings(home=inputprefs.HOME_STICKS),
                    inputprefs.Settings(home=inputprefs.HOME_GUIDE, keyboard_home=""))
        for card in every_card():
            for chip in card.chips:
                self.assertIn(chip.button, CHIP_BUTTONS, card.key)
                self.assertTrue(chip.words.strip())
                for family in FAMILIES:
                    for choice in settings:
                        keys = tvhelp.chip_keys(chip.button, family, choice)
                        self.assertTrue(keys.strip())
                        self.assertTrue(quick.names_keyboard_keys(keys) or chip.button == "home" and " OR " not in keys,
                                        f"{family} {chip.button}: {keys!r} has no keyboard key")

    def test_chip_keys_use_the_pads_own_glyphs_and_names(self):
        self.assertEqual(tvhelp.chip_keys("activate", "xbox"), "Ⓐ OR ENTER")
        self.assertEqual(tvhelp.chip_keys("activate", "playstation"), "✕ OR ENTER")
        self.assertEqual(tvhelp.chip_keys("back", "nintendo"), "Ⓐ OR ESC")  # Nintendo's B is south
        self.assertEqual(tvhelp.chip_keys("hold-x", "playstation"), "HOLD △ OR F10")
        self.assertEqual(tvhelp.chip_keys("phone", "xbox"), "VIEW OR F2")
        self.assertEqual(tvhelp.chip_keys("guide", "playstation"), "PS BUTTON OR HOME KEY")
        self.assertEqual(tvhelp.chip_keys("guide", "nintendo"), "HOME BUTTON OR HOME KEY")
        self.assertEqual(tvhelp.chip_keys("home", "xbox"), "HOLD VIEW+MENU OR CTRL+ALT+H")
        self.assertEqual(tvhelp.chip_keys("home", "playstation", inputprefs.Settings(home=inputprefs.HOME_STICKS)),
                         "HOLD L3+R3 OR CTRL+ALT+H")
        self.assertEqual(tvhelp.chip_keys("home", "xbox", inputprefs.Settings(keyboard_home="")), "HOLD VIEW+MENU")
        with self.assertRaises(ValueError):
            tvhelp.chip_keys("jump", "xbox")

    def test_a_card_as_drawn_names_the_pads_guide_and_has_a_prompt_with_keys(self):
        tour = tvhelp.Tour(tvhelp.tour_cards())
        tour.index = [card.key for card in tour.cards].index("buttons")
        for family in FAMILIES:
            view = tour.view(family)
            self.assertNotIn("{", " ".join(view.lines))
            self.assertIn(tvhelp.guide_name(family), " ".join(view.lines))
        for index in range(len(tour.cards)):
            tour.index = index
            for family in FAMILIES:
                view = tour.view(family)
                self.assertEqual((view.index, view.count), (index, len(tour.cards)))
                self.assertTrue(quick.names_keyboard_keys(view.prompt), view.prompt)
                self.assertEqual(len(view.chips), len(tour.card.chips))


class TourKeysTest(unittest.TestCase):
    def test_right_and_a_go_on_left_and_b_go_back(self):
        tour = tvhelp.Tour(tvhelp.tour_cards())
        self.assertEqual(tour.key("left"), None)  # nothing before the first card
        self.assertEqual(tour.key("right"), tvhelp.NEXT)
        self.assertEqual(tour.key("activate"), tvhelp.NEXT)
        self.assertEqual(tour.index, 2)
        self.assertEqual(tour.key("back"), tvhelp.PREVIOUS)
        self.assertEqual(tour.key("left"), tvhelp.PREVIOUS)
        self.assertEqual(tour.index, 0)
        self.assertEqual(tour.key("up"), None)
        self.assertEqual(tour.key("keyboard"), None)

    def test_it_ends_with_a_on_the_last_card_b_on_the_first_or_a_skip(self):
        tour = tvhelp.Tour(tvhelp.tour_cards())
        self.assertEqual(tour.key("back"), tvhelp.DONE)  # SKIP on the first card
        tour.index = len(tour.cards) - 1
        self.assertEqual(tour.key("right"), None)  # RIGHT does not end it by surprise
        self.assertEqual(tour.key("activate"), tvhelp.DONE)
        tour.index = 2
        self.assertEqual(tour.key("close"), tvhelp.DONE)

    def test_the_prompt_says_what_each_button_does_on_each_card(self):
        tour = tvhelp.Tour(tvhelp.tour_cards())
        self.assertEqual(tour.prompt_entries(), (("activate", "NEXT"), ("back", "SKIP")))
        tour.index = 1
        self.assertEqual(tour.prompt_entries(), (("activate", "NEXT"), ("back", "BACK"), ("close", "SKIP")))
        tour.index = len(tour.cards) - 1
        self.assertEqual(tour.prompt_entries(), (("activate", "DONE"), ("back", "BACK")))


class TourStateTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = pathlib.Path(temporary.name)
        (base / "state").mkdir()
        (base / "run").mkdir()
        self.setup = base / "state" / "setup-complete"
        self.markers = (base / "state" / "tutorial-seen", base / "run" / "tutorial-seen")
        self.controls = base / "state" / "controls-shown"

    def due(self) -> bool:
        return tvhelp.tour_due(self.markers, self.setup)

    def test_due_only_after_setup_and_only_once(self):
        self.assertFalse(self.due(), "never before the setup wizard")
        self.setup.touch()
        self.assertTrue(self.due())
        tvhelp.mark_tour_seen(self.markers, self.controls)
        self.assertFalse(self.due())
        self.assertTrue(all(path.exists() for path in self.markers))
        self.assertTrue(self.controls.exists(), "the tour has the buttons the CONTROLS screen showed once")

    def test_either_marker_is_enough(self):
        self.setup.touch()
        self.markers[1].touch()  # the disk could not be written, this boot's mark was
        self.assertFalse(self.due())

    def test_a_marker_that_cannot_be_written_is_not_an_error(self):
        self.setup.touch()
        unwritable = (self.setup.parent / "missing-folder" / "tutorial-seen", self.markers[1])
        tvhelp.mark_tour_seen(unwritable, self.controls)
        self.assertFalse(tvhelp.tour_due(unwritable, self.setup))

    def test_help_opens_the_tour_again_after_it_was_seen(self):
        self.setup.touch()
        tvhelp.mark_tour_seen(self.markers, self.controls)
        model = tvhelp.HelpModel(tvhelp.topics())
        self.assertEqual(model.topic.action, tvhelp.TOUR)
        self.assertEqual(model.key("activate"), tvhelp.TOUR)
        self.assertFalse(model.reading)

    def test_the_markers_live_in_the_state_and_run_folders(self):
        self.assertEqual(tvhelp.SEEN, tvhelp.STATE / "tutorial-seen")
        self.assertEqual(tvhelp.SESSION_SEEN, tvhelp.RUN / "tutorial-seen")
        self.assertEqual(tvhelp.SETUP_MARKER, controls.SETUP_MARKER)


class TopicsTest(unittest.TestCase):
    def test_every_topic_has_a_short_title_and_text(self):
        for topic in every_topic():
            self.assertTrue(topic.title.strip())
            self.assertLessEqual(len(topic.title), 26, topic.title)  # one row in the list
            self.assertTrue(topic.lines and all(line.strip() for line in topic.lines), topic.key)
            self.assertLessEqual(len(topic.lines), 6, f"{topic.key}: short answers")
            for line in topic.lines:
                self.assertLessEqual(len(line), 200, line)
                self.assertEqual(line, line.upper(), line)

    def test_the_topics_asked_for_are_all_there(self):
        keys = [topic.key for topic in tvhelp.topics()]
        self.assertEqual(len(set(keys)), len(keys))
        for key in ("tour", "pc", "streaming", "controllers", "phone", "apps", "network", "updates", "power", "support"):
            self.assertIn(key, keys)
        self.assertEqual(keys[0], "tour")
        self.assertNotIn("tour", [topic.key for topic in tvhelp.topics(tour=False)])

    def test_every_settings_path_names_a_real_entry_and_menu_row(self):
        labels = [entry.label for entry in tvscreens.ENTRIES]
        texts = [line for topic in every_topic() for line in topic.lines]
        texts += [line for card in every_card() for line in card.lines] + [tvhelp.HELP_LINE, tvhelp.ADD_LINE]
        seen = rows = 0
        for text in texts:
            for path in places(text):
                seen += 1
                self.assertTrue(known_start(path[0], labels.__contains__), f"{text}: no Settings entry")
                for row in path[1:]:
                    rows += 1
                    self.assertTrue(known_start(row, lambda words: f'"{words}"' in LAUNCHER_TEXT),
                                    f"{text}: no menu row {row!r}")
        self.assertGreater(seen, 15)
        self.assertGreater(rows, 6)
        self.assertIn("HELP", labels)

    def test_the_rows_home_never_points_at_xmb_categories(self):
        rows = " ".join(line for topic in tvhelp.topics(cross=False) for line in topic.lines)
        for words in ("AT THE FAR LEFT", "AT THE FAR RIGHT", "TV & VIDEO HOLDS"):
            self.assertNotIn(words, rows)


class HelpModelTest(unittest.TestCase):
    def test_up_and_down_choose_a_topic_and_stop_at_the_ends(self):
        model = tvhelp.HelpModel(tvhelp.topics())
        self.assertIsNone(model.key("up"))
        self.assertEqual(model.focus, 0)
        for _ in range(30):
            model.key("down")
        self.assertEqual(model.focus, len(model.topics) - 1)

    def test_a_topic_is_read_scrolled_and_left_and_b_closes_help(self):
        model = tvhelp.HelpModel(tvhelp.topics())
        model.key("down")
        self.assertIsNone(model.key("activate"))
        self.assertTrue(model.reading)
        self.assertEqual(model.key("down"), tvhelp.SCROLL_DOWN)
        self.assertEqual(model.key("up"), tvhelp.SCROLL_UP)
        self.assertEqual(model.focus, 1, "scrolling does not change the topic")
        self.assertIsNone(model.key("back"))
        self.assertFalse(model.reading)
        model.key("right")
        self.assertTrue(model.reading)
        model.key("left")
        self.assertFalse(model.reading)
        self.assertEqual(model.key("back"), tvhelp.CLOSE)

    def test_a_long_list_keeps_the_focus_in_view(self):
        model = tvhelp.HelpModel(tvhelp.topics())
        self.assertEqual(model.visible(50), range(len(model.topics)))
        for _ in range(len(model.topics)):
            model.key("down")
            shown = model.visible(4)
            self.assertEqual(len(shown), 4)
            self.assertIn(model.focus, shown)

    def test_every_prompt_names_the_keyboard_keys(self):
        model = tvhelp.HelpModel(tvhelp.topics())
        for family in FAMILIES:
            self.assertIn("START THE TOUR", model.prompt(family))
            self.assertTrue(quick.names_keyboard_keys(model.prompt(family)))
        model.key("down")
        model.key("activate")
        for family in FAMILIES:
            self.assertIn("SCROLLS", model.prompt(family))
            self.assertTrue(quick.names_keyboard_keys(model.prompt(family)))

    def test_the_settings_entry_opens_help(self):
        entry = next(entry for entry in tvscreens.ENTRIES if entry.label == "HELP")
        self.assertEqual((entry.kind, entry.target), (tvscreens.VIEW, "help"))
        self.assertEqual(tvscreens.ENTRIES[-1].label, "BACK")
        self.assertEqual(tvscreens.ENTRIES[-2].label, "HELP", "HELP is last, above BACK")


class FakeScreen:
    """Just enough of a curses window for run_classic."""

    def __init__(self, keys, height=24, width=80):
        self.keys = list(keys)
        self.size = (height, width)
        self.frames: list[list[str]] = []
        self.rows: list[str] = []

    def erase(self):
        self.rows = []

    def getmaxyx(self):
        return self.size

    def border(self):
        pass

    def addnstr(self, row, column, text, limit):
        assert 0 < row < self.size[0] - 1 and column + min(len(text), limit) <= self.size[1] - 1, (row, column, text)
        self.rows.append(text[:limit])

    def refresh(self):
        self.frames.append(self.rows)

    def read_key(self):
        return self.keys.pop(0)


@unittest.skipIf(curses is None, "the classic screen needs curses")
class ClassicTest(unittest.TestCase):
    def test_lines_wrap_to_the_width_with_a_gap_between_paragraphs(self):
        topic = tvhelp.topics(cross=False, tour=False)[0]
        lines = tvhelp.classic_lines(topic, 40)
        self.assertTrue(all(len(line) <= 40 for line in lines))
        self.assertEqual(lines.count(""), len(topic.lines) - 1)

    def test_the_list_then_a_topic_then_back_out(self):
        keys = [curses.KEY_DOWN, 10, curses.KEY_DOWN, curses.KEY_UP, 27, 27]
        screen = FakeScreen(keys)
        tvhelp.run_classic(screen, screen.read_key)
        items = tvhelp.topics(cross=False, tour=False)
        self.assertIn(tvhelp.CLASSIC_TITLE, screen.frames[0])
        self.assertTrue(any(items[0].title in row and row.startswith(">") for row in screen.frames[0]))
        self.assertIn(items[1].title, screen.frames[2])  # the second topic, opened
        self.assertIn(tvhelp.CLASSIC_TEXT_HINT, screen.frames[2])
        self.assertIn(tvhelp.CLASSIC_LIST_HINT, screen.frames[-1])
        self.assertEqual(screen.keys, [], "the second Esc left HELP")
        self.assertNotIn("THE TOUR", " ".join(" ".join(frame) for frame in screen.frames))

    def test_a_small_terminal_still_draws(self):
        screen = FakeScreen([10, curses.KEY_DOWN] + [curses.KEY_DOWN] * 30 + [27, 27], height=12, width=40)
        tvhelp.run_classic(screen, screen.read_key)
        self.assertEqual(screen.keys, [])

    def test_the_hints_name_the_keyboard_keys(self):
        for hint in (tvhelp.CLASSIC_LIST_HINT, tvhelp.CLASSIC_TEXT_HINT):
            self.assertTrue(quick.names_keyboard_keys(hint), hint)

    def test_the_classic_settings_menu_has_help(self):
        spec = importlib.util.spec_from_file_location("couchliteos_launcher_for_help", HERE / "couchliteos-launcher.py")
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        self.assertEqual(launcher.SETTINGS_MENU[-2:], ("HELP", "BACK"))


class StylesheetTest(unittest.TestCase):
    def test_no_text_under_28_px_at_1080p_and_every_class_is_styled(self):
        css = tvhelp.stylesheet(theme.FALLBACK, tvlayout.Layout(1920, 1080))
        for size in re.findall(r"font-size: (\d+)px", css):
            self.assertGreaterEqual(int(size), tvlayout.MIN_FONT)
        for name in ("tv-band", "tv-rule", "tv-card-title", "tv-card-line", "tv-chip-keys", "tv-chip-words",
                     "tv-dot", "tv-on", "tv-help-pane", "tv-reading", "tv-help-title", "tv-help-line", "tv-open"):
            self.assertIn(f".{name}", css)
        self.assertNotIn("var(", css)  # GTK 4.14 has no CSS variables

    def test_it_follows_the_theme_and_the_screen_size(self):
        small = tvhelp.stylesheet(theme.FALLBACK, tvlayout.Layout(1280, 720))
        big = tvhelp.stylesheet(theme.FALLBACK, tvlayout.Layout(1920, 1080))
        self.assertNotEqual(small, big)
        self.assertIn(f"#{theme.FALLBACK.colours['accent']}", big)


if __name__ == "__main__":
    unittest.main()
