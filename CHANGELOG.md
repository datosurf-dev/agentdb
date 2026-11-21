# Changelog

All notable changes to AgentDB are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow the pre-1.0
policy in `RELEASING.md`.

## [Unreleased]

### Fixed — consolidation auto-trigger
- `AgentDBMiddleware.prepare_turn` and the Hermes adapter now include `agent_id`
  in the `conversation_started` dispatch. Previously only `session_id` was
  passed, so `_handle_conversation_started_metadata` saw `agent_id=None`, skipped
  `_increment_session_count`, and the per-agent session counter in
  `consolidation_state` never advanced — automatic consolidation could never
  fire. Per-agent scoping is unchanged (the counter still keys on `agent_id`).

> **Integrators patching around this bug:** `_increment_session_count` is a blind
> `+1` with no idempotency guard. If you manually incremented the session counter
> to compensate, remove that workaround (and version-gate it if you must support
> both) or the counter will advance twice per session once you upgrade.

### Added — consolidation synthesis helper
- `agentdb.providers.consolidation.build_llm_fn(model, *, base_url, api_key,
  max_tokens, temperature, timeout)` builds the `llm_fn(prompt)->str` callable
  `consolidate()` expects, so integrators supply only a model + connection params.
  Requires the new `[llm]` extra (`openai`).

## [0.6.0] — 2026-11-21

Contextual correctness and final hardening: trustworthy, relevant, efficiently
distilled context across tasks/workspaces, with verifiable authority boundaries,
safe consolidation, and concurrent consistency. All schema changes are additive
and idempotent; legacy records remain readable.

### Added — context applicability & identity
- Optional `scope=` on `retrieve()`/`retrieve_by_entities()`: `workspace_id`,
  `repository_id`, `environment`, `task_type`, `as_of`. Explicit identity/scope
  mismatch is inapplicable; missing scope is unknown (not a match); exact scope
  is a deterministic tie-break that does **not** change ranking weights.
- Host-supplied `workspace_id` / `repository_id` (independent, never credentials)
  plus `write()` kwargs `environment`, `task_type`, `observed_at`,
  `effective_from`, `effective_until`. Legacy `working_dirs` retained; basename is
  only a soft hint that never overrides explicit identity.
- Explicit temporal applicability: `scope={"as_of": t}` excludes records outside
  their `effective_from`/`effective_until`/`expiry_time` window. `created_at` is
  never substituted for `effective_from`.
- `render_context(result)`: concise canonical rendering with optional Scope/
  Evidence qualifiers and an explicit "Unresolved disagreement" block for
  material conflicts.

### Added — consolidation hardening (OD-1 / Workstream C)
- Only **equivalent** or **compatible-complementary** values merge. Negation
  ("required" vs "not required"), mutually-exclusive values (PostgreSQL 15 vs
  16), and explicit scope conflicts never merge; unknown compatibility → no merge.
- `validate_canonicals(agent_id)`: non-destructive maintenance reporting invalid
  canonicals (source revised/missing, no lineage, now-incompatible source set).
  Invalid canonicals are excluded from retrieval; sources remain reachable.

### Fixed — concurrent consolidation (OD-6)
- `consolidate()` is now transactional and idempotent: a stable canonical
  fingerprint + a single `BEGIN IMMEDIATE` transaction + partial unique index
  prevent duplicate active canonicals under concurrency. A failure rolls back, so
  sources are never archived without a valid replacement canonical. The former
  OD-6 expected-failure test now passes.

### Fixed — retrieval determinism
- `MultiSignalRanker` breaks equal-score ties by record `id` (was candidate-
  arrival order). Ranking weights unchanged — secondary sort key only.

### Changed — verified human authority (OD-2 / Workstream B) — SECURITY
- A bare `origin="human_approved"` is now **asserted**, not verified, authority.
  **Verified** authority requires a host-configured `authority_verifier` and an
  affirmative decision via `approve_authority()`; it is bound to a versioned
  content/scope fingerprint (edits invalidate it). `revoke_authority()` removes
  it with an audit trail. `write(approval=...)` stores unverified metadata only.
  Without a verifier, verified-authority requests fail closed.
- Legacy `human_approved` records (and old value-digest approvals) are preserved
  as **asserted**; never silently promoted. Promotion requires re-verification.

### Changed — read authorization (OD-3) — SECURITY
- With `permissions_path` (isolation mode), `read()`, `retrieve()`, and
  `retrieve_by_entities()` enforce `can_read`; reads without identity fail
  closed; a canonical is disclosed only if all its sources are readable. Default
  trusted mode (no `permissions_path`) is unchanged.

