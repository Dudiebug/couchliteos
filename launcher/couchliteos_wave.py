"""The XMB background: a soft gradient with slow, glowing ribbons, as plain Python.

couchliteos_gtk_wave draws it. With the GL renderer it runs FRAGMENT (built here from the same
WAVES the Python uses) in a Gtk.GLArea at up to FPS frames a second; with the software (cairo)
renderer, with MOTION OFF, or when the GL wave failed before, it paints one still frame from
ribbon_points(); with the HIGH CONTRAST theme it is the flat background colour. It stops while an
application or a stream is in front or the screen is blank: a still screen costs nothing.

Colours come from the theme: the background, tinted with the accent towards the top, a little
brighter by day and dimmer at night (as the console's wave follows the clock). The tint is held
back until the theme's text keeps MIN_CONTRAST against the brightest part of the gradient.

Between the first two ribbons hangs a sheet of light, brighter at its edges, that twists to a line
where they cross: the console's silk. Along it ride the sparkles: tiny lights that flow with the
wave and glint, and fade out a little above and below it. Each LAYER is a grid of cells that
follows the main ribbon up and down and drifts along it, with at most one sparkle in a cell,
whose glow stays inside it, so a pixel only looks at its own cell in each layer: a few dozen sums
a pixel, nothing an old GPU notices.
"""

from __future__ import annotations

import dataclasses
import math

import couchliteos_motion as motion
import couchliteos_theme as theme

GL, STATIC, FLAT = "gl", "static", "flat"
FPS = 30
FLAT_THEMES = {"high-contrast"}
STILL_TIME = 37.0  # s: the moment a still frame shows (the ribbons are nicely apart there)

# Each ribbon's height (0 = top, 1 = bottom) is BASE + its offset + a sum of sines:
# amplitude * sin(x * frequency + time * speed + ribbon * phase), x from 0 to 1 across the screen.
BASE = 0.60
RIBBON_OFFSETS = (0.0, 0.035, -0.03)
WAVES = ((0.060, 2.1, 0.21, 1.7), (0.035, 4.7, -0.33, 0.9), (0.015, 9.3, 0.50, 1.3))
WIDTHS = (0.010, 0.016, 0.022)  # each ribbon's bright core, in screen heights
HAZE = 0.09  # the soft glow around a ribbon
RIBBON_ALPHA = (0.55, 0.35, 0.25)
SHEET_ALPHA = 0.3  # the sheet between ribbons 0 and 1, at its edges (0.35 of that in its middle)
SHEET_EDGE = 0.003  # its soft edge, in screen heights
TAU = 2 * math.pi
# Every ribbon's core and haze, and the sheet's edge, on one pixel: the brightest it gets.
MAX_GLOW = sum(RIBBON_ALPHA) * 1.35 + SHEET_ALPHA
LARGE_CONTRAST = 3.0  # WCAG AA for large text: the XMB's labels crossing a ribbon


@dataclasses.dataclass(frozen=True)
class Layer:
    """One layer of sparkles; lengths in screen heights, x along the screen, y from the main ribbon."""
    cell: float  # the grid's cell size
    density: float  # the share of cells with a sparkle
    size: float  # the bright core's radius
    drift: tuple[float, float]  # how far the grid moves a second
    brightness: float


# Far (small, slow, dim) and near (larger, faster): the near ones pass the far ones, as on the console.
LAYERS = (Layer(0.035, 0.45, 0.0016, (0.022, 0.0), 1.1), Layer(0.07, 0.55, 0.003, (0.035, 0.0), 1.7))
SPARKLE_MARGIN = 0.3  # of a cell: where a sparkle's centre may sit, before it wanders
SPARKLE_WANDER = 0.1  # of a cell: how far it wanders; its glow ends inside the cell (MARGIN - WANDER)
SPARKLE_BAND = 0.06  # sparkles live within about this of the main ribbon
SPARKLE_REACH = 3 * SPARKLE_BAND  # and none at all beyond this
SPARKLE_GLINT = 0.3  # a sparkle never goes quite dark: it glints up from this
SPARKLE_WHITE = 0.6  # a sparkle is the ribbon colour this far towards white


