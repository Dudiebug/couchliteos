"""What is behind the XMB: Settings > APPEARANCE > BACKGROUND, saved as [appearance] background.

    WAVE AND SPARKLES  (wave)   the console's wave with its sparkles: the default
    WAVE               (calm)   the wave alone, no sparkles
    PLAIN              (plain)  the theme's gradient alone: no ribbons, nothing moving

The TV interface reads it with the theme and applies it at once (a change to config.ini is seen
by its 1 s tick): it turns couchliteos_wave's mode and palette into these. HIGH CONTRAST stays
flat whatever is chosen, and a still wave (MOTION OFF, the software renderer) stays still.
"""

from __future__ import annotations

import dataclasses
import pathlib

import couchliteos_theme as theme
import couchliteos_wave as wave

KEY = "background"
WAVE, CALM, PLAIN = "wave", "calm", "plain"
CHOICES = ((WAVE, "WAVE AND SPARKLES"), (CALM, "WAVE"), (PLAIN, "PLAIN"))
DEFAULT = WAVE


def load(path: pathlib.Path | None = None) -> str:
    """The saved style; DEFAULT when it is missing, unknown or cannot be read."""
    value = theme.load_value(KEY, DEFAULT, path)
    return value if value in dict(CHOICES) else DEFAULT


def save(value: str, path: pathlib.Path | None = None) -> None:
    """Raises OSError, and ValueError for a style that is not one of CHOICES."""
    if value not in dict(CHOICES):
        raise ValueError(f"NO BACKGROUND CALLED {value[:16]!r}".upper())
    theme.save_values({KEY: value}, path)


def label(value: str) -> str:
    return dict(CHOICES).get(value, dict(CHOICES)[DEFAULT])


def wave_mode(how: str, value: str) -> str:
    """couchliteos_wave.mode()'s answer for this style: PLAIN draws the still frame (nothing to
    animate), FLAT stays flat."""
    return wave.STATIC if value == PLAIN and how == wave.GL else how


def wave_palette(colours: wave.Palette, value: str) -> wave.Palette:
    """The wave's colours for this style: WAVE has no sparkles, PLAIN no ribbons either."""
    if value == PLAIN:
        return dataclasses.replace(colours, alpha=0.0, sparkle=0.0)
    if value == CALM:
        return dataclasses.replace(colours, sparkle=0.0)
    return colours
