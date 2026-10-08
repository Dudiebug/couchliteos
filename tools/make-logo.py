#!/usr/bin/env python3
"""Draw the CouchLiteOS logo and the boot splash, from nothing but this script.

    tools/make-logo.py            write the files below
    tools/make-logo.py --check    exit 1 when a written file differs from what this script makes

The logo is our own: a couch drawn as a silver, glossy silhouette like the XMB's icons, a glint
of light over its arm, and COUCHLITEOS in thin, wide-spaced capitals with OS in the wave's blue.
It copies no one's mark or lettering: every letter is a few strokes defined below.

Into overlay/usr/share/plymouth/themes/couchliteos/ (the Plymouth theme; the TV's loading screen,
couchliteos_gtk_boot, draws the same pictures):
    logo.png halo.png ribbon-1.png ribbon-2.png dot.png    sized for a screen 1080 lines high
    couchliteos.plymouth couchliteos.script                 the theme, timed by launcher/couchliteos_boot.py
and into docs/images/: couchliteos-logo.png (the README's banner) and couchliteos-icon.png (the
mark on a glossy tile, 256 px, for anywhere an app or OS icon is shown).

Pure Python (zlib for the PNGs) and deterministic. --check compares pixels, allowing 1/255 for
maths libraries that round differently, and the theme's text exactly.
"""

from __future__ import annotations

import math
import pathlib
import struct
import sys
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "launcher"))
import couchliteos_boot as boot  # noqa: E402

THEME_DIR = ROOT / "overlay/usr/share/plymouth/themes/couchliteos"
IMAGES = ROOT / "docs/images"
INF = float("inf")

SILVER = ((0.0, (1.0, 1.0, 1.0)), (0.5, (0.886, 0.906, 0.933)), (1.0, (0.592, 0.639, 0.710)))  # the XMB icons'
WORD = ((0.0, (1.0, 1.0, 1.0)), (1.0, (0.788, 0.824, 0.878)))
BLUE = ((0.0, (0.812, 0.878, 1.0)), (1.0, (0.561, 0.706, 0.961)))
INK = (0.169, 0.220, 0.314)  # details engraved into a silver shape, as the icons' INK
GLOW = (0.659, 0.776, 1.0)

# ---------------------------------------------------------------------- shapes

# The mark, in units (y down): a sofa from the front. Its back (an arch-topped panel), two arms
# and the seat are rounded polygons (corners, then how far they are rounded); the legs are
# capsules (segment ends and a radius); seams engraved in INK mark the cushions; a four-pointed
# glint sits over the right arm.
BACK = ((19.0, 17.0), (50.0, -1.0), (81.0, 17.0))  # the top: a quadratic curve, start, control, end
BACK_BOTTOM = 36.0
PANELS = (
    (((9.0, 29.0), (15.0, 29.0), (15.0, 49.0), (9.0, 49.0)), 6.0),
    (((85.0, 29.0), (91.0, 29.0), (91.0, 49.0), (85.0, 49.0)), 6.0),
    (((22.0, 38.0), (78.0, 38.0), (78.0, 45.0), (22.0, 45.0)), 3.0),
)
BACK_ROUNDING = 4.0
LEGS = ((13.0, 55.0, 13.0, 58.0, 2.2), (87.0, 55.0, 87.0, 58.0, 2.2))
SEAMS = ((21.6, 35.3, 78.4, 35.3, 0.7), (21.2, 30.0, 21.2, 54.0, 0.7), (78.8, 30.0, 78.8, 54.0, 0.7),
         (50.0, 36.5, 50.0, 47.0, 0.7), (50.0, 9.5, 50.0, 34.0, 0.7))
GLINT = (93.0, 5.0, 8.0, 0.24)  # centre, reach of its points, its waist (of the reach)
GLOSS_BOTTOM = 26.0  # the shine on the top of the back fades out here
MARK_BOX = (2.0, -4.0, 102.0, 61.0)  # what the mark covers, for placing it

# The wordmark: each letter is strokes (lists of points, y up from the baseline, x from the
# letter's left) in a box CAP high; ADVANCE is its width. Arcs are (cx, cy, r, from, to) in degrees.
CAP = 52.0
STROKE = 3.6  # half the stroke width
TRACKING = 28.0


