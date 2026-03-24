from __future__ import annotations

import logging
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)


class SlackClient:
    """Thin async wrapper around Slack Web API."""

    def __init__(self, bot_token: str) -> None:
        self._web = AsyncWebClient(token=bot_token)

    @property
    def web(self) -> AsyncWebClient:
        return self._web

    async def post_blocks(
        self,
        channel: str,
        blocks: list[dict],
        text: str = "",
        thread_ts: str | None = None,
    ) -> str:
        """Post a Block Kit message. Returns message ts."""
        kwargs: dict[str, Any] = {
            "channel": channel,
            "blocks": blocks,
            "text": text or "Claude Code Bridge",
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = await self._web.chat_postMessage(**kwargs)
        return resp["ts"]

    async def update_blocks(
        self,
        channel: str,
        ts: str,
        blocks: list[dict],
        text: str = "",
    ) -> None:
        """Update an existing message."""
        await self._web.chat_update(
            channel=channel,
            ts=ts,
            blocks=blocks,
            text=text or "Claude Code Bridge",
        )

    async def post_text(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> str:
        """Post a plain text message. Returns message ts."""
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = await self._web.chat_postMessage(**kwargs)
        return resp["ts"]

    async def create_channel(self, name: str) -> tuple[str, bool]:
        """Create a channel. Returns (channel_id, created).
        If channel already exists, returns (existing_id, False).
        """
        try:
            resp = await self._web.conversations_create(name=name)
            return resp["channel"]["id"], True
        except Exception as e:
            if "name_taken" in str(e):
                resp = await self._web.conversations_list(types="public_channel")
                for ch in resp.get("channels", []):
                    if ch["name"] == name:
                        return ch["id"], False
            raise

    async def archive_channel(self, channel_id: str) -> None:
        """Archive a channel."""
        try:
            await self._web.conversations_archive(channel=channel_id)
        except Exception:
            logger.warning("Failed to archive channel %s", channel_id, exc_info=True)

    async def auth_test(self) -> dict:
        """Validate token. Returns auth info."""
        resp = await self._web.auth_test()
        return dict(resp)
