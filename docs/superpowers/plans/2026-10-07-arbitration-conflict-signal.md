# Skill–Memory Conflict Signal

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dispatch `skill_review_needed` via `db.gateway` when an agent writes a memory record that touches a domain governed by one of its scoped skills — completing the arbitration loop between agent inference and human intent.

**Architecture:** One new private method `_check_skill_conflict(record)` on `AgentDB`, called at the end of `write()`. Phase 1 uses keyword overlap (skill name/ID words vs. record key words and entity list); Phase 3 adds cosine similarity when an embedding provider is active. Both phases dispatch the same `skill_review_needed` trigger with a `reason` field (`"keyword_overlap"` or `"semantic_overlap"`) so consumers can filter by confidence. No new files, no new dependencies.

**Tech Stack:** Python 3.11+, existing `SkillStore`, `Gateway`, `EmbeddingStore` (optional).

**Spec:** `docs/context/architecture-principles.md` (Principles 4a, 12b), `docs/context/capabilities.md` (skill–memory conflict signal gap).

## Global Constraints

- Test runner: `cd /Users/jm-nx/documents/github/agentdb && /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q tests/unit`
- Baseline: 345 unit tests pass. Every task must leave the suite green.
- No new files, no new dependencies, no new public API surface on `AgentDB`.
- `_check_skill_conflict` is private — never part of the public interface.
- The dispatch must never propagate exceptions to `write()` callers — wrap in `try/except`.
- `skill_review_needed` must fire at most once per skill per write, even if both keyword and semantic checks both match.
- Keyword check: overlap between non-trivial words (≥4 chars, not in stop-list) in skill name/ID and record key segments + entity list. Must not fire on trivially short words.
- No `Co-Authored-By` trailers on commits.

## Review Focus

1. **Stop-word false positives** — words like "data", "base", "code" appear in many skill names and record keys. The minimum-word-length check (≥4 chars) helps but is not sufficient; test that a skill named "analytics-schema" does NOT fire on a record keyed `memory/working/some_data_note` unless "analytics" or "schema" appears in it.
2. **Double dispatch** — if keyword AND semantic both match, only one `skill_review_needed` should fire per skill per write. Test this with a controlled embedding fixture.
3. **No provider, no semantic check** — when `_embedding_provider is None`, the semantic branch must not execute at all. Test by verifying no embedding calls happen.
4. **Archived skills excluded** — `get_for_agent()` only returns non-archived skills; test that an archived skill does not trigger the signal.
5. **`agent_id=None` writes skip the check** — anonymous writes have no scoped skills to check against. Test that `_check_skill_conflict` returns early and dispatches nothing.

---

## Task 1: Skill–memory conflict signal

**Files:**
- Modify: `src/agentdb/interface/client.py` — add `_check_skill_conflict()` method; call it from `write()`
- Test: `tests/unit/test_skill_conflict_signal.py` — new test file

**Interfaces:**
- Consumes:
  - `self.skills.get_for_agent(agent_id) -> list[Skill]` — already exists
  - `self.gateway.dispatch("skill_review_needed", ...)` — already exists
  - `self._embedding_provider`, `self._embedding_store`, `self._raw_embeddings_cfg` — already on `AgentDB`
  - `_cosine` from `agentdb.embeddings.provider` — already exists
- Produces:
  - `AgentDB._check_skill_conflict(record: MemoryRecord) -> None` — private method
  - `db.gateway` event: `"skill_review_needed"` with kwargs: `skill_id`, `record_id`, `record_key`, `agent_id`, `reason` (`"keyword_overlap"` or `"semantic_overlap"`), optional `similarity: float`

**Keyword overlap algorithm:**
```python
STOP_WORDS = frozenset({"with", "from", "this", "that", "have", "been", "will", "your", "into"})
MIN_WORD_LEN = 4

def _skill_words(skill: Skill) -> set[str]:
    raw = f"{skill.id} {skill.name}".replace("-", " ").replace("_", " ").lower()
    return {w for w in raw.split() if len(w) >= MIN_WORD_LEN and w not in STOP_WORDS}

def _record_words(record: MemoryRecord) -> set[str]:
    raw = record.key.replace("/", " ").replace("-", " ").replace("_", " ").lower()
    entity_str = " ".join(record.entities or []).replace("/", " ").replace("-", " ").lower()
    combined = f"{raw} {entity_str}"
    return {w for w in combined.split() if len(w) >= MIN_WORD_LEN and w not in STOP_WORDS}
```

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_skill_conflict_signal.py`:

```python
import pytest
from agentdb import AgentDB