def arc(cx: float, cy: float, r: float, start: float, end: float, steps: int = 0) -> list[tuple[float, float]]:
    steps = steps or max(8, int(abs(end - start) / 6))
    return [(cx + r * math.cos(math.radians(start + (end - start) * i / steps)),
             cy + r * math.sin(math.radians(start + (end - start) * i / steps))) for i in range(steps + 1)]


GLYPHS: dict[str, tuple[float, list[list[tuple[float, float]]]]] = {
    "C": (46.0, [arc(26, 26, 26, 45, 315)]),
    "O": (52.0, [arc(26, 26, 26, 0, 360)]),
    "U": (36.0, [[(0, 52)] + arc(18, 18, 18, 180, 360) + [(36, 52)]]),
    "H": (36.0, [[(0, 0), (0, 52)], [(36, 0), (36, 52)], [(0, 27), (36, 27)]]),
    "L": (30.0, [[(0, 52), (0, 0), (30, 0)]]),
    "I": (0.0, [[(0, 0), (0, 52)]]),
    "T": (38.0, [[(0, 52), (38, 52)], [(19, 52), (19, 0)]]),
    "E": (32.0, [[(32, 52), (0, 52), (0, 0), (32, 0)], [(0, 27), (27, 27)]]),
    "S": (32.0, [arc(16, 39, 13, 25, 270) + arc(16, 13, 13, 90, -155)]),
}
WORDS = (("COUCHLITE", WORD), ("OS", BLUE))


def quadratic(p0, c, p1, steps: int = 16) -> list[tuple[float, float]]:
    return [((1 - t) ** 2 * p0[0] + 2 * t * (1 - t) * c[0] + t * t * p1[0],
             (1 - t) ** 2 * p0[1] + 2 * t * (1 - t) * c[1] + t * t * p1[1])
            for t in (i / steps for i in range(steps + 1))]


def glint_polygon(cx: float, cy: float, reach: float, waist: float) -> list[tuple[float, float]]:
    """A four-pointed glint: each side curves in towards the centre (a quadratic through a point
    `waist` of the reach out on the diagonal)."""
    tips = [(cx, cy - reach), (cx + reach, cy), (cx, cy + reach), (cx - reach, cy)]
    points = []
    for i, tip in enumerate(tips):
        nxt = tips[(i + 1) % 4]
        diagonal = ((tip[0] + nxt[0]) / 2 - cx, (tip[1] + nxt[1]) / 2 - cy)
        length = math.hypot(*diagonal)
        control = (cx + diagonal[0] / length * reach * waist * 2 - (tip[0] + nxt[0] - 2 * cx) / 2,
                   cy + diagonal[1] / length * reach * waist * 2 - (tip[1] + nxt[1] - 2 * cy) / 2)
        points += quadratic(tip, control, nxt, 12)[:-1]
    return points


def wordmark() -> tuple[list[tuple[list[tuple[float, float]], tuple]], float]:
    """Every stroke of COUCHLITEOS with its colour ramp, y down from the cap line, and the width."""
    strokes = []
    x = 0.0
    for text, ramp in WORDS:
        for letter in text:
            advance, paths = GLYPHS[letter]
            for path in paths:
                strokes.append(([(x + px, CAP - py) for px, py in path], ramp))
            x += advance + TRACKING
    return strokes, x - TRACKING


# ---------------------------------------------------------------------- drawing


