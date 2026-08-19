"""The reusable streaming tool loop.

Both the conversational core and the autonomous background worker drive
Claude the same way: stream a turn, execute whatever tools it asks for,
feed the results back, repeat until it stops asking. The only differences
are the system prompt, the tool surface, and which HUD channel the
telemetry lands on — so all of that is parameterised here.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .events import BUS

# Whether this model accepts mid-conversation `role: "system"` messages.
# It's a property of the model, not of any one conversation, so both agents
# share the discovery and neither has to rediscover it.
CAPABILITIES = {"mid_conversation_system": True}

MAX_TOOL_ROUNDS = 24


@dataclass
class AgentResult:
    text: str = ""
    stop_reason: str = "end_turn"
    rounds: int = 0
    tool_calls: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    refused: bool = False


class Agent:
    """One configured way of talking to Claude, with tools."""

    def __init__(
        self,
        client,
        registry,
        *,
        model: str,
        effort: str,
        max_tokens: int,
        system: list[dict[str, Any]] | str,
        channel: str = "main",
        tool_names: list[str] | None = None,
        include_server_tools: bool = True,
        thinking: bool = True,
        on_state=None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.system = system
        self.channel = channel
        self.tool_names = tool_names
        self.include_server_tools = include_server_tools
        self.thinking = thinking
        self.on_state = on_state
        self.totals = {"input": 0, "output": 0, "cache_read": 0}

    # -- helpers ---------------------------------------------------------

    def _emit(self, kind: str, **payload: Any) -> None:
        BUS.emit(kind, channel=self.channel, **payload)

    def _state(self, state: str, intensity: float, **extra: Any) -> None:
        if self.on_state:
            self.on_state(state, intensity, **extra)

    def _tools(self) -> list[dict[str, Any]]:
        specs = self.registry.specs(
            names=self.tool_names, include_server_tools=self.include_server_tools
        )
        return specs

    # -- the loop --------------------------------------------------------

    async def run(self, messages: list[dict[str, Any]]) -> AgentResult:
        """Drive a turn to completion, appending to `messages` as it goes."""
        result = AgentResult()

        for round_index in range(MAX_TOOL_ROUNDS):
            result.rounds = round_index + 1
            message = await self._stream_once(messages)
            messages.append({"role": "assistant", "content": message.content})

            text = "".join(
                block.text
                for block in message.content
                if getattr(block, "type", None) == "text"
            ).strip()
            if text:
                result.text = text

            stop = message.stop_reason
            result.stop_reason = stop or "end_turn"

            if stop == "refusal":
                details = getattr(message, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                self._emit("error", message=f"Refusal ({category or 'unspecified'})")
                result.refused = True
                result.text = (
                    "That one trips my safety classifiers, so I'm declining it"
                    f"{f' (category: {category})' if category else ''}."
                )
                return result

            if stop == "pause_turn":
                # A server-side tool hit its iteration limit; re-sending with
                # the assistant turn appended lets the server resume.
                self._emit("tool_pause", round=round_index)
                continue

            if stop == "tool_use":
                names = await self._execute_tools(message, messages)
                result.tool_calls.extend(names)
                continue

            return result

        self._emit("error", message=f"Stopped after {MAX_TOOL_ROUNDS} tool rounds.")
        return result

    async def _stream_once(self, messages: list[dict[str, Any]]):
        try:
            return await self._stream_call(messages)
        except Exception as exc:  # noqa: BLE001 - inspected, then re-raised
            if not _downgrade_system_messages(exc, messages, self.channel):
                raise
            return await self._stream_call(messages)

    async def _stream_call(self, messages: list[dict[str, Any]]):
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "messages": messages,
            "tools": self._tools(),
            "output_config": {"effort": self.effort},
        }
        if self.thinking:
            # Summarized thinking is what the brain visualization renders as
            # deep-layer activity; omitted (the default) would show nothing.
            params["thinking"] = {"type": "adaptive", "display": "summarized"}

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
                        self._state("thinking", 0.85)
                    elif btype == "text":
                        self._state("speaking", 0.6)
                    elif btype in ("tool_use", "server_tool_use"):
                        self._state("tool", 0.75, tool=getattr(block, "name", "tool"))

                elif etype == "content_block_delta":
                    delta = event.delta
                    dtype = getattr(delta, "type", "")
                    if dtype == "thinking_delta":
                        first_token_at = first_token_at or time.time()
                        self._emit("thinking_token", text=delta.thinking)
                    elif dtype == "text_delta":
                        first_token_at = first_token_at or time.time()
                        self._emit("token", text=delta.text)
                    elif dtype == "input_json_delta":
                        self._emit("tool_args", text=delta.partial_json)

                elif etype == "message_delta":
                    usage = getattr(event, "usage", None)
                    if usage is not None and getattr(usage, "output_tokens", None):
                        output_tokens = usage.output_tokens

            message = await stream.get_final_message()

        elapsed = max(1e-3, time.time() - started)
        usage = getattr(message, "usage", None)
        if usage is not None:
            self.totals["input"] += getattr(usage, "input_tokens", 0) or 0
            self.totals["output"] += getattr(usage, "output_tokens", 0) or 0
            self.totals["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", output_tokens) or output_tokens

        self._emit(
            "usage",
            output_tokens=output_tokens,
            tokens_per_second=round(output_tokens / elapsed, 1),
            ttft_ms=int(((first_token_at or time.time()) - started) * 1000),
            latency_ms=int(elapsed * 1000),
            totals=dict(self.totals),
        )
        return message

    async def _execute_tools(self, message, messages: list[dict[str, Any]]) -> list[str]:
        """Run every local tool_use block in parallel and reply with results."""
        import asyncio

        calls = [
            block
            for block in message.content
            if getattr(block, "type", None) == "tool_use"
        ]
        if not calls:
            return []

        async def run_one(block) -> tuple[str, dict[str, Any]]:
            name = block.name
            args = block.input if isinstance(block.input, dict) else {}
            tool = self.registry.tools.get(name)
            started = time.time()
            self._emit(
                "tool_start",
                name=name,
                input=_preview(args),
                dangerous=bool(tool and tool.dangerous),
                id=block.id,
            )
            self._state("tool", 0.8, tool=name)
            output, is_error = await self.registry.execute(name, args)
            self._emit(
                "tool_end",
                name=name,
                ok=not is_error,
                ms=int((time.time() - started) * 1000),
                preview=output[:220],
                id=block.id,
            )
            if name in ("remember", "forget", "set_goal", "set_reminder"):
                self._emit("memory", op=name, detail=output[:120])
            return name, {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output[:60000] or "<empty>",
                "is_error": is_error,
            }

        pairs = await asyncio.gather(*(run_one(b) for b in calls))
        # All results go back in a single user message — splitting them
        # trains the model out of parallel tool calls.
        messages.append({"role": "user", "content": [payload for _, payload in pairs]})
        return [name for name, _ in pairs]


def _downgrade_system_messages(
    exc: Exception, messages: list[dict[str, Any]], channel: str
) -> bool:
    """Fold `role: "system"` turns into user turns after a model rejects them.

    Returns True when the history was rewritten and the call is worth
    retrying, False when the error was something else entirely.
    """
    if not CAPABILITIES["mid_conversation_system"]:
        return False
    text = str(exc).lower()
    if "system" not in text or "role" not in text:
        return False

    CAPABILITIES["mid_conversation_system"] = False
    BUS.emit(
        "error",
        channel=channel,
        message=(
            "This model rejects mid-conversation system messages;"
            " folding context into user turns instead."
        ),
    )
    folded: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "system":
            folded.append(message)
            continue
        content = message.get("content")
        body = content if isinstance(content, str) else json.dumps(content)
        if (
            folded
            and folded[-1].get("role") == "user"
            and isinstance(folded[-1].get("content"), str)
        ):
            folded[-1] = {
                "role": "user",
                "content": f"{body}\n\n---\n\n{folded[-1]['content']}",
            }
        else:
            folded.append({"role": "user", "content": body})
    messages[:] = folded
    return True


def _preview(value: Any, limit: int = 160) -> str:
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
