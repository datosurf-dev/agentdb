"""Deterministic adversarial canonical/consolidation tests (Workstream D; gate D).

Exercises the consolidation pipeline against the product contract's canonical
invariants using a deterministic synthesis stub (no real LLM). Controlled
fixtures: exact repos, provenance classes, frozen timestamps, fixed IDs.

Invariants checked (BOUNDARY_CLEANUP §6):
  D1 original sources recoverable
  D2 source content not overwritten
  D3 canonical lineage contains all contributing source IDs
  D4 canonical authority never exceeds verified authority
  D5 unrelated facts not merged on shared keywords
  D6 contradictory facts not silently combined
  D7 unique source details remain accessible
  D8 stale canonicals handled explicitly
  D9 retrieval avoids canonical/source duplication
  D10 repeated consolidation does not corrupt lineage
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from agentdb import AgentDB
from tests.fixtures.adversarial import (
    FROZEN_NOW,
    digest_of,
    frozen_clock,
    make_record_spec,
    write_specs,
)


def _db(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)


def _stub_llm(prompt: str) -> str:
    """Deterministic synthesis stub — echoes a fixed canonical phrase.

    Returning a fixed string (not a model call) keeps consolidation offline and
    reproducible while exercising the 'synthesis happened' code path.
    """
    return "canonical synthesis result"


def _seed_duplicates(db, created_offset_hours=1):
    """Two near-duplicate records sharing the same key domain (mergeable)."""
    with frozen_clock():
        write_specs(db, [
            make_record_spec("S1", "memory/analytics-schema-note",
                             "analytics schema uses columnar layout",
                             created_at=FROZEN_NOW - timedelta(hours=created_offset_hours)),
            make_record_spec("S2", "memory/analytics-schema-update",
                             "analytics schema columnar store confirmed",
                             created_at=FROZEN_NOW - timedelta(hours=created_offset_hours)),
        ])
        return db.consolidate("a1", since_hours=0, min_cluster_size=2)


# D1 + D2 — sources recoverable, not overwritten
def test_d1_d2_sources_preserved_after_consolidation(tmp_path):
    db = _db(tmp_path)
    _seed_duplicates(db)
    s1 = db._store.get_by_id("S1")
    s2 = db._store.get_by_id("S2")
    assert s1.value == "analytics schema uses columnar layout"
    assert s2.value == "analytics schema columnar store confirmed"
    # archived (out of default selection) but still present and recoverable
    assert s1.archived and s2.archived
    assert db._store.get_by_id("S1") is not None


# D3 — lineage contains all contributing source IDs
def test_d3_lineage_has_all_sources(tmp_path):
    db = _db(tmp_path)
    _seed_duplicates(db)
    canon = [r for r in db._store.list_all() if r.kind == "derived_canonical"]
    # sources are archived so list_all hides them; search all including archived
    all_recs = list(db._store._by_id.values())
    canon = [r for r in all_recs if r.kind == "derived_canonical"]
    assert len(canon) == 1
    assert set(canon[0].source_record_ids) == {"S1", "S2"}
    derivation_ids = {s["id"] for s in canon[0].derivation["sources"]}
    assert derivation_ids == {"S1", "S2"}


# D4 — canonical authority never exceeds verified authority
def test_d4_canonical_never_human_approved(tmp_path):
    db = _db(tmp_path)
    _seed_duplicates(db)
    all_recs = list(db._store._by_id.values())
    canon = [r for r in all_recs if r.kind == "derived_canonical"][0]
    assert str(canon.origin) == "agent_inferred"
    assert canon.producer == "consolidation"


def test_d4_human_approved_source_blocks_merge(tmp_path):
    """A cluster containing a human_approved record is never auto-merged
    (it must not silently gain or dilute human authority)."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("H", "memory/fact-note", "verified human fact",
                             origin="human_approved"),
            make_record_spec("A", "memory/fact-update", "agent fact",
                             origin="agent_inferred"),
        ])
        result = db.consolidate("a1", since_hours=0, min_cluster_size=2)
    all_recs = list(db._store._by_id.values())
    assert not [r for r in all_recs if r.kind == "derived_canonical"]
    assert result.records_merged == 0
    # human record untouched and still active
    h = db._store.get_by_id("H")
    assert str(h.origin) == "human_approved"
    assert not h.archived


