"""Tests that run without an API key or network access."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import CONFIG  # noqa: E402
from server.events import EventBus  # noqa: E402
from server.memory import Memory, _fts_query  # noqa: E402
from server.tools import ToolRegistry, collect_system_status  # noqa: E402


@pytest.fixture()
def memory(tmp_path):
    store = Memory(tmp_path / "test.db")
    yield store
    store.close()


@pytest.fixture()
def tools(memory):
    return ToolRegistry(memory)


# -- memory ---------------------------------------------------------------


def test_episode_roundtrip(memory):
    async def run():
        await memory.add_episode("user", "the launch window is Thursday")
        await memory.add_episode("assistant", "noted")
        recent = await memory.recent_episodes(10)
        assert [e["role"] for e in recent] == ["user", "assistant"]

    asyncio.run(run())


def test_recall_ranks_facts_above_episodes(memory):
    async def run():
        await memory.add_fact("user.timezone", "lives in Reykjavik", importance=0.9)
        await memory.add_episode("user", "something about reykjavik weather")
        hits = await memory.recall("reykjavik", limit=5)
        assert hits, "expected at least one hit"
        assert hits[0].kind == "fact"
        assert "Reykjavik" in hits[0].text

    asyncio.run(run())


def test_facts_are_updated_not_duplicated(memory):
    async def run():
        first = await memory.add_fact("user.car", "drives a blue estate")
        second = await memory.add_fact("user.car", "drives a red coupe")
        assert first == second
        facts = await memory.all_facts()
        matching = [f for f in facts if f["subject"] == "user.car"]
        assert len(matching) == 1
        assert matching[0]["content"] == "drives a red coupe"

    asyncio.run(run())


def test_recall_survives_fts_operator_characters(memory):
    async def run():
        await memory.add_fact("project", "shipping the thing")
        # Raw FTS5 would treat these as syntax; the query builder must not.
        for hostile in ['"unbalanced', "NEAR(", "a AND OR b", "*", "-x", ""]:
            await memory.recall(hostile, limit=3)

    asyncio.run(run())


def test_fts_query_quotes_every_token():
    assert _fts_query('rocket "launch"') == '"rocket" OR "launch"'
    assert _fts_query("!!!") == ""


def test_goals_and_progress(memory):
    async def run():
        goal_id = await memory.add_goal("finish the HUD", "webgl brain", priority=1)
        assert await memory.update_goal(goal_id, progress=0.5)
        open_goals = await memory.list_goals("open")
        assert open_goals[0]["progress"] == 0.5
        await memory.update_goal(goal_id, status="done")
        assert await memory.list_goals("open") == []

    asyncio.run(run())


def test_due_reminders_fire_once(memory):
    async def run():
        await memory.add_reminder("stand up", time.time() - 1)
        await memory.add_reminder("much later", time.time() + 9999)
        first = await memory.due_reminders()
        assert [r["text"] for r in first] == ["stand up"]
        assert await memory.due_reminders() == []

    asyncio.run(run())


# -- tools ----------------------------------------------------------------


def test_tool_specs_include_server_tools(tools):
    names = {spec.get("name") for spec in tools.specs()}
    assert {"remember", "recall", "get_time", "run_python"} <= names
    if CONFIG.enable_web:
        assert "web_search" in names


def test_unknown_tool_is_an_error_not_a_crash(tools):
    async def run():
        output, is_error = await tools.execute("nonexistent", {})
        assert is_error and "Unknown tool" in output

    asyncio.run(run())


def test_run_python_captures_stdout(tools):
    async def run():
        output, is_error = await tools.execute("run_python", {"code": "print(6*7)"})
        assert not is_error
        assert "42" in output

    asyncio.run(run())


def test_run_python_reports_exceptions_without_raising(tools):
    async def run():
        output, is_error = await tools.execute("run_python", {"code": "1/0"})
        # Tracebacks come back as normal output so the model can react to them.
        assert not is_error
        assert "ZeroDivisionError" in output

    asyncio.run(run())


def test_safe_mode_blocks_path_escape(tools):
    async def run():
        output, is_error = await tools.execute("read_file", {"path": "/etc/passwd"})
        if CONFIG.safe_mode:
            assert is_error and "outside the folders I'm allowed to touch" in output
        else:
            assert True  # safe mode disabled by the operator; nothing to assert

    asyncio.run(run())


def test_safe_mode_blocks_shell_metacharacters(tools):
    async def run():
        output, is_error = await tools.execute(
            "shell", {"command": "ls; cat /etc/passwd"}
        )
        if CONFIG.safe_mode:
            assert is_error and "shell operators" in output

    asyncio.run(run())


def test_file_write_then_read(tools):
    async def run():
        written, err = await tools.execute(
            "write_file", {"path": "notes/test.txt", "content": "hello core"}
        )
        assert not err, written
        read, err = await tools.execute("read_file", {"path": "notes/test.txt"})
        assert not err
        assert read == "hello core"

    asyncio.run(run())


def test_reminder_tool_requires_a_time(tools):
    async def run():
        output, is_error = await tools.execute("set_reminder", {"text": "no when"})
        assert is_error and "in_seconds" in output

    asyncio.run(run())


def test_system_status_has_core_fields():
    status = collect_system_status()
    assert "platform" in status and "cpu_count" in status


# -- event bus ------------------------------------------------------------


def test_event_bus_fans_out():
    async def run():
        bus = EventBus()
        a, b = bus.subscribe(), bus.subscribe()
        bus.emit("state", state="thinking")
        assert (await a.get())["state"] == "thinking"
        assert (await b.get())["state"] == "thinking"

    asyncio.run(run())


def test_slow_subscriber_drops_events_instead_of_blocking():
    async def run():
        bus = EventBus(queue_size=4)
        queue = bus.subscribe()
        for i in range(20):
            bus.emit("token", text=str(i))
        assert queue.qsize() == 4
        # The oldest were dropped, so the newest survived.
        drained = [queue.get_nowait()["text"] for _ in range(4)]
        assert drained[-1] == "19"

    asyncio.run(run())


def test_token_events_are_not_kept_in_replay_history():
    bus = EventBus()
    bus.emit("token", text="x")
    bus.emit("state", state="idle")
    kinds = [e["kind"] for e in bus.replay()]
    assert kinds == ["state"]
