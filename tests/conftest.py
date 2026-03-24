from pathlib import Path

import pytest

from claude_slack_bridge.approval import ApprovalManager
from claude_slack_bridge.config import BridgeConfig
from claude_slack_bridge.registry import SessionRegistry


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Temporary config directory for tests."""
    config_dir = tmp_path / "slack-bridge"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def bridge_config(tmp_config_dir: Path) -> BridgeConfig:
    return BridgeConfig(
        config_dir=tmp_config_dir,
        require_approval=False,
        default_channel="C12345",
    )


@pytest.fixture
def bridge_registry(tmp_config_dir: Path) -> SessionRegistry:
    return SessionRegistry(storage_path=tmp_config_dir / "sessions.json")


@pytest.fixture
def bridge_approval() -> ApprovalManager:
    return ApprovalManager()
