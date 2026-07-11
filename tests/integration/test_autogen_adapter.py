"""
Tests AgentDBMemory against the autogen_core.memory.Memory ABC contract.
Does NOT require autogen-core installed — tests the interface contract directly.

Memory ABC requires:
  update_context(model_context) -> None   inject memories into model context
  query(query, cancellation_token) -> MemoryQueryResult
  add(content, cancellation_token) -> None
  clear() -> None
  close() -> None
"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from agentdb import AgentDB
from agentdb.adapters.autogen import AgentDBMemory

PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"

@pytest.fixture
def db(tmp_path):
    return AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

@pytest.fixture
def memory(db):
    return AgentDBMemory(db=db, agent_id="research_agent")

@pytest.mark.asyncio
async def test_add_and_query(memory):
    content = MagicMock()
    content.content = "the user prefers concise bullet points"
    await memory.add(content, cancellation_token=None)
    result = await memory.query("bullet points", cancellation_token=None)
    assert hasattr(result, "results")
    assert len(result.results) >= 1

@pytest.mark.asyncio
async def test_query_no_match_returns_empty(memory):
    content = MagicMock()
    content.content = "the user prefers markdown"
    await memory.add(content, cancellation_token=None)
    result = await memory.query("completely irrelevant query xyz", cancellation_token=None)
    assert result.results == []

@pytest.mark.asyncio
async def test_clear_removes_all(memory):
    content = MagicMock()
    content.content = "some memory"
    await memory.add(content, cancellation_token=None)
    await memory.clear()
    result = await memory.query("memory", cancellation_token=None)
    assert result.results == []

@pytest.mark.asyncio
async def test_update_context_adds_memories(memory):
    content = MagicMock()
    content.content = "important context about the user"
    await memory.add(content, cancellation_token=None)
    model_context = MagicMock()
    model_context.add_message = AsyncMock()
    await memory.update_context(model_context)
    assert model_context.add_message.called

@pytest.mark.asyncio
async def test_close_does_not_raise(memory):
    await memory.close()

@pytest.mark.asyncio
async def test_add_logged_to_audit(memory, db):
    content = MagicMock()
    content.content = "test memory"
    await memory.add(content, cancellation_token=None)
    entries = db.audit_log.replay(from_sequence=0)
    assert any(e.event_type == "memory_write" for e in entries)
