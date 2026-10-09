"""The controller keyboard drawn in GTK 4, in the TV interface's colours.

Started by `couchliteos-osk --gtk` from couchliteos-osk-session. The window's app-id is
couchliteos-osk, so Cage docks it along the bottom 40% of the screen like the terminal keyboard
it replaces. The keys, the text and what is done with it are couchliteos_osk's; this file only
draws them. Anything going wrong exits GTK_UNAVAILABLE, and the session opens the terminal
keyboard instead: the controller is grabbed while the keyboard is up, so it must never hang.
"""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

import couchliteos_osk as osk  # noqa: E402

try:
    import couchliteos_theme as theme
except Exception:  # the keyboard works in its own colours
    theme = None

try:
    import couchliteos_renderer as renderer
except Exception:
    renderer = None

PANEL_SHARE = 0.40  # CAGE_OSK_PANEL_SHARE in config/cage/cage-0.2.0-osk-panel.patch


def colours() -> dict[str, str]:
    chosen = theme.chosen() if theme is not None else None
    return dict(chosen.colours) if chosen is not None else dict(osk.FALLBACK_COLOURS)


def panel_height() -> int:
    try:
        monitors = Gdk.Display.get_default().get_monitors()
        return max(240, round(monitors.get_item(0).get_geometry().height * PANEL_SHARE))
    except Exception:
        return 432  # 40% of 1080


class KeyboardWindow:
    def __init__(self, loop: GLib.MainLoop) -> None:
        self.loop = loop
        self.status = 0
        self.keyboard = osk.Keyboard()
        self.keyboard.masked = osk.consume_mask_request()
        self.shown_rows: tuple = ()
        self.cells: list[list[Gtk.Label]] = []

        window = self.window = Gtk.Window(title=osk.TITLE)
        window.set_decorated(False)
        window.add_css_class("osk-window")
        provider = Gtk.CssProvider()
        provider.load_from_data(osk.stylesheet(colours(), panel_height()).encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        panel.add_css_class("osk-panel")
        panel.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=osk.TITLE)
        title.add_css_class("osk-title")
        panel.append(title)
        self.text = Gtk.Label()
        self.text.add_css_class("osk-text")
        self.text.set_ellipsize(Pango.EllipsizeMode.START)  # the end being typed stays in view
        self.text.set_max_width_chars(64)
        self.text.set_halign(Gtk.Align.CENTER)
        panel.append(self.text)
        self.keys = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.keys.set_halign(Gtk.Align.CENTER)
        panel.append(self.keys)
        hint = Gtk.Label(label=osk.HINT)
        hint.add_css_class("osk-hint")
        panel.append(hint)
        window.set_child(panel)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        window.add_controller(keys)
        window.connect("close-request", self.on_close)
        self.render()
        window.present()

    def build_rows(self) -> None:
        child = self.keys.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.keys.remove(child)
            child = following
        self.cells = []
        rows = self.keyboard.rows
        for index, row in enumerate(rows):
            line = Gtk.Box(spacing=6)
            line.set_halign(Gtk.Align.CENTER)
            if index == len(rows) - len(osk.ACTION_ROWS):
                line.set_margin_top(6)
            cells = []
            for key in row:
                cell = Gtk.Label(label=key)
                cell.add_css_class("osk-key")
                if len(key) > 1:
                    cell.add_css_class("osk-action")
                line.append(cell)
                cells.append(cell)
            self.keys.append(line)
            self.cells.append(cells)
        self.shown_rows = rows

    def render(self) -> None:
        keyboard = self.keyboard
        if keyboard.rows != self.shown_rows:
            self.build_rows()  # SHIFT or SYMBOLS changed the keys; otherwise only classes change
        for row_index, cells in enumerate(self.cells):
            for column, cell in enumerate(cells):
                focused = (row_index, column) == (keyboard.row, keyboard.column)
                (cell.add_css_class if focused else cell.remove_css_class)("osk-focused")
                key = cell.get_label()
                on = key == "SHIFT" and keyboard.shift or key == "SYMBOLS" and keyboard.symbols \
                    or key == "MASK/SHOW" and keyboard.masked
                (cell.add_css_class if on else cell.remove_css_class)("osk-on")
        self.text.set_label(osk.shown_text(keyboard))

    def on_key(self, _controller, keyval: int, _keycode: int, state) -> bool:
        try:
            if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
                return False
            name = Gdk.keyval_name(keyval) or ""
            code = Gdk.keyval_to_unicode(keyval)
            action = osk.handle(self.keyboard, name, chr(code) if code else "")
            if action in ("type", "enter"):
                osk.atomic_payload(self.keyboard.text, action == "enter")
                self.finish(0)
            elif action == "cancel":
                self.finish(0)
            else:
                self.render()
        except Exception as error:
            print(f"couchliteos-osk: GTK keyboard failed: {error!r}", file=sys.stderr)
            self.finish(osk.GTK_UNAVAILABLE)
        return True

    def press(self, name: str, char: str = "") -> bool:
        """A key by Gdk name, as tests/tv-headless.sh sends them (COUCHLITEOS_OSK_KEYS)."""
        keyval = Gdk.keyval_from_name(name)
        self.on_key(None, keyval, 0, Gdk.ModifierType(0))
        return GLib.SOURCE_REMOVE

    def on_close(self, _window) -> bool:
        self.finish(self.status)
        return False

    def finish(self, status: int) -> None:
        self.status = status
        self.loop.quit()


def run() -> int:
    if renderer is not None:
        try:
            renderer.prepare(os.environ)  # the TV's choice: OpenGL where Vulkan is slow or missing
        except Exception:
            pass
    GLib.set_prgname(osk.APP_ID)  # becomes the Wayland app-id Cage docks
    GLib.set_application_name(osk.TITLE)
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        return osk.GTK_UNAVAILABLE
    loop = GLib.MainLoop()
    try:
        window = KeyboardWindow(loop)
    except Exception as error:
        print(f"couchliteos-osk: GTK keyboard failed: {error!r}", file=sys.stderr)
        return osk.GTK_UNAVAILABLE
    keys = [key for key in os.environ.get("COUCHLITEOS_OSK_KEYS", "").split(",") if key]
    for index, key in enumerate(keys):  # the last one late enough for a screenshot before it
        GLib.timeout_add(4000 if index == len(keys) - 1 else 1000 + 150 * index, window.press, key)
    loop.run()
    window.window.destroy()
    return window.status
