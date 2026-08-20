"""The background mind.

A loop that runs whether or not anyone is talking. Every tick it builds a
snapshot of the world — recent conversation, open goals, stored facts, its
own prior notes, host telemetry, idle time — hands it to a cheaper model,
and acts on the structured verdict that comes back: record observations,
promote facts, advance goals, and occasionally decide to speak first.

Reminders are handled here too, and they bypass the model entirely: a due
reminder is spoken regardless of what the reflection pass decides.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Any

from .config import CONFIG
from .core import Jarvis
from .events import BUS
from .persona import COGNITION_PROMPT
from .tools import collect_system_status

# Flavour text for the HUD's idle thought ticker. These are never spoken
# and never stored — they exist so the interface reads as alive between
# real reflection passes.
IDLE_MURMURS = [
    "indexing recent context",
    "no anomalies in host telemetry",
    "memory consolidation nominal",
    "goal queue steady",
    "listening",
    "background inference idle",
    "attention low, awareness on",
]


class Cognition:
    def __init__(self, jarvis: Jarvis, autonomy=None) -> None:
        self.jarvis = jarvis
        self.memory = jarvis.memory
        # Scheduled routines (mail, briefing, ventures, the work queue) ride
        # on the same heartbeat as reflection.
        self.autonomy = autonomy
        self.running = False
        self.ticks = 0
        self.reflections = 0
        self.mood = "calm"
        self._task: asyncio.Task | None = None
        # Reflection is the only part of a tick that costs money, so it is
        # gated on the world having actually changed.
        self._fingerprint: tuple | None = None
        self._last_reflection = 0.0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self.running = True
            self._task = asyncio.create_task(self._loop(), name="jarvis-cognition")

    async def stop(self) -> None:
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        # Let the server finish coming up before the first reflection.
        await asyncio.sleep(6.0)
        while self.running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the mind
                BUS.emit("error", message=f"Cognition tick failed: {exc}")
            jitter = random.uniform(-0.2, 0.2) * CONFIG.cognition_interval
            await asyncio.sleep(max(5.0, CONFIG.cognition_interval + jitter))

    # -- one pass --------------------------------------------------------

    async def tick(self) -> None:
        self.ticks += 1

        # Reminders fire on their own authority, model or no model.
        for reminder in await self.memory.due_reminders():
            await self.jarvis.speak_unprompted(
                f"Reminder: {reminder['text']}", reason="reminder"
            )
            return

        # Scheduled work (mail sweep, briefing, ventures, the task queue) runs
        # before reflection, so reflection sees the results of this tick.
        if self.autonomy is not None:
            await self.autonomy.tick()

        # Never think over the top of a live turn.
        if self.jarvis.busy.locked():
            return

        if not self.jarvis.online:
            BUS.emit("cognition", text=random.choice(IDLE_MURMURS), mood="calm",
                     tick=self.ticks)
            return

        # An assistant that runs at login reflects around the clock. Skip the
        # model call when nothing has changed since the last pass, so an idle
        # machine costs a few calls an hour instead of one every interval.
        fingerprint = await self._world_fingerprint()
        stale = time.time() - self._last_reflection >= CONFIG.deep_reflection_interval
        if fingerprint == self._fingerprint and not stale:
            BUS.emit(
                "cognition",
                text=random.choice(IDLE_MURMURS),
                mood=self.mood,
                tick=self.ticks,
                idle=True,
            )
            return
        self._fingerprint = fingerprint
        self._last_reflection = time.time()
        self.reflections += 1

        snapshot = await self._snapshot()
        verdict = await self._reflect(snapshot)
        if verdict is None:
            return

        self.mood = str(verdict.get("mood", self.mood))
        thought = str(verdict.get("thought", "")).strip()
        if thought:
            BUS.emit("cognition", text=thought, mood=self.mood, tick=self.ticks)

        for note in verdict.get("observations") or []:
            text = str(note).strip()
            if text:
                await self.memory.add_observation(text)
                BUS.emit("memory", op="observe", detail=text[:120])

        for fact in verdict.get("facts") or []:
            if not isinstance(fact, dict):
                continue
            subject = str(fact.get("subject", "")).strip()
            content = str(fact.get("content", "")).strip()
            if subject and content:
                await self.memory.add_fact(
                    subject,
                    content,
                    importance=float(fact.get("importance", 0.5) or 0.5),
                    source="cognition",
                )
                BUS.emit("memory", op="consolidate", detail=f"{subject}: {content}"[:120])

        for update in verdict.get("goal_updates") or []:
            if not isinstance(update, dict) or "goal_id" not in update:
                continue
            await self.memory.update_goal(
                int(update["goal_id"]),
                status=update.get("status"),
                progress=update.get("progress"),
                detail=update.get("detail"),
            )
            BUS.emit("memory", op="goal", detail=json.dumps(update)[:120])

        for task in verdict.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            title = str(task.get("title", "")).strip()
            if title:
                task_id = await self.memory.queue_task(
                    title,
                    str(task.get("detail", "")),
                    origin="cognition",
                    priority=int(task.get("priority", 3) or 3),
                )
                BUS.emit("work_queued", id=task_id, title=title, origin="cognition")

        speak = verdict.get("speak")
        if speak and self._may_speak():
            await self.jarvis.speak_unprompted(str(speak).strip(), reason="reflection")

    def _may_speak(self) -> bool:
        """Interrupting is a privilege — idle long enough, and not too often."""
        now = time.time()
        idle = now - self.jarvis.last_user_activity
        since_last = now - self.jarvis.last_proactive
        return (
            idle >= CONFIG.proactive_after_idle
            and since_last >= CONFIG.proactive_cooldown
            and not self.jarvis.busy.locked()
        )

    async def _world_fingerprint(self) -> tuple:
        """A cheap summary of everything reflection would look at.

        If this is unchanged, a fresh reflection pass would be handed an
        identical snapshot and produce nothing new.
        """
        episodes = await self.memory.recent_episodes(1)
        goals = await self.memory.list_goals("open")
        observations = await self.memory.recent_observations(1)
        reminders = await self.memory.list_reminders()
        stats = await self.memory.stats()
        return (
            episodes[-1]["id"] if episodes else 0,
            tuple(sorted((g["id"], round(g["progress"], 2), g["status"]) for g in goals)),
            observations[0]["id"] if observations else 0,
            len(reminders),
            # New mail and finished background work are both worth thinking about.
            stats.get("emails", 0),
            stats.get("emails_unhandled", 0),
            stats.get("tasks_pending", 0),
        )

    # -- snapshot & reflection -------------------------------------------

    async def _snapshot(self) -> str:
        episodes = await self.memory.recent_episodes(12)
        goals = await self.memory.list_goals("open")
        facts = await self.memory.all_facts(20)
        observations = await self.memory.recent_observations(8)
        reminders = await self.memory.list_reminders()
        status = await asyncio.to_thread(collect_system_status)
        idle = time.time() - self.jarvis.last_user_activity
        quiet_for = time.time() - self.jarvis.last_proactive

        def block(title: str, lines: list[str]) -> str:
            return f"{title}:\n" + ("\n".join(f"  {ln}" for ln in lines) or "  (none)")

        return "\n\n".join(
            [
                f"Idle: the user has not spoken for {idle:.0f} seconds.",
                f"You last spoke unprompted {quiet_for:.0f} seconds ago.",
                f"You are {'allowed' if self._may_speak() else 'NOT allowed'} to"
                " speak unprompted on this pass.",
                block(
                    "Recent conversation",
                    [f'{e["role"]}: {e["content"][:300]}' for e in episodes],
                ),
                block(
                    "Open goals",
                    [
                        f'#{g["id"]} p{g["priority"]} {int(g["progress"] * 100)}% '
                        f'{g["title"]} — {g["detail"][:120]}'
                        for g in goals
                    ],
                ),
                block(
                    "Stored facts",
                    [f'{f["subject"]}: {f["content"][:160]}' for f in facts],
                ),
                block("Your earlier observations", [o["text"][:200] for o in observations]),
                block(
                    "Pending reminders",
                    [f'in {(r["due"] - time.time()) / 60:.0f} min: {r["text"]}'
                     for r in reminders],
                ),
                block("Host telemetry", [f"{k}: {v}" for k, v in status.items()]),
            ]
        )

    async def _reflect(self, snapshot: str) -> dict[str, Any] | None:
        BUS.emit("state", state="dreaming", intensity=0.4)
        try:
            response = await self.jarvis.client.messages.create(
                model=self.jarvis.background_model,
                max_tokens=CONFIG.background_max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": COGNITION_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_config={"effort": CONFIG.background_effort},
                messages=[{"role": "user", "content": snapshot}],
            )
        except Exception as exc:  # noqa: BLE001 - reflection is best-effort
            BUS.emit("error", message=f"Reflection call failed: {exc}")
            return None
        finally:
            if self.jarvis.state in ("dreaming", "idle"):
                BUS.emit("state", state="idle", intensity=0.12)

        if getattr(response, "stop_reason", None) == "refusal":
            return None

        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
        return _parse_json_object(text)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of model output, tolerating stray prose/fences."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
