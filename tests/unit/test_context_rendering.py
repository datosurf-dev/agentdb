"""Distilled knowledge & context rendering (CONTEXTUAL-SPEC §8).

Concise model-facing rendering: one line per selected record, canonical scope/
evidence qualifiers only when present, and an explicit unresolved-disagreement
block for material conflicts (no silent merge), without dumping all sources.
"""
from __future__ import annotations

from agentdb import AgentDB
from agentdb.core.models import ConflictRecord, MemoryRecord
from agentdb.governance.lifecycle_manager import _record_digest


def _db(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)


def _make_canonical(db, value, *, env=None, repo_dirs=None, sources=None):
    rec = MemoryRecord(
        key="memory/canonical/x/y", value=value, origin="agent_inferred",
        trust_level="agent", agent_id="a1", consolidated=True,
        kind="derived_canonical", producer="consolidation",
        environment=env, working_dirs=repo_dirs or [],
        source_record_ids=[s.id for s in (sources or [])],
        derivation={"version": "1", "sources": [
            {"id": s.id, "digest": _record_digest(s)} for s in (sources or [])
        ]},
    )
    db._store.write(rec)
    return rec


def test_render_simple_record_is_concise(tmp_path):
    db = _db(tmp_path)
    db.write("memory/x/a", "use tabs for indentation", origin="agent_inferred", agent_id="a1")
    res = db.retrieve("indentation tabs", agent_id="a1", limit=10)
    text = db.render_context(res)
    assert "use tabs for indentation" in text
    # No verbose metadata for a plain source record.
    assert "Evidence:" not in text


def test_render_canonical_includes_scope_and_evidence(tmp_path):
    db = _db(tmp_path)
    s1 = db.write("memory/billing/s1", "billing x", origin="agent_inferred", agent_id="a1")
    s2 = db.write("memory/billing/s2", "billing y", origin="agent_inferred", agent_id="a1")
    canon = _make_canonical(db, "production billing uses postgres 16",
                            env="production", repo_dirs=["/srv/billing-service"],
                            sources=[s1, s2])
    res = db.retrieve("production billing postgres", agent_id="a1", limit=10)
    text = db.render_context(res)
    assert "production billing uses postgres 16" in text
    assert "Scope:" in text and "production" in text and "billing-service" in text
    assert "Evidence:" in text and s1.id in text and s2.id in text


def test_render_does_not_dump_archived_sources(tmp_path):
    db = _db(tmp_path)
    s1 = db.write("memory/auth/s1", "auth uses tokens", origin="agent_inferred", agent_id="a1")
    s2 = db.write("memory/auth/s2", "auth uses tokens now", origin="agent_inferred", agent_id="a1")
    canon = _make_canonical(db, "auth uses tokens", sources=[s1, s2])
    for s in (s1, s2):
        db._store.archive_record(s.id)
    res = db.retrieve("auth uses tokens", agent_id="a1", limit=10)
    text = db.render_context(res)
    # Canonical rendered; its source bodies are not dumped as separate lines.
    assert "auth uses tokens" in text
    assert text.count("auth uses tokens") <= 2  # canonical line (+ maybe evidence id)


def test_render_appends_conflict_block(tmp_path):
    db = _db(tmp_path)
    a = db.write("memory/db/a", "use postgres 15", origin="agent_inferred", agent_id="a1")
    b = db.write("memory/db/b", "use postgres 16", origin="agent_inferred", agent_id="a1")
    res = db.retrieve("db postgres", agent_id="a1", limit=10)
    # Force a recorded conflict among the two selected records.
    res.conflicts = [ConflictRecord(record_a_id=a.id, record_b_id=b.id, conflict_type="temporal")]
    text = db.render_context(res)
    assert "Unresolved disagreement:" in text
    assert "No verified supersession found." in text
    assert "postgres 15" in text and "postgres 16" in text


def test_render_no_conflict_block_when_none(tmp_path):
    db = _db(tmp_path)
    db.write("memory/db/a", "use postgres 15", origin="agent_inferred", agent_id="a1")
    res = db.retrieve("db postgres", agent_id="a1", limit=10)
    text = db.render_context(res)
    assert "Unresolved disagreement:" not in text


def test_render_respects_max_records(tmp_path):
    db = _db(tmp_path)
    for i in range(5):
        db.write(f"memory/t/{i}", f"topic item {i}", origin="agent_inferred", agent_id="a1")
    res = db.retrieve("topic item", agent_id="a1", limit=10)
    text = db.render_context(res, max_records=2)
    assert text.count("\n") <= 1  # at most two lines (2 records, no conflict block)
