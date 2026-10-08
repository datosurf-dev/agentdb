# Session Metadata, Skill Conflict Signal, and Memory Consolidation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three capabilities that complete the arbitration loop: (1) structured session metadata for cross-repo isolation, (2) skill–memory conflict signal using metadata-aware overlap detection, (3) memory consolidation — auto-triggered, producing marked canonical records with provenance traces, excluding them from injection on request, and re-checking skill conflicts on LLM-synthesized output.

**Architecture:** No new source files, no new dependencies. `SessionProfile` is an in-memory dataclass in `AgentDB._session_profiles`. Classification is rule-based (no LLM, no embeddings). Consolidation state lives in a `consolidation_state` SQLite table created by `LifecycleManager`. Auto-trigger fires at `conversation_started` after 7 days AND 10+ sessions per agent. Canonical records are marked `consolidated=True` and carry `source_record_ids` as a JSON list (same pattern as `entities`). Skill conflicts on LLM-synthesized canonicals are re-checked synchronously inside `AgentDB.consolidate()`.

**Tech Stack:** Python 3.11+, existing `MemoryStore`, `LifecycleManager`, `SkillStore`, `Gateway`.

**Spec:** `docs/context/architecture-principles.md` (Principles 1a, 4a, 7, 12b), `docs/context/capabilities.md`.

## Global Constraints

- Test runner: `cd /Users/jm-nx/documents/github/agentdb && /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q tests/unit`
- Baseline: 345 unit tests pass. Every task must leave the suite green.
- No new source files. One new test file per task.
- No new dependencies.
- No `Co-Authored-By` trailers on commits.
- Every gateway dispatch and every `llm_fn` call wraps in `try/except Exception: pass` — never propagates to callers.
- `human_approved` records are immune to all archival operations, including consolidation.
- All new `MemoryRecord` fields follow the existing migration pattern in `store.py`: add to `_MEMORIES_MIGRATION_COLUMNS`, update `_row_to_record()`, and update `write()` INSERT to include the new columns.

## Review Focus

1. **Multi-repo session filter correctness** — records with empty `working_dirs` must always pass the repo filter. Org-scoped records and records predating this feature have no `working_dirs`. Test this explicitly.
2. **Consolidation idempotency** — running `consolidate()` twice on the same agent must not archive the already-archived records from the first pass, because `list_all()` and `list_by_agent()` both filter `archived=True`.
3. **Auto-trigger threshold** — both conditions (7 days elapsed AND 10+ sessions) must hold simultaneously. Test that 10 sessions within 7 days does NOT trigger.
4. **`include_consolidated=False` doesn't hide human-approved canonicals** — human_approved records can never be consolidated (immune), so the `consolidated` flag is never set on them. The filter is safe, but test it explicitly.
5. **Skill conflict re-check scope** — the re-check after LLM synthesis only runs for canonicals whose value was updated by `llm_fn`. Non-LLM consolidation must not trigger a second round of skill conflict checks.

---

## Task 1: Session metadata and repo filter

**Files:**
- Modify: `src/agentdb/core/models.py` — add `SessionProfile` dataclass
- Modify: `src/agentdb/interface/client.py` — add `_extract_repo()`, `_classify_request_type()` module-level helpers; add `_session_profiles` to `__init__`; add `_handle_conversation_started_metadata` subscriber (always active, no embeddings required); expand profile in `write()`; apply repo filter in `retrieve()`
- Test: `tests/unit/test_session_metadata.py`

**Interfaces:**
- Produces:
  - `SessionProfile(session_id: str, agent_id: str | None, repos: set[str], request_types: set[str])` in `models.py`
  - `AgentDB._session_profiles: dict[str, SessionProfile]`
  - `_extract_repo(working_dirs) -> str | None` — module-level in `client.py`
  - `_classify_request_type(text: str) -> str` — module-level in `client.py`
  - `conversation_started` dispatch now accepts optional `agent_id: str` and `working_dirs: list[str]` kwargs (additive — existing callers unaffected)
- Consumed by Task 3: `SessionProfile.agent_id` used by the auto-trigger in `_handle_conversation_started_metadata`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_session_metadata.py`:

```python
import pytest
from agentdb import AgentDB
from agentdb.interface.client import _extract_repo, _classify_request_type


def _db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


# ── Helper unit tests ────────────────────────────────────────────────────────

def test_extract_repo_from_path():
    assert _extract_repo(["/Users/x/agentdb"]) == "agentdb"
    assert _extract_repo(["/Users/x/dbt/models"]) == "models"
    assert _extract_repo([]) is None
    assert _extract_repo(None) is None


def test_classify_request_type_rules():
    assert _classify_request_type("review this PR") == "code_review"
    assert _classify_request_type("implement the new feature") == "code_generation"
    assert _classify_request_type("debug the error traceback") == "debugging"
    assert _classify_request_type("write documentation for this") == "documentation"
    assert _classify_request_type("research options for caching") == "research"
    assert _classify_request_type("hello") == "general"


# ── Session profile seeding ──────────────────────────────────────────────────

