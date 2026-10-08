"""Real icons on glossy PS3-style tiles, as plain Python.

An application's own icon (couchliteos_icons.official) or a website's (couchliteos_siteicon)
is drawn on a rounded square with a soft shadow, a gloss arc over its top half and a rim
of light, so a row of logos from different makers looks like one set. Nobody's logo is in
this repository: the tiles are made on the box, from the icon the box already has, once.

plan() looks at the logo: one that fills its own square, sharp or rounded (Netflix's, Max's),
becomes the whole tile, its own square fitted to ours; a logo on transparency sits on a
light tile, or on a dark one when the logo itself is light. A small icon is blown up at most
MAX_UPSCALE times: what it does not cover is its own edge colour, or the tile's.
compose() paints the tile from the logo, already scaled to logo_box() (GdkPixbuf does that
in make_tile, the only part that needs GTK's libraries). Tiles is the front end's cache of
tiles for installed applications' icons, made off the UI thread.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import os
import pathlib
import queue
import threading

import couchliteos_artwork as artwork

SIZE = 256
MARGIN = 0.07  # of SIZE on each side: room for the shadow
RADIUS = 0.2  # of the tile's side
LOGO = 0.64  # a logo on transparency fits in this much of the tile
FULL_BLEED = 0.9  # of a ring's pixels opaque: the logo is a tile already
INSETS = (0.0, 0.04, 0.08, 0.12)  # rings tried, in from the edge: 0.12 is inside a rounded corner
MAX_UPSCALE = 3.0  # more than this and a favicon turns to mush
LIGHT_LOGO = 0.72  # a logo this bright (luminance, 0-1) goes on a dark tile
LIGHT_TILE = ((1.0, 1.0, 1.0), (0.80, 0.83, 0.88))  # top and bottom of the gradient
DARK_TILE = ((0.24, 0.30, 0.42), (0.07, 0.10, 0.16))
SHADOW = 0.5  # its darkest alpha, under the tile's middle
SHADOW_DROP = 0.025  # of SIZE: the shadow sits this far below the tile
SHADOW_BLUR = 0.04  # of SIZE
GLOSS = (0.42, 0.10)  # white over the top half: alpha at the top, alpha at the arc
GLOSS_DEPTH = 0.5  # the arc's middle, down the tile
GLOSS_BOW = 0.09  # how much higher the arc is at the sides
RIM = 0.4  # the top edge's highlight
MAX_SOURCE = 2 * 1024 * 1024
TILE_DIR = artwork.CACHE_DIR / "tiles"
TILE_LIMIT = 200  # files; the oldest go first


@dataclasses.dataclass(frozen=True)
class Plan:
    full_bleed: bool
    top: tuple[float, float, float]
    bottom: tuple[float, float, float]
    # A full bleed logo's own square, as fractions of its width and height (left, top, right,
    # bottom): that square is what covers the tile.
    square: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)


def _pixels(rgba: bytes, width: int, height: int):
    if len(rgba) != width * height * 4 or width < 1 or height < 1:
        raise ValueError("RGBA data does not match its size")
    return memoryview(rgba)


def _ring(width: int, height: int, inset: float) -> list[tuple[int, int]]:
    x0, y0 = min(width // 2, round(width * inset)), min(height // 2, round(height * inset))
    x1, y1 = max(x0, width - 1 - x0), max(y0, height - 1 - y0)
    ring = [(x, y0) for x in range(x0, x1 + 1)] + [(x, y1) for x in range(x0, x1 + 1)]
    return ring + [(x0, y) for y in range(y0 + 1, y1)] + [(x1, y) for y in range(y0 + 1, y1)]


def border_opaque(rgba: bytes, width: int, height: int, inset: float = 0.0) -> float:
    """The share of a ring's pixels, `inset` in from the edge, that are (nearly) opaque."""
    data = _pixels(rgba, width, height)
    ring = _ring(width, height, inset)
    return sum(data[(y * width + x) * 4 + 3] >= 200 for x, y in ring) / len(ring)


def opaque_bounds(rgba: bytes, width: int, height: int) -> tuple[float, float, float, float]:
    """The box around the (nearly) opaque pixels, as fractions; the whole picture when none."""
    data = _pixels(rgba, width, height)
    xs, ys = [], []
    for y in range(height):
        row = y * width * 4
        found = [x for x in range(width) if data[row + x * 4 + 3] >= 200]
        if found:
            xs += (found[0], found[-1])
            ys.append(y)
    if not xs:
        return 0.0, 0.0, 1.0, 1.0
    return min(xs) / width, ys[0] / height, (max(xs) + 1) / width, (ys[-1] + 1) / height


def ring_colour(rgba: bytes, width: int, height: int, inset: float) -> tuple[float, float, float]:
    data = _pixels(rgba, width, height)
    ring = _ring(width, height, inset)
    sums = [sum(data[(y * width + x) * 4 + i] for x, y in ring) / len(ring) / 255 for i in range(3)]
    return sums[0], sums[1], sums[2]


