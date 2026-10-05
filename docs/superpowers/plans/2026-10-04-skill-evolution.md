# Skill Evolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate WikiSkill (online, experience-driven) and SkillOpt (offline, trajectory-optimized) as first-class evolution backends that write into agentdb's `SkillStore` as the single governed source of truth.

**Architecture:** Three additive layers: (1) data model changes to `Skill` + `SkillStore` that are fully backward-compatible; (2) two thin adapters (`WikiSkillAdapter`, `SkillOptAdapter`) that bridge the external systems into agentdb's existing skill API; (3) a `SkillEvolutionManager` that coordinates both adapters and manages an optimistic lock to prevent concurrent mutation. All external-package imports are guarded behind `try/except ImportError` so agentdb without either package installed works identically.

**Tech Stack:** Python 3.11+, SQLite (existing), agentdb `SkillStore` + `SkillsAPI`, optional `wikiskill` and `skillopt` packages.

**Spec:** `docs/superpowers/specs/2026-10-04-skill-evolution-design.md`

## Global Constraints

- All changes are **additive and backward-compatible** — no column removals, no breaking API changes to existing callers
- SQLite schema additions use `ALTER TABLE ... ADD COLUMN` with defaults; guard with `try/except sqlite3.OperationalError` (column already exists)
- WikiSkill and SkillOpt are optional; all their imports must be inside `try/except ImportError` blocks
- `validation_score` and `locked_until` are nullable — existing skills have NULL in both
- `origin` uses string literals `"human"` (default), `"wikiskill"`, `"skillopt"` — matches agentdb's existing style (not an enum)
- `authored_by` stays `"human"` for all skills (existing check in `SkillStore.create()` is unchanged)
- SkillOptAdapter only writes to agentdb when `gate_result.action == "accept_new_best"` — no writes on `"accept"` or `"reject"`
- Version string from SkillOpt runs is `f"skillopt_step_{best_step}"` — deterministic, enabling idempotent re-runs
- `locked_until` is advisory: check it before writing, but do not hold a DB-level lock

## Design Deviation from Spec

The spec says to add `locked_until TEXT` as a column on the `skills` table. This plan uses a separate `skill_locks` table (`skill_id TEXT PRIMARY KEY, locked_until TEXT NOT NULL`) instead, because the `skills` table has `PRIMARY KEY (id, version)` — there is no single "skill ID row", making a per-skill lock column awkward. The behavior (advisory optimistic lock, acquire/release/expiry semantics) is identical.

## Review Focus

1. **metadata NULL from old rows** — rows before migration have `metadata = '{}'` (string default); rows written without the column might have NULL; `json.loads(None)` raises `TypeError`. `_row_to_skill()` must `try/except` the JSON parse and fall back to `{}`. Test added to Task 1.
2. **validation_score = 0.0 (not NULL) must blend** — `None` check in `_compute_health` must use `is not None`, not truthiness; `validation_score=0.0` must produce `0.4 * 0.0 + 0.6 * 1.0 = 0.60`, not usage-only. Test added to Task 1.
3. **SkillOptAdapter idempotency on re-run** — `push_result()` called twice with the same `best_step`; second call must return `None` without raising `SkillConflict`. Requires `SkillStore.has_version()` check before the insert. Test added to Task 4.
4. **WikiSkillAdapter.restore() with no changes** — if `restore()` is called with a snapshot that already matches current state (no proposal was applied), it must not archive anything or corrupt state. Test added to Task 3.
5. **Concurrent lock expiry and acquire** — `acquire_lock(duration_seconds=0)` expires immediately; `is_locked()` must return `False` and a subsequent `acquire_lock()` must succeed. Test added to Task 2.

---

## File Map

| File | Action | What it does |
|------|--------|--------------|
| `src/agentdb/core/models.py` | Modify | Add `origin`, `validation_score`, `metadata` to `Skill` dataclass |
| `src/agentdb/skills/skill_store.py` | Modify | Schema migration, `_row_to_skill`, `create`, `update`, `_compute_health`, lock table + `acquire_lock`/`release_lock`/`is_locked`/`has_version` |
| `src/agentdb/interface/client.py` | Modify | `SkillsAPI.create_skill()` and `update_skill_version()` new optional params |
| `src/agentdb/skills/evolution/__init__.py` | Create | Package exports |
| `src/agentdb/skills/evolution/wikiskill_adapter.py` | Create | `WikiSkillAdapter` |
| `src/agentdb/skills/evolution/skillopt_adapter.py` | Create | `SkillOptAdapter` |
| `src/agentdb/skills/evolution/manager.py` | Create | `SkillEvolutionManager` |
| `tests/unit/test_skill_evolution_data_model.py` | Create | Task 1 tests |
| `tests/unit/test_skill_evolution_lock.py` | Create | Task 2 tests |
| `tests/unit/test_wikiskill_adapter.py` | Create | Task 3 tests |
| `tests/unit/test_skillopt_adapter.py` | Create | Task 4 tests |
| `pyproject.toml` | Modify | Add `wikiskill`, `skillopt`, `evolution` optional extras |

---

### Task 1: Data model + SkillStore migration + health blending

**Files:**
- Modify: `src/agentdb/core/models.py:77-91`
- Modify: `src/agentdb/skills/skill_store.py`
- Modify: `src/agentdb/interface/client.py:158-224`
- Test: `tests/unit/test_skill_evolution_data_model.py`

**Interfaces:**
- Consumes: existing `Skill`, `SkillStore`, `SkillsAPI`
- Produces:
  - `Skill` now has `origin: str = "human"`, `validation_score: Optional[float] = None`, `metadata: dict = field(default_factory=dict)`
  - `SkillStore.create(skill)` writes new columns; `SkillStore.update(skill_id, content, new_version, origin="human", validation_score=None, metadata=None)` accepts new kwargs
  - `SkillsAPI.create_skill(skill_id, name, content, scope, origin="human", validation_score=None, metadata=None)`
  - `SkillsAPI.update_skill_version(skill_id, content, new_version, origin="human", validation_score=None, metadata=None)`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_skill_evolution_data_model.py`:

```python
import pytest
from agentdb import AgentDB
from agentdb.core.models import Skill
from agentdb.skills.skill_store import SkillStore


