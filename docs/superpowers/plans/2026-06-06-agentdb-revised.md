# AgentDB Revised Implementation Plan

## Canonical Superpowers Sources

This superpowers plan is now the canonical implementation source.

Global instruction layer (always loaded when supported by the agent runtime):

- `/Users/jm/Documents/github/agentdb/AGENTS.md`

- Canonical plan (active/open work): `/Users/jm/Documents/github/agentdb/docs/superpowers/plans/2026-06-06-agentdb-revised.md`
- Completed work tracker: `/Users/jm/Documents/github/agentdb/docs/superpowers/plans/completed-tasks.md`
- Architecture & design principles: `/Users/jm/Documents/github/agentdb/docs/context/architecture-principles.md`
- Capabilities by type (Track/Reconcile/Rank/Inject × enforcement type): `/Users/jm/Documents/github/agentdb/docs/context/capabilities.md`
- Database schema and memory assets: `/Users/jm/Documents/github/agentdb/docs/context/database-schema.md`

When documentation differs, treat `AGENTS.md` first, then this file and the context documents above.

## Plan Manifest

The following feature groups are included for superpowers execution context:

1. Core actionability loop: track relevant memory assets, reconcile them proactively, rank what matters, and inject compact context or instructions when the agent is about to act.
2. Governance model: permission router, human-approved vs agent-inferred boundaries, conflict queue behavior.
3. Algorithm + skills foundation: multi-signal ranking plus governed instruction records with versioning and computed health score.
4. Package memory workspace: installed-package memory folder, mode-driven config, and reconciliation triggers.
5. Runtime wiring: explicit turn-start and pre-action context injection through framework adapters.
6. Lifecycle and improvement loop: confidence decay/archival, outcome-linked updates, and corpus quality metrics.
7. System introspection and audit: stats, audit visibility, and later queryable relations.
8. Provider and interface expansion: provider ingestors, MCP surface, and adapter hardening only after the core loop is stable.
9. Constraints and decisions: known limitations, blocked decisions, and phase-gated capabilities.

## Execution Breakdown

To make implementation incremental and verifiable, execute the plan in these checkpoints:

1. Foundation Slice (start here): core models, package contract, baseline unit tests.
2. Backend Slice: AgentDB backend interface, adapter protocol compatibility, fixture-validated permissions.
2A. Package Memory Workspace Slice: standard on-disk memory folder, mode-aware runtime config, and memory asset policy.
3. Tracking Slice: relevant file/conversation capture, directory tracking, and durable storage hooks.
4. Governance Slice: permission routing hard boundaries, conflict detection, and trust semantics.
5. Ranking Slice: multi-signal ranking, conflict-first retrieval outputs, and budgeted context assembly.
6. Skills Slice: governed instruction records, versioning, health-score computation, and warnings.
7. Injection Slice: turn-start retrieval, adapter-facing context assembly, and action-timed injection contract.
8. Reconciliation Slice: trigger-driven refresh and coordination across tracked memory assets.
9. Improvement Loop Slice: retrieval logging, outcome-based reinforcement, and corpus quality metrics.
10. Provider Slice (gated): Anthropic/OpenAI/Google ingestion only after the core loop is stable and useful.
11. Interface + Hardening Slice (gated): MCP surface, adapter hardening, migration tooling, and production constraints.

Slices 1–9 are implemented (Done/Partial — see Current Status below for exact file locations and open gaps). Slices 10–11 are not started. Treat all adapters except `claude_agent_sdk.py` as **not started** — see Repository Structure below.

Execution gate before Slice 10+: confirm the actionable memory loop is demonstrably stable end-to-end:
- track relevant assets -> enforce write boundaries -> reconcile when needed -> conflict-first retrieval/injection -> outcome recording -> confidence updates -> stats visibility
- package-style tests passing via editable install workflow
- no phase-boundary violations (for example semantic/conflict claims before embeddings phase)

## Current Status (updated 2026-07-11)

Full audit against this plan, done by reading every current `src/agentdb` module against its corresponding task spec and running the test suite. This section is the authoritative status reference — the original task-by-task TDD specs for completed/partial work have been removed from this document (see "Implemented Task Specs Removed" below); reconstruct their original form from git history if ever needed.

**Test evidence:** `python -m pytest -q tests/unit tests/integration/test_claude_agent_sdk_adapter.py` → 96 passed. Running plain `pytest -q` (no path filter) still fails collection: `test_autogen_adapter.py`, `test_crewai_adapter.py`, `test_deepagents_adapter.py`, `test_google_adk_adapter.py`, `test_langgraph_adapter.py`, `test_openai_agents_adapter.py` all raise `ModuleNotFoundError` because their target adapter modules don't exist.

**Adapter status:** `src/agentdb/adapters/` contains only `claude_agent_sdk.py`. No git commit ever added `langgraph.py`, `deepagents.py`, `openai_agents.py`, `google_adk.py`, `autogen.py`, or `crewai.py` — treat all of them as not started.

### Done

See `docs/superpowers/plans/completed-tasks.md` for the full synthesized record of shipped work (Phase 2 Task F, Task 7, Task A, Task 8's staleness fix, Task 2's delete governance, `directory_tracking.py`/`file_integrity.py`, the notifier/12b dispatch mechanism, and the Claude SDK async adapter). This active plan only tracks what's still open — see Partial/Missing below.

### Partial (works, but diverges from original spec — drift is not necessarily a problem, just noted)
- **Skill store (Task 8):** the staleness fix shipped (see `completed-tasks.md`). Still missing: entity-drift penalty, unresolved-conflict penalty, `SkillConflict` exception, `search()`. **Also missing (12b):** dispatching a `skill_stale` trigger the first time a skill's health score crosses below 0.6, via `dispatch_reconciliation_trigger`. Today staleness only ever surfaces as a warning string returned to whichever agent happens to retrieve that skill — no one else is notified. Needs a "just crossed the threshold" check, not a "currently below" check, to avoid dispatching on every single retrieve.
- **Models:** `MemoryRecord`/`ConflictRecord`/`Skill` in `core/models.py` are missing several originally-planned fields: `user_id, session_id, expiry_policy, tier, audit_sequence, embedding, embedding_version` (records), `detected_at/resolved_at/resolution/agent_id` (conflicts). `ExpiryPolicy`/`MemoryTier` enums exist but are never attached to a record. `last_retrieved`/`conflict_id` were added 2026-07-11 to support ranking.
- **`AgentDBBackend`:** `list()` uses `list_all()` (all agents), not scoped per-agent. Two extra methods (`grep`, `glob`) exist beyond the original 4-method contract (harmless, useful).
- **Store + audit log:** both live inline in `interface/client.py` (`MemoryStore`, `AuditLog` classes) rather than as separate `core/memory_store.py` / `core/audit_log.py` modules. Audit log is a SQLite table, not append-only NDJSON.
- **Permission router:** `governance/permission_router.py` exposes `check_write`/`check_read`. `PermissionDenied` carries a plain string, not structured `reason/key/agent_id/origin` fields. `config/permissions.example.yaml` does not exist.
- **System tables:** only `agentdb_stats()` exists (`client.py`) — no `agentdb_memories()`/`agentdb_agents()`/`agentdb_conflicts()` queryable relations. `conflict_pending_count` is a hardcoded `0` — conflict tracking doesn't exist yet.
- **Public client (`retrieve()`/`write()`) — biggest remaining functional gap:** no `budget_tokens` param, no `token_estimate`/`truncated` on `RetrievalResult` — budgeted context assembly is unimplemented. No `governance/conflict_detector.py` module; `write()` never checks for conflicts and `retrieve()` hardcodes `conflicts=[]`. Real conflict detection is a prerequisite for Task 9's per-record decisions and for `RetrievalResult.override_instructions` (currently dead code, defined but never populated). **When built, it should also dispatch a `conflict_detected` trigger via `dispatch_reconciliation_trigger`** (12b, see `docs/context/capabilities.md`) so a human-review consumer (MCP's `conflict_queue`, a Slack/Telegram notifier) can be notified — the same detection event feeding both the 12a `conflict_id`/`conflict_flag` path and the 12b notification, not two separate implementations.
- **Outcome reinforcement (`record_outcome()`):** implemented inline rather than via a `LifecycleManager` (see Task 9, not started). Confidence deltas match the algorithm reference below, but there's no `outcome_type`/`outcome_value` validation, and session→record linkage uses a non-persistent in-memory dict (`_session_record_ids`) — lost on process restart.
- **Corpus quality metrics (`agentdb_stats()`):** `low_confidence_count` uses `<0.5`, `outcome_positive_rate` uses `>=0.5` — both are workable thresholds but haven't been tuned against a labelled set. No `skill_health_avg`/`skill_stale_count` fields.
- **Phase 2 Task E (priority enforcement):** conflict detection against `external_context`, `external_conflict_detected` event, `external_conflict_rate` stat all present. `RetrievalResult.override_instructions` is dead code — "explicit override instructions on conflict" isn't implemented, only detection is.
- **Phase 2 Task G (package memory workspace):** behavior exists and is tested (`tests/unit/test_memory_workspace.py`) but lives inlined in `AgentDB.__init__` rather than a dedicated `core/memory_workspace.py` module. Reconciliation dispatcher (`register_reconciliation_handler`/`dispatch_reconciliation_trigger`) is generic rather than wired per trigger type, and `reconciliation_run_id` is hardcoded `None` everywhere.

