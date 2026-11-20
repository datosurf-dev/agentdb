# Design notes — `ingest_turns()` and `purge_session()`

Status: **implemented** (`src/agentdb/ingest.py`, `AgentDB.ingest_turns` /
`AgentDB.purge_session`). The notes below describe the behaviour as built.

Two related additions:

- **`ingest_turns()`** — a door for history a harness already has, so AgentDB can
  derive memory from it without duplicating storage.
- **`purge_session()`** — hard removal across every store, because today's
  `delete()` is a soft delete that leaves the bytes in place.

---

# Part 1 — `ingest_turns()`

## Problem

AgentDB can *write* a conversation log (`capture.conversation_jsonl`) but cannot
accept one it did not observe. Every harness (AgentGate, Claude Code, Codex,
opencode, Hermes) already keeps its own append-only session log in its own
format, so today a harness must either re-log through `finalize_turn`
(duplicating storage) or get no memory benefit from history it already has.

## Non-goals

- **No log parsing.** No vendor format recognition, no file discovery.
- **No LLM prompt.** Extraction and synthesis stay in `consolidate(llm_fn=...)`.
- **No transcript storage in the memory DB.** Turns are not memories.
- **No opinion on cadence.** Per-turn, hourly, or a one-time dump are all the
  caller's choice; the method only has to be safe to call repeatedly.

## Public API

```python
@dataclass(frozen=True)
class ConversationTurn:
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    timestamp: datetime      # REQUIRED
    turn_id: str             # REQUIRED — caller's stable id, for traceability
    memories: list[tuple[str, Any]] = ()   # optional (key, value) pairs
    tool_calls: list[dict] | None = None

@dataclass(frozen=True)
class IngestResult:
    accepted: int
    skipped: int             # duplicates
    memory_records: list[str]

def ingest_turns(
    self,
    *,
    agent_id: str,
    session_id: str,
    turns: Sequence[ConversationTurn],
    source: str = "",        # provenance label, e.g. "claude_code_jsonl"
) -> IngestResult
```

Declared on `AgentDB` in `interface/client.py`, beside `write_compaction_summary`
and `consolidate` — a bulk, retroactive session-level operation, not a
turn-lifecycle hook.

**Validation.** All fields are checked before any write: a missing `timestamp`
or `turn_id` raises `ValueError` and **nothing is written** for that call. A log
without timestamps is not a quality log, and silently degrading dedup would make
re-ingest unsafe.

## Deduplication

Key: `sha256(agent_id | session_id | role | content | timestamp)`.

Deliberately **not** `turn_id`: a caller may regenerate ids, or re-ingest the
same file with fresh ids. Content+timestamp+session catches both, while repeated
text at different times is still kept (e.g. a repeated `"approved"` or
`"troubleshoot the PR"`).

Scope is **per (agent, session)**, so two sessions may legitimately repeat text.

```sql
CREATE TABLE ingest_ledger (
    agent_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    turn_hash   TEXT NOT NULL,
    turn_id     TEXT NOT NULL,   -- traceability only
    source      TEXT,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, session_id, turn_hash)
);
```

The PK gives `INSERT OR IGNORE` semantics — no content scans.

## Ordering — JSONL first

1. validate every turn
2. dedupe check against `ingest_ledger`
3. **append accepted turns to `conversations/<agent>--<session>.jsonl`**
4. write each turn's `memories` through the normal `write()` path
5. commit ledger rows

The JSONL is the durable record of what was ingested and uses the **same store
and shape** as `capture.conversation_jsonl`, so an ingested session and a
captured session are indistinguishable on disk. Memory writes then behave like
any other write (permissions, conflicts); a failure there leaves
an intact log plus a ledger row to reconcile.

Step 4 may legitimately write **zero** records for a turn.

**Tool turns:** `role="tool"` is written to the JSONL only. Attached `memories`
are ignored with a warning — tool output is high-token, low-knowledge, and the
most common accidental-secret vector. Knowledge belongs on the assistant turn
that interpreted it.

## Storage touched

