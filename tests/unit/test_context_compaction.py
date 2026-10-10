"""
Tests for checkpoint() and write_compaction_summary().

Both methods write human_approved records so they survive lifecycle sweeps
and are retrievable across session boundaries without working_dirs filtering.
"""
from agentdb import AgentDB


def _db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


def test_checkpoint_writes_recoverable_record(tmp_path):
    db = _db(tmp_path)
    db.checkpoint(agent_id="agent1", session_id="sess-1")
    results = db.retrieve(query="compaction checkpoint", agent_id="agent1", session_id="sess-2")
    assert any("checkpoint" in r.key for r in results.records)


def test_checkpoint_record_is_human_approved(tmp_path):
    db = _db(tmp_path)
    db.checkpoint(agent_id="agent1", session_id="sess-1")
    record = db.read("memory/compaction/checkpoint/sess-1")
    assert record is not None
    assert record.origin == "human_approved"


def test_write_compaction_summary_survives_lifecycle_sweep(tmp_path):
    db = _db(tmp_path)
    db.write_compaction_summary(
        agent_id="agent1", session_id="sess-1",
        summary="Completed refactor of orders pipeline. Next: test coverage.",
    )
    db.lifecycle.run_sweep()
    results = db.retrieve(query="compaction summary", agent_id="agent1", session_id="sess-2")
    assert any("summary" in r.key for r in results.records)


def test_compaction_summary_is_human_approved(tmp_path):
    db = _db(tmp_path)
    db.write_compaction_summary(agent_id="agent1", session_id="sess-1", summary="Summary text.")
    record = db.read("memory/compaction/summary/sess-1")
    assert record is not None
    assert record.origin == "human_approved"
    assert record.value == "Summary text."


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
