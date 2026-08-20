#!/usr/bin/env python3
"""One-time setup: make JARVIS an always-on local app.

After this runs you never need a terminal again:

  * the core starts automatically when you log in, and restarts if it dies
  * a JARVIS icon appears in your applications / desktop
  * clicking it opens the HUD in its own window

Everything is per-user and local. Nothing is installed system-wide, no
ports are opened beyond 127.0.0.1, and no admin rights are needed.

    python3 install.py            # set it up
    python3 install.py --dry-run  # show exactly what it would write
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from server.console import read_secret, supports_interaction  # noqa: E402

SYSTEM = platform.system()  # Darwin | Linux | Windows

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="26" fill="#04070d"/>
  <circle cx="64" cy="64" r="42" fill="none" stroke="#38e1ff" stroke-width="2.5" opacity="0.55"/>
  <circle cx="64" cy="64" r="52" fill="none" stroke="#38e1ff" stroke-width="1.5"
          opacity="0.3" stroke-dasharray="40 18"/>
  <circle cx="64" cy="64" r="15" fill="#38e1ff"/>
  <circle cx="64" cy="64" r="26" fill="none" stroke="#8b7cff" stroke-width="2" opacity="0.8"/>
</svg>
"""


@dataclass
class Plan:
    """Everything the installer intends to do, before it does any of it."""

    files: list[tuple[Path, str, bool]] = field(default_factory=list)  # path, body, executable
    commands: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def write(self, path: Path, body: str, executable: bool = False) -> None:
        self.files.append((path, body, executable))

    def run(self, *args: str) -> None:
        self.commands.append(list(args))


# -- helpers ---------------------------------------------------------------


def say(message: str) -> None:
    print(f"  {message}")


def venv_python(for_gui: bool = False) -> Path:
    """The interpreter the service and launcher should use."""
    if SYSTEM == "Windows":
        scripts = HERE / ".venv" / "Scripts"
        # pythonw runs without opening a console window.
        preferred = scripts / ("pythonw.exe" if for_gui else "python.exe")
        return preferred if preferred.exists() else Path(sys.executable)
    candidate = HERE / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


def ensure_venv(skip: bool) -> None:
    if skip:
        return
    venv = HERE / ".venv"
    if not venv.exists():
        say("creating virtual environment")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv_python()
    say("installing dependencies (this can take a minute)")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        check=False,
    )
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "-r", str(HERE / "requirements.txt")],
        check=True,
    )


def ollama_ready() -> tuple[bool, str]:
    """Is a free local model already available? Returns (ready, detail)."""
    try:
        import json as _json
        import urllib.request

        url = os.environ.get("JARVIS_OLLAMA_URL", "http://127.0.0.1:11434")
        with urllib.request.urlopen(f"{url}/api/tags", timeout=3) as response:
            models = _json.loads(response.read()).get("models", [])
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        return False, "not running"
    if not models:
        return False, "running, but no model pulled"
    return True, ", ".join(m.get("name", "?") for m in models[:3])


