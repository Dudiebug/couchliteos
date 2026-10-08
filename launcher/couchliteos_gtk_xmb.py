"""The XMB on screen (GTK 4): the wave behind every screen and the cross of icons over it.

couchliteos-tv.py imports this once GTK is up. The models are GTK-free and tested on their
own: the cross is couchliteos_xmb.XmbModel, the motion couchliteos_motion, the wave's colours,
shader and still frame couchliteos_wave. Here they are only drawn.

The wave (make_wave): a Gtk.GLArea running couchliteos_wave's shader at up to wave.FPS, over the
still frame, which is all there is with the software renderer, MOTION OFF, or when the GL wave
failed before: RUN/wave-starting is written before the first GL frame and removed after it, so
a driver that dies drawing it leaves the mark and the next start draws the still frame. The
GL calls go through libepoxy (GTK's own GL loader) with ctypes: no extra package.

The cross (XmbView): one widget that draws everything itself in do_snapshot, so nothing is
rebuilt when the focus moves; positions come from Animator values, the tick callback runs
only while something moves. Sizes are fractions of the screen, after the PS3 at 1920x1080.
"""

from __future__ import annotations

import ctypes
import dataclasses
import math
import pathlib
import time
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Graphene, Gtk, Pango  # noqa: E402

import couchliteos_motion as motion  # noqa: E402
import couchliteos_wave as wave  # noqa: E402
import couchliteos_xmb as xmb  # noqa: E402

WAVE_MARK = "wave-starting"
FONT = "Inter, DejaVu Sans, Sans"

# The cross, in screen widths (W) and heights (H).
COLUMN_X = 0.28  # the focused category's centre
ROW_Y = 0.28  # the category icons' centre
CATEGORY_GAP = 0.125  # between category centres (W)
CATEGORY_ICON = 0.075
FOCUS_CATEGORY_ICON = 0.1
ITEM_ICON = 0.07
FOCUS_ITEM_ICON = 0.1
ITEM_GAP = 0.1  # between item centres
BELOW = 0.175  # the category row's centre to the focused item's centre
ABOVE = 0.13  # the category row's centre to the item just above it
LABEL_GAP = 0.025  # an icon's right edge to its label (W)
TEXT = 0.03
FOCUS_TEXT = 0.037
DETAIL = 0.026
CATEGORY_TEXT = 0.028
CLOCK_TEXT = 0.034
EDGE_X = 0.055  # the clock's and the status line's margin (W)
EDGE_Y = 0.06
COVER_HEIGHT = 1.15  # a game's cover, as a portrait, against the icon size
DIM_ITEM = 0.7  # an item away from the focus
DIM_CATEGORY = 0.5
WHITE = (1.0, 1.0, 1.0)

ICON, TILE, COVER = "icon", "tile", "cover"


@dataclasses.dataclass(frozen=True)
class Picture:
    path: pathlib.Path
    kind: str = ICON  # ICON: our silver one; TILE: a glossy tile; COVER: a game's cover (portrait)


# ---------------------------------------------------------------------- the wave


