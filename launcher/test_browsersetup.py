"""ADD A WEB BROWSER: rows, the request file, progress from the status file, NOT NOW."""

import curses
import json
import pathlib
import shutil
import tempfile
import unittest

import couchliteos_browser as browser
import couchliteos_browsersetup as bs
from test_softwareupdate import TextScreen, flat

ENTER, ESC, DOWN, UP = 10, 27, curses.KEY_DOWN, curses.KEY_UP


class BrowserSetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.screen = TextScreen()
        self.now = 1000.0
        self.script = []
        self.awake = 0
        self.have = []

    def read_key(self, _screen):
        self.now += 0.5
        if not self.script:
            raise AssertionError("the screen asked for more keys than the test scripted")
        item = self.script.pop(0)
        if callable(item):
            item()
            return -1
        return -1 if item is None else item

    def keep_awake(self):
        self.awake += 1

    def make(self):
        return bs.BrowserSetup(
            self.screen, read_key=self.read_key, keep_awake=self.keep_awake, installed=lambda: list(self.have),
            run_dir=self.tmp, clock=lambda: self.now)

    def status(self, phase, message="", percent=None):
        def write():
            (self.tmp / browser.STATUS_NAME).write_text(
                json.dumps({"phase": phase, "message": message, "percent": percent, "browser": "firefox"}))
        return write

    def installed(self, name):
        def mark():
            self.have.append(name)
        return mark

    # -- the main screen -------------------------------------------------------------------------

    def test_rows_and_one_line_each_on_what_it_is_for(self):
        self.script = [ESC]
        self.make().run()
        text = self.screen.frames[0]
        for line in ("FIREFOX", "GOOGLE CHROME", "NOT NOW", *bs.ABOUT):
            self.assertIn(line, text)
        self.assertIn("ADD A WEB BROWSER", text)

    def test_installed_browsers_are_marked(self):
        self.have = ["chrome"]
        self.assertEqual(self.make().rows(), ["FIREFOX", "GOOGLE CHROME  (INSTALLED)", "NOT NOW"])

    def test_not_now_and_b_install_nothing(self):
        for keys in ([DOWN, DOWN, ENTER], [ESC], [UP, ENTER]):
            self.script = list(keys)
            self.assertIsNone(self.make().run(), keys)
            self.assertFalse((self.tmp / browser.REQUEST_NAME).exists())

    def test_an_installed_browser_is_not_installed_again(self):
        self.have = ["firefox"]
        self.script = [ENTER, ESC]
        self.make().run()
        self.assertIn("FIREFOX IS ALREADY INSTALLED", flat(self.screen.frames[-1]))
        self.assertFalse((self.tmp / browser.REQUEST_NAME).exists())

    # -- installing ------------------------------------------------------------------------------

    def test_picking_writes_the_request_and_follows_the_status_to_done(self):
        (self.tmp / browser.STATUS_NAME).write_text('{"phase": "failed"}')  # from an earlier try
        requests = []
        self.script = [
            DOWN, ENTER,
            lambda: requests.append((self.tmp / browser.REQUEST_NAME).read_text()),
            self.status("downloading", "DOWNLOADING GOOGLE CHROME...", 30),
            self.status("installing", "INSTALLING GOOGLE CHROME...", 70),
            lambda: (self.installed("chrome")(), self.status("done", "GOOGLE CHROME IS INSTALLED", 100)()),
            ESC,
        ]
        self.assertTrue(self.make().run())
        self.assertEqual(requests, ["chrome\n"])
        self.assertEqual(list(self.tmp.glob(".*")), [], "no temporary request file is left")
        frames = "\n".join(self.screen.frames)
        self.assertIn("INSTALLING GOOGLE CHROME", frames)
        self.assertIn("DOWNLOADING GOOGLE CHROME...", frames)
        self.assertIn("70%", frames)
        self.assertIn("GOOGLE CHROME IS INSTALLED", self.screen.frames[-1])
        self.assertGreaterEqual(self.awake, 4)
        self.assertEqual(self.screen.timeouts[-1], bs.NORMAL_MS)

    def test_b_while_installing_only_says_to_wait(self):
        self.script = [ENTER, self.status("installing", "INSTALLING FIREFOX...", 70), ESC,
                       lambda: (self.installed("firefox")(), self.status("done", "FIREFOX IS INSTALLED", 100)()), ESC]
        self.assertTrue(self.make().run())
        self.assertTrue(any(bs.WAIT in frame for frame in self.screen.frames))

    def test_a_failure_is_shown_until_a_and_the_screen_reports_it(self):
        message = "COULD NOT REACH THE INTERNET: CHECK SETTINGS > NETWORK"
        self.script = [ENTER, self.status("failed", message), ENTER, ESC]
        self.assertFalse(self.make().run())
        self.assertIn(bs.PRESS_A, self.screen.frames[-2])
        self.assertIn(message, flat(self.screen.frames[-1]))

    def test_a_service_that_never_answers_counts_as_not_started(self):
        self.script = [ENTER] + [None] * (bs.START_WAIT * 2) + [ENTER, ESC]
        self.assertFalse(self.make().run())
        self.assertIn(bs.NOT_STARTED, flat(self.screen.frames[-1]))

    def test_a_request_that_cannot_be_written_says_so(self):
        setup = self.make()
        setup.run_dir = self.tmp / "missing"
        self.script = [ENTER, ESC]
        self.assertFalse(setup.run())
        self.assertIn("COULD NOT START THE INSTALL", flat(self.screen.frames[-1]))


if __name__ == "__main__":
    unittest.main()
