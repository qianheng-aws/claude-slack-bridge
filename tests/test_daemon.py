import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient

from claude_slack_bridge.config import BridgeConfig
from claude_slack_bridge.conversation_parser import ConversationParser
from claude_slack_bridge.daemon import Daemon
from claude_slack_bridge.daemon_http import (
    _maybe_warn_version_mismatch,
    _read_last_turn_from_jsonl,
    _strip_wrapper_blocks,
    create_http_app,
)
from claude_slack_bridge.session_manager import SessionManager, SessionMode


@pytest.fixture
def config(tmp_config_dir: Path) -> BridgeConfig:
    return BridgeConfig(
        config_dir=tmp_config_dir,
        slack_app_token="xapp-test",
        slack_bot_token="xoxb-test",
    )


def test_daemon_init(config: BridgeConfig) -> None:
    daemon = Daemon(config)
    assert daemon._config is config
    assert daemon._session_mgr is not None
    assert daemon._approval_mgr is not None
    assert daemon._pool is not None



async def test_daemon_handle_interactive_approve(config: BridgeConfig) -> None:
    daemon = Daemon(config)
    state = daemon._approval_mgr.create("req-123")

    await daemon._handle_interactive(
        action={"action_id": "approve_tool", "value": "req-123"},
        payload={},
    )

    assert state.status == "approved"


async def test_daemon_handle_interactive_reject(config: BridgeConfig) -> None:
    daemon = Daemon(config)
    state = daemon._approval_mgr.create("req-123")

    await daemon._handle_interactive(
        action={"action_id": "reject_tool", "value": "req-123"},
        payload={},
    )

    assert state.status == "rejected"


async def test_hook_pre_tool_use_yolo_session(config: BridgeConfig) -> None:
    """YOLO button marks the session trusted → PreToolUse auto-approves."""
    daemon = Daemon(config)
    # Pre-register the session and mark it trusted, as the YOLO button does.
    from claude_slack_bridge.session_manager import SessionMode
    daemon._session_mgr.create(
        session_id="test-session", session_name="t",
        channel_id="C1", thread_ts="1.0", mode=SessionMode.HOOK,
    )
    daemon._trusted_sessions.add("test-session")
    app = create_http_app(daemon)

    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/pre-tool-use",
            json={
                "session_key": "test-session",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "cwd": "/tmp",
            },
        )
        assert resp.status == 200
        text = await resp.text()
        assert text == "approved"


async def test_hook_pre_tool_use_auto_approve_safe_tool(config: BridgeConfig) -> None:
    """Issue #2: Safe tools (Read, Glob, Grep) should auto-approve."""
    daemon = Daemon(config)
    app = create_http_app(daemon)

    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        for tool in ["Read", "Glob", "Grep"]:
            resp = await client.post(
                "/hooks/pre-tool-use",
                json={
                    "session_key": "test-session",
                    "tool_name": tool,
                    "tool_input": {},
                    "cwd": "/tmp",
                },
            )
            assert resp.status == 200
            text = await resp.text()
            assert text == "approved", f"{tool} should be auto-approved"


async def test_hook_pre_tool_use_no_slack_approves(config: BridgeConfig) -> None:
    """Issue #2: Without Slack connection, fail open (approve)."""
    daemon = Daemon(config)
    daemon._slack = None  # No Slack
    app = create_http_app(daemon)

    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/pre-tool-use",
            json={
                "session_key": "unknown-session",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
                "cwd": "/tmp",
            },
        )
        assert resp.status == 200
        text = await resp.text()
        assert text == "approved"


async def test_hook_pre_tool_use_trusted_session(config: BridgeConfig) -> None:
    """Issue #2: Trusted sessions should auto-approve."""
    daemon = Daemon(config)

    # Create a session and trust it
    session = daemon._session_mgr.create(
        session_id="s1", session_name="test",
        channel_id="C123", thread_ts="t1",
        mode=SessionMode.HOOK,
    )
    daemon._trusted_sessions.add("s1")

    app = create_http_app(daemon)

    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/pre-tool-use",
            json={
                "session_key": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
                "cwd": "/tmp",
            },
        )
        assert resp.status == 200
        text = await resp.text()
        assert text == "approved"


# ── _read_last_turn_from_jsonl tests ──


def test_read_last_turn_from_jsonl(tmp_path: Path) -> None:
    """_read_last_turn_from_jsonl returns assistant text after the last user message."""
    # Build a fake JSONL file mimicking Claude Code session format
    session_id = "test-session-abc"
    cwd = str(tmp_path / "project")
    project_dir = cwd.replace("/", "-").replace(".", "-")
    jsonl_dir = Path.home() / ".claude" / "projects" / project_dir
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = jsonl_dir / f"{session_id}.jsonl"

    lines = [
        json.dumps({"type": "user", "message": {"content": "Hello"}, "timestamp": "t1"}),
        json.dumps({"type": "assistant", "message": {"content": "Hi there"}, "timestamp": "t2"}),
        json.dumps({"type": "user", "message": {"content": "What is 2+2?"}, "timestamp": "t3"}),
        json.dumps({"type": "assistant", "message": {"content": "The answer is 4."}, "timestamp": "t4"}),
        json.dumps({"type": "assistant", "message": {"content": "Anything else?"}, "timestamp": "t5"}),
    ]
    jsonl_path.write_text("\n".join(lines) + "\n")

    try:
        parser = ConversationParser()
        result = _read_last_turn_from_jsonl(parser, session_id, cwd)
        # Should contain assistant text after the last user message ("What is 2+2?")
        assert "The answer is 4." in result
        assert "Anything else?" in result
        # Should NOT contain the first assistant reply (before the last user msg)
        assert "Hi there" not in result
    finally:
        jsonl_path.unlink(missing_ok=True)
        # Clean up the directory if empty
        try:
            jsonl_dir.rmdir()
        except OSError:
            pass


def test_read_last_turn_from_jsonl_empty_cwd() -> None:
    """_read_last_turn_from_jsonl returns empty string for empty cwd."""
    parser = ConversationParser()
    result = _read_last_turn_from_jsonl(parser, "nonexistent", "")
    assert result == ""


def test_read_last_turn_prefixes_blocks_with_bullet(tmp_path: Path) -> None:
    """Each assistant block gets a leading ●, matching the streaming progress
    message so Slack's finalized text keeps the same visual rhythm as the TUI.
    """
    session_id = "test-bullets-xyz"
    cwd = str(tmp_path / "bullets")
    project_dir = cwd.replace("/", "-").replace(".", "-")
    jsonl_dir = Path.home() / ".claude" / "projects" / project_dir
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = jsonl_dir / f"{session_id}.jsonl"

    lines = [
        json.dumps({"type": "user", "message": {"content": "go"}, "timestamp": "t1"}),
        json.dumps({"type": "assistant", "message": {"content": "first thought"}, "timestamp": "t2"}),
        json.dumps({"type": "assistant", "message": {"content": "second thought"}, "timestamp": "t3"}),
    ]
    jsonl_path.write_text("\n".join(lines) + "\n")

    try:
        parser = ConversationParser()
        result = _read_last_turn_from_jsonl(parser, session_id, cwd)
        # Both blocks present, each with the bullet prefix.
        assert result.startswith("● first thought")
        assert "● second thought" in result
        # Exactly two bullets (one per assistant block), not one global prefix.
        assert result.count("● ") == 2
    finally:
        jsonl_path.unlink(missing_ok=True)
        try:
            jsonl_dir.rmdir()
        except OSError:
            pass


