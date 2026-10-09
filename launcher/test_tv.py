import testenv  # noqa: F401  (first: scratch run and state directories)
import contextlib
import importlib.util
import os
import pathlib
import shutil
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
                     "render_current", "after_screen", "watch_update", "open_whatsnew", "autostream",
                     "refresh_settings"):
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

    def test_keys_go_to_the_screen_drawn_here(self):
        import curses

        module, tv = self.tv()
        tv.child_pid, tv.child = 42, mock.Mock()
        tv.idle = mock.Mock(**{"key.return_value": False})
        tv.ui_sound = mock.Mock()
        with mock.patch.object(module, "Gdk", create=True) as gdk, \
                mock.patch.object(module.session, "focus_launcher") as focus, \
                mock.patch.object(module.display, "confirm_restore"):
            gdk.ModifierType.CONTROL_MASK, gdk.ModifierType.ALT_MASK, gdk.ModifierType.SUPER_MASK = 4, 8, 64
            gdk.keyval_name.return_value, gdk.keyval_to_unicode.return_value = "Down", 0
            self.assertTrue(tv.on_key(None, 0, 0, 0))
            gdk.keyval_name.return_value, gdk.keyval_to_unicode.return_value = "a", ord("a")
            self.assertTrue(tv.on_key(None, 0, 0, 0))  # typing too (a Wi-Fi password)
            self.assertFalse(tv.on_key(None, 0, 0, 4))  # Ctrl+A is not for the screen
        self.assertEqual([call.args[0] for call in tv.child.send.call_args_list], [curses.KEY_DOWN, "a"])
        focus.assert_not_called()

    def test_a_screen_that_fails_before_drawing_opens_in_foot(self):
        module, tv = self.tv()
        tv.screen_closed = mock.Mock()
        tv.open_screen.return_value = True
        tv.child, tv.child_pid, tv.child_drawn, tv.mode, tv.child_return = mock.Mock(), 42, False, "classic", "settings"
        with mock.patch.object(module.display, "log"):
            tv.on_bridge_exit("display", "", 1)
        tv.open_screen.assert_called_once_with("display", "", foot=True)
        tv.show.assert_called_once_with("settings")
        tv.screen_closed.assert_not_called()
        self.assertIsNone(tv.child)
        self.assertIsNone(tv.child_pid)

    def test_a_screen_that_drew_and_closed_goes_back_to_its_page(self):
        module, tv = self.tv()
        tv.screen_closed = mock.Mock()
        tv.child, tv.child_pid, tv.child_drawn, tv.mode, tv.child_return = mock.Mock(), 42, True, "classic", "settings"
        tv.on_bridge_exit("display", "", 0)
        tv.open_screen.assert_not_called()
        tv.show.assert_called_once_with("settings")
        tv.screen_closed.assert_called_once_with("display", 0)

    def test_a_frame_is_drawn_on_the_classic_page(self):
        module, tv = self.tv()
        tv.child, tv.child_name, tv.classic_page = mock.Mock(), "display", mock.Mock()
        frame = {"rows": 30, "cols": 100, "boxed": True, "ops": [[3, 46, "DISPLAY", 0]],
                 "lists": [[10, 30, ["RESOLUTION  1920X1080", "BACK"], 0]]}
        tv.on_screen_frame(frame)
        view = tv.classic_page.show_view.call_args.args[0]
        self.assertEqual(view.title, "DISPLAY")
        tv.show.assert_called_once_with("classic")

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

    def test_guide_while_a_classic_screen_is_open_only_puts_it_back_in_front(self):
        module, tv = self.tv()
        run = pathlib.Path(tempfile.mkdtemp(dir=os.environ["COUCHLITEOS_RUN_DIR"]))
        tv.child_pid, tv.child_name, tv.idle, tv.progress = 42, "remote-desktop", mock.Mock(), None
        with mock.patch.object(module, "GLib", create=True), \
                mock.patch.object(module.session.Session, "run_dir", run), \
                mock.patch.object(module.session, "focus_launcher") as focus:
            (run / "home.request").write_text(module.quick.GUIDE)
            tv.screens_tick()
            # Left for the screen's own loops (an app it started waits for Home in launch_and_wait).
            self.assertTrue((run / "home.request").exists())
            focus.assert_called_once_with(module.tvscreens.CHILD_TITLE)
            tv.screens_tick()
            focus.assert_called_once()  # once per press, not every second over an app the screen started
            os.utime(run / "home.request", ns=(1, 1))  # the next press
            tv.screens_tick()
            self.assertEqual(focus.call_count, 2)
            tv.on_child_exit(42, 0)
            self.assertFalse((run / "home.request").exists(), "a press the screen never took opens nothing later")

    def test_an_update_drops_guide_presses(self):
        module, tv = self.tv()
        run = pathlib.Path(tempfile.mkdtemp(dir=os.environ["COUCHLITEOS_RUN_DIR"]))
        tv.mode, tv.idle, tv.progress = "update", mock.Mock(), None
        with mock.patch.object(module.session.Session, "run_dir", run), \
                mock.patch.object(module.session, "focus_launcher") as focus:
            (run / "home.request").write_text(module.quick.GUIDE)
            tv.screens_tick()
        self.assertFalse((run / "home.request").exists())
        focus.assert_not_called()

    def test_a_screen_that_started_an_app_leaves_it_in_front(self):
        # Remote Desktop's CONNECT: the session took the screen and the controller as the screen closed.
        module, tv = self.tv()
        run = pathlib.Path(tempfile.mkdtemp(dir=os.environ["COUCHLITEOS_RUN_DIR"]))
        with mock.patch.object(module, "GLib", create=True), \
                mock.patch.object(module.session.Session, "run_dir", run), \
                mock.patch.object(module.session, "focus_launcher") as focus:
            (run / "app-active").write_text("office\n")
            (run / "office-ready").touch()
            tv.child_pid, tv.child_name = 9, "connect"
            tv.on_child_exit(9, 0)
            focus.assert_not_called()
            self.assertFalse((run / "launcher-focus").exists(), "gamepad-nav keeps sending the pad to the session")
            tv.after_screen.assert_called_once()
            # Nothing started (BACK, or the session ended at once): this screen comes back with the pad.
            for leftover in ("app-active", "office-ready"):
                (run / leftover).unlink()
            tv.child_pid, tv.child_name = 10, "connect"
            tv.on_child_exit(10, 0)
            focus.assert_called_once_with()
            self.assertTrue((run / "launcher-focus").exists())
            # The screen kept the controller (open_screen's launcher-focus): it is closed, this comes back.
            focus.reset_mock()
            (run / "app-active").write_text("firefox\n")
            tv.child_pid, tv.child_name = 11, "network"
            tv.on_child_exit(11, 0)
            focus.assert_called_once_with()

    def test_the_start_runs_whats_new_then_setup_then_the_auto_stream(self):
        module, tv = self.tv()
        tv.starting, tv.start_steps = True, ["whatsnew", "setup", "update", "tutorial", "autostream"]
        tv.open_screen.return_value = True
        tv.open_tutorial = mock.Mock()
        with mock.patch.object(module.whatsnew, "due", return_value=("0.3.0", "0.2.7")), \
                mock.patch.object(module.tvscreens, "setup_due", return_value=True), \
                mock.patch.object(module.tvscreens, "update_running", return_value=False), \
                mock.patch.object(module.tvhelp, "tour_due", return_value=False), \
                mock.patch.object(module.tvhelp, "mark_tour_seen") as seen:
            tv.continue_start()
            tv.open_whatsnew.assert_called_once_with("0.3.0", "0.2.7")
            seen.assert_called_once_with()  # an upgrade: What's New is the one notice, never the tour too
            tv.continue_start()
            tv.open_screen.assert_called_once_with("setup")
            tv.autostream.assert_not_called()
            tv.continue_start()
        tv.open_tutorial.assert_not_called()
        tv.autostream.assert_called_once_with()
        self.assertFalse(tv.starting)

    def test_a_new_install_gets_setup_then_the_tour_once_then_the_auto_stream(self):
        module, tv = self.tv()
        tv.starting, tv.start_steps = True, ["whatsnew", "setup", "update", "tutorial", "autostream"]
        tv.open_screen.return_value = True
        tv.open_tutorial = mock.Mock()
        with mock.patch.object(module.whatsnew, "due", return_value=None), \
                mock.patch.object(module.tvscreens, "setup_due", return_value=True), \
                mock.patch.object(module.tvscreens, "update_running", return_value=False), \
                mock.patch.object(module.tvhelp, "tour_due", return_value=True), \
                mock.patch.object(module.tvhelp, "mark_tour_seen") as seen:
            tv.continue_start()
            tv.open_screen.assert_called_once_with("setup")
            tv.open_tutorial.assert_not_called()
            tv.continue_start()  # setup closed
            seen.assert_called_once_with()  # before it is drawn
            tv.open_tutorial.assert_called_once_with()
            tv.autostream.assert_not_called()
            tv.continue_start()  # the tour closed
        tv.autostream.assert_called_once_with()
        self.assertFalse(tv.starting)

    def test_help_opens_from_settings_and_from_the_xmb(self):
        module, tv = self.tv()
        tv.open_help = mock.Mock()
        self.focus(tv, "HELP")
        tv.settings_key("activate")
        tv.open_help.assert_called_once_with("settings")
        tv.run_xmb_action(("entry", module.tvscreens.VIEW, "help"))
        tv.open_help.assert_called_with()

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