@dataclasses.dataclass(frozen=True)
class Palette:
    top: tuple[float, float, float]  # 0..1 RGB
    bottom: tuple[float, float, float]
    ribbon: tuple[float, float, float]
    alpha: float  # how strongly the ribbons show
    sparkle: float = 0.0  # how strongly the sparkles show (0: none)

    def hex(self, part: str) -> str:
        """"top", "bottom" or "ribbon" as rrggbb."""
        return _hex(getattr(self, part))


def mode(renderer: str | None, level: str, theme_name: str, failed_before: bool = False) -> str:
    """How to draw the wave: GL (animated), STATIC (one cairo frame) or FLAT."""
    if theme_name in FLAT_THEMES:
        return FLAT
    if failed_before or level == motion.OFF or (renderer or "").strip().lower() == "cairo":
        return STATIC
    return GL


def _rgb(colour: str) -> tuple[float, float, float]:
    colour = theme.parse_colour(colour)
    return tuple(int(colour[index:index + 2], 16) / 255 for index in (0, 2, 4))  # type: ignore[return-value]


def _hex(colour: tuple[float, ...]) -> str:
    return "".join(f"{round(min(1.0, max(0.0, channel)) * 255):02x}" for channel in colour)


def _mix(a: tuple[float, ...], b: tuple[float, ...], amount: float) -> tuple[float, float, float]:
    return tuple(x + (y - x) * amount for x, y in zip(a, b))  # type: ignore[return-value]


def daylight(hour: float) -> float:
    """0.0 in the middle of the night (3 o'clock) to 1.0 in the early afternoon (15 o'clock)."""
    return 0.5 - 0.5 * math.cos(TAU * ((hour - 3) % 24) / 24)


def palette(colours: theme.Theme, hour: float = 12.0) -> Palette:
    background = _rgb(colours.colours["background"])
    accent = _rgb(colours.colours["accent"])
    text = colours.colours["text"]
    light_theme = theme._luminance(theme.parse_colour(colours.colours["background"])) > 0.4
    day = daylight(hour)
    tint = (0.10 + 0.08 * day) if light_theme else (0.16 + 0.14 * day)
    while True:
        top = _mix(background, accent, tint)
        if light_theme:
            bottom = _mix(background, (1.0, 1.0, 1.0), 0.25 * day)
        else:
            bottom = _mix(background, (0.0, 0.0, 0.0), 0.35 * (1 - day))
        worst = min(theme.contrast(text, _hex(colour)) for colour in (top, bottom))
        if worst >= theme.MIN_CONTRAST or tint < 0.01:
            break
        tint *= 0.75
    ribbon = _mix(accent, (1.0, 1.0, 1.0), 0.15 if light_theme else 0.5)
    alpha = (0.22 if light_theme else 0.30) * (0.8 + 0.2 * day)
    while alpha > 0.02 and min(theme.contrast(text, _hex(_mix(colour, ribbon, min(1.0, MAX_GLOW * alpha))))
                               for colour in (top, bottom)) < LARGE_CONTRAST:
        alpha *= 0.85
    # The sparkles are dots a few pixels wide, smaller than a letter's stroke: they are left out of
    # the contrast sums, and fainter on a light theme, where white on white would only look grey.
    sparkle = (0.5 if light_theme else 0.9) * (0.8 + 0.2 * day)
    return Palette(top, bottom, ribbon, round(alpha, 3), round(sparkle, 3))


def ribbon_y(x: float, time: float, ribbon: int) -> float:
    """Ribbon `ribbon`'s height at `x` (both 0..1 of the screen) at `time` seconds."""
    y = BASE + RIBBON_OFFSETS[ribbon]
    for amplitude, frequency, speed, phase in WAVES:
        y += amplitude * math.sin(x * frequency + time * speed + ribbon * phase)
    return y


def _fract(value: float) -> float:
    return value - math.floor(value)


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    t = min(1.0, max(0.0, (value - edge0) / (edge1 - edge0)))
    return t * t * (3 - 2 * t)


