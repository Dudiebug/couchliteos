#!/usr/bin/python3
"""The TV interface: the GTK 4 home screen CouchLiteOS shows inside Cage.

One full-screen window holding a stack of screens: HOME (top bar, GAMES, APPS and SYSTEM
rows, prompt bar), STARTING / WAITING, a message screen (a failure, a question) and ACTIVE
APPLICATIONS (Guide / Home with apps running); SETTINGS (two panes), POWER, WHAT'S NEW and
the SOFTWARE UPDATE progress come from couchliteos_tvscreens. A Settings screen the TV
interface does not draw itself, the setup wizard and a Remote Desktop start run as the
classic curses screen in a foot window on top (`couchliteos-launcher --screen <name>`);
the rows are read again when it closes.

The rows come from couchliteos_home.HomeModel; starting, resuming and closing
apps is couchliteos_session's, the same code the classic launcher runs. Sizes, colours,
keys and blanking are couchliteos_tvlayout's.

Keys are the ones gamepad-nav sends (arrows, Enter, Esc, Delete, F5-F8, F12), so a
controller, a TV remote and a keyboard all work. Guide / Home arrives as home.request.

Exit status INIT_FAILED (3) means GTK could not start (no gi, no display, no renderer):
couchliteos-session then tries once more with GSK_RENDERER=cairo, then starts the classic
launcher. `launcher-ready` is written after the first frame is drawn.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import couchliteos_apps as apps
import couchliteos_controllers as controllers
import couchliteos_display as display
import couchliteos_home as home
import couchliteos_power as power
import couchliteos_session as session
import couchliteos_stream as stream
import couchliteos_theme as theme
import couchliteos_tvlayout as tvlayout
import couchliteos_tvscreens as tvscreens
import couchliteos_update as update
import couchliteos_whatsnew as whatsnew

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk, Pango
except (ImportError, ValueError) as _error:  # no PyGObject or no GTK 4 typelib
    Gtk = None
    GI_ERROR = str(_error)
else:
    GI_ERROR = ""

INIT_FAILED = 3
TITLE = "CouchLiteOS Launcher"  # the classic launcher's title too: focus_launcher() finds either
APPLICATION_ID = "org.couchliteos.Launcher"
TICK_SECONDS = 1
RELOAD_SECONDS = 5  # how often the rows are read again (pairing, apps added in Settings)
AUTOSTREAM_SECONDS = 5
REPO_THEMES = pathlib.Path(__file__).resolve().parents[1] / "overlay/usr/share/couchliteos/themes"
BACK_HINT = "B / CIRCLE OR ESC GOES BACK"


def visible_applications() -> apps.LoadResult:
    result = session.application_result()
    return apps.LoadResult(tuple(app for app in result.applications if app.enabled and app.visible), result.errors)


def current_theme() -> theme.Theme:
    builtin = theme.BUILTIN_DIR if theme.BUILTIN_DIR.is_dir() else REPO_THEMES
    return theme.current(builtin=builtin)


def label(text: str, *classes: str, xalign: float = 0.0, ellipsize: bool = True, wrap: bool = False) -> "Gtk.Label":
    widget = Gtk.Label(label=text, xalign=xalign)
    for name in classes:
        widget.add_css_class(name)
    if wrap:
        widget.set_wrap(True)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    elif ellipsize:
        widget.set_ellipsize(Pango.EllipsizeMode.END)
        widget.set_max_width_chars(1)  # take the width given (a tile's), not the text's
        widget.set_hexpand(True)
    return widget


def clear(box: "Gtk.Box") -> None:
    child = box.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


class Screens:
    """SETTINGS, POWER, WHAT'S NEW, the SOFTWARE UPDATE progress, the classic screens opened on
    top, and the steps of the start. Part of Tv: the models are couchliteos_tvscreens'."""

    def init_screens(self) -> None:
        self.child_pid: int | None = None  # the classic screen running on top (open_screen)
        self.child_name = ""
        self.starting = True  # the steps before the home screen still run (continue_start)
        self.start_steps = ["whatsnew", "setup", "update", "autostream"]
        self.settings = tvscreens.SettingsModel(tvscreens.value_sources(
            updates=self.updates, controllers=self.controllers, running=self.running_applications,
            applications=lambda: len(visible_applications().applications),
            can_sleep=lambda: self.can_sleep, network=home.link_status,
        ))
        self.power_menu = tvscreens.PowerModel(self.can_sleep, self.can_wake)
        self.progress: tvscreens.UpdateProgress | None = None
        self.progress_return = "home"
        self.screen_pages: list = []

    def screen_keys(self) -> dict:
        return {"settings": self.settings_key, "power": self.power_key, "whatsnew": self.whatsnew_key,
                "update": self.update_key}

    # ------------------------------------------------------------------ building

    def build_screens(self) -> None:
        # SETTINGS: title, the categories on the left and the focused one's value on the right.
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.append(label("SETTINGS", "tv-title"))
        panes = self.settings_panes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        panes.set_vexpand(True)
        self.settings_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.settings_list.add_css_class("tv-list")
        self.settings_list.set_hexpand(False)  # its labels would take the width: the right pane gets it
        panes.append(self.settings_list)
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pane.add_css_class("tv-pane")
        pane.set_hexpand(True)
        pane.set_valign(Gtk.Align.START)
        self.settings_title = label("", "tv-title", wrap=True)
        self.settings_value = label("", "tv-value", wrap=True)
        self.settings_help = label("", "tv-help", wrap=True)
        for widget in (self.settings_title, self.settings_value, self.settings_help):
            pane.append(widget)
        self.settings_pane = pane
        panes.append(pane)
        page.append(panes)
        self.settings_status = label("", "tv-status", xalign=0.5, wrap=True)
        page.append(self.settings_status)
        page.append(label(tvscreens.SETTINGS_HINT, "tv-prompt", xalign=0.5, wrap=True))
        self.stack.add_named(page, "settings")

        # POWER: a short list in the middle, as ACTIVE APPLICATIONS.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_valign(Gtk.Align.CENTER)
        box.append(label("POWER", "tv-title", xalign=0.5))
        self.power_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.power_list.set_halign(Gtk.Align.CENTER)
        box.append(self.power_list)
        self.power_hint = label(tvscreens.POWER_HINT, "tv-prompt", xalign=0.5, wrap=True)
        box.append(self.power_hint)
        power_page = box
        self.stack.add_named(box, "power")

        # WHAT'S NEW: the title, one line per feature, how to go on.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_valign(Gtk.Align.CENTER)
        self.whatsnew_title = label("", "tv-title", xalign=0.5, wrap=True)
        box.append(self.whatsnew_title)
        self.whatsnew_lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.whatsnew_lines.set_halign(Gtk.Align.CENTER)
        box.append(self.whatsnew_lines)
        box.append(label(tvscreens.WHATS_NEW_HINT, "tv-prompt", xalign=0.5, wrap=True))
        whatsnew_page = box
        self.stack.add_named(box, "whatsnew")

        # SOFTWARE UPDATE progress: what the service does now, a bar, what B does.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_valign(Gtk.Align.CENTER)
        self.update_title = label("", "tv-title", xalign=0.5, wrap=True)
        self.update_text = label("", "tv-body", xalign=0.5, wrap=True)
        self.update_bar = Gtk.ProgressBar()
        self.update_bar.set_halign(Gtk.Align.CENTER)
        self.update_percent = label("", "tv-body", xalign=0.5, ellipsize=False)
        self.update_hint = label("", "tv-prompt", xalign=0.5, wrap=True)
        for widget in (self.update_title, self.update_text, self.update_bar, self.update_percent, self.update_hint):
            box.append(widget)
        update_page = box
        self.stack.add_named(box, "update")
        self.screen_pages = [page, power_page, whatsnew_page, update_page]

    def relayout_screens(self) -> None:
        layout = self.layout
        self.settings_panes.set_spacing(layout.px(40))
        self.settings_list.set_size_request(round((layout.width - 2 * layout.margin_x) * 0.36), -1)
        self.settings_pane.set_spacing(layout.px(20))
        self.update_bar.set_size_request(round(layout.width * 0.5), layout.px(24))
        for box in (self.power_list, self.whatsnew_lines):
            box.set_spacing(layout.px(8))
        self.power_list.set_size_request(round(layout.width * 0.4), -1)
        if self.mode == "settings":
            self.render_settings()

    # ------------------------------------------------------------------ SETTINGS

    def open_settings(self) -> None:
        self.settings.focus = 0
        self.settings.refresh()
        self.status = ""
        self.render_settings()
        self.show("settings")

    def render_settings(self) -> None:
        if not hasattr(self, "layout"):
            return
        clear(self.settings_list)
        rows = self.settings.rows()
        for index in self.settings.visible(tvscreens.list_slots(self.layout)):
            item = label(rows[index], "tv-item")
            if index == self.settings.focus:
                item.add_css_class("tv-focused")
            self.settings_list.append(item)
        title, value, text = self.settings.detail()
        self.settings_title.set_label(title)
        self.settings_value.set_label(value)
        self.settings_value.set_visible(bool(value))
        self.settings_help.set_label(text)
        self.settings_status.set_label(self.status)
        self.settings_status.set_visible(bool(self.status))

    def settings_key(self, name: str) -> None:
        if name in ("up", "down"):
            self.status = ""
            self.settings.move(1 if name == "down" else -1)
        elif name in ("back", "home"):
            self.close_screen()
            return
        elif name == "activate":
            self.status = ""
            kind, target = self.settings.activate()
            if kind == tvscreens.SCREEN:
                self.open_screen(target)
            elif kind == tvscreens.APP:
                self.launch_by_id(target)
                self.show("settings")
            elif target == "active":
                self.open_active()
                return
            elif target == "updates":
                self.status = tvscreens.toggle_updates(self.updates)
                self.settings.refresh()
            else:
                self.close_screen()
                return
        self.render_settings()

    def close_screen(self) -> None:
        """Back to the home screen, its rows read again."""
        self.model.reload()
        self.show("home")
        self.render_home()

    # ------------------------------------------------------------------ POWER

    def open_power(self) -> None:
        self.can_sleep = power.can_suspend()  # checked again: the stick may have moved to another PC
        running = [app.name for app in self.running_applications()]
        self.power_menu = tvscreens.PowerModel(self.can_sleep, self.can_wake, running)
        self.status = ""
        self.render_power()
        self.show("power")

    def render_power(self) -> None:
        clear(self.power_list)
        for index, text in enumerate(self.power_menu.rows()):
            item = label(text, "tv-item", xalign=0.5, ellipsize=False)
            if index == self.power_menu.focus:
                item.add_css_class("tv-focused")
            self.power_list.append(item)
        self.power_hint.set_label(self.status or tvscreens.POWER_HINT)

    def power_key(self, name: str) -> None:
        if name in ("up", "down"):
            self.status = ""
            self.power_menu.move(1 if name == "down" else -1)
        elif name in ("back", "home"):
            self.close_screen()
            return
        elif name == "activate":
            choice = self.power_menu.focused()
            if not choice.request:
                self.close_screen()
                return
            if choice.request == "suspend" and not self.power_menu.can_sleep:
                self.status = "SLEEP IS NOT SUPPORTED ON THIS PC"
            elif self.ask(choice.label, choice.question, tvlayout.QUESTION_HINT) == "yes":
                try:
                    self.request(choice.request)  # couchliteos-<request>.path carries it out as root
                    self.status = tvscreens.PowerModel.done(choice.request)
                except OSError as error:
                    self.status = f"COULD NOT ASK FOR {choice.label}: {error.strerror or 'ERROR'}".upper()
                self.close_screen()
                return
            self.show("power")
        self.render_power()

    # ------------------------------------------------------------------ WHAT'S NEW

    def open_whatsnew(self, version: str, seen: str) -> None:
        title, lines = tvscreens.whats_new(version, seen)
        self.whatsnew_title.set_label(title)
        clear(self.whatsnew_lines)
        for line in lines:
            self.whatsnew_lines.append(label(line, "tv-line", wrap=True))
        self.show("whatsnew")

    def whatsnew_key(self, name: str) -> None:
        if name in ("activate", "back", "home"):
            self.show("home")
            self.continue_start()

    # ------------------------------------------------------------------ SOFTWARE UPDATE progress

    def watch_update(self, version: str) -> None:
        self.progress = tvscreens.UpdateProgress(version, self.run_dir)
        self.progress_return = self.mode if self.mode in ("settings", "home") else "home"
        self.render_update(self.progress.poll())
        self.show("update")

    def render_update(self, view: tvscreens.Progress) -> None:
        self.update_title.set_label(view.title)
        self.update_text.set_label(view.text)
        self.update_bar.set_visible(view.bar)
        self.update_percent.set_visible(view.bar and view.percent is not None)
        if view.bar and view.percent is None:
            self.update_bar.pulse()
        elif view.bar:
            self.update_bar.set_fraction(view.percent / 100)
            self.update_percent.set_label(f"{view.percent}%")
        self.update_hint.set_label(view.hint)
        self.update_hint.set_visible(bool(view.hint))

    def update_key(self, name: str) -> None:
        if self.progress is None:
            return
        if name in ("back", "home"):
            self.progress.back()
        elif name == "activate":
            self.progress.dismiss()
        self.update_tick()

    def update_tick(self) -> None:
        self.idle.keep_awake()  # a long download must not blank or sleep the box
        view = self.progress.poll()
        if view.finished:
            self.progress = None
            if self.starting:
                self.continue_start()
            self.status = view.finished
            if self.progress_return == "settings":
                self.settings.refresh()
                self.render_settings()
                self.show("settings")
            else:
                self.show("home")
                self.render_home()
            return
        self.render_update(view)

    # ------------------------------------------------------------------ classic screens on top

    def open_screen(self, name: str, app: str = "") -> bool:
        """Run a classic screen (`couchliteos-launcher --screen`) in a foot window on top of this one."""
        if self.child_pid is not None:
            return False
        try:
            pid, *_streams = GLib.spawn_async(
                tvscreens.child_command(name, app),
                flags=GLib.SpawnFlags.DO_NOT_REAP_CHILD | GLib.SpawnFlags.SEARCH_PATH,
            )
        except GLib.Error as error:
            self.status = tvscreens.CHILD_FAILED.format(name.upper().replace("-", " "), error.message.upper())
            display.log(f"tv: cannot open --screen {name}: {error.message}", session.LOG)
            self.render_current()
            return False
        self.child_pid, self.child_name = pid, name
        self.set_launcher_focus(True)  # gamepad-nav sends the controller's keys to the screen on top
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid, self.on_child_exit)
        return True

    def on_child_exit(self, pid: int, wait_status: int) -> None:
        GLib.spawn_close_pid(pid)
        name, self.child_pid, self.child_name = self.child_name, None, ""
        try:
            code = os.waitstatus_to_exitcode(wait_status)
        except ValueError:
            code = 1
        if code:
            display.log(f"tv: --screen {name} exited with {code}", session.LOG)
            self.status = f"{name.upper().replace('-', ' ')} CLOSED WITH AN ERROR ({code})"
        session.focus_launcher()
        self.after_screen()
        if (self.run_dir / tvscreens.REOPEN_SETUP).exists():
            self.open_screen("setup")  # it restarted for a new picture size: carry on at the next step
        elif tvscreens.take_reopen_display(self.run_dir):
            self.open_screen("display")  # it restarted to apply SCREEN EDGES / TEXT SIZE
        elif (version := tvscreens.take_update_watch(self.run_dir)) is not None:
            self.watch_update(version)
        elif self.starting:
            self.continue_start()

    def after_screen(self) -> None:
        """A classic screen closed: whatever it changed (apps, pairing, theme, sleep, display) shows here."""
        self.model.reload()
        self.updates.reload()  # SOFTWARE UPDATE's check is saved by the screen's own process
        self.can_sleep = power.can_suspend()
        self.can_wake = bool(power.wake_sources())
        self.idle.apply(power.effective_settings(power.load_settings(), self.can_sleep, self.can_wake))
        self.size = (0, 0)  # the theme or the picture size may be new: CSS and sizes again
        self.relayout(self.screen_size())
        self.settings.refresh()
        self.render_current()

    def render_current(self) -> None:
        if self.mode == "settings":
            self.render_settings()
        elif self.mode == "power":
            self.render_power()
        elif self.mode == "home":
            self.render_home()

    def screens_tick(self) -> None:
        """The 1 s tick while a classic screen is on top or an update runs: nothing else happens."""
        if self.mode == "update" and self.progress is not None:
            self.update_tick()
        if self.child_pid is not None:
            self.idle.keep_awake()  # the classic screen blanks the screen itself
            if self.home_request.exists():
                # Guide brought this window up: the screen is still open, put it back in front. The
                # Home press stays and is answered when the screen closes, as in classic Settings.
                session.focus_launcher(tvscreens.CHILD_TITLE)

    # ------------------------------------------------------------------ the start

    def continue_start(self) -> None:
        """The steps before the home screen, one at a time; a step that opens a screen ends this call
        and the screen's end calls it again: What's New (upgrades), the setup wizard, an update that
        runs (the TV interface restarted mid-way), then the auto-stream."""
        while self.start_steps:
            step = self.start_steps.pop(0)
            if step == "whatsnew":
                pending = whatsnew.due()
                if pending is not None:
                    self.open_whatsnew(*pending)
                    return
            elif step == "setup":
                if tvscreens.setup_due(self.run_dir) and self.open_screen("setup"):
                    return
            elif step == "update":
                if tvscreens.update_running(self.run_dir):
                    self.watch_update("")
                    return
            elif step == "autostream":
                self.starting = False
                self.autostream()
        self.starting = False


