from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("claude_slack_bridge")


class SessionMode(str, Enum):
    PROCESS = "process"  # daemon manages claude --print
    HOOK = "hook"        # TUI active, hooks push to Slack
    IDLE = "idle"        # neither active


@dataclass
class Session:
    session_id: str  # Claude Code UUID
    session_name: str
    channel_id: str
    thread_ts: str | None
    mode: str = SessionMode.IDLE.value
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    status: str = "active"
    cwd: str = ""  # working directory for claude processes
    tui_active: float = 0  # timestamp of last TUI hook activity
    origin: str = ""  # "slack" (from mention/DM) or "tui" (from bind/hooks)
    tmux_pane_id: str = ""  # tmux pane_id (e.g. "%7") for precise send-keys targeting

    def touch(self) -> None:
        self.last_active = time.time()


class SessionManager:
    def __init__(self, storage_path: Path) -> None:
        self._path = storage_path
        self._sessions: dict[str, Session] = {}
        # Reverse index: (channel_id, thread_ts) -> session_id
        self._thread_index: dict[tuple[str, str], str] = {}
        if self._path.is_file():
            self._load()

    def create(
        self,
        session_id: str,
        session_name: str,
        channel_id: str,
        thread_ts: str | None,
        mode: SessionMode = SessionMode.PROCESS,
    ) -> Session:
        s = self._sessions.get(session_id)
        if s is not None:
            # Re-create with an existing ID: refresh routing fields but keep
            # accumulated state (cwd, origin, tmux_pane_id). Replacing the
            # object wholesale would silently drop those and break tmux
            # forwarding / cwd-based JSONL reads for the session.
            if s.thread_ts and self._thread_index.get((s.channel_id, s.thread_ts)) == session_id:
                self._thread_index.pop((s.channel_id, s.thread_ts), None)
            s.session_name = session_name
            s.channel_id = channel_id
            s.thread_ts = thread_ts
            s.mode = mode.value
            s.status = "active"
            s.touch()
        else:
            s = Session(
                session_id=session_id,
                session_name=session_name,
                channel_id=channel_id,
                thread_ts=thread_ts,
                mode=mode.value,
            )
            self._sessions[session_id] = s
        if thread_ts:
            # Never steal a thread that an *active* session already owns —
            # a reply that missed find_by_thread (lost state, race) must not
            # permanently hijack the thread's routing. The new session still
            # exists and can post; replies keep routing to the original owner.
            owner_id = self._thread_index.get((channel_id, thread_ts))
            owner = self._sessions.get(owner_id) if owner_id else None
            if owner_id and owner_id != session_id and owner and owner.status == "active":
                logger.warning(
                    "Thread (%s, %s) already bound to active session %s — "
                    "NOT re-binding to new session %s",
                    channel_id, thread_ts, owner_id, session_id,
                )
            else:
                self._thread_index[(channel_id, thread_ts)] = session_id
        self._save()
        return s

    def get(self, session_id: str) -> Session | None:
        s = self._sessions.get(session_id)
        if s:
            return s
        # Fallback: match by cwd (hooks may send cwd as session_key)
        # When multiple sessions share the same cwd, prefer the most
        # recently active one (highest last_active timestamp).
        best: Session | None = None
        for sess in self._sessions.values():
            if sess.status == "active" and sess.cwd == session_id:
                if best is None or sess.last_active > best.last_active:
                    best = sess
        if best is not None:
            logger.warning(
                "Session lookup for %r matched via cwd fallback → %s "
                "(cross-session risk if multiple sessions share cwd)",
                session_id, best.session_id,
            )
        return best

    def find_by_thread(self, channel_id: str, thread_ts: str) -> Session | None:
        sid = self._thread_index.get((channel_id, thread_ts))
        if sid:
            return self._sessions.get(sid)
        # Fallback: linear scan
        for s in self._sessions.values():
            if s.status == "active" and s.channel_id == channel_id and s.thread_ts == thread_ts:
                return s
        return None

    def set_mode(self, session_id: str, mode: SessionMode) -> None:
        if s := self._sessions.get(session_id):
            s.mode = mode.value
            s.touch()
            self._save()

    def list_active(self) -> list[Session]:
        return [s for s in self._sessions.values() if s.status == "active"]

    def archive(self, session_id: str) -> None:
        if s := self._sessions.get(session_id):
            s.status = "archived"
            # Only drop the index entry if this session actually owns it —
            # a session that lost the create()-time bind race points at a
            # thread whose index entry belongs to another live session.
            if s.thread_ts and self._thread_index.get((s.channel_id, s.thread_ts)) == session_id:
                self._thread_index.pop((s.channel_id, s.thread_ts), None)
            self._save()

    def _rebuild_index(self) -> None:
        # Earliest-created session wins a contested thread, mirroring
        # create()'s no-steal rule (dict order is not a guarantee here).
        self._thread_index = {}
        for s in sorted(self._sessions.values(), key=lambda s: s.created_at):
            if s.status == "active" and s.thread_ts:
                self._thread_index.setdefault((s.channel_id, s.thread_ts), s.session_id)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {sid: asdict(s) for sid, s in self._sessions.items()}
        self._path.write_text(json.dumps(data, indent=2))

    def _load(self) -> None:
        raw = json.loads(self._path.read_text())
        for sid, d in raw.items():
            # Compat: drop unknown fields, add defaults for new fields
            known = {f.name for f in Session.__dataclass_fields__.values()}
            clean = {k: v for k, v in d.items() if k in known}
            clean.setdefault("mode", SessionMode.IDLE.value)
            self._sessions[sid] = Session(**clean)
        self._rebuild_index()
