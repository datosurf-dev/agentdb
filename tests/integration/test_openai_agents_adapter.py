"""
Tests AgentDBSession against the openai.agents.Session protocol contract.
Does NOT require openai-agents installed — tests the interface contract directly.

The Session protocol requires:
  session_id: str
  get_items(limit) -> list[Any]
  add_items(items: list[Any]) -> None
  pop_item() -> Any | None
  clear_session() -> None
"""
import pytest
from pathlib import Path
from agentdb import AgentDB
from agentdb.adapters.openai_agents import AgentDBSession

PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"

@pytest.fixture
def db(tmp_path):
    return AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

@pytest.fixture
def session(db):
    return AgentDBSession(db=db, agent_id="research_agent",
                          session_id="sess-test-001")

def test_session_id_property(session):
    assert session.session_id == "sess-test-001"

def test_add_and_get_items(session):
    session.add_items([{"role": "user", "content": "hello"}])
    items = session.get_items(limit=10)
    assert len(items) == 1
    assert items[0]["role"] == "user"

def test_get_items_respects_limit(session):
    session.add_items([{"role": "user", "content": f"msg {i}"} for i in range(5)])
    items = session.get_items(limit=3)
    assert len(items) == 3
    assert items[0]["content"] == "msg 2"
    assert items[2]["content"] == "msg 4"

def test_get_items_returns_in_order(session):
    session.add_items([{"role": "user", "content": "first"}])
    session.add_items([{"role": "assistant", "content": "second"}])
    items = session.get_items(limit=10)
    assert items[0]["content"] == "first"
    assert items[1]["content"] == "second"

def test_pop_item_removes_last(session):
    session.add_items([{"role": "user", "content": "a"}])
    session.add_items([{"role": "user", "content": "b"}])
    popped = session.pop_item()
    assert popped["content"] == "b"
    remaining = session.get_items(limit=10)
    assert len(remaining) == 1

def test_pop_item_empty_returns_none(session):
    assert session.pop_item() is None

def test_clear_session_removes_all(session):
    session.add_items([{"role": "user", "content": "hello"}])
    session.clear_session()
    assert session.get_items(limit=10) == []

def test_two_sessions_are_isolated(db):
    s1 = AgentDBSession(db=db, agent_id="research_agent", session_id="s1")
    s2 = AgentDBSession(db=db, agent_id="research_agent", session_id="s2")
    s1.add_items([{"role": "user", "content": "session 1"}])
    assert s2.get_items(limit=10) == []

def test_session_writes_logged_to_audit(session, db):
    session.add_items([{"role": "user", "content": "test"}])
    entries = db.audit_log.replay(from_sequence=0)
    assert any(e.event_type == "memory_write" for e in entries)