def test_skill_defaults_have_origin_human(tmp_path):
    db = AgentDB(path=tmp_path / "a.sqlite")
    skill = db.skills_api.create_skill("s1", "Skill One", "Do things.", scope=["a1"])
    assert skill.origin == "human"
    assert skill.validation_score is None
    assert skill.metadata == {}


def test_skill_origin_wikiskill_persists_round_trip(tmp_path):
    db = AgentDB(path=tmp_path / "a.sqlite")
    db.skills_api.create_skill(
        "s1", "Skill One", "Do things.", scope=["a1"],
        origin="wikiskill", validation_score=0.88, metadata={"iteration": 3},
    )
    fetched = db.skills_api.get_skill("s1")
    assert fetched.origin == "wikiskill"
    assert fetched.validation_score == pytest.approx(0.88)
    assert fetched.metadata == {"iteration": 3}


def test_update_skill_version_with_origin_skillopt(tmp_path):
    db = AgentDB(path=tmp_path / "a.sqlite")
    db.skills_api.create_skill("s1", "Skill One", "v1 content.", scope=["a1"])
    db.skills_api.update_skill_version(
        "s1", "v2 content.", "2.0.0",
        origin="skillopt", validation_score=0.95, metadata={"best_step": 12},
    )
    fetched = db.skills_api.get_skill("s1")
    assert fetched.origin == "skillopt"
    assert fetched.validation_score == pytest.approx(0.95)
    assert fetched.metadata == {"best_step": 12}


def test_health_blends_validation_score_when_present(tmp_path):
    db = AgentDB(path=tmp_path / "a.sqlite")
    db.skills_api.create_skill(
        "s1", "Skill One", "Do things.", scope=["a1"],
        validation_score=0.5,
    )
    skills = db.skills.get_for_agent("a1")
    # usage_score=1.0 (fresh), validation=0.5 → 0.4*0.5 + 0.6*1.0 = 0.80
    assert skills[0].health_score == pytest.approx(0.80, abs=0.01)
    assert "validation_blended" in skills[0].health_score_reasons


def test_health_unchanged_without_validation_score(tmp_path):
    db = AgentDB(path=tmp_path / "a.sqlite")
    db.skills_api.create_skill("s1", "Skill One", "Do things.", scope=["a1"])
    skills = db.skills.get_for_agent("a1")
    assert skills[0].health_score == pytest.approx(1.0)
    assert "validation_blended" not in skills[0].health_score_reasons


def test_validation_score_zero_is_not_null_and_blends(tmp_path):
    # Review Focus #2: validation_score=0.0 must blend (is not None), not fall through
    db = AgentDB(path=tmp_path / "a.sqlite")
    db.skills_api.create_skill(
        "s1", "S1", "content", scope=["a1"], validation_score=0.0
    )
    skills = db.skills.get_for_agent("a1")
    # 0.4*0.0 + 0.6*1.0 = 0.60, NOT 1.0
    assert skills[0].health_score == pytest.approx(0.60, abs=0.01)
    assert "validation_blended" in skills[0].health_score_reasons


def test_existing_db_reopens_without_error(tmp_path):
    # Review Focus #1 (idempotent migration): opening same DB twice does not raise
    db_path = tmp_path / "skills.sqlite"
    store1 = SkillStore(db_path=db_path)
    store2 = SkillStore(db_path=db_path)
    store2.create(Skill(id="s1", name="S1", content="c", authored_by="human", scope=["a1"]))
    assert store2.get("s1") is not None


def test_metadata_null_in_row_reads_as_empty_dict(tmp_path):
    # Review Focus #1: rows with NULL metadata (pre-migration) must not crash
    import sqlite3
    db_path = tmp_path / "skills.sqlite"
    store = SkillStore(db_path=db_path)
    store.create(Skill(id="s1", name="S1", content="c", authored_by="human", scope=["a1"]))
    # Manually corrupt the metadata column to NULL
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE skills SET metadata = NULL WHERE id = 's1'")
    conn.commit()
    conn.close()
    fetched = store.get("s1")
    assert fetched.metadata == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jm-nx/documents/github/agentdb
pytest tests/unit/test_skill_evolution_data_model.py -v 2>&1 | head -40
```

Expected: `AttributeError: 'Skill' object has no attribute 'origin'` or `TypeError`.

- [ ] **Step 3: Add `origin`, `validation_score`, `metadata` to `Skill` dataclass**

In `src/agentdb/core/models.py`, after `parent_skill_id`:

```python
@dataclass
class Skill:
    id: str
    name: str
    content: str
    authored_by: str
    scope: list
    version: str = "1.0.0"
    archived: bool = False
    health_score: float = 1.0
    health_score_reasons: list = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    last_retrieved: Optional[datetime] = None
    parent_skill_id: Optional[str] = None
    origin: str = "human"
    validation_score: Optional[float] = None
    metadata: dict = field(default_factory=dict)
```

- [ ] **Step 4: Add migration constants and update `SkillStore.__init__`**

In `src/agentdb/skills/skill_store.py`, after the `_CREATE_SKILLS_TABLE` constant, add:

```python
_CREATE_SKILL_LOCKS_TABLE = """
CREATE TABLE IF NOT EXISTS skill_locks (
    skill_id TEXT PRIMARY KEY,
    locked_until TEXT NOT NULL
)
"""

_MIGRATION_STMTS = [
    "ALTER TABLE skills ADD COLUMN origin TEXT NOT NULL DEFAULT 'human'",
    "ALTER TABLE skills ADD COLUMN validation_score REAL",
    "ALTER TABLE skills ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'",
]
```

In `SkillStore.__init__`, replace the single `conn.execute(_CREATE_SKILLS_TABLE)` call with:

```python
with self._connect() as conn:
    conn.execute(_CREATE_SKILLS_TABLE)
    conn.execute(_CREATE_SKILL_LOCKS_TABLE)
    for stmt in _MIGRATION_STMTS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
