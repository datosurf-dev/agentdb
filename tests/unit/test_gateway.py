"""
Unit tests for db.gateway (Task 16).

Tests follow the brief spec exactly:
  - test_subscribe_and_dispatch_fires_handler
  - test_conflict_queue_empty_before_any_conflicts
  - test_gateway_adapter_inherits_gateway_reference
  - test_claude_middleware_still_works_after_migration  (regression)
"""
from __future__ import annotations

import pytest
from agentdb import AgentDB
from agentdb.gateway import Gateway
from agentdb.gateway.adapter import GatewayAdapter
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


# ---------------------------------------------------------------------------
# Gateway tests
# ---------------------------------------------------------------------------

def test_subscribe_and_dispatch_fires_handler(tmp_path):
    db = _make_db(tmp_path)
    gw = db.gateway
    calls = []
    gw.subscribe("custom_event", lambda **kw: calls.append(kw))
    count = gw.dispatch("custom_event", session_id="s1", agent_id="a1")
    assert count == 1
    assert calls == [{"session_id": "s1", "agent_id": "a1"}]


def test_conflict_queue_empty_before_any_conflicts(tmp_path):
    db = _make_db(tmp_path)
    gw = db.gateway
    # No conflicts written yet — should return empty list
    queue = gw.conflict_queue()
    assert queue == []


def test_gateway_adapter_inherits_gateway_reference(tmp_path):
    db = _make_db(tmp_path)

    class MyAdapter(GatewayAdapter):
        pass

    adapter = MyAdapter(db=db, agent_id="test-agent")
    assert adapter._db is db
    assert adapter._agent_id == "test-agent"
    assert adapter.gateway is db.gateway
    assert isinstance(adapter.gateway, Gateway)


def test_gateway_adapter_exposes_agent_id_publicly(tmp_path):
    """
    Downstream apps need the bound agent_id to scope their own calls without
    reaching into the underscore-prefixed private attribute.
    """
    db = _make_db(tmp_path)

    class MyAdapter(GatewayAdapter):
        pass

    adapter = MyAdapter(db=db, agent_id="test-agent")
    assert adapter.agent_id == "test-agent"


# ---------------------------------------------------------------------------
# Regression: claude middleware still works after migration
# ---------------------------------------------------------------------------

def test_claude_middleware_still_works_after_migration(tmp_path):
    db = _make_db(tmp_path)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="claude-agent")

    # GatewayAdapter must be in MRO
    assert issubclass(AgentDBClaudeMiddleware, GatewayAdapter)

    # middleware must have a gateway reference
    assert hasattr(middleware, "gateway")
    assert middleware.gateway is db.gateway

    # Core prepare_turn / finalize_turn path must still work
    messages, retrieval = middleware.prepare_turn(
        user_query="hello world",
        session_id="sess-1",
    )
    assert any(m["role"] == "user" for m in messages)
    assert retrieval is not None

    middleware.finalize_turn(
        session_id="sess-1",
        model_response="hi there",
    )


# ---------------------------------------------------------------------------
# AgentDB backward-compat aliases still work
# ---------------------------------------------------------------------------

def test_agentdb_aliases_still_work(tmp_path):
    db = _make_db(tmp_path)
    calls = []
    db.register_reconciliation_handler("ev", lambda **kw: calls.append(kw))
    count = db.dispatch_reconciliation_trigger("ev", x=1)
    assert count == 1
    assert calls == [{"x": 1}]
