"""OD-3 — configured read authorization (CONTEXTUAL-SPEC §6).

Default trusted mode keeps behavior unchanged. When a permissions policy is
configured (isolation mode), read authorization is enforced across direct reads,
lexical and semantic retrieval, entity retrieval, and canonical/source recovery.
Canonicals whose sources are not visible are not disclosed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentdb import AgentDB
from agentdb.governance.permission_router import PermissionDenied
from tests.fixtures.embeddings import controlled_embed

PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def _iso_perms(tmp_path):
    """Isolation policy: two agents with disjoint read scopes."""
    p = tmp_path / "perm.yaml"
    p.write_text(
        "\n".join([
            "default_policy: deny",
            "agents:",
            "  alice:",
            "    can_write:",
            "      - \"memory/alice/\"",
            "    can_read:",
            "      - \"memory/alice/\"",
            "  bob:",
            "    can_write:",
            "      - \"memory/bob/\"",
            "    can_read:",
            "      - \"memory/bob/\"",
        ]),
        encoding="utf-8",
    )
    return p


def _db(tmp_path):
    return AgentDB(path=tmp_path / "a.sqlite", memory_dir=tmp_path)


def _db_iso(tmp_path):
    return AgentDB(path=tmp_path / "a.sqlite", memory_dir=tmp_path,
                   permissions_path=_iso_perms(tmp_path))


# ---------------------------------------------------------------------------
# Default trusted mode compatibility
# ---------------------------------------------------------------------------

def test_trusted_mode_read_has_no_authorization(tmp_path):
    db = _db(tmp_path)  # no permissions configured
    db.write("memory/anything/x", "v", origin="agent_inferred", agent_id="a1")
    assert db.read("memory/anything/x") is not None  # no agent_id needed
    res = db.retrieve("anything", agent_id="a1")
    assert any(r.key == "memory/anything/x" for r in res.records)


# ---------------------------------------------------------------------------
# Direct reads
# ---------------------------------------------------------------------------

def test_authorized_direct_read(tmp_path):
    db = _db_iso(tmp_path)
    db.write("memory/alice/note", "alice secret", origin="agent_inferred", agent_id="alice")
    assert db.read("memory/alice/note", agent_id="alice").value == "alice secret"


def test_unauthorized_direct_read_denied(tmp_path):
    db = _db_iso(tmp_path)
    db.write("memory/alice/note", "alice secret", origin="agent_inferred", agent_id="alice")
    with pytest.raises(PermissionDenied):
        db.read("memory/alice/note", agent_id="bob")


def test_missing_caller_identity_fails_closed(tmp_path):
    db = _db_iso(tmp_path)
    db.write("memory/alice/note", "v", origin="agent_inferred", agent_id="alice")
    with pytest.raises(PermissionDenied):
        db.read("memory/alice/note")  # no agent_id under isolation


def test_read_denial_is_audited(tmp_path):
    db = _db_iso(tmp_path)
    db.write("memory/alice/note", "v", origin="agent_inferred", agent_id="alice")
    with pytest.raises(PermissionDenied):
        db.read("memory/alice/note", agent_id="bob")
    assert any(e.event_type == "permission_denied"
               for e in db.audit_log.replay(from_sequence=0))


# ---------------------------------------------------------------------------
# Retrieval isolation (lexical + semantic + entity)
# ---------------------------------------------------------------------------

def test_lexical_retrieval_isolation(tmp_path):
    db = _db_iso(tmp_path)
    db.write("memory/alice/widget", "widget note from alice", origin="agent_inferred", agent_id="alice")
    db.write("memory/bob/widget", "widget note from bob", origin="agent_inferred", agent_id="bob")
    res = db.retrieve("widget", agent_id="alice", limit=10)
    keys = {r.key for r in res.records}
    assert keys == {"memory/alice/widget"}


def test_semantic_retrieval_isolation(tmp_path):
    # With embeddings active, a semantically-strong but unauthorized record must
    # still be excluded (scope check precedes/overrides similarity).
    db = AgentDB(path=tmp_path / "a.sqlite", memory_dir=tmp_path,
                 permissions_path=_iso_perms(tmp_path), embed_fn=controlled_embed)
    db.write("memory/bob/alpha", "phrase about topic_A marker", origin="agent_inferred", agent_id="bob")
    res = db.retrieve("please find topic_A docs", agent_id="alice", limit=10, trace=True)
    assert "memory/bob/alpha" not in {r.key for r in res.records}


def test_entity_retrieval_isolation(tmp_path):
    db = _db_iso(tmp_path)
    db.write("memory/alice/e", "x", origin="agent_inferred", agent_id="alice", entities=["proj"])
    db.write("memory/bob/e", "x", origin="agent_inferred", agent_id="bob", entities=["proj"])
    res = db.retrieve_by_entities(["proj"], agent_id="alice")
    assert {r.key for r in res.records} == {"memory/alice/e"}


def test_retrieval_missing_identity_returns_nothing(tmp_path):
    """retrieve() with no identity under isolation fails closed (no leak)."""
    db = _db_iso(tmp_path)
    db.write("memory/alice/widget", "widget", origin="agent_inferred", agent_id="alice")
    # retrieve() requires agent_id positionally; passing empty string simulates
    # an unresolved identity.
    res = db.retrieve("widget", agent_id="", limit=10)
    assert res.records == []


# ---------------------------------------------------------------------------
# Canonical source-visibility (critical rule)
# ---------------------------------------------------------------------------

def test_canonical_with_mixed_visibility_sources_not_disclosed(tmp_path):
    """A canonical aggregating a source the caller cannot read is withheld."""
    from agentdb.core.models import MemoryRecord
    from agentdb.governance.lifecycle_manager import _record_digest

    db = _db_iso(tmp_path)
    # alice-written source (readable by alice) and bob-written source (not).
    a_src = db.write("memory/alice/src", "alice fact", origin="agent_inferred", agent_id="alice")
    b_src = db.write("memory/bob/src", "bob secret", origin="agent_inferred", agent_id="bob")
    # Build a canonical that references BOTH sources.
    canon = MemoryRecord(
        key="memory/canonical/mixed/x", value="combined fact",
        origin="agent_inferred", trust_level="agent", agent_id="alice",
        consolidated=True, kind="derived_canonical", producer="consolidation",
        source_record_ids=[a_src.id, b_src.id],
        derivation={"version": "1", "sources": [
            {"id": a_src.id, "digest": _record_digest(a_src)},
            {"id": b_src.id, "digest": _record_digest(b_src)},
        ]},
    )
    db._store.write(canon)

    res = db.retrieve("combined fact", agent_id="alice", limit=10, trace=True)
    ids = {r.id for r in res.records}
    assert canon.id not in ids, "canonical with bob-only source must not leak to alice"
    reasons = {e["id"]: e["reason"] for e in res.trace["excluded"]}
    assert reasons.get(canon.id) == "read_denied"


def test_canonical_with_all_visible_sources_disclosed(tmp_path):
    from agentdb.core.models import MemoryRecord
    from agentdb.governance.lifecycle_manager import _record_digest

    db = _db_iso(tmp_path)
    s1 = db.write("memory/alice/s1", "alice fact one", origin="agent_inferred", agent_id="alice")
    s2 = db.write("memory/alice/s2", "alice fact two", origin="agent_inferred", agent_id="alice")
    canon = MemoryRecord(
        key="memory/canonical/alice/x", value="alice combined",
        origin="agent_inferred", trust_level="agent", agent_id="alice",
        consolidated=True, kind="derived_canonical", producer="consolidation",
        source_record_ids=[s1.id, s2.id],
        derivation={"version": "1", "sources": [
            {"id": s1.id, "digest": _record_digest(s1)},
            {"id": s2.id, "digest": _record_digest(s2)},
        ]},
    )
    db._store.write(canon)
    res = db.retrieve("alice combined", agent_id="alice", limit=10)
    assert canon.id in {r.id for r in res.records}


# ---------------------------------------------------------------------------
# Cross-agent isolation & archived-source retrieval
# ---------------------------------------------------------------------------

def test_cross_agent_isolation(tmp_path):
    db = _db_iso(tmp_path)
    db.write("memory/alice/x", "alice", origin="agent_inferred", agent_id="alice")
    db.write("memory/bob/x", "bob", origin="agent_inferred", agent_id="bob")
    alice = db.retrieve("x", agent_id="alice", limit=10)
    bob = db.retrieve("x", agent_id="bob", limit=10)
    assert {r.key for r in alice.records} == {"memory/alice/x"}
    assert {r.key for r in bob.records} == {"memory/bob/x"}


def test_archived_source_read_respects_authorization(tmp_path):
    """Authorization is enforced before the archive-state lookup: an
    unauthorized caller is denied even for an archived key (no info leak)."""
    db = _db_iso(tmp_path)
    r = db.write("memory/alice/x", "alice archived", origin="agent_inferred", agent_id="alice")
    db._store.archive_record(r.id)
    # read() returns only active records, so the owner sees None (archived),
    # but a non-owner is denied outright rather than learning it exists/is empty.
    assert db.read("memory/alice/x", agent_id="alice") is None
    with pytest.raises(PermissionDenied):
        db.read("memory/alice/x", agent_id="bob")