class HelpTest(unittest.TestCase):
    """The tour and HELP: which page shows, which way a card slides and where B goes back to,
    with the drawing left out."""

    def tv(self, starting=False):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.cross, tv.family, tv.motion_level = True, "xbox", module.motion.FULL
        tv.mode, tv.starting = "home", starting
        tv.stack, tv.tour_page, tv.help_page = mock.Mock(), mock.Mock(), mock.Mock()
        for name in ("show", "render_home", "render_settings", "close_screen", "continue_start"):
            setattr(tv, name, mock.Mock())
        tv.init_help()
        return module, tv

    def test_the_tour_goes_card_by_card_then_home_and_on_with_the_start(self):
        module, tv = self.tv(starting=True)
        with mock.patch.object(module, "Gtk", create=True):
            tv.open_tutorial()
            tv.show.assert_called_with("tutorial")
            view, direction, level = tv.tour_page.show_card.call_args.args
            self.assertEqual((view.title, direction, level), (tv.tour.cards[0].title, 0, module.motion.FULL))
            tv.tutorial_key("right")
            self.assertEqual(tv.tour_page.show_card.call_args.args[1], 1)  # in from the right
            tv.tutorial_key("left")
            self.assertEqual(tv.tour_page.show_card.call_args.args[1], -1)
            tv.continue_start.assert_not_called()
            tv.tutorial_key("back")  # SKIP on the first card
        tv.show.assert_called_with("home")
        tv.render_home.assert_called_once_with()
        tv.continue_start.assert_called_once_with()

    def test_the_tour_fades_in_unless_motion_is_off(self):
        module, tv = self.tv()
        with mock.patch.object(module, "Gtk", create=True) as gtk:
            tv.open_tutorial()
            tv.stack.set_visible_child_full.assert_called_once_with("tutorial", gtk.StackTransitionType.CROSSFADE)
            tv.stack.set_transition_duration.assert_called_once_with(module.motion.FADE_MS)
            tv.stack.reset_mock()
            tv.motion_level = module.motion.OFF
            tv.open_tutorial()
            tv.stack.set_visible_child_full.assert_called_once_with("tutorial", gtk.StackTransitionType.NONE)
            tv.stack.set_transition_duration.assert_called_once_with(0)

    def test_help_reads_a_topic_scrolls_it_and_goes_back_to_settings(self):
        module, tv = self.tv()
        tv.open_help("settings")
        tv.show.assert_called_with("help")
        tv.help_page.scroll_to_top.assert_called_once_with()
        tv.help_key("down")
        self.assertEqual(tv.help_model.focus, 1)
        tv.help_page.render.assert_called_with(tv.help_model, "xbox")
        tv.help_key("activate")
        self.assertTrue(tv.help_model.reading)
        tv.help_key("down")
        tv.help_page.scroll.assert_called_once_with(1, module.motion.FULL)
        tv.help_key("up")
        tv.help_page.scroll.assert_called_with(-1, module.motion.FULL)
        tv.help_key("back")
        self.assertFalse(tv.help_model.reading)
        tv.render_settings.assert_not_called()
        tv.help_key("back")
        tv.render_settings.assert_called_once_with()
        tv.show.assert_called_with("settings")
        tv.close_screen.assert_not_called()

    def test_help_from_the_xmb_goes_back_home(self):
        module, tv = self.tv()
        tv.open_help()
        tv.help_key("back")
        tv.close_screen.assert_called_once_with()
        tv.render_settings.assert_not_called()

    def test_the_tour_from_help_comes_back_to_help_and_never_moves_the_start_on(self):
        module, tv = self.tv(starting=True)
        tv.open_help()
        tv.help_model.focus = [topic.action for topic in tv.help_model.topics].index(module.tvhelp.TOUR)
        with mock.patch.object(module, "Gtk", create=True):
            tv.help_key("activate")
            tv.show.assert_called_with("tutorial")
            tv.tutorial_key("back")
        tv.show.assert_called_with("help")
        tv.continue_start.assert_not_called()
        tv.render_home.assert_not_called()

    def test_the_busy_screen_has_a_ring_not_a_text_spinner(self):
        module, tv = self.tv()
        tv.show_text = mock.Mock()
        tv.draw_launching("MOONLIGHT", "|")
        self.assertEqual(tv.show_text.call_args.args[:2], ("busy", "STARTING MOONLIGHT"))

    def test_open_loading_shows_the_starting_screen_until_a_key(self):
        module, tv = self.tv()
        tv.busy_depth, tv.busy_pressed = 0, False
        tv.draw_launching = mock.Mock()

        def pump(_seconds):
            self.assertEqual(tv.busy_depth, 1)  # keys go to the loop, as while an app starts
            tv.busy_pressed = tv.draw_launching.call_count >= 3

        tv.pump = mock.Mock(side_effect=pump)
        tv.script_loading()
        self.assertEqual(tv.draw_launching.call_count, 3)
        self.assertEqual((tv.busy_depth, tv.busy_pressed), (0, False))
        tv.show.assert_called_once_with("home")