```

- [ ] **Step 5: Update `_row_to_skill()` to read new fields**

Replace the existing `_row_to_skill` function:

```python
def _row_to_skill(row: sqlite3.Row) -> Skill:
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    return Skill(
        id=row["id"],
        name=row["name"],
        content=row["content"],
        authored_by=row["authored_by"],
        scope=json.loads(row["scope"]),
        version=row["version"],
        archived=bool(row["archived"]),
        health_score=row["health_score"],
        health_score_reasons=json.loads(row["health_score_reasons"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_retrieved=datetime.fromisoformat(row["last_retrieved"]) if row["last_retrieved"] else None,
        parent_skill_id=row["parent_skill_id"],
        origin=row["origin"] or "human",
        validation_score=row["validation_score"],
        metadata=metadata,
    )
```

- [ ] **Step 6: Update `_compute_health()` to blend validation_score**

Replace the existing `_compute_health` function:

```python
_ALPHA = 0.4


def _compute_health(skill: Skill) -> tuple[float, list[str]]:
    now = _utcnow()
    reference = skill.last_retrieved or skill.created_at
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_days = (now - reference).days
    usage_score = 1.0
    reasons: list[str] = []
    if age_days > 30:
        usage_score = max(0.1, 1.0 - (age_days - 30) * 0.03)
        reasons.append("staleness")
    if skill.validation_score is not None:
        health = _ALPHA * skill.validation_score + (1 - _ALPHA) * usage_score
        reasons.append("validation_blended")
    else:
        health = usage_score
    return health, reasons
```

- [ ] **Step 7: Update `SkillStore.create()` to write new columns**

In `SkillStore.create()`, replace the `INSERT INTO skills` statement to include the new columns:

```python
def create(self, skill: Skill) -> Skill:
    if skill.authored_by != "human":
        raise SkillStoreError(
            f"Skills may only be authored by humans, got '{skill.authored_by}'"
        )
    with self._connect() as conn:
        conn.execute(
            """
            INSERT INTO skills
                (id, name, content, authored_by, scope, version, archived,
                 health_score, health_score_reasons, created_at, updated_at,
                 last_retrieved, parent_skill_id,
                 origin, validation_score, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill.id,
                skill.name,
                skill.content,
                skill.authored_by,
                json.dumps(skill.scope),
                skill.version,
                int(skill.archived),
                skill.health_score,
                json.dumps(skill.health_score_reasons),
                skill.created_at.isoformat(),
                skill.updated_at.isoformat(),
                skill.last_retrieved.isoformat() if skill.last_retrieved else None,
                skill.parent_skill_id,
                skill.origin,
                skill.validation_score,
                json.dumps(skill.metadata),
            ),
        )
    return skill
```

- [ ] **Step 8: Update `SkillStore.update()` to accept and write new kwargs**

Replace the existing `update()` signature and the new skill creation inside it:

```python
def update(
    self,
    skill_id: str,
    content: str,
    new_version: str,
    origin: str = "human",
    validation_score: Optional[float] = None,
    metadata: Optional[dict] = None,
) -> Skill:
    original = self.get(skill_id)
    if original is None:
        raise SkillStoreError(f"Skill '{skill_id}' not found")
    now = _utcnow()
    new_skill = Skill(
        id=skill_id,
        name=original.name,
        content=content,
        authored_by=original.authored_by,
        scope=original.scope,
        version=new_version,
        archived=False,
        health_score=1.0,
        health_score_reasons=[],
        created_at=now,
        updated_at=now,
        last_retrieved=None,
        parent_skill_id=skill_id,
        origin=origin,
        validation_score=validation_score,
        metadata=metadata or {},
    )
    with self._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM skills WHERE id = ? AND archived = 0",
            (skill_id,),
        ).fetchone()[0]
        if count > 1:
            raise SkillConflict(
                f"Skill '{skill_id}' has unresolved active versions — "
                f"archive or reconcile existing versions before updating"
            )
        conn.execute(
            """
            INSERT INTO skills
                (id, name, content, authored_by, scope, version, archived,
                 health_score, health_score_reasons, created_at, updated_at,
                 last_retrieved, parent_skill_id,
                 origin, validation_score, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_skill.id,
                new_skill.name,
                new_skill.content,
                new_skill.authored_by,
                json.dumps(new_skill.scope),
                new_skill.version,
                int(new_skill.archived),
                new_skill.health_score,
                json.dumps(new_skill.health_score_reasons),
                new_skill.created_at.isoformat(),
                new_skill.updated_at.isoformat(),
                None,
                new_skill.parent_skill_id,
                new_skill.origin,
                new_skill.validation_score,
                json.dumps(new_skill.metadata),
            ),
        )
    return new_skill
```

- [ ] **Step 9: Update `SkillsAPI.create_skill()` and `update_skill_version()` signatures**

In `src/agentdb/interface/client.py`, replace `SkillsAPI.create_skill()`:

```python
def create_skill(
    self,
    skill_id: str,
    name: str,
    content: str,
    scope: list[str],
    origin: str = "human",
    validation_score: Optional[float] = None,
    metadata: Optional[dict] = None,
) -> Skill:
    skill = Skill(
        id=skill_id,
        name=name,
        content=content,
        authored_by="human",
        scope=scope,
        origin=origin,
        validation_score=validation_score,
        metadata=metadata or {},
    )
    created = self._store.create(skill)
    self._maybe_embed_skill(created)
    return created
```

Replace `SkillsAPI.update_skill_version()`:

```python
def update_skill_version(
    self,
    skill_id: str,
    content: str,
    new_version: str,
    origin: str = "human",
    validation_score: Optional[float] = None,
    metadata: Optional[dict] = None,
) -> Skill:
    updated = self._store.update(
        skill_id, content, new_version,
        origin=origin,
        validation_score=validation_score,
        metadata=metadata,
    )
    self._maybe_embed_skill(updated)
    return updated
```

- [ ] **Step 10: Run the tests and verify they pass**

```bash
cd /Users/jm-nx/documents/github/agentdb
pytest tests/unit/test_skill_evolution_data_model.py -v
```

Expected: 8 tests pass.

- [ ] **Step 11: Run the full unit suite to verify no regressions**

```bash
pytest tests/unit/ -q --tb=short
```

Expected: all existing tests still pass (count goes up by 8).

- [ ] **Step 12: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb
git add src/agentdb/core/models.py \
        src/agentdb/skills/skill_store.py \
        src/agentdb/interface/client.py \
        tests/unit/test_skill_evolution_data_model.py
git commit -m "feat: add origin/validation_score/metadata to Skill and blend health score"
```

---

### Task 2: Lock primitives + SkillEvolutionManager

**Files:**
- Modify: `src/agentdb/skills/skill_store.py` (add `has_version`, `acquire_lock`, `release_lock`, `is_locked`)
- Create: `src/agentdb/skills/evolution/__init__.py` (empty for now)
- Create: `src/agentdb/skills/evolution/manager.py`
- Test: `tests/unit/test_skill_evolution_lock.py`

**Interfaces:**
- Consumes: `SkillStore` (with `skill_locks` table from Task 1), `AgentDB`
- Produces:
  - `SkillStore.has_version(skill_id: str, version: str) -> bool`
  - `SkillStore.acquire_lock(skill_id: str, duration_seconds: int = 3600) -> bool`
  - `SkillStore.release_lock(skill_id: str) -> None`
  - `SkillStore.is_locked(skill_id: str) -> bool`
  - `SkillEvolutionManager(db: AgentDB, agent_id: str)`
  - `SkillEvolutionManager.acquire_lock(skill_id, duration_seconds=3600) -> bool`
  - `SkillEvolutionManager.release_lock(skill_id) -> None`
  - `SkillEvolutionManager.is_locked(skill_id) -> bool`
  - `SkillEvolutionManager.wikiskill_adapter() -> WikiSkillAdapter` (returns adapter; WikiSkillAdapter implemented in Task 3)
  - `SkillEvolutionManager.skillopt_adapter() -> SkillOptAdapter` (returns adapter; implemented in Task 4)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_skill_evolution_lock.py`:

```python
import pytest
from agentdb import AgentDB
from agentdb.skills.skill_store import SkillStore


def _store(tmp_path):
    return SkillStore(db_path=tmp_path / "skills.sqlite")


def test_acquire_lock_returns_true_when_free(tmp_path):
    assert _store(tmp_path).acquire_lock("skill-1", duration_seconds=60) is True


def test_acquire_lock_returns_false_when_held(tmp_path):
    store = _store(tmp_path)
    store.acquire_lock("skill-1", duration_seconds=60)
    assert store.acquire_lock("skill-1", duration_seconds=60) is False


def test_release_lock_allows_reacquire(tmp_path):
    store = _store(tmp_path)
    store.acquire_lock("skill-1", duration_seconds=60)
    store.release_lock("skill-1")
    assert store.acquire_lock("skill-1", duration_seconds=60) is True


def test_expired_lock_is_not_locked(tmp_path):
    # Review Focus #5: acquire with duration=0 expires immediately
    store = _store(tmp_path)
    store.acquire_lock("skill-1", duration_seconds=0)
    assert store.is_locked("skill-1") is False


def test_expired_lock_can_be_reacquired(tmp_path):
    store = _store(tmp_path)
    store.acquire_lock("skill-1", duration_seconds=0)
    assert store.acquire_lock("skill-1", duration_seconds=60) is True


def test_is_locked_false_when_no_lock(tmp_path):
    assert _store(tmp_path).is_locked("skill-1") is False


def test_has_version_true_when_version_exists(tmp_path):
    from agentdb.core.models import Skill
    store = _store(tmp_path)
    store.create(Skill(id="s1", name="S1", content="c", authored_by="human", scope=["a1"]))
    assert store.has_version("s1", "1.0.0") is True


def test_has_version_false_when_version_missing(tmp_path):
    from agentdb.core.models import Skill
    store = _store(tmp_path)
    store.create(Skill(id="s1", name="S1", content="c", authored_by="human", scope=["a1"]))
    assert store.has_version("s1", "9.9.9") is False


def test_manager_acquire_and_release(tmp_path):
    from agentdb.skills.evolution.manager import SkillEvolutionManager
    db = AgentDB(path=tmp_path / "a.sqlite")
    mgr = SkillEvolutionManager(db, agent_id="a1")
    assert mgr.acquire_lock("s1") is True
    assert mgr.is_locked("s1") is True
    mgr.release_lock("s1")
    assert mgr.is_locked("s1") is False


def test_manager_double_acquire_returns_false(tmp_path):
    from agentdb.skills.evolution.manager import SkillEvolutionManager
    db = AgentDB(path=tmp_path / "a.sqlite")
    mgr = SkillEvolutionManager(db, agent_id="a1")
    mgr.acquire_lock("s1")
    assert mgr.acquire_lock("s1") is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jm-nx/documents/github/agentdb
pytest tests/unit/test_skill_evolution_lock.py -v 2>&1 | head -20
```

Expected: `AttributeError: 'SkillStore' object has no attribute 'acquire_lock'`.

- [ ] **Step 3: Add `has_version`, `acquire_lock`, `release_lock`, `is_locked` to `SkillStore`**

Add these methods to the `SkillStore` class (anywhere after `list_all_active_ids`):

```python
def has_version(self, skill_id: str, version: str) -> bool:
    with self._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM skills WHERE id = ? AND version = ?",
            (skill_id, version),
        ).fetchone()
    return row is not None

def acquire_lock(self, skill_id: str, duration_seconds: int = 3600) -> bool:
    from datetime import timedelta
    now = _utcnow()
    locked_until = (now + timedelta(seconds=duration_seconds)).isoformat()
    with self._connect() as conn:
        # Remove expired lock if present
        conn.execute(
            "DELETE FROM skill_locks WHERE skill_id = ? AND locked_until <= ?",
            (skill_id, now.isoformat()),
        )
        try:
            conn.execute(
                "INSERT INTO skill_locks (skill_id, locked_until) VALUES (?, ?)",
                (skill_id, locked_until),
            )
            return True
        except sqlite3.IntegrityError:
            return False

def release_lock(self, skill_id: str) -> None:
    with self._connect() as conn:
        conn.execute(
            "DELETE FROM skill_locks WHERE skill_id = ?",
            (skill_id,),
        )

def is_locked(self, skill_id: str) -> bool:
    now = _utcnow()
    with self._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM skill_locks WHERE skill_id = ? AND locked_until > ?",
            (skill_id, now.isoformat()),
        ).fetchone()
    return row is not None
