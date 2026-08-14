# Working Directories Metadata Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `working_dirs` metadata to memory records so that records written in the context of specific working directories are ranked higher when retrieved in sessions using the same directories, reducing cross-context pollution.

**Architecture:** Extend `MemoryRecord` with a `working_dirs: list[str]` field. Capture the active working directories at session start via `prepare_turn()`, stamp them onto records written in `finalize_turn()`, and score them in `MultiSignalRanker` with a new Jaccard-based `workspace_overlap` signal weighted at 0.0 by default (opt-in). Records with no `working_dirs` score neutrally (0.5), so legacy data is not penalized.

**Tech Stack:** Python 3.10+, SQLite (via WAL mode), dataclasses, pytest

**Spec:** This document is self-contained — no separate spec file.

## Global Constraints

- All new fields default to empty/zero — no breaking changes to existing data or APIs
- `working_dirs` stores **normalized absolute paths** (resolved via `os.path.realpath`)
- Workspace signal weight defaults to `0.0` — feature is opt-in via `memory.yaml`
- All ranker weights must still sum to `1.0` when workspace is non-zero
- No new runtime dependencies
- Python minimum: 3.10

---

## Files Created or Modified

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `src/agentdb/core/models.py` | Add `working_dirs: list[str]` field to `MemoryRecord` |
| Modify | `src/agentdb/core/store.py` | DB schema migration + read/write for new column |
| Modify | `src/agentdb/core/profiles.py` | Add `workspace: float` to `RankerWeights` |
| Modify | `src/agentdb/retrieval/algorithm.py` | Workspace signal + new `session_working_dirs` param on `rank()` |
| Modify | `src/agentdb/interface/client.py` | Thread `working_dirs` through `retrieve()` and `write()` |
| Modify | `src/agentdb/adapters/claude_agent_sdk.py` | Capture `working_dirs` in `prepare_turn`, stamp onto writes in `finalize_turn` |
| Modify | `tests/unit/test_algorithm.py` | Tests for workspace signal and Jaccard scoring |
| Modify | `tests/unit/test_store.py` | Test new column schema migration + round-trip |
| Modify | `tests/integration/test_claude_agent_sdk_adapter.py` | End-to-end: working_dirs boosts correct records |

---

## Task 1: Add `working_dirs` to `MemoryRecord` and DB schema

**Files:**
- Modify: `src/agentdb/core/models.py` (lines 44–62)
- Modify: `src/agentdb/core/store.py` (lines 17–161)
- Test: `tests/unit/test_store.py`

**Interfaces:**
- Produces: `MemoryRecord.working_dirs: list[str]` (default `[]`)
- Produces: DB column `working_dirs TEXT NOT NULL DEFAULT '[]'`
- Produces: `MemoryStore.write(record)` serializes `working_dirs` as JSON
- Produces: `MemoryStore._row_to_record(row)` deserializes `working_dirs`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_store.py — add these tests

def test_memory_record_has_working_dirs_field():
    rec = MemoryRecord(
        key="memory/org/test",
        value="v",
        origin="agent_inferred",
        trust_level="agent",
        agent_id="test_agent",
    )
    assert rec.working_dirs == []

def test_memory_record_working_dirs_stored_and_retrieved(tmp_path):
    store = MemoryStore(str(tmp_path / "test.db"))
    rec = MemoryRecord(
        key="memory/org/test",
        value="v",
        origin="agent_inferred",
        trust_level="agent",
        agent_id="test_agent",
        working_dirs=["/home/user/project-a", "/home/user/project-b"],
    )
    store.write(rec)
    retrieved = store.get_all()
    match = next(r for r in retrieved if r.key == "memory/org/test")
    assert match.working_dirs == ["/home/user/project-a", "/home/user/project-b"]