def test_session_profile_seeded_at_conversation_started(tmp_path):
    db = _db(tmp_path)
    db.gateway.dispatch(
        "conversation_started",
        session_id="s1",
        agent_id="a1",
        opening_message="review this pull request",
        working_dirs=["/Users/x/agentdb"],
    )
    profile = db._session_profiles.get("s1")
    assert profile is not None
    assert "agentdb" in profile.repos
    assert "code_review" in profile.request_types
    assert profile.agent_id == "a1"


def test_session_profile_created_with_no_working_dirs(tmp_path):
    db = _db(tmp_path)
    db.gateway.dispatch(
        "conversation_started",
        session_id="s2",
        opening_message="implement this feature",
    )
    profile = db._session_profiles.get("s2")
    assert profile is not None
    assert "code_generation" in profile.request_types
    assert profile.repos == set()


# ── Profile expansion on write ───────────────────────────────────────────────

def test_repos_expand_on_write(tmp_path):
    db = _db(tmp_path)
    db.gateway.dispatch("conversation_started", session_id="s1", agent_id="a1",
                        opening_message="review PR", working_dirs=["/Users/x/agentdb"])
    db.write("memory/task", "val", origin="agent_inferred", agent_id="a1",
             session_id="s1", working_dirs=["/Users/x/dbt"])
    profile = db._session_profiles["s1"]
    assert "agentdb" in profile.repos
    assert "dbt" in profile.repos


def test_request_types_expand_on_write(tmp_path):
    db = _db(tmp_path)
    db.gateway.dispatch("conversation_started", session_id="s1", agent_id="a1",
                        opening_message="review PR", working_dirs=["/Users/x/agentdb"])
    db.write("memory/debug-task", "debug error traceback",
             origin="agent_inferred", agent_id="a1", session_id="s1")
    profile = db._session_profiles["s1"]
    assert "code_review" in profile.request_types
    assert "debugging" in profile.request_types


def test_write_without_session_id_does_not_create_profile(tmp_path):
    db = _db(tmp_path)
    db.write("memory/task", "val", origin="agent_inferred", agent_id="a1")
    assert db._session_profiles == {}


# ── Repo filter at retrieve ───────────────────────────────────────────────────

def test_repo_filter_excludes_other_repo_records(tmp_path):
    db = _db(tmp_path)
    db.write("memory/dbt-task", "dbt value", origin="agent_inferred",
             agent_id="a1", working_dirs=["/Users/x/dbt"])
    db.gateway.dispatch("conversation_started", session_id="s1", agent_id="a1",
                        opening_message="implement feature",
                        working_dirs=["/Users/x/agentdb"])
    result = db.retrieve("task", agent_id="a1", session_id="s1")
    keys = [r.key for r in result.records]
    assert "memory/dbt-task" not in keys


def test_repo_filter_passes_records_with_no_working_dirs(tmp_path):
    db = _db(tmp_path)
    db.write("memory/org/policy", "always true", origin="human_approved", agent_id="a1")
    db.gateway.dispatch("conversation_started", session_id="s1", agent_id="a1",
                        opening_message="implement feature",
                        working_dirs=["/Users/x/agentdb"])
    result = db.retrieve("policy", agent_id="a1", session_id="s1")
    keys = [r.key for r in result.records]
    assert "memory/org/policy" in keys


def test_repo_filter_inactive_without_session_id(tmp_path):
    db = _db(tmp_path)
    db.write("memory/dbt-task", "value", origin="agent_inferred",
             agent_id="a1", working_dirs=["/Users/x/dbt"])
    result = db.retrieve("dbt task", agent_id="a1")
    assert len(result.records) >= 1


def test_multi_repo_session_retrieves_from_both(tmp_path):
    db = _db(tmp_path)
    db.write("memory/agentdb-note", "agentdb val", origin="agent_inferred",
             agent_id="a1", working_dirs=["/Users/x/agentdb"])
    db.write("memory/dbt-note", "dbt val", origin="agent_inferred",
             agent_id="a1", working_dirs=["/Users/x/dbt"])
    db.gateway.dispatch("conversation_started", session_id="s1", agent_id="a1",
                        opening_message="implement feature",
                        working_dirs=["/Users/x/agentdb"])
    db.write("memory/dbt-task", "new dbt work", origin="agent_inferred",
             agent_id="a1", session_id="s1", working_dirs=["/Users/x/dbt"])
    result = db.retrieve("note", agent_id="a1", session_id="s1")
    keys = [r.key for r in result.records]
    assert "memory/agentdb-note" in keys
    assert "memory/dbt-note" in keys
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_session_metadata.py -v 2>&1 | tail -20
```

Expected: all tests fail — `_extract_repo`, `_classify_request_type` not importable; `_session_profiles` not on `AgentDB`.

- [ ] **Step 3: Add `SessionProfile` to `models.py`**

Append after the `Skill` dataclass:

```python
@dataclass
class SessionProfile:
    session_id: str
    agent_id: Optional[str] = None
    repos: set = field(default_factory=set)
    request_types: set = field(default_factory=set)
