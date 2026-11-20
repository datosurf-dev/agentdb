"""
Tests for checkpoint() and write_compaction_summary().

Both are retrievable across session boundaries. Per contract P-01 / ADB-03 they
default to origin="agent_inferred" (machine/agent-produced) and never become
human_approved implicitly; a verified human action may pass origin="human_approved".
"""
from agentdb import AgentDB


def _db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


def test_checkpoint_writes_recoverable_record(tmp_path):
    db = _db(tmp_path)
    db.checkpoint(agent_id="agent1", session_id="sess-1")
    results = db.retrieve(query="compaction checkpoint", agent_id="agent1", session_id="sess-2")
    assert any("checkpoint" in r.key for r in results.records)


def test_checkpoint_record_is_agent_inferred_by_default(tmp_path):
    """ADB-03: a machine/agent checkpoint must NOT be labeled human_approved."""
    db = _db(tmp_path)
    db.checkpoint(agent_id="agent1", session_id="sess-1")
    record = db.read("memory/compaction/checkpoint/sess-1")
    assert record is not None
    assert record.origin == "agent_inferred"
    assert record.trust_level == "agent"


def test_checkpoint_honors_explicit_human_origin(tmp_path):
    """A verified human action may explicitly mark a checkpoint human_approved."""
    db = _db(tmp_path)
    db.checkpoint(agent_id="agent1", session_id="sess-1", origin="human_approved")
    record = db.read("memory/compaction/checkpoint/sess-1")
    assert record.origin == "human_approved"
    assert record.trust_level == "human"


def test_write_compaction_summary_survives_lifecycle_sweep(tmp_path):
    db = _db(tmp_path)
    db.write_compaction_summary(
        agent_id="agent1", session_id="sess-1",
        summary="Completed refactor of orders pipeline. Next: test coverage.",
    )
    db.lifecycle.run_sweep()
    results = db.retrieve(query="compaction summary", agent_id="agent1", session_id="sess-2")
    assert any("summary" in r.key for r in results.records)


def test_compaction_summary_is_agent_inferred_by_default(tmp_path):
    """ADB-03: caller-provided summary text must not become human_approved."""
    db = _db(tmp_path)
    db.write_compaction_summary(agent_id="agent1", session_id="sess-1", summary="Summary text.")
    record = db.read("memory/compaction/summary/sess-1")
    assert record is not None
    assert record.origin == "agent_inferred"
    assert record.trust_level == "agent"
    assert record.value == "Summary text."


def test_compaction_summary_honors_explicit_human_origin(tmp_path):
    db = _db(tmp_path)
    db.write_compaction_summary(
        agent_id="agent1", session_id="sess-1", summary="Reviewed and approved.",
        origin="human_approved",
    )
    record = db.read("memory/compaction/summary/sess-1")
    assert record.origin == "human_approved"
    assert record.trust_level == "human"


def test_compaction_summary_rejects_masquerade_via_provider_origin(tmp_path):
    """A caller cannot launder untrusted content into human authority implicitly:
    only the explicit human_approved origin maps to human trust; agent/provider
    stay at their true (lower) authority."""
    db = _db(tmp_path)
    db.write_compaction_summary(
        agent_id="agent1", session_id="sess-1", summary="raw model text",
        origin="provider_ingested",
    )
    record = db.read("memory/compaction/summary/sess-1")
    assert record.origin == "provider_ingested"
    assert record.trust_level == "provider"
    assert record.origin != "human_approved"


def test_compaction_summary_per_session_not_overwritten(tmp_path):
    """Two compaction summaries for different sessions have independent keys."""
    db = _db(tmp_path)
    db.write_compaction_summary(agent_id="agent1", session_id="sess-1", summary="First summary.")
    db.write_compaction_summary(agent_id="agent1", session_id="sess-2", summary="Second summary.")

    r1 = db.read("memory/compaction/summary/sess-1")
    r2 = db.read("memory/compaction/summary/sess-2")
    assert r1 is not None and r1.value == "First summary."
    assert r2 is not None and r2.value == "Second summary."


def test_checkpoint_retrieves_across_session_boundary(tmp_path):
    db = _db(tmp_path)
    db.checkpoint(agent_id="agent1", session_id="sess-A")

    # Simulate a new session — session_id has changed but agent_id is same
    results = db.retrieve(query="checkpoint session", agent_id="agent1", session_id="sess-B")
    assert any("checkpoint" in r.key for r in results.records)


def test_compaction_records_have_max_confidence(tmp_path):
    db = _db(tmp_path)
    db.checkpoint(agent_id="agent1", session_id="sess-1")
    db.write_compaction_summary(agent_id="agent1", session_id="sess-1", summary="done")

    cp = db.read("memory/compaction/checkpoint/sess-1")
    sm = db.read("memory/compaction/summary/sess-1")
    assert cp.confidence == 1.0
    assert sm.confidence == 1.0


def test_agent_summary_is_decayable_human_summary_immune(tmp_path):
    """Provenance integrity (T02): an agent_inferred summary is subject to
    lifecycle archival when stale, while an explicitly human_approved one is
    immune. Machine output never gets human immunity by default."""
    from datetime import datetime, timedelta, timezone

    db = _db(tmp_path)
    db.write_compaction_summary(agent_id="a1", session_id="s1", summary="old agent text")
    db.write_compaction_summary(
        agent_id="a1", session_id="s2", summary="approved text", origin="human_approved"
    )

    # Rewind both records so they are stale.
    stale = datetime.now(timezone.utc) - timedelta(days=200)
    for key in ("memory/compaction/summary/s1", "memory/compaction/summary/s2"):
        rec = db._store.get_by_key(key)
        rec.created_at = stale
        rec.last_retrieved = None
        db._store.write(rec)

    result = db.lifecycle.run_sweep()
    assert result["archived"] == 1

    # Agent summary archived (get_by_key returns None once archived).
    assert db._store.get_by_key("memory/compaction/summary/s1") is None
    # Human-approved summary survives.
    survivor = db._store.get_by_key("memory/compaction/summary/s2")
    assert survivor is not None and not survivor.archived
