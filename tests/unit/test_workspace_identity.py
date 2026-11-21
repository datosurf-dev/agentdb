"""Workstream A — stable workspace and repository identity (FINAL-HARDENING §3).

Host-supplied workspace_id / repository_id are independent applicability
metadata (never credentials). Exact identity is applicable; explicit mismatch is
inapplicable; missing identity is unknown (not a match). Legacy working-dir
basename matching is retained as a soft hint but never overrides explicit IDs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentdb import AgentDB
from agentdb.governance.permission_router import PermissionDenied


def _db(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)


# 1. Two unrelated repositories with identical basenames — distinguished by ID
def test_same_basename_repos_distinguished_by_id(tmp_path):
    db = _db(tmp_path)
    db.write("memory/x/a", "build uses make targets", origin="agent_inferred",
             agent_id="a1", repository_id="org/alpha", working_dirs=["/teams/one/app"])
    db.write("memory/x/b", "build uses make targets", origin="agent_inferred",
             agent_id="a1", repository_id="org/beta", working_dirs=["/teams/two/app"])
    res = db.retrieve("build uses make targets", agent_id="a1", limit=10,
                      scope={"repository_id": "org/alpha"})
    keys = {r.key for r in res.records}
    assert keys == {"memory/x/a"}


# 2. One repository across different workspaces
def test_one_repository_across_workspaces(tmp_path):
    db = _db(tmp_path)
    db.write("memory/deploy/ws1", "deploy config", origin="agent_inferred",
             agent_id="a1", repository_id="repo/shared", workspace_id="ws-1")
    db.write("memory/deploy/ws2", "deploy config", origin="agent_inferred",
             agent_id="a1", repository_id="repo/shared", workspace_id="ws-2")
    res = db.retrieve("deploy config", agent_id="a1", limit=10,
                      scope={"repository_id": "repo/shared", "workspace_id": "ws-1"})
    assert {r.key for r in res.records} == {"memory/deploy/ws1"}


# 3. Multiple repositories in one workspace
def test_multiple_repos_in_one_workspace(tmp_path):
    db = _db(tmp_path)
    db.write("memory/r1", "shared note", origin="agent_inferred", agent_id="a1",
             workspace_id="ws-1", repository_id="repo/one")
    db.write("memory/r2", "shared note", origin="agent_inferred", agent_id="a1",
             workspace_id="ws-1", repository_id="repo/two")
    # Workspace-only scope includes both (both belong to ws-1).
    res = db.retrieve("shared note", agent_id="a1", limit=10, scope={"workspace_id": "ws-1"})
    assert {r.key for r in res.records} == {"memory/r1", "memory/r2"}
    # Repo scope narrows to one.
    res2 = db.retrieve("shared note", agent_id="a1", limit=10,
                       scope={"workspace_id": "ws-1", "repository_id": "repo/two"})
    assert {r.key for r in res2.records} == {"memory/r2"}


# 4. Explicit identity mismatch → inapplicable
def test_explicit_repository_mismatch_excluded(tmp_path):
    db = _db(tmp_path)
    db.write("memory/x/a", "note alpha", origin="agent_inferred", agent_id="a1",
             repository_id="repo/alpha")
    db.write("memory/x/b", "note beta", origin="agent_inferred", agent_id="a1",
             repository_id="repo/beta")
    res = db.retrieve("note", agent_id="a1", limit=10, scope={"repository_id": "repo/alpha"})
    assert {r.key for r in res.records} == {"memory/x/a"}


def test_explicit_workspace_mismatch_excluded(tmp_path):
    db = _db(tmp_path)
    db.write("memory/x/a", "note", origin="agent_inferred", agent_id="a1", workspace_id="ws-a")
    db.write("memory/x/b", "note", origin="agent_inferred", agent_id="a1", workspace_id="ws-b")
    res = db.retrieve("note", agent_id="a1", limit=10, scope={"workspace_id": "ws-a"})
    assert {r.key for r in res.records} == {"memory/x/a"}


# 5. Partial identity: one matches, one explicitly conflicts → no exact match
def test_partial_identity_conflict_is_not_exact_match(tmp_path):
    db = _db(tmp_path)
    # record ws matches but repo conflicts -> must be inapplicable
    db.write("memory/x/a", "note", origin="agent_inferred", agent_id="a1",
             workspace_id="ws-1", repository_id="repo/alpha")
    db.write("memory/x/ok", "note", origin="agent_inferred", agent_id="a1",
             workspace_id="ws-1", repository_id="repo/beta")
    res = db.retrieve("note", agent_id="a1", limit=10,
                      scope={"workspace_id": "ws-1", "repository_id": "repo/beta"})
    assert {r.key for r in res.records} == {"memory/x/ok"}


def test_missing_identity_is_unknown_not_match(tmp_path):
    db = _db(tmp_path)
    db.write("memory/x/a", "note", origin="agent_inferred", agent_id="a1")  # no identity
    res = db.retrieve("note", agent_id="a1", limit=10, scope={"repository_id": "repo/alpha"})
    # Unknown identity => eligible (not excluded) but no exact-scope bonus.
    assert "memory/x/a" in {r.key for r in res.records}


# 6. Legacy unscoped records remain discoverable
def test_legacy_unscoped_records_discoverable(tmp_path):
    db = _db(tmp_path)
    db.write("memory/legacy/x", "legacy note", origin="agent_inferred", agent_id="a1")
    res = db.retrieve("legacy note", agent_id="a1", limit=10,
                      scope={"repository_id": "repo/new", "workspace_id": "ws-new"})
    assert "memory/legacy/x" in {r.key for r in res.records}


# 7. Working-directory hints vs explicit identity
def test_explicit_id_overrides_basename_hint(tmp_path):
    """A record with an explicit repository_id that conflicts is excluded even
    though its working_dir basename matches the scope's repo hint."""
    db = _db(tmp_path)
    # basename 'app' matches scope repo 'app', but explicit repo id conflicts
    db.write("memory/x/a", "note", origin="agent_inferred", agent_id="a1",
             repository_id="org/alpha", working_dirs=["/work/app"])
    res = db.retrieve("note", agent_id="a1", limit=10,
                      scope={"repository_id": "org/beta", "repo": "app"})
    assert "memory/x/a" not in {r.key for r in res.records}


# 8. No authorization escalation: identity is metadata, not credential
def test_workspace_identity_does_not_bypass_read_auth(tmp_path):
    perms = tmp_path / "perm.yaml"
    perms.write_text(
        "default_policy: deny\n"
        "agents:\n"
        "  alice:\n"
        "    can_write:\n      - \"memory/alice/\"\n"
        "    can_read:\n      - \"memory/alice/\"\n"
        "  bob:\n"
        "    can_write:\n      - \"memory/bob/\"\n"
        "    can_read:\n      - \"memory/bob/\"\n",
        encoding="utf-8",
    )
    db = AgentDB(path=tmp_path / "a.sqlite", memory_dir=tmp_path, permissions_path=perms)
    db.write("memory/bob/secret", "bob note", origin="agent_inferred", agent_id="bob",
             workspace_id="ws-1")
    # Alice supplies a matching workspace scope, but authorization still denies.
    res = db.retrieve("bob note", agent_id="alice", limit=10, scope={"workspace_id": "ws-1"})
    assert "memory/bob/secret" not in {r.key for r in res.records}
    with pytest.raises(PermissionDenied):
        db.read("memory/bob/secret", agent_id="alice")
