"""Tests for tmux_controller send-keys routing.

Core invariant: `send_message_to_session` must only send to the exact
pane_id it was given. No cwd-based fallback — a second Claude TUI in
the same cwd must never receive a message meant for a session whose
pane has closed.
"""
from __future__ import annotations

import pytest

from claude_slack_bridge import tmux_controller


@pytest.fixture
def stub_tmux(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Capture every tmux send-keys invocation.

    Returns the shared log list; each entry is the full argv tuple passed
    to `_run`. Also stubs `find_tmux` / `pane_exists` so nothing touches
    the real system.
    """
    calls: list[tuple] = []

    async def fake_find_tmux() -> str:
        return "/usr/bin/tmux"

    async def fake_run(cmd: str, *args: str):
        calls.append((cmd, *args))
        return ""  # non-None → "success"

    monkeypatch.setattr(tmux_controller, "find_tmux", fake_find_tmux)
    monkeypatch.setattr(tmux_controller, "_run", fake_run)
    return calls


@pytest.fixture
def live_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    async def yes(pane_id: str) -> bool:
        return bool(pane_id)

    monkeypatch.setattr(tmux_controller, "pane_exists", yes)


@pytest.fixture
def dead_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no(pane_id: str) -> bool:
        return False

    monkeypatch.setattr(tmux_controller, "pane_exists", no)


async def test_sends_to_exact_pane_id(
    stub_tmux: list[tuple], live_pane: None
) -> None:
    assert await tmux_controller.send_message_to_session("hi", pane_id="%5")

    # Must include a send-keys -t %5 invocation, never any list-panes -a.
    targets = [c for c in stub_tmux if c[1] == "send-keys"]
    assert targets, stub_tmux
    assert all(c[c.index("-t") + 1] == "%5" for c in targets)
    assert not any("list-panes" in c for c in stub_tmux)


async def test_no_pane_id_does_not_send(
    stub_tmux: list[tuple], live_pane: None
) -> None:
    # The bug: empty pane_id used to fall back to cwd search, which
    # would pick any pane in the same cwd — potentially another
    # session's TUI. Now it must return False and send nothing.
    assert await tmux_controller.send_message_to_session("hi", pane_id="") is False
    assert stub_tmux == []


async def test_stale_pane_id_does_not_fall_back(
    stub_tmux: list[tuple], dead_pane: None
) -> None:
    # pane_id recorded, but the pane has since closed. Must NOT
    # fall back to listing panes by cwd — that's exactly how the
    # user's bug reproduces (old pane gone, new Claude in same cwd,
    # Slack message lands in the new Claude).
    assert await tmux_controller.send_message_to_session("hi", pane_id="%99") is False

    # No send-keys should happen at all. (pane_exists is stubbed, so
    # tmux isn't consulted for anything else either.)
    assert not any(c[1] == "send-keys" for c in stub_tmux)
    assert not any("list-panes" in c for c in stub_tmux)


async def test_signature_has_no_cwd_parameter() -> None:
    # Belt-and-braces: nobody re-introduces a cwd fallback behind the API.
    import inspect
    sig = inspect.signature(tmux_controller.send_message_to_session)
    assert "cwd" not in sig.parameters, sig
