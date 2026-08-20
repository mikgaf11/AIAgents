"""Run JARVIS on a local model, for free.

Ollama speaks a different protocol to the Anthropic API, so rather than
teaching the agent loop about two shapes, this presents the *same* surface
the loop already uses:

    async with client.messages.stream(**params) as stream:
        async for event in stream: ...
        message = await stream.get_final_message()

Everything above it — tool orchestration, telemetry, the brain
visualization — is unchanged. Only the transport differs.

Reasoning models that emit <think> blocks (deepseek-r1, qwen3 and friends)
have that text routed to thinking events, so the HUD's deep-layer activity
works locally too.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

# Anthropic-shaped stand-ins. The agent loop only ever reads these
# attributes, so matching the shape is enough.


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ThinkingBlock:
    thinking: str
    type: str = "thinking"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class Message:
    content: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"
    stop_details: Any = None
    usage: Usage = field(default_factory=Usage)
    model: str = "local"


@dataclass
class Delta:
    type: str
    text: str = ""
    thinking: str = ""
    partial_json: str = ""


@dataclass
class Event:
    type: str
    delta: Delta | None = None
    content_block: Any = None
    usage: Usage | None = None


THINK_OPEN = re.compile(r"<think(?:ing)?>", re.I)
THINK_CLOSE = re.compile(r"</think(?:ing)?>", re.I)


class OllamaError(RuntimeError):
    """Something the user can fix: Ollama not running, model not pulled."""


def _to_ollama_messages(system, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic message blocks into Ollama's flat chat format."""
    out: list[dict[str, Any]] = []

    if system:
        text = system if isinstance(system, str) else "\n\n".join(
            block.get("text", "") for block in system if isinstance(block, dict)
        )
        if text.strip():
            out.append({"role": "system", "content": text})

    for message in messages:
        role = message.get("role", "user")
        content = message.get("content")

        if isinstance(content, str):
            # Mid-conversation system turns are an Anthropic feature; local
            # models take them as ordinary system messages.
            out.append({"role": "system" if role == "system" else role,
                        "content": content})
            continue

        if not isinstance(content, list):
            continue

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for block in content:
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", "")
            get = (lambda k, d=None: block.get(k, d)) if isinstance(block, dict) \
                else (lambda k, d=None: getattr(block, k, d))

            if btype == "text":
                text_parts.append(get("text", "") or "")
            elif btype == "thinking":
                continue  # local models don't replay their own reasoning
            elif btype == "tool_use":
                tool_calls.append({
                    "function": {
                        "name": get("name", ""),
                        "arguments": get("input", {}) or {},
                    }
                })
            elif btype == "tool_result":
                body = get("content", "")
                if isinstance(body, list):
                    body = " ".join(
                        b.get("text", "") for b in body if isinstance(b, dict)
                    )
                tool_results.append({
                    "role": "tool",
                    "content": str(body)[:20000],
                    "name": get("tool_use_id", "tool"),
                })

        if tool_results:
            out.extend(tool_results)
        if text_parts or tool_calls:
            entry: dict[str, Any] = {
                "role": "system" if role == "system" else role,
                "content": "\n".join(p for p in text_parts if p),
            }
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)

    return out


