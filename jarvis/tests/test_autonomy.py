"""Background routines, the work queue, and venture tracking."""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.autonomy import WORKER_TOOLS, Autonomy, Routine  # noqa: E402
from server.memory import Memory  # noqa: E402
from server.tools import ToolRegistry  # noqa: E402


@pytest.fixture()
def memory(tmp_path):
    store = Memory(tmp_path / "work.db")
    yield store
    store.close()


@pytest.fixture()
def tools(memory):
    return ToolRegistry(memory)


# -- scheduling ------------------------------------------------------------


async def _noop() -> None:
    return None


def test_interval_routine_is_due_only_after_its_interval():
    routine = Routine("test", 60.0, _noop, last_run=time.time())
    assert routine.due(time.time()) is False
    assert routine.due(time.time() + 61) is True


def test_disabled_routine_is_never_due():
    routine = Routine("test", 1.0, _noop, enabled=False, last_run=0)
    assert routine.due(time.time()) is False


def test_wall_clock_routine_fires_once_a_day_at_its_hour():
    routine = Routine("briefing", 0, _noop, at_hour=8, at_minute=0, last_run=0)

    def at(hour, minute=0):
        return datetime.now().replace(hour=hour, minute=minute, second=0).timestamp()

    assert routine.due(at(7, 59)) is False
    assert routine.due(at(8, 0)) is True

    # Having just run, it must not fire again the same morning.
    routine.last_run = at(8, 0)
    assert routine.due(at(8, 30)) is False


# -- the work queue --------------------------------------------------------


def test_queue_claim_and_finish_cycle(memory):
    async def run():
        task_id = await memory.queue_task("Research pricing", "for the venture")
        claimed = await memory.next_task()
        assert claimed["id"] == task_id
        assert claimed["title"] == "Research pricing"

        # A claimed task must not be handed out twice.
        assert await memory.next_task() is None

        await memory.finish_task(task_id, "found three comparables")
        tasks = await memory.list_tasks()
        assert tasks[0]["status"] == "done"
        assert "comparables" in tasks[0]["result"]

    asyncio.run(run())


def test_identical_work_is_not_queued_twice(memory):
    async def run():
        first = await memory.queue_task("Draft reply to Ada")
        second = await memory.queue_task("Draft reply to Ada")
        assert first == second
        pending = [t for t in await memory.list_tasks() if t["status"] == "pending"]
        assert len(pending) == 1

        # Once it's finished, the same title may legitimately be queued again.
        await memory.next_task()
        await memory.finish_task(first, "done")
        third = await memory.queue_task("Draft reply to Ada")
        assert third != first

    asyncio.run(run())


def test_priority_orders_the_queue(memory):
    async def run():
        await memory.queue_task("low", priority=5)
        await memory.queue_task("urgent", priority=1)
        assert (await memory.next_task())["title"] == "urgent"

    asyncio.run(run())


def test_tasks_orphaned_by_a_restart_are_requeued(memory):
    async def run():
        task_id = await memory.queue_task("Long job")
        await memory.next_task()  # now 'running'
        # Simulate the process dying mid-run an hour ago.
        memory._db.execute(
            "UPDATE tasks SET started = ? WHERE id = ?", (time.time() - 3600, task_id)
        )
        memory._db.commit()

        requeued = await memory.requeue_stuck_tasks()
        assert requeued == 1
        assert (await memory.next_task())["id"] == task_id

    asyncio.run(run())


def test_repeatedly_failing_tasks_are_abandoned_not_looped(memory):
    async def run():
        task_id = await memory.queue_task("Doomed job")
        for _ in range(3):
            await memory.next_task()
            memory._db.execute(
                "UPDATE tasks SET started = ? WHERE id = ?",
                (time.time() - 3600, task_id),
            )
            memory._db.commit()
            await memory.requeue_stuck_tasks()

        statuses = {t["id"]: t["status"] for t in await memory.list_tasks()}
        assert statuses[task_id] == "failed", "must give up rather than retry forever"

    asyncio.run(run())


# -- ventures --------------------------------------------------------------


