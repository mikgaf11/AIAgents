"""Exercise the real streaming tool loop against a scripted mock model.

These tests drive `Jarvis` through the same code path a live API call takes
— streamed deltas, tool_use rounds, pause_turn resumption, refusals — with
no network and no API key.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.core import Jarvis  # noqa: E402
from server.events import BUS  # noqa: E402
from server.memory import Memory  # noqa: E402
from server.tools import ToolRegistry  # noqa: E402


# -- scripted stand-in for anthropic.AsyncAnthropic ------------------------


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(name, tool_id, payload):
    return SimpleNamespace(type="tool_use", name=name, id=tool_id, input=payload)


def turn(content, stop_reason, *, deltas=(), stop_details=None):
    """One scripted assistant turn: the deltas to stream, then the result."""
    return SimpleNamespace(
        content=list(content),
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=25, cache_read_input_tokens=40
        ),
        _deltas=list(deltas),
    )


def delta(kind, value):
    field = {"text_delta": "text", "thinking_delta": "thinking"}[kind]
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(**{"type": kind, field: value}),
    )


class FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for event in self.message._deltas:
            yield event

    def __aiter__(self):  # noqa: F811 - async generator form used above
        async def gen():
            for event in self.message._deltas:
                yield event
        return gen()

    async def get_final_message(self):
        return self.message


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def stream(self, **params):
        self.calls.append(params)
        if not self.script:
            raise AssertionError("model called more times than the script allows")
        return FakeStream(self.script.pop(0))


class FakeClient:
    def __init__(self, script):
        self.messages = FakeMessages(script)


# -- fixtures --------------------------------------------------------------


@pytest.fixture()
def jarvis(tmp_path):
    memory = Memory(tmp_path / "core.db")
    agent = Jarvis(memory, ToolRegistry(memory))
    yield agent
    memory.close()


def collect(bus_queue):
    events = []
    while not bus_queue.empty():
        events.append(bus_queue.get_nowait())
    return events


# -- tests -----------------------------------------------------------------


def test_tool_round_executes_and_feeds_results_back(jarvis):
    """A tool_use turn must run the tool and continue with a tool_result."""
    jarvis.client = FakeClient(
        [
            turn(
                [text_block("Checking."), tool_block("get_time", "tu_1", {})],
                "tool_use",
                deltas=[delta("text_delta", "Checking.")],
            ),
            turn([text_block("It is currently mid-afternoon.")], "end_turn"),
        ]
    )
    queue = BUS.subscribe()
    reply = asyncio.run(jarvis.respond("what time is it"))
    BUS.unsubscribe(queue)

    assert reply == "It is currently mid-afternoon."
    assert len(jarvis.client.messages.calls) == 2

    # The second request must carry a tool_result matching the tool_use id.
    second = jarvis.client.messages.calls[1]["messages"]
    results = [
        block
        for message in second
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert len(results) == 1
    assert results[0]["tool_use_id"] == "tu_1"
    assert results[0]["is_error"] is False
    # get_time returns real clock data, so the result should mention a year.
    assert "20" in results[0]["content"]

    kinds = [e["kind"] for e in collect(queue)]
    assert "tool_start" in kinds and "tool_end" in kinds and "turn_end" in kinds


def test_outgoing_request_carries_a_valid_effort_and_thinking_config(jarvis):
    """The request actually sent must satisfy the API's schema.

    A malformed effort reaches the user as "I hit an error reaching my
    reasoning core", so it's worth asserting on the real outgoing payload
    rather than only on the parsed config.
    """
    from server.config import VALID_EFFORTS

    jarvis.client = FakeClient([turn([text_block("Fine.")], "end_turn")])
    asyncio.run(jarvis.respond("hello"))

    params = jarvis.client.messages.calls[0]
    assert params["output_config"]["effort"] in VALID_EFFORTS
    assert params["thinking"]["type"] == "adaptive"
    # Summarized thinking is what drives the brain visualization.
    assert params["thinking"]["display"] == "summarized"
    assert isinstance(params["max_tokens"], int) and params["max_tokens"] > 0


def test_parallel_tool_calls_return_in_one_user_message(jarvis):
    """Splitting results across messages trains the model out of parallelism."""
    jarvis.client = FakeClient(
        [
            turn(
                [
                    tool_block("get_time", "tu_a", {}),
                    tool_block("system_status", "tu_b", {}),
                ],
                "tool_use",
            ),
            turn([text_block("Both checked.")], "end_turn"),
        ]
    )
    asyncio.run(jarvis.respond("time and system status"))

    second = jarvis.client.messages.calls[1]["messages"]
    tool_messages = [
        m
        for m in second
        if isinstance(m.get("content"), list)
        and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"]
        )
    ]
    assert len(tool_messages) == 1, "results must be batched into a single turn"
    assert len(tool_messages[0]["content"]) == 2


def test_failing_tool_returns_is_error_not_an_exception(jarvis):
    jarvis.client = FakeClient(
        [
            turn([tool_block("read_file", "tu_x", {"path": "/nope/missing"})], "tool_use"),
            turn([text_block("That path doesn't exist.")], "end_turn"),
        ]
    )
    reply = asyncio.run(jarvis.respond("read a missing file"))
    assert reply == "That path doesn't exist."

    second = jarvis.client.messages.calls[1]["messages"]
    result = [
        b
        for m in second
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ][0]
    assert result["is_error"] is True


def test_pause_turn_resumes_without_a_new_user_message(jarvis):
    """Server-side tools pause; the loop re-sends and the server resumes."""
    jarvis.client = FakeClient(
        [
            turn([text_block("Searching…")], "pause_turn"),
            turn([text_block("Here is what I found.")], "end_turn"),
        ]
    )
    reply = asyncio.run(jarvis.respond("search the web"))
    assert reply == "Here is what I found."

    second = jarvis.client.messages.calls[1]["messages"]
    # The resumed request ends on the paused assistant turn — no synthetic
    # "continue" user message may be appended.
    assert second[-1]["role"] == "assistant"


def test_refusal_is_reported_not_raised(jarvis):
    jarvis.client = FakeClient(
        [
            turn(
                [],
                "refusal",
                stop_details=SimpleNamespace(category="cyber", explanation="no"),
            )
        ]
    )
    queue = BUS.subscribe()
    reply = asyncio.run(jarvis.respond("something disallowed"))
    events = collect(queue)
    BUS.unsubscribe(queue)

    assert "declining" in reply and "cyber" in reply
    assert any(e["kind"] == "error" for e in events)


def test_streamed_deltas_reach_the_event_bus(jarvis):
    jarvis.client = FakeClient(
        [
            turn(
                [text_block("Answer.")],
                "end_turn",
                deltas=[
                    delta("thinking_delta", "weighing options"),
                    delta("text_delta", "Ans"),
                    delta("text_delta", "wer."),
                ],
            )
        ]
    )
    queue = BUS.subscribe()
    asyncio.run(jarvis.respond("think about something"))
    events = collect(queue)
    BUS.unsubscribe(queue)

    thinking = [e["text"] for e in events if e["kind"] == "thinking_token"]
    tokens = [e["text"] for e in events if e["kind"] == "token"]
    assert thinking == ["weighing options"]
    assert "".join(tokens) == "Answer."


def test_usage_totals_accumulate_across_rounds(jarvis):
    jarvis.client = FakeClient(
        [
            turn([tool_block("get_time", "tu_1", {})], "tool_use"),
            turn([text_block("Done.")], "end_turn"),
        ]
    )
    asyncio.run(jarvis.respond("what time is it"))
    assert jarvis.total_tokens["input"] == 200
    assert jarvis.total_tokens["output"] == 50
    assert jarvis.total_tokens["cache_read"] == 80


def test_transport_failure_becomes_a_spoken_error_not_a_crash(jarvis):
    class Exploding:
        def __init__(self):
            self.messages = self

        def stream(self, **_):
            raise RuntimeError("connection reset")

    jarvis.client = Exploding()
    queue = BUS.subscribe()
    reply = asyncio.run(jarvis.respond("hello"))
    events = collect(queue)
    BUS.unsubscribe(queue)

    assert "connection reset" in reply
    assert any(e["kind"] == "error" for e in events)
    assert jarvis.state == "idle", "state must recover after an error"


def test_history_trim_never_orphans_a_tool_result(jarvis):
    """Trimming must not leave a tool_result whose tool_use was dropped."""
    jarvis.messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": [tool_block("get_time", "t1", {})]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "ok", "is_error": False}]},
        {"role": "assistant", "content": "done"},
    ] * 30

    from server.config import CONFIG

    jarvis._trim_history()
    assert len(jarvis.messages) <= CONFIG.history_turns + 4
    first = jarvis.messages[0]
    assert first["role"] == "user"
    assert not (
        isinstance(first.get("content"), list)
        and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in first["content"]
        )
    ), "history must not start with an orphaned tool_result"


class CountingReflector:
    """Stands in for the background model, counting how often it's consulted."""

    def __init__(self):
        self.messages = self
        self.calls = 0

    async def create(self, **_):
        self.calls += 1
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[text_block('{"thought": "quiet", "mood": "calm", "speak": null}')],
        )


