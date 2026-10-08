"""The loading screen on screen (GTK 4): the black, the item's picture and name, the turning ring.

couchliteos-tv.py imports this once GTK is up and lays LoadingView over its screens (under the
quick menu, the toast and the blank screen). The timing is couchliteos_loading's, tested on its
own; here it is only drawn, and only while it moves: the tick callback goes once a frame holds
(plain black behind a game), and the widget hides itself, picture and all, once revealed.
"""

from __future__ import annotations

import math
import pathlib
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Graphene, Gtk, Pango  # noqa: E402

import couchliteos_gtk_xmb as gtk_xmb  # noqa: E402
import couchliteos_loading as loading  # noqa: E402
import couchliteos_motion as motion  # noqa: E402

# In screen heights (H), after the PS3 at 1920x1080.
PICTURE_Y = 0.42  # the picture's centre
ICON = 0.2  # a bundled icon's size
TILE = 0.24
COVER = 0.34  # a cover's height (a portrait, 2:3)
GLOW = 2.3  # the light behind the picture, against its size
TITLE = 0.045
DETAIL = 0.03
HINT = 0.027
RING = 0.032  # the ring's radius
DOT = 0.0065  # a dot's radius
EDGE_X, EDGE_Y = gtk_xmb.EDGE_X, gtk_xmb.EDGE_Y


def _spot(size: int, hard: bool) -> Gdk.Texture:
    """A white disc (`hard`, anti-aliased: a dot) or a soft white spot (the glow)."""
    data = bytearray()
    middle = (size - 1) / 2
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - middle, y - middle)
            if hard:
                alpha = min(1.0, max(0.0, middle - d + 0.5))
            else:
                alpha = max(0.0, 1 - d / middle) ** 2.2
            data += bytes((255, 255, 255, round(alpha * 255)))
    return Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(bytes(data)), size * 4)


def _rect(x: float, y: float, width: float, height: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, width, height)


def _rgba(grey: float, alpha: float) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    rgba.red = rgba.green = rgba.blue = grey
    rgba.alpha = alpha
    return rgba


