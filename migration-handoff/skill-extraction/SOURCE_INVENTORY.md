# SOURCE_INVENTORY — Skill Functionality in AgentDB

**Extracted from commit:** `fe871c449c76025b81b4bd74561714b4d4886bf0`
**Archived:** see `archive/` (paths preserved under `archive/src/`, `archive/tests/`, `archive/shared/`)
**Checksums:** `CHECKSUMS.sha256`

This inventory lists every skill-specific module, class, API and persistence
mechanism that existed in AgentDB at the baseline commit, so the future
independent Skill-Governance package can reconstruct behavior.

## 1. Dedicated skill source modules

| File (original path) | Contents |
|---|---|
| `src/agentdb/skills/__init__.py` | exports `SkillStore`, `SkillStoreError` |
| `src/agentdb/skills/skill_store.py` | `SkillStore`, `SkillStoreError`, `SkillConflict`, `SkillLockConflict`; schema creation + migrations; health computation; advisory locks; search |
| `src/agentdb/skills/evolution/__init__.py` | exports `WikiSkillAdapter`, `SkillOptAdapter`, `SkillEvolutionManager` |
| `src/agentdb/skills/evolution/manager.py` | `SkillEvolutionManager` — lock acquire/release, adapter factories |
| `src/agentdb/skills/evolution/wikiskill_adapter.py` | `WikiSkillAdapter` — WikiSkill bridge |
| `src/agentdb/skills/evolution/skillopt_adapter.py` | `SkillOptAdapter` — SkillOpt bridge |

## 2. Skill data model

`src/agentdb/core/models.py` → `@dataclass Skill`:
`id, name, content, authored_by, scope, version, archived, health_score,
health_score_reasons, created_at, updated_at, last_retrieved, parent_skill_id,
origin, validation_score, metadata`.

## 3. Persistence / storage schema

Skill storage uses a **separate SQLite file** (`<db_dir>/skills.sqlite`) unless the
DB path is `:memory:` (then same in-memory DB). Defined in `skill_store.py`:

```sql
CREATE TABLE skills (
  id TEXT NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL,
  authored_by TEXT NOT NULL, scope TEXT NOT NULL, version TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0, health_score REAL NOT NULL DEFAULT 1.0,
  health_score_reasons TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_retrieved TEXT,
  parent_skill_id TEXT,
  origin TEXT NOT NULL DEFAULT 'human',            -- migration-added
  validation_score REAL,                            -- migration-added
  metadata TEXT,                                    -- migration-added
  PRIMARY KEY (id, version)
);
CREATE TABLE skill_locks (skill_id TEXT PRIMARY KEY, locked_until TEXT NOT NULL);
```

Migration statements (`_MIGRATION_STMTS`) add `origin`, `validation_score`,
`metadata` columns to pre-existing `skills` tables (idempotent, swallow
"column already exists").

Skill embeddings (when embeddings enabled) are stored in the shared
`embeddings.db` under the `"skills"` namespace with key `"{id}:{version}"`.

## 4. Public APIs / behavior to reproduce

### `SkillStore` (direct)
- `create(skill)` — rejects non-human `authored_by`
- `get(skill_id)` — newest non-archived version
- `update(skill_id, content, new_version, origin, validation_score, metadata)` — versioning, `SkillConflict` on >1 active version
- `get_for_agent(agent_id)` — scope-filtered, recomputes health, fires `on_stale_crossing` once at <0.6
- `archive(skill_id, version)`
- `list_all_active_ids()` → `"id:version"`
- `list_stale(agent_id, days=30)`
- `search(query, agent_id=None)` — case-insensitive name/content
- `has_version`, `acquire_lock`, `release_lock`, `is_locked`

### `SkillsAPI` (facade, was in `interface/client.py`)
- `create_skill`, `update_skill_version`, `get_skill`, `archive_skill`,
  `list_stale_skills_for_agent`, `list_skill_warnings_for_agent`
- Embeds skills into the `"skills"` vector namespace on create/update; deletes on archive.

### Gateway skill proposals (`gateway/__init__.py`)
- `propose(name, content, scope, agent_id, ...)` → writes `proposal/<uuid>` memory record
- `approve(proposal_id)` → `skills_api.create_skill` + archives proposal record
- `reject(proposal_id, reason)`
- `list_proposals(status="pending"|"approved"|"rejected")`
- `auto_propose_from_cluster(dominant_words, sample_keys, agent_id)` — dedups on domain words

### Client integration (`interface/client.py`)
- `self.skills` (`SkillStore`), `self.skills_api` (`SkillsAPI`)
- `skill_stale` gateway event dispatched on staleness crossing
- `untracked_skill_candidate` event → `auto_propose_from_cluster`
- `_check_skill_conflict(record)` — keyword + semantic overlap between a written
  memory and scoped skills → dispatches `skill_review_needed`
- `_skill_words()` helper (keyword stoplist)

### Injection paths (REMOVED — must not be reproduced in AgentDB)
- `adapters/capabilities.py`: `RunnerCapabilities.inject_skills`
- `adapters/middleware.py`: `_build_skills_block`, async `prepare_turn_async` skill block (40% of injection budget)
- `adapters/hermes.py`: `system_prompt_block()` → "## Governed Skills" with staleness warnings

### Notifications (`notifications/dispatcher.py`)
Default events included `skill_stale`, `skill_review_needed`,
`untracked_skill_candidate` with formatters.

### System stats (`core/system.py`)
`total_skills`, `stale_skill_count` (counted from the skills DB).

### Lifecycle (`governance/lifecycle_manager.py`)
- `skill_store` param; skills-namespace embedding prune in `run_sweep`
- `consolidate()` builds skill word-sets to detect ungoverned clusters, dispatches
  `untracked_skill_candidate`, increments `skill_candidates`

## 5. Tests

Dedicated skill tests archived under `archive/tests/`:
`test_skill_store`, `test_skills_api`, `test_client_skills`,
`test_skill_conflict_signal`, `test_skill_draft_reconciliation`,
`test_skill_evolution_data_model`, `test_skill_evolution_lock`,
`test_skill_evolution_package`, `test_skillopt_adapter`, `test_wikiskill_adapter`.

Shared files that referenced skills are archived under `archive/shared/` for
restoration reference: `core_models.py`, `embeddings_text.py`, `gateway_init.py`,
`notifications_dispatcher.py`, `adapters_capabilities.py`, `adapters_middleware.py`,
`adapters_hermes.py`, `core_system.py`, `governance_lifecycle_manager.py`, `testing.py`.

## 6. Optional dependencies (from pyproject)
`wikiskill = ["wikiskill>=0.1.0"]`, `skillopt = ["skillopt>=0.1.0"]`,
`evolution = ["wikiskill>=0.1.0", "skillopt>=0.1.0"]` — evolution bridges only.
