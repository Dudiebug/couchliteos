"""The controller buttons help screen: Settings > CONTROLLER BUTTONS, and once after setup.

The text must describe what gamepad-nav does (launcher/gamepad-nav.py key_for_event), which
test_controls.py checks against the real mapping. Pad names come from /proc/bus/input/devices.
"""

from __future__ import annotations

import curses
import itertools
import pathlib
import textwrap

import moonlightos_setup as setup

PROC_INPUT = pathlib.Path("/proc/bus/input/devices")
SHOWN = setup.MARKER.parent / "controls-shown"
TITLE = "CONTROLLER BUTTONS"
WIDTH = 76  # the widest line; the screen also fits 80x24
SLEEP_HOLD_SECONDS = 3  # how long Guide is held to sleep (gamepad-nav)
BTN_SOUTH = 0x130  # evdev code of the A / Cross button: what makes an input device a gamepad
CLOSE_KEYS = (10, 13, curses.KEY_ENTER, 27)  # gamepad-nav sends A as Enter and B as Esc

# Names printed on each family's pad for gamepad-nav's BTN_SOUTH, EAST, NORTH, WEST, TL, TR,
# SELECT, START and the Guide button. BTN_NORTH opens the keyboard and BTN_WEST deletes:
# X and Y on Xbox, Triangle and Square on PlayStation. Nintendo's kernel driver reports its
# B button as south and its A button as east.
XBOX = ("A", "B", "X", "Y", "LB", "RB", "VIEW", "MENU", "GUIDE")
PLAYSTATION = ("CROSS", "CIRCLE", "TRIANGLE", "SQUARE", "L1", "R1", "SHARE", "OPTIONS", "PS BUTTON")
NINTENDO = ("B", "A", "X", "Y", "L", "R", "MINUS", "PLUS", "HOME")
GENERIC = tuple(f"{first} ({second})" for first, second in zip(XBOX, PLAYSTATION))
FAMILY_NAMES = {"xbox": XBOX, "playstation": PLAYSTATION, "nintendo": NINTENDO, "generic": GENERIC}
# Lower-case words in an input device name that identify the maker, checked in this order
# ("Xbox Wireless Controller" must not count as a PlayStation "Wireless Controller").
FAMILY_WORDS = (
    ("nintendo", ("nintendo", "joy-con", "pro controller")),
    ("xbox", ("xbox", "x-box", "microsoft")),
    ("playstation", ("sony", "dualsense", "dualshock", "playstation", "wireless controller")),
)


def pad_names(text: str) -> list[str]:
    """Names of the gamepads in /proc/bus/input/devices text.

    A gamepad has the BTN_SOUTH bit in its "B: KEY=" bitmap (the test gamepad-nav uses). The
    bitmap is 64-bit hex words, highest first, so the last word holds bits 0-63. Devices made
    with uinput (gamepad-nav's own keyboard, the CEC remote) sit under /devices/virtual/input/
    and are skipped. Bluetooth pads do not: bluetoothd makes them through uhid, which sysfs
    puts under /devices/virtual/misc/uhid.
    """
    names = []
    for block in text.split("\n\n"):
        name, words, uinput = "", [], False
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line[len("N: Name="):].strip().strip('"')
            elif line.startswith("S: Sysfs="):
                uinput = "/devices/virtual/input/" in line
            elif line.startswith("B: KEY="):
                words = line[len("B: KEY="):].split()
        try:
            south = len(words) > BTN_SOUTH // 64 and int(words[-1 - BTN_SOUTH // 64], 16) >> (BTN_SOUTH % 64) & 1
        except ValueError:
            south = 0
        if name and south and not uinput:
            names.append(name)
    return names


def family(names: list[str]) -> str:
    """"xbox", "playstation" or "nintendo" when every pad is one of them, else "generic"."""
    found = {
        next((kind for kind, words in FAMILY_WORDS if any(word in name.lower() for word in words)), "")
        for name in names
    }
    return found.pop() or "generic" if len(found) == 1 else "generic"


def detect_family(path: pathlib.Path = PROC_INPUT) -> str:
    try:
        return family(pad_names(path.read_text(errors="replace")))
    except OSError:
        return "generic"


