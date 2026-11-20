"""OD-2 — human authority without an approval subsystem (CONTEXTUAL-SPEC §5).

Verifies the trusted-host provenance contract:
- A caller cannot manufacture *verified* human authority with an origin string.
- The trusted-host approval pathway (@AgentDB.approve_authority / write(approval=))
  records and binds verified approval evidence to the content digest.
- Legacy origin='human_approved' records remain readable as *asserted* authority.
- Derived canonicals cannot escalate to verified human authority.
- Applicable human decisions rank above agent knowledge; a bare asserted-human
  record ranks below a verified one.
"""
from __future__ import annotations

from agentdb import AgentDB
from agentdb.core.models import MemoryRecord


def _db(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)


def _rec(**kw):
    base = dict(key="memory/x", value="v", origin="agent_inferred",
               trust_level="agent", agent_id="a1")
    base.update(kw)
    return MemoryRecord(**base)


# ---------------------------------------------------------------------------
# Caller cannot manufacture verified authority
# ---------------------------------------------------------------------------

def test_origin_string_alone_is_asserted_not_verified(tmp_path):
    db = _db(tmp_path)
    r = db.write("memory/policy/a", "use tabs", origin="human_approved", agent_id="a1")
    assert str(r.origin) == "human_approved"
    assert r.has_verified_approval() is False
    assert r.authority() == "asserted_human"


def test_write_approval_with_wrong_digest_is_not_verified(tmp_path):
    db = _db(tmp_path)
    r = db.write(
        "memory/policy/b", "use spaces", origin="human_approved", agent_id="a1",
        approval={"principal": "alice", "mechanism": "ui", "timestamp": "t",
                  "digest": "deadbeef-not-matching"},
    )
    assert r.has_verified_approval() is False
    assert r.authority() == "asserted_human"


# ---------------------------------------------------------------------------
# Trusted-host approval pathway preserves verified evidence
# ---------------------------------------------------------------------------

def test_approve_authority_grants_verified_authority(tmp_path):
    db = _db(tmp_path)
    r = db.write("memory/policy/c", "deploy blue", origin="agent_inferred", agent_id="a1")
    updated = db.approve_authority(r.id, principal="alice", mechanism="cli-confirm")
    assert updated.origin == "human_approved"
    assert updated.has_verified_approval() is True
    assert updated.authority() == "verified_human"
    ap = updated.approval
    assert ap["principal"] == "alice" and ap["mechanism"] == "cli-confirm"
    assert ap["digest"] and ap["timestamp"]


def test_write_with_correct_approval_digest_is_verified(tmp_path):
    db = _db(tmp_path)
    # A trusted host can compute the digest and pass evidence at write time.
    digest = db._content_digest("production uses postgres 16")
    r = db.write(
        "memory/policy/d", "production uses postgres 16",
        origin="human_approved", agent_id="a1",
        approval={"principal": "bob", "mechanism": "reviewed-pr",
                  "timestamp": "2026-11-20T00:00:00+00:00", "digest": digest},
    )
    assert r.has_verified_approval() is True
    assert r.authority() == "verified_human"


def test_post_approval_edit_voids_verification(tmp_path):
    db = _db(tmp_path)
    r = db.write("memory/policy/e", "value A", origin="agent_inferred", agent_id="a1")
    db.approve_authority(r.id, principal="alice", mechanism="cli")
    # Tamper with value after approval → digest no longer matches.
    rec = db._store.get_by_id(r.id)
    rec.value = "value B (edited after approval)"
    assert rec.has_verified_approval() is False
    assert rec.authority() == "asserted_human"


# ---------------------------------------------------------------------------
# Legacy asserted authority remains readable, not rewritten
# ---------------------------------------------------------------------------

def test_legacy_human_approved_readable_as_asserted():
    legacy = _rec(origin="human_approved", trust_level="human", approval={})
    assert str(legacy.origin) == "human_approved"     # not rewritten
    assert legacy.has_verified_approval() is False
    assert legacy.authority() == "asserted_human"


# ---------------------------------------------------------------------------
# Derived canonicals cannot escalate authority
# ---------------------------------------------------------------------------

def test_derived_canonical_cannot_be_verified_human():
    canon = _rec(kind="derived_canonical", producer="consolidation",
                 origin="agent_inferred", approval={})
    assert canon.authority() == "agent"


# ---------------------------------------------------------------------------
# Ranking: verified > asserted > provider > agent
# ---------------------------------------------------------------------------

def test_verified_human_ranks_above_asserted_human(tmp_path):
    from agentdb.retrieval.algorithm import MultiSignalRanker
    verified = _rec(key="memory/orders/v", value="orders verified", origin="human_approved")
    verified.approval = {"principal": "alice", "mechanism": "cli",
                         "timestamp": "t", "digest": _digest_of(verified.value)}
    asserted = _rec(key="memory/orders/a", value="orders asserted", origin="human_approved")
    ranker = MultiSignalRanker(staleness_days=90)
    order = [r.record.key for r in ranker.rank("orders", [asserted, verified], agent_id="a1")]
    assert order[0] == "memory/orders/v"


def _digest_of(value):
    import hashlib, json
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
