"""Tests for AgentDB.ingest_turns — the parser-free conversation door."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agentdb import AgentDB
from agentdb.ingest import ConversationTurn, IngestValidationError


@pytest.fixture
def db(tmp_path):
    return AgentDB(memory_dir=str(tmp_path / "ws"))


NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def turn(role="user", content="hello", ts=None, turn_id="t1", memories=()):
    return ConversationTurn(
        role=role,
        content=content,
        timestamp=ts or NOW,
        turn_id=turn_id,
        memories=tuple(memories),
    )


def conv_path(db, agent_id="a1", session_id="s1"):
    return db.memory_dir / "conversations" / f"{agent_id}--{session_id}.jsonl"


# -- validation -------------------------------------------------------------

@pytest.mark.parametrize("kwargs,needle", [
    (dict(timestamp=None), "timestamp"),
    (dict(turn_id=""), "turn_id"),
    (dict(role="narrator"), "role"),
    (dict(content=None), "content"),
])
def test_invalid_turn_rejects_whole_batch(db, kwargs, needle):
    payload = dict(turn_id="t1", role="user", content="x", timestamp=NOW)
    payload.update(kwargs)
    bad = ConversationTurn(**payload)
    with pytest.raises(IngestValidationError, match=needle):
        db.ingest_turns(agent_id="a1", session_id="s1", turns=[turn(), bad])
    # nothing written
    assert not conv_path(db).exists()


def test_memory_pair_must_be_key_value(db):
    bad = turn(memories=("not-a-pair",))
    with pytest.raises(IngestValidationError, match="pairs"):
        db.ingest_turns(agent_id="a1", session_id="s1", turns=[bad])


def test_empty_batch_is_a_noop(db):
    result = db.ingest_turns(agent_id="a1", session_id="s1", turns=[])
    assert result.accepted == 0 and result.skipped == 0


# -- storage ----------------------------------------------------------------

def test_turns_land_in_the_capture_conversation_log(db):
    db.ingest_turns(agent_id="a1", session_id="s1", turns=[turn(content="hi")])
    lines = conv_path(db).read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[0])
    # same shape capture.conversation_jsonl produces, plus ingest markers
    assert entry["session_id"] == "s1"
    assert entry["message_id"] == "t1"
    assert entry["user_query"] == "hi"
    assert entry["ingested"] is True


def test_extracted_memories_are_stored_as_provider_ingested(db):
    db.ingest_turns(
        agent_id="a1", session_id="s1",
        turns=[turn(memories=[("memory/working/style", "bullets")])],
    )
    record = db._store.get_by_key("memory/working/style")
    assert record is not None
    assert record.value == "bullets"
    assert record.session_id == "s1"
    assert str(record.origin) == "provider_ingested"


def test_turns_are_not_stored_as_memories(db):
    db.ingest_turns(agent_id="a1", session_id="s1", turns=[turn(content="just chatter")])
    assert all("just chatter" not in json.dumps(r.value) for r in db._store._by_id.values())


# -- deduplication ----------------------------------------------------------

def test_reingest_same_batch_is_skipped(db):
    turns = [turn(content="a", turn_id="1"), turn(content="b", turn_id="2")]
    first = db.ingest_turns(agent_id="a1", session_id="s1", turns=turns)
    second = db.ingest_turns(agent_id="a1", session_id="s1", turns=turns)
    assert (first.accepted, first.skipped) == (2, 0)
    assert (second.accepted, second.skipped) == (0, 2)


def test_dedup_ignores_turn_id_so_regenerated_ids_are_caught(db):
    a = turn(content="same", turn_id="id-one")
    b = turn(content="same", turn_id="id-totally-different")
    db.ingest_turns(agent_id="a1", session_id="s1", turns=[a])
    result = db.ingest_turns(agent_id="a1", session_id="s1", turns=[b])
    assert result.skipped == 1


def test_same_text_at_a_different_time_is_kept(db):
    db.ingest_turns(agent_id="a1", session_id="s1", turns=[turn(content="approved", turn_id="1")])
    later = turn(content="approved", ts=NOW + timedelta(hours=1), turn_id="2")
    result = db.ingest_turns(agent_id="a1", session_id="s1", turns=[later])
    assert result.accepted == 1


def test_dedup_is_scoped_per_session(db):
    t = turn(content="same text")
    db.ingest_turns(agent_id="a1", session_id="s1", turns=[t])
    other = db.ingest_turns(agent_id="a1", session_id="s2", turns=[t])
    assert other.accepted == 1


def test_dedup_is_scoped_per_agent(db):
    t = turn(content="same text")
    db.ingest_turns(agent_id="a1", session_id="s1", turns=[t])
    other = db.ingest_turns(agent_id="a2", session_id="s1", turns=[t])
    assert other.accepted == 1


# -- tool turns -------------------------------------------------------------

def test_tool_turn_memories_are_ignored_with_a_warning(db):
    with pytest.warns(UserWarning, match="tool turn"):
        result = db.ingest_turns(
            agent_id="a1", session_id="s1",
            turns=[turn(role="tool", content="dump", memories=[("memory/x", "no")])],
        )
    assert result.memory_records == ()
    assert db._store.get_by_key("memory/x") is None
    # but the raw turn is still logged
    assert conv_path(db).exists()


# -- idempotency across instances (persisted ledger) ------------------------

def test_ledger_survives_a_new_instance(tmp_path):
    ws = str(tmp_path / "ws")
    turns = [turn(content="persisted", turn_id="1")]
    AgentDB(memory_dir=ws).ingest_turns(agent_id="a1", session_id="s1", turns=turns)
    again = AgentDB(memory_dir=ws).ingest_turns(agent_id="a1", session_id="s1", turns=turns)
    assert again.skipped == 1
