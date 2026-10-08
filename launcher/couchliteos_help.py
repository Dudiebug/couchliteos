"""The tour after setup and the HELP pages, as plain Python.

couchliteos-tv.py draws both with couchliteos_gtk_help; the classic launcher shows the same HELP
topics as a text screen (run_classic). Nothing here imports GTK, and curses only inside
run_classic, so the TV interface and the tests load this without either.

The tour: five short cards (moving around, streaming, apps, the quick menu and Home, settings
and HELP), shown once after the setup wizard. It takes the place of the CONTROLLER BUTTONS
screen the wizard used to end with, so showing it also marks that one as shown. An upgrade
that showed What's New marks it seen as well: one notice per start, and someone who already
knows the box is not walked through it again. HELP > THE TOUR shows it again any time.

A card's buttons are written in the connected pad's own symbols (quick.glyphs, controls'
button names) with the keyboard key alongside, so a keyboard user reads the same card.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import textwrap
from collections.abc import Callable, Sequence

import couchliteos_controls as controls
import couchliteos_input as inputprefs
import couchliteos_quick as quick
import couchliteos_stream as stream
import couchliteos_theme as theme

STATE = pathlib.Path(os.environ.get("COUCHLITEOS_STATE_DIR", "/var/lib/couchliteos"))
RUN = pathlib.Path(os.environ.get("COUCHLITEOS_RUN_DIR", "/run/couchliteos"))
SETUP_MARKER = STATE / "setup-complete"
SEEN = STATE / "tutorial-seen"
# /run is always writable: if SEEN cannot be saved (disk full, read-only), the tour still does
# not come back when the TV interface restarts during the same boot.
SESSION_SEEN = RUN / "tutorial-seen"

# What a key did in the tour or in HELP (the TV interface acts on it).
NEXT, PREVIOUS, DONE, TOUR, CLOSE, SCROLL_UP, SCROLL_DOWN = (
    "next", "previous", "done", "tour", "close", "scroll-up", "scroll-down")

# ---------------------------------------------------------------------- the tour


@dataclasses.dataclass(frozen=True)
class Chip:
    """One button on a card: `button` (a quick.BUTTONS action, or "move", "guide", "home",
    "hold-x", "phone") and what it does."""

    button: str
    words: str


@dataclasses.dataclass(frozen=True)
class Card:
    key: str
    icon: str  # a bundled icon (couchliteos_icons), as the XMB draws it
    title: str
    lines: tuple[str, ...]  # may name {guide}, the pad's own name for its Guide button
    chips: tuple[Chip, ...] = ()


REMOTE_LINE = "A TV REMOTE WORKS TOO, WITH HDMI-CEC: SETTINGS > TV CONTROL."
HELP_LINE = "HELP, AT THE BOTTOM OF SETTINGS, HAS SHORT ANSWERS AND THIS TOUR AGAIN."
ADD_LINE = "ADD A WEB BROWSER, OR ANY WEBSITE AS AN APP, IN SETTINGS > APPLICATIONS."


def tour_cards(cross: bool = True) -> tuple[Card, ...]:
    """The tour's cards, for the XMB (`cross`) or the rows of tiles."""
    return (
        Card("move", "game-controller", "MOVING AROUND", (
            ("LEFT AND RIGHT CHOOSE A CATEGORY: GAMES, APPS, SETTINGS AND MORE. UP AND DOWN CHOOSE WHAT IS IN IT."
             if cross else "THE ARROWS MOVE BETWEEN THE TILES: GAMES FIRST, THEN APPS, THEN SETTINGS AND POWER."),
            REMOTE_LINE,
        ), (Chip("move", "MOVE"), Chip("activate", "OPEN"), Chip("back", "GO BACK"))),
        Card("stream", "desktop-tower", "GAMES FROM YOUR GAMING PC", (
            "INSTALL SUNSHINE ON YOUR GAMING PC, THEN PAIR IT: SETTINGS > STREAMING > PAIR A GAMING PC.",
            "ITS GAMES APPEAR IN " + ("GAMES" if cross else "THE FIRST ROW") + ", THE LAST ONE PLAYED FIRST.",
        ), (Chip("activate", "STREAM THE GAME"), Chip("hold-x", "THAT GAME'S STREAM SETTINGS"))),
        Card("apps", "squares-four", "APPS AND WEBSITES", (
            ("TV & VIDEO HOLDS STREAMING SITES AND WEB BROWSERS. APPS HOLDS THE REST."
             if cross else "YOUR APPS ARE IN THE ROW UNDER THE GAMES."),
            ADD_LINE,
        ), (Chip("keyboard", "ON-SCREEN KEYBOARD"), Chip("phone", "TYPE ON YOUR PHONE, IN A TEXT FIELD"))),
        Card("buttons", "stack", "QUICK MENU AND HOME", (
            "THE QUICK MENU HAS THE VOLUME, CONTROLLER BATTERIES, RUNNING APPS, SLEEP AND TURN OFF.",
            "IN A STREAM, {guide} GOES TO THE GAMING PC: HOLD THE HOME SHORTCUT TO COME BACK.",
        ), (Chip("guide", "QUICK MENU, OVER ANY APP"), Chip("home", "HOME, ALSO FROM A STREAM"))),
        Card("help", "question", "SETTINGS AND HELP", (
            ("SETTINGS, TWO STEPS LEFT OF GAMES, HAS WI-FI, BLUETOOTH, CONTROLLERS, DISPLAY AND MORE."
             if cross else "SETTINGS IS IN THE LAST ROW, NEXT TO POWER: WI-FI, BLUETOOTH, CONTROLLERS AND MORE."),
            HELP_LINE,
            *(("POWER, AT THE FAR LEFT, HAS SLEEP, RESTART AND SHUT DOWN.",) if cross else ()),
        )),
    )