def cell_hash(x: float, y: float) -> float:
    """0..1 for a cell (the shader's hash(): "hash without sine", which stays exact in GLSL floats)."""
    a, b, c = _fract(x * 0.1031), _fract(y * 0.1031), _fract(x * 0.1031)
    dot = a * (b + 33.33) + b * (c + 33.33) + c * (a + 33.33)
    a, b, c = a + dot, b + dot, c + dot
    return _fract((a + b) * c)


def sparkle(x: float, y: float, time: float, aspect: float, layer: Layer, wave_y: float,
            min_size: float = 0.0) -> float:
    """How bright `layer`'s sparkle is at (x, y) (0..1 of the screen, y down) on a screen `aspect`
    wide for 1 high, with the main ribbon at `wave_y` there; 0 almost everywhere. `min_size` keeps
    a dot at least a pixel wide in the small still frame."""
    above = y - wave_y
    if abs(above) > SPARKLE_REACH:
        return 0.0
    gx = (x * aspect - layer.drift[0] * time) / layer.cell
    gy = (above - layer.drift[1] * time) / layer.cell
    cx, cy = math.floor(gx), math.floor(gy)
    if cell_hash(cx, cy) >= layer.density:
        return 0.0
    middle_x, middle_y, twinkle = _sparkle_in(cx, cy, time)
    d = math.hypot(gx - cx - middle_x, gy - cy - middle_y) * layer.cell
    size = max(layer.size, min_size)
    glow = math.exp(-(d * d) / (size * size)) + 0.3 * math.exp(-(d * d) / (9 * size * size))
    reach = (SPARKLE_MARGIN - SPARKLE_WANDER) * layer.cell
    band = math.exp(-(above * above) / (SPARKLE_BAND * SPARKLE_BAND))
    return layer.brightness * twinkle * glow * (1 - _smoothstep(0.5 * reach, reach, d)) * band


def _sparkle_in(cx: int, cy: int, time: float) -> tuple[float, float, float]:
    """Where in its cell (0..1) the sparkle of cell (cx, cy) is at `time`, and how bright it glints
    (SPARKLE_GLINT..1)."""
    r2, r3 = cell_hash(cx + 17.0, cy + 5.0), cell_hash(cx + 3.0, cy + 29.0)
    spread = 1 - 2 * SPARKLE_MARGIN
    middle_x = SPARKLE_MARGIN + spread * r2 + SPARKLE_WANDER * math.sin(time * 0.7 * (0.5 + r3) + r2 * TAU)
    middle_y = SPARKLE_MARGIN + spread * r3 + SPARKLE_WANDER * math.sin(time * 0.5 * (0.5 + r2) + r3 * TAU)
    glint = max(0.0, math.sin(time * (1.2 + 2.4 * r2) + r3 * TAU))
    return middle_x, middle_y, SPARKLE_GLINT + (1 - SPARKLE_GLINT) * glint * glint