```

- [ ] **Step 4: Create the `evolution` package stub**

```bash
mkdir -p /Users/jm-nx/documents/github/agentdb/src/agentdb/skills/evolution
```

Create `src/agentdb/skills/evolution/__init__.py` (empty for now — will be filled in Task 5):

```python
```

- [ ] **Step 5: Create `SkillEvolutionManager`**

Create `src/agentdb/skills/evolution/manager.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...interface.client import AgentDB


class SkillEvolutionManager:
    def __init__(self, db: "AgentDB", agent_id: str) -> None:
        self._db = db
        self._agent_id = agent_id

    def acquire_lock(self, skill_id: str, duration_seconds: int = 3600) -> bool:
        return self._db.skills.acquire_lock(skill_id, duration_seconds)

    def release_lock(self, skill_id: str) -> None:
        self._db.skills.release_lock(skill_id)

    def is_locked(self, skill_id: str) -> bool:
        return self._db.skills.is_locked(skill_id)

    def wikiskill_adapter(self) -> "WikiSkillAdapter":
        from .wikiskill_adapter import WikiSkillAdapter
        return WikiSkillAdapter(self._db, self._agent_id)

    def skillopt_adapter(self) -> "SkillOptAdapter":
        from .skillopt_adapter import SkillOptAdapter
        return SkillOptAdapter(self._db, self._agent_id)
