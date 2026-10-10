import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.machinery
import importlib.util
import pathlib
import re
import tempfile
import unittest

import couchliteos_pointer as pointer
import couchliteos_session as session

REPO = pathlib.Path(__file__).resolve().parents[1]
PAGE = REPO / "overlay/usr/share/couchliteos/lofi-radio"
_loader = importlib.machinery.SourceFileLoader("lofi_radio", str(REPO / "scripts/couchliteos-lofi-radio"))
_spec = importlib.util.spec_from_loader("lofi_radio", _loader)
radio = importlib.util.module_from_spec(_spec)
_loader.exec_module(radio)


class LofiRadioTests(unittest.TestCase):
    def test_chrome_first_then_firefox_then_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            self.assertIsNone(radio.browser_command("http://127.0.0.1:1/", root))
            (root / "usr/bin").mkdir(parents=True)
            (root / "usr/bin/firefox-esr").touch()
            self.assertEqual(radio.browser_command("http://x/", root), ["/usr/bin/firefox-esr", "--kiosk", "http://x/"])
            (root / "usr/bin/google-chrome-stable").touch()
            command = radio.browser_command("http://x/", root)
            self.assertEqual(command[0], "/usr/bin/google-chrome-stable")
            self.assertIn("--kiosk", command)
            self.assertEqual(command[-1], "http://x/")

    def test_the_rhodes_samples_are_not_shipped_and_the_page_does_not_ask_for_them(self):
        self.assertFalse((PAGE / "samples/erh").exists())
        html = (PAGE / "index.html").read_text(encoding="utf-8")
        self.assertIn("if(k==='rhodes'&&S.noRhodes)continue;", html)
        self.assertIn("$nr.onclick=()=>{if(/[?&]safe=1/.test(location.search))return;", html)
        self.assertIn("index.html?safe=1", (REPO / "scripts/couchliteos-lofi-radio").read_text())

    def test_every_other_sample_the_page_names_is_shipped(self):
        html = (PAGE / "index.html").read_text(encoding="utf-8")
        dirs = set(re.findall(r"dir:'([a-z]+)'", html)) - {"erh"}
        for name in dirs | {"drums", "shinym"}:
            self.assertTrue(any((PAGE / "samples" / name).glob("*.mp3")), name)

    def test_controller_mouse_and_window_focus(self):
        self.assertIn("lofi-radio", pointer.BROWSER_IDS)
        self.assertIn("title:Lo-fi Radio", session.WINDOW_MATCHES["lofi-radio"])


if __name__ == "__main__":
    unittest.main()
