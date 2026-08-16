"""In-process pub/sub used to push cognitive telemetry to the HUD.

Every interesting thing JARVIS does — a token, a thought, a tool call, a
memory write, a state change — becomes an event. The websocket layer
subscribes and forwards; the brain visualization is driven entirely by
this stream.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator


class EventBus:
    """Fan-out broadcaster with bounded per-subscriber queues.

    A slow subscriber drops its oldest events rather than stalling the
    reasoning loop — telemetry is never allowed to backpressure cognition.
    """

    def __init__(self, queue_size: int = 512) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size
        self._history: list[dict[str, Any]] = []
        self._history_limit = 200

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def emit(self, kind: str, **payload: Any) -> dict[str, Any]:
        event = {"kind": kind, "t": time.time(), **payload}
        if kind not in ("token", "thinking_token", "audio"):
            self._history.append(event)
            if len(self._history) > self._history_limit:
                del self._history[: len(self._history) - self._history_limit]
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race only
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - race only
                pass
        return event

    def replay(self) -> list[dict[str, Any]]:
        """Recent non-token events, so a reconnecting HUD isn't blank."""
        return list(self._history)

    async def stream(
        self, queue: asyncio.Queue[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await queue.get()


BUS = EventBus()