```

- [ ] **Step 6: Run tests and verify they pass**

```bash
cd /Users/jm-nx/documents/github/agentdb
pytest tests/unit/test_skill_evolution_lock.py -v
```

Expected: 11 tests pass.

- [ ] **Step 7: Run full unit suite**

```bash
pytest tests/unit/ -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb
git add src/agentdb/skills/skill_store.py \
        src/agentdb/skills/evolution/__init__.py \
        src/agentdb/skills/evolution/manager.py \
        tests/unit/test_skill_evolution_lock.py
git commit -m "feat: add lock primitives to SkillStore and SkillEvolutionManager"
```

---

### Task 3: WikiSkillAdapter

**Files:**
- Create: `src/agentdb/skills/evolution/wikiskill_adapter.py`
- Test: `tests/unit/test_wikiskill_adapter.py`

**Interfaces:**
- Consumes: `AgentDB.skills_api` (`create_skill`, `get_skill`, `update_skill_version`, `archive_skill`), `AgentDB.skills.get_for_agent(agent_id)` — all from Task 1+2
- Produces:
  - `WikiSkillAdapter(db: AgentDB, agent_id: str)`
  - `WikiSkillAdapter.list_skills() -> list[str]` — names of non-archived skills in scope
  - `WikiSkillAdapter.skill_content(name: str) -> str | None`
  - `WikiSkillAdapter.purpose(name: str) -> str` — returns skill name as purpose proxy (agentdb has no purpose field); returns `""` if not found
  - `WikiSkillAdapter.active_skills_context() -> str` — formatted string of name + content for all active skills
  - `WikiSkillAdapter.snapshot() -> dict[str, dict]` — `{name: {content, version}}`
  - `WikiSkillAdapter.apply_proposal(proposal: dict) -> dict` — executes create or patch proposal; returns `{skill, action, diff}`
  - `WikiSkillAdapter.restore(snap: dict[str, dict]) -> None` — archives candidates, preserves prior versions

  **`apply_proposal` proposal dict format:**
  - create: `{action: "create", name: str, content: str, scope: list[str], validation_score: float | None}`
  - patch:  `{action: "patch", skill_name: str, new_content: str, new_version: str, validation_score: float | None}`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_wikiskill_adapter.py`:

```python
import pytest
from agentdb import AgentDB
from agentdb.skills.evolution.wikiskill_adapter import WikiSkillAdapter


def _adapter(tmp_path, agent_id="a1"):
    db = AgentDB(path=tmp_path / "a.sqlite")
    return WikiSkillAdapter(db, agent_id=agent_id), db


def test_list_skills_empty_initially(tmp_path):
    adapter, _ = _adapter(tmp_path)
    assert adapter.list_skills() == []


def test_list_skills_returns_names_in_agent_scope(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "How to debug.", scope=["a1"])
    db.skills_api.create_skill("testing", "Testing", "How to test.", scope=["a1"])
    db.skills_api.create_skill("other", "Other", "Not in scope.", scope=["a2"])
    names = adapter.list_skills()
    assert sorted(names) == ["debug", "testing"]


def test_skill_content_returns_content(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "How to debug.", scope=["a1"])
    assert adapter.skill_content("debug") == "How to debug."


def test_skill_content_returns_none_for_missing(tmp_path):
    adapter, _ = _adapter(tmp_path)
    assert adapter.skill_content("nonexistent") is None


def test_purpose_returns_skill_name_as_proxy(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "How to debug.", scope=["a1"])
    assert adapter.purpose("debug") == "Debugging"


def test_purpose_returns_empty_string_for_missing(tmp_path):
    adapter, _ = _adapter(tmp_path)
    assert adapter.purpose("nonexistent") == ""


def test_active_skills_context_contains_name_and_content(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "How to debug.", scope=["a1"])
    ctx = adapter.active_skills_context()
    assert "debug" in ctx
    assert "How to debug." in ctx


def test_snapshot_captures_content_and_version(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "How to debug.", scope=["a1"])
    snap = adapter.snapshot()
    assert "debug" in snap
    assert snap["debug"]["content"] == "How to debug."
    assert snap["debug"]["version"] == "1.0.0"


def test_apply_proposal_create_adds_skill_with_origin_wikiskill(tmp_path):
    adapter, db = _adapter(tmp_path)
    result = adapter.apply_proposal({
        "action": "create",
        "name": "my-skill",
        "content": "Do the thing.",
        "scope": ["a1"],
        "validation_score": 0.91,
    })
    assert result["action"] == "create"
    fetched = db.skills_api.get_skill("my-skill")
    assert fetched is not None
    assert fetched.origin == "wikiskill"
    assert fetched.validation_score == pytest.approx(0.91)


def test_apply_proposal_patch_updates_content(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "Original.", scope=["a1"])
    result = adapter.apply_proposal({
        "action": "patch",
        "skill_name": "debug",
        "new_content": "Updated.",
        "new_version": "1.1.0",
        "validation_score": 0.85,
    })
    assert result["action"] == "patch"
    fetched = db.skills_api.get_skill("debug")
    assert fetched.content == "Updated."
    assert fetched.origin == "wikiskill"
    assert fetched.validation_score == pytest.approx(0.85)


def test_restore_archives_candidate_and_preserves_prior(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "v1 content.", scope=["a1"])
    snap = adapter.snapshot()

    adapter.apply_proposal({
        "action": "patch",
        "skill_name": "debug",
        "new_content": "v2 candidate.",
        "new_version": "1.1.0",
        "validation_score": 0.5,
    })
    adapter.restore(snap)

    fetched = db.skills_api.get_skill("debug")
    assert fetched.content == "v1 content."
    assert fetched.version == "1.0.0"


def test_restore_with_unchanged_snapshot_is_noop(tmp_path):
    # Review Focus #4: restore with snapshot matching current state must not corrupt
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "v1 content.", scope=["a1"])
    snap = adapter.snapshot()
    adapter.restore(snap)  # no proposal was applied
    fetched = db.skills_api.get_skill("debug")
    assert fetched.content == "v1 content."
    assert fetched.version == "1.0.0"


def test_restore_archives_skills_created_during_iteration(tmp_path):
    adapter, db = _adapter(tmp_path)
    snap = adapter.snapshot()  # empty — no skills yet
    adapter.apply_proposal({
        "action": "create",
        "name": "new-skill",
        "content": "Brand new.",
        "scope": ["a1"],
        "validation_score": None,
    })
    adapter.restore(snap)
    # New skill should be archived
    assert adapter.list_skills() == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jm-nx/documents/github/agentdb
pytest tests/unit/test_wikiskill_adapter.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'agentdb.skills.evolution.wikiskill_adapter'`.

