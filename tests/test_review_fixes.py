"""Regression tests for the 2026-07 full-code-review findings.

One test (or small group) per finding, referencing the finding it pins:

  F1  Reaction-controller leaks on early-return thread-reply paths
  F2  ProcessPool kill-existing race / SIGTERM(-15) misclassified as error
  F3  Lazy DM binding must honor owner_user_id
  F4  JSONL watcher poll loop survives callback exceptions
  F5  SessionManager.create must not hijack an active session's thread
  F7  extract_options mid-text truncation
  F8  Conversation parser: one JSONL record → ALL blocks
  F9  Parser memory stays bounded
  F10 decode_project_dir handles dots
  F11 .env values may be quoted
  F12 Options button click respects pending approval
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_slack_bridge.config import BridgeConfig, load_config
from claude_slack_bridge.conversation_parser import (
    ConversationParser,
    SessionFileWatcher,
    _MAX_KEPT_MESSAGES,
)
from claude_slack_bridge.daemon import Daemon
from claude_slack_bridge.daemon_utils import decode_project_dir
from claude_slack_bridge.process_pool import ClaudeProcess, ProcessPool
from claude_slack_bridge.session_manager import SessionManager, SessionMode
from claude_slack_bridge.slack_formatter import extract_options
from claude_slack_bridge.stream_parser import StreamEvent


@pytest.fixture
def config(tmp_config_dir: Path) -> BridgeConfig:
    return BridgeConfig(
        config_dir=tmp_config_dir,
        slack_app_token="xapp-test",
        slack_bot_token="xoxb-test",
    )


def _daemon_with_slack(config: BridgeConfig) -> Daemon:
    daemon = Daemon(config)
    fake_web = MagicMock()
    fake_web.chat_update = AsyncMock()
    daemon._slack = MagicMock()
    daemon._slack.web = fake_web
    daemon._slack.post_text = AsyncMock(return_value="ts.post")
    daemon._slack.post_blocks = AsyncMock(return_value="ts.blocks")
    daemon._slack.set_thread_status = AsyncMock()
    daemon._slack.add_reaction = AsyncMock()
    daemon._slack.remove_reaction = AsyncMock()
    daemon._bot_user_id = "U_BOT"
    return daemon


# ── F1: reaction-controller lifecycle on thread replies ──


async def test_command_reply_creates_no_reaction_controller(config: BridgeConfig) -> None:
    """`sync summary` (and friends) answer instantly — arming the stall
    watchdog for them орphaned a controller whose 45s timer fired
    :cold_sweat: on an already-answered message."""
    daemon = _daemon_with_slack(config)
    daemon._session_mgr.create(
        session_id="s-cmd", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )

    await daemon._handle_thread_reply(
        {"channel": "C1", "text": "sync summary", "ts": "2.0"}, "1.0",
    )

    assert "s-cmd" not in daemon._reaction_controllers
    # No reaction was ever added to the command message.
    daemon._slack.add_reaction.assert_not_awaited()


async def test_approval_reminder_creates_no_reaction_controller(config: BridgeConfig) -> None:
    daemon = _daemon_with_slack(config)
    daemon._session_mgr.create(
        session_id="s-rem", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    daemon._pending_approval_msgs["s-rem"] = "ts.approval"

    await daemon._handle_thread_reply(
        {"channel": "C1", "text": "keep going", "ts": "2.0"}, "1.0",
    )

    assert "s-rem" not in daemon._reaction_controllers
    daemon._slack.add_reaction.assert_not_awaited()


async def test_process_resume_seeds_controller_into_progress(config: BridgeConfig) -> None:
    """Thread reply routed to --print: the rc must land in the progress state
    so stream events drive it. --print suppresses hooks, so without the seed
    nothing ever finalizes the controller (stuck 👀 + false 😰)."""
    daemon = _daemon_with_slack(config)
    session = daemon._session_mgr.create(
        session_id="s-proc", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.PROCESS,
    )
    session.origin = "slack"
    daemon._pool.start = AsyncMock()  # don't spawn a real claude

    await daemon._handle_thread_reply(
        {"channel": "C1", "text": "run the thing", "ts": "2.0"}, "1.0",
    )

    state = daemon._progress.get("s-proc")
    assert state is not None
    rc = state.get("_reactions")
    assert rc is not None
    # Same rc is registered for hooks (e.g. slack→tui origin promotion later).
    assert daemon._reaction_controllers.get("s-proc") is rc


async def test_result_event_pops_hook_controller_reference(config: BridgeConfig) -> None:
    """After the stream `result` finalizes the rc, the hook-side reference
    must be dropped so a later Stop hook can't re-finalize a stale rc."""
    daemon = _daemon_with_slack(config)
    daemon._session_mgr.create(
        session_id="s-res", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.PROCESS,
    )
    rc = MagicMock()
    rc.finalize = AsyncMock()
    daemon._progress["s-res"] = {
        "msg_ts": None, "last_update": 0, "lines": [],
        "_text_blocks": [], "_tool": "", "_full_text": "",
        "_bracket_hold": "", "_reactions": rc, "_finalized": False,
    }
    daemon._reaction_controllers["s-res"] = rc

    evt = StreamEvent(raw_type="result", result={"is_error": False})
    await daemon._on_stream_event("s-res", evt)
    await asyncio.sleep(0)

    rc.finalize.assert_called_once_with(error=False)
    assert "s-res" not in daemon._reaction_controllers


