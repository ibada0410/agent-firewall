"""Held calls wait here until a reviewer decides, or the timeout blocks them."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

DECISIONS = {"allow_once", "block", "always_allow", "always_block"}


@dataclass
class Decision:
    decision: str        # one of DECISIONS, or "timeout"
    reviewer: str | None

    @property
    def allowed(self) -> bool:
        return self.decision in ("allow_once", "always_allow")


class ReviewBroker:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[Decision]] = {}

    def is_pending(self, call_id: str) -> bool:
        fut = self._pending.get(call_id)
        return fut is not None and not fut.done()

    def pending_ids(self) -> list[str]:
        return [k for k, f in self._pending.items() if not f.done()]

    async def wait(self, call_id: str, timeout: float | None) -> Decision:
        fut: asyncio.Future[Decision] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return Decision("timeout", None)
        finally:
            self._pending.pop(call_id, None)

    def decide(self, call_id: str, decision: str, reviewer: str) -> bool:
        if decision not in DECISIONS:
            raise ValueError(f"Unknown decision: {decision}")
        fut = self._pending.get(call_id)
        if fut is None or fut.done():
            return False
        fut.set_result(Decision(decision, reviewer))
        return True

    def cancel_all(self) -> None:
        for call_id in list(self._pending):
            self.decide(call_id, "block", "system")
