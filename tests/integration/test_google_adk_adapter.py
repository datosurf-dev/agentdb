"""
Tests AgentDBMemoryService against the google.adk.memory.BaseMemoryService contract.
Does NOT require google-adk installed — tests the interface contract directly.

BaseMemoryService ABC requires:
  add_session_to_memory(session) -> None
  search_memory(app_name, user_id, query) -> SearchMemoryResponse

SearchMemoryResponse contains: memories: list[MemoryResult]
MemoryResult contains: content: Content, author: str, timestamp: datetime
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from agentdb import AgentDB
from agentdb.adapters.google_adk import AgentDBMemoryService

PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"

@pytest.fixture
def db(tmp_path):
    return AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

@pytest.fixture
def service(db):
    return AgentDBMemoryService(db=db, agent_id="research_agent")

def _make_session(app_name="myapp", user_id="user1", events=None):
    session = MagicMock()
    session.app_name = app_name
    session.user_id = user_id
    session.id = "sess-001"
    session.events = events or []
    return session

def _make_event(author="user", text="hello"):
    event = MagicMock()
    event.author = author
    event.content = MagicMock()
    event.content.parts = [MagicMock(text=text)]
    event.timestamp = 1000.0
    return event

def test_add_session_stores_events(service, db):
    session = _make_session(events=[_make_event(text="the user prefers JSON")])
    service.add_session_to_memory(session)
    stats = db.system.agentdb_stats()
    assert stats["total_memories"] >= 1

def test_search_memory_returns_response(service):
    session = _make_session(events=[_make_event(text="the user prefers JSON output")])
    service.add_session_to_memory(session)
    response = service.search_memory("myapp", "user1", "JSON")
    assert hasattr(response, "memories")
    assert isinstance(response.memories, list)
    assert len(response.memories) >= 1

def test_search_memory_finds_stored_content(service):
    session = _make_session(
        app_name="myapp", user_id="user1",
        events=[_make_event(text="the user prefers concise answers")]
    )
    service.add_session_to_memory(session)
    response = service.search_memory("myapp", "user1", "concise")
    texts = [m.content for m in response.memories]
    assert any("concise" in str(t) for t in texts)

def test_search_memory_different_user_isolated(service):
    session_a = _make_session(app_name="app", user_id="userA",
                               events=[_make_event(text="private data")])
    service.add_session_to_memory(session_a)
    response = service.search_memory("app", "userB", "private data")
    assert response.memories == []

def test_add_session_logged_to_audit(service, db):
    session = _make_session(events=[_make_event(text="test")])
    service.add_session_to_memory(session)
    entries = db.audit_log.replay(from_sequence=0)
    assert any(e.event_type == "memory_write" for e in entries)
