import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.machinery
import importlib.util
import pathlib
import tempfile
import unittest

import couchliteos_pointer as pointer
import couchliteos_session as session

REPO = pathlib.Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader("lofi_radio", str(REPO / "scripts/couchliteos-lofi-radio"))
_spec = importlib.util.spec_from_loader("lofi_radio", _loader)
radio = importlib.util.module_from_spec(_spec)
_loader.exec_module(radio)


class LofiRadioTests(unittest.TestCase):
    def test_chrome_first_then_firefox_then_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            self.assertIsNone(radio.browser_command("https://x/", root))
            (root / "usr/bin").mkdir(parents=True)
            (root / "usr/bin/firefox-esr").touch()
            self.assertEqual(radio.browser_command("https://x/", root), ["/usr/bin/firefox-esr", "--kiosk", "https://x/"])
            (root / "usr/bin/google-chrome-stable").touch()
            command = radio.browser_command("https://x/", root)
            self.assertEqual(command[0], "/usr/bin/google-chrome-stable")
            self.assertIn("--kiosk", command)
            self.assertEqual(command[-1], "https://x/")

    def test_it_opens_the_radio_site_and_ships_no_page(self):
        self.assertEqual(radio.URL, "https://radio.dudiebug.net/")
        self.assertFalse((REPO / "overlay/usr/share/couchliteos/lofi-radio").exists())

    def test_controller_mouse_and_window_focus(self):
        self.assertIn("lofi-radio", pointer.BROWSER_IDS)
        self.assertIn("app_id:google-chrome", session.WINDOW_MATCHES["lofi-radio"])


if __name__ == "__main__":
    unittest.main()
