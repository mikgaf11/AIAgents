"""Reading secrets from a terminal without it looking broken.

`getpass` echoes absolutely nothing — no dots, no asterisks, no cursor
movement — which reliably reads as a frozen prompt. People retype, paste
again, and give up. So: say up front that typing is invisible, confirm
afterwards that something was received, and fall back to visible input on
consoles that can't hide it at all (IDE run windows, some Windows shells)
rather than failing outright.
"""

from __future__ import annotations

import getpass
import sys


def mask(secret: str) -> str:
    """A recognisable fingerprint of a secret, safe to print."""
    if not secret:
        return "(empty)"
    if len(secret) <= 8:
        return f"{secret[0]}{'•' * (len(secret) - 1)}"
    return f"{secret[:6]}{'•' * 6}{secret[-4:]}  ({len(secret)} characters)"


def read_secret(prompt: str, *, allow_visible: bool = True) -> str:
    """Prompt for a secret, explaining that the typing will not appear."""
    print()
    print("  Your typing will NOT appear on screen — no dots, no stars, nothing.")
    print("  That is normal. Paste or type it, then press Enter.")
    print("  Paste is Cmd+V on macOS, Ctrl+Shift+V or right-click in most")
    print("  terminals on Windows and Linux.")
    print()

    try:
        secret = getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""
    except Exception:  # noqa: BLE001 - console can't hide input at all
        if not allow_visible:
            return ""
        print("  This console cannot hide input, so it will be visible as you type.")
        try:
            secret = input(prompt)
        except (EOFError, KeyboardInterrupt):
            return ""

    secret = secret.strip()
    if secret:
        # Prove it landed, without printing the secret itself.
        print(f"  got it: {mask(secret)}")
    return secret


def supports_interaction() -> bool:
    """False when there is no real terminal to prompt on."""
    return sys.stdin is not None and sys.stdin.isatty()
