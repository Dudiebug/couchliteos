"""The XMB background: a soft gradient with slow, glowing ribbons, as plain Python.

couchliteos_gtk_wave draws it. With the GL renderer it runs FRAGMENT (built here from the same
WAVES the Python uses) in a Gtk.GLArea at up to FPS frames a second; with the software (cairo)
renderer, with MOTION OFF, or when the GL wave failed before, it paints one still frame from
ribbon_points(); with the HIGH CONTRAST theme it is the flat background colour. It stops while an
application or a stream is in front or the screen is blank: a still screen costs nothing.

Colours come from the theme: the background, tinted with the accent towards the top, a little
brighter by day and dimmer at night (as the console's wave follows the clock). The tint is held
back until the theme's text keeps MIN_CONTRAST against the brightest part of the gradient.
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
TAU = 2 * math.pi
MAX_GLOW = sum(RIBBON_ALPHA) * 1.35  # every ribbon's core and haze on one pixel: the brightest it gets
LARGE_CONTRAST = 3.0  # WCAG AA for large text: the XMB's labels crossing a ribbon


@dataclasses.dataclass(frozen=True)
class Palette:
    top: tuple[float, float, float]  # 0..1 RGB
    bottom: tuple[float, float, float]
    ribbon: tuple[float, float, float]
    alpha: float  # how strongly the ribbons show

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
    return Palette(top, bottom, ribbon, round(alpha, 3))


def ribbon_y(x: float, time: float, ribbon: int) -> float:
    """Ribbon `ribbon`'s height at `x` (both 0..1 of the screen) at `time` seconds."""
    y = BASE + RIBBON_OFFSETS[ribbon]
    for amplitude, frequency, speed, phase in WAVES:
        y += amplitude * math.sin(x * frequency + time * speed + ribbon * phase)
    return y


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


def fragment(version: str = "330 core") -> str:
    """The wave's fragment shader. `version` is "330 core" (desktop GL) or "300 es" (GLES)."""
    widths = ", ".join(_float(w) for w in WIDTHS)
    alphas = ", ".join(_float(a) for a in RIBBON_ALPHA)
    precision = "precision mediump float;\n" if version.endswith("es") else ""
    return (
        f"#version {version}\n{precision}"
        "uniform vec2 u_size;\nuniform float u_time;\nuniform vec3 u_top;\nuniform vec3 u_bottom;\n"
        "uniform vec3 u_ribbon;\nuniform float u_alpha;\nuniform float u_fade;\n"
        "out vec4 colour;\n"
        + _ribbon_glsl()
        + f"const float WIDTHS[{len(WIDTHS)}] = float[]({widths});\n"
        + f"const float ALPHAS[{len(RIBBON_ALPHA)}] = float[]({alphas});\n"
        + "void main() {\n"
        "  vec2 uv = vec2(gl_FragCoord.x / u_size.x, 1.0 - gl_FragCoord.y / u_size.y);\n"
        "  vec3 c = mix(u_top, u_bottom, smoothstep(0.0, 1.0, uv.y * 0.85 + uv.x * 0.15));\n"
        "  float glow = 0.0;\n"
        f"  for (int i = 0; i < {len(WIDTHS)}; i++) {{\n"
        "    float d = abs(uv.y - ribbon_y(uv.x, u_time, i));\n"
        "    float core = exp(-(d * d) / (WIDTHS[i] * WIDTHS[i]));\n"
        f"    float haze = exp(-d / {_float(HAZE)}) * 0.35;\n"
        "    glow += ALPHAS[i] * (core + haze);\n"
        "  }\n"
        "  c = mix(c, u_ribbon, clamp(glow * u_alpha * u_fade, 0.0, 1.0));\n"
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