def _to_ollama_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert tool specs, dropping Anthropic server-side tools.

    web_search and web_fetch run on Anthropic's infrastructure and have no
    local equivalent, so they are silently unavailable rather than being
    offered and then failing.
    """
    converted = []
    for tool in tools or []:
        if "input_schema" not in tool:
            continue  # a server-side tool; nothing to run locally
        converted.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        })
    return converted


class _Stream:
    """Async context manager mirroring the Anthropic streaming helper."""

    def __init__(self, client: "OllamaClient", params: dict[str, Any]) -> None:
        self._client = client
        self._params = params
        self._final: Message | None = None
        self._response = None
        self._ctx = None

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *exc) -> bool:
        if self._ctx is not None:
            await self._ctx.__aexit__(*exc)
        return False

    async def __aiter__(self):
        params = self._params
        body = {
            "model": self._client.model_for(params.get("model")),
            "messages": _to_ollama_messages(params.get("system"), params["messages"]),
            "stream": True,
            "options": {
                "num_predict": min(int(params.get("max_tokens", 4096)), 8192),
                "temperature": 0.6,
            },
        }
        tools = _to_ollama_tools(params.get("tools"))
        if tools:
            body["tools"] = tools

        text_parts: list[str] = []
        think_parts: list[str] = []
        tool_calls: list[ToolUseBlock] = []
        in_think = False
        started_text = False
        started_think = False
        prompt_tokens = 0
        completion_tokens = 0

        context = self._client.http.stream(
            "POST", "/api/chat", json=body, timeout=httpx.Timeout(600.0, connect=10.0)
        )
        try:
            self._response = await context.__aenter__()
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise OllamaError(
                f"Cannot reach Ollama at {self._client.base_url}. "
                "Install it from https://ollama.com and make sure it is running."
            ) from exc
        # Only adopt the context once entering it succeeded. Calling __aexit__
        # on an @asynccontextmanager whose __aenter__ raised produces a
        # "generator didn't stop after athrow()" RuntimeError that buries the
        # real cause — which is the message the user actually needs.
        self._ctx = context

        if self._response.status_code == 404:
            raise OllamaError(
                f"Model '{body['model']}' is not installed. "
                f"Run:  ollama pull {body['model']}"
            )
        if self._response.status_code >= 400:
            detail = (await self._response.aread()).decode("utf-8", "replace")[:300]
            raise OllamaError(f"Ollama returned {self._response.status_code}: {detail}")

        async for line in self._response.aiter_lines():
            if not line.strip():
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue

            if chunk.get("error"):
                raise OllamaError(str(chunk["error"]))

            message = chunk.get("message") or {}

            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                block = ToolUseBlock(
                    id=f"toolu_local_{len(tool_calls)}",
                    name=function.get("name", ""),
                    input=arguments or {},
                )
                tool_calls.append(block)
                yield Event("content_block_start", content_block=block)

            piece = message.get("content") or ""
            if piece:
                # Split reasoning out of the visible answer.
                while piece:
                    if not in_think:
                        match = THINK_OPEN.search(piece)
                        if match:
                            before, piece = piece[: match.start()], piece[match.end():]
                            if before:
                                if not started_text:
                                    started_text = True
                                    yield Event("content_block_start",
                                                content_block=TextBlock(""))
                                text_parts.append(before)
                                yield Event("content_block_delta",
                                            delta=Delta("text_delta", text=before))
                            in_think = True
                            if not started_think:
                                started_think = True
                                yield Event("content_block_start",
                                            content_block=ThinkingBlock(""))
                            continue
                        if not started_text:
                            started_text = True
                            yield Event("content_block_start",
                                        content_block=TextBlock(""))
                        text_parts.append(piece)
                        yield Event("content_block_delta",
                                    delta=Delta("text_delta", text=piece))
                        piece = ""
                    else:
                        match = THINK_CLOSE.search(piece)
                        if match:
                            inside, piece = piece[: match.start()], piece[match.end():]
                            if inside:
                                think_parts.append(inside)
                                yield Event("content_block_delta",
                                            delta=Delta("thinking_delta", thinking=inside))
                            in_think = False
                            continue
                        think_parts.append(piece)
                        yield Event("content_block_delta",
                                    delta=Delta("thinking_delta", thinking=piece))
                        piece = ""

            if chunk.get("done"):
                prompt_tokens = chunk.get("prompt_eval_count", 0) or 0
                completion_tokens = chunk.get("eval_count", 0) or 0
                yield Event("message_delta",
                            usage=Usage(output_tokens=completion_tokens))

        blocks: list[Any] = []
        if think_parts:
            blocks.append(ThinkingBlock("".join(think_parts)))
        text = "".join(text_parts).strip()
        if text:
            blocks.append(TextBlock(text))
        blocks.extend(tool_calls)

        self._final = Message(
            content=blocks,
            stop_reason="tool_use" if tool_calls else "end_turn",
            usage=Usage(
                input_tokens=prompt_tokens, output_tokens=completion_tokens
            ),
            model=body["model"],
        )

    async def get_final_message(self) -> Message:
        if self._final is None:
            # Nothing was streamed — surface it as an empty turn rather than
            # letting the caller dereference None.
            self._final = Message(content=[TextBlock("")], stop_reason="end_turn")
        return self._final


class _Messages:
    def __init__(self, client: "OllamaClient") -> None:
        self._client = client

    def stream(self, **params: Any) -> _Stream:
        return _Stream(self._client, params)

    async def create(self, **params: Any) -> Message:
        """Non-streaming call, used by triage and the reflection loop."""
        stream = _Stream(self._client, params)
        async with stream:
            async for _ in stream:
                pass
            return await stream.get_final_message()


class OllamaClient:
    """A drop-in stand-in for AsyncAnthropic, backed by a local model."""

    is_local = True

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "llama3.1:8b",
        background_model: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.background_model = background_model or model
        self.http = httpx.AsyncClient(base_url=self.base_url)
        self.messages = _Messages(self)

    def model_for(self, requested: str | None) -> str:
        """Map Anthropic model ids onto the configured local models."""
        if requested and not requested.startswith("claude"):
            return requested
        # Anything asking for a cheaper model gets the background model.
        if requested and ("haiku" in requested or "sonnet" in requested):
            return self.background_model
        return self.model

    async def probe(self) -> dict[str, Any]:
        """Check Ollama is up and the model is installed."""
        try:
            response = await self.http.get("/api/tags", timeout=5.0)
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            return {
                "ok": False,
                "error": f"Ollama not reachable at {self.base_url} ({exc}). "
                         "Install from https://ollama.com and start it.",
            }
        installed = [m.get("name", "") for m in response.json().get("models", [])]
        missing = [
            name for name in {self.model, self.background_model}
            if not any(i == name or i.startswith(name.split(":")[0]) for i in installed)
        ]
        if missing:
            return {
                "ok": False,
                "installed": installed,
                "error": f"Model(s) not installed: {', '.join(missing)}. "
                         f"Run:  ollama pull {missing[0]}",
            }
        return {"ok": True, "installed": installed, "model": self.model}

    async def aclose(self) -> None:
        await self.http.aclose()