async def test_new_turn_finalizes_previous_controller(config: BridgeConfig) -> None:
    """A second thread reply while the previous turn's rc is still registered
    must finalize the old one (kill its timers) before arming a new one."""
    daemon = _daemon_with_slack(config)
    session = daemon._session_mgr.create(
        session_id="s-two", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    session.origin = "slack"
    daemon._pool.start = AsyncMock()

    old_rc = MagicMock()
    old_rc.finalize = AsyncMock()
    daemon._reaction_controllers["s-two"] = old_rc

    await daemon._handle_thread_reply(
        {"channel": "C1", "text": "next prompt", "ts": "3.0"}, "1.0",
    )
    await asyncio.sleep(0)

    old_rc.finalize.assert_called_once()
    assert daemon._reaction_controllers["s-two"] is not old_rc


# ── F2: ProcessPool terminate/replace race and -15 handling ──


async def _fake_claude_process(session_id: str = "sid") -> ClaudeProcess:
    proc = await asyncio.create_subprocess_exec(
        "sleep", "30",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return ClaudeProcess(session_id=session_id, process=proc)


async def test_terminated_process_does_not_fire_on_exit() -> None:
    """terminate() marks the exit intentional; the reader's finally must not
    invoke on_exit (which would IDLE the session and post an error)."""
    pool = ProcessPool()
    cp = await _fake_claude_process()
    pool._processes["sid"] = cp

    exits: list[tuple[str, int | None]] = []

    async def on_exit(sid: str, rc: int | None) -> None:
        exits.append((sid, rc))

    cp._reader_task = asyncio.create_task(pool._read_stdout(cp, None, on_exit))
    await asyncio.sleep(0.05)
    await pool.terminate("sid")
    # Reader task may have been cancelled — give the finally a beat to run.
    try:
        await asyncio.wait_for(cp._reader_task, timeout=2)
    except (asyncio.CancelledError, Exception):
        pass
    await asyncio.sleep(0.05)

    assert exits == []


async def test_reader_pop_is_identity_guarded() -> None:
    """The old reader's finally must not evict a replacement process that was
    registered under the same session key."""
    pool = ProcessPool()
    old = await _fake_claude_process()
    pool._processes["sid"] = old

    old._reader_task = asyncio.create_task(pool._read_stdout(old, None, None))
    await asyncio.sleep(0.05)

    # Replacement registered under the same key (what start() does).
    new = await _fake_claude_process()
    pool._processes["sid"] = new

    await old.terminate()
    try:
        await asyncio.wait_for(old._reader_task, timeout=2)
    except (asyncio.CancelledError, Exception):
        pass
    await asyncio.sleep(0.05)

    assert pool._processes.get("sid") is new

    await new.terminate()


async def test_sigterm_exit_not_reported_as_error(config: BridgeConfig) -> None:
    """asyncio reports SIGTERM death as rc=-15 (not shell's 143). That's a
    clean stop: no error emoji, no 'exited unexpectedly' message."""
    daemon = _daemon_with_slack(config)
    daemon._session_mgr.create(
        session_id="s-term", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.PROCESS,
    )
    rc_ctrl = MagicMock()
    rc_ctrl.finalize = AsyncMock()
    daemon._progress["s-term"] = {"_reactions": rc_ctrl}

    await daemon._on_process_exit("s-term", -15)
    await asyncio.sleep(0)

    rc_ctrl.finalize.assert_called_once_with(error=False)
    daemon._slack.post_text.assert_not_awaited()


# ── F3: owner_user_id DM binding ──


async def test_lazy_bind_prefers_owner_dm(config: BridgeConfig) -> None:
    config.owner_user_id = "U_OWNER"
    daemon = _daemon_with_slack(config)
    daemon._slack.web.conversations_open = AsyncMock(
        return_value={"channel": {"id": "D_OWNER"}}
    )
    daemon._slack.web.conversations_list = AsyncMock(
        return_value={"channels": [{"id": "D_SOMEONE_ELSE"}]}
    )
    session = daemon._register_session("s-own", "/tmp")

    assert await daemon._ensure_slack_thread(session) is True
    assert session.channel_id == "D_OWNER"
    daemon._slack.web.conversations_open.assert_awaited_once_with(users="U_OWNER")
    daemon._slack.web.conversations_list.assert_not_awaited()


async def test_lazy_bind_falls_back_without_owner(config: BridgeConfig) -> None:
    daemon = _daemon_with_slack(config)
    daemon._slack.web.conversations_list = AsyncMock(
        return_value={"channels": [{"id": "D_FIRST"}]}
    )
    session = daemon._register_session("s-fall", "/tmp")

    assert await daemon._ensure_slack_thread(session) is True
    assert session.channel_id == "D_FIRST"


# ── F4: watcher poll loop survives callback exceptions ──


async def test_poll_loop_survives_callback_exception(tmp_path: Path, monkeypatch) -> None:
    """One session's failing dispatch must not kill JSONL sync for others."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = "/proj"
    proj_dir = tmp_path / ".claude" / "projects" / "-proj"
    proj_dir.mkdir(parents=True)

    def write_line(sid: str, text: str) -> None:
        with open(proj_dir / f"{sid}.jsonl", "a") as f:
            f.write(json.dumps(
                {"type": "assistant", "message": {"content": text}, "timestamp": "t"}
            ) + "\n")

    delivered: list[tuple[str, str]] = []

    async def on_new(sid: str, msgs) -> None:
        if sid == "bad":
            raise RuntimeError("slack exploded")
        for m in msgs:
            delivered.append((sid, m.text))

    parser = ConversationParser()
    watcher = SessionFileWatcher(parser, on_new_messages=on_new)
    # Files must exist before watch() so start-offset skipping is exercised
    # deterministically (watch skips pre-existing content).
    (proj_dir / "bad.jsonl").touch()
    (proj_dir / "good.jsonl").touch()
    watcher.watch("bad", cwd)
    watcher.watch("good", cwd)
    try:
        # "bad" gets a message first → its callback raises.
        write_line("bad", "boom")
        await asyncio.sleep(0.7)
        # Poll task must still be alive to deliver "good"'s message.
        write_line("good", "hello")
        await asyncio.sleep(0.7)
        assert ("good", "hello") in delivered
        assert not watcher._task.done()
    finally:
        watcher.stop()


# ── F5: thread-hijack prevention ──


def test_create_does_not_steal_active_sessions_thread(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path / "sessions.json")
    mgr.create("owner", "tui", "C1", "t1", SessionMode.HOOK)

    hijacker = mgr.create("hijacker", "slack", "C1", "t1", SessionMode.PROCESS)

    # Replies to (C1, t1) still route to the original owner.
    found = mgr.find_by_thread("C1", "t1")
    assert found is not None and found.session_id == "owner"
    # The new session still exists and is usable.
    assert mgr.get("hijacker") is hijacker


def test_create_rebinds_thread_of_archived_owner(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path / "sessions.json")
    mgr.create("old", "tui", "C1", "t1", SessionMode.HOOK)
    mgr.archive("old")

    mgr.create("fresh", "slack", "C1", "t1", SessionMode.PROCESS)
    found = mgr.find_by_thread("C1", "t1")
    assert found is not None and found.session_id == "fresh"


def test_create_same_id_preserves_accumulated_state(tmp_path: Path) -> None:
    """Re-creating an existing session_id must keep cwd/origin/tmux pane."""
    mgr = SessionManager(tmp_path / "sessions.json")
    s = mgr.create("sid", "name", "C1", "t1", SessionMode.HOOK)
    s.cwd = "/work/proj"
    s.origin = "tui"
    s.tmux_pane_id = "%7"

    s2 = mgr.create("sid", "renamed", "C1", "t2", SessionMode.PROCESS)

    assert s2 is s  # same object, not a replacement
    assert s2.cwd == "/work/proj"
    assert s2.origin == "tui"
    assert s2.tmux_pane_id == "%7"
    assert s2.thread_ts == "t2"
    # Old thread key released, new one bound.
    assert mgr.find_by_thread("C1", "t1") is None
    found = mgr.find_by_thread("C1", "t2")
    assert found is not None and found.session_id == "sid"


def test_archive_of_loser_does_not_drop_owners_index(tmp_path: Path) -> None:
    """Archiving the session that LOST the bind race must not remove the
    winner's index entry."""
    mgr = SessionManager(tmp_path / "sessions.json")
    mgr.create("owner", "tui", "C1", "t1", SessionMode.HOOK)
    mgr.create("loser", "slack", "C1", "t1", SessionMode.PROCESS)

    mgr.archive("loser")

    found = mgr.find_by_thread("C1", "t1")
    assert found is not None and found.session_id == "owner"


def test_rebuild_index_prefers_earliest_created(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    mgr = SessionManager(path)
    first = mgr.create("first", "a", "C1", "t1", SessionMode.HOOK)
    first.created_at = 100.0
    second = mgr.create("second", "b", "C2", "t9", SessionMode.HOOK)
    # Simulate legacy corrupt state where both point at the same thread.
    second.channel_id, second.thread_ts = "C1", "t1"
    second.created_at = 200.0
    mgr._save()

    reloaded = SessionManager(path)
    found = reloaded.find_by_thread("C1", "t1")
    assert found is not None and found.session_id == "first"


# ── F7: extract_options ──


def test_extract_options_trailing_still_works() -> None:
    cleaned, choices = extract_options("Answer.\n[OPTIONS: A | B]")
    assert choices == ["A", "B"]
    assert cleaned == "Answer."


def test_extract_options_mid_text_does_not_truncate() -> None:
    text = (
        "You can end a reply with\n"
        "[OPTIONS: yes | no]\n"
        "to show buttons.\n\n"
        "That is the whole trick."
    )
    cleaned, choices = extract_options(text)
    assert choices == []
    assert cleaned == text  # nothing dropped


def test_extract_options_takes_last_when_multiple() -> None:
    text = "See marker\n[OPTIONS: a | b]\nexplained above.\n[OPTIONS: c | d]"
    cleaned, choices = extract_options(text)
    assert choices == ["c", "d"]
    assert "explained above." in cleaned
    assert "[OPTIONS: a | b]" in cleaned  # mid-text marker left alone


# ── F8/F9: conversation parser ──


def _write_jsonl(dirpath: Path, sid: str, records: list[dict]) -> None:
    with open(dirpath / f"{sid}.jsonl", "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_parser_emits_all_blocks_of_one_record(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / ".claude" / "projects" / "-proj"
    proj.mkdir(parents=True)
    _write_jsonl(proj, "sid", [{
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "Let me check two files."},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}},
            {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "b.py"}},
        ]},
        "timestamp": "ts",
    }])

    parser = ConversationParser()
    msgs = parser.parse_incremental("sid", "/proj")

    assert [m.role for m in msgs] == ["assistant", "tool_use", "tool_use"]
    assert msgs[1].tool_input["file_path"] == "a.py"
    assert msgs[2].tool_input["file_path"] == "b.py"