```

- [ ] **Step 4: Add module-level helpers to `client.py`**

Add after the `EVENT_*` constants block, before `@dataclass AuditEntry`:

```python
# ---------------------------------------------------------------------------
# Session metadata helpers
# ---------------------------------------------------------------------------

_REQUEST_TYPE_RULES: list = [
    ({"review", "feedback", "diff", "pr", "pull request"}, "code_review"),
    ({"implement", "build", "create", "feat", "feature", "add"}, "code_generation"),
    ({"debug", "fix", "error", "bug", "broken", "failing", "traceback"}, "debugging"),
    ({"doc", "readme", "explain", "document", "overview"}, "documentation"),
    ({"research", "brainstorm", "explore", "investigate", "survey"}, "research"),
    ({"plan", "design", "architect", "strategy", "roadmap"}, "planning"),
]


def _extract_repo(working_dirs) -> "Optional[str]":
    if not working_dirs:
        return None
    last = (working_dirs[-1] or "").rstrip("/")
    segment = last.split("/")[-1] if "/" in last else last
    return segment or None


def _classify_request_type(text: str) -> str:
    text_lower = text.lower()
    for keywords, request_type in _REQUEST_TYPE_RULES:
        if any(kw in text_lower for kw in keywords):
            return request_type
    return "general"


# ---------------------------------------------------------------------------
# Skill conflict helpers (used in Task 2)
# ---------------------------------------------------------------------------

_SKILL_CONFLICT_STOP_WORDS = frozenset({
    "with", "from", "this", "that", "have", "been", "will",
    "your", "into", "they", "them", "then", "when", "what",
})
_SKILL_CONFLICT_MIN_WORD_LEN = 4


def _skill_words(text: str) -> set:
    cleaned = str(text).replace("-", " ").replace("_", " ").lower()
    return {
        w for w in cleaned.split()
        if len(w) >= _SKILL_CONFLICT_MIN_WORD_LEN and w not in _SKILL_CONFLICT_STOP_WORDS
    }
```

- [ ] **Step 5: Wire `_session_profiles` into `AgentDB.__init__`**

After `self._session_record_ids` (line ~508):

```python
self._session_profiles: dict[str, "SessionProfile"] = {}
```

After the existing embedding subscription block (around line 543):

```python
self.gateway.subscribe("conversation_started", self._handle_conversation_started_metadata)
```

Add handler method after `_handle_conversation_started_embedding`:

```python
    def _handle_conversation_started_metadata(self, **kwargs) -> None:
        from ..core.models import SessionProfile
        session_id = kwargs.get("session_id")
        if not session_id:
            return
        agent_id = kwargs.get("agent_id")
        opening_message = kwargs.get("opening_message", "")
        working_dirs = kwargs.get("working_dirs") or []
        repo = _extract_repo(working_dirs)
        rt = _classify_request_type(opening_message) if opening_message else "general"
        profile = SessionProfile(session_id=session_id, agent_id=agent_id)
        if repo:
            profile.repos.add(repo)
        profile.request_types.add(rt)
        self._session_profiles[session_id] = profile
```

- [ ] **Step 6: Expand profile in `write()`**

After `self._store.write(record)` (line 744), before the embedding block:

```python
        if session_id and session_id in self._session_profiles:
            _repo = _extract_repo(record.working_dirs)
            _rt = _classify_request_type(f"{key} {record.value!s}")
            _profile = self._session_profiles[session_id]
            if _repo:
                _profile.repos.add(_repo)
            _profile.request_types.add(_rt)
```

- [ ] **Step 7: Apply repo filter in `retrieve()`**

After the episodic scope filter block, before the embedding hydration block:

```python
        if session_id and session_id in self._session_profiles:
            _profile = self._session_profiles[session_id]
            if _profile.repos:
                candidates = [
                    r for r in candidates
                    if not r.working_dirs
                    or _extract_repo(r.working_dirs) in _profile.repos
                ]
```

- [ ] **Step 8: Run the new tests, then full suite**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_session_metadata.py -v 2>&1 | tail -20
```

Expected: all 12 tests pass. Then:

```bash
/Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q tests/unit
```

Expected: 357 passed (345 + 12).

- [ ] **Step 9: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  git add src/agentdb/core/models.py src/agentdb/interface/client.py \
          tests/unit/test_session_metadata.py && \
  git commit -m "feat: session metadata for cross-repo isolation — SessionProfile seeds at conversation_started, expands on write, gates retrieve"
```

---

## Task 2: Skill–memory conflict signal

**Files:**
- Modify: `src/agentdb/interface/client.py` — add `_check_skill_conflict()` method; call from `write()`
- Test: `tests/unit/test_skill_conflict_signal.py`

**Interfaces:**
- Consumes (Task 1 produces): `_extract_repo()`, `_skill_words()` — already in `client.py`
- Consumes: `self.skills.get_for_agent(agent_id) -> list[Skill]`; `self.gateway.dispatch("skill_review_needed", ...)`; `self._embedding_provider`, `self._embedding_store`, `self._raw_embeddings_cfg`
- Produces: `AgentDB._check_skill_conflict(record: MemoryRecord) -> None`; `skill_review_needed` event: `skill_id`, `record_id`, `record_key`, `agent_id`, `reason` (`"keyword_overlap"` | `"semantic_overlap"`), optional `similarity: float`
- Consumed by Task 3: `_check_skill_conflict()` called on LLM-synthesized canonicals after consolidation

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_skill_conflict_signal.py`:

