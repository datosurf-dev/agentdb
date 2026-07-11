from pathlib import Path

from agentdb import AgentDB


PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def test_system_stats_contract_keys_and_defaults(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

    stats = db.system.agentdb_stats()

    assert set(stats.keys()) == {
        "total_memories",
        "avg_confidence",
        "low_confidence_count",
        "conflict_pending_count",
        "outcome_positive_rate",
        "retrieval_count_total",
        "external_conflict_rate",
    }
    assert stats["total_memories"] == 0
    assert stats["avg_confidence"] == 0.0
    assert stats["low_confidence_count"] == 0
    assert stats["conflict_pending_count"] == 0
    assert stats["retrieval_count_total"] == 0
    assert stats["outcome_positive_rate"] is None
    assert stats["external_conflict_rate"] is None


def test_outcome_positive_rate_requires_min_sample_size(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    sess = "sess-stats-outcome"
    db.write(
        key="memory/working/item",
        value="sample memory",
        origin="agent_inferred",
        agent_id="research_agent",
        confidence=0.8,
        session_id=sess,
    )
    db.retrieve(query="sample", agent_id="research_agent", session_id=sess)

    for _ in range(9):
        db.record_outcome(
            session_id=sess,
            agent_id="research_agent",
            outcome_type="task_complete",
            outcome_value=1.0,
        )
    assert db.system.agentdb_stats()["outcome_positive_rate"] is None

    db.record_outcome(
        session_id=sess,
        agent_id="research_agent",
        outcome_type="human_correction",
        outcome_value=0.0,
    )
    assert db.system.agentdb_stats()["outcome_positive_rate"] == 0.9


def test_external_conflict_rate_reflects_conflict_events(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    sess = "sess-stats-external"
    db.write(
        key="memory/working/policy",
        value="prefer internal canonical docs",
        origin="agent_inferred",
        agent_id="research_agent",
        confidence=0.7,
        session_id=sess,
    )

    # Retrieval with overlap should emit external conflict event.
    db.retrieve(
        query="canonical docs",
        agent_id="research_agent",
        session_id=sess,
        external_context=[{"source": "chat", "content": "canonical docs from another source"}],
    )
    # Retrieval without overlap should not emit external conflict event.
    db.retrieve(
        query="canonical docs",
        agent_id="research_agent",
        session_id=sess,
        external_context=[{"source": "chat", "content": "unrelated text"}],
    )

    stats = db.system.agentdb_stats()
    assert stats["external_conflict_rate"] == 0.5
