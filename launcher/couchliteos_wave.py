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
where they cross: the console's silk. Around it float the sparkles, at several depths seen in
perspective: far away tiny sharp specks that crawl, near the wave crisp glints, and close to the
eye soft out-of-focus discs that sweep past, lower and swinging further with the swell. Each LAYER
is a grid of cells that follows its own lane (the swell at that depth) and drifts along it, with at
most one sparkle in a cell, whose light stays inside it, so a pixel only looks at its own cell in
each layer: a few dozen sums a pixel, nothing an old GPU notices.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import os
import pathlib
import tempfile
import zlib

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
    """Sparkles at one depth. Lengths in screen heights. `scale` is the perspective: 1 at the wave's
    own depth, smaller further away; the nearer, the larger, faster and wider spread everything is."""
    scale: float
    cell: float  # the grid's cell size: at most one sparkle a cell
    density: float  # the share of cells with a sparkle
    size: float  # a sharp sparkle's bright core
    blur: float  # an out-of-focus one's disc of light, its radius (0: sharp)
    drift: float  # how far it moves along the screen a second
    brightness: float
    twinkle: float  # how much it glints (1) or only gently breathes (less)
    seed: float  # so no two layers have the same pattern


# Far to near. The ones at the wave (scale 1) are in focus; nearer, they blur into discs.
LAYERS = (
    Layer(0.5, 0.03, 0.4, 0.0013, 0.0, 0.009, 0.8, 1.0, 0.0),
    Layer(0.75, 0.042, 0.35, 0.0019, 0.0, 0.014, 1.1, 1.0, 101.0),
    Layer(1.0, 0.06, 0.3, 0.003, 0.0, 0.02, 1.7, 1.0, 211.0),
    Layer(1.5, 0.12, 0.3, 0.0, 0.012, 0.032, 0.55, 0.4, 307.0),
    Layer(2.3, 0.3, 0.35, 0.0, 0.031, 0.05, 0.35, 0.3, 401.0),
)
SHARP = tuple(layer for layer in LAYERS if layer.blur == 0)  # drawn towards white
SOFT = tuple(layer for layer in LAYERS if layer.blur > 0)  # drawn in the ribbon's colour, as out-of-focus light is
HORIZON = 0.45  # where the lanes would meet, infinitely far away
SPARKLE_BAND = 0.06  # at the wave's depth, sparkles live within about this of their lane (scaled by depth)
SPARKLE_MARGIN = 0.3  # of a cell: where a sparkle's centre may sit, before it wanders
SPARKLE_WANDER = 0.1  # of a cell: how far it wanders
# A sparkle's light ends this far (of a cell) from its centre: inside its cell, even where its lane
# slopes (by at most the swell's amplitude * frequency per screen height, on a screen at least as
# wide as high).
SPARKLE_CLEAR = (SPARKLE_MARGIN - SPARKLE_WANDER) / (1 + WAVES[0][0] * WAVES[0][1])
SPARKLE_GLINT = 0.3  # a sparkle never goes quite dark: it glints up from this
SPARKLE_WHITE = 0.6  # a sharp sparkle is the ribbon colour this far towards white


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


def lane_y(x: float, time: float, scale: float) -> float:
    """The middle of the sparkles' band at `x` (0..1 of the screen) for a layer `scale` deep: the
    wave's big swell (its first sine) seen in perspective."""
    amplitude, frequency, speed, _phase = WAVES[0]
    far_x = 0.5 + (x - 0.5) / scale
    return HORIZON + (BASE + amplitude * math.sin(far_x * frequency + time * speed) - HORIZON) * scale


