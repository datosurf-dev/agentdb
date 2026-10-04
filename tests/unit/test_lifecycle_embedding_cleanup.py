import pytest
from tests.fixtures.embeddings import controlled_embed
from agentdb import AgentDB


def test_run_sweep_prunes_orphaned_memory_embeddings(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=controlled_embed)

    r1 = db.write("k1", "v1", origin="agent_inferred", agent_id="a1")
    r2 = db.write("k2", "v2", origin="agent_inferred", agent_id="a1")

    assert db._embedding_store.get("memories", r1.id) is not None
    assert db._embedding_store.get("memories", r2.id) is not None

    db._embedding_store.put("memories", "orphan_id", [0.0] * 384, working_dirs=[])
    assert db._embedding_store.get("memories", "orphan_id") is not None

    db.lifecycle.run_sweep()

    assert db._embedding_store.get("memories", "orphan_id") is None
    assert db._embedding_store.get("memories", r1.id) is not None
    assert db._embedding_store.get("memories", r2.id) is not None


def test_run_sweep_prunes_orphaned_skill_embeddings(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=controlled_embed)

    db.skills_api.create_skill("s1", "Skill One", "Do things.", scope=["a1"])
    assert db._embedding_store.get("skills", "s1:1.0.0") is not None

    db._embedding_store.put("skills", "s_orphan:9.9.9", [0.0] * 384, working_dirs=[])
    assert db._embedding_store.get("skills", "s_orphan:9.9.9") is not None

    db.lifecycle.run_sweep()

    assert db._embedding_store.get("skills", "s_orphan:9.9.9") is None
    assert db._embedding_store.get("skills", "s1:1.0.0") is not None


def test_run_sweep_no_error_without_embedding_store(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)
    result = db.lifecycle.run_sweep()
    assert "archived" in result


def test_run_sweep_returns_dict_with_embedding_store(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=controlled_embed)
    result = db.lifecycle.run_sweep()
    assert "archived" in result
    assert "decayed" in result