def test_ventures_are_recorded_and_refined_not_duplicated(memory):
    async def run():
        first = await memory.add_venture(
            {"title": "Freelance data cleanup", "thesis": "they know pandas",
             "next_step": "list 5 agencies", "confidence": 0.3}
        )
        second = await memory.add_venture(
            {"title": "freelance data cleanup", "thesis": "refined",
             "next_step": "email 5 agencies", "confidence": 0.45}
        )
        assert first == second, "same idea must sharpen, not duplicate"

        ventures = await memory.list_ventures()
        assert len(ventures) == 1
        assert ventures[0]["next_step"] == "email 5 agencies"
        assert ventures[0]["confidence"] == 0.45

    asyncio.run(run())


def test_venture_status_transitions(memory):
    async def run():
        venture_id = await memory.add_venture({"title": "Newsletter", "next_step": "x"})
        assert await memory.update_venture(venture_id, status="active")
        assert [v["title"] for v in await memory.list_ventures("active")] == ["Newsletter"]
        await memory.update_venture(venture_id, status="dropped")
        assert await memory.list_ventures("active") == []

    asyncio.run(run())


def test_a_proposal_without_a_concrete_next_step_is_rejected(tools):
    """An idea with no first step isn't actionable, so the tool refuses it."""

    async def run():
        output, is_error = await tools.execute(
            "propose_venture", {"title": "Do something lucrative"}
        )
        assert is_error
        assert "next_step" in output

    asyncio.run(run())


def test_venture_tool_records_and_lists(tools):
    async def run():
        output, is_error = await tools.execute(
            "propose_venture",
            {
                "title": "Local SEO for trades",
                "thesis": "they build websites already",
                "next_step": "call three plumbers this week",
                "confidence": 0.35,
                "category": "service",
            },
        )
        assert not is_error, output
        listed, _ = await tools.execute("list_ventures", {"status": "all"})
        assert "Local SEO for trades" in listed
        assert "call three plumbers" in listed

    asyncio.run(run())


# -- worker surface --------------------------------------------------------


def test_background_worker_cannot_send_email_or_run_a_shell(tools):
    """Autonomous work may draft and research; it may not act outwardly."""
    assert "draft_reply" in WORKER_TOOLS, "it should be able to prepare replies"
    for forbidden in ("shell", "forget", "set_reminder"):
        assert forbidden not in WORKER_TOOLS, f"{forbidden} must not run unattended"
    # And every tool it is granted must actually exist.
    for name in WORKER_TOOLS:
        assert name in tools.tools, f"WORKER_TOOLS names a missing tool: {name}"


def test_tool_surface_can_be_restricted(tools):
    restricted = tools.specs(names=["get_time", "recall"], include_server_tools=False)
    assert {spec["name"] for spec in restricted} == {"get_time", "recall"}


def test_email_sweep_queues_a_draft_for_mail_needing_a_reply(memory, tools):
    """Priority mail that wants an answer becomes background work."""

    class StubJarvis:
        def __init__(self):
            self.memory = memory
            self.tools = tools
            self.busy = asyncio.Lock()
            self.online = True
            self.client = object()
            self.spoken: list[str] = []

        async def speak_unprompted(self, text, reason="cognition"):
            self.spoken.append(text)

    class StubTriage:
        enabled = True

        async def sweep(self):
            return [
                {
                    "id": 1, "uid": 10, "sender": "Ada", "sender_email": "ada@x.com",
                    "subject": "Contract", "summary": "needs signature today",
                    "priority": 4, "priority_label": "critical", "needs_reply": True,
                },
                {
                    "id": 2, "uid": 11, "sender": "Shop", "sender_email": "s@x.com",
                    "subject": "Sale", "summary": "marketing",
                    "priority": 0, "priority_label": "noise", "needs_reply": False,
                },
            ]

    jarvis = StubJarvis()
    autonomy = Autonomy(jarvis, StubTriage())
    asyncio.run(autonomy.run_email_sweep())

    tasks = asyncio.run(memory.list_tasks())
    titles = [t["title"] for t in tasks]
    assert any("Ada" in t for t in titles), "critical mail needing a reply must queue work"
    assert not any("Shop" in t for t in titles), "noise must not generate work"
    # Critical mail is worth interrupting for.
    assert jarvis.spoken and "Ada" in jarvis.spoken[0]
    # And the queued brief must tell the background run not to send.
    ada_task = next(t for t in tasks if "Ada" in t["title"])
    assert "not send" in ada_task["detail"].lower()
