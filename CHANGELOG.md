# Changelog

All notable changes to AgentDB are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow the pre-1.0
policy in `RELEASING.md`.

## [Unreleased]

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
  - Removed the `src/agentdb/skills/` package (archived under
    `migration-handoff/skill-extraction/`).
- Skill data is **not deleted**: existing `skills.sqlite` files are left intact.
  Use `migration-handoff/skill-extraction/export_skills.py` to export and
  `DATA_MIGRATION.md` to restore into a future skill package. Skill records are
  never converted to memories.

### Unchanged
- Memory ranking, retrieval, consolidation (minus skill-candidate dispatch),
  provenance/trust, permissions, audit and lifecycle are preserved. `428 passed,
  4 skipped` at extraction.

### Notes
- Recommended version bump for this change: **MINOR** (`0.4.1` → `0.5.0`) per the
  pre-1.0 "breaking changes bump MINOR" policy. Left to the release manager.
- Deferred / out of scope: no Skill Governance implementation or AgentDB↔skill
  integration contract was added.
