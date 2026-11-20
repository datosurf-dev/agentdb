"""Phase B retrieval tests: hybrid candidates, scope parity, budget packing,
entity parity, and diagnostic trace (ADB-05, 08, 09, 10; T07-T11)."""
import pytest

from tests.fixtures.embeddings import controlled_embed
from agentdb import AgentDB


def _db_embed(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=controlled_embed)


def _db_lexical(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite")


# ---------------------------------------------------------------------------
# T07 — semantic recall without lexical overlap
# ---------------------------------------------------------------------------

def test_semantic_only_record_is_retrieved_without_lexical_overlap(tmp_path):
    db = _db_embed(tmp_path)
    # Query maps to topic_A vector. Record text maps to similar_to_A (cosine ~1.0)
    # but shares NO meaningful word with the query ("topic_A ...").
    db.write("m/alpha", "phrase containing similar_to_A marker",
             origin="agent_inferred", agent_id="a1", confidence=0.9)
    res = db.retrieve("topic_A documents please", agent_id="a1", limit=10)
    keys = [r.key for r in res.records]
    assert "m/alpha" in keys, f"semantic-only record should be retrieved, got {keys}"


def test_semantic_only_record_absent_when_embeddings_disabled(tmp_path):
    """Same corpus without embeddings: no lexical overlap -> not retrieved."""
    db = _db_lexical(tmp_path)
    db.write("m/alpha", "phrase containing similar_to_A marker",
             origin="agent_inferred", agent_id="a1", confidence=0.9)
    res = db.retrieve("topic_A documents please", agent_id="a1", limit=10)
    assert "m/alpha" not in [r.key for r in res.records]


# ---------------------------------------------------------------------------
# T08 — deterministic lexical fallback (embeddings off)
# ---------------------------------------------------------------------------

def test_lexical_fallback_returns_lexical_matches(tmp_path):
    db = _db_lexical(tmp_path)
    db.write("memory/orders/schema", "orders table new partition key",
             origin="agent_inferred", agent_id="a1")
    res = db.retrieve("orders partition", agent_id="a1")
    assert any(r.key == "memory/orders/schema" for r in res.records)


def test_embedding_failure_falls_back_to_lexical(tmp_path):
    def flaky_embed(text):
        raise RuntimeError("provider down")
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=flaky_embed)
    db.write("memory/orders/schema", "orders table partition key",
             origin="agent_inferred", agent_id="a1")
    # Should not raise; lexical path still works.
    res = db.retrieve("orders partition", agent_id="a1")
    assert any(r.key == "memory/orders/schema" for r in res.records)


# ---------------------------------------------------------------------------
# T09 — scope consistency across candidate branches
# ---------------------------------------------------------------------------

def test_semantic_candidates_respect_agent_scope(tmp_path):
    db = _db_embed(tmp_path)
    # Two agents, both semantic matches. With isolation, only own records returned.
    db.write("m/a1", "similar_to_A belonging to agent one",
             origin="agent_inferred", agent_id="agent1", confidence=0.9)
    db.write("m/a2", "similar_to_A belonging to agent two",
             origin="agent_inferred", agent_id="agent2", confidence=0.9)
    # Configure explicit per-agent profiles to enable isolate_by_agent.
    db.memory_config = {"profile": {"name": "budget_optimized", "agents": {
        "agent1": {"name": "budget_optimized"}, "agent2": {"name": "budget_optimized"}}}}
    res = db.retrieve("topic_A query words here", agent_id="agent1", limit=10)
    assert all(r.agent_id == "agent1" for r in res.records)
    assert "m/a1" in [r.key for r in res.records]
    assert "m/a2" not in [r.key for r in res.records]


def test_expired_record_excluded_from_semantic_branch(tmp_path):
    from datetime import datetime, timedelta, timezone
    db = _db_embed(tmp_path)
    expired = datetime.now(timezone.utc) - timedelta(days=1)
    db.write("m/expired", "similar_to_A expired content",
             origin="agent_inferred", agent_id="a1", confidence=0.9, expiry_time=expired)
    res = db.retrieve("topic_A query words here", agent_id="a1", limit=10)
    assert "m/expired" not in [r.key for r in res.records]


