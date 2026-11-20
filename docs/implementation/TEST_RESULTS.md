# Test Results

**Release:** boundary-cleanup & retrieval-correctness hardening
**Date:** 2026-11-20
**Spec:** `docs/spec/BOUNDARY_CLEANUP.md` §8–§11, Gate E

## Environment

| Item | Value |
|---|---|
| Python | 3.14.7 |
| AgentDB branch | `release/boundary-cleanup` |
| AgentDB base | `ffe1e36` (main) |
| AgentDB head tested | `3330c6e`+ (this branch) |
| AgentGate branch | `chore/agentdb-0.5-drop-skills` |
| AgentGate head tested | `ff77b4f` |
| AgentDB distribution | `agentdatabase` 0.5.0 (install); source via `PYTHONPATH` |
| Embeddings | `controlled_embed` fixture only (offline, no model download) |
| Network / LLM / external embeddings | **None** (per spec exclusions) |

## Baseline (Phase 0, before changes)

| Suite | Result |
|---|---|
| AgentDB (`ffe1e36` / current base behavior) | **461 passed, 4 skipped** |
| AgentGate (installed agentdb 0.5.0) | **509 passed, 3 skipped** |

The 4 AgentDB skips are pre-existing `sentence-transformers` slow tests
(`test_local_embedding_provider.py`, require model download); unchanged.

## Final (after changes)

| Suite | Head | Result |
|---|---|---|
| AgentDB full | `3330c6e`+ | **504 passed, 4 skipped, 1 xfailed** |
| AgentGate full (installed 0.5.0) | `ff77b4f` | **514 passed, 3 skipped** |
| AgentGate full vs agentdb `3330c6e` (PYTHONPATH) | `ff77b4f` | **509 passed, 3 skipped** |
| AgentGate AgentDB-specific vs `3330c6e` | `ff77b4f` | **10 passed** |

**Delta:** AgentDB +43 new tests (one is a documented strict-`xfail`), zero
regressions. AgentGate +5 new integration tests, zero regressions.

## New test files (AgentDB)

| File | Tests | Purpose |
|---|---|---|
| `tests/fixtures/adversarial.py` | (fixtures) | frozen clock, explicit-id specs, digests |
| `tests/unit/test_adversarial_retrieval.py` | 19 | C1–C4 candidate/eligibility/ranking determinism/budget |
| `tests/unit/test_adversarial_canonical.py` | 11 + 1 xfail | D1–D10 canonical invariants |
| `tests/unit/test_provenance_regression.py` | 5 | E: trust boundary, derived authority, in-memory |
| `tests/unit/test_migration_regression.py` | 4 | E: additive schema migration + rollback read |
| `tests/unit/test_skill_cleanup_compat.py` | 4 | A: no exported skill surface, dead kwarg shim |

New test file (AgentGate): `tests/test_agentdb_memory_integration.py` (5).

## Bug fixed, justified by contract violation

**Retrieval determinism (Product Contract §5D; Gate C "results deterministic
when inputs and clock are fixed").**

`MultiSignalRanker.rank()` sorted only by `composite_score`. Equal-score records
therefore returned in candidate-arrival order, which depends on `dict`/SQLite
row iteration and differs across runs and entry points. Demonstrated by two
pre-fix failing tests (reversed-input order produced reversed output).

Fix: secondary sort key `(-composite_score, record.id)` — no ranking weights
changed. Both tests pass post-fix.

## Adversarial scenarios that failed pre-fix

| Test | Pre-fix | Post-fix |
|---|---|---|
| `test_c3_deterministic_tie_break_independent_of_input_order` | FAIL | PASS |
| `test_c3_deterministic_tie_break_through_retrieve` | FAIL | PASS |

## Documented gap (not a test failure)

- `test_d6_cross_key_contradiction_should_not_merge` — `xfail(strict=True)`.
  Cross-key contradictory values in one domain currently merge (sources
  preserved). Tracked as **OD-1** in `OPEN_DECISIONS.md`.

## Commands used

```bash
# AgentDB
cd agentdb && .venv/bin/python -m pytest -q
# AgentGate (installed 0.5.0)
cd agentgate && .venv/bin/python -m pytest -q
# AgentGate vs release-branch agentdb
cd agentgate && PYTHONPATH=/Users/jm/Documents/github/agentdb/src .venv/bin/python -m pytest -q
```

## Gate E status

**PASS.**
- Existing tests continue to pass (504 + prior semantics), except the
  intentionally-updated obsolete skill-param test (now the deprecation shim).
- Changed public API documented: `consolidate()` accepts-and-warns obsolete
  `skill_candidate_threshold`; ranking tie-break is internal (no API change).
- Migrations reversible: additive columns, non-destructive code rollback
  (documented + tested).
- No unresolved failures hidden — the one gap is an explicit `xfail` + OD-1.
