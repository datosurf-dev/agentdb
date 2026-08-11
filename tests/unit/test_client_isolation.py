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


# ---------------------------------------------------------------------------
# Session-scoped episodic filtering
# ---------------------------------------------------------------------------

def test_episodic_records_from_other_sessions_excluded_when_session_id_given(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    current_session = "thread_abc"
    old_session = "thread_xyz"
    agent = "research_agent"

    # Episodic record from a different session
    db.write(
        key=f"memory/episodic/claude/{agent}/{old_session}/20260806T120000",
        value="prior session response",
        origin="agent_inferred",
        agent_id=agent,
    )
    # Episodic record from the current session
    db.write(
        key=f"memory/episodic/claude/{agent}/{current_session}/20260806T130000",
        value="current session response",
        origin="agent_inferred",
        agent_id=agent,
    )

    result = db.retrieve("response", agent_id=agent, session_id=current_session)

    keys = {r.key for r in result.records}
    assert f"memory/episodic/claude/{agent}/{old_session}/20260806T120000" not in keys
    assert f"memory/episodic/claude/{agent}/{current_session}/20260806T130000" in keys


def test_non_episodic_records_unaffected_by_session_filter(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    agent = "research_agent"

    db.write(
        key="memory/working/user_preference",
        value="prior session response",
        origin="agent_inferred",
        agent_id=agent,
    )

    result = db.retrieve("response", agent_id=agent, session_id="any_session")

    assert any("memory/working/user_preference" == r.key for r in result.records)


def test_episodic_records_not_filtered_when_no_session_id(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    agent = "research_agent"

    db.write(
        key="memory/episodic/claude/research_agent/old_session/20260806T120000",
        value="old response",
        origin="agent_inferred",
        agent_id=agent,
    )

    # No session_id — cross-session episodic records are returned
    result = db.retrieve("response", agent_id=agent)

    assert any("episodic" in r.key for r in result.records)


# ---------------------------------------------------------------------------
# Minimum composite score threshold
# ---------------------------------------------------------------------------

def _mem_dir_with_min_score(tmp_path, min_score: float):
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    import yaml
    (d / "config" / "memory.yaml").write_text(
        yaml.dump({
            "profile": {
                "name": "budget_optimized",
                "retrieval": {"min_composite_score": min_score},
            }
        }),
        encoding="utf-8",
    )
    return d


def test_min_composite_score_zero_returns_all_records(tmp_path):
    db = AgentDB(memory_dir=_mem_dir_with_min_score(tmp_path, 0.0), permissions_path=PERMS)
    db.write(key="memory/working/a", value="widget", origin="agent_inferred", agent_id="research_agent")
    db.write(key="memory/working/b", value="widget", origin="agent_inferred", agent_id="research_agent")

    result = db.retrieve("widget", agent_id="research_agent", limit=10)

    assert len(result.records) == 2


def test_min_composite_score_filters_low_scoring_records(tmp_path):
    db = AgentDB(memory_dir=_mem_dir_with_min_score(tmp_path, 0.99), permissions_path=PERMS)
    # agent_inferred + no retrieval history → composite score well below 0.99
    db.write(key="memory/working/a", value="widget", origin="agent_inferred", agent_id="research_agent")
    db.write(key="memory/working/b", value="widget", origin="human_approved", agent_id="research_agent")

    result = db.retrieve("widget", agent_id="research_agent", limit=10)

    # Only the human_approved record can score high enough at 0.99
    assert all(r.key == "memory/working/b" for r in result.records)


def test_min_composite_score_configurable_per_agent(tmp_path):
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    import yaml
    (d / "config" / "memory.yaml").write_text(
        yaml.dump({
            "profile": {
                "name": "budget_optimized",
                "agents": {
                    "research_agent": {"retrieval": {"min_composite_score": 0.99}},
                    "coder_agent": {"retrieval": {"min_composite_score": 0.0}},
                },
            }
        }),
        encoding="utf-8",
    )
    db = AgentDB(memory_dir=d, permissions_path=PERMS)
    db.write(key="memory/working/research_note", value="widget", origin="agent_inferred", agent_id="research_agent")
    db.write(key="memory/working/coder_note", value="widget", origin="agent_inferred", agent_id="coder_agent")

    strict_result = db.retrieve("widget", agent_id="research_agent", limit=10)
    loose_result = db.retrieve("widget", agent_id="coder_agent", limit=10)

    assert len(strict_result.records) == 0
    assert len(loose_result.records) == 1