def test_cognition_skips_the_model_when_nothing_changed(jarvis):
    """An always-on assistant must not pay for reflection on an idle machine."""
    from server.cognition import Cognition

    jarvis.client = CountingReflector()
    mind = Cognition(jarvis)

    asyncio.run(mind.tick())
    assert jarvis.client.calls == 1, "first pass should reflect"

    # Nothing has changed — these ticks must be free.
    asyncio.run(mind.tick())
    asyncio.run(mind.tick())
    assert jarvis.client.calls == 1, "unchanged world must not trigger the model"

    # A new episode changes the world, so reflection resumes.
    asyncio.run(jarvis.memory.add_episode("user", "something new happened"))
    asyncio.run(mind.tick())
    assert jarvis.client.calls == 2, "a changed world must trigger reflection"


def test_cognition_reflects_again_once_the_pass_goes_stale(jarvis):
    """Even with a static world it must think occasionally, for time-based things."""
    from server.cognition import Cognition

    jarvis.client = CountingReflector()
    mind = Cognition(jarvis)
    asyncio.run(mind.tick())
    assert jarvis.client.calls == 1

    # Pretend the last reflection was long ago.
    mind._last_reflection = 0.0
    asyncio.run(mind.tick())
    assert jarvis.client.calls == 2


def test_cognition_never_speaks_while_a_turn_is_running(jarvis):
    from server.cognition import Cognition

    jarvis.client = CountingReflector()
    mind = Cognition(jarvis)

    async def run():
        async with jarvis.busy:  # a turn is in flight
            await mind.tick()

    asyncio.run(run())
    assert jarvis.client.calls == 0, "must not think over the top of a live turn"


