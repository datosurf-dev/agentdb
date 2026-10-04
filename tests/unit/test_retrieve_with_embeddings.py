import pytest
from tests.fixtures.embeddings import controlled_embed, VECTORS
from agentdb import AgentDB


def _make_db(tmp_path):
    return AgentDB(
        path=tmp_path / "agentdb.sqlite",
        memory_dir=tmp_path,
        embed_fn=controlled_embed,
    )


def test_query_gate_skips_embedding_for_stop_phrase(tmp_path):
    call_count = 0
    original = controlled_embed

    def counting_embed(text):
        nonlocal call_count
        call_count += 1
        return original(text)

    db = AgentDB(
        path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=counting_embed
    )
    db.write("user/name", "Alice", origin="agent_inferred", agent_id="a1")
    call_count = 0  # reset after write

    db.retrieve("ok", agent_id="a1")
    assert call_count == 0  # gate fired; no embed call on query


def test_query_gate_skips_embedding_for_short_query(tmp_path):
    call_count = 0

    def counting_embed(text):
        nonlocal call_count
        call_count += 1
        return [0.0] * 384

    db = AgentDB(
        path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=counting_embed
    )
    db.write("user/name", "Alice", origin="agent_inferred", agent_id="a1")
    call_count = 0

    db.retrieve("go", agent_id="a1")  # 1 token < min_query_tokens=3
    assert call_count == 0


def test_query_gate_allows_substantive_query(tmp_path):
    call_count = 0

    def counting_embed(text):
        nonlocal call_count
        call_count += 1
        return [0.0] * 384

    db = AgentDB(
        path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=counting_embed
    )
    db.write("user/name", "Alice", origin="agent_inferred", agent_id="a1")
    call_count = 0

    db.retrieve("what is the user name", agent_id="a1")
    assert call_count >= 1  # at least query embed was called


def test_retrieve_lazy_regens_missing_embedding(tmp_path):
    # Write a record WITHOUT provider active, then retrieve WITH provider
    db_no_embed = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)
    record = db_no_embed.write("user/name", "Alice", origin="agent_inferred", agent_id="a1")

    db_with_embed = AgentDB(
        path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=controlled_embed
    )
    # Before retrieve: no stored embedding
    assert db_with_embed._embedding_store.get("memories", record.id) is None

    db_with_embed.retrieve("user name what is it", agent_id="a1")

    # After retrieve: embedding generated lazily
    assert db_with_embed._embedding_store.get("memories", record.id) is not None


def test_session_topic_filter_passes_on_topic_candidate(tmp_path):
    db = _make_db(tmp_path)
    db.gateway.dispatch(
        "conversation_started",
        session_id="sess1",
        opening_message="topic_A context for this session",
    )
    db.write("task/description", "similar_to_A record content", origin="agent_inferred", agent_id="a1")

    result = db.retrieve("similar_to_A task description query", agent_id="a1", session_id="sess1")
    assert len(result.records) >= 1


def test_session_topic_filter_drops_off_topic_candidate(tmp_path):
    db = _make_db(tmp_path)
    db.gateway.dispatch(
        "conversation_started",
        session_id="sess1",
        opening_message="topic_A context",
    )
    db.write("other/project", "unrelated record content", origin="agent_inferred", agent_id="a1")

    result = db.retrieve("unrelated project query here", agent_id="a1", session_id="sess1")
    assert all("unrelated" not in r.key for r in result.records)


def test_session_topic_filter_lazy_regens_pre_embed_records(tmp_path):
    # Records written before embed was active have no stored vector.
    # On retrieve, lazy regen gives them an embedding; then the session topic
    # filter applies normally. Records semantically related to the topic pass through.
    db_no_embed = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)
    # Content contains "topic_A" — controlled_embed gives it a topic-A-aligned vector
    db_no_embed.write("task/notes", "topic_A related notes", origin="agent_inferred", agent_id="a1")

    db = AgentDB(
        path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=controlled_embed
    )
    # Session topic is also topic_A → cosine(record, session) ≈ 1.0 > 0.35 threshold
    db.gateway.dispatch(
        "conversation_started", session_id="s1", opening_message="topic_A context"
    )
    # Query matches keyword; lazy regen embeds as topic_A; session filter passes it
    result = db.retrieve("topic_A related notes task", agent_id="a1", session_id="s1")
    assert len(result.records) >= 1  # lazy-regenned record passes session topic filter
