# AgentDB Core Product Alignment — Execution Plan

**Branch:** `feat/core-product-alignment`
**Base:** `main` @ `3dce7ba` (skill-extraction PR #10 merged)
**Baseline tests:** 428 passed, 4 skipped · Python 3.14.7

## 0. Previous-phase validation (done)

Verified against source, not docs:
- No `db.skills`, `db.skills_api`, `SkillStore`, `Skill`, `inject_skills` in `src/agentdb/`.
- Only intentional migration notes remain (hermes/gateway docstrings, unused `skill_candidate_threshold` param).
- 428 passed, 4 skipped. Cleanup confirmed complete — **not** repeating it.

## 1. Current vs contract — verified gaps

| ID | Gap (verified in source) | Location |
|---|---|---|
| ADB-03 | `write_compaction_summary()` and `checkpoint()` write `origin="human_approved"` for agent/external input | `client.py:1068-1100` |
| ADB-05 | Candidates = `store.search(query, limit=max(limit,50))` — **lexical-only, capped 50**; embeddings only rerank lexical hits → semantic-only records unreachable | `client.py:1140` |
| ADB-07 | No canonical/source dedup in retrieve; `consolidate` mutates highest-confidence record in place (`canonical.value = llm_fn(...)`) | `lifecycle_manager.consolidate`, `client.retrieve` |
| ADB-06 | Consolidated canonical overwrites a source record's value (T03 violation) | `lifecycle_manager.py:consolidate` |
| ADB-08 | Budget loop `break`s on first oversized record → smaller later records never considered | `client.retrieve` (~line 1222) |
| ADB-09 | `retrieve_by_entities()` diverges in filtering/packing from `retrieve()` | `client.py:1322` |
| ADB-10 | No structured candidate trace (origin/exclusion reason); `signal_breakdown` exists but not query-level provenance of candidate source | `algorithm.RankedRecord` |
| ADB-12 | `AgentDB(path=":memory:")` → `Path(":memory:").resolve()` = `./:memory:` **file**, not in-memory | `client.py:~` db_path resolution |
| ADB-11 | read()/retrieve() do not enforce can_read — documented as limitation | README/AGENTS |

## 2. Decisions affecting irreversible behavior (from DECISIONS §4)

- **A3 Canonical trust inheritance** — affects Phase C authority. **Plan:** canonicals are `agent_inferred` (derived, producer=consolidation); never inherit `human_approved` unless ALL sources + a verified human approval action. Document alternatives in `REMAINING_DECISIONS.md`; do not silently promote.
- **A4 Temporal semantics** — **Plan:** keep newer/older as distinct records; do not auto-merge temporal conflicts; document.
- **A2 Security scope** — **Plan:** docs-only correction + tests capturing current behavior. **No** silent authorization change (requires explicit approval).
- **A6 In-memory bug** — low-risk reliability; **Plan:** fix `:memory:` handling with test (ADB-12).
- **A1 Migration policy** — schema changes are **additive** (new nullable/defaulted columns) with idempotent migration; no destructive migration. Documented rollback.

Where a decision blocks correctness, implement the *safe subset* and record the open question — never guess irreversible behavior.

## 3. Phases (each independently tested)

### Phase A — Baseline + provenance (ADB-03, ADB-04)
1. Add regression fixtures capturing current provenance/trust behavior.
2. Fix `write_compaction_summary`: `origin` derived from producer (default `agent_inferred`); add explicit `origin=` param so a verified human action can pass `human_approved` deliberately. Same for `checkpoint`.
3. Tests: machine summary → `agent_inferred`; explicit human origin honored; provider-ingested never promoted.

### Phase B — Retrieval (ADB-05, 08, 09, 10)
1. Hybrid candidates: lexical (uncapped-ish) **union** semantic candidates (vector search over `memories` namespace) when embeddings active; dedupe by id.
2. Shared eligibility filter applied identically to both branches (agent/scope/archive/expiry/episodic/repo/consolidated).
3. Reuse existing `MultiSignalRanker` (no second ranker).
4. Deterministic lexical fallback when embeddings off/fail.
5. Budget packing: continue past oversized items instead of `break`.
6. Candidate trace in `RetrievalResult` (origin + exclusion reason).
7. `retrieve_by_entities` routed through shared eligibility+packing.
8. Tests: T07, T08, T09, T10, T11.

### Phase C — Distilled knowledge (ADB-06, 07)
1. Derived canonical as a **separate record** (`kind=derived_canonical`, `producer=consolidation`), sources never overwritten (T03).
2. Lineage: `source_record_ids` + new `derivation` metadata (per-source digest).
3. Conservative merge gate: require key/word overlap AND non-contradictory (reuse ConflictDetector); skip temporal contradiction.
4. Canonical/source dedup at retrieve: prefer valid canonical covering the fact; retain source with unique query-critical detail; trace decisions.
5. Invalidation: source revision marks canonical stale (skip stale canonicals from selection).
6. Tests: T03, T04, T05, T06.

### Phase D — Hardening (ADB-12, 11, docs, benchmarks)
1. `:memory:` fix (ADB-12) + test.
2. Additive schema migration + idempotency + rollback doc (T12).
3. Adapter compatibility tests; full evaluation.
4. Benchmarks before/after (recall@k, nDCG@k, canonical precision, false-merge rate, dup tokens, budget util, P50/P95 latency) with measured thresholds.
5. Docs + the 5 deliverable files.

## 4. Guardrails
- Reuse existing ranker/store/embeddings; no parallel retrieval framework.
- Additive schema only; no destructive deletes; source records never overwritten.
- No security promise expansion without tests + explicit approval.
- No skill reintroduction; no cross-package integration.
- Preserve public memory APIs; document any change.