class LeaveStartTest(unittest.TestCase):
    """Home while an app starts: back to the home screen, as the starting screen says."""

    def setUp(self):
        self.module = load_tv()
        self.run = pathlib.Path(tempfile.mkdtemp(dir=os.environ["COUCHLITEOS_RUN_DIR"]))
        patcher = mock.patch.object(self.module.session.Session, "run_dir", self.run)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tv = object.__new__(self.module.Tv)
        self.tv.status = ""
        self.tv.set_launcher_focus = mock.Mock()
        self.app = apps.Application(id="moonlight", name="MOONLIGHT", kind="request", status_id="moonlight",
                                    request="start-moonlight")

    def test_a_home_press_while_it_starts_leaves_it(self):
        tv = self.tv
        self.assertFalse(tv.launch_left(self.app))
        tv.busy_home = True
        self.assertTrue(tv.launch_left(self.app))
        self.assertFalse(tv.busy_home)
        self.assertTrue(tv.came_back)
        self.assertEqual(tv.key_home_at, tv.came_back_at)  # gamepad-nav's request for it opens nothing
        self.assertIs(tv.left_start[0], self.app)
        tv.set_launcher_focus.assert_called_once_with(True)
        self.assertFalse(tv.launch_left(self.app))
        (self.run / "home.request").write_text("home\n")  # the Guide button
        self.assertTrue(tv.launch_left(self.app))
        self.assertFalse((self.run / "home.request").exists())

    def test_leaving_the_start_puts_the_tv_back_awake(self):
        tv = self.tv
        tv._front, tv.front_plan, tv.show = mock.Mock(), mock.Mock(), mock.Mock()
        tv.busy_depth, tv.came_back = 1, True
        tv.launch_wait_end()
        tv.show.assert_called_once_with("home")
        tv.front_plan.assert_called_once_with(tv.front.returning.return_value)
        self.assertFalse(tv.came_back)

    def test_the_app_left_starting_waits_behind_the_home_screen(self):
        tv = self.tv
        tv.left_start = (self.app, self.module.time.monotonic() + 18)
        with mock.patch.object(self.module.session, "focus_launcher") as focus:
            tv.left_start_tick()
            focus.assert_not_called()
            (self.run / "launcher-focus").touch()
            (self.run / "moonlight-ready").touch()
            tv.left_start_tick()
            focus.assert_called_once_with()
        self.assertEqual(tv.status, "MOONLIGHT IS READY: FIND IT IN ACTIVE APPLICATIONS")
        self.assertIsNone(tv.left_start)

    def test_an_app_left_starting_that_failed_says_so_at_the_end(self):
        tv = self.tv
        (self.run / "moonlight-status").write_text("failed: no such file\n")
        tv.left_start = (self.app, self.module.time.monotonic() - 1)
        tv.left_start_tick()
        self.assertEqual(tv.status, "MOONLIGHT FAILED TO START")
        self.assertIsNone(tv.left_start)


