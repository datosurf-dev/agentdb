# Skill Extraction — Execution Plan (Phase 1)

**Branch:** `feat/skill-extraction-phase1`
**Baseline commit:** `fe871c449c76025b81b4bd74561714b4d4886bf0`
**Baseline tests:** `547 passed, 4 skipped` (`python -m pytest -q`)

## Scope

Extract skill management out of AgentDB and remove skill injection, per
`PRODUCT_CONTRACT.md` P-10 and `TECHNICAL_SPEC.md` ADB-01/ADB-02. This is a
controlled extraction + cleanup; **no** retrieval/consolidation redesign, **no**
new Skill Governance package, **no** cross-package integration.

## Source audit (verified, not assumed)

### Dedicated skill modules (whole package)
- `src/agentdb/skills/skill_store.py` — `SkillStore`, schema `skills`, `skill_locks`
- `src/agentdb/skills/evolution/{manager,skillopt_adapter,wikiskill_adapter}.py`
- `src/agentdb/skills/__init__.py`, `skills/evolution/__init__.py`

### Skill coupling points in shared modules
| File | Coupling |
|---|---|
| `core/models.py` | `Skill` dataclass |
| `embeddings/text.py` | `_embed_text_for_skill`, imports `Skill` |
| `interface/client.py` | imports `Skill`/`SkillStore`; `SkillsAPI` class; `self.skills`; `skill_stale` dispatch; `untracked_skill_candidate` subscribe+handler; `_check_skill_conflict` + `_skill_words` helper; skill-conflict call in `write()`; `skills_db_path` for `SystemStats`; `LifecycleManager(skill_store=...)` |
| `interface/__init__.py` | exports `SkillsAPI` |
| `__init__.py` | exports `SkillConflict` |
| `core/__init__.py` | exports `Skill` |
| `core/system.py` | `skills_db_path` param; `total_skills`/`stale_skill_count` stats |
| `governance/lifecycle_manager.py` | `skill_store` param; skill prune in sweep; skill-embed word sets + `untracked_skill_candidate` dispatch in `consolidate()`; `skill_candidates` result field |
| `gateway/__init__.py` | `propose/approve/reject/list_proposals`, `auto_propose_from_cluster` |
| `notifications/dispatcher.py` | `skill_stale`/`skill_review_needed`/`untracked_skill_candidate` default events + format |
| `adapters/capabilities.py` | `inject_skills` field |
| `adapters/middleware.py` | `_build_skills_block`; skill block in `prepare_turn_async` |
| `adapters/hermes.py` | `system_prompt_block` injects skills |
| `testing.py` | `propose_skill` fake |
| `interface/mcp_server.py` | none (verify) |
| `governance/permission_router.py` | only docstring examples use `"skills/"` (generic prefix, keep) |

### Tests
Skill-dedicated (archive): `test_skill_store`, `test_skills_api`, `test_client_skills`,
`test_skill_conflict_signal`, `test_skill_draft_reconciliation`,
`test_skill_evolution_data_model`, `test_skill_evolution_lock`,
`test_skill_evolution_package`, `test_skillopt_adapter`, `test_wikiskill_adapter`.
Mixed (edit to drop skill assertions): `test_capabilities`, `test_middleware_injection`,
`test_notifications`, `test_gateway`, `test_backend`, `test_system_stats`,
`test_memory_consolidation`, `test_models`, `test_testing_fakes`,
`test_claude_agent_sdk_adapter`, `test_injection_consolidation_flow`.

## Stages (each validated with `pytest -q`)

1. **Preserve (Phase 2):** archive skill source + skill tests into
   `migration-handoff/skill-extraction/` with inventory, checksums, data-migration
   and known-issues docs. No behavior change yet — tests still green.
2. **Capabilities + adapters (ADB-01, P0):** remove `inject_skills`,
   `_build_skills_block`, skill block in async prepare, Hermes `system_prompt_block`
   skill body. Update adapter tests to assert **no** skill text.
3. **Gateway + notifications + system + lifecycle:** remove proposal workflow,
   skill notifications, skill stats, skill pruning/skill-candidate dispatch.
4. **Client core:** remove `SkillsAPI`, `self.skills`, `skill_stale`, skill-conflict
   signal, `skills_db_path`, `skill_store` wiring.
5. **Models/exports/embeddings:** remove `Skill`, `_embed_text_for_skill`, exports.
6. **Delete skill package** + skill-dedicated tests (after archival).
7. **Docs:** README, AGENTS.md (stale vision ref), changelog, `docs/context/*`.
8. **Full validation:** sync+async adapters, migration/backwards-compat, imports.

## Compatibility policy

Skill injection removed immediately (no hidden flag). Skill-management APIs are
**removed** on this feature branch with an export/handoff path; final version
bump/deprecation release to be decided by maintainer (flagged in KNOWN_ISSUES).
No skill records converted to memories. Existing skill data preserved via
handoff export.

## Out of scope (do not touch)

Retrieval candidate generation, ranker weights, consolidation algorithm
beyond removing the skill-candidate dispatch, read-authorization behavior,
`:memory:` constructor fix.
