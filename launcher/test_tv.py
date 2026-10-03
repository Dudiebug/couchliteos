import testenv  # noqa: F401  (first: scratch run and state directories)
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import couchliteos_apps as apps
import couchliteos_home as home
import couchliteos_stream as stream

PATH = pathlib.Path(__file__).with_name("couchliteos-tv.py")


def load_tv():
    spec = importlib.util.spec_from_file_location("couchliteos_tv_for_tests", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    import gi  # noqa: F401

    gi.require_version("Gtk", "4.0")
    HAVE_GTK = True
except (ImportError, ValueError):
    HAVE_GTK = False


class StartTest(unittest.TestCase):
    """make test runs without PyGObject: the file still loads and says it could not start."""

    def test_it_loads_without_gtk_and_exits_3(self):
        module = load_tv()
        with mock.patch.object(module, "Gtk", None), mock.patch.object(module, "GI_ERROR", "no gi"), \
                mock.patch.object(module.sys, "stderr"):
            self.assertEqual(module.main([]), module.INIT_FAILED)
        self.assertEqual(module.INIT_FAILED, 3)

    @unittest.skipUnless(HAVE_GTK, "needs PyGObject with GTK 4")
    def test_without_a_display_it_exits_3_and_writes_no_ready_mark(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {key: value for key, value in os.environ.items() if key not in ("WAYLAND_DISPLAY", "DISPLAY")}
            env["COUCHLITEOS_RUN_DIR"] = directory
            result = subprocess.run([sys.executable, str(PATH)], env=env, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertFalse((pathlib.Path(directory) / "launcher-ready").exists())


class HomeKeysTest(unittest.TestCase):
    """The key handling of the home screen, with the drawing left out (no GTK needed)."""

    HOST = stream.Host(name="Gaming-PC", uuid="U1", local="192.168.1.50", apps=("Desktop", "Steam"))

    def tv(self, applications=()):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.model = home.HomeModel(
            hosts=lambda: [self.HOST], applications=lambda: apps.LoadResult(tuple(applications), ()),
            installed=lambda _app: True, history=dict,
        )
        tv.status = ""
        tv.render_home = mock.Mock()
        tv.render_bar = mock.Mock()
        tv.open_active = mock.Mock()
        tv.run_action = mock.Mock()
        tv.launch_app = mock.Mock()
        tv.after_launch = mock.Mock()
        return module, tv

    def test_arrows_move_the_focus_and_redraw(self):
        _module, tv = self.tv()
        tv.home_key("right")
        self.assertEqual(tv.model.focused().label, "STEAM")
        tv.home_key("down")
        self.assertEqual(tv.model.focused().label, "SETTINGS")  # APPS is empty: skipped
        self.assertEqual(tv.render_home.call_count, 2)
        tv.home_key("back")
        self.assertEqual(tv.model.focus, (0, 0))

    def test_enter_runs_the_focused_tiles_action(self):
        _module, tv = self.tv()
        tv.home_key("activate")
        tv.run_action.assert_called_once_with(("stream", self.HOST, "Desktop"))

    def test_home_opens_the_running_apps(self):
        _module, tv = self.tv()
        tv.home_key("home")
        tv.open_active.assert_called_once_with()

    def test_a_shortcut_button_starts_its_app(self):
        tool = apps.Application(id="tool", name="TOOL", kind="command", command="/bin/true", status_id="tool", shortcut="view")
        module, tv = self.tv([tool])
        with mock.patch.object(module, "visible_applications", return_value=apps.LoadResult((tool,), ())):
            tv.home_key("shortcut:view")
            tv.launch_app.assert_called_once_with(tool)
            tv.home_key("shortcut:lb")
        self.assertEqual(tv.status, f"NO BUTTON USES THE {apps.SHORTCUTS['lb']} SHORTCUT")

    def test_system_tiles_open_their_screens(self):
        _module, tv = self.tv()
        tv.open_settings, tv.open_power, tv.open_screen = mock.Mock(), mock.Mock(), mock.Mock()
        tv.open_view("settings")
        tv.open_settings.assert_called_once_with()
        tv.open_view("power")
        tv.open_power.assert_called_once_with()
        tv.open_view("hosts")
        tv.open_view("software-update")
        self.assertEqual([call.args for call in tv.open_screen.call_args_list], [("streaming",), ("software-update",)])

    def test_a_remote_desktop_tile_starts_in_the_classic_screen(self):
        _module, tv = self.tv()
        tv.open_screen = mock.Mock()
        app = apps.Application(id="office", name="OFFICE", kind="rdp", connection="office", status_id="office")
        self.assertFalse(tv.prepare_remote_desktop(app))
        tv.open_screen.assert_called_once_with("connect", "office")


class ScreensTest(unittest.TestCase):
    """Settings, the classic screens on top and the start, with the drawing left out."""

    def tv(self):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.status = ""
        tv.mode = "settings"
        tv.child_pid = None
        tv.child_name = ""
        tv.starting = False
        tv.start_steps = []
        tv.settings = module.tvscreens.SettingsModel()
        tv.updates = mock.Mock(enabled=True, online=lambda: True)
        for name in ("render_settings", "open_screen", "open_active", "close_screen", "launch_by_id", "show",
                     "render_current", "after_screen", "watch_update", "open_whatsnew", "autostream"):
            setattr(tv, name, mock.Mock())
        return module, tv

    def focus(self, tv, label):
        tv.settings.focus = tv.settings.rows().index(label)

    def test_settings_entries_open_their_screen_app_or_view(self):
        module, tv = self.tv()
        self.focus(tv, "NETWORK")
        tv.settings_key("activate")
        tv.open_screen.assert_called_once_with("network")
        self.focus(tv, "TAILSCALE")
        tv.settings_key("activate")
        tv.launch_by_id.assert_called_once_with("tailscale")
        self.focus(tv, "ACTIVE APPLICATIONS")
        tv.settings_key("activate")
        tv.open_active.assert_called_once_with()
        self.focus(tv, "CHECK FOR UPDATES")
        tv.settings_key("activate")
        tv.updates.set_enabled.assert_called_once_with(False)
        self.assertEqual(tv.status, "UPDATE CHECK OFF: NOTHING IS SENT")
        tv.settings_key("back")
        tv.close_screen.assert_called_once_with()

    def test_a_key_while_a_classic_screen_is_open_puts_it_back_in_front(self):
        module, tv = self.tv()
        tv.child_pid = 42
        tv.idle = mock.Mock(**{"key.return_value": False})
        tv.settings_key = mock.Mock()
        with mock.patch.object(module, "Gdk", create=True) as gdk, \
                mock.patch.object(module.session, "focus_launcher") as focus, \
                mock.patch.object(module.display, "confirm_restore"):
            gdk.keyval_name.return_value = "Return"
            self.assertTrue(tv.on_key(None, 0, 0, None))
        focus.assert_called_once_with(module.tvscreens.CHILD_TITLE)
        tv.settings_key.assert_not_called()

    def test_the_start_runs_whats_new_then_setup_then_the_auto_stream(self):
        module, tv = self.tv()
        tv.starting, tv.start_steps = True, ["whatsnew", "setup", "update", "autostream"]
        tv.open_screen.return_value = True
        with mock.patch.object(module.whatsnew, "due", return_value=("0.3.0", "0.2.7")), \
                mock.patch.object(module.tvscreens, "setup_due", return_value=True), \
                mock.patch.object(module.tvscreens, "update_running", return_value=False):
            tv.continue_start()
            tv.open_whatsnew.assert_called_once_with("0.3.0", "0.2.7")
            tv.continue_start()
            tv.open_screen.assert_called_once_with("setup")
            tv.autostream.assert_not_called()
            tv.continue_start()
        tv.autostream.assert_called_once_with()
        self.assertFalse(tv.starting)

    def test_when_a_classic_screen_closes_its_markers_decide_what_comes_next(self):
        module, tv = self.tv()
        run = pathlib.Path(tempfile.mkdtemp(dir=os.environ["COUCHLITEOS_RUN_DIR"]))
        with mock.patch.object(module, "GLib", create=True), \
                mock.patch.object(module.session.Session, "run_dir", run), \
                mock.patch.object(module.session, "focus_launcher"), \
                mock.patch.object(module.display, "log"):  # never the real /var/log
            for marker, content, expected in (("reopen-display", "", ("display",)), ("reopen-setup", "", ("setup",))):
                (run / marker).write_text(content)
                tv.child_pid, tv.child_name = 7, "display"
                tv.open_screen.reset_mock()
                tv.on_child_exit(7, 0)
                self.assertIsNone(tv.child_pid)
                tv.open_screen.assert_called_once_with(*expected)
                (run / marker).unlink(missing_ok=True)
            (run / "update-watch").write_text("0.3.0\n")
            tv.child_pid, tv.child_name = 8, "software-update"
            tv.on_child_exit(8, 256)
            tv.watch_update.assert_called_once_with("0.3.0")
            self.assertEqual(tv.status, "SOFTWARE UPDATE CLOSED WITH AN ERROR (1)")
            tv.after_screen.assert_called()


if __name__ == "__main__":
    unittest.main()
