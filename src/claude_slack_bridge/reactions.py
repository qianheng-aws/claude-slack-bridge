"""Phase-aware emoji reactions on Slack messages.

Shows changing emoji reactions on the user's message based on what
processing phase Claude is in (queued, thinking, coding, browsing, etc.).
Includes debounce to avoid Slack API spam and stall detection to surface
hung requests.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Phase -> emoji mapping ──

PHASE_EMOJI: dict[str, str] = {
    "queued": "eyes",
    "thinking": "thinking_face",
    "coding": "technologist",
    "browsing": "globe_with_meridians",
    "tool": "wrench",
    "done": "lobster",
    "error": "rotating_light",
}

TERMINAL_PHASES = frozenset({"done", "error"})

# ── Tool -> phase mapping ──

_CODING_TOOLS = frozenset({
    "Bash", "Write", "Edit", "Read", "Glob", "Grep", "NotebookEdit",
})

_WEB_TOOLS = frozenset({
    "WebFetch", "WebSearch",
})

# ── Timing constants ──

_DEBOUNCE_SECS = 0.7
_SOFT_STALL_SECS = 15.0
_HARD_STALL_SECS = 45.0

_STALL_SOFT_EMOJI = "yawning_face"
_STALL_HARD_EMOJI = "cold_sweat"


def tool_to_phase(tool_name: str) -> str:
    """Map a tool name to a reaction phase.

    For MCP tools (prefixed like ``mcp__server__tool``), the base tool name
    after the last ``__`` is used for classification.
    """
    # Normalise MCP-prefixed tool names
    if tool_name.startswith("mcp__") and "__" in tool_name[5:]:
        tool_name = tool_name.rsplit("__", 1)[-1]

    if tool_name in _CODING_TOOLS:
        return "coding"
    if tool_name in _WEB_TOOLS:
        return "browsing"
    return "tool"


class StatusReactionController:
    """Manages phase-aware emoji reactions on a single Slack message.

    Parameters
    ----------
    slack_client:
        Object with ``add_reaction(channel, ts, emoji)`` and
        ``remove_reaction(channel, ts, emoji)`` async methods.
    channel:
        Slack channel ID.
    msg_ts:
        Timestamp of the message to react to.
    loop:
        asyncio event loop for scheduling timers.
    """

    def __init__(
        self,
        slack_client,
        channel: str,
        msg_ts: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._client = slack_client
        self._channel = channel
        self._ts = msg_ts
        self._loop = loop

        self._current_emoji: Optional[str] = None
        self._current_phase: Optional[str] = None
        self._finalized: bool = False

        # Debounce handle for non-terminal phase changes
        self._debounce_handle: Optional[asyncio.TimerHandle] = None

        # Stall detection
        self._soft_stall_handle: Optional[asyncio.TimerHandle] = None
        self._hard_stall_handle: Optional[asyncio.TimerHandle] = None
        self._stall_emoji: Optional[str] = None
        self._stall_paused: bool = False

    # ── Public API ──

    async def set_phase(self, phase: str) -> None:
        """Set the current processing phase.

        Terminal phases (``done``, ``error``) and the initial ``queued``
        phase skip debounce and apply immediately.  All other transitions
        are debounced by 700 ms.
        """
        if self._finalized:
            return

        # Reset stall watchdog on every phase change
        self._reset_stall()

        if phase in TERMINAL_PHASES or self._current_phase is None:
            # Initial or terminal -- apply immediately
            self._cancel_debounce()
            await self._apply_phase(phase)
        else:
            # Debounce non-terminal transitions
            self._cancel_debounce()
            self._debounce_handle = self._loop.call_later(
                _DEBOUNCE_SECS,
                lambda: asyncio.ensure_future(self._apply_phase(phase)),
            )

    def on_progress(self) -> None:
        """Signal that progress was made -- resets the stall watchdog."""
        if self._finalized or self._stall_paused:
            return
        self._reset_stall()

    def pause_stall(self) -> None:
        """Pause stall detection (e.g. while waiting for approval)."""
        self._stall_paused = True
        self._cancel_stall_timers()

    def resume_stall(self) -> None:
        """Resume stall detection."""
        self._stall_paused = False
        self._reset_stall()

    async def finalize(self, error: bool = False) -> None:
        """Cancel all timers and set terminal emoji.

        Idempotent -- safe to call multiple times.
        """
        if self._finalized:
            return
        self._finalized = True

        self._cancel_debounce()
        self._cancel_stall_timers()

        # Remove stall emoji if present
        await self._remove_stall_emoji()

        phase = "error" if error else "done"
        await self._apply_phase(phase)

    # ── Internal ──

    async def _apply_phase(self, phase: str) -> None:
        """Swap the reaction to match *phase*."""
        if self._finalized and phase not in TERMINAL_PHASES:
            return
        emoji = PHASE_EMOJI.get(phase)
        if emoji is None:
            logger.warning("Unknown phase %r, ignoring", phase)
            return

        self._current_phase = phase
        await self._swap_emoji(emoji)

    async def _swap_emoji(self, new_emoji: str) -> None:
        """Remove the old reaction and add the new one.

        If they are the same emoji, skip the swap.
        """
        if new_emoji == self._current_emoji:
            return

        old_emoji = self._current_emoji
        self._current_emoji = new_emoji

        # Remove old (best-effort)
        if old_emoji is not None:
            try:
                await self._client.remove_reaction(
                    self._channel, self._ts, old_emoji,
                )
            except Exception:
                logger.debug(
                    "Failed to remove reaction :%s: from %s/%s",
                    old_emoji, self._channel, self._ts,
                    exc_info=True,
                )

        # Add new (best-effort)
        try:
            await self._client.add_reaction(
                self._channel, self._ts, new_emoji,
            )
        except Exception:
            logger.debug(
                "Failed to add reaction :%s: to %s/%s",
                new_emoji, self._channel, self._ts,
                exc_info=True,
            )

    # ── Stall detection ──

    def _reset_stall(self) -> None:
        """Cancel existing stall timers, remove stall emoji, and restart."""
        self._cancel_stall_timers()

        if self._finalized or self._stall_paused:
            return

        # Remove any existing stall emoji asynchronously
        if self._stall_emoji is not None:
            asyncio.ensure_future(self._remove_stall_emoji())

        self._soft_stall_handle = self._loop.call_later(
            _SOFT_STALL_SECS,
            lambda: asyncio.ensure_future(self._on_soft_stall()),
        )
        self._hard_stall_handle = self._loop.call_later(
            _HARD_STALL_SECS,
            lambda: asyncio.ensure_future(self._on_hard_stall()),
        )

    async def _on_soft_stall(self) -> None:
        if self._finalized or self._stall_paused:
            return
        logger.info(
            "Soft stall detected on %s/%s", self._channel, self._ts,
        )
        await self._add_stall_emoji(_STALL_SOFT_EMOJI)

    async def _on_hard_stall(self) -> None:
        if self._finalized or self._stall_paused:
            return
        logger.warning(
            "Hard stall detected on %s/%s", self._channel, self._ts,
        )
        # Replace soft stall emoji with hard stall emoji
        await self._remove_stall_emoji()
        await self._add_stall_emoji(_STALL_HARD_EMOJI)

    async def _add_stall_emoji(self, emoji: str) -> None:
        """Add a stall indicator emoji (separate from the phase emoji)."""
        self._stall_emoji = emoji
        try:
            await self._client.add_reaction(
                self._channel, self._ts, emoji,
            )
        except Exception:
            logger.debug(
                "Failed to add stall reaction :%s: to %s/%s",
                emoji, self._channel, self._ts,
                exc_info=True,
            )

    async def _remove_stall_emoji(self) -> None:
        """Remove the current stall emoji if any."""
        emoji = self._stall_emoji
        if emoji is None:
            return
        self._stall_emoji = None
        try:
            await self._client.remove_reaction(
                self._channel, self._ts, emoji,
            )
        except Exception:
            logger.debug(
                "Failed to remove stall reaction :%s: from %s/%s",
                emoji, self._channel, self._ts,
                exc_info=True,
            )

    # ── Timer helpers ──

    def _cancel_debounce(self) -> None:
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None

    def _cancel_stall_timers(self) -> None:
        if self._soft_stall_handle is not None:
            self._soft_stall_handle.cancel()
            self._soft_stall_handle = None
        if self._hard_stall_handle is not None:
            self._hard_stall_handle.cancel()
            self._hard_stall_handle = None
