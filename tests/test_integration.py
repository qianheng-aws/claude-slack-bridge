import json
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient

from claude_slack_bridge.http_api import create_app
from claude_slack_bridge.config import BridgeConfig


@pytest.fixture
def slack_mock() -> AsyncMock:
    mock = AsyncMock()
    mock.post_blocks = AsyncMock(return_value="1234.5678")
    mock.update_blocks = AsyncMock()
    mock.post_text = AsyncMock(return_value="1234.5678")
    return mock


@pytest.fixture
def app(bridge_config, bridge_registry, bridge_approval, slack_mock):
    return create_app(bridge_config, bridge_registry, bridge_approval, slack_mock)


@pytest.fixture
async def client(aiohttp_client, app) -> TestClient:
    return await aiohttp_client(app)


async def test_full_tool_lifecycle(client: TestClient, slack_mock: AsyncMock) -> None:
    """Simulate: user prompt -> pre-tool (auto-approved) -> post-tool -> stop."""
    base = {"session_key": "integration-test", "cwd": "/workplace/test-project"}

    # 1. User prompt
    resp = await client.post("/hooks/user-prompt", json={
        **base, "event": "user-prompt", "prompt": "Fix the bug"
    })
    assert resp.status == 200

    # 2. Pre-tool (auto-approved since require_approval=False)
    resp = await client.post("/hooks/pre-tool-use", json={
        **base, "event": "pre-tool-use", "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/test.py"},
    })
    assert resp.status == 200
    text = await resp.text()
    assert text == "approved"

    # 3. Post-tool
    resp = await client.post("/hooks/post-tool-use", json={
        **base, "event": "post-tool-use", "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/test.py"},
        "tool_output": "file contents here", "duration_ms": 50,
    })
    assert resp.status == 200

    # 4. Stop
    resp = await client.post("/hooks/stop", json={
        **base, "event": "stop", "response": "I've read the file.",
    })
    assert resp.status == 200

    # Verify Slack messages were sent (session header + 4 events)
    assert slack_mock.post_blocks.call_count >= 4