class StartOrderTest(unittest.TestCase):
    """The start: what runs after what, with the drawing and the threads left out."""

    def tv(self):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.status, tv.mode, tv.cross, tv.starting = "", "home", False, True
        for name in ("show", "render_home", "render_settings", "refresh_settings", "read_can_sleep", "ui_sound",
                     "music_holds", "hide_loading", "continue_start", "render_bar", "start_script"):
            setattr(tv, name, mock.Mock())
        tv.music = mock.Mock()
        return module, tv

    def test_an_update_finished_during_the_start_leaves_the_tour_on_top(self):
        module, tv = self.tv()
        tv.idle, tv.progress, tv.progress_return = mock.Mock(), mock.Mock(), "home"
        tv.progress.poll.return_value = mock.Mock(finished="UPDATED")
        order = []
        tv.show.side_effect = order.append
        tv.continue_start.side_effect = lambda: tv.show("tutorial")
        tv.update_tick()
        self.assertEqual(order, ["home", "tutorial"])
        self.assertEqual(tv.status, "UPDATED")

    def test_start_steps_that_fail_still_bring_home_the_fade_and_the_music(self):
        module, tv = self.tv()
        tv.continue_start.side_effect = RuntimeError("broken step")
        with mock.patch.object(module.display, "log") as log:
            tv.after_first_frame()
        self.assertIn("broken step", log.call_args.args[0])
        self.assertFalse(tv.starting)
        tv.show.assert_called_once_with("home")
        tv.music.start.assert_called_once_with()
        tv.hide_loading.assert_called_once_with()

    def test_the_auto_stream_waits_for_the_boot_picture_and_the_display_mode(self):
        module, tv = self.tv()
        tv.booting, tv.autostream = True, mock.Mock()
        tv.when_booted(tv.autostream)
        tv.autostream.assert_not_called()
        tv.start_script.side_effect = lambda: tv.autostream.assert_called_once_with()  # before the test steps
        tv.mode_restored(True)
        tv.start_script.assert_called_once_with()
        self.assertFalse(tv.booting)
        tv.when_booted(tv.render_home)  # once booted: at once
        tv.render_home.assert_called_once_with()

    def test_a_skipped_display_mode_is_said_once_it_is_known(self):
        module, tv = self.tv()
        tv.mode_restored(None)
        self.assertIn("SAVED DISPLAY MODE SKIPPED", tv.status)
        tv.render_bar.assert_called_once_with()

    def test_the_loading_screen_gets_the_started_tiles_picture(self):
        module, tv = self.tv()
        tv.cross, tv.motion_level = True, module.motion.FULL
        tv._front, tv.loading_view, tv.show_text = mock.Mock(starting=True), mock.Mock(shown=False), mock.Mock()
        tv.xmb_picture = mock.Mock(return_value="cover")
        tv.draw_launching("MOONLIGHT", "|")
        self.assertIsNone(tv.loading_view.start.call_args.args[2])  # nothing focused: no picture
        tv.launch_item = item = mock.Mock(icon="moonlight")
        tv.draw_launching("MOONLIGHT", "|")
        tv.xmb_picture.assert_called_once_with(item)
        self.assertEqual(tv.loading_view.start.call_args.args[2], "cover")