def _db(tmp_path, embed_fn=None):
    return AgentDB(path=tmp_path / "test.db", embed_fn=embed_fn)


# ── Keyword overlap ──────────────────────────────────────────────────────────

def test_keyword_overlap_fires_skill_review_needed(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "The revenue table has date, amount_usd.",
        scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))

    db.write(
        "memory/working/analytics-schema-notes",
        "The analytics schema changed today.",
        origin="agent_inferred",
        agent_id="agent1",
    )
    assert len(fired) == 1
    assert fired[0]["skill_id"] == "analytics-schema"
    assert fired[0]["reason"] == "keyword_overlap"
    assert fired[0]["agent_id"] == "agent1"


def test_no_overlap_no_dispatch(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table docs.",
        scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))

    db.write(
        "memory/working/meeting-notes",
        "Standup went well.",
        origin="agent_inferred",
        agent_id="agent1",
    )
    assert fired == []


def test_short_words_do_not_match(tmp_path):
    # Skill "sql-db" — "sql" is 3 chars, "db" is 2 chars — neither meets min_word_len=4
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "sql-db", "SQL DB", "Use proper joins.",
        scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))

    db.write(
        "memory/working/anything-here",
        "Some value",
        origin="agent_inferred",
        agent_id="agent1",
    )
    assert fired == []


def test_entity_list_overlap_fires(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "orders-model", "Orders Model", "The orders table joins on customer_id.",
        scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))

    db.write(
        "memory/working/task",
        "Updated the model.",
        origin="agent_inferred",
        agent_id="agent1",
        entities=["models/orders.sql", "orders"],
    )
    assert len(fired) == 1
    assert fired[0]["skill_id"] == "orders-model"


def test_no_dispatch_when_agent_id_none(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table.",
        scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))

    # write() with no agent_id — should not dispatch
    db.write(
        "memory/working/analytics-note",
        "analytics schema ref",
        origin="agent_inferred",
        agent_id=None,
    )
    assert fired == []


def test_archived_skill_does_not_fire(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table.",
        scope=["agent1"],
    )
    db.skills_api.archive_skill("analytics-schema", "1.0.0")
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))

    db.write(
        "memory/working/analytics-schema-task",
        "analytics schema ref",
        origin="agent_inferred",
        agent_id="agent1",
    )
    assert fired == []


def test_out_of_scope_skill_does_not_fire(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table.",
        scope=["agent2"],  # scoped to a different agent
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))

    db.write(
        "memory/working/analytics-schema-task",
        "analytics schema ref",
        origin="agent_inferred",
        agent_id="agent1",
    )
    assert fired == []


def test_dispatch_fires_at_most_once_per_skill(tmp_path):
    # Even with multiple overlapping words, fires once per skill
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue analytics schema.",
        scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))

    db.write(
        "memory/working/analytics-schema-notes",
        "analytics schema update",
        origin="agent_inferred",
        agent_id="agent1",
    )
    # "analytics" AND "schema" both match — but only one dispatch per skill
    assert len(fired) == 1


# ── Semantic overlap (with embeddings active) ────────────────────────────────

def test_semantic_overlap_fires_when_embeddings_active(tmp_path):
    from tests.fixtures.embeddings import controlled_embed
    db = AgentDB(path=tmp_path / "test.db", embed_fn=controlled_embed)
    db.skills_api.create_skill(
        "topic-skill", "Topic Skill", "topic_A domain content.",
        scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))

    # "similar_to_A" key → controlled_embed returns a vector cosine ≈ 0.9999 with topic_A
    db.write(
        "memory/working/similar_to_A-task",
        "similar_to_A value",
        origin="agent_inferred",
        agent_id="agent1",
    )
    semantic_fires = [f for f in fired if f.get("reason") == "semantic_overlap"]
    assert len(semantic_fires) >= 1
    assert semantic_fires[0]["skill_id"] == "topic-skill"
    assert "similarity" in semantic_fires[0]


