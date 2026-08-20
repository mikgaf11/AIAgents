"""Runtime configuration for JARVIS.

Every value can be overridden with an environment variable (or a .env file
sitting next to the project root). Nothing here reaches out to the network.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse_env_value(raw: str) -> str:
    """Parse one .env value, honouring quotes and trailing comments.

    People copy .env.example, which annotates settings with `# like this`,
    so a naive parser hands the application `high   # low | medium | ...`
    as the value. An inline comment must be preceded by whitespace, which
    keeps a literal '#' inside a password (`pa#ssword`) intact.
    """
    value = raw.strip()
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        # Anything after the closing quote is a comment.
        return value[1:end] if end != -1 else value[1:]
    comment = re.search(r"\s#", value)
    if comment:
        value = value[: comment.start()]
    return value.strip()


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
        if key.startswith("export "):
            key = key[len("export "):].strip()
        os.environ.setdefault(key, parse_env_value(value))


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


def _env_effort(name: str, default: str) -> str:
    """Read an effort level, refusing to pass an invalid one to the API.

    The API rejects anything outside the documented set with a 400, which
    surfaces to the user as a broken assistant rather than a bad setting —
    so a typo is corrected here, loudly, instead of at request time.
    """
    value = _env(name, default).strip().lower()
    if value in VALID_EFFORTS:
        return value
    print(
        f"  [config] {name}={value!r} is not a valid effort level"
        f" ({', '.join(VALID_EFFORTS)}); using {default!r} instead.",
        file=sys.stderr,
    )
    return default


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

    effort: str = _env_effort("JARVIS_EFFORT", "high")
    background_effort: str = _env_effort("JARVIS_BACKGROUND_EFFORT", "low")
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

    # --- email ----------------------------------------------------------
    # Host/port are optional: they're inferred from the address for the
    # common providers. The password must be an app password on any
    # provider with 2FA.
    email_address: str = _env("JARVIS_EMAIL_ADDRESS", "")
    email_password: str = _env("JARVIS_EMAIL_PASSWORD", "")
    email_imap_host: str = _env("JARVIS_EMAIL_IMAP_HOST", "")
    email_imap_port: int = _env_int("JARVIS_EMAIL_IMAP_PORT", 993)
    email_smtp_host: str = _env("JARVIS_EMAIL_SMTP_HOST", "")
    email_smtp_port: int = _env_int("JARVIS_EMAIL_SMTP_PORT", 587)
    email_smtp_ssl: bool = _env_bool("JARVIS_EMAIL_SMTP_SSL", False)
    email_folder: str = _env("JARVIS_EMAIL_FOLDER", "INBOX")
    email_poll_interval: float = _env_float("JARVIS_EMAIL_POLL", 180.0)
    email_batch: int = _env_int("JARVIS_EMAIL_BATCH", 15)
    # Flag high-priority mail in the real mailbox so triage is visible in
    # whatever mail client you already use.
    email_flag_important: bool = _env_bool("JARVIS_EMAIL_FLAG", True)
    # Sending is off by default. Drafts always require explicit approval.
    email_allow_send: bool = _env_bool("JARVIS_EMAIL_ALLOW_SEND", False)
    # Speak up unprompted when mail this important lands (4 = critical).
    email_announce_priority: int = _env_int("JARVIS_EMAIL_ANNOUNCE", 4)

    # --- autonomy schedule ----------------------------------------------
    briefing_hour: int = _env_int("JARVIS_BRIEFING_HOUR", 8)
    briefing_minute: int = _env_int("JARVIS_BRIEFING_MINUTE", 0)
    venture_interval: float = _env_float("JARVIS_VENTURE_INTERVAL", 21600.0)
    task_worker_enabled: bool = _env_bool("JARVIS_TASK_WORKER", True)

    @property
    def email_configured(self) -> bool:
        return bool(self.email_address and self.email_password)

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