# ── Session lookup by cwd fallback tests ──


def test_session_get_by_cwd_prefers_most_recent(tmp_path: Path) -> None:
    """When multiple sessions share the same cwd, get() returns the most recently active."""
    mgr = SessionManager(tmp_path / "sessions.json")

    # Create two sessions with the same cwd
    s1 = mgr.create("s1", "old session", "C1", "t1", SessionMode.HOOK)
    s1.cwd = "/workplace/project"
    s1.last_active = 1000.0

    s2 = mgr.create("s2", "new session", "C2", "t2", SessionMode.HOOK)
    s2.cwd = "/workplace/project"
    s2.last_active = 2000.0

    # Look up by cwd — should return the more recently active session (s2)
    result = mgr.get("/workplace/project")
    assert result is not None
    assert result.session_id == "s2"


def test_session_get_by_cwd_skips_archived(tmp_path: Path) -> None:
    """Archived sessions should not match cwd lookup."""
    mgr = SessionManager(tmp_path / "sessions.json")

    s1 = mgr.create("s1", "archived", "C1", "t1", SessionMode.HOOK)
    s1.cwd = "/workplace/project"
    s1.last_active = 9999.0  # Very recent but archived
    mgr.archive("s1")

    s2 = mgr.create("s2", "active", "C2", "t2", SessionMode.HOOK)
    s2.cwd = "/workplace/project"
    s2.last_active = 1000.0

    result = mgr.get("/workplace/project")
    assert result is not None
    assert result.session_id == "s2"


def test_session_get_by_id_still_works(tmp_path: Path) -> None:
    """Direct session_id lookup should still take priority over cwd fallback."""
    mgr = SessionManager(tmp_path / "sessions.json")

    s1 = mgr.create("s1", "session one", "C1", "t1", SessionMode.HOOK)
    s1.cwd = "/workplace/project"

    result = mgr.get("s1")
    assert result is not None
    assert result.session_id == "s1"


# ── _forwarded_prompts bounded size tests ──


def test_forwarded_prompts_capped_at_50(config: BridgeConfig) -> None:
    """_forwarded_prompts should not grow beyond 50 entries."""
    daemon = Daemon(config)

    # Simulate adding 50 prompts
    for i in range(50):
        daemon._forwarded_prompts.add(f"prompt-{i}")
    assert len(daemon._forwarded_prompts) == 50

    # The 51st add in daemon_events.py checks len >= 50 and clears first.
    # Simulate that logic here:
    if len(daemon._forwarded_prompts) >= 50:
        daemon._forwarded_prompts.clear()
    daemon._forwarded_prompts.add("prompt-50")
    assert len(daemon._forwarded_prompts) == 1
    assert "prompt-50" in daemon._forwarded_prompts


def test_forwarded_prompts_under_cap_not_cleared(config: BridgeConfig) -> None:
    """Under the cap, prompts accumulate normally."""
    daemon = Daemon(config)

    for i in range(10):
        if len(daemon._forwarded_prompts) >= 50:
            daemon._forwarded_prompts.clear()
        daemon._forwarded_prompts.add(f"prompt-{i}")

    assert len(daemon._forwarded_prompts) == 10


# ── Mute level persistence + semantics ──


def test_mute_levels_roundtrip_persists_to_disk(config: BridgeConfig) -> None:
    """set_mute_level/clear_mute_level survive daemon restart via muted.json."""
    daemon = Daemon(config)
    muted_path = config.config_dir / "muted.json"

    daemon.set_mute_level("s1", "sync")
    daemon.set_mute_level("s2", "ring")
    assert daemon._mute_levels == {"s1": "sync", "s2": "ring"}
    assert json.loads(muted_path.read_text()) == {"s1": "sync", "s2": "ring"}

    # Rehydrate — new Daemon instance reads muted.json via _load_muted().
    # Guards against NameError/import regressions in the persistence path.
    reborn = Daemon(config)
    assert reborn._mute_levels == {"s1": "sync", "s2": "ring"}

    reborn.clear_mute_level("s1")
    assert reborn._mute_levels == {"s2": "ring"}
    assert json.loads(muted_path.read_text()) == {"s2": "ring"}


def test_default_mute_semantics(config: BridgeConfig) -> None:
    """Unknown sessions are fully muted by default — new CC sessions stay quiet."""
    daemon = Daemon(config)
    assert daemon.is_silenced("never-seen") is True
    assert daemon.is_fully_muted("never-seen") is True


def test_mute_level_predicates(config: BridgeConfig) -> None:
    """sync opts in fully; ring silences chatter but keeps permission ringing."""
    daemon = Daemon(config)
    daemon.set_mute_level("synced", "sync")
    daemon.set_mute_level("ringing", "ring")

    # sync = not silenced, not fully-muted → full TUI→Slack sync.
    assert not daemon.is_silenced("synced") and not daemon.is_fully_muted("synced")
    # ring = silenced (ambient gone), but NOT fully muted (permission rings).
    assert daemon.is_silenced("ringing") and not daemon.is_fully_muted("ringing")


def test_mute_invalid_level_rejected(config: BridgeConfig) -> None:
    daemon = Daemon(config)
    with pytest.raises(ValueError):
        daemon.set_mute_level("s1", "bogus")
    # "full" is no longer a level — default state replaces it.
    with pytest.raises(ValueError):
        daemon.set_mute_level("s1", "full")


async def test_mute_api_level_shape(config: BridgeConfig) -> None:
    """POST /sessions/{id}/mute accepts {level: sync|ring|none}."""
    daemon = Daemon(config)
    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/sessions/s1/mute", json={"level": "sync"})
        assert resp.status == 200
        assert (await resp.json())["level"] == "sync"
        assert daemon._mute_levels == {"s1": "sync"}

        # ring overrides sync on same session
        resp = await client.post("/sessions/s1/mute", json={"level": "ring"})
        assert (await resp.json())["level"] == "ring"
        assert daemon._mute_levels == {"s1": "ring"}

        # none drops back to default full mute (clears the dict entry)
        resp = await client.post("/sessions/s1/mute", json={"level": "none"})
        body = await resp.json()
        assert body["ok"] is True and body["level"] is None
        assert daemon._mute_levels == {}

        # Invalid payloads → 400
        resp = await client.post("/sessions/s1/mute", json={})
        assert resp.status == 400
        resp = await client.post("/sessions/s1/mute", json={"level": "bogus"})
        assert resp.status == 400
        # "full" is no longer a valid API level either
        resp = await client.post("/sessions/s1/mute", json={"level": "full"})
        assert resp.status == 400