# D5 — unrelated facts sharing keywords are not merged
def test_d5_shared_keyword_distinct_domain_not_merged(tmp_path):
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("O", "memory/orders-note", "orders module retries"),
            make_record_spec("B", "memory/billing-note", "billing module retries"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    all_recs = list(db._store._by_id.values())
    assert not [r for r in all_recs if r.kind == "derived_canonical"]
    assert not db._store.get_by_id("O").archived
    assert not db._store.get_by_id("B").archived


# D6 — contradictory facts must not be merged nor destroyed (OD-1 fixed).
def test_d6_same_key_temporal_conflict_not_merged(tmp_path):
    """Identical-key temporal conflict (v1 vs v2): not merged (hard invariant)."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("V1", "memory/orders-model-state", "v1"),
            make_record_spec("V2", "memory/orders-model-state", "v2"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    all_recs = list(db._store._by_id.values())
    assert not [r for r in all_recs if r.kind == "derived_canonical"]
    assert not db._store.get_by_id("V1").archived
    assert not db._store.get_by_id("V2").archived


def test_d6_cross_key_contradictory_values_remain_recoverable(tmp_path):
    """Cross-key contradiction in one domain (postgres 15 vs 16):

    With OD-1 fixed, these records no longer merge. This test pins the
    invariants that must hold either way: no source is destroyed or overwritten,
    and (if any canonical existed) it would retain both source IDs. Sources stay
    recoverable and distinguishable.
    """
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("V15", "memory/db-version-note", "use postgres 15"),
            make_record_spec("V16", "memory/db-version-update", "use postgres 16"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2)

    v15 = db._store.get_by_id("V15")
    v16 = db._store.get_by_id("V16")
    # No destructive alteration: original values intact and recoverable.
    assert v15.value == "use postgres 15"
    assert v16.value == "use postgres 16"
    # If a canonical was produced, it must retain BOTH source ids (no data loss).
    canon = [r for r in db._store._by_id.values() if r.kind == "derived_canonical"]
    for c in canon:
        assert set(c.source_record_ids) == {"V15", "V16"}


def test_d6_cross_key_contradiction_should_not_merge(tmp_path):
    """OD-1 fixed: cross-key records with a single differing value token
    ("postgres 15" vs "postgres 16") must NOT be merged into one canonical."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("V15", "memory/db-version-note", "use postgres 15"),
            make_record_spec("V16", "memory/db-version-update", "use postgres 16"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    all_recs = list(db._store._by_id.values())
    assert not [r for r in all_recs if r.kind == "derived_canonical"]
    # Both sources remain active (distinction preserved).
    assert not db._store.get_by_id("V15").archived
    assert not db._store.get_by_id("V16").archived


# D7 — unique source details remain accessible
def test_d7_unique_detail_retrievable_after_consolidation(tmp_path):
    """When the canonical is selected for a query that also names a detail the
    canonical's value omits, the source carrying that detail is re-injected
    (support path for Product Contract §5C)."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("S1", "memory/auth-schema-note",
                             "auth schema uses tokens"),
            # S2 adds a non-value detail token ("rotation") so the pair is a
            # compatible near-duplicate that merges; the canonical stub omits it.
            make_record_spec("S2", "memory/auth-schema-update",
                             "auth schema uses tokens rotation"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2, llm_fn=_stub_llm)
    # Query names the canonical domain (auth schema) AND the detail (rotation).
    res = db.retrieve("auth schema rotation", agent_id="a1", limit=10, trace=True)
    assert "S2" in [r.id for r in res.records]
    assert "S2" in res.trace.get("unique_detail_ids", [])


# D8 — stale canonical handled explicitly
def test_d8_stale_canonical_excluded(tmp_path):
    db = _db(tmp_path)
    _seed_duplicates(db)
    # revise a source after consolidation
    with frozen_clock() as clk:
        clk.advance(hours=2)
        s1 = db._store.get_by_id("S1")
        s1.value = "analytics schema uses ROW layout now"  # content changed
        db._store.write(s1)
        res = db.retrieve("analytics schema", agent_id="a1", limit=10, trace=True)
    canon_ids = [r.id for r in res.records if r.kind == "derived_canonical"]
    assert canon_ids == []
    reasons = {e["id"]: e["reason"] for e in res.trace["excluded"]}
    assert any(r == "canonical_source_revised" for r in reasons.values())


# D9 — retrieval avoids canonical/source duplication
def test_d9_no_canonical_source_duplication(tmp_path):
    db = _db(tmp_path)
    _seed_duplicates(db)
    with frozen_clock() as clk:
        clk.advance(hours=1)
        res = db.retrieve("analytics schema", agent_id="a1", limit=10)
    ids = [r.id for r in res.records]
    # canonical present, its sources not re-injected
    assert any(r.kind == "derived_canonical" for r in res.records)
    assert "S1" not in ids and "S2" not in ids


# D10 — repeated consolidation does not corrupt lineage
def test_d10_repeated_consolidation_idempotent_lineage(tmp_path):
    db = _db(tmp_path)
    _seed_duplicates(db)
    # run again: sources are archived, canonicals excluded from re-consolidation
    with frozen_clock() as clk:
        clk.advance(hours=1)
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    all_recs = list(db._store._by_id.values())
    canon = [r for r in all_recs if r.kind == "derived_canonical"]
    # exactly one canonical; no canonical-of-canonical, no duplicate
    assert len(canon) == 1
    assert set(canon[0].source_record_ids) == {"S1", "S2"}


# D8b — a genuinely missing/deleted source invalidates its canonical
def test_d8_missing_source_invalidates_canonical(tmp_path):
    """A canonical whose source was deleted is excluded (canonical_source_missing)
    rather than presented as fresh."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("S1", "memory/orders/s1", "orders v1", archived=True),
        ])
        dg = digest_of("orders v1", key="memory/orders/s1")
        write_specs(db, [
            make_record_spec(
                "C1", "memory/canonical/orders/x", "orders canonical",
                consolidated=True, kind="derived_canonical",
                derived_from=["S1"], derived_digests=[dg],
            ),
        ])
        # Hard-delete the source (simulates a genuinely missing source).
        with db._store._connect() as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", ("S1",))
        db._store._reload()
        assert db._store.get_by_id("S1") is None

        res = db.retrieve("orders", agent_id="a1", limit=10, trace=True)
    assert "C1" not in [r.id for r in res.records]
    reasons = {e["id"]: e["reason"] for e in res.trace["excluded"]}
    assert reasons.get("C1") == "canonical_source_missing"


