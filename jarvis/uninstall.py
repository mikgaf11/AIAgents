#!/usr/bin/env python3
"""Remove the autostart service and app shortcuts.

Leaves the project, the virtual environment, your .env and your memory
database alone — this only undoes what install.py added to the system.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYSTEM = platform.system()


def remove(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        else:
            return
        print(f"  removed {path}")
    except OSError as exc:
        print(f"  could not remove {path}: {exc}")


def run(*args: str) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    verb = "ran" if result.returncode == 0 else "tried"
    print(f"  {verb} {' '.join(args[:3])}…")


def main() -> int:
    print(f"\n  Removing JARVIS autostart — {SYSTEM}\n")

    if SYSTEM == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.jarvis.core.plist"
        run("launchctl", "unload", str(plist))
        remove(plist)
        remove(Path.home() / "Applications" / "JARVIS.app")

    elif SYSTEM == "Linux":
        if shutil.which("systemctl"):
            run("systemctl", "--user", "disable", "--now", "jarvis.service")
        remove(Path.home() / ".config" / "systemd" / "user" / "jarvis.service")
        remove(Path.home() / ".config" / "autostart" / "jarvis-core.desktop")
        remove(Path.home() / ".local" / "share" / "applications" / "jarvis.desktop")
        if shutil.which("systemctl"):
            run("systemctl", "--user", "daemon-reload")

    elif SYSTEM == "Windows":
        startup = (
            Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        )
        remove(startup / "JARVIS core.vbs")
        remove(startup.parent / "JARVIS.vbs")
        remove(Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop" / "JARVIS.vbs")

    else:
        print(f"  Unsupported platform: {SYSTEM}")
        return 1

    print("\n  Done. Your project, .env and memory database were left untouched.")
    print("  Delete the jarvis/ folder to remove everything.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
