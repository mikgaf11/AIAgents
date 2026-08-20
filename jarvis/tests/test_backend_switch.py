"""Switching between Claude and the free local model.

The risk here is a botched .env: this file also holds the API key and the
mail password, so a rewrite that loses a line costs the user real setup.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend  # noqa: E402


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(backend, "ENV_FILE", path)
    return path


# -- reading ---------------------------------------------------------------


def test_a_missing_setting_means_the_built_in_default(env_file):
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-x\n", encoding="utf-8")
    assert backend.current_setting() == "auto"


def test_no_env_file_at_all_still_reports_a_setting(env_file):
    assert backend.current_setting() == "auto"


def test_an_inline_comment_is_not_part_of_the_value(env_file):
    """The exact shape .env.example ships, which bit us once already."""
    env_file.write_text(
        "JARVIS_BACKEND=ollama              # auto | anthropic | ollama\n",
        encoding="utf-8",
    )
    assert backend.current_setting() == "ollama"


def test_an_exported_line_is_read_like_any_other(env_file):
    env_file.write_text("export JARVIS_BACKEND=anthropic\n", encoding="utf-8")
    assert backend.current_setting() == "anthropic"


# -- writing ---------------------------------------------------------------


def test_switching_rewrites_only_the_backend_line(env_file):
    env_file.write_text(
        "ANTHROPIC_API_KEY=sk-ant-secret\n"
        "JARVIS_BACKEND=auto\n"
        "JARVIS_EMAIL_PASSWORD=hunter2\n"
        "# a comment worth keeping\n",
        encoding="utf-8",
    )
    backend.write_setting("ollama")

    body = env_file.read_text(encoding="utf-8")
    assert "JARVIS_BACKEND=ollama" in body
    assert "JARVIS_BACKEND=auto" not in body
    # Everything else survives untouched.
    assert "ANTHROPIC_API_KEY=sk-ant-secret" in body
    assert "JARVIS_EMAIL_PASSWORD=hunter2" in body
    assert "# a comment worth keeping" in body


def test_an_env_written_before_this_setting_existed_gains_the_line(env_file):
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-x\n", encoding="utf-8")
    backend.write_setting("ollama")

    body = env_file.read_text(encoding="utf-8")
    assert body.startswith("JARVIS_BACKEND=ollama")
    assert "ANTHROPIC_API_KEY=sk-ant-x" in body
    assert backend.current_setting() == "ollama"


def test_an_exported_setting_is_replaced_not_duplicated(env_file):
    env_file.write_text("export JARVIS_BACKEND=auto\n", encoding="utf-8")
    backend.write_setting("ollama")

    body = env_file.read_text(encoding="utf-8")
    assert body.count("JARVIS_BACKEND") == 1
    assert backend.current_setting() == "ollama"


def test_the_file_keeps_owner_only_permissions(env_file):
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-x\n", encoding="utf-8")
    backend.write_setting("ollama")
    assert env_file.stat().st_mode & 0o077 == 0  # nothing for group or others


def test_switching_back_and_forth_is_stable(env_file):
    env_file.write_text("JARVIS_BACKEND=auto\nJARVIS_PORT=8788\n", encoding="utf-8")
    for value in ("ollama", "anthropic", "ollama", "auto"):
        backend.write_setting(value)
        assert backend.current_setting() == value
    assert "JARVIS_PORT=8788" in env_file.read_text(encoding="utf-8")
    assert env_file.read_text(encoding="utf-8").count("JARVIS_BACKEND") == 1


# -- refusing to break things ---------------------------------------------


def test_local_is_refused_when_ollama_is_not_usable(env_file, monkeypatch, capsys):
    """Writing the setting anyway would leave a core that cannot think."""
    env_file.write_text("JARVIS_BACKEND=auto\n", encoding="utf-8")
    monkeypatch.setattr(
        backend, "check_ollama", lambda: (False, "Model 'llama3.1:8b' isn't pulled yet.")
    )
    assert backend.switch("local") == 1
    assert backend.current_setting() == "auto"  # unchanged
    assert "Nothing was changed" in capsys.readouterr().out


def test_claude_is_refused_without_credentials(env_file, monkeypatch, capsys):
    env_file.write_text("JARVIS_BACKEND=ollama\n", encoding="utf-8")
    monkeypatch.setattr(backend.CONFIG.__class__, "has_credentials",
                        property(lambda self: False))
    assert backend.switch("claude") == 1
    assert backend.current_setting() == "ollama"
    assert "Nothing was changed" in capsys.readouterr().out


def test_a_usable_local_backend_is_written_and_the_core_restarted(
    env_file, monkeypatch
):
    env_file.write_text("JARVIS_BACKEND=auto\n", encoding="utf-8")
    monkeypatch.setattr(backend, "check_ollama", lambda: (True, "llama3.1:8b"))
    restarts = []
    monkeypatch.setitem(sys.modules, "restart",
                        type(sys)("restart"))
    sys.modules["restart"].main = lambda: (restarts.append(1), 0)[1]
    monkeypatch.setattr(backend, "live_status", lambda: None)

    assert backend.switch("local") == 0
    assert backend.current_setting() == "ollama"
    assert restarts == [1]


# -- naming ----------------------------------------------------------------


def test_the_words_a_person_would_actually_type_all_work():
    for word in ("local", "free", "ollama"):
        assert backend.CHOICES[word] == "ollama"
    for word in ("claude", "anthropic", "api"):
        assert backend.CHOICES[word] == "anthropic"


def test_an_unknown_option_changes_nothing(env_file, capsys):
    env_file.write_text("JARVIS_BACKEND=auto\n", encoding="utf-8")
    monkey = sys.argv
    try:
        sys.argv = ["backend.py", "sideways"]
        assert backend.main() == 2
    finally:
        sys.argv = monkey
    assert backend.current_setting() == "auto"
