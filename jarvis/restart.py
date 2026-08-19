#!/usr/bin/env python3
"""Restart the JARVIS core so configuration changes take effect.

Settings are read once at startup, so anything you change in .env — an API
key, email credentials, whether sending is allowed — needs the core to come
back up before it applies.

Works whether or not the autostart service is installed: the supervisor
(launchd / systemd) brings it back on its own, and if there isn't one, this
starts it directly.

    python3 restart.py
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from launcher import URL, core_is_up, start_core  # noqa: E402

SYSTEM = platform.system()


def core_pids() -> list[int]:
    """PIDs of running core processes, excluding us and our caller."""
    if SYSTEM == "Windows":
        result = subprocess.run(
            ["wmic", "process", "where",
             "commandline like '%server.main%' and name like '%python%'",
             "get", "processid"],
            capture_output=True, text=True,
        )
        found = [int(l.strip()) for l in result.stdout.splitlines() if l.strip().isdigit()]
    else:
        result = subprocess.run(
            ["pgrep", "-f", "python -m server[.]main"], capture_output=True, text=True
        )
        found = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    # Never signal ourselves or the shell that launched us: `pgrep -f` matches
    # whole command lines, so a caller whose own arguments contain the pattern
    # would otherwise be killed instead of the core.
    protected = {os.getpid(), os.getppid()}
    return [pid for pid in found if pid not in protected]


def alive(pid: int) -> bool:
    if SYSTEM == "Windows":
        return pid in core_pids()
    try:
        os.kill(pid, 0)  # signal 0 only tests for existence
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_core() -> int:
    """Stop every running core process and wait for it to actually exit."""
    targets = core_pids()
    if not targets:
        return 0

    for pid in targets:
        if SYSTEM == "Windows":
            subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True)
        else:
            try:
                os.kill(pid, 15)  # SIGTERM: let uvicorn shut down cleanly
            except OSError:
                pass

    # Graceful shutdown isn't instant, and reporting success while the old
    # process is still serving would be a lie.
    deadline = time.time() + 10
    while time.time() < deadline and any(alive(pid) for pid in targets):
        time.sleep(0.3)

    for pid in targets:
        if alive(pid):
            if SYSTEM == "Windows":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            else:
                try:
                    os.kill(pid, 9)  # it ignored SIGTERM; insist
                except OSError:
                    pass
    return len(targets)


def main() -> int:
    print("\n  Restarting the JARVIS core…\n")

    was_up = core_is_up()
    stopped = stop_core()
    print(f"  stopped {stopped} process{'' if stopped == 1 else 'es'}"
          if stopped else "  nothing was running")

    # A supervisor (launchd KeepAlive / systemd Restart=always) will bring it
    # back by itself; give it a moment before starting one ourselves.
    deadline = time.time() + 8
    while time.time() < deadline:
        if core_is_up():
            print(f"  back up at {URL}\n")
            return 0
        time.sleep(0.5)

    print("  no supervisor picked it up — starting it directly")
    start_core()
    deadline = time.time() + 30
    while time.time() < deadline:
        if core_is_up():
            print(f"  back up at {URL}\n")
            return 0
        time.sleep(0.5)

    print("\n  It did not come back up. To see the error, run:")
    print(f"      cd {HERE} && ./.venv/bin/python -m server.main\n")
    return 1 if was_up else 0


if __name__ == "__main__":
    raise SystemExit(main())