class _GL:
    """The few GL calls the wave needs, through libepoxy's dispatch pointers."""

    VERTEX_SHADER, FRAGMENT_SHADER = 0x8B31, 0x8B30
    COMPILE_STATUS, LINK_STATUS, TRIANGLES = 0x8B81, 0x8B82, 0x0004

    def __init__(self) -> None:
        lib = ctypes.CDLL("libepoxy.so.0")
        u, i, f, p = ctypes.c_uint, ctypes.c_int, ctypes.c_float, ctypes.c_void_p

        def fn(name: str, restype, *args):
            pointer = ctypes.c_void_p.in_dll(lib, f"epoxy_{name}").value
            if not pointer:
                raise OSError(f"libepoxy has no {name}")
            return ctypes.CFUNCTYPE(restype, *args)(pointer)

        self.create_shader = fn("glCreateShader", u, u)
        self.shader_source = fn("glShaderSource", None, u, i, ctypes.POINTER(ctypes.c_char_p), p)
        self.compile_shader = fn("glCompileShader", None, u)
        self.get_shaderiv = fn("glGetShaderiv", None, u, u, ctypes.POINTER(i))
        self.shader_log = fn("glGetShaderInfoLog", None, u, i, p, ctypes.c_char_p)
        self.create_program = fn("glCreateProgram", u)
        self.attach_shader = fn("glAttachShader", None, u, u)
        self.link_program = fn("glLinkProgram", None, u)
        self.get_programiv = fn("glGetProgramiv", None, u, u, ctypes.POINTER(i))
        self.delete_shader = fn("glDeleteShader", None, u)
        self.delete_program = fn("glDeleteProgram", None, u)
        self.use_program = fn("glUseProgram", None, u)
        self.uniform_location = fn("glGetUniformLocation", i, u, ctypes.c_char_p)
        self.uniform1f = fn("glUniform1f", None, i, f)
        self.uniform2f = fn("glUniform2f", None, i, f, f)
        self.uniform3f = fn("glUniform3f", None, i, f, f, f)
        self.gen_vertex_arrays = fn("glGenVertexArrays", None, i, ctypes.POINTER(u))
        self.delete_vertex_arrays = fn("glDeleteVertexArrays", None, i, ctypes.POINTER(u))
        self.bind_vertex_array = fn("glBindVertexArray", None, u)
        self.draw_arrays = fn("glDrawArrays", None, u, i, i)

    def shader(self, kind: int, source: str) -> int:
        shader = self.create_shader(kind)
        text = ctypes.c_char_p(source.encode())
        self.shader_source(shader, 1, ctypes.byref(text), None)
        self.compile_shader(shader)
        ok = ctypes.c_int(0)
        self.get_shaderiv(shader, self.COMPILE_STATUS, ctypes.byref(ok))
        if not ok.value:
            log = ctypes.create_string_buffer(1024)
            self.shader_log(shader, 1024, None, log)
            self.delete_shader(shader)
            raise OSError(f"shader: {log.value.decode(errors='replace').strip()}")
        return shader

    def program(self, vertex: str, fragment: str) -> int:
        shaders = [self.shader(self.VERTEX_SHADER, vertex), self.shader(self.FRAGMENT_SHADER, fragment)]
        program = self.create_program()
        for shader in shaders:
            self.attach_shader(program, shader)
        self.link_program(program)
        for shader in shaders:
            self.delete_shader(shader)
        ok = ctypes.c_int(0)
        self.get_programiv(program, self.LINK_STATUS, ctypes.byref(ok))
        if not ok.value:
            self.delete_program(program)
            raise OSError("shader program did not link")
        return program


def still_texture(colours: wave.Palette, size: tuple[int, int] = wave.STILL_SIZE) -> Gdk.Texture:
    width, height = size
    data = wave.still_frame(width, height, colours)
    return Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(data), width * 3)