def test_parser_memory_stays_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / ".claude" / "projects" / "-proj"
    proj.mkdir(parents=True)
    records = [
        {"type": "assistant", "message": {"content": f"msg {i}"}, "timestamp": "t"}
        for i in range(_MAX_KEPT_MESSAGES + 300)
    ]
    _write_jsonl(proj, "sid", records)

    parser = ConversationParser()
    parser.parse_incremental("sid", "/proj")

    kept = parser.get_all_messages("sid")
    assert len(kept) == _MAX_KEPT_MESSAGES
    # The tail is what's kept — the last turn is always intact.
    assert kept[-1].text == f"msg {_MAX_KEPT_MESSAGES + 299}"


# ── F10: decode_project_dir with dots ──


def test_decode_project_dir_reconstructs_dots(tmp_path: Path) -> None:
    target = tmp_path / "my.project"
    target.mkdir()
    encoded = str(target).replace("/", "-").replace(".", "-")
    assert decode_project_dir(encoded) == str(target)


def test_decode_project_dir_plain_path(tmp_path: Path) -> None:
    target = tmp_path / "plain" / "proj"
    target.mkdir(parents=True)
    encoded = str(target).replace("/", "-")
    assert decode_project_dir(encoded) == str(target)


# ── F11: quoted .env values ──