async def test_permission_request_default_mute_falls_through(config: BridgeConfig) -> None:
    """Sessions with no explicit level default to full mute — permission-request
    returns empty body so CC's TUI dialog takes over."""
    daemon = Daemon(config)
    daemon._slack = MagicMock()
    daemon._slack.set_thread_status = AsyncMock()
    daemon._slack.post_blocks = AsyncMock(return_value="ts.1")
    daemon._session_mgr.create(
        session_id="s1", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    # No set_mute_level call — default state applies.

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/permission-request", json={
            "session_key": "s1", "tool_name": "Bash",
            "tool_input": {"command": "rm file"}, "cwd": "/tmp",
        })
        assert resp.status == 200
        assert (await resp.text()) == ""
        daemon._slack.post_blocks.assert_not_awaited()


async def test_permission_request_ring_mute_still_posts_to_slack(config: BridgeConfig) -> None:
    """ring mute silences ambient sync but keeps permission-request buttons."""
    daemon = Daemon(config)
    daemon._slack = MagicMock()
    daemon._slack.set_thread_status = AsyncMock()
    daemon._slack.post_blocks = AsyncMock(return_value="ts.1")
    daemon._session_mgr.create(
        session_id="s1", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    daemon.set_mute_level("s1", "ring")

    # Stub out the approval wait so we don't block the test.
    original_create = daemon._approval_mgr.create
    def _fake_create(request_id, **kwargs):
        state = original_create(request_id, **kwargs)
        state.resolve("approved")
        return state
    daemon._approval_mgr.create = _fake_create

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/permission-request", json={
            "session_key": "s1", "tool_name": "Bash",
            "tool_input": {"command": "rm file"}, "cwd": "/tmp",
        })
        assert resp.status == 200
        body = await resp.json()
        assert body["decision"] == "approved"
        # Buttons were posted despite ring mute.
        daemon._slack.post_blocks.assert_awaited()


def _mock_slack_for_lazy_bind(daemon) -> None:
    """Rig up a SlackClient double that supports _ensure_slack_thread + posts."""
    fake_web = MagicMock()
    fake_web.conversations_list = AsyncMock(return_value={"channels": [{"id": "D1"}]})
    daemon._slack = MagicMock()
    daemon._slack.web = fake_web
    daemon._slack.post_blocks = AsyncMock(return_value="ts.auto")
    daemon._slack.post_text = AsyncMock()
    daemon._slack.set_thread_status = AsyncMock()
    daemon._bot_user_id = "U_BOT"


async def test_session_start_default_mute_creates_no_slack_thread(config: BridgeConfig) -> None:
    """New TUI sessions default to full mute: no DM thread, no post at all.

    Regression for: (a) _auto_bind_session AttributeError on every new session;
    (b) auto-bind posting a thread header into the DM for every fresh CC session,
    cluttering the user's Slack inbox with empty threads.
    """
    daemon = Daemon(config)
    _mock_slack_for_lazy_bind(daemon)

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/session-start", json={
            "session_key": "brand-new-sid",
            "cwd": "/tmp/project",
            "tmux_pane_id": "%0",
        })
        assert resp.status == 200

    # Session row exists (so we can track origin/tmux/mode) but has no Slack binding.
    session = daemon._session_mgr.get("brand-new-sid")
    assert session is not None
    assert session.channel_id == ""
    assert session.thread_ts == ""
    # Nothing posted to Slack.
    daemon._slack.post_blocks.assert_not_awaited()
    daemon._slack.post_text.assert_not_awaited()


async def test_sync_on_lazy_creates_thread(config: BridgeConfig) -> None:
    """/sessions/bind (sync-on) is the trigger that creates the Slack thread."""
    daemon = Daemon(config)
    _mock_slack_for_lazy_bind(daemon)

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/sessions/bind", json={
            "session_id": "sid-A", "name": "TUI-A", "cwd": "/tmp/project",
        })
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        assert body["channel_id"] == "D1"
        assert body["thread_ts"] == "ts.auto"

    # Thread header posted exactly once.
    daemon._slack.post_blocks.assert_awaited_once()
    session = daemon._session_mgr.get("sid-A")
    assert session.channel_id == "D1" and session.thread_ts == "ts.auto"


async def test_sync_on_idempotent(config: BridgeConfig) -> None:
    """Calling /sessions/bind twice reuses the existing thread; no re-post."""
    daemon = Daemon(config)
    _mock_slack_for_lazy_bind(daemon)

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        await client.post("/sessions/bind", json={"session_id": "sid-B", "cwd": "/tmp"})
        await client.post("/sessions/bind", json={"session_id": "sid-B", "cwd": "/tmp"})

    # First call posts; second is a no-op on the Slack side.
    assert daemon._slack.post_blocks.await_count == 1


