from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ApprovalState:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.status: str = "pending"  # pending | approved | rejected | timed_out
        self._event = asyncio.Event()
        self._resolved = False

    def resolve(self, decision: str) -> bool:
        """Atomically resolve. Returns True if this was the first resolution."""
        if self._resolved:
            return False
        self._resolved = True
        self.status = decision
        self._event.set()
        return True

    async def wait(self, timeout: float | None = None) -> str:
        """Wait for resolution. Returns status string."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.resolve("timed_out")
        return self.status


class ApprovalManager:
    def __init__(self) -> None:
        self._pending: dict[str, ApprovalState] = {}

    def create(self, request_id: str) -> ApprovalState:
        state = ApprovalState(request_id)
        self._pending[request_id] = state
        return state

    def resolve(self, request_id: str, decision: str) -> bool:
        state = self._pending.get(request_id)
        if state is None:
            logger.warning("No pending approval for request_id=%s", request_id)
            return False
        return state.resolve(decision)

    def get(self, request_id: str) -> ApprovalState | None:
        return self._pending.get(request_id)

    def cleanup(self, request_id: str) -> None:
        self._pending.pop(request_id, None)