class BatteryTest(unittest.TestCase):
    """The PC's own battery: a toast once per warning level, and the battery's screen-off times on unplug."""

    def tick(self, warning=None, source_change=False):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.toast_feed, tv.toasts, tv.idle, tv.quick_open = mock.Mock(), mock.Mock(), mock.Mock(), False
        tv.can_sleep, tv.can_wake = True, True
        tv.battery = mock.Mock(take_warning=mock.Mock(return_value=warning),
                               take_source_change=mock.Mock(return_value=source_change),
                               on_battery=mock.Mock(return_value=True))
        with mock.patch.object(module.controls, "detect_family", return_value="xbox"), \
                mock.patch.object(module.power, "load_settings", return_value=module.power.Settings()):
            tv.tick_quick()
        return module, tv

    def test_a_crossed_warning_level_is_a_toast(self):
        module, tv = self.tick(warning=10)
        tv.toasts.push.assert_called_once_with(module.battery.warning_text(10))

    def test_no_warning_no_toast_and_no_power_change(self):
        _module, tv = self.tick()
        tv.toasts.push.assert_not_called()
        tv.idle.apply.assert_not_called()

    def test_unplugging_applies_the_battery_settings(self):
        _module, tv = self.tick(source_change=True)
        tv.idle.apply.assert_called_once()


class SwitchInterfaceTest(unittest.TestCase):
    """APPEARANCE > INTERFACE > CLASSIC, chosen in the screen on top: the TV interface ends, and
    the restarted service starts the classic launcher (scripts/couchliteos-session)."""

    def closed(self, marker):
        module = load_tv()
        tv = object.__new__(module.Tv)
        run = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, run, True)
        if marker:
            (run / module.tvscreens.SWITCH_INTERFACE).touch()
        tv.child_name, tv.child_pid, tv.starting = "appearance", 1, False
        tv.application = mock.Mock()
        tv.child_left_an_app = mock.Mock(return_value=True)
        tv.after_screen = mock.Mock()
        with mock.patch.object(module.GLib, "spawn_close_pid", create=True), mock.patch.object(module.session, "RUN", run):
            tv.on_child_exit(1, 0)
        return tv, run

    def test_the_tv_interface_ends_after_a_switch(self):
        tv, run = self.closed(marker=True)
        tv.application.quit.assert_called_once_with()
        self.assertFalse(any(run.iterdir()))

    def test_otherwise_it_stays(self):
        tv, _run = self.closed(marker=False)
        tv.application.quit.assert_not_called()


