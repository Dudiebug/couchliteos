#!/usr/bin/python3
"""TV screen edges (overscan) and text size: the saved choice and the math.

Many TVs hide the outer few percent of the picture, so the launcher's border is cut
off. SCREEN EDGES pads foot's window inward; TEXT SIZE scales foot's automatic font.
The launcher saves the choice here and couchliteos_foot.py (the foot wrapper, on the
boot path) reads it, so `load` never raises: anything unreadable means the defaults.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib

# Next to config.ini and setup-complete.
PATH = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos")) / "screen.json"
EDGE_CHOICES = (0, 2, 4, 6)  # percent of the width and height hidden on each side; 6 is the cap
TEXT_CHOICES = ("auto", "smaller", "larger")
TEXT_SCALES = {"smaller": 0.8, "auto": 1.0, "larger": 1.25}
MAX_FILE = 4096


@dataclasses.dataclass(frozen=True)
class Settings:
    edges: int = 0
    text: str = "auto"


def load(path: pathlib.Path | None = None) -> Settings:
    """The saved settings; any missing, unreadable or unknown value gives its default."""
    try:
        with (PATH if path is None else path).open("rb") as stream:
            data = json.loads(stream.read(MAX_FILE))
        edges, text = data.get("edges"), data.get("text")
    except Exception:  # boot path: a bad file must never stop the launcher from starting
        return Settings()
    return Settings(
        edges=edges if type(edges) is int and edges in EDGE_CHOICES else 0,
        text=text if type(text) is str and text in TEXT_CHOICES else "auto",
    )


def save(settings: Settings, path: pathlib.Path | None = None) -> None:
    """Write the settings atomically. Raises OSError when they cannot be stored."""
    import couchliteos_apps as apps  # only the launcher saves; the boot-path wrapper never imports it

    content = json.dumps({"edges": settings.edges, "text": settings.text}) + "\n"
    apps.atomic_write(PATH if path is None else path, content)


def pad_pixels(width: int, height: int, edges_pct: int) -> tuple[int, int]:
    """Extra padding in pixels on each side for the chosen edge percentage; 0 for any unknown value."""
    if type(edges_pct) is not int or edges_pct not in EDGE_CHOICES:
        return 0, 0
    return round(width * edges_pct / 100), round(height * edges_pct / 100)


def text_scale(choice: str) -> float:
    """The factor applied to the automatic font size: 0.8, 1.0 or 1.25."""
    return TEXT_SCALES.get(choice, 1.0) if isinstance(choice, str) else 1.0
