from pathlib import Path

from agentdb import AgentDB
from agentdb.interface.client import (
    EVENT_EXTERNAL_CONFLICT_DETECTED,
    EVENT_MEMORY_OUTCOME,
    EVENT_MEMORY_RETRIEVE,
)


PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def _build_db_with_seed_record(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/contract",
        value="contract payload baseline",
        origin="agent_inferred",
        agent_id="research_agent",
        session_id="sess-contract",
    )
    return db


def test_memory_retrieve_event_payload_schema(tmp_path):
    db = _build_db_with_seed_record(tmp_path)
    session_id = "sess-contract"

    db.retrieve(query="contract payload", agent_id="research_agent", session_id=session_id)
    retrieve_entry = next(
        entry for entry in db.audit_log.replay(from_sequence=0) if entry.event_type == EVENT_MEMORY_RETRIEVE
    )

    assert set(retrieve_entry.payload.keys()) == {
        "query",
        "record_ids",
        "signal_scores",
        "session_id",
        "trigger_source",
        "reconciliation_run_id",
        "isolate_by_agent",
    }
    assert isinstance(retrieve_entry.payload["query"], str)
    assert isinstance(retrieve_entry.payload["record_ids"], list)
    assert isinstance(retrieve_entry.payload["signal_scores"], dict)
    assert retrieve_entry.payload["session_id"] == session_id
    assert retrieve_entry.payload["trigger_source"] == "query"
    assert retrieve_entry.payload["reconciliation_run_id"] is None
    assert retrieve_entry.payload["isolate_by_agent"] is False


def test_external_conflict_event_payload_schema(tmp_path):
    db = _build_db_with_seed_record(tmp_path)
    session_id = "sess-contract"

    db.retrieve(
        query="contract payload",
        agent_id="research_agent",
        session_id=session_id,
        external_context=[{"source": "chat", "content": "contract payload overlap"}],
    )
    external_entry = next(
        entry
        for entry in db.audit_log.replay(from_sequence=0)
        if entry.event_type == EVENT_EXTERNAL_CONFLICT_DETECTED
    )

    assert set(external_entry.payload.keys()) == {
        "query",
        "count",
        "sources",
        "session_id",
        "trigger_source",
        "reconciliation_run_id",
    }
    assert isinstance(external_entry.payload["query"], str)
    assert isinstance(external_entry.payload["count"], int)
    assert isinstance(external_entry.payload["sources"], list)
    assert external_entry.payload["session_id"] == session_id
    assert external_entry.payload["trigger_source"] == "query"
    assert external_entry.payload["reconciliation_run_id"] is None


def test_memory_outcome_event_payload_schema(tmp_path):
    db = _build_db_with_seed_record(tmp_path)
    session_id = "sess-contract"

    db.retrieve(query="contract payload", agent_id="research_agent", session_id=session_id)
    db.record_outcome(
        session_id=session_id,
        agent_id="research_agent",
        outcome_type="task_complete",
        outcome_value=1.0,
    )
    outcome_entry = next(
        entry for entry in db.audit_log.replay(from_sequence=0) if entry.event_type == EVENT_MEMORY_OUTCOME
    )

    assert set(outcome_entry.payload.keys()) == {
        "session_id",
        "outcome_type",
        "outcome_value",
        "linked_retrieval_sequence",
        "adjusted_record_ids",
    }
    assert outcome_entry.payload["session_id"] == session_id
    assert isinstance(outcome_entry.payload["outcome_type"], str)
    assert isinstance(outcome_entry.payload["outcome_value"], float)
    assert isinstance(outcome_entry.payload["adjusted_record_ids"], list)