class LoadingView(Gtk.Widget):
    """start(title, detail, picture, hint) while something starts; black() once it has the screen;
    reveal() when the TV is in front again. `picture` is a gtk_xmb.Picture (or None)."""

    __gtype_name__ = "CouchliteLoadingView"

    def __init__(self, level: str = motion.FULL) -> None:
        super().__init__()
        self.loading = loading.Loading(time.monotonic, level)
        self.title = self.detail = self.hint = ""
        self.picture: gtk_xmb.Picture | None = None
        self.texture: Gdk.Texture | None = None
        self.texture_key: tuple | None = None
        self.glow = _spot(64, hard=False)
        self.dot = _spot(32, hard=True)
        self.tick_id = 0
        self.drawn_at = -math.inf
        self.last: loading.Frame | None = None
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_can_target(False)
        self.set_visible(False)

    @property
    def shown(self) -> bool:
        return self.loading.shown

    def set_level(self, level: str) -> None:
        self.loading.level = level

    def start(self, title: str, detail: str, picture: gtk_xmb.Picture | None, hint: str) -> None:
        self.title, self.detail, self.picture, self.hint = title, detail, picture, hint
        self.loading.start()
        self.animate()

    def black(self) -> None:
        self.loading.black()
        self.animate()

    def reveal(self) -> None:
        self.loading.reveal()
        self.animate()

    def animate(self) -> None:
        self.set_visible(self.loading.shown)
        if self.loading.shown and not self.tick_id:
            self.tick_id = self.add_tick_callback(self.on_tick)
        self.queue_draw()

    def on_tick(self, _widget, frame_clock) -> bool:
        frame = self.loading.frame()
        if not frame.drawn:  # revealed: nothing left to draw, nothing kept
            self.tick_id = 0
            self.texture = self.texture_key = None
            self.set_visible(False)
            return False
        last = self.last
        fading = last is None or (frame.black, frame.content) != (last.black, last.content)
        if fading or (frame != last and frame_clock.get_frame_time() / 1_000_000 - self.drawn_at >= 0.9 / loading.FPS):
            self.queue_draw()  # every frame while it fades, FPS while only the ring turns
        if not frame.moving and frame == self.last:
            self.tick_id = 0  # it holds (plain black): no more frames
            return False
        return True

    # -------------------------------------------------------------- drawing

    def picture_texture(self, px: int) -> Gdk.Texture | None:
        picture = self.picture
        if picture is None:
            return None
        key = (str(picture.path), px)
        if key != self.texture_key:
            self.texture_key, self.texture = key, None
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(picture.path), px, px, True)
                self.texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            except GLib.Error:
                pass
        return self.texture

    def text(self, snapshot: Gtk.Snapshot, text: str, y: float, px: float, alpha: float, bold: bool,
             width: float) -> float:
        """`text` centred on the screen with its top at y; returns its height."""
        if not text or alpha <= 0.01:
            return 0.0
        layout = self.create_pango_layout(text)
        font = Pango.FontDescription.from_string(f"{gtk_xmb.FONT} {'Bold' if bold else 'Medium'}")
        font.set_absolute_size(max(1.0, px) * Pango.SCALE)
        layout.set_font_description(font)
        layout.set_width(round(width * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_alignment(Pango.Alignment.CENTER)
        snapshot.save()
        snapshot.translate(Graphene.Point().init((self.get_width() - width) / 2, y))
        snapshot.append_layout(layout, _rgba(1.0, alpha))
        snapshot.restore()
        return layout.get_pixel_size()[1]

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        frame = self.loading.frame()
        self.last = frame
        clock = self.get_frame_clock()
        self.drawn_at = clock.get_frame_time() / 1_000_000 if clock is not None else time.monotonic()
        if width <= 0 or height <= 0 or not frame.drawn:
            return
        snapshot.append_color(_rgba(0.0, frame.black), _rect(0, 0, width, height))
        alpha = frame.content
        if alpha <= 0.01:
            return
        # The picture, over a soft light, breathing a little.
        kind = self.picture.kind if self.picture is not None else gtk_xmb.ICON
        size = height * {gtk_xmb.COVER: COVER, gtk_xmb.TILE: TILE}.get(kind, ICON) * frame.swell
        texture = self.picture_texture(round(height * COVER if kind == gtk_xmb.COVER else height * TILE))
        cx, cy = width / 2, height * PICTURE_Y
        box_w, box_h = size, size
        if texture is not None:
            ratio = texture.get_width() / max(1, texture.get_height())
            box_w, box_h = (size * ratio, size) if ratio < 1 else (size, size / ratio)
        glow = max(box_w, box_h) * GLOW
        snapshot.push_opacity(0.22 * alpha)
        snapshot.append_texture(self.glow, _rect(cx - glow / 2, cy - glow / 2, glow, glow))
        snapshot.pop()
        if texture is not None:
            snapshot.push_opacity(alpha)
            snapshot.append_texture(texture, _rect(cx - box_w / 2, cy - box_h / 2, box_w, box_h))
            snapshot.pop()
        # The name, what it is, and how to come back.
        y = cy + max(box_h, height * ICON) / 2 + height * 0.05
        y += self.text(snapshot, self.title, y, height * TITLE, alpha, True, width * 0.8)
        self.text(snapshot, self.detail, y + height * 0.012, height * DETAIL, alpha * 0.7, False, width * 0.8)
        self.text(snapshot, self.hint, height * (1 - EDGE_Y - HINT * 1.3), height * HINT, alpha * 0.55, False,
                  width * (1 - 4 * EDGE_X) - height * RING * 4)
        # The ring, bottom right.
        rx = width * (1 - EDGE_X) - height * RING
        ry = height * (1 - EDGE_Y) - height * RING
        dot = height * DOT
        for index, bright in enumerate(frame.dots):
            angle = 2 * math.pi * index / loading.DOTS - math.pi / 2  # clockwise from the top
            x, y = rx + math.cos(angle) * height * RING, ry + math.sin(angle) * height * RING
            snapshot.push_opacity(alpha * bright)
            snapshot.append_texture(self.dot, _rect(x - dot, y - dot, dot * 2, dot * 2))
            snapshot.pop()
