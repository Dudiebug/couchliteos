"""The classic screens drawn with the TV interface's own widgets (GTK 4).

couchliteos_uibridge runs a screen's curses code in its own process and sends what it draws as
frames; ScreenChild starts that process and passes frames and keys, ScreenPage draws a frame's
View: the title, a panel with the lists (the focused row as the TV interface's other lists show
it, a row's value on its right), the text and progress, and the hint under the panel.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

import couchliteos_uibridge as bridge  # noqa: E402


def _label(text: str, *classes: str, xalign: float = 0.0, wrap: bool = True) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign)
    for name in classes:
        widget.add_css_class(name)
    if wrap:
        widget.set_wrap(True)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if xalign == 0.5:
        widget.set_justify(Gtk.Justification.CENTER)
    return widget


def _clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


class ScreenChild:
    """One classic screen's process: frames come in, keys go out; `on_exit(code)` when it ends."""

    def __init__(self, command: list[str], on_frame: Callable[[dict], None], on_exit: Callable[[int], None]) -> None:
        keys_read, self.keys_write = os.pipe()
        self.frames_read, frames_write = os.pipe()
        env = dict(os.environ, **{bridge.ENV: f"{keys_read},{frames_write}"})
        try:
            self.process = subprocess.Popen(command, pass_fds=(keys_read, frames_write), env=env, close_fds=True)
        finally:
            os.close(keys_read)
            os.close(frames_write)
        self.pid = self.process.pid
        self.on_frame, self.on_exit = on_frame, on_exit
        self.buffer = b""
        self.latest: dict | None = None
        self.pending_draw = False
        os.set_blocking(self.frames_read, False)
        self.watch = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, self.frames_read, GLib.IOCondition.IN | GLib.IOCondition.HUP,
                                           self._readable)
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.pid, self._exited)

    def _readable(self, _fd: int, _condition) -> bool:
        try:
            chunk = os.read(self.frames_read, 1 << 16)
        except BlockingIOError:
            return True
        except OSError:
            chunk = b""
        if not chunk:
            self.watch = 0
            return False
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                self.latest = json.loads(line)
            except ValueError:
                continue
        if self.latest is not None and not self.pending_draw:
            self.pending_draw = True  # only the newest frame is drawn, once the loop is idle
            GLib.idle_add(self._draw)
        return True

    def _draw(self) -> bool:
        self.pending_draw = False
        if self.latest is not None:
            self.on_frame(self.latest)
        return False

    def send(self, key: int | str) -> None:
        try:
            os.write(self.keys_write, bridge.encode_key(key).encode())
        except OSError:
            pass  # it is closing: on_exit follows

    def _exited(self, pid: int, wait_status: int) -> None:
        GLib.spawn_close_pid(pid)
        self.process.returncode = 0  # reaped here, not by Popen
        if self.watch:
            GLib.source_remove(self.watch)
            self.watch = 0
        for fd in (self.keys_write, self.frames_read):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            code = os.waitstatus_to_exitcode(wait_status)
        except ValueError:
            code = 1
        self.on_exit(code)


