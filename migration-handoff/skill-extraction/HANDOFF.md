# HANDOFF — Skill Extraction Phase 1

**Branch:** `feat/skill-extraction-phase1`
**Baseline commit:** `fe871c4` (before extraction)
**Code commit:** `c3e251f` (skill removal from AgentDB core)
**Scope:** Extract skill management from AgentDB and clean up the core. No new
Skill Governance implementation and no cross-package integration were built.

## What this package contains

| File | Purpose |
|---|---|
| `EXECUTION_PLAN.md` | Audit-based plan, stages, guardrails |
| `SOURCE_INVENTORY.md` | Every skill module/class/API/persistence point at baseline |
| `ARCHITECTURE.md` | How the skill subsystem was structured inside AgentDB |
| `DATA_MIGRATION.md` | Where skill data lives, export + restore procedures |
| `KNOWN_ISSUES.md` | Skill-subsystem limitations + extraction notes + open decisions |
| `TEST_RESULTS.md` | Baseline vs post-extraction tests, requirement checks |
| `export_skills.py` | Standalone exporter (no `agentdb` import) → JSONL |
| `CHECKSUMS.sha256` | SHA-256 of every archived file |
| `archive/src/...` | Original skill source (paths preserved) |
| `archive/tests/` | Original skill-dedicated tests |
| `archive/shared/` | Reference snapshots of shared files (skill + memory interleaved) |

## Removed functionality (AgentDB core)

- `src/agentdb/skills/` package (SkillStore, evolution manager + WikiSkill/SkillOpt adapters).
- Client: `self.skills`, `self.skills_api` (`SkillsAPI`), `_check_skill_conflict`,
  `_skill_words`, `_on_untracked_skill_candidate`, `skill_stale` dispatch,
  `untracked_skill_candidate` subscription, `skill_store` wiring.
- Gateway: `propose/approve/reject/list_proposals`, `auto_propose_from_cluster`.
- Adapters: `RunnerCapabilities.inject_skills`, `_build_skills_block`,
  skill block in async `prepare_turn`, Hermes skill body in `system_prompt_block`.
- Notifications: `skill_stale` / `skill_review_needed` / `untracked_skill_candidate`.
- System stats: `total_skills`, `stale_skill_count`, `skills_db_path`.
- Lifecycle: `skill_store` param, skill-embedding prune, skill-candidate dispatch in `consolidate()`.
- Models/embeddings/exports: `Skill`, `_embed_text_for_skill`, `SkillConflict`,
  `Skill`/`SkillsAPI` exports.
- Deps: `wikiskill` / `skillopt` / `evolution` extras.

## Preserved (skill side, in this package)

Full skill source, tests, schema definition, public-API behavior inventory,
AgentDB-internal coupling notes, and a working data exporter — enough to
rebuild the skill feature in a future independent package without the current
tree. No new architecture was designed.

## Public API changes & compatibility

- **Breaking** for anyone using `db.skills`, `db.skills_api`, gateway proposals,
  notifications on skill events, `agentdb_stats()` skill keys, or the `evolution` extra.
- **Skill injection removed immediately** (P0). No flag re-enables it.
- Version strategy (one-release deprecation vs. immediate breaking minor) is a
  maintainer decision — recommended MINOR bump `0.4.1 → 0.5.0` per pre-1.0 policy.
  See `KNOWN_ISSUES.md` and `CHANGELOG.md`.

## Data safety

- `skills.sqlite` is never dropped or truncated by this change.
- Skill records are never converted to memories.
- Export: `python export_skills.py <skills.sqlite> -o skills_export.jsonl`.
- Restore: see `DATA_MIGRATION.md §4`.

## Remaining risks / open decisions

- Compatibility/version policy (A1) unsettled — flagged, not guessed.
- Skill embeddings are model-dependent and not exported as vectors.
- Existing `proposal/` memories in `agentdb.sqlite` are not auto-migrated (they
  are AgentDB memories, not skills).
- `consolidate()` keeps unused `gateway`/`skill_candidate_threshold` params for
  API stability (documented).

## Verification

`python -m pytest -q` → **428 passed, 4 skipped** (baseline was 547 passed, 4
skipped; the delta is removed skill tests). See `TEST_RESULTS.md`.
