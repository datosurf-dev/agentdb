"""
Tests for write_compaction_summary().

The summary is retrievable across session boundaries. Per contract P-01 / ADB-03
it defaults to origin="agent_inferred" (machine/agent-produced) and never becomes
human_approved implicitly; a verified human action may pass origin="human_approved".

`checkpoint()` was removed: it wrote a record whose entire value was
``{"session_id": ..., "type": "checkpoint"}``, nothing in the package ever read
it, and its only observable effect was a line of noise in every injection that
retrieval could surface. The compaction SUMMARY is the record that carries the
handover content, and it is written at the same moment.
"""
from agentdb import AgentDB


def _db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


def test_checkpoint_is_gone(tmp_path):
    """Removed entirely: nothing read it, and it polluted every injection.

    A test rather than a comment so the removal is a contract: re-adding the
    method has to be a deliberate decision, not a quiet reintroduction.
    """
    db = _db(tmp_path)
    assert not hasattr(db, "checkpoint")


def test_end_session_writes_a_summary_but_no_checkpoint(tmp_path):
    """The summary is the record that survives a session; the tombstone is not."""
    from agentdb.adapters.middleware import AgentDBMiddleware
    from agentdb.adapters.capabilities import LOCAL_CAPABILITIES
    import asyncio

    db = _db(tmp_path)
    mw = AgentDBMiddleware(db=db, agent_id="agent1", capabilities=LOCAL_CAPABILITIES)
    jsonl = tmp_path / "session_summary.jsonl"
    asyncio.run(mw.end_session("sess-1", str(jsonl)))

    all_keys = [r.key for r in db.retrieve(
        query="session", agent_id="agent1", session_id="sess-1", limit=100,
    ).records]
    assert not any("compaction/checkpoint" in k for k in all_keys)
    # The session summary line is still written, as the durable record of the session.
    assert jsonl.exists()
    assert "agentdb_checkpoint_key" not in jsonl.read_text()


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


def test_compaction_summary_retrieves_across_session_boundary(tmp_path):
    """The summary survives the session that wrote it — this is the handover."""
    db = _db(tmp_path)
    db.write_compaction_summary(
        agent_id="agent1", session_id="sess-A", summary="Implemented the milestone.",
    )

    # Simulate a new session — session_id has changed but agent_id is same
    results = db.retrieve(query="compaction summary", agent_id="agent1", session_id="sess-B")
    assert any("summary" in r.key for r in results.records)


def test_compaction_summary_has_max_confidence(tmp_path):
    db = _db(tmp_path)
    db.write_compaction_summary(agent_id="agent1", session_id="sess-1", summary="done")

    sm = db.read("memory/compaction/summary/sess-1")
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