### Changed — final skill-coupling removal
- `LifecycleManager.consolidate()` drops the dead skill-specific
  `skill_candidate_threshold` parameter; `AgentDB.consolidate()` still accepts it
  with a `DeprecationWarning` (ignored) for backward compatibility.

### Schema (additive, idempotent)
- New nullable/defaulted `memories` columns: `environment`, `task_type`,
  `workspace_id`, `repository_id`, `observed_at`, `effective_from`,
  `effective_until`, `canonical_fingerprint`, `approval`. Best-effort partial
  unique index `ux_memories_canonical_fp` (skipped if legacy duplicates exist).

### Tests
- New deterministic, offline suites: adversarial retrieval/canonical (D1–D10),
  OD-1 consolidation, OD-2 authority, OD-3 read isolation, OD-6 concurrency,
  context relevance/rendering, temporal applicability, workspace identity,
  30-scenario golden retrieval (Recall@5 = 1.0, mean nDCG@5 ≈ 0.87), and a
  lineage-authorization benchmark. Full run: **614 passed, 4 skipped, 0 xfailed**.

### Notes
- No new runtime dependencies (SQLite + stdlib only). No knowledge graph, skills,
  orchestration, or autonomous-memory framework introduced.

## [0.5.0] — 2026-11-20

### Changed — core product alignment
- **Provenance fix (ADB-03):** `write_compaction_summary()` / `checkpoint()` now
  default to `origin="agent_inferred"`; machine summaries no longer acquire
  `human_approved` implicitly. Pass `origin="human_approved"` only for a
  verified human action.
- **Hybrid retrieval (ADB-05/08/09/10):** lexical ∪ semantic candidate union
  (semantic-only records now reachable), shared eligibility filters across both
  branches, budget packing that skips oversized records instead of stopping,
  `retrieve_by_entities` parity (now ranked via the multi-signal ranker), and an
  optional `trace=` diagnostic. Reuses the existing multi-signal ranker;
  deterministic lexical fallback. New config flag `embeddings.semantic_candidates`
  (default `true`) forces lexical-only retrieval when set `false`.
- **Distilled knowledge (ADB-06/07):** consolidation now creates a new
  `derived_canonical` record with immutable source lineage (sources are never
  overwritten), a conservative merge gate (no false merges), source-revision
  invalidation, and canonical/source dedup at retrieval. When a canonical's
  value omits a query-critical detail an archived source carries, that source is
  re-injected (`unique_detail`, surfaced in `trace.unique_detail_ids`).
- **Reliability (ADB-12):** `AgentDB(path=":memory:")` uses a working in-memory
  DB (no bogus `./:memory:` file).
- **Schema:** additive, idempotent columns `kind`, `producer`, `derivation` on
  `memories`; legacy rows unaffected.

### Changed — BREAKING
- **Skill management removed from AgentDB core.** Skill registration, versioning,
  health/staleness tracking, proposals/approval, evolution bridges, skill-specific
  notifications and **all skill-text injection** are gone. AgentDB is now a
  memory-only library.
  - Removed public API: `db.skills`, `db.skills_api` (`SkillsAPI`), and the
    `SkillConflict` / `Skill` exports.
  - Removed gateway skill proposals: `db.gateway.propose/approve/reject/list_proposals`
    and `auto_propose_from_cluster`.
  - Removed `RunnerCapabilities.inject_skills`; adapter async context and the
    Hermes `system_prompt_block()` no longer emit any skill content.
  - Removed skill notification events (`skill_stale`, `skill_review_needed`,
    `untracked_skill_candidate`); the only default notification event is now
    `conflict_detected`.
  - Removed `total_skills` / `stale_skill_count` from `db.system.agentdb_stats()`.
  - Removed optional extras `wikiskill`, `skillopt`, `evolution`.
  - Removed the `src/agentdb/skills/` package (archived in a separate skill package).
- Skill data is **not deleted**: existing `skills.sqlite` files are left intact.
  Use the standalone exporter to export and restore into a future skill package.
  Skill records are never converted to memories.

### Unchanged
- Memory ranking, retrieval, consolidation (minus skill-candidate dispatch),
  provenance/trust, permissions, audit and lifecycle are preserved. `428 passed,
  4 skipped` at extraction.

### Notes
- Released as **MINOR** (`0.4.1` → `0.5.0`) per the pre-1.0 "breaking changes
  bump MINOR" policy.
- Deferred / out of scope: no Skill Governance implementation or AgentDB↔skill
  integration contract was added.