def test_legacy_record_missing_column_defaults_to_empty_list(tmp_path):
    """Schema migration: existing DB rows without working_dirs column return []."""
    import sqlite3
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    # Create schema WITHOUT working_dirs column (simulates pre-migration DB)
    conn.execute("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, key TEXT NOT NULL, value TEXT NOT NULL,
            origin TEXT NOT NULL, trust_level TEXT NOT NULL, agent_id TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0, version INTEGER NOT NULL DEFAULT 1,
            archived INTEGER NOT NULL DEFAULT 0, retrieval_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, entities TEXT NOT NULL DEFAULT '[]',
            last_retrieved TEXT, conflict_id TEXT, expiry_time TEXT,
            inferred INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "INSERT INTO memories VALUES (?,?,?,?,?,?,1.0,1,0,0,?,?,NULL,NULL,NULL,0)",
        ("id1", "memory/org/old", '"value"', "agent_inferred", "agent", "ag",
         "2025-01-01T00:00:00", "[]"),
    )
    conn.commit()
    conn.close()

    # Opening this DB through MemoryStore should migrate and return working_dirs=[]
    store = MemoryStore(db_path)
    records = store.get_all()
    assert records[0].working_dirs == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jm-nx/Documents/GitHub/agentdb
python -m pytest tests/unit/test_store.py::test_memory_record_has_working_dirs_field tests/unit/test_store.py::test_memory_record_working_dirs_stored_and_retrieved tests/unit/test_store.py::test_legacy_record_missing_column_defaults_to_empty_list -v
```

Expected: FAIL — `MemoryRecord` has no `working_dirs` field.

- [ ] **Step 3: Add `working_dirs` to `MemoryRecord`**

In `src/agentdb/core/models.py`, add the field after `inferred: bool = False`:

```python
# src/agentdb/core/models.py — inside MemoryRecord dataclass
working_dirs: list = field(default_factory=list)
```

- [ ] **Step 4: Add DB column + migration in `MemoryStore.__init__`**

In `src/agentdb/core/store.py`:

```python
# In CREATE TABLE statement (add after `inferred INTEGER NOT NULL DEFAULT 0`):
#   working_dirs TEXT NOT NULL DEFAULT '[]'

# In the _migrate() method (or wherever ALTER TABLE migrations live), add:
try:
    conn.execute("ALTER TABLE memories ADD COLUMN working_dirs TEXT NOT NULL DEFAULT '[]'")
except Exception:
    pass  # column already exists
```

Find the existing migration block (lines ~38–43 in store.py) and add this alongside `last_retrieved`, `inferred`, etc.

- [ ] **Step 5: Update `store.write()` to serialize `working_dirs`**

In the `conn.execute(INSERT OR REPLACE ...)` call, add `working_dirs` to:
- The column list: `..., inferred, working_dirs)`
- The values tuple: `..., int(record.inferred), json.dumps(record.working_dirs))`

- [ ] **Step 6: Update `_row_to_record()` (or equivalent) to deserialize `working_dirs`**

Wherever rows are converted back to `MemoryRecord` objects, add:

```python
working_dirs=json.loads(row["working_dirs"]) if row["working_dirs"] else [],
```

Use `row["working_dirs"]` with a fallback for legacy rows where the column may be absent:

```python
working_dirs=json.loads(row.get("working_dirs") or "[]"),
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
python -m pytest tests/unit/test_store.py::test_memory_record_has_working_dirs_field tests/unit/test_store.py::test_memory_record_working_dirs_stored_and_retrieved tests/unit/test_store.py::test_legacy_record_missing_column_defaults_to_empty_list -v
```

Expected: PASS

- [ ] **Step 8: Run full unit test suite to catch regressions**

```bash
python -m pytest tests/unit/ -v
```

Expected: all previously passing tests still pass.

- [ ] **Step 9: Commit**

```bash
git add src/agentdb/core/models.py src/agentdb/core/store.py tests/unit/test_store.py
git commit -m "feat: add working_dirs field to MemoryRecord and DB schema"
```

---

## Task 2: Add `workspace` weight to `RankerWeights` and profiles

**Files:**
- Modify: `src/agentdb/core/profiles.py` (lines 22–113)
- Modify: `src/agentdb/interface/client.py` (lines 461–470)

**Interfaces:**
- Consumes: `RankerWeights` dataclass from Task 1 (unchanged)
- Produces: `RankerWeights.workspace: float` (new field, default `0.0`)
- Produces: all three built-in profiles include `workspace=0.0`
- Produces: `client.py` passes `weight_workspace` to `MultiSignalRanker.__init__`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_profiles.py (or test_algorithm.py) — add:

def test_ranker_weights_has_workspace_field():
    from agentdb.core.profiles import RankerWeights
    w = RankerWeights(
        provenance=0.35, recency=0.25, confidence=0.25,
        causal=0.15, semantic=0.0, workspace=0.0
    )
    assert w.workspace == 0.0

def test_builtin_profiles_include_workspace_weight():
    from agentdb.core.profiles import BUDGET_OPTIMIZED, FULL_STORAGE, RESEARCH
    for profile in (BUDGET_OPTIMIZED, FULL_STORAGE, RESEARCH):
        assert hasattr(profile.retrieval.ranker_weights, "workspace")
        assert profile.retrieval.ranker_weights.workspace == 0.0

def test_weights_sum_to_one_including_workspace():
    from agentdb.core.profiles import RankerWeights
    # When workspace > 0, all six weights must sum to 1.0
    w = RankerWeights(
        provenance=0.30, recency=0.25, confidence=0.25,
        causal=0.10, semantic=0.0, workspace=0.10
    )
    total = w.provenance + w.recency + w.confidence + w.causal + w.semantic + w.workspace
    assert abs(total - 1.0) < 1e-9
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/unit/test_profiles.py -k "workspace" -v 2>/dev/null || \
python -m pytest tests/unit/test_algorithm.py -k "workspace" -v
```