- [ ] **Step 3: Create `WikiSkillAdapter`**

Create `src/agentdb/skills/evolution/wikiskill_adapter.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ...interface.client import AgentDB
    from ...core.models import Skill


class WikiSkillAdapter:
    """Implements WikiSkill's SkillsLayer protocol backed by agentdb SkillStore.

    Pass an instance of this class as the `skills` argument to WikiSkill's
    EvolutionOrchestrator in place of the filesystem-backed SkillsLayer.
    """

    def __init__(self, db: "AgentDB", agent_id: str) -> None:
        self._db = db
        self._agent_id = agent_id

    def list_skills(self) -> list[str]:
        """Return names of all non-archived skills in this agent's scope."""
        skills = self._db.skills.get_for_agent(self._agent_id)
        return [s.id for s in skills]

    def skill_content(self, name: str) -> Optional[str]:
        skill = self._db.skills_api.get_skill(name)
        return skill.content if skill is not None else None

    def purpose(self, name: str) -> str:
        """Return skill's name as a purpose proxy; empty string if not found."""
        skill = self._db.skills_api.get_skill(name)
        return skill.name if skill is not None else ""

    def active_skills_context(self) -> str:
        skills = self._db.skills.get_for_agent(self._agent_id)
        parts = []
        for skill in skills:
            parts.append(f"## {skill.id}\n{skill.content}")
        return "\n\n".join(parts)

    def snapshot(self) -> dict[str, dict]:
        """Capture {skill_id: {content, version}} for the current active state."""
        skills = self._db.skills.get_for_agent(self._agent_id)
        return {s.id: {"content": s.content, "version": s.version} for s in skills}

    def apply_proposal(self, proposal: dict) -> dict:
        """Apply a WikiSkill proposal to agentdb.

        proposal keys:
          action="create": name, content, scope, validation_score
          action="patch":  skill_name, new_content, new_version, validation_score
        """
        action = proposal["action"]
        vs = proposal.get("validation_score")

        if action == "create":
            name = proposal["name"]
            skill = self._db.skills_api.create_skill(
                skill_id=name,
                name=name,
                content=proposal["content"],
                scope=proposal.get("scope", [self._agent_id]),
                origin="wikiskill",
                validation_score=vs,
                metadata={"source": "wikiskill"},
            )
            return {"skill": skill, "action": "create", "diff": None}

        if action == "patch":
            skill_name = proposal["skill_name"]
            new_content = proposal["new_content"]
            new_version = proposal["new_version"]
            skill = self._db.skills_api.update_skill_version(
                skill_id=skill_name,
                content=new_content,
                new_version=new_version,
                origin="wikiskill",
                validation_score=vs,
                metadata={"source": "wikiskill"},
            )
            return {"skill": skill, "action": "patch", "diff": None}

        raise ValueError(f"Unknown proposal action: {action!r}")

    def restore(self, snap: dict[str, dict]) -> None:
        """Roll back the iteration by archiving candidate versions.

        For each skill in snap: if current version differs, archive current.
        For each active skill NOT in snap: archive it (created during iteration).
        """
        current_names = set(self.list_skills())

        # Archive skills created during the iteration
        for name in current_names:
            if name not in snap:
                skill = self._db.skills_api.get_skill(name)
                if skill is not None:
                    self._db.skills_api.archive_skill(name, skill.version)

        # Restore changed skills to their prior version
        for name, prior in snap.items():
            skill = self._db.skills_api.get_skill(name)
            if skill is None:
                continue
            if skill.version != prior["version"]:
                self._db.skills_api.archive_skill(name, skill.version)
```

- [ ] **Step 4: Run tests and verify they pass**

```bash
cd /Users/jm-nx/documents/github/agentdb
pytest tests/unit/test_wikiskill_adapter.py -v
```

Expected: 13 tests pass.

- [ ] **Step 5: Run full unit suite**

```bash
pytest tests/unit/ -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb
git add src/agentdb/skills/evolution/wikiskill_adapter.py \
        tests/unit/test_wikiskill_adapter.py
git commit -m "feat: add WikiSkillAdapter backed by agentdb SkillStore"
```

---

### Task 4: SkillOptAdapter

**Files:**
- Create: `src/agentdb/skills/evolution/skillopt_adapter.py`
- Test: `tests/unit/test_skillopt_adapter.py`

**Interfaces:**
- Consumes:
  - `AgentDB.skills_api.get_skill(skill_id) -> Skill | None`
  - `AgentDB.skills_api.update_skill_version(skill_id, content, new_version, origin, validation_score, metadata)`
  - `AgentDB.skills.has_version(skill_id, version) -> bool` — from Task 2
  - `AgentDB.skills.acquire_lock(skill_id, duration_seconds) -> bool` — from Task 2
  - `AgentDB.skills.release_lock(skill_id)` — from Task 2
