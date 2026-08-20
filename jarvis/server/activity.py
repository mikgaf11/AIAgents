"""Watching what you actually do, so the assistant can learn from it.

This samples the *foreground application* — its name and window title —
every few seconds. It is deliberately not a screen recorder and not a
keylogger: no screenshots, no keystrokes, no page contents. An app name
and a window title are enough to know that you spent two hours in a game
or forty minutes in a code editor, which is all the coaching and
play-time features need.

Everything stays on this machine, in the same SQLite file as the rest of
the memory. `JARVIS_ACTIVITY=0` turns the whole thing off.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CONFIG
from .events import BUS

try:
    import psutil
except ImportError:  # pragma: no cover - optional
    psutil = None

SYSTEM = platform.system()

# Default classification. The user can override or extend it by dropping a
# JSON file at data/activity_rules.json, which is how this gets personal:
# nobody's app list looks like anyone else's.
DEFAULT_RULES: dict[str, list[str]] = {
    "game": [
        "steam", "epicgames", "battle.net", "riotclient", "league of legends",
        "valorant", "csgo", "cs2", "dota", "minecraft", "roblox", "fortnite",
        "callofduty", "gta", "cyberpunk", "eldenring", "xbox", "playstation",
        "origin.exe", "gog galaxy", "unity", "unreal",
    ],
    "media": [
        "youtube", "netflix", "spotify", "vlc", "twitch", "disney", "hulu",
        "primevideo", "plex", "apple tv", "music", "soundcloud", "podcast",
    ],
    "code": [
        "code.exe", "visual studio", "vscode", "pycharm", "intellij", "webstorm",
        "sublime", "neovim", "vim", "emacs", "terminal", "iterm", "powershell",
        "cmd.exe", "windowsterminal", "xcode", "android studio", "godot",
    ],
    "work": [
        "excel", "word", "powerpoint", "outlook", "notion", "obsidian", "figma",
        "photoshop", "illustrator", "blender", "docs.google", "sheets.google",
        "jira", "linear", "confluence", "onenote", "pages", "numbers",
    ],
    "comms": [
        "slack", "discord", "teams", "zoom", "whatsapp", "telegram", "signal",
        "messages", "facetime", "skype", "gmail", "mail",
    ],
    "browse": [
        "chrome", "firefox", "safari", "edge", "brave", "arc", "opera", "vivaldi",
    ],
}

# Browsing is ambiguous — the window title says far more than the app name,
# so a browser gets reclassified by what is actually on screen.
TITLE_HINTS: dict[str, list[str]] = {
    "game": ["twitch", "steam", "roblox", "game"],
    "media": ["youtube", "netflix", "spotify", "- watch", "episode", "movie"],
    "work": ["docs.google", "sheets", "jira", "linear", "notion", "invoice"],
    "code": ["github", "stack overflow", "localhost", "docs.python", "mdn"],
}


@dataclass
class Sample:
    app: str
    title: str
    category: str
    at: float

    def label(self) -> str:
        return f"{self.app}: {self.title}"[:200] if self.title else self.app


def _foreground_macos() -> tuple[str, str]:
    script = (
        'tell application "System Events"\n'
        ' set p to first application process whose frontmost is true\n'
        ' set appName to name of p\n'
        ' try\n'
        '  set winName to name of front window of p\n'
        ' on error\n'
        '  set winName to ""\n'
        ' end try\n'
        'end tell\n'
        'return appName & "|" & winName'
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=5
    )
    app, _, title = result.stdout.strip().partition("|")
    return app, title


def _foreground_windows() -> tuple[str, str]:
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    handle = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(handle)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, length + 1)
    title = buffer.value

    app = ""
    if psutil is not None:
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        try:
            app = psutil.Process(pid.value).name()
        except Exception:  # noqa: BLE001 - process may have exited
            app = ""
    return app, title


def _foreground_linux() -> tuple[str, str]:
    if shutil.which("xdotool"):
        result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True, text=True, timeout=5,
        )
        title = result.stdout.strip()
        if title:
            return title.split(" - ")[-1], title
    if shutil.which("xprop"):
        result = subprocess.run(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
            capture_output=True, text=True, timeout=5,
        )
        match = re.search(r"0x[0-9a-f]+", result.stdout)
        if match:
            name = subprocess.run(
                ["xprop", "-id", match.group(0), "WM_CLASS", "_NET_WM_NAME"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            app = re.findall(r'"([^"]+)"', name)
            title = app[-1] if app else ""
            return (app[0] if app else ""), title
    return "", ""


def foreground() -> tuple[str, str]:
    """(app, window title) for whatever is in front, or ('','') if unknown."""
    try:
        if SYSTEM == "Darwin":
            return _foreground_macos()
        if SYSTEM == "Windows":
            return _foreground_windows()
        return _foreground_linux()
    except Exception:  # noqa: BLE001 - never let monitoring break the assistant
        return "", ""


class ActivityMonitor:
    """Samples the foreground app and turns it into sessions and totals."""

    def __init__(self, memory) -> None:
        self.memory = memory
        self.rules = self._load_rules()
        self.current: Sample | None = None
        self.session_started = 0.0
        self.last_sample = 0.0
        self.running = False
        self._task: asyncio.Task | None = None
        self.available = SYSTEM in ("Darwin", "Windows") or bool(
            shutil.which("xdotool") or shutil.which("xprop")
        )

    def _load_rules(self) -> dict[str, list[str]]:
        rules = {k: list(v) for k, v in DEFAULT_RULES.items()}
        override = CONFIG.db_path.parent / "activity_rules.json"
        if override.exists():
            try:
                custom = json.loads(override.read_text(encoding="utf-8"))
                for category, needles in custom.items():
                    rules.setdefault(category, [])
                    rules[category].extend(n.lower() for n in needles)
            except (json.JSONDecodeError, OSError, AttributeError) as exc:
                BUS.emit("error", message=f"activity_rules.json ignored: {exc}")
        return rules

    def classify(self, app: str, title: str) -> str:
        haystack = f"{app} {title}".lower()
        base = "other"
        for category, needles in self.rules.items():
            if any(needle in haystack for needle in needles):
                base = category
                break
        # A browser is whatever it is showing.
        if base == "browse":
            for category, needles in TITLE_HINTS.items():
                if any(needle in title.lower() for needle in needles):
                    return category
        return base

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if not CONFIG.activity_enabled or not self.available:
            return
        if self._task is None or self._task.done():
            self.running = True
            self._task = asyncio.create_task(self._loop(), name="jarvis-activity")

    async def stop(self) -> None:
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._close_session()

    async def _loop(self) -> None:
        while self.running:
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                BUS.emit("error", message=f"Activity sample failed: {exc}")
            await asyncio.sleep(CONFIG.activity_interval)

    # -- sampling --------------------------------------------------------

    async def sample(self) -> Sample | None:
        app, title = await asyncio.to_thread(foreground)
        if not app and not title:
            return None

        now = time.time()
        category = self.classify(app, title)
        sample = Sample(app=app or "unknown", title=title, category=category, at=now)

        # A change of app (or of category) closes the previous session.
        if self.current is None:
            self.current, self.session_started = sample, now
        elif sample.app != self.current.app or sample.category != self.current.category:
            await self._close_session()
            self.current, self.session_started = sample, now
        else:
            self.current = sample

        self.last_sample = now
        BUS.emit(
            "activity",
            app=sample.app,
            title=sample.title[:120],
            category=category,
            minutes=round((now - self.session_started) / 60, 1),
        )
        return sample

    async def _close_session(self) -> None:
        if self.current is None or not self.session_started:
            return
        seconds = time.time() - self.session_started
        # Ignore alt-tab noise; only real stretches are worth remembering.
        if seconds >= CONFIG.activity_min_session:
            await self.memory.record_activity(
                app=self.current.app,
                title=self.current.title,
                category=self.current.category,
                started=self.session_started,
                seconds=seconds,
            )
        self.current = None
        self.session_started = 0.0

    # -- queries ---------------------------------------------------------

    async def today(self) -> dict[str, float]:
        """Minutes per category so far today, including the open session."""
        totals = await self.memory.activity_totals(since=_start_of_day())
        if self.current and self.session_started:
            live = (time.time() - self.session_started) / 60
            totals[self.current.category] = totals.get(self.current.category, 0) + live
        return {k: round(v, 1) for k, v in totals.items()}

    async def play_minutes(self) -> float:
        totals = await self.today()
        return sum(totals.get(c, 0.0) for c in CONFIG.play_categories)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": CONFIG.activity_enabled,
            "available": self.available,
            "platform": SYSTEM,
            "current": {
                "app": self.current.app if self.current else "",
                "title": (self.current.title[:80] if self.current else ""),
                "category": self.current.category if self.current else "",
                "minutes": round((time.time() - self.session_started) / 60, 1)
                if self.session_started else 0.0,
            },
            "note": "" if self.available else (
                "Foreground-window detection needs xdotool or xprop on Linux."
            ),
        }


def _start_of_day() -> float:
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
