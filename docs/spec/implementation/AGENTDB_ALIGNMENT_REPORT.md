# AgentDB Core Product Alignment — Implementation Report

**Date:** 2026-11-20
**Branch:** `feat/core-product-alignment` (base `main` @ `3dce7ba`)
**Baseline tests:** 428 passed, 4 skipped
**Final tests:** 457 passed, 4 skipped

## 1. Implementation summary

This phase implemented AgentDB's core product contract (relevant, distilled,
trustworthy, token-efficient memory) on top of the completed skill extraction.
Work proceeded in four independently-tested phases:

### Phase A — Provenance (ADB-03)
`checkpoint()` / `write_compaction_summary()` previously wrote
`origin="human_approved"` for agent-supplied input — laundering machine text
into human authority. Now they default to `origin="agent_inferred"` and accept
an explicit `origin=` only for a verified human action. Agent summaries are now
subject to lifecycle decay; human-approved ones stay immune.

### Phase B — Retrieval (ADB-05, 08, 09, 10)
- Hybrid candidate generation: **lexical ∪ semantic** (whole-corpus vector scan),
  deduplicated by id. Semantic-only records are now reachable (was: capped
  lexical pass of 50, embeddings only re-ranked lexical hits).
- Shared `_eligibility_reason` applied identically to both branches (agent /
  episodic / repo / consolidated / expiry) — no branch can bypass scope.
- Budget packing **skips oversized records and continues** to smaller ones
  (was: `break` on first overflow).
- `retrieve_by_entities` routed through the same eligibility + budget packing (parity).
- Optional `trace=` on both entry points exposes candidate sources, exclusion
  reasons, and per-candidate ranking evidence.
- Reuses the existing `MultiSignalRanker`; deterministic lexical fallback.

### Phase C — Distilled knowledge (ADB-06, 07)
- Consolidation creates a **new `derived_canonical` record**; sources are never
  mutated/overwritten (only soft-archived). Full lineage via `source_record_ids`
  + per-source content digests in `derivation{}`.
- Canonicals are always `agent_inferred` (no implicit human authority).
- Conservative merge gate: shared key-domain slug AND no temporal/authority
  conflict → related-but-distinct or contradictory records are not merged.
- Canonical invalidation on source revision (digest mismatch) excludes stale
  canonicals from selection; retrieval returns the canonical, not its sources.

### Phase D — Hardening (ADB-12)
- `AgentDB(path=":memory:")` now uses a working process-wide shared-cache
  in-memory DB (no more bogus `./:memory:` file, tables visible across the
  per-store connections).
- Additive, idempotent schema migration; legacy rows unaffected.

## 2. Changed files

```
src/agentdb/core/models.py                  # kind/producer/derivation fields
src/agentdb/core/store.py                   # schema columns + read/write
src/agentdb/core/sqlite_connection.py       # shared-cache in-memory support
src/agentdb/embeddings/store.py             # get_all() for semantic candidates
src/agentdb/governance/lifecycle_manager.py # derived canonicals, merge gate, invalidation
src/agentdb/interface/client.py             # provenance fix, hybrid retrieval, packing, trace, entity parity, :memory:
tests/unit/test_context_compaction.py       # provenance tests updated (T02)
tests/unit/test_retrieval_alignment.py      # NEW: T07-T11 + trace
tests/unit/test_distilled_knowledge.py      # NEW: T03-T06
tests/unit/test_in_memory_db.py             # NEW: ADB-12
.gitignore                                   # track docs/spec/implementation/
docs/spec/implementation/*                   # deliverables
CHANGELOG.md                                 # updated
```

## 3. Tests executed

- Full suite: `python -m pytest -q` → **457 passed, 4 skipped** (baseline 428/4).
- New/changed blocking-test coverage:
  - Provenance: `test_context_compaction.py` (agent_inferred default, explicit
    human, provider not promoted, decay-vs-immune).
  - Retrieval: T07 semantic recall, T08 lexical fallback + embedding failure,
    T09 scope parity (agent/expiry), T10 budget packing, T11 entity parity,
    trace + determinism.
  - Distilled: T03 source immutability, T04 invalidation, T05 false-merge
    prevention, T06 canonical/source dedup.
  - Reliability: `:memory:` works, no file created.
- Migration: legacy old-schema DB opens, reads unchanged, new features work,
  canonicals persist across reopen (verified).

## 4. Before/after benchmarks

See `RETRIEVAL_BENCHMARKS.md`. Headline: **semantic recall@5 on paraphrase-only
queries: 0.0 → 0.625** with embeddings; lexical recall/nDCG/tokens/latency
unchanged. Budget packing and canonical dedup verified.

## 5. Database migration and rollback

See `MIGRATION_AND_ROLLBACK.md`. Purely additive columns (`kind`, `producer`,
`derivation`), idempotent, legacy-safe, non-destructive rollback (older code
ignores new columns).

## 6. Compatibility changes

- **Public API:** preserved. `retrieve`/`retrieve_by_entities` gained optional
  `trace=` (and entity retrieval gained `budget_tokens`, `include_consolidated`).
  All defaults preserve prior signatures.
- **Behavior change (intended, contract-mandated):** `write_compaction_summary`
  / `checkpoint` now default to `agent_inferred` (not `human_approved`). Callers
  needing human authority pass `origin="human_approved"` explicitly.
- **`MemoryRecord`** gained `kind`/`producer`/`derivation` (defaulted; safe).

## 7. Remaining issues / technical debt

See `REMAINING_DECISIONS.md`. Key open items: A1 (version policy — recommend
MINOR bump), A2 (read-authz — docs only, no silent change), A3 (canonical trust
across mixed provenance — safe lower bound only), A4 (temporal supersession),
A5 (conflict exclusion). Follow-ups: unique-source-detail re-injection, entity
ranking through the multi-signal ranker, ANN for large-corpus semantic scan.

## 8. Skill independence — confirmed

AgentDB remains fully independent of skill management:
- No `db.skills`, `db.skills_api`, `SkillStore`, `Skill`, `inject_skills` in
  `src/agentdb/` (verified by grep; only docstring migration notes remain).
- No skill injection in any adapter (tests assert no `## Skills` block).
- No Skill Governance functionality, cross-package interface, or skill data
  model was added in this phase.
