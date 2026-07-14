"""
Unit tests for db.gateway (Task 16).

Tests follow the brief spec exactly:
  - test_subscribe_and_dispatch_fires_handler
  - test_conflict_queue_empty_before_any_conflicts
  - test_propose_writes_memory_record
  - test_approve_calls_skills_api
  - test_reject_archives_draft_without_touching_skills
  - test_list_proposals_filters_by_status
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
    gw.subscribe("conversation_started", lambda **kw: calls.append(kw))
    count = gw.dispatch("conversation_started", session_id="s1", agent_id="a1")
    assert count == 1
    assert calls == [{"session_id": "s1", "agent_id": "a1"}]


def test_conflict_queue_empty_before_any_conflicts(tmp_path):
    db = _make_db(tmp_path)
    gw = db.gateway
    # No conflicts written yet — should return empty list
    queue = gw.conflict_queue()
    assert queue == []


def test_propose_writes_memory_record(tmp_path):
    db = _make_db(tmp_path)
    gw = db.gateway
    proposal_id = gw.propose(
        name="summarise-bullets",
        content="Always reply with bullet points",
        scope=["research_agent"],
        agent_id="research_agent",
    )
    assert isinstance(proposal_id, str) and proposal_id

    # The record must be retrievable via db.read
    record = db.read(f"proposal/{proposal_id}")
    assert record is not None
    assert record.value["status"] == "pending"
    assert record.value["name"] == "summarise-bullets"


def test_approve_calls_skills_api(tmp_path):
    db = _make_db(tmp_path)
    gw = db.gateway
    proposal_id = gw.propose(
        name="summarise-bullets",
        content="Always reply with bullet points",
        scope=["research_agent"],
        agent_id="research_agent",
    )
    gw.approve(proposal_id)

    # Skill must now exist in skills_api
    skill = db.skills_api.get_skill(proposal_id)
    assert skill is not None
    assert skill.name == "summarise-bullets"
    assert skill.content == "Always reply with bullet points"

    # Proposal record must be archived
    record = db._store.get_by_key(f"proposal/{proposal_id}")
    assert record is None or record.archived


def test_reject_archives_draft_without_touching_skills(tmp_path):
    db = _make_db(tmp_path)
    gw = db.gateway
    proposal_id = gw.propose(
        name="verbose-mode",
        content="Always write 500 words",
        scope=["research_agent"],
        agent_id="research_agent",
    )
    gw.reject(proposal_id, reason="too verbose")

    # No skill should exist
    skill = db.skills_api.get_skill(proposal_id)
    assert skill is None

    # Proposal record must be archived / status=rejected
    record = db._store.get_by_key(f"proposal/{proposal_id}")
    assert record is None or record.archived


def test_list_proposals_filters_by_status(tmp_path):
    db = _make_db(tmp_path)
    gw = db.gateway
    p1 = gw.propose(
        name="skill-a", content="content-a", scope=["agent-x"], agent_id="agent-x"
    )
    p2 = gw.propose(
        name="skill-b", content="content-b", scope=["agent-x"], agent_id="agent-x"
    )
    p3 = gw.propose(
        name="skill-c", content="content-c", scope=["agent-x"], agent_id="agent-x"
    )
    gw.approve(p2)
    gw.reject(p3, reason="nope")

    pending = gw.list_proposals(status="pending")
    approved = gw.list_proposals(status="approved")
    rejected = gw.list_proposals(status="rejected")

    pending_ids = {p["proposal_id"] for p in pending}
    assert p1 in pending_ids
    assert p2 not in pending_ids
    assert p3 not in pending_ids

    approved_ids = {p["proposal_id"] for p in approved}
    assert p2 in approved_ids

    rejected_ids = {p["proposal_id"] for p in rejected}
    assert p3 in rejected_ids


def test_gateway_adapter_inherits_gateway_reference(tmp_path):
    db = _make_db(tmp_path)

    class MyAdapter(GatewayAdapter):
        pass

    adapter = MyAdapter(db=db, agent_id="test-agent")
    assert adapter._db is db
    assert adapter._agent_id == "test-agent"
    assert adapter.gateway is db.gateway
    assert isinstance(adapter.gateway, Gateway)


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