```python
import pytest
from agentdb import AgentDB


def _db(tmp_path, embed_fn=None):
    return AgentDB(path=tmp_path / "test.db", embed_fn=embed_fn)


def test_keyword_overlap_fires_skill_review_needed(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table docs.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/analytics-schema-notes", "analytics schema changed",
             origin="agent_inferred", agent_id="agent1")
    assert len(fired) == 1
    assert fired[0]["skill_id"] == "analytics-schema"
    assert fired[0]["reason"] == "keyword_overlap"
    assert fired[0]["agent_id"] == "agent1"


def test_no_overlap_no_dispatch(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/meeting-notes", "standup went well",
             origin="agent_inferred", agent_id="agent1")
    assert fired == []


def test_short_words_do_not_match(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill("sql-db", "SQL DB", "Use proper joins.", scope=["agent1"])
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/anything", "some value", origin="agent_inferred", agent_id="agent1")
    assert fired == []


def test_entity_list_overlap_fires(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "orders-model", "Orders Model", "The orders table.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/task", "updated the model", origin="agent_inferred",
             agent_id="agent1", entities=["models/orders.sql", "orders"])
    assert len(fired) == 1
    assert fired[0]["skill_id"] == "orders-model"


def test_repo_segment_overlap_fires(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "agentdb-schema", "AgentDB Schema", "Governance rules.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/schema-note", "updated schema",
             origin="agent_inferred", agent_id="agent1",
             working_dirs=["/Users/x/agentdb"])
    assert len(fired) == 1
    assert fired[0]["skill_id"] == "agentdb-schema"


def test_no_dispatch_when_agent_id_none(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/analytics-note", "analytics ref",
             origin="agent_inferred", agent_id=None)
    assert fired == []


def test_archived_skill_does_not_fire(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table.", scope=["agent1"],
    )
    db.skills_api.archive_skill("analytics-schema", "1.0.0")
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/analytics-schema-task", "analytics ref",
             origin="agent_inferred", agent_id="agent1")
    assert fired == []


def test_out_of_scope_skill_does_not_fire(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table.", scope=["agent2"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/analytics-schema-task", "analytics ref",
             origin="agent_inferred", agent_id="agent1")
    assert fired == []


def test_dispatch_fires_at_most_once_per_skill(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue analytics schema.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/analytics-schema-notes", "analytics schema update",
             origin="agent_inferred", agent_id="agent1")
    assert len(fired) == 1


def test_no_semantic_check_without_provider(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill("topic-skill", "Topic Skill", "Some domain.", scope=["agent1"])
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/unrelated", "unrelated value",
             origin="agent_inferred", agent_id="agent1")
    assert [f for f in fired if f.get("reason") == "semantic_overlap"] == []


def test_semantic_overlap_fires_when_embeddings_active(tmp_path):
    from tests.fixtures.embeddings import controlled_embed
    db = AgentDB(path=tmp_path / "test.db", embed_fn=controlled_embed)
    db.skills_api.create_skill(
        "topic-skill", "Topic Skill", "topic_A domain content.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/similar_to_A-task", "similar_to_A value",
             origin="agent_inferred", agent_id="agent1")
    semantic_fires = [f for f in fired if f.get("reason") == "semantic_overlap"]
    assert len(semantic_fires) >= 1
    assert semantic_fires[0]["skill_id"] == "topic-skill"
    assert "similarity" in semantic_fires[0]


def test_keyword_and_semantic_fires_once_per_skill(tmp_path):
    from tests.fixtures.embeddings import controlled_embed
    db = AgentDB(path=tmp_path / "test.db", embed_fn=controlled_embed)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "topic_A domain.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/analytics-schema-similar_to_A", "similar_to_A ref",
             origin="agent_inferred", agent_id="agent1")
    assert len([f for f in fired if f["skill_id"] == "analytics-schema"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_skill_conflict_signal.py -v 2>&1 | tail -20
```

Expected: all 12 tests fail — `_check_skill_conflict` not yet defined.

- [ ] **Step 3: Add `_check_skill_conflict()` to `AgentDB`**

Add after `_handle_conversation_started_metadata`:

