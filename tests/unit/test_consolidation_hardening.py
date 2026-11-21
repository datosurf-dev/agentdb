"""Workstream C — conservative consolidation (FINAL-HARDENING §5).

Similarity is separate from compatibility: only equivalent or compatible-
complement values may merge. Negation, mutually exclusive values, scope
conflicts, and unsupported inference block merging. Existing unsafe canonicals
can be detected and safely invalidated without rewriting sources.
"""
from __future__ import annotations

from agentdb import AgentDB


def _db(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)


def _canon(db):
    return [r for r in db._store._by_id.values() if r.kind == "derived_canonical"]


def _pair(db, k1, v1, k2, v2, **kw):
    db.write(k1, v1, origin="agent_inferred", agent_id="a1", **kw)
    db.write(k2, v2, origin="agent_inferred", agent_id="a1", **kw)
    return db.consolidate("a1", since_hours=0, min_cluster_size=2)


# ---------------------------------------------------------------------------
# Contradictions never merge
# ---------------------------------------------------------------------------

def test_negation_contradiction_not_merged(tmp_path):
    db = _db(tmp_path)
    _pair(db, "memory/auth-policy-note", "authentication is required",
          "memory/auth-policy-update", "authentication is not required")
    assert _canon(db) == []
    # Both sources remain active/recoverable.
    assert db._store.get_by_key("memory/auth-policy-note") is not None
    assert db._store.get_by_key("memory/auth-policy-update") is not None


def test_approval_modality_contradiction_not_merged(tmp_path):
    db = _db(tmp_path)
    _pair(db, "memory/deploy-policy-note", "always request approval",
          "memory/deploy-policy-update", "deploy without approval")
    assert _canon(db) == []


def test_value_contradiction_not_merged(tmp_path):
    db = _db(tmp_path)
    _pair(db, "memory/db-version-note", "use postgres 15",
          "memory/db-version-update", "use postgres 16")
    assert _canon(db) == []


def test_environment_scope_conflict_not_merged(tmp_path):
    db = _db(tmp_path)
    _pair(db, "memory/cfg-note", "pool size 5", "memory/cfg-update", "pool size 5",
          )  # same value, then set different envs
    db2 = _db(tmp_path / "b")
    db2.write("memory/cfg-note", "pool size 5", origin="agent_inferred", agent_id="a1",
              environment="production")
    db2.write("memory/cfg-update", "pool size 5", origin="agent_inferred", agent_id="a1",
              environment="staging")
    db2.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert _canon(db2) == []


def test_repository_scope_conflict_not_merged(tmp_path):
    db = _db(tmp_path)
    db.write("memory/x-note", "build with bazel", origin="agent_inferred", agent_id="a1",
             repository_id="org/alpha")
    db.write("memory/x-update", "build with bazel", origin="agent_inferred", agent_id="a1",
             repository_id="org/beta")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert _canon(db) == []


# ---------------------------------------------------------------------------
# Safe merges still happen
# ---------------------------------------------------------------------------

def test_equivalent_instructions_merge(tmp_path):
    db = _db(tmp_path)
    _pair(db, "memory/lint-rule-note", "always run the linter before commit",
          "memory/lint-rule-update", "always run the linter before commit")
    assert len(_canon(db)) == 1


def test_compatible_complementary_facts_merge(tmp_path):
    db = _db(tmp_path)
    _pair(db, "memory/analytics-schema-note", "analytics schema uses columnar layout",
          "memory/analytics-schema-update", "analytics schema columnar layout confirmed")
    assert len(_canon(db)) == 1


def test_similar_text_different_subject_not_merged(tmp_path):
    db = _db(tmp_path)
    # Shares slug-ish words but different subjects/values, insufficient evidence.
    _pair(db, "memory/svc-note", "orders service retries requests",
          "memory/svc-update", "billing service handles invoices")
    assert _canon(db) == []


# ---------------------------------------------------------------------------
# Validate-existing-canonicals maintenance operation
# ---------------------------------------------------------------------------

def test_validate_canonicals_reports_valid(tmp_path):
    db = _db(tmp_path)
    _pair(db, "memory/redis-cache-note", "use redis cache", "memory/redis-cache-update", "use redis cache")
    report = db.validate_canonicals("a1")
    assert len(report) == 1 and report[0]["valid"] is True


def test_validate_canonicals_detects_revised_source(tmp_path):
    db = _db(tmp_path)
    _pair(db, "memory/redis-cache-note", "use redis cache", "memory/redis-cache-update", "use redis cache")
    canon = _canon(db)[0]
    # Revise a source after consolidation.
    src = db._store.get_by_id(canon.source_record_ids[0])
    src.value = "use memcached cache"
    db._store.write(src)
    report = db.validate_canonicals("a1")
    assert report[0]["valid"] is False
    assert report[0]["reason"] == "canonical_source_revised"


def test_validate_canonicals_is_non_destructive(tmp_path):
    db = _db(tmp_path)
    _pair(db, "memory/redis-cache-note", "use redis cache", "memory/redis-cache-update", "use redis cache")
    canon = _canon(db)[0]
    before = (canon.value, list(canon.source_record_ids))
    db.validate_canonicals("a1")
    after = db._store.get_by_id(canon.id)
    assert (after.value, list(after.source_record_ids)) == before
    # Sources still present (archived, not deleted).
    for sid in canon.source_record_ids:
        assert db._store.get_by_id(sid) is not None


def test_invalid_canonical_excluded_from_retrieval_sources_reachable(tmp_path):
    db = _db(tmp_path)
    _pair(db, "memory/redis-cache-note", "use redis cache", "memory/redis-cache-update", "use redis cache")
    canon = _canon(db)[0]
    src = db._store.get_by_id(canon.source_record_ids[0])
    src.value = "use memcached cache"
    db._store.write(src)
    res = db.retrieve("use redis cache", agent_id="a1", limit=10, trace=True)
    assert canon.id not in {r.id for r in res.records}
    reasons = {e["id"]: e["reason"] for e in res.trace["excluded"]}
    assert reasons.get(canon.id) == "canonical_source_revised"