def sparkle(x: float, y: float, time: float, aspect: float, layer: Layer, min_size: float = 0.0) -> float:
    """How bright `layer`'s sparkle is at (x, y) (0..1 of the screen, y down) on a screen `aspect`
    wide for 1 high; 0 almost everywhere. `min_size` (about a pixel) keeps the smallest from
    flickering as they cross pixels."""
    band = SPARKLE_BAND * layer.scale
    lane = lane_y(x, time, layer.scale)
    if abs(y - lane) > 3 * band:
        return 0.0
    left = layer.drift * time
    gx, gy = (x * aspect - left) / layer.cell, (y - lane) / layer.cell
    cx, cy = math.floor(gx), math.floor(gy)
    if cell_hash(cx + layer.seed, cy) >= layer.density:
        return 0.0
    middle_x, middle_y, glint, grow = _sparkle_in(cx + layer.seed, cy, time)
    sx = left + (cx + middle_x) * layer.cell  # its centre, in screen heights across
    offset = (cy + middle_y) * layer.cell  # and from its lane
    sy = lane_y(sx / aspect, time, layer.scale) + offset
    d = math.hypot(x * aspect - sx, y - sy)
    if layer.blur > 0:  # a soft disc, a little brighter at its rim, as a lens draws it
        blur = layer.blur * grow
        edge = max(0.35 * blur, min_size)
        light = (1 - _smoothstep(blur - edge, blur, d)) * (0.8 + 0.2 * _smoothstep(0.5 * blur, blur, d))
    else:
        size = max(layer.size * grow, min_size)
        light = math.exp(-(d * d) / (size * size)) + 0.3 * math.exp(-(d * d) / (9 * size * size))
    reach = SPARKLE_CLEAR * layer.cell
    return (layer.brightness * (1 - layer.twinkle * (1 - glint)) * light * (1 - _smoothstep(0.6 * reach, reach, d))
            * math.exp(-(offset * offset) / (band * band)))


def _sparkle_in(cx: float, cy: int, time: float) -> tuple[float, float, float, float]:
    """Where in its cell (0..1) the sparkle of cell (cx, cy) is at `time`, how bright it glints
    (SPARKLE_GLINT..1), and how large it is (0.6..1 of its layer's size)."""
    r2, r3 = cell_hash(cx + 17.0, cy + 5.0), cell_hash(cx + 3.0, cy + 29.0)
    spread = 1 - 2 * SPARKLE_MARGIN
    middle_x = SPARKLE_MARGIN + spread * r2 + SPARKLE_WANDER * math.sin(time * 0.7 * (0.5 + r3) + r2 * TAU)
    middle_y = SPARKLE_MARGIN + spread * r3 + SPARKLE_WANDER * math.sin(time * 0.5 * (0.5 + r2) + r3 * TAU)
    glint = max(0.0, math.sin(time * (1.2 + 2.4 * r2) + r3 * TAU))
    return middle_x, middle_y, SPARKLE_GLINT + (1 - SPARKLE_GLINT) * glint * glint, 0.6 + 0.4 * _fract(r2 * 7 + r3 * 3)


