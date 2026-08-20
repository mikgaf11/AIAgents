"""Where the thinking happens.

Two backends, same interface:

    anthropic  Claude via the API — best reasoning, costs money per token
    ollama     a model running on this machine — free forever, weaker

Both expose `client.messages.stream(...)` and `client.messages.create(...)`,
so the agent loop, triage and the background worker never know which one
they are talking to.
"""

from __future__ import annotations

from typing import Any

from ..events import BUS


def make_client(config) -> tuple[Any, str]:
    """Build the configured backend. Returns (client, description)."""
    backend = (config.backend or "auto").lower()

    if backend in ("ollama", "local"):
        return _make_ollama(config)

    if backend == "auto":
        # Prefer Claude when credentials exist, otherwise fall back to a
        # local model so a fresh install still thinks.
        if config.has_credentials:
            client, label = _make_anthropic(config)
            if client is not None:
                return client, label
        client, label = _make_ollama(config)
        if client is not None:
            return client, label
        return None, "offline"

    return _make_anthropic(config)


def _make_anthropic(config) -> tuple[Any, str]:
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        BUS.emit("error", message="anthropic SDK not installed.")
        return None, "offline"
    if not config.has_credentials:
        BUS.emit(
            "error",
            message=(
                "No Anthropic credentials. Set ANTHROPIC_API_KEY, or set"
                " JARVIS_BACKEND=ollama to run free on a local model."
            ),
        )
        return None, "offline"
    try:
        # Long turns are normal at high effort; the timeout has to allow for
        # minutes of thinking plus tool rounds.
        return AsyncAnthropic(timeout=900.0, max_retries=3), config.model
    except Exception as exc:  # noqa: BLE001 - surface, don't crash the server
        BUS.emit("error", message=f"Could not create Anthropic client: {exc}")
        return None, "offline"


def _make_ollama(config) -> tuple[Any, str]:
    try:
        from .ollama import OllamaClient
    except ImportError as exc:  # pragma: no cover - httpx ships with anthropic
        BUS.emit("error", message=f"Local backend unavailable: {exc}")
        return None, "offline"
    client = OllamaClient(
        base_url=config.ollama_url,
        model=config.ollama_model,
        background_model=config.ollama_background_model,
    )
    return client, f"{config.ollama_model} (local)"