Expected: FAIL — `RankerWeights` has no `workspace` field.

- [ ] **Step 3: Add `workspace` to `RankerWeights`**

In `src/agentdb/core/profiles.py`:

```python
@dataclass(frozen=True)
class RankerWeights:
    provenance: float
    recency: float
    confidence: float
    causal: float
    semantic: float
    workspace: float = 0.0  # opt-in; weight 0.0 = disabled
```

- [ ] **Step 4: Update all three built-in profile definitions**

Each `RankerWeights(...)` instantiation in the file (BUDGET_OPTIMIZED, FULL_STORAGE, RESEARCH) already sums to 1.0 across the first five fields. Add `workspace=0.0` to each:

```python
# Example (repeat pattern for all three profiles):
ranker_weights=RankerWeights(
    provenance=0.35, recency=0.25, confidence=0.25,
    causal=0.15, semantic=0.0, workspace=0.0
)
```

- [ ] **Step 5: Update YAML config parsing to handle `workspace` key**

Find where `memory.yaml`'s `ranker_weights` block is parsed into a dict (likely in `config_for()` or a YAML loader). Ensure `workspace` defaults to `0.0` if absent:

```python
# Wherever ranker_weights dict is assembled from YAML:
rw_dict = {
    "provenance": raw.get("provenance", 0.35),
    "recency":    raw.get("recency",    0.25),
    "confidence": raw.get("confidence", 0.25),
    "causal":     raw.get("causal",     0.15),
    "semantic":   raw.get("semantic",   0.0),
    "workspace":  raw.get("workspace",  0.0),   # new
}
```

- [ ] **Step 6: Pass `weight_workspace` to `MultiSignalRanker` in `client.py`**

In `src/agentdb/interface/client.py` lines 461–470:

```python
rw = self._resolved_config["retrieval"]["ranker_weights"]
lc = self._resolved_config["lifecycle"]
self._ranker = MultiSignalRanker(
    weight_provenance=rw["provenance"],
    weight_recency=rw["recency"],
    weight_confidence=rw["confidence"],
    weight_causal=rw["causal"],
    weight_semantic=rw["semantic"],
    weight_workspace=rw.get("workspace", 0.0),   # new
    staleness_days=lc["staleness_days"],
)
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
python -m pytest tests/unit/ -k "workspace" -v
```

Expected: PASS

- [ ] **Step 8: Run full unit test suite**

```bash
python -m pytest tests/unit/ -v
```