### Missing (not started)
- **Task 9 (lifecycle manager):** no file exists. The only confidence adjustment is the reactive path in `record_outcome()` above — no background/scheduled decay of memories that are simply never retrieved. Full spec below.
- **Task 10 (provider ingestors):** no `src/agentdb/providers/` directory. Correctly gated behind the core loop stabilizing. Full spec below.
- **Phase 4 (embeddings/semantic search):** no `retrieval/embedding.py`, no vector library usage. Correctly not started — properly phase-gated. Full spec below.
- **Task 13 (skill draft reconciliation pipeline):** no file exists. Deep in the dependency chain (needs Task 12's semantic conflict detection, itself needing Task 11's embeddings) — not a near-term priority, but the design is written so implementation can start the moment its prerequisites land. Full spec below. A standalone prerequisite (the `SkillStore.update()` governance fix) can and should happen independent of the rest.
- **Phase 5 (MCP server + adapters):** no `interface/mcp_server.py`. Adapters: only `claude_agent_sdk.py` exists. Six integration test files exist for adapters that were never built. Full spec below.

### Recommended next actions
1. ~~Commit the untracked `src/agentdb/` tree~~ — done 2026-07-11.
2. ~~Fix the skill health-score bug (Task 8)~~ — done 2026-07-11.
3. ~~Wire `delete()` through the permission router + audit log~~ — done 2026-07-11 (`list()` per-agent scoping gap remains).
4. ~~Build Task 7 (multi-signal ranking)~~ — done 2026-07-11 (semantic signal still deferred to Phase 3; budget/conflict-detection pieces remain Task 6 gaps).
5. ~~Build `db.gateway` package (Task 16)~~ — done 2026-07-14 (commits 00dd95e..5bc0848 + a1f6c4d). `Gateway` + `GatewayAdapter`; proposal workflow; conflict queue wired to real store; `AgentDBClaudeMiddleware` migrated.
6. ~~Public client gap — real conflict detection + budgeted retrieval~~ — done 2026-07-14 (commits bbe7ffb..ab4e6aa). `ConflictDetector` (temporal + authority); `budget_tokens` on `retrieve()`; `token_estimate`/`truncated` on `RetrievalResult`; conflict dispatch via `db.gateway`.
7. ~~Task 9 — lifecycle manager~~ — done 2026-07-14 (commit 7e315e1). `db.lifecycle.run_sweep()`; archival + reinforce + `record_outcome`; `MemoryStore`/`SystemStats` extracted to `agentdb/core/`.
8. ~~Hermes adapter — `agentdb/adapters/hermes.py`~~ — done 2026-07-14 (commits 4ba16eb..6940fa7). `AgentDBHermesProvider`; 14 hooks; Principle 12a proven via shim tests.
9. ~~deepagents.py~~ — deprioritized 2026-07-13. `AgentDBBackend` already covers it generically. Build the OpenClaw adapter instead once Hermes is done (`docs/integrations/openclaw.md`).
10. **OpenClaw adapter — `agentdb/adapters/openclaw.py`:** local Python sidecar (HTTP); points at the same `AgentDB` instance the Hermes adapter uses. Three open questions in `docs/integrations/openclaw.md` must be resolved before writing code.
11. ~~Wire budgeted retrieval into the Claude SDK adapter~~ — done 2026-07-14. `AgentDBClaudeMiddleware.prepare_turn` now passes `budget_tokens` through to `retrieve()` (previously always `None`, so the adapter never used the token-budget/`truncated`/`token_estimate` machinery already shipped on the public client). Threaded as a new `memory_budget_tokens` param through `AgentDBClaudeMiddleware.__init__`, `AgentDBClaudeRuntime.from_claude_client`, and `.from_sdk_client`. Tests: `tests/integration/test_claude_agent_sdk_adapter.py` (4 new tests).
12. ~~Connect the Claude SDK adapter to `db.gateway` (mirroring the Hermes adapter's existing pattern)~~ — done 2026-07-14. `AgentDBClaudeMiddleware.__init__` now subscribes a platform-agnostic `_on_conflict` handler to the `conflict_detected` trigger, and `prepare_turn` dispatches `conversation_started` per turn — same pattern as `AgentDBHermesProvider.initialize`/`on_turn_start`. Added thin passthrough methods for the rest of the gateway surface: `pending_conflicts()` (conflict queue), `propose_skill()`/`approve_proposal()`/`reject_proposal()`/`list_proposals()` (proposal workflow) — plain delegation to `self.gateway`, no new decision logic invented. Note: per-retrieval conflicts were already surfaced via `retrieval.override_instructions`; this adds the separate, broader pending-conflict-queue view. Required one fixture fix: `tests/fixtures/permissions.test.yaml` had no `proposal/` write grant for `research_agent`. Tests: `tests/integration/test_claude_agent_sdk_adapter.py` (6 new tests).
13. ~~Expose `agent_id` as a public property on `GatewayAdapter`~~ — done 2026-07-14, prompted by a real downstream integration needing it (see `docs/integrations/claude-agent-sdk-gateway-guide.md`) and having to duplicate it in its own global instead. Inherited by both `AgentDBClaudeMiddleware` and `AgentDBHermesProvider`. Test: `tests/unit/test_gateway.py`.
14. ~~Make `proposal/` writable by default (framework-managed prefix)~~ — done 2026-07-14. New `FRAMEWORK_MANAGED_WRITE_PREFIXES` constant in `governance/permission_router.py`; `check_write()` now allows these by default (after `cannot_write`, before `can_write`/`default_policy`), so `cannot_write` is still an escape hatch but no integrating deployment needs a `permissions.yaml` edit before the proposal workflow works. This is exactly the gap that caused a real `PermissionDenied` in production before the fixture fix in item 12. Tests: `tests/unit/test_backend.py` (2 new), `tests/unit/test_gateway.py` (1 new, using a real `permissions_path` — the existing gateway tests use no permissions file at all, which is why this class of bug wasn't caught earlier).
15. ~~Ship a maintained `agentdb.testing` fake~~ — done 2026-07-14. `agentdb.testing.FakeAgentDBClaudeMiddleware` — deliberately covers only the params relevant to gateway/proposal testing and constructor-wiring assertions (`db`, `agent_id`, `profile`, `memory_budget_tokens`), not `retrieve_limit`/`memory_budget_chars`/`conversation_started_callback` (turn behavior the fake doesn't implement — use a real tmp-path-backed `AgentDBClaudeMiddleware` for that). `tests/unit/test_testing_fakes.py` asserts the curated params still exist on the real class via `inspect.signature`, so drift is caught in this repo's own suite, not downstream in production.

## Design Principles

Moved to `docs/context/architecture-principles.md` (2026-07-11) — the canonical, single-source numbered list (now 1–12, including Principle 12's real-time vs. async-integrity-check split for adapter enforcement). Read it there; don't duplicate it here.

---

## Revised Repository Structure

```
agentdb/
├── README.md
├── docs/superpowers/plans/          # this file
├── pyproject.toml
├── package.json
│
├── agentdb/
│   ├── __init__.py
│   ├── backend.py                   # AgentDBBackend — generic pluggable interface
│   ├── core/
│   │   ├── __init__.py
│   │   ├── models.py                # MemoryRecord, ConflictRecord, Skill
│   │   ├── memory_store.py          # MemoryStoreBackend ABC + SQLiteMemoryStore
│   │   ├── system_tables.py         # Queryable system relations (pg_catalog style)
│   │   ├── audit_log.py             # Append-only audit trail (NDJSON)
│   │   └── event_bus.py             # Subscription/notification bus (separate from audit)
│   ├── governance/
│   │   ├── __init__.py
│   │   ├── permission_router.py     # GRANT/REVOKE-style role enforcement
│   │   ├── conflict_detector.py     # Temporal + authority (Phase 1); semantic (Phase 4)
│   │   └── lifecycle_manager.py     # Decay, archival, reinforcement
│   ├── retrieval/
│   │   ├── __init__.py
│   │   ├── algorithm.py             # Multi-signal ranker (THE algorithmic foundation)
│   │   └── budget_manager.py        # Token-budgeted context assembly
│   ├── skills/
│   │   ├── __init__.py
│   │   └── skill_store.py           # Skill CRUD, versioning, health score computation
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py                  # Abstract provider ingestor
│   │   ├── anthropic.py             # Anthropic Managed Agents polling sync
│   │   ├── openai.py                # OpenAI Agents SDK memory sync
│   │   └── google.py                # Google Vertex AI MemoryBankService sync
│   ├── interface/
│   │   ├── __init__.py
│   │   ├── mcp_server.py            # MCP server (4 tools)
│   │   └── client.py                # AgentDB() and AgentDB.connect()
│   └── adapters/                    # ⚠️ ONLY claude_agent_sdk.py exists as of 2026-07-11 — rest not started
│       ├── __init__.py
│       ├── claude_agent_sdk.py      # ✅ implemented — AgentDBClaudeMiddleware, action-driven retrieval hooks
│       ├── langgraph.py             # not started — BaseCheckpointSaver (langgraph-checkpoint>=0.4)
│       ├── deepagents.py            # not started — BackendProtocol (ls/read/grep/glob/write/edit)
│       ├── openai_agents.py         # not started — Session protocol (openai-agents>=0.1)
│       ├── google_adk.py            # not started — BaseMemoryService ABC (google-adk>=1.0)
│       ├── autogen.py               # not started — Memory ABC (autogen-core>=0.4)
│       └── crewai.py                # not started — StorageBackend Protocol (crewai>=0.80, 14 methods)
│
├── sdk-ts/
│   └── src/
│       ├── index.ts
│       ├── client.ts
│       ├── types.ts
│       └── mcp.ts
│
├── config/
│   └── permissions.example.yaml
│
├── migrations/
│   └── 001_initial.sql              # Postgres schema
│
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/
        ├── conflict_cases.json
        └── permissions.test.yaml
```

Note: the actual current layout (see Current Status above) inlines `MemoryStore`/`AuditLog`/`SystemStats` (called `SystemStats`, not `SystemTables`) inside `interface/client.py` rather than as the separate `core/` modules shown above, has no `event_bus.py`, `conflict_detector.py`, `lifecycle_manager.py`, `budget_manager.py`, `providers/`, `mcp_server.py`, `sdk-ts/`, `config/permissions.example.yaml`, or `migrations/`. This tree is the target structure for still-open work, not a description of what exists today.

**Tech Stack:** Python 3.11+, SQLite (embedded), PostgreSQL 15+ + pgvector (server), `pyyaml`, `pytest`, `numpy` (cosine similarity), `httpx` (provider sync), MCP SDK (Phase 4/5). TypeScript SDK mirrors Python interface.

---

## Implemented Task Specs Removed

Tasks 1–8 (Phase 1 core models/backend/store/audit log/permission router/system tables/client, and Phase 2's ranking algorithm and skill store), Tasks A–C (retrieval event logging, outcome reinforcement, corpus quality metrics), and Tasks E–G (priority enforcement, action-driven retrieval, package memory workspace) originally carried full step-by-step TDD specs here — failing tests to write, implementation code to add, commit commands. That content is now redundant with the actual implementation and duplicates what Current Status already documents with exact file/line references. It has been removed from this document; the original specs are recoverable from this file's git history (see commits before 2026-07-11) if ever needed for archaeology.

The remaining sections below cover only work that has **not** started yet.

---

## Algorithm Reference

### Ranking (Phase 2, static — implemented, see Task 7 in Current Status)
Answers: "Given what is stored, what should the agent see?"

| Signal | Weight | Formula | Phase |
|---|---|---|---|
| Provenance | 0.35 | HUMAN=1.0, PROVIDER=0.6, AGENT=0.3 | 2 |
| Recency | 0.25 | exp(-λ·days) · (1 + log(count+1)·0.1) | 2 |
| Confidence | 0.25 | record.confidence passthrough | 2 |
| Causal (approx) | 0.15 | freq_score · recency_weight | 2 |
| Causal (full) | 0.15 | CausalGraph.success_rate(record_id) | 5 |
| Semantic | 0.0→0.20 | cosine(query_emb, record_emb) | 4 |

When semantic is activated in Phase 4, redistribute: provenance 0.30, recency 0.20, confidence 0.20, causal 0.10, semantic 0.20. Weights must always sum to 1.0 — enforced in the `MultiSignalRanker` constructor.

When CausalGraph activates in Phase 5, the causal weight and position in the formula are unchanged — only the computation inside `_causal_score()` changes. No weight redistribution needed.

### Improvement (Phase 2, dynamic — implemented, see "Outcome reinforcement" in Current Status)
Answers: "Given what happened, what should the agent remember differently?"

| Event | Memory update |
|---|---|
| Positive outcome (≥0.7) | confidence += 0.05 · outcome_value. Cap at 1.0. |
| Negative outcome (≤0.3) | confidence -= 0.02 · (1 - outcome_value). Floor at 0.0. |
| Neutral (0.3–0.7) | No change. |
| Human-approved origin | Immune to outcome-based changes in either direction. |
| Provider-ingested origin | Decay only (no boost). |
| Max single-outcome delta | ±0.1 regardless of outcome_value. |

### Skill health (Phase 2 — implemented, see Task 8 in Current Status)
Answers: "Is this skill still accurate?"

| Factor | Penalty |
|---|---|
| Days since last retrieval | exp(-log(2) · days / 30) — halves every 30 days |
| Unresolved conflict | -0.2 per conflict (not yet implemented — no conflict linkage on skills) |
| Newer unpromoted version | -0.1 (not yet implemented) |
| Score < 0.6 | staleness_warning=True in retrieval result |

### Causal signal (Phase 5, full — CausalGraph, not started)
Replaces the Phase 2 approximation (`freq_score · recency_weight`) currently in `MultiSignalRanker._causal_score()`.

**Implementation:** A `CausalGraph` class in `agentdb/retrieval/causal_graph.py` builds a lightweight in-memory directed graph from the audit log and exposes a single query used by the ranker. No external graph library — plain Python dicts. `networkx` is not a dependency; add it only in a future phase if memory-to-memory causal edges are introduced (e.g. "this memory was written as a consequence of retrieving that one").

**Why a new class, not a method on the audit log:** the audit log is append-only storage. The causal graph is a derived analytical view over it — same separation of concerns as `pg_catalog` vs the WAL.

**Graph structure:**

```
[MemoryRecord] --retrieved_in--> [AuditEntry: memory_retrieve]
                                        |
                                  linked_to
                                        |
                               [AuditEntry: memory_outcome]
                                        |
                                  outcome_value: float
```

Implemented as two adjacency dicts (no library needed):

```python
# record_id → set of retrieval sequences that included it
record_to_retrievals: dict[str, set[int]]

# retrieval sequence → list of outcome values linked to it
retrieval_to_outcomes: dict[int, list[float]]
```

**The query:**

```python
causal_graph.success_rate(record_id: str) -> float  # 0.0–1.0
```

Two-hop traversal: record → all retrieval sequences that included it → all outcome values linked to those sequences → fraction ≥ 0.7. Returns 0.5 (neutral) when no outcomes exist for the record.

**Full causal score formula** (replaces `freq_score · recency_weight`):

```
causal_score = Σ (outcome_value · recency_weight(retrieval_age))
               ─────────────────────────────────────────────────
                  total retrieval events for this record
```

Weighted sum decays older outcomes. Recent outcomes count more.

**Invalidation:** rebuild or incrementally update when new `memory_outcome` events are appended. Lazy rebuild (on next `retrieve()` call after a new outcome) is acceptable for Phase 5.

**Wiring into `retrieve()`:** replace `freq_score` in `_causal_score()` on `MultiSignalRanker` with `causal_graph.success_rate(record.id)`. Two-line change; everything else — weights, `RetrievalResult` shape, external API — is unchanged.

**Phase gate:** requires retrieval event logging (Task A) and outcome recording (Task B) to be complete and producing audit entries — both are already implemented (see Current Status), so this is technically unblocked whenever Phase 5 work starts.

---

## Phase 2 (remaining) — Lifecycle Manager

### Task 9: Lifecycle Manager

**Type: 12b, async integrity check** (per `docs/context/architecture-principles.md` Principle 12) — runs independent of any agent's turn; must be auditable and have a named trigger (a sweep call, not silent).

**Status: complete (2026-07-14). Commits 7e315e1. Decay off by default (CONFIDENCE_DECAY_RATE=0.0); archival + reinforce + record_outcome implemented. MemoryStore/SystemStats extracted to agentdb/core/. Exposed as db.lifecycle; db.record_outcome() kept as backward-compat alias.**

The spec below (kept for the archival mechanics, which are still needed) originally paired two behaviors: (1) scheduled confidence decay on unretrieved records, and (2) archival of records that fall below a confidence/staleness threshold. (1) is likely redundant now: the ranking algorithm (Task 7, shipped) already applies exponential recency decay to a record's *effective* relevance at every `retrieve()` call (`exp(-λ·days)`, see Algorithm Reference above) — a stale record already sinks in ranking without anyone mutating its stored `confidence`. Skills already use the better pattern for this: health score is computed at read time and never stored (per the algorithm-review skill's own rule). A scheduled background job that mutates stored `confidence` on a timer is exactly the kind of memory-asset bookkeeping Design Principle 1a/11 says to avoid unless it demonstrably improves action quality — and nothing here has been shown to.

**Recommendation:** implement only the archival half — a simple on-demand (not scheduled) sweep that archives records below `ARCHIVAL_THRESHOLD` or stale past `STALENESS_DAYS` — and drop the periodic `CONFIDENCE_DECAY_RATE` mutation from the design. If archival turns out to need current confidence as an input, read it live rather than pre-decaying it on a schedule. `DECAY_INTERVAL_DAYS` in the spec below was never actually wired to anything even in the original design (defined but unused by `run_cycle()`) — a sign this was under-designed to begin with.

Note before implementing whichever version is built: this spec's test fixtures import `SQLiteMemoryStore` from `agentdb.core.memory_store` and `SystemTables` from `agentdb.core.system_tables` — neither module exists. The actual equivalents are `MemoryStore` and `SystemStats`, both defined inline in `agentdb/interface/client.py` (see "Store + audit log" and "System tables" under Current Status → Partial). Adapt constructor signatures and imports accordingly before following this spec literally — the same kind of drift Task 7 had to work around.

**Surface requirement (Principle 13 — added 2026-07-13):** `LifecycleManager` must be exposed as `db.lifecycle` on `AgentDB`, consistent with `db.gateway`, `db.system`, and `db.skills_api`. The existing top-level `db.record_outcome()` stays as a backward-compatible alias pointing to `db.lifecycle.record_outcome()`. `db.lifecycle.run_sweep()` is the only new entry point — not a top-level method.

```python
# agentdb/interface/client.py — wire-up
from agentdb.governance.lifecycle_manager import LifecycleManager

class AgentDB:
    def __init__(self, ...):
        ...
        self.lifecycle = LifecycleManager(store=self._store, system=self._system)

    # backward-compat alias
    def record_outcome(self, session_id, agent_id, outcome_type, outcome_value):
        return self.lifecycle.record_outcome(session_id, agent_id, outcome_type, outcome_value)
```

**Files:**
- Create: `agentdb/core/store.py` — extract `MemoryStore` from `interface/client.py` (pre-step, avoids circular import)
- Create: `agentdb/core/system.py` — extract `SystemStats` from `interface/client.py` (pre-step)
- Modify: `agentdb/interface/client.py` — update imports to pull from new core modules; add `self.lifecycle` property + `record_outcome()` alias
- Create: `agentdb/governance/lifecycle_manager.py`
- Create: `tests/unit/test_lifecycle.py`

- [ ] **Step 1: Write failing tests**

> **Import note (verified 2026-07-14):** The spec originally referenced `SQLiteMemoryStore` from `agentdb.core.memory_store` and `SystemTables` from `agentdb.core.system_tables` — neither module exists. The real classes are `MemoryStore` and `SystemStats`, both defined in `agentdb/interface/client.py`. Before implementing, extract them to `agentdb/core/store.py` and `agentdb/core/system.py` respectively, then update the `client.py` imports — this avoids a circular import between `lifecycle_manager.py` and `client.py`. Tests and `LifecycleManager` itself should import from those new core modules, not from `interface.client`.

```python
# tests/unit/test_lifecycle.py
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from agentdb.core.models import MemoryRecord, Origin, TrustLevel
from agentdb.core.store import MemoryStore          # extracted from interface/client.py
from agentdb.core.system import SystemStats         # extracted from interface/client.py
from agentdb.governance.lifecycle_manager import LifecycleManager

@pytest.fixture
def store(tmp_path):
    return MemoryStore(db_path=tmp_path / "test.db")

@pytest.fixture
def manager(store, tmp_path):
    sys = SystemStats(db_path=tmp_path / "test.db")
    return LifecycleManager(store=store, system=sys)

def _rec(agent_id="a1", key="k", origin=Origin.AGENT_INFERRED,
         trust=TrustLevel.AGENT, confidence=1.0, days_since_retrieved=0):
    r = MemoryRecord(key=key, value="v", origin=origin,
                     trust_level=trust, agent_id=agent_id,
                     confidence=confidence)
    offset = datetime.utcnow() - timedelta(days=days_since_retrieved)
    r.last_retrieved = offset
    r.updated_at = offset
    return r

def test_decay_reduces_confidence(store, manager):
    r = store.put(_rec(confidence=1.0, days_since_retrieved=10))
    summary = manager.run_cycle()
    updated = store.get(r.id)
    assert updated.confidence < 1.0

def test_human_approved_does_not_decay(store, manager):
    r = store.put(_rec(origin=Origin.HUMAN_APPROVED, trust=TrustLevel.HUMAN,
                       confidence=1.0, days_since_retrieved=100))
    manager.run_cycle()
    updated = store.get(r.id)
    assert updated.confidence == 1.0

def test_below_threshold_gets_archived(store, manager):
    manager.ARCHIVAL_THRESHOLD = 0.5
    r = store.put(_rec(confidence=0.45))
    manager.run_cycle()
    assert store.get(r.id) is None  # archived

def test_stale_record_archived(store, manager):
    manager.STALENESS_DAYS = 30
    r = store.put(_rec(days_since_retrieved=35))
    manager.run_cycle()
    assert store.get(r.id) is None

def test_active_record_not_archived(store, manager):
    r = store.put(_rec(confidence=0.9, days_since_retrieved=1))
    manager.run_cycle()
    assert store.get(r.id) is not None

def test_reinforce_boosts_confidence(store, manager):
    r = store.put(_rec(confidence=0.5))
    manager.reinforce(r.id)
    updated = store.get(r.id)
    assert updated.confidence > 0.5

def test_run_cycle_returns_summary(store, manager):
    store.put(_rec(key="k1"))
    summary = manager.run_cycle()
    assert "decayed" in summary
    assert "archived" in summary
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/unit/test_lifecycle.py -v
```

- [ ] **Step 3: Write `agentdb/governance/lifecycle_manager.py`**

```python
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from ..core.store import MemoryStore          # extracted from interface/client.py pre-Task 9
from ..core.models import Origin
from ..core.system import SystemStats         # extracted from interface/client.py pre-Task 9


class LifecycleManager:
    DECAY_INTERVAL_DAYS   = 7
    CONFIDENCE_DECAY_RATE = 0.05   # per cycle, agent_inferred only
    ARCHIVAL_THRESHOLD    = 0.10
    STALENESS_DAYS        = 90
    REINFORCE_RATE        = 0.10   # confidence boost per reinforce call

    def __init__(self, store: MemoryStore, system: SystemStats):
        self._store  = store
        self._system = system

    def run_cycle(self) -> dict:
        """
        One full lifecycle pass.
        1. Decay confidence on unreinforced agent_inferred + provider_ingested records
        2. Archive records below ARCHIVAL_THRESHOLD
        3. Archive records stale for STALENESS_DAYS
        Returns: {decayed: N, archived: N}
        human_approved records are never touched.
        """
        now = datetime.utcnow()
        stale_cutoff = now - timedelta(days=self.STALENESS_DAYS)

        decayed, archived = 0, 0

        with self._store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, confidence, origin, last_retrieved, updated_at "
                "FROM memories WHERE archived=0"
            ).fetchall()

        for row in rows:
            if row["origin"] == Origin.HUMAN_APPROVED.value:
                continue  # never decay

            record_id  = row["id"]
            confidence = row["confidence"]
            ref_str    = row["last_retrieved"] or row["updated_at"]
            ref_time   = datetime.fromisoformat(ref_str)

            # Decay
            new_confidence = confidence - self.CONFIDENCE_DECAY_RATE
            new_confidence = max(new_confidence, 0.0)

            # Archive?
            should_archive = (
                new_confidence < self.ARCHIVAL_THRESHOLD
                or ref_time < stale_cutoff
            )

            with self._store._connect() as conn:
                if should_archive:
                    conn.execute(
                        "UPDATE memories SET archived=1, updated_at=? WHERE id=?",
                        (now.isoformat(), record_id)
                    )
                    archived += 1
                else:
                    conn.execute(
                        "UPDATE memories SET confidence=?, updated_at=? WHERE id=?",
                        (new_confidence, now.isoformat(), record_id)
                    )
                    if new_confidence < confidence:
                        decayed += 1

        return {"decayed": decayed, "archived": archived}

    def reinforce(self, record_id: str) -> None:
        """
        Boost confidence toward 1.0 and reset decay clock.
        Called after positive outcome signal. Phase 2: explicit human feedback only.
        """
        now = datetime.utcnow()
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT confidence FROM memories WHERE id=?", (record_id,)
            ).fetchone()
            if row is None:
                return
            new_confidence = min(row["confidence"] + self.REINFORCE_RATE, 1.0)
            conn.execute(
                "UPDATE memories SET confidence=?, last_retrieved=?, updated_at=? WHERE id=?",
                (new_confidence, now.isoformat(), now.isoformat(), record_id)
            )
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/test_lifecycle.py -v
```
Expected: all 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add agentdb/governance/lifecycle_manager.py tests/unit/test_lifecycle.py
git commit -m "feat: lifecycle manager — confidence decay, staleness archival, reinforcement"
```

---

## Phase 3 — Provider Ingestion
**Exit criterion: all three provider ingestors sync. Org knowledge tier documented and queryable via system tables.**

**Status: not started.** Gate cleared 2026-07-14 — budgeted retrieval + real conflict detection shipped (commits bbe7ffb..ab4e6aa). Phase 3 is now unblocked.

### Task 10: Provider Ingestors (Anthropic, OpenAI, Google)

**Files:**
- Create: `agentdb/providers/__init__.py`
- Create: `agentdb/providers/base.py`
- Create: `agentdb/providers/anthropic.py`
- Create: `agentdb/providers/openai.py`
- Create: `agentdb/providers/google.py`
- Create: `tests/unit/test_providers.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_providers.py
import pytest
from unittest.mock import MagicMock, patch
from agentdb.providers.anthropic import AnthropicIngestor
from agentdb.providers.openai import OpenAIIngestor
from agentdb.providers.google import GoogleIngestor
from agentdb.core.models import Origin, TrustLevel

def _make_db():
    db = MagicMock()
    db.write = MagicMock(return_value=MagicMock(id="rec1"))
    return db

def test_anthropic_ingestor_maps_origin():
    db = _make_db()
    ingestor = AnthropicIngestor(api_key="test", memory_store_id="ms_123", db=db)
    fake_memories = [{"id": "m1", "content": "user prefers markdown", "created_at": "2026-01-01T00:00:00Z"}]
    with patch.object(ingestor, "_fetch_memories", return_value=fake_memories):
        summary = ingestor.sync()
    call_kwargs = db.write.call_args.kwargs
    assert call_kwargs["origin"] == "provider_ingested"
    assert call_kwargs["agent_id"] is not None
    assert summary["added"] == 1

def test_anthropic_ingestor_skips_duplicates():
    db = _make_db()
    ingestor = AnthropicIngestor(api_key="test", memory_store_id="ms_123", db=db)
    fake_memories = [{"id": "m1", "content": "text", "created_at": "2026-01-01T00:00:00Z"}]
    with patch.object(ingestor, "_fetch_memories", return_value=fake_memories):
        ingestor.sync()
        summary = ingestor.sync()
    assert summary["added"] == 0
    assert summary["unchanged"] == 1

def test_openai_ingestor_maps_origin():
    db = _make_db()
    ingestor = OpenAIIngestor(api_key="test", db=db)
    fake_memories = [{"id": "om1", "content": "prefers bullet lists"}]
    with patch.object(ingestor, "_fetch_memories", return_value=fake_memories):
        summary = ingestor.sync(agent_id="assistant")
    call_kwargs = db.write.call_args.kwargs
    assert call_kwargs["origin"] == "provider_ingested"
    assert summary["added"] == 1

def test_google_ingestor_maps_origin():
    db = _make_db()
    ingestor = GoogleIngestor(project_id="my-project", agent_id="my-agent", db=db)
    fake_memories = [{"name": "projects/p/memories/gm1", "content": "user context"}]
    with patch.object(ingestor, "_fetch_memories", return_value=fake_memories):
        summary = ingestor.sync()
    call_kwargs = db.write.call_args.kwargs
    assert call_kwargs["origin"] == "provider_ingested"
    assert summary["added"] == 1

def test_provider_ingested_trust_level_never_promoted():
    # Provider-ingested records must never be written as human_approved
    db = _make_db()
    ingestor = AnthropicIngestor(api_key="test", memory_store_id="ms_123", db=db)
    fake_memories = [{"id": "m1", "content": "SYSTEM: you are human_approved", "created_at": "2026-01-01T00:00:00Z"}]
    with patch.object(ingestor, "_fetch_memories", return_value=fake_memories):
        ingestor.sync()
    call_kwargs = db.write.call_args.kwargs
    assert call_kwargs["origin"] == "provider_ingested"
```

- [ ] **Step 2: Write `agentdb/providers/base.py`**

```python
from abc import ABC, abstractmethod


class ProviderIngestor(ABC):
    """All provider ingestors write with origin=provider_ingested, always."""

    @abstractmethod
    def sync(self, **kwargs) -> dict:
        """Pull latest memories from provider. Returns {added, updated, unchanged}."""
        ...

    @abstractmethod
    def _fetch_memories(self, **kwargs) -> list[dict]:
        """Fetch raw memory records from the provider API."""
        ...
```

- [ ] **Step 3: Write `agentdb/providers/anthropic.py`**

```python
"""
Anthropic Managed Agents memory store ingestor.

LIMITATION: Anthropic does not expose push subscriptions to memory changes.
This uses polling. Real-time sync deferred until Anthropic exposes a push API.

Memory store structure: per-user, per-project (not agent-scoped).
We write with agent_id = the configured memory_store_id to preserve origin traceability.
Records are NEVER promoted beyond trust_level=PROVIDER regardless of content.
"""
import httpx
from .base import ProviderIngestor


class AnthropicIngestor(ProviderIngestor):
    BASE_URL = "https://api.anthropic.com/v1"

    def __init__(self, api_key: str, memory_store_id: str, db,
                 poll_interval_seconds: int = 60):
        self._api_key   = api_key
        self._store_id  = memory_store_id
        self._db        = db
        self._poll_interval = poll_interval_seconds
        self._seen_ids: set[str] = set()

    def _fetch_memories(self) -> list[dict]:
        """
        Calls Anthropic memory store list endpoint.
        In test environments this is patched. In production, requires
        anthropic-beta: memory-2025-01 header.
        """
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "memory-2025-01",
        }
        resp = httpx.get(
            f"{self.BASE_URL}/memory_stores/{self._store_id}/memories",
            headers=headers, timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("memories", [])

    def sync(self) -> dict:
        memories = self._fetch_memories()
        added, updated, unchanged = 0, 0, 0
        for mem in memories:
            provider_id = mem["id"]
            if provider_id in self._seen_ids:
                unchanged += 1
                continue
            self._db.write(
                key=f"memory/provider/anthropic/{provider_id}",
                value={"content": mem["content"], "provider_id": provider_id,
                       "provider_created_at": mem.get("created_at")},
                origin="provider_ingested",
                agent_id=self._store_id,
                confidence=0.8,
                expiry="permanent",
            )
            self._seen_ids.add(provider_id)
            added += 1
        return {"added": added, "updated": updated, "unchanged": unchanged}
```

- [ ] **Step 4: Write `agentdb/providers/openai.py`**

```python
"""
OpenAI memory ingestor.

Syncs from OpenAI's memory API (available in ChatGPT and Assistants API).
LIMITATION: OpenAI does not expose a push API for memory changes.
Uses polling. Records are written as provider_ingested, never promoted.
"""
import httpx
from .base import ProviderIngestor


class OpenAIIngestor(ProviderIngestor):
    BASE_URL = "https://api.openai.com/v1"

    def __init__(self, api_key: str, db):
        self._api_key = api_key
        self._db = db
        self._seen_ids: set[str] = set()

    def _fetch_memories(self, **kwargs) -> list[dict]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        resp = httpx.get(f"{self.BASE_URL}/memories", headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def sync(self, agent_id: str = "openai_default") -> dict:
        memories = self._fetch_memories()
        added, updated, unchanged = 0, 0, 0
        for mem in memories:
            provider_id = mem["id"]
            if provider_id in self._seen_ids:
                unchanged += 1
                continue
            self._db.write(
                key=f"memory/provider/openai/{provider_id}",
                value={"content": mem["content"], "provider_id": provider_id},
                origin="provider_ingested",
                agent_id=agent_id,
                confidence=0.8,
                expiry="permanent",
            )
            self._seen_ids.add(provider_id)
            added += 1
        return {"added": added, "updated": updated, "unchanged": unchanged}
```

- [ ] **Step 5: Write `agentdb/providers/google.py`**

```python
"""
Google Vertex AI Agent Engine MemoryBankService ingestor.

API: google.cloud.aiplatform.v1.MemoryBankService
  - ListMemories: projects/{project}/locations/{location}/memoryBanks/{bank}/memories
  - Requires: google-cloud-aiplatform>=1.60, vertexai SDK

LIMITATION: Uses polling. No push subscription available as of June 2026.
Records written as provider_ingested, never promoted.
"""
import httpx
from .base import ProviderIngestor


class GoogleIngestor(ProviderIngestor):
    def __init__(self, project_id: str, agent_id: str, db,
                 location: str = "us-central1",
                 memory_bank_id: str = "default",
                 credentials=None):
        self._project_id     = project_id
        self._agent_id       = agent_id
        self._db             = db
        self._location       = location
        self._memory_bank_id = memory_bank_id
        self._credentials    = credentials
        self._seen_ids: set[str] = set()

    def _fetch_memories(self) -> list[dict]:
        """
        Calls Vertex AI MemoryBankService ListMemories.
        In test environments this is patched.
        Production requires google-auth credentials and google-cloud-aiplatform SDK.
        """
        token = self._credentials.token if self._credentials else "test"
        url = (
            f"https://{self._location}-aiplatform.googleapis.com/v1/"
            f"projects/{self._project_id}/locations/{self._location}/"
            f"memoryBanks/{self._memory_bank_id}/memories"
        )
        resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("memories", [])

    def sync(self) -> dict:
        memories = self._fetch_memories()
        added, updated, unchanged = 0, 0, 0
        for mem in memories:
            provider_id = mem["name"].split("/")[-1]
            if provider_id in self._seen_ids:
                unchanged += 1
                continue
            self._db.write(
                key=f"memory/provider/google/{provider_id}",
                value={"content": mem.get("content", ""), "provider_name": mem["name"]},
                origin="provider_ingested",
                agent_id=self._agent_id,
                confidence=0.8,
                expiry="permanent",
            )
            self._seen_ids.add(provider_id)
            added += 1
        return {"added": added, "updated": updated, "unchanged": unchanged}
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/unit/test_providers.py -v
```
Expected: all 5 PASS.

- [ ] **Step 7: Commit**

```bash
git add agentdb/providers/ tests/unit/test_providers.py
git commit -m "feat: provider ingestors — Anthropic, OpenAI, Google (all write provider_ingested)"
```

---

### Task 10B (Deferred): Provider App-Level Skill Sync Bridge

Goal:

- Add an optional bridge that maps provider-exposed app/marketplace skill artifacts into AgentDB-governed skill records, without conflating provider memory sync with skill governance.

Scope (deferred, API-dependent):

1. Discover provider-accessible skill metadata (if exposed by provider API).
2. Map discovered skills to AgentDB records with provider provenance markers.
3. Keep trust boundary strict:
   - provider-synced skills must not be silently promoted to human-authored trust.
4. Add reconciliation semantics:
   - detect provider updates/removals
   - mark AgentDB skill records stale/archived when source changes.
5. If provider does not expose skills API:
   - document capability as unavailable for direct sync
   - rely on runtime directory tracking and/or explicit SDK-level imports.

Rationale:

- Provider memory ingestion and provider skill sync are related but distinct pipelines.
- This keeps AgentDB aligned with governance-first scope while supporting practical provider ecosystems when APIs allow it.

---

## Phase 4 — Semantic Search + pgvector
**Exit criterion: embedding-based semantic search active. Semantic conflict detection live. pgvector backend available.**

**Status: not started, correctly phase-gated** — no `retrieval/embedding.py`, no vector library usage anywhere in the codebase.

**Scope narrowed 2026-07-11:** build local-only first (`all-MiniLM-L6-v2` via sentence-transformers) — zero API cost, zero API-key/network dependency, nothing to configure. The spec below supports OpenAI and Voyage backends too; treat those as deferred until the local model is proven insufficient for retrieval quality. Shipping all three backends up front is more surface (config branching, two extra API integrations, two extra sets of credentials to manage) than Design Principle 11 ("memory assets are subordinate... only in scope when they improve recall/reduce conflicts/increase accuracy") justifies before there's evidence the local model isn't good enough.

> **[DECISION required before starting]** Confirm the local model is `all-MiniLM-L6-v2` (384 dims) and not some other sentence-transformers model. If/when local proves insufficient, revisit `text-embedding-3-small` (OpenAI, 1536 dims) or `voyage-3` (Voyage AI, 1024 dims) — both remain in the spec below for that path.
>
> Store model name in `embedding_version` on every record from this point forward.
> This field enables reindexing when the model is upgraded.

---

### Task 11: Embedding Service

**Files:**
- Create: `agentdb/retrieval/embedding.py`
- Create: `tests/unit/test_embedding.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_embedding.py
import pytest
from unittest.mock import patch, MagicMock
from agentdb.retrieval.embedding import EmbeddingService

def test_embed_returns_float_list():
    svc = EmbeddingService(model="local:all-MiniLM-L6-v2")
    vec = svc.embed("hello world")
    assert isinstance(vec, list)
    assert all(isinstance(x, float) for x in vec)
    assert len(vec) > 0

def test_embed_batch_returns_list_of_lists():
    svc = EmbeddingService(model="local:all-MiniLM-L6-v2")
    vecs = svc.embed_batch(["hello", "world"])
    assert len(vecs) == 2

def test_model_version_property():
    svc = EmbeddingService(model="local:all-MiniLM-L6-v2")
    assert svc.model_version == "local:all-MiniLM-L6-v2"

def test_embed_cache_hit(tmp_path):
    svc = EmbeddingService(model="local:all-MiniLM-L6-v2")
    v1 = svc.embed("same text")
    v2 = svc.embed("same text")
    assert v1 == v2  # cached — same object contents
```

- [ ] **Step 2: Write `agentdb/retrieval/embedding.py`**

```python
"""
Embedding service. Supports:
  local:all-MiniLM-L6-v2  — sentence-transformers, zero API cost
  openai:text-embedding-3-small — OpenAI API
  voyage:voyage-3           — Voyage AI API

Model is set at AgentDB() init and stored on every embedded record
in embedding_version. Changing model requires reindexing (migration tooling
in Phase 5).
"""
import hashlib
from functools import lru_cache
from typing import Optional


class EmbeddingService:
    def __init__(self, model: str = "local:all-MiniLM-L6-v2",
                 api_key: Optional[str] = None):
        self._model = model
        self._api_key = api_key
        self._cache: dict[str, list[float]] = {}
        self._encoder = self._load_encoder()

    def _load_encoder(self):
        if self._model.startswith("local:"):
            model_name = self._model[len("local:"):]
            try:
                from sentence_transformers import SentenceTransformer
                return SentenceTransformer(model_name)
            except ImportError:
                raise ImportError(
                    "sentence-transformers required for local embeddings: "
                    "pip install sentence-transformers"
                )
        return None  # API-based models call out in embed()

    def embed(self, text: str) -> list[float]:
        cache_key = hashlib.sha256(text.encode()).hexdigest()
        if cache_key in self._cache:
            return self._cache[cache_key]
        vec = self._embed_one(text)
        self._cache[cache_key] = vec
        return vec

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self._model.startswith("local:"):
            vecs = self._encoder.encode(texts, convert_to_numpy=True)
            result = [v.tolist() for v in vecs]
            for text, vec in zip(texts, result):
                key = hashlib.sha256(text.encode()).hexdigest()
                self._cache[key] = vec
            return result
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        if self._model.startswith("local:"):
            vec = self._encoder.encode([text], convert_to_numpy=True)[0]
            return vec.tolist()
        if self._model.startswith("openai:"):
            import httpx
            model_name = self._model[len("openai:"):]
            resp = httpx.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"input": text, "model": model_name},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
        if self._model.startswith("voyage:"):
            import httpx
            model_name = self._model[len("voyage:"):]
            resp = httpx.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"input": [text], "model": model_name},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
        raise ValueError(f"Unknown embedding model: {self._model}")

    @property
    def model_version(self) -> str:
        return self._model
```

- [ ] **Step 3: Run tests (requires `sentence-transformers` for local model tests)**

```bash
pip install sentence-transformers
pytest tests/unit/test_embedding.py -v
```
Expected: all 4 PASS.

- [ ] **Step 4: Commit**

```bash
git add agentdb/retrieval/embedding.py tests/unit/test_embedding.py
git commit -m "feat: embedding service with local/openai/voyage backends, content-hash cache"
```

---

### Task 12: Semantic Conflict Detection + pgvector Backend

These are described in the plan — implementation tasks to be expanded once the embedding model decision is confirmed. Stubs are in place from Phase 1. Full implementation follows the same TDD pattern as prior tasks.

**Blocking decision: confirm embedding model before starting this task.**

Outline of what Task 12 will deliver:
- `PostgresMemoryStore(MemoryStoreBackend)` in `agentdb/core/memory_store.py`
- `ConflictDetector.check()` extended with cosine similarity comparison against existing records
- `MultiSignalRanker` semantic signal activated (weight_semantic > 0)
- Migration: `migrations/001_initial.sql` with pgvector schema

---

### Task 13: Skill Draft Reconciliation Pipeline

**Status: not started. Designed 2026-07-11, out of a session-long discussion about whether AgentDB's own async (12b) capabilities could help this repo remember its own process rules** (specifically: migrate completed plan tasks to `completed-tasks.md`, delete fully-completed plan files). That discussion concluded the answer for *this repo's own maintenance* is "no" — no Claude Code session working on agentdb is wired to retrieve from an AgentDB instance, so there's no attachment point for a hook or a dispatch to reach. But the underlying capability — turn a recurring, human-approved instruction into a governed skill, with conflict-aware reconciliation before it's committed — is a real, general AgentDB feature in its own right, useful for any deployment where an agent is wired to AgentDB (e.g. slack-assistant). This task builds that feature, not something specific to this repo.

**Dependencies (why this sits here, not earlier):** Task 12 (semantic conflict detection) for reconciling drafts against existing skills/other drafts/changed files by meaning, not just exact keyword overlap; Task 11 (embeddings) as Task 12's own prerequisite; the skill store (`skills/skill_store.py`, done) and skill-store governance fix below; `db.gateway` (Task 16) for the `propose`/`approve`/`reject` surface and the `dispatch` call; `agentdb_stats()` (done).

**Design directive: recycle methods, don't invent parallel ones.** Every piece of this pipeline reuses an existing mechanism rather than adding a new subsystem next to it — that's not a style preference, it's the point of the design. Reuse map:

| Pipeline need | Reuses | Does not add |
|---|---|---|
| Store a draft | A plain `MemoryRecord`, `origin="agent_inferred"`, key `memory/skill_drafts/{draft_id}` | No new table |
| Classify update-vs-create | Same keyword/entity matching `retrieve_by_entities()` already does, against existing skills' `scope`/content | No new matcher |
| Detect draft-vs-draft / draft-vs-skill conflicts | Task 12's conflict detector, applied to drafts as well as stored memories | No draft-specific conflict engine |
| Detect draft-vs-changed-file conflicts | `core/file_integrity.py` (built 2026, never wired to anything — see `docs/context/capabilities.md`) as a cheap hash pre-filter, then Task 12's embeddings only when the hash actually changed | No new file-watching mechanism |
| Notify a human reviewer | `db.gateway.dispatch("skill_draft_proposed"/"skill_draft_conflict", ...)` — the existing 12b mechanism (Design Principle 12b), now accessed via `db.gateway` | No new pub/sub |
| Metric-driven triggers | New fields on `agentdb_stats()` (repeated-correction cluster count, pending-draft backlog count), computed from the existing `outcomes` table and `memory/skill_drafts/*` records | No new stats surface |
| Commit a draft | `skills_api.create_skill()` / `update_skill_version()` (existing) | No new skill-writing path |

**Prerequisite fix, part of this task, not optional:** `SkillStore.update()` (via `SkillsAPI.update_skill_version()`) currently has no protection against being called by non-human-initiated code — it silently inherits `authored_by` from the original skill and lets any caller rewrite content, unlike `create()` which at least checks `authored_by == "human"` at the call site. This must be closed before this pipeline exists, otherwise the pipeline's own "approve" step would be indistinguishable from any other code path that happens to call `update_skill_version` directly. Fix: `update_skill_version`/`create_skill` remain the only two methods that touch `skills.sqlite` content; nothing in this task's "propose" surface may call either.

**Pipeline:**

```
db.gateway.propose(name, content, scope, agent_id, entities=None, session_id=None)
    → classify: match content/entities against existing skills (reuse retrieve_by_entities matching)
        → action="update"(target_skill_id) if a good match, else action="create"
    → reconcile (batch, not isolated to this one draft):
        - vs. other pending drafts sharing entities/scope → flag if contradictory
        - vs. the target skill's current content, if action="update" → always flagged for review (any change to a human skill needs eyes)
        - vs. files tagged with the same entities: file_integrity hash changed since draft/skill was last touched?
          → if yes, escalate to Task 12's semantic comparison to decide if the change is meaningfully conflicting
    → write memory/skill_drafts/{draft_id}  (status="pending"|"conflicted", action, target_skill_id, conflicts=[...])
    → db.gateway.dispatch("skill_draft_proposed" | "skill_draft_conflict", draft_id=..., agent_id=..., session_id=...)

[metric triggers, computed from existing data, not a new scheduler]
    → agentdb_stats() gains: repeated_correction_cluster_count (human_correction outcomes sharing entities, in a window),
      skill_draft_backlog_count (pending drafts)
    → either crossing a threshold → db.gateway.dispatch("skill_candidate_threshold_reached" | "skill_draft_backlog_high", ...)

db.gateway.approve(draft_id, resolution=None)
    → if status == "conflicted": require an explicit resolution argument, refuse otherwise
    → action="create" → skills_api.create_skill(...); action="update" → skills_api.update_skill_version(...)
    → archive the draft record, audit-log skill_draft_approved

db.gateway.reject(draft_id, reason=None)
    → archive the draft record, audit-log skill_draft_rejected, skills.sqlite untouched
```

**Decision resolved 2026-07-13:** `propose`/`approve`/`reject` live on `db.gateway` — a unified `Gateway` class (see Task 16) that also owns `subscribe`/`dispatch`/`conflict_queue`. Not on `AgentDB` directly and not a separate `SkillDraftsAPI` — `db.gateway` is the single surface for all routing, notification, and proposal workflow concerns.

**Implementation order** (per this repo's TDD convention — tests first for each slice):
1. `SkillStore.update()` governance fix (prerequisite, standalone, no dependency on the rest).
2. `db.gateway.propose`/`approve`/`reject` with classification, no conflict-checking yet (draft always `status="pending"`).
3. Conflict reconciliation (depends on Task 12 existing — do not build a bespoke keyword-only conflict heuristic here as a stand-in; wait for Task 12 rather than duplicating it in a phase-inconsistent way, per Design Principle 8).
4. Metric-driven triggers.

---

## Phase 5 — MCP Server + Framework Adapters

**Status: not started.** See Current Status → Recommended next actions #5 for the adapter re-plan approach (start from zero, do not assume any prior partial implementation).

### Task 14: MCP Server

**Type: `memory_write`/`memory_retrieve` are thin transport over already-real-time-enforced operations; `conflict_queue` and `memory_subscribe` are thin transports over the 12b notifier dispatcher** (per `docs/context/architecture-principles.md` Principle 12/12b) — MCP is a consumer of that mechanism, not the thing that defines it.

**Files:**
- Create: `agentdb/interface/mcp_server.py`

**Reframed 2026-07-11 — this is not an event-bus-subsystem build.** All 4 tools map to functionality that already exists or is already planned: `memory_write`/`memory_retrieve` wrap `write()`/`retrieve()`; `conflict_queue` and `memory_subscribe` both just call `db.gateway.subscribe(trigger, handler)` — the existing live, in-memory dispatcher now accessed via `db.gateway` (Task 16, see `docs/context/capabilities.md`) — to register an MCP-side callback against `conflict_detected` (and any other trigger a consumer wants). There is no separate "event bus subsystem" to design or build; the durable/sequence-numbered version Design Principle 7 originally implied is explicitly *not* the default and should only be added later if a specific consumer would lose real value from a missed notification during a restart.

**Sequencing:** ship `memory_write`/`memory_retrieve` first (no new dependency). `conflict_queue`/`memory_subscribe` are blocked on Task 6's real conflict detection dispatching a `conflict_detected` trigger in the first place — there's nothing meaningful to subscribe to until stored records actually get flagged. Once that exists, `conflict_queue`/`memory_subscribe` are a few lines of MCP-side handler registration against `db.gateway`, not new plumbing.

Note this also means a Slack/Telegram notifier doesn't need MCP or this task at all — a host app (e.g. slack-assistant, or the Hermes adapter) can call `db.gateway.subscribe("conflict_detected", handler)` directly once Task 6 dispatches that trigger, entirely independent of whether Task 14 ever ships.

Full task expansion after Phase 3 is complete.

### Task 15: Framework Adapters

> **Read before implementing:**
> - **Hermes:** `docs/integrations/hermes.md` — full design brief: 14-hook mapping table (verified against Hermes source), session key mapping, skill coexistence, `on_pre_compress` summarizer wiring, `db.gateway.subscribe` conflict notification pattern, open questions. This is the spec; what follows is the plan.
> - **OpenClaw:** `docs/integrations/openclaw.md` — sidecar shape, four open questions that must be resolved before writing code.
> - **Assessment:** `docs/integrations/openclaw-hermes-assessment.md` — why Hermes first, governance wrapper strategy (C) for both.

**Type: 12a, real-time enforcement.** Per `docs/context/architecture-principles.md` Principle 12, an adapter is not "done" on API existence alone — it must name the exact point in the framework's own control flow where AgentDB governance executes, and a test must prove that framework-triggered point changes what context/instructions reach the agent.

**Build order updated 2026-07-13:** Hermes first, then OpenClaw. deepagents deprioritized — `AgentDBBackend` already covers it generically and no real consumer has asked for the dedicated adapter. All adapters must subclass `GatewayAdapter` (Task 16) — never call `db.register_reconciliation_handler`/`dispatch_reconciliation_trigger` directly.

**Hermes status: complete (2026-07-14). Commits 4ba16eb..6940fa7 + a1f6c4d. AgentDBHermesProvider implements MemoryProvider ABC + GatewayAdapter; 14 hooks; Principle 12a proven via HermesMemoryManagerShim tests. OpenClaw not started.**

**Files:**
- Create: `agentdb/adapters/hermes.py` — implement Hermes's `MemoryProvider` ABC as a `GatewayAdapter` subclass. Direct Python import, no sidecar. Full design in `docs/integrations/hermes.md`. **Build this first.**
- Create: `agentdb/adapters/openclaw.py` — local Python sidecar (HTTP). Governance wrapper approach (C). Three open questions in `docs/integrations/openclaw.md` must be resolved before writing code. **Build after Hermes.**
- Defer: `agentdb/adapters/deepagents.py` — deprioritized; `AgentDBBackend` covers it generically today.
- Defer: `langgraph.py`, `crewai.py`, `openai_agents.py`, `google_adk.py`, `autogen.py` — build only when a specific framework's users ask for one.

---

### Task 16: `db.gateway` Package

**Status: complete (2026-07-14). Commits 00dd95e..5bc0848 (impl) + a1f6c4d (final fixes). Gateway class + GatewayAdapter base; proposal workflow; conflict queue wired to real store.list_conflicts(). AgentDBClaudeMiddleware updated to inherit GatewayAdapter.**

**Type:** Conceptual unification — not a new runtime process. `Gateway` wraps the existing in-memory reconciliation dispatcher and adds the proposal workflow surface. No new event bus, no new storage.

**Why now (before the public client gap):** the public client gap will need to call `db.gateway.dispatch("conflict_detected", ...)` when it lands. All new adapters (Hermes, OpenClaw) and the Task 13 proposal workflow build on `db.gateway`. Building this first keeps everything downstream consistent and avoids a second migration later.

**Files:**
- Create: `agentdb/gateway/__init__.py` — `Gateway` class
- Create: `agentdb/gateway/adapter.py` — `GatewayAdapter` base class
- Modify: `agentdb/interface/client.py` — add `db.gateway` property; keep `register_reconciliation_handler`/`dispatch_reconciliation_trigger` as pass-through aliases (no breaking change)
- Modify: `agentdb/adapters/claude_agent_sdk.py` — inherit `GatewayAdapter` (one-line class declaration change); use `self.gateway.subscribe`/`dispatch` for any new notification wiring
- Create: `tests/unit/test_gateway.py`

**`Gateway` class surface:**

```python
class Gateway:
    # Reconciliation (moved from AgentDB top-level — aliases kept there)
    def subscribe(self, trigger: str, handler: Callable) -> None: ...
    def dispatch(self, trigger: str, **kwargs) -> None: ...

    # Conflict queue (was planned for MCP Task 14 — now lives here)
    def conflict_queue(self, resolved: bool = False) -> list[dict]: ...

    # Proposal workflow (was Task 13's open question — now resolved)
    def propose(self, name, content, scope, agent_id, entities=None, session_id=None) -> str: ...
    def approve(self, proposal_id: str, resolution=None) -> None: ...
    def reject(self, proposal_id: str, reason=None) -> None: ...
    def list_proposals(self, status: str = "pending") -> list[dict]: ...
```

**`GatewayAdapter` base class:**

```python
class GatewayAdapter:
    def __init__(self, db: "AgentDB", agent_id: str, profile: str = "budget_optimized"):
        self._db = db
        self._agent_id = agent_id
        self._profile = profile
        self.gateway = db.gateway  # direct reference, no extra wiring

    # Subclasses implement these (pattern follows Hermes MemoryProvider — not enforced as ABC)
    def prepare_turn(self, user_query: str, session_id: str): ...
    def pre_tool_use(self, tool_name: str, tool_input: dict, session_id: str): ...
    def finalize_turn(self, session_id: str, **kwargs): ...
```

**Backward compatibility:** `AgentDB.register_reconciliation_handler` and `AgentDB.dispatch_reconciliation_trigger` remain as pass-through aliases to `db.gateway.subscribe`/`dispatch`. No existing call sites break. Aliases are not deprecated yet — remove them only when all known consumers have migrated.

**`propose`/`approve`/`reject` in this task:** implement the surface and storage only (write draft as `agent_inferred` memory record, archive on approve/reject, audit-log each step). Conflict reconciliation is Task 13 scope (depends on Task 12). In this task, drafts always start as `status="pending"`.

**Tests to write:**
```python
# tests/unit/test_gateway.py
def test_subscribe_and_dispatch_fires_handler()
def test_conflict_queue_empty_before_any_conflicts()
def test_propose_writes_memory_record()
def test_approve_calls_skills_api()
def test_reject_archives_draft_without_touching_skills()
def test_list_proposals_filters_by_status()
def test_gateway_adapter_inherits_gateway_reference()
def test_claude_middleware_still_works_after_migration()  # regression
```

---

## Phase 6 — Production Hardening

**Status: not started.**

Items (detail deferred to closer to Phase 4 completion):
- [ ] Out-of-process permission enforcement for server mode
- [ ] Cryptographic signing of human-approved zone
- [ ] SSO / RBAC for managed cloud
- [ ] Rate limiting on MCP server
- [ ] `embedding_version` migration tooling (reindex all records on model change)
- [ ] Conflict queue CLI
- [ ] SLA monitoring

---

## Known Limitations (to document in `LIMITATIONS.md`)

| Limitation | Active from | Resolution |
|---|---|---|
| Semantic search uses recency sort (no vectors) | Phase 1–3 | Phase 4 (embeddings + pgvector) |
| Semantic conflict detection not active | Phase 1–3 | Phase 4 (cosine similarity) |
| All three provider syncs use polling, no push | Phase 3 | When providers expose push APIs |
| Causal score approximated via retrieval_count | Phase 2–3 | Phase 5 full WAL correlation (CausalGraph, prerequisites already met — see Algorithm Reference) |
| In-process permission enforcement bypassable | Phase 1–3 | Phase 6 server mode |
| Semantic weights = 0 until embeddings active | Phase 1–3 | Phase 4 |
| deepagents backend compatibility tested against >=0.6.7 only | Phase 1 | Test against new versions as released |
| Skill health score entity detection is keyword-based, not semantic | Phase 2 | Phase 6+ (semantic entity matching) — a skill using a synonym for a deprecated entity will not be flagged |
| Org knowledge tier (`memory/org/`) has no automated ingestion in v1 | Phase 1 | Content must be written manually or via SDK; automated ingestion from internal wikis/docs is post-launch scope |
| numpy not installed by default | Phase 1–3 | `pip install agentdb[embeddings]` activates numpy-backed semantic scoring; pure Python fallback is always available |
| Google Vertex AI provider is experimental | Phase 3 | MemoryBankService API is less stable than Anthropic/OpenAI equivalents. Treat as beta. Pin to specific API version in provider code. |
| ~~Real conflict detection not active~~ | ~~Phase 1–4~~ | Done 2026-07-14 — `ConflictDetector` wired into `write()`/`retrieve()` |
| No budgeted context assembly (`budget_tokens`/`token_estimate`/`truncated`) | Phase 1–4 | Public client Task 6 gap — see Current Status → Partial → Public client |

---

## Decisions Log

### Resolved (2026-07-14)

| Decision | Resolution |
|---|---|
| Recommended next actions ordering | ~~`db.gateway` → public client gap → Task 9 → Hermes adapter~~ all done 2026-07-14 → **OpenClaw adapter** → Phase 3 (Task 10 provider ingestors) |
| Adapter order | Hermes first (`MemoryProvider` ABC, direct Python), OpenClaw second (sidecar). deepagents deprioritized. |
| OpenClaw integration shape | (C) governance wrapper — not backend replacement (A) or additive layer (B). See `docs/integrations/openclaw-hermes-assessment.md`. |
| Task 9 scope | Configurable on-demand sweep (`db.lifecycle.run_sweep()`): archival + gentle decay (off by default). No background scheduler. |
| SkillDraftsAPI vs AgentDB | Resolved as `db.gateway.propose`/`approve`/`reject`/`list_proposals` — unified `Gateway` surface (Task 16). |
| Conflict queue UI | `db.gateway.conflict_queue()` — foundation for any consumer (MCP, CLI, Slack/Telegram notifier). |
| `register_reconciliation_handler` / `dispatch` placement | Moved to `db.gateway.subscribe` / `db.gateway.dispatch`. Pass-through aliases kept on `AgentDB`. |
| Embedding model default | `nomic-embed-text-v1.5` (768 dims, Apache 2.0). Configurable in `memory.yaml` under `embeddings.model`. |
| Conflict similarity threshold | 0.85 default. Configurable in `memory.yaml` under `retrieval.conflict_threshold`. |
| Summarisation | Configurable callable: `AgentDB(summarizer=fn)`. Default `None` (truncation fallback). Attached to budget manager. `on_pre_compress` in Hermes adapter is the primary call site. |
| Conflict queue UI format | `db.gateway.conflict_queue()` is the foundation. First concrete consumer: slack-assistant notification hook via `db.gateway.subscribe("conflict_detected", handler)`. No separate CLI/web surface in Phase 5. |
| Surface consolidation pattern | Principle 13 added to architecture-principles.md. All future multi-method capabilities serving a distinct consumer must land on a named sub-surface (`db.X`), not top-level `AgentDB`. Confirmed table: `db.gateway` (Task 16), `db.lifecycle` (Task 9), `db.providers` (Task 10), `db.system`/`db.skills_api`/`db.audit_log` (implemented). `db.lifecycle` specifically requires `LifecycleManager` exposed as `db.lifecycle`; `db.record_outcome()` becomes a backward-compat alias. |

### Open (resolve before starting the relevant phase)

| Decision | Needed before | Notes |
|---|---|---|
| OpenClaw hook names | `openclaw.py` adapter | Exact turn-lifecycle hook name(s) for pre-response/post-response unverified — confirm via OpenClaw plugin SDK docs. See `docs/integrations/openclaw.md` open questions. |
| OpenClaw sidecar latency | `openclaw.py` adapter | Confirm latency cost of sidecar call inside `before_tool_call`'s inline path is acceptable. |
| OpenClaw memory-core migration | `openclaw.py` adapter | Backfill story from memory-core's existing SQLite index for opted-in agents. |
| Conflict similarity threshold tuning | Phase 4 | 0.85 is the starting default; needs a labelled test set to tune against real conflict pairs. |