def mean_colour(rgba: bytes, width: int, height: int) -> tuple[float, float, float] | None:
    """The logo's colour: the average of its visible pixels, weighted by their alpha."""
    data = _pixels(rgba, width, height)
    total = red = green = blue = 0.0
    for at in range(0, len(data), 4):
        alpha = data[at + 3]
        if alpha > 32:
            total += alpha
            red += data[at] * alpha
            green += data[at + 1] * alpha
            blue += data[at + 2] * alpha
    if not total:
        return None
    return red / total / 255, green / total / 255, blue / total / 255


def luminance(colour: tuple[float, float, float]) -> float:
    return 0.2126 * colour[0] + 0.7152 * colour[1] + 0.0722 * colour[2]


def plan(rgba: bytes, width: int, height: int) -> Plan:
    for inset in INSETS:
        if border_opaque(rgba, width, height, inset) >= FULL_BLEED:
            edge = ring_colour(rgba, width, height, inset)
            return Plan(True, edge, edge, square=opaque_bounds(rgba, width, height))
    colour = mean_colour(rgba, width, height)
    light = colour is not None and luminance(colour) >= LIGHT_LOGO
    return Plan(False, *(DARK_TILE if light else LIGHT_TILE))


def tile_rect(size: int = SIZE) -> tuple[float, float, float]:
    """(left, top, side): the tile, square, a little high to leave the shadow room."""
    margin = size * MARGIN
    side = size - 2 * margin
    return margin, margin - size * SHADOW_DROP / 2, side


def logo_box(how: Plan, width: int, height: int, size: int = SIZE) -> tuple[int, int, int, int]:
    """(x, y, width, height) of the scaled logo on the canvas: covering the tile for a full
    bleed one, else fitted into LOGO of it, centred."""
    left, top, side = tile_rect(size)
    if how.full_bleed:
        x0, y0, x1, y1 = how.square
        scale = min(max(side / (width * (x1 - x0)), side / (height * (y1 - y0))), MAX_UPSCALE)
        middle = ((x0 + x1) / 2 * width * scale, (y0 + y1) / 2 * height * scale)
    else:
        scale = min(side * LOGO / width, side * LOGO / height, MAX_UPSCALE)
        middle = (width * scale / 2, height * scale / 2)
    out_w, out_h = max(1, round(width * scale)), max(1, round(height * scale))
    return round(left + side / 2 - middle[0]), round(top + side / 2 - middle[1]), out_w, out_h


def _rounded(px: float, py: float, cx: float, cy: float, half: float, radius: float) -> float:
    """Signed distance from a rounded square (negative inside)."""
    qx = abs(px - cx) - (half - radius)
    qy = abs(py - cy) - (half - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - radius


def _clamp(value: float) -> float:
    return 0.0 if value < 0 else 1.0 if value > 1 else value


def compose(logo: bytes, box: tuple[int, int, int, int], how: Plan, size: int = SIZE) -> bytes:
    """The tile as straight (not premultiplied) RGBA, size x size. `logo` is RGBA already
    scaled to `box` (logo_box); the parts of it outside the tile are cut off."""
    lx, ly, lw, lh = box
    source = _pixels(logo, lw, lh)
    left, top, side = tile_rect(size)
    half = side / 2
    cx, cy = left + half, top + half
    radius = side * RADIUS
    drop, blur = size * SHADOW_DROP, size * SHADOW_BLUR
    out = bytearray(size * size * 4)
    for py in range(size):
        y = py + 0.5
        v = _clamp((y - top) / side)
        base = [how.top[i] + (how.bottom[i] - how.top[i]) * v for i in range(3)]
        for px in range(size):
            x = px + 0.5
            inside = _rounded(x, y, cx, cy, half, radius)
            cover = _clamp(0.5 - inside)
            shade = _rounded(x, y - drop, cx, cy, half, radius)
            shadow = SHADOW * _clamp((blur - shade) / (2 * blur))
            at = (py * size + px) * 4
            if cover <= 0:
                out[at + 3] = round(shadow * 255)
                continue
            red, green, blue = base
            sx, sy = px - lx, py - ly
            if 0 <= sx < lw and 0 <= sy < lh:
                src = (sy * lw + sx) * 4
                alpha = source[src + 3] / 255
                red += (source[src] / 255 - red) * alpha
                green += (source[src + 1] / 255 - green) * alpha
                blue += (source[src + 2] / 255 - blue) * alpha
            if how.full_bleed:  # a logo's own square gets the same light from above
                dim = 1 - 0.16 * v * v
                red, green, blue = red * dim, green * dim, blue * dim
            arc = top + side * GLOSS_DEPTH - side * GLOSS_BOW * ((x - cx) / half) ** 2
            gloss = _clamp(arc - y + 0.5) * (GLOSS[0] + (GLOSS[1] - GLOSS[0]) * _clamp((y - top) / (arc - top)))
            rim = _clamp(1.0 + inside / 2.0) * RIM * (1 - v) if inside > -2.5 else 0.0
            light = 1 - (1 - gloss) * (1 - rim)
            red, green, blue = (c + (1 - c) * light for c in (red, green, blue))
            alpha = cover + shadow * (1 - cover)
            out[at] = round(red * cover / alpha * 255)
            out[at + 1] = round(green * cover / alpha * 255)
            out[at + 2] = round(blue * cover / alpha * 255)
            out[at + 3] = round(alpha * 255)
    return bytes(out)


def make_tile(data: bytes = b"", path: pathlib.Path | None = None, size: int = SIZE) -> bytes:
    """A PNG of the tile for an icon given as bytes (PNG, ICO, JPEG; a site's icon) or as a file
    (PNG or SVG; an installed icon). Raises artwork.ArtworkError when it cannot be read."""
    import gi  # here, not at the top: the module and its tests work without GTK

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib

    try:
        if path is not None:
            if path.stat().st_size > MAX_SOURCE:
                raise artwork.ArtworkError("icon file is too large")
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), size * 2, size * 2, True)
        else:
            if len(data) > MAX_SOURCE:
                raise artwork.ArtworkError("icon is too large")
            loader = GdkPixbuf.PixbufLoader.new_with_type(artwork.image_kind(data))
            loader.write(data)
            loader.close()
            pixbuf = loader.get_pixbuf()
    except (GLib.Error, OSError):
        raise artwork.ArtworkError("not a readable icon") from None
    if pixbuf is None or pixbuf.get_width() * pixbuf.get_height() > artwork.MAX_PIXELS:
        raise artwork.ArtworkError("icon size is not usable")
    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(False, 0, 0, 0)

    def tight(image) -> bytes:
        width, height, stride = image.get_width(), image.get_height(), image.get_rowstride()
        raw = image.get_pixels()
        return b"".join(raw[row * stride: row * stride + width * 4] for row in range(height))

    how = plan(tight(pixbuf), pixbuf.get_width(), pixbuf.get_height())
    box = logo_box(how, pixbuf.get_width(), pixbuf.get_height(), size)
    scaled = pixbuf.scale_simple(box[2], box[3], GdkPixbuf.InterpType.HYPER)
    rgba = compose(tight(scaled), box, how, size)
    tile = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(rgba), GdkPixbuf.Colorspace.RGB, True, 8,
                                           size, size, size * 4)
    ok, buffer = tile.save_to_bufferv("png", [], [])
    if not ok:
        raise artwork.ArtworkError("could not encode the tile")
    return bytes(buffer)


