#!/usr/bin/env python3
"""Connect JARVIS to your email.

Asks for your address and an app password, works out the server settings
from the domain, proves the login works before saving anything, and writes
the result to jarvis/.env.

    python3 connect.py            # set up email
    python3 connect.py --test     # just re-test the saved credentials
    python3 connect.py --forget   # remove the saved credentials
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from server.config import CONFIG  # noqa: E402
from server.connectors.email import (  # noqa: E402
    PROVIDERS,
    EmailConnector,
    guess_provider,
)

ENV_FILE = HERE / ".env"
MANAGED_KEYS = (
    "JARVIS_EMAIL_ADDRESS",
    "JARVIS_EMAIL_PASSWORD",
    "JARVIS_EMAIL_IMAP_HOST",
    "JARVIS_EMAIL_IMAP_PORT",
    "JARVIS_EMAIL_SMTP_HOST",
    "JARVIS_EMAIL_SMTP_PORT",
    "JARVIS_EMAIL_SMTP_SSL",
)


def write_env(values: dict[str, str]) -> None:
    """Update .env in place, preserving everything already in it."""
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    for key, value in values.items():
        replaced = False
        for index, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[index] = f"{key}={value}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        ENV_FILE.chmod(0o600)  # it holds a password now
    except OSError:
        pass


def strip_env(keys: tuple[str, ...]) -> None:
    if not ENV_FILE.exists():
        return
    kept = [
        line
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
        if not any(line.strip().startswith(f"{k}=") for k in keys)
    ]
    ENV_FILE.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")


async def test_connection(connector: EmailConnector) -> bool:
    print("  testing the connection…")
    result = await connector.probe()
    if result.get("ok"):
        print(f"  connected — {result['messages']} messages in {result['folder']}")
        return True
    print(f"  failed: {result.get('error')}")
    return False


async def setup() -> int:
    print("\n  Connect JARVIS to your email\n")
    print("  JARVIS reads new mail over IMAP, sorts it by how much it actually")
    print("  matters to you, and flags the important ones in your normal mail")
    print("  app. It can draft replies, but it never sends anything without you")
    print("  approving it first.\n")

    address = input("  Email address: ").strip()
    if not address or "@" not in address:
        print("  That isn't an email address. Nothing was saved.")
        return 1

    provider, settings = guess_provider(address)
    if provider:
        print(f"\n  Recognised {provider}.")
        print(f"  {settings['note']}\n")
        imap_host = settings["imap_host"]
        imap_port = settings["imap_port"]
        smtp_host = settings["smtp_host"]
        smtp_port = settings["smtp_port"]
        smtp_ssl = settings["smtp_ssl"]
    else:
        print("\n  I don't know that domain, so I need the server details.")
        print("  Your provider's help pages list them under 'IMAP settings'.\n")
        imap_host = input("  IMAP host (e.g. imap.example.com): ").strip()
        imap_port = int(input("  IMAP port [993]: ").strip() or 993)
        smtp_host = input("  SMTP host (e.g. smtp.example.com): ").strip()
        smtp_port = int(input("  SMTP port [587]: ").strip() or 587)
        smtp_ssl = smtp_port == 465
        if not imap_host:
            print("  No IMAP host given. Nothing was saved.")
            return 1

    print("  The password is not echoed, and is stored only in jarvis/.env.")
    password = getpass.getpass("  App password: ").strip()
    if not password:
        print("  No password given. Nothing was saved.")
        return 1
    # App passwords are often shown in groups of four; the spaces aren't part of it.
    password = re.sub(r"\s+", "", password)

    connector = EmailConnector(
        address, password,
        imap_host=imap_host, imap_port=imap_port,
        smtp_host=smtp_host, smtp_port=smtp_port, smtp_ssl=smtp_ssl,
    )

    print()
    if not await test_connection(connector):
        print("\n  Nothing was saved. Common causes:")
        print("   - using your normal password where an app password is required")
        print("   - two-factor authentication not enabled yet (most providers")
        print("     only offer app passwords once it is)")
        print("   - IMAP not switched on in your provider's settings\n")
        return 1

    write_env(
        {
            "JARVIS_EMAIL_ADDRESS": address,
            "JARVIS_EMAIL_PASSWORD": password,
            "JARVIS_EMAIL_IMAP_HOST": imap_host,
            "JARVIS_EMAIL_IMAP_PORT": str(imap_port),
            "JARVIS_EMAIL_SMTP_HOST": smtp_host,
            "JARVIS_EMAIL_SMTP_PORT": str(smtp_port),
            "JARVIS_EMAIL_SMTP_SSL": "1" if smtp_ssl else "0",
        }
    )
    print(f"\n  Saved to {ENV_FILE} (permissions 600).")

    print("\n  Sending is currently OFF. JARVIS will draft replies and wait for")
    print("  you to approve each one in the HUD. If you want approved drafts to")
    print("  actually go out, add this line to jarvis/.env:")
    print("      JARVIS_EMAIL_ALLOW_SEND=1")
    print("  Even then, nothing is sent until you press Approve.\n")
    print("  Restart JARVIS to pick this up. It will start triaging within a")
    print(f"  few minutes (polling every {int(CONFIG.email_poll_interval)}s).\n")
    return 0


async def test_saved() -> int:
    if not CONFIG.email_configured:
        print("\n  No email configured. Run: python3 connect.py\n")
        return 1
    from server.connectors.email import from_config

    connector = from_config(CONFIG)
    print(f"\n  Testing {CONFIG.email_address}…")
    ok = await test_connection(connector)
    print()
    return 0 if ok else 1


def forget() -> int:
    strip_env(MANAGED_KEYS)
    print("\n  Email credentials removed from .env. Restart JARVIS to apply.\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Connect JARVIS to your email.")
    parser.add_argument("--test", action="store_true", help="test saved credentials")
    parser.add_argument("--forget", action="store_true", help="remove credentials")
    args = parser.parse_args()

    if args.forget:
        return forget()
    if args.test:
        return asyncio.run(test_saved())
    try:
        return asyncio.run(setup())
    except (KeyboardInterrupt, EOFError):
        print("\n  Cancelled. Nothing was saved.\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
