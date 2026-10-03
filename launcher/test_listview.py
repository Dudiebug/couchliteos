import testenv  # noqa: F401  (first: scratch run and state directories)
import unittest

import couchliteos_listview as listview


class Screen:
    def __init__(self, height=24, width=80):
        self.size = (height, width)
        self.cells = {}

    def getmaxyx(self):
        return self.size

    def addnstr(self, row, column, text, length):
        self.cells[row] = (column, text[:length])


class ScrollWindowTest(unittest.TestCase):
    def test_a_list_that_fits_is_never_scrolled(self):
        for selected in range(5):
            self.assertEqual(listview.scroll_window(selected, 5, 5), 0)
            self.assertEqual(listview.scroll_window(selected, 5, 9), 0)

    def test_the_window_always_contains_the_selection_and_stays_inside_the_list(self):
        for count in (1, 2, 7, 20, 41):
            for visible in (1, 2, 5, 6, 13):
                for selected in range(count):
                    first = listview.scroll_window(selected, count, visible)
                    self.assertLessEqual(0, first, (selected, count, visible))
                    self.assertLessEqual(first + min(visible, count), count, (selected, count, visible))
                    self.assertLessEqual(first, selected, (selected, count, visible))
                    self.assertLess(selected, first + visible, (selected, count, visible))

    def test_the_cursor_moves_inside_the_window_instead_of_sticking_to_its_edge(self):
        # Going up one row must not scroll the list again: the window only moves
        # once the cursor leaves the middle of it.
        firsts = [listview.scroll_window(selected, 20, 6) for selected in range(20)]
        self.assertEqual(firsts, sorted(firsts))
        self.assertEqual(firsts[0], 0)
        self.assertEqual(firsts[-1], 14)
        self.assertEqual(listview.scroll_window(10, 20, 6), 7)
        self.assertEqual(listview.scroll_window(9, 20, 6), 6)

    def test_out_of_range_selection_is_clamped(self):
        self.assertEqual(listview.scroll_window(99, 10, 4), 6)
        self.assertEqual(listview.scroll_window(-3, 10, 4), 0)


class DrawRowsTest(unittest.TestCase):
    def draw(self, rows, selected, top=5, bottom=10, height=24):
        screen = Screen(height)
        listview.draw_rows(screen, rows, selected, top, bottom, 4)
        return screen.cells

    def test_a_short_list_is_drawn_whole_without_markers(self):
        cells = self.draw(["a", "b", "c"], 1)
        self.assertEqual({row: text for row, (_c, text) in cells.items()}, {5: "   a", 6: ">  b", 7: "   c"})

    def test_a_long_list_shows_the_selection_and_says_more_is_hidden(self):
        rows = [f"item {number}" for number in range(30)]
        for selected in (0, 13, 29):
            cells = self.draw(rows, selected)
            shown = [text for _row, (_c, text) in sorted(cells.items())]
            self.assertIn(f">  item {selected}", shown)
            self.assertTrue(all(row < 10 for row in cells), cells)  # never below the list area
            self.assertEqual(any(text.startswith("^") for text in shown), selected > 0)
            self.assertEqual(any(text.startswith("v") for text in shown), selected < 29)

    def test_nothing_is_drawn_outside_the_screen_or_the_border(self):
        cells = self.draw([f"item {number}" for number in range(30)], 29, top=5, bottom=40, height=12)
        self.assertTrue(all(row < 11 for row in cells), cells)

    def test_no_selection_shows_the_start_of_the_list(self):
        cells = self.draw([f"item {number}" for number in range(30)], None)
        self.assertEqual(cells[5][1], "   item 0")


if __name__ == "__main__":
    unittest.main()
