"""Phase C distilled-knowledge tests: immutable source lineage, conservative
merge, invalidation, and canonical/source dedup (ADB-06/07; T03-T06)."""
import pytest

from agentdb import AgentDB


def _db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


def _canonicals(db, agent_id):
    return [r for r in db._store.list_all()
            if r.agent_id == agent_id and getattr(r, "kind", "source") == "derived_canonical"]


# ---------------------------------------------------------------------------
# T03 — source immutability
# ---------------------------------------------------------------------------

def test_consolidation_does_not_mutate_source_values(tmp_path):
    db = _db(tmp_path)
    r1 = db.write("memory/analytics-schema-note", "original note value",
                  origin="agent_inferred", agent_id="a1", confidence=0.7)
    r2 = db.write("memory/analytics-schema-update", "original update value",
                  origin="agent_inferred", agent_id="a1", confidence=0.9)
    # Capture exact source values/digests before consolidation.
    before = {r.id: (r.key, r.value) for r in (r1, r2)}

    db.consolidate("a1", since_hours=0, min_cluster_size=2,
                   llm_fn=lambda prompt: "SYNTHESIZED canonical")

    # Sources still exist (archived, not deleted) with unchanged key+value.
    for rid, (key, value) in before.items():
        rec = db._store.get_by_id(rid)
        assert rec is not None, "source must not be deleted"
        assert rec.key == key
        assert rec.value == value, "source value must NOT be overwritten"
        assert rec.archived is True  # archived from selection, not mutated