```python
    def _check_skill_conflict(self, record: "MemoryRecord") -> None:
        if not record.agent_id:
            return
        scoped_skills = self.skills.get_for_agent(record.agent_id)
        if not scoped_skills:
            return

        record_words = _skill_words(record.key)
        for entity in (record.entities or []):
            record_words.update(_skill_words(entity))
        repo = _extract_repo(record.working_dirs)
        if repo:
            record_words.update(_skill_words(repo))

        for skill in scoped_skills:
            skill_ws = _skill_words(f"{skill.id} {skill.name}")
            dispatched = False

            if skill_ws & record_words:
                try:
                    self.gateway.dispatch(
                        "skill_review_needed",
                        skill_id=skill.id,
                        record_id=record.id,
                        record_key=record.key,
                        agent_id=record.agent_id,
                        reason="keyword_overlap",
                    )
                except Exception:
                    pass
                dispatched = True

            if (
                not dispatched
                and self._embedding_provider is not None
                and self._embedding_store is not None
            ):
                try:
                    from ..embeddings.provider import _cosine
                    record_vec = self._embedding_store.get("memories", record.id)
                    skill_vec = self._embedding_store.get(
                        "skills", f"{skill.id}:{skill.version}"
                    )
                    if record_vec is not None and skill_vec is not None:
                        threshold = float(
                            self._raw_embeddings_cfg.get("skill_conflict_threshold", 0.70)
                        )
                        sim = _cosine(record_vec, skill_vec)
                        if sim >= threshold:
                            self.gateway.dispatch(
                                "skill_review_needed",
                                skill_id=skill.id,
                                record_id=record.id,
                                record_key=record.key,
                                agent_id=record.agent_id,
                                reason="semantic_overlap",
                                similarity=sim,
                            )
                except Exception:
                    pass
```

- [ ] **Step 4: Call `_check_skill_conflict()` from `write()`**

After the session profile expansion block, before the embedding block:

```python
        try:
            self._check_skill_conflict(record)
        except Exception:
            pass
```

- [ ] **Step 5: Run the new tests, then full suite**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_skill_conflict_signal.py -v 2>&1 | tail -20
```

Expected: all 12 tests pass. Then:

```bash
/Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q tests/unit
```

Expected: 369 passed (357 + 12).

- [ ] **Step 6: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  git add src/agentdb/interface/client.py tests/unit/test_skill_conflict_signal.py && \
  git commit -m "feat: dispatch skill_review_needed when agent write overlaps a scoped skill domain"
```

---

## Task 3: Memory consolidation

**Files:**
- Modify: `src/agentdb/core/models.py` — add `consolidated: bool` and `source_record_ids: list` to `MemoryRecord`
- Modify: `src/agentdb/core/store.py` — add migration columns; update `_row_to_record()` and `write()`
- Modify: `src/agentdb/governance/lifecycle_manager.py` — add `ConsolidationResult`; add `consolidation_state` table + state helpers; add `consolidate()` method
- Modify: `src/agentdb/interface/client.py` — update `_handle_conversation_started_metadata` with auto-trigger; add `include_consolidated` param to `retrieve()`; add `AgentDB.consolidate()` pass-through with skill conflict re-check
- Test: `tests/unit/test_memory_consolidation.py`

**Interfaces:**
- Consumes (Task 1): `_skill_words()`, `SessionProfile.agent_id` from `kwargs` in metadata handler
- Consumes (Task 2): `AgentDB._check_skill_conflict(record)` — called on LLM-synthesized canonicals
- Consumes: `self._store.archive_record(record_id)` (line 204 of `store.py`); `self._store.get_by_id(record_id)` (line 173); `self._store.write(record)` (line 134); `self._skill_store.get_for_agent(agent_id)` (LifecycleManager)
- Produces:
  - `MemoryRecord.consolidated: bool` — `True` on canonical records produced by consolidation
  - `MemoryRecord.source_record_ids: list[str]` — IDs of records merged into this canonical
  - `ConsolidationResult(clusters_found, records_merged, records_archived, skill_candidates, synthesized_record_ids: list[str])`
  - `LifecycleManager.consolidate(agent_id, since_hours, min_cluster_size, skill_candidate_threshold, llm_fn, gateway) -> ConsolidationResult`
  - `AgentDB.consolidate(agent_id, **kwargs) -> ConsolidationResult` — public method
  - `retrieve(include_consolidated: bool = True)` — new optional parameter

**Auto-trigger thresholds:** 7 days elapsed since last consolidation AND 10+ sessions since last consolidation. Both conditions required. Default when consolidation has never run: never auto-trigger (wait for 10 sessions first). Triggered from `_handle_conversation_started_metadata`; runs synchronously without `llm_fn` (fast path only — no LLM latency on session start).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_memory_consolidation.py`:

```python
import pytest
from datetime import datetime, timezone, timedelta
from agentdb import AgentDB


def _db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


# ── Core clustering and archival ─────────────────────────────────────────────