# D10b — concurrent consolidation does not double-archive or duplicate canonicals
@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: concurrent consolidate() calls each create a canonical "
           "because there is no idempotency guard/lock; a proper fix is a "
           "storage/concurrency design decision (tracked separately; see "
           "docs/spec/implementation/MIGRATION_REPORT.md). "
           "Remove xfail when a guard is approved and implemented.",
)
def test_d10_concurrent_consolidation_no_duplicates(tmp_path):
    """Two consolidation runs racing over the same cluster must not produce more
    than one canonical nor double-count archived sources (idempotence under
    concurrency). Deterministic single-process simulation using threads.

    KNOWN GAP (xfail): without a guard, N concurrent runs create N canonicals for
    the same source set. Sources themselves are never destroyed. See OD-6."""
    import threading

    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("S1", "memory/analytics-schema-note", "analytics layout"),
            make_record_spec("S2", "memory/analytics-schema-update", "analytics confirmed"),
        ])
    errors = []

    def run():
        try:
            db.consolidate("a1", since_hours=0, min_cluster_size=2)
        except Exception as e:  # pragma: no cover - surfaced if racy
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"consolidation raised under concurrency: {errors}"
    all_recs = list(db._store._by_id.values())
    canon = [r for r in all_recs if r.kind == "derived_canonical"]
    # No duplicate canonicals for the same source set.
    lineages = {frozenset(c.source_record_ids) for c in canon}
    assert len(canon) == len(lineages), f"duplicate canonicals: {lineages}"
    assert len(canon) <= 1
    # Sources archived exactly once (flag is idempotent, values intact).
    assert db._store.get_by_id("S1").value == "analytics layout"
    assert db._store.get_by_id("S2").value == "analytics confirmed"

