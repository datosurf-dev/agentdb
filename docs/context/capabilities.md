# AgentDB Capabilities by Type

This is the canonical inventory of AgentDB capabilities, cross-referenced two ways: by core-loop stage (Track / Reconcile / Rank / Inject) and by enforcement type (see `docs/context/architecture-principles.md` Principle 12). The README provides a concise implementation overview; this document adds the enforcement-type dimension and details capabilities that serve more than one type. It doesn't repeat file/line detail already in `docs/context/database-schema.md` — cross-reference that for schema details and verify them against source.

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
| Directory tracking snapshots | Track | Dual (today: turn-lifecycle capture only) | Implemented, opt-in (default OFF) | `collect_directory_snapshots()` fires inside `finalize_turn` **only when `capture.track_directories` is enabled** (default OFF since 0.7.0 §6); explicit `directory_snapshots` passed by the caller are still honoured. The same function could be called from an async `file_modified`/`cron` sweep independent of any turn — not wired |
| `file_integrity.py` (SHA-256 hashing) | Track | Dual, unbuilt | Built, unused | Could power a 12a pre-edit staleness check ("this file changed since you last saw it") *and* a 12b periodic drift sweep — same primitive, neither wired in |
| Conversation JSONL capture | Track | N/A (pure capture) | Implemented (initial) | Logging sink, not consumed by retrieval or any dispatcher |
| Entity tagging (`entities` field) | Track | Feeds 12a (`retrieve_by_entities`) | Implemented | |
| Conflict detection | Reconcile | **Dual — the clearest example** | Implemented (temporal + authority + semantic) | Wired into `write()` via `ConflictDetector`. Temporal conflicts auto-resolve by recency; authority conflicts flag the record and populate `override_instructions` on retrieve. `conflict_detected` dispatched via `db.gateway.dispatch`. Semantic conflict detection active when `embed_fn=` is set. `agentdb_stats()` `conflict_pending_count` reflects unresolved rows live. |
| External-context conflict check | Reconcile | 12a only | Implemented | Returned synchronously to the calling `retrieve()`; no 12b consumer today |
| Reconciliation triggers | Reconcile | `conversation_started` = 12a; `conflict_detected` = 12a→12b; `file_modified`/`cron` = 12b | Partial | `conversation_started` fires from both adapters at turn start; also always seeds `SessionProfile` and auto-triggers consolidation at 7 days + 10 sessions. `conflict_detected` dispatched by `write()` via `db.gateway`. `file_modified`/`cron` remain unwired. |
| Session metadata + cross-repo isolation | Reconcile | 12a | Implemented | `SessionProfile` seeds at `conversation_started` (always active, no embeddings). Rule-based `_classify_request_type()` populates `request_types`; `repos` is the last path segment of `working_dirs`. `retrieve()` filters to repos the session has touched; records with no `working_dirs` always pass. `write()` expands `SessionProfile` mid-session for repo pivots. |
| Lifecycle Manager | Reconcile | 12b | Implemented (`db.lifecycle`) | `run_sweep()`: Step 0 expires TTL records; Step 1 optional confidence decay; Step 2 archives below threshold; Step 3 archives past staleness. `reinforce()` and `record_outcome()` also on `db.lifecycle`. `human_approved` immune to all. |
| Memory consolidation (`db.consolidate()`) | Reconcile | 12b | Implemented | `_lc_cluster()` groups near-duplicates by key-word overlap, gated by a compatibility check (shared key-domain slug + no temporal/authority conflict) so related-but-distinct records are not merged. Creates a **NEW `derived_canonical` record** (`kind=derived_canonical`, `origin=agent_inferred`, `source_record_ids` + per-source digests in `derivation`); source records are **never overwritten**, only soft-archived. Canonicals invalidate when a source's digest changes. Auto-triggers at `conversation_started` after 7 days AND ≥10 sessions per agent (defaults; tunable via `profile.consolidation`: `enabled`, `min_sessions`, `days`, `min_cluster_size`, `since_hours`). `retrieve()` accepts `include_consolidated=False`. `human_approved` sources immune; idempotent. |
| Notifier dispatcher (`db.gateway.subscribe`/`dispatch`) | Reconcile | **Is** the 12b mechanism | Implemented | The core capability behind every 12b row. Formerly `register_reconciliation_handler`/`dispatch_reconciliation_trigger` (pass-through aliases still work). Not MCP-specific, not durable — see Principle 7. |
| Notification channels (`EmailChannel`, `SlackChannel`, `NotificationDispatcher`) | Reconcile | 12b consumer | Implemented | `src/agentdb/notifications/`. `NotificationDispatcher` subscribes to gateway events and routes formatted subject+body to registered channels. Channel failures are isolated (log + continue). Configurable via `memory.yaml` `notifications:` block (`type: email` requires `to`/`smtp_host`/`smtp_user`/`smtp_password`; `type: slack` requires `bot_token`/`channel`). Optional `events:` subset. Slack requires `slack-sdk` extra. |
| MultiSignalRanker | Rank | 12a | Implemented | |
| Entity-aware retrieval | Rank | 12a | Implemented | |
| Context compaction (`write_compaction_summary`) | Reconcile | 12b | Implemented | `AgentDB.write_compaction_summary(agent_id, session_id, summary, origin='agent_inferred')` writes a record (key: `memory/compaction/summary/{session_id}`). The `checkpoint()` tombstone was removed — it wrote `{session_id, type}` that nothing read, and retrieval injected it as noise. Provenance defaults to `agent_inferred` (machine-produced) — **not** `human_approved` unless a verified human passes `origin="human_approved"` explicitly (contract P-01). Agent-origin summaries are lifecycle-decayable; human-approved ones are immune. Retrievable by any future session for the same agent. |
| Provider ingestors | Reconcile/Rank | Ingest writes feed 12a on retrieve; sync-caused conflicts dispatch 12b | Partially implemented | `src/agentdb/providers/anthropic.py`: `AnthropicProvider(db, client).sync(agent_id, limit=50)` ingests assistant message text as `provider_ingested` memories. Never overwrites `human_approved`. Optional extra: `pip install agentdb[providers]`. OpenAI + Google providers not yet built. |
| System stats (`db.system.agentdb_stats()`) | Rank | N/A (read-only surface) | Implemented | Returns: `total_memories`, `avg_confidence`, `low_confidence_count`, `conflict_pending_count`, `retrieval_count_total`, `outcome_positive_rate`, `external_conflict_rate`. |
| `prepare_turn` / `pre_tool_use` | Inject | 12a | Implemented | `AgentDBMiddleware` injects governed memories; its async preparation path can also inject workspace and recent work context when enabled by `RunnerCapabilities`. The Claude SDK hook map is exposed for downstream registration. Hermes has its own provider hooks. |
| MCP server | Inject | `memory_write`/`memory_retrieve` = transport over 12a; `conflict_queue` = transport over 12b | Implemented (stdio, Task 14) | `interface/mcp_server.py`, 4 tools, `agentdb-mcp` console script. HTTP+SSE deferred. |

## What this means for near-term work

The core loop is operational. Completed items:

- ~~**Memory consolidation**~~ — **Implemented 2026-10-08.** `db.consolidate()` with auto-trigger, `consolidated` flag, `source_record_ids`, `include_consolidated` on `retrieve()`, LLM synthesis.
- ~~**Phase 4 embeddings**~~ — **Implemented.** SQLite-Vec embedding store, `embed_fn=` hook in `AgentDB.__init__`, semantic retrieval, and semantic conflict detection all working.

~~**Context compaction**~~ — **Implemented 2026-10-10; provenance corrected 2026-11-20 (ADB-03); `checkpoint()` removed.** `write_compaction_summary()` defaults to `agent_inferred`; only explicit `origin="human_approved"` gets human immunity.

~~**Anthropic provider ingestor**~~ — **Implemented 2026-10-10.** `AnthropicProvider.sync()` ingests assistant messages as `provider_ingested`. OpenAI + Google remain pending.

Remaining gap:

1. **HTTP+SSE MCP transport** — stdio covers Claude Desktop; HTTP+SSE needed only for browser-based or remote agents. Deferred until a concrete integration is requested.

None of these require a new event bus — the dispatcher (`db.gateway`) already exists and is wired.
