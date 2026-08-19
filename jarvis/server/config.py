"""Runtime configuration for JARVIS.

Every value can be overridden with an environment variable (or a .env file
sitting next to the project root). Nothing here reaches out to the network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader so the project has no import-time dependencies."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # --- identity -------------------------------------------------------
    assistant_name: str = _env("JARVIS_NAME", "JARVIS")
    user_name: str = _env("JARVIS_USER", "Sir")

    # --- models ---------------------------------------------------------
    # The reasoning core. Opus 5 thinks by default and returns summarized
    # reasoning, which is what drives the brain visualization.
    model: str = _env("JARVIS_MODEL", "claude-opus-5")
    # A cheaper model for the background cognition loop and memory chores.
    background_model: str = _env("JARVIS_BACKGROUND_MODEL", "claude-sonnet-5")

    effort: str = _env("JARVIS_EFFORT", "high")  # low|medium|high|xhigh|max
    background_effort: str = _env("JARVIS_BACKGROUND_EFFORT", "low")
    max_tokens: int = _env_int("JARVIS_MAX_TOKENS", 32000)
    background_max_tokens: int = _env_int("JARVIS_BACKGROUND_MAX_TOKENS", 4000)

    # --- server ---------------------------------------------------------
    host: str = _env("JARVIS_HOST", "127.0.0.1")
    port: int = _env_int("JARVIS_PORT", 8788)

    # --- storage --------------------------------------------------------
    db_path: Path = field(
        default_factory=lambda: Path(
            _env("JARVIS_DB", str(PROJECT_ROOT / "data" / "jarvis.db"))
        )
    )
    workspace: Path = field(
        default_factory=lambda: Path(
            _env("JARVIS_WORKSPACE", str(PROJECT_ROOT / "workspace"))
        ).resolve()
    )

    # --- capabilities ---------------------------------------------------
    # Safe mode keeps shell commands on an allowlist and file access inside
    # the workspace. Turning it off hands the model a real shell.
    safe_mode: bool = _env_bool("JARVIS_SAFE_MODE", True)
    enable_web: bool = _env_bool("JARVIS_ENABLE_WEB", True)
    shell_timeout: int = _env_int("JARVIS_SHELL_TIMEOUT", 30)

    # --- autonomy -------------------------------------------------------
    # How often the background mind wakes up to reflect, and how long the
    # user must be quiet before it is allowed to speak first.
    cognition_enabled: bool = _env_bool("JARVIS_COGNITION", True)
    cognition_interval: float = _env_float("JARVIS_COGNITION_INTERVAL", 45.0)
    # Longest gap between real reflection passes when nothing is changing.
    # Ticks in between are free — they never call the model.
    deep_reflection_interval: float = _env_float("JARVIS_DEEP_REFLECTION", 900.0)
    proactive_after_idle: float = _env_float("JARVIS_PROACTIVE_AFTER_IDLE", 120.0)
    proactive_cooldown: float = _env_float("JARVIS_PROACTIVE_COOLDOWN", 300.0)

    # --- conversation ---------------------------------------------------
    history_turns: int = _env_int("JARVIS_HISTORY_TURNS", 40)
    recall_results: int = _env_int("JARVIS_RECALL_RESULTS", 8)

    @property
    def api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY") or None

    @property
    def has_credentials(self) -> bool:
        # The SDK also resolves `ant auth login` profiles, so a missing env
        # var is not proof that we're unauthenticated.
        return bool(
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or (Path.home() / ".config" / "anthropic").exists()
        )


CONFIG = Config()

CONFIG.db_path.parent.mkdir(parents=True, exist_ok=True)
CONFIG.workspace.mkdir(parents=True, exist_ok=True)
