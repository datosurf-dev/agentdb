# AgentDB Completed Tasks Tracker

Canonical archive of shipped work, in the same condensed style as the main plan's status sections: what shipped, exact file pointers, and any remaining gap left behind (cross-referenced to the active plan, not repeated in full here). This is not a build guide — no step-by-step TDD specs, no full code listings. If you need the original implementation walkthrough for something below, it's recoverable from git history; nothing here should be treated as a task still to do.

Referenced from `docs/superpowers/plans/2026-06-06-agentdb-revised.md` (the active plan) — that file tracks what's Partial/Missing and what to do next; this file is where finished work goes so the active plan doesn't have to keep re-stating it.

## Claude SDK Async Adapter (completed 2026-06-26)

Migrated here from the now-deleted `docs/superpowers/plans/2026-06-26-claude-sdk-async-adapter.md`, which tracked this work task-by-task; all four tasks shipped and were verified against git history before removal.

- **Async turn methods on `AgentDBClaudeMiddleware`** (`src/agentdb/adapters/claude_agent_sdk.py`): `prepare_turn_async`, `run_turn_async`, `finalize_turn_async` — async equivalents of the sync `prepare_turn`/`run_turn`/`finalize_turn`, supporting a `call_model_async: Callable[..., Awaitable[str]]` for `ClaudeSDKClient.query()` + `receive_response()` style runtimes instead of only sync `messages.create()`.
- **`from_sdk_client` factory + `handle_turn_async`** on `AgentDBClaudeRuntime`: builds the async call_model callable from a `ClaudeSDKClient` factory function, then wraps the governed turn contract (retrieve → call model → persist → record outcome) in one call.
- **`examples/slack_assistant_agentdb_runtime.py`** updated from a sync `FakeClaudeClient`/`messages.create()` example to an async `ClaudeSDKClient`-style fake, with `SlackAgentHandler` using `handle_turn_async`.
- **slack-assistant integration** (separate repo, `claude_runner.py`/`config.py`/`bot.py`): wired `AgentDB` around `run_prompt` — retrieve before `client.query()`, persist response + record outcome after `receive_response()` — plus `AGENTDB_PATH`/`AGENTDB_PERMISSIONS_PATH` config and startup instantiation. This part lives outside this repository; mentioned here only because it was in scope of the same plan.

Test evidence at the time: `tests/integration/test_claude_agent_sdk_adapter.py` covers the async methods and factory.

## Phase 2 Task F — Action-driven retrieval

`MemoryRecord.entities` (`core/models.py`), `AgentDB.retrieve_by_entities()` (`interface/client.py`) with keyword matching, entity-triggered audit metadata. Claude SDK adapter (`adapters/claude_agent_sdk.py`) implements `_extract_entities_from_tool`, `build_action_context`, `pre_tool_use`, `build_hook_handlers`, `PreToolUse` wiring, `conversation_started_callback`, and JSONL conversation capture gated by `memory_config.capture.conversation_jsonl`.

## Task 7 — Multi-signal ranking algorithm (2026-07-11)

