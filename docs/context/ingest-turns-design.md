# Design note — `AgentDB.ingest_turns()`

Status: **proposal**, not implemented.

## Problem

AgentDB can *write* a conversation log (`capture.conversation_jsonl`) but has no
way to accept one it did not observe. Every harness (AgentGate, Claude Code,
Codex, opencode, Hermes) already keeps its own append-only session log in its own
format, so today a harness must either:

- re-log through `finalize_turn` (**duplicate storage**), or
- get no memory benefit from history it already has.

We want a *door*, not a parser: AgentDB accepts **normalised turns** and derives
memory from them; the harness owns its own log format.

## Non-goals

- **No log parsing.** No vendor format recognition, no file discovery.
- **No LLM prompt.** Extraction/synthesis stays in `consolidate(llm_fn=...)`.
- **No transcript storage in the memory DB.** Turns are not memories.

## Public API

```python
@dataclass(frozen=True)
class ConversationTurn:
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    timestamp: datetime      # REQUIRED — a quality log always has one
    turn_id: str             # REQUIRED — caller's stable id, for traceability
    memories: list[tuple[str, Any]] = ()   # optional (key, value) pairs
    tool_calls: list[dict] | None = None

@dataclass(frozen=True)
class IngestResult:
    accepted: int
    skipped: int             # duplicates
    memory_records: list[str]  # record ids written
    jsonl_appended: int

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
and `consolidate` — it is a bulk, retroactive session-level operation, not a
turn-lifecycle hook.

**`turn_id` is mandatory but is *not* the dedup key** (see below). It exists so
records can be traced back to the source log.

## Deduplication

Dedup key is `sha256(agent_id | session_id | role | content | timestamp)`.

This is deliberately *not* `turn_id`: a caller may regenerate ids, or re-ingest
the same file with fresh ids. Content+timestamp+session catches both, while
still allowing the same text at different times to be kept (e.g. a repeated
`"approved"` or `"troubleshoot the PR"`).

Backed by a new table (PK gives `INSERT OR IGNORE` semantics — no scans):

```sql
CREATE TABLE ingest_ledger (
    agent_id   TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_hash  TEXT NOT NULL,
    turn_id    TEXT NOT NULL,   -- traceability only
    source     TEXT,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, session_id, turn_hash)
);
```

Scope is **per (agent, session)** so two different sessions may legitimately
repeat the same text.

## Ordering — JSONL first

The conversation log is written **before** memory records:

1. dedupe check per turn against `ingest_ledger`
2. **append accepted turns to `conversations/<agent>--<session>.jsonl`**
3. write each turn's `memories` through the normal `write()` path
4. commit ledger rows

Rationale: the JSONL is the durable record of what was ingested, and it is the
same store `capture.conversation_jsonl` uses — so an ingested session occupies
exactly the same place, in the same shape, as a captured one. Memory writes then
behave like normal writes (permissions, conflicts, skill signals), and a failure
there leaves an intact log plus a ledger row to reconcile rather than a silently
truncated history.

Step 3 may legitimately write **zero** records for a turn (no `memories`
supplied) — turns are not stored as memories.

## Storage touched

| Step | Destination | Format |
|---|---|---|
| 2 | `<memory_dir>/conversations/<agent>--<session>.jsonl` | agentdb's existing entry shape |
| 3 | `<memory_dir>/database/agentdb.sqlite` | `MemoryRecord` via `write()` |
| 4 | `ingest_ledger` table | dedup bookkeeping |

No new directories; no changes to `capture.conversation_jsonl`, which remains
the opt-in "log as you go" path. `ingest_turns` is additive.

## Interaction with summarisation

`consolidate()` only considers records with `created_at <= now - since_hours`
(default **24**). Records written by `ingest_turns` are therefore **invisible to
a default `consolidate()`** — a caller doing end-of-session ingest followed by
`consolidate()` must pass `since_hours=0`. This is a documented trap, not a bug
to paper over.

`ingest_turns` does **not** summarise. Its contract is dedupe + store + write
caller-supplied memories.

## Open questions

1. Should `ingest_turns` return per-turn outcomes (accepted/skipped with reason)
   rather than aggregate counts? Useful for drift detection on re-ingest.
2. Should the ledger be pruned when a session is deleted/archived?
3. Tool turns (`role="tool"`) carry large content; store them in JSONL but never
   as memories — confirm that's the expectation.