def sparkle_field(width: int, height: int, time: float, wave_ys: list[float], min_size: float = 0.0) -> dict[int, float]:
    """Every layer's sparkle() summed, by pixel index (y * width + x), for the pixels it is not 0 at:
    only the few pixels around each sparkle are worked out (a still frame in a blink, not seconds
    on an old CPU). `wave_ys` is the main ribbon's height at each column."""
    aspect = width / height
    field: dict[int, float] = {}
    for layer in LAYERS:
        cell, (drift_x, drift_y) = layer.cell, layer.drift
        reach = (SPARKLE_MARGIN - SPARKLE_WANDER) * cell
        tall = reach * 1.6 + 1 / height  # the grid leans with the wave: a dot's pixels reach a little further
        left, top = drift_x * time, drift_y * time  # where cell (0, 0) is: across, and from the ribbon
        for cx in range(math.floor(-left / cell), math.floor((aspect - left) / cell) + 1):
            for cy in range(math.floor((-SPARKLE_REACH - top) / cell), math.floor((SPARKLE_REACH - top) / cell) + 1):
                if cell_hash(cx, cy) >= layer.density:
                    continue
                middle_x, middle_y, _glint = _sparkle_in(cx, cy, time)
                sx = left + (cx + middle_x) * cell  # screen heights across
                sy = ribbon_y(min(1.0, max(0.0, sx / aspect)), time, 0) + top + (cy + middle_y) * cell
                for py in range(max(0, math.floor((sy - tall) * height)), min(height, math.ceil((sy + tall) * height) + 1)):
                    y = (py + 0.5) / height
                    for px in range(max(0, math.floor((sx - reach) / aspect * width)),
                                    min(width, math.ceil((sx + reach) / aspect * width) + 1)):
                        x = (px + 0.5) / width
                        if (math.floor((x * aspect - left) / cell), math.floor((y - wave_ys[px] - top) / cell)) != (cx, cy):
                            continue  # a neighbouring cell's pixel: its own sparkle, if any, counts it
                        value = sparkle(x, y, time, aspect, layer, wave_ys[px], min_size)
                        if value > 0:
                            field[py * width + px] = field.get(py * width + px, 0.0) + value
    return field


def ribbon_points(width: int, height: int, time: float = STILL_TIME, ribbon: int = 0,
                  segments: int = 64) -> list[tuple[float, float]]:
    """The ribbon as `segments` + 1 points in pixels, for the still (cairo) frame."""
    return [(width * i / segments, height * ribbon_y(i / segments, time, ribbon)) for i in range(segments + 1)]


def _float(value: float) -> str:
    text = f"{value:.6f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def _ribbon_glsl() -> str:
    terms = " + ".join(
        f"{_float(a)} * sin(x * {_float(f)} + t * {_float(s)} + k * {_float(p)})" for a, f, s, p in WAVES)
    offsets = ", ".join(_float(o) for o in RIBBON_OFFSETS)
    return (
        f"const float OFFSETS[{len(RIBBON_OFFSETS)}] = float[]({offsets});\n"
        f"float ribbon_y(float x, float t, int i) {{\n"
        f"  float k = float(i);\n"
        f"  return {_float(BASE)} + OFFSETS[i] + {terms};\n"
        f"}}\n"
    )


def _sparkle_glsl() -> str:
    """sparkle() and cell_hash() as GLSL, from the same constants."""
    margin, wander = _float(SPARKLE_MARGIN), _float(SPARKLE_WANDER)
    return (
        "float cell_hash(vec2 p) {\n"
        "  vec3 h = fract(vec3(p.xyx) * 0.1031);\n"
        "  h += dot(h, h.yzx + 33.33);\n"
        "  return fract((h.x + h.y) * h.z);\n"
        "}\n"
        "float sparkle(vec2 uv, float t, float aspect, float wave_y, float cell, float density, float size,"
        " vec2 drift, float brightness) {\n"
        "  float above = uv.y - wave_y;\n"
        f"  if (abs(above) > {_float(SPARKLE_REACH)}) return 0.0;\n"
        "  vec2 g = (vec2(uv.x * aspect, above) - drift * t) / cell;\n"
        "  vec2 id = floor(g);\n"
        "  if (cell_hash(id) >= density) return 0.0;\n"
        "  float r2 = cell_hash(id + vec2(17.0, 5.0));\n"
        "  float r3 = cell_hash(id + vec2(3.0, 29.0));\n"
        f"  float spread = 1.0 - 2.0 * {margin};\n"
        f"  vec2 middle = vec2({margin} + spread * r2 + {wander} * sin(t * 0.7 * (0.5 + r3) + r2 * {_float(TAU)}),\n"
        f"                     {margin} + spread * r3 + {wander} * sin(t * 0.5 * (0.5 + r2) + r3 * {_float(TAU)}));\n"
        "  float d = length(g - id - middle) * cell;\n"
        f"  float glint = max(0.0, sin(t * (1.2 + 2.4 * r2) + r3 * {_float(TAU)}));\n"
        f"  float twinkle = {_float(SPARKLE_GLINT)} + {_float(1 - SPARKLE_GLINT)} * glint * glint;\n"
        "  float glow = exp(-(d * d) / (size * size)) + 0.3 * exp(-(d * d) / (9.0 * size * size));\n"
        f"  float reach = ({margin} - {wander}) * cell;\n"
        f"  float band = exp(-(above * above) / {_float(SPARKLE_BAND * SPARKLE_BAND)});\n"
        "  return brightness * twinkle * glow * (1.0 - smoothstep(0.5 * reach, reach, d)) * band;\n"
        "}\n"
    )


