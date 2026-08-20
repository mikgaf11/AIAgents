#!/usr/bin/env python3
"""Switch between Claude and the free local model.

`JARVIS_BACKEND=auto` prefers Claude whenever it finds credentials, which is
the right default but not what you want if the point is to stop paying. This
writes the setting into .env and restarts the core, so the switch is one
command in either direction.

    python3 backend.py           # what is it using right now?
    python3 backend.py local     # free, on this machine
    python3 backend.py claude    # the Anthropic API
    python3 backend.py auto      # Claude if a key exists, else local
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from server.config import CONFIG, parse_env_value  # noqa: E402

ENV_FILE = HERE / ".env"

# What the user types -> what the config expects.
CHOICES = {
    "local": "ollama",
    "ollama": "ollama",
    "free": "ollama",
    "claude": "anthropic",
    "anthropic": "anthropic",
    "api": "anthropic",
    "auto": "auto",
}

LABELS = {
    "ollama": "the local model (free)",
    "anthropic": "Claude via the Anthropic API (paid)",
    "auto": "Claude if a key is present, otherwise the local model",
}


def say(message: str) -> None:
    print(f"  {message}")


def read_env() -> list[str]:
    if not ENV_FILE.exists():
        return []
    return ENV_FILE.read_text(encoding="utf-8").splitlines()


def current_setting() -> str:
    """The backend named in .env, ignoring whatever is in the environment."""
    for raw in read_env():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        if key.strip() == "JARVIS_BACKEND":
            return parse_env_value(value).lower() or "auto"
    return "auto"  # absent from .env means the built-in default applies


def write_setting(backend: str) -> None:
    """Set JARVIS_BACKEND in .env, leaving every other line untouched."""
    lines = read_env()
    replaced = False
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        candidate = stripped[len("export "):] if stripped.startswith("export ") else stripped
        if candidate.split("=", 1)[0].strip() == "JARVIS_BACKEND":
            lines[index] = f"JARVIS_BACKEND={backend}"
            replaced = True
    if not replaced:
        # An .env written before this setting existed won't have the line.
        lines.insert(0, f"JARVIS_BACKEND={backend}")
        lines.insert(1, "")
    ENV_FILE.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    try:
        ENV_FILE.chmod(0o600)  # the file also holds secrets
    except OSError:
        pass


def live_status() -> dict | None:
    try:
        url = f"http://{CONFIG.host}:{CONFIG.port}/api/status"
        with urllib.request.urlopen(url, timeout=4) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def check_ollama() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{CONFIG.ollama_url}/api/tags", timeout=4) as r:
            installed = [m.get("name", "") for m in json.loads(r.read()).get("models", [])]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return False, (
            f"Ollama isn't answering at {CONFIG.ollama_url}."
            " Install it from https://ollama.com and start it."
        )
    wanted = CONFIG.ollama_model
    if not any(i == wanted or i.startswith(wanted.split(":")[0]) for i in installed):
        return False, f"Model '{wanted}' isn't pulled yet. Run:  ollama pull {wanted}"
    return True, ", ".join(installed[:3])


def show() -> int:
    setting = current_setting()
    print(f"\n  Setting in .env:  JARVIS_BACKEND={setting}")
    say(f"which means: {LABELS.get(setting, setting)}")

    status = live_status()
    if status is None:
        say("the core isn't running, so I can't say what it's actually using")
    elif not status.get("online"):
        say("running right now: nothing — no reasoning core attached")
    else:
        where = "free, on this machine" if status.get("local") else "paid, via the API"
        say(f"running right now: {status.get('model')} ({where})")
        if status.get("reachable") is False:
            say(f"but it is unreachable: {status.get('backend_error', '')}")

    if setting == "auto" and status and not status.get("local"):
        print()
        say("You have credentials, so 'auto' picked Claude. To stop paying:")
        say("    python3 backend.py local")
    print()
    return 0


def switch(choice: str) -> int:
    backend = CHOICES[choice]
    print(f"\n  Switching to {LABELS[backend]}\n")

    if backend in ("ollama", "auto"):
        ready, detail = check_ollama()
        if ready:
            say(f"local models available: {detail}")
        elif backend == "ollama":
            # Writing the setting anyway would leave a core that can't think.
            say(detail)
            say("Fix that first, then run this again. Nothing was changed.")
            print()
            return 1
        else:
            say(f"note: {detail}")

    if backend == "anthropic" and not CONFIG.has_credentials:
        say("No Anthropic credentials found — set ANTHROPIC_API_KEY in .env first.")
        say("Nothing was changed.")
        print()
        return 1

    write_setting(backend)
    say(f"wrote JARVIS_BACKEND={backend} to {ENV_FILE}")

    from restart import main as restart

    print()
    code = restart()
    if code == 0:
        status = live_status() or {}
        if status.get("online"):
            where = "free" if status.get("local") else "paid"
            say(f"now thinking with {status.get('model')} ({where})")
            print()
    return code


def main() -> int:
    if len(sys.argv) < 2:
        return show()
    choice = sys.argv[1].strip().lower()
    if choice in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if choice not in CHOICES:
        print(f"\n  Unknown option '{choice}'.")
        print("  Use one of: local, claude, auto  (or no argument to just look)\n")
        return 2
    return switch(choice)


if __name__ == "__main__":
    raise SystemExit(main())
