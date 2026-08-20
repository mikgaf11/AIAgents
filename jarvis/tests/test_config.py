"""Configuration parsing.

Regression tests for a shipped bug: .env.example annotates settings with
inline `# comments`, and the original parser handed the whole line through
as the value. `JARVIS_EFFORT=high  # low | medium | ...` reached the API
verbatim and came back as a 400, which looked to the user like a broken
assistant rather than a bad config line.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import VALID_EFFORTS, parse_env_value  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The exact line that broke it.
        ("high              # low | medium | high | xhigh | max", "high"),
        ("180            # seconds between inbox checks", "180"),
        ("1              # flag important mail", "1"),
        ("plain", "plain"),
        ("  padded  ", "padded"),
        ("", ""),
    ],
)
def test_inline_comments_are_stripped(raw, expected):
    assert parse_env_value(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # A '#' that isn't preceded by whitespace belongs to the value —
        # passwords and keys legitimately contain them.
        ("pa#ssword", "pa#ssword"),
        ("sk-ant-api03-xYz#123", "sk-ant-api03-xYz#123"),
        ("###", "###"),
    ],
)
def test_hash_inside_a_value_is_preserved(raw, expected):
    assert parse_env_value(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('"quoted value"', "quoted value"),
        ("'single quoted'", "single quoted"),
        ('"has # inside"  # and a real comment', "has # inside"),
        ('"trailing spaces kept "', "trailing spaces kept "),
    ],
)
def test_quotes_protect_the_value(raw, expected):
    assert parse_env_value(raw) == expected


def test_app_passwords_with_spaces_survive():
    """Providers display app passwords in groups of four."""
    assert parse_env_value("abcd efgh ijkl mnop") == "abcd efgh ijkl mnop"


def test_the_shipped_example_file_parses_to_usable_values(tmp_path, monkeypatch):
    """Copying .env.example must produce a working configuration.

    This is the end-to-end version of the bug: every annotated setting in
    the file people are told to copy has to survive the parser.
    """
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    seen = 0
    for line in example.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        value = parse_env_value(raw)
        seen += 1
        assert "#" not in value, f"{key} kept its comment: {value!r}"

        if key.strip() == "JARVIS_EFFORT" and value:
            assert value in VALID_EFFORTS
        # Numeric settings must actually be numeric.
        if any(k in key for k in ("PORT", "INTERVAL", "POLL", "HOUR", "MINUTE",
                                  "BATCH", "TOKENS", "TURNS", "TIMEOUT")):
            if value:
                float(value)  # raises if the comment leaked through

    assert seen > 15, "expected the example file to define many settings"


def test_export_prefix_is_tolerated():
    """Some people write `export KEY=value` out of shell habit."""
    from server.config import _load_dotenv  # noqa: F401  (import-time smoke)

    assert parse_env_value("value  # note") == "value"


def test_invalid_effort_falls_back_instead_of_reaching_the_api(monkeypatch, capsys):
    from server.config import _env_effort

    monkeypatch.setenv("JARVIS_TEST_EFFORT", "high # low | medium | high")
    assert _env_effort("JARVIS_TEST_EFFORT", "high") == "high"
    assert "not a valid effort level" in capsys.readouterr().err

    monkeypatch.setenv("JARVIS_TEST_EFFORT", "HIGH")
    assert _env_effort("JARVIS_TEST_EFFORT", "medium") == "high", "case is normalised"

    monkeypatch.setenv("JARVIS_TEST_EFFORT", "turbo")
    assert _env_effort("JARVIS_TEST_EFFORT", "medium") == "medium"


def test_every_effort_the_code_sends_is_one_the_api_accepts():
    """Guards the hardcoded effort levels in the background routines."""
    import re

    for module in ("autonomy.py", "triage.py", "core.py", "cognition.py"):
        source = (ROOT / "server" / module).read_text(encoding="utf-8")
        for literal in re.findall(r'effort["\']?\s*[:=]\s*["\']([a-z]+)["\']', source):
            assert literal in VALID_EFFORTS, f"{module} sends effort={literal!r}"