Expected: all previously passing tests still pass.

- [ ] **Step 9: Commit**

```bash
git add src/agentdb/core/profiles.py src/agentdb/interface/client.py
git commit -m "feat: add workspace weight to RankerWeights and built-in profiles"
```

---

## Task 3: Add `workspace_overlap` signal to `MultiSignalRanker`

**Files:**
- Modify: `src/agentdb/retrieval/algorithm.py` (lines 57–186)
- Test: `tests/unit/test_algorithm.py`

**Interfaces:**
- Consumes: `MemoryRecord.working_dirs: list[str]` (from Task 1)
- Consumes: `weight_workspace: float` kwarg on `MultiSignalRanker.__init__` (from Task 2)
- Produces: `MultiSignalRanker.__init__` accepts `weight_workspace: float = 0.0`
- Produces: `MultiSignalRanker.rank()` accepts `session_working_dirs: Optional[list[str]] = None`
- Produces: `RankedRecord.signal_breakdown` includes `"workspace"` key
- Produces: `_workspace_score(record, session_working_dirs) -> float` (internal method)

**Signal semantics:**
- `session_working_dirs` is `None` or `[]` → return `0.5` (neutral, no data)
- `record.working_dirs` is `[]` → return `0.5` (neutral, legacy record)
- Both non-empty → Jaccard coefficient: `|intersection| / |union|` (0.0–1.0)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_algorithm.py — add:

def _rec_with_dirs(working_dirs, **kwargs):
    """Helper: build a MemoryRecord with working_dirs set."""
    defaults = dict(
        key="memory/org/test", value="v", origin="agent_inferred",
        trust_level="agent", agent_id="ag", confidence=1.0,
    )
    defaults.update(kwargs)
    return MemoryRecord(**defaults, working_dirs=working_dirs)


def test_workspace_score_neutral_when_no_session_dirs():
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.10,
    )
    rec = _rec_with_dirs(["/home/user/repo-a"])
    # No session dirs → neutral score (0.5) for workspace signal
    score = ranker._workspace_score(rec, session_working_dirs=None)
    assert score == 0.5


def test_workspace_score_neutral_when_record_has_no_dirs():
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.10,
    )
    rec = _rec_with_dirs([])
    score = ranker._workspace_score(rec, session_working_dirs=["/home/user/repo-a"])
    assert score == 0.5


def test_workspace_score_full_overlap():
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.10,
    )
    rec = _rec_with_dirs(["/home/user/repo-a", "/home/user/repo-b"])
    score = ranker._workspace_score(
        rec, session_working_dirs=["/home/user/repo-a", "/home/user/repo-b"]
    )
    assert score == 1.0


def test_workspace_score_partial_overlap():
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.10,
    )
    rec = _rec_with_dirs(["/home/user/repo-a", "/home/user/repo-b"])
    # intersection={repo-a}, union={repo-a, repo-b, repo-c} → 1/3
    score = ranker._workspace_score(
        rec, session_working_dirs=["/home/user/repo-a", "/home/user/repo-c"]
    )
    assert abs(score - 1/3) < 1e-9


def test_workspace_score_zero_overlap():
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.10,
    )
    rec = _rec_with_dirs(["/home/user/repo-a"])
    score = ranker._workspace_score(
        rec, session_working_dirs=["/home/user/repo-b"]
    )
    assert score == 0.0


def test_workspace_boosts_matching_dir_record_above_non_matching():
    """Records written in the active working dir rank above those from other dirs."""
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.10, weight_semantic=0.0, weight_workspace=0.15,
    )
    matching = _rec_with_dirs(["/home/user/dbt"])
    different = _rec_with_dirs(["/home/user/other-repo"])

    ranked = ranker.rank(
        query="some query",
        candidates=[different, matching],
        agent_id="ag",
        session_working_dirs=["/home/user/dbt"],
    )
    # matching record should rank first
    assert ranked[0].record.working_dirs == ["/home/user/dbt"]


