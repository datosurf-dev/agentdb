"""Provenance and trust-boundary regression tests (Workstream E; gate D/E).

These pin the *actual* provenance behavior of the public API so it cannot drift
silently, and explicitly document the trust boundary where the caller controls
``origin=`` (see PRODUCT_CONTRACT P-01 and OPEN_DECISIONS).

They also verify in-memory SQLite works across the connection lifecycle
(ADB-12), and that derived records do not inherit stronger authority.
"""
from __future__ import annotations

from datetime import timedelta

from agentdb import AgentDB
from tests.fixtures.adversarial import (
    FROZEN_NOW,
    frozen_clock,
    make_record_spec,
    write_specs,
)


def _db(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)


# E — write_compaction_summary never auto-grants human approval
def test_e_compaction_summary_default_is_not_human_approved(tmp_path):
    db = _db(tmp_path)
    db.write_compaction_summary(agent_id="a1", session_id="s1", summary="agent text")
    rec = db.read("memory/compaction/summary/s1")
    assert str(rec.origin) == "agent_inferred"


def test_e_trust_boundary_explicit_human_origin_is_caller_controlled(tmp_path):
    """DOCUMENTED TRUST BOUNDARY: ``origin='human_approved'`` is honored without
    independent verification. This test exists to make that boundary *visible*
    and is deliberately not treated as a fix target — adding an authorization
    system is a product decision (OPEN_DECISIONS.md). If this behavior is ever
    gated behind verification, update this test and the decision log together.
    """
    db = _db(tmp_path)
    db.write_compaction_summary(
        agent_id="a1", session_id="s1", summary="anything",
        origin="human_approved",
    )
    rec = db.read("memory/compaction/summary/s1")
    assert str(rec.origin) == "human_approved"  # caller-controlled, unverified


# E — derived records do not silently inherit stronger authority
def test_e_derived_record_never_inherits_human_authority(tmp_path):
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("H", "memory/policy-note", "policy A", origin="human_approved"),
            make_record_spec("A", "memory/policy-update", "policy A confirmed",
                             origin="agent_inferred"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    all_recs = list(db._store._by_id.values())
    for c in all_recs:
        if c.kind == "derived_canonical":
            assert str(c.origin) == "agent_inferred"


# E — lifecycle operations preserve provenance fields
def test_e_lifecycle_preserves_origin(tmp_path):
    db = _db(tmp_path)
    db.write("memory/orders/x", "orders content", origin="agent_inferred", agent_id="a1")
    rec_id = db.read("memory/orders/x").id
    db.lifecycle.reinforce(rec_id)
    after = db._store.get_by_id(rec_id)
    assert str(after.origin) == "agent_inferred"


# E — in-memory SQLite works across the connection lifecycle
def test_e_in_memory_db_persists_across_store_connections():
    db = AgentDB(path=":memory:")
    db.write("memory/mem/x", "in memory value", origin="agent_inferred", agent_id="a1")
    # Multiple separate store connections occur during retrieve; the record must
    # survive them (the anchor connection keeps the shared-cache DB alive).
    rec = db.read("memory/mem/x")
    assert rec is not None
    assert rec.value == "in memory value"
    res = db.retrieve("in memory value", agent_id="a1")
    assert any(r.id == rec.id for r in res.records)