- Produces:
  - `SkillOptAdapter(db: AgentDB, agent_id: str)`
  - `SkillOptAdapter.push_result(skill_id: str, out_root: str | Path) -> Skill | None`
    - reads `{out_root}/best_skill.md` and `{out_root}/runtime_state.json`
    - `runtime_state.json` must have `best_step: int` and `gate_result: {action: str, score: float}`
    - writes to agentdb only when `gate_result.action == "accept_new_best"`
    - idempotent: if `skillopt_step_{best_step}` already exists, returns `None`
    - returns the new `Skill` on success, `None` otherwise
  - `SkillOptAdapter._run_subprocess(config: dict, init_content: str, out_root: Path) -> None`
    - extracted so tests can monkeypatch it without touching `subprocess`
  - `SkillOptAdapter.run_and_push(skill_id: str, config: dict, out_root: str | Path) -> Skill | None`
    - acquires lock on `skill_id`, runs `_run_subprocess`, calls `push_result`, releases lock

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_skillopt_adapter.py`:

```python
import json
import pytest
from agentdb import AgentDB
from agentdb.skills.evolution.skillopt_adapter import SkillOptAdapter


def _adapter(tmp_path):
    db = AgentDB(path=tmp_path / "a.sqlite")
    return SkillOptAdapter(db, agent_id="a1"), db


def _write_output(out_root, best_step, action, content, score=0.93):
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "best_skill.md").write_text(content, encoding="utf-8")
    (out_root / "runtime_state.json").write_text(
        json.dumps({"best_step": best_step, "gate_result": {"action": action, "score": score}}),
        encoding="utf-8",
    )