class Field:
    """A signed distance field in pixels (negative inside), filled shape by shape: union by min.
    Only pixels within `reach` of a shape are visited."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.d = [INF] * (width * height)

    def capsule(self, ax: float, ay: float, bx: float, by: float, r: float, reach: float) -> None:
        w, d = self.width, self.d
        x0, x1 = max(0, int(min(ax, bx) - r - reach)), min(w - 1, int(max(ax, bx) + r + reach) + 1)
        y0, y1 = max(0, int(min(ay, by) - r - reach)), min(self.height - 1, int(max(ay, by) + r + reach) + 1)
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        sqrt = math.sqrt
        for py in range(y0, y1 + 1):
            cy = py + 0.5 - ay
            row = py * w
            for px in range(x0, x1 + 1):
                cx = px + 0.5 - ax
                t = (cx * dx + cy * dy) / length if length else 0.0
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                ex, ey = cx - t * dx, cy - t * dy
                value = sqrt(ex * ex + ey * ey) - r
                if value < d[row + px]:
                    d[row + px] = value

    def polyline(self, points: list[tuple[float, float]], r: float, reach: float) -> None:
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            self.capsule(ax, ay, bx, by, r, reach)
        if len(points) == 1:
            self.capsule(*points[0], *points[0], r, reach)

    def polygon(self, points: list[tuple[float, float]], reach: float, rounding: float = 0.0) -> None:
        """Fill a polygon, its outline pushed out (and its corners rounded) by `rounding`."""
        w, d = self.width, self.d
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        reach += rounding
        x0, x1 = max(0, int(min(xs) - reach)), min(w - 1, int(max(xs) + reach) + 1)
        y0, y1 = max(0, int(min(ys) - reach)), min(self.height - 1, int(max(ys) + reach) + 1)
        edges = list(zip(points, points[1:] + points[:1]))
        for py in range(y0, y1 + 1):
            cy = py + 0.5
            for px in range(x0, x1 + 1):
                cx = px + 0.5
                best, inside = INF, False
                for (ax, ay), (bx, by) in edges:
                    if (ay > cy) != (by > cy) and cx < ax + (cy - ay) * (bx - ax) / (by - ay):
                        inside = not inside
                    dx, dy = bx - ax, by - ay
                    t = max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / (dx * dx + dy * dy)))
                    ex, ey = cx - ax - t * dx, cy - ay - t * dy
                    best = min(best, ex * ex + ey * ey)
                value = (-math.sqrt(best) if inside else math.sqrt(best)) - rounding
                if value < d[py * w + px]:
                    d[py * w + px] = value


def coverage(value: float) -> float:
    return 0.0 if value >= 0.5 else 1.0 if value <= -0.5 else 0.5 - value


def ramp(stops, t: float) -> tuple[float, float, float]:
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return tuple(a + (b - a) * k for a, b in zip(c0, c1))  # type: ignore[return-value]
    return stops[-1][1]


class Image:
    """Premultiplied RGBA floats, composited with `over`."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.px = [(0.0, 0.0, 0.0, 0.0)] * (width * height)

    def over(self, index: int, colour: tuple[float, float, float], alpha: float) -> None:
        if alpha <= 0:
            return
        r, g, b, a = self.px[index]
        keep = 1 - alpha
        self.px[index] = (colour[0] * alpha + r * keep, colour[1] * alpha + g * keep,
                          colour[2] * alpha + b * keep, alpha + a * keep)

    def fill_gradient(self, top: tuple, bottom: tuple) -> None:
        for y in range(self.height):
            colour = tuple(a + (b - a) * y / max(1, self.height - 1) for a, b in zip(top, bottom))
            for x in range(self.width):
                self.px[y * self.width + x] = (*colour, 1.0)

    def paste(self, other: "Image", left: int, top: int, opacity: float = 1.0) -> None:
        for y in range(other.height):
            ty = top + y
            if not 0 <= ty < self.height:
                continue
            for x in range(other.width):
                tx = left + x
                if not 0 <= tx < self.width:
                    continue
                r, g, b, a = other.px[y * other.width + x]
                if a <= 0:
                    continue
                dr, dg, db, da = self.px[ty * self.width + tx]
                k = 1 - a * opacity
                self.px[ty * self.width + tx] = (r * opacity + dr * k, g * opacity + dg * k,
                                                 b * opacity + db * k, a * opacity + da * k)

    def rgba(self) -> bytes:
        out = bytearray()
        for r, g, b, a in self.px:
            if a <= 0:
                out += b"\0\0\0\0"
                continue
            out += bytes((min(255, round(r / a * 255)), min(255, round(g / a * 255)),
                          min(255, round(b / a * 255)), min(255, round(a * 255))))
        return bytes(out)


def glow(value: float, inner: float, outer: float) -> float:
    """The light around a shape at signed distance `value` px: a tight glow and a wide haze."""
    if value <= 0:
        return 0.42 + 0.2
    return 0.42 * math.exp(-(value / inner) ** 2) + 0.2 * math.exp(-(value / outer) ** 2)


