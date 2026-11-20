# Consolidation Validation — Derived Canonicals & Source Immutability

**Date:** 2026-11-20
**Requirement:** ADB-06, ADB-07 (T03–T06)
**Tests:** `tests/unit/test_distilled_knowledge.py`, `tests/unit/test_memory_consolidation.py`

## Problem (baseline)

`LifecycleManager.consolidate()` selected the highest-confidence record **in a
cluster as the canonical and overwrote its value** (`canonical.value =
llm_fn(...)`), then archived the rest. Consequences:

- Source value destroyed (no immutability — T03 violated).
- The "canonical" was a mutated source, not a derived record with lineage.
- Mixed-provenance clusters could end up attributed to whichever record was mutated.

## Implemented behavior

1. **Derived canonical is a NEW record.**
   - `key = memory/canonical/<domain-slug>/<uuid>`
   - `kind = "derived_canonical"`, `producer = "consolidation"`
   - `origin = "agent_inferred"` always (a derived record cannot acquire
     `human_approved` implicitly — contract P-03 / decision A3).
   - `source_record_ids = [all cluster ids]`
   - `derivation = {version, created_at, strategy, sources:[{id,key,digest,origin}]}`

2. **Sources are never mutated.** Their `value`, `key`, `origin`, `confidence`
   are untouched. They are only soft-archived (removed from default selection,
   still recoverable by id / audit). Verified by `test_consolidation_does_not_mutate_source_values`.

3. **Conservative compatibility gate (T05).** A cluster is merged only if:
   - all members share the same **key-domain slug** (`_key_slug`, which drops a
     trailing qualifier token like `note`/`update`/`v2`), AND
   - no pair forms a temporal/authority conflict via `ConflictDetector.check`.
   Records that merely share words (`orders-note` vs `billing-note`) or that are
   temporal versions of the same key are **not** merged.

4. **Invalidation (T04).** Each source's content digest is recorded. On
   retrieval, a canonical whose recorded digest no longer matches a live source
   digest (source revised) is excluded with reason `canonical_source_revised`
   (also `canonical_source_missing`, `canonical_no_lineage`). The audit-retained
   sources remain the fallback.

5. **Selection & dedup (T06/T07c).** When `include_consolidated=True`, a valid
   canonical is selected and its (archived) sources are not re-injected. When
   `include_consolidated=False`, canonicals are excluded and the caller sees the
   raw source signal.

## Test evidence

| Test | Asserts |
|---|---|
| `test_consolidation_does_not_mutate_source_values` | source key+value unchanged; archived not deleted |
| `test_canonical_is_a_distinct_derived_record` | new id, origin=agent_inferred, producer=consolidation, lineage digests |
| `test_canonical_invalidated_when_source_revised` | digest mismatch → excluded with reason |
| `test_valid_canonical_is_selected` | valid canonical has no invalidation reason |
| `test_related_but_distinct_keys_are_not_merged` | no canonical; both sources active |
| `test_temporal_conflict_blocks_merge` | same-key temporal pair not unified |
| `test_human_approved_sources_never_merged` | human record untouched |
| `test_retrieval_returns_canonical_not_its_archived_sources` | canonical in output, 0 source dupes |
| `test_include_consolidated_false_returns_no_canonical` | no canonicals when excluded |

All pass; see `TEST_RESULTS`.

## Deferred (open decisions — see REMAINING_DECISIONS)

- **A3 — canonical trust across mixed-provenance sources:** canonicals are always
  `agent_inferred` today. Allowing a canonical to inherit *some* human authority
  when every source is human-approved requires an explicit rule and approval.
- **A4 — temporal supersession:** temporal-version clusters are deliberately
  *not* merged; defining newer-supersedes-older canonicalization is deferred.
- **Unique source detail preservation:** canonical currently suppresses its
  sources. Re-injecting a source when it carries a query-critical detail the
  canonical omits is designed but gated on A3/A4 (needs a per-query "unique
  detail" signal and a dedup policy that keeps archived sources reachable).
