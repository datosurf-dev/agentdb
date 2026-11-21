"""Context-aware relevance (CONTEXTUAL-SPEC §7).

Deterministic offline tests for workspace/repository/environment/task
applicability. Explicit environment mismatch excludes; missing scope never
matches nor excludes; task-type labels are soft (never exclude); exact-scope
matches act as a deterministic tie-break without altering ranking weights.
"""
from __future__ import annotations

import pytest

from agentdb import AgentDB


def _db(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)


def _wd(*parts):
    return list(parts)


# ---------------------------------------------------------------------------
# Explicit environment mismatch is inapplicable
# ---------------------------------------------------------------------------

def test_environment_mismatch_excluded(tmp_path):
    db = _db(tmp_path)
    db.write("memory/deploy/prod", "deploy with 3 replicas",
             origin="agent_inferred", agent_id="a1", environment="production")
    db.write("memory/deploy/stage", "deploy with 1 replica",
             origin="agent_inferred", agent_id="a1", environment="staging")
    res = db.retrieve("deploy replicas", agent_id="a1", limit=10,
                      scope={"environment": "production"})
    keys = {r.key for r in res.records}
    assert keys == {"memory/deploy/prod"}


def test_missing_record_environment_is_not_a_mismatch(tmp_path):
    """A record with no environment is not excluded by an env-scoped query."""
    db = _db(tmp_path)
    db.write("memory/deploy/unscoped", "deploy with 3 replicas",
             origin="agent_inferred", agent_id="a1")  # no environment
    res = db.retrieve("deploy replicas", agent_id="a1", limit=10,
                      scope={"environment": "production"})
    assert "memory/deploy/unscoped" in {r.key for r in res.records}


def test_missing_query_scope_does_not_exclude(tmp_path):
    db = _db(tmp_path)
    db.write("memory/deploy/prod", "deploy replicas",
             origin="agent_inferred", agent_id="a1", environment="production")
    res = db.retrieve("deploy replicas", agent_id="a1", limit=10)  # no scope
    assert "memory/deploy/prod" in {r.key for r in res.records}


# ---------------------------------------------------------------------------
# Task-type label difference does NOT exclude
# ---------------------------------------------------------------------------

def test_task_type_difference_does_not_exclude(tmp_path):
    db = _db(tmp_path)
    db.write("memory/lint/rule", "always run the linter before commit",
             origin="agent_inferred", agent_id="a1", task_type="code_review")
    res = db.retrieve("run the linter before commit", agent_id="a1", limit=10,
                      scope={"task_type": "code_generation"})
    # Cross-task knowledge remains discoverable when content is relevant.
    assert "memory/lint/rule" in {r.key for r in res.records}


# ---------------------------------------------------------------------------
# Exact-scope match: deterministic tie-break, weights preserved
# ---------------------------------------------------------------------------

def test_exact_environment_match_breaks_tie(tmp_path):
    """Two equally-relevant records tie on composite score; the one matching the
    query environment ranks first (tie-break, not weight change)."""
    db = _db(tmp_path)
    db.write("memory/cache/gen", "cache uses redis cluster",
             origin="agent_inferred", agent_id="a1", environment="staging")
    db.write("memory/cache/prod", "cache uses redis cluster",
             origin="agent_inferred", agent_id="a1", environment="production")
    res = db.retrieve("cache uses redis cluster", agent_id="a1", limit=10,
                      scope={"environment": "production"}, trace=True)
    keys = [r.key for r in res.records]
    assert keys[0] == "memory/cache/prod"


def test_unscoped_record_gets_no_exact_scope_bonus(tmp_path):
    """An unscoped legacy record stays discoverable but does not outrank an
    equally-relevant explicitly-scoped match."""
    db = _db(tmp_path)
    db.write("memory/cache/legacy", "cache uses redis cluster",
             origin="agent_inferred", agent_id="a1")  # unscoped
    db.write("memory/cache/prod", "cache uses redis cluster",
             origin="agent_inferred", agent_id="a1", environment="production")
    res = db.retrieve("cache uses redis cluster", agent_id="a1", limit=10,
                      scope={"environment": "production"})
    keys = [r.key for r in res.records]
    assert "memory/cache/legacy" in keys          # discoverable
    assert keys[0] == "memory/cache/prod"          # exact match first


# ---------------------------------------------------------------------------
# Workspace / repository applicability
# ---------------------------------------------------------------------------

def test_same_instruction_different_workspace(tmp_path):
    """A repo-scoped fact is preferred when the query names that repo."""
    db = _db(tmp_path)
    db.write("memory/build/alpha", "build with make targets",
             origin="agent_inferred", agent_id="a1",
             working_dirs=["/work/alpha"])
    db.write("memory/build/beta", "build with make targets",
             origin="agent_inferred", agent_id="a1",
             working_dirs=["/work/beta"])
    res = db.retrieve("build with make targets", agent_id="a1", limit=10,
                      scope={"repo": "alpha"})
    keys = [r.key for r in res.records]
    assert keys[0] == "memory/build/alpha"


def test_repos_sharing_basename_not_conflated_by_scope(tmp_path):
    """Two repos with the same basename under different parents: scope='app'
    matches both by basename (documented limitation), but both remain eligible
    and no record is wrongly excluded."""
    db = _db(tmp_path)
    db.write("memory/x/one", "app config uses port 8080",
             origin="agent_inferred", agent_id="a1", working_dirs=["/teams/one/app"])
    db.write("memory/x/two", "app config uses port 8080",
             origin="agent_inferred", agent_id="a1", working_dirs=["/teams/two/app"])
    res = db.retrieve("app config port", agent_id="a1", limit=10, scope={"repo": "app"})
    assert {r.key for r in res.records} == {"memory/x/one", "memory/x/two"}


# ---------------------------------------------------------------------------
# Weight preservation (no ranking weights changed by this feature)
# ---------------------------------------------------------------------------

def test_scope_does_not_change_composite_scores(tmp_path):
    # Two identical DBs so retrieval-count side effects don't contaminate the
    # comparison; the only difference is whether scope is supplied.
    def build(sub):
        d = tmp_path / sub
        d.mkdir()
        db = AgentDB(path=d / "agentdb.sqlite", memory_dir=d)
        rid = db.write("memory/c/k", "alpha beta gamma", origin="agent_inferred",
                       agent_id="a1", environment="production").id
        return db, rid

    db1, rid = build("a")
    db2, _ = build("b")
    r1 = db1.retrieve("alpha beta gamma", agent_id="a1", trace=True)
    r2 = db2.retrieve("alpha beta gamma", agent_id="a1",
                      scope={"environment": "production"}, trace=True)
    # Record ids differ per DB (UUIDs); compare the score multiset.
    s1 = sorted(x["composite_score"] for x in r1.trace["ranked"])
    s2 = sorted(x["composite_score"] for x in r2.trace["ranked"])
    assert s1 == s2, "scope must not alter composite scores (tie-break only)"
