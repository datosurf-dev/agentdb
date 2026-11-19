"""Tests for AgentDB.purge_session — hard removal across every store."""

import json
from datetime import datetime, timezone

import pytest

from agentdb import AgentDB
from agentdb.ingest import ConversationTurn

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    return AgentDB(memory_dir=str(tmp_path / "ws"))


def seed(db, agent_id="a1", session_id="s1", key="memory/working/style", value="bullets"):
    return db.ingest_turns(
        agent_id=agent_id,
        session_id=session_id,
        turns=[ConversationTurn(
            role="user", content=f"turn for {session_id}",
            timestamp=NOW, turn_id="t1", memories=((key, value),),
        )],
    )


def test_requires_explicit_confirmation(db):
    with pytest.raises(ValueError, match="irreversible"):
        db.purge_session("a1", "s1")


def test_removes_memories_from_the_store(db):
    seed(db)
    assert db._store.get_by_key("memory/working/style") is not None
    db.purge_session("a1", "s1", require_confirmation=False)
    # gone from the in-memory cache and from the database itself
    assert db._store.get_by_key("memory/working/style") is None
    with db._store._connect() as conn:
        remaining = conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
    assert remaining == 0


def test_removes_the_conversation_log(db):
    seed(db)
    path = db.memory_dir / "conversations" / "a1--s1.jsonl"
    assert path.exists()
    result = db.purge_session("a1", "s1", require_confirmation=False)
    assert not path.exists()
    assert result.jsonl_lines_removed == 1


def test_removes_the_ledger_so_reingest_is_not_silently_skipped(db):
    turns = [ConversationTurn(role="user", content="x", timestamp=NOW, turn_id="t1")]
    db.ingest_turns(agent_id="a1", session_id="s1", turns=turns)
    db.purge_session("a1", "s1", require_confirmation=False)
    # after a purge the same log may be ingested again
    again = db.ingest_turns(agent_id="a1", session_id="s1", turns=turns)
    assert again.accepted == 1


def test_does_not_touch_other_sessions(db):
    seed(db, session_id="s1", key="memory/a", value="one")
    seed(db, session_id="s2", key="memory/b", value="two")
    db.purge_session("a1", "s1", require_confirmation=False)
    assert db._store.get_by_key("memory/a") is None
    assert db._store.get_by_key("memory/b") is not None


def test_does_not_touch_other_agents(db):
    seed(db, agent_id="a1", key="memory/a", value="one")
    seed(db, agent_id="a2", key="memory/b", value="two")
    db.purge_session("a1", "s1", require_confirmation=False)
    assert db._store.get_by_key("memory/b") is not None


def test_audit_keeps_the_event_but_not_the_content(db):
    seed(db, key="memory/secret", value="super-secret-token")
    db.purge_session("a1", "s1", require_confirmation=False)
    rows = db.audit_log.replay()
    events = [r for r in rows if r.event_type == "session_purged"]
    assert events, "purge must be evidenced in the audit log"
    payload = json.dumps(events[-1].payload)
    assert "super-secret-token" not in payload
    assert "s1" in payload


def test_skills_derived_from_the_session_survive(db):
    skill = db.skills_api.create_skill(
        skill_id="deploy-check",
        name="deploy-check",
        content="verify CI before deploy",
        scope=["a1"],
    )
    seed(db)
    db.purge_session("a1", "s1", require_confirmation=False)
    assert db.skills_api.get_skill(skill.id) is not None