`agentdb/retrieval/algorithm.py` adds `MultiSignalRanker`/`RankedRecord` with provenance/recency/confidence/causal weights and formulas (see the active plan's Algorithm Reference); semantic weight stays 0.0, correctly phase-gated behind embeddings. Wired into `AgentDB.retrieve()`: candidates are fetched from `search()`, ranked, then sliced to `limit`; `signal_scores` in the `memory_retrieve` audit event reflects real composite scores instead of the old `1.0` stub. Required adding `last_retrieved`/`conflict_id` fields to `MemoryRecord` (with a schema migration for pre-existing databases) — `increment_retrieval_count` now also stamps `last_retrieved`.

Deliberately out of scope, tracked as open in the active plan (Task 6): `budget_tokens`/`token_estimate`/`truncated` on `retrieve()`, real conflict detection, and weighting outcome reinforcement by a record's `signal_score` at retrieval time.

## Task A — Retrieval event logging (2026-07-11)

`signal_scores` in the `memory_retrieve` audit event now reflects real `MultiSignalRanker` composite scores (see Task 7 above) instead of a hardcoded `{id: 1.0}`.

## Task 8 — Skill store health-score staleness fix (2026-07-11)

Staleness (`skills/skill_store.py:_compute_health`) now measures age from `skill.last_retrieved`, falling back to `created_at` only when a skill has never been retrieved — fixes a real correctness bug where a daily-retrieved skill still decayed purely by calendar age since creation. Remaining Task 8 gaps (entity-drift penalty, unresolved-conflict penalty, `SkillConflict`, `search()`, `skill_stale` 12b dispatch) are tracked as open in the active plan's Partial section, not here — this entry only covers the fix that shipped.

## Task 2 — `AgentDBBackend` delete governance (2026-07-11)

`AgentDB.delete()` (`interface/client.py`) runs the same `check_write` permission check as `write()` (denies and audit-logs `permission_denied` on failure), appends a `memory_delete` audit event on success. `AgentDBBackend.delete()` routes through it instead of calling `db._store.delete_by_key()` directly — closes a real governance-bypass bug. `list()`'s per-agent scoping gap remains open, tracked in the active plan.

## `directory_tracking.py` / `file_integrity.py`

`core/directory_tracking.py` and `core/file_integrity.py`: tested, working. No dedicated task spec was ever written for these. See `docs/context/capabilities.md` for `file_integrity.py`'s still-unwired status (built and tested, but no code path calls it yet).

## 2026-07-14 batch — conflict detection, lifecycle, gateway, adapters, testing

All items below shipped in the 2026-07-14 sprint (commits 7e315e1, bbe7ffb..ab4e6aa, 00dd95e..5bc0848, 4ba16eb..6940fa7, a1f6c4d and follow-ons).

**Public client gap — real conflict detection + budgeted retrieval** (`governance/conflict_detector.py`, `interface/client.py`): `ConflictDetector.check()` wired into `write()` — temporal conflicts auto-resolve by recency, authority conflicts (agent vs. human-approved) auto-resolve in favour of human. Both records get `conflict_id` stamped. `db.gateway.dispatch("conflict_detected", ...)` fires on detection. `budget_tokens` param live on `retrieve()`; `RetrievalResult.token_estimate` and `truncated` are real, not stubs. `RetrievalResult.override_instructions` populated for authority conflicts.

**Task 9 — Lifecycle manager** (`governance/lifecycle_manager.py`): `db.lifecycle` surface. `run_sweep()` with four steps: Step 0 expires records past `expiry_time`, Step 1 optional confidence decay (off by default), Step 2 archives below `archival_threshold`, Step 3 archives past `staleness_days`. `reinforce(record_id)` boosts confidence toward 1.0. `record_outcome(...)` moved here from `AgentDB` (backward-compat alias kept). `MemoryStore`/`SystemStats` extracted to `agentdb/core/`. `human_approved` immune to all operations.

**Task 16 — `db.gateway` package** (`gateway/__init__.py`, `gateway/adapter.py`): `Gateway` class owns `subscribe`/`dispatch` (replacing top-level aliases, which are kept as pass-throughs), `conflict_queue()` (wired to real `store.list_conflicts()`), and proposal workflow (`propose`/`approve`/`reject`/`list_proposals`). `GatewayAdapter` base class — all adapters must subclass it. `db.gateway` property on `AgentDB`. `AgentDBClaudeMiddleware` migrated to `GatewayAdapter`.

**Hermes adapter** (`adapters/hermes.py`): `AgentDBHermesProvider` implements Hermes `MemoryProvider` ABC + `GatewayAdapter`. 14 hooks mapped. Principle 12a proven via `HermesMemoryManagerShim` tests. `on_pre_compress` summarizer wiring. `db.gateway.subscribe("conflict_detected", handler)` notification pattern.

**Claude SDK adapter improvements**: `AgentDBClaudeMiddleware` subscribes `_on_conflict` to `conflict_detected` trigger; `prepare_turn` dispatches `conversation_started` per turn. Thin pass-throughs added: `pending_conflicts()`, `propose_skill()`, `approve_proposal()`, `reject_proposal()`, `list_proposals()`. `agent_id` exposed as public property on `GatewayAdapter`. `proposal/` made writable by default via `FRAMEWORK_MANAGED_WRITE_PREFIXES` in `permission_router.py`. `memory_budget_tokens` threaded end-to-end through `AgentDBClaudeMiddleware.__init__` and `AgentDBClaudeRuntime`.

**Testing fakes** (`agentdb.testing.FakeAgentDBClaudeMiddleware`): covers `db`, `agent_id`, `profile`, `memory_budget_tokens` params. `inspect.signature` assertions catch real-class drift in the same suite.

---

## Task 10C — `infer` flag + `expiry_time` (2026-07-17, commit 2d7dccb)

`MemoryRecord` gains `expiry_time: Optional[datetime]` and `inferred: bool`. SQLite schema updated with migration columns. `AgentDB.write()` gains `infer: bool = False` and `expiry_time: Optional[datetime]`. `infer=True` routes the raw value through `AgentDB._infer_fn` before storage; raises `ValueError` if no `infer_fn` set at init time. `lifecycle.run_sweep()` Step 0 archives records where `expiry_time ≤ now`; `human_approved` records skip this regardless of field value. 10 TDD tests in `tests/unit/test_infer_write.py`.

## Task 10D — Centralized `memory.yaml` config + profiles (2026-07-17, commits 7cd0954 + beecf33)

`_MODE_DEFAULTS` dict + `_resolve_config()` helper in `interface/client.py`. Resolution order: built-in mode defaults → user-defined `profiles:` block (with optional `extends:`) → top-level per-field overrides. `LifecycleManager.__init__` gains constructor params (`confidence_decay_rate`, `archival_threshold`, `staleness_days`, `reinforce_rate`) — class attributes remain as fallback for backward compat. `MultiSignalRanker` and `LifecycleManager` constructed from resolved config in `AgentDB.__init__`. `db.config` property exposes the fully resolved dict. Built-in profiles: `budget_optimized` (retrieve_limit=5, 2k chars, staleness=90d), `full_storage` (20, 12k, 90d), `research` (50, 24k, 180d, recency-first, archival_threshold=0.05). User-defined profiles: `profiles:` block in `memory.yaml` with optional `extends:` key. 13 TDD tests in `tests/unit/test_config.py`.

## Notifier / 12b dispatch mechanism (reframed 2026-07-11)

`AgentDB.register_reconciliation_handler(trigger, handler)` / `dispatch_reconciliation_trigger(trigger, **kwargs)` (`interface/client.py`) was identified as already being the core async-integrity-check mechanism described in `docs/context/architecture-principles.md` Principle 12b — a live, in-memory register/dispatch pair, not durable, not MCP-specific. Previously scoped only for reconciliation triggers (`conversation_started`, `file_modified`, `cron`); confirmed it can carry any named trigger. New dispatch call sites (`conflict_detected`, `skill_stale`) remain open work, tracked in the active plan. See `docs/context/capabilities.md`.