def test_workspace_signal_in_breakdown():
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.10,
    )
    rec = _rec_with_dirs(["/home/user/repo-a"])
    ranked = ranker.rank(
        "q", [rec], "ag", session_working_dirs=["/home/user/repo-a"]
    )
    assert "workspace" in ranked[0].signal_breakdown


def test_workspace_disabled_when_weight_zero():
    """weight_workspace=0.0 means workspace signal has no effect on scores."""
    ranker = MultiSignalRanker(
        weight_provenance=0.35, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.0,
    )
    with_dirs = _rec_with_dirs(["/home/user/repo-a"])
    without_dirs = _rec_with_dirs([])

    ranked_with = ranker.rank("q", [with_dirs], "ag", session_working_dirs=["/home/user/repo-a"])
    ranked_without = ranker.rank("q", [without_dirs], "ag", session_working_dirs=["/home/user/repo-a"])
    # Scores should be identical because workspace weight is 0
    assert ranked_with[0].composite_score == ranked_without[0].composite_score
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/unit/test_algorithm.py -k "workspace" -v
```

Expected: FAIL — `MultiSignalRanker` has no `weight_workspace` param or `_workspace_score`.

- [ ] **Step 3: Add `weight_workspace` to `MultiSignalRanker.__init__`**

In `src/agentdb/retrieval/algorithm.py`:

```python
def __init__(
    self,
    weight_provenance: float = 0.35,
    weight_recency: float = 0.25,
    weight_confidence: float = 0.25,
    weight_causal: float = 0.15,
    weight_semantic: float = 0.0,
    weight_workspace: float = 0.0,   # new
    recency_lambda: float = 0.01,
    staleness_days: int = 90,
    provenance_scores: Optional[dict] = None,
):
    ...
    self.w_workspace = weight_workspace   # new
```

- [ ] **Step 4: Add `_workspace_score()` method**

```python
def _workspace_score(
    self, record: "MemoryRecord", session_working_dirs: Optional[list[str]]
) -> float:
    """Jaccard similarity between record's working_dirs and session's working_dirs.

    Returns 0.5 (neutral) when either side has no data, so legacy records
    are not penalized and sessions with no working_dirs don't distort scores.
    """
    rec_dirs = set(getattr(record, "working_dirs", []) or [])
    sess_dirs = set(session_working_dirs or [])
    if not rec_dirs or not sess_dirs:
        return 0.5
    intersection = rec_dirs & sess_dirs
    union = rec_dirs | sess_dirs
    return len(intersection) / len(union)
```

- [ ] **Step 5: Add `session_working_dirs` param to `rank()` and integrate signal**

```python
def rank(
    self,
    query: str,
    candidates: list,
    agent_id: str,
    query_embedding: Optional[list] = None,
    session_id: Optional[str] = None,
    session_working_dirs: Optional[list[str]] = None,   # new
) -> list["RankedRecord"]:
```

Inside the per-record loop where composite is calculated, add `workspace` signal:

```python
workspace = self._workspace_score(record, session_working_dirs)