async def test_finalize_progress_chunk_failure_does_not_swallow_rest(config: BridgeConfig) -> None:
    """If one chunk's post_text raises, remaining chunks must still be attempted.

    Regression for the 'long sync response stops after first chunk' bug:
    _finalize_progress used to wrap the entire chunk loop in one try/except,
    so any Slack API hiccup on chunk N silently dropped chunks N+1..end.
    """
    daemon = Daemon(config)
    session = daemon._register_session("sid-F", "/tmp")
    session.channel_id = "D1"
    session.thread_ts = "ts.root"

    # Pretend we have a live progress message that finalize should chat_update.
    daemon._progress["sid-F"] = {
        "msg_ts": "ts.prog", "last_update": 0, "lines": [],
        "_full_text": "", "_bracket_hold": "",
    }

    # Build a display long enough to split into 3+ chunks (SLACK_MSG_LIMIT = 3900).
    long_text = ("word " * 2500)  # ~12,500 chars
    # Slack double: chat_update succeeds, post_text fails on the SECOND call
    # (i.e. first post_text after the chat_update — chunk index 1).
    call_count = {"n": 0}
    async def flaky_post_text(channel, text, thread_ts=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated Slack rate limit")
        return "ts.ok"

    fake_web = MagicMock()
    fake_web.chat_update = AsyncMock()
    daemon._slack = MagicMock()
    daemon._slack.web = fake_web
    daemon._slack.post_text = AsyncMock(side_effect=flaky_post_text)

    await daemon._finalize_progress(session, long_text)

    # chat_update ran once for chunk 0.
    fake_web.chat_update.assert_awaited_once()
    # post_text was called for every remaining chunk even though one raised —
    # the previous bug would have stopped at the first failure.
    from claude_slack_bridge.slack_formatter import split_message, md_to_mrkdwn
    expected_chunks = len(split_message(md_to_mrkdwn(long_text)))
    assert expected_chunks >= 3, "test needs a message that splits into ≥3 chunks"
    # chunks after chat_update = expected_chunks - 1
    assert daemon._slack.post_text.await_count == expected_chunks - 1


async def test_finalize_progress_blocks_in_flight_preview_update(config: BridgeConfig) -> None:
    """Regression: streaming preview chat_update races _finalize_progress on the
    same msg_ts. Without serialization, a late preview clobbers the finalized
    reply with a tail-500 snapshot + streaming cursor — users see a truncated
    response ending in the ◍ character.

    We pin the race by making the preview's chat_update slow (blocks on an
    event). Finalize fires during the block, then we release. The preview must
    bail out via the _finalized flag instead of overwriting the final text.
    """
    from claude_slack_bridge.daemon_stream import _CURSOR

    daemon = Daemon(config)
    session = daemon._register_session("sid-race", "/tmp")
    session.channel_id = "D1"
    session.thread_ts = "ts.root"

    # Seed PROCESS-mode progress state with a live msg_ts (as if streaming has
    # already started and some preview updates have landed).
    daemon._progress["sid-race"] = {
        "msg_ts": "ts.prog",
        "last_update": 0,
        "lines": [],
        "_text_blocks": [],
        "_tool": "",
        "_full_text": "a" * 800,  # long enough that preview is tail-500
        "_bracket_hold": "",
        "_finalized": False,
    }

    # chat_update call log + finalize gate.
    chat_update_texts: list[str] = []
    finalize_started = asyncio.Event()
    finalize_done = asyncio.Event()

    async def recording_chat_update(**kwargs) -> None:
        chat_update_texts.append(kwargs.get("text", ""))

    fake_web = MagicMock()
    fake_web.chat_update = AsyncMock(side_effect=recording_chat_update)
    daemon._slack = MagicMock()
    daemon._slack.web = fake_web
    daemon._slack.post_text = AsyncMock(return_value="ts.ok")

    # Preview task: acquire lock, do one "in-progress" chat_update, release,
    # then try again *after* finalize has taken the flag. We simulate this by
    # sleeping until finalize has finished between two preview attempts.
    async def preview_task() -> None:
        state = daemon._progress["sid-race"]

        # First update: runs before finalize, should succeed.
        state["last_update"] = 0
        async with daemon._progress_lock(session.session_id):
            if state.get("_finalized"):
                return
            await daemon._slack.web.chat_update(
                channel=session.channel_id, ts=state["msg_ts"],
                text=("preview-early" + _CURSOR)[:4000],
            )
            state["last_update"] = time.time()

        # Let finalize run to completion now.
        finalize_started.set()
        await finalize_done.wait()

        # Second update: after finalize has sealed. Must bail out.
        state["last_update"] = 0
        async with daemon._progress_lock(session.session_id):
            if state.get("_finalized"):
                return  # correct path: do not call chat_update
            await daemon._slack.web.chat_update(
                channel=session.channel_id, ts=state["msg_ts"],
                text=("preview-late" + _CURSOR)[:4000],
            )

    async def finalize_task() -> None:
        await finalize_started.wait()
        await daemon._finalize_progress(session, "FINAL REPLY TEXT")
        finalize_done.set()

    await asyncio.gather(preview_task(), finalize_task())

    # Exactly 2 chat_update calls: one early preview (with cursor) and one
    # finalize (no cursor). The late preview must have been suppressed.
    assert len(chat_update_texts) == 2, (
        f"expected 2 chat_update calls, got {len(chat_update_texts)}: {chat_update_texts}"
    )
    assert _CURSOR in chat_update_texts[0], "early preview should carry cursor"
    assert chat_update_texts[1] == "FINAL REPLY TEXT", (
        f"finalize chat_update must carry full reply, got {chat_update_texts[1]!r}"
    )
    assert _CURSOR not in chat_update_texts[1], "finalize text must not contain cursor"
    # Preview-late text never reached Slack — the ◍ tail-preview bug cannot recur.
    assert all("preview-late" not in t for t in chat_update_texts)


async def test_finalize_progress_preview_waiting_on_lock_bails_after_finalize(
    config: BridgeConfig,
) -> None:
    """Variant: a preview coroutine is already *waiting* on the lock when
    finalize takes it. When the preview finally acquires the lock, the
    _finalized flag must cause it to return without calling chat_update.

    This is the closest simulation of the real race (multiple fire-and-forget
    _on_stream_event tasks piling up in the event loop).
    """
    from claude_slack_bridge.daemon_stream import _CURSOR

    daemon = Daemon(config)
    session = daemon._register_session("sid-wait", "/tmp")
    session.channel_id = "D1"
    session.thread_ts = "ts.root"

    daemon._progress["sid-wait"] = {
        "msg_ts": "ts.prog",
        "last_update": 0,
        "lines": [],
        "_text_blocks": [],
        "_tool": "",
        "_full_text": "a" * 800,
        "_bracket_hold": "",
        "_finalized": False,
    }

    chat_update_texts: list[str] = []
    finalize_can_proceed = asyncio.Event()
    preview_waiting = asyncio.Event()

    async def recording_chat_update(**kwargs) -> None:
        chat_update_texts.append(kwargs.get("text", ""))

    fake_web = MagicMock()
    fake_web.chat_update = AsyncMock(side_effect=recording_chat_update)
    daemon._slack = MagicMock()
    daemon._slack.web = fake_web
    daemon._slack.post_text = AsyncMock(return_value="ts.ok")

    # Preview acquires the lock first and parks inside, so finalize has to
    # queue. While finalize is queued, a *second* preview task lines up
    # behind it. After preview-1 releases, finalize should run, then
    # preview-2 finds _finalized=True and bails.
    async def preview_1() -> None:
        state = daemon._progress["sid-wait"]
        async with daemon._progress_lock(session.session_id):
            preview_waiting.set()
            await finalize_can_proceed.wait()
            # Inside lock: pretend we're mid update. Real bug: this lands on Slack.
            if state.get("_finalized"):
                return
            await daemon._slack.web.chat_update(
                channel=session.channel_id, ts=state["msg_ts"],
                text=("preview-1" + _CURSOR)[:4000],
            )

    async def preview_2() -> None:
        state = daemon._progress["sid-wait"]
        # Queue behind preview-1 (and then behind finalize).
        await preview_waiting.wait()
        async with daemon._progress_lock(session.session_id):
            if state.get("_finalized"):
                return
            await daemon._slack.web.chat_update(
                channel=session.channel_id, ts=state["msg_ts"],
                text=("preview-2-late" + _CURSOR)[:4000],
            )

    async def finalize_runner() -> None:
        # Wait until preview-1 is in the lock, then release it so finalize
        # gets queued (and so does preview-2).
        await preview_waiting.wait()
        # Give preview-2 a tick to enqueue behind finalize.
        await asyncio.sleep(0)
        finalize_can_proceed.set()
        await daemon._finalize_progress(session, "COMPLETE REPLY")

    await asyncio.gather(preview_1(), preview_2(), finalize_runner())

    # preview-1 ran before finalize flipped the flag, so its chat_update lands.
    # finalize then ran and posted the full reply. preview-2 saw _finalized
    # and bailed — it must NOT appear in the call log.
    texts = [t for t in chat_update_texts]
    assert "COMPLETE REPLY" in texts, f"finalize text missing: {texts}"
    assert all("preview-2-late" not in t for t in texts), (
        f"late preview clobbered finalize: {texts}"
    )
    # Also verify the finalize call was the *last* chat_update to touch msg_ts —
    # otherwise the user sees a partial/stale message in Slack.
    assert texts[-1] == "COMPLETE REPLY", f"finalize must be last; got {texts}"


async def test_stream_events_end_to_end_finalize_wins_under_race(config: BridgeConfig) -> None:
    """End-to-end regression: drive real _on_stream_event with many concurrent
    assistant-text events followed by a result event, exactly like
    process_pool._read_stdout does (asyncio.create_task per event, no
    serialization by the dispatcher).

    The whole point of _progress_lock + _finalized flag is that no matter
    how tasks interleave, the *last* chat_update to msg_ts must carry the
    finalized reply — not a truncated streaming preview ending in ◍.

    Before the fix, a late preview task could land after finalize and leave
    Slack showing a 500-char tail + cursor. The fix guarantees finalize
    wins every race.
    """
    from claude_slack_bridge.daemon_stream import _CURSOR
    from claude_slack_bridge.stream_parser import StreamEvent
    from claude_slack_bridge.session_manager import SessionMode

    daemon = Daemon(config)
    sid = "sid-e2e-race"
    session = daemon._session_mgr.create(
        session_id=sid, session_name="e2e",
        channel_id="D1", thread_ts="ts.root",
        mode=SessionMode.PROCESS,
    )
    session.cwd = "/tmp"

    # Capture chat_update history in order. Each call takes a tiny async pause
    # so multiple fire-and-forget tasks actually interleave at await points
    # (deterministic scheduler, but enough yield points to shuffle ordering).
    chat_update_log: list[tuple[str, str]] = []  # (ts, text)

    # Simulate realistic Slack API latency variance: early preview calls are
    # SLOW (imagine a transient rate-limit retry or a slow round-trip), while
    # the finalize call is fast. Without _progress_lock + _finalized, the slow
    # preview would land *after* finalize and clobber the final text. The
    # record captures the ORDER in which calls actually complete — that's
    # what Slack sees as the last-write-wins.
    call_seq = {"n": 0}

    async def slow_chat_update(**kwargs) -> None:
        call_seq["n"] += 1
        my_n = call_seq["n"]
        text = kwargs.get("text", "")
        # Preview calls carry the cursor; make them arbitrarily slow so later
        # events "overtake" earlier ones. Finalize (no cursor) is fast.
        if _CURSOR in text:
            # Descending delays: earlier previews sleep longer. With a fast
            # finalize, this is exactly the interleaving that triggered the
            # production bug (stale preview overwrites final reply).
            await asyncio.sleep(0.01 * (20 - my_n))
        else:
            await asyncio.sleep(0)
        chat_update_log.append((kwargs.get("ts", ""), text))

    fake_web = MagicMock()
    fake_web.chat_update = AsyncMock(side_effect=slow_chat_update)
    daemon._slack = MagicMock()
    daemon._slack.web = fake_web
    daemon._slack.post_text = AsyncMock(return_value="ts.progress")
    daemon._slack.post_blocks = AsyncMock(return_value="ts.blocks")
    daemon._slack.set_thread_status = AsyncMock()

    # Build a growing stream: 12 assistant-text deltas, each adding more text,
    # followed by a result event. Text is long enough that tail-500 slicing
    # would lose the opening if it clobbered the final.
    growing_text = ""
    full_reply = (
        "Hello! Here is the complete answer the user expects.\n"
        + ("detail " * 200)  # pushes past the 500 tail window
        + "\n\nSincerely, Claude."
    )
    assert len(full_reply) > 500
    step = len(full_reply) // 12
    text_events = []
    for i in range(1, 13):
        chunk_end = step * i if i < 12 else len(full_reply)
        growing_text = full_reply[:chunk_end]
        text_events.append(StreamEvent(raw_type="assistant", text=growing_text))

    # Drive EDIT_INTERVAL to 0 so every preview attempts a chat_update.
    # Also need to coerce state["last_update"] between dispatches — but the
    # real flow does this naturally; for the test we zero last_update before
    # each dispatch by reaching into state after _on_stream_event seeds it.
    # Simpler: monkeypatch _EDIT_INTERVAL down to 0 via module attr for this test.
    import claude_slack_bridge.daemon_stream as ds
    orig_interval = ds._EDIT_INTERVAL
    ds._EDIT_INTERVAL = 0.0
    try:
        # Fire all events as create_task (matches process_pool dispatch style)
        # plus the terminal result event. They all race on the event loop.
        result_evt = StreamEvent(
            raw_type="result", text="", result={"is_error": False, "permission_denials": []}
        )

        tasks = [asyncio.create_task(daemon._on_stream_event(sid, evt)) for evt in text_events]
        # Interleave the result a bit later, after most previews have queued.
        await asyncio.sleep(0)
        tasks.append(asyncio.create_task(daemon._on_stream_event(sid, result_evt)))

        await asyncio.gather(*tasks)
    finally:
        ds._EDIT_INTERVAL = orig_interval

    # Invariants:
    # 1. At least one chat_update happened on ts.progress (the shared msg_ts).
    ts_prog_calls = [(ts, txt) for ts, txt in chat_update_log if ts == "ts.progress"]
    assert ts_prog_calls, (
        f"expected chat_update on progress msg_ts, got: {chat_update_log}"
    )

    # 2. The LAST chat_update on that msg_ts is the finalized full reply —
    #    NOT a streaming preview (which would end in _CURSOR).
    last_ts, last_text = ts_prog_calls[-1]
    assert _CURSOR not in last_text, (
        f"last chat_update on progress msg still has streaming cursor: "
        f"{last_text[-60:]!r} — race not suppressed"
    )
    # Finalize path sends md_to_mrkdwn(full_reply); the distinctive head of
    # the reply must be present (the bug would leave only the tail-500).
    assert last_text.startswith("Hello! Here is the complete answer"), (
        f"last chat_update lost the opening of the reply: head={last_text[:80]!r}"
    )
    assert "Sincerely, Claude." in last_text, (
        f"last chat_update lost the closing of the reply: tail={last_text[-80:]!r}"
    )

    # 3. No chat_update with the cursor appears AFTER the finalized one.
    #    (walk from the last finalized call forward; there should be none.)
    final_index = len(chat_update_log) - 1
    for idx in range(len(chat_update_log) - 1, -1, -1):
        ts, txt = chat_update_log[idx]
        if ts == "ts.progress" and _CURSOR not in txt:
            final_index = idx
            break
    tail_after_final = chat_update_log[final_index + 1:]
    assert all(ts != "ts.progress" or _CURSOR not in txt for ts, txt in tail_after_final), (
        f"streaming preview landed AFTER finalize: {tail_after_final}"
    )

    # 4. Cleanup: progress state popped, lock dropped.
    assert sid not in daemon._progress
    assert sid not in daemon._progress_locks


async def test_permission_request_ring_mute_lazy_binds_thread(config: BridgeConfig) -> None:
    """First permission-request under ring mute creates the thread on demand."""
    daemon = Daemon(config)
    _mock_slack_for_lazy_bind(daemon)
    # Pre-register session in ring mode (mimics /sync-ring having set the level
    # but not yet bound, e.g. if ring was set before any Slack interaction).
    session = daemon._register_session("sid-R", "/tmp/proj")
    daemon.set_mute_level("sid-R", "ring")
    assert session.channel_id == ""  # no thread yet

    # Stub out the approval wait so we don't block.
    original_create = daemon._approval_mgr.create
    def _fake_create(request_id, **kwargs):
        state = original_create(request_id, **kwargs)
        state.resolve("approved")
        return state
    daemon._approval_mgr.create = _fake_create

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/permission-request", json={
            "session_key": "sid-R", "tool_name": "Bash",
            "tool_input": {"command": "rm"}, "cwd": "/tmp",
        })
        body = await resp.json()
        assert body["decision"] == "approved"

    # Thread was lazy-bound during the permission-request handling.
    session = daemon._session_mgr.get("sid-R")
    assert session.channel_id == "D1" and session.thread_ts == "ts.auto"
    daemon._slack.post_blocks.assert_awaited()  # header + approval buttons


