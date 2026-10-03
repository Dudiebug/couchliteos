import testenv  # noqa: F401  (first: scratch run and state directories)
"""couchliteos-browser (root side): apt arguments, refusals, status phases, refresh, request file, web apps."""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

import couchliteos_browser as browser

GIB = 1 << 30
ROUTE = "default via 192.168.1.1 dev enp2s0\n"


class Runner:
    """Records every argv; `replies` maps a word of the command line to (returncode, output)."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, argv, **_kwargs):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        reply = (0, ROUTE if argv[:2] == ["ip", "-4"] else "")
        for word, answer in self.replies.items():
            if word in argv:
                reply = answer
        return subprocess.CompletedProcess(argv, reply[0], stdout=reply[1])

    def apt(self):
        """The apt-get calls without the fixed source options."""
        options = list(browser.APT_OPTIONS)
        return [call[1 + len(options):] for call in self.calls if call[0] == "apt-get"]


class RecordingStatus(browser.Status):
    def __init__(self, path):
        super().__init__(path)
        self.history = []

    def set(self, phase, message, percent=None):
        self.history.append((phase, message, percent))
        super().set(phase, message, percent)

    def phases(self):
        return [phase for phase, _message, _percent in self.history]


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.root = self.tmp / "root"
        self.run_dir = self.tmp / "run"
        self.runner = Runner()
        self.free = 5 * GIB
        self.logged = []
        self.env = browser.Env(
            runner=self.runner, free_bytes=lambda _path: self.free, root=self.root, run_dir=self.run_dir,
            euid=lambda: 0, log=self.logged.append,
        )
        self.status = RecordingStatus(self.run_dir / browser.STATUS_NAME)

    def put(self, rel, text=""):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def written(self):
        return json.loads((self.run_dir / browser.STATUS_NAME).read_text())


class InstallTest(Case):
    def test_apt_reads_only_couchliteos_sources_and_installs_without_recommends(self):
        browser.install(self.env, self.status, browser.BROWSERS["firefox"])
        for call in (call for call in self.runner.calls if call[0] == "apt-get"):
            self.assertEqual(call[1:9], [
                "-o", "Dir::Etc::sourcelist=/usr/share/couchliteos/apt/sources.list",
                "-o", "Dir::Etc::sourceparts=/usr/share/couchliteos/apt/sources.list.d",
                "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"])
        self.assertEqual(self.runner.apt(), [
            ["update", "--error-on=any"],
            ["install", "-y", "--no-install-recommends", "--download-only", "firefox-esr"],
            ["install", "-y", "--no-install-recommends", "firefox-esr"],
            ["clean"],
        ])

    def test_chrome_installs_its_package(self):
        browser.install(self.env, self.status, browser.BROWSERS["chrome"])
        self.assertEqual(self.runner.apt()[2], ["install", "-y", "--no-install-recommends", "google-chrome-stable"])

    def test_status_goes_checking_downloading_installing_done(self):
        browser.install(self.env, self.status, browser.BROWSERS["firefox"])
        self.assertEqual(self.status.phases(), ["checking", "downloading", "downloading", "installing", "done"])
        percents = [percent for _phase, _message, percent in self.status.history]
        self.assertEqual(percents, sorted(percents))
        self.assertEqual(self.written(),
                         {"phase": "done", "percent": 100, "message": "FIREFOX IS INSTALLED", "browser": "firefox"})

    def test_no_route_refuses_before_apt_runs(self):
        self.runner.replies = {"-4": (0, ""), "-6": (0, "")}
        with self.assertRaises(browser.BrowserFailed) as caught:
            browser.install(self.env, self.status, browser.BROWSERS["firefox"])
        self.assertEqual(caught.exception.message, browser.MSG_NETWORK)
        self.assertEqual(self.runner.apt(), [])

    def test_an_ipv6_only_network_is_a_network(self):
        self.runner.replies = {"-4": (0, ""), "-6": (0, "default via fe80::1 dev wlan0\n")}
        browser.install(self.env, self.status, browser.BROWSERS["firefox"])
        self.assertEqual(self.status.phases()[-1], "done")

    def test_an_unreachable_mirror_is_a_network_problem(self):
        self.runner.replies = {"update": (100, "E: Failed to fetch http://deb.debian.org/...")}
        with self.assertRaises(browser.BrowserFailed) as caught:
            browser.install(self.env, self.status, browser.BROWSERS["firefox"])
        self.assertEqual(caught.exception.message, browser.MSG_NETWORK)
        self.assertEqual(len(self.runner.apt()), 1)
        self.assertTrue(any("Failed to fetch" in line for line in self.logged), "apt's words go to the log")

    def test_too_little_free_space_refuses_with_plain_words(self):
        self.free = browser.MIN_FREE - 1  # a live stick without persistence
        with self.assertRaises(browser.BrowserFailed) as caught:
            browser.install(self.env, self.status, browser.BROWSERS["chrome"])
        self.assertEqual(caught.exception.message, "NOT ENOUGH FREE SPACE: A WEB BROWSER NEEDS ABOUT 1 GB")
        self.assertEqual(self.runner.apt(), [])

    def test_a_failed_install_points_at_the_log(self):
        self.runner.replies = {"firefox-esr": (100, "E: Sub-process /usr/bin/dpkg returned an error code (1)")}
        self.runner.replies["--download-only"] = (0, "")
        with self.assertRaises(browser.BrowserFailed) as caught:
            browser.install(self.env, self.status, browser.BROWSERS["firefox"])
        self.assertEqual(caught.exception.message, browser.MSG_FAILED)

    def test_remove_runs_apt_remove(self):
        browser.remove(self.env, self.status, browser.BROWSERS["chrome"])
        self.assertEqual(self.runner.apt(), [["remove", "-y", "google-chrome-stable"]])
        self.assertEqual(self.status.phases(), ["removing", "done"])


class RefreshTest(Case):
    def setUp(self):
        super().setUp()
        self.marker = self.put(browser.REFRESH_REL)

    def test_only_the_installed_browsers_are_installed_again(self):
        self.put("usr/bin/firefox-esr")
        browser.refresh(self.env)
        self.assertEqual(self.runner.apt(), [
            ["update", "--error-on=any"],
            ["install", "-y", "--no-install-recommends", "--reinstall", "firefox-esr"],
            ["clean"],
        ])
        self.assertFalse(self.marker.exists())

    def test_both_installed_means_both_in_one_apt_run(self):
        self.put("usr/bin/firefox-esr")
        self.put("usr/bin/google-chrome-stable")
        browser.refresh(self.env)
        self.assertEqual(self.runner.apt()[1][-2:], ["google-chrome-stable", "firefox-esr"])

    def test_no_browser_means_no_apt_and_the_marker_goes(self):
        browser.refresh(self.env)
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.marker.exists())

    def test_a_failure_keeps_the_marker_for_the_next_start(self):
        self.put("usr/bin/google-chrome-stable")
        self.runner.replies = {"update": (100, "offline")}
        with self.assertRaises(browser.BrowserFailed):
            browser.refresh(self.env)
        self.assertTrue(self.marker.exists())


class RequestTest(Case):
    def request(self, text):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / browser.REQUEST_NAME
        path.write_text(text)
        return path

    def test_the_request_names_the_browser_and_is_removed(self):
        path = self.request("chrome\n")
        self.assertEqual(browser.read_request(path), "chrome")
        self.assertFalse(path.exists())

    def test_anything_else_is_refused_and_still_removed(self):
        for text in ("opera\n", "firefox" * 20, "\xff"):
            path = self.request(text)
            with self.assertRaises(browser.BrowserFailed):
                browser.read_request(path)
            self.assertFalse(path.exists(), text)

    def test_a_symlink_is_not_followed(self):
        secret = self.put("etc/shadow", "chrome\n")
        self.run_dir.mkdir(parents=True)
        path = self.run_dir / browser.REQUEST_NAME
        path.symlink_to(secret)
        with self.assertRaises(browser.BrowserFailed):
            browser.read_request(path)
        self.assertTrue(secret.exists())

    def test_main_request_installs_and_writes_the_status_file(self):
        path = self.request("firefox\n")
        self.assertEqual(browser.main(["request"], self.env), 0)
        self.assertFalse(path.exists())
        self.assertEqual(self.written()["phase"], "done")
        self.assertIn(["install", "-y", "--no-install-recommends", "firefox-esr"], self.runner.apt())

    def test_main_reports_a_refusal_in_the_status_file(self):
        self.request("firefox\n")
        self.free = 0
        self.assertEqual(browser.main(["request"], self.env), 1)
        self.assertEqual(self.written()["phase"], "failed")
        self.assertEqual(self.written()["message"], browser.MSG_SPACE)

    def test_a_second_install_while_one_runs_says_so_and_drops_its_request(self):
        import fcntl
        path = self.request("chrome\n")
        with open(self.run_dir / browser.LOCK_NAME, "a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            self.assertEqual(browser.main(["request"], self.env), 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.written()["message"], browser.MSG_BUSY)
        self.assertEqual(self.runner.calls, [])

    def test_main_needs_root(self):
        self.env.euid = lambda: 1000
        self.assertEqual(browser.main(["install", "firefox"], self.env), 2)
        self.assertEqual(self.runner.calls, [])

    def test_refresh_writes_no_status(self):
        self.assertEqual(browser.main(["refresh"], self.env), 0)
        self.assertFalse((self.run_dir / browser.STATUS_NAME).exists())


class KioskCommandTest(Case):
    def test_chrome_is_preferred_when_installed(self):
        self.put("usr/bin/google-chrome-stable")
        self.put("usr/bin/firefox-esr")
        self.assertEqual(browser.kiosk_command("https://tv.example/", self.root), (
            "/usr/bin/google-chrome-stable", "--ozone-platform=wayland --kiosk --no-first-run https://tv.example/"))

    def test_firefox_alone_opens_in_kiosk_mode(self):
        self.put("usr/bin/firefox-esr")
        self.assertEqual(browser.kiosk_command("https://tv.example/?a=1&b=2", self.root),
                         ("/usr/bin/firefox-esr", "--kiosk 'https://tv.example/?a=1&b=2'"))

    def test_no_browser_means_none(self):
        self.assertIsNone(browser.kiosk_command("https://tv.example/", self.root))


if __name__ == "__main__":
    unittest.main()
