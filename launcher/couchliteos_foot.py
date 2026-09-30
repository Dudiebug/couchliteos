#!/usr/bin/python3
"""Start foot with a font size that suits the display's resolution.

The launcher, the on-screen keyboard and terminal apps all run in full-screen
foot windows, and foot's font is a fixed point size: 24 pt gives about 65x18
cells on a 720p TV but about 197x56 (small text) on a 4K one. This wrapper asks
the compositor for the screen's mode and passes `--font` so every resolution
shows about 28 rows. If anything goes wrong foot starts as before, with the
font in /etc/xdg/foot/foot.ini. COUCHLITEOS_FONT_SIZE (8-96) forces a size.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

import couchliteos_display as display

FOOT = "/usr/bin/foot"
MIN_SIZE = 8
MAX_SIZE = 96
ROW_TARGET = 28
MIN_COLUMNS = 80
PAD = 24  # foot.ini has pad=12x12
# DejaVu Sans Mono at foot's 96 dpi: a cell is about 0.80 x 1.55 px per point of font size.
CELL_WIDTH = 0.80
CELL_HEIGHT = 1.55


def grid(width: int, height: int, size: int) -> tuple[int, int]:
    """Estimated terminal columns and rows of a full-screen foot window."""
    return int((width - PAD) / (CELL_WIDTH * size)), int((height - PAD) / (CELL_HEIGHT * size))


def font_size(width: int, height: int) -> int:
    """Font size in points that gives about ROW_TARGET rows and at least MIN_COLUMNS columns."""
    by_rows = (height - PAD) / (ROW_TARGET * CELL_HEIGHT)
    by_columns = (width - PAD) / (MIN_COLUMNS * CELL_WIDTH)
    return max(MIN_SIZE, min(MAX_SIZE, int(min(by_rows, by_columns))))


def target_resolution(outputs: list[display.Output], saved: dict[str, str]) -> tuple[int, int] | None:
    """The resolution foot will be shown at, or None when no screen is active.

    The launcher restores the saved mode right after foot starts, so that mode wins
    when it still belongs to this screen.
    """
    output = display.active_output(outputs)
    if output is None or output.current_mode is None:
        return None
    wanted = saved.get("resolution", "")
    if (
        saved.get("output") == output.name
        and saved.get("identity") == output.identity
        and any(mode.resolution == wanted for mode in output.modes)
    ):
        width, height = wanted.split("x")
        return int(width), int(height)
    return output.current_mode.width, output.current_mode.height


def choose_size(
    environ: dict[str, str], *, run=subprocess.run, saved: dict[str, str] | None = None, sleep=time.sleep
) -> int | None:
    """The font size to use, or None to leave foot's configured font alone."""
    forced = environ.get("COUCHLITEOS_FONT_SIZE", "")
    if re.fullmatch(r"\d{1,2}", forced) and MIN_SIZE <= int(forced) <= MAX_SIZE:
        return int(forced)
    saved = display.load_saved_display() if saved is None else saved
    for attempt in range(2):
        if attempt:
            sleep(0.3)  # the compositor may still be bringing the screen up
        try:
            result = run(["wlr-randr"], text=True, capture_output=True, check=False, timeout=2)
            resolution = None if result.returncode else target_resolution(display.parse_wlr_randr(result.stdout), saved)
        except (OSError, ValueError, subprocess.SubprocessError):
            resolution = None
        if resolution:
            size = font_size(*resolution)
            print(f"couchliteos-foot: {resolution[0]}x{resolution[1]} -> font size {size}", file=sys.stderr)
            return size
    print("couchliteos-foot: screen size unknown, using the configured font", file=sys.stderr)
    return None


def command(size: int | None, arguments: list[str]) -> list[str]:
    return [FOOT, *(["--font", f"monospace:size={size}"] if size else []), *arguments]


def main(arguments: list[str] | None = None) -> None:
    arguments = sys.argv[1:] if arguments is None else arguments
    os.execv(FOOT, command(choose_size(dict(os.environ)), arguments))


if __name__ == "__main__":
    main()