def test_env_tokens_quotes_stripped(tmp_config_dir: Path) -> None:
    (tmp_config_dir / ".env").write_text(
        'SLACK_APP_TOKEN="xapp-quoted"\n'
        "SLACK_BOT_TOKEN='xoxb-quoted'\n"
    )
    cfg = load_config(tmp_config_dir)
    assert cfg.slack_app_token == "xapp-quoted"
    assert cfg.slack_bot_token == "xoxb-quoted"


def test_env_tokens_unquoted_unchanged(tmp_config_dir: Path) -> None:
    (tmp_config_dir / ".env").write_text(
        "SLACK_APP_TOKEN=xapp-plain\nSLACK_BOT_TOKEN=xoxb-plain\n"
    )
    cfg = load_config(tmp_config_dir)
    assert cfg.slack_app_token == "xapp-plain"
    assert cfg.slack_bot_token == "xoxb-plain"


# ── F12: options click blocked while approval pending ──


async def test_options_click_blocked_while_approval_pending(config: BridgeConfig) -> None:
    daemon = _daemon_with_slack(config)
    daemon._session_mgr.create(
        session_id="s-opt", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    daemon._pending_approval_msgs["s-opt"] = "ts.approval"
    daemon._resume_process = AsyncMock()

    await daemon._handle_interactive(
        action={"action_id": "options_choice_0", "value": "Option A"},
        payload={"channel": {"id": "C1"}, "message": {"ts": "5.0", "thread_ts": "1.0"}},
    )

    daemon._resume_process.assert_not_awaited()
    # User got the reminder instead.
    posted = " ".join(str(c.args[1]) for c in daemon._slack.post_text.await_args_list)
    assert "approval" in posted.lower()


# ── AskUserQuestion: question rendered as option buttons, answer round-trips ──

_ASK_INPUT = {
    "questions": [{
        "question": "Merge or PR?",
        "header": "Finish work",
        "multiSelect": False,
        "options": [
            {"label": "Merge to main locally", "description": "merge + cleanup"},
            {"label": "Push and create a PR", "description": "keep worktree"},
        ],
    }]
}


def _stub_instant_answer(daemon, label_idx: int = 0):
    """Make approval waits resolve instantly with an 'answered' decision.

    Mirrors the button-click handler's updatedInput shape: `answers` maps
    question text -> chosen label (the shape CC actually consumes).
    """
    original_create = daemon._approval_mgr.create

    def _fake_create(request_id, **kwargs):
        state = original_create(request_id, **kwargs)
        questions = json.loads(json.dumps(_ASK_INPUT))["questions"]
        label = questions[0]["options"][label_idx]["label"]
        state.resolve("answered", answered_input={
            "questions": questions,
            "answers": {questions[0]["question"]: label},
        })
        return state

    daemon._approval_mgr.create = _fake_create


async def test_ask_user_question_posts_option_buttons(config: BridgeConfig) -> None:
    """AskUserQuestion must render the question + one button per option —
    not the generic 🔐 Approve/Trust/YOLO/Reject card."""
    from claude_slack_bridge.daemon_http import create_http_app
    from aiohttp.test_utils import TestServer, TestClient

    daemon = _daemon_with_slack(config)
    daemon._session_mgr.create(
        session_id="s-ask", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    daemon.set_mute_level("s-ask", "summary")
    _stub_instant_answer(daemon)

    app = create_http_app(daemon)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/permission-request", json={
            "session_key": "s-ask", "tool_name": "AskUserQuestion",
            "tool_input": _ASK_INPUT, "cwd": "/tmp",
        })
        assert resp.status == 200
        body = await resp.json()

    assert body["decision"] == "answered"
    assert body["updated_input"]["answers"] == {"Merge or PR?": "Merge to main locally"}

    blocks = daemon._slack.post_blocks.await_args.args[1]
    rendered = json.dumps(blocks)
    assert "Merge or PR?" in rendered
    assert "ask_option_0" in rendered and "ask_option_1" in rendered
    # The TUI's free-text row has a Slack counterpart: numbered after the
    # real options (here *3.*), pointing at thread reply.
    assert "*3.*" in rendered and "Type something" in rendered
    assert "reply in this thread" in rendered
    # It is NOT the permission card.
    assert "Approve" not in rendered and "YOLO" not in rendered


