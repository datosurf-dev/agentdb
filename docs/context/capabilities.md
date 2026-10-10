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
| Conflict detection | Reconcile | **Dual — the clearest example** | Implemented (temporal + authority + semantic) | Wired into `write()` via `ConflictDetector`. Temporal conflicts auto-resolve by recency; authority conflicts flag the record and populate `override_instructions` on retrieve. `conflict_detected` dispatched via `db.gateway.dispatch`. Semantic conflict detection active when `embed_fn=` is set. `agentdb_stats()` `conflict_pending_count` reflects unresolved rows live. |
| External-context conflict check | Reconcile | 12a only | Implemented | Returned synchronously to the calling `retrieve()`; no 12b consumer today |
| Reconciliation triggers | Reconcile | `conversation_started` = 12a; `conflict_detected` = 12a→12b; `file_modified`/`cron` = 12b | Partial | `conversation_started` fires from both adapters at turn start; also always seeds `SessionProfile` and auto-triggers consolidation at 7 days + 10 sessions. `conflict_detected` dispatched by `write()` via `db.gateway`. `file_modified`/`cron` remain unwired. |
| Session metadata + cross-repo isolation | Reconcile | 12a | Implemented | `SessionProfile` seeds at `conversation_started` (always active, no embeddings). Rule-based `_classify_request_type()` populates `request_types`; `repos` is the last path segment of `working_dirs`. `retrieve()` filters to repos the session has touched; records with no `working_dirs` always pass. `write()` expands `SessionProfile` mid-session for repo pivots. |
| Skill–memory conflict signal (`skill_review_needed`) | Reconcile | **Dual** | Implemented (keyword + semantic) | Dispatched from `write()` on key/entity/repo-segment word overlap with any scoped skill. At most one dispatch per skill per write. Also re-checked after LLM synthesis in `consolidate(llm_fn=...)`. Semantic check fires when embeddings active and keyword didn't match — fixed 2026-10-10: embedding is now stored before the skill conflict check runs so `record_vec` is always available. |
| Skill health/staleness | Reconcile | **Dual** | Implemented | Health score anchored on `updated_at` (content age). Crossing 0.6 dispatches `on_stale_crossing` callback, which calls `db.gateway.dispatch("skill_stale", skill_id=...)` (wired in `AgentDB.__init__` via `_on_skill_stale`). Consumers subscribe via `db.gateway.subscribe("skill_stale", handler)` or configure email/Slack channels in `memory.yaml`. Skills are NOT injected into agent context — staleness is purely a governance/reconciliation signal. |
| Lifecycle Manager | Reconcile | 12b | Implemented (`db.lifecycle`) | `run_sweep()`: Step 0 expires TTL records; Step 1 optional confidence decay; Step 2 archives below threshold; Step 3 archives past staleness. `reinforce()` and `record_outcome()` also on `db.lifecycle`. `human_approved` immune to all. |
| Memory consolidation (`db.consolidate()`) | Reconcile | 12b | Implemented | `_lc_cluster()` groups near-duplicates by key-word overlap. Canonical (highest confidence) marked `consolidated=True` with `source_record_ids`; lower-confidence copies archived. Auto-triggers at `conversation_started` after 7 days AND ≥10 sessions per agent (`consolidation_state` table). `retrieve()` accepts `include_consolidated=False`. `untracked_skill_candidate` dispatched when cluster has no governing skill. LLM-synthesized canonicals (`llm_fn=`) re-checked for skill conflict. `human_approved` immune; idempotent. |
| Notifier dispatcher (`db.gateway.subscribe`/`dispatch`) | Reconcile | **Is** the 12b mechanism | Implemented | The core capability behind every 12b row. Formerly `register_reconciliation_handler`/`dispatch_reconciliation_trigger` (pass-through aliases still work). Not MCP-specific, not durable — see Principle 7. |
| Notification channels (`EmailChannel`, `SlackChannel`, `NotificationDispatcher`) | Reconcile | 12b consumer | Implemented | `src/agentdb/notifications/`. `NotificationDispatcher` subscribes to gateway events and routes formatted subject+body to registered channels. Channel failures are isolated (log + continue). Configurable via `memory.yaml` `notifications:` block (`type: email` requires `to`/`smtp_host`/`smtp_user`/`smtp_password`; `type: slack` requires `bot_token`/`channel`). Optional `events:` subset. Slack requires `slack-sdk` extra. |
| MultiSignalRanker | Rank | 12a | Implemented | |
| Entity-aware retrieval | Rank | 12a | Implemented | |
| Skill draft auto-proposal from consolidation | Reconcile | 12b | Implemented | `Gateway.auto_propose_from_cluster()` called by `_on_untracked_skill_candidate` handler (subscribed in `AgentDB.__init__`). Derives a domain name from cluster dominant words (generic structural words filtered); deduplicates against existing proposals using stored `domain_words`. Dispatches `skill_draft_proposed` event. Human approves/rejects via `db.gateway.approve(proposal_id)` / `reject()`. |
| Context compaction (`checkpoint`, `write_compaction_summary`) | Reconcile | 12b | Implemented | `AgentDB.checkpoint(agent_id, session_id)` and `write_compaction_summary(agent_id, session_id, summary)` write `human_approved` records (key: `memory/compaction/{type}/{session_id}`) that survive all lifecycle sweeps. Retrievable by any future session for the same agent. Framework adapters call these before context truncation to preserve cross-session continuity. |
| Provider ingestors | Reconcile/Rank | Ingest writes feed 12a on retrieve; sync-caused conflicts dispatch 12b | Partially implemented | `src/agentdb/providers/anthropic.py`: `AnthropicProvider(db, client).sync(agent_id, limit=50)` ingests assistant message text as `provider_ingested` memories. Never overwrites `human_approved`. Optional extra: `pip install agentdb[providers]`. OpenAI + Google providers not yet built. |
| System stats (`db.system.agentdb_stats()`) | Rank | N/A (read-only surface) | Implemented | Returns: `total_memories`, `avg_confidence`, `low_confidence_count`, `conflict_pending_count`, `retrieval_count_total`, `outcome_positive_rate`, `external_conflict_rate`, `total_skills`, `stale_skill_count`. Skills queried from separate `skills.sqlite` via `SystemStats(skills_db_path=...)`. |
| `prepare_turn` / `pre_tool_use` | Inject | 12a | Implemented | Injects governed **memory records** only. Skills are not injected — they are tracked database objects for conflict detection, not context augmentation. |
| MCP server | Inject | `memory_write`/`memory_retrieve` = transport over 12a; `conflict_queue` = transport over 12b | Implemented (stdio, Task 14) | `interface/mcp_server.py`, 4 tools, `agentdb-mcp` console script. HTTP+SSE deferred. |

