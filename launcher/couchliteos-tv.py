#!/usr/bin/python3
"""The TV interface: the GTK 4 home screen CouchLiteOS shows inside Cage.

One full-screen window holding a stack of screens: HOME (top bar, GAMES, APPS and SYSTEM
rows, prompt bar), STARTING / WAITING, a message screen (a failure, a question, a screen
that is not in the TV interface yet) and ACTIVE APPLICATIONS (a held Home shortcut with apps
running). Over them: the quick menu (a Guide tap), toasts and the blank screen. The rows
come from couchliteos_home.HomeModel; starting, resuming and closing apps is
couchliteos_session's, the same code the classic launcher runs. Sizes, colours, keys and
blanking are couchliteos_tvlayout's; the quick menu, prompt bar texts, toasts and sounds
are couchliteos_quick's.

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
import couchliteos_controls as controls
import couchliteos_display as display
import couchliteos_home as home
import couchliteos_pcstatus as pcstatus
import couchliteos_power as power
import couchliteos_quick as quick
import couchliteos_session as session
import couchliteos_stream as stream
import couchliteos_theme as theme
import couchliteos_tvlayout as tvlayout
import couchliteos_update as update

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
# Screens the TV interface does not draw yet (Settings and power come with the next screens).
NOT_YET = {
    "settings": ("SETTINGS", "SETTINGS IS NOT IN THE TV INTERFACE YET."),
    "hosts": ("HOSTS", "PAIRING AND MANAGING GAMING PCS IS NOT IN THE TV INTERFACE YET."),
    "power": ("POWER", "THE POWER MENU IS NOT IN THE TV INTERFACE YET."),
    "software-update": ("SOFTWARE UPDATE", "SOFTWARE UPDATE IS NOT IN THE TV INTERFACE YET."),
}
BACK_HINT = "B / CIRCLE OR ESC GOES BACK"
TOAST_TICK_MS = 250
KEY_HOME_SECONDS = 1.0  # the Home key reaches the window and gamepad-nav: one press, not two


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


class Tv(session.Session):
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
        self.init_quick()

    # ------------------------------------------------------------------ building

    def build(self) -> None:
        window = self.window = Gtk.ApplicationWindow(application=self.application, title=TITLE)
        window.add_css_class("tv-root")
        overlay = Gtk.Overlay()
        window.set_child(overlay)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        overlay.set_child(self.stack)
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
        self.css.load_from_data(tvlayout.stylesheet(current_theme(), self.layout).encode(), -1)
        self.relayout_quick()
        layout = self.layout
        for page in (self.home_page, self.active_page, *(entry[0] for entry in self.pages.values())):
            page.set_margin_start(layout.margin_x)
            page.set_margin_end(layout.margin_x)
            page.set_margin_top(layout.margin_y)
            page.set_margin_bottom(layout.margin_y)
            page.set_spacing(layout.px(16))
        self.bar.set_spacing(layout.px(40))
        for _title, tiles in self.row_boxes:
            tiles.set_spacing(layout.gap)
            tiles.set_size_request(-1, -1)
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
        self.home_prompt.set_label(quick.prompt(self.family, quick.HOME_PROMPT))

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
        rows = [f"{app.name}  RUNNING" for app in running] + ["QUICK MENU", "BACK TO HOME"]
        self.active_index = min(self.active_index, len(rows) - 1)
        for index, text in enumerate(rows):
            item = label(text, "tv-item", xalign=0.5, ellipsize=False)  # centred: its own width
            if index == self.active_index:
                item.add_css_class("tv-focused")
            self.active_list.append(item)
        on_app = self.active_index < len(running)
        self.active_hint.set_label(self.status or quick.prompt(
            self.family, quick.ACTIVE_PROMPT if on_app else quick.QUICK_PROMPT[:1] + quick.ACTIVE_PROMPT[-1:]))

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

    def show_launch_failure(
        self, label_text: str, message: str, app: apps.Application | None = None, *, retry: bool = True
    ) -> str:
        answer = self.ask(f"{label_text} DID NOT START", message.upper(),
                          tvlayout.FAILURE_HINT if retry else BACK_HINT)
        return "retry" if answer == "yes" and retry else "dismiss"

    def prepare_remote_desktop(self, app: apps.Application) -> bool:
        # The certificate check and the password prompt are curses screens for now.
        self.status = f"{app.name}: OPEN REMOTE DESKTOP FROM SETTINGS > REMOTE DESKTOP"
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
        if self.busy_depth and self.mode != "message":
            self.busy_pressed = True
            return True
        self.sounds.for_key(name)
        if self.mode != "message" and (name == "home" or self.quick_open):
            self.quick_key_or_open(name)  # a question on screen keeps Home as its NO
            return True
        if self.status and self.mode == "home":
            self.status = ""  # a press dismisses the last result
            self.home_status.set_label("")
        handler = {"home": self.home_key, "active": self.active_key, "message": self.message_key}.get(self.mode)
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
        count = len(running) + 2  # QUICK MENU, BACK TO HOME
        if name in ("up", "down"):
            self.active_index = max(0, min(count - 1, self.active_index + (1 if name == "down" else -1)))
            self.status = ""
        elif name == "activate" and self.active_index == len(running):
            self.status = ""
            self.show("home")
            self.open_quick()
            return
        elif name in ("back", "home") or (name == "activate" and self.active_index > len(running)):
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
        """A screen of its own (Settings, Hosts, Power). Those not in the TV interface yet say so."""
        title, body = NOT_YET.get(name, (name.upper(), ""))
        self.show_text("message", title, body, BACK_HINT)

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
        self.tick_quick()
        if self.mode == "home":
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
        """Rows at the top of the quick menu while a stream runs (R1: the preset and SHOW STATS)."""
        return []

    def quick_extra_action(self, action: tuple) -> None:
        """A quick_stream_items row was chosen (R1)."""

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
        self.quick_css.load_from_data(quick.stylesheet(current_theme(), layout).encode(), -1)
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
            question = quick.POWER_QUESTIONS.get(request)
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
        self.autostream()
        return False

    def activate(self, _application) -> None:
        if self.window is not None:
            self.window.present()
            return
        self.prepare_session()
        self.controllers.start()
        self.updates.start()
        self.pcstatus.start()
        self.build()
        GLib.timeout_add_seconds(TICK_SECONDS, self.tick)
        GLib.timeout_add(TOAST_TICK_MS, self.render_toast)


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
