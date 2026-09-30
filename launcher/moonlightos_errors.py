#!/usr/bin/python3
"""Launcher error screens that say what to do next and offer the obvious buttons.

A failure is a title, what went wrong, one sentence on what to do about it, and
a short list of buttons (`Action`). Every screen ends with BACK, and Escape (the
controller's B button) always means BACK.

Adding a button for a particular kind of failure, for example WAKE PC for a
Moonlight host that does not answer, takes two lines:

    errors.register_action(errors.Action("WAKE PC", "wake-pc"), app_ids=("moonlight",))
    launcher.failure_actions["wake-pc"] = launcher.wake_pc   # called with the app;
                                                             # return True to try again

Registered buttons come before the standard ones. `kinds` selects by kind of
failure ("network", "bluetooth", "app") instead of by application.
"""

from __future__ import annotations

import curses
import dataclasses
import re
import textwrap
from collections.abc import Callable, Sequence

ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
ESCAPE = 27
MAX_DETAIL = 400


@dataclasses.dataclass(frozen=True)
class Action:
    label: str
    id: str


DISMISS = Action("BACK", "dismiss")
RETRY = Action("TRY AGAIN", "retry")
NETWORK = Action("NETWORK SETTINGS", "network")
SUPPORT = Action("SAVE SUPPORT FILE", "support")
BLUETOOTH = Action("BLUETOOTH SETTINGS", "bluetooth")

# Applications that cannot work without a network connection.
NETWORK_APP_IDS = frozenset({"moonlight", "chiaki-ng", "firefox", "google-chrome", "tailscale"})
# The application that is the network settings screen: it must not offer to open itself.
NETWORK_SETUP_ID = "network-setup"

NETWORK_WORDS = re.compile(
    r"could not reach|unreachable|no route to host|network is down|name or service not known|"
    r"name resolution|connection was lost|\bnetwork\b|\boffline\b",
    re.IGNORECASE,
)
BLUETOOTH_WORDS = re.compile(r"bluetooth", re.IGNORECASE)

HINTS = {
    "network-offline": (
        "THIS PC IS NOT CONNECTED TO A NETWORK. OPEN NETWORK SETTINGS TO JOIN WI-FI OR PLUG IN "
        "ETHERNET, THEN TRY AGAIN."
    ),
    "network": (
        "THE OTHER COMPUTER MAY BE OFF OR ASLEEP, OR THIS PC MAY BE ON A DIFFERENT NETWORK. "
        "CHECK NETWORK SETTINGS, THEN TRY AGAIN."
    ),
    "network-setup": "CHECK THE CABLE OR WI-FI AND TRY AGAIN.",
    "bluetooth": "OPEN BLUETOOTH SETTINGS TO TURN BLUETOOTH ON OR PAIR THE DEVICE AGAIN, THEN TRY AGAIN.",
    "rdp": "CHECK THE CONNECTION UNDER SETTINGS > REMOTE DESKTOP, THEN TRY AGAIN.",
    "app": "TRY AGAIN. IF IT FAILS AGAIN, SAVE A SUPPORT FILE TO A USB DRIVE SO IT CAN BE DIAGNOSED.",
}
BASE_ACTIONS = {
    "network": (NETWORK, RETRY),
    "bluetooth": (BLUETOOTH, RETRY),
    "app": (RETRY, SUPPORT),
}

# (action, kinds, app ids) added with register_action().
_EXTRA: list[tuple[Action, tuple[str, ...], tuple[str, ...]]] = []


@dataclasses.dataclass(frozen=True)
class Failure:
    title: str
    detail: str
    hint: str
    kind: str
    actions: tuple[Action, ...]


def register_action(action: Action, *, kinds: Sequence[str] = (), app_ids: Sequence[str] = ()) -> None:
    """Offer `action` on failures of these kinds or of these applications."""
    entry = (action, tuple(kinds), tuple(app_ids))
    if entry not in _EXTRA:
        _EXTRA.append(entry)


def actions_for(kind: str, app_id: str = "", *, retry: bool = True) -> tuple[Action, ...]:
    chosen: list[Action] = []
    for action, kinds, app_ids in _EXTRA:
        if kind in kinds or (app_id and app_id in app_ids):
            chosen.append(action)
    chosen.extend(BASE_ACTIONS.get(kind, (RETRY,)))
    chosen.append(DISMISS)
    result: list[Action] = []
    for action in chosen:
        if action.id in {item.id for item in result}:
            continue
        if action.id == NETWORK.id and app_id == NETWORK_SETUP_ID:
            continue
        if action.id == RETRY.id and not retry:
            continue
        result.append(action)
    return tuple(result)


def _clean(message: str) -> str:
    text = " ".join(str(message).split()).upper()[:MAX_DETAIL]
    return text or "UNKNOWN ERROR"


