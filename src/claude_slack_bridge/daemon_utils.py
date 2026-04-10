"""Daemon utilities — logging setup, event dedup cache, path decoding."""

from __future__ import annotations

import logging
import logging.handlers
import os

from claude_slack_bridge.config import BridgeConfig

logger = logging.getLogger("claude_slack_bridge")


def setup_logging(config: BridgeConfig) -> None:
    """Configure rotating file + stream logging for the daemon."""
    log_dir = config.config_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "daemon.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("claude_slack_bridge")
    root.setLevel(getattr(logging, config.log_level, logging.INFO))
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


class SeenCache:
    """Bounded set remembering recent event IDs to prevent duplicate processing."""

    __slots__ = ("_maxsize", "_seen", "_order")

    def __init__(self, maxsize: int = 5000) -> None:
        self._maxsize = maxsize
        self._seen: set[str] = set()
        self._order: list[str] = []

    def check_and_add(self, key: str) -> bool:
        """Return True if *key* was already seen; otherwise record it."""
        if key in self._seen:
            return True
        self._seen.add(key)
        self._order.append(key)
        if len(self._order) > self._maxsize:
            oldest = self._order.pop(0)
            self._seen.discard(oldest)
        return False


def decode_project_dir(encoded: str) -> str:
    """Decode Claude Code project dir name back to filesystem path.

    Claude encodes /local/home/user as -local-home-user.
    We greedily try to find the longest existing path.
    """
    raw = encoded.lstrip("-")
    parts = raw.split("-")
    best = ""
    _try_paths(parts, 0, "", best_ref := [best])
    return best_ref[0] or ("/" + raw.replace("-", "/"))


def _try_paths(parts: list[str], idx: int, current: str, best: list[str], _depth: int = 0) -> None:
    """Recursively try combining remaining parts with / or - to find existing dirs."""
    if _depth > 20:
        return
    if idx >= len(parts):
        if os.path.isdir(current) and len(current) > len(best[0]):
            best[0] = current
        return
    with_slash = f"{current}/{parts[idx]}"
    _try_paths(parts, idx + 1, with_slash, best, _depth + 1)
    if current and not current.endswith("/"):
        with_dash = f"{current}-{parts[idx]}"
        _try_paths(parts, idx + 1, with_dash, best, _depth + 1)