class LaunchFailureTest(unittest.TestCase):
    """A start that failed: the classic launcher's failure screen and its buttons, without GTK."""

    MOONLIGHT = apps.Application(id="moonlight", name="MOONLIGHT", kind="request", request="start-moonlight",
                                 status_id="moonlight")

    def tv(self, *choices):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.choose = mock.Mock(side_effect=list(choices))
        tv.wake_from_failure = mock.Mock()
        tv.wait_for_screen = mock.Mock()
        patches = contextlib.ExitStack()
        patches.enter_context(mock.patch.object(module.stream, "link_up", return_value=True))
        patches.enter_context(mock.patch.object(module.controllers, "bluetooth_present", return_value=True))
        self.addCleanup(patches.close)
        return module, tv

    def labels(self, tv):
        return tv.choose.call_args.args[2]

    def test_moonlight_offers_wake_pc_and_comes_back_after_it(self):
        module, tv = self.tv(0, 1)
        self.assertEqual(tv.show_launch_failure("MOONLIGHT", "boom", self.MOONLIGHT), "retry")
        self.assertEqual(self.labels(tv), ["WAKE PC", "TRY AGAIN", "SAVE SUPPORT FILE", "BACK"])
        tv.wake_from_failure.assert_called_once_with()
        title, body, _labels = tv.choose.call_args.args
        self.assertEqual(title, "MOONLIGHT FAILED TO START")
        self.assertIn("BOOM", body)
        self.assertIn("SUPPORT FILE", body)  # what to do next, as on the classic screen

    def test_network_and_support_buttons_open_the_classic_screens(self):
        module, tv = self.tv(1, 0, None)
        self.assertEqual(tv.show_launch_failure("MOONLIGHT", "could not reach the host", self.MOONLIGHT), "dismiss")
        self.assertEqual(self.labels(tv), ["WAKE PC", "NETWORK SETTINGS", "TRY AGAIN", "BACK"])
        tv.wait_for_screen.assert_called_once_with("network")
        tv = self.tv(1, None)[1]
        tool = apps.Application(id="tool", name="TOOL", kind="command", command="/bin/true", status_id="tool")
        self.assertEqual(tv.show_launch_failure("TOOL", "crashed", tool), "dismiss")
        self.assertEqual(self.labels(tv), ["TRY AGAIN", "SAVE SUPPORT FILE", "BACK"])
        tv.wait_for_screen.assert_called_once_with("support-file")

    def test_without_retry_there_is_no_try_again(self):
        _module, tv = self.tv(None)
        self.assertEqual(tv.show_launch_failure("TOOL", "still running", retry=False), "dismiss")
        self.assertNotIn("TRY AGAIN", self.labels(tv))

    def test_left_and_right_pick_a_button_and_a_picks_it(self):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.choices, tv.choice, tv.answer, tv.busy_depth = ["WAKE PC", "TRY AGAIN", "BACK"], 0, None, 1
        hint = mock.Mock()
        tv.pages = {"message": (None, None, None, hint)}
        tv.message_key("right")
        tv.message_key("right")
        tv.message_key("right")
        self.assertEqual(tv.choice, 2)
        self.assertIn("[ BACK ]", hint.set_label.call_args.args[0])
        tv.message_key("left")
        tv.message_key("activate")
        self.assertEqual((tv.choice, tv.answer), (1, "yes"))


class ReadyMarkTest(unittest.TestCase):
    def test_launcher_ready_waits_for_the_first_painted_frame(self):
        # A tick callback runs before the paint: a GL crash in the first paint must leave no mark.
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.ready_written = False
        tv.after_first_frame = mock.Mock()
        run = pathlib.Path(tempfile.mkdtemp(dir=os.environ["COUCHLITEOS_RUN_DIR"]))
        window, clock, glib = mock.Mock(), mock.Mock(), mock.Mock()
        window.get_frame_clock.return_value = clock
        with mock.patch.object(module, "GLib", glib, create=True), \
                mock.patch.object(module.session.Session, "run_dir", run):
            tv.watch_first_frame(window)
            tick = window.add_tick_callback.call_args.args[0]
            self.assertFalse(tick(window, clock))
            self.assertFalse((run / "launcher-ready").exists(), "not in the UPDATE phase")
            signal, painted = clock.connect.call_args.args
            self.assertEqual(signal, "after-paint")
            painted(clock)
            clock.disconnect.assert_called_once()
            glib.idle_add.assert_called_once_with(tv.first_frame_painted)
            self.assertFalse((run / "launcher-ready").exists())
            tv.first_frame_painted()
            self.assertTrue((run / "launcher-ready").exists())
            tv.after_first_frame.assert_called_once_with()
            tv.first_frame_painted()
            tv.after_first_frame.assert_called_once_with()


