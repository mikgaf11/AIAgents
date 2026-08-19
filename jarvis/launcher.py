#!/usr/bin/env python3
"""What the JARVIS icon runs.

Makes sure the core is up (starting it if the background service isn't
running), waits for it to answer, then opens the HUD in a chromeless
browser window so it behaves like a native app rather than a tab.

Safe to run repeatedly — it never starts a second core.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from server.config import CONFIG  # noqa: E402

URL = f"http://{CONFIG.host}:{CONFIG.port}"


def core_is_up(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{URL}/api/health", timeout=timeout) as response:
            return json.loads(response.read()).get("ok") is True
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False


def python_executable() -> str:
    """Prefer the project venv; fall back to whatever is running this."""
    candidates = [
        HERE / ".venv" / "bin" / "python",
        HERE / ".venv" / "Scripts" / "pythonw.exe",
        HERE / ".venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def start_core() -> None:
    """Launch the core detached, so closing the launcher won't kill it."""
    kwargs: dict = {
        "cwd": str(HERE),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if platform.system() == "Windows":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([python_executable(), "-m", "server.main"], **kwargs)


def find_app_browser() -> str | None:
    """A Chromium-family browser, which can open a window with no chrome."""
    system = platform.system()
    if system == "Darwin":
        for app in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ):
            if Path(app).exists():
                return app
        return None
    if system == "Windows":
        for app in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ):
            if Path(app).exists():
                return app
        return None
    for name in ("google-chrome", "chromium", "chromium-browser", "brave-browser",
                 "microsoft-edge", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_hud() -> None:
    """Open the HUD, preferring an app window over a browser tab.

    Speech recognition needs a Chromium-family browser, so if we can find
    one we use it even when it isn't the system default.
    """
    browser = find_app_browser()
    if browser:
        profile = HERE / "data" / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(
                [
                    browser,
                    f"--app={URL}",
                    f"--user-data-dir={profile}",
                    "--window-size=1600,980",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=(platform.system() != "Windows"),
            )
            return
        except OSError:
            pass  # fall through to the default browser
    webbrowser.open(URL)


def main() -> int:
    if not core_is_up():
        start_core()
        deadline = time.time() + 30
        while time.time() < deadline:
            if core_is_up():
                break
            time.sleep(0.4)
        else:
            sys.stderr.write(
                f"{CONFIG.assistant_name} core did not come up within 30s.\n"
                f"Run `{python_executable()} -m server.main` in {HERE} to see why.\n"
            )
            return 1
    open_hud()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
