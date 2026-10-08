#!/usr/bin/python3
"""The TV interface: the GTK 4 home screen CouchLiteOS shows inside Cage.

One full-screen window holding a stack of screens: HOME (top bar, GAMES, APPS and SYSTEM
rows, prompt bar), STARTING / WAITING, a message screen (a failure, a question) and ACTIVE
APPLICATIONS (a held Home shortcut with apps running); SETTINGS (two panes), POWER, WHAT'S
NEW and the SOFTWARE UPDATE progress come from couchliteos_tvscreens. Over them: the quick
menu (a Guide tap), toasts and the blank screen. A Settings screen the TV interface does
not draw itself, the setup wizard and a Remote Desktop start run as the classic curses
screen in a foot window on top (`couchliteos-launcher --screen <name>`); the rows are read
again when it closes.

The rows come from couchliteos_home.HomeModel; starting, resuming and closing apps is
couchliteos_session's, the same code the classic launcher runs. Sizes, colours, keys and
blanking are couchliteos_tvlayout's; the quick menu, prompt bar texts, toasts and sounds
are couchliteos_quick's.

Keys are the ones gamepad-nav sends (arrows, Enter, Esc, Delete, F5-F8, F12), so a
controller, a TV remote and a keyboard all work. Guide / Home arrives as home.request.

Exit status INIT_FAILED (3) means GTK could not start (no gi, no display, no renderer):
couchliteos-session then tries once more with GSK_RENDERER=cairo, then starts the classic
launcher. `launcher-ready` is written after the home screen's first frame is drawn.

Before anything else is built the window shows the boot picture Plymouth showed
(couchliteos_gtk_boot); the home screen is built behind it once it is on screen, and it fades
away when the home screen has been drawn.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import couchliteos_apps as apps
import couchliteos_artwork as artwork
import couchliteos_battery as battery
import couchliteos_controllers as controllers
import couchliteos_controls as controls
import couchliteos_display as display
import couchliteos_errors as errors
import couchliteos_home as home
import couchliteos_icons as icons
import couchliteos_motion as motion
import couchliteos_music as music
import couchliteos_pcstatus as pcstatus
import couchliteos_power as power
import couchliteos_quick as quick
import couchliteos_session as session
import couchliteos_siteicon as siteicon
import couchliteos_softwareupdate as softwareupdate
import couchliteos_stream as stream
import couchliteos_theme as theme
import couchliteos_tile as tile
import couchliteos_tvlayout as tvlayout
import couchliteos_tvscreens as tvscreens
import couchliteos_update as update
import couchliteos_wave as wave
import couchliteos_whatsnew as whatsnew
import couchliteos_xmb as xmb

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

    import couchliteos_gtk_boot as gtk_boot
    import couchliteos_gtk_xmb as gtk_xmb
except (ImportError, ValueError) as _error:  # no PyGObject or no GTK 4 typelib
    Gtk = None
    GI_ERROR = str(_error)
else:
    GI_ERROR = ""

INIT_FAILED = 3
TITLE = "CouchLiteOS Launcher"  # the classic launcher's title too: focus_launcher() finds either
APPLICATION_ID = "org.couchliteos.Launcher"
TICK_SECONDS = 1
LOADING_WAIT_MS = 2000  # build the home screen anyway if the boot picture's frame never comes
RELOAD_SECONDS = 5  # how often the rows are read again (pairing, apps added in Settings)
AUTOSTREAM_SECONDS = 5
REPO_THEMES = pathlib.Path(__file__).resolve().parents[1] / "overlay/usr/share/couchliteos/themes"
BACK_HINT = "B / CIRCLE OR ESC GOES BACK"
CHOICE_HINT = "A / CROSS OR ENTER CHOOSES  ·  B / CIRCLE OR ESC GOES BACK"
# A failure screen's buttons (couchliteos_errors) that open a classic screen on top.
FAILURE_SCREENS = {errors.NETWORK.id: "network", errors.SUPPORT.id: "support-file",
                   errors.BLUETOOTH.id: "bluetooth"}
WAKE_PC = errors.Action("WAKE PC", "wake-pc")  # Moonlight's failures, as the classic launcher's
errors.register_action(WAKE_PC, app_ids=("moonlight",))
TOAST_TICK_MS = 250
KEY_HOME_SECONDS = 1.0  # the Home key reaches the window and gamepad-nav: one press, not two
ART_COLUMNS = 6
ART_HINT = "A / CROSS OR ENTER CHOOSES  ·  B / CIRCLE OR ESC GOES BACK"
# Labels that end in "…" on purpose when the text is longer than their box (a tile's title and
# detail); --dump-layout marks them, and the headless run accepts them shortened.
ELLIPSIZED_ON_PURPOSE = ("tv-name", "tv-detail")
BACKDROP_BLUR_WIDTH = 12  # px: a cover shrunk to this and stretched back to the screen is a cheap blur
BACKDROP_ART_ALPHA = 80  # of 255: the blurred cover over the theme background (dimmed)
BACKDROP_WALLPAPER_ALPHA = 140  # of 255: the theme's wallpaper, dimmed less
BACKDROP_MAX_FILE = 20 * 1024 * 1024
BACKDROP_CACHE = 16
SCRIPT_STEP_MS = 400
ROWS = "rows"  # [appearance] home = rows: the rows of tiles instead of the XMB (until beta 8)
WAVE_HOURS = 4  # the wave's colours follow the time of day, in quarter hours


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
        # A Guide / Home press the screen did not take must not open the quick menu later.
        self.home_request.unlink(missing_ok=True)
        if not self.child_left_an_app():
            session.focus_launcher()
            self.set_launcher_focus(True)
        if tvscreens.take_switch_interface(self.run_dir):
            self.application.quit()  # Restart=always: couchliteos-session starts the classic launcher
            return
        self.after_screen()
        if (self.run_dir / tvscreens.REOPEN_SETUP).exists():
            self.open_screen("setup")  # it restarted for a new picture size: carry on at the next step
        elif tvscreens.take_reopen_display(self.run_dir):
            self.open_screen("display")  # it restarted to apply SCREEN EDGES / TEXT SIZE
        elif (version := tvscreens.take_update_watch(self.run_dir)) is not None:
            self.watch_update(version)
        elif self.starting:
            self.continue_start()

    def child_left_an_app(self) -> bool:
        """The closed classic screen started something that now has the screen and the controller
        (CONNECT for Remote Desktop): its start let go of launcher-focus and the app runs. Raising
        this window over it would leave gamepad-nav dropping every press."""
        run = self.run_dir
        if (run / session.LAUNCHER_FOCUS_NAME).exists():
            return False
        return (run / "app-active").exists() or self.any_app_running()

    def after_screen(self) -> None:
        """A classic screen closed: whatever it changed (apps, pairing, theme, sleep, display) shows here."""
        self.model.reload()
        self.updates.reload()  # SOFTWARE UPDATE's check is saved by the screen's own process
        self.can_sleep = power.can_suspend()
        self.can_wake = bool(power.wake_sources())
        self.idle.apply(power.effective_settings(power.load_settings(), self.can_sleep, self.can_wake, self.battery.on_battery()))
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
        # Guide / Home (a tap or a held shortcut) does nothing else here: the quick menu cannot be
        # drawn over a classic screen's window, and an update cannot be left. A classic screen's own
        # loops (an app it started, ACTIVE APPLICATIONS) wait for the same file, so it is left for
        # them; gamepad-nav raised this window with it, so the screen is put back in front, once per
        # press. on_child_exit drops a press the screen never took.
        if self.mode == "update" and self.progress is not None:
            self.update_tick()
        if self.child_pid is not None:
            self.idle.keep_awake()  # the classic screen blanks the screen itself
            try:
                stamp = self.home_request.stat().st_mtime_ns
            except OSError:
                stamp = None
            if stamp is not None and stamp != getattr(self, "child_home_seen", None):
                self.child_home_seen = stamp
                session.focus_launcher(tvscreens.CHILD_TITLE)
        else:
            quick.take_request(self.home_request)  # an update cannot be left: the press is dropped

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


class Look:
    """The theme on screen: the stylesheets (written again when the theme changes) and the
    background behind every screen: the focused game's cover, blurred and dimmed, on the home
    screen, else the theme's wallpaper, else the theme's background colour. Part of Tv."""

    def init_look(self) -> None:
        self.colours = theme.FALLBACK
        self.theme_stamp: object = None
        self.backdrop_textures: dict[tuple, "Gdk.Texture | None"] = {}

    def build_backdrop(self, overlay: "Gtk.Overlay") -> None:
        """The background picture (the XMB's wave), with the stack of screens over it."""
        if self.cross:
            failed = gtk_xmb.wave_failed_before(self.run_dir)
            how = wave.mode(os.environ.get("GSK_RENDERER"), self.motion_level, current_theme().name, failed)
            self.wave = gtk_xmb.make_wave(how, self.wave_palette(), self.run_dir,
                                          lambda text: display.log(text, session.LOG))
            self.wave.set_can_target(False)
            overlay.set_child(self.wave)
            overlay.add_overlay(self.stack)
            overlay.set_measure_overlay(self.stack, True)
            return
        picture = self.backdrop = Gtk.Picture()
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_can_shrink(True)
        picture.set_can_target(False)
        overlay.set_child(picture)
        overlay.add_overlay(self.stack)
        overlay.set_measure_overlay(self.stack, True)

    @staticmethod
    def theme_stamp_now() -> object:
        """What changes when Settings > APPEARANCE saves another theme or accent."""
        try:
            info = theme.CONFIG.stat()
            return info.st_mtime_ns, info.st_size
        except OSError:
            return None

    def load_css(self) -> None:
        """Every stylesheet (home and its screens, the quick menu) from the current theme, at user
        priority. GTK 4.14 has no CSS var(), so the colours are written in (not theme.css()'s variables)."""
        self.theme_stamp = self.theme_stamp_now()
        colours = self.colours = current_theme()
        self.css.load_from_data((tvlayout.stylesheet(colours, self.layout)
                                 + tvscreens.stylesheet(colours, self.layout)).encode(), -1)
        self.quick_css.load_from_data(quick.stylesheet(colours, self.layout).encode(), -1)
        self.render_backdrop()
        if self.cross and hasattr(self, "wave"):
            self.wave.set_colours(self.wave_palette())
            self.xmb_view.text_colour = tuple(int(theme.parse_colour(colours.colours["text"])[at:at + 2], 16) / 255
                                              for at in (0, 2, 4))
            self.xmb_view.queue_draw()

    def wave_palette(self) -> "wave.Palette":
        now = time.localtime()
        self.wave_quarter = now.tm_hour * WAVE_HOURS + now.tm_min * WAVE_HOURS // 60
        return wave.palette(current_theme(), self.wave_quarter / WAVE_HOURS)

    def theme_tick(self) -> None:
        """The 1 s tick: a theme or accent saved since the CSS was written is applied now."""
        if hasattr(self, "layout") and self.theme_stamp_now() != self.theme_stamp:
            self.load_css()
            self.music.settings()
        elif self.cross and hasattr(self, "wave"):
            now = time.localtime()
            if now.tm_hour * WAVE_HOURS + now.tm_min * WAVE_HOURS // 60 != self.wave_quarter:
                self.wave.set_colours(self.wave_palette())

    def render_backdrop(self) -> None:
        if not hasattr(self, "backdrop"):
            return
        texture = None
        if self.mode == "home":
            item = self.model.focused()
            path = self.cover(item) if item is not None else None
            texture = self.backdrop_texture(path, blur=True) if path is not None else None
        if texture is None and self.colours.wallpaper.startswith("/"):
            texture = self.backdrop_texture(pathlib.Path(self.colours.wallpaper), blur=False)
        if self.backdrop.get_paintable() is not texture:
            self.backdrop.set_paintable(texture)

    def backdrop_texture(self, path: pathlib.Path, blur: bool) -> "Gdk.Texture | None":
        """`path` dimmed over the theme background: for a cover shrunk and stretched back (a blur),
        a wallpaper at screen size. Kept until the file, the theme or the screen size changes."""
        try:
            info = path.stat()
        except OSError:
            return None
        width, height = self.size
        key = (str(path), info.st_mtime_ns, blur, self.colours.colours["background"], width, height)
        if key in self.backdrop_textures:
            return self.backdrop_textures[key]
        texture = None
        if 0 < info.st_size <= BACKDROP_MAX_FILE and width > 0 and height > 0:
            try:
                if blur:
                    image = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), BACKDROP_BLUR_WIDTH, -1, True)
                    # A tenth of the screen: Gtk.Picture stretches it the rest of the way, smoothly.
                    out_w, out_h, alpha = max(1, width // 10), max(1, height // 10), BACKDROP_ART_ALPHA
                else:
                    image = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), width, height, True)
                    out_w, out_h, alpha = width, height, BACKDROP_WALLPAPER_ALPHA
                scale = max(out_w / image.get_width(), out_h / image.get_height())  # fill it, crop the rest
                base = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, out_w, out_h)
                base.fill(int(self.colours.colours["background"], 16) << 8 | 0xFF)
                image.composite(base, 0, 0, out_w, out_h,
                                (out_w - image.get_width() * scale) / 2, (out_h - image.get_height() * scale) / 2,
                                scale, scale, GdkPixbuf.InterpType.BILINEAR, alpha)
                texture = Gdk.Texture.new_for_pixbuf(base)
            except GLib.Error as error:
                display.log(f"tv backdrop {path.name}: {error.message}", session.LOG)
        if len(self.backdrop_textures) >= BACKDROP_CACHE:
            self.backdrop_textures.clear()
        self.backdrop_textures[key] = texture
        return texture


