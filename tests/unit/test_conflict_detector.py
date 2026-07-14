"""
Unit tests for ConflictDetector (public-client-gap task).

Tests validate the Phase 1 temporal + authority conflict detection rules.
"""
from __future__ import annotations

import pytest
from agentdb.core.models import ConflictRecord, MemoryRecord
from agentdb.governance.conflict_detector import ConflictDetector


def _make_record(key: str, origin: str, record_id: str | None = None) -> MemoryRecord:
    r = MemoryRecord(
        key=key,
        value="some value",
        origin=origin,
        trust_level="agent" if origin == "agent_inferred" else "human",
        agent_id="agent-1",
    )
    if record_id is not None:
        r.id = record_id
    return r


# ---------------------------------------------------------------------------
# Temporal conflict: same key + same origin
# ---------------------------------------------------------------------------

def test_temporal_conflict_same_key_same_origin():
    detector = ConflictDetector()
    existing = _make_record("pref/lang", "agent_inferred")
    new_rec = _make_record("pref/lang", "agent_inferred")
    result = detector.check(new_record=new_rec, existing=existing)
    assert result is not None
    assert result.conflict_type == "temporal"


# ---------------------------------------------------------------------------
# Authority conflict: agent_inferred / provider_ingested trying to overwrite human_approved
# ---------------------------------------------------------------------------

def test_authority_conflict_agent_overwrites_human():
    detector = ConflictDetector()
    existing = _make_record("pref/lang", "human_approved")
    new_rec = _make_record("pref/lang", "agent_inferred")
    result = detector.check(new_record=new_rec, existing=existing)
    assert result is not None
    assert result.conflict_type == "authority"


def test_authority_conflict_provider_overwrites_human():
    detector = ConflictDetector()
    existing = _make_record("pref/lang", "human_approved")
    new_rec = _make_record("pref/lang", "provider_ingested")
    result = detector.check(new_record=new_rec, existing=existing)
    assert result is not None
    assert result.conflict_type == "authority"


# ---------------------------------------------------------------------------
# No conflict: human_approved supersedes agent — NOT flagged
# ---------------------------------------------------------------------------

def test_no_conflict_human_overwrites_agent():
    detector = ConflictDetector()
    existing = _make_record("pref/lang", "agent_inferred")
    new_rec = _make_record("pref/lang", "human_approved")
    result = detector.check(new_record=new_rec, existing=existing)
    assert result is None


# ---------------------------------------------------------------------------
# No conflict: different keys (shouldn't reach check() in practice, but guard it)
# ---------------------------------------------------------------------------

def test_no_conflict_different_keys():
    detector = ConflictDetector()
    existing = _make_record("pref/lang", "agent_inferred")
    new_rec = _make_record("pref/color", "agent_inferred")
    result = detector.check(new_record=new_rec, existing=existing)
    assert result is None


# ---------------------------------------------------------------------------
# ConflictRecord structure
# ---------------------------------------------------------------------------

def test_conflict_record_has_correct_type_and_ids():
    detector = ConflictDetector()
    existing = _make_record("pref/lang", "agent_inferred", record_id="existing-id")
    new_rec = _make_record("pref/lang", "agent_inferred", record_id="new-id")
    result = detector.check(new_record=new_rec, existing=existing)
    assert result is not None
    assert isinstance(result, ConflictRecord)
    assert result.conflict_type == "temporal"
    assert result.record_a_id == "existing-id"
    assert result.record_b_id == "new-id"
    assert isinstance(result.id, str) and len(result.id) > 0