# ---------------------------------------------------------------------------
# T10 — budget packing skips oversized, continues to smaller
# ---------------------------------------------------------------------------

def test_budget_skips_oversized_and_keeps_smaller(tmp_path):
    db = _db_lexical(tmp_path)
    # A large high-relevance record and a smaller one sharing the query words.
    db.write("memory/k1big", "orders " + ("x" * 4000),
             origin="human_approved", agent_id="a1")
    db.write("memory/k2small", "orders small",
             origin="agent_inferred", agent_id="a1")
    # Budget fitting only the small record's token estimate (~ a few).
    res = db.retrieve("orders", agent_id="a1", budget_tokens=20)
    keys = [r.key for r in res.records]
    assert "memory/k2small" in keys, f"smaller record should still fit, got {keys}"
    assert "memory/k1big" not in keys
    assert res.truncated is True
    # Budget honored on rendered estimate.
    assert res.token_estimate <= 20


def test_budget_zero_returns_nothing_and_truncates(tmp_path):
    db = _db_lexical(tmp_path)
    db.write("memory/k1", "orders value", origin="agent_inferred", agent_id="a1")
    res = db.retrieve("orders", agent_id="a1", budget_tokens=0)
    assert res.records == []
    assert res.truncated is True


# ---------------------------------------------------------------------------
# T11 — entity retrieval parity (filters + budget)
# ---------------------------------------------------------------------------

def test_entity_retrieval_respects_budget(tmp_path):
    db = _db_lexical(tmp_path)
    db.write("memory/e1big", "orders " + ("y" * 4000),
             origin="agent_inferred", agent_id="a1", entities=["Ord"])
    db.write("memory/e2small", "orders small",
             origin="agent_inferred", agent_id="a1", entities=["Ord"])
    res = db.retrieve_by_entities(["Ord"], agent_id="a1", budget_tokens=20)
    keys = [r.key for r in res.records]
    assert "memory/e2small" in keys
    assert res.token_estimate <= 20


def test_entity_retrieval_respects_agent_scope(tmp_path):
    db = _db_lexical(tmp_path)
    db.write("memory/e/a1", "x", origin="agent_inferred", agent_id="agent1", entities=["Ent"])
    db.write("memory/e/a2", "y", origin="agent_inferred", agent_id="agent2", entities=["Ent"])
    db.memory_config = {"profile": {"name": "budget_optimized", "agents": {
        "agent1": {"name": "budget_optimized"}, "agent2": {"name": "budget_optimized"}}}}
    res = db.retrieve_by_entities(["Ent"], agent_id="agent1")
    assert all(r.agent_id == "agent1" for r in res.records)


# ---------------------------------------------------------------------------
# ADB-10 — diagnostic trace
# ---------------------------------------------------------------------------

def test_trace_reports_candidate_sources_and_exclusions(tmp_path):
    db = _db_embed(tmp_path)
    db.write("m/alpha", "similar_to_A marker", origin="agent_inferred", agent_id="a1", confidence=0.9)
    db.write("memory/orders", "orders partition", origin="agent_inferred", agent_id="a1")
    res = db.retrieve("topic_A documents", agent_id="a1", limit=10, trace=True)
    tr = res.trace
    assert tr, "trace should be populated when trace=True"
    assert "candidate_sources" in tr
    assert "selected_ids" in tr
    assert set(tr["selected_ids"]) == {r.id for r in res.records}
    assert "ranked" in tr and all("composite_score" in x for x in tr["ranked"])


def test_trace_empty_by_default(tmp_path):
    db = _db_lexical(tmp_path)
    db.write("memory/x", "value", origin="agent_inferred", agent_id="a1")
    res = db.retrieve("value", agent_id="a1")
    assert res.trace == {}


def test_retrieval_is_deterministic(tmp_path):
    db = _db_lexical(tmp_path)
    for i in range(5):
        db.write(f"memory/d{i}", "orders partition key", origin="agent_inferred", agent_id="a1")
    r1 = [r.key for r in db.retrieve("orders", agent_id="a1").records]
    r2 = [r.key for r in db.retrieve("orders", agent_id="a1").records]
    assert r1 == r2
