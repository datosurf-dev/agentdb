"""OD-2 / FINAL-HARDENING §4 — trusted-host authority.

Verified human authority requires an affirmative decision from a host-configured
authority_verifier. Caller-supplied metadata (origin string, approval dict,
even a self-computed fingerprint) cannot manufacture verified authority.
Legacy origin='human_approved' records remain readable as *asserted*.
"""
from __future__ import annotations

from agentdb import AgentDB
from agentdb.core.models import MemoryRecord
from agentdb.governance.permission_router import PermissionDenied


def _db(tmp_path, verifier=None):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path,
                   authority_verifier=verifier)


def _rec(**kw):
    base = dict(key="memory/x", value="v", origin="agent_inferred",
               trust_level="agent", agent_id="a1")
    base.update(kw)
    return MemoryRecord(**base)


def _allow(claim):
    return True


def _deny(claim):
    return False


# ---------------------------------------------------------------------------
# Origin / metadata cannot manufacture verified authority
# ---------------------------------------------------------------------------

def test_origin_string_alone_is_asserted_not_verified(tmp_path):
    db = _db(tmp_path)
    r = db.write("memory/policy/a", "use tabs", origin="human_approved", agent_id="a1")
    assert str(r.origin) == "human_approved"
    assert r.has_verified_approval() is False
    assert r.authority() == "asserted_human"


def test_forged_matching_fingerprint_via_write_is_not_verified(tmp_path):
    """A caller who computes the exact fingerprint and passes it as approval=
    still cannot create verified authority through write()."""
    db = _db(tmp_path)
    probe = _rec(key="memory/policy/b", value="use spaces", origin="human_approved")
    fp = probe.approval_fingerprint()
    r = db.write(
        "memory/policy/b", "use spaces", origin="human_approved", agent_id="a1",
        approval={"verified": True, "version": probe.APPROVAL_DIGEST_VERSION,
                  "principal": "eve", "mechanism": "self", "timestamp": "t",
                  "fingerprint": fp},
    )
    assert r.has_verified_approval() is False
    assert r.authority() == "asserted_human"


# ---------------------------------------------------------------------------
# Fail-closed and verifier decisions
# ---------------------------------------------------------------------------

def test_missing_verifier_fails_closed(tmp_path):
    db = _db(tmp_path)  # no verifier
    r = db.write("memory/policy/c", "deploy blue", origin="agent_inferred", agent_id="a1")
    try:
        db.approve_authority(r.id, principal="alice", mechanism="cli")
        assert False, "expected PermissionDenied"
    except PermissionDenied:
        pass
    assert db._store.get_by_id(r.id).has_verified_approval() is False


def test_verifier_denial_rejects(tmp_path):
    db = _db(tmp_path, verifier=_deny)
    r = db.write("memory/policy/d", "deploy", origin="agent_inferred", agent_id="a1")
    try:
        db.approve_authority(r.id, principal="alice", mechanism="cli")
        assert False, "expected PermissionDenied"
    except PermissionDenied:
        pass
    assert db._store.get_by_id(r.id).authority() == "agent"


def test_verifier_approval_grants_verified(tmp_path):
    db = _db(tmp_path, verifier=_allow)
    r = db.write("memory/policy/e", "deploy green", origin="agent_inferred", agent_id="a1")
    updated = db.approve_authority(r.id, principal="alice", mechanism="cli-confirm")
    assert updated.origin == "human_approved"
    assert updated.has_verified_approval() is True
    assert updated.authority() == "verified_human"
    ap = updated.approval
    assert ap["principal"] == "alice" and ap["verified"] is True
    assert ap["fingerprint"] and ap["timestamp"]


# ---------------------------------------------------------------------------
# Binding invalidation
# ---------------------------------------------------------------------------

def test_post_approval_value_change_voids_verification(tmp_path):
    db = _db(tmp_path, verifier=_allow)
    r = db.write("memory/policy/f", "value A", origin="agent_inferred", agent_id="a1")
    db.approve_authority(r.id, principal="alice", mechanism="cli")
    rec = db._store.get_by_id(r.id)
    rec.value = "value B (edited after approval)"
    assert rec.has_verified_approval() is False
    assert rec.authority() == "asserted_human"