def test_current_utterance_is_not_recalled_back_as_memory(jarvis):
    """Retrieval runs before the episode is stored, or every turn echoes itself."""
    jarvis.client = FakeClient([turn([text_block("Noted.")], "end_turn")])
    phrase = "the zeppelin manifest is overdue"
    asyncio.run(jarvis.respond(phrase))

    sent = jarvis.client.messages.calls[0]["messages"]
    context = [
        m["content"]
        for m in sent
        if m["role"] == "system" and isinstance(m["content"], str)
    ]
    assert not any(phrase in block for block in context), (
        "the turn recalled its own utterance as prior memory"
    )
    # It must still be persisted for future turns.
    stored = asyncio.run(jarvis.memory.recent_episodes(5))
    assert any(phrase in e["content"] for e in stored)


def test_system_message_rejection_folds_context_into_user_turn(jarvis):
    """A model that rejects role:system must transparently fall back."""

    class PickyClient:
        def __init__(self):
            self.messages = self
            self.calls = []
            self.script = [turn([text_block("Recovered.")], "end_turn")]

        def stream(self, **params):
            self.calls.append(params)
            if any(m.get("role") == "system" for m in params["messages"]):
                raise RuntimeError(
                    "messages: Unexpected role 'system'. "
                    "The role 'system' is not supported on this model."
                )
            return FakeStream(self.script.pop(0))

    asyncio.run(jarvis.memory.add_fact("user.city", "lives in Oslo", importance=0.9))
    jarvis.client = PickyClient()

    reply = asyncio.run(jarvis.respond("remind me what city that was"))

    assert reply == "Recovered."
    assert jarvis.mid_conversation_system is False
    assert len(jarvis.client.calls) == 2
    retried = jarvis.client.calls[1]["messages"]
    assert all(m["role"] != "system" for m in retried)
    # The recalled context must survive the fold, not be silently dropped.
    assert any("Oslo" in m["content"] for m in retried if isinstance(m["content"], str))
