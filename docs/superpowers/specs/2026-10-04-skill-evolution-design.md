# Skill Evolution Integration Design

**Goal:** Integrate WikiSkill (online, experience-driven skill creation) and SkillOpt (offline, trajectory-optimized skill improvement) as first-class skill evolution backends for agentdb, with agentdb's `SkillStore` as the single governed source of truth.

**Architecture:** Two thin adapters bridge the external systems into agentdb's existing skill API. WikiSkill's `SkillsLayer` is replaced by an agentdb-backed adapter so all accepted proposals flow through governed versioning and conflict detection. SkillOpt runs as a periodic offline optimizer and writes `best_skill.md` results back into agentdb via a post-training bridge. A `SkillEvolutionManager` coordinates both, prevents concurrent mutation via an optimistic lock, and exposes a unified event hook for downstream consumers.

**Tech Stack:** Python 3.11+, existing agentdb `SkillStore` + `SkillsAPI`, WikiSkill (`kenhuangus/wikiskill` — `src/wikiskill/`), SkillOpt (`microsoft/SkillOpt` — `skillopt/`). Both external packages installed as optional extras.

---

## Global Constraints

- All changes to the `Skill` dataclass and `SkillStore` schema are **additive and backward-compatible** — no column removals, no breaking API changes
- SQLite schema changes use `ALTER TABLE ... ADD COLUMN` with defaults; no data migrations required
- WikiSkill and SkillOpt are optional dependencies; all imports are guarded so agentdb without either package still works identically
- `validation_score` and `locked_until` are nullable — existing skills that predate this feature have `NULL` in both columns
- The `origin` field uses string literals: `"human"` (default), `"wikiskill"`, `"skillopt"` — no enum, matches agentdb's existing `Origin` style
- Adapters never modify WikiSkill's wiki layer (patterns, logs) — they only replace `SkillsLayer`
- SkillOptAdapter only writes to agentdb when `GateResult.action == "accept_new_best"` — no writes on `"accept"` or `"reject"`
- `locked_until` is advisory (optimistic lock): the adapter checks it before writing but does not hold a DB-level lock

---

## Review Focus

1. **Concurrent mutation window** — WikiSkill runs online per-iteration; SkillOpt runs offline for multiple epochs. If both target the same skill, the optimistic lock on `locked_until` must be checked atomically with the write; a TOCTOU gap between check and write could let both land.
2. **Health score formula with missing validation_score** — existing skills (and new skills created without a gate) have `validation_score = NULL`; the blended formula must fall back to usage-only scoring without dividing by zero or penalizing the skill.
3. **WikiSkill snapshot/restore against agentdb versioning** — WikiSkill's `SkillsLayer.restore()` rolls back skill content on gate rejection; the agentdb adapter must archive the candidate version cleanly rather than hard-deleting it, so the audit trail is preserved.
4. **SkillOptAdapter idempotency** — SkillOpt can be re-run from a checkpoint (`runtime_state.json`); if the same `best_step` is pushed twice to agentdb, `update_skill_version()` must not create a duplicate version.
5. **`metadata` JSON column injection** — `provenance` is a free-form dict stored as JSON in a new `metadata` TEXT column; deserialisation must not fail on NULL or malformed JSON from older rows.

---

## Data Model Changes

### `Skill` dataclass (`src/agentdb/core/models.py`)

Add three fields (all optional, default-safe):

```python
origin: str = "human"                   # "human" | "wikiskill" | "skillopt"
validation_score: float | None = None   # gate score from WikiSkill / SkillOpt
metadata: dict = field(default_factory=dict)  # provenance, training config, iteration
```

### `SkillStore` schema (`src/agentdb/skills/skill_store.py`)

Add three columns to the `skills` table via `ALTER TABLE ... ADD COLUMN` on open:

```sql
ALTER TABLE skills ADD COLUMN origin TEXT NOT NULL DEFAULT 'human';
ALTER TABLE skills ADD COLUMN validation_score REAL;
ALTER TABLE skills ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}';
```

Add one column to support the optimistic lock (on the skill ID row, not the version row — one lock per skill, regardless of version):

```sql
ALTER TABLE skills ADD COLUMN locked_until TEXT;  -- ISO datetime or NULL
```

### Updated `create_skill()` and `update_skill_version()` signatures

```python
def create_skill(
    self,
    skill_id: str,
    name: str,
    content: str,
    scope: list[str],
    origin: str = "human",
    validation_score: float | None = None,
    metadata: dict | None = None,
) -> Skill: ...

def update_skill_version(
    self,
    skill_id: str,
    new_content: str,
    new_version: str | None = None,
    origin: str = "human",
    validation_score: float | None = None,
    metadata: dict | None = None,
) -> Skill: ...
```

Both existing callers pass no new kwargs — backward compatible.

### Health score formula

Current (usage-only):

```python
health = recency_score * retrieval_weight + confidence * confidence_weight
```

Updated (blended when `validation_score` is available):

```python
ALPHA = 0.4  # weight of validation score vs usage score

if validation_score is not None:
    health = ALPHA * validation_score + (1 - ALPHA) * usage_score
else:
    health = usage_score  # unchanged for skills without a gate score
```