def classify(message: str, *, app_id: str = "", app_kind: str = "", online: bool | None = None) -> str:
    """"network", "bluetooth" or "app" (anything else that went wrong while starting something)."""
    if NETWORK_WORDS.search(message):
        return "network"
    if BLUETOOTH_WORDS.search(message):
        return "bluetooth"
    if online is False and (app_kind == "rdp" or app_id in NETWORK_APP_IDS):
        return "network"
    return "app"


def problem(
    title: str,
    message: str,
    *,
    kind: str = "app",
    app_id: str = "",
    app_kind: str = "",
    online: bool | None = None,
    retry: bool = True,
) -> Failure:
    if kind == "network":
        hint = HINTS["network-setup"] if app_id == NETWORK_SETUP_ID else HINTS["network-offline" if online is False else "network"]
    elif kind == "app" and app_kind == "rdp":
        hint = HINTS["rdp"]
    else:
        hint = HINTS.get(kind, HINTS["app"])
    return Failure(title, _clean(message), hint, kind, actions_for(kind, app_id, retry=retry))


def describe_failure(
    label: str,
    message: str,
    *,
    app_id: str = "",
    app_kind: str = "",
    online: bool | None = None,
    retry: bool = True,
) -> Failure:
    """The screen for an application that would not start or could not connect."""
    kind = classify(message, app_id=app_id, app_kind=app_kind, online=online)
    return problem(
        f"{label} FAILED TO START", message, kind=kind, app_id=app_id, app_kind=app_kind, online=online, retry=retry
    )


def simple_failure(
    title: str, message: str, *actions: Action, hint: str = "", retry: bool = False
) -> Failure:
    """A failure with exactly the buttons the caller names (then TRY AGAIN if `retry`, then BACK)."""
    buttons = [*actions, *([RETRY] if retry else []), DISMISS]
    unique = tuple({action.id: action for action in buttons}.values())
    return Failure(title, _clean(message), hint.upper(), "generic", unique)


class ActionMenu:
    """Which button is highlighted, and what a key press chooses. No drawing."""

    def __init__(self, actions: Sequence[Action]) -> None:
        if not actions:
            raise ValueError("an error screen needs at least one action")
        self.actions = tuple(actions)
        self.selected = 0

    @property
    def current(self) -> Action:
        return self.actions[self.selected]

    def handle_key(self, key: int) -> str | None:
        """Return the chosen action id, or None when the key only moved the highlight (or did nothing)."""
        if key in (curses.KEY_UP, ord("k")):
            self.selected = (self.selected - 1) % len(self.actions)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected = (self.selected + 1) % len(self.actions)
        elif key in ENTER_KEYS:
            return self.current.id
        elif key == ESCAPE:
            return DISMISS.id
        return None


def _centered(screen: "curses.window", row: int, text: str) -> None:
    height, width = screen.getmaxyx()
    if not 0 <= row < height or width < 2:
        return
    clipped = text[: max(0, width - 4)]
    try:
        screen.addstr(row, max(1, (width - len(clipped)) // 2), clipped)
    except curses.error:
        pass


def draw(screen: "curses.window", failure: Failure, menu: ActionMenu) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    if height >= 8 and width >= 24:
        try:
            screen.border(ord("|"), ord("|"), ord("-"), ord("-"), ord("+"), ord("+"), ord("+"), ord("+"))
        except curses.error:
            pass
    _centered(screen, max(2, height // 8), failure.title)
    wrap = max(8, width - 8)
    rows = textwrap.wrap(failure.detail, width=wrap)
    if failure.hint:
        rows += [""] + textwrap.wrap(failure.hint, width=wrap)
    first = max(4, height // 8 + 3)
    rows = rows[: max(1, height - first - len(menu.actions) - 5)]
    for offset, row in enumerate(rows):
        _centered(screen, first + offset, row)
    widest = max(len(action.label) for action in menu.actions)
    left = max(2, (width - widest - 3) // 2)
    top = first + len(rows) + 1
    for index, action in enumerate(menu.actions):
        row = top + index
        if not 0 <= row < height:
            break
        marker = ">" if index == menu.selected else " "
        try:
            screen.addnstr(row, left, f"{marker}  {action.label}", max(1, width - left - 1))
        except curses.error:
            pass
    _centered(screen, height - 3, "UP/DOWN CHOOSES  -  A / CROSS SELECTS  -  B / CIRCLE GOES BACK")
    screen.refresh()


def show(
    screen: "curses.window",
    failure: Failure,
    read_key: Callable[["curses.window"], int] | None = None,
) -> str:
    """Show the failure until a button is chosen; return that button's id ("dismiss" for Escape)."""
    read = read_key or (lambda window: window.getch())
    menu = ActionMenu(failure.actions)
    screen.timeout(1000)
    while True:
        draw(screen, failure, menu)
        chosen = menu.handle_key(read(screen))
        if chosen is not None:
            return chosen