class ActiveApplicationsTest(unittest.TestCase):
    """ACTIVE APPLICATIONS: resume, close, type into an app and its CONTROLLER MOUSE, without GTK."""

    CHROME = apps.Application(id="google-chrome", name="GOOGLE CHROME", kind="request", request="start-chrome",
                              status_id="chrome")
    STEAM = apps.Application(id="steam", name="STEAM", kind="command", command="/usr/games/steam", status_id="steam")

    def tv(self, running):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.status, tv.active_index = "", 0
        tv.running_applications = mock.Mock(return_value=list(running))
        modes = module.session.pointer.Modes(pathlib.Path(tempfile.mkdtemp(dir=os.environ["COUCHLITEOS_RUN_DIR"])) / "pointer")
        type(tv).pointer_modes = property(lambda _self: modes)
        self.addCleanup(delattr, type(tv), "pointer_modes")
        for name in ("render_active", "render_home", "show", "open_quick", "focus_app", "close_app"):
            setattr(tv, name, mock.Mock())
        return module, tv, modes

    def test_the_rows_add_the_controller_mouse_of_the_app_in_front(self):
        _module, tv, modes = self.tv([self.STEAM, self.CHROME])
        modes.front = "google-chrome"
        _running, front, rows = tv.active_rows()
        self.assertEqual(front, self.CHROME)
        self.assertEqual(rows, ["STEAM  RUNNING", "GOOGLE CHROME  RUNNING", "CONTROLLER MOUSE (GOOGLE CHROME)  ON",
                                "QUICK MENU", "BACK TO HOME"])
        self.assertEqual(self.tv([])[1].active_rows()[2], ["QUICK MENU", "BACK TO HOME"])

    def test_a_switches_the_controller_mouse(self):
        _module, tv, modes = self.tv([self.STEAM])
        tv.active_index = 1
        tv.active_key("activate")
        self.assertTrue(modes.enabled(self.STEAM))
        self.assertEqual(tv.status, "CONTROLLER MOUSE ON FOR STEAM")
        tv.active_key("activate")
        self.assertFalse(modes.enabled(self.STEAM))
        tv.open_quick.assert_not_called()
        tv.active_index = 2
        tv.active_key("activate")
        tv.open_quick.assert_called_once_with()

    def test_x_types_into_the_focused_app_or_the_one_in_front(self):
        module, tv, modes = self.tv([self.STEAM, self.CHROME])
        run = pathlib.Path(tempfile.mkdtemp(dir=os.environ["COUCHLITEOS_RUN_DIR"]))
        with mock.patch.object(module.session.Session, "run_dir", run):
            tv.focus_app.return_value = False
            tv.active_index = 1
            tv.active_key("keyboard")
            tv.focus_app.assert_called_once_with(self.CHROME)
            self.assertIn("COULD NOT FOCUS GOOGLE CHROME", tv.status)
            self.assertFalse((run / "start-osk").exists())
            tv.focus_app.return_value = True
            tv.active_index = 3  # QUICK MENU: the app in front (STEAM, the first) gets the text
            tv.active_key("keyboard")
            tv.focus_app.assert_called_with(self.STEAM)
            self.assertTrue((run / "start-osk").exists())
            self.assertEqual(tv.status, "TYPING INTO STEAM")
            tv.show.assert_called_with("home")
        _module, tv, _modes = self.tv([])
        tv.active_key("keyboard")
        self.assertEqual(tv.status, "NO APP IS RUNNING TO TYPE INTO")


class DiskNoticeTest(unittest.TestCase):
    def test_a_live_stick_names_the_installed_system_it_can_update_on_the_home_screen(self):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.status = ""
        tv.updates = mock.Mock(installed=False, current="0.3.0")
        notice = "INSTALLED SYSTEM 0.2.7 FOUND: UPDATE IT IN SETTINGS > SOFTWARE UPDATE"
        with mock.patch.object(module.update, "disk_notice", return_value=notice) as disk:
            self.assertEqual(tv.home_line(), notice)
            disk.assert_called_once_with("0.3.0")
            tv.status = "MOONLIGHT STARTED"
            self.assertEqual(tv.home_line(), "MOONLIGHT STARTED", "the last result first, the notice after it")
            tv.status, tv.updates.installed = "", True
            self.assertEqual(tv.home_line(), "")
            disk.assert_called_once()


