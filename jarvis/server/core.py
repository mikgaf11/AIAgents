"""The reasoning core: streaming, tool orchestration, cognitive telemetry.

One turn looks like this:

    user text -> recall relevant memory -> stream a response from Claude
              -> execute any tools it asks for -> stream again
              -> repeat until it stops asking -> persist the episode

Everything the loop does is announced on the event bus, which is what the
HUD's brain visualization consumes.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .config import CONFIG
from .events import BUS
from .memory import Memory
from .persona import system_prompt
from .tools import ToolRegistry

MAX_TOOL_ROUNDS = 24


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
        # Mid-conversation `role: "system"` messages keep the cached prefix
        # intact, but only some models accept them. Flipped off on the 400.
        self.mid_conversation_system = True
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
            BUS.emit("memory", op="recall", count=len(hits),
                     detail=hits[0].text[:90])

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
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
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
                reply = await self._run_tool_loop()
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

    async def _run_tool_loop(self) -> str:
        """Stream turns until the model stops asking for tools."""
        final_text = ""

        for round_index in range(MAX_TOOL_ROUNDS):
            self._trim_history()
            message = await self._stream_once()

            if message is None:
                return final_text or "My reasoning core returned nothing."

            self.messages.append({"role": "assistant", "content": message.content})

            text = "".join(
                block.text
                for block in message.content
                if getattr(block, "type", None) == "text"
            ).strip()
            if text:
                final_text = text

            stop = message.stop_reason

            if stop == "refusal":
                details = getattr(message, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                BUS.emit("error", message=f"Refusal ({category or 'unspecified'})")
                self.set_state("alert", 0.9)
                return (
                    "That one trips my safety classifiers, so I'm declining it"
                    f"{f' (category: {category})' if category else ''}."
                )

            if stop == "pause_turn":
                # A server-side tool hit its iteration limit. Re-send with the
                # assistant turn appended and the server resumes on its own.
                BUS.emit("tool_pause", round=round_index)
                continue

            if stop == "tool_use":
                await self._execute_tool_calls(message)
                continue

            return final_text

        BUS.emit("error", message=f"Stopped after {MAX_TOOL_ROUNDS} tool rounds.")
        return final_text or "I ran out of tool rounds before finishing that."

    async def _stream_once(self):
        """One streamed model call, with a one-shot fallback for models that
        reject mid-conversation system messages."""
        try:
            return await self._stream_call()
        except Exception as exc:  # noqa: BLE001 - inspected, then re-raised
            if not self._downgrade_system_messages(exc):
                raise
            return await self._stream_call()

    def _downgrade_system_messages(self, exc: Exception) -> bool:
        """Fold `role: "system"` turns into user turns after a model rejects them.

        Returns True when the history was rewritten and the call is worth
        retrying, False when the error was something else entirely.
        """
        if not self.mid_conversation_system:
            return False
        text = str(exc).lower()
        if "system" not in text or "role" not in text:
            return False

        self.mid_conversation_system = False
        BUS.emit(
            "error",
            message=(
                f"{CONFIG.model} rejects mid-conversation system messages;"
                " folding context into user turns instead."
            ),
        )
        folded: list[dict[str, Any]] = []
        for message in self.messages:
            if message.get("role") != "system":
                folded.append(message)
                continue
            content = message.get("content")
            body = content if isinstance(content, str) else json.dumps(content)
            if folded and folded[-1].get("role") == "user" and isinstance(
                folded[-1].get("content"), str
            ):
                folded[-1] = {
                    "role": "user",
                    "content": f"{body}\n\n---\n\n{folded[-1]['content']}",
                }
            else:
                folded.append({"role": "user", "content": body})
        self.messages = folded
        return True

    async def _stream_call(self):
        """One streamed model call, narrating tokens onto the event bus."""
        params: dict[str, Any] = {
            "model": CONFIG.model,
            "max_tokens": CONFIG.max_tokens,
            "system": system_prompt(),
            "messages": self.messages,
            "tools": self.tools.specs(),
            # Summarized thinking is what the brain visualization renders as
            # deep-layer activity; omitted (the default) would show nothing.
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": CONFIG.effort},
        }

        first_token_at: float | None = None
        output_tokens = 0
        started = time.time()

        async with self.client.messages.stream(**params) as stream:
            async for event in stream:
                etype = getattr(event, "type", "")

                if etype == "content_block_start":
                    block = event.content_block
                    btype = getattr(block, "type", "")
                    if btype == "thinking":
                        self.set_state("thinking", 0.85)
                    elif btype == "text":
                        self.set_state("speaking", 0.6)
                    elif btype in ("tool_use", "server_tool_use"):
                        name = getattr(block, "name", "tool")
                        self.set_state("tool", 0.75, tool=name)

                elif etype == "content_block_delta":
                    delta = event.delta
                    dtype = getattr(delta, "type", "")
                    if dtype == "thinking_delta":
                        if first_token_at is None:
                            first_token_at = time.time()
                        BUS.emit("thinking_token", text=delta.thinking)
                    elif dtype == "text_delta":
                        if first_token_at is None:
                            first_token_at = time.time()
                        BUS.emit("token", text=delta.text)
                    elif dtype == "input_json_delta":
                        # Tool arguments materializing — a nice HUD signal.
                        BUS.emit("tool_args", text=delta.partial_json)

                elif etype == "message_delta":
                    usage = getattr(event, "usage", None)
                    if usage is not None and getattr(usage, "output_tokens", None):
                        output_tokens = usage.output_tokens

            message = await stream.get_final_message()

        elapsed = max(1e-3, time.time() - started)
        usage = getattr(message, "usage", None)
        if usage is not None:
            self.total_tokens["input"] += getattr(usage, "input_tokens", 0) or 0
            self.total_tokens["output"] += getattr(usage, "output_tokens", 0) or 0
            self.total_tokens["cache_read"] += (
                getattr(usage, "cache_read_input_tokens", 0) or 0
            )
            output_tokens = getattr(usage, "output_tokens", output_tokens) or output_tokens

        BUS.emit(
            "usage",
            output_tokens=output_tokens,
            tokens_per_second=round(output_tokens / elapsed, 1),
            ttft_ms=int(((first_token_at or time.time()) - started) * 1000),
            latency_ms=int(elapsed * 1000),
            totals=dict(self.total_tokens),
        )
        return message

    async def _execute_tool_calls(self, message) -> None:
        """Run every local tool_use block, in parallel, and reply with results."""
        calls = [
            block
            for block in message.content
            if getattr(block, "type", None) == "tool_use"
        ]
        if not calls:
            return

        async def run_one(block) -> dict[str, Any]:
            name = block.name
            args = block.input if isinstance(block.input, dict) else {}
            tool = self.tools.tools.get(name)
            started = time.time()
            BUS.emit(
                "tool_start",
                name=name,
                input=_preview(args),
                dangerous=bool(tool and tool.dangerous),
                id=block.id,
            )
            self.set_state("tool", 0.8, tool=name)
            output, is_error = await self.tools.execute(name, args)
            BUS.emit(
                "tool_end",
                name=name,
                ok=not is_error,
                ms=int((time.time() - started) * 1000),
                preview=output[:220],
                id=block.id,
            )
            if name in ("remember", "forget", "set_goal", "set_reminder"):
                BUS.emit("memory", op=name, detail=output[:120])
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output[:60000] or "<empty>",
                "is_error": is_error,
            }

        results = await asyncio.gather(*(run_one(b) for b in calls))
        # All results go back in a single user message — splitting them
        # trains the model out of parallel tool calls.
        self.messages.append({"role": "user", "content": list(results)})

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
        BUS.emit("turn_end", text=reply, ms=0, tokens=dict(self.total_tokens),
                 turn=self.turn_count)
        self.set_state("idle", 0.1)
        return reply

    # -- proactive speech ------------------------------------------------

    async def speak_unprompted(self, text: str, *, reason: str = "cognition") -> None:
        """Say something the user didn't ask for (reminder, observation)."""
        self.last_proactive = time.time()
        await self.memory.add_episode("assistant", text, importance=0.6)
        # Keep the conversation coherent: the model should see what it said.
        self.messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            }
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


def _preview(value: Any, limit: int = 160) -> str:
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
