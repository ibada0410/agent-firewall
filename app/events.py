"""In-process event bus feeding the browser's live stream (Server-Sent Events)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator


class EventBus:
    def __init__(self, history: int = 200):
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._history: list[dict[str, Any]] = []
        self._limit = history
        self._seq = 0

    def publish(self, event_type: str, **data: Any) -> None:
        self._seq += 1
        event = {"id": self._seq, "type": event_type, "ts": time.time(), **data}
        self._history.append(event)
        del self._history[: -self._limit]
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a stalled browser tab must not slow the firewall down

    async def stream(self, last_id: int = 0) -> AsyncIterator[str]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        try:
            for event in self._history:
                if event["id"] > last_id:
                    yield self._format(event)
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield self._format(event)
        finally:
            self._subscribers.discard(q)

    @staticmethod
    def _format(event: dict[str, Any]) -> str:
        return f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"
