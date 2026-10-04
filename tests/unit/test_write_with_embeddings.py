import pytest
from tests.fixtures.embeddings import controlled_embed
from agentdb import AgentDB


def test_write_stores_memory_embedding(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=controlled_embed)
    record = db.write("user/name", "Alice", origin="agent_inferred", agent_id="a1")
    vec = db._embedding_store.get("memories", record.id)
    assert vec is not None
    assert len(vec) == 384


def test_write_embedding_failure_does_not_block(tmp_path):
    call_count = 0
    def failing_embed(text: str) -> list[float]:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("embed service down")
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=failing_embed)
    # Write must succeed even when embed fails
    record = db.write("user/name", "Alice", origin="agent_inferred", agent_id="a1")
    assert record is not None
    assert db._embedding_store.get("memories", record.id) is None


def test_write_no_embedding_without_provider(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)
    db.write("user/name", "Alice", origin="agent_inferred", agent_id="a1")
    assert db._embedding_store is None


def test_create_skill_stores_embedding(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=controlled_embed)
    skill = db.skills_api.create_skill("s1", "Code Review", "Review all PRs.", scope=["agent1"])
    composite_id = f"{skill.id}:{skill.version}"
    vec = db._embedding_store.get("skills", composite_id)
    assert vec is not None


def test_update_skill_stores_new_version_embedding(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=controlled_embed)
    db.skills_api.create_skill("s1", "Code Review", "Review all PRs.", scope=["agent1"])
    db.skills_api.update_skill_version("s1", "Review and sign off PRs.", "1.1.0")
    vec = db._embedding_store.get("skills", "s1:1.1.0")
    assert vec is not None


def test_archive_skill_deletes_embedding(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=controlled_embed)
    db.skills_api.create_skill("s1", "Code Review", "Review all PRs.", scope=["agent1"])
    assert db._embedding_store.get("skills", "s1:1.0.0") is not None
    db.skills_api.archive_skill("s1", "1.0.0")
    assert db._embedding_store.get("skills", "s1:1.0.0") is None
