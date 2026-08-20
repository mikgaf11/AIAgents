"""Getting your attention when you aren't looking at the HUD.

Two channels, used together:

  the HUD    a floating overlay that appears over the interface and is
             spoken aloud — the thing you see if the window is open
  the OS     a real desktop notification, so a nudge still lands when
             JARVIS is minimised behind a game

Nudges are rate-limited on purpose. An assistant that interrupts freely
gets muted within a day, which makes it useless — so there is a floor on
the gap between interruptions and a cap on how many arrive per hour.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import subprocess
import time
from collections import deque
from typing import Any

from .config import CONFIG
from .events import BUS

SYSTEM = platform.system()


def _desktop_notify(title: str, message: str) -> bool:
    """Fire an OS-level notification. Returns True if one was shown."""
    safe = message.replace('"', "'").replace("\\", "")[:300]
    safe_title = title.replace('"', "'")[:80]
    try:
        if SYSTEM == "Darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe}" with title "{safe_title}"'],
                capture_output=True, timeout=6,
            )
            return True
        if SYSTEM == "Windows":
            # PowerShell toast via the shell API — no third-party module needed.
            script = (
                '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,'
                ' ContentType = WindowsRuntime] > $null;'
                '$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(0);'
                '$x = $t.GetElementsByTagName("text");'
                f'$x.Item(0).AppendChild($t.CreateTextNode("{safe_title}")) > $null;'
                f'$x.Item(1).AppendChild($t.CreateTextNode("{safe}")) > $null;'
                '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier'
                '("JARVIS").Show([Windows.UI.Notifications.ToastNotification]::new($t))'
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, timeout=10,
            )
            return True
        if shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", "-a", "JARVIS", safe_title, safe],
                capture_output=True, timeout=6,
            )
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


class Notifier:
    """Rate-limited nudges, delivered to the HUD and the desktop."""

    def __init__(self) -> None:
        self.last_nudge = 0.0
        self.recent: deque[float] = deque(maxlen=32)
        self.muted_until = 0.0
        self.delivered = 0

    def snooze(self, minutes: float) -> None:
        self.muted_until = time.time() + minutes * 60

    def may_nudge(self, *, urgent: bool = False) -> bool:
        now = time.time()
        if now < self.muted_until and not urgent:
            return False
        if now - self.last_nudge < CONFIG.nudge_min_gap and not urgent:
            return False
        hour_ago = now - 3600
        recent = [t for t in self.recent if t > hour_ago]
        return urgent or len(recent) < CONFIG.nudge_max_per_hour

    async def nudge(
        self,
        text: str,
        *,
        kind: str = "coach",
        title: str = "",
        urgent: bool = False,
        speak: bool = True,
    ) -> bool:
        """Deliver a nudge. Returns False if rate limiting suppressed it."""
        if not text.strip():
            return False
        if not self.may_nudge(urgent=urgent):
            # 'tone', not 'kind': the bus already uses 'kind' for the event
            # name itself, and a payload key of the same name collides.
            BUS.emit("nudge_suppressed", text=text[:120], tone=kind)
            return False

        now = time.time()
        self.last_nudge = now
        self.recent.append(now)
        self.delivered += 1

        # The HUD renders this as a floating popup and speaks it.
        BUS.emit("nudge", text=text, tone=kind, speak=speak,
                 title=title or CONFIG.assistant_name)

        if CONFIG.desktop_notifications:
            await asyncio.to_thread(
                _desktop_notify, title or CONFIG.assistant_name, text
            )
        return True

    def status(self) -> dict[str, Any]:
        return {
            "delivered": self.delivered,
            "muted_for": max(0, round(self.muted_until - time.time())),
            "last_nudge": self.last_nudge,
            "desktop": CONFIG.desktop_notifications,
        }


NOTIFIER = Notifier()