def test_consolidate_merges_near_duplicate_records(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    result = db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert result.clusters_found >= 1
    assert result.records_archived >= 1


def test_consolidate_archives_lower_confidence_records(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "low conf", origin="agent_inferred",
             agent_id="a1", confidence=0.4)
    db.write("memory/analytics-schema-update", "high conf", origin="agent_inferred",
             agent_id="a1", confidence=0.9)
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    active = [r for r in db._store.list_all() if r.agent_id == "a1"]
    assert all(r.confidence == 0.9 for r in active)


def test_canonical_marked_consolidated(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    active = [r for r in db._store.list_all() if r.agent_id == "a1"]
    assert len(active) == 1
    assert active[0].consolidated is True


def test_source_record_ids_traced_on_canonical(tmp_path):
    db = _db(tmp_path)
    r1 = db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    r2 = db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    active = [r for r in db._store.list_all() if r.agent_id == "a1"]
    assert len(active) == 1
    assert set(active[0].source_record_ids) == {r1.id, r2.id}


# ── Injection control ─────────────────────────────────────────────────────────

def test_include_consolidated_false_excludes_canonical(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    result = db.retrieve("analytics schema", agent_id="a1", include_consolidated=False)
    assert all(not r.consolidated for r in result.records)


def test_include_consolidated_true_returns_canonical_by_default(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    result = db.retrieve("analytics schema", agent_id="a1")
    assert any(r.consolidated for r in result.records)


# ── Skill candidate dispatch ──────────────────────────────────────────────────

def test_consolidate_dispatches_untracked_skill_candidate(tmp_path):
    db = _db(tmp_path)
    for i in range(5):
        db.write(f"memory/analytics-schema-record-{i}", f"value {i}",
                 origin="agent_inferred", agent_id="a1")
    fired = []
    db.gateway.subscribe("untracked_skill_candidate", lambda **kw: fired.append(kw))
    db.consolidate("a1", since_hours=0, min_cluster_size=2, skill_candidate_threshold=3)
    assert len(fired) >= 1
    assert "agent_id" in fired[0]
    assert "dominant_words" in fired[0]
    assert "sample_keys" in fired[0]


def test_consolidate_no_dispatch_when_skill_exists(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill("analytics-schema", "Analytics Schema", "Revenue.", scope=["a1"])
    for i in range(5):
        db.write(f"memory/analytics-schema-record-{i}", f"value {i}",
                 origin="agent_inferred", agent_id="a1")
    fired = []
    db.gateway.subscribe("untracked_skill_candidate", lambda **kw: fired.append(kw))
    db.consolidate("a1", since_hours=0, min_cluster_size=2, skill_candidate_threshold=3)
    assert fired == []


# ── Correctness and safety ────────────────────────────────────────────────────

def test_consolidate_human_approved_immune(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-policy", "authoritative rule",
             origin="human_approved", agent_id="a1")
    db.write("memory/analytics-schema-note", "agent observation",
             origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    human = [r for r in db._store.list_all() if r.origin == "human_approved"]
    assert len(human) == 1
    assert not human[0].archived


def test_consolidate_only_touches_agent_records(tmp_path):
    db = _db(tmp_path)
    db.write("memory/orders-model-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/orders-model-update", "v2", origin="agent_inferred", agent_id="other")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    other = [r for r in db._store.list_all() if r.agent_id == "other"]
    assert len(other) == 1


def test_consolidate_idempotent(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    r2 = db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert r2.records_archived == 0


# ── LLM synthesis ─────────────────────────────────────────────────────────────

def test_consolidate_with_llm_fn_synthesizes_value(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "old note", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "old update", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2,
                   llm_fn=lambda prompt: "synthesized canonical memory")
    active = [r for r in db._store.list_all() if not r.archived and r.agent_id == "a1"]
    assert any("synthesized" in str(r.value) for r in active)


def test_llm_canonical_triggers_skill_conflict_recheck(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue schema.", scope=["a1"],
    )
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    # Clear fires from write() calls above
    fired.clear()
    db.consolidate("a1", since_hours=0, min_cluster_size=2,
                   llm_fn=lambda prompt: "analytics schema synthesized")
    # The re-check on the synthesized canonical should fire again
    assert any(f.get("reason") == "keyword_overlap" for f in fired)


def test_non_llm_consolidation_does_not_recheck_skill_conflict(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue schema.", scope=["a1"],
    )
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    fired.clear()
    db.consolidate("a1", since_hours=0, min_cluster_size=2)  # no llm_fn
    assert fired == []


# ── Auto-trigger ──────────────────────────────────────────────────────────────

def test_auto_trigger_fires_after_threshold(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    # Backdate last_run_at to 8 days ago in the consolidation_state table
    with db._store._connect() as conn:
        from datetime import timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO consolidation_state (agent_id, last_run_at, session_count) VALUES (?, ?, ?)",
            ("a1", old_ts, 9),
        )
    # This 10th session_started should trigger auto-consolidation
    db.gateway.dispatch("conversation_started", session_id="s10", agent_id="a1",
                        opening_message="let's review")
    active = [r for r in db._store.list_all() if r.agent_id == "a1"]
    # If auto-trigger fired, cluster was consolidated → at most 1 active record
    assert len(active) <= 1


def test_auto_trigger_does_not_fire_before_threshold(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    # Only 5 sessions — under the 10-session threshold
    with db._store._connect() as conn:
        from datetime import timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO consolidation_state (agent_id, last_run_at, session_count) VALUES (?, ?, ?)",
            ("a1", old_ts, 4),
        )
    db.gateway.dispatch("conversation_started", session_id="s5", agent_id="a1",
                        opening_message="let's review")
    active = [r for r in db._store.list_all() if r.agent_id == "a1"]
    assert len(active) == 2  # untouched


def test_auto_trigger_resets_session_count(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    with db._store._connect() as conn:
        from datetime import timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO consolidation_state (agent_id, last_run_at, session_count) VALUES (?, ?, ?)",
            ("a1", old_ts, 9),
        )
    db.gateway.dispatch("conversation_started", session_id="s10", agent_id="a1",
                        opening_message="review")
    with db._store._connect() as conn:
        row = conn.execute(
            "SELECT session_count FROM consolidation_state WHERE agent_id = ?", ("a1",)
        ).fetchone()
    assert row is not None
    assert row["session_count"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_memory_consolidation.py -v 2>&1 | tail -20
```

Expected: all 18 tests fail — `AgentDB.consolidate` not defined; `MemoryRecord` has no `consolidated` or `source_record_ids`.

- [ ] **Step 3: Add new fields to `MemoryRecord` in `models.py`**

Add two fields to `MemoryRecord` after `working_dirs`:

```python
    consolidated: bool = False
    source_record_ids: list = field(default_factory=list)
```

- [ ] **Step 4: Update `store.py` for the new columns**

Add to `_MEMORIES_MIGRATION_COLUMNS`:

```python
    ("consolidated", "INTEGER NOT NULL DEFAULT 0"),
    ("source_record_ids", "TEXT NOT NULL DEFAULT '[]'"),
```

Update `_row_to_record()` — add after `working_dirs=...`:

```python
            consolidated=bool(row["consolidated"]) if "consolidated" in row.keys() else False,
            source_record_ids=json.loads(
                row["source_record_ids"] if "source_record_ids" in row.keys() else "[]"
            ),
```

Update `write()` INSERT — add `consolidated` and `source_record_ids` to the column list and values tuple:

```python
# In the INSERT OR REPLACE column list, add:
#   consolidated, source_record_ids
# In the VALUES tuple, add at the matching positions:
#   int(record.consolidated),
#   json.dumps(record.source_record_ids),
```

The exact positions must match the column list order. Find the `working_dirs` entry (last in the existing INSERT) and append after it. The INSERT statement, VALUES tuple, and `_row_to_record` must all stay in sync.

- [ ] **Step 5: Add `ConsolidationResult`, consolidation state helpers, and `consolidate()` to `lifecycle_manager.py`**

Add `ConsolidationResult` after imports (also add `from dataclasses import dataclass, field`):

```python
@dataclass
class ConsolidationResult:
    clusters_found: int = 0
    records_merged: int = 0
    records_archived: int = 0
    skill_candidates: int = 0
    synthesized_record_ids: list = field(default_factory=list)
```

Add `_lc_words()` and `_lc_cluster()` module-level functions (replicated locally to avoid circular imports with `client.py`):

```python
def _lc_words(text: str) -> set:
    _STOP = frozenset({"with", "from", "this", "that", "have", "been",
                       "will", "your", "into", "they", "them", "then", "when", "what"})
    cleaned = str(text).replace("/", " ").replace("-", " ").replace("_", " ").lower()
    return {w for w in cleaned.split() if len(w) >= 4 and w not in _STOP}


def _lc_cluster(records, min_overlap=2):
    clusters, assigned = [], set()
    for i, rec in enumerate(records):
        if i in assigned:
            continue
        group = [rec]
        assigned.add(i)
        rw = _lc_words(rec.key)
        for j, other in enumerate(records):
            if j <= i or j in assigned:
                continue
            if len(rw & _lc_words(other.key)) >= min_overlap:
                group.append(other)
                assigned.add(j)
        if len(group) >= 2:
            clusters.append(group)
    return clusters
```

Add consolidation state methods and `consolidate()` to `LifecycleManager`:

```python
    def _init_consolidation_table(self) -> None:
        with self._store._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS consolidation_state (
                    agent_id TEXT PRIMARY KEY,
                    last_run_at TEXT,
                    session_count INTEGER NOT NULL DEFAULT 0
                )
            """)

    def _increment_session_count(self, agent_id: str) -> None:
        with self._store._connect() as conn:
            conn.execute(
                """INSERT INTO consolidation_state (agent_id, session_count) VALUES (?, 1)
                   ON CONFLICT(agent_id) DO UPDATE SET session_count = session_count + 1""",
                (agent_id,),
            )

    def _should_auto_consolidate(self, agent_id: str, days: int = 7, min_sessions: int = 10) -> bool:
        from datetime import timedelta
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT last_run_at, session_count FROM consolidation_state WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if row is None or row["session_count"] < min_sessions:
            return False
        if row["last_run_at"] is None:
            return True
        cutoff = _utcnow() - timedelta(days=days)
        return datetime.fromisoformat(row["last_run_at"]) < cutoff

    def _reset_consolidation_state(self, agent_id: str) -> None:
        with self._store._connect() as conn:
            conn.execute(
                "UPDATE consolidation_state SET last_run_at = ?, session_count = 0 WHERE agent_id = ?",
                (_utcnow().isoformat(), agent_id),
            )

    def consolidate(
        self,
        agent_id: str,
        since_hours: int = 24,
        min_cluster_size: int = 2,
        skill_candidate_threshold: int = 5,
        llm_fn=None,
        gateway=None,
    ) -> "ConsolidationResult":
        from datetime import timedelta
        cutoff = _utcnow() - timedelta(hours=since_hours)
        all_records = [
            r for r in self._store.list_all()
            if r.agent_id == agent_id
            and not r.archived
            and _origin_value(r.origin) != "human_approved"
            and r.created_at <= cutoff
        ]
        clusters = _lc_cluster(all_records, min_overlap=2)
        result = ConsolidationResult(clusters_found=len(clusters))

        skill_word_sets = []
        if self._skill_store is not None:
            for skill in self._skill_store.get_for_agent(agent_id):
                skill_word_sets.append(_lc_words(f"{skill.id} {skill.name}"))

        for cluster in clusters:
            cluster.sort(key=lambda r: r.confidence, reverse=True)
            canonical = cluster[0]
            to_archive = cluster[1:]

            canonical.consolidated = True
            canonical.source_record_ids = [r.id for r in cluster]

            if llm_fn is not None:
                try:
                    prompt = (
                        f"Synthesize one canonical memory from these {len(cluster)} records:\n"
                        + "\n".join(f"- {r.key}: {r.value}" for r in cluster)
                        + "\nReturn only the synthesized content, no explanation."
                    )
                    canonical.value = llm_fn(prompt)
                    result.synthesized_record_ids.append(canonical.id)
                except Exception:
                    pass

            try:
                self._store.write(canonical)
            except Exception:
                pass

            for rec in to_archive:
                try:
                    self._store.archive_record(rec.id)
                    result.records_archived += 1
                    result.records_merged += 1
                except Exception:
                    pass

            if gateway is not None and len(cluster) >= skill_candidate_threshold:
                cluster_words = _lc_words(" ".join(r.key for r in cluster))
                governed = any(len(sw & cluster_words) >= 1 for sw in skill_word_sets)
                if not governed:
                    try:
                        gateway.dispatch(
                            "untracked_skill_candidate",
                            agent_id=agent_id,
                            dominant_words=list(cluster_words)[:10],
                            record_count=len(cluster),
                            sample_keys=[r.key for r in cluster[:3]],
                        )
                        result.skill_candidates += 1
                    except Exception:
                        pass

        return result
```

Call `_init_consolidation_table()` from `LifecycleManager.__init__`, after the existing assignments:

```python
        self._init_consolidation_table()
```

- [ ] **Step 6: Update `_handle_conversation_started_metadata` in `client.py` for auto-trigger**

Add to the handler after building and storing the `SessionProfile`:

```python
        # Auto-consolidation: increment session count; trigger if threshold reached
        if agent_id:
            try:
                self.lifecycle._increment_session_count(agent_id)
                if self.lifecycle._should_auto_consolidate(agent_id):
                    self.consolidate(agent_id)  # no llm_fn — synchronous fast path
            except Exception:
                pass
```

- [ ] **Step 7: Add `include_consolidated` to `retrieve()`**

Add parameter to `retrieve()` signature:

```python
    def retrieve(
        self,
        query: str,
        agent_id: str,
        ...
        include_consolidated: bool = True,
    ) -> RetrievalResult:
```

Add filter after the episodic scope filter block, before the repo filter block:

```python
        if not include_consolidated:
            candidates = [r for r in candidates if not r.consolidated]
```

- [ ] **Step 8: Add `AgentDB.consolidate()` pass-through**

Add after the `delete()` method:

```python
    def consolidate(self, agent_id: str, llm_fn=None, **kwargs) -> "ConsolidationResult":
        """Cluster near-duplicate records, archive lower-confidence copies, mark canonical as
        consolidated with provenance trace. Dispatches untracked_skill_candidate when a cluster
        exceeds the threshold and no scoped skill governs the domain. If llm_fn is provided,
        synthesizes canonical content and re-checks skill conflicts on the result."""
        result = self.lifecycle.consolidate(
            agent_id, gateway=self.gateway, llm_fn=llm_fn, **kwargs
        )
        try:
            self.lifecycle._reset_consolidation_state(agent_id)
        except Exception:
            pass
        # Re-check skill conflicts for LLM-synthesized canonicals only
        if llm_fn is not None:
            for record_id in result.synthesized_record_ids:
                try:
                    canonical = self._store.get_by_id(record_id)
                    if canonical:
                        self._check_skill_conflict(canonical)
                except Exception:
                    pass
        return result
```

- [ ] **Step 9: Run the new tests, then full suite**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_memory_consolidation.py -v 2>&1 | tail -25
```

Expected: all 18 tests pass. Then:

```bash
/Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q tests/unit
```

Expected: 387 passed (369 + 18).

- [ ] **Step 10: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  git add src/agentdb/core/models.py src/agentdb/core/store.py \
          src/agentdb/governance/lifecycle_manager.py \
          src/agentdb/interface/client.py \
          tests/unit/test_memory_consolidation.py && \
  git commit -m "feat: memory consolidation — auto-trigger at 7d+10 sessions, consolidated flag, source trace, injection control, skill conflict re-check on LLM synthesis"
```