| Step | Destination | Format |
|---|---|---|
| 3 | `<memory_dir>/conversations/<agent>--<session>.jsonl` | agentdb's existing entry shape |
| 4 | `<memory_dir>/database/agentdb.sqlite` → `memories` | `MemoryRecord` via `write()` |
| 5 | `ingest_ledger` | dedup bookkeeping |

No new directories. `capture.conversation_jsonl` is unchanged; `ingest_turns` is
additive.

## Interaction with summarisation

`consolidate()` only considers `created_at <= now - since_hours` (default **24**),
so records written by `ingest_turns` are **invisible to a default
`consolidate()`**. A caller ingesting then consolidating must pass
`since_hours=0`. Documented trap, not a bug.

`ingest_turns` does **not** summarise.

---

# Part 2 — `purge_session()`

## Why this exists

Today `AgentDB.delete(key)` is a **soft** delete: `store.delete_by_key()` sets
`archived = 1` and keeps the row (`core/store.py:185-190`). Archiving is correct
for lifecycle management — it is **not** data removal.

A session's content is meanwhile spread over **seven** places:

| Store | Location | Session data |
|---|---|---|
| memories | `database/agentdb.sqlite` → `memories` | rows (`archived` still holds content) |
| conflicts | `database/agentdb.sqlite` → `conflicts` | references to record ids |
| embeddings | `embeddings/…` → `embeddings`, `session_topics` | vectors + topic rows |
| outcomes | `database/agentdb.sqlite` → `outcomes` | per-turn outcomes (`session_id`) |
| external retrieves | `database/agentdb.sqlite` → `external_retrieves` | per-session flags (`session_id`) |
| conversation log | `conversations/<agent>--<session>.jsonl` | **full raw transcript** |
| audit log | `database/agentdb.sqlite` → `audit_log` | event metadata |

A user who says *"this session leaked a token, remove it"* therefore deletes one
view and leaves the bytes in six other places. That gap exists **today**,
before ingest; ingestion would add a seventh (the ledger plus re-landed JSONL).

## API

```python
@dataclass(frozen=True)
class PurgeResult:
    memories: int
    embeddings: int
    outcomes: int
    external_retrieves: int
    conflicts: int
    ledger: int
    jsonl_lines_removed: int

def purge_session(
    self,
    agent_id: str,
    session_id: str,
    *,
    require_confirmation: bool = True,
) -> PurgeResult
```

## What it does

| Store | Action |
|---|---|
| `memories` | **hard DELETE** rows for that agent+session (not archive) |
| `conflicts` | DELETE rows referencing purged record ids |
| `embeddings` / `session_topics` | DELETE rows for the purged record ids / session |
| `outcomes` | DELETE rows for the session |
| `external_retrieves` | DELETE rows for the session |
| `conversations/*.jsonl` | remove the file (or the session's lines, if shared) |
| `ingest_ledger` | DELETE rows for `(agent, session)` — otherwise a re-ingest would be silently skipped while the data is gone |
| `audit_log` | **append** `session_purged` with counts only, **no content** |

**Audit is kept, content is not.** The event is the evidence that deletion
happened; keeping the payload would defeat the purpose.

## Relationship to `delete()`

They are different operations and should stay so:

| | `delete(key)` | `purge_session()` |
|---|---|---|
| Intent | retire a memory | remove a session's data |
| Mechanism | `archived = 1` (recoverable) | hard DELETE (irreversible) |
| Scope | one key | every store for the session |
| Use case | lifecycle, supersession | privacy / leak remediation |

`delete()`'s archived-only behaviour should be **documented as intentional**
(soft delete, recoverable) rather than changed.

## Resolved decisions

1. **Explicit only.** `purge_session()` is never called from
   `lifecycle.run_sweep()` and has no retention-based auto-purge. Destructive
   operations stay deliberate and operator-initiated.
2. **Row deletion is sufficient for embeddings.** Removing the rows from
   `embeddings` and `session_topics` needs no rebuild; both are derived caches
   and are recomputed on demand once embeddings are re-enabled.

## Still open

- Whether `IngestResult` should expose per-turn detail after all, once real
  re-ingest scenarios appear. The current aggregate is intentional: cadence is
  the caller's choice, so the method should not presume a per-turn workflow.
