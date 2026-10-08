"""The colour of the month: Settings > APPEARANCE > ACCENT > BY MONTH.

The PS3's background takes a colour for each month of the year, as the PSP's did before it:
grey in January, then a dull yellow, lime green, pink, dark green, light purple, teal, blue,
purple, gold and brown, and red in December. It moves on to the month's colour about halfway
through the month, blending over the days before: from the 12th to the 14th last month's colour
turns into this month's, and from the 15th on it is this month's.

As the accent it tints the wave (which already dims at night: couchliteos_wave.palette) and
colours the screens' accent. The colours are our own, picked in the console's spirit and kept
readable behind light text on every built-in theme (test_month.py checks them all).
"""

from __future__ import annotations

import datetime

NAME = "month"  # [appearance] accent = month
LABEL = "BY MONTH"
# January to December.
COLOURS = (
    "a3a9b4", "c9b037", "8cc63f", "e57fa8", "2f9e44", "a98bd8",
    "1fb5c9", "2f7fe0", "9b4fd1", "e0a526", "a8703a", "dc3b3b",
)
SWITCH_DAY = 15  # this month's colour from this day of the month
BLEND_DAYS = 3  # the days before it, when last month's colour turns into it


def _mix(a: str, b: str, amount: float) -> str:
    return "".join(
        f"{round(int(a[at:at + 2], 16) + (int(b[at:at + 2], 16) - int(a[at:at + 2], 16)) * amount):02x}"
        for at in (0, 2, 4))


def colour(day: datetime.date | None = None) -> str:
    """The accent (rrggbb) on `day`, today when None."""
    day = day or datetime.date.today()
    this, last = COLOURS[day.month - 1], COLOURS[day.month - 2]  # January's last is December's
    if day.day >= SWITCH_DAY:
        return this
    into = day.day - (SWITCH_DAY - BLEND_DAYS) + 1  # 1 to BLEND_DAYS on the blending days
    if into <= 0:
        return last
    return _mix(last, this, into / (BLEND_DAYS + 1))
