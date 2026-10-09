"""The boot picture on screen (GTK 4): what couchliteos-tv shows until its home screen is ready.

Plymouth shows the CouchLiteOS logo while the system starts; couchliteos-tv shows the same
picture from its very first frame, builds the home screen behind it, and fades it out once the
home screen has been drawn. Where everything goes and how it moves is couchliteos_boot's
(GTK-free, tested); the pictures are the Plymouth theme's. Here they are only drawn.

Only MOTION FULL animates (cairo is never FULL): the light breathes, the ribbons drift and the
dots pulse, drawn at most DRAW_FPS times a second from a tick callback. Otherwise the picture
stands still and costs nothing. A picture that will not load is left out; with none at all the
gradient alone is still a calm screen to start on.
"""

from __future__ import annotations

import pathlib
import time
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gtk  # noqa: E402

import couchliteos_boot as boot  # noqa: E402
import couchliteos_motion as motion  # noqa: E402

DRAW_FPS = 30  # plenty for a slow drift, and half the work of the display's rate while starting
GRADIENT_ROWS = 512


def gradient_texture(rows: int = GRADIENT_ROWS) -> Gdk.Texture:
    """The background, top to bottom, two pixels wide: stretched over the screen like Plymouth's."""
    top, bottom = boot.rgb(boot.TOP), boot.rgb(boot.BOTTOM)
    data = bytearray()
    for row in range(rows):
        k = row / (rows - 1)
        pixel = bytes(round((a + (b - a) * k) * 255) for a, b in zip(top, bottom))
        data += pixel * 2
    return Gdk.MemoryTexture.new(2, rows, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(bytes(data)), 6)


def load_pictures(folder: pathlib.Path) -> dict[str, Gdk.Texture]:
    textures = {}
    for name in boot.FILES:
        try:
            textures[name] = Gdk.Texture.new_from_filename(str(folder / name))
        except GLib.Error:
            continue
    return textures


def _rect(x: float, y: float, width: float, height: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, width, height)


class LoadingScreen(Gtk.Widget):
    """The boot picture, filling its parent. fade_out() fades it away and then calls `done`."""

    __gtype_name__ = "CouchliteLoadingScreen"

    def __init__(self, level: str, folder: pathlib.Path = boot.ASSETS) -> None:
        super().__init__()
        self.level = level
        self.animate = boot.moving(level)
        self.gradient = gradient_texture()
        self.textures = load_pictures(folder)
        self.sizes = {name: (texture.get_width(), texture.get_height()) for name, texture in self.textures.items()}
        self.started: float | None = None  # time 0 of the motion, set at the first frame
        self.drawn_at = 0.0
        self.fading: motion.FrameFade | None = None
        self.done: Callable[[], None] | None = None
        self.tick_id = 0
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_can_target(False)
        if self.animate:
            self.tick_id = self.add_tick_callback(self.on_tick)

    def fade_out(self, done: Callable[[], None]) -> None:
        """Fade away over the home screen (at once with MOTION OFF), then call `done`."""
        seconds = boot.fade_seconds(self.level)
        if seconds <= 0:
            self.set_opacity(0.0)
            done()
            return
        self.done = done
        self.fading = motion.FrameFade(seconds)  # by drawn frames: the home screen's slow first frames cannot skip it
        if not self.tick_id:
            self.tick_id = self.add_tick_callback(self.on_tick)

    def on_tick(self, _widget, _clock) -> bool:
        now = time.monotonic()
        if self.fading is not None:
            self.set_opacity(1.0 - motion.ease_in_out_cubic(self.fading.advance(now)))
            if self.fading.done:
                self.tick_id = 0
                done, self.done, self.fading = self.done, None, None
                if done is not None:
                    done()
                return False
        if self.animate and now - self.drawn_at >= 1 / DRAW_FPS - 0.004:
            self.queue_draw()
        return True

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        now = self.drawn_at = time.monotonic()
        if self.started is None:
            # Time 0 is Plymouth's (couchliteos_boot.splash_seconds): the motion goes on from
            # where the splash had it, it does not start again.
            self.started = now - boot.splash_seconds()
        snapshot.append_texture(self.gradient, _rect(0, 0, width, height))
        for sprite in boot.frame(width, height, self.sizes, now - self.started, self.animate):
            if sprite.opacity <= 0:
                continue
            faded = sprite.opacity < 1
            if faded:
                snapshot.push_opacity(sprite.opacity)
            snapshot.append_texture(self.textures[sprite.name], _rect(sprite.x, sprite.y, sprite.width, sprite.height))
            if faded:
                snapshot.pop()