def draw_logo(scale: float, mark_only: bool = False, glow_size: float = 1.0) -> Image:
    """The mark over the wordmark (or the mark alone), `scale` px per unit of the mark."""
    word_scale = scale * 0.338  # the capitals are a third of the mark's height
    strokes, word_width = wordmark()
    mx0, my0, mx1, my1 = MARK_BOX
    mark_w, mark_h = (mx1 - mx0) * scale, (my1 - my0) * scale
    word_w, word_h = (word_width + 2 * STROKE) * word_scale, (CAP + 2 * STROKE) * word_scale
    gap = 0.0 if mark_only else 16.0 * scale
    inner, outer = 3.6 * glow_size, 15.0 * glow_size
    pad = math.ceil(outer * 2.6)
    content_w = mark_w if mark_only else max(mark_w, word_w)
    content_h = mark_h if mark_only else mark_h + gap + word_h
    width, height = math.ceil(content_w + 2 * pad), math.ceil(content_h + 2 * pad)
    reach = outer * 2.6

    def mark_xy(x: float, y: float) -> tuple[float, float]:
        return width / 2 + (x - (mx0 + mx1) / 2) * scale, pad + (y - my0) * scale

    mark = Field(width, height)
    back = quadratic(*BACK) + [(BACK[2][0], BACK_BOTTOM), (BACK[0][0], BACK_BOTTOM)]
    for points, rounding in ((back, BACK_ROUNDING), *PANELS):
        mark.polygon([mark_xy(*p) for p in points], reach, rounding * scale)
    for x0, y0, x1, y1, r in LEGS:
        mark.capsule(*mark_xy(x0, y0), *mark_xy(x1, y1), r * scale, reach)
    seams = Field(width, height)
    for x0, y0, x1, y1, r in SEAMS:
        seams.capsule(*mark_xy(x0, y0), *mark_xy(x1, y1), r * scale, 2.0)
    glint = Field(width, height)
    gx, gy, reach_units, waist = GLINT
    glint.polygon([mark_xy(x, y) for x, y in glint_polygon(gx, gy, reach_units, waist)], reach)
    words: list[tuple[Field, tuple]] = []
    if not mark_only:
        left = width / 2 - word_w / 2 + STROKE * word_scale
        top = pad + mark_h + gap + STROKE * word_scale
        for colour in (WORD, BLUE):
            field = Field(width, height)
            for points, stroke_colour in strokes:
                if stroke_colour is colour:
                    field.polyline([(left + x * word_scale, top + y * word_scale) for x, y in points],
                                   STROKE * word_scale, reach)
            words.append((field, colour))

    image = Image(width, height)
    mark_top, mark_bottom = pad, pad + mark_h
    word_top, word_bottom = pad + mark_h + gap, pad + mark_h + gap + word_h
    gloss_bottom = mark_xy(0, GLOSS_BOTTOM)[1]
    for index in range(width * height):
        y = index // width + 0.5
        fields = [mark.d[index], glint.d[index]] + [field.d[index] for field, _ in words]
        nearest = min(fields)
        if nearest == INF:
            continue
        image.over(index, GLOW, glow(nearest, inner, outer) * 0.8)
        cover = coverage(mark.d[index])
        if cover:
            image.over(index, ramp(SILVER, (y - mark_top) / (mark_bottom - mark_top)), cover)
            if y < gloss_bottom:
                image.over(index, (1.0, 1.0, 1.0), cover * 0.55 * (1 - (y - mark_top) / (gloss_bottom - mark_top)))
            image.over(index, INK, coverage(seams.d[index]) * cover * 0.45)
        image.over(index, (1.0, 1.0, 1.0), coverage(glint.d[index]))
        for field, colour in words:
            image.over(index, ramp(colour, (y - word_top) / (word_bottom - word_top)), coverage(field.d[index]))
    return image


def draw_halo(width: int, height: int) -> Image:
    """The light behind the logo: a wide, soft ellipse of the accent blue, lowered to nothing at
    the picture's edges so it has no visible border."""
    image = Image(width, height)
    colour = (0.290, 0.471, 0.816)
    sx, sy = width * 0.29, height * 0.26
    floor = math.exp(-min(width / 2 / sx, height / 2 / sy) ** 2)
    for y in range(height):
        ny = (y + 0.5 - height / 2) / sy
        for x in range(width):
            nx = (x + 0.5 - width / 2) / sx
            image.over(y * width + x, colour, 0.34 * max(0.0, math.exp(-(nx * nx + ny * ny)) - floor) / (1 - floor))
    return image


