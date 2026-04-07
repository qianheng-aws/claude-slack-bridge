"""Tmux controller for sending input to Claude Code TUI sessions.

Channel 3: Slack → tmux send-keys → Claude Code TUI

Finds the tmux pane running a Claude Code session by matching PID
or working directory, then sends keystrokes via tmux send-keys.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("claude_slack_bridge")


@dataclass
class TmuxTarget:
    session: str
    window: int
    pane: int

    @property
    def target_string(self) -> str:
        return f"{self.session}:{self.window}.{self.pane}"

    @classmethod
    def from_string(cls, s: str) -> Optional["TmuxTarget"]:
        m = re.match(r"^(.+):(\d+)\.(\d+)$", s)
        if not m:
            return None
        return cls(session=m.group(1), window=int(m.group(2)), pane=int(m.group(3)))


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


async def find_target_by_cwd(cwd: str) -> Optional[TmuxTarget]:
    """Find tmux pane by working directory."""
    tmux = await find_tmux()
    if not tmux:
        return None
    output = await _run(tmux, "list-panes", "-a", "-F",
                        "#{session_name}:#{window_index}.#{pane_index} #{pane_current_path}")
    if not output:
        return None
    for line in output.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].strip() == cwd:
            return TmuxTarget.from_string(parts[0])
    return None


async def find_target_by_command(command: str = "claude") -> Optional[TmuxTarget]:
    """Find tmux pane running a specific command."""
    tmux = await find_tmux()
    if not tmux:
        return None
    output = await _run(tmux, "list-panes", "-a", "-F",
                        "#{session_name}:#{window_index}.#{pane_index} #{pane_current_command}")
    if not output:
        return None
    for line in output.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].strip() == command:
            return TmuxTarget.from_string(parts[0])
    return None


async def send_keys(target: TmuxTarget, text: str, press_enter: bool = True) -> bool:
    """Send text to a tmux pane via send-keys."""
    tmux = await find_tmux()
    if not tmux:
        return False
    # Send text literally (-l flag)
    result = await _run(tmux, "send-keys", "-t", target.target_string, "-l", text)
    if result is None:
        return False
    if press_enter:
        await _run(tmux, "send-keys", "-t", target.target_string, "Enter")
    return True


async def send_message_to_session(cwd: str, text: str) -> bool:
    """Find the Claude TUI pane for a session and send a message."""
    # Try by cwd first, then by command name
    target = await find_target_by_cwd(cwd)
    if not target:
        target = await find_target_by_command("claude")
    if not target:
        logger.warning("No tmux pane found for cwd=%s", cwd)
        return False
    logger.info("Sending to tmux %s: %s", target.target_string, text[:50])
    return await send_keys(target, text)
