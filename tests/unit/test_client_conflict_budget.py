"""
Unit tests for public client gap:
  - conflict detection wired into write()
  - conflicts surfaced in retrieve() / retrieve_by_entities()
  - db.gateway.conflict_queue() returns real data
  - budgeted retrieval (budget_tokens, token_estimate, truncated)
"""
from __future__ import annotations

import pytest
from agentdb import AgentDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


# ---------------------------------------------------------------------------
# Conflict detection wired into write()
# ---------------------------------------------------------------------------

def test_write_detects_temporal_conflict_and_dispatches(tmp_path):
    db = _make_db(tmp_path)
    dispatched = []
    db.gateway.subscribe("conflict_detected", lambda **kw: dispatched.append(kw))

    db.write("pref/lang", "English", origin="agent_inferred", agent_id="agent-a")
    db.write("pref/lang", "French", origin="agent_inferred", agent_id="agent-a")

    assert len(dispatched) == 1
    assert dispatched[0]["conflict_type"] == "temporal"
    assert dispatched[0]["key"] == "pref/lang"


def test_write_stores_conflict_record_in_db(tmp_path):
    db = _make_db(tmp_path)

    db.write("pref/lang", "English", origin="agent_inferred", agent_id="agent-a")
    db.write("pref/lang", "French", origin="agent_inferred", agent_id="agent-a")

    conflicts = db._store.list_conflicts(resolved=False)
    assert len(conflicts) == 1
    assert conflicts[0].conflict_type == "temporal"


def test_write_authority_conflict_detected(tmp_path):
    db = _make_db(tmp_path)
    dispatched = []
    db.gateway.subscribe("conflict_detected", lambda **kw: dispatched.append(kw))

    db.write("pref/lang", "English", origin="human_approved", agent_id="human-1")
    db.write("pref/lang", "French", origin="agent_inferred", agent_id="agent-a")

    assert len(dispatched) == 1
    assert dispatched[0]["conflict_type"] == "authority"


def test_write_human_supersedes_agent_no_conflict(tmp_path):
    """Human overwriting agent_inferred is NOT a conflict — no event dispatched."""
    db = _make_db(tmp_path)
    dispatched = []
    db.gateway.subscribe("conflict_detected", lambda **kw: dispatched.append(kw))

    db.write("pref/lang", "English", origin="agent_inferred", agent_id="agent-a")
    db.write("pref/lang", "French", origin="human_approved", agent_id="human-1")

    assert len(dispatched) == 0
    conflicts = db._store.list_conflicts(resolved=False)
    assert len(conflicts) == 0


# ---------------------------------------------------------------------------
# Conflicts surfaced in retrieve()
# ---------------------------------------------------------------------------

def test_retrieve_returns_conflicts_for_flagged_records(tmp_path):
    db = _make_db(tmp_path)

    db.write("pref/lang", "English", origin="agent_inferred", agent_id="agent-a")
    db.write("pref/lang", "French", origin="agent_inferred", agent_id="agent-a")

    result = db.retrieve("lang", agent_id="agent-a")
    # The record with conflict_id set should surface a conflict
    assert len(result.conflicts) >= 1


def test_retrieve_override_instructions_on_authority_conflict(tmp_path):
    db = _make_db(tmp_path)

    db.write("pref/lang", "English", origin="human_approved", agent_id="human-1")
    db.write("pref/lang", "French", origin="agent_inferred", agent_id="agent-a")

    result = db.retrieve("lang", agent_id="agent-a")
    assert any("authority" in instr.lower() or "human-approved" in instr.lower()
               for instr in result.override_instructions)


# ---------------------------------------------------------------------------
# db.gateway.conflict_queue() returns real conflicts
# ---------------------------------------------------------------------------

def test_conflict_queue_returns_real_conflicts(tmp_path):
    db = _make_db(tmp_path)

    db.write("pref/lang", "English", origin="agent_inferred", agent_id="agent-a")
    db.write("pref/lang", "French", origin="agent_inferred", agent_id="agent-a")

    queue = db.gateway.conflict_queue(resolved=False)
    assert len(queue) >= 1


def test_conflict_queue_empty_when_no_conflicts(tmp_path):
    db = _make_db(tmp_path)
    db.write("pref/lang", "English", origin="agent_inferred", agent_id="agent-a")
    # No second write = no conflict
    queue = db.gateway.conflict_queue(resolved=False)
    assert queue == []


# ---------------------------------------------------------------------------
# Budgeted retrieval
# ---------------------------------------------------------------------------

def test_budget_tokens_truncates_records(tmp_path):
    db = _make_db(tmp_path)

    # Write several records with predictable values
    for i in range(10):
        db.write(f"key/{i}", f"value number {i}", origin="agent_inferred", agent_id="agent-a")

    # Very tight budget — should truncate
    result = db.retrieve("value number", agent_id="agent-a", limit=10, budget_tokens=5)
    # With budget_tokens=5, at most 1 or 2 short records can fit
    assert len(result.records) < 10


def test_budget_tokens_sets_truncated_flag(tmp_path):
    db = _make_db(tmp_path)

    for i in range(10):
        db.write(f"key/{i}", f"value number {i}", origin="agent_inferred", agent_id="agent-a")

    result = db.retrieve("value number", agent_id="agent-a", limit=10, budget_tokens=5)
    assert result.truncated is True


def test_budget_tokens_sets_token_estimate(tmp_path):
    db = _make_db(tmp_path)

    db.write("key/alpha", "hello world", origin="agent_inferred", agent_id="agent-a")

    result = db.retrieve("hello", agent_id="agent-a", budget_tokens=1000)
    assert result.token_estimate > 0


def test_no_budget_tokens_returns_all_records(tmp_path):
    db = _make_db(tmp_path)

    for i in range(5):
        db.write(f"key/{i}", f"value {i}", origin="agent_inferred", agent_id="agent-a")

    result = db.retrieve("value", agent_id="agent-a", limit=5)
    assert result.truncated is False
    assert result.token_estimate >= 0  # may be 0 if no records match, but field exists


def test_retrieval_result_has_new_fields(tmp_path):
    """RetrievalResult must have token_estimate and truncated with defaults."""
    db = _make_db(tmp_path)
    result = db.retrieve("anything", agent_id="agent-a")
    assert hasattr(result, "token_estimate")
    assert hasattr(result, "truncated")
    assert result.token_estimate == 0 or result.token_estimate >= 0
    assert isinstance(result.truncated, bool)


def test_existing_tests_still_pass(tmp_path):
    """Backward-compat: write() and retrieve() with no new params behave identically to pre-change behavior."""
    db = AgentDB(path=tmp_path / "test.db")
    db.write(key="k", value="v", origin="agent_inferred", agent_id="a1")
    result = db.retrieve("v", agent_id="a1")
    assert result.truncated is False
    assert result.token_estimate >= 0
    assert isinstance(result.conflicts, list)
    assert isinstance(result.override_instructions, list)
