"""The boot screen, as plain Python: where everything goes and how it moves.

From power-on to the home screen the TV shows one picture: the CouchLiteOS logo on MIDNIGHT's
gradient, a soft light behind it that breathes, two glowing ribbons drifting across the lower
half (the XMB's wave, simplified) and three dots pulsing under them. Plymouth draws it while the
system starts (couchliteos.script, which tools/make-logo.py writes from this module); then
couchliteos-tv draws the same picture (couchliteos_gtk_boot) from its first frame until its home
screen is ready, and fades it out over the home screen.

The pictures are tools/make-logo.py's PNGs in the Plymouth theme. Their sizes are for a screen
1080 lines high and scale with the screen's height; positions are fractions of the screen. A
ribbon is one wave long and as wide as the screen: two copies side by side drift left and wrap.
"""

from __future__ import annotations

import dataclasses
import math
import pathlib

import couchliteos_motion as motion

ASSETS = pathlib.Path("/usr/share/plymouth/themes/couchliteos")
REFERENCE_HEIGHT = 1080
TOP = "162a51"  # MIDNIGHT's wave gradient (couchliteos_wave.palette at 9 o'clock), top to bottom
BOTTOM = "090d1a"
LOGO_Y = 0.42  # the logo's centre, in screen heights
RIBBONS = ("ribbon-1.png", "ribbon-2.png")
RIBBON_Y = (0.68, 0.70)  # each ribbon's middle line
RIBBON_SECONDS = (21.0, 13.0)  # to drift one screen width to the left
STILL_DRIFT = (0.18, 0.62)  # where the ribbons rest when nothing moves (they cross nicely there)
HALO_SECONDS = 3.6  # one breath of the light behind the logo
HALO_LOW = 0.45  # its opacity at the bottom of a breath
DOTS = 3
DOTS_Y = 0.86
DOT_GAP = 0.03  # between dot centres, in screen heights
DOT_SECONDS = 1.5
DOT_STAGGER = 0.2  # each dot's pulse this much after the one on its left
DOT_LOW = 0.15
FADE_MS = 450  # the loading screen fading into the home screen (MOTION FULL)
FPS = 50  # Plymouth's script plugin refreshes this often; its script counts frames
FILES = ("logo.png", "halo.png", *RIBBONS, "dot.png")
TAU = 2 * math.pi


@dataclasses.dataclass(frozen=True)
class Sprite:
    """One picture to draw: `name` (a file in ASSETS) at x, y with this size and opacity."""

    name: str
    x: float
    y: float
    width: float
    height: float
    opacity: float = 1.0


def rgb(colour: str) -> tuple[float, float, float]:
    """"rrggbb" as 0..1 floats."""
    return tuple(int(colour[at:at + 2], 16) / 255 for at in (0, 2, 4))  # type: ignore[return-value]


def moving(level: str) -> bool:
    """Only MOTION FULL animates; REDUCED and OFF (and the cairo renderer, which is at most
    REDUCED) get the still picture."""
    return level == motion.FULL


def fade_seconds(level: str) -> float:
    return motion.scaled(FADE_MS, level)


def drift(t: float, ribbon: int) -> float:
    """How far ribbon `ribbon` has drifted left at `t` seconds, in screen widths (0..1, wrapping)."""
    return (t / RIBBON_SECONDS[ribbon]) % 1.0


def breath(t: float) -> float:
    """The halo's opacity: HALO_LOW to 1 and back, starting low."""
    return HALO_LOW + (1 - HALO_LOW) * (0.5 - 0.5 * math.cos(TAU * t / HALO_SECONDS))


def dot(t: float, index: int) -> float:
    """Dot `index`'s opacity: a short, bright pulse travelling left to right."""
    pulse = 0.5 + 0.5 * math.cos(TAU * (t - index * DOT_STAGGER) / DOT_SECONDS)
    return DOT_LOW + (1 - DOT_LOW) * pulse ** 3


def centred(name: str, size: tuple[int, int], scale: float, cx: float, cy: float, opacity: float = 1.0) -> Sprite:
    width, height = size[0] * scale, size[1] * scale
    return Sprite(name, cx - width / 2, cy - height / 2, width, height, opacity)


def frame(width: int, height: int, sizes: dict[str, tuple[int, int]], t: float, animate: bool) -> list[Sprite]:
    """Everything drawn over the gradient at `t` seconds, back to front. `sizes` holds the
    pictures that loaded (name -> pixels); a missing one is left out. Still (animate False):
    the ribbons rest at STILL_DRIFT, the halo is full and there are no dots."""
    scale = height / REFERENCE_HEIGHT
    sprites = []
    for index, name in enumerate(RIBBONS):
        if name not in sizes:
            continue
        ribbon_height = sizes[name][1] * scale
        shift = drift(t, index) if animate else STILL_DRIFT[index]
        x = -shift * width
        y = height * RIBBON_Y[index] - ribbon_height / 2
        sprites += [Sprite(name, x, y, width, ribbon_height), Sprite(name, x + width, y, width, ribbon_height)]
    if "halo.png" in sizes:
        sprites.append(centred("halo.png", sizes["halo.png"], scale, width / 2, height * LOGO_Y,
                               breath(t) if animate else 1.0))
    if "logo.png" in sizes:
        sprites.append(centred("logo.png", sizes["logo.png"], scale, width / 2, height * LOGO_Y))
    if animate and "dot.png" in sizes:
        for index in range(DOTS):
            cx = width / 2 + (index - (DOTS - 1) / 2) * DOT_GAP * height
            sprites.append(centred("dot.png", sizes["dot.png"], scale, cx, height * DOTS_Y, dot(t, index)))
    return sprites