def pad_name(family: str, index: int) -> str:
    """controls.FAMILY_NAMES' name of button `index` (6 VIEW, 7 MENU, 8 GUIDE) for `family`."""
    return controls.FAMILY_NAMES.get(family, controls.GENERIC)[index]


def guide_name(family: str) -> str:
    name = pad_name(family, 8)
    return "HOME BUTTON" if name == "HOME" else name  # Nintendo's: not the keyboard's Home key


def chip_keys(button: str, family: str, settings: inputprefs.Settings | None = None) -> str:
    """What to press for a chip: the pad's button and the keyboard key that does the same."""
    settings = settings or inputprefs.Settings()
    marks = quick.glyphs(family)
    if button in marks:
        return f"{marks[button]} OR {quick.KEYBOARD[button]}"
    if button == "move":
        return "D-PAD OR ARROW KEYS"
    if button == "hold-x":  # gamepad-nav sends F10 while X / Triangle is held
        return f"HOLD {marks['keyboard']} OR F10"
    if button == "phone":  # a VIEW tap is F7 to the launcher; F2 on a keyboard
        return f"{pad_name(family, 6)} OR F2"
    if button == "guide":
        return f"{guide_name(family)} OR HOME KEY"
    if button == "home":  # Settings > CONTROLS: the held pad combo and the keyboard chord
        pad = ("HOLD L3+R3" if settings.home == inputprefs.HOME_STICKS
               else f"HOLD {pad_name(family, 6)}+{pad_name(family, 7)}")
        chord = inputprefs.chord_label(settings.keyboard_home)
        return pad if chord == "OFF" else f"{pad} OR {chord}"
    raise ValueError(f"no such button: {button}")


@dataclasses.dataclass(frozen=True)
class CardView:
    """One card as the TV draws it."""

    key: str
    icon: str
    title: str
    lines: tuple[str, ...]
    chips: tuple[tuple[str, str], ...]  # (what to press, what it does)
    prompt: str  # the prompt bar
    index: int
    count: int


class Tour:
    """The cards and which one is up. RIGHT / A goes on (DONE after the last), LEFT / B goes back
    (B on the first card leaves), Y / Delete skips the rest."""

    def __init__(self, cards: Sequence[Card]) -> None:
        self.cards = tuple(cards)
        self.index = 0

    @property
    def card(self) -> Card:
        return self.cards[self.index]

    @property
    def last(self) -> bool:
        return self.index == len(self.cards) - 1

    def key(self, name: str) -> str | None:
        if name in ("activate", "right"):
            if self.last:
                return DONE if name == "activate" else None
            self.index += 1
            return NEXT
        if name in ("back", "left"):
            if self.index == 0:
                return DONE if name == "back" else None
            self.index -= 1
            return PREVIOUS
        if name == "close":
            return DONE
        return None

    def prompt_entries(self) -> tuple[tuple[str, str], ...]:
        if self.index == 0:
            return (("activate", "NEXT"), ("back", "SKIP"))
        if self.last:
            return (("activate", "DONE"), ("back", "BACK"))
        return (("activate", "NEXT"), ("back", "BACK"), ("close", "SKIP"))

    def view(self, family: str, settings: inputprefs.Settings | None = None) -> CardView:
        card = self.card
        guide = guide_name(family)
        return CardView(
            card.key, card.icon, card.title, tuple(line.format(guide=guide) for line in card.lines),
            tuple((chip_keys(chip.button, family, settings), chip.words) for chip in card.chips),
            quick.prompt(family, self.prompt_entries()), self.index, len(self.cards),
        )


