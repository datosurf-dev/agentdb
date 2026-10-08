# Session Metadata, Skill Conflict Signal, and Memory Consolidation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three capabilities that complete the arbitration loop: (1) structured session metadata for cross-repo isolation, (2) skill–memory conflict signal using metadata-aware overlap detection, (3) memory consolidation that distills repeated patterns into canonical records and surfaces untracked skill candidates.

**Architecture:** All changes land in existing files — no new source files, no new dependencies. `SessionProfile` is a lightweight in-memory dataclass stored in `AgentDB._session_profiles`. Session classification is rule-based (no LLM, no embeddings required). Consolidation clusters by keyword overlap and archives lower-confidence duplicates; an optional `llm_fn=` callable enables content synthesis. Skill candidates are dispatched via `db.gateway` when a cluster exceeds a size threshold and no scoped skill governs the domain.

**Tech Stack:** Python 3.11+, existing `MemoryStore`, `LifecycleManager`, `SkillStore`, `Gateway`.

**Spec:** `docs/context/architecture-principles.md` (Principles 1a, 4a, 7, 12b), `docs/context/capabilities.md`.

## Global Constraints

- Test runner: `cd /Users/jm-nx/documents/github/agentdb && /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q tests/unit`
- Baseline: 345 unit tests pass. Every task must leave the suite green.
- No new source files. One new test file per task.
- No new dependencies.
- No `Co-Authored-By` trailers on commits.
- Every gateway dispatch wraps in `try/except Exception: pass` — never propagates to callers.
- `human_approved` records are immune to all archival operations, including consolidation.

## Review Focus

1. **Multi-repo session pollution** — a session that writes to repo A then repo B must retrieve from both, not just the most recent. Test that `session.repos` grows additively and the filter allows records from all repos touched.
2. **Records with no `working_dirs`** — org-scoped records and records written before this feature shipped have empty `working_dirs`. They must always pass the repo filter, never be excluded.
3. **Consolidation and `human_approved` immunity** — the consolidation algorithm must check `origin == "human_approved"` and skip those records entirely, even if they cluster with agent-inferred records.
4. **Double dispatch in skill conflict** — if keyword AND semantic overlap both match the same skill in a single write, only one `skill_review_needed` fires for that skill. The `dispatched` flag guards this; test with a controlled embedding fixture.
5. **Consolidation idempotency** — running `consolidate()` twice on the same agent should not archive the already-archived records from the first pass. The filter `not r.archived` handles this; test it explicitly.

---

## Task 1: Session metadata and repo filter

**Files:**
- Modify: `src/agentdb/core/models.py` — add `SessionProfile` dataclass
- Modify: `src/agentdb/interface/client.py` — add module-level `_extract_repo()` and `_classify_request_type()`; add `_session_profiles` to `__init__`; add `_handle_conversation_started_metadata` subscriber; expand profile in `write()`; apply repo filter in `retrieve()`
- Test: `tests/unit/test_session_metadata.py`

**Interfaces:**
- Produces:
  - `SessionProfile` dataclass in `models.py`: `session_id: str`, `repos: set[str]`, `request_types: set[str]`
  - `AgentDB._session_profiles: dict[str, SessionProfile]` — in-memory, same lifetime as `_session_record_ids`
  - `_extract_repo(working_dirs: list[str]) -> str | None` — module-level in `client.py`
  - `_classify_request_type(text: str) -> str` — module-level in `client.py`, returns one of: `code_review`, `code_generation`, `debugging`, `documentation`, `research`, `planning`, `general`
  - `conversation_started` dispatch now accepts optional `working_dirs: list[str]` kwarg (additive — existing callers unaffected)

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
        opening_message="review this pull request",
        working_dirs=["/Users/x/agentdb"],
    )
    profile = db._session_profiles.get("s1")
    assert profile is not None
    assert "agentdb" in profile.repos
    assert "code_review" in profile.request_types


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
    db.gateway.dispatch("conversation_started", session_id="s1",
                        opening_message="review PR", working_dirs=["/Users/x/agentdb"])
    db.write("memory/task", "val", origin="agent_inferred", agent_id="a1",
             session_id="s1", working_dirs=["/Users/x/dbt"])
    profile = db._session_profiles["s1"]
    assert "agentdb" in profile.repos
    assert "dbt" in profile.repos