class LookTest(unittest.TestCase):
    """The theme on screen and the test script, with GTK left out."""

    def test_a_saved_theme_reloads_the_stylesheets_once(self):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.layout = module.tvlayout.Layout(1920, 1080)
        tv.load_css = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            config = pathlib.Path(directory) / "config.ini"
            with mock.patch.object(module.theme, "CONFIG", config):
                tv.theme_stamp = tv.theme_stamp_now()
                tv.theme_tick()
                tv.load_css.assert_not_called()
                module.theme.save_choice("slate", "", config)
                tv.theme_tick()
                tv.load_css.assert_called_once()

    def test_the_next_colour_of_the_month_reloads_the_stylesheets(self):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.layout = module.tvlayout.Layout(1920, 1080)
        tv.load_css = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(module.theme, "CONFIG", pathlib.Path(directory) / "config.ini"):
                with mock.patch.object(module.month, "colour", return_value="a3a9b4"):
                    tv.theme_stamp = tv.theme_stamp_now()
                    tv.theme_tick()
                    tv.load_css.assert_not_called()
                with mock.patch.object(module.month, "colour", return_value="c9b037"):  # the 12th
                    tv.theme_tick()
                    tv.load_css.assert_called_once()

    @staticmethod
    def xmb_tv(module):
        """A TV on the XMB home, with the wave widget, the stylesheets and the XMB view left out."""
        tv = object.__new__(module.Tv)
        tv.cross = True
        tv.layout = module.tvlayout.Layout(1920, 1080)
        tv.css, tv.quick_css, tv.xmb_view, tv.wave = mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock()
        tv.motion_level = module.motion.FULL
        tv.wave_failed = False
        return tv

    def test_a_saved_background_is_applied_at_once(self):
        module = load_tv()
        tv = self.xmb_tv(module)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(module.theme, "CONFIG", pathlib.Path(directory) / "config.ini"), \
                    mock.patch.dict(os.environ, {"GSK_RENDERER": "ngl"}):
                tv.load_css()
                tv.wave.set_how.assert_called_with(module.wave.GL)
                self.assertGreater(tv.wave.set_colours.call_args.args[0].sparkle, 0)
                # In this order each save changes config.ini's size: two saves in a few
                # milliseconds can share a modification time.
                for style, how, ribbons, sparkles in (("calm", module.wave.GL, True, False),
                                                      ("plain", module.wave.STATIC, False, False),
                                                      ("wave", module.wave.GL, True, True)):
                    module.background.save(style)
                    tv.theme_tick()
                    colours = tv.wave.set_colours.call_args.args[0]
                    with self.subTest(style):
                        tv.wave.set_how.assert_called_with(how)
                        self.assertEqual((colours.alpha > 0, colours.sparkle > 0), (ribbons, sparkles))
                module.background.save("plain")
                module.theme.save_choice("high-contrast", "")
                tv.theme_tick()
                tv.wave.set_how.assert_called_with(module.wave.FLAT)

    def test_a_still_wave_stays_still_whatever_the_background(self):
        module = load_tv()
        tv = self.xmb_tv(module)
        tv.motion_level = module.motion.OFF
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(module.theme, "CONFIG", pathlib.Path(directory) / "config.ini"):
                for style in ("wave", "calm", "plain"):
                    module.background.save(style)
                    tv.load_css()
                    tv.wave.set_how.assert_called_with(module.wave.STATIC)

    def test_the_script_saves_a_background_and_an_accent(self):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.script = ["background:plain", "accent:month", "background:fireworks"]
        with tempfile.TemporaryDirectory() as directory:
            config = pathlib.Path(directory) / "config.ini"
            with mock.patch.object(module.theme, "CONFIG", config), \
                    mock.patch.object(module, "GLib", mock.Mock(), create=True), \
                    mock.patch.object(module.sys, "stderr"):
                module.theme.save_choice("ocean", "")
                tv.script_step()
                tv.script_step()
                self.assertFalse(tv.script_failed)
                self.assertEqual(module.background.load(), "plain")
                self.assertEqual(module.theme.load_choice(), ("ocean", "month"))
                tv.script_step()
                self.assertTrue(tv.script_failed)
                self.assertEqual(module.background.load(), "plain")

    def test_the_layout_dump_says_what_is_behind_the_xmb(self):
        module = load_tv()
        tv = self.xmb_tv(module)
        tv.background_style = "calm"
        tv.wave.how = module.wave.GL
        tv.wave.gl.get_visible.return_value = True
        tv.wave.palette = module.background.wave_palette(module.wave.palette(module.theme.FALLBACK, 14), "calm")
        facts = tv.wave_facts()
        self.assertEqual((facts["background"], facts["how"], facts["gl"], facts["sparkle"]),
                         ("calm", module.wave.GL, True, 0.0))
        self.assertGreater(facts["alpha"], 0)

    def test_the_script_times_the_next_step_first_and_reports_a_bad_one(self):
        module = load_tv()
        tv = object.__new__(module.Tv)
        tv.script = ["NoSuchKey", "wait:1500", "quit"]
        tv.on_key = mock.Mock()
        tv.application = mock.Mock()
        glib = mock.Mock()
        gdk = mock.Mock(KEY_VoidSymbol=0xFFFFFF)
        gdk.keyval_from_name.return_value = 0xFFFFFF
        with mock.patch.object(module, "GLib", glib, create=True), \
                mock.patch.object(module, "Gdk", gdk, create=True), mock.patch.object(module.sys, "stderr"):
            self.assertFalse(tv.script_step())
            self.assertTrue(tv.script_failed)
            tv.on_key.assert_not_called()
            glib.timeout_add.assert_called_with(module.SCRIPT_STEP_MS, tv.script_step)
            tv.script_step()  # wait:1500 times the step after it
            glib.timeout_add.assert_called_with(1500, tv.script_step)
            glib.timeout_add.reset_mock()
            tv.script_step()
            glib.timeout_add.assert_not_called()  # the last step
            tv.application.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
