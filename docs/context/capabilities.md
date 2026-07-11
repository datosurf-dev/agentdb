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
| Conflict detection (Task 6) | Reconcile | **Dual — the clearest example** | Missing | Detection at write time feeds 12a for free (next retrieve sees `conflict_id`); the same detection event should also dispatch a `conflict_detected` trigger for 12b consumers (human review). Neither half exists yet |
| External-context conflict check | Reconcile | 12a only | Implemented | Returned synchronously to the calling `retrieve()`; no 12b consumer today |
| Reconciliation triggers | Reconcile | `conversation_started` = 12a; `file_modified`/`cron` = 12b | Partial | Only `conversation_started` actually fires (inline at turn start via the Claude adapter) |
| Skill health/staleness | Reconcile | **Dual** | 12a implemented, 12b missing | Recomputed fresh at every retrieve (already 12a); crossing the 0.6 threshold should also dispatch a `skill_stale` trigger — today it only ever surfaces as a warning string to whichever agent happens to retrieve |
| Lifecycle Manager (Task 9) | Reconcile | 12b | Missing (scope narrowed 2026-07-11 to archival-only) | |
| Notifier dispatcher (`register_reconciliation_handler`/`dispatch_reconciliation_trigger`) | Reconcile | **Is** the 12b mechanism | Implemented, narrowly used | The core capability behind every 12b row in this table. Not MCP-specific, not durable — see Principle 7 |
| MultiSignalRanker (Task 7) | Rank | 12a | Implemented | |
| Entity-aware retrieval | Rank | 12a | Implemented | |
| Provider ingestors (Task 10) | Rank | Ingest feeds 12a for free; a sync-caused conflict could dispatch 12b | Missing | |
| `prepare_turn` / `pre_tool_use` | Inject | 12a | Implemented | |
| MCP server (Task 14) | Inject | `memory_write`/`memory_retrieve` = transport over 12a; `conflict_queue` = transport over 12b (subscribes to the existing dispatcher) | Missing | Not a new event-bus subsystem — a thin consumer of the dispatcher row above |
| Skill draft reconciliation pipeline (Task 13) | Reconcile | **Dual** — propose/metric-triggers = 12b (dispatches `skill_draft_proposed`/`skill_candidate_threshold_reached`); approve/reject = write-time governance (the only path allowed to touch `skills.sqlite` content) | Missing, designed | Reuses `retrieve_by_entities` matching, Task 12's conflict detector, `file_integrity.py` (finally gets a caller), the notifier dispatcher, and `agentdb_stats()` — no new subsystem. Depends on Task 12/11. Requires the `SkillStore.update()` governance fix as a prerequisite |

## What this means for near-term work

The biggest unlock isn't new plumbing — the 12b mechanism (dispatcher) already exists. It's:

1. **Task 6 (conflict detection)** dispatching a `conflict_detected` trigger when it lands.
2. **Skill staleness** dispatching a `skill_stale` trigger the first time a skill crosses the 0.6 threshold (not on every retrieve — needs a "did this just cross" check, not a "is this below" check, to avoid firing every single retrieval).
3. Any consumer — MCP's `conflict_queue`, or a Slack/Telegram notifier registered directly by a host app like slack-assistant — calling `db.register_reconciliation_handler("conflict_detected", handler)` once those exist.

None of this requires building an event bus. It requires wiring two new dispatch call sites into work that's already planned (Task 6, and a small addition to Task 8's skill store).