def test_post_approval_scope_change_voids_verification(tmp_path):
    db = _db(tmp_path, verifier=_allow)
    r = db.write("memory/policy/g", "value", origin="agent_inferred", agent_id="a1",
                 workspace_id="ws-1")
    db.approve_authority(r.id, principal="alice", mechanism="cli")
    rec = db._store.get_by_id(r.id)
    rec.workspace_id = "ws-2"  # scope change after approval
    assert rec.has_verified_approval() is False


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

def test_revocation_removes_authority_with_audit(tmp_path):
    db = _db(tmp_path, verifier=_allow)
    r = db.write("memory/policy/h", "value", origin="agent_inferred", agent_id="a1")
    db.approve_authority(r.id, principal="alice", mechanism="cli")
    assert db._store.get_by_id(r.id).has_verified_approval() is True
    db.revoke_authority(r.id, principal="alice", mechanism="cli")
    rec = db._store.get_by_id(r.id)
    assert rec.has_verified_approval() is False
    assert rec.authority() == "agent"  # reverts to the underlying machine origin
    assert rec.approval.get("revoked") is True
    # Audit trail records both approve and revoke.
    actions = [e.payload.get("action") for e in db.audit_log.replay(0)
               if e.event_type == "memory_write"]
    assert "approve_authority" in actions and "revoke_authority" in actions
    # Underlying memory preserved (not deleted).
    assert rec.value == "value"


def test_revocation_requires_verifier(tmp_path):
    db = _db(tmp_path)  # no verifier
    r = db.write("memory/policy/i", "value", origin="human_approved", agent_id="a1")
    try:
        db.revoke_authority(r.id, principal="alice", mechanism="cli")
        assert False
    except PermissionDenied:
        pass


def test_repeated_approval_is_idempotent_and_audited(tmp_path):
    db = _db(tmp_path, verifier=_allow)
    r = db.write("memory/policy/j", "value", origin="agent_inferred", agent_id="a1")
    db.approve_authority(r.id, principal="alice", mechanism="cli")
    db.approve_authority(r.id, principal="alice", mechanism="cli")
    rec = db._store.get_by_id(r.id)
    assert rec.has_verified_approval() is True
    assert len(rec.approval.get("history", [])) >= 2


# ---------------------------------------------------------------------------
# Legacy + derived + ranking
# ---------------------------------------------------------------------------

def test_legacy_human_approved_readable_as_asserted():
    legacy = _rec(origin="human_approved", trust_level="human", approval={})
    assert str(legacy.origin) == "human_approved"
    assert legacy.has_verified_approval() is False
    assert legacy.authority() == "asserted_human"


def test_legacy_summary_approval_not_promoted():
    """Old-style approval metadata (value digest, no verified flag) is legacy
    evidence, NOT verified under the new contract."""
    legacy = _rec(origin="human_approved",
                  approval={"principal": "x", "mechanism": "ui",
                            "timestamp": "t", "digest": "abc"})
    assert legacy.has_verified_approval() is False
    assert legacy.authority() == "asserted_human"


def test_derived_canonical_cannot_be_verified_human():
    canon = _rec(kind="derived_canonical", producer="consolidation",
                 origin="agent_inferred", approval={})
    assert canon.authority() == "agent"


def test_verified_human_ranks_above_asserted_human():
    from agentdb.retrieval.algorithm import MultiSignalRanker
    verified = _rec(key="memory/orders/v", value="orders verified", origin="human_approved")
    verified.approval = {"verified": True, "version": verified.APPROVAL_DIGEST_VERSION,
                         "principal": "alice", "mechanism": "cli", "timestamp": "t",
                         "fingerprint": verified.approval_fingerprint()}
    asserted = _rec(key="memory/orders/a", value="orders asserted", origin="human_approved")
    ranker = MultiSignalRanker(staleness_days=90)
    order = [r.record.key for r in ranker.rank("orders", [asserted, verified], agent_id="a1")]
    assert order[0] == "memory/orders/v"
