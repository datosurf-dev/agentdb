import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agentdb import AgentDB


PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def test_retrieve_logs_event_and_updates_retrieval_counts(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/task_context",
        value="user prefers concise bullet points",
        origin="agent_inferred",
        agent_id="research_agent",
        session_id="sess-1",
    )

    result = db.retrieve(query="bullet", agent_id="research_agent", session_id="sess-1")

    assert result.conflicts == []
    assert len(result.records) == 1
    assert result.records[0].retrieval_count == 1
    entries = db.audit_log.replay(from_sequence=0)
    assert any(entry.event_type == "memory_retrieve" for entry in entries)


def test_record_outcome_applies_origin_sensitive_confidence_rules(tmp_path):
    perms = tmp_path / "permissions.allow.skills.yaml"
    perms.write_text(
        "\n".join(
            [
                "default_policy: deny",
                "human_zone_prefixes:",
                "  - \"skills/\"",
                "agents:",
                "  research_agent:",
                "    can_write:",
                "      - \"memory/working/\"",
                "      - \"memory/provider/\"",
                "      - \"skills/\"",
                "    can_read:",
                "      - \"memory/working/\"",
                "      - \"memory/provider/\"",
                "      - \"skills/\"",
            ]
        )
    )
    db = AgentDB(path=tmp_path / "test.db", permissions_path=perms)
    agent = "research_agent"
    sess = "sess-2"

    agent_rec = db.write(
        key="memory/working/agent_fact",
        value="candidate preference",
        origin="agent_inferred",
        agent_id=agent,
        confidence=0.50,
        session_id=sess,
    )
    provider_rec = db.write(
        key="memory/provider/provider_fact",
        value="synced provider memory",
        origin="provider_ingested",
        agent_id=agent,
        confidence=0.50,
        session_id=sess,
    )
    human_rec = db.write(
        key="skills/writing-style",
        value="always use type hints",
        origin="human_approved",
        agent_id=agent,
        confidence=0.50,
        session_id=sess,
    )

    db.retrieve(query="memory", agent_id=agent, session_id=sess)
    adjusted = db.record_outcome(session_id=sess, agent_id=agent, outcome_type="task_complete", outcome_value=1.0)

    assert adjusted == 1
    assert db._store._by_id[agent_rec.id].confidence == 0.55
    assert db._store._by_id[provider_rec.id].confidence == 0.50
    assert db._store._by_id[human_rec.id].confidence == 0.50

    db.retrieve(query="memory", agent_id=agent, session_id=sess)
    adjusted_negative = db.record_outcome(
        session_id=sess,
        agent_id=agent,
        outcome_type="human_correction",
        outcome_value=0.0,
    )
    assert adjusted_negative == 2
    assert db._store._by_id[agent_rec.id].confidence == 0.53
    assert db._store._by_id[provider_rec.id].confidence == 0.48
    assert db._store._by_id[human_rec.id].confidence == 0.50


def test_last_retrieved_and_conflict_id_persist_across_store_reload(tmp_path):
    db_path = tmp_path / "test.db"
    db = AgentDB(path=db_path, permissions_path=PERMS)
    record = db.write(
        key="memory/working/x", value="v", origin="agent_inferred", agent_id="research_agent"
    )
    record.last_retrieved = datetime.now(timezone.utc)
    record.conflict_id = "conf-1"
    db._store.write(record)

    reloaded_db = AgentDB(path=db_path, permissions_path=PERMS)
    reloaded = reloaded_db.read("memory/working/x")
    assert reloaded.last_retrieved is not None
    assert reloaded.conflict_id == "conf-1"


def test_store_tolerates_legacy_schema_missing_new_columns(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            origin TEXT NOT NULL,
            trust_level TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            version INTEGER NOT NULL DEFAULT 1,
            archived INTEGER NOT NULL DEFAULT 0,
            retrieval_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            entities TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    conn.commit()
    conn.close()

    db = AgentDB(path=db_path, permissions_path=PERMS)
    db.write(key="memory/working/x", value="v", origin="agent_inferred", agent_id="research_agent")
    assert db.read("memory/working/x").value == "v"


def test_retrieve_ranks_human_approved_above_agent_inferred(tmp_path):
    perms = tmp_path / "permissions.allow.skills.yaml"
    perms.write_text(
        "\n".join(
            [
                "default_policy: deny",
                "human_zone_prefixes:",
                "  - \"skills/\"",
                "agents:",
                "  research_agent:",
                "    can_write:",
                "      - \"memory/working/\"",
                "      - \"skills/\"",
                "    can_read:",
                "      - \"memory/working/\"",
                "      - \"skills/\"",
            ]
        )
    )
    db = AgentDB(path=tmp_path / "test.db", permissions_path=perms)
    db.write(
        key="memory/working/agent_note",
        value="dbt model naming convention",
        origin="agent_inferred",
        agent_id="research_agent",
    )
    db.write(
        key="skills/dbt-convention",
        value="dbt model naming convention: prefix with stg_",
        origin="human_approved",
        agent_id="research_agent",
    )

    result = db.retrieve(query="convention", agent_id="research_agent")

    assert len(result.records) == 2
    assert result.records[0].origin == "human_approved"


def test_retrieve_logs_real_composite_signal_scores(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/task_context",
        value="user prefers concise bullet points",
        origin="agent_inferred",
        agent_id="research_agent",
    )

    db.retrieve(query="bullet", agent_id="research_agent")

    entries = db.audit_log.replay(from_sequence=0)
    retrieve_entry = next(e for e in entries if e.event_type == "memory_retrieve")
    scores = list(retrieve_entry.payload["signal_scores"].values())
    assert scores[0] != 1.0


def test_retrieve_updates_last_retrieved_for_recency_scoring(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/task_context",
        value="user prefers concise bullet points",
        origin="agent_inferred",
        agent_id="research_agent",
    )
    assert db.read("memory/working/task_context").last_retrieved is None

    db.retrieve(query="bullet", agent_id="research_agent")

    assert db.read("memory/working/task_context").last_retrieved is not None


def test_agentdb_tracking_configuration_is_initialized(tmp_path):
    db = AgentDB(
        path=tmp_path / "test.db",
        permissions_path=PERMS,
        tracking_directories=["docs", ".claude/skills"],
        tracking_root=tmp_path,
        tracking_max_files_per_directory=123,
    )
    assert db.tracking_directories == ["docs", ".claude/skills"]
    assert db.tracking_root == tmp_path.resolve()
    assert db.tracking_max_files_per_directory == 123




