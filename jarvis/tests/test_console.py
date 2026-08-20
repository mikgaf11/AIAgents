"""Secret prompts: hidden input that doesn't look like a frozen terminal."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import console  # noqa: E402


def test_mask_never_reveals_the_middle_of_a_secret():
    secret = "sk-ant-api03-AbCdEfGh12345678xyz"
    masked = console.mask(secret)
    assert "AbCdEfGh12345678" not in masked
    # Enough of the ends to recognise it, and the length as a paste check.
    assert masked.startswith("sk-ant")
    assert masked.endswith("(32 characters)")


def test_mask_handles_short_and_empty_secrets():
    assert console.mask("") == "(empty)"
    # A short secret must not be echoed in full.
    assert console.mask("short") == "s••••"
    assert "hort" not in console.mask("short")


def test_read_secret_announces_that_typing_is_invisible(monkeypatch, capsys):
    """The whole point: the user must be told nothing will appear."""
    monkeypatch.setattr(console.getpass, "getpass", lambda prompt: "sk-ant-secret-value")
    value = console.read_secret("  API key: ")
    printed = capsys.readouterr().out

    assert value == "sk-ant-secret-value"
    assert "NOT appear" in printed
    assert "Paste" in printed
    # It confirms receipt without printing the secret.
    assert "got it:" in printed
    assert "secret-value" not in printed


def test_read_secret_strips_stray_whitespace(monkeypatch):
    monkeypatch.setattr(console.getpass, "getpass", lambda prompt: "  sk-ant-key  \n")
    assert console.read_secret("key: ") == "sk-ant-key"


def test_read_secret_falls_back_when_the_console_cannot_hide_input(monkeypatch, capsys):
    def explode(prompt):
        raise OSError("no tty available")

    monkeypatch.setattr(console.getpass, "getpass", explode)
    monkeypatch.setattr("builtins.input", lambda prompt: "typed-visibly")

    value = console.read_secret("key: ")
    assert value == "typed-visibly"
    assert "cannot hide input" in capsys.readouterr().out


def test_read_secret_returns_empty_when_the_user_cancels(monkeypatch):
    def cancel(prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr(console.getpass, "getpass", cancel)
    assert console.read_secret("key: ") == ""


def test_read_secret_gives_up_quietly_on_a_dead_stdin(monkeypatch):
    """Piped or closed stdin must not hang or crash the installer."""

    def eof(prompt):
        raise EOFError

    monkeypatch.setattr(console.getpass, "getpass", eof)
    assert console.read_secret("key: ") == ""


def test_interaction_support_follows_stdin(monkeypatch):
    monkeypatch.setattr(console.sys, "stdin", type("S", (), {"isatty": lambda self: True})())
    assert console.supports_interaction() is True
    monkeypatch.setattr(console.sys, "stdin", type("S", (), {"isatty": lambda self: False})())
    assert console.supports_interaction() is False
