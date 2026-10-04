import pytest
from agentdb import AgentDB
from agentdb.embeddings.provider import FunctionEmbeddingProvider


def _embed(text: str) -> list[float]:
    return [0.1] * 384


def test_no_embedding_by_default(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)
    assert db._embedding_provider is None
    assert db._embedding_store is None


def test_embed_fn_creates_function_provider(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=_embed)
    assert isinstance(db._embedding_provider, FunctionEmbeddingProvider)
    assert db._embedding_store is not None


def test_semantic_weight_zero_without_provider(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)
    assert db._ranker.w_semantic == 0.0


def test_semantic_weight_activated_when_provider_present(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=_embed)
    assert db._ranker.w_semantic > 0.0


def test_ranker_weights_sum_to_one_with_provider(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=_embed)
    r = db._ranker
    total = (r.w_provenance + r.w_recency + r.w_confidence
             + r.w_causal + r.w_semantic + r.w_workspace)
    assert abs(total - 1.0) < 1e-6


def test_ranker_weights_sum_to_one_without_provider(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)
    r = db._ranker
    total = (r.w_provenance + r.w_recency + r.w_confidence
             + r.w_causal + r.w_semantic + r.w_workspace)
    assert abs(total - 1.0) < 1e-6


def test_session_topic_stored_on_conversation_started(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=_embed)
    db.gateway.dispatch(
        "conversation_started",
        session_id="sess1",
        opening_message="Let's work on the auth module",
    )
    result = db._embedding_store.get_session_topic("sess1")
    assert result is not None
    assert len(result) == 384


def test_no_session_topic_handler_without_provider(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)
    db.gateway.dispatch(
        "conversation_started", session_id="sess1", opening_message="hello"
    )
    # No embedding store, so nothing to check — just assert no crash


def test_embed_fn_takes_priority_over_local_embed(tmp_path):
    db = AgentDB(
        path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path,
        embed_fn=_embed, local_embed=True
    )
    assert isinstance(db._embedding_provider, FunctionEmbeddingProvider)


def test_skill_store_list_all_active_ids(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)
    db.skills_api.create_skill("s1", "My Skill", "Do things.", scope=["agent1"])
    ids = db.skills.list_all_active_ids()
    assert "s1:1.0.0" in ids


def test_skill_store_list_all_active_ids_excludes_archived(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)
    db.skills_api.create_skill("s1", "My Skill", "Do things.", scope=["agent1"])
    db.skills_api.archive_skill("s1", "1.0.0")
    ids = db.skills.list_all_active_ids()
    assert "s1:1.0.0" not in ids