async def test_ask_user_question_skips_yolo_fast_path(config: BridgeConfig) -> None:
    """YOLO auto-approve must NOT swallow a question — 'approved' makes CC
    proceed with an empty answer, discarding the user's decision."""
    from claude_slack_bridge.daemon_http import create_http_app
    from aiohttp.test_utils import TestServer, TestClient

    daemon = _daemon_with_slack(config)
    daemon._session_mgr.create(
        session_id="s-yolo-q", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    daemon.set_mute_level("s-yolo-q", "summary")
    daemon._trusted_sessions.add("s-yolo-q")
    _stub_instant_answer(daemon, label_idx=1)

    app = create_http_app(daemon)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/permission-request", json={
            "session_key": "s-yolo-q", "tool_name": "AskUserQuestion",
            "tool_input": _ASK_INPUT, "cwd": "/tmp",
        })
        body = await resp.json()

    assert body["decision"] == "answered"
    assert body["updated_input"]["answers"] == {"Merge or PR?": "Push and create a PR"}


async def test_ask_user_question_multiselect_falls_through_to_tui(config: BridgeConfig) -> None:
    """Shapes Slack buttons can't express (multiSelect, several questions)
    hand back to the TUI dialog (empty body) instead of guessing."""
    from claude_slack_bridge.daemon_http import create_http_app
    from aiohttp.test_utils import TestServer, TestClient

    daemon = _daemon_with_slack(config)
    daemon._session_mgr.create(
        session_id="s-multi", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    daemon.set_mute_level("s-multi", "summary")

    multi = json.loads(json.dumps(_ASK_INPUT))
    multi["questions"][0]["multiSelect"] = True

    app = create_http_app(daemon)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/permission-request", json={
            "session_key": "s-multi", "tool_name": "AskUserQuestion",
            "tool_input": multi, "cwd": "/tmp",
        })
        assert resp.status == 200
        assert (await resp.text()) == ""
    daemon._slack.post_blocks.assert_not_awaited()


