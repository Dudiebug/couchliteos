"""Settings > NETWORK for a controller: join Wi-Fi with the wizard's network step.

Everything is injected (UI, join step, launch callback), so the tests use fakes.
The password is typed into the wizard's masked text input and goes straight to
NetworkManager over D-Bus; this module never sees it, logs it or puts it in argv.
"""

from __future__ import annotations

from typing import Any, Callable

import moonlightos_setup as setup

TITLE = "NETWORK"
JOIN = "JOIN A WI-FI NETWORK"
NO_ADAPTER_ROW = "JOIN A WI-FI NETWORK  (NO WI-FI ADAPTER FOUND)"
ADVANCED = "ADVANCED NETWORK SETTINGS (NEEDS A KEYBOARD)"
BACK = "BACK"
PASSWORD_HINT = "WI-FI PASSWORD CHANGED? CHOOSE YOUR NETWORK AGAIN, TYPE THE NEW ONE."
NO_ADAPTER_NOTICE = "NO WI-FI ADAPTER ON THIS PC. PLUG IN A NETWORK CABLE."
CONNECTED = "NETWORK: CONNECTED"
NOT_CONNECTED = "NETWORK: NOT CONNECTED. TRY AGAIN, OR USE A NETWORK CABLE."
FAILED_TO_OPEN = "COULD NOT OPEN WI-FI SETUP. TRY AGAIN."


def header(summary: str, wifi_present: bool) -> list[str]:
    """Status line, plus the password hint when the PC is offline (usually a changed password)."""
    if summary.endswith("  ONLINE"):
        return [f"NOW: {summary}"]
    # The launcher's OFFLINE text points at this very screen, so drop the pointer.
    return [f"NOW: {summary.split(' - ')[0]}", PASSWORD_HINT]


def choices(wifi_present: bool) -> list[str]:
    return [JOIN if wifi_present else NO_ADAPTER_ROW, ADVANCED, BACK]


def result_status(outcome: str) -> str:
    """What Settings shows afterwards; nothing when the user backed out."""
    return {setup.DONE: CONNECTED, setup.FAILED: NOT_CONNECTED}.get(outcome, "")


def run(ui: Any, summary: str, wifi_present: bool, join: Callable[[], str], advanced: Callable[[], Any]) -> str:
    rows = choices(wifi_present)
    while True:
        index = ui.menu(TITLE, header(summary, wifi_present), rows)
        if index == 0 and wifi_present:
            return result_status(join())
        if index == 0:
            ui.menu(TITLE, [NO_ADAPTER_NOTICE], [setup.OK])
        elif index == 1:
            advanced()  # the launch reports its own result in Settings
            return ""
        else:
            return ""


def open(
    screen: Any, text_input: Callable[..., Any], advanced: Callable[[], Any], summary_fn: Callable[[], str],
    *, system: Any = None, ui: Any = None,
) -> str:
    """Show the menu on `screen`; return the status text for Settings ("" for none)."""
    try:
        ui = ui or setup.CursesUI(screen)
        system = system or setup.System()
        join = setup.SetupWizard(ui, {"text": text_input}, system).step_network
        return run(ui, summary_fn(), system.wifi_interface() is not None, join, advanced)
    except Exception:
        return FAILED_TO_OPEN
