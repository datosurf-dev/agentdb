from pathlib import Path

from agentdb import AgentDB
from agentdb.interface.client import EVENT_MEMORY_RETRIEVE


PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def test_search_isolation_survives_truncation_by_other_agents_volume(tmp_path):
    """A single matching record for the calling agent must not be truncated
    away by another agent's larger matching set — the agent filter has to be
    applied during the scan, before search()'s internal limit slice, not
    after."""
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

    for i in range(60):
        db.write(
            key=f"memory/working/other_{i}",
            value="a widget note",
            origin="agent_inferred",
            agent_id="research_agent",
        )
    db.write(
        key="memory/working/mine",
        value="a widget note",
        origin="agent_inferred",
        agent_id="coder_agent",
    )

    results = db._store.search("widget", limit=5, agent_id="coder_agent")

    assert [r.key for r in results] == ["memory/working/mine"]


def _write_two_agents(db, key_suffix="note", **write_kwargs):
    db.write(
        key=f"memory/working/{key_suffix}",
        value="a widget note",
        origin="agent_inferred",
        agent_id="research_agent",
        **write_kwargs,
    )
    db.write(
        key=f"memory/working/{key_suffix}_mine",
        value="a widget note",
        origin="agent_inferred",
        agent_id="coder_agent",
        **write_kwargs,
    )


def test_retrieve_isolate_by_agent_true_excludes_other_agent_records(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    _write_two_agents(db)

    result = db.retrieve("widget", agent_id="coder_agent", isolate_by_agent=True)

    assert [r.key for r in result.records] == ["memory/working/note_mine"]


def test_retrieve_isolate_by_agent_default_false_returns_mixed_pool(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    _write_two_agents(db)

    result = db.retrieve("widget", agent_id="coder_agent")

    assert {r.key for r in result.records} == {
        "memory/working/note",
        "memory/working/note_mine",
    }


def test_retrieve_audit_log_records_isolate_by_agent(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    _write_two_agents(db)

    db.retrieve("widget", agent_id="coder_agent", isolate_by_agent=True)

    entry = next(
        event for event in db.audit_log.replay(from_sequence=0) if event.event_type == EVENT_MEMORY_RETRIEVE
    )
    assert entry.payload["isolate_by_agent"] is True


def test_retrieve_by_entities_isolate_by_agent_true_excludes_other_agent_records(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    _write_two_agents(db, entities=["widget_entity"])

    result = db.retrieve_by_entities(["widget_entity"], agent_id="coder_agent", isolate_by_agent=True)

    assert [r.key for r in result.records] == ["memory/working/note_mine"]


def test_retrieve_by_entities_isolate_by_agent_default_false_returns_mixed_pool(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    _write_two_agents(db, entities=["widget_entity"])

    result = db.retrieve_by_entities(["widget_entity"], agent_id="coder_agent")

    assert {r.key for r in result.records} == {
        "memory/working/note",
        "memory/working/note_mine",
    }