async def test_ask_option_click_resolves_answered(config: BridgeConfig) -> None:
    """Clicking an option button resolves the pending state with the chosen
    label wrapped in updatedInput shape."""
    daemon = _daemon_with_slack(config)
    state = daemon._approval_mgr.create(
        "req-ask", tool_name="AskUserQuestion", tool_input=_ASK_INPUT,
        session_id="s1",
    )

    await daemon._handle_interactive(
        action={"action_id": "ask_option_1", "value": "req-ask:1"},
        payload={"channel": {"id": "C1"}, "message": {"ts": "9.0"}},
    )

    assert state.status == "answered"
    # `answers` maps question text -> chosen label — the shape CC's
    # AskUserQuestion schema consumes (verified live: `selectedOptions`
    # is silently ignored and produces "did not answer").
    assert state.answered_input["answers"] == {"Merge or PR?": "Push and create a PR"}
    assert state.answered_input["questions"] == _ASK_INPUT["questions"]
    # Card was replaced with the chosen answer.
    call = daemon._slack.web.chat_update.await_args
    assert "Push and create a PR" in call.kwargs["text"]


async def test_ask_option_click_after_timeout_marks_expired(config: BridgeConfig) -> None:
    daemon = _daemon_with_slack(config)
    # No pending state — simulates timeout/cleanup already having run.
    await daemon._handle_interactive(
        action={"action_id": "ask_option_0", "value": "req-gone:0"},
        payload={"channel": {"id": "C1"}, "message": {"ts": "9.0"}},
    )
    call = daemon._slack.web.chat_update.await_args
    assert "expired" in call.kwargs["text"].lower()