class GLWave(Gtk.GLArea):
    __gtype_name__ = "CouchliteGLWave"

    def __init__(self, colours: wave.Palette, run_dir: pathlib.Path, failed: Callable[[str], None]) -> None:
        super().__init__()
        self.colours = colours
        self.mark = run_dir / WAVE_MARK
        self.failed = failed
        self.clock = wave.Clock()
        self.gl: _GL | None = None
        self.program = 0
        self.vao = ctypes.c_uint(0)
        self.broken = False
        self.first = True
        self.running = False
        self.tick_id = 0
        self.shown_at: float | None = None
        self.set_has_depth_buffer(False)
        self.set_can_target(False)
        self.connect("realize", self.on_realize)
        self.connect("unrealize", self.on_unrealize)
        self.connect("render", self.on_render)

    def fail(self, why: str) -> None:
        if not self.broken:
            self.broken = True
            self.set_running(False)
            self.set_visible(False)
            self.failed(why)

    def on_realize(self, _area) -> None:
        self.make_current()
        if self.get_error() is not None:
            self.fail(f"GL context: {self.get_error().message}")
            return
        try:
            self.gl = _GL()
            version = "300 es" if self.get_context().get_use_es() else "330 core"
            self.program = self.gl.program(wave.vertex(version), wave.fragment(version))
            self.gl.gen_vertex_arrays(1, ctypes.byref(self.vao))
        except (OSError, AttributeError, ValueError) as error:
            self.fail(str(error))

    def on_unrealize(self, _area) -> None:
        self.make_current()
        if self.gl is not None and self.get_error() is None:
            if self.program:
                self.gl.delete_program(self.program)
            if self.vao.value:
                self.gl.delete_vertex_arrays(1, ctypes.byref(self.vao))
        self.program, self.vao = 0, ctypes.c_uint(0)

    def on_render(self, _area, _context) -> bool:
        if self.broken or self.gl is None or not self.program:
            return False
        if self.first:
            try:
                self.mark.write_text("1\n", encoding="utf-8")
            except OSError:
                pass
        gl, scale = self.gl, self.get_scale_factor()
        now = time.monotonic()
        if self.shown_at is None:
            self.shown_at = now
        fade = min(1.0, (now - self.shown_at) * 1000 / motion.WAVE_IN_MS)
        gl.use_program(self.program)
        location = lambda name: gl.uniform_location(self.program, name.encode())  # noqa: E731
        gl.uniform2f(location("u_size"), float(self.get_width() * scale), float(self.get_height() * scale))
        gl.uniform1f(location("u_time"), float(self.clock.time))
        gl.uniform3f(location("u_top"), *map(float, self.colours.top))
        gl.uniform3f(location("u_bottom"), *map(float, self.colours.bottom))
        gl.uniform3f(location("u_ribbon"), *map(float, self.colours.ribbon))
        gl.uniform1f(location("u_alpha"), float(self.colours.alpha))
        gl.uniform1f(location("u_sparkle"), float(self.colours.sparkle))
        gl.uniform1f(location("u_fade"), float(fade))
        gl.bind_vertex_array(self.vao.value)
        gl.draw_arrays(_GL.TRIANGLES, 0, 3)
        if self.first:
            self.first = False
            GLib.idle_add(self.drawn)
        return True

    def drawn(self) -> bool:
        self.mark.unlink(missing_ok=True)
        return False

    def set_colours(self, colours: wave.Palette) -> None:
        self.colours = colours
        self.queue_render()

    def set_running(self, running: bool) -> None:
        """Animate (the home screen and its pages) or hold still (an app or a stream in front, a
        blank screen): a held wave costs nothing."""
        running = running and not self.broken
        if running == self.running:
            return
        self.running = running
        if running:
            self.tick_id = self.add_tick_callback(self.on_tick)
        else:
            if self.tick_id:
                self.remove_tick_callback(self.tick_id)
                self.tick_id = 0
            self.clock.pause()

    def on_tick(self, _widget, frame_clock) -> bool:
        now = frame_clock.get_frame_time() / 1_000_000
        if self.clock.due(now):
            self.clock.frame(now)
            self.queue_render()
        return True


class Wave(Gtk.Overlay):
    """The still frame, and over it the GL wave when it runs. `animate`: whether the GL wave is
    made at all (default: `how` is GL), so set_how() can turn it on later (a theme or background
    that was flat or plain at the start)."""

    __gtype_name__ = "CouchliteWave"

    def __init__(self, how: str, colours: wave.Palette, run_dir: pathlib.Path, log: Callable[[str], None],
                 animate: bool | None = None) -> None:
        super().__init__()
        self.how = how
        self.log = log
        self.started = False
        self.running = True  # what set_running() last asked for
        self.still = Gtk.Picture()
        self.still.set_content_fit(Gtk.ContentFit.FILL)
        self.still.set_can_shrink(True)
        self.still.set_can_target(False)
        self.set_child(self.still)
        self.gl: GLWave | None = None
        if how == wave.GL if animate is None else animate:
            self.gl = GLWave(colours, run_dir, self.gl_failed)
            self.gl.set_visible(False)  # start(): not in the first frame
            self.add_overlay(self.gl)
        self.set_colours(colours)

    def gl_failed(self, why: str) -> None:
        self.how = wave.STATIC
        self.log(f"tv wave: GL off, still frame instead: {why}")

    def set_how(self, how: str) -> None:
        """Another theme or [appearance] background: GL, STATIC or FLAT from now on (GL only where
        the GL wave was made and works). set_colours() next draws the still frame for it."""
        if how == wave.GL and (self.gl is None or self.gl.broken):
            how = wave.STATIC
        self.how = how
        self.show_gl()

    def show_gl(self) -> None:
        if self.gl is not None and not self.gl.broken:
            shown = self.started and self.how == wave.GL
            if shown and not self.gl.get_visible():
                self.gl.shown_at = None  # fade in again over the still frame
            self.gl.set_visible(shown)
            self.gl.set_running(shown and self.running)

    def set_colours(self, colours: wave.Palette) -> None:
        if self.how == wave.FLAT:
            colours = wave.Palette(colours.top, colours.top, colours.ribbon, 0.0)
        self.palette = colours  # as drawn (tv-headless.sh reads it)
        self.still.set_paintable(still_texture(colours))
        if self.gl is not None:
            self.gl.set_colours(colours)

    def start(self) -> None:
        """Show the GL wave (after the first frame) and let it run."""
        self.started = True
        self.show_gl()

    def set_running(self, running: bool) -> None:
        self.running = running
        if self.gl is not None and self.gl.get_visible():
            self.gl.set_running(running)


