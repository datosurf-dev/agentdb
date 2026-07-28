import pytest
from pathlib import Path
from agentdb import AgentDB
from agentdb.adapters.hermes import AgentDBHermesProvider


@pytest.fixture
def db(tmp_path):
    return AgentDB(db_path=tmp_path / "test.db")


@pytest.fixture
def provider(db):
    p = AgentDBHermesProvider(db=db, agent_id="test-agent")
    p.initialize("session-1")
    return p


def _mem_dir_with_isolated_agent(tmp_path, agent_id):
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    import yaml
    (d / "config" / "memory.yaml").write_text(
        yaml.dump({"profile": {"name": "budget_optimized", "agents": {agent_id: {}}}}), encoding="utf-8",
    )
    return d


@pytest.fixture
def isolated_provider(tmp_path):
    isolated_db = AgentDB(
        memory_dir=_mem_dir_with_isolated_agent(tmp_path, "test-agent"),
    )
    p = AgentDBHermesProvider(db=isolated_db, agent_id="test-agent")
    p.initialize("session-1")
    return p


def test_initialize_sets_session_id(db):
    p = AgentDBHermesProvider(db=db, agent_id="a1")
    p.initialize("sess-abc")
    assert p._session_id == "sess-abc"


def test_initialize_registers_conflict_handler(db):
    p = AgentDBHermesProvider(db=db, agent_id="a1")
    p.initialize("sess-abc")
    assert "conflict_detected" in db.gateway._handlers


def test_prefetch_returns_records(provider, db):
    db.write(key="pref/k", value="hello hermes", origin="agent_inferred", agent_id="test-agent")
    records = provider.prefetch("hello", session_id="session-1")
    assert isinstance(records, list)


def test_prefetch_default_returns_mixed_pool(provider, db):
    db.write(key="pref/mine", value="hello hermes", origin="agent_inferred", agent_id="test-agent")
    db.write(key="pref/other", value="hello hermes", origin="agent_inferred", agent_id="other-agent")

    records = provider.prefetch("hello", session_id="session-1")

    assert {r.key for r in records} == {"pref/mine", "pref/other"}


def test_prefetch_isolates_by_agent_when_enabled(isolated_provider):
    isolated_provider._db.write(key="pref/mine", value="hello hermes", origin="agent_inferred", agent_id="test-agent")
    isolated_provider._db.write(key="pref/other", value="hello hermes", origin="agent_inferred", agent_id="other-agent")

    records = isolated_provider.prefetch("hello", session_id="session-1")

    assert [r.key for r in records] == ["pref/mine"]


def test_handle_tool_call_memory_search_isolates_by_agent_when_enabled(isolated_provider):
    isolated_provider._db.write(key="pref/mine", value="hello hermes", origin="agent_inferred", agent_id="test-agent")
    isolated_provider._db.write(key="pref/other", value="hello hermes", origin="agent_inferred", agent_id="other-agent")

    records = isolated_provider.handle_tool_call(
        "memory_search", {"query": "hello"}, session_id="session-1",
    )

    assert [r.key for r in records] == ["pref/mine"]


def test_sync_turn_writes_memory(provider, db):
    provider.sync_turn("user msg", "assistant reply", session_id="session-1")
    record = db.read("memory/hermes/test-agent/session-1/turn", agent_id="test-agent")
    assert record is not None
    assert record.value == "assistant reply"


def test_on_turn_start_dispatches_event(provider, db):
    dispatched = []
    db.gateway.subscribe("conversation_started", lambda **kw: dispatched.append(kw))
    provider.on_turn_start(2, "hi")
    assert len(dispatched) >= 1


def test_on_session_switch_updates_session_id(provider):
    provider.on_session_switch("new-session")
    assert provider._session_id == "new-session"


def test_system_prompt_block_returns_string(provider):
    result = provider.system_prompt_block()
    assert isinstance(result, str)


def test_on_pre_compress_returns_string_no_summarizer(provider):
    result = provider.on_pre_compress([{"role": "user", "content": "hi"}])
    assert isinstance(result, str)


def test_on_pre_compress_calls_summarizer_if_set(db, tmp_path):
    summarizer_called = []

    def fake_summarizer(messages):
        summarizer_called.append(messages)
        return "summary"

    db2 = AgentDB(db_path=tmp_path / "test2.db", summarizer=fake_summarizer)
    p = AgentDBHermesProvider(db=db2, agent_id="a1")
    p.initialize("s1")
    result = p.on_pre_compress([{"role": "user", "content": "hi"}])
    assert result == "summary"
    assert len(summarizer_called) == 1


def test_gateway_adapter_gateway_reference(provider, db):
    assert provider.gateway is db.gateway


def test_backup_paths_returns_list(provider):
    paths = provider.backup_paths()
    assert isinstance(paths, list)


def test_principle_12a_prefetch_via_framework_caller(db):
    """Principle 12a: framework-driven call (not direct) triggers governed retrieval."""
    db.write(key="fw/test", value="framework governed", origin="agent_inferred", agent_id="fw-agent")

    provider = AgentDBHermesProvider(db=db, agent_id="fw-agent")
    provider.initialize("fw-session")

    # Simulate Hermes MemoryManager invoking prefetch (framework path)
    class HermesMemoryManagerShim:
        def __init__(self, p): self._provider = p
        def call_prefetch(self, query, session_id):
            return self._provider.prefetch(query, session_id=session_id)

    mm = HermesMemoryManagerShim(provider)
    records = mm.call_prefetch("framework governed", session_id="fw-session")

    # Governed retrieval happened: verify AgentDB state changed (retrieval_count bumped)
    assert isinstance(records, list)
    stored = db.read("fw/test", agent_id="fw-agent")
    assert stored.retrieval_count > 0  # proves governance executed, not just callable


def test_principle_12a_sync_turn_via_framework_caller(db):
    """Principle 12a: framework-driven sync_turn call writes a governed record."""
    provider = AgentDBHermesProvider(db=db, agent_id="fw-agent2")
    provider.initialize("fw-session2")

    class HermesMemoryManagerShim:
        def __init__(self, p): self._provider = p
        def call_sync_turn(self, user, asst, session_id):
            return self._provider.sync_turn(user, asst, session_id=session_id)

    mm = HermesMemoryManagerShim(provider)
    mm.call_sync_turn("user msg", "assistant governed response", session_id="fw-session2")

    # Prove the framework caller path wrote a verifiable governed record
    record = db.read("memory/hermes/fw-agent2/fw-session2/turn", agent_id="fw-agent2")
    assert record is not None
    assert record.value == "assistant governed response"
    assert record.origin == "agent_inferred"