def table(kind: str, can_sleep: bool) -> list[tuple[list[str], str]]:
    """(button label lines, what the button does) for each row."""
    south, east, north, west, left, right, view, menu, guide = FAMILY_NAMES[kind]
    shortcuts = [f"{left}  {right}  {view}  {menu}"]
    if len(shortcuts[0]) > 24:  # both names per button: two lines
        shortcuts = [f"{left}  {right}", f"{view}  {menu}"]
    rows = [
        ([south], "SELECT / OK"),
        ([east], "BACK / CANCEL"),
        ([north], "ON-SCREEN KEYBOARD"),
        ([west], "DELETE A LETTER / CLOSE AN APP"),
        (["D-PAD OR LEFT STICK"], "MOVE (HOLD TO KEEP MOVING)"),
        (shortcuts, "APP SHORTCUTS (SET IN SETTINGS > APPLICATIONS)"),
        ([guide], "OPEN ACTIVE APPLICATIONS FROM ANY APP OR GAME STREAM"),
    ]
    if can_sleep:
        rows.append(([f"HOLD {guide} {SLEEP_HOLD_SECONDS} S"], "SLEEP"))
    return rows


def rows(kind: str, can_sleep: bool) -> list[str]:
    """The table as text lines of at most WIDTH columns; long descriptions wrap."""
    cells = table(kind, can_sleep)
    column = max(len(line) for label, _text in cells for line in label) + 2
    return [
        f"{label:<{column}}{text}".rstrip()
        for label_lines, text in cells
        for label, text in itertools.zip_longest(label_lines, textwrap.wrap(text, WIDTH - column), fillvalue="")
    ]


def footer(kind: str) -> str:
    south, east = FAMILY_NAMES[kind][:2]
    return f"PRESS {south} OR {east} TO CLOSE"


def sleep_supported() -> bool:
    """Whether this PC can sleep now; the sleep row is left out when it cannot."""
    try:
        import moonlightos_power as power  # only in builds that include the sleep feature

        return bool(power.can_suspend())
    except Exception:  # no such module, or logind/sysfs trouble: do not promise sleep
        return False


def put(screen: curses.window, row: int, column: int, text: str) -> None:
    height, width = screen.getmaxyx()
    if 0 <= row < height and 0 <= column < width - 1:
        try:
            screen.addnstr(row, column, text, width - column - 1)
        except curses.error:
            pass


def show(screen: curses.window, can_sleep: bool | None = None, proc: pathlib.Path = PROC_INPUT) -> None:
    """Draw the help for the connected pad until A or B (Enter or Esc) is pressed."""
    kind = detect_family(proc)
    lines = rows(kind, sleep_supported() if can_sleep is None else can_sleep)
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        if height >= 8 and width >= 24:
            try:
                screen.border()
            except curses.error:
                pass
        title_row = 2 if height >= 16 else 1
        bottom = height - 3 if height >= 16 else height - 2
        put(screen, title_row, max(1, (width - len(TITLE)) // 2), TITLE)
        left = max(2, (width - max(map(len, lines))) // 2)
        for offset, line in enumerate(lines):
            if title_row + 2 + offset < bottom:
                put(screen, title_row + 2 + offset, left, line)
        text = footer(kind)
        put(screen, bottom, max(1, (width - len(text)) // 2), text)
        screen.refresh()
        if screen.getch() in CLOSE_KEYS:
            return


def should_show_once(marker: pathlib.Path = SHOWN, setup_marker: pathlib.Path = setup.MARKER) -> bool:
    """Only after setup is complete, so it never comes before the wizard."""
    return setup_marker.exists() and not marker.exists()


def mark_shown(marker: pathlib.Path = SHOWN) -> None:
    try:
        setup.write_complete(marker)
    except OSError:
        pass  # the worst case is that the screen shows again


def show_once(
    screen: curses.window,
    can_sleep: bool | None = None,
    marker: pathlib.Path = SHOWN,
    setup_marker: pathlib.Path = setup.MARKER,
    proc: pathlib.Path = PROC_INPUT,
) -> None:
    if not should_show_once(marker, setup_marker):
        return
    mark_shown(marker)  # before drawing: a screen that cannot draw must not return on every start
    try:
        show(screen, can_sleep, proc)
    except curses.error:
        pass
