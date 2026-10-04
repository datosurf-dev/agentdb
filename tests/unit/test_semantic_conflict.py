import pytest
from tests.fixtures.embeddings import controlled_embed, VECTORS
from agentdb import AgentDB
from agentdb.core.models import MemoryRecord, ConflictRecord
from agentdb.governance.conflict_detector import ConflictDetector


def _make_db(tmp_path, threshold=0.92):
    db = AgentDB(
        path=tmp_path / "agentdb.sqlite",
        memory_dir=tmp_path,
        embed_fn=controlled_embed,
    )
    db._raw_embeddings_cfg["semantic_conflict_threshold"] = threshold
    return db


def test_check_semantic_batch_detects_high_cosine(tmp_path):
    db = _make_db(tmp_path)
    # "topic_A" → [1.0, 0, 0, ...]; "semantic_dup" → [0.9998, 0, ...]; cosine ≈ 0.9998 > 0.92
    rec_a = db.write("k/topic_A content", "v", origin="agent_inferred", agent_id="a1")
    rec_b = db.write("k2/semantic_dup content", "v2", origin="agent_inferred", agent_id="a1")

    conflicts = db._store.list_conflicts(resolved=False)
    semantic_conflicts = [c for c in conflicts if c.conflict_type == "semantic"]
    assert len(semantic_conflicts) >= 1


def test_check_semantic_batch_no_conflict_below_threshold(tmp_path):
    db = _make_db(tmp_path, threshold=0.99)  # very high threshold
    db.write("k/topic_A content", "v", origin="agent_inferred", agent_id="a1")
    db.write("k2/different content different project", "v2", origin="agent_inferred", agent_id="a1")

    conflicts = db._store.list_conflicts(resolved=False)
    semantic_conflicts = [c for c in conflicts if c.conflict_type == "semantic"]
    assert len(semantic_conflicts) == 0


def test_check_semantic_batch_caps_at_50_candidates():
    candidates = [
        MemoryRecord(key=f"k{i}", value="v", origin="agent_inferred",
                     trust_level="agent", agent_id="a1")
        for i in range(60)
    ]

    class MockEmbeddingStore:
        def get(self, table, record_id):
            return [0.99] * 4

        def get_batch(self, table, record_ids):
            assert len(record_ids) <= 50, f"Expected ≤50 IDs, got {len(record_ids)}"
            return {rid: [0.99] * 4 for rid in record_ids}

    new_record = MemoryRecord(
        key="new", value="v", origin="agent_inferred", trust_level="agent", agent_id="a1"
    )
    new_record.id = "new_id"

    conflicts = ConflictDetector.check_semantic_batch(
        new_record=new_record,
        candidates=candidates,
        embedding_store=MockEmbeddingStore(),
        threshold=0.92,
    )
    assert len(conflicts) <= 50


def test_check_semantic_batch_skips_self():
    new_record = MemoryRecord(
        key="k", value="v", origin="agent_inferred", trust_level="agent", agent_id="a1"
    )
    conflicts = ConflictDetector.check_semantic_batch(
        new_record=new_record,
        candidates=[new_record],
        embedding_store=None,
        threshold=0.92,
    )
    assert conflicts == []


def test_no_semantic_conflict_without_embedding_store(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)
    db.write("k", "v", origin="agent_inferred", agent_id="a1")
    db.write("k2", "v2", origin="agent_inferred", agent_id="a1")
    conflicts = db._store.list_conflicts(resolved=False)
    semantic = [c for c in conflicts if c.conflict_type == "semantic"]
    assert len(semantic) == 0
