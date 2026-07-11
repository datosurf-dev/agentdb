"""
Tests the AgentDBCheckpointSaver against LangGraph's checkpointer contract.
Requires: pip install langgraph-checkpoint
"""
import pytest
from pathlib import Path
from agentdb import AgentDB
from agentdb.adapters.langgraph import AgentDBCheckpointSaver

PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"

@pytest.fixture
def db(tmp_path):
    return AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

@pytest.fixture
def saver(db):
    return AgentDBCheckpointSaver(db=db, agent_id="langgraph_agent")

def _make_config(thread_id: str, checkpoint_id: str = "cp1"):
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
        }
    }

def _make_checkpoint(checkpoint_id: str = "cp1"):
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-01-01T00:00:00Z",
        "channel_values": {"messages": []},
        "channel_versions": {},
        "versions_seen": {},
        "pending_sends": [],
    }

def _make_metadata():
    return {"source": "input", "step": 0, "writes": {}, "parents": {}}

def test_put_and_get_tuple(saver):
    config = _make_config("thread-1")
    checkpoint = _make_checkpoint()
    metadata = _make_metadata()
    new_config = saver.put(config, checkpoint, metadata, {})
    assert new_config["configurable"]["checkpoint_id"] is not None

    result = saver.get_tuple(config)
    assert result is not None
    assert result.checkpoint["id"] == "cp1"

def test_get_tuple_missing_returns_none(saver):
    config = _make_config("nonexistent-thread")
    assert saver.get_tuple(config) is None

def test_list_returns_checkpoints_for_thread(saver):
    config = _make_config("thread-list")
    saver.put(config, _make_checkpoint("cp1"), _make_metadata(), {})
    saver.put(
        {**config, "configurable": {**config["configurable"], "checkpoint_id": "cp2"}},
        _make_checkpoint("cp2"), _make_metadata(), {}
    )
    results = list(saver.list(config))
    assert len(results) == 2

def test_put_writes_stored(saver):
    config = _make_config("thread-writes")
    saver.put(config, _make_checkpoint(), _make_metadata(), {})
    saver.put_writes(config, [("channel_a", "value_a")], "cp1")
    result = saver.get_tuple(config)
    assert result is not None
    assert result.pending_writes is not None
    assert len(result.pending_writes) > 0

def test_checkpoints_written_to_human_approved_zone_denied(db, tmp_path):
    from agentdb.governance.permission_router import PermissionDenied
    restricted_saver = AgentDBCheckpointSaver(
        db=db, agent_id="research_agent",
        key_prefix="skills/"
    )
    config = _make_config("thread-bad")
    with pytest.raises(PermissionDenied):
        restricted_saver.put(config, _make_checkpoint(), _make_metadata(), {})

def test_checkpoint_logged_to_audit(saver, db):
    config = _make_config("thread-audit")
    saver.put(config, _make_checkpoint(), _make_metadata(), {})
    entries = db.audit_log.replay(from_sequence=0)
    assert any(e.event_type == "memory_write" for e in entries)