class ScreenPage(Gtk.Box):
    """A View: the title, a panel (lists, text, progress) and the hint."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_valign(Gtk.Align.FILL)
        self.title = _label("", "tv-title", xalign=0.5)
        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.panel.add_css_class("tv-pane")
        self.panel.add_css_class("tv-screen")
        self.panel.set_halign(Gtk.Align.CENTER)
        self.panel.set_valign(Gtk.Align.START)
        self.hint = _label("", "tv-prompt", xalign=0.5)
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        for widget in (self.title, self.panel, spacer, self.hint):
            self.append(widget)
        self.width = 900
        self.slots = 9
        self.px: Callable[[int], int] = lambda value: value
        self.height = 1080
        self.view: bridge.View | None = None
        self.qr: tuple[str, GdkPixbuf.Pixbuf | None] = ("", None)

    def relayout(self, layout, slots: int) -> None:
        """Sizes from the TV interface's Layout (the page's margins are set with the other pages')."""
        self.panel.set_spacing(layout.px(14))
        self.width = max(320, min(round(layout.width * 0.62), layout.width - 2 * layout.margin_x))
        self.panel.set_size_request(self.width, -1)
        self.px, self.slots = layout.px, max(3, slots)
        self.height = layout.height
        if self.view is not None:
            self.show_view(self.view)

    def show_view(self, view: bridge.View) -> None:
        self.view = view
        self.title.set_label(view.title)
        self.title.set_visible(bool(view.title))
        _clear(self.panel)
        lists = sum(isinstance(block, bridge.ListBlock) for block in view.blocks)
        for block in view.blocks:
            if isinstance(block, bridge.ListBlock):
                self.panel.append(self._list(block, self.slots if lists == 1 else max(3, self.slots // lists)))
            elif isinstance(block, bridge.ProgressBlock):
                self.panel.append(self._progress(block))
            elif isinstance(block, bridge.QrBlock):
                if (widget := self._qr(block)) is not None:
                    self.panel.append(widget)
            else:
                self.panel.append(self._text(block))
        self.panel.set_visible(bool(view.blocks))
        self.hint.set_label("\n".join(view.hint))
        self.hint.set_visible(bool(view.hint))

    def _list(self, block: bridge.ListBlock, slots: int) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("tv-list")
        shown = bridge.visible(len(block.rows), block.selected, slots)
        if shown.start > 0:
            box.append(_label("▲", "tv-prompt", xalign=0.5, wrap=False))
        for index in shown:
            row = block.rows[index]
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            line.set_spacing(self.px(24))
            line.add_css_class("tv-item")
            if index == block.selected:
                line.add_css_class("tv-focused")
            name = _label(row.label, xalign=0.0)
            name.set_hexpand(True)
            line.append(name)
            if row.value:
                value = _label(row.value, "tv-row-value", xalign=1.0)
                value.set_max_width_chars(28)
                line.append(value)
            box.append(line)
        if shown.stop < len(block.rows):
            box.append(_label("▼", "tv-prompt", xalign=0.5, wrap=False))
        return box

    def _text(self, block: bridge.TextBlock) -> Gtk.Widget:
        # Wrapped lines of one paragraph are one label: the screen wrapped them for a terminal.
        lines = block.lines
        paragraph = all(not line.startswith(("- ", "* ", "> ")) for line in lines) and not any("   " in line for line in lines)
        text = " ".join(lines) if paragraph and block.centered else "\n".join(lines)
        widget = _label(text, "tv-body", xalign=0.5 if block.centered else 0.0)
        if block.strong:
            widget.add_css_class("tv-strong")
        return widget

    def _qr(self, block: bridge.QrBlock) -> Gtk.Widget | None:
        """The QR code as a picture, dark on white with its quiet zone, a third of the screen high
        at most so the page fits at any size. Made once per URL (the screen redraws every poll)."""
        if self.qr[0] != block.url:
            self.qr = (block.url, qr_modules(block.url))
        modules = self.qr[1]
        if modules is None:
            return None
        side = max(160, min(round(self.height * 0.33), self.px(420)))
        # Whole pixels per module, scaled without smoothing: every module stays a sharp square.
        size = max(1, side // modules.get_width()) * modules.get_width()
        scaled = modules.scale_simple(size, size, GdkPixbuf.InterpType.NEAREST)
        picture = Gtk.Picture.new_for_paintable(Gdk.Texture.new_for_pixbuf(scaled))
        picture.set_can_shrink(False)
        picture.set_halign(Gtk.Align.CENTER)
        return picture

    def _progress(self, block: bridge.ProgressBlock) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_spacing(self.px(10))
        bar = Gtk.ProgressBar()
        bar.set_fraction(block.fraction)
        bar.set_size_request(-1, self.px(24))
        box.append(bar)
        if block.text:
            box.append(_label(block.text, "tv-body", xalign=0.5))
        return box


def qr_modules(url: str) -> GdkPixbuf.Pixbuf | None:
    """qrencode's PNG of `url`, one pixel per module with a 2-module quiet zone, or None."""
    try:
        result = subprocess.run(["qrencode", "-t", "PNG", "-s", "1", "-m", "2", "-o", "-", url],
                                capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(result.stdout)
        loader.close()
    except GLib.Error:
        return None
    return loader.get_pixbuf()


def stylesheet(colours, layout) -> str:
    """CSS for ScreenPage, added to the TV interface's own (a theme.Theme, a tvlayout.Layout)."""
    c = {field: f"#{value}" for field, value in colours.colours.items()}
    px, body = layout.px, layout.fonts["body"]
    return f"""
.tv-screen {{ padding: {px(32)}px {px(40)}px; }}
.tv-screen .tv-row-value {{ font-size: {body}px; color: {c['accent']}; }}
.tv-screen .tv-focused .tv-row-value {{ color: inherit; }}
.tv-screen .tv-strong {{ font-weight: bold; }}
"""
