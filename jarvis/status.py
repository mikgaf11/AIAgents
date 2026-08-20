#!/usr/bin/env python3
"""Is JARVIS actually running, and is everything wired up?

One command that answers the questions you'd otherwise have to dig for:
is the core alive, can it think, will it come back after a reboot, is it
watching, is mail connected. Every failing line comes with the command that
fixes it, so this doubles as the troubleshooting guide.

    python3 status.py
"""

from __future__ import annotations

import getpass
import json
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from server.config import CONFIG  # noqa: E402

SYSTEM = platform.system()
BASE = f"http://{CONFIG.host}:{CONFIG.port}"

OK, WARN, BAD = "  ok  ", " warn ", " down "


def line(mark: str, label: str, detail: str = "", fix: str = "") -> None:
    print(f"  [{mark}] {label:<22} {detail}")
    if fix:
        print(f"         └─ {fix}")


def get(path: str, timeout: float = 4.0):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def check_core() -> dict | None:
    health = get("/api/health")
    if health is None:
        line(BAD, "core", f"nothing answering on {BASE}",
             f"start it:  {HERE / '.venv' / 'bin' / 'python'} -m server.main"
             if SYSTEM != "Windows" else
             rf"start it:  {HERE}\.venv\Scripts\python.exe -m server.main")
        return None
    line(OK, "core", f"up at {BASE}")
    return health


def check_autostart() -> None:
    """Will it come back on its own after a reboot?"""
    if SYSTEM == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.jarvis.core.plist"
        if plist.exists():
            line(OK, "starts at login", "LaunchAgent installed")
        else:
            line(WARN, "starts at login", "no LaunchAgent",
                 "run:  python3 install.py")
        return

    if SYSTEM == "Windows":
        startup = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" \
            / "Start Menu" / "Programs" / "Startup" / "jarvis.vbs"
        if startup.exists():
            line(OK, "starts at login", "in the Startup folder")
        else:
            line(WARN, "starts at login", "not in Startup",
                 "run:  python3 install.py")
        return

    unit = Path.home() / ".config" / "systemd" / "user" / "jarvis.service"
    xdg = Path.home() / ".config" / "autostart" / "jarvis-core.desktop"
    if unit.exists():
        active = subprocess.run(
            ["systemctl", "--user", "is-enabled", "jarvis.service"],
            capture_output=True, text=True,
        ).stdout.strip()
        if active == "enabled":
            line(OK, "starts at login", "systemd user unit enabled")
        else:
            line(WARN, "starts at login", f"unit is {active or 'unknown'}",
                 "run:  systemctl --user enable --now jarvis.service")
        # A user unit stops when you log out unless lingering is on.
        linger = subprocess.run(
            ["loginctl", "show-user", getpass.getuser(), "-p", "Linger"],
            capture_output=True, text=True,
        ).stdout.strip()
        if linger.endswith("no"):
            line(WARN, "survives logout", "lingering is off",
                 f"run:  sudo loginctl enable-linger {getpass.getuser()}")
    elif xdg.exists():
        line(OK, "starts at login", "XDG autostart entry")
    else:
        line(WARN, "starts at login", "nothing registered",
             "run:  python3 install.py")


def check_thinking(status: dict) -> None:
    if not status.get("online"):
        line(BAD, "reasoning", "no core attached — it can't think",
             "free:  ollama pull llama3.1:8b     or put ANTHROPIC_API_KEY in .env")
        return
    if not status.get("reachable", True):
        # Configured but unusable: Ollama stopped, or the model never pulled.
        line(BAD, "reasoning", status.get("backend_error") or "backend unreachable")
        return
    where = "on this machine, free" if status.get("local") else "via the Anthropic API"
    line(OK, "reasoning", f"{status.get('model')} — {where}")


def check_activity(activity: dict | None) -> None:
    if activity is None:
        return
    monitor = activity.get("monitor", {})
    if not monitor.get("enabled"):
        line(WARN, "watching", "disabled (JARVIS_ACTIVITY=0)")
        return
    if not monitor.get("available"):
        fix = "sudo apt install xdotool" if SYSTEM == "Linux" else ""
        line(BAD, "watching", monitor.get("note") or "cannot read the foreground window",
             fix)
        return
    current = monitor.get("current") or {}
    today = activity.get("today") or {}
    total = round(sum(today.values()))
    detail = f"{total} min tracked today"
    if current.get("app"):
        detail += f" · now: {current['app']} ({current['category']})"
    line(OK, "watching", detail)

    play = activity.get("play_minutes", 0)
    limit = activity.get("play_limit", 0)
    if limit:
        mark = WARN if play >= limit else OK
        line(mark, "play time", f"{round(play)} of {round(limit)} min")


def check_reach() -> None:
    roots = [str(r) for r in CONFIG.allowed_roots]
    if len(roots) <= 1:
        line(WARN, "file access", "workspace only",
             "to reach your real files, set JARVIS_FILE_ROOTS in jarvis/.env")
    else:
        line(OK, "file access", ", ".join(roots[1:]))


def check_email(inbox: dict | None) -> None:
    if inbox is None:
        return
    email = inbox.get("email") or {}
    if not email.get("configured"):
        line(WARN, "email", "not connected", "run:  python3 connect.py")
    elif email.get("error"):
        line(BAD, "email", email["error"], "re-run:  python3 connect.py")
    else:
        unhandled = len([m for m in inbox.get("messages", []) if not m.get("handled")])
        line(OK, "email", f"{email.get('address')} · {unhandled} unhandled")


def check_extras() -> None:
    if CONFIG.news_topics.strip():
        line(OK, "morning news", CONFIG.news_topics)
    else:
        line(WARN, "morning news", "no topics set",
             "set JARVIS_NEWS_TOPICS in jarvis/.env")


def main() -> int:
    print(f"\n  {CONFIG.assistant_name} — status\n")

    health = check_core()
    if health is None:
        print()
        return 1

    status = get("/api/status") or {}
    check_thinking(status)
    check_autostart()
    check_activity(get("/api/activity"))
    check_reach()
    check_email(get("/api/inbox"))
    check_extras()

    insights = (get("/api/activity") or {}).get("insights") or []
    line(OK, "learned about you", f"{len(insights)} insights recorded")

    print(f"\n  Open it:  {BASE}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