class Tiles:
    """Tiles for installed applications' icons, kept in `directory` by the icon's path and
    modification time. `tile()` never waits: it returns the tile when made, else queues it."""

    def __init__(self, directory: pathlib.Path = TILE_DIR, make=make_tile, limit: int = TILE_LIMIT) -> None:
        self.directory = directory
        self.make = make
        self.limit = limit
        self.jobs: queue.Queue = queue.Queue()
        self.pending: set[pathlib.Path] = set()
        self.failed: set[str] = set()
        self.lock = threading.Lock()
        self._changed = threading.Event()
        self._started = False
        self.awake = threading.Event()  # cleared while the TV interface rests: tiles wait
        self.awake.set()

    def name(self, icon: pathlib.Path) -> str | None:
        try:
            stamp = icon.stat().st_mtime_ns
        except OSError:
            return None
        return hashlib.sha256(f"{icon}\0{stamp}".encode()).hexdigest()[:32] + ".png"

    def tile(self, icon: pathlib.Path) -> pathlib.Path | None:
        name = self.name(icon)
        if name is None or name in self.failed:
            return None
        made = self.directory / name
        if made.is_file():
            return made
        with self.lock:
            if icon in self.pending:
                return None
            self.pending.add(icon)
            if not self._started:
                self._started = True
                threading.Thread(target=self._loop, name="tiles", daemon=True).start()
        self.jobs.put(icon)
        return None

    def _loop(self) -> None:
        while True:
            icon = self.jobs.get()
            self.awake.wait()
            try:
                self.run_job(icon)
            finally:
                with self.lock:
                    self.pending.discard(icon)

    def run_job(self, icon: pathlib.Path) -> bool:
        """Make one tile; False (and no retry until the icon changes) when it cannot be made."""
        name = self.name(icon)
        if name is None:
            return False
        try:
            data = self.make(path=icon)
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.directory / f".{name}.{os.getpid()}.tmp"
            temporary.write_bytes(data)
            temporary.replace(self.directory / name)
        except (artwork.ArtworkError, OSError, ValueError):
            self.failed.add(name)
            return False
        self.evict()
        self._changed.set()
        return True

    def evict(self) -> None:
        try:
            files = sorted(self.directory.glob("*.png"), key=lambda path: path.stat().st_mtime)
        except OSError:
            return
        for old in files[:max(0, len(files) - self.limit)]:
            old.unlink(missing_ok=True)

    def take_changed(self) -> bool:
        """True once after a tile was made: the front end draws again."""
        changed = self._changed.is_set()
        self._changed.clear()
        return changed
