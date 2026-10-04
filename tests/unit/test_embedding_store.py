import pytest
from agentdb.embeddings.store import EmbeddingStore


def _make_store(tmp_path, model_name="test-model", dimensions=4):
    return EmbeddingStore(
        db_path=tmp_path / "embeddings.db",
        model_name=model_name,
        dimensions=dimensions,
    )


def _vec(pos: int, size: int = 4) -> list[float]:
    v = [0.0] * size
    v[pos] = 1.0
    return v


def test_put_and_get_round_trip(tmp_path):
    store = _make_store(tmp_path)
    store.put("memories", "rec1", _vec(0), working_dirs=[])
    result = store.get("memories", "rec1")
    assert result is not None
    assert len(result) == 4
    assert abs(result[0] - 1.0) < 1e-5


def test_get_returns_none_for_missing(tmp_path):
    store = _make_store(tmp_path)
    assert store.get("memories", "nonexistent") is None


def test_get_batch_returns_all_stored(tmp_path):
    store = _make_store(tmp_path)
    store.put("memories", "r1", _vec(0), working_dirs=[])
    store.put("memories", "r2", _vec(1), working_dirs=[])
    store.put("memories", "r3", _vec(2), working_dirs=[])
    result = store.get_batch("memories", ["r1", "r2", "r3"])
    assert set(result.keys()) == {"r1", "r2", "r3"}
    assert abs(result["r1"][0] - 1.0) < 1e-5


def test_get_batch_empty_list_returns_empty_dict(tmp_path):
    store = _make_store(tmp_path)
    assert store.get_batch("memories", []) == {}


def test_get_batch_partial_results(tmp_path):
    store = _make_store(tmp_path)
    store.put("memories", "r1", _vec(0), working_dirs=[])
    result = store.get_batch("memories", ["r1", "r_missing"])
    assert "r1" in result
    assert "r_missing" not in result


def test_delete_removes_record(tmp_path):
    store = _make_store(tmp_path)
    store.put("memories", "rec1", _vec(0), working_dirs=[])
    store.delete("memories", "rec1")
    assert store.get("memories", "rec1") is None


def test_prune_orphans_removes_stale_and_returns_count(tmp_path):
    store = _make_store(tmp_path)
    store.put("memories", "r1", _vec(0), working_dirs=[])
    store.put("memories", "r2", _vec(1), working_dirs=[])
    store.put("memories", "r3", _vec(2), working_dirs=[])
    deleted = store.prune_orphans("memories", ["r1"])
    assert deleted == 2
    assert store.get("memories", "r1") is not None
    assert store.get("memories", "r2") is None
    assert store.get("memories", "r3") is None


def test_prune_orphans_empty_valid_ids_returns_zero_and_deletes_nothing(tmp_path):
    store = _make_store(tmp_path)
    store.put("memories", "r1", _vec(0), working_dirs=[])
    deleted = store.prune_orphans("memories", [])
    assert deleted == 0
    assert store.get("memories", "r1") is not None


def test_put_session_topic_and_get(tmp_path):
    store = _make_store(tmp_path)
    vec = _vec(1)
    store.put_session_topic("sess1", vec)
    result = store.get_session_topic("sess1")
    assert result is not None
    assert abs(result[1] - 1.0) < 1e-5


def test_get_session_topic_returns_none_for_missing(tmp_path):
    store = _make_store(tmp_path)
    assert store.get_session_topic("nonexistent") is None


def test_model_swap_wipes_embeddings_and_session_topics(tmp_path):
    db_path = tmp_path / "embeddings.db"
    store1 = EmbeddingStore(db_path=db_path, model_name="model-A", dimensions=4)
    store1.put("memories", "r1", _vec(0), working_dirs=[])
    store1.put_session_topic("sess1", _vec(1))
    assert store1.get("memories", "r1") is not None

    store2 = EmbeddingStore(db_path=db_path, model_name="model-B", dimensions=4)
    assert store2.get("memories", "r1") is None
    assert store2.get_session_topic("sess1") is None


def test_same_model_reinit_preserves_embeddings(tmp_path):
    db_path = tmp_path / "embeddings.db"
    store1 = EmbeddingStore(db_path=db_path, model_name="model-A", dimensions=4)
    store1.put("memories", "r1", _vec(0), working_dirs=[])

    store2 = EmbeddingStore(db_path=db_path, model_name="model-A", dimensions=4)
    assert store2.get("memories", "r1") is not None


def test_dimension_change_same_model_name_wipes_embeddings(tmp_path):
    # Swapping FunctionEmbeddingProvider from 4-dim to 8-dim — both named "custom" —
    # must wipe stored embeddings to prevent silently corrupt cosine math
    db_path = tmp_path / "embeddings.db"
    store1 = EmbeddingStore(db_path=db_path, model_name="custom", dimensions=4)
    store1.put("memories", "r1", _vec(0, size=4), working_dirs=[])
    assert store1.get("memories", "r1") is not None

    store2 = EmbeddingStore(db_path=db_path, model_name="custom", dimensions=8)
    assert store2.get("memories", "r1") is None  # wipe triggered on dim mismatch


def test_upsert_overwrites_existing(tmp_path):
    store = _make_store(tmp_path)
    store.put("memories", "r1", _vec(0), working_dirs=[])
    store.put("memories", "r1", _vec(3), working_dirs=[])
    result = store.get("memories", "r1")
    assert abs(result[3] - 1.0) < 1e-5
    assert abs(result[0]) < 1e-5
