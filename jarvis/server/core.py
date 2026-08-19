"""The conversational core.

One turn looks like this:

    user text -> recall relevant memory -> stream a response from Claude
              -> execute any tools it asks for -> stream again
              -> repeat until it stops asking -> persist the episode

The streaming loop itself lives in agent.py, shared with the autonomous
background worker. This module owns conversation state, memory context,
and the cognitive telemetry the HUD renders.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .agent import CAPABILITIES, Agent
from .config import CONFIG
from .events import BUS
from .memory import Memory
from .persona import system_prompt
from .tools import ToolRegistry


class Jarvis:
    """Owns conversation state and drives the model."""

    def __init__(self, memory: Memory, tools: ToolRegistry) -> None:
        self.memory = memory
        self.tools = tools
        self.messages: list[dict[str, Any]] = []
        self.state = "idle"
        self.busy = asyncio.Lock()
        self.last_user_activity = time.time()
        self.last_proactive = 0.0
        self.turn_count = 0
        self.total_tokens = {"input": 0, "output": 0, "cache_read": 0}
        self.client = self._make_client()

    # -- setup -----------------------------------------------------------

    def _make_client(self):
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            BUS.emit(
                "error",
                message="anthropic SDK not installed — running in offline mode.",
            )
            return None
        if not CONFIG.has_credentials:
            BUS.emit(
                "error",
                message=(
                    "No Anthropic credentials found. Set ANTHROPIC_API_KEY or run"
                    " `ant auth login`. Running in offline mode."
                ),
            )
            return None
        try:
            # Long turns are normal at high effort; the per-request timeout
            # has to allow for minutes of thinking plus tool rounds.
            return AsyncAnthropic(timeout=900.0, max_retries=3)
        except Exception as exc:  # noqa: BLE001 - surface, don't crash the server
            BUS.emit("error", message=f"Could not create Anthropic client: {exc}")
            return None

    @property
    def online(self) -> bool:
        return self.client is not None

    @property
    def mid_conversation_system(self) -> bool:
        return CAPABILITIES["mid_conversation_system"]

    # -- state -----------------------------------------------------------

    def set_state(self, state: str, intensity: float = 0.5, **extra: Any) -> None:
        self.state = state
        BUS.emit("state", state=state, intensity=round(intensity, 3), **extra)

    # -- context assembly ------------------------------------------------

    async def _context_block(self, user_text: str) -> str | None:
        """Retrieved memory + live status, injected as an operator message.

        This rides in `messages` rather than the system prompt so the cached
        system prefix stays byte-identical across the whole session.
        """
        parts: list[str] = []

        hits = await self.memory.recall(user_text, CONFIG.recall_results)
        if hits:
            lines = "\n".join(f"  - {h.render()}" for h in hits)
            parts.append(f"Relevant memory:\n{lines}")
            BUS.emit("memory", op="recall", count=len(hits), detail=hits[0].text[:90])

        goals = await self.memory.list_goals("open")
        if goals:
            lines = "\n".join(
                f'  - #{g["id"]} [{int(g["progress"] * 100)}%] {g["title"]}'
                for g in goals[:6]
            )
            parts.append(f"Open goals:\n{lines}")

        observations = await self.memory.recent_observations(4)
        if observations:
            lines = "\n".join(f'  - {o["text"]}' for o in observations)
            parts.append(f"Recent notes from your background cognition:\n{lines}")

        urgent = await self.memory.urgent_emails(4)
        if urgent:
            lines = "\n".join(
                f'  - [{e["priority_label"]}] {e["sender"]}: {e["subject"]}'
                f' — {e["summary"][:110]}'
                for e in urgent
            )
            parts.append(f"Unhandled priority mail:\n{lines}")

        ventures = await self.memory.list_ventures("active")
        if ventures:
            lines = "\n".join(
                f'  - #{v["id"]} {v["title"]} — next: {v["next_step"][:90]}'
                for v in ventures[:4]
            )
            parts.append(f"Live ventures you are tracking:\n{lines}")

        if not parts:
            return None
        return (
            "Context assembled for this turn. Use it silently — do not read it "
            "back to the user or mention that you were given it.\n\n"
            + "\n\n".join(parts)
        )

    def _trim_history(self) -> None:
        """Keep the tail of the conversation, never splitting a tool exchange.

        Dropping an assistant turn that contains tool_use blocks while keeping
        its tool_result reply is a 400, so the trim point is walked forward to
        the next clean user turn.
        """
        limit = CONFIG.history_turns
        if len(self.messages) <= limit:
            return
        cut = len(self.messages) - limit
        while cut < len(self.messages):
            msg = self.messages[cut]
            content = msg.get("content")
            has_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
            if msg["role"] == "user" and not has_tool_result:
                break
            cut += 1
        if cut < len(self.messages):
            del self.messages[:cut]

    # -- the main turn ---------------------------------------------------

    async def respond(self, user_text: str, *, spoken: bool = False) -> str:
        """Handle one user turn end to end. Returns the final reply text."""
        async with self.busy:
            self.last_user_activity = time.time()
            self.turn_count += 1
            started = time.time()

            BUS.emit("user_message", text=user_text, spoken=spoken)

            if not self.online:
                await self.memory.add_episode("user", user_text)
                return await self._offline_reply(user_text)

            self.set_state("thinking", 0.35)

            # Retrieve before storing, so the turn doesn't recall its own
            # utterance back to itself as "relevant memory".
            context = await self._context_block(user_text)
            await self.memory.add_episode("user", user_text)

            if context and self.mid_conversation_system:
                self.messages.append({"role": "user", "content": user_text})
                # Operator-authority context, placed after the cached history.
                self.messages.append({"role": "system", "content": context})
            elif context:
                self.messages.append(
                    {"role": "user", "content": f"{context}\n\n---\n\n{user_text}"}
                )
            else:
                self.messages.append({"role": "user", "content": user_text})

            try:
                self._trim_history()
                agent = Agent(
                    self.client,
                    self.tools,
                    model=CONFIG.model,
                    effort=CONFIG.effort,
                    max_tokens=CONFIG.max_tokens,
                    system=system_prompt(),
                    channel="main",
                    on_state=self.set_state,
                )
                result = await agent.run(self.messages)
                reply = result.text
                for key in self.total_tokens:
                    self.total_tokens[key] += agent.totals[key]
                if result.refused:
                    self.set_state("alert", 0.9)
            except Exception as exc:  # noqa: BLE001 - keep the server alive
                BUS.emit("error", message=f"{type(exc).__name__}: {exc}")
                self.set_state("alert", 1.0)
                reply = (
                    "I hit an error reaching my reasoning core. "
                    f"{type(exc).__name__}: {exc}"
                )
                await asyncio.sleep(0.6)

            elapsed = time.time() - started
            if reply:
                await self.memory.add_episode("assistant", reply, importance=0.5)
            BUS.emit(
                "turn_end",
                text=reply,
                ms=int(elapsed * 1000),
                tokens=dict(self.total_tokens),
                turn=self.turn_count,
            )
            self.set_state("idle", 0.12)
            self.last_user_activity = time.time()
            return reply

    # -- offline mode ----------------------------------------------------

    async def _offline_reply(self, user_text: str) -> str:
        """Keep the HUD alive and honest when there are no credentials."""
        self.set_state("thinking", 0.7)
        for fragment in (
            "No reasoning core attached. ",
            "Running the interface on local telemetry only. ",
        ):
            BUS.emit("thinking_token", text=fragment)
            await asyncio.sleep(0.25)

        self.set_state("speaking", 0.55)
        reply = (
            "I'm running without a reasoning core — no Anthropic credentials were "
            "found. Set ANTHROPIC_API_KEY in the environment or in jarvis/.env, "
            "then restart me. Memory, tools and the interface are all live; only "
            "the thinking is missing."
        )
        for word in reply.split(" "):
            BUS.emit("token", text=word + " ")
            await asyncio.sleep(0.03)
        await self.memory.add_episode("assistant", reply)
        BUS.emit(
            "turn_end", text=reply, ms=0, tokens=dict(self.total_tokens),
            turn=self.turn_count,
        )
        self.set_state("idle", 0.1)
        return reply

    # -- proactive speech ------------------------------------------------

    async def speak_unprompted(self, text: str, *, reason: str = "cognition") -> None:
        """Say something the user didn't ask for (reminder, briefing, alert)."""
        self.last_proactive = time.time()
        await self.memory.add_episode("assistant", text, importance=0.6)
        # Keep the conversation coherent: the model should see what it said.
        self.messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": text}]}
        )
        self.set_state("speaking", 0.7)
        BUS.emit("proactive", text=text, reason=reason)
        await asyncio.sleep(0.1)
        self.set_state("idle", 0.12)

    # -- introspection ---------------------------------------------------

    async def status(self) -> dict[str, Any]:
        from .tools import collect_system_status

        return {
            "host": await asyncio.to_thread(collect_system_status),
            "online": self.online,
            "model": CONFIG.model if self.online else "offline",
            "effort": CONFIG.effort,
            "state": self.state,
            "turns": self.turn_count,
            "tokens": dict(self.total_tokens),
            "safe_mode": CONFIG.safe_mode,
            "web": CONFIG.enable_web,
            "tools": sorted(self.tools.tools),
            "memory": await self.memory.stats(),
            "idle_seconds": round(time.time() - self.last_user_activity, 1),
        }