## What this means for near-term work

The core loop is operational. Completed items:

- ~~**Skill staleness dispatch**~~ — **Implemented 2026-10-10.** `_on_skill_stale` wired in `AgentDB.__init__`; dispatches `skill_stale` to gateway. Email/Slack consumers configurable via `memory.yaml`.
- ~~**Skill–memory conflict signal**~~ — **Implemented 2026-10-08 + 2026-10-10.** Keyword path dispatches `skill_review_needed` from `write()`; semantic path (cosine similarity via embedding store) implemented and fixed — embedding is now stored before conflict check runs.
- ~~**Memory consolidation**~~ — **Implemented 2026-10-08.** `db.consolidate()` with auto-trigger, `consolidated` flag, `source_record_ids`, `include_consolidated` on `retrieve()`, LLM synthesis with skill conflict re-check.
- ~~**Phase 4 embeddings**~~ — **Implemented.** SQLite-Vec embedding store, `embed_fn=` hook in `AgentDB.__init__`, semantic retrieval, and semantic conflict detection all working.

~~**Skill draft reconciliation pipeline**~~ — **Implemented 2026-10-10.** Auto-proposal from consolidation, semantic dedup by domain_words, `skill_draft_proposed` event, approve/reject workflow all wired.

~~**Context compaction**~~ — **Implemented 2026-10-10.** `checkpoint()` + `write_compaction_summary()` write human_approved records that survive sweeps and are retrievable cross-session.

~~**`agentdb_stats()` skill visibility**~~ — **Implemented 2026-10-10.** `total_skills` + `stale_skill_count` added; skills DB queried via separate `SystemStats(skills_db_path=...)`.

~~**Anthropic provider ingestor**~~ — **Implemented 2026-10-10.** `AnthropicProvider.sync()` ingests assistant messages as `provider_ingested`. OpenAI + Google remain pending.

Remaining gap:

1. **HTTP+SSE MCP transport** — stdio covers Claude Desktop; HTTP+SSE needed only for browser-based or remote agents. Deferred until a concrete integration is requested.

None of these require a new event bus — the dispatcher (`db.gateway`) already exists and is wired.