def tour_due(markers: Sequence[pathlib.Path] = (SEEN, SESSION_SEEN), setup_marker: pathlib.Path = SETUP_MARKER) -> bool:
    """After setup is complete, until the tour was shown once (on this disk, or this boot)."""
    try:
        return setup_marker.exists() and not any(path.exists() for path in markers)
    except OSError:
        return False


def mark_tour_seen(markers: Sequence[pathlib.Path] = (SEEN, SESSION_SEEN),
                   controls_marker: pathlib.Path = controls.SHOWN) -> None:
    """Before the tour is drawn, so a crash while it is up cannot bring it back at every start.
    The tour holds the buttons the CONTROLLER BUTTONS screen showed once: that one is done too."""
    for path in (*markers, controls_marker):
        try:
            stream._atomic_write(path, b"1\n")
        except (OSError, ValueError):
            pass  # the worst case is that it shows again


# ---------------------------------------------------------------------- HELP


@dataclasses.dataclass(frozen=True)
class Topic:
    key: str
    title: str  # short: it is a row in the list
    lines: tuple[str, ...]  # paragraphs, wrapped by whoever draws them
    action: str = ""  # TOUR: A / Enter starts the tour instead of reading


def topics(cross: bool = True, tour: bool = True) -> tuple[Topic, ...]:
    """HELP's topics, short and in the words the screens use. `tour`: THE TOUR first (the classic
    interface has no tour)."""
    first = (Topic("tour", "THE TOUR", (
        "FIVE SHORT CARDS: MOVING AROUND, YOUR GAMING PC, APPS, THE QUICK MENU AND SETTINGS.",
        "THEY CAME UP ONCE AFTER SETUP. OPEN THE TOUR TO SEE THEM AGAIN.",
    ), TOUR),) if tour else ()
    return first + (
        Topic("pc", "ADDING A GAMING PC", (
            "YOUR GAMING PC NEEDS SUNSHINE, RUNNING, ON THE SAME HOME NETWORK AS THIS BOX.",
            "CHOOSE SETTINGS > STREAMING > PAIR A GAMING PC, THEN SEARCH THE NETWORK (RECOMMENDED) AND PICK YOUR PC.",
            "A 4 DIGIT PIN APPEARS ON THE TV. ON THE GAMING PC, OPEN SUNSHINE'S WEB PAGE "
            "(HTTPS://LOCALHOST:47990), CLICK THE PIN TAB AND TYPE THE PIN.",
            "ITS GAMES THEN APPEAR IN " + ("GAMES" if cross else "THE FIRST ROW") + ", THE LAST ONE PLAYED FIRST.",
            "A SLEEPING GAMING PC IS WOKEN UP WHEN YOU START A GAME. WAKE PC IS IN SETTINGS > STREAMING.",
        )),
        Topic("streaming", "STREAMING TIPS", (
            "A NETWORK CABLE, ON THE GAMING PC OR ON THIS BOX, HELPS MORE THAN ANY SETTING.",
            "SETTINGS > STREAMING > STREAM CHECK TESTS THE NETWORK TO THE GAMING PC AND SAYS WHAT TO CHANGE.",
            "OPTIMIZE STREAM SETTINGS PICKS SETTINGS FOR THIS TV AND NETWORK. "
            "SMOOTHER STREAM LOWERS THE QUALITY ONE STEP.",
            "HOLD X / TRIANGLE (OR F10) ON A GAME FOR ITS OWN STREAM SETTINGS: PERFORMANCE, BALANCED OR QUALITY.",
            "IN A STREAM, GUIDE / PS GOES TO THE GAMING PC. HOLD SELECT+START (VIEW+MENU) TO COME BACK HERE.",
        )),
        Topic("controllers", "CONTROLLERS AND BLUETOOTH", (
            "XBOX, PLAYSTATION, NINTENDO-STYLE AND OTHER PADS WORK, BY USB OR BLUETOOTH.",
            "TO PAIR ONE, OPEN SETTINGS > BLUETOOTH AND PUT THE PAD IN PAIRING MODE. XBOX: HOLD THE PAIR "
            "BUTTON ON TOP. PS4: HOLD SHARE + PS. PS5: HOLD CREATE + PS. SWITCH PRO: HOLD THE SYNC BUTTON.",
            "SETTINGS > CONTROLLERS SHOWS EACH PAD WITH ITS BATTERY, SETS THE PLAYER ORDER, MAKES A PAD "
            "RUMBLE (IDENTIFY), TESTS ITS BUTTONS AND SWAPS A/B AND X/Y.",
            "HEADPHONES AND SPEAKERS PAIR IN SETTINGS > BLUETOOTH TOO. THE SOUND MOVES TO THEM WHEN THEY CONNECT.",
            "EVERY BUTTON: SETTINGS > CONTROLS > CONTROLLER BUTTONS.",
        )),
        Topic("phone", "TYPE ON YOUR PHONE", (
            "IN A TEXT FIELD, PRESS SELECT (VIEW) OR F2. A QR CODE APPEARS.",
            "SCAN IT WITH A PHONE ON THE SAME HOME NETWORK, TYPE ON THE PHONE AND SEND, THEN CONFIRM ON THE TV.",
            "THE CODE WORKS ONCE AND RUNS OUT AFTER 5 MINUTES.",
            "THE TEXT IS NOT ENCRYPTED ON ITS WAY. FOR A PASSWORD, USE IT ONLY ON A HOME NETWORK YOU TRUST.",
            "X / TRIANGLE (OR F12) OPENS THE ON-SCREEN KEYBOARD INSTEAD.",
        )),
        Topic("apps", "APPS AND WEBSITES", (
            "SETTINGS > APPLICATIONS > ADD A WEB BROWSER INSTALLS FIREFOX OR GOOGLE CHROME FROM THE INTERNET.",
            "ADD WEB APPLICATION, IN THE SAME PLACE, MAKES ANY WEBSITE AN APP: A NAME AND AN ADDRESS. "
            "IT OPENS FULL SCREEN.",
            "ON A WEBSITE THE CONTROLLER IS A MOUSE: THE LEFT STICK MOVES THE POINTER AND A / CROSS CLICKS.",
            ("STREAMING SITES GO IN TV & VIDEO, OTHER APPS IN APPS." if cross
             else "YOUR APPS ARE IN THE ROW UNDER THE GAMES."),
            "ACTIVE APPLICATIONS BRINGS BACK OR CLOSES AN APP THAT IS STILL RUNNING.",
        )),
        Topic("network", "WI-FI AND NETWORK", (
            "A NETWORK CABLE WORKS AS SOON AS IT IS PLUGGED IN.",
            "FOR WI-FI, CHOOSE SETTINGS > NETWORK > JOIN A WI-FI NETWORK, PICK YOURS AND TYPE THE PASSWORD.",
            "WI-FI PASSWORD CHANGED? CHOOSE YOUR NETWORK AGAIN AND TYPE THE NEW ONE.",
            *(("NETWORK, AT THE FAR RIGHT, SHOWS WHETHER THIS BOX IS ONLINE.",) if cross else ()),
            "TAILSCALE REACHES YOUR GAMING PC AWAY FROM HOME.",
        )),
        Topic("updates", "UPDATES", (
            "COUCHLITEOS LOOKS FOR A NEW VERSION AT EVERY START AND ASKS BEFORE IT INSTALLS ONE. "
            "SETTINGS > CHECK FOR UPDATES TURNS THAT ON OR OFF.",
            "SETTINGS > SOFTWARE UPDATE INSTALLS IT AND KEEPS YOUR SETTINGS AND PAIRINGS.",
            "IT SAVES THE CURRENT SYSTEM FIRST: IF THE NEW VERSION MISBEHAVES, RESTORE PREVIOUS VERSION PUTS IT BACK.",
            "SECURITY FIXES AND APP UPDATES INSTALL BY THEMSELVES ONCE A DAY, NEVER DURING A STREAM.",
            "BETA VERSIONS: SETTINGS > SOFTWARE UPDATE > UPDATE CHANNEL.",
        )),
        Topic("power", "SLEEP AND POWER", (
            ("POWER, AT THE FAR LEFT, HAS SLEEP, RESTART AND SHUT DOWN. SO DOES THE QUICK MENU." if cross
             else "THE POWER TILE HAS SLEEP, RESTART AND SHUT DOWN. SO DOES THE QUICK MENU."),
            "HOLD GUIDE / PS FOR 5 SECONDS TO SLEEP (NEVER DURING A GAME).",
            "SETTINGS > SLEEP & SCREEN SETS WHEN THE SCREEN GOES BLANK AND WHEN THE BOX SLEEPS.",
            "A CONTROLLER OR KEYBOARD WAKES THE BOX WHERE THE PC CAN DO IT; ELSE PRESS ITS POWER BUTTON.",
            "SETTINGS > TV CONTROL TURNS THE TV ON AND OFF WITH THE BOX (HDMI-CEC).",
        )),
        Topic("support", "TROUBLESHOOTING", (
            "AN UPDATE BROKE SOMETHING? SETTINGS > SOFTWARE UPDATE > RESTORE PREVIOUS VERSION.",
            "IF THE BOX NO LONGER STARTS, THE BOOT MENU STAYS UP FOR 5 SECONDS AT THE NEXT START: "
            "CHOOSE COUCHLITEOS: RESTORE PREVIOUS VERSION.",
            "A TEXT MENU INSTEAD OF THIS HOME SCREEN MEANS THE TV INTERFACE COULD NOT START; THE CLASSIC ONE TOOK OVER.",
            "THE TV CUTS OFF THE PICTURE? SETTINGS > DISPLAY > SCREEN EDGES.",
            "FOR A BUG REPORT, PLUG IN A USB DRIVE (ON A LIVE USB STICK, A SECOND ONE) AND CHOOSE "
            "SETTINGS > GENERATE SUPPORT FILE. ATTACH THE FILE IT SAVES.",
            "THE USER GUIDE: GITHUB.COM/DUDIEBUG/COUCHLITEOS/WIKI",
        )),
    )