def sparkle_field(width: int, height: int, time: float, min_size: float = 0.0,
                  layers: tuple[Layer, ...] = LAYERS) -> dict[int, float]:
    """The layers' sparkle() summed, by pixel index (y * width + x), for the pixels it is not 0 at:
    only the few pixels around each sparkle are worked out (a still frame in a blink, not seconds
    on an old CPU)."""
    aspect = width / height
    field: dict[int, float] = {}
    for layer in layers:
        cell, scale = layer.cell, layer.scale
        left = layer.drift * time  # where cell (0, 0) is
        reach = SPARKLE_CLEAR * cell
        rows = math.ceil(3 * SPARKLE_BAND * scale / cell)
        lanes = [lane_y((px + 0.5) / width, time, scale) for px in range(width)]
        for cx in range(math.floor(-left / cell), math.floor((aspect - left) / cell) + 1):
            for cy in range(-rows - 1, rows + 1):
                if cell_hash(cx + layer.seed, cy) >= layer.density:
                    continue
                middle_x, middle_y, _glint, _grow = _sparkle_in(cx + layer.seed, cy, time)
                sx = left + (cx + middle_x) * cell
                sy = lane_y(sx / aspect, time, scale) + (cy + middle_y) * cell
                for py in range(max(0, math.floor((sy - reach) * height)), min(height, math.ceil((sy + reach) * height) + 1)):
                    y = (py + 0.5) / height
                    for px in range(max(0, math.floor((sx - reach) / aspect * width)),
                                    min(width, math.ceil((sx + reach) / aspect * width) + 1)):
                        x = (px + 0.5) / width
                        if (math.floor((x * aspect - left) / cell), math.floor((y - lanes[px]) / cell)) != (cx, cy):
                            continue  # a neighbouring cell's pixel: its own sparkle, if any, counts it
                        value = sparkle(x, y, time, aspect, layer, min_size)
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
    """lane_y(), sparkle() and cell_hash() as GLSL, from the same constants."""
    margin, wander = _float(SPARKLE_MARGIN), _float(SPARKLE_WANDER)
    amplitude, frequency, speed, _phase = WAVES[0]
    return (
        "float cell_hash(vec2 p) {\n"
        "  vec3 h = fract(vec3(p.xyx) * 0.1031);\n"
        "  h += dot(h, h.yzx + 33.33);\n"
        "  return fract((h.x + h.y) * h.z);\n"
        "}\n"
        "float lane_y(float x, float t, float scale) {\n"
        "  float far_x = 0.5 + (x - 0.5) / scale;\n"
        f"  return {_float(HORIZON)} + ({_float(BASE)} + {_float(amplitude)} * sin(far_x * {_float(frequency)}"
        f" + t * {_float(speed)}) - {_float(HORIZON)}) * scale;\n"
        "}\n"
        "float sparkle(vec2 uv, float t, float aspect, float min_size, float scale, float cell, float density,"
        " float size, float blur, float drift, float brightness, float twinkle, float seed) {\n"
        f"  float band = {_float(SPARKLE_BAND)} * scale;\n"
        "  float lane = lane_y(uv.x, t, scale);\n"
        "  if (abs(uv.y - lane) > 3.0 * band) return 0.0;\n"
        "  float left = drift * t;\n"
        "  vec2 cid = floor(vec2(uv.x * aspect - left, uv.y - lane) / cell);\n"
        "  vec2 id = cid + vec2(seed, 0.0);\n"
        "  if (cell_hash(id) >= density) return 0.0;\n"
        "  float r2 = cell_hash(id + vec2(17.0, 5.0));\n"
        "  float r3 = cell_hash(id + vec2(3.0, 29.0));\n"
        f"  float spread = 1.0 - 2.0 * {margin};\n"
        f"  vec2 middle = vec2({margin} + spread * r2 + {wander} * sin(t * 0.7 * (0.5 + r3) + r2 * {_float(TAU)}),\n"
        f"                     {margin} + spread * r3 + {wander} * sin(t * 0.5 * (0.5 + r2) + r3 * {_float(TAU)}));\n"
        "  float sx = left + (cid.x + middle.x) * cell;\n"
        "  float offset = (cid.y + middle.y) * cell;\n"
        "  float sy = lane_y(sx / aspect, t, scale) + offset;\n"
        "  float d = length(vec2(uv.x * aspect - sx, uv.y - sy));\n"
        f"  float glint = max(0.0, sin(t * (1.2 + 2.4 * r2) + r3 * {_float(TAU)}));\n"
        f"  glint = {_float(SPARKLE_GLINT)} + {_float(1 - SPARKLE_GLINT)} * glint * glint;\n"
        "  float grow = 0.6 + 0.4 * fract(r2 * 7.0 + r3 * 3.0);\n"
        "  float light;\n"
        "  if (blur > 0.0) {\n"
        "    float b = blur * grow;\n"
        "    float edge = max(0.35 * b, min_size);\n"
        "    light = (1.0 - smoothstep(b - edge, b, d)) * (0.8 + 0.2 * smoothstep(0.5 * b, b, d));\n"
        "  } else {\n"
        "    float s = max(size * grow, min_size);\n"
        "    light = exp(-(d * d) / (s * s)) + 0.3 * exp(-(d * d) / (9.0 * s * s));\n"
        "  }\n"
        f"  float reach = {_float(SPARKLE_CLEAR)} * cell;\n"
        "  return brightness * (1.0 - twinkle * (1.0 - glint)) * light * (1.0 - smoothstep(0.6 * reach, reach, d))\n"
        "      * exp(-(offset * offset) / (band * band));\n"
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
    numbers = ", ".join(_float(value) for value in (
        layer.scale, layer.cell, layer.density, layer.size, layer.blur, layer.drift, layer.brightness, layer.twinkle,
        layer.seed))
    return f"sparkle(uv, u_time, aspect, min_size, {numbers})"


def fragment(version: str = "330 core") -> str:
    """The wave's fragment shader. `version` is "330 core" (desktop GL) or "300 es" (GLES).
    GLES gets highp: hours of u_time in mediump would make the waves and the sparkles stutter."""
    widths = ", ".join(_float(w) for w in WIDTHS)
    alphas = ", ".join(_float(a) for a in RIBBON_ALPHA)
    precision = "precision highp float;\n" if version.endswith("es") else ""
    sharp = " + ".join(_layer_call(layer) for layer in SHARP)
    soft = " + ".join(_layer_call(layer) for layer in SOFT)
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
        "    float min_size = 0.8 / u_size.y;\n"
        f"    float soft = {soft};\n"
        "    c = mix(c, u_ribbon, clamp(soft * u_sparkle * u_fade, 0.0, 1.0));\n"
        f"    float sharp = {sharp};\n"
        f"    c = mix(c, mix(u_ribbon, vec3(1.0), {_float(SPARKLE_WHITE)}), clamp(sharp * u_sparkle * u_fade, 0.0, 1.0));\n"
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


CORE_CUT = 200.0  # exp(-200) ~ 1e-87: far under the last bit of any haze on the screen (> 1e-4)
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
    sharp: dict[int, float] = {}
    soft: dict[int, float] = {}
    if colours.sparkle > 0:  # each dot at least a pixel wide, or the small frame would lose most of them
        sharp = sparkle_field(width, height, time, 0.8 / height, SHARP)
        soft = sparkle_field(width, height, time, 0.8 / height, SOFT)
    # The same sums as before in the same order (so the same bytes), with what cannot change a
    # pixel left out: a ribbon's core where it is far below the haze's last bit, and a mix by 0.
    top, ribbon_colour = colours.top, colours.ribbon
    span = tuple(b - a for a, b in zip(colours.top, colours.bottom))
    squares = tuple(w ** 2 for w in WIDTHS)
    alpha, sparkle_amount = colours.alpha, colours.sparkle
    exp = math.exp
    out = bytearray(width * height * 3)
    at = 0
    for py in range(height):
        y = (py + 0.5) / height
        for x, ribbons in columns:
            t = y * 0.85 + x * 0.15
            t = 0.0 if t <= 0.0 else 1.0 if t >= 1.0 else t
            t = t * t * (3 - 2 * t)  # smoothstep
            amount = 0.0
            if alpha > 0:
                glow = 0.0
                for strength, square, middle in zip(RIBBON_ALPHA, squares, ribbons):
                    d = abs(y - middle)
                    far = (d * d) / square
                    haze = exp(-d / HAZE) * 0.35
                    # exp(-CORE_CUT) is under half the haze's last bit here: adding it changes nothing.
                    glow += strength * ((exp(-far) + haze) if far < CORE_CUT else haze)
                lo, hi = (ribbons[0], ribbons[1]) if ribbons[0] <= ribbons[1] else (ribbons[1], ribbons[0])
                if lo - SHEET_EDGE < y < hi + SHEET_EDGE:  # sheet() is 0 outside: skip the sums
                    glow += sheet(y, lo, hi)
                amount = glow * alpha
                amount = 0.0 if amount <= 0.0 else 1.0 if amount >= 1.0 else amount
            shine = haze = 0.0
            if sparkle_amount > 0:
                shine = sharp.get(at // 3, 0.0) * sparkle_amount
                shine = 0.0 if shine <= 0.0 else 1.0 if shine >= 1.0 else shine
                haze = soft.get(at // 3, 0.0) * sparkle_amount
                haze = 0.0 if haze <= 0.0 else 1.0 if haze >= 1.0 else haze
            for channel in (0, 1, 2):
                base = top[channel] + span[channel] * t
                if amount:
                    base += (ribbon_colour[channel] - base) * amount
                if haze:
                    base += (ribbon_colour[channel] - base) * haze
                if shine:
                    base += (white[channel] - base) * shine
                out[at + channel] = round(255 * base)
            at += 3
    return bytes(out)


STILL_VERSION = 1  # raise when still_frame() draws differently (the shader text is in the key too)
STILL_CACHE = pathlib.Path(os.environ.get("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache") / "couchliteos/wave"


def still_key(width: int, height: int, colours: Palette, time: float = STILL_TIME) -> str:
    """What a still frame is made of, as a file name: its size, its colours, its moment, and the
    wave itself (FRAGMENT's text holds every constant still_frame() uses)."""
    made_of = repr((STILL_VERSION, width, height, dataclasses.astuple(colours), time, fragment()))
    return hashlib.sha256(made_of.encode()).hexdigest()[:32]


class StillCache:
    """Still frames on disk, compressed: a theme, accent and quarter hour seen before is read back
    rather than worked out again (a fraction of a second of an old CPU each). At most LIMIT files;
    the ones least recently used go first. A cache that cannot be read or written only costs time."""

    LIMIT = 256  # a few tens of KB each

    def __init__(self, directory: pathlib.Path = STILL_CACHE, limit: int = LIMIT) -> None:
        self.directory = directory
        self.limit = limit

    def frame(self, width: int, height: int, colours: Palette, time: float = STILL_TIME) -> bytes:
        path = self.directory / f"{still_key(width, height, colours, time)}.rgb.z"
        try:
            data = zlib.decompress(path.read_bytes())
            if len(data) == width * height * 3:
                os.utime(path)
                return data
        except (OSError, zlib.error):
            pass
        data = still_frame(width, height, colours, time)
        self.store(path, data)
        return data

    def store(self, path: pathlib.Path, data: bytes) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".still.", dir=self.directory)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(zlib.compress(data, 6))
                os.replace(temporary, path)
            except BaseException:
                pathlib.Path(temporary).unlink(missing_ok=True)
                raise
            self.prune()
        except OSError:
            pass

    def prune(self) -> None:
        files = []
        for item in self.directory.glob("*.rgb.z"):
            try:
                files.append((item.stat().st_mtime_ns, item))
            except OSError:
                continue
        files.sort()
        for _mtime, item in files[:max(0, len(files) - self.limit)]:
            item.unlink(missing_ok=True)


def render_divisor(width: int, height: int, most: int = 1920 * 1080) -> float:
    """How much smaller than the screen the GL wave is drawn (then stretched back): 1 up to a
    1080p screen's pixels, beyond that just enough to stay at that many (1440p: 1.33, 4K: 2).
    The wave is soft light; above 1080p a stretched frame looks the same and costs a half to
    a quarter of the GPU (or CPU) time."""
    pixels = width * height
    if pixels <= most * 1.25 or most <= 0:  # 1920x1200 too
        return 1.0
    return math.sqrt(pixels / most)
