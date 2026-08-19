"""Working in the background, without being asked.

Three things run here:

  routines   scheduled jobs — sweep the mail, deliver a morning briefing,
             review the venture pipeline
  the queue  work JARVIS decided to do for itself, executed with the full
             tool loop so it can research, write files and report back
  reflection the cheap "notice things" pass, which decides what to queue

The rule throughout is that autonomous work is allowed to *think, research
and draft* freely, but anything that leaves the machine — sending mail,
above all — waits for the user to press a button.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from .agent import Agent
from .config import CONFIG
from .events import BUS
from .persona import BRIEFING_PROMPT, VENTURE_PROMPT, WORKER_PROMPT

# Tools the background worker may reach for. Deliberately excludes anything
# that talks to a person or changes the machine's state irreversibly.
WORKER_TOOLS = [
    "get_time", "recall", "remember", "list_goals", "update_goal",
    "read_file", "write_file", "list_dir", "search_files", "run_python",
    "system_status", "list_emails", "read_email", "draft_reply",
    "list_ventures", "propose_venture", "update_venture", "queue_task",
    "note_observation",
]


@dataclass
class Routine:
    name: str
    interval: float                      # seconds; 0 means "only at a wall time"
    run: Callable[[], Awaitable[None]]
    at_hour: int | None = None           # optional wall-clock trigger
    at_minute: int = 0
    last_run: float = 0.0
    enabled: bool = True

    def due(self, now: float) -> bool:
        if not self.enabled:
            return False
        if self.at_hour is not None:
            local = datetime.fromtimestamp(now)
            if local.hour != self.at_hour or local.minute < self.at_minute:
                return False
            # Once per day, at or just after the target minute.
            return (now - self.last_run) > 20 * 3600
        return self.interval > 0 and (now - self.last_run) >= self.interval


class Autonomy:
    """The scheduler and the worker."""

    def __init__(self, jarvis, triage) -> None:
        self.jarvis = jarvis
        self.memory = jarvis.memory
        self.triage = triage
        self.routines: list[Routine] = []
        self.working_on: str | None = None
        self.completed = 0
        self._build_routines()

    def _build_routines(self) -> None:
        self.routines = [
            Routine("email", CONFIG.email_poll_interval, self.run_email_sweep),
            Routine("briefing", 0, self.run_briefing,
                    at_hour=CONFIG.briefing_hour, at_minute=CONFIG.briefing_minute),
            Routine("ventures", CONFIG.venture_interval, self.run_venture_review),
            Routine("worker", 45.0, self.run_task_queue,
                    enabled=CONFIG.task_worker_enabled),
        ]

    # -- scheduling ------------------------------------------------------

    async def tick(self) -> None:
        """Run whatever routines are due. Called from the cognition loop."""
        now = time.time()
        for routine in self.routines:
            if not routine.due(now):
                continue
            routine.last_run = now
            try:
                await routine.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad routine must not stop the rest
                BUS.emit("error", message=f"Routine '{routine.name}' failed: {exc}")

    # -- routines --------------------------------------------------------

    async def run_email_sweep(self) -> None:
        if not self.triage.enabled:
            return
        records = await self.triage.sweep()
        if not records:
            return

        urgent = [r for r in records if r["priority"] >= CONFIG.email_announce_priority]
        important = [r for r in records if r["priority"] == 3]

        # Only critical mail is worth interrupting for.
        if urgent and not self.jarvis.busy.locked():
            if len(urgent) == 1:
                item = urgent[0]
                line = (
                    f"Something needs you: {item['sender']} — {item['subject']}. "
                    f"{item['summary']}"
                )
            else:
                line = (
                    f"{len(urgent)} messages need your attention, including "
                    f"{urgent[0]['sender']} about {urgent[0]['subject']}."
                )
            await self.jarvis.speak_unprompted(line, reason="email")

        # Anything needing a reply becomes queued work: draft it in advance.
        for record in urgent + important:
            if record.get("needs_reply"):
                await self.memory.queue_task(
                    f"Draft a reply to {record['sender']} re: {record['subject']}",
                    detail=(
                        f"Email #{record['id']} from {record['sender']} "
                        f"<{record['sender_email']}>. Summary: {record['summary']}. "
                        f"Read it, then draft a reply for approval. Do not send."
                    ),
                    origin="email",
                    priority=1 if record["priority"] >= 4 else 2,
                )

    async def run_briefing(self) -> None:
        """The morning digest: what happened overnight and what matters today."""
        if not self.jarvis.online:
            return

        emails = await self.memory.list_emails(limit=25, unhandled_only=True)
        goals = await self.memory.list_goals("open")
        ventures = await self.memory.list_ventures()
        tasks = await self.memory.list_tasks(10)
        reminders = await self.memory.list_reminders()

        def block(title: str, rows: list[str]) -> str:
            return f"{title}:\n" + ("\n".join(f"  {r}" for r in rows) or "  (none)")

        snapshot = "\n\n".join([
            block("Unhandled mail", [
                f'[{e["priority_label"]}] {e["sender"]} — {e["subject"]}: {e["summary"][:120]}'
                for e in emails[:15]
            ]),
            block("Open goals", [
                f'#{g["id"]} {g["title"]} ({int(g["progress"]*100)}%)' for g in goals
            ]),
            block("Ventures", [
                f'{v["title"]} [{v["status"]}] next: {v["next_step"][:90]}'
                for v in ventures[:6]
            ]),
            block("Background work", [
                f'{t["status"]}: {t["title"]}' for t in tasks[:6]
            ]),
            block("Reminders", [r["text"] for r in reminders[:6]]),
        ])

        text = await self._think(BRIEFING_PROMPT, snapshot, max_tokens=1400)
        if not text:
            return
        BUS.emit("briefing", text=text)
        await self.memory.add_observation(f"Morning briefing delivered: {text[:200]}")
        await self.jarvis.speak_unprompted(text, reason="briefing")

    async def run_venture_review(self) -> None:
        """Look for ways the user could make money, grounded in their situation."""
        if not self.jarvis.online:
            return

        facts = await self.memory.all_facts(40)
        goals = await self.memory.list_goals("open")
        existing = await self.memory.list_ventures()

        snapshot = "\n\n".join([
            "What is known about them:\n"
            + ("\n".join(f"  - {f['subject']}: {f['content'][:180]}" for f in facts)
               or "  (very little — say so)"),
            "Their stated goals:\n"
            + ("\n".join(f"  - {g['title']}" for g in goals) or "  (none)"),
            "Ventures already tracked (refine these rather than repeating them):\n"
            + ("\n".join(
                f'  - {v["title"]} [{v["status"]}, confidence {v["confidence"]:.1f}]'
                f' next: {v["next_step"][:90]}' for v in existing) or "  (none yet)"),
        ])

        agent = Agent(
            self.jarvis.client,
            self.jarvis.tools,
            model=CONFIG.model,
            effort="medium",
            max_tokens=8000,
            system=[{"type": "text", "text": VENTURE_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            channel="background",
            tool_names=["propose_venture", "update_venture", "list_ventures",
                        "recall", "note_observation"],
            include_server_tools=CONFIG.enable_web,
        )
        BUS.emit("work_start", title="Reviewing money-making opportunities",
                 origin="ventures")
        messages: list[dict[str, Any]] = [{"role": "user", "content": snapshot}]
        try:
            result = await agent.run(messages)
            BUS.emit("work_end", title="Venture review", ok=True,
                     summary=result.text[:300])
        except Exception as exc:  # noqa: BLE001
            BUS.emit("work_end", title="Venture review", ok=False, summary=str(exc))

    async def run_task_queue(self) -> None:
        """Execute one queued task, with tools, reporting progress to the HUD."""
        if not self.jarvis.online or self.working_on:
            return
        # Never compete with a live conversation for attention or rate limit.
        if self.jarvis.busy.locked():
            return

        await self.memory.requeue_stuck_tasks()
        task = await self.memory.next_task()
        if not task:
            return

        self.working_on = task["title"]
        BUS.emit("work_start", id=task["id"], title=task["title"],
                 origin=task["origin"], detail=task["detail"][:200])

        brief = (
            f"Task: {task['title']}\n\n"
            f"Detail: {task['detail'] or '(none given)'}\n\n"
            "Do the work now, then reply with a short report of what you did "
            "and what you found. If the task turns out not to be worth doing, "
            "say so plainly instead of inventing work."
        )
        agent = Agent(
            self.jarvis.client,
            self.jarvis.tools,
            model=CONFIG.model,
            effort="medium",
            max_tokens=12000,
            system=[{"type": "text", "text": WORKER_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            channel="background",
            tool_names=WORKER_TOOLS,
            include_server_tools=CONFIG.enable_web,
        )

        messages: list[dict[str, Any]] = [{"role": "user", "content": brief}]
        try:
            result = await agent.run(messages)
            summary = result.text or "(no report)"
            await self.memory.finish_task(task["id"], summary)
            self.completed += 1
            BUS.emit("work_end", id=task["id"], title=task["title"], ok=True,
                     summary=summary[:400], tools=result.tool_calls)
            await self.memory.add_observation(
                f"Completed background task '{task['title']}': {summary[:180]}"
            )
        except Exception as exc:  # noqa: BLE001 - record and move on
            await self.memory.finish_task(task["id"], f"{type(exc).__name__}: {exc}",
                                          status="failed")
            BUS.emit("work_end", id=task["id"], title=task["title"], ok=False,
                     summary=str(exc)[:300])
        finally:
            self.working_on = None

    # -- helpers ---------------------------------------------------------

    async def _think(self, system: str, content: str, *, max_tokens: int = 1200) -> str:
        """A single toolless call, for jobs that just need prose back."""
        try:
            response = await self.jarvis.client.messages.create(
                model=CONFIG.model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                output_config={"effort": "medium"},
                messages=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # noqa: BLE001
            BUS.emit("error", message=f"Background call failed: {exc}")
            return ""
        if getattr(response, "stop_reason", None) == "refusal":
            return ""
        return "".join(
            block.text for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()

    def status(self) -> dict[str, Any]:
        return {
            "working_on": self.working_on,
            "completed": self.completed,
            "routines": [
                {
                    "name": r.name,
                    "enabled": r.enabled,
                    "last_run": r.last_run,
                    "interval": r.interval,
                    "at_hour": r.at_hour,
                }
                for r in self.routines
            ],
        }
