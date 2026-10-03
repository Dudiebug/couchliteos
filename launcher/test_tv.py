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

    def test_screens_not_in_the_tv_interface_yet_say_so(self):
        module, tv = self.tv()
        tv.show_text = mock.Mock()
        tv.open_view("settings")
        title, body, hint = tv.show_text.call_args.args[1:]
        self.assertEqual(title, "SETTINGS")
        self.assertIn("ESC", hint)
        for name, (title, body) in module.NOT_YET.items():
            self.assertEqual(body, body.upper())


if __name__ == "__main__":
    unittest.main()