# ── Version mismatch warning ──


async def test_version_mismatch_posts_warning_once(config: BridgeConfig) -> None:
    """Plugin/daemon version drift triggers exactly one Slack warning per pair."""
    daemon = Daemon(config)
    daemon._slack = MagicMock()
    daemon._slack.post_text = AsyncMock()

    # Drift the plugin version relative to the daemon's own __version__.
    from claude_slack_bridge import __version__ as daemon_version
    stale_plugin = "0.0.0-stale"
    assert stale_plugin != daemon_version

    await _maybe_warn_version_mismatch(daemon, "C1", "t1", stale_plugin)
    await _maybe_warn_version_mismatch(daemon, "C1", "t1", stale_plugin)

    # First call warns; second call is suppressed because the (plugin, daemon)
    # pair hasn't changed — otherwise every SessionStart spams the thread.
    assert daemon._slack.post_text.await_count == 1
    warning = daemon._slack.post_text.await_args.args[1]
    assert "Version mismatch" in warning
    assert stale_plugin in warning
    assert daemon_version in warning


async def test_version_mismatch_silent_when_matching(config: BridgeConfig) -> None:
    """No warning when plugin_version == daemon __version__, or when empty."""
    from claude_slack_bridge import __version__ as daemon_version

    daemon = Daemon(config)
    daemon._slack = MagicMock()
    daemon._slack.post_text = AsyncMock()

    await _maybe_warn_version_mismatch(daemon, "C1", "t1", daemon_version)
    await _maybe_warn_version_mismatch(daemon, "C1", "t1", "")

    assert daemon._slack.post_text.await_count == 0


