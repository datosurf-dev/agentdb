# Remaining Decisions & Technical Debt

**Date:** 2026-11-20
**Source:** `docs/spec/DECISIONS_AND_TRACEABILITY.md` §4 (A1–A6)

Status legend: ✅ resolved in this work · 🟡 partially addressed · ⛔ open (needs maintainer)

## A1 — Migration/compatibility policy · ⛔ open (safe subset applied)

- **Done:** All schema changes are **additive + idempotent**; legacy rows read
  unchanged. No destructive migration. Public memory APIs preserved.
- **Open:** Whether this ships as one deprecation release or an immediate
  breaking minor. **Recommendation:** MINOR bump (`0.4.x → 0.5.0`) per
  `RELEASING.md` pre-1.0 policy. Version left to release manager.

## A2 — Read-authorization scope · ⛔ open (docs only, per ADB-11)

- **Done:** `read()`/`retrieve()` still do **not** enforce `can_read`. This is
  documented, not overstated.
- **Open:** Whether to enforce read authorization now (requires explicit tests +
  approval; no silent security change was made). Recommended as a separate,
  clearly disclosed security release.

## A3 — Canonical trust across mixed-provenance sources · 🟡 conservative default

- **Done:** Derived canonicals are **always `agent_inferred`**; they never
  inherit `human_approved`. `human_approved` sources are never merged/archived.
- **Open:** A rule that lets a canonical inherit *bounded* human authority when
  **all** sources are human-approved and a verified human action approves the
  canonical. Needs explicit approval before implementation. Current behavior is
  the safe lower bound.

## A4 — Temporal supersession · ⛔ open

- **Done:** Temporal-version conflicts (same key, different value) are **not**
  merged; they remain distinct and both active.
- **Open:** Newer-supersedes-older canonicalization without erasing history.
  Deferred; requires temporal scope + validity-interval semantics.

## A5 — Conflict treatment (advisory vs exclusion) · ⛔ open

- **Done:** Conflict flags still surface via `override_instructions`; records
  are not silently dropped.
- **Open:** Whether contradicted memories should be excluded from injection in
  high-risk cases. Advisory-only remains the default.

## A6 — In-memory constructor · ✅ resolved

- **Done:** `AgentDB(path=":memory:")` now uses a process-wide shared-cache
  in-memory DB with a lifetime anchor connection. No `./:memory:` file is
  created. Documented that two in-memory instances in one process share the DB.

## Other technical debt / follow-ups

- **Unique source detail preservation (T06 nuance):** canonical currently
  suppresses its sources entirely. Re-injecting a source that carries a
  query-critical detail omitted by its canonical is designed but gated on
  A3/A4 — it needs a per-query "unique detail" signal and a way to keep
  archived sources reachable without re-introducing duplication.
- **`retrieve_by_entities` ranking:** entity retrieval now shares eligibility +
  budget packing, but ranks by recency (not the multi-signal ranker, which needs
  a query embedding). Consider feeding entity matches through the ranker with a
  neutral query embedding.
- **Kill-switch for hybrid candidates:** `semantic_candidate_cap` is
  configurable; a full lexical-only override could be exposed if operators want
  to disable the semantic branch explicitly.
- **`consolidate()` unused params:** `gateway` and `skill_candidate_threshold`
  remain accepted for API compatibility but unused (documented in the method).
- **Latency at scale:** semantic candidate discovery scans all stored vectors
  (`EmbeddingStore.get_all`). Fine for local corpora; for large corpora a vector
  index or ANN structure would be needed. Out of scope here (no new storage
  engine per constraints).
