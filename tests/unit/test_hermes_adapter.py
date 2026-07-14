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