def test_push_result_writes_to_agentdb_on_accept_new_best(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    _write_output(out_root, best_step=5, action="accept_new_best", content="optimized.")

    result = adapter.push_result("my-skill", out_root)

    assert result is not None
    assert result.content == "optimized."
    assert result.version == "skillopt_step_5"
    assert result.origin == "skillopt"
    assert result.validation_score == pytest.approx(0.93)


def test_push_result_no_write_on_action_accept(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    _write_output(out_root, best_step=3, action="accept", content="tweaked.")

    result = adapter.push_result("my-skill", out_root)

    assert result is None
    assert db.skills_api.get_skill("my-skill").content == "original."


def test_push_result_no_write_on_action_reject(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    _write_output(out_root, best_step=3, action="reject", content="worse.")

    assert adapter.push_result("my-skill", out_root) is None


def test_push_result_idempotent_same_version_is_noop(tmp_path):
    # Review Focus #3: second push with same best_step returns None, no SkillConflict
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    _write_output(out_root, best_step=5, action="accept_new_best", content="optimized.")

    adapter.push_result("my-skill", out_root)
    result2 = adapter.push_result("my-skill", out_root)

    assert result2 is None


def test_push_result_missing_runtime_state_returns_none(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    out_root.mkdir()
    assert adapter.push_result("my-skill", out_root) is None


def test_push_result_missing_best_skill_md_returns_none(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    out_root.mkdir()
    (out_root / "runtime_state.json").write_text(
        json.dumps({"best_step": 1, "gate_result": {"action": "accept_new_best", "score": 0.9}})
    )
    assert adapter.push_result("my-skill", out_root) is None


def test_run_and_push_acquires_lock_during_subprocess(tmp_path, monkeypatch):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    _write_output(out_root, best_step=7, action="accept_new_best", content="best version.")

    lock_states_during_run = []

    def fake_subprocess(config, init_content, out_root_path):
        lock_states_during_run.append(db.skills.is_locked("my-skill"))

    monkeypatch.setattr(adapter, "_run_subprocess", fake_subprocess)
    adapter.run_and_push("my-skill", config={}, out_root=out_root)

    assert lock_states_during_run == [True]
    assert not db.skills.is_locked("my-skill")


def test_run_and_push_releases_lock_even_on_subprocess_error(tmp_path, monkeypatch):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    out_root.mkdir()

    def failing_subprocess(config, init_content, out_root_path):
        raise RuntimeError("training crashed")

    monkeypatch.setattr(adapter, "_run_subprocess", fake_subprocess := failing_subprocess)
    with pytest.raises(RuntimeError):
        adapter.run_and_push("my-skill", config={}, out_root=out_root)

    assert not db.skills.is_locked("my-skill")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jm-nx/documents/github/agentdb
pytest tests/unit/test_skillopt_adapter.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'agentdb.skills.evolution.skillopt_adapter'`.

- [ ] **Step 3: Create `SkillOptAdapter`**

Create `src/agentdb/skills/evolution/skillopt_adapter.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from ...interface.client import AgentDB
    from ...core.models import Skill


class SkillOptAdapter:
    """Post-training bridge that reads SkillOpt output and writes into agentdb.

    Usage::

        adapter = SkillOptAdapter(db, agent_id="my-agent")
        # After SkillOpt training finishes:
        new_skill = adapter.push_result("my-skill-id", out_root="./skillopt-runs/my-skill")
    """

    def __init__(self, db: "AgentDB", agent_id: str) -> None:
        self._db = db
        self._agent_id = agent_id

    def push_result(
        self,
        skill_id: str,
        out_root: Union[str, Path],
    ) -> Optional["Skill"]:
        """Read SkillOpt output and write to agentdb when gate accepted a new best.

        Returns the new Skill version on success, None if no write occurred.
        """
        out_root = Path(out_root)
        runtime_path = out_root / "runtime_state.json"
        best_skill_path = out_root / "best_skill.md"

        if not runtime_path.exists() or not best_skill_path.exists():
            return None

        try:
            state = json.loads(runtime_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        gate_result = state.get("gate_result", {})
        if gate_result.get("action") != "accept_new_best":
            return None

        best_step = state.get("best_step")
        if best_step is None:
            return None

        target_version = f"skillopt_step_{best_step}"

        # Idempotency: if this version already exists, do nothing
        if self._db.skills.has_version(skill_id, target_version):
            return None

        content = best_skill_path.read_text(encoding="utf-8")
        score = gate_result.get("score")
        validation_score = float(score) if score is not None else None

        return self._db.skills_api.update_skill_version(
            skill_id=skill_id,
            content=content,
            new_version=target_version,
            origin="skillopt",
            validation_score=validation_score,
            metadata={"best_step": best_step, "source": "skillopt"},
        )

    def _run_subprocess(
        self,
        config: dict,
        init_content: str,
        out_root: Path,
    ) -> None:
        """Run SkillOpt training as a subprocess.

        Writes `init_content` to `{out_root}/skill_init.md`, then invokes
        the `skillopt` CLI. Extracted as a method so tests can monkeypatch it.
        """
        import subprocess
        import tempfile

        out_root.mkdir(parents=True, exist_ok=True)
        init_path = out_root / "skill_init.md"
        init_path.write_text(init_content, encoding="utf-8")

        config_json = out_root / "config.json"
        config_json.write_text(json.dumps(config), encoding="utf-8")

        subprocess.run(
            ["skillopt", "train", "--config", str(config_json),
             "--skill-init", str(init_path), "--out-root", str(out_root)],
            check=True,
        )

    def run_and_push(
        self,
        skill_id: str,
        config: dict,
        out_root: Union[str, Path],
    ) -> Optional["Skill"]:
        """Acquire lock, run SkillOpt training, push the result, release lock.

        Returns the new Skill version if accepted, None otherwise.
        Releases the lock even if training raises.
        """
        out_root = Path(out_root)
        if not self._db.skills.acquire_lock(skill_id):
            return None

        skill = self._db.skills_api.get_skill(skill_id)
        init_content = skill.content if skill is not None else ""

        try:
            self._run_subprocess(config, init_content, out_root)
            return self.push_result(skill_id, out_root)
        finally:
            self._db.skills.release_lock(skill_id)
```

- [ ] **Step 4: Run tests and verify they pass**

```bash
cd /Users/jm-nx/documents/github/agentdb
pytest tests/unit/test_skillopt_adapter.py -v
```

Expected: 8 tests pass.

- [ ] **Step 5: Run full unit suite**

```bash
pytest tests/unit/ -q --tb=short
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb
git add src/agentdb/skills/evolution/skillopt_adapter.py \
        tests/unit/test_skillopt_adapter.py
git commit -m "feat: add SkillOptAdapter for post-training push into agentdb"
```

---

### Task 5: Package wiring + pyproject.toml extras

**Files:**
- Modify: `src/agentdb/skills/evolution/__init__.py` (add exports)
- Modify: `pyproject.toml` (add optional extras)

**Interfaces:**
- Consumes: `WikiSkillAdapter` (Task 3), `SkillOptAdapter` (Task 4), `SkillEvolutionManager` (Task 2)
- Produces: `from agentdb.skills.evolution import WikiSkillAdapter, SkillOptAdapter, SkillEvolutionManager`

- [ ] **Step 1: Write the failing import test**

Add to a new file `tests/unit/test_skill_evolution_package.py`:

```python
def test_evolution_package_exports_all_three():
    from agentdb.skills.evolution import (
        WikiSkillAdapter,
        SkillOptAdapter,
        SkillEvolutionManager,
    )
    assert WikiSkillAdapter is not None
    assert SkillOptAdapter is not None
    assert SkillEvolutionManager is not None
```

Run:

```bash
cd /Users/jm-nx/documents/github/agentdb
pytest tests/unit/test_skill_evolution_package.py -v 2>&1 | head -10
```

Expected: `ImportError` — `WikiSkillAdapter` not exported from `__init__.py`.

- [ ] **Step 2: Update `src/agentdb/skills/evolution/__init__.py`**

```python
from .wikiskill_adapter import WikiSkillAdapter
from .skillopt_adapter import SkillOptAdapter
from .manager import SkillEvolutionManager

__all__ = ["WikiSkillAdapter", "SkillOptAdapter", "SkillEvolutionManager"]
```

- [ ] **Step 3: Run import test and verify it passes**

```bash
cd /Users/jm-nx/documents/github/agentdb
pytest tests/unit/test_skill_evolution_package.py -v
```

Expected: 1 test passes.

- [ ] **Step 4: Add optional extras to `pyproject.toml`**

In `pyproject.toml`, replace the existing `[project.optional-dependencies]` section:

```toml
[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]
mcp = ["mcp>=1.0,<2.0"]
local-embed = ["sentence-transformers>=2.7,<4.0"]
wikiskill = ["wikiskill>=0.1.0"]
skillopt = ["skillopt>=0.1.0"]
evolution = ["wikiskill>=0.1.0", "skillopt>=0.1.0"]
```

- [ ] **Step 5: Run full test suite**

```bash
cd /Users/jm-nx/documents/github/agentdb
pytest tests/unit/ -q --tb=short
```

Expected: all tests pass (new total includes 1 from this task + previous).

- [ ] **Step 6: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb
git add src/agentdb/skills/evolution/__init__.py \
        tests/unit/test_skill_evolution_package.py \
        pyproject.toml
git commit -m "feat: wire evolution package exports and add pyproject.toml extras"
```

---

## Self-Review Checklist

**Spec coverage:**

| Spec requirement | Task |
|-----------------|------|
| `Skill.origin`, `validation_score`, `metadata` fields | Task 1 |
| `ALTER TABLE ADD COLUMN` migration | Task 1 |
| `create_skill()` / `update_skill_version()` new kwargs | Task 1 |
| Health blending formula (`ALPHA = 0.4`) | Task 1 |
| `locked_until` / optimistic lock | Task 2 (via `skill_locks` table) |
| `SkillEvolutionManager` acquire/release/is_locked | Task 2 |
| `WikiSkillAdapter` full protocol | Task 3 |
| `WikiSkillAdapter.restore()` archives candidates | Task 3 |
| `SkillOptAdapter.push_result()` + idempotency | Task 4 |
| `SkillOptAdapter.run_and_push()` + lock management | Task 4 |
| `src/agentdb/skills/evolution/__init__.py` | Task 5 |
| Optional extras in `pyproject.toml` | Task 5 |

**No placeholders found.**

**Type consistency verified:** `SkillEvolutionManager.wikiskill_adapter()` returns `WikiSkillAdapter` (Task 3); `skillopt_adapter()` returns `SkillOptAdapter` (Task 4). Both are lazy-imported to avoid circular imports. `SkillStore.has_version()` is called in Task 4 and defined in Task 2 — consistent.

**Review Focus coverage:**
1. metadata NULL → handled in `_row_to_skill` try/except + test `test_metadata_null_in_row_reads_as_empty_dict` (Task 1)
2. validation_score=0.0 → `is not None` check + test `test_validation_score_zero_is_not_null_and_blends` (Task 1)
3. SkillOptAdapter idempotency → `has_version()` check + test `test_push_result_idempotent_same_version_is_noop` (Task 4)
4. restore with no changes → test `test_restore_with_unchanged_snapshot_is_noop` (Task 3)
5. Expired lock reacquire → test `test_expired_lock_can_be_reacquired` (Task 2)