def ensure_api_key(dry_run: bool) -> bool:
    """Make sure a reasoning core is configured. Returns True if one is."""
    env_file = HERE / ".env"
    existing = ""
    if env_file.exists():
        existing = env_file.read_text(encoding="utf-8")
        for line in existing.splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY=") and len(line.strip()) > 20:
                say("API key already configured in .env")
                return True
    if os.environ.get("ANTHROPIC_API_KEY"):
        say("using ANTHROPIC_API_KEY from the environment")
        if not dry_run:
            body = existing or (HERE / ".env.example").read_text(encoding="utf-8")
            if "ANTHROPIC_API_KEY=" not in body:
                body = f"ANTHROPIC_API_KEY={os.environ['ANTHROPIC_API_KEY']}\n" + body
                env_file.write_text(body, encoding="utf-8")
        return True

    if dry_run:
        say("would prompt for an Anthropic API key")
        return False

    local_ready, local_detail = ollama_ready()

    print()
    print("  JARVIS needs a reasoning core. There are two, and either works:")
    print()
    print("  1. Claude — much better reasoning, costs per token.")
    print("     Get a key at https://console.anthropic.com/settings/keys")
    print("     Stored only in jarvis/.env on this machine.")
    print()
    if local_ready:
        print(f"  2. A local model — free. Already installed: {local_detail}")
        print("     Press Enter below to use it and skip the key entirely.")
    else:
        print("  2. A local model — free forever, runs on your machine, weaker.")
        print("     Install https://ollama.com then:  ollama pull llama3.1:8b")
        print(f"     (Ollama is {local_detail} right now.)")
    print()

    if not supports_interaction():
        # No real terminal (an IDE run window, or piped input): prompting here
        # would hang or silently read nothing.
        say("this console isn't interactive, so I can't prompt for the key")
        say("add it by hand instead: copy .env.example to .env and set")
        say("ANTHROPIC_API_KEY=sk-ant-...  then run:  python3 restart.py")
        return local_ready

    print("  Press Enter on its own to skip the key.")
    key = read_secret("  API key: ")
    if not key:
        if local_ready:
            say("no key given — running free on the local model")
            return True
        say("skipped — JARVIS will start in offline mode")
        say("to fix that later, either put ANTHROPIC_API_KEY=... in jarvis/.env")
        say("or install Ollama and run:  ollama pull llama3.1:8b")
        say("then run:  python3 restart.py")
        return False
    if not key.startswith("sk-"):
        say("that doesn't look like an Anthropic key (they start with 'sk-'), storing anyway")

    template = existing or (HERE / ".env.example").read_text(encoding="utf-8")
    lines, replaced = [], False
    for line in template.splitlines():
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            lines.append(f"ANTHROPIC_API_KEY={key}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.insert(0, f"ANTHROPIC_API_KEY={key}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        env_file.chmod(0o600)  # the key is a secret; keep it owner-readable
    except OSError:
        pass
    say("key saved to jarvis/.env (permissions 600)")
    return True


# -- per-platform plans ----------------------------------------------------


def plan_macos(plan: Plan) -> None:
    python = venv_python()
    label = "com.jarvis.core"
    agents = Path.home() / "Library" / "LaunchAgents"
    plist_path = agents / f"{label}.plist"

    plan.write(
        plist_path,
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>server.main</string>
  </array>
  <key>WorkingDirectory</key><string>{HERE}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{HERE}/data/core.log</string>
  <key>StandardErrorPath</key><string>{HERE}/data/core.err.log</string>
</dict>
</plist>
""",
    )
    # Reload rather than load, so re-running the installer is idempotent.
    plan.run("launchctl", "unload", str(plist_path))
    plan.run("launchctl", "load", "-w", str(plist_path))

    # A minimal .app bundle so JARVIS shows up in Spotlight and Launchpad.
    app = Path.home() / "Applications" / "JARVIS.app"
    plan.write(
        app / "Contents" / "Info.plist",
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>JARVIS</string>
  <key>CFBundleDisplayName</key><string>JARVIS</string>
  <key>CFBundleIdentifier</key><string>com.jarvis.launcher</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>JARVIS</string>
  <key>LSUIElement</key><false/>
</dict>
</plist>
""",
    )
    plan.write(
        app / "Contents" / "MacOS" / "JARVIS",
        f'#!/bin/sh\ncd "{HERE}"\nexec "{python}" "{HERE}/launcher.py"\n',
        executable=True,
    )
    plan.notes.append("JARVIS.app added to ~/Applications — drag it to your Dock.")


def plan_linux(plan: Plan) -> None:
    python = venv_python()
    has_systemd = shutil.which("systemctl") is not None

    if has_systemd:
        unit = Path.home() / ".config" / "systemd" / "user" / "jarvis.service"
        plan.write(
            unit,
            f"""[Unit]
Description=JARVIS core
After=network-online.target

[Service]
Type=simple
WorkingDirectory={HERE}
ExecStart={python} -m server.main
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
""",
        )
        plan.run("systemctl", "--user", "daemon-reload")
        plan.run("systemctl", "--user", "enable", "--now", "jarvis.service")
        plan.notes.append(
            "To keep JARVIS running when you're logged out:  "
            f"sudo loginctl enable-linger {getpass.getuser()}"
        )
    else:
        # No systemd (or a container) — fall back to XDG autostart.
        autostart = Path.home() / ".config" / "autostart" / "jarvis-core.desktop"
        plan.write(
            autostart,
            f"""[Desktop Entry]
Type=Application
Name=JARVIS core
Exec={python} -m server.main
Path={HERE}
X-GNOME-Autostart-enabled=true
NoDisplay=true
""",
        )
        plan.notes.append("systemd not found — using XDG autostart instead.")

    icon = HERE / "web" / "icon.svg"
    plan.write(icon, ICON_SVG)
    desktop = Path.home() / ".local" / "share" / "applications" / "jarvis.desktop"
    plan.write(
        desktop,
        f"""[Desktop Entry]
Type=Application
Name=JARVIS
Comment=Personal AI assistant
Exec={python} {HERE}/launcher.py
Icon={icon}
Terminal=false
Categories=Utility;
StartupNotify=true
""",
        executable=True,
    )
    if shutil.which("update-desktop-database"):
        plan.run("update-desktop-database", str(desktop.parent))
    plan.notes.append("JARVIS added to your application menu.")

    if not (shutil.which("xdotool") or shutil.which("xprop")):
        # Without one of these there is no way to see which window is in
        # front, so time tracking and coaching silently do nothing.
        plan.notes.append(
            "Activity tracking needs xdotool:  sudo apt install xdotool"
            "  (without it, play-time and coaching stay idle)"
        )


def plan_windows(plan: Plan) -> None:
    gui_python = venv_python(for_gui=True)
    startup = (
        Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )
    plan.write(
        startup / "JARVIS core.vbs",
        f'''Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "{HERE}"
shell.Run """{gui_python}"" -m server.main", 0, False
''',
    )
    # A .vbs launcher on the Desktop and in the Start Menu — no console window.
    launcher_vbs = f'''Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "{HERE}"
shell.Run """{gui_python}"" ""{HERE}\\launcher.py""", 0, False
'''
    desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
    plan.write(desktop / "JARVIS.vbs", launcher_vbs)
    plan.write(startup.parent / "JARVIS.vbs", launcher_vbs)
    plan.notes.append("JARVIS added to your Desktop and Start Menu.")


PLANNERS = {"Darwin": plan_macos, "Linux": plan_linux, "Windows": plan_windows}


# -- apply -----------------------------------------------------------------


def apply(plan: Plan, dry_run: bool) -> None:
    for path, body, executable in plan.files:
        if dry_run:
            print(f"\n  --- would write {path} ---")
            preview = body if len(body) < 700 else body[:700] + "\n  …"
            print("\n".join("  " + line for line in preview.splitlines()))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        if executable:
            path.chmod(0o755)
        say(f"wrote {path}")

    for command in plan.commands:
        if dry_run:
            print(f"  would run: {' '.join(command)}")
            continue
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            # launchctl unload of a not-yet-loaded agent is expected to fail.
            detail = (result.stderr or result.stdout).strip().splitlines()
            say(f"note: `{' '.join(command[:3])}…` exited {result.returncode}"
                + (f" — {detail[0]}" if detail else ""))
        else:
            say(f"ran {' '.join(command[:3])}…")


def verify(timeout: float = 25.0) -> bool:
    """Poll the core until it answers, so the installer proves it worked."""
    import time

    from server.config import CONFIG

    url = f"http://{CONFIG.host}:{CONFIG.port}/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return json.loads(response.read()).get("ok") is True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.6)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Install JARVIS as a local app.")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be written, change nothing")
    parser.add_argument("--skip-deps", action="store_true",
                        help="don't touch the virtual environment")
    args = parser.parse_args()

    print(f"\n  JARVIS setup — {SYSTEM}\n")

    if SYSTEM not in PLANNERS:
        print(f"  Unsupported platform: {SYSTEM}")
        print("  You can still run it manually with ./run.sh")
        return 1

    if not args.dry_run:
        ensure_venv(args.skip_deps)
    has_key = ensure_api_key(args.dry_run)

    plan = Plan()
    PLANNERS[SYSTEM](plan)
    apply(plan, args.dry_run)

    if args.dry_run:
        print("\n  Dry run — nothing was changed.\n")
        return 0

    sys.path.insert(0, str(HERE))
    from server.config import CONFIG

    print()
    if verify():
        say(f"core is up and answering on http://{CONFIG.host}:{CONFIG.port}")
    else:
        say("core did not answer yet — it may still be starting.")
        say(f"if the icon does nothing, run:  {venv_python()} -m server.main")

    print("\n  Done.\n")
    for note in plan.notes:
        say(note)
    say(f"Open JARVIS any time at http://{CONFIG.host}:{CONFIG.port}")
    if not has_key:
        say("No reasoning core yet — the HUD runs, but it can't think.")
        say("Fix with either:  ollama pull llama3.1:8b   (free)")
        say("             or:  ANTHROPIC_API_KEY=... in jarvis/.env")
        say("then:  python3 restart.py")
    print()
    say("To remove all of this later:  python3 uninstall.py")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