def test_request_types_expand_on_write(tmp_path):
    db = _db(tmp_path)
    db.gateway.dispatch("conversation_started", session_id="s1",
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
    # Write a record in repo "dbt"
    db.write("memory/dbt-task", "dbt value", origin="agent_inferred",
             agent_id="a1", working_dirs=["/Users/x/dbt"])
    # Start session in repo "agentdb"
    db.gateway.dispatch("conversation_started", session_id="s1",
                        opening_message="implement feature",
                        working_dirs=["/Users/x/agentdb"])
    result = db.retrieve("task", agent_id="a1", session_id="s1")
    keys = [r.key for r in result.records]
    assert "memory/dbt-task" not in keys


def test_repo_filter_passes_records_with_no_working_dirs(tmp_path):
    db = _db(tmp_path)
    db.write("memory/org/policy", "always true", origin="human_approved", agent_id="a1")
    db.gateway.dispatch("conversation_started", session_id="s1",
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
    # Session starts in agentdb, then expands to dbt via a write
    db.gateway.dispatch("conversation_started", session_id="s1",
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

Expected: all tests fail — `_extract_repo`, `_classify_request_type` not yet importable; `_session_profiles` not on `AgentDB`.

- [ ] **Step 3: Add `SessionProfile` to `models.py`**

Append after the `Skill` dataclass:

```python
@dataclass
class SessionProfile:
    session_id: str
    repos: set = field(default_factory=set)
    request_types: set = field(default_factory=set)
```

- [ ] **Step 4: Add module-level helpers to `client.py`**

Add after the `EVENT_*` constants block (before the `@dataclass AuditEntry`):

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
```

- [ ] **Step 5: Wire `_session_profiles` into `AgentDB.__init__`**

Add after the `self._session_record_ids` assignment (line ~508):

```python
self._session_profiles: dict[str, "SessionProfile"] = {}
```

Register the metadata handler unconditionally (after the existing conditional embedding subscription, around line 543):

```python
self.gateway.subscribe("conversation_started", self._handle_conversation_started_metadata)
```

Add the handler method (after `_handle_conversation_started_embedding`):

```python
def _handle_conversation_started_metadata(self, **kwargs) -> None:
    from ..core.models import SessionProfile
    session_id = kwargs.get("session_id")
    if not session_id:
        return
    opening_message = kwargs.get("opening_message", "")
    working_dirs = kwargs.get("working_dirs") or []
    repo = _extract_repo(working_dirs)
    rt = _classify_request_type(opening_message) if opening_message else "general"
    profile = SessionProfile(session_id=session_id)
    if repo:
        profile.repos.add(repo)
    profile.request_types.add(rt)
    self._session_profiles[session_id] = profile
```

- [ ] **Step 6: Expand profile in `write()`**

Add after `self._store.write(record)` (line 744), before the embedding block:

```python
        # Expand session profile with this write's context
        if session_id and session_id in self._session_profiles:
            _repo = _extract_repo(record.working_dirs)
            _rt = _classify_request_type(f"{key} {record.value!s}")
            _profile = self._session_profiles[session_id]
            if _repo:
                _profile.repos.add(_repo)
            _profile.request_types.add(_rt)
```

- [ ] **Step 7: Apply repo filter in `retrieve()`**

Add after the episodic scope filter block (the `if session_id is not None:` block at line ~856), before `if _query_passes_gate and self._embedding_store is not None:`:

```python
        # Repo filter — exclude records written in repos the session has never touched
        if session_id and session_id in self._session_profiles:
            _profile = self._session_profiles[session_id]
            if _profile.repos:
                candidates = [
                    r for r in candidates
                    if not r.working_dirs
                    or _extract_repo(r.working_dirs) in _profile.repos
                ]
```

- [ ] **Step 8: Run the new tests**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_session_metadata.py -v 2>&1 | tail -25
```

Expected: all 12 tests pass.

- [ ] **Step 9: Run full suite**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q tests/unit
```

Expected: 357 passed (345 + 12).

- [ ] **Step 10: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  git add src/agentdb/core/models.py src/agentdb/interface/client.py \
          tests/unit/test_session_metadata.py && \
  git commit -m "feat: session metadata for cross-repo isolation — SessionProfile seeds at conversation_started, expands on write, gates retrieve"
```

---

## Task 2: Skill–memory conflict signal

**Files:**
- Modify: `src/agentdb/interface/client.py` — add `_SKILL_CONFLICT_STOP_WORDS`, `_SKILL_CONFLICT_MIN_WORD_LEN`, `_skill_words()` module-level helpers; add `_check_skill_conflict()` method; call from `write()`
- Test: `tests/unit/test_skill_conflict_signal.py`

**Interfaces:**
- Consumes (Task 1 produces): `_extract_repo()` — used to add repo segment to `_record_words` so a skill named "agentdb-schema" matches records written in the `agentdb` working dir
- Consumes: `self.skills.get_for_agent(agent_id) -> list[Skill]`; `self.gateway.dispatch("skill_review_needed", ...)`; `self._embedding_provider`, `self._embedding_store`
- Produces: `AgentDB._check_skill_conflict(record: MemoryRecord) -> None`; `skill_review_needed` event with kwargs: `skill_id`, `record_id`, `record_key`, `agent_id`, `reason` (`"keyword_overlap"` or `"semantic_overlap"`), optional `similarity: float`

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
    # "sql" (3 chars) and "db" (2 chars) are below MIN_WORD_LEN=4 — no match possible
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
    # Skill "agentdb-schema" should match a record written in the "agentdb" working dir
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "agentdb-schema", "AgentDB Schema", "The schema governance rules.", scope=["agent1"],
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
    # "analytics" AND "schema" both match — still only one dispatch per skill
    assert len(fired) == 1


def test_no_semantic_check_without_provider(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill("topic-skill", "Topic Skill", "Some domain.", scope=["agent1"])
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/unrelated", "unrelated value",
             origin="agent_inferred", agent_id="agent1")
    semantic_fires = [f for f in fired if f.get("reason") == "semantic_overlap"]
    assert semantic_fires == []


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
    skill_fires = [f for f in fired if f["skill_id"] == "analytics-schema"]
    assert len(skill_fires) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_skill_conflict_signal.py -v 2>&1 | tail -20
```

Expected: all 12 tests fail — `_check_skill_conflict` not yet defined.

- [ ] **Step 3: Add module-level constants and helpers to `client.py`**

Add after the session metadata helpers block (Task 1), before `@dataclass AuditEntry`:

```python
# ---------------------------------------------------------------------------
# Skill conflict helpers
# ---------------------------------------------------------------------------

_SKILL_CONFLICT_STOP_WORDS = frozenset({
    "with", "from", "this", "that", "have", "been", "will",
    "your", "into", "they", "them", "then", "when", "what",
})
_SKILL_CONFLICT_MIN_WORD_LEN = 4


def _skill_words(text: str) -> set:
    cleaned = text.replace("-", " ").replace("_", " ").lower()
    return {
        w for w in cleaned.split()
        if len(w) >= _SKILL_CONFLICT_MIN_WORD_LEN and w not in _SKILL_CONFLICT_STOP_WORDS
    }
```

- [ ] **Step 4: Add `_check_skill_conflict()` to `AgentDB`**

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
        # Include the repo segment from working_dirs so a skill named
        # "agentdb-schema" matches records written in the "agentdb" dir
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

- [ ] **Step 5: Call `_check_skill_conflict()` from `write()`**

Add after the session profile expansion block (Task 1's addition), before the embedding block:

```python
        try:
            self._check_skill_conflict(record)
        except Exception:
            pass
```

- [ ] **Step 6: Run the new tests**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_skill_conflict_signal.py -v 2>&1 | tail -25
```

Expected: all 12 tests pass.

- [ ] **Step 7: Run full suite**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q tests/unit
```

Expected: 369 passed (357 + 12).

- [ ] **Step 8: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  git add src/agentdb/interface/client.py tests/unit/test_skill_conflict_signal.py && \
  git commit -m "feat: dispatch skill_review_needed when agent write overlaps a scoped skill domain"
```

---

## Task 3: Memory consolidation

**Files:**
- Modify: `src/agentdb/governance/lifecycle_manager.py` — add `consolidate()` method and `ConsolidationResult` dataclass
- Modify: `src/agentdb/interface/client.py` — add `consolidate()` pass-through on `AgentDB`; pass `gateway` into lifecycle's consolidate call
- Test: `tests/unit/test_memory_consolidation.py`

**Interfaces:**
- Consumes (Task 1 produces): `_skill_words()` — replicated locally in `lifecycle_manager.py` as a module-level `_lc_words()` (4-line function; not imported from `client.py` to avoid circular imports)
- Consumes: `self._store.list_all()` filtered by `agent_id`; `self._store.archive(record_id)` (find exact method name in `MemoryStore` — likely `archive`, `archive_record`, or `delete_by_key` with archive flag); `self._skill_store.get_for_agent(agent_id)`
- Produces:
  - `ConsolidationResult` dataclass in `lifecycle_manager.py`: `clusters_found: int`, `records_merged: int`, `records_archived: int`, `skill_candidates: int`
  - `LifecycleManager.consolidate(agent_id, since_hours=24, min_cluster_size=2, skill_candidate_threshold=5, llm_fn=None, gateway=None) -> ConsolidationResult`
  - `AgentDB.consolidate(agent_id, **kwargs) -> ConsolidationResult` — pass-through that injects `gateway=self.gateway`

**Clustering algorithm:**

```python
def _lc_words(text: str) -> set:
    _STOP = frozenset({"with", "from", "this", "that", "have", "been", "will",
                       "your", "into", "they", "them", "then", "when", "what"})
    cleaned = str(text).replace("/", " ").replace("-", " ").replace("_", " ").lower()
    return {w for w in cleaned.split() if len(w) >= 4 and w not in _STOP}


def _cluster(records, min_overlap=2):
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

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_memory_consolidation.py`:

```python
import pytest
from datetime import datetime, timezone, timedelta
from agentdb import AgentDB


def _db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


def _old(hours=25):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def test_consolidate_merges_near_duplicate_records(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    # Backdate both to be older than since_hours=0
    result = db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert result.clusters_found >= 1
    assert result.records_archived >= 1


def test_consolidate_archives_lower_confidence_records(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "low conf", origin="agent_inferred",
             agent_id="a1", confidence=0.4)
    db.write("memory/analytics-schema-update", "high conf", origin="agent_inferred",
             agent_id="a1", confidence=0.9)
    result = db.consolidate("a1", since_hours=0, min_cluster_size=2)
    # The lower-confidence record should be archived
    all_records = db._store.list_all()
    non_archived = [r for r in all_records if not r.archived and r.agent_id == "a1"]
    confidences = [r.confidence for r in non_archived]
    if confidences:
        assert max(confidences) == 0.9


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
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue schema.", scope=["a1"],
    )
    for i in range(5):
        db.write(f"memory/analytics-schema-record-{i}", f"value {i}",
                 origin="agent_inferred", agent_id="a1")
    fired = []
    db.gateway.subscribe("untracked_skill_candidate", lambda **kw: fired.append(kw))
    db.consolidate("a1", since_hours=0, min_cluster_size=2, skill_candidate_threshold=3)
    assert fired == []


def test_consolidate_returns_correct_counts(tmp_path):
    db = _db(tmp_path)
    db.write("memory/orders-model-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/orders-model-update", "v2", origin="agent_inferred", agent_id="a1")
    result = db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert result.clusters_found >= 1
    assert result.records_archived == result.records_merged


def test_consolidate_only_touches_agent_records(tmp_path):
    db = _db(tmp_path)
    db.write("memory/orders-model-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/orders-model-update", "v2", origin="agent_inferred", agent_id="other")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    other_records = [r for r in db._store.list_all()
                     if r.agent_id == "other" and not r.archived]
    assert len(other_records) == 1


def test_consolidate_human_approved_immune(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-policy", "authoritative rule",
             origin="human_approved", agent_id="a1")
    db.write("memory/analytics-schema-note", "agent observation",
             origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    human_records = [r for r in db._store.list_all()
                     if r.origin == "human_approved" and not r.archived]
    assert len(human_records) == 1


def test_consolidate_idempotent(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    r1 = db.consolidate("a1", since_hours=0, min_cluster_size=2)
    r2 = db.consolidate("a1", since_hours=0, min_cluster_size=2)
    # Second run sees already-archived records — no new archival
    assert r2.records_archived == 0


def test_consolidate_with_llm_fn_updates_canonical_value(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "old note", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "old update", origin="agent_inferred", agent_id="a1")

    def fake_llm(prompt: str) -> str:
        return "synthesized canonical memory"

    db.consolidate("a1", since_hours=0, min_cluster_size=2, llm_fn=fake_llm)
    active = [r for r in db._store.list_all() if not r.archived and r.agent_id == "a1"]
    values = [str(r.value) for r in active]
    assert any("synthesized" in v for v in values)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_memory_consolidation.py -v 2>&1 | tail -20
```

Expected: all 9 tests fail — `AgentDB.consolidate` not defined.

- [ ] **Step 3: Add `ConsolidationResult` and `consolidate()` to `lifecycle_manager.py`**

Add the `ConsolidationResult` dataclass after the imports:

```python
from dataclasses import dataclass, field

@dataclass
class ConsolidationResult:
    clusters_found: int = 0
    records_merged: int = 0
    records_archived: int = 0
    skill_candidates: int = 0
```

Add `_lc_words()` as a module-level function:

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

Add `consolidate()` to `LifecycleManager`:

```python
    def consolidate(
        self,
        agent_id: str,
        since_hours: int = 24,
        min_cluster_size: int = 2,
        skill_candidate_threshold: int = 5,
        llm_fn=None,
        gateway=None,
    ) -> ConsolidationResult:
        from datetime import timedelta
        cutoff = _utcnow() - timedelta(hours=since_hours)
        all_records = [
            r for r in self._store.list_all()
            if r.agent_id == agent_id
            and not r.archived
            and r.origin != "human_approved"
            and r.created_at <= cutoff
        ]
        clusters = _lc_cluster(all_records, min_overlap=2)
        result = ConsolidationResult(clusters_found=len(clusters))

        # Get scoped skill words for candidate suppression
        skill_word_sets = []
        if self._skill_store is not None:
            for skill in self._skill_store.get_for_agent(agent_id):
                skill_word_sets.append(_lc_words(f"{skill.id} {skill.name}"))

        for cluster in clusters:
            cluster.sort(key=lambda r: r.confidence, reverse=True)
            canonical = cluster[0]
            to_archive = cluster[1:]

            if llm_fn is not None:
                try:
                    prompt = (
                        f"Synthesize one canonical memory from these {len(cluster)} records:\n"
                        + "\n".join(f"- {r.key}: {r.value}" for r in cluster)
                        + "\nReturn only the synthesized content."
                    )
                    canonical.value = llm_fn(prompt)
                    self._store.write(canonical)
                except Exception:
                    pass

            for rec in to_archive:
                try:
                    # Use the MemoryStore archival method — check exact name: archive, archive_record, or delete_by_key
                    self._store.archive(rec.id)
                    result.records_archived += 1
                    result.records_merged += 1
                except Exception:
                    pass

            # Dispatch untracked_skill_candidate if cluster is large and no skill governs it
            if gateway is not None and len(cluster) >= skill_candidate_threshold:
                cluster_words = _lc_words(" ".join(r.key for r in cluster))
                governed = any(
                    len(sw & cluster_words) >= 1 for sw in skill_word_sets
                )
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

**Implementation note:** Read `MemoryStore` to find the exact archive method name before implementing the `self._store.archive(rec.id)` call. Common candidates: `archive(id)`, `archive_record(id)`, `mark_archived(id)`. If none exists, add `archive(self, record_id: str)` to `MemoryStore` that sets `archived=True` and writes back.

- [ ] **Step 4: Add `consolidate()` pass-through to `AgentDB`**

Add after the `delete()` method:

```python
    def consolidate(self, agent_id: str, **kwargs) -> "ConsolidationResult":
        """Cluster and merge near-duplicate records for agent_id, dispatch untracked_skill_candidate
        for large clusters with no governing skill. Returns a ConsolidationResult summary."""
        return self.lifecycle.consolidate(agent_id, gateway=self.gateway, **kwargs)
```

- [ ] **Step 5: Run the new tests**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_memory_consolidation.py -v 2>&1 | tail -25
```

Expected: all 9 tests pass.

- [ ] **Step 6: Run full suite**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q tests/unit
```

Expected: 378 passed (369 + 9).

- [ ] **Step 7: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  git add src/agentdb/governance/lifecycle_manager.py src/agentdb/interface/client.py \
          tests/unit/test_memory_consolidation.py && \
  git commit -m "feat: memory consolidation — cluster near-duplicates, archive lower-confidence records, dispatch untracked_skill_candidate"
```
