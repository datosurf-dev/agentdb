from pathlib import Path

from agentdb import AgentDB
from agentdb.interface.client import EVENT_MEMORY_RETRIEVE


PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def test_write_persists_entities_on_record(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

    record = db.write(
        key="memory/working/model_hint",
        value="remember full-refresh for this model",
        origin="agent_inferred",
        agent_id="research_agent",
        entities=["models/marts/core/report.sql", "report_model"],
    )

    assert record.entities == ["models/marts/core/report.sql", "report_model"]


def test_retrieve_by_entities_matches_explicit_entities_and_logs_entity_trigger(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/model_hint",
        value="remember full-refresh for this model",
        origin="agent_inferred",
        agent_id="research_agent",
        entities=["report_model"],
        session_id="sess-entity-1",
    )

    result = db.retrieve_by_entities(
        ["report_model"],
        agent_id="research_agent",
        session_id="sess-entity-1",
    )

    assert len(result.records) == 1
    assert result.records[0].key == "memory/working/model_hint"
    entry = next(
        event for event in db.audit_log.replay(from_sequence=0) if event.event_type == EVENT_MEMORY_RETRIEVE
    )
    assert entry.payload["trigger_source"] == "entity"
    assert entry.payload["entities"] == ["report_model"]


def test_retrieve_by_entities_falls_back_to_key_and_value_match(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/dbt/report_model",
        value="run with --full-refresh",
        origin="agent_inferred",
        agent_id="research_agent",
    )

    result = db.retrieve_by_entities(["report_model"], agent_id="research_agent")

    assert len(result.records) == 1
    assert result.records[0].key == "memory/working/dbt/report_model"


def test_retrieve_by_entities_no_match_is_noop_result(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/other",
        value="unrelated memory",
        origin="agent_inferred",
        agent_id="research_agent",
    )

    result = db.retrieve_by_entities(["missing_entity"], agent_id="research_agent")

    assert result.records == []
    assert result.conflicts == []