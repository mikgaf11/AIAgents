"""The local backend: same surface as the Anthropic client, different wire.

These run against a stubbed HTTP transport, so no Ollama install is needed.
What matters is that the adapter presents exactly what the agent loop
expects — the loop must never learn there are two backends.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.backends import make_client  # noqa: E402
from server.backends.ollama import (  # noqa: E402
    OllamaClient,
    OllamaError,
    _to_ollama_messages,
    _to_ollama_tools,
)
from server.config import Config  # noqa: E402


def _ndjson(*chunks: dict) -> bytes:
    return b"".join(json.dumps(c).encode() + b"\n" for c in chunks)


def _client(handler) -> OllamaClient:
    client = OllamaClient(model="test-model")
    client.http = httpx.AsyncClient(
        base_url="http://local", transport=httpx.MockTransport(handler)
    )
    return client


# -- message translation ---------------------------------------------------


def test_a_system_block_list_becomes_one_system_message():
    out = _to_ollama_messages(
        [{"type": "text", "text": "You are JARVIS."},
         {"type": "text", "text": "Be brief."}],
        [{"role": "user", "content": "hi"}],
    )
    assert out[0] == {"role": "system", "content": "You are JARVIS.\n\nBe brief."}
    assert out[1] == {"role": "user", "content": "hi"}


def test_mid_conversation_system_turns_become_system_messages():
    """Anthropic allows role:'system' mid-conversation; Ollama needs it flat."""
    out = _to_ollama_messages(None, [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "Relevant memory: they like jazz."},
    ])
    assert [m["role"] for m in out] == ["user", "system"]


def test_tool_use_and_results_survive_the_round_trip():
    out = _to_ollama_messages(None, [
        {"role": "user", "content": "what time is it"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "should not be replayed"},
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "t1", "name": "get_time", "input": {"tz": "UTC"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "12:00"},
        ]},
    ])

    assistant = out[1]
    assert assistant["content"] == "Checking."
    assert assistant["tool_calls"][0]["function"]["name"] == "get_time"
    # The model's own reasoning is not fed back to it.
    assert "should not be replayed" not in json.dumps(out)

    result = out[2]
    assert result["role"] == "tool" and result["content"] == "12:00"


def test_structured_tool_results_are_flattened_to_text():
    out = _to_ollama_messages(None, [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "part one"},
                         {"type": "text", "text": "part two"}]},
        ]},
    ])
    assert out[0]["content"] == "part one part two"


def test_server_side_tools_are_dropped_rather_than_offered():
    converted = _to_ollama_tools([
        {"name": "read_file", "description": "read", "input_schema": {"type": "object"}},
        {"type": "web_search_20260209", "name": "web_search"},
    ])
    assert [t["function"]["name"] for t in converted] == ["read_file"]


def test_no_tools_converts_to_an_empty_list():
    assert _to_ollama_tools(None) == []


# -- streaming -------------------------------------------------------------


def test_text_streams_as_deltas_and_lands_in_the_final_message():
    def handler(request):
        return httpx.Response(200, content=_ndjson(
            {"message": {"content": "Good "}},
            {"message": {"content": "morning."}},
            {"done": True, "prompt_eval_count": 40, "eval_count": 7},
        ))

    async def run():
        client = _client(handler)
        deltas = []
        async with client.messages.stream(
            model="claude-opus-5", max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    deltas.append(event.delta.text)
            final = await stream.get_final_message()

        assert "".join(deltas) == "Good morning."
        assert final.content[0].text == "Good morning."
        assert final.stop_reason == "end_turn"
        assert final.usage.input_tokens == 40
        assert final.usage.output_tokens == 7
        await client.aclose()

    asyncio.run(run())


def test_think_tags_are_routed_to_thinking_not_the_reply():
    def handler(request):
        return httpx.Response(200, content=_ndjson(
            {"message": {"content": "<think>They asked about "}},
            {"message": {"content": "the weather.</think>It's sunny."}},
            {"done": True},
        ))

    async def run():
        client = _client(handler)
        thinking, text = [], []
        async with client.messages.stream(
            max_tokens=100, messages=[{"role": "user", "content": "weather?"}],
        ) as stream:
            async for event in stream:
                if event.type != "content_block_delta":
                    continue
                if event.delta.type == "thinking_delta":
                    thinking.append(event.delta.thinking)
                else:
                    text.append(event.delta.text)
            final = await stream.get_final_message()

        assert "".join(thinking) == "They asked about the weather."
        assert "".join(text) == "It's sunny."
        # The reply the user sees must not contain the reasoning.
        assert final.content[-1].text == "It's sunny."
        assert final.content[0].type == "thinking"
        await client.aclose()

    asyncio.run(run())


def test_a_tool_call_produces_an_anthropic_shaped_stop_reason():
    def handler(request):
        return httpx.Response(200, content=_ndjson(
            {"message": {"tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}
            ]}},
            {"done": True},
        ))

    async def run():
        client = _client(handler)
        async with client.messages.stream(
            max_tokens=100, messages=[{"role": "user", "content": "read a.txt"}],
        ) as stream:
            async for _ in stream:
                pass
            final = await stream.get_final_message()

        assert final.stop_reason == "tool_use"
        block = final.content[0]
        assert block.type == "tool_use"
        assert block.name == "read_file"
        assert block.input == {"path": "a.txt"}
        assert block.id  # the loop pairs results by id, so one is required
        await client.aclose()

    asyncio.run(run())


def test_tool_arguments_arriving_as_a_json_string_are_parsed():
    def handler(request):
        return httpx.Response(200, content=_ndjson(
            {"message": {"tool_calls": [
                {"function": {"name": "recall", "arguments": '{"query": "jazz"}'}}
            ]}},
            {"done": True},
        ))

    async def run():
        client = _client(handler)
        message = await client.messages.create(
            max_tokens=100, messages=[{"role": "user", "content": "x"}]
        )
        assert message.content[0].input == {"query": "jazz"}
        await client.aclose()

    asyncio.run(run())


def test_a_missing_model_says_which_command_fixes_it():
    def handler(request):
        return httpx.Response(404, text="model not found")

    async def run():
        client = _client(handler)
        with pytest.raises(OllamaError) as caught:
            async with client.messages.stream(
                max_tokens=10, messages=[{"role": "user", "content": "hi"}],
            ) as stream:
                async for _ in stream:
                    pass
        assert "ollama pull test-model" in str(caught.value)
        await client.aclose()

    asyncio.run(run())


def test_an_error_inside_the_stream_is_raised_not_swallowed():
    def handler(request):
        return httpx.Response(200, content=_ndjson({"error": "out of memory"}))

    async def run():
        client = _client(handler)
        with pytest.raises(OllamaError, match="out of memory"):
            async with client.messages.stream(
                max_tokens=10, messages=[{"role": "user", "content": "hi"}],
            ) as stream:
                async for _ in stream:
                    pass
        await client.aclose()

    asyncio.run(run())


# -- model routing ---------------------------------------------------------


def test_claude_model_names_map_onto_the_local_models():
    client = OllamaClient(model="llama3.1:8b", background_model="llama3.2:3b")
    assert client.model_for("claude-opus-5") == "llama3.1:8b"
    # Anything asking for a cheaper Claude gets the cheaper local model.
    assert client.model_for("claude-sonnet-5") == "llama3.2:3b"
    # An explicit local name is honoured as given.
    assert client.model_for("mistral:7b") == "mistral:7b"


def test_background_model_defaults_to_the_main_one():
    client = OllamaClient(model="llama3.1:8b")
    assert client.background_model == "llama3.1:8b"


# -- probing ---------------------------------------------------------------


def test_probe_reports_a_model_that_is_not_pulled():
    def handler(request):
        return httpx.Response(200, json={"models": [{"name": "other:latest"}]})

    async def run():
        client = _client(handler)
        result = await client.probe()
        assert result["ok"] is False
        assert "ollama pull test-model" in result["error"]
        await client.aclose()

    asyncio.run(run())


def test_probe_succeeds_when_the_model_is_installed():
    def handler(request):
        return httpx.Response(200, json={"models": [{"name": "test-model:latest"}]})

    async def run():
        client = _client(handler)
        assert (await client.probe())["ok"] is True
        await client.aclose()

    asyncio.run(run())


def test_probe_explains_an_unreachable_ollama():
    def handler(request):
        raise httpx.ConnectError("refused")

    async def run():
        client = _client(handler)
        result = await client.probe()
        assert result["ok"] is False
        assert "ollama.com" in result["error"]
        await client.aclose()

    asyncio.run(run())


# -- selection -------------------------------------------------------------


class _FakeConfig:
    def __init__(self, backend="auto", has_credentials=False):
        self.backend = backend
        self.has_credentials = has_credentials
        self.model = "claude-opus-5"
        self.ollama_url = "http://127.0.0.1:11434"
        self.ollama_model = "llama3.1:8b"
        self.ollama_background_model = ""


def test_auto_falls_back_to_the_free_local_model_without_credentials():
    client, label = make_client(_FakeConfig(backend="auto", has_credentials=False))
    assert isinstance(client, OllamaClient)
    assert "local" in label
    assert client.is_local is True


def test_ollama_can_be_forced_even_with_credentials_present():
    client, _ = make_client(_FakeConfig(backend="ollama", has_credentials=True))
    assert isinstance(client, OllamaClient)


def test_the_real_config_exposes_a_backend_setting():
    # A typo here would silently route everyone to the wrong backend.
    assert Config().backend in ("auto", "anthropic", "ollama", "local")
