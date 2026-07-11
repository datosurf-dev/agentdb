"""
Tests AgentDBCrewAIStorage against the CrewAI StorageBackend protocol contract.
Does NOT require crewai installed — tests the interface contract directly using
the _make_memory_record / _make_scope_info helpers from the adapter.

Actual CrewAI StorageBackend protocol (crewai.memory.storage.backend):
  save(records: list[MemoryRecord]) -> None
  search(query_embedding: list[float], scope_prefix=None, categories=None,
         metadata_filter=None, limit=10, min_score=0.0) -> list[tuple[MemoryRecord, float]]
  delete(scope_prefix=None, categories=None, record_ids=None, older_than=None,
         metadata_filter=None) -> int
  update(record: MemoryRecord) -> None
  get_record(record_id: str) -> MemoryRecord | None
  list_records(scope_prefix=None, limit=200, offset=0) -> list[MemoryRecord]
  get_scope_info(scope: str) -> ScopeInfo
  list_scopes(parent="/") -> list[str]
  list_categories(scope_prefix=None) -> dict[str, int]
  count(scope_prefix=None) -> int
  reset(scope_prefix=None) -> None
  async asave / asearch / adelete
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentdb import AgentDB
from agentdb.adapters.crewai import (
    AgentDBCrewAIStorage,
    _make_memory_record,
    _make_scope_info,
)

PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def make_record(
    content: str = "test content",
    scope: str = "/",
    categories: list | None = None,
    metadata: dict | None = None,
    importance: float = 0.5,
    source: str | None = None,
    private: bool = False,
    record_id: str | None = None,
) -> object:
    """Create a MemoryRecord (or shim) for testing."""
    return _make_memory_record(
        id=record_id or str(uuid.uuid4()),
        content=content,
        scope=scope,
        categories=categories or [],
        metadata=metadata or {},
        importance=importance,
        created_at=datetime.now(timezone.utc),
        last_accessed=datetime.now(timezone.utc),
        source=source,
        private=private,
        embedding=None,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)


@pytest.fixture
def storage(db):
    return AgentDBCrewAIStorage(db=db, agent_id="research_agent")


# ---------------------------------------------------------------------------
# save / search
# ---------------------------------------------------------------------------

def test_save_single_record(storage):
    rec = make_record(content="user prefers concise bullet points")
    storage.save([rec])
    assert storage.count() == 1


def test_save_multiple_records(storage):
    records = [make_record(content=f"item {i}") for i in range(3)]
    storage.save(records)
    assert storage.count() == 3


def test_save_empty_list_is_noop(storage):
    storage.save([])
    assert storage.count() == 0


def test_search_returns_all_when_no_filter(storage):
    records = [make_record(content=f"content {i}") for i in range(4)]
    storage.save(records)
    # Phase 1: query_embedding is ignored; all records returned up to limit
    results = storage.search(query_embedding=[], limit=10)
    assert len(results) == 4


def test_search_respects_limit(storage):
    records = [make_record(content=f"item {i}") for i in range(5)]
    storage.save(records)
    results = storage.search(query_embedding=[], limit=2)
    assert len(results) == 2


def test_search_returns_scored_tuples(storage):
    storage.save([make_record(content="hello world")])
    results = storage.search(query_embedding=[], limit=5)
    assert len(results) >= 1
    rec, score = results[0]
    assert hasattr(rec, "content")
    assert isinstance(score, float)


def test_search_min_score_zero_returns_all(storage):
    storage.save([make_record()])
    results = storage.search(query_embedding=[], min_score=0.0)
    assert len(results) == 1


def test_search_min_score_above_one_returns_empty(storage):
    storage.save([make_record()])
    # Phase 1 assigns score 1.0; anything > 1.0 should exclude all
    results = storage.search(query_embedding=[], min_score=1.1)
    assert results == []


def test_search_scope_prefix_filter(storage):
    storage.save([make_record(content="in scope", scope="/team/alpha")])
    storage.save([make_record(content="out of scope", scope="/team/beta")])
    results = storage.search(query_embedding=[], scope_prefix="/team/alpha")
    assert len(results) == 1
    assert results[0][0].content == "in scope"


def test_search_category_filter(storage):
    storage.save([make_record(content="tagged", categories=["important"])])
    storage.save([make_record(content="untagged", categories=[])])
    results = storage.search(query_embedding=[], categories=["important"])
    assert len(results) == 1
    assert results[0][0].content == "tagged"


def test_search_metadata_filter(storage):
    storage.save([make_record(content="priority task", metadata={"priority": "high"})])
    storage.save([make_record(content="normal task", metadata={"priority": "low"})])
    results = storage.search(
        query_embedding=[], metadata_filter={"priority": "high"}
    )
    assert len(results) == 1
    assert results[0][0].content == "priority task"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def test_delete_by_record_ids(storage):
    rec = make_record(content="to be deleted")
    storage.save([rec])
    deleted = storage.delete(record_ids=[rec.id])
    assert deleted == 1
    assert storage.count() == 0


def test_delete_by_scope_prefix(storage):
    storage.save([make_record(content="in scope", scope="/work/session1")])
    storage.save([make_record(content="keeper", scope="/archive/old")])
    deleted = storage.delete(scope_prefix="/work")
    assert deleted == 1
    assert storage.count() == 1


def test_delete_by_category(storage):
    storage.save([make_record(content="temp", categories=["ephemeral"])])
    storage.save([make_record(content="keeper", categories=["permanent"])])
    deleted = storage.delete(categories=["ephemeral"])
    assert deleted == 1
    assert storage.count() == 1


def test_delete_older_than(storage):
    old_time = datetime.now(timezone.utc) - timedelta(days=1)
    old_rec = _make_memory_record(
        id=str(uuid.uuid4()),
        content="old record",
        scope="/",
        categories=[],
        metadata={},
        importance=0.5,
        created_at=old_time,
        last_accessed=old_time,
        source=None,
        private=False,
        embedding=None,
    )
    fresh_rec = make_record(content="fresh record")
    storage.save([old_rec, fresh_rec])
    deleted = storage.delete(older_than=datetime.now(timezone.utc) - timedelta(hours=1))
    assert deleted == 1
    assert storage.count() == 1


def test_delete_returns_zero_when_no_match(storage):
    storage.save([make_record(content="irrelevant")])
    deleted = storage.delete(record_ids=["nonexistent-id"])
    assert deleted == 0


# ---------------------------------------------------------------------------
# update / get_record
# ---------------------------------------------------------------------------

def test_update_replaces_content(storage):
    rec = make_record(content="original content", record_id="update-test-id")
    storage.save([rec])

    updated = _make_memory_record(
        id="update-test-id",
        content="updated content",
        scope=rec.scope,
        categories=rec.categories,
        metadata=rec.metadata,
        importance=rec.importance,
        created_at=rec.created_at,
        last_accessed=datetime.now(timezone.utc),
        source=rec.source,
        private=rec.private,
        embedding=None,
    )
    storage.update(updated)

    found = storage.get_record("update-test-id")
    assert found is not None
    assert found.content == "updated content"


def test_get_record_returns_none_for_missing(storage):
    assert storage.get_record("does-not-exist") is None


def test_get_record_returns_correct_record(storage):
    rec = make_record(content="findable", record_id="find-me")
    storage.save([rec])
    found = storage.get_record("find-me")
    assert found is not None
    assert found.content == "findable"
    assert found.id == "find-me"


# ---------------------------------------------------------------------------
# list_records
# ---------------------------------------------------------------------------

def test_list_records_newest_first(db):
    storage = AgentDBCrewAIStorage(db=db, agent_id="research_agent")
    t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 1, 12, 0, 2, tzinfo=timezone.utc)
    r1 = _make_memory_record(id=str(uuid.uuid4()), content="first",  scope="/", categories=[], metadata={}, importance=0.5, created_at=t1, last_accessed=t1, source=None, private=False, embedding=None)
    r2 = _make_memory_record(id=str(uuid.uuid4()), content="second", scope="/", categories=[], metadata={}, importance=0.5, created_at=t2, last_accessed=t2, source=None, private=False, embedding=None)
    r3 = _make_memory_record(id=str(uuid.uuid4()), content="third",  scope="/", categories=[], metadata={}, importance=0.5, created_at=t3, last_accessed=t3, source=None, private=False, embedding=None)
    storage.save([r1, r2, r3])
    results = storage.list_records()
    assert len(results) == 3
    assert results[0].content == "third"
    assert results[2].content == "first"


def test_list_records_respects_limit_and_offset(storage):
    for i in range(5):
        storage.save([make_record(content=f"record {i}")])
    page1 = storage.list_records(limit=2, offset=0)
    page2 = storage.list_records(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    # No overlap
    ids1 = {r.id for r in page1}
    ids2 = {r.id for r in page2}
    assert ids1.isdisjoint(ids2)


def test_list_records_scope_prefix_filter(storage):
    storage.save([make_record(content="in", scope="/work/a")])
    storage.save([make_record(content="out", scope="/personal/b")])
    records = storage.list_records(scope_prefix="/work")
    assert len(records) == 1
    assert records[0].content == "in"


# ---------------------------------------------------------------------------
# get_scope_info
# ---------------------------------------------------------------------------

def test_get_scope_info_empty_scope(storage):
    info = storage.get_scope_info("/nonexistent")
    assert info.record_count == 0
    assert info.categories == []
    assert info.oldest_record is None


def test_get_scope_info_populated_scope(storage):
    storage.save([
        make_record(content="a", scope="/proj/x", categories=["bug"]),
        make_record(content="b", scope="/proj/x", categories=["feature"]),
    ])
    info = storage.get_scope_info("/proj/x")
    assert info.record_count == 2
    assert "bug" in info.categories
    assert "feature" in info.categories
    assert info.oldest_record is not None
    assert info.newest_record is not None


# ---------------------------------------------------------------------------
# list_scopes
# ---------------------------------------------------------------------------

def test_list_scopes_returns_immediate_children(storage):
    storage.save([make_record(scope="/root/child1/leaf")])
    storage.save([make_record(scope="/root/child2/leaf")])
    scopes = storage.list_scopes(parent="/root")
    assert "/root/child1" in scopes
    assert "/root/child2" in scopes


def test_list_scopes_empty_when_no_children(storage):
    storage.save([make_record(scope="/solo")])
    scopes = storage.list_scopes(parent="/solo")
    assert scopes == []


# ---------------------------------------------------------------------------
# list_categories
# ---------------------------------------------------------------------------

def test_list_categories_counts(storage):
    storage.save([
        make_record(categories=["bug", "urgent"]),
        make_record(categories=["bug"]),
        make_record(categories=["feature"]),
    ])
    cats = storage.list_categories()
    assert cats["bug"] == 2
    assert cats["urgent"] == 1
    assert cats["feature"] == 1


def test_list_categories_scope_filtered(storage):
    storage.save([make_record(categories=["tag-a"], scope="/work")])
    storage.save([make_record(categories=["tag-b"], scope="/personal")])
    cats = storage.list_categories(scope_prefix="/work")
    assert "tag-a" in cats
    assert "tag-b" not in cats


# ---------------------------------------------------------------------------
# count
# ---------------------------------------------------------------------------

def test_count_all(storage):
    storage.save([make_record() for _ in range(4)])
    assert storage.count() == 4


def test_count_with_scope_prefix(storage):
    storage.save([make_record(scope="/a")])
    storage.save([make_record(scope="/a")])
    storage.save([make_record(scope="/b")])
    assert storage.count(scope_prefix="/a") == 2
    assert storage.count(scope_prefix="/b") == 1


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

def test_reset_clears_all(storage):
    storage.save([make_record() for _ in range(3)])
    storage.reset()
    assert storage.count() == 0


def test_reset_scope_prefix_clears_only_that_scope(storage):
    storage.save([make_record(content="gone", scope="/temp")])
    storage.save([make_record(content="kept", scope="/archive")])
    storage.reset(scope_prefix="/temp")
    assert storage.count() == 1
    records = storage.list_records()
    assert records[0].content == "kept"


def test_reset_none_clears_all(storage):
    storage.save([make_record() for _ in range(2)])
    storage.reset(scope_prefix=None)
    assert storage.count() == 0


# ---------------------------------------------------------------------------
# Isolation between storage instances
# ---------------------------------------------------------------------------

def test_two_agents_are_isolated(db):
    """Different agent_ids must not see each other's records."""
    # Note: both agents need write/read permissions; use separate db paths
    s1 = AgentDBCrewAIStorage(db=db, agent_id="research_agent")
    # coder_agent also has memory/working/ permissions per fixtures
    s2 = AgentDBCrewAIStorage(db=db, agent_id="coder_agent")
    s1.save([make_record(content="agent 1 secret")])
    assert s2.count() == 0