def _sheet_glsl() -> str:
    edge = _float(SHEET_EDGE)
    return (
        "float sheet(float y, float lo, float hi) {\n"
        "  float s = clamp((y - lo) / max(hi - lo, 0.0001), 0.0, 1.0);\n"
        f"  float inside = smoothstep(lo - {edge}, lo + {edge}, y) * (1.0 - smoothstep(hi - {edge}, hi + {edge}, y));\n"
        f"  return {_float(SHEET_ALPHA)} * inside * (0.35 + 0.65 * pow(abs(2.0 * s - 1.0), 3.0));\n"
        "}\n"
    )


def sheet(y: float, lo: float, hi: float) -> float:
    """The sheet's glow at height `y` between ribbons at `lo` and `hi` (lo <= hi): brighter at its
    edges, as light catches the folds of silk."""
    s = min(1.0, max(0.0, (y - lo) / max(hi - lo, 0.0001)))
    inside = _smoothstep(lo - SHEET_EDGE, lo + SHEET_EDGE, y) * (1 - _smoothstep(hi - SHEET_EDGE, hi + SHEET_EDGE, y))
    return SHEET_ALPHA * inside * (0.35 + 0.65 * abs(2 * s - 1) ** 3)


def _layer_call(layer: Layer) -> str:
    return (f"sparkle(uv, u_time, aspect, wave_y, {_float(layer.cell)}, {_float(layer.density)}, "
            f"{_float(layer.size)}, vec2({_float(layer.drift[0])}, {_float(layer.drift[1])}), "
            f"{_float(layer.brightness)})")


def fragment(version: str = "330 core") -> str:
    """The wave's fragment shader. `version` is "330 core" (desktop GL) or "300 es" (GLES).
    GLES gets highp: hours of u_time in mediump would make the waves and the sparkles stutter."""
    widths = ", ".join(_float(w) for w in WIDTHS)
    alphas = ", ".join(_float(a) for a in RIBBON_ALPHA)
    precision = "precision highp float;\n" if version.endswith("es") else ""
    sparkles = " + ".join(_layer_call(layer) for layer in LAYERS)
    return (
        f"#version {version}\n{precision}"
        "uniform vec2 u_size;\nuniform float u_time;\nuniform vec3 u_top;\nuniform vec3 u_bottom;\n"
        "uniform vec3 u_ribbon;\nuniform float u_alpha;\nuniform float u_sparkle;\nuniform float u_fade;\n"
        "out vec4 colour;\n"
        + _ribbon_glsl()
        + _sparkle_glsl()
        + _sheet_glsl()
        + f"const float WIDTHS[{len(WIDTHS)}] = float[]({widths});\n"
        + f"const float ALPHAS[{len(RIBBON_ALPHA)}] = float[]({alphas});\n"
        + "void main() {\n"
        "  vec2 uv = vec2(gl_FragCoord.x / u_size.x, 1.0 - gl_FragCoord.y / u_size.y);\n"
        "  vec3 c = mix(u_top, u_bottom, smoothstep(0.0, 1.0, uv.y * 0.85 + uv.x * 0.15));\n"
        "  float glow = 0.0;\n"
        "  float wave_y = ribbon_y(uv.x, u_time, 0);\n"
        f"  for (int i = 0; i < {len(WIDTHS)}; i++) {{\n"
        "    float d = abs(uv.y - ribbon_y(uv.x, u_time, i));\n"
        "    float core = exp(-(d * d) / (WIDTHS[i] * WIDTHS[i]));\n"
        f"    float haze = exp(-d / {_float(HAZE)}) * 0.35;\n"
        "    glow += ALPHAS[i] * (core + haze);\n"
        "  }\n"
        "  float y1 = ribbon_y(uv.x, u_time, 1);\n"
        "  glow += sheet(uv.y, min(wave_y, y1), max(wave_y, y1));\n"
        "  c = mix(c, u_ribbon, clamp(glow * u_alpha * u_fade, 0.0, 1.0));\n"
        "  if (u_sparkle > 0.0) {\n"
        "    float aspect = u_size.x / u_size.y;\n"
        f"    float s = {sparkles};\n"
        f"    c = mix(c, mix(u_ribbon, vec3(1.0), {_float(SPARKLE_WHITE)}), clamp(s * u_sparkle * u_fade, 0.0, 1.0));\n"
        "  }\n"
        "  colour = vec4(c, 1.0);\n"
        "}\n"
    )