composite = (
    self.w_provenance * prov
    + self.w_recency * rec
    + self.w_confidence * conf
    + self.w_causal * causal
    + self.w_semantic * sem
    + self.w_workspace * workspace   # new
)
```

Update `signal_breakdown` dict:

```python
signal_breakdown = {
    "provenance": prov,
    "recency": rec,
    "confidence": conf,
    "causal": causal,
    "semantic": sem,
    "workspace": workspace,   # new
}
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
python -m pytest tests/unit/test_algorithm.py -k "workspace" -v
```

Expected: all 8 workspace tests PASS.

- [ ] **Step 7: Run full unit test suite**

```bash
python -m pytest tests/unit/ -v
```

Expected: all previously passing tests still pass.

- [ ] **Step 8: Commit**

```bash
git add src/agentdb/retrieval/algorithm.py tests/unit/test_algorithm.py
git commit -m "feat: add workspace_overlap signal to MultiSignalRanker"
```

---

## Task 4: Thread `working_dirs` through `retrieve()` and middleware

**Files:**
- Modify: `src/agentdb/interface/client.py` (lines 670–678, `retrieve()`)
- Modify: `src/agentdb/adapters/claude_agent_sdk.py` (`prepare_turn`, `_session_working_dirs`)
- Test: `tests/unit/test_algorithm.py` (existing tests still pass)

**Interfaces:**
- Consumes: `MultiSignalRanker.rank(session_working_dirs=...)` (from Task 3)
- Produces: `AgentDB.retrieve(working_dirs: Optional[list[str]] = None)` — new param
- Produces: `AgentDBClaudeMiddleware` stores `_session_working_dirs: dict[str, list[str]]`
- Produces: `prepare_turn(working_dirs: Optional[list[str]] = None)` — new param
- Produces: `prepare_turn_async(working_dirs: Optional[list[str]] = None)` — new param

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_store.py or test_client.py — add:

def test_retrieve_accepts_working_dirs_param(tmp_path):
    """retrieve() must accept working_dirs without error even when weight=0."""
    import os
    from agentdb import AgentDB
    mem_dir = tmp_path / "ws"
    mem_dir.mkdir()
    (mem_dir / "config").mkdir()
    (mem_dir / "config" / "memory.yaml").write_text(
        "profile:\n  name: budget_optimized\nagents:\n  ag: {}\n"
    )
    db = AgentDB(path=str(tmp_path / "db.db"), memory_dir=str(mem_dir))
    db.write(key="memory/org/x", value="v", origin="agent_inferred", agent_id="ag")
    result = db.retrieve(
        query="x",
        agent_id="ag",
        working_dirs=["/home/user/repo-a"],
    )
    assert result is not None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/unit/test_store.py::test_retrieve_accepts_working_dirs_param -v
```

Expected: FAIL — `retrieve()` does not accept `working_dirs`.

- [ ] **Step 3: Add `working_dirs` param to `retrieve()` in `client.py`**

```python
def retrieve(
    self,
    query: str,
    agent_id: str,
    limit: Optional[int] = None,
    session_id: Optional[str] = None,
    external_context: Optional[list[dict]] = None,
    budget_tokens: Optional[int] = None,
    working_dirs: Optional[list[str]] = None,   # new
) -> RetrievalResult:
```

Pass it to the ranker call:

```python
ranked_all = self._ranker.rank(
    query, candidates, agent_id=agent_id,
    session_working_dirs=working_dirs,   # new
)
```

- [ ] **Step 4: Add `_session_working_dirs` dict to `AgentDBClaudeMiddleware.__init__`**

In `src/agentdb/adapters/claude_agent_sdk.py` inside `__init__`:

```python
self._session_working_dirs: dict[str, list[str]] = {}
```

- [ ] **Step 5: Add `working_dirs` param to `prepare_turn()` and store it**

```python
def prepare_turn(
    self,
    *,
    user_query: str,
    session_id: str,
    system_prompt: Optional[str] = None,
    external_context: Optional[list[dict[str, Any]]] = None,
    base_messages: Optional[list[dict[str, str]]] = None,
    working_dirs: Optional[list[str]] = None,   # new
) -> tuple[list[dict[str, str]], "RetrievalResult"]:
    if working_dirs is not None:
        self._session_working_dirs[session_id] = [
            os.path.realpath(d) for d in working_dirs
        ]
    ...
```

Then in the `db.retrieve()` call inside `prepare_turn()`, pass the stored dirs:

```python
result = self._db.retrieve(
    query=user_query,
    agent_id=self._agent_id,
    session_id=session_id,
    working_dirs=self._session_working_dirs.get(session_id),   # new
    ...
)
```

- [ ] **Step 6: Mirror the new param in `prepare_turn_async()`**

```python
async def prepare_turn_async(
    self,
    *,
    user_query: str,
    session_id: str,
    system_prompt: Optional[str] = None,
    external_context: Optional[list[dict[str, Any]]] = None,
    base_messages: Optional[list[dict[str, str]]] = None,
    working_dirs: Optional[list[str]] = None,   # new
) -> tuple[list[dict[str, str]], "RetrievalResult"]:
    return self.prepare_turn(
        user_query=user_query,
        session_id=session_id,
        system_prompt=system_prompt,
        external_context=external_context,
        base_messages=base_messages,
        working_dirs=working_dirs,   # new
    )
```

