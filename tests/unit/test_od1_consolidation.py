"""OD-1 — safe canonical consolidation (CONTEXTUAL-SPEC §4).

Deterministic, offline tests for the value-compatibility gate that prevents
cross-key contradictory values from being merged into one canonical, while
preserving legitimate merges. Also unit-tests the classifier directly.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from agentdb import AgentDB
from agentdb.governance.conflict_detector import (
    COMPATIBLE,
    CONTEXT_SPECIFIC_DIFFERENCE,
    INSUFFICIENT_EVIDENCE,
    UNRESOLVED_CONTRADICTION,
    VERIFIED_SUPERSESSION,
    ConflictDetector,
)
from tests.fixtures.adversarial import (
    FROZEN_NOW,
    frozen_clock,
    make_record_spec,
    write_specs,
)


def _db(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)


def _canonicals(db):
    return [r for r in db._store._by_id.values() if r.kind == "derived_canonical"]


# ---------------------------------------------------------------------------
# Classifier unit tests — narrow deterministic value compatibility
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("use postgres 15", "use postgres 16", UNRESOLVED_CONTRADICTION),
        ("deploy on port 8080", "deploy on port 9090", UNRESOLVED_CONTRADICTION),
        ("region us-east", "region eu-west", UNRESOLVED_CONTRADICTION),
        ("analytics schema columnar", "analytics schema row", COMPATIBLE),
        ("v1", "v2", INSUFFICIENT_EVIDENCE),  # bare labels, no shared context
        ("same value", "same value", COMPATIBLE),
    ],
)
def test_classifier_value_pairs(a, b, expected):
    assert ConflictDetector.classify_value_compatibility(a, b) == expected


def test_classifier_prose_variant_is_not_a_contradiction():
    # A prose-only difference must not be classified as a value contradiction
    # (so the conservative merge gate does not block it).
    verdict = ConflictDetector.classify_value_compatibility(
        "caching is enabled", "caching enabled"
    )
    assert verdict != UNRESOLVED_CONTRADICTION


def test_classifier_verified_supersession_flag():
    assert (
        ConflictDetector.classify_value_compatibility(
            "use postgres 15", "use postgres 16", explicit_supersession=True
        )
        == VERIFIED_SUPERSESSION
    )


# ---------------------------------------------------------------------------
# Consolidation behaviour — cross-key and same-key incompatibility
# ---------------------------------------------------------------------------

def test_cross_key_incompatible_values_not_merged(tmp_path):
    """The OD-1 regression: postgres 15 vs 16 under different keys, one domain."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("P15", "memory/db-version-note", "use postgres 15"),
            make_record_spec("P16", "memory/db-version-update", "use postgres 16"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert _canonicals(db) == []
    assert not db._store.get_by_id("P15").archived
    assert not db._store.get_by_id("P16").archived


def test_same_key_incompatible_values_not_merged(tmp_path):
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("A", "memory/cache-mode", "cache mode redis"),
            make_record_spec("B", "memory/cache-mode", "cache mode memcached"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert _canonicals(db) == []
    assert not db._store.get_by_id("A").archived
    assert not db._store.get_by_id("B").archived


def test_compatible_near_duplicates_do_merge(tmp_path):
    """Prose near-duplicates (no value contradiction) still consolidate."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("A", "memory/billing-schema-note",
                             "billing schema uses integer cents"),
            make_record_spec("B", "memory/billing-schema-update",
                             "billing schema stores integer cents"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    canon = _canonicals(db)
    assert len(canon) == 1
    assert set(canon[0].source_record_ids) == {"A", "B"}


def test_multiple_sources_with_mixed_provenance_contradiction_blocks(tmp_path):
    """Mixed-provenance cluster with a value contradiction does not merge."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("H", "memory/region-note", "region us-east",
                             origin="provider_ingested"),
            make_record_spec("A", "memory/region-update", "region eu-west",
                             origin="agent_inferred"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert _canonicals(db) == []


def test_temporal_upgrade_without_supersession_not_merged(tmp_path):
    """A version bump across keys is an unverified supersession → keep separate."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("V1", "memory/api-version-note", "api version 1"),
            make_record_spec("V2", "memory/api-version-update", "api version 2"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert _canonicals(db) == []


def test_distinct_workspace_facts_not_merged(tmp_path):
    """Two deployments of the same app with different values must stay separate."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("S", "memory/app-scale-note", "app replicas 3"),
            make_record_spec("P", "memory/app-scale-update", "app replicas 12"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert _canonicals(db) == []


def test_sources_preserved_after_blocked_merge(tmp_path):
    """No source is destroyed or overwritten when a merge is refused (OD-1 + D1/D2)."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("V15", "memory/db-version-note", "use postgres 15"),
            make_record_spec("V16", "memory/db-version-update", "use postgres 16"),
        ])
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert db._store.get_by_id("V15").value == "use postgres 15"
    assert db._store.get_by_id("V16").value == "use postgres 16"
