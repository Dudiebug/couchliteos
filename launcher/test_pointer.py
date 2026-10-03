import testenv  # noqa: F401  (first: scratch run and state directories)
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import couchliteos_apps as apps
import couchliteos_pointer as pointer


def app(app_id, kind="command", command="", terminal=False):
    return apps.Application(id=app_id, name=app_id.upper(), kind=kind, command=command,
                            status_id=app_id, terminal=terminal)


class DefaultForTest(unittest.TestCase):
    def test_the_built_in_browsers_get_the_controller_mouse(self):
        self.assertTrue(pointer.default_for(app("firefox", kind="request")))
        self.assertTrue(pointer.default_for(app("google-chrome", kind="request")))

    def test_games_and_streaming_clients_keep_the_controller(self):
        for app_id in ("moonlight", "chiaki-ng", "terminal", "tailscale"):
            self.assertFalse(pointer.default_for(app(app_id, kind="request")), app_id)
        self.assertFalse(pointer.default_for(app("rdp-work", kind="rdp")))

    def test_user_added_web_applications_are_found_by_their_browser_binary(self):
        for command in ("/usr/bin/google-chrome-stable", "/usr/bin/google-chrome", "/usr/bin/firefox-esr",
                        "/usr/bin/firefox", "/usr/bin/chromium", "chromium-browser"):
            self.assertTrue(pointer.default_for(app("youtube", command=command)), command)

    def test_other_commands_terminal_apps_and_empty_commands_do_not(self):
        self.assertFalse(pointer.default_for(app("steam", command="/usr/games/steam")))
        self.assertFalse(pointer.default_for(app("kodi", command="/usr/bin/kodi")))
        # A terminal app that happens to run a browser binary is a text app in foot.
        self.assertFalse(pointer.default_for(app("lynx", command="/usr/bin/firefox", terminal=True)))
        self.assertFalse(pointer.default_for(app("empty", command="")))
        # Only the file name counts, not a directory that looks like a browser.
        self.assertFalse(pointer.default_for(app("tool", command="/opt/firefox/updater")))

    def test_a_request_app_is_judged_by_id_not_by_its_command(self):
        self.assertFalse(pointer.default_for(app("other", kind="request", command="/usr/bin/firefox")))

    def test_apps_without_a_terminal_attribute_are_treated_as_graphical(self):
        class Bare:
            id, kind, command = "web", "command", "/usr/bin/chromium"

        self.assertTrue(pointer.default_for(Bare()))


class ModesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.flag = pathlib.Path(directory.name) / "pointer-mode"
        self.modes = pointer.Modes(self.flag)

    def test_the_default_flag_is_the_one_gamepad_nav_reads(self):
        self.assertEqual(pointer.FLAG, pathlib.Path(os.environ["COUCHLITEOS_RUN_DIR"], "pointer-mode"))
        self.assertEqual(pointer.Modes().flag, pointer.FLAG)

    def test_enabled_follows_the_default_until_toggled(self):
        chrome, moonlight = app("google-chrome", kind="request"), app("moonlight", kind="request")
        self.assertTrue(self.modes.enabled(chrome))
        self.assertFalse(self.modes.enabled(moonlight))
        self.assertFalse(self.modes.toggle(chrome))
        self.assertFalse(self.modes.enabled(chrome))
        self.assertTrue(self.modes.toggle(moonlight))
        self.assertTrue(self.modes.enabled(moonlight))
        self.assertTrue(self.modes.toggle(chrome))
        self.assertTrue(self.modes.enabled(chrome))

    def test_apply_writes_the_flag_for_the_app_coming_to_the_front(self):
        chrome, moonlight = app("google-chrome", kind="request"), app("moonlight", kind="request")
        self.modes.apply(chrome)
        self.assertEqual(self.modes.front, "google-chrome")
        self.assertTrue(self.flag.exists())
        self.modes.apply(moonlight)
        self.assertEqual(self.modes.front, "moonlight")
        self.assertFalse(self.flag.exists())

    def test_toggling_the_front_app_updates_the_flag_at_once(self):
        chrome = app("google-chrome", kind="request")
        self.modes.apply(chrome)
        self.modes.toggle(chrome)
        self.assertFalse(self.flag.exists())
        self.modes.toggle(chrome)
        self.assertTrue(self.flag.exists())

    def test_toggling_an_app_behind_leaves_the_flag_alone(self):
        chrome, moonlight = app("google-chrome", kind="request"), app("moonlight", kind="request")
        self.modes.apply(chrome)
        self.modes.toggle(moonlight)
        self.assertTrue(self.flag.exists(), "Chrome is still in front with its mouse")
        # The choice is remembered for when Moonlight comes forward.
        self.modes.apply(moonlight)
        self.assertTrue(self.flag.exists())

    def test_nothing_applied_yet_means_toggles_never_write(self):
        self.modes.toggle(app("moonlight", kind="request"))
        self.assertFalse(self.flag.exists())

    def test_a_missing_run_directory_is_not_an_error(self):
        modes = pointer.Modes(self.flag.parent / "missing" / "pointer-mode")
        modes.apply(app("google-chrome", kind="request"))  # touch fails: no error
        modes.apply(app("moonlight", kind="request"))  # unlink of a missing file: no error
        with mock.patch.object(pathlib.Path, "unlink", side_effect=PermissionError("read-only")):
            modes.write(False)

    def test_turning_off_twice_is_harmless(self):
        self.modes.write(False)
        self.modes.write(True)
        self.modes.write(True)
        self.modes.write(False)
        self.modes.write(False)
        self.assertFalse(self.flag.exists())


if __name__ == "__main__":
    unittest.main()
