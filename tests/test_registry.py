import time
from pathlib import Path

import pytest

from claude_slack_bridge.registry import SessionMapping, SessionRegistry


@pytest.fixture
def registry(tmp_config_dir: Path) -> SessionRegistry:
    return SessionRegistry(storage_path=tmp_config_dir / "sessions.json")


def test_register_new_session(registry: SessionRegistry) -> None:
    mapping = registry.register(
        session_id="sess-1",
        session_name="my-project",
        channel_id="C123",
        thread_ts="1234567890.123456",
    )
    assert mapping.session_id == "sess-1"
    assert mapping.channel_id == "C123"
    assert mapping.thread_ts == "1234567890.123456"
    assert mapping.is_dedicated_channel is False
    assert mapping.status == "active"


def test_get_existing_session(registry: SessionRegistry) -> None:
    registry.register("sess-1", "proj", "C123", "1234.5678")
    result = registry.get("sess-1")
    assert result is not None
    assert result.session_id == "sess-1"


def test_get_missing_session(registry: SessionRegistry) -> None:
    assert registry.get("nonexistent") is None


def test_touch_updates_last_active(registry: SessionRegistry) -> None:
    registry.register("sess-1", "proj", "C123", "1234.5678")
    before = registry.get("sess-1")
    assert before is not None
    before_timestamp = before.last_active
    time.sleep(0.01)
    registry.touch("sess-1")
    after = registry.get("sess-1")
    assert after is not None
    assert after.last_active > before_timestamp


def test_promote_to_channel(registry: SessionRegistry) -> None:
    registry.register("sess-1", "proj", "C123", "1234.5678")
    registry.promote("sess-1", new_channel_id="C999")
    mapping = registry.get("sess-1")
    assert mapping is not None
    assert mapping.channel_id == "C999"
    assert mapping.thread_ts is None
    assert mapping.is_dedicated_channel is True


def test_archive_session(registry: SessionRegistry) -> None:
    registry.register("sess-1", "proj", "C123", "1234.5678")
    registry.archive("sess-1")
    mapping = registry.get("sess-1")
    assert mapping is not None
    assert mapping.status == "archived"


def test_list_active_sessions(registry: SessionRegistry) -> None:
    registry.register("sess-1", "proj1", "C123", "1.0")
    registry.register("sess-2", "proj2", "C123", "2.0")
    registry.archive("sess-1")
    active = registry.list_active()
    assert len(active) == 1
    assert active[0].session_id == "sess-2"


def test_persistence_save_and_load(tmp_config_dir: Path) -> None:
    path = tmp_config_dir / "sessions.json"
    reg1 = SessionRegistry(storage_path=path)
    reg1.register("sess-1", "proj", "C123", "1.0")
    reg1.save()

    reg2 = SessionRegistry(storage_path=path)
    reg2.load()
    mapping = reg2.get("sess-1")
    assert mapping is not None
    assert mapping.session_name == "proj"


def test_cleanup_expired(registry: SessionRegistry) -> None:
    registry.register("sess-old", "proj", "C123", "1.0")
    # Force old timestamp
    m = registry.get("sess-old")
    assert m is not None
    m.last_active = time.time() - 100000
    archived = registry.cleanup(max_idle_secs=86400)
    assert len(archived) == 1
    assert archived[0] == "sess-old"
    assert registry.get("sess-old") is not None
    assert registry.get("sess-old").status == "archived"