def test_no_semantic_check_without_provider(tmp_path):
    # Without embed_fn, no embedding provider → semantic branch never runs
    db = _db(tmp_path)  # no embed_fn
    db.skills_api.create_skill(
        "topic-skill", "Topic Skill", "Some domain content.",
        scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))

    db.write(
        "memory/working/unrelated-task",
        "unrelated value",
        origin="agent_inferred",
        agent_id="agent1",
    )
    semantic_fires = [f for f in fired if f.get("reason") == "semantic_overlap"]
    assert semantic_fires == []


def test_keyword_and_semantic_overlap_fires_once_per_skill(tmp_path):
    from tests.fixtures.embeddings import controlled_embed
    db = AgentDB(path=tmp_path / "test.db", embed_fn=controlled_embed)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "topic_A domain.",
        scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))

    # Key overlaps on "analytics"+"schema" AND embedding matches topic_A
    db.write(
        "memory/working/analytics-schema-similar_to_A",
        "similar_to_A ref",
        origin="agent_inferred",
        agent_id="agent1",
    )
    # Both keyword and semantic match — only ONE fire for this skill
    skill_fires = [f for f in fired if f["skill_id"] == "analytics-schema"]
    assert len(skill_fires) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_skill_conflict_signal.py -v 2>&1 | tail -20
```

Expected: all tests fail with `AssertionError` — `fired` is always `[]` because `_check_skill_conflict` does not exist yet.

- [ ] **Step 3: Implement `_check_skill_conflict` in `src/agentdb/interface/client.py`**

Add these two module-level constants near the top of `client.py` (after the `EVENT_*` constants):

```python
_SKILL_CONFLICT_STOP_WORDS = frozenset({
    "with", "from", "this", "that", "have", "been", "will",
    "your", "into", "they", "them", "then", "when", "what",
})
_SKILL_CONFLICT_MIN_WORD_LEN = 4
```

Add the private method to the `AgentDB` class (after `_handle_conversation_started_embedding`):

```python
    def _check_skill_conflict(self, record: "MemoryRecord") -> None:
        """Dispatch skill_review_needed when a write touches a skill's domain.

        Phase 1: keyword overlap between skill name/ID and record key + entities.
        Phase 3: cosine similarity when embedding provider is active.
        Each skill fires at most once per write, even if both checks match.
        """
        if not record.agent_id:
            return

        scoped_skills = self.skills.get_for_agent(record.agent_id)
        if not scoped_skills:
            return

        def _words(text: str) -> set[str]:
            cleaned = text.replace("/", " ").replace("-", " ").replace("_", " ").lower()
            return {
                w for w in cleaned.split()
                if len(w) >= _SKILL_CONFLICT_MIN_WORD_LEN
                and w not in _SKILL_CONFLICT_STOP_WORDS
            }

        record_words = _words(record.key)
        for entity in (record.entities or []):
            record_words.update(_words(entity))

        for skill in scoped_skills:
            skill_words = _words(f"{skill.id} {skill.name}")
            dispatched = False

            if skill_words & record_words:
                self.gateway.dispatch(
                    "skill_review_needed",
                    skill_id=skill.id,
                    record_id=record.id,
                    record_key=record.key,
                    agent_id=record.agent_id,
                    reason="keyword_overlap",
                )
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

- [ ] **Step 4: Call `_check_skill_conflict` from `write()`**

In `write()`, after the semantic conflict block (after the `except Exception: pass` on line ~774), add:

```python
        # Skill–memory conflict signal
        try:
            self._check_skill_conflict(record)
        except Exception:
            pass
```

- [ ] **Step 5: Run the new tests**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q \
  tests/unit/test_skill_conflict_signal.py -v 2>&1 | tail -25
```

Expected: all 11 tests pass.

- [ ] **Step 6: Run full suite**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  /Users/jm-nx/Documents/nx/slack-assistant/.venv/bin/python -m pytest -q tests/unit
```

Expected: 356 passed (345 + 11 new).

- [ ] **Step 7: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb && \
  git add src/agentdb/interface/client.py tests/unit/test_skill_conflict_signal.py && \
  git commit -m "feat: dispatch skill_review_needed when agent write overlaps a scoped skill domain"
```
