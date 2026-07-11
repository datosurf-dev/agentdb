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

## Notifier / 12b dispatch mechanism (reframed 2026-07-11)

`AgentDB.register_reconciliation_handler(trigger, handler)` / `dispatch_reconciliation_trigger(trigger, **kwargs)` (`interface/client.py`) was identified as already being the core async-integrity-check mechanism described in `docs/context/architecture-principles.md` Principle 12b — a live, in-memory register/dispatch pair, not durable, not MCP-specific. Previously scoped only for reconciliation triggers (`conversation_started`, `file_modified`, `cron`); confirmed it can carry any named trigger. New dispatch call sites (`conflict_detected`, `skill_stale`) remain open work, tracked in the active plan. See `docs/context/capabilities.md`.
