"""Workstream E — temporal applicability (FINAL-HARDENING §7).

Explicit validity windows (effective_from/effective_until/expiry_time) gate
current-applicability results when an as_of time is supplied. Unknown validity
stays eligible (no exclusion, no bonus). created_at is never substituted for
effective_from. Recency alone does not establish supersession.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentdb import AgentDB


def _db(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Explicit validity windows
# ---------------------------------------------------------------------------

def test_record_not_yet_effective_excluded(tmp_path):
    db = _db(tmp_path)
    db.write("memory/cfg/v", "v2 config", origin="agent_inferred", agent_id="a1",
             effective_from=T0 + timedelta(days=100))
    res = db.retrieve("v2 config", agent_id="a1", limit=10, scope={"as_of": T0})
    assert res.records == []


def test_record_inside_window_included(tmp_path):
    db = _db(tmp_path)
    db.write("memory/cfg/v", "v2 config", origin="agent_inferred", agent_id="a1",
             effective_from=T0, effective_until=T0 + timedelta(days=30))
    res = db.retrieve("v2 config", agent_id="a1", limit=10,
                      scope={"as_of": T0 + timedelta(days=10)})
    assert {r.key for r in res.records} == {"memory/cfg/v"}


def test_record_past_window_excluded(tmp_path):
    db = _db(tmp_path)
    db.write("memory/cfg/v", "old config", origin="agent_inferred", agent_id="a1",
             effective_from=T0, effective_until=T0 + timedelta(days=5))
    res = db.retrieve("old config", agent_id="a1", limit=10,
                      scope={"as_of": T0 + timedelta(days=30)})
    assert res.records == []


def test_expired_record_excluded_at_as_of(tmp_path):
    db = _db(tmp_path)
    db.write("memory/cfg/e", "short lived", origin="agent_inferred", agent_id="a1",
             expiry_time=T0 + timedelta(days=1))
    res = db.retrieve("short lived", agent_id="a1", limit=10,
                      scope={"as_of": T0 + timedelta(days=5)})
    assert res.records == []


# ---------------------------------------------------------------------------
# Overlapping / historical
# ---------------------------------------------------------------------------

def test_overlapping_periods_current_query(tmp_path):
    db = _db(tmp_path)
    db.write("memory/cfg/a", "region config A", origin="agent_inferred", agent_id="a1",
             effective_from=T0, effective_until=T0 + timedelta(days=100))
    db.write("memory/cfg/b", "region config B", origin="agent_inferred", agent_id="a1",
             effective_from=T0 + timedelta(days=50), effective_until=T0 + timedelta(days=150))
    # At day 60 both are valid -> both eligible.
    res = db.retrieve("region config", agent_id="a1", limit=10,
                      scope={"as_of": T0 + timedelta(days=60)})
    assert {r.key for r in res.records} == {"memory/cfg/a", "memory/cfg/b"}


def test_historical_query_returns_past_record(tmp_path):
    db = _db(tmp_path)
    db.write("memory/cfg/h", "historical config", origin="agent_inferred", agent_id="a1",
             effective_from=T0, effective_until=T0 + timedelta(days=30))
    res = db.retrieve("historical config", agent_id="a1", limit=10,
                      scope={"as_of": T0 + timedelta(days=10)})
    assert {r.key for r in res.records} == {"memory/cfg/h"}


# ---------------------------------------------------------------------------
# Unknown validity
# ---------------------------------------------------------------------------

def test_unknown_validity_stays_eligible(tmp_path):
    db = _db(tmp_path)
    db.write("memory/cfg/u", "no validity metadata", origin="agent_inferred", agent_id="a1")
    res = db.retrieve("no validity metadata", agent_id="a1", limit=10,
                      scope={"as_of": T0 + timedelta(days=999)})
    assert "memory/cfg/u" in {r.key for r in res.records}


def test_created_at_not_used_as_effective_from(tmp_path):
    """A record with created_at in the future relative to as_of but no
    effective_from is NOT excluded (created_at != effective_from)."""
    db = _db(tmp_path)
    rec = db.write("memory/cfg/c", "content", origin="agent_inferred", agent_id="a1")
    # Force a created_at far in the future.
    stored = db._store.get_by_id(rec.id)
    stored.created_at = T0 + timedelta(days=500)
    db._store.write(stored)
    res = db.retrieve("content", agent_id="a1", limit=10, scope={"as_of": T0})
    assert "memory/cfg/c" in {r.key for r in res.records}


# ---------------------------------------------------------------------------
# Timezone handling & no-scope
# ---------------------------------------------------------------------------

def test_naive_as_of_is_treated_as_utc(tmp_path):
    db = _db(tmp_path)
    db.write("memory/cfg/tz", "tz config", origin="agent_inferred", agent_id="a1",
             effective_from=T0 + timedelta(days=10))
    # naive datetime (no tzinfo) earlier than effective_from -> excluded
    res = db.retrieve("tz config", agent_id="a1", limit=10,
                      scope={"as_of": datetime(2026, 1, 2)})  # before T0+10d
    assert res.records == []


def test_no_as_of_disables_temporal_gate(tmp_path):
    db = _db(tmp_path)
    db.write("memory/cfg/f", "future config", origin="agent_inferred", agent_id="a1",
             effective_from=T0 + timedelta(days=1000))
    res = db.retrieve("future config", agent_id="a1", limit=10)  # no as_of
    assert "memory/cfg/f" in {r.key for r in res.records}


def test_iso_string_as_of_accepted(tmp_path):
    db = _db(tmp_path)
    db.write("memory/cfg/i", "iso config", origin="agent_inferred", agent_id="a1",
             effective_from=T0 + timedelta(days=100))
    res = db.retrieve("iso config", agent_id="a1", limit=10,
                      scope={"as_of": "2026-01-02T00:00:00+00:00"})
    assert res.records == []