def vertex(version: str = "330 core") -> str:
    """A full-screen triangle; no vertex buffer needed (gl_VertexID)."""
    return (
        f"#version {version}\n"
        "void main() {\n"
        "  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));\n"
        "  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);\n"
        "}\n"
    )


class Clock:
    """Wave time that only moves while the wave is shown, so it never jumps after a pause."""

    def __init__(self, start: float = STILL_TIME) -> None:
        self.time = start
        self.last: float | None = None

    def frame(self, now: float) -> float:
        if self.last is not None:
            self.time += min(now - self.last, 0.1)  # a stall is a pause, not a jump
        self.last = now
        return self.time

    def pause(self) -> None:
        self.last = None

    def due(self, now: float) -> bool:
        """At most FPS frames a second."""
        return self.last is None or now - self.last >= 1 / FPS - 0.002


STILL_SIZE = (320, 180)  # the still frame is made this small and stretched: the wave is all soft edges


def still_frame(width: int, height: int, colours: Palette, time: float = STILL_TIME) -> bytes:
    """One frame of FRAGMENT's picture as RGB bytes, for the software renderer, MOTION OFF and the
    moment before the GL wave's first frame. A palette with alpha and sparkle 0 is the gradient
    alone (FLAT)."""
    columns = []
    for px in range(width):
        x = (px + 0.5) / width
        columns.append((x, [ribbon_y(x, time, ribbon) for ribbon in range(len(WIDTHS))]))
    white = _mix(colours.ribbon, (1.0, 1.0, 1.0), SPARKLE_WHITE)
    field = {}
    if colours.sparkle > 0:  # each dot at least a pixel wide, or the small frame would lose most of them
        field = sparkle_field(width, height, time, [ribbons[0] for _x, ribbons in columns], 0.8 / height)
    out = bytearray(width * height * 3)
    at = 0
    for py in range(height):
        y = (py + 0.5) / height
        for x, ribbons in columns:
            t = min(1.0, max(0.0, y * 0.85 + x * 0.15))
            t = t * t * (3 - 2 * t)  # smoothstep
            glow = 0.0
            if colours.alpha > 0:
                for ribbon, middle in enumerate(ribbons):
                    d = abs(y - middle)
                    glow += RIBBON_ALPHA[ribbon] * (math.exp(-(d * d) / (WIDTHS[ribbon] ** 2)) + math.exp(-d / HAZE) * 0.35)
                lo, hi = min(ribbons[0], ribbons[1]), max(ribbons[0], ribbons[1])
                if lo - SHEET_EDGE < y < hi + SHEET_EDGE:  # sheet() is 0 outside: skip the sums
                    glow += sheet(y, lo, hi)
            amount = min(1.0, max(0.0, glow * colours.alpha))
            shine = min(1.0, max(0.0, field.get(at // 3, 0.0) * colours.sparkle))
            for channel in range(3):
                base = colours.top[channel] + (colours.bottom[channel] - colours.top[channel]) * t
                base += (colours.ribbon[channel] - base) * amount
                out[at + channel] = round(255 * (base + (white[channel] - base) * shine))
            at += 3
    return bytes(out)