- [ ] **Step 7: Run tests**

```bash
python -m pytest tests/unit/ tests/integration/ -v
```

Expected: all previously passing tests still pass; new test also passes.

- [ ] **Step 8: Commit**

```bash
git add src/agentdb/interface/client.py src/agentdb/adapters/claude_agent_sdk.py
git commit -m "feat: thread working_dirs through retrieve() and prepare_turn middleware"
```

---

## Task 5: Stamp `working_dirs` onto records at write time

**Files:**
- Modify: `src/agentdb/interface/client.py` (`write()` method)
- Modify: `src/agentdb/adapters/claude_agent_sdk.py` (`finalize_turn()`)
- Test: `tests/integration/test_claude_agent_sdk_adapter.py`

**Interfaces:**
- Consumes: `_session_working_dirs` (from Task 4)
- Produces: `AgentDB.write(working_dirs: Optional[list[str]] = None)` — new param
- Produces: records written inside `finalize_turn` carry session's `working_dirs`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/test_claude_agent_sdk_adapter.py — add:

def test_records_stamped_with_working_dirs_and_ranked_higher(tmp_path):
    """End-to-end: records written in session A's working_dirs score above records
    from session B's dirs when retrieved in a session with A's dirs."""
    import os
    from agentdb import AgentDB
    from agentdb.adapters.claude_agent_sdk import AgentDBClaudeMiddleware

    mem_dir = _mem_dir_with_profile(tmp_path, """
profile:
  name: budget_optimized
  retrieval:
    retrieve_limit: 10
    min_composite_score: 0.0
    ranker_weights:
      provenance: 0.25
      recency: 0.25
      confidence: 0.25
      causal: 0.10
      semantic: 0.0
      workspace: 0.15
agents:
  agent_a: {}
""")
    db = AgentDB(path=str(tmp_path / "db.db"), memory_dir=mem_dir)
    mw = AgentDBClaudeMiddleware(db=db, agent_id="agent_a")

    # Session 1: working in /project-dbt — writes a record
    mw.prepare_turn(
        user_query="dbt query",
        session_id="sess-dbt",
        working_dirs=["/project-dbt"],
    )
    mw.finalize_turn(
        session_id="sess-dbt",
        model_response="dbt response",
        memory_writes=[{
            "key": "memory/org/dbt-fact",
            "value": "models are in /project-dbt/models",
            "origin": "agent_inferred",
        }],
    )

    # Session 2: working in /project-other — writes a record
    mw.prepare_turn(
        user_query="other query",
        session_id="sess-other",
        working_dirs=["/project-other"],
    )
    mw.finalize_turn(
        session_id="sess-other",
        model_response="other response",
        memory_writes=[{
            "key": "memory/org/other-fact",
            "value": "something from other project",
            "origin": "agent_inferred",
        }],
    )

    # New session: working in /project-dbt — retrieves; dbt-fact should rank higher
    messages, result = mw.prepare_turn(
        user_query="tell me about dbt",
        session_id="sess-new",
        working_dirs=["/project-dbt"],
    )
    record_keys = [r.key for r in result.records]
    assert "memory/org/dbt-fact" in record_keys
    dbt_idx = record_keys.index("memory/org/dbt-fact")
    if "memory/org/other-fact" in record_keys:
        other_idx = record_keys.index("memory/org/other-fact")
        assert dbt_idx < other_idx, "dbt-fact should rank above other-fact"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/integration/test_claude_agent_sdk_adapter.py::test_records_stamped_with_working_dirs_and_ranked_higher -v