`ALPHA` is hardcoded; not exposed in config (YAGNI — can be promoted later).

---

## New Components

### `src/agentdb/skills/evolution/wikiskill_adapter.py`

Replaces WikiSkill's `SkillsLayer`. Passed to `EvolutionOrchestrator` in place of the filesystem-backed layer.

```python
class WikiSkillAdapter:
    def __init__(self, db: "AgentDB", agent_id: str) -> None: ...

    # WikiSkill SkillsLayer protocol
    def list_skills(self) -> list[str]: ...
    def skill_content(self, name: str) -> str | None: ...
    def purpose(self, name: str) -> str | None: ...
    def active_skills_context(self) -> str: ...

    def apply_proposal(self, proposal: dict) -> dict:
        """Called by EvolutionOrchestrator after gate accepts a proposal."""
        # action == "create"  → db.skills_api.create_skill(origin="wikiskill", ...)
        # action == "patch"   → apply edits to current content, then update_skill_version()
        # Returns {skill, action, diff}
        ...

    def snapshot(self) -> dict[str, dict]:
        """Called before each iteration; returns {name: {content, version}} for rollback."""
        ...

    def restore(self, snap: dict[str, dict]) -> None:
        """Called on gate rejection; archives candidate version, restores prior."""
        ...
```

**Key invariant:** `restore()` calls `archive_skill()` on the rejected candidate version rather than deleting it. The audit trail is preserved.

### `src/agentdb/skills/evolution/skillopt_adapter.py`

Post-training bridge. Reads SkillOpt output directory and writes accepted results into agentdb.

```python
class SkillOptAdapter:
    def __init__(self, db: "AgentDB", agent_id: str) -> None: ...

    def push_result(
        self,
        skill_id: str,
        out_root: str | Path,
    ) -> Skill | None:
        """
        Reads best_skill.md and runtime_state.json from out_root.
        Writes to agentdb only when GateResult action == "accept_new_best".
        Idempotent: if best_step version already exists in SkillStore, no-op.
        Returns the new Skill version or None if no write occurred.
        """
        ...

    def run_and_push(
        self,
        skill_id: str,
        config: dict,
        out_root: str | Path,
    ) -> Skill | None:
        """
        Reads current skill content from agentdb, writes it as skill_init,
        invokes SkillOpt training via subprocess, then calls push_result().
        Acquires optimistic lock on skill_id for duration of training.
        """
        ...
```

**Key invariant:** Version string is `f"skillopt_step_{best_step}"` — deterministic, so duplicate pushes from the same run are rejected by the `update_skill_version()` uniqueness check.

### `src/agentdb/skills/evolution/manager.py`

Thin coordinator. Not required for either adapter to work standalone — useful when running both against the same agentdb instance.

```python
class SkillEvolutionManager:
    def __init__(self, db: "AgentDB", agent_id: str) -> None: ...

    def acquire_lock(self, skill_id: str, duration_seconds: int = 3600) -> bool:
        """Sets locked_until on skill row. Returns False if already locked."""
        ...

    def release_lock(self, skill_id: str) -> None: ...

    def is_locked(self, skill_id: str) -> bool: ...

    def wikiskill_adapter(self) -> "WikiSkillAdapter": ...
    def skillopt_adapter(self) -> "SkillOptAdapter": ...
```

### `src/agentdb/skills/evolution/__init__.py`

```python
from .wikiskill_adapter import WikiSkillAdapter
from .skillopt_adapter import SkillOptAdapter
from .manager import SkillEvolutionManager

__all__ = ["WikiSkillAdapter", "SkillOptAdapter", "SkillEvolutionManager"]
```

---

## Optional Extras in `pyproject.toml`

```toml
[project.optional-dependencies]
wikiskill = ["wikiskill>=0.1.0"]
skillopt  = ["skillopt>=0.1.0"]
evolution = ["wikiskill>=0.1.0", "skillopt>=0.1.0"]
```

Install:
```bash
pip install "agentdatabase[wikiskill]"
pip install "agentdatabase[skillopt]"
pip install "agentdatabase[evolution]"  # both
```

---

## Usage Example

```python
from agentdb import AgentDB
from agentdb.skills.evolution import WikiSkillAdapter, SkillOptAdapter, SkillEvolutionManager

db = AgentDB(path="db.sqlite", embed_fn=my_embed)
mgr = SkillEvolutionManager(db, agent_id="my-agent")

# Online evolution (WikiSkill)
from wikiskill.orchestrator import EvolutionOrchestrator
adapter = mgr.wikiskill_adapter()
orchestrator = EvolutionOrchestrator(llm=llm, dataset=dataset, ws=ws, tools=tools,
                                      skills=adapter)
orchestrator.run()

# Offline optimization (SkillOpt) — locks skill during training
skill = mgr.skillopt_adapter().run_and_push(
    skill_id="my-skill",
    config={"model": {"optimizer_model": "claude-opus-4-8"}, ...},
    out_root="./skillopt-runs/my-skill",
)
```