class HelpModel:
    """The topic list and the focused topic's text beside it. UP / DOWN choose a topic; RIGHT or
    A / Enter go into its text (or start THE TOUR), where UP / DOWN scroll; LEFT or B / Esc come
    back out, and B / Esc on the list closes HELP."""

    def __init__(self, items: Sequence[Topic]) -> None:
        self.topics = tuple(items)
        self.focus = 0
        self.reading = False

    @property
    def topic(self) -> Topic:
        return self.topics[self.focus]

    def key(self, name: str) -> str | None:
        if self.reading:
            if name in ("up", "down"):
                return SCROLL_UP if name == "up" else SCROLL_DOWN
            if name in ("back", "left", "activate"):
                self.reading = False
            return None
        if name in ("up", "down"):
            self.focus = max(0, min(len(self.topics) - 1, self.focus + (1 if name == "down" else -1)))
            return None
        if name in ("activate", "right"):
            if self.topic.action:
                return self.topic.action
            self.reading = True
            return None
        if name == "back":
            return CLOSE
        return None

    def visible(self, slots: int) -> range:
        """The rows on screen: a long list scrolls so the focus stays in the middle."""
        slots = max(1, slots)
        if len(self.topics) <= slots:
            return range(len(self.topics))
        start = max(0, min(self.focus - slots // 2, len(self.topics) - slots))
        return range(start, start + slots)

    def prompt(self, family: str) -> str:
        if self.reading:
            return f"UP / DOWN SCROLLS    {quick.prompt(family, (('back', 'BACK'),))}"
        open_words = "START THE TOUR" if self.topic.action == TOUR else "READ"
        return quick.prompt(family, (("activate", open_words), ("back", "BACK")))


# ---------------------------------------------------------------------- the classic text screen

CLASSIC_TITLE = "HELP"
CLASSIC_LIST_HINT = "A / CROSS OR ENTER READS  ·  B / CIRCLE OR ESC GOES BACK"
CLASSIC_TEXT_HINT = "UP / DOWN SCROLLS  ·  B / CIRCLE OR ESC GOES BACK"


def classic_lines(topic: Topic, width: int) -> list[str]:
    """`topic`'s paragraphs wrapped to `width` columns, a blank line between them."""
    lines: list[str] = []
    for paragraph in topic.lines:
        if lines:
            lines.append("")
        lines.extend(textwrap.wrap(paragraph, width=max(16, width)))
    return lines


def run_classic(screen, read_key: Callable[[], int], items: Sequence[Topic] | None = None) -> None:
    """Settings > HELP in the classic launcher: the topics, then one topic's text, until B / Esc."""
    import curses  # here, not at the top: the TV interface loads this module without a terminal

    items = tuple(items or topics(cross=False, tour=False))
    enter = (curses.KEY_ENTER, 10, 13)
    selected, reading, top = 0, False, 0
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        try:
            screen.border()
        except curses.error:
            pass
        title_row, bottom = (2, height - 3) if height >= 16 else (1, height - 2)
        room = max(1, bottom - title_row - 3)
        if reading:
            topic = items[selected]
            lines = classic_lines(topic, min(width - 8, 76))
            top = max(0, min(top, len(lines) - room))
            _put_centred(screen, title_row, topic.title)
            for offset, line in enumerate(lines[top:top + room]):
                _put(screen, title_row + 2 + offset, max(2, (width - min(width - 8, 76)) // 2), line)
            hint = CLASSIC_TEXT_HINT
        else:
            _put_centred(screen, title_row, CLASSIC_TITLE)
            first = max(0, min(selected - room // 2, len(items) - room))
            left = max(2, (width - max(len(item.title) for item in items)) // 2 - 3)
            for offset, item in enumerate(items[first:first + room]):
                mark = ">" if first + offset == selected else " "
                _put(screen, title_row + 2 + offset, left, f"{mark}  {item.title}")
            hint = CLASSIC_LIST_HINT
        _put_centred(screen, bottom, hint)
        screen.refresh()
        key = read_key()
        if reading:
            if key == curses.KEY_UP:
                top = max(0, top - 1)
            elif key == curses.KEY_DOWN:
                top += 1
            elif key in (27, curses.KEY_LEFT, *enter):
                reading = False
        elif key == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif key == curses.KEY_DOWN:
            selected = min(len(items) - 1, selected + 1)
        elif key in (curses.KEY_RIGHT, *enter):
            reading, top = True, 0
        elif key == 27:
            return


def _put(screen, row: int, column: int, text: str) -> None:
    height, width = screen.getmaxyx()
    if 0 < row < height - 1 and 0 <= column < width - 1:
        try:
            screen.addnstr(row, column, text, width - column - 2)
        except Exception:  # noqa: BLE001 - curses.error: a cramped terminal draws what fits
            pass


def _put_centred(screen, row: int, text: str) -> None:
    _height, width = screen.getmaxyx()
    _put(screen, row, max(1, (width - len(text)) // 2), text)


# ---------------------------------------------------------------------- style


def _rgba(colour: str, alpha: float) -> str:
    value = theme.parse_colour(colour)
    red, green, blue = (int(value[at:at + 2], 16) for at in (0, 2, 4))
    return f"rgba({red}, {green}, {blue}, {alpha:g})"


def stylesheet(colours: theme.Theme, layout) -> str:
    """CSS for the tour and HELP (added to tvlayout.stylesheet); `layout` is a tvlayout.Layout.

    The tour is a PS3-style band across the whole screen: thin rules above and below, the
    theme's background showing the wave through it, the card in it."""
    c = {field: f"#{value}" for field, value in colours.colours.items()}
    f, px = layout.fonts, layout.px
    radius = px(14)
    return f"""
.tv-band {{ background-color: {_rgba(c['background'], 0.62)}; }}
.tv-rule {{ background-color: {_rgba(c['text'], 0.45)}; min-height: {max(1, px(2))}px; }}
.tv-card-icon {{ min-width: {px(200)}px; min-height: {px(200)}px; }}
.tv-card-title {{ font-size: {f['title']}px; font-weight: bold; color: {c['text']}; }}
.tv-card-line {{ font-size: {f['body']}px; color: {c['text']}; }}
.tv-chip-keys {{ font-size: {f['body']}px; font-weight: bold; color: {c['accent']};
  background-color: {_rgba(c['surface'], 0.85)}; border-radius: {radius}px; padding: {px(6)}px {px(18)}px; }}
.tv-chip-words {{ font-size: {f['body']}px; color: {c['text']}; }}
.tv-dot {{ min-width: {px(16)}px; min-height: {px(16)}px; border-radius: {px(8)}px;
  background-color: {_rgba(c['text'], 0.3)}; }}
.tv-dot.tv-on {{ background-color: {c['accent']}; }}
.tv-help-pane {{ background-color: {c['surface']}; border-radius: {radius}px; padding: {px(28)}px;
  border: {px(4)}px solid transparent; }}
.tv-help-pane.tv-reading {{ border-color: {c['accent']}; }}
.tv-help-title {{ font-size: {f['title']}px; font-weight: bold; color: {c['text']}; }}
.tv-help-line {{ font-size: {f['body']}px; color: {c['text']}; }}
.tv-list .tv-item.tv-open {{ background-color: {c['surface']}; }}
"""