```

Expected: FAIL — `write()` doesn't accept `working_dirs`; records are not stamped.

- [ ] **Step 3: Add `working_dirs` param to `db.write()` in `client.py`**

```python
def write(
    self,
    key: str,
    value: Any,
    origin: str,
    agent_id: str,
    session_id: Optional[str] = None,
    entities: Optional[list] = None,
    confidence: Optional[float] = None,
    infer: bool = False,
    expiry_time: Optional["datetime"] = None,
    working_dirs: Optional[list[str]] = None,   # new
) -> MemoryRecord:
```

In the `MemoryRecord(...)` constructor call inside `write()`, add:

```python
working_dirs=[os.path.realpath(d) for d in (working_dirs or [])],
```

- [ ] **Step 4: Stamp `working_dirs` onto records in `finalize_turn()`**

In `src/agentdb/adapters/claude_agent_sdk.py`, inside `finalize_turn()`, wherever `self._db.write(...)` is called for `memory_writes` entries:

```python
session_dirs = self._session_working_dirs.get(session_id)

for write_spec in (memory_writes or []):
    self._db.write(
        key=write_spec["key"],
        value=write_spec["value"],
        origin=write_spec.get("origin", "agent_inferred"),
        agent_id=self._agent_id,
        session_id=session_id,
        entities=write_spec.get("entities"),
        confidence=write_spec.get("confidence"),
        working_dirs=session_dirs,   # new
    )
```

Also stamp `session_dirs` on the auto-persist write (if `auto_persist_response` is enabled), using the same pattern.

- [ ] **Step 5: Run the integration test**

```bash
python -m pytest tests/integration/test_claude_agent_sdk_adapter.py::test_records_stamped_with_working_dirs_and_ranked_higher -v
```

Expected: PASS

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/agentdb/interface/client.py src/agentdb/adapters/claude_agent_sdk.py tests/integration/test_claude_agent_sdk_adapter.py
git commit -m "feat: stamp working_dirs onto records at write time in finalize_turn"
```

---

## Task 6: Document the feature and update `memory.yaml` reference

**Files:**
- Modify: `docs/context/database-schema.md` or equivalent reference doc
- Modify: any sample `memory.yaml` files in `docs/` or `examples/`

**Interfaces:**
- Consumes: all previous tasks (feature complete)
- Produces: documented `working_dirs` field, `workspace` weight in `memory.yaml`

- [ ] **Step 1: Add `working_dirs` to the schema docs**

In `docs/context/database-schema.md`, find the `memories` table column list. Add:

```
| working_dirs | TEXT | JSON array of normalized absolute paths active at write time. Empty by default. |
```

- [ ] **Step 2: Document `workspace` weight in `memory.yaml` reference**

Find the `ranker_weights` block documentation. Add:

```yaml
ranker_weights:
  provenance: 0.35   # trust-origin weight
  recency: 0.25      # time decay weight
  confidence: 0.25   # record confidence weight
  causal: 0.15       # retrieval-frequency weight
  semantic: 0.0      # embedding similarity (Phase 3, currently inactive)
  workspace: 0.0     # working-directory overlap (Jaccard). Opt-in: set > 0 and reduce other weights to maintain sum=1.0
```

- [ ] **Step 3: Commit**

```bash
git add docs/
git commit -m "docs: document working_dirs field and workspace ranker weight"
```

---

## Self-Review

**Spec coverage:**
- `working_dirs` field on `MemoryRecord` + DB — ✅ Task 1
- `workspace` weight in `RankerWeights` and profiles — ✅ Task 2
- `workspace_overlap` Jaccard signal in `MultiSignalRanker` — ✅ Task 3
- `retrieve()` accepts `working_dirs` — ✅ Task 4
- `prepare_turn` / `prepare_turn_async` accept `working_dirs` — ✅ Task 4
- Records stamped at `finalize_turn` write time — ✅ Task 5
- Neutral score (0.5) for legacy records — ✅ Task 3
- Default weight 0.0 (opt-in) — ✅ Task 2
- Backward compatible (existing DBs migrated) — ✅ Task 1
- `os.path.realpath` normalization — ✅ Tasks 4, 5
- Documentation — ✅ Task 6

**No placeholders found.**

**Type consistency:** `working_dirs` is `list[str]` consistently across model, store, write API, retrieve API, ranker, and middleware. `session_working_dirs: Optional[list[str]]` in ranker matches middleware usage.
