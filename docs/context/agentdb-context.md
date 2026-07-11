# AgentDB Project Context

## Purpose

Provide a single reusable project-level context document that summarizes code features so agentic workers can implement tasks without re-reading the full plan each time.

## Source Of Truth

- Primary: `/Users/jm/Documents/github/agentdb/docs/context/agentdb-context.md`
- Supporting plan: `/Users/jm/Documents/github/agentdb/docs/superpowers/plans/2026-06-06-agentdb-revised.md`
- Database schema and memory assets: `/Users/jm/Documents/github/agentdb/docs/context/database-schema.md`
- Testing strategy: `/Users/jm/Documents/github/agentdb/docs/context/testing-strategy.md`

If conflicts exist between planning notes, treat the canonical superpowers plan as authoritative for technical behavior and delivery scope.

## When To Use

Use this context when:

- Implementing any AgentDB feature from the phased roadmap.
- Needing quick architecture context before coding.
- Mapping tests to feature intent.
- Verifying whether a capability is current, phased, or explicitly deferred.

Do not use this context for:

- Non-AgentDB repositories.
- Generic Python packaging guidance unrelated to PLAN-defined features.

## Feature Context Map

### 1. Database-first identity

- Product is a governed memory database, not a prompt framework.
- API and introspection should feel database-native.
- Product focus is actionable memory, not memory-asset accumulation.
- AgentDB should complement existing runtime memory by making it more trustworthy, more relevant, and better timed.

### 2. Backend integration contract

- `AgentDBBackend` is framework-agnostic.
- Core backend operations are key-value style; adapters map framework protocol semantics onto this backend.
- deepagents is flagship, but not the API owner.

### 3. Governance boundaries

- Human-approved zone and agent-inferred zone are enforced at write time.
- Permission router is authoritative for writes.
- Violations raise `PermissionDenied` and are logged.

### 4. Memory model primitives

- Core entities: memory records, conflict records, and skills.
- Metadata supports provenance, confidence, expiry policy, lifecycle, and retrieval signals.

### 5. System tables

- Queryable relations expose memory catalog, agent summaries, conflict queue, and global stats.
- Goal: pg_catalog-like introspection for memory state.

### 6. Audit/event split

- Append-only audit log and event bus are separate subsystems.
- Shared sequencing is allowed; implementation coupling is not.

### 7. Conflict model

- Temporal and authority conflict detection are required earlier phases.
- Semantic conflict detection is explicitly phase-gated to embedding availability.
- Never imply semantic conflict detection before embeddings are active.

### 8. Retrieval model

- Multi-signal ranking is foundational.
- Signals: provenance, recency, confidence, causal; semantic activates in embedding phase.
- Conflict flags are assembled before memory content is returned.
- Causal signal ships in two stages: Phase 2 approximation (retrieval frequency × recency weight); Phase 5 full implementation via `CausalGraph`.
- Retrieval exists to improve action quality at turn start and pre-action time, not to maximize memory recall for its own sake.

### 8a. CausalGraph (Phase 5)

- New class: `agentdb/retrieval/causal_graph.py`. Separate from `AuditLog` — same separation as `pg_catalog` vs WAL.
- No external graph library. Two plain Python adjacency dicts: `record_to_retrievals: dict[str, set[int]]` and `retrieval_to_outcomes: dict[int, list[float]]`.
- Single public query: `success_rate(record_id) -> float` — two-hop traversal returning fraction of linked outcomes ≥ 0.7. Returns 0.5 (neutral) when no outcomes exist.
- Wired into `MultiSignalRanker._causal_score()` replacing `freq_score · recency_weight`. Causal weight (0.15) and all other weights are unchanged.
- Requires Tasks A and B (retrieval event logging, outcome recording) to be producing audit entries before implementation.
- Future: if memory-to-memory causal edges are added (Phase 6+), evaluate `networkx` at that point. Do not add it now.

### 9. Skills model

- Skills are governed database objects, not loose prompt files.
- Versioned skill records include computed health score.
- Health score degradation and warnings are first-class behavior.
- Skills and retrieval algorithm should be treated as one foundation: algorithm ranks what matters now; skills provide maintained guidance to inject with that context.

### 10. Lifecycle and reinforcement

- Confidence decay and archival policies manage staleness.
- Outcome-linked reinforcement updates confidence based on retrieval-outcome linkage.
- Human-approved and provider-ingested constraints must be respected.

### 11. Compaction protocol

- Pre-compaction checkpoint promotes durable working context.
- Post-compaction summary persists compressed context with provenance tags.
- Compaction events are auditable.

### 12. Provider ingestion