class Tv(Screens, session.Session):
    def __init__(self, application: "Gtk.Application") -> None:
        self.application = application
        self.window: Gtk.ApplicationWindow | None = None
        self.status = ""
        self.status_since = 0.0
        self.mode = "home"  # home, active, message, busy
        self.busy_depth = 0  # > 0 while a start or a wait runs its own loop
        self.busy_pressed = False
        self.answer: str | None = None  # what the message screen's keys chose
        self.active_index = 0
        self.ready_written = False
        self.size = (0, 0)
        self.last_reload = time.monotonic()
        self.was_running = False
        self.stream_host: stream.Host | None = None
        self.controllers = controllers.Monitor()
        self.updates = update.Checker()
        self.model = home.HomeModel(applications=visible_applications, controllers=self.controllers, updates=self.updates)
        self.can_sleep = power.can_suspend()
        self.can_wake = bool(power.wake_sources())
        self.idle = tvlayout.IdleWatch(
            power.effective_settings(power.load_settings(), self.can_sleep, self.can_wake),
            apps_running=self.apps_running, enabled=lambda: not power.smoke_test_active(),
        )
        self.css = Gtk.CssProvider()
        self.init_screens()

    # ------------------------------------------------------------------ building

    def build(self) -> None:
        window = self.window = Gtk.ApplicationWindow(application=self.application, title=TITLE)
        window.add_css_class("tv-root")
        overlay = Gtk.Overlay()
        window.set_child(overlay)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        overlay.set_child(self.stack)
        self.blank = Gtk.Box()
        self.blank.add_css_class("tv-blank")
        self.blank.set_visible(False)
        self.blank.set_hexpand(True)
        self.blank.set_vexpand(True)
        overlay.add_overlay(self.blank)

        # HOME
        page = self.home_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = self.bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bar.add_css_class("tv-bar")
        self.bar_name = label("COUCHLITEOS", "tv-row-title")
        self.bar_name.set_hexpand(True)
        self.bar_update = label("", "tv-warning", xalign=1.0, ellipsize=False)
        self.bar_battery = label("", xalign=1.0, ellipsize=False)
        self.bar_network = label("", xalign=1.0, ellipsize=False)
        self.bar_clock = label("", xalign=1.0, ellipsize=False)
        for widget in (self.bar_name, self.bar_update, self.bar_battery, self.bar_network, self.bar_clock):
            bar.append(widget)
        page.append(bar)
        self.row_boxes: list[tuple[Gtk.Label, Gtk.Box]] = []
        for _name in (home.GAMES, home.APPS, home.SYSTEM):
            title = label("", "tv-row-title")
            tiles = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            page.append(title)
            page.append(tiles)
            self.row_boxes.append((title, tiles))
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        page.append(spacer)
        self.home_status = label("", "tv-status", xalign=0.5)
        page.append(self.home_status)
        self.home_prompt = label(tvlayout.HOME_HINT, "tv-prompt", xalign=0.5)
        page.append(self.home_prompt)
        self.stack.add_named(page, "home")

        # STARTING / WAITING and messages share one simple layout: title, body, hint.
        self.pages: dict[str, tuple[Gtk.Box, Gtk.Label, Gtk.Label, Gtk.Label]] = {}
        for name in ("busy", "message"):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.set_valign(Gtk.Align.CENTER)
            title = label("", "tv-title", xalign=0.5, wrap=True)
            body = label("", "tv-body", xalign=0.5, wrap=True)
            hint = label("", "tv-prompt", xalign=0.5, wrap=True)
            for widget in (title, body, hint):
                box.append(widget)
            self.stack.add_named(box, name)
            self.pages[name] = (box, title, body, hint)

        # ACTIVE APPLICATIONS
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_valign(Gtk.Align.CENTER)
        box.append(label("ACTIVE APPLICATIONS", "tv-title", xalign=0.5))
        self.active_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.active_list.set_halign(Gtk.Align.CENTER)
        box.append(self.active_list)
        self.active_hint = label("", "tv-prompt", xalign=0.5, wrap=True)
        box.append(self.active_hint)
        self.active_page = box
        self.stack.add_named(box, "active")
        self.build_screens()

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        window.add_controller(keys)
        window.connect("map", self.on_map)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_USER,
        )
        self.relayout(self.screen_size())
        self.render_home()
        window.fullscreen()
        window.present()

    def screen_size(self) -> tuple[int, int]:
        """The window's size once shown, else the first monitor's."""
        if self.window is not None and self.window.get_height() > 0:
            return self.window.get_width(), self.window.get_height()
        try:
            monitors = Gdk.Display.get_default().get_monitors()
            geometry = monitors.get_item(0).get_geometry()
            return geometry.width, geometry.height
        except Exception:  # noqa: BLE001 - any size will do until the window has one
            return 1920, 1080

    def relayout(self, size: tuple[int, int]) -> None:
        if size == self.size:
            return
        self.size = size
        self.layout = tvlayout.Layout(*size)
        colours = current_theme()
        self.css.load_from_data((tvlayout.stylesheet(colours, self.layout)
                                 + tvscreens.stylesheet(colours, self.layout)).encode(), -1)
        layout = self.layout
        for page in (self.home_page, self.active_page, *(entry[0] for entry in self.pages.values()),
                     *self.screen_pages):
            page.set_margin_start(layout.margin_x)
            page.set_margin_end(layout.margin_x)
            page.set_margin_top(layout.margin_y)
            page.set_margin_bottom(layout.margin_y)
            page.set_spacing(layout.px(16))
        self.bar.set_spacing(layout.px(40))
        for _title, tiles in self.row_boxes:
            tiles.set_spacing(layout.gap)
            tiles.set_size_request(-1, -1)
        self.relayout_screens()
        self.render_home()

    # ------------------------------------------------------------------ drawing

    def tile(self, item: home.Tile, row: str, focused: bool) -> "Gtk.Box":
        layout = self.layout
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("tv-tile")
        box.set_hexpand(False)  # the labels expand within the tile, the tile keeps its width
        if focused:
            box.add_css_class("tv-focused")
        box.set_size_request(layout.tile_width, layout.tile_height(row))
        if row != home.SYSTEM:
            # A generated title card: the PC's initial on a game, the app's own on an app.
            mark = label(tvlayout.initial(item.detail or item.label), "tv-initial", xalign=0.5)
            mark.set_vexpand(True)
            box.append(mark)
        name = label(item.label, "tv-name", xalign=0.5 if row == home.SYSTEM else 0.0)
        name.set_vexpand(row == home.SYSTEM)
        box.append(name)
        if item.detail:
            box.append(label(item.detail, "tv-detail"))
        return box

    def render_home(self) -> None:
        if not hasattr(self, "layout"):
            return
        focus_row, focus_column = self.model.focus
        for index, (title, tiles) in enumerate(self.row_boxes):
            clear(tiles)
            row = self.model.rows[index] if index < len(self.model.rows) else None
            shown = row is not None and bool(row.tiles)
            title.set_visible(shown)
            tiles.set_visible(shown)
            if not shown:
                continue
            title.set_label(row.title)
            column = self.model.columns[row.name]
            for at in tvlayout.visible(len(row.tiles), column):
                tiles.append(self.tile(row.tiles[at], row.name, index == focus_row and at == focus_column))
        self.render_bar()

    def render_bar(self) -> None:
        status = self.model.status()
        self.bar_clock.set_label(status.clock)
        self.bar_network.set_label(status.network)
        self.bar_battery.set_label(status.battery)
        self.bar_battery.set_visible(bool(status.battery))
        self.bar_update.set_label(f"UPDATE {status.update}" if status.update else "")
        self.bar_update.set_visible(bool(status.update))
        self.home_status.set_label(self.status)

    def show(self, name: str) -> None:
        self.mode = name
        self.stack.set_visible_child_name(name)

    def show_text(self, name: str, title: str, body: str, hint: str) -> None:
        _box, title_label, body_label, hint_label = self.pages[name]
        title_label.set_label(title)
        body_label.set_label(body)
        hint_label.set_label(hint)
        if self.mode != name:
            self.show(name)

    def render_active(self) -> None:
        clear(self.active_list)
        running = self.running_applications()
        rows = [f"{app.name}  RUNNING" for app in running] + ["BACK TO HOME"]
        self.active_index = min(self.active_index, len(rows) - 1)
        for index, text in enumerate(rows):
            item = label(text, "tv-item", xalign=0.5)
            if index == self.active_index:
                item.add_css_class("tv-focused")
            self.active_list.append(item)
        self.active_hint.set_label(self.status or (tvlayout.ACTIVE_HINT if running else BACK_HINT))

    # ------------------------------------------------------------------ the main loop's own loops

    def pump(self, seconds: float = 0.1) -> None:
        """Keep the window alive for `seconds` while a start or a wait blocks the caller."""
        context = GLib.MainContext.default()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not context.iteration(False):
                time.sleep(0.01)

    def draw_launching(self, label_text: str, frame: str) -> None:
        self.show_text("busy", f"STARTING {label_text}  {frame}", "PLEASE WAIT",
                       "HOLD SELECT+START (VIEW+MENU) TO COME BACK TO THE LAUNCHER")

    def launch_wait_begin(self) -> None:
        self.busy_depth += 1

    def launch_wait(self) -> None:
        self.pump(0.1)

    def launch_wait_end(self) -> None:
        self.busy_depth -= 1
        if self.busy_depth == 0:
            self.show("home")

    def ask(self, title: str, body: str, hint: str) -> str:
        """Show a message and wait for A / Enter ("yes") or B / Esc ("no")."""
        self.answer = None
        self.set_launcher_focus(True)  # over a failed app: keep the controller on this screen
        self.busy_depth += 1
        try:
            self.show_text("message", title, body, hint)
            while self.answer is None:
                self.pump(0.1)
                if self.take_home_request():
                    self.answer = "no"
        finally:
            self.busy_depth -= 1
        return self.answer

    def show_launch_failure(
        self, label_text: str, message: str, app: apps.Application | None = None, *, retry: bool = True
    ) -> str:
        answer = self.ask(f"{label_text} DID NOT START", message.upper(),
                          tvlayout.FAILURE_HINT if retry else BACK_HINT)
        return "retry" if answer == "yes" and retry else "dismiss"

    def prepare_remote_desktop(self, app: apps.Application) -> bool:
        # The certificate check and the password prompt are curses screens for now: the whole
        # start runs in the classic screen on top, and this start stops here.
        self.open_screen("connect", app.id)
        return False

    def start_stream(self, host: stream.Host, app_name: str, *, by_hand: bool = False) -> bool:
        self.stream_host = host
        try:
            return super().start_stream(host, app_name, by_hand=by_hand)
        finally:
            self.stream_host = None

    def wait_tick(self, title: str, detail: str) -> bool:
        """One step of a cancellable wait: draw it, keep the window alive; True when it was cancelled."""
        self.show_text("busy", title, detail, tvlayout.WAIT_HINT)
        self.pump(0.1)
        pressed, self.busy_pressed = self.busy_pressed, False
        return pressed or self.take_home_request()

    def wake_before_moonlight(self, app: apps.Application, auto: bool = False) -> bool:
        """Wake the PC being streamed from if it sleeps; any button stops waiting and cancels."""
        try:
            host = self.stream_host or stream.default_host(stream.load_hosts(), stream.load_settings())
            if host is None or not host.mac or not stream.link_up():
                return True
            self.busy_depth += 1
            self.busy_pressed = False
            try:
                result = stream.wake_and_wait(
                    host, sleep=self.pump,
                    tick=lambda elapsed: self.wait_tick(
                        f"WAKING {host.label}...  {session.SPINNER[int(elapsed * 4) % len(session.SPINNER)]}",
                        f"{int(elapsed)} OF {int(stream.WAKE_TIMEOUT)} SECONDS"),
                )
            finally:
                self.busy_depth -= 1
        except (OSError, ValueError):
            return True
        if result == "cancelled":
            self.status = f"{self.stream_word} CANCELLED"
            return False
        return True  # awake, or it stayed silent: Moonlight still lists the PC and says what is wrong

    def autostream(self) -> None:
        target = self.autostream_target()
        if target is None:
            return
        host, app_name = target
        self.busy_depth += 1
        self.busy_pressed = False
        try:
            end = time.monotonic() + AUTOSTREAM_SECONDS
            while (remaining := end - time.monotonic()) > 0:
                if self.wait_tick(f"STARTING STREAM TO {host.label}...", f"{int(remaining) + 1} SECONDS"):
                    self.status = "AUTO-STREAM CANCELLED"
                    return
        finally:
            self.busy_depth -= 1
            self.show("home")
        self.start_stream(host, app_name)
        self.after_launch()

    # ------------------------------------------------------------------ keys

    def on_key(self, _controller, keyval: int, _keycode: int, _state) -> bool:
        name = tvlayout.action(Gdk.keyval_name(keyval))
        display.confirm_restore()
        if self.idle.key():
            self.blank.set_visible(False)
            return True  # the key that wakes the screen does nothing else
        if name is None:
            return False
        if self.child_pid is not None:
            session.focus_launcher(tvscreens.CHILD_TITLE)  # Guide raised this window: the screen is still open
            return True
        if self.busy_depth and self.mode != "message":
            self.busy_pressed = True
            return True
        if self.status and self.mode == "home":
            self.status = ""  # a press dismisses the last result
            self.home_status.set_label("")
        handler = {"home": self.home_key, "active": self.active_key, "message": self.message_key,
                   **self.screen_keys()}.get(self.mode)
        if handler is not None:
            handler(name)
        return True

    def message_key(self, name: str) -> None:
        if name == "activate":
            self.answer = "yes"
        elif name in ("back", "home"):
            self.answer = "no"
        if self.answer is not None and not self.busy_depth:
            self.show("home")  # a screen that was only shown, not asked
            self.answer = None
            self.render_home()

    def home_key(self, name: str) -> None:
        model = self.model
        if name in ("up", "down", "left", "right"):
            dx = {"left": -1, "right": 1}.get(name, 0)
            dy = {"up": -1, "down": 1}.get(name, 0)
            if model.move(dx, dy):
                self.render_home()
        elif name == "back":
            if model.back():
                self.render_home()
        elif name == "activate":
            self.run_action(model.activate())
        elif name == "home":
            self.open_active()
        elif name.startswith("shortcut:"):
            tag = name.split(":", 1)[1]
            app = next((item for item in visible_applications().applications if item.shortcut == tag), None)
            if app is None:
                self.status = f"NO BUTTON USES THE {apps.SHORTCUTS[tag]} SHORTCUT"
                self.render_bar()
            else:
                self.launch_app(app)
                self.after_launch()

    def active_key(self, name: str) -> None:
        running = self.running_applications()
        count = len(running) + 1
        if name in ("up", "down"):
            self.active_index = max(0, min(count - 1, self.active_index + (1 if name == "down" else -1)))
            self.status = ""
        elif name in ("back", "home") or (name == "activate" and self.active_index >= len(running)):
            self.status = ""
            self.show("home")
            self.render_home()
            return
        elif name == "activate":
            app = running[self.active_index]
            if self.focus_app(app):
                self.status = f"RESUMED {app.name}"
                self.show("home")
                self.render_home()
                return
            self.status = f"COULD NOT FOCUS {app.name}: PRESS Y / SQUARE OR DELETE TO CLOSE IT"
        elif name == "close" and self.active_index < len(running):
            app = running[self.active_index]
            self.close_app(app)
            self.status = f"CLOSING {app.name}"
        self.render_active()

    def run_action(self, action: home.Action | None) -> None:
        if not action:
            return
        if action[0] == "stream":
            _kind, host, app_name = action
            self.start_stream(host, app_name, by_hand=True)
            self.after_launch()
        elif action[0] == "app":
            app = self.app_by_id(action[1])
            if app is None:
                self.status = f"{action[1].upper()} IS NOT AVAILABLE: CHECK SETTINGS > APPLICATIONS"
            else:
                self.launch_app(app)
            self.after_launch()
        else:
            self.open_view(action[0])

    def open_view(self, name: str) -> None:
        """A screen of its own: SETTINGS and POWER here, HOSTS and SOFTWARE UPDATE as classic screens."""
        if name == "settings":
            self.open_settings()
        elif name == "power":
            self.open_power()
        elif name in tvscreens.TILE_SCREENS:
            self.open_screen(tvscreens.TILE_SCREENS[name])
        else:
            self.show_text("message", name.upper(), "", BACK_HINT)

    def open_active(self) -> None:
        self.set_launcher_focus(True)  # gamepad-nav forwards keys to this menu while an app runs
        self.active_index = 0
        self.status = ""
        self.render_active()
        self.show("active")

    def after_launch(self) -> None:
        """Back from a start: read the rows again, land on the game just played."""
        self.model.reload()
        self.model.focus_last_played()
        self.show("home")
        self.render_home()

    # ------------------------------------------------------------------ the 1 s tick

    def apps_running(self) -> bool:
        try:
            return bool(self.running_applications())
        except Exception:  # noqa: BLE001 - a failed check must not sleep the box or crash the launcher
            return True

    def tick(self) -> bool:
        try:
            self.tick_once()
        except Exception as error:  # noqa: BLE001 - the home screen must keep running
            display.log(f"tv tick failed: {error!r}", session.LOG)
        return True  # keep the timeout

    def tick_once(self) -> None:
        if self.busy_depth:
            self.idle.keep_awake()
            return
        if self.child_pid is not None or self.mode == "update":
            self.screens_tick()
            return
        self.relayout(self.screen_size())
        if self.take_resumed():
            self.idle.resumed()
            self.blank.set_visible(False)
            self.can_sleep = power.can_suspend()
            self.idle.apply(power.effective_settings(power.load_settings(), self.can_sleep, self.can_wake))
            self.status = "RESUMED FROM SLEEP"
            session.focus_launcher()
            self.show("home")
            self.autostream()
        if self.take_home_request():
            self.idle.keep_awake()
            self.blank.set_visible(False)
            session.focus_launcher()
            if self.mode == "active":
                self.show("home")
            elif self.running_applications():
                self.open_active()
            else:
                self.set_launcher_focus(True)
                self.show("home")
        action = self.idle.tick()
        if action == power.BLANK:
            self.blank.set_visible(True)
        elif action == power.SLEEP and self.can_sleep:
            try:
                self.request("suspend")  # couchliteos-suspend.path removes it before suspending
            except OSError:
                pass
        running = self.apps_running()
        now = time.monotonic()
        if (self.was_running and not running) or now - self.last_reload >= RELOAD_SECONDS:
            if self.was_running and not running:
                self.model.reload()
                self.model.focus_last_played()  # back from a stream: on the game just played
            elif self.mode == "home":
                self.model.reload()
            self.last_reload = now
            if self.mode == "home":
                self.render_home()
        self.was_running = running
        if self.mode == "home":
            self.render_bar()
            self.offer_update()
        elif self.mode == "active":
            self.render_active()

    def offer_update(self) -> None:
        """Once per release, ask on the home screen whether to open SOFTWARE UPDATE."""
        version = self.updates.to_offer()
        if not version or not self.may_offer_update(blanked=self.idle.blanked):
            return
        self.idle.keep_awake()
        self.updates.mark_offered(version)  # before the question: a crash cannot make it come back
        if self.ask(f"COUCHLITEOS {version} IS AVAILABLE", "OPEN SOFTWARE UPDATE NOW?", tvlayout.QUESTION_HINT) == "yes":
            self.open_view("software-update")
        else:
            self.show("home")

    # ------------------------------------------------------------------ start

    def on_map(self, window) -> None:
        def first_frame(*_args) -> bool:
            if not self.ready_written:
                self.ready_written = True
                try:
                    (self.run_dir / "launcher-ready").touch()
                except OSError as error:
                    print(f"couchliteos-tv: cannot write launcher-ready: {error}", file=sys.stderr)
                GLib.idle_add(self.after_first_frame)
            return False

        window.add_tick_callback(first_frame)

    def after_first_frame(self) -> bool:
        if display.restore_saved_mode() is None:
            self.status = "SAVED DISPLAY MODE SKIPPED — CHOOSE IT AGAIN IN SETTINGS > DISPLAY"
            self.render_bar()
        self.continue_start()
        return False

    def activate(self, _application) -> None:
        if self.window is not None:
            self.window.present()
            return
        self.prepare_session()
        self.controllers.start()
        self.updates.start()
        self.build()
        GLib.timeout_add_seconds(TICK_SECONDS, self.tick)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CouchLiteOS TV interface")
    parser.parse_args(argv)
    if Gtk is None:
        print(f"couchliteos-tv: GTK 4 is not available: {GI_ERROR}", file=sys.stderr)
        return INIT_FAILED
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        print("couchliteos-tv: GTK could not open the display", file=sys.stderr)
        return INIT_FAILED
    GLib.set_prgname("couchliteos-launcher")
    application = Gtk.Application(application_id=APPLICATION_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    tv = Tv(application)
    application.connect("activate", tv.activate)
    status = application.run([sys.argv[0]])
    return status if tv.ready_written else INIT_FAILED  # never drew a frame: let the wrapper fall back


if __name__ == "__main__":
    sys.exit(main())
