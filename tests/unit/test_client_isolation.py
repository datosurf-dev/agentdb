from pathlib import Path

from agentdb import AgentDB
from agentdb.interface.client import EVENT_MEMORY_RETRIEVE


PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def test_search_isolation_survives_truncation_by_other_agents_volume(tmp_path):
    """Unchanged from the prior spec -- MemoryStore.search()'s own agent_id
    filter is unaffected by retrieve()'s signature change."""
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

    for i in range(60):
        db.write(key=f"memory/working/other_{i}", value="a widget note", origin="agent_inferred", agent_id="research_agent")
    db.write(key="memory/working/mine", value="a widget note", origin="agent_inferred", agent_id="coder_agent")

    results = db._store.search("widget", limit=5, agent_id="coder_agent")

    assert [r.key for r in results] == ["memory/working/mine"]


def _mem_dir_with_isolated_agent(tmp_path, agent_id):
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    import yaml
    (d / "config" / "memory.yaml").write_text(
        yaml.dump({"profile": {"name": "budget_optimized", "agents": {agent_id: {}}}}), encoding="utf-8",
    )
    return d


def _write_two_agents(db, key_suffix="note", **write_kwargs):
    db.write(key=f"memory/working/{key_suffix}", value="a widget note", origin="agent_inferred", agent_id="research_agent", **write_kwargs)
    db.write(key=f"memory/working/{key_suffix}_mine", value="a widget note", origin="agent_inferred", agent_id="coder_agent", **write_kwargs)


def test_retrieve_isolates_when_agent_is_listed_under_profile_agents(tmp_path):
    db = AgentDB(memory_dir=_mem_dir_with_isolated_agent(tmp_path, "coder_agent"), permissions_path=PERMS)
    _write_two_agents(db)

    result = db.retrieve("widget", agent_id="coder_agent")

    assert [r.key for r in result.records] == ["memory/working/note_mine"]


def test_retrieve_returns_mixed_pool_when_agent_not_listed(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)  # no memory.yaml at all -> never isolated
    _write_two_agents(db)

    result = db.retrieve("widget", agent_id="coder_agent")

    assert {r.key for r in result.records} == {"memory/working/note", "memory/working/note_mine"}


def test_retrieve_explicit_limit_overrides_config_default(tmp_path):
    db = AgentDB(memory_dir=_mem_dir_with_isolated_agent(tmp_path, "coder_agent"), permissions_path=PERMS)
    for i in range(10):
        db.write(key=f"memory/working/note_{i}", value="a widget note", origin="agent_inferred", agent_id="coder_agent")

    result = db.retrieve("widget", agent_id="coder_agent", limit=2)

    assert len(result.records) == 2


def test_retrieve_by_entities_isolates_when_agent_is_listed(tmp_path):
    db = AgentDB(memory_dir=_mem_dir_with_isolated_agent(tmp_path, "coder_agent"), permissions_path=PERMS)
    _write_two_agents(db, entities=["widget_entity"])

    result = db.retrieve_by_entities(["widget_entity"], agent_id="coder_agent")

    assert [r.key for r in result.records] == ["memory/working/note_mine"]


def test_retrieve_by_entities_returns_mixed_pool_when_agent_not_listed(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    _write_two_agents(db, entities=["widget_entity"])

    result = db.retrieve_by_entities(["widget_entity"], agent_id="coder_agent")

    assert {r.key for r in result.records} == {"memory/working/note", "memory/working/note_mine"}