# ── Plan-mode sync regression (reporter: sync-on then /plan stops posting) ──


def _setup_bound_sync_session(daemon, sid: str = "sid-plan") -> None:
    """Bound HOOK-mode session with sync mute + mocked Slack. Shared by plan-mode tests."""
    session = daemon._session_mgr.create(
        session_id=sid, session_name=f"TUI-{sid}",
        channel_id="D1", thread_ts="ts.root",
        mode=SessionMode.HOOK,
    )
    session.cwd = "/tmp"
    daemon.set_mute_level(sid, "sync")  # opt in so is_silenced() is False

    fake_web = MagicMock()
    fake_web.chat_update = AsyncMock()
    daemon._slack = MagicMock()
    daemon._slack.web = fake_web
    daemon._slack.post_text = AsyncMock(return_value="ts.new")
    daemon._slack.post_blocks = AsyncMock(return_value="ts.new")
    daemon._slack.set_thread_status = AsyncMock()
    daemon._slack.update_text = AsyncMock()
    daemon._bot_user_id = "U_BOT"


async def test_user_prompt_plan_mode_system_reminder_filter_drops_real_text(
    config: BridgeConfig,
) -> None:
    """H1 regression: plan-mode prepends <system-reminder> to every user prompt;
    the user-prompt handler now strips that leading wrapper and still syncs the
    real text. Before the fix, daemon_http.py's startswith("<") + tag-in-100-chars
    heuristic dropped the whole payload.
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-plan")

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    real_text = "Hey, actually write the code"
    prompt = (
        "<system-reminder>\nPlan mode is active. The user indicated "
        "that they do not want you to execute yet.\n</system-reminder>\n\n"
        + real_text
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/user-prompt", json={
            "session_key": "sid-plan", "prompt": prompt, "cwd": "/tmp",
        })
        assert resp.status == 200

    # The real user text must be synced to Slack. Current code filters it out.
    post_calls = daemon._slack.post_text.await_args_list
    texts = [c.args[1] for c in post_calls]
    assert any(real_text in t for t in texts), (
        f"Real user text was never posted to Slack. post_text calls: {texts!r}"
    )


async def test_user_prompt_bare_system_reminder_still_filtered(
    config: BridgeConfig,
) -> None:
    """H1 guard: a prompt that is *only* a system-reminder (no real user text)
    must stay filtered, so the fix doesn't leak internal scaffolding into Slack.
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-bare")

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    prompt = "<system-reminder>internal reminder only</system-reminder>"

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/user-prompt", json={
            "session_key": "sid-bare", "prompt": prompt, "cwd": "/tmp",
        })
        assert resp.status == 200

    # Must NOT post the reminder as a user message.
    for call in daemon._slack.post_text.await_args_list:
        assert "internal reminder only" not in call.args[1]


