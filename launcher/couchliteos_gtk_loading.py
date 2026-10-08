"""The busy screen's loading ring on screen (GTK 4). couchliteos_loading says where each dot is
and how bright; here they are only drawn.

couchliteos-tv.py puts the ring above the busy screen's title (STARTING <APP>, WAKING <PC>, the
auto-stream count-down). Its tick callback runs only while the ring is mapped, so it costs
nothing while another screen is up.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gtk  # noqa: E402

import couchliteos_loading as loading  # noqa: E402

DISC_PX = 64  # the dot's texture, drawn smaller


def disc_texture(colour: str, size: int = DISC_PX) -> Gdk.Texture:
    """A round dot in `colour` ("rrggbb"), its edge smoothed over a pixel."""
    red, green, blue = (int(colour[at:at + 2], 16) for at in (0, 2, 4))
    middle, edge = (size - 1) / 2, size / 2 - 1
    data = bytearray()
    for y in range(size):
        for x in range(size):
            alpha = min(1.0, max(0.0, edge - math.hypot(x - middle, y - middle) + 0.5))
            data += bytes((red, green, blue, round(alpha * 255)))
    return Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(bytes(data)), size * 4)


class LoadingRing(Gtk.Widget):
    """The ring, as big as its size request. `colour()` is the theme's accent ("rrggbb"), read at
    every frame so a theme change shows at once; `level` the MOTION level."""

    __gtype_name__ = "CouchliteLoadingRing"

    def __init__(self, colour: Callable[[], str], level: str) -> None:
        super().__init__()
        self.colour = colour
        self.level = level
        self.texture: Gdk.Texture | None = None
        self.texture_colour = ""
        self.started: float | None = None  # the frame clock's time of the first frame shown
        self.elapsed = 0.0
        self.shown: float | None = None  # loading.frame() of the frame on screen
        self.tick_id = 0
        self.set_can_target(False)
        self.connect("map", self.on_map)
        self.connect("unmap", self.on_unmap)

    def on_map(self, _widget) -> None:
        """Every busy screen starts the ring from the top, fading in."""
        self.started, self.elapsed, self.shown = None, 0.0, None
        if not self.tick_id:
            self.tick_id = self.add_tick_callback(self.on_tick)

    def on_unmap(self, _widget) -> None:
        if self.tick_id:
            self.remove_tick_callback(self.tick_id)
            self.tick_id = 0

    def on_tick(self, _widget, clock) -> bool:
        now = clock.get_frame_time() / 1_000_000
        if self.started is None:
            self.started = now
        self.elapsed = now - self.started
        frame = loading.frame(self.elapsed, self.level)
        if frame != self.shown:  # MOTION OFF: twice a second, not every frame
            self.shown = frame
            self.queue_draw()
        return True

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        colour = self.colour()
        if self.texture is None or colour != self.texture_colour:
            self.texture, self.texture_colour = disc_texture(colour), colour
        fade = loading.appear(self.elapsed, self.level)
        # The biggest dot (the head) must fit inside the widget.
        radius = min(width, height) / 2 / (1 + loading.DOT * (1 + loading.HEAD_GROWTH))
        for dot in loading.ring(self.elapsed, self.level):
            size = dot.radius * radius
            x, y = width / 2 + dot.x * radius, height / 2 + dot.y * radius
            snapshot.push_opacity(dot.alpha * fade)
            snapshot.append_texture(self.texture, Graphene.Rect().init(x - size, y - size, 2 * size, 2 * size))
            snapshot.pop()
