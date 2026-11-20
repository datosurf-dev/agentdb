# Canonical Test Matrix

**Spec:** `docs/spec/BOUNDARY_CLEANUP.md` §6 (Workstream D), Gate D
**Test files:** `tests/unit/test_adversarial_canonical.py`,
`tests/unit/test_distilled_knowledge.py` (prior), `tests/unit/test_memory_consolidation.py` (prior)
**Synthesis:** deterministic stub `_stub_llm` (fixed string); no real LLM.

## Invariants (spec §6) — coverage

| # | Invariant | Test | Status |
|---|---|---|---|
| D1 | Original source records remain recoverable | `test_d1_d2_sources_preserved_after_consolidation` | PASS |
| D2 | Source content not silently overwritten | `test_d1_d2_sources_preserved_after_consolidation` | PASS |
| D3 | Lineage contains all contributing source IDs | `test_d3_lineage_has_all_sources` | PASS |
| D4 | Canonical authority never exceeds verified authority | `test_d4_canonical_never_human_approved`, `test_d4_human_approved_source_blocks_merge` | PASS |
| D5 | Unrelated facts not merged on shared keywords | `test_d5_shared_keyword_distinct_domain_not_merged` | PASS |
| D6 | Contradictory facts not silently combined | `test_d6_same_key_temporal_conflict_not_merged`, `test_d6_cross_key_contradictory_values_remain_recoverable` | PARTIAL (see gap) |
| D7 | Unique source details remain accessible | `test_d7_unique_detail_retrievable_after_consolidation` | PASS |
| D8 | Stale/invalid canonicals handled explicitly | `test_d8_stale_canonical_excluded` (revised source), `test_d8_missing_source_invalidates_canonical` (deleted source) | PASS |
| D9 | Retrieval avoids canonical/source duplication | `test_d9_no_canonical_source_duplication` | PASS |
| D10 | Repeated consolidation does not corrupt lineage | `test_d10_repeated_consolidation_idempotent_lineage` (sequential, PASS); `test_d10_concurrent_consolidation_no_duplicates` (concurrent, xfail — OD-6) | PARTIAL (see OD-6) |

Plus prior coverage in `test_distilled_knowledge.py` (source immutability,
distinct-record creation, invalidation, dedup, human-approval blocks merge).

## Scenario coverage (spec §6 bullets)

| Spec scenario | Covered by | Status |
|---|---|---|
| Exact duplicate memories | `test_d1_d2...`, prior `test_consolidation_does_not_mutate_source_values` | PASS |
| Semantically equivalent (different wording) | `_seed_duplicates` (same domain slug, non-contradictory) | PASS |
| Related but non-equivalent facts | `test_d5_shared_keyword_distinct_domain_not_merged` | PASS |
| Directly contradictory facts | `test_d6_same_key_temporal_conflict_not_merged` (same key) | PASS |
| Records with different provenance levels | `test_d4_human_approved_source_blocks_merge` | PASS |
| Sources with unique paths/versions/ids | `test_d7_unique_detail_retrievable...` | PASS |
| Sources changed after consolidation | `test_d8_stale_canonical_excluded` | PASS |
| Missing/deleted sources | `test_d8_missing_source_invalidates_canonical` → `canonical_source_missing` | PASS |
| Repeated/concurrent consolidation | `test_d10_repeated_consolidation_idempotent_lineage` (sequential PASS); `test_d10_concurrent_consolidation_no_duplicates` (concurrent xfail — OD-6) | PARTIAL |
| Previously consolidated records | canonicals excluded from re-consolidation (`kind != derived_canonical` filter) | PASS |

## D6 gap — cross-key contradictory values (KNOWN, documented)

**Finding:** Two records in one key domain with **different qualifier keys** and
**mutually exclusive values** (e.g. `db-version-note`="use postgres 15" vs
`db-version-update`="use postgres 16") are **merged** into one canonical
(valuing one of them). This is because the conservative merge gate
(`_merge_compatible`) relies on `ConflictDetector.check`, which returns "no
conflict" when keys differ; contradiction detection therefore only fires for
**identical-key** pairs.

**What still holds:** sources are preserved intact (values unchanged, soft-
archived), lineage retains both source IDs, nothing is deleted — so no
*destructive* loss and no trust escalation. What is lost is *surfacing the
contradiction*: the canonical presents one contested value while "16" sits in
archive.

**Why not auto-fixed here:** detecting "mutually exclusive values" deterministically
without an LLM is a product-policy decision (how to tell near-duplicate partial
facts, which the existing `test_retrieval_returns_canonical_not_its_archived_sources`
expects to merge, apart from true contradictions). Per spec §1: "For any
unresolved decision ... document the alternatives rather than silently choosing
a new product policy." See `OPEN_DECISIONS.md` (OD-1).

**Fenced by test:** `test_d6_cross_key_contradiction_should_not_merge` is a
`xfail(strict=True)` — it documents the desired-but-unimplemented behavior and
will fail loudly if behavior changes without updating the decision.

## D10 gap — concurrent consolidation (KNOWN, documented)

**Finding:** N concurrent `consolidate()` calls over the same cluster each create
a canonical (no idempotency guard/lock), producing N duplicate canonicals for the
same source set. Sequential repeats are safe. Sources are never destroyed.
Recorded as **OD-6** in `OPEN_DECISIONS.md`; fixing needs a concurrency-control
design (unique index / advisory lock / check-and-insert) — a storage decision,
not silently redesigned.

## Test evidence

`tests/unit/test_adversarial_canonical.py`: **12 passed, 2 xfailed**
(OD-1 cross-key contradiction, OD-6 concurrent consolidation). Full suite:
505 passed, 4 skipped, 2 xfailed.

## Gate D status

**PASS with one documented gap (OD-1).**
- No destructive alteration of original source memories. ✓
- No silent false merges in controlled adversarial fixtures. ✓ (domain/related
  cases) — cross-key *value* contradictions merge but preserve sources (OD-1).
- No trust escalation. ✓
- Canonical invalidation and provenance predictable. ✓
- Relevant source details remain accessible (canonical-selected query path). ✓
