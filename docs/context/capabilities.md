# AgentDB Capabilities by Type

This is the canonical inventory of AgentDB capabilities, cross-referenced two ways: by core-loop stage (Track / Reconcile / Rank / Inject) and by enforcement type (see `docs/context/architecture-principles.md` Principle 12). `README.md`'s Feature State Matrix covers stage + implementation status for a quick read; this document adds the enforcement-type dimension and details the capabilities that serve more than one type. It doesn't repeat file/line detail already in `docs/context/database-schema.md` — cross-reference that for exact schema.

## Enforcement types, briefly

- **Write-time governance** — blocks or shapes a write before it lands (the permission router). Enforced only because `write()`/`delete()` are the sole sanctioned paths — bypassable if code reaches the store directly, which is exactly the bug Task 2 fixed for `delete()`. Worth naming as its own category rather than folding into 12a: it gates *writes*, not what an agent *sees* on read/retrieve.
- **12a, real-time enforcement** — synchronous, wired into a framework's own control flow (Claude SDK's `PreToolUse` + `prepare_turn`). Because retrieval is always a live query (nothing cached), a 12a hook already sees the freshest state on every firing.
- **12b, async integrity check** — decoupled from any single agent's turn. The concrete mechanism is `AgentDB.register_reconciliation_handler`/`dispatch_reconciliation_trigger` — a live, in-memory register/dispatch pair, not a durable event bus (see Principle 7 on why durability isn't the default).
- **Dual** — the same underlying detector/primitive can be called from a 12a hook *or* a 12b sweep/dispatch; which one happens depends on the caller, not the capability itself.

## The dual-mode pattern

Every capability tagged **Dual** below shares one shape: the detection logic (compute a hash, check a threshold, compare a key) doesn't care who calls it. What makes a given firing 12a vs. 12b is only the trigger — inside a framework hook (12a) vs. inside a scheduled sweep or a dispatched event (12b). This is why extending 12b coverage rarely means building new detection logic — it usually means adding one more call site that dispatches an existing signal to `dispatch_reconciliation_trigger(...)`.

## Inventory

| Capability | Stage | Type | Status | Notes |
|---|---|---|---|---|
| `write()` + permission router | Track | Write-time governance | Implemented | Denies + audit-logs at write time; see Design Principle re: bypass risk if store is touched directly |
| Directory tracking snapshots | Track | Dual (today: turn-lifecycle capture only) | Implemented (one trigger only) | `collect_directory_snapshots()` fires inside `finalize_turn`; the same function could be called from an async `file_modified`/`cron` sweep independent of any turn — not wired |
| `file_integrity.py` (SHA-256 hashing) | Track | Dual, unbuilt | Built, unused | Could power a 12a pre-edit staleness check ("this file changed since you last saw it") *and* a 12b periodic drift sweep — same primitive, neither wired in |
| Conversation JSONL capture | Track | N/A (pure capture) | Implemented (initial) | Logging sink, not consumed by retrieval or any dispatcher |
| Entity tagging (`entities` field) | Track | Feeds 12a (`retrieve_by_entities`) | Implemented | |
| Conflict detection | Reconcile | **Dual — the clearest example** | Implemented (temporal + authority) | Wired into `write()` via `ConflictDetector`. Temporal conflicts auto-resolve by recency; authority conflicts flag the record and populate `override_instructions` on retrieve. `conflict_detected` dispatched via `db.gateway.dispatch`. Semantic conflict detection deferred to Phase 3 (embeddings). |
| External-context conflict check | Reconcile | 12a only | Implemented | Returned synchronously to the calling `retrieve()`; no 12b consumer today |
| Reconciliation triggers | Reconcile | `conversation_started` = 12a; `conflict_detected` = 12a→12b; `file_modified`/`cron` = 12b | Partial | `conversation_started` fires from both adapters at turn start. `conflict_detected` dispatched by `write()` via `db.gateway`. `file_modified`/`cron` remain unwired. |
| Skill health/staleness | Reconcile | **Dual** | 12a implemented, 12b missing | Recomputed fresh at every retrieve (already 12a); crossing the 0.6 threshold should also dispatch a `skill_stale` trigger — today it only ever surfaces as a warning string to whichever agent happens to retrieve |
| Lifecycle Manager | Reconcile | 12b | Implemented (`db.lifecycle`) | `run_sweep()`: Step 0 expires TTL records; Step 1 optional confidence decay; Step 2 archives below threshold; Step 3 archives past staleness. `reinforce()` and `record_outcome()` also on `db.lifecycle`. `human_approved` immune to all. |
| Notifier dispatcher (`db.gateway.subscribe`/`dispatch`) | Reconcile | **Is** the 12b mechanism | Implemented | The core capability behind every 12b row. Formerly `register_reconciliation_handler`/`dispatch_reconciliation_trigger` (pass-through aliases still work). Not MCP-specific, not durable — see Principle 7. |
| MultiSignalRanker | Rank | 12a | Implemented | |
| Entity-aware retrieval | Rank | 12a | Implemented | |
| Provider ingestors | Rank | Ingest feeds 12a for free; a sync-caused conflict could dispatch 12b | Missing (Phase 6) | |
| `prepare_turn` / `pre_tool_use` | Inject | 12a | Implemented | |
| MCP server | Inject | `memory_write`/`memory_retrieve` = transport over 12a; `conflict_queue` = transport over 12b | Implemented (stdio, Task 14) | `interface/mcp_server.py`, 4 tools, `agentdb-mcp` console script. HTTP+SSE deferred. |
| Skill draft reconciliation pipeline (Task 13) | Reconcile | **Dual** — propose/metric-triggers = 12b; approve/reject = write-time governance | Missing (Phase 3) | Depends on Task 12 (semantic conflict detection) → Task 11 (embeddings). `SkillStore.update()` governance fix can ship independently. |

## What this means for near-term work

The core loop is operational. Remaining gaps in 12b coverage:

1. **Skill staleness** dispatching a `skill_stale` trigger the first time a skill crosses the 0.6 threshold — needs a "did this just cross" check, not "is this below," to avoid firing on every retrieve. Any consumer then registers via `db.gateway.subscribe("skill_stale", handler)`.
2. **Phase 3** (embeddings) unlocks semantic conflict detection and Task 13's skill draft reconciliation pipeline — both correctly gated behind embeddings being active.

Neither requires a new event bus — the dispatcher (`db.gateway`) already exists and is wired.