class Script:
    """`--script` and `--dump-layout`, for tests/tv-headless.sh: steps fed to the key handler one at
    a time, and a JSON record of every label on screen. Part of Tv."""

    script: list[str] = []
    script_failed = False
    dump_dir: pathlib.Path | None = None
    shot_dir: pathlib.Path | None = None

    def start_script(self) -> None:
        if self.script:
            GLib.timeout_add(SCRIPT_STEP_MS, self.script_step)

    def script_step(self) -> bool:
        """One step: a Gdk key name, `wait:<ms>`, `dump:<name>`, `theme:<name>` (saved as Settings
        saves it), `open:whatsnew`, `open:update` (writes a downloading status) or `quit`. The next step is timed before this one
        runs, so a step that waits for an answer (a question) is answered by the next one."""
        if not self.script:
            return False
        step = self.script.pop(0)
        kind, _, value = step.partition(":")
        delay = int(value) if kind == "wait" and value.isdigit() else SCRIPT_STEP_MS
        if self.script:
            GLib.timeout_add(delay, self.script_step)
        try:
            if kind == "wait":
                pass
            elif kind == "dump":
                self.dump_layout(value)
            elif kind == "theme":
                theme.save_choice(value, "")
            elif step == "open:whatsnew":
                self.open_whatsnew(*whatsnew_versions())
            elif step == "open:update":  # as if the update service were downloading
                (self.run_dir / softwareupdate.STATUS).write_text(
                    json.dumps({"phase": "downloading", "percent": 42, "version": "9.9.9"}), encoding="utf-8")
                self.watch_update("9.9.9")
            elif step == "quit":
                self.application.quit()
            else:
                keyval = Gdk.keyval_from_name(step)
                if keyval in (0, Gdk.KEY_VoidSymbol):
                    raise ValueError("not a step or a key name")
                self.on_key(None, keyval, 0, None)
        except Exception as error:  # noqa: BLE001 - reported, and the run exits non-zero
            self.script_failed = True
            print(f"couchliteos-tv: script step {step!r} failed: {error!r}", file=sys.stderr)
        return False

    def dump_layout(self, name: str) -> None:
        """<dump dir>/<name>.json: the screen, its size and theme, and every label on screen with its
        text, font size in px, position, width, natural (one line, unshortened) text width and whether
        it was shortened; then <shot dir>/<name>.png with grim."""
        labels = []

        def walk(widget: "Gtk.Widget") -> None:
            if not widget.get_mapped() or widget.get_opacity() == 0:
                return
            if isinstance(widget, Gtk.Label) and widget.get_text().strip():
                labels.append(self.label_facts(widget))
            child = widget.get_first_child()
            while child is not None:
                walk(child)
                child = child.get_next_sibling()

        walk(self.window)
        if self.cross and self.mode == "home":
            labels += self.xmb_view.drawn_labels
        record = {"name": name, "screen": self.mode, "quick": self.quick_open, "theme": self.colours.name,
                  "width": self.window.get_width(), "height": self.window.get_height(), "labels": labels}
        if self.dump_dir is not None:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            (self.dump_dir / f"{name}.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
        if self.shot_dir is not None:
            self.shot_dir.mkdir(parents=True, exist_ok=True)
            self.pump(0.2)  # the frame with this screen on it
            subprocess.run(["grim", str(self.shot_dir / f"{name}.png")], check=True, timeout=30)

    def label_facts(self, widget: "Gtk.Label") -> dict:
        font = widget.get_pango_context().get_font_description()
        size = font.get_size() / Pango.SCALE
        ok, bounds = widget.compute_bounds(self.window)
        return {
            "text": widget.get_text(),
            "classes": list(widget.get_css_classes()),
            "font_px": round(size if font.get_size_is_absolute() else size * 96 / 72, 1),
            "x": round(bounds.get_x()) if ok else 0,
            "y": round(bounds.get_y()) if ok else 0,
            "right": round(bounds.get_x() + bounds.get_width()) if ok else 0,
            "width": widget.get_width(),
            "natural": widget.create_pango_layout(widget.get_text()).get_pixel_size()[0],
            "wrap": widget.get_wrap(),
            "ellipsized": widget.get_layout().is_ellipsized(),
            "on_purpose": any(name in ELLIPSIZED_ON_PURPOSE for name in widget.get_css_classes()),
        }


def whatsnew_versions() -> tuple[str, str]:
    """(this version, the one before it) for `open:whatsnew`: what an upgrade from it shows."""
    versions = [version for version, _features in whatsnew.RELEASES]
    return versions[0], versions[1] if len(versions) > 1 else ""


class Tv(Screens, Look, Script, session.Session):
    cross = False  # the XMB home (init_xmb), else the rows
    music = music.Music(enabled=lambda: False, track=lambda: None)  # silent until init_xmb
    def __init__(self, application: "Gtk.Application") -> None:
        self.application = application
        self.window: Gtk.ApplicationWindow | None = None
        self.status = ""
        self.status_since = 0.0
        self.mode = "home"  # home, active, message, busy
        self.busy_depth = 0  # > 0 while a start or a wait runs its own loop
        self.busy_pressed = False
        self.answer: str | None = None  # what the message screen's keys chose
        self.choices: list[str] = []  # the message screen's buttons (choose), focus at choice
        self.choice = 0
        self.active_index = 0
        self.ready_written = False
        self.home_built = False
        self.loading: "gtk_boot.LoadingScreen | None" = None
        self.size = (0, 0)
        self.last_reload = time.monotonic()
        self.was_running = False
        self.stream_host: stream.Host | None = None
        self.controllers = controllers.Monitor()
        self.updates = update.Checker()
        self.battery = battery.Monitor()  # the PC's own battery; nothing shown without one
        self.model = home.HomeModel(
            applications=visible_applications, controllers=self.controllers, updates=self.updates, battery=self.battery,
        )
        self.can_sleep = power.can_suspend()
        self.can_wake = bool(power.wake_sources())
        self.idle = tvlayout.IdleWatch(
            power.effective_settings(power.load_settings(), self.can_sleep, self.can_wake, self.battery.on_battery()),
            apps_running=self.apps_running, enabled=lambda: not power.smoke_test_active(),
        )
        self.css = Gtk.CssProvider()
        self.init_look()
        self.init_screens()
        self.init_quick()
        self.init_xmb()

    # ------------------------------------------------------------------ building

    def show_loading(self) -> None:
        """The window with only the boot picture in it, on screen as early as can be: the home
        screen is built once that frame is up (build_home), under it."""
        window = self.window = Gtk.ApplicationWindow(application=self.application, title=TITLE)
        window.add_css_class("tv-root")
        self.root = Gtk.Overlay()
        window.set_child(self.root)
        self.loading = gtk_boot.LoadingScreen(self.motion_level)
        self.root.add_overlay(self.loading)
        window.connect("map", lambda _window: self.when_painted(window, self.loading_drawn))
        GLib.timeout_add(LOADING_WAIT_MS, self.build_home)
        window.fullscreen()
        window.present()

    def loading_drawn(self) -> bool:
        print(f"couchliteos-tv: boot picture drawn {time.monotonic():.1f} s after boot", file=sys.stderr)
        return self.build_home()

    def build_home(self) -> bool:
        if not self.home_built:
            self.home_built = True
            self.build()
            GLib.timeout_add_seconds(TICK_SECONDS, self.tick)
            GLib.timeout_add(TOAST_TICK_MS, self.render_toast)
        return False

    def build(self) -> None:
        window = self.window
        overlay = Gtk.Overlay()
        self.root.set_child(overlay)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.build_backdrop(overlay)
        self.build_quick(overlay)
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
        if self.cross:
            self.xmb_view = gtk_xmb.XmbView(self.xmb, self.xmb_picture, icons.bundled, self.xmb_status,
                                            self.motion_level)
            home_overlay = Gtk.Overlay()
            home_overlay.set_child(page)
            home_overlay.add_overlay(self.xmb_view)
            page.set_visible(False)  # the rows' widgets stay for the code that sets them
            self.stack.add_named(home_overlay, "home")
        else:
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
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_USER,
        )
        self.relayout(self.screen_size())
        self.render_home()
        self.watch_first_frame(window)

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
        self.load_css()
        self.relayout_quick()
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
            # A game's cover art when there is some, else a generated title card: the PC's
            # initial on a game, the app's own on an app.
            mark = self.art_picture(self.cover(item)) if row == home.GAMES else None
            if mark is None:
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
        if self.cross:
            self.xmb.reload(reread_home=False)
            self.xmb_pictures.clear()
            self.xmb_view.sync()
            self.render_bar()
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
        self.render_backdrop()

    def render_bar(self) -> None:
        status = self.model.status()
        self.bar_clock.set_label(status.clock)
        self.bar_network.set_label(status.network)
        self.bar_battery.set_label(status.battery)
        self.bar_battery.set_visible(bool(status.battery))
        self.bar_update.set_label(f"UPDATE {status.update}" if status.update else "")
        self.bar_update.set_visible(bool(status.update))
        self.home_status.set_label(self.home_line())
        self.home_prompt.set_label(quick.prompt(self.family, quick.HOME_PROMPT))
        if self.cross:
            self.xmb_view.message = self.home_line()
            self.xmb_view.queue_draw()

    def home_line(self) -> str:
        """The home screen's status line: the last result, else, on a live stick, the installed
        system it can update (as the classic home's footer; the bar shows a newer release)."""
        if self.status or self.updates.installed:
            return self.status
        try:
            return update.disk_notice(self.updates.current)
        except Exception:  # noqa: BLE001 - a notice must never take the home screen down
            return ""

    def show(self, name: str) -> None:
        self.mode = name
        self.stack.set_visible_child_name(name)
        self.render_backdrop()

    def show_text(self, name: str, title: str, body: str, hint: str) -> None:
        _box, title_label, body_label, hint_label = self.pages[name]
        title_label.set_label(title)
        body_label.set_label(body)
        hint_label.set_label(hint)
        if self.mode != name:
            self.show(name)

    def active_rows(self) -> tuple[list[apps.Application], apps.Application | None, list[str]]:
        """ACTIVE APPLICATIONS as the classic Guide menu has it: the running apps, the CONTROLLER
        MOUSE switch for the app in front (the last one brought there), QUICK MENU, BACK TO HOME."""
        running = self.running_applications()
        modes = self.pointer_modes
        front = next((app for app in running if app.id == modes.front), running[0] if running else None)
        rows = [f"{app.name}  RUNNING" for app in running]
        if front is not None:
            rows.append(f"CONTROLLER MOUSE ({front.name})  {'ON' if modes.enabled(front) else 'OFF'}")
        return running, front, rows + ["QUICK MENU", "BACK TO HOME"]

    def render_active(self) -> None:
        clear(self.active_list)
        running, front, rows = self.active_rows()
        self.active_index = min(self.active_index, len(rows) - 1)
        for index, text in enumerate(rows):
            item = label(text, "tv-item", xalign=0.5, ellipsize=False)  # centred: its own width
            if index == self.active_index:
                item.add_css_class("tv-focused")
            self.active_list.append(item)
        if self.active_index < len(running):
            entries = quick.ACTIVE_PROMPT
        elif front is not None and self.active_index == len(running):
            entries = quick.MOUSE_PROMPT
        else:
            entries = quick.QUICK_PROMPT[:1] + quick.ACTIVE_PROMPT[-1:]
        self.active_hint.set_label(self.status or quick.prompt(self.family, entries))

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
                       "HOLD SELECT+START (VIEW+MENU) OR PRESS THE HOME KEY TO COME BACK HERE")

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

    def choose(self, title: str, body: str, choices: list[str]) -> int | None:
        """A message with buttons: LEFT / RIGHT or UP / DOWN picks one, A / Enter chooses it (its
        index); B / Esc and Home are None."""
        self.choices, self.choice = list(choices), 0
        try:
            answer = self.ask(title, body, self.choice_hint())
        finally:
            self.choices = []
        return self.choice if answer == "yes" else None

    def choice_hint(self) -> str:
        buttons = "   ".join(f"[ {text} ]" if index == self.choice else text for index, text in enumerate(self.choices))
        return f"{buttons}\n{CHOICE_HINT}"

    def show_launch_failure(
        self, label_text: str, message: str, app: apps.Application | None = None, *, retry: bool = True
    ) -> str:
        """The classic launcher's failure screen (couchliteos_errors): what went wrong, what to do,
        and its buttons. NETWORK SETTINGS, SAVE SUPPORT FILE and BLUETOOTH SETTINGS open the classic
        screen on top and come back here, as WAKE PC does, with TRY AGAIN one press away."""
        failure = errors.describe_failure(
            label_text, message, app_id=app.id if app else "", app_kind=app.kind if app else "",
            online=stream.link_up(), retry=retry,
        )
        if not controllers.bluetooth_present():
            failure = failure.without_bluetooth()
        actions = [action for action in failure.actions if action.id in FAILURE_SCREENS
                   or action.id in (errors.RETRY.id, errors.DISMISS.id, WAKE_PC.id)]
        while True:
            index = self.choose(failure.title, f"{failure.detail}\n\n{failure.hint}", [action.label for action in actions])
            choice = actions[index].id if index is not None else errors.DISMISS.id
            if choice in (errors.RETRY.id, errors.DISMISS.id):
                return choice
            if choice == WAKE_PC.id:
                self.wake_from_failure()
            else:
                self.wait_for_screen(FAILURE_SCREENS[choice])

    def wake_from_failure(self) -> None:
        """WAKE PC on a Moonlight failure: wake the default PC and say how it went."""
        try:
            host = stream.default_host(stream.load_hosts(), stream.load_settings())
        except (OSError, ValueError):
            host = None
        if host is None:
            self.ask("WAKE PC", "NO GAMING PC IS PAIRED YET.", BACK_HINT)
            return
        if not stream.link_up():
            result = "nonetwork"
        else:
            self.busy_pressed = False
            try:
                result = stream.wake_and_wait(
                    host, force=True, sleep=self.pump,
                    tick=lambda elapsed: self.wait_tick(
                        f"WAKING {host.label}...  {session.SPINNER[int(elapsed * 4) % len(session.SPINNER)]}",
                        f"{int(elapsed)} OF {int(stream.WAKE_TIMEOUT)} SECONDS"),
                )
            except OSError as error:
                self.ask("WAKE PC", f"WAKE FAILED: {error}".upper(), BACK_HINT)
                return
        self.ask("WAKE PC", stream.WAKE_RESULTS.get(result, "{}").format(host.label), BACK_HINT)

    def wait_for_screen(self, name: str) -> None:
        """Open a classic screen on top and wait for it to close (a failure screen's button)."""
        if not self.open_screen(name):
            return
        self.busy_depth += 1
        try:
            while self.child_pid is not None:
                self.pump(0.1)
        finally:
            self.busy_depth -= 1

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
        if not (self.cross and self.mode == "home" and not self.quick_open):
            self.ui_sound(quick.SOUND_KEYS.get(name, ""))  # the XMB plays its own: a category, an edge
        if self.mode not in ("message", "update") and (name == "home" or self.quick_open):
            self.quick_key_or_open(name)  # a question on screen keeps Home as its NO
            return True
        if self.status and self.mode == "home":
            self.status = ""  # a press dismisses the last result
            self.home_status.set_label(self.home_line())
            if self.cross:
                self.xmb_view.message = self.home_line()
        handler = {"home": self.xmb_key if self.cross else self.home_key, "active": self.active_key, "message": self.message_key,
                   "art": self.art_key, "streamset": self.stream_settings_key, **self.screen_keys()}.get(self.mode)
        if handler is not None:
            handler(name)
        return True

    def message_key(self, name: str) -> None:
        if self.choices and name in ("left", "right", "up", "down"):
            step = 1 if name in ("right", "down") else -1
            self.choice = max(0, min(len(self.choices) - 1, self.choice + step))
            self.pages["message"][3].set_label(self.choice_hint())
            return
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
        elif name == "hold-y":
            self.change_artwork()
        elif name == "hold-x":
            self.open_stream_settings()
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
        running, front, rows = self.active_rows()
        count = len(rows)
        quick_row = count - 2  # QUICK MENU, then BACK TO HOME
        if name in ("up", "down"):
            self.active_index = max(0, min(count - 1, self.active_index + (1 if name == "down" else -1)))
            self.status = ""
        elif name == "keyboard":
            # X / Triangle: type into the focused app, or the one in front from any other row.
            target = running[self.active_index] if self.active_index < len(running) else front
            if target is None:
                self.status = "NO APP IS RUNNING TO TYPE INTO"
            else:
                problem = self.type_into(target)
                if problem is None:
                    self.show("home")
                    self.render_home()
                    return
                self.status = problem
        elif front is not None and self.active_index == len(running) and name in ("activate", "left", "right"):
            on = self.pointer_modes.toggle(front)
            self.status = f"CONTROLLER MOUSE {'ON' if on else 'OFF'} FOR {front.name}"
        elif name == "activate" and self.active_index == quick_row:
            self.status = ""
            self.show("home")
            self.open_quick()
            return
        elif name in ("back", "home") or (name == "activate" and self.active_index > quick_row):
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
        self.xmb_last_played()
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
        self.music_holds()
        if self.busy_depth:
            self.idle.keep_awake()
            return
        if self.child_pid is not None or self.mode == "update":
            self.screens_tick()
            return
        self.relayout(self.screen_size())
        self.theme_tick()
        if self.take_resumed():
            self.music.release("sleep")
            self.idle.resumed()
            self.blank.set_visible(False)
            self.can_sleep = power.can_suspend()
            self.idle.apply(power.effective_settings(power.load_settings(), self.can_sleep, self.can_wake, self.battery.on_battery()))
            self.status = "RESUMED FROM SLEEP"
            session.focus_launcher()
            self.show("home")
            self.autostream()
        kind = quick.take_request(self.home_request)
        if kind is not None:
            self.idle.keep_awake()
            self.blank.set_visible(False)
            session.focus_launcher()
            self.home_request_arrived(kind)
        action = self.idle.tick()
        if action == power.BLANK:
            self.blank.set_visible(True)
        elif action == power.SLEEP and self.can_sleep:
            try:
                self.music.hold("sleep")
                self.request("suspend")  # couchliteos-suspend.path removes it before suspending
            except OSError:
                pass
        running = self.apps_running()
        now = time.monotonic()
        if (self.was_running and not running) or now - self.last_reload >= RELOAD_SECONDS:
            if self.was_running and not running:
                self.model.reload()
                self.model.focus_last_played()  # back from a stream: on the game just played
                self.xmb_last_played()
            elif self.mode == "home":
                self.model.reload()
            self.last_reload = now
            if self.mode == "home":
                self.render_home()
        self.was_running = running
        if self.cross and hasattr(self, "wave") and self.ready_written:
            self.wave.set_running(not running and not self.idle.blanked)
        self.tick_quick()
        if self.mode == "home":
            self.art_tick()
            self.xmb_tick()
            self.render_bar()
            if not self.quick_open:
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

    # ------------------------------------------------------------------ quick menu, toasts, sounds

    def init_quick(self) -> None:
        self.family = controls.detect_family()
        self.quick_open = False
        self.quick_front = ""  # the app Guide was tapped over: B / Esc goes back to it
        self.key_home_at = float("-inf")
        self.quick = quick.QuickMenu(
            quick.Sources.system(
                battery=self.controllers.line, network=home.link_status,
                running=lambda: len(self.running_applications()), can_sleep=lambda: self.can_sleep,
            ),
            extra=self.quick_stream_items,
        )
        self.toasts = quick.Toasts()
        self.pcstatus = pcstatus.Monitor()
        self.toast_feed = quick.ToastFeed(
            self.toasts, pads=lambda: controls.pad_names(controls.PROC_INPUT.read_text(errors="replace")),
            low=self.controllers.low, pc_line=self.pcstatus.line, update=self.updates.available,
        )
        self.sounds = quick.Sounds(muted=self.apps_running)
        self.quick_css = Gtk.CssProvider()

    def quick_stream_items(self) -> list[quick.Item]:
        """Rows at the top of the quick menu while Moonlight runs: the stream's preset and SHOW STATS."""
        if not (self.run_dir / "moonlight-ready").exists():
            return []
        return [
            quick.Item("preset", "STREAM PRESET", stream.active_preset() or stream.DEFAULT, selectable=False),
            # An action, not an ON / OFF row: the shortcut toggles Moonlight's overlay, and only
            # gamepad-nav knows whether it got to send it (the stream may end first).
            quick.Item("stats", "SHOW STATS", "", ("stats",)),
        ]

    def quick_extra_action(self, action: tuple) -> None:
        """SHOW STATS: back to Moonlight, where gamepad-nav types its statistics shortcut."""
        if action != ("stats",):
            return
        self.close_quick(resume=False)
        app = self.app_by_id("moonlight")
        if app is None or not self.focus_app(app):
            self.status = "MOONLIGHT HAS NO WINDOW: STATISTICS NOT SHOWN"
            self.show("home")
            self.render_home()
            return
        try:
            (self.run_dir / "moonlight-stats.request").touch()  # gamepad-nav sends Ctrl+Alt+Shift+S
        except OSError as error:
            display.log(f"tv stats request failed: {error!r}", session.LOG)

    def build_quick(self, overlay: "Gtk.Overlay") -> None:
        """The quick menu panel (right edge) and the toast card (top right), over every screen."""
        panel = self.quick_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        panel.add_css_class("tv-quick")
        panel.set_halign(Gtk.Align.END)
        panel.set_valign(Gtk.Align.FILL)
        panel.append(label("QUICK MENU", "tv-title"))
        self.quick_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.quick_rows.set_vexpand(True)
        panel.append(self.quick_rows)
        self.quick_prompt = label("", "tv-prompt", wrap=True)
        panel.append(self.quick_prompt)
        panel.set_visible(False)
        overlay.add_overlay(panel)
        toast = self.toast = label("", "tv-toast", ellipsize=False, wrap=True)
        toast.set_halign(Gtk.Align.END)
        toast.set_valign(Gtk.Align.START)
        toast.set_hexpand(False)
        toast.set_focusable(False)  # a toast never takes focus, and the pointer passes through it
        toast.set_can_target(False)
        toast.set_visible(False)
        overlay.add_overlay(toast)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.quick_css, Gtk.STYLE_PROVIDER_PRIORITY_USER,
        )

    def relayout_quick(self) -> None:
        layout = self.layout
        self.quick_width = max(1, round(layout.width * 0.34))
        self.quick_panel.set_size_request(self.quick_width, -1)
        self.quick_panel.set_spacing(layout.px(16))
        self.toast.set_margin_top(layout.margin_y)
        self.toast.set_max_width_chars(40)
        self.place_toast()

    def place_toast(self) -> None:
        """Top right, or left of the quick menu while it is open, so it never covers a row."""
        self.toast.set_margin_end(self.layout.margin_x + (self.quick_width if self.quick_open else 0))

    def render_quick(self) -> None:
        # The 1 s tick refreshes the values: rebuild the rows only when what they show changed.
        shown = (tuple((item.label, item.value, item.selectable) for item in self.quick.items),
                 self.quick.index, self.quick.prompt(self.family))
        if shown == getattr(self, "quick_shown", None) and self.quick_panel.get_visible():
            return
        self.quick_shown = shown
        clear(self.quick_rows)
        for index, item in enumerate(self.quick.items):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            row.add_css_class("tv-quick-row")
            if not item.selectable:
                row.add_css_class("tv-info")
            if index == self.quick.index:
                row.add_css_class("tv-focused")
            row.append(label(item.label))
            if item.value:
                value = label(item.value, xalign=1.0, ellipsize=False)
                value.set_hexpand(False)
                row.append(value)
            self.quick_rows.append(row)
        self.quick_prompt.set_label(self.quick.prompt(self.family))

    def open_quick(self) -> None:
        self.quick_front = quick.front_app_id(self.run_dir) if self.apps_running() else ""
        self.set_launcher_focus(True)  # gamepad-nav forwards the pad's keys to this menu over an app
        self.quick_open = True
        self.quick.open()
        self.render_quick()
        self.quick_panel.set_visible(True)
        self.place_toast()

    def close_quick(self, resume: bool = True) -> None:
        """Close the menu. `resume`: back to the app Guide was tapped over, if it still runs."""
        self.quick_open = False
        self.quick_panel.set_visible(False)
        self.place_toast()
        front, self.quick_front = self.quick_front, ""
        if resume and front:
            app = next((item for item in self.running_applications() if item.id == front), None)
            if app is not None and self.focus_app(app):
                return
        if self.mode == "home":
            self.render_home()

    def home_request_arrived(self, kind: str) -> None:
        """Guide / Home through gamepad-nav: a tap opens or closes the quick menu, a held shortcut goes Home."""
        if kind == quick.GUIDE and time.monotonic() - self.key_home_at < KEY_HOME_SECONDS:
            return  # the Home key this window already acted on
        target = quick.home_target(kind, self.quick_open, self.mode, bool(self.running_applications()))
        if target == "quick":
            self.open_quick()
        elif target == "close":
            self.close_quick()
        else:
            if self.quick_open:
                self.close_quick(resume=False)
            if target == "active":
                self.open_active()
            else:
                self.set_launcher_focus(True)
                self.show("home")
                self.render_home()

    def quick_key_or_open(self, name: str) -> None:
        if name == "home":  # the keyboard's Home key: a Guide tap
            self.key_home_at = time.monotonic()
            self.home_request.unlink(missing_ok=True)
            if self.quick_open:
                self.close_quick()
            else:
                self.open_quick()
            return
        menu = self.quick
        if name in ("up", "down"):
            menu.move(1 if name == "down" else -1)
        elif name in ("left", "right"):
            menu.adjust(1 if name == "right" else -1)
        elif name == "back":
            self.close_quick()
            return
        elif name == "activate":
            self.quick_action(menu.activate())
        if self.quick_open:
            self.render_quick()

    def quick_action(self, action: tuple) -> None:
        if not action:
            return
        if action == ("active",):
            self.close_quick(resume=False)
            self.open_active()
        elif action == ("home",):
            self.close_quick(resume=False)
            self.show("home")
            self.render_home()
        elif action[0] == "power":
            self.close_quick(resume=False)
            request = action[1]
            question = quick.power_question(request, self.can_wake)
            if question is not None:
                answer = self.ask(*question, tvlayout.QUESTION_HINT)
                self.show("home")
                if answer != "yes":
                    return
            try:
                self.request(request)  # couchliteos-<request>.path does the root work
            except OSError as error:
                self.status = f"COULD NOT ASK FOR {question[0] if question else 'SLEEP'}: {error}".upper()
                self.render_bar()
        else:
            self.quick_extra_action(action)

    def tick_quick(self) -> None:
        """Once a second: the pad family for the prompts, new toasts, and the open menu's values."""
        self.family = controls.detect_family()
        self.toast_feed.poll()
        if self.battery.take_source_change():  # mains <-> battery: the battery's screen-off times apply
            self.idle.apply(power.effective_settings(power.load_settings(), self.can_sleep, self.can_wake, self.battery.on_battery()))
        level = self.battery.take_warning()
        if level is not None:
            self.toasts.push(battery.warning_text(level))
        if self.quick_open:
            self.quick.refresh()
            self.render_quick()

    def render_toast(self) -> bool:
        try:
            text = self.toasts.current()
            if self.toast.get_label() != text:
                self.toast.set_label(text)
            self.toast.set_visible(bool(text))
        except Exception as error:  # noqa: BLE001 - toasts must never stop the home screen
            display.log(f"tv toast failed: {error!r}", session.LOG)
        return True  # keep the timeout

    # ------------------------------------------------------------------ artwork

    def art_worker(self) -> artwork.Worker:
        """The background cover lookup, made on first use."""
        worker = getattr(self, "_art_worker", None)
        if worker is None:
            worker = self._art_worker = artwork.Worker()
        return worker

    def cover(self, item: home.Tile) -> pathlib.Path | None:
        """A game tile's cover file (picked, Moonlight's, or looked up), None for the title card."""
        if not item.action or item.action[0] != "stream":
            return None
        _kind, host, app = item.action
        try:
            return self.art_worker().art(host.uuid or host.name, app, host.app_id(app), host.label)
        except Exception as error:  # noqa: BLE001 - no art is never a reason to lose the home screen
            display.log(f"tv artwork failed: {error!r}", session.LOG)
            return None

    def art_picture(self, path: pathlib.Path | None, width: int = -1, height: int = -1) -> "Gtk.Picture | None":
        """A picture of a cover file; textures are kept until the file changes."""
        if path is None:
            return None
        textures = self.__dict__.setdefault("_art_textures", {})
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return None
        texture = textures.get(str(path), (None, None))
        if texture[0] != stamp:
            try:
                texture = (stamp, Gdk.Texture.new_from_filename(str(path)))
            except GLib.Error:
                return None
            if len(textures) > 64:
                textures.clear()
            textures[str(path)] = texture
        picture = Gtk.Picture.new_for_paintable(texture[1])
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_can_shrink(True)
        picture.set_size_request(width, height)
        return picture

    def art_tick(self) -> None:
        if self.art_worker().take_changed():
            self.render_home()

    def change_artwork(self) -> None:
        """Hold Y on a game: up to 12 covers to pick from, RESET and TITLE CARD."""
        item = self.focused_item()
        if item is None or not item.action or item.action[0] != "stream":
            return
        _kind, host, app = item.action
        worker = self.art_worker()
        found: list[pathlib.Path] = []
        done = threading.Event()

        def look() -> None:
            try:
                if artwork.lookup_enabled():  # with LOOKUP OFF no name leaves the box
                    found.extend(worker.choices(app))
            except Exception:  # noqa: BLE001 - no covers still leaves RESET and TITLE CARD
                pass
            finally:
                done.set()

        threading.Thread(target=look, name="artwork-choices", daemon=True).start()
        self.busy_depth += 1
        self.busy_pressed = False
        try:
            while not done.is_set():
                if self.wait_tick(f"LOOKING FOR COVERS FOR {item.label}", "PLEASE WAIT"):
                    self.status = "CHANGE ARTWORK CANCELLED"
                    self.show("home")
                    self.render_home()
                    return
        finally:
            self.busy_depth -= 1
        self.art_choice = (host, app, item.label, [*found, None, artwork.TITLE_CARD])
        self.art_index = 0
        if not found:
            self.status = "NO COVERS FOUND" if artwork.lookup_enabled() else "ARTWORK LOOKUP IS OFF IN SETTINGS > APPEARANCE"
        self.render_art()
        self.show("art")

    def render_art(self) -> None:
        """CHANGE ARTWORK: covers in rows of ART_COLUMNS, then RESET and TITLE CARD."""
        if "art" not in self.pages:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.set_valign(Gtk.Align.CENTER)
            title = label("", "tv-title", xalign=0.5, wrap=True)
            grid = Gtk.Grid()
            grid.set_halign(Gtk.Align.CENTER)
            hint = label("", "tv-prompt", xalign=0.5, wrap=True)
            for widget in (title, grid, hint):
                box.append(widget)
            self.stack.add_named(box, "art")
            self.pages["art"] = (box, title, grid, hint)
        box, title, grid, hint = self.pages["art"]
        layout = self.layout
        box.set_margin_start(layout.margin_x)
        box.set_margin_end(layout.margin_x)
        box.set_margin_top(layout.margin_y)
        box.set_margin_bottom(layout.margin_y)
        box.set_spacing(layout.px(16))
        grid.set_row_spacing(layout.gap)
        grid.set_column_spacing(layout.gap)
        _host, _app, name, choices = self.art_choice
        title.set_label(f"CHANGE ARTWORK: {name}")
        hint.set_label(self.status or ART_HINT)
        while (child := grid.get_first_child()) is not None:
            grid.remove(child)
        width = max(1, min(layout.px(180), (layout.width - 2 * layout.margin_x) // ART_COLUMNS - layout.gap))
        covers = len(choices) - 2
        for index, choice in enumerate(choices):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            cell.add_css_class("tv-tile")
            if index == self.art_index:
                cell.add_css_class("tv-focused")
            picture = self.art_picture(choice, width, width * 3 // 2) if index < covers else None
            if picture is not None:
                cell.append(picture)
            else:
                text = "RESET" if choice is None else "TITLE CARD" if choice == artwork.TITLE_CARD else "NO PREVIEW"
                cell.set_size_request(width, -1)
                cell.append(label(text, "tv-name", xalign=0.5, ellipsize=False))
            if index < covers:
                grid.attach(cell, index % ART_COLUMNS, index // ART_COLUMNS, 1, 1)
            else:  # RESET and TITLE CARD share the last row
                grid.attach(cell, (index - covers) * 3, (covers + ART_COLUMNS - 1) // ART_COLUMNS, 3, 1)

    def art_key(self, name: str) -> None:
        host, app, _name, choices = self.art_choice
        last = len(choices) - 1
        if name in ("left", "right"):
            self.art_index = max(0, min(last, self.art_index + (1 if name == "right" else -1)))
        elif name in ("up", "down"):
            self.art_index = max(0, min(last, self.art_index + (ART_COLUMNS if name == "down" else -ART_COLUMNS)))
        elif name in ("back", "home"):
            self.status = ""
            self.show("home")
            self.render_home()
            return
        elif name == "activate":
            choice = choices[self.art_index]
            try:
                self.art_worker().pick(host.uuid or host.name, app, choice)
                self.status = {None: "ARTWORK RESET", artwork.TITLE_CARD: "TITLE CARD CHOSEN"}.get(choice, "ARTWORK CHANGED")
            except OSError as error:
                self.status = f"NOT SAVED: {error}".upper()
            self.show("home")
            self.render_home()
            return
        self.status = ""
        self.render_art()

    # ------------------------------------------------------------------ stream settings

    def open_stream_settings(self) -> None:
        """Hold X on a game: STREAM SETTINGS, a preset or CUSTOM kept for that game alone."""
        item = self.focused_item()
        if item is None or not item.action or item.action[0] != "stream":
            return
        _kind, host, app = item.action
        try:
            output = display.active_output(display.query_outputs())
        except Exception:  # noqa: BLE001 - without the mode the screen still saves; it only cannot preview
            output = None
        current = output.current_mode if output else None
        mode = (current.width, current.height, current.refresh_mhz) if current else None
        key = host.uuid or host.name  # recent.host_key: the key start_stream looks it up by
        self.stream_form = (key, app, item.label, mode, stream.video_decode(),
                            stream.SettingsForm(stream.game_settings(key, app)))
        self.status = ""
        self.render_stream_settings()
        self.show("streamset")

    def render_stream_settings(self) -> None:
        if "streamset" not in self.pages:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.set_valign(Gtk.Align.CENTER)
            title = label("", "tv-title", xalign=0.5, wrap=True)
            rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            rows.set_halign(Gtk.Align.CENTER)
            hint = label("", "tv-prompt", xalign=0.5, wrap=True)
            hint.set_justify(Gtk.Justification.CENTER)
            for widget in (title, rows, hint):
                box.append(widget)
            self.stack.add_named(box, "streamset")
            self.pages["streamset"] = (box, title, rows, hint)
        box, title, rows, hint = self.pages["streamset"]
        layout = self.layout
        for side in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{side}")(layout.margin_x if side in ("start", "end") else layout.margin_y)
        box.set_spacing(layout.px(16))
        _key, _app, name, mode, decode, form = self.stream_form
        title.set_label(f"STREAM SETTINGS: {name}")
        clear(rows)
        width = max(1, round(layout.width * 0.5))
        for index, (_row, text, value) in enumerate(form.rows()):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            row.add_css_class("tv-item")
            row.set_size_request(width, -1)
            if index == form.index:
                row.add_css_class("tv-focused")
            row.append(label(text))
            if value:
                row.append(label(f"‹ {value} ›" if index == form.index else value, xalign=1.0, ellipsize=False))
            rows.append(row)
        settings = form.settings()
        plan = stream.game_plan(settings, mode, decode) if settings is not None else None
        if settings is None:
            about = "USES THE SETTINGS FROM SETTINGS > STREAMING"
        elif plan is None:
            about = "NO DISPLAY MODE DETECTED: THE PRESET IS WORKED OUT WHEN THE GAME STARTS"
        else:
            about = f"ON THIS TV: {stream.describe(plan)}"
        hint.set_label(f"{about}\n{self.status or tvlayout.STREAM_SETTINGS_HINT}")

    def stream_settings_key(self, name: str) -> None:
        key, app, _name, _mode, _decode, form = self.stream_form
        if name in ("up", "down"):
            form.move(1 if name == "down" else -1)
        elif name in ("left", "right"):
            form.adjust(1 if name == "right" else -1)
        elif name in ("back", "home"):
            self.status = ""
            self.show("home")
            self.render_home()
            return
        elif name == "activate":
            if form.rows()[form.index][0] != "save":
                form.adjust(1)
            else:
                settings = form.settings()
                try:
                    stream.save_game(key, app, settings)
                    self.status = f"STREAM SETTINGS SAVED: {settings.preset if settings else stream.DEFAULT}"
                except (OSError, ValueError) as error:
                    self.status = f"NOT SAVED: {error}".upper()
                self.show("home")
                self.render_home()
                return
        self.status = ""
        self.render_stream_settings()

    # ------------------------------------------------------------------ the XMB

    def init_xmb(self) -> None:
        self.cross = theme.load_value("home") != ROWS
        self.motion_level = motion.level_for(theme.load_value("motion"), os.environ.get("GSK_RENDERER"))
        self.xmb = xmb.XmbModel(self.model, self.settings, power=self.power_choices)
        self.icons = icons.Icons()
        self.tiles = tile.Tiles()
        self.site_icons = siteicon.SiteIcons()
        self.xmb_pictures: dict[str, "gtk_xmb.Picture | None"] = {}
        self.wave_quarter = -1
        self.music = music.Music()

    # ------------------------------------------------------------------ sounds and music

    def ui_sound(self, name: str) -> None:
        """A UI sound; the music ducks under the bigger ones and comes back up."""
        if name and self.sounds.play(name):
            seconds = self.music.sound(name)
            if seconds is not None:
                GLib.timeout_add(round(seconds * 1000) + 10, self.music_tick)

    def music_tick(self) -> bool:
        self.music.tick()
        return False

    def music_holds(self) -> None:
        """The music fades out while an application or a stream is in front, a classic screen is
        open (some have their own test sounds), something starts (not a question), or the screen is blank."""
        try:
            running = self.apps_running()
        except Exception:  # noqa: BLE001
            running = True
        for reason, held in (("app", running), ("screen", self.child_pid is not None),
                             ("start", bool(self.busy_depth) and self.mode == "busy"), ("blank", self.idle.blanked)):
            (self.music.hold if held else self.music.release)(reason)

    def stop_music(self, _application=None) -> None:
        self.music.close()

    def power_choices(self) -> tvscreens.PowerModel:
        try:
            running = [app.name for app in self.running_applications()]
        except Exception:  # noqa: BLE001 - the power column must always be there
            running = []
        return tvscreens.PowerModel(self.can_sleep, self.can_wake, running)

    def focused_item(self):
        return self.xmb.focused() if self.cross else self.model.focused()

    def xmb_status(self) -> tuple[str, str]:
        """The clock, and under it the network, the battery and a newer release, as the bar has them."""
        status = self.model.status()
        line = "   ".join(part for part in (status.network, status.battery,
                                            f"UPDATE {status.update}" if status.update else "") if part)
        return status.clock, line

    def xmb_picture(self, item: "xmb.Item") -> "gtk_xmb.Picture | None":
        """What the XMB draws for an item: a game's cover, a site's or an application's own icon as a
        glossy tile (once made; the bundled icon until then), else None for the bundled icon."""
        if item.key not in self.xmb_pictures:
            self.xmb_pictures[item.key] = self.find_xmb_picture(item)
        return self.xmb_pictures[item.key]

    def find_xmb_picture(self, item: "xmb.Item") -> "gtk_xmb.Picture | None":
        try:
            if item.game is not None:
                cover = self.cover(item)
                return gtk_xmb.Picture(cover, gtk_xmb.COVER) if cover is not None else None
            if item.url:
                site = self.site_icons.icon(item.url)
                if site is not None:
                    return gtk_xmb.Picture(site, gtk_xmb.TILE)
            if item.app:
                path, official = self.icons.for_item(item.icon, self.app_by_id(item.app))
                made = self.tiles.tile(path) if official else None
                if made is not None:
                    return gtk_xmb.Picture(made, gtk_xmb.TILE)
        except Exception as error:  # noqa: BLE001 - the bundled icon is always there
            display.log(f"tv xmb picture {item.key}: {error!r}", session.LOG)
        return None

    def xmb_tick(self) -> None:
        """New covers, tiles or site icons arrived: draw them."""
        if self.cross and (self.tiles.take_changed() | self.site_icons.take_changed()):
            self.xmb_pictures.clear()
            self.xmb_view.queue_draw()

    def xmb_last_played(self) -> None:
        if self.cross:
            self.xmb.reload(reread_home=False)
            self.xmb.focus_last_played()

    def refresh_xmb_settings(self) -> None:
        """The SETTINGS column's value lines, read off the main loop (some ask other programs)."""
        def read() -> None:
            self.settings.refresh()
            GLib.idle_add(self.xmb_settings_ready)

        threading.Thread(target=read, daemon=True).start()

    def xmb_settings_ready(self) -> bool:
        self.xmb.settings_values(self.settings.values)
        self.xmb_view.queue_draw()
        return False

    def xmb_key(self, name: str) -> None:
        model = self.xmb
        if name in ("left", "right"):
            moved = model.move_h(1 if name == "right" else -1) == xmb.MOVED
            self.ui_sound("category" if moved else "edge")
        elif name in ("up", "down"):
            moved = model.move_v(1 if name == "down" else -1) == xmb.MOVED
            self.ui_sound("move" if moved else "edge")
        elif name == "back":
            if model.back() == xmb.MOVED:
                self.ui_sound("back")
        elif name == "activate":
            self.ui_sound("select")
            self.run_xmb_action(model.activate())
        else:
            self.home_key(name)  # Home, hold Y, hold X, the shortcut buttons
        if self.mode == "home":
            self.xmb_view.sync()

    def run_xmb_action(self, action: "xmb.Action | None") -> None:
        if not action:
            return
        if action[0] == "entry":
            _kind, kind, target = action
            if kind == tvscreens.SCREEN:
                self.open_screen(target)
            elif kind == tvscreens.APP:
                self.launch_by_id(target)
                self.after_launch()
            elif target == "active":
                self.open_active()
        elif action[0] == "updates":
            self.status = tvscreens.toggle_updates(self.updates)
            self.refresh_xmb_settings()
            self.render_bar()
        elif action[0] == "power":
            self.xmb_power(action[1])
        else:
            self.run_action(action)

    def xmb_power(self, request: str) -> None:
        """POWER's items ask first, as the POWER screen does."""
        self.can_sleep = power.can_suspend()
        choice = next((item for item in self.power_choices().choices if item.request == request), None)
        if choice is None:
            return
        if request == "suspend" and not self.can_sleep:
            self.status = "SLEEP IS NOT SUPPORTED ON THIS PC"
        elif self.ask(choice.label, choice.question, tvlayout.QUESTION_HINT) == "yes":
            try:
                self.request(request)  # couchliteos-<request>.path carries it out as root
                self.status = tvscreens.PowerModel.done(request)
            except OSError as error:
                self.status = f"COULD NOT ASK FOR {choice.label}: {error.strerror or 'ERROR'}".upper()
        self.show("home")
        self.render_home()

    # ------------------------------------------------------------------ start

    def when_painted(self, window, then) -> None:
        """Call `then` (when idle) once the window's next frame has been painted. A tick callback
        runs in the frame's UPDATE phase, before the renderer draws: the paint's own after-paint
        signal is the frame on screen."""
        def painted(clock) -> None:
            clock.disconnect(handler)
            GLib.idle_add(then)

        def first_tick(*_args) -> bool:
            nonlocal handler
            handler = window.get_frame_clock().connect("after-paint", painted)
            return False

        handler = 0
        window.add_tick_callback(first_tick)

    def watch_first_frame(self, window) -> None:
        # launcher-ready only once the home screen's first frame is painted: a GL driver that
        # crashes or hangs in that first paint must leave no mark (couchliteos-session then
        # retries with cairo).
        self.when_painted(window, self.first_frame_painted)

    def first_frame_painted(self) -> bool:
        if not self.ready_written:
            self.ready_written = True
            print(f"couchliteos-tv: home screen drawn {time.monotonic():.1f} s after boot", file=sys.stderr)
            try:
                (self.run_dir / "launcher-ready").touch()
            except OSError as error:
                print(f"couchliteos-tv: cannot write launcher-ready: {error}", file=sys.stderr)
            self.after_first_frame()
        return False

    def after_first_frame(self) -> bool:
        if display.restore_saved_mode() is None:
            self.status = "SAVED DISPLAY MODE SKIPPED — CHOOSE IT AGAIN IN SETTINGS > DISPLAY"
            self.render_bar()
        self.continue_start()
        if self.cross and hasattr(self, "wave"):
            self.wave.start()  # after the first frame: a GL driver that fails here leaves launcher-ready
            self.refresh_xmb_settings()
        self.ui_sound("startup")
        self.music_holds()
        self.music.start()
        self.hide_loading()
        return False

    def hide_loading(self) -> None:
        """Fade the boot picture away over the home screen; test steps start once it is gone."""
        loading, self.loading = self.loading, None
        if loading is None:
            self.start_script()
            return

        def gone() -> None:
            self.root.remove_overlay(loading)
            self.start_script()

        loading.fade_out(gone)

    def activate(self, _application) -> None:
        if self.window is not None:
            self.window.present()
            return
        self.prepare_session()
        tvscreens.take_switch_interface(self.run_dir)  # left by a classic launcher that switched to this one
        self.controllers.start()
        self.battery.start()
        self.updates.start()
        self.pcstatus.start()
        self.show_loading()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CouchLiteOS TV interface")
    parser.add_argument("--script", default="", help="test steps, comma-separated (Tv.script_step)")
    parser.add_argument("--dump-layout", metavar="DIR", help="where `dump:<name>` steps write <name>.json")
    parser.add_argument("--screenshots", metavar="DIR", help="where `dump:<name>` steps write <name>.png (grim)")
    args = parser.parse_args(argv)
    if Gtk is None:
        print(f"couchliteos-tv: GTK 4 is not available: {GI_ERROR}", file=sys.stderr)
        return INIT_FAILED
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        print("couchliteos-tv: GTK could not open the display", file=sys.stderr)
        return INIT_FAILED
    GLib.set_prgname("couchliteos-launcher")
    application = Gtk.Application(application_id=APPLICATION_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    tv = Tv(application)
    tv.script = [step.strip() for step in args.script.split(",") if step.strip()]
    tv.dump_dir = pathlib.Path(args.dump_layout) if args.dump_layout else None
    tv.shot_dir = pathlib.Path(args.screenshots) if args.screenshots else None
    application.connect("activate", tv.activate)
    application.connect("shutdown", tv.stop_music)
    status = application.run([sys.argv[0]])
    if tv.script_failed:
        return 1
    return status if tv.ready_written else INIT_FAILED  # never drew a frame: let the wrapper fall back


if __name__ == "__main__":
    sys.exit(main())
