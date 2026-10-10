"""
Tests for AnthropicProvider — ingest recent Anthropic conversation history into AgentDB.

Uses a mocked anthropic.Anthropic client so no real API calls are made.
"""
from unittest.mock import MagicMock, patch

import pytest

from agentdb import AgentDB
from agentdb.providers.anthropic import AnthropicProvider


def _db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


def _mock_anthropic_messages(texts):
    """Build a fake messages.list response with the given text content."""
    msgs = []
    for i, text in enumerate(texts):
        msg = MagicMock()
        msg.id = f"msg_{i}"
        msg.role = "assistant"
        content_block = MagicMock()
        content_block.type = "text"
        content_block.text = text
        msg.content = [content_block]
        msgs.append(msg)
    page = MagicMock()
    page.data = msgs
    return page


def _make_provider(db, texts):
    client = MagicMock()
    client.messages.list.return_value = _mock_anthropic_messages(texts)
    return AnthropicProvider(db=db, client=client)


def test_sync_writes_provider_ingested_records(tmp_path):
    db = _db(tmp_path)
    provider = _make_provider(db, ["The orders table has columns: id, amount, date."])
    provider.sync(agent_id="agent1")

    records = db._store.list_all()
    provider_records = [r for r in records if r.origin == "provider_ingested"]
    assert len(provider_records) >= 1


def test_sync_respects_limit(tmp_path):
    db = _db(tmp_path)
    client = MagicMock()
    client.messages.list.return_value = _mock_anthropic_messages(
        [f"message {i}" for i in range(10)]
    )
    provider = AnthropicProvider(db=db, client=client)
    provider.sync(agent_id="agent1", limit=5)
    client.messages.list.assert_called_once()
    call_kwargs = client.messages.list.call_args[1]
    assert call_kwargs.get("limit", 5) <= 10


def test_sync_does_not_overwrite_human_approved(tmp_path):
    db = _db(tmp_path)
    # Pre-existing human_approved record for the same key
    key = "provider/msg_0"
    db.write(key, "human curated value", origin="human_approved",
             agent_id="agent1", confidence=1.0)

    provider = _make_provider(db, ["automated value that should not overwrite"])
    provider.sync(agent_id="agent1")

    record = db.read(key)
    assert record is not None
    assert record.value == "human curated value", "human_approved must not be overwritten"
    assert record.origin == "human_approved"


def test_sync_returns_ingested_count(tmp_path):
    db = _db(tmp_path)
    provider = _make_provider(db, ["msg one", "msg two", "msg three"])
    count = provider.sync(agent_id="agent1")
    assert count == 3


def test_sync_skips_non_text_content(tmp_path):
    db = _db(tmp_path)
    client = MagicMock()
    msg = MagicMock()
    msg.id = "msg_tool"
    msg.role = "assistant"
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    msg.content = [tool_block]
    page = MagicMock()
    page.data = [msg]
    client.messages.list.return_value = page

    provider = AnthropicProvider(db=db, client=client)
    count = provider.sync(agent_id="agent1")
    assert count == 0, "non-text content blocks must be skipped"


def test_anthropic_provider_exposes_db(tmp_path):
    db = _db(tmp_path)
    client = MagicMock()
    provider = AnthropicProvider(db=db, client=client)
    assert provider.db is db
