# KNOWN_ISSUES — Skill Functionality At Extraction

Observed limitations and incomplete capabilities of the skill subsystem as it
existed in AgentDB at commit `fe871c4`. The future package should be aware of
these; whether to carry them forward is a product decision for that package.

## Behavioral / design limitations found in source

1. **Proposals are stored as AgentDB memory records.** `Gateway.propose()` writes
   `proposal/<uuid>` into the *memory* store and `approve()`/`reject()` call
   `_store.delete_by_key()`. This couples skill workflow to memory storage and
   audit semantics — the new package should own its own tables.

2. **Approval promotes a draft to a skill but drops provenance.** `approve()`
   calls `skills_api.create_skill(...)` with a **new** `skill_id = proposal_id`;
   the draft's agent-inferred origin, domain words and any reviewer identity are
   not carried onto the created skill. Human-approval provenance is implicit.

3. **Health score mixes incomparable signals.** `_compute_health` blends an
   optional `validation_score` with an *age-based* usage proxy (`updated_at`).
   Age since edit is not evidence of usage or effectiveness — the maintainer
   assessment already flagged this (DECISIONS D06). Staleness ≠ degraded skill.

4. **Staleness/health crossing is a one-shot side effect inside a read.** 
   `get_for_agent()` persists a health drop and fires `on_stale_crossing` as a
   side effect of a getter, mixing read/write semantics.

5. **`SkillStore.get()` docstring is misleading.** It claims "falls back to oldest
   non-archived" but actually returns the newest non-archived row.

6. **`SkillConflict` guard is count-based, not identity-based.** `update()`
   raises only when >1 *active* version exists; it does not itself archive the
   superseded version, so callers must manage archival.

7. **Skill–memory "conflict" is keyword/semantic overlap, not contradiction.**
   `_check_skill_conflict` fires `skill_review_needed` on any word/semantic
   overlap — a weak signal, not proof of conflict (contract P-04 warns against
   treating similarity as contradiction).

8. **Advisory locks are wall-clock only.** `acquire_lock` uses `locked_until`
   timestamps with no liveness check; a crashed holder blocks until expiry.

9. **`inject_skills` defaults to `True` on bare `RunnerCapabilities`** and on
   `SERVERLESS`/`LOCAL` tiers, meaning injection was on by default for those
   adapters. (Removed by this extraction — capture this if the new product ever
   re-introduces injection, which it should not inside AgentDB.)

10. **Evolution bridges (`wikiskill`, `skillopt`) are thin wrappers** gated behind
    optional extras; not covered by core retrieval and largely untested against
    live backends.

## Extraction-specific notes

- Skill source and tests were **migrated to the standalone `agentskills`
  package** (`github/agentskills`). Self-contained tests run there; the
  AgentDB-coupled ones are kept as reference-only and do not run against that
  package.
- Proposals already persisted in user `agentdb.sqlite` as `proposal/` memories
  are **not** migrated automatically (they are AgentDB memories). See
  `DATA_MIGRATION.md §4`.
- Skill **embeddings** are model-dependent and not exported as vectors.

## Open decisions (do not guess — maintainer sign-off required)

- **Compatibility/versioning policy:** Is this a breaking major release, or a
  one-release deprecation cycle (warn → export → remove)? This branch removes
  skill APIs outright; the final release strategy is undecided (contract §8,
  DECISIONS A1).
- **Read-authorization scope:** unrelated to skills but tracked; not addressed here.