async def test_ask_user_question_timeout_falls_through_to_tui(config: BridgeConfig) -> None:
    """No click before timeout → empty body (TUI dialog), not a decision."""
    from claude_slack_bridge.daemon_http import create_http_app
    from aiohttp.test_utils import TestServer, TestClient

    daemon = _daemon_with_slack(config)
    daemon._session_mgr.create(
        session_id="s-tmo", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    daemon.set_mute_level("s-tmo", "summary")
    daemon._config.approval_timeout_secs = 0.05

    app = create_http_app(daemon)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/permission-request", json={
            "session_key": "s-tmo", "tool_name": "AskUserQuestion",
            "tool_input": _ASK_INPUT, "cwd": "/tmp",
        })
        assert resp.status == 200
        assert (await resp.text()) == ""


async def test_thread_reply_answers_pending_question_as_free_text(config: BridgeConfig) -> None:
    """While a question is pending, a plain thread reply is the free-text
    answer (Slack counterpart of the TUI's "Type something" row): resolve
    with `response`, don't forward the reply as a new prompt."""
    daemon = _daemon_with_slack(config)
    daemon._session_mgr.create(
        session_id="s-ft", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    state = daemon._approval_mgr.create(
        "req-ft", tool_name="AskUserQuestion", tool_input=_ASK_INPUT,
        session_id="s-ft",
    )
    daemon._pending_questions["s-ft"] = "req-ft"
    daemon._pending_approval_msgs["s-ft"] = "ts.q"
    daemon._resume_process = AsyncMock()

    await daemon._handle_thread_reply(
        {"channel": "C1", "text": "actually, rebase onto develop instead", "ts": "2.0"},
        "1.0",
    )

    assert state.status == "answered"
    assert state.answered_input["response"] == "actually, rebase onto develop instead"
    assert state.answered_input["questions"] == _ASK_INPUT["questions"]
    # Not forwarded as a prompt, no reaction controller armed.
    daemon._resume_process.assert_not_awaited()
    assert "s-ft" not in daemon._reaction_controllers
    # Card updated with the typed answer.
    call = daemon._slack.web.chat_update.await_args
    assert "rebase onto develop" in call.kwargs["text"]


async def test_thread_reply_with_stale_question_marker_forwards_normally(
    config: BridgeConfig,
) -> None:
    """A leftover pending-question marker (state already cleaned up) must not
    swallow the reply — it should clear the marker and forward as usual."""
    daemon = _daemon_with_slack(config)
    session = daemon._session_mgr.create(
        session_id="s-stale", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    session.origin = "slack"
    daemon._pending_questions["s-stale"] = "req-gone"  # no matching state
    daemon._pool.start = AsyncMock()

    await daemon._handle_thread_reply(
        {"channel": "C1", "text": "continue please", "ts": "2.0"}, "1.0",
    )

    assert "s-stale" not in daemon._pending_questions
    daemon._pool.start.assert_awaited()  # forwarded as a normal prompt


# ── 2026-07-15 wrong-binding incident: bind cross-check against hook activity ──


async def test_bind_corrects_to_actively_hooking_session(config: BridgeConfig) -> None:
    """If the requested session never hooked but another session is actively
    hooking from the SAME tmux pane, that session is this TUI — bind it and
    report the correction (discovery scripts can guess wrong)."""
    import time as _t
    from claude_slack_bridge.daemon_http import create_http_app
    from aiohttp.test_utils import TestServer, TestClient

    daemon = _daemon_with_slack(config)
    daemon._slack.web.conversations_list = AsyncMock(
        return_value={"channels": [{"id": "D1"}]}
    )
    # The REAL session: registered by hooks, actively hooking from pane %17.
    real = daemon._register_session("real-sid", "/proj")
    real.tmux_pane_id = "%17"
    real.tui_active = _t.time()

    app = create_http_app(daemon)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/sessions/bind", json={
            "session_id": "guessed-sid",  # wrong id from a discovery fallback
            "name": "TUI-guessed",
            "cwd": "/proj",
            "tmux_pane_id": "%17",
        })
        assert resp.status == 200
        body = await resp.json()

    assert body["session_id"] == "real-sid"
    assert body["corrected_from"] == "guessed-sid"
    # The real session got the Slack thread (mock post_blocks → "ts.blocks").
    assert daemon._session_mgr.get("real-sid").thread_ts == "ts.blocks"


