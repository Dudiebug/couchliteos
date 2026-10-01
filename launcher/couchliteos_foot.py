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

try:
    import couchliteos_screenfit as screenfit
except Exception:  # boot path: without the module (or with a broken one) foot starts as before
    screenfit = None

FOOT = "/usr/bin/foot"
MIN_SIZE = 8
MAX_SIZE = 96
ROW_TARGET = 28
MIN_COLUMNS = 80
MIN_ROWS = 24  # TEXT SIZE LARGER never shrinks the launcher below 80x24
BASE_PAD = 12  # foot.ini has pad=12x12
PAD = 2 * BASE_PAD
# DejaVu Sans Mono at foot's 96 dpi: a cell is about 0.80 x 1.55 px per point of font size.
CELL_WIDTH = 0.80
CELL_HEIGHT = 1.55


def grid(width: int, height: int, size: int, extra: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """Estimated terminal columns and rows of a full-screen foot window; `extra` is the screen-edge padding per side."""
    return (
        int((width - PAD - 2 * extra[0]) / (CELL_WIDTH * size)),
        int((height - PAD - 2 * extra[1]) / (CELL_HEIGHT * size)),
    )


def font_size(width: int, height: int, extra: tuple[int, int] = (0, 0)) -> int:
    """Font size in points that gives about ROW_TARGET rows and at least MIN_COLUMNS columns."""
    by_rows = (height - PAD - 2 * extra[1]) / (ROW_TARGET * CELL_HEIGHT)
    by_columns = (width - PAD - 2 * extra[0]) / (MIN_COLUMNS * CELL_WIDTH)
    return max(MIN_SIZE, min(MAX_SIZE, int(min(by_rows, by_columns))))


def largest_size(width: int, height: int, extra: tuple[int, int] = (0, 0)) -> int:
    """The biggest font that still leaves MIN_COLUMNS x MIN_ROWS cells."""
    by_rows = (height - PAD - 2 * extra[1]) / (MIN_ROWS * CELL_HEIGHT)
    by_columns = (width - PAD - 2 * extra[0]) / (MIN_COLUMNS * CELL_WIDTH)
    return max(MIN_SIZE, int(min(by_rows, by_columns)))


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


def load_settings():
    """The user's screen-edge and text-size settings, or None (today's behaviour) when they cannot be had."""
    try:
        return screenfit.load()
    except Exception:
        return None


def edge_padding(resolution: tuple[int, int], settings) -> tuple[int, int]:
    """Extra padding per side for the screen edges; (0, 0) for zero edges or on any problem."""
    try:
        x, y = screenfit.pad_pixels(*resolution, settings.edges)
        return (x, y) if type(x) is int and type(y) is int and x >= 0 and y >= 0 else (0, 0)
    except Exception:
        return 0, 0


def scaled(size: int, settings) -> int:
    """The automatic font size changed by the text-size choice, kept within foot's limits."""
    try:
        return max(MIN_SIZE, min(MAX_SIZE, round(size * screenfit.text_scale(settings.text))))
    except Exception:
        return size


def detect_resolution(run, saved: dict[str, str], sleep) -> tuple[int, int] | None:
    for attempt in range(2):
        if attempt:
            sleep(0.3)  # the compositor may still be bringing the screen up
        try:
            result = run(["wlr-randr"], text=True, capture_output=True, check=False, timeout=2)
            resolution = None if result.returncode else target_resolution(display.parse_wlr_randr(result.stdout), saved)
        except (OSError, ValueError, subprocess.SubprocessError):
            resolution = None
        if resolution:
            return resolution
    return None


def choose(
    environ: dict[str, str], *, run=subprocess.run, saved: dict[str, str] | None = None, sleep=time.sleep, settings=None
) -> tuple[int | None, tuple[int, int] | None]:
    """The font size (None: keep foot's configured font) and pad (None: keep foot.ini's) to use.

    `settings` are the user's screen edges and text size; None is exactly the old behaviour.
    """
    forced = environ.get("COUCHLITEOS_FONT_SIZE", "")
    size = int(forced) if re.fullmatch(r"\d{1,2}", forced) and MIN_SIZE <= int(forced) <= MAX_SIZE else None
    if size is not None and not getattr(settings, "edges", 0):
        return size, None
    saved = display.load_saved_display() if saved is None else saved
    resolution = detect_resolution(run, saved, sleep)
    if resolution is None:
        print("couchliteos-foot: screen size unknown, using the configured font", file=sys.stderr)
        return size, None
    extra = edge_padding(resolution, settings) if settings else (0, 0)
    pad = (BASE_PAD + extra[0], BASE_PAD + extra[1]) if any(extra) else None
    if size is None:
        size = font_size(*resolution, extra)
        if settings:
            size = min(scaled(size, settings), max(size, largest_size(*resolution, extra)))
        print(f"couchliteos-foot: {resolution[0]}x{resolution[1]} -> font size {size}", file=sys.stderr)
    if pad:
        print(f"couchliteos-foot: screen edges -> pad {pad[0]}x{pad[1]}", file=sys.stderr)
    return size, pad


def choose_size(
    environ: dict[str, str], *, run=subprocess.run, saved: dict[str, str] | None = None, sleep=time.sleep
) -> int | None:
    """The font size to use, or None to leave foot's configured font alone."""
    return choose(environ, run=run, saved=saved, sleep=sleep)[0]


def command(size: int | None, arguments: list[str], pad: tuple[int, int] | None = None) -> list[str]:
    return [
        FOOT,
        *(["--font", f"monospace:size={size}"] if size else []),
        *(["-o", f"main.pad={pad[0]}x{pad[1]}"] if pad else []),
        *arguments,
    ]


def plan(environ: dict[str, str]) -> tuple[int | None, tuple[int, int] | None]:
    """Font size and pad for this start.

    With no screen settings chosen (the default) this is exactly the old code path, and
    any failure in the new one falls back to it too.
    """
    try:
        settings = load_settings()
        if settings is not None and (settings.edges or settings.text != "auto"):
            return choose(environ, settings=settings)
    except Exception:
        pass
    return choose_size(environ), None


def main(arguments: list[str] | None = None) -> None:
    arguments = sys.argv[1:] if arguments is None else arguments
    size, pad = plan(dict(os.environ))
    os.execv(FOOT, command(size, arguments, pad))


if __name__ == "__main__":
    main()