def make_wave(how: str, colours: wave.Palette, run_dir: pathlib.Path, log: Callable[[str], None],
              animate: bool | None = None) -> Wave:
    return Wave(how, colours, run_dir, log, animate)


def wave_failed_before(run_dir: pathlib.Path) -> bool:
    """The last GL wave never finished its first frame: draw the still one this time."""
    mark = run_dir / WAVE_MARK
    if mark.exists():
        mark.unlink(missing_ok=True)
        return True
    return False


# ---------------------------------------------------------------------- the cross


def _glow_texture(size: int = 64) -> Gdk.Texture:
    """A soft white spot, the light behind the focused item."""
    data = bytearray()
    middle = (size - 1) / 2
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - middle, y - middle) / middle
            alpha = max(0.0, 1 - d) ** 2.2
            data += bytes((255, 255, 255, round(alpha * 255)))
    return Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(bytes(data)), size * 4)


def _rect(x: float, y: float, width: float, height: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, width, height)


def _rgba(colour: tuple[float, float, float], alpha: float) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue, rgba.alpha = colour[0], colour[1], colour[2], alpha
    return rgba


class XmbView(Gtk.Widget):
    """`model` is the cross; `picture(item)` what to draw for an item (a Picture or None for its
    bundled icon); `icon_path(name)` a bundled icon's file; `status()` the clock line."""

    __gtype_name__ = "CouchliteXmbView"

    def __init__(
        self,
        model: xmb.XmbModel,
        picture: Callable[[xmb.Item], Picture | None],
        icon_path: Callable[[str], pathlib.Path],
        status: Callable[[], tuple[str, str]],
        level: str = motion.FULL,
    ) -> None:
        super().__init__()
        self.model = model
        self.picture = picture
        self.icon_path = icon_path
        self.status = status
        self.animator = motion.Animator(time.monotonic, level)
        self.textures: dict[tuple, Gdk.Texture | None] = {}
        self.glow = _glow_texture()
        self.tick_id = 0
        self.message = ""  # the last result, at the bottom (Tv.status)
        self.text_colour = WHITE
        self.drawn_labels: list[dict] = []
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_can_target(False)
        self.sync(animate=False)

    # -------------------------------------------------------------- motion

    def sync(self, animate: bool = True) -> None:
        """Move to what the model shows now: its category and each category's row."""
        go = self.animator.to if animate else (lambda name, value, _ms=0, **_kw: self.animator.set(name, value))
        go("column", float(self.model.column), motion.CATEGORY_MS)
        for category in self.model.categories:
            go(f"row:{category.key}", float(self.model.rows.get(category.key, 0)), motion.ITEM_MS)
        self.animate()

    def animate(self) -> None:
        if not self.animator.idle and not self.tick_id:
            self.tick_id = self.add_tick_callback(self.on_tick)
        self.queue_draw()

    def on_tick(self, _widget, _clock) -> bool:
        moving = self.animator.step()
        self.queue_draw()
        if not moving:
            self.tick_id = 0
            return False
        return True

    def set_level(self, level: str) -> None:
        self.animator.level = level

    # -------------------------------------------------------------- pictures

    def texture(self, path: pathlib.Path, px: int) -> Gdk.Texture | None:
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return None
        key = (str(path), stamp, px)
        if key not in self.textures:
            if len(self.textures) > 160:
                self.textures.clear()
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), px, px, True)
                self.textures[key] = Gdk.Texture.new_for_pixbuf(pixbuf)
            except GLib.Error:
                self.textures[key] = None
        return self.textures[key]

    def forget_pictures(self) -> None:
        self.textures.clear()
        self.queue_draw()

    # -------------------------------------------------------------- text

    def layout(self, text: str, px: float, bold: bool = False, width: float = 0) -> Pango.Layout:
        layout = self.create_pango_layout(text)
        font = Pango.FontDescription.from_string(f"{FONT} {'Bold' if bold else 'Medium'}")
        font.set_absolute_size(max(1.0, px) * Pango.SCALE)
        layout.set_font_description(font)
        if width > 0:
            layout.set_width(round(width * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
        return layout

    def text(self, snapshot: Gtk.Snapshot, text: str, x: float, y: float, px: float, alpha: float,
             bold: bool = False, width: float = 0, centre: bool = False, kind: str = "item") -> float:
        """Draw `text` with its top left at (x, y) (or centred on x); returns its height."""
        if not text or alpha <= 0.01:
            return 0.0
        layout = self.layout(text, px, bold, width)
        shown_w, shown_h = layout.get_pixel_size()
        if centre:
            x -= shown_w / 2
        for dx, dy, shade in ((0, px * 0.06, 0.45), (0, 0, 0.0)):
            snapshot.save()
            snapshot.translate(Graphene.Point().init(x + dx, y + dy))
            colour = (0.0, 0.0, 0.0) if shade else self.text_colour
            snapshot.append_layout(layout, _rgba(colour, alpha * (shade or 1.0)))
            snapshot.restore()
        natural = self.layout(text, px, bold).get_pixel_size()[0]
        self.drawn_labels.append({
            "text": text, "classes": [f"xmb-{kind}"], "font_px": round(px, 1), "x": round(x), "y": round(y),
            "right": round(x + shown_w), "width": round(width or shown_w), "natural": natural, "wrap": False,
            "ellipsized": layout.is_ellipsized(), "on_purpose": kind in ("item", "detail", "message"),
        })
        return shown_h

    # -------------------------------------------------------------- drawing

    def draw_picture(self, snapshot: Gtk.Snapshot, item_picture: Picture | None, icon: str,
                     cx: float, cy: float, size: float, alpha: float, max_px: int) -> float:
        """The item's picture centred on (cx, cy); returns its drawn width."""
        kind = item_picture.kind if item_picture is not None else ICON
        path = item_picture.path if item_picture is not None else self.icon_path(icon)
        if kind == COVER:
            height = size * COVER_HEIGHT
            width = height * 2 / 3
            texture = self.texture(path, round(max_px * COVER_HEIGHT))
        else:
            width = height = size * (1.12 if kind == TILE else 1.0)  # a tile's shadow margin
            texture = self.texture(path, max_px)
        if texture is None and kind != ICON:
            return self.draw_picture(snapshot, None, icon, cx, cy, size, alpha, max_px)
        if texture is None:
            return width
        if kind == COVER:  # a cover keeps its own shape inside the portrait box
            ratio = texture.get_width() / max(1, texture.get_height())
            width = min(width, height * ratio) if ratio < 2 / 3 else width
            height = width / ratio if ratio >= 2 / 3 else height
        snapshot.push_opacity(alpha)
        snapshot.append_texture(texture, _rect(cx - width / 2, cy - height / 2, width, height))
        snapshot.pop()
        return width

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0 or not self.model.categories:
            return
        self.drawn_labels = []
        column = self.animator.get("column", float(self.model.column))
        row_y = height * ROW_Y
        max_px = round(height * FOCUS_ITEM_ICON)
        left = width * COLUMN_X
        # The clock and the status, top right, as the console has them.
        clock, line = self.status()
        y = height * EDGE_Y
        right = width * (1 - EDGE_X)
        clock_layout = self.layout(clock, height * CLOCK_TEXT, bold=True)
        y += self.text(snapshot, clock, right - clock_layout.get_pixel_size()[0], y, height * CLOCK_TEXT, 0.95,
                       bold=True, kind="clock")
        if line:
            line_layout = self.layout(line, height * DETAIL)
            self.text(snapshot, line, right - line_layout.get_pixel_size()[0], y + height * 0.006,
                      height * DETAIL, 0.8, kind="status")
        # Categories, left to right.
        for index, category in enumerate(self.model.categories):
            offset = index - column
            x = left + offset * width * CATEGORY_GAP
            if x < -width * 0.1 or x > width * 1.1:
                continue
            focus = max(0.0, 1 - abs(offset))
            size = height * (CATEGORY_ICON + (FOCUS_CATEGORY_ICON - CATEGORY_ICON) * focus)
            alpha = DIM_CATEGORY + (1 - DIM_CATEGORY) * focus
            if focus > 0.01:
                glow = size * 2.0
                snapshot.push_opacity(0.35 * focus)
                snapshot.append_texture(self.glow, _rect(x - glow / 2, row_y - glow / 2, glow, glow))
                snapshot.pop()
            self.draw_picture(snapshot, None, category.icon, x, row_y, size, alpha, max_px)
            self.text(snapshot, category.label, x, row_y + size / 2 + height * 0.012, height * CATEGORY_TEXT,
                      focus ** 2, bold=True, centre=True, kind="category")
        # The focused category's items (and the next one's, fading, while sliding).
        for index, category in enumerate(self.model.categories):
            offset = index - column
            presence = max(0.0, 1 - abs(offset))
            if presence <= 0.01:
                continue
            x = left + offset * width * CATEGORY_GAP
            self.draw_items(snapshot, category, x, row_y, width, height, presence ** 1.5, max_px)
        if self.message:
            self.text(snapshot, self.message, width / 2, height * (1 - EDGE_Y) - height * DETAIL * 1.4,
                      height * DETAIL, 0.9, width=width * (1 - 2 * EDGE_X), centre=True, kind="message")

    def draw_items(self, snapshot: Gtk.Snapshot, category: xmb.Category, x: float, row_y: float,
                   width: float, height: float, presence: float, max_px: int) -> None:
        row = self.animator.get(f"row:{category.key}", float(self.model.rows.get(category.key, 0)))
        below, above, gap = height * BELOW, height * ABOVE, height * ITEM_GAP
        label_room = width * (1 - EDGE_X) - x
        for index, item in enumerate(category.items):
            d = index - row
            if d >= 0:
                cy = row_y + below + d * gap
            elif d > -1:  # between the focus spot and the first place above the row
                cy = row_y + below + d * (above + below)
            else:
                cy = row_y - above + (d + 1) * gap
            if cy < -gap or cy > height + gap:
                continue
            focus = max(0.0, 1 - abs(d))
            size = height * (ITEM_ICON + (FOCUS_ITEM_ICON - ITEM_ICON) * focus)
            alpha = presence * (DIM_ITEM + (1 - DIM_ITEM) * focus)
            if focus > 0.01:
                glow = size * 2.1
                snapshot.push_opacity(0.4 * focus * presence)
                snapshot.append_texture(self.glow, _rect(x - glow / 2, cy - glow / 2, glow, glow))
                snapshot.pop()
            shown = self.draw_picture(snapshot, self.picture(item), item.icon, x, cy, size, alpha, max_px)
            text_x = x + max(shown, size) / 2 + width * LABEL_GAP
            px = height * (TEXT + (FOCUS_TEXT - TEXT) * focus)
            room = max(1.0, label_room - (text_x - x))
            label_h = self.layout(item.label, px, focus > 0.5).get_pixel_size()[1]
            detail = item.detail if focus > 0.5 else ""
            detail_h = height * DETAIL * 1.25 if detail else 0
            top = cy - (label_h + detail_h) / 2
            self.text(snapshot, item.label, text_x, top, px, alpha, bold=focus > 0.5, width=room)
            if detail:
                self.text(snapshot, detail, text_x, top + label_h, height * DETAIL, alpha * 0.8 * (focus - 0.5) * 2,
                          width=room, kind="detail")