async def test_user_prompt_pops_progress_even_when_filtered(
    config: BridgeConfig,
) -> None:
    """H2 probe: user-prompt's _progress.pop runs before the filter, so a
    filtered plan-mode prompt still resets progress state — which means a
    *subsequent* post-tool-use creates a fresh message, NOT editing the old one.

    If this assertion ever flips, the pop has been moved inside the filter
    and H2 becomes the new explanation for "edits previous message".
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-pop")

    # Seed stale progress state from a prior (unfinalized) turn.
    daemon._progress["sid-pop"] = {
        "msg_ts": "ts.old", "last_update": 0, "lines": [],
        "_text_blocks": [], "_tool": "",
    }

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    prompt = "<system-reminder>Plan mode is active.</system-reminder>"  # filtered

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/user-prompt", json={
            "session_key": "sid-pop", "prompt": prompt, "cwd": "/tmp",
        })
        assert resp.status == 200

    # Pop at daemon_http.py:361 runs unconditionally (before the filter).
    assert "sid-pop" not in daemon._progress, (
        "user-prompt handler should clear stale _progress before filtering"
    )


async def test_post_tool_use_does_not_touch_progress_message(
    config: BridgeConfig,
) -> None:
    """Invariant: the post-tool-use hook must NOT write the progress message.
    Tool display is driven exclusively by the JSONL watcher (single content
    source, prevents double-append). The hook still does status side-effects
    — phase reaction, thread status, pending-approval cleanup — but stays
    out of _update_progress.

    Regression guard: an earlier version of the hook called
    _update_progress(is_tool=True) directly, which caused every tool to
    appear twice once we made the watcher the content source, and caused
    stale-msg_ts edits when a turn boundary dropped the user-prompt hook.
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-notouch")

    # Seed progress state as if a prior turn were still in flight.
    daemon._progress["sid-notouch"] = {
        "msg_ts": "ts.old", "last_update": 0, "lines": [],
        "_text_blocks": ["prior assistant text"], "_tool": "",
    }

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/post-tool-use", json={
            "session_key": "sid-notouch",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_output": "",
            "cwd": "/tmp",
        })
        assert resp.status == 200

    # Hook did side-effects (set_thread_status), but did NOT chat_update
    # the progress message.
    daemon._slack.web.chat_update.assert_not_awaited()
    daemon._slack.set_thread_status.assert_awaited()


async def test_post_tool_use_clears_pending_approval_card_under_ring_mute(
    config: BridgeConfig,
) -> None:
    """Regression: under ring mute, approving from the TUI must still flip
    the Slack approval card to "Approved in TUI".

    Bug: post-tool-use short-circuits through `if not daemon.is_silenced(...)`,
    which is True for ring mute — so the `_pending_approval_msgs.pop + update_blocks`
    block never runs, leaving the Approve/Reject/Trust/YOLO buttons live in
    Slack even after the TUI has already decided. Meanwhile the *approval
    request* itself is gated on `is_fully_muted`, which IS False for ring,
    so the card gets posted in the first place. The two gates disagree and
    the card leaks.
    """
    daemon = Daemon(config)
    session = daemon._session_mgr.create(
        session_id="sid-ring", session_name="TUI-ring",
        channel_id="D1", thread_ts="ts.root",
        mode=SessionMode.HOOK,
    )
    session.cwd = "/tmp"
    daemon.set_mute_level("sid-ring", "ring")  # is_silenced=True, is_fully_muted=False

    fake_web = MagicMock()
    fake_web.chat_update = AsyncMock()
    daemon._slack = MagicMock()
    daemon._slack.web = fake_web
    daemon._slack.update_blocks = AsyncMock()
    daemon._slack.set_thread_status = AsyncMock()

    # Simulate: permission-request previously posted an approval card and
    # recorded its ts. TUI then approved locally → post-tool-use arrives.
    daemon._pending_approval_msgs["sid-ring"] = "ts.approval"

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/post-tool-use", json={
            "session_key": "sid-ring",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_output": "",
            "cwd": "/tmp",
        })
        assert resp.status == 200

    # Card must be updated to the "approved in TUI" state.
    daemon._slack.update_blocks.assert_awaited_once()
    call = daemon._slack.update_blocks.await_args
    assert call.args[0] == "D1"
    assert call.args[1] == "ts.approval"
    # And the pending pointer cleared so a future turn doesn't re-edit it.
    assert "sid-ring" not in daemon._pending_approval_msgs


# ── _strip_wrapper_blocks unit tests ──


def test_strip_wrapper_keeps_real_text_after_system_reminder():
    """The plan-mode case: one leading <system-reminder> block + real user text."""
    got = _strip_wrapper_blocks(
        "<system-reminder>Plan mode is active.</system-reminder>\n\nfix the bug"
    )
    assert got == "fix the bug"


def test_strip_wrapper_peels_multiple_leading_blocks():
    """Slash-command invocations stack several wrapper blocks before real text."""
    got = _strip_wrapper_blocks(
        "<command-name>/plan</command-name>"
        "<command-message>plan</command-message>"
        "<command-args></command-args>\n\n"
        "design the migration"
    )
    assert got == "design the migration"


def test_strip_wrapper_returns_empty_for_wrapper_only():
    """Pure wrapper payload → caller treats as skip (nothing to sync)."""
    assert _strip_wrapper_blocks(
        "<system-reminder>internal only</system-reminder>"
    ) == ""


def test_strip_wrapper_leaves_plain_text_untouched():
    """No wrappers: stripper is a no-op (sans outer whitespace)."""
    assert _strip_wrapper_blocks("  hello world  ") == "hello world"


def test_strip_wrapper_does_not_strip_inline_lookalikes():
    """<system-reminder> in the middle of a real prompt stays put — we only peel
    leading wrappers, not anything that happens to contain angle brackets."""
    got = _strip_wrapper_blocks(
        "look at <system-reminder>this literal</system-reminder> in the code"
    )
    assert got == "look at <system-reminder>this literal</system-reminder> in the code"


def test_strip_wrapper_handles_multiline_reminder():
    """The real plan-mode payload has newlines and a big block of rules."""
    prompt = (
        "<system-reminder>\n"
        "Plan mode is active. The user indicated that they do not want you to\n"
        "execute yet -- you MUST NOT make any edits...\n"
        "</system-reminder>\n\n"
        "implement the feature"
    )
    assert _strip_wrapper_blocks(prompt) == "implement the feature"


# ── Progress accumulation (B1) and approval sealing ──


async def test_update_progress_accumulates_assistant_text(config: BridgeConfig) -> None:
    """Successive assistant text blocks should stack in the progress message
    instead of overwriting each other. The old single-slot behavior made long
    reasoning look like "one sentence, then suddenly the final answer."
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-acc")
    # Drive updates past the 1s throttle between calls.
    import time as _t

    await daemon._update_progress(daemon._session_mgr.get("sid-acc"), "_first thought_")
    state = daemon._progress["sid-acc"]
    state["last_update"] = 0  # let the next update fire chat_update
    await daemon._update_progress(daemon._session_mgr.get("sid-acc"), "_second thought_")

    # Both blocks must survive in state.
    assert daemon._progress["sid-acc"]["_text_blocks"] == [
        "_first thought_", "_second thought_"
    ]
    # The chat_update payload should contain both.
    chat_update = daemon._slack.web.chat_update
    chat_update.assert_awaited()
    last_text = chat_update.await_args.kwargs.get("text", "")
    assert "_first thought_" in last_text and "_second thought_" in last_text


async def test_update_progress_tolerates_legacy_state_without_text_blocks(
    config: BridgeConfig,
) -> None:
    """PROCESS-mode seed used to write _progress entries that only had
    _full_text/_bracket_hold — no _text_blocks/_tool. Then the first tool_use
    event called _update_progress(is_tool=True), which KeyError'd on
    state['_text_blocks']. That killed intermediate streaming events and
    the final response showed up truncated in Slack.

    Regression guard: _update_progress must not crash when it finds a
    pre-seeded progress dict that predates the tool-timeline keys.
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-legacy")
    session = daemon._session_mgr.get("sid-legacy")

    # Pre-seed with the OLD PROCESS-mode schema (what 0.4.5 would have left
    # behind in _progress when tool_use arrived).
    daemon._progress["sid-legacy"] = {
        "msg_ts": "ts.legacy", "last_update": 0, "lines": [],
        "_full_text": "streaming so far...",
        "_bracket_hold": "",
    }

    # First tool_use after the legacy seed — must not raise.
    await daemon._update_progress(session, "🪆 `Bash` ls", is_tool=True)

    # State was patched up and tool was recorded in the expected slot.
    assert daemon._progress["sid-legacy"]["_tool"] == "🪆 `Bash` ls"
    assert daemon._progress["sid-legacy"]["_text_blocks"] == []


