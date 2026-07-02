"""Tmux controller for sending input to Claude Code TUI sessions.

Channel 3: Slack → tmux send-keys → Claude Code TUI

Targets panes by tmux `pane_id` (e.g. `%7`), captured into the session
record via `$TMUX_PANE` at bind/hook time. There is deliberately no
cwd-based fallback: two panes can share a cwd, and if the bound pane
has closed, picking "some other pane in the same cwd" routes the Slack
message to a different session's TUI. Caller is expected to fall back
to `--print` resume when this returns False.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger("claude_slack_bridge")


async def _run(cmd: str, *args: str) -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return None
        return stdout.decode().strip()
    except Exception:
        return None


async def find_tmux() -> Optional[str]:
    """Find tmux binary path."""
    for path in ["/usr/local/bin/tmux", "/usr/bin/tmux"]:
        if os.path.isfile(path):
            return path
    result = await _run("which", "tmux")
    return result if result else None


async def pane_exists(pane_id: str) -> bool:
    """Check whether a tmux pane_id (e.g. '%7') is still live."""
    if not pane_id:
        return False
    tmux = await find_tmux()
    if not tmux:
        return False
    result = await _run(tmux, "display-message", "-p", "-t", pane_id, "#{pane_id}")
    return result == pane_id


async def send_keys_by_pane_id(pane_id: str, text: str, press_enter: bool = True) -> bool:
    """Send text to a tmux pane identified by pane_id (e.g. '%7').

    pane_id is stable across renames/reorders, so this is the preferred
    way to target a specific Claude TUI pane.
    """
    if not pane_id:
        return False
    tmux = await find_tmux()
    if not tmux:
        return False
    if not await pane_exists(pane_id):
        return False
    result = await _run(tmux, "send-keys", "-t", pane_id, "-l", text)
    if result is None:
        return False
    if press_enter:
        await _run(tmux, "send-keys", "-t", pane_id, "Enter")
    return True


async def send_message_to_session(text: str, pane_id: str = "") -> bool:
    """Send a message to the Claude TUI pane identified by pane_id.

    Returns False when pane_id is empty or the pane is gone — the caller
    must fall back to `--print` resume rather than guessing a pane by
    cwd (which can clobber an unrelated session).
    """
    if not pane_id:
        logger.info("No pane_id recorded for session; skipping tmux send")
        return False
    if await send_keys_by_pane_id(pane_id, text):
        logger.info("Sent to tmux pane %s: %s", pane_id, text[:50])
        return True
    logger.info("pane_id %s unavailable (pane gone); skipping tmux send", pane_id)
    return False
