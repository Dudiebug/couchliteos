#!/usr/bin/python3
"""Thin first-boot setup wizard for existing MoonlightOS settings and apps."""

from __future__ import annotations

import curses
import os
import pathlib
import tempfile
from collections.abc import Callable


MARKER = pathlib.Path("/var/lib/moonlightos/setup-complete")
STEPS = (
    ("network", "NETWORK", "OPEN NETWORK SETUP"),
    ("bluetooth", "BLUETOOTH", "OPEN BLUETOOTH"),
    ("display", "DISPLAY", "OPEN DISPLAY SETTINGS"),
    ("audio", "AUDIO", "TEST AUDIO"),
    ("controller", "CONTROLLER", ""),
    ("moonlight", "MOONLIGHT", "OPEN MOONLIGHT"),
    ("chiaki-ng", "CHIAKI-NG", "OPEN CHIAKI-NG"),
    ("tailscale", "TAILSCALE", "OPEN TAILSCALE SETUP"),
    ("applications", "OPTIONAL APPS", "OPEN APPLICATIONS"),
)


def write_complete(path: pathlib.Path = MARKER) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write("1\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def move(selected: int, key: int, count: int) -> int:
    if key == curses.KEY_UP:
        return (selected - 1) % count
    if key == curses.KEY_DOWN:
        return (selected + 1) % count
    return selected


class SetupWizard:
    def __init__(
        self,
        screen: curses.window,
        actions: dict[str, Callable[[], object]],
        statuses: dict[str, Callable[[], str]],
        *,
        marker: pathlib.Path = MARKER,
    ) -> None:
        self.screen = screen
        self.actions = actions
        self.statuses = statuses
        self.marker = marker

    def draw(self, title: str, details: list[str], choices: list[str], selected: int) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        try:
            self.screen.border()
        except curses.error:
            pass

        def center(row: int, text: str) -> None:
            clipped = text[: max(1, width - 4)]
            try:
                self.screen.addnstr(row, max(1, (width - len(clipped)) // 2), clipped, max(1, width - 2))
            except curses.error:
                pass

        center(max(2, height // 8), title)
        for offset, detail in enumerate(details):
            center(max(4, height // 4) + offset, detail)
        first = max(8, height // 2)
        for index, choice in enumerate(choices):
            center(first + index, f"{'>' if index == selected else ' '}  {choice}")
        center(height - 3, "A / CROSS SELECTS  ·  B / CIRCLE BACK  ·  X / TRIANGLE KEYBOARD")
        self.screen.refresh()

    def choose(self, title: str, details: list[str], choices: list[str]) -> int | None:
        selected = 0
        while True:
            self.draw(title, details, choices, selected)
            key = self.screen.getch()
            selected = move(selected, key, len(choices))
            if key in (curses.KEY_ENTER, 10, 13):
                return selected
            if key == curses.KEY_F12:
                action = self.actions.get("osk")
                if action:
                    action()
            elif key == 27:
                return None

    @staticmethod
    def controls(action_label: str) -> list[str]:
        return ([action_label] if action_label else []) + ["BACK", "CONTINUE", "SKIP"]

    def status(self, step_id: str) -> str:
        try:
            return self.statuses.get(step_id, lambda: "READY")()
        except Exception:
            return "STATUS UNAVAILABLE"

    def welcome(self) -> int | None:
        return self.choose(
            "WELCOME TO MOONLIGHTOS",
            ["SET UP NETWORK, INPUT, DISPLAY, AUDIO, AND STREAMING."],
            ["START SETUP", "SKIP SETUP", "EXIT TO LAUNCHER"],
        )

    def run(self, *, force: bool = False) -> bool:
        if self.marker.exists() and not force:
            return False
        welcome = self.welcome()
        if welcome is None or welcome == 2:
            return True
        if welcome == 1:
            write_complete(self.marker)
            return True

        index = 0
        while True:
            while index < len(STEPS):
                step_id, title, action_label = STEPS[index]
                choices = self.controls(action_label)
                selected = self.choose(
                    f"SETUP {index + 2}/11  ·  {title}", [self.status(step_id)], choices
                )
                if selected is None:
                    index = max(0, index - 1)
                    continue
                choice = choices[selected]
                if choice == action_label and action_label:
                    action = self.actions.get(step_id)
                    if action:
                        action()
                elif choice == "BACK":
                    if index:
                        index -= 1
                    else:
                        welcome = self.welcome()
                        if welcome is None or welcome == 2:
                            return True
                        if welcome == 1:
                            write_complete(self.marker)
                            return True
                else:
                    index += 1

            finish = self.choose(
                "SETUP 11/11  ·  FINISH",
                ["SETUP CAN BE RERUN FROM SETTINGS."],
                ["BACK", "FINISH"],
            )
            if finish == 0:
                index = len(STEPS) - 1
                continue
            if finish == 1:
                write_complete(self.marker)
            return True