- Anthropic, OpenAI, and Google provider ingestors write with provider-origin semantics.
- Polling-only limitations are explicit until push APIs exist.

### 13. Packaging and execution expectation

- Project should support package-style imports under editable install workflows.
- Tests should validate behavior through package imports, not ad hoc path hacks.

### 14. Priority enforcement

- Context assembly order is a first-class concern for effective instruction priority.
- Retrieval should support external-context conflict checks and explicit override instructions.
- Audit and stats should surface external conflict detection and rates.
- Tool-call/system-prompt enforcement pattern is part of practical authority, not optional documentation.

### 15. Action-driven retrieval

- Standard retrieval is query-driven: memories retrieved once at turn start based on user prompt.
- Action-driven retrieval is tool-triggered: memories retrieved at the moment an agent acts on a specific file or command.
- Core primitives: `entities: list[str]` on `MemoryRecord`; `retrieve_by_entities(entities, agent_id)` on `AgentDB`.
- Framework adapters are responsible for write-time entity extraction and tagging.
- Current implementation exposes adapter-side action surfaces in the Claude SDK path:
	- `build_action_context(...)`
	- `pre_tool_use(...)`
	- `build_hook_handlers()` / `runtime.get_sdk_hooks()` for downstream attachment
- Full downstream SDK-specific automatic hook attachment remains owned by app/runtime code.
- Phase 2: entity matching is keyword-based. Phase 4: falls back to embedding similarity for unindexed entities.
- Core/adapter boundary is strict: `retrieve_by_entities` is framework-agnostic; `PreToolUse` hook is Claude-specific.

### 16. Package memory workspace and reconciliation

- AgentDB package installs must use a deterministic memory workspace root (default `~/.agentdb/memory/`, env/config overridable).
- Workspace layout must include config, database, conversations, embeddings, reconciliation-state, and logs subpaths.
- `config/memory.yaml` is the runtime contract for mode and trigger behavior.
- Current implementation includes:
	- workspace root resolution and default materialization
	- `memory.yaml` load + validation
	- config-driven conversation JSONL capture
	- internal reconciliation dispatcher with initial `conversation_started` support
- Required config semantics:
	- `mode`: `full_storage` or `budget_optimized`
	- `tracked_directories`: required for full-storage tracking/reconciliation workflows
	- conversation capture toggle for JSONL storage
	- reconciliation trigger list (`conversation_started`, `file_modified`, `cron`)
	- database mode/config block (embedded vs server)
- Trigger behavior is phase-gated:
	- Phase 2: keyword/entity-index reconciliation only
	- Phase 4+: embedding refresh and semantic reconciliation
- Memory assets inside the workspace are justified only when they improve context quality, reduce conflicts, or increase action accuracy.

### 17. Explicit context-injection wiring

- Wiring must be explicit and testable at adapter boundaries.
- Canonical flow:
	1. Turn start retrieval (`prepare_turn` or equivalent)
	2. Action-driven retrieval (`PreToolUse` or equivalent, once Task F is implemented)
	3. Conflict-first merge with override instructions
	4. Injection into model context (`system` block / `additionalContext` / equivalent)
- Adapters must document hook points and injection surface, not just retrieval APIs.
- At least one integration path must prove end-to-end traceability:
	`trigger -> reconciliation -> retrieval -> injected context in model invocation`.
- Current implemented reference path:
	- `conversation_started` dispatcher
	- turn-start retrieval in Claude middleware
	- `pre_tool_use(...)` / `PreToolUse` handler map for downstream action-timed injection

## Phase-Level Delivery Context

- Phase 1: backend interface + foundational storage/governance primitives.
- Phase 2: actionable-memory core — ranking, skills, injection timing, workspace config, and reconciliation triggers.
- Phase 3: provider ingestion and broader practical usage only after the core loop proves useful.
- Phase 4: embeddings and semantic upgrades only where they improve action quality or reduce conflicts.
- Phase 5: MCP server + adapter completion.
- Phase 6: hardening and production controls.

Additional roadmap requirement:
- Package memory workspace + reconciliation trigger orchestration is part of the early implementation backlog and must be considered before provider/MCP expansion.

## Execution Rules For Agents

1. Read current repository status before assuming a phase is complete.
2. Implement minimum cohesive slices with tests.
3. Preserve phase-gated constraints from the canonical superpowers plan.
4. Avoid documenting a capability as active before tests and code verify it.
5. When ambiguity exists, defer to the canonical superpowers plan wording.

## Expected Output In Task Work

When using this context, workers should report:

- Which feature group(s) were touched.
- Which phase requirement(s) were satisfied or deferred.
- Any limitation or decision dependency encountered.
- Test evidence validating the intended behavior.