# (amplitude in px at 1080 lines, waves per screen width, phase) for each ribbon's line
RIBBON_SHAPES = (
    ((40.0, 1, 0.3), (14.0, 2, 1.9), (5.0, 3, 4.0)),
    ((50.0, 1, 2.4), (11.0, 2, 0.7), (4.0, 4, 5.1)),
)
# (core half-width px, core alpha, glow sigma, glow alpha, haze sigma, haze alpha, sheet alpha)
RIBBON_LOOK = ((1.1, 0.95, 4.0, 0.5, 26.0, 0.2, 0.08), (0.8, 0.7, 3.5, 0.38, 20.0, 0.14, 0.0))
RIBBON_HEIGHT = 300
CORE = (0.933, 0.957, 1.0)
INNER = (0.725, 0.824, 1.0)
HAZE = (0.435, 0.612, 0.941)


def draw_ribbon(index: int, width: int = 1920, height: int = RIBBON_HEIGHT, scale: float = 1.0) -> Image:
    """One wave of a glowing ribbon, as wide as the screen and seamless when two are laid side by
    side: a bright core, a tight glow, a wide haze and (the first) a faint sheet trailing under it."""
    image = Image(width, height)
    core, core_alpha, inner, inner_alpha, haze, haze_alpha, sheet = RIBBON_LOOK[index]
    core, inner, haze = core * scale, inner * scale, haze * scale
    middle = height / 2
    for x in range(width):
        u = (x + 0.5) / width
        centre, slope = middle, 0.0
        for amplitude, waves, phase in RIBBON_SHAPES[index]:
            angle = 2 * math.pi * waves * u + phase
            centre += amplitude * scale * math.sin(angle)
            slope += amplitude * scale * 2 * math.pi * waves / width * math.cos(angle)
        across = 1 / math.sqrt(1 + slope * slope)
        for y in range(height):
            offset = (y + 0.5 - centre) * across
            distance = abs(offset)
            alpha = haze_alpha * math.exp(-(distance / haze) ** 2)
            if sheet and offset > 0:
                alpha += sheet * math.exp(-offset / (60 * scale))
            at = y * width + x
            image.over(at, HAZE, alpha)
            image.over(at, INNER, inner_alpha * math.exp(-(distance / inner) ** 2))
            image.over(at, CORE, core_alpha * coverage(distance - core))
    return image


def draw_dot(size: int = 28) -> Image:
    image = Image(size, size)
    for y in range(size):
        for x in range(size):
            distance = math.hypot(x + 0.5 - size / 2, y + 0.5 - size / 2)
            image.over(y * size + x, INNER, 0.6 * math.exp(-((distance - 2.0) / 4.0) ** 2) if distance > 2 else 0.6)
            image.over(y * size + x, (1.0, 1.0, 1.0), coverage(distance - 3.2))
    return image