def test_two_key_prefixes_are_isolated(db):
    """Different key_prefix values must not interfere."""
    s1 = AgentDBCrewAIStorage(db=db, agent_id="research_agent",
                               key_prefix="memory/working/crewai/proj_a")
    s2 = AgentDBCrewAIStorage(db=db, agent_id="research_agent",
                               key_prefix="memory/working/crewai/proj_b")
    s1.save([make_record(content="project a data")])
    assert s2.count() == 0


# ---------------------------------------------------------------------------
# Async interface
# ---------------------------------------------------------------------------

def test_asave_and_asearch(storage):
    async def _run():
        rec = make_record(content="async memory")
        await storage.asave([rec])
        results = await storage.asearch(query_embedding=[], limit=5)
        return results

    results = asyncio.run(_run())
    assert len(results) == 1
    assert results[0][0].content == "async memory"


def test_adelete(storage):
    async def _run():
        rec = make_record(content="to delete")
        await storage.asave([rec])
        count_before = storage.count()
        deleted = await storage.adelete(record_ids=[rec.id])
        count_after = storage.count()
        return count_before, deleted, count_after

    before, deleted, after = asyncio.run(_run())
    assert before == 1
    assert deleted == 1
    assert after == 0


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def test_writes_are_audit_logged(storage, db):
    storage.save([make_record(content="audited")])
    entries = db.audit_log.replay(from_sequence=0)
    assert any(e.event_type == "memory_write" for e in entries)