async def test_bind_trusts_requested_id_when_it_has_hooked(config: BridgeConfig) -> None:
    """No correction when the requested session itself has hook activity —
    even if another session shares the pane (e.g. a previous session in the
    same tmux window)."""
    import time as _t
    from claude_slack_bridge.daemon_http import create_http_app
    from aiohttp.test_utils import TestServer, TestClient

    daemon = _daemon_with_slack(config)
    daemon._slack.web.conversations_list = AsyncMock(
        return_value={"channels": [{"id": "D1"}]}
    )
    requested = daemon._register_session("requested-sid", "/proj")
    requested.tmux_pane_id = "%17"
    requested.tui_active = _t.time()
    other = daemon._register_session("other-sid", "/proj")
    other.tmux_pane_id = "%17"
    other.tui_active = _t.time() - 100

    app = create_http_app(daemon)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/sessions/bind", json={
            "session_id": "requested-sid", "name": "TUI-requested",
            "cwd": "/proj", "tmux_pane_id": "%17",
        })
        body = await resp.json()

    assert body["session_id"] == "requested-sid"
    assert "corrected_from" not in body


async def test_bind_without_pane_never_corrects(config: BridgeConfig) -> None:
    """No tmux pane info → no cross-check basis → trust the request as before."""
    import time as _t
    from claude_slack_bridge.daemon_http import create_http_app
    from aiohttp.test_utils import TestServer, TestClient

    daemon = _daemon_with_slack(config)
    daemon._slack.web.conversations_list = AsyncMock(
        return_value={"channels": [{"id": "D1"}]}
    )
    hooked = daemon._register_session("hooked-sid", "/proj")
    hooked.tmux_pane_id = "%17"
    hooked.tui_active = _t.time()

    app = create_http_app(daemon)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/sessions/bind", json={
            "session_id": "fresh-sid", "name": "TUI-fresh", "cwd": "/proj",
            "tmux_pane_id": "",
        })
        body = await resp.json()

    assert body["session_id"] == "fresh-sid"
    assert "corrected_from" not in body


def test_sync_command_markdowns_have_no_jsonl_fallback() -> None:
    """The resolver contract forbids newest-jsonl fallbacks (they bind the
    wrong session). Pin that no sync command markdown reintroduces one, and
    that bind-calling commands adopt the daemon's corrected session id."""
    commands = Path(__file__).resolve().parent.parent / "plugins" / "slack-bridge" / "commands"
    for name in ("sync-on.md", "sync-summary.md", "sync-ring.md", "sync-off.md"):
        text = (commands / name).read_text()
        assert "ls -t" not in text, f"{name}: newest-jsonl fallback is forbidden"
        assert ".jsonl" not in text, f"{name}: jsonl heuristics are forbidden"
        # Resolver stderr must not be discarded — it's the bug report.
        assert "claude-slack-bridge-session-id\" \"$PWD\" 2>/dev/null" not in text, (
            f"{name}: resolver diagnostics must not be silenced"
        )
    for name in ("sync-on.md", "sync-summary.md"):
        text = (commands / name).read_text()
        assert "BOUND_ID" in text, f"{name}: must adopt daemon-corrected session id"
    # CLAUDE_CODE_SESSION_ID (documented, exported into every Bash tool
    # subprocess) is the authoritative id source; the pid-walk resolver is
    # only the fallback for older CC versions.
    for name in ("sync-on.md", "sync-summary.md", "sync-ring.md", "sync-off.md"):
        text = (commands / name).read_text()
        assert "CLAUDE_CODE_SESSION_ID" in text, (
            f"{name}: must prefer the documented env var over pid-walking"
        )
        env_pos = text.index("CLAUDE_CODE_SESSION_ID")
        resolver_pos = text.index("claude-slack-bridge-session-id")
        assert env_pos < resolver_pos, f"{name}: env var must be tried first"
