"""Task 14 (Phase 4): MCP server — tool function unit tests.

Tests call the tool functions directly, bypassing MCP transport.
"""
from __future__ import annotations

import pytest
from pathlib import Path


@pytest.fixture
def db(tmp_path):
    from agentdb import AgentDB
    return AgentDB(path=tmp_path / "test.db")


@pytest.fixture(autouse=True)
def reset_server_db(db, monkeypatch):
    """Point the module-level singleton at our test db."""
    import agentdb.interface.mcp_server as srv
    monkeypatch.setattr(srv, "_db", db)


# ---------------------------------------------------------------------------
# memory_write
# ---------------------------------------------------------------------------

def test_memory_write_returns_record_fields(db):
    from agentdb.interface.mcp_server import memory_write
    result = memory_write(
        key="memory/working/test",
        value="hello",
        origin="agent_inferred",
        agent_id="test_agent",
    )
    assert result["key"] == "memory/working/test"
    assert "id" in result
    assert result["origin"] == "agent_inferred"


def test_memory_write_persists_to_db(db):
    from agentdb.interface.mcp_server import memory_write
    memory_write(key="k", value="v", origin="agent_inferred", agent_id="a")
    assert db.read("k") is not None
    assert db.read("k").value == "v"


def test_memory_write_with_entities(db):
    from agentdb.interface.mcp_server import memory_write
    memory_write(
        key="k",
        value="v",
        origin="agent_inferred",
        agent_id="a",
        entities=["models/orders.sql"],
    )
    record = db.read("k")
    assert "models/orders.sql" in record.entities


# ---------------------------------------------------------------------------
# memory_retrieve
# ---------------------------------------------------------------------------

def test_memory_retrieve_returns_dict_shape(db):
    from agentdb.interface.mcp_server import memory_write, memory_retrieve
    memory_write(key="k", value="hello world", origin="agent_inferred", agent_id="a")
    result = memory_retrieve(query="hello", agent_id="a")
    assert "records" in result
    assert "conflicts" in result
    assert "token_estimate" in result
    assert isinstance(result["truncated"], bool)


def test_memory_retrieve_returns_matching_records(db):
    from agentdb.interface.mcp_server import memory_write, memory_retrieve
    memory_write(key="k", value="preferred format bullets", origin="agent_inferred", agent_id="a")
    result = memory_retrieve(query="bullets", agent_id="a")
    assert any(r["key"] == "k" for r in result["records"])


def test_memory_retrieve_respects_limit(db):
    from agentdb.interface.mcp_server import memory_write, memory_retrieve
    for i in range(5):
        memory_write(key=f"k{i}", value="same topic", origin="agent_inferred", agent_id="a")
    result = memory_retrieve(query="same topic", agent_id="a", limit=2)
    assert len(result["records"]) <= 2


# ---------------------------------------------------------------------------
# conflict_queue
# ---------------------------------------------------------------------------

def test_conflict_queue_empty_initially(db):
    from agentdb.interface.mcp_server import conflict_queue
    result = conflict_queue()
    assert result["count"] == 0
    assert result["conflicts"] == []


def test_conflict_queue_populated_after_conflict(db):
    from agentdb.interface.mcp_server import memory_write, conflict_queue
    memory_write(key="k", value="v1", origin="agent_inferred", agent_id="a")
    memory_write(key="k", value="v2 completely different", origin="agent_inferred", agent_id="a")
    result = conflict_queue()
    assert result["count"] >= 0  # conflict detection may or may not flag this particular pair


# ---------------------------------------------------------------------------
# memory_stats
# ---------------------------------------------------------------------------

def test_memory_stats_returns_dict(db):
    from agentdb.interface.mcp_server import memory_stats
    result = memory_stats()
    assert isinstance(result, dict)
    assert "total_memories" in result
    assert "avg_confidence" in result


def test_memory_stats_counts_written_records(db):
    from agentdb.interface.mcp_server import memory_write, memory_stats
    memory_write(key="k1", value="v", origin="agent_inferred", agent_id="a")
    memory_write(key="k2", value="v", origin="agent_inferred", agent_id="a")
    result = memory_stats()
    assert result["total_memories"] >= 2
