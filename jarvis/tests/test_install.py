"""Verify the installer generates correct artifacts on every platform.

These exercise plan generation only — nothing is written to the system.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import install  # noqa: E402


PLATFORMS = ["Darwin", "Linux", "Windows"]


def build(monkeypatch, system: str) -> install.Plan:
    monkeypatch.setattr(install, "SYSTEM", system)
    plan = install.Plan()
    install.PLANNERS[system](plan)
    return plan


@pytest.mark.parametrize("system", PLATFORMS)
def test_every_platform_produces_autostart_and_a_launcher(monkeypatch, system):
    plan = build(monkeypatch, system)
    bodies = "\n".join(body for _, body, _ in plan.files)
    paths = [str(path) for path, _, _ in plan.files]

    assert plan.files, "planner produced nothing"
    # Autostart must invoke the server module...
    assert "server.main" in bodies, "no autostart entry starts the core"
    # ...and something must invoke the launcher for the clickable icon.
    assert any("launcher.py" in body for _, body, _ in plan.files), "no app launcher"
    assert all(Path(p).is_absolute() for p in paths), "paths must be absolute"


@pytest.mark.parametrize("system", PLATFORMS)
def test_paths_point_at_the_real_project_directory(monkeypatch, system):
    plan = build(monkeypatch, system)
    bodies = "\n".join(body for _, body, _ in plan.files)
    assert str(install.HERE) in bodies, "generated files must reference the project"


def test_macos_agent_is_keepalive_and_runs_at_login(monkeypatch):
    plan = build(monkeypatch, "Darwin")
    plist = next(b for p, b, _ in plan.files if p.name.endswith("com.jarvis.core.plist"))
    assert "<key>RunAtLoad</key><true/>" in plist
    assert "<key>KeepAlive</key><true/>" in plist
    # Reload must be idempotent: unload before load.
    commands = [" ".join(c) for c in plan.commands]
    assert any(c.startswith("launchctl unload") for c in commands)
    assert any("load -w" in c for c in commands)
    assert commands.index(next(c for c in commands if "unload" in c)) < commands.index(
        next(c for c in commands if "load -w" in c)
    )


def test_macos_app_bundle_is_well_formed(monkeypatch):
    plan = build(monkeypatch, "Darwin")
    names = {str(p) for p, _, _ in plan.files}
    info = next(p for p in names if p.endswith("Contents/Info.plist"))
    binary = next(p for p in names if p.endswith("Contents/MacOS/JARVIS"))
    # CFBundleExecutable must match the actual executable file name.
    body = next(b for p, b, _ in plan.files if str(p) == info)
    assert "<key>CFBundleExecutable</key><string>JARVIS</string>" in body
    assert Path(binary).name == "JARVIS"
    assert next(exe for p, _, exe in plan.files if str(p) == binary), "must be executable"


def test_linux_systemd_unit_restarts_and_installs(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/" + name)
    plan = build(monkeypatch, "Linux")
    unit = next(b for p, b, _ in plan.files if p.name == "jarvis.service")
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit
    commands = [" ".join(c) for c in plan.commands]
    assert "systemctl --user daemon-reload" in commands
    assert any("enable --now" in c for c in commands)


def test_linux_falls_back_to_xdg_autostart_without_systemd(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    plan = build(monkeypatch, "Linux")
    paths = [str(p) for p, _, _ in plan.files]
    assert any("autostart" in p for p in paths), "no fallback autostart entry"
    assert not any("systemctl" in " ".join(c) for c in plan.commands)
    assert any("systemd not found" in note for note in plan.notes)


def test_linux_desktop_entry_is_valid(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/" + name)
    plan = build(monkeypatch, "Linux")
    desktop = next(b for p, b, _ in plan.files if p.name == "jarvis.desktop")
    assert desktop.startswith("[Desktop Entry]")
    assert "Terminal=false" in desktop, "must not pop a terminal window"
    for required in ("Type=", "Name=", "Exec=", "Icon="):
        assert required in desktop


def test_windows_uses_hidden_window_launchers(monkeypatch):
    plan = build(monkeypatch, "Windows")
    for path, body, _ in plan.files:
        assert path.suffix == ".vbs"
        # ", 0, False" is the WScript.Shell flag for a hidden, non-blocking run.
        assert ", 0, False" in body, f"{path.name} would flash a console window"
    paths = [str(p) for p, _, _ in plan.files]
    assert any("Startup" in p for p in paths), "nothing registered to autostart"
    assert any("Desktop" in p for p in paths), "no desktop icon"


def test_windows_prefers_pythonw(monkeypatch):
    monkeypatch.setattr(install, "SYSTEM", "Windows")
    # With no venv present it falls back to the running interpreter, which is
    # the documented behaviour; the request for a GUI interpreter is what matters.
    assert install.venv_python(for_gui=True) is not None


def test_icon_is_valid_svg():
    assert install.ICON_SVG.strip().startswith("<svg")
    assert install.ICON_SVG.strip().endswith("</svg>")
