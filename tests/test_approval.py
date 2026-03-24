import asyncio

import pytest

from claude_slack_bridge.approval import ApprovalManager, ApprovalState


@pytest.fixture
def manager() -> ApprovalManager:
    return ApprovalManager()


async def test_create_and_resolve_approval(manager: ApprovalManager) -> None:
    state = manager.create("req-1")
    assert state.status == "pending"

    manager.resolve("req-1", "approved")
    result = await asyncio.wait_for(state.wait(), timeout=1.0)
    assert result == "approved"
    assert state.status == "approved"


async def test_resolve_rejected(manager: ApprovalManager) -> None:
    state = manager.create("req-1")
    manager.resolve("req-1", "rejected")
    result = await asyncio.wait_for(state.wait(), timeout=1.0)
    assert result == "rejected"


async def test_wait_timeout_returns_timed_out(manager: ApprovalManager) -> None:
    state = manager.create("req-1")
    result = await state.wait(timeout=0.05)
    assert result == "timed_out"
    assert state.status == "timed_out"


async def test_double_resolve_is_noop(manager: ApprovalManager) -> None:
    state = manager.create("req-1")
    manager.resolve("req-1", "approved")
    manager.resolve("req-1", "rejected")  # should be ignored
    result = await asyncio.wait_for(state.wait(), timeout=1.0)
    assert result == "approved"


async def test_resolve_unknown_request(manager: ApprovalManager) -> None:
    # Should not raise
    manager.resolve("nonexistent", "approved")


async def test_cleanup_removes_resolved(manager: ApprovalManager) -> None:
    state = manager.create("req-1")
    manager.resolve("req-1", "approved")
    await asyncio.wait_for(state.wait(), timeout=1.0)
    manager.cleanup("req-1")
    assert manager.get("req-1") is None


def test_get_pending(manager: ApprovalManager) -> None:
    state = manager.create("req-1")
    assert manager.get("req-1") is state
    assert manager.get("req-2") is None