def test_canonical_is_a_distinct_derived_record(tmp_path):
    db = _db(tmp_path)
    r1 = db.write("memory/analytics-schema-note", "analytics schema columnar layout", origin="agent_inferred", agent_id="a1")
    r2 = db.write("memory/analytics-schema-update", "analytics schema columnar layout confirmed", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    canon = _canonicals(db, "a1")
    assert len(canon) == 1
    c = canon[0]
    assert c.id not in {r1.id, r2.id}
    assert c.origin == "agent_inferred"
    assert c.producer == "consolidation"
    assert set(c.source_record_ids) == {r1.id, r2.id}
    assert c.derivation.get("sources")
    # Lineage digests recorded per source.
    dig_ids = {s["id"] for s in c.derivation["sources"]}
    assert dig_ids == {r1.id, r2.id}


# ---------------------------------------------------------------------------
# T04 — invalidation on source revision
# ---------------------------------------------------------------------------

def test_canonical_invalidated_when_source_revised(tmp_path):
    db = _db(tmp_path)
    r1 = db.write("memory/analytics-schema-note", "analytics schema columnar layout", origin="agent_inferred", agent_id="a1")
    r2 = db.write("memory/analytics-schema-update", "analytics schema columnar layout confirmed", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)

    # Revise one source out-of-band (simulate correction).
    src = db._store.get_by_id(r1.id)
    src.value = "analytics schema columnar layout CORRECTED"
    db._store.write(src)

    canon = _canonicals(db, "a1")[0]
    reason = db._canonical_invalidation_reason(canon)
    assert reason == "canonical_source_revised"

    # And it is excluded from retrieval selection.
    res = db.retrieve("analytics schema", agent_id="a1", trace=True)
    assert canon.id not in [r.id for r in res.records]
    excluded = {e["id"]: e["reason"] for e in res.trace.get("excluded", [])}
    assert excluded.get(canon.id) == "canonical_source_revised"


def test_valid_canonical_is_selected(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "analytics schema columnar layout", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "analytics schema columnar layout confirmed", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    canon = _canonicals(db, "a1")[0]
    assert db._canonical_invalidation_reason(canon) is None


# ---------------------------------------------------------------------------
# T05 — false-merge prevention
# ---------------------------------------------------------------------------

def test_related_but_distinct_keys_are_not_merged(tmp_path):
    db = _db(tmp_path)
    # Different domains that merely share the word "note".
    db.write("memory/orders-note", "orders fact", origin="agent_inferred", agent_id="a1")
    db.write("memory/billing-note", "billing fact", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    # Neither merged away: no canonical, both still active.
    assert _canonicals(db, "a1") == []
    active = [r.key for r in db._store.list_all() if r.agent_id == "a1"]
    assert "memory/orders-note" in active
    assert "memory/billing-note" in active


def test_temporal_conflict_blocks_merge(tmp_path):
    """Two records with the same key but different values are a temporal
    conflict; they must not be silently unified."""
    db = _db(tmp_path)
    # Same key written twice → temporal conflict (both active).
    db.write("memory/orders-model-state", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/orders-model-state", "v2", origin="agent_inferred", agent_id="a1")
    result = db.consolidate("a1", since_hours=0, min_cluster_size=2)
    # No canonical created from temporal-conflicting same-key pair.
    assert _canonicals(db, "a1") == []


def test_human_approved_sources_never_merged(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-policy", "authoritative", origin="human_approved", agent_id="a1")
    db.write("memory/analytics-schema-note", "observation", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    human = [r for r in db._store.list_all() if r.origin == "human_approved"]
    assert len(human) == 1 and not human[0].archived


# ---------------------------------------------------------------------------
# T06 — canonical/source dedup in retrieval
# ---------------------------------------------------------------------------

def test_retrieval_returns_canonical_not_its_archived_sources(tmp_path):
    db = _db(tmp_path)
    # Compatible near-duplicates (same assertion, prose difference — no value
    # contradiction), so they consolidate into one canonical (CONTEXTUAL-SPEC §4).
    db.write("memory/analytics-schema-note",
             "analytics schema uses a columnar layout",
             origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update",
             "analytics schema columnar layout confirmed",
             origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    canon = _canonicals(db, "a1")[0]

    res = db.retrieve("analytics schema", agent_id="a1", trace=True)
    ids = [r.id for r in res.records]
    assert canon.id in ids
    # The archived sources are not re-injected alongside the canonical.
    for sid in canon.source_record_ids:
        assert sid not in ids
    assert res.trace["selected_ids"] == ids


def test_include_consolidated_false_returns_no_canonical(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "analytics schema columnar layout", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "analytics schema columnar layout confirmed", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    res = db.retrieve("analytics schema", agent_id="a1", include_consolidated=False)
    assert all(getattr(r, "kind", "source") != "derived_canonical" for r in res.records)


def test_canonical_ids_explainable_in_trace(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "analytics schema columnar layout", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "analytics schema columnar layout confirmed", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    canon = _canonicals(db, "a1")[0]
    res = db.retrieve("analytics schema", agent_id="a1", trace=True)
    ranked_ids = [x["id"] for x in res.trace["ranked"]]
    assert canon.id in ranked_ids


# ---------------------------------------------------------------------------
# ADB-07 / T06 — unique source-detail re-injection
# ---------------------------------------------------------------------------

def test_unique_detail_source_reinjected_when_canonical_omits_it(tmp_path):
    db = _db(tmp_path)
    # Two compatible same-domain records (prose near-duplicates; the second adds
    # a non-value detail token "traceback") so they merge. The synthesized
    # canonical omits that detail, so a query naming it must re-inject the source.
    db.write("memory/api-errors-note",
             "API validation errors on duplicate key",
             origin="agent_inferred", agent_id="a1", confidence=0.6)
    db.write("memory/api-errors-update",
             "API validation errors on duplicate key traceback",
             origin="agent_inferred", agent_id="a1", confidence=0.9)
    # Synthesize a canonical that OMITS the detail token.
    db.consolidate("a1", since_hours=0, min_cluster_size=2,
                   llm_fn=lambda prompt: "API validation errors on duplicate key")
    canon = [r for r in db._store.list_all() if r.kind == "derived_canonical"][0]
    assert "traceback" not in str(canon.value)

    res = db.retrieve("api errors traceback", agent_id="a1", trace=True, budget_tokens=500)
    ids = [r.id for r in res.records]
    # Canonical present, and the source carrying the "traceback" detail re-injected.
    assert canon.id in ids
    assert any("traceback" in str(r.value) for r in res.records)
    assert res.trace.get("unique_detail_ids"), "trace must record unique-detail re-injection"


def test_no_unique_detail_reinjection_when_canonical_covers_query(tmp_path):
    db = _db(tmp_path)
    db.write("memory/pricing-policy-note", "pricing reflects current tiers",
             origin="agent_inferred", agent_id="a1", confidence=0.6)
    db.write("memory/pricing-policy-update", "pricing reflects current tiers now",
             origin="agent_inferred", agent_id="a1", confidence=0.9)
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    canon = [r for r in db._store.list_all() if r.kind == "derived_canonical"][0]
    res = db.retrieve("pricing policy", agent_id="a1", trace=True)
    # Query covered by canonical key/domain -> no source re-injection.
    assert not res.trace.get("unique_detail_ids")
    assert all(r.id not in canon.source_record_ids for r in res.records)