async def test_update_progress_archives_previous_tool_into_history(
    config: BridgeConfig,
) -> None:
    """When a new tool line arrives, the previous tool line graduates into
    _text_blocks rather than being dropped. This keeps the full tool timeline
    visible in the progress message (matches TUI's interleaved thinking+tools).
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-tools")
    session = daemon._session_mgr.get("sid-tools")

    # Tool 1: seeds _tool slot.
    await daemon._update_progress(session, "🪆 `Read` foo.py", is_tool=True)
    state = daemon._progress["sid-tools"]
    state["last_update"] = 0
    assert state["_tool"] == "🪆 `Read` foo.py"
    assert state["_text_blocks"] == []

    # Tool 2 arrives: tool 1 should be archived into history.
    await daemon._update_progress(session, "🪆 `Bash` ls", is_tool=True)
    assert daemon._progress["sid-tools"]["_text_blocks"] == [
        "🪆 `Read` foo.py"
    ]
    assert daemon._progress["sid-tools"]["_tool"] == "🪆 `Bash` ls"

    # A thought interleaves, then tool 3 — both prior tools now in history.
    daemon._progress["sid-tools"]["last_update"] = 0
    await daemon._update_progress(session, "● _hmm_")
    daemon._progress["sid-tools"]["last_update"] = 0
    await daemon._update_progress(session, "🪆 `Grep` pattern", is_tool=True)
    assert daemon._progress["sid-tools"]["_text_blocks"] == [
        "🪆 `Read` foo.py",
        "● _hmm_",
        "🪆 `Bash` ls",
    ]
    assert daemon._progress["sid-tools"]["_tool"] == "🪆 `Grep` pattern"


async def test_update_progress_no_200_char_truncation(config: BridgeConfig) -> None:
    """The watcher used to truncate assistant text to 200 chars — verify a
    longer block survives end-to-end through _update_progress.
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-long")
    long_block = "_" + ("word " * 80).strip() + "_"  # ~400 chars
    assert len(long_block) > 200

    await daemon._update_progress(daemon._session_mgr.get("sid-long"), long_block)
    state = daemon._progress["sid-long"]
    state["last_update"] = 0
    await daemon._update_progress(daemon._session_mgr.get("sid-long"), "_next_")

    text = daemon._slack.web.chat_update.await_args.kwargs.get("text", "")
    assert long_block in text


async def test_seal_progress_clears_state_and_drops_cursor(config: BridgeConfig) -> None:
    """_seal_progress should: (a) remove the session's entry from _progress so
    the next update creates a fresh Slack message, (b) chat_update the frozen
    copy one last time without the streaming cursor.
    """
    from claude_slack_bridge.daemon_stream import _CURSOR

    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-seal")
    session = daemon._session_mgr.get("sid-seal")
    daemon._progress["sid-seal"] = {
        "msg_ts": "ts.live", "last_update": 0, "lines": [],
        "_text_blocks": ["thinking..."], "_tool": "🪆 `Bash` ls",
    }

    await daemon._seal_progress(session)

    # State popped — next _update_progress would start fresh.
    assert "sid-seal" not in daemon._progress
    # Frozen redraw landed and did NOT carry the cursor marker.
    chat_update = daemon._slack.web.chat_update
    chat_update.assert_awaited_once()
    kwargs = chat_update.await_args.kwargs
    assert kwargs["ts"] == "ts.live"
    assert _CURSOR not in kwargs["text"]
    assert "thinking..." in kwargs["text"]
    assert "Bash" in kwargs["text"]


async def test_seal_progress_is_noop_when_no_active_progress(config: BridgeConfig) -> None:
    """Sealing when there's no in-flight progress must not crash and must not
    spam Slack with an empty chat_update (e.g. approval as the very first
    event of a turn)."""
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-noseal")
    session = daemon._session_mgr.get("sid-noseal")

    await daemon._seal_progress(session)  # should just return

    daemon._slack.web.chat_update.assert_not_awaited()


async def test_permission_request_seals_progress_before_posting_buttons(
    config: BridgeConfig,
) -> None:
    """When a TUI permission-request arrives with a live progress message,
    the approval buttons must come AFTER a sealed copy of the current progress.
    Observable: _progress[sid] is cleared by the time post_blocks is called,
    and the seal's chat_update fired before the buttons' post_blocks.
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-perm")

    # Seed live progress state.
    daemon._progress["sid-perm"] = {
        "msg_ts": "ts.live", "last_update": 0, "lines": [],
        "_text_blocks": ["I should probably run this"], "_tool": "",
    }

    # Track call ordering: chat_update (seal) must happen before post_blocks (buttons).
    call_order: list[str] = []
    orig_chat_update = daemon._slack.web.chat_update
    async def tracked_chat_update(**kw):
        call_order.append("seal")
        return await orig_chat_update(**kw)
    daemon._slack.web.chat_update = tracked_chat_update

    orig_post_blocks = daemon._slack.post_blocks
    async def tracked_post_blocks(*a, **kw):
        call_order.append("buttons")
        return await orig_post_blocks(*a, **kw)
    daemon._slack.post_blocks = tracked_post_blocks

    # Stub approval wait so it doesn't block.
    original_create = daemon._approval_mgr.create
    def _fake_create(request_id, **kwargs):
        st = original_create(request_id, **kwargs)
        st.resolve("approved")
        return st
    daemon._approval_mgr.create = _fake_create

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/permission-request", json={
            "session_key": "sid-perm",
            "tool_name": "Bash",
            "tool_input": {"command": "rm file"},
            "cwd": "/tmp",
        })
        assert resp.status == 200

    # Progress state was sealed → no longer tracked.
    assert "sid-perm" not in daemon._progress
    # Seal ran, then buttons were posted.
    assert "seal" in call_order and "buttons" in call_order
    assert call_order.index("seal") < call_order.index("buttons"), (
        f"Approval buttons must land after the sealed progress; got {call_order}"
    )