def draw_banner(logo: Image, width: int = 1280, height: int = 400) -> Image:
    """The README's banner: the boot screen in small (gradient, ribbons, light, logo)."""
    banner = Image(width, height)
    banner.fill_gradient(boot.rgb(boot.TOP), boot.rgb(boot.BOTTOM))
    scale = height / 640
    for index in range(len(boot.RIBBONS)):
        ribbon = draw_ribbon(index, width, round(RIBBON_HEIGHT * scale), scale)
        banner.paste(ribbon, 0, round(height * 0.74 - ribbon.height / 2) + index * 6)
    halo = draw_halo(round(900 * scale * 1.2), round(420 * scale * 1.2))
    banner.paste(halo, (width - halo.width) // 2, round(height * 0.44 - halo.height / 2))
    banner.paste(logo, (width - logo.width) // 2, round(height * 0.44 - logo.height / 2))
    return banner


def draw_icon(size: int = 256) -> Image:
    """The mark on a glossy rounded tile in the boot gradient, like the XMB's full tile icons."""
    icon = Image(size, size)
    inset, radius = size * 0.06, size * 0.2
    near, far = inset + radius, size - inset - radius
    tile = Field(size, size)
    tile.polygon([(near, near), (far, near), (far, far), (near, far)], 1.0, radius)
    top, bottom = boot.rgb(boot.TOP), boot.rgb(boot.BOTTOM)
    for index in range(size * size):
        cover = coverage(tile.d[index])
        if not cover:
            continue
        k = (index // size + 0.5 - inset) / (size - 2 * inset)
        icon.over(index, tuple(a + (b - a) * k for a, b in zip(top, bottom)), cover)
        if k < 0.5:
            icon.over(index, (1.0, 1.0, 1.0), cover * 0.16 * (1 - 2 * k))
        icon.over(index, (0.75, 0.84, 1.0), cover * 0.5 * (1 - k) * coverage(-tile.d[index] - 1.5))
    mark = draw_logo(size / 150, mark_only=True, glow_size=0.6)
    icon.paste(mark, (size - mark.width) // 2, (size - mark.height) // 2 + round(size * 0.02))
    return icon


# ---------------------------------------------------------------------- the Plymouth theme


def number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def theme_file() -> str:
    assets = boot.ASSETS.as_posix()
    return (
        "[Plymouth Theme]\n"
        "Name=CouchLiteOS\n"
        "Description=The CouchLiteOS logo on a dark blue gradient, with a slow glowing wave\n"
        "ModuleName=script\n"
        "\n"
        "[script]\n"
        f"ImageDir={assets}\n"
        f"ScriptFile={assets}/couchliteos.script\n"
    )


def script() -> str:
    """couchliteos.script: couchliteos_boot.frame() in Plymouth's script language."""
    top, bottom = boot.rgb(boot.TOP), boot.rgb(boot.BOTTOM)
    ribbons = "".join(
        f'ribbon[{i}].image = Image("{name}");\n'
        f"ribbon[{i}].image = ribbon[{i}].image.Scale(screen.width, Math.Int(ribbon[{i}].image.GetHeight() * scale + 0.5));\n"
        f"ribbon[{i}].a = Sprite(ribbon[{i}].image);\n"
        f"ribbon[{i}].b = Sprite(ribbon[{i}].image);\n"
        f"ribbon[{i}].y = screen.y + screen.height * {number(boot.RIBBON_Y[i])} - ribbon[{i}].image.GetHeight() / 2;\n"
        f"ribbon[{i}].seconds = {number(boot.RIBBON_SECONDS[i])};\n"
        f"ribbon[{i}].a.SetPosition(screen.x, ribbon[{i}].y, 1);\n"
        f"ribbon[{i}].b.SetPosition(screen.x + screen.width, ribbon[{i}].y, 1);\n"
        for i, name in enumerate(boot.RIBBONS))
    return f"""\
# The CouchLiteOS boot splash. Written by tools/make-logo.py from launcher/couchliteos_boot.py,
# which couchliteos-tv's loading screen draws too: change them there, not here.
# No text (plymouth-label is not installed): Esc shows the boot messages (Plymouth's details
# view), and the units that print (the restore, a failed screen) quit the splash first.

Window.SetBackgroundTopColor({number(top[0])}, {number(top[1])}, {number(top[2])});
Window.SetBackgroundBottomColor({number(bottom[0])}, {number(bottom[1])}, {number(bottom[2])});

screen.width = Window.GetWidth();
screen.height = Window.GetHeight();
screen.x = Window.GetX();
screen.y = Window.GetY();
scale = screen.height / {boot.REFERENCE_HEIGHT};

fun scaled(image) {{
  if (scale == 1) {{
    return image;
  }}
  return image.Scale(Math.Int(image.GetWidth() * scale + 0.5), Math.Int(image.GetHeight() * scale + 0.5));
}}

fun centre(sprite, image, x, y, z) {{
  sprite.SetPosition(x - image.GetWidth() / 2, y - image.GetHeight() / 2, z);
}}

{ribbons}
halo.image = scaled(Image("halo.png"));
halo.sprite = Sprite(halo.image);
centre(halo.sprite, halo.image, screen.x + screen.width / 2, screen.y + screen.height * {number(boot.LOGO_Y)}, 2);
halo.sprite.SetOpacity({number(boot.HALO_LOW)});

logo.image = scaled(Image("logo.png"));
logo.sprite = Sprite(logo.image);
centre(logo.sprite, logo.image, screen.x + screen.width / 2, screen.y + screen.height * {number(boot.LOGO_Y)}, 3);

dot_image = scaled(Image("dot.png"));
i = 0;
while (i < {boot.DOTS}) {{
  dot[i].sprite = Sprite(dot_image);
  centre(dot[i].sprite, dot_image, screen.x + screen.width / 2 + (i - {number((boot.DOTS - 1) / 2)}) * {number(boot.DOT_GAP)} * screen.height,
         screen.y + screen.height * {number(boot.DOTS_Y)}, 3);
  dot[i].sprite.SetOpacity({number(boot.DOT_LOW)});
  i = i + 1;
}}

frame = 0;

fun refresh_callback() {{
  global.frame = global.frame + 1;
  t = global.frame / {boot.FPS};
  i = 0;
  while (i < {len(boot.RIBBONS)}) {{
    shift = t / ribbon[i].seconds;
    shift = shift - Math.Int(shift);
    x = screen.x - shift * screen.width;
    ribbon[i].a.SetX(x);
    ribbon[i].b.SetX(x + screen.width);
    i = i + 1;
  }}
  halo.sprite.SetOpacity({number(boot.HALO_LOW)} + {number(1 - boot.HALO_LOW)} * (0.5 - 0.5 * Math.Cos(2 * Math.Pi * t / {number(boot.HALO_SECONDS)})));
  i = 0;
  while (i < {boot.DOTS}) {{
    pulse = 0.5 + 0.5 * Math.Cos(2 * Math.Pi * (t - i * {number(boot.DOT_STAGGER)}) / {number(boot.DOT_SECONDS)});
    dot[i].sprite.SetOpacity({number(boot.DOT_LOW)} + {number(1 - boot.DOT_LOW)} * pulse * pulse * pulse);
    i = i + 1;
  }}
}}

Plymouth.SetRefreshFunction(refresh_callback);
"""


# ---------------------------------------------------------------------- PNG


def png_bytes(image: Image) -> bytes:
    """An 8-bit RGBA PNG, every row Sub-filtered (smooth gradients pack well that way)."""
    data = image.rgba()
    stride = image.width * 4
    raw = bytearray()
    for y in range(image.height):
        row = data[y * stride:(y + 1) * stride]
        raw.append(1)
        raw += row[:4]
        raw += bytes((row[i] - row[i - 4]) & 0xFF for i in range(4, stride))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", image.width, image.height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def png_pixels(data: bytes) -> tuple[int, int, bytes]:
    """Width, height and RGBA bytes of an 8-bit RGBA PNG (any row filters)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, width, height = 8, b"", 0, 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            if (depth, colour) != (8, 6):
                raise ValueError("not 8-bit RGBA")
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4
    out = bytearray()
    previous = bytearray(stride)
    for y in range(height):
        kind = raw[y * (stride + 1)]
        row = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            left = row[i - 4] if i >= 4 else 0
            up = previous[i]
            corner = previous[i - 4] if i >= 4 else 0
            if kind == 1:
                row[i] = (row[i] + left) & 0xFF
            elif kind == 2:
                row[i] = (row[i] + up) & 0xFF
            elif kind == 3:
                row[i] = (row[i] + (left + up) // 2) & 0xFF
            elif kind == 4:
                p = left + up - corner
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
                row[i] = (row[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 0xFF
        out += row
        previous = row
    return width, height, bytes(out)


# ---------------------------------------------------------------------- main


def outputs() -> dict[pathlib.Path, bytes | Image]:
    logo = draw_logo(1.8)
    return {
        THEME_DIR / "logo.png": logo,
        THEME_DIR / "halo.png": draw_halo(900, 420),
        THEME_DIR / "ribbon-1.png": draw_ribbon(0),
        THEME_DIR / "ribbon-2.png": draw_ribbon(1),
        THEME_DIR / "dot.png": draw_dot(),
        THEME_DIR / "couchliteos.plymouth": theme_file().encode(),
        THEME_DIR / "couchliteos.script": script().encode(),
        IMAGES / "couchliteos-logo.png": draw_banner(logo),
        IMAGES / "couchliteos-icon.png": draw_icon(),
    }


def same(path: pathlib.Path, made: bytes | Image) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if isinstance(made, bytes):
        return data == made
    try:
        width, height, pixels = png_pixels(data)
    except (ValueError, zlib.error, struct.error):
        return False
    expected = made.rgba()
    return (width, height) == (made.width, made.height) and all(
        abs(a - b) <= 1 for a, b in zip(pixels, expected))


def main(argv: list[str]) -> int:
    check = argv[1:] == ["--check"]
    if argv[1:] and not check:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    stale = []
    for path, made in outputs().items():
        name = path.relative_to(ROOT)
        if check:
            if not same(path, made):
                stale.append(str(name))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(made if isinstance(made, bytes) else png_bytes(made))
        print(name)
    if stale:
        print("make-logo: out of date (run tools/make-logo.py): " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
