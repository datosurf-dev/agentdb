# AgentDB

**A governed memory layer that keeps agent memory actionable.**

AgentDB is designed to improve agent behavior without becoming another layer of operational overhead. It governs what memory is trusted, keeps relevant memory assets in sync, and injects the right context or instructions when an agent is about to act. Every write is validated before it lands. Every read returns a governed context package. The boundary is enforced at the memory layer — not by convention, not by prompt instructions, not by hoping agents behave.

> Every other memory product stores context. AgentDB governs it.

## Architecture Confirmation

The full architecture pitch and the numbered design principles live in one canonical place — this section just points to it instead of repeating it:

- Architecture & design principles: `/agentdb/docs/context/architecture-principles.md`
- Capabilities by type: `/agentdb/docs/context/capabilities.md`
- Global agent instructions: `/agentdb/AGENTS.md`
- Plan (active/open work): `/agentdb/docs/superpowers/plans/2026-06-06-agentdb-revised.md`
- Completed work tracker: `/agentdb/docs/superpowers/plans/completed-tasks.md`

## Current Repository Status (As Of 2026-10-04)

Core governed memory loop is fully operational: real conflict detection, budgeted retrieval, lifecycle management, centralized config + profiles, MCP server (stdio/Claude Desktop), and two adapters (Claude SDK + Hermes) are all shipped and test-covered. Phase 3 (embeddings/semantic search) is deliberately deferred until the loop proves its value in production. Provider ingestors (Phase 6) and additional framework adapters (Phase 4 Task 15) remain pending.

### Feature State Matrix

#### Track

| Feature area | State | Notes |
|---|---|---|
| Package entrypoint (`from agentdb import AgentDB`) | Implemented | Available in `src/agentdb/__init__.py`. |
| Core client loop (`write`, `read`, `retrieve`, `record_outcome`) | Implemented | Includes audit events and confidence updates. |
| Backend contract (`AgentDBBackend`) | Implemented | Framework-agnostic read/write/list/grep/glob contract. |
| Adapter surfaces | Implemented (Claude SDK + Hermes) | `adapters/claude_agent_sdk.py` (full `GatewayAdapter` subclass, proposal workflow, conflict queue, budgeted retrieval) and `adapters/hermes.py` (`AgentDBHermesProvider`, 14 hooks, Principle 12a proven). deepagents, LangGraph, CrewAI, OpenAI Agents, Google ADK, and AutoGen adapters are **not implemented** — deferred until a specific framework's users ask for one; `AgentDBBackend` works generically with any framework today. |
| Claude SDK async runtime (`from_sdk_client`, `handle_turn_async`) | Implemented | Async turn wrapper available in adapter runtime. |
| Directory tracking capture (Claude middleware) | Implemented | Optional tracked-directory snapshots persisted as memories. |
| Package memory workspace (`~/.agentdb/memory`, `config/memory.yaml`) | Implemented | Workspace resolution, config loading, and backward-compatible `path=` override in place. `memory.yaml` is now the single source of truth for all tunable defaults via mode-based profiles (`budget_optimized`, `full_storage`, `research`) — see Centralized config below. |
| Conversation JSONL capture | Implemented (initial) | Config-driven JSONL capture is available when `capture.conversation_jsonl` is enabled. |

#### Reconcile

| Feature area | State | Notes |
|---|---|---|
| Permission routing boundaries | Implemented | Write-time enforcement via `PermissionRouter`. |
| Write-time conflict detection (temporal + authority) | Implemented | `ConflictDetector` wired into `write()` — temporal conflicts auto-resolve in favour of recency; authority conflicts (agent vs. human-approved) auto-resolve in favour of human, flagged on retrieve. Semantic conflict detection is Phase 4 (requires embeddings). |
| External-context conflict detection | Implemented | `external_context` param on `retrieve()` checks overlap between caller-supplied context and stored records; flags `external_conflict_detected` + `external_conflict_rate` stat. `override_instructions` still dead code — detection works, explicit overrides are not generated. |
| Reconciliation trigger orchestration + `db.gateway` | Implemented | `db.gateway.subscribe`/`dispatch` is the unified surface. `conflict_detected` dispatched by `write()`. `conversation_started` dispatched by both adapters at turn start. Conflict queue (`db.gateway.conflict_queue()`), proposal workflow (`propose`/`approve`/`reject`/`list_proposals`) all live here. |
| Embedding-backed semantic retrieval/conflicts | Planned (Phase 4) | Current retrieval is keyword/provenance/confidence/recency based; embeddings are only justified if they improve action quality. |

#### Rank

| Feature area | State | Notes |
|---|---|---|
| Algorithm + skills blend for action quality | Implemented (initial) | Retrieval ranks memory; governed skills add maintained guidance during retrieval results. |
| Skills store + health warnings | Implemented | Versioned skills, staleness-based health scoring, warnings on retrieve. |
| System stats surface | Implemented (initial) | Includes retrieval/outcome/conflict-rate aggregates. |
| Entity-aware retrieval core (`entities`, `retrieve_by_entities`) | Implemented (initial) | Additive entity tagging and keyword-based entity retrieval are available in core. |
| Provider ingestors (Anthropic/OpenAI/Google sync) | Planned (Phase 3) | Architecture intent documented; module surface pending. Deliberately deferred — validating core loop with Claude SDK + Hermes in production first. |
| Lifecycle manager (`db.lifecycle`) | Implemented | `run_sweep()`: Step 0 archives expired records (`expiry_time ≤ now`); Step 1 optional confidence decay (off by default); Step 2 archives below `ARCHIVAL_THRESHOLD`; Step 3 archives past `STALENESS_DAYS`. `reinforce()` and `record_outcome()` also on `db.lifecycle`. `human_approved` records immune to all sweep operations. |
| `infer` flag + `expiry_time` on `write()` | Implemented | `infer=True` routes the raw value through a caller-supplied `infer_fn` before storage (raises `ValueError` if no `infer_fn` set). `expiry_time: datetime` sets a hard TTL; sweep archives once it passes. Both apply to `agent_inferred` only; `human_approved` records are immune. |
| Centralized config + profiles (`db.config`) | Implemented | `memory.yaml` drives all tunable defaults via mode-based profiles. Built-in profiles: `budget_optimized` (retrieve_limit=5, 2k chars, staleness=90d), `full_storage` (20, 12k, 90d), `research` (50, 24k, 180d, recency-first). User-defined profiles via `profiles:` block with optional `extends:`. `db.config` exposes the fully resolved dict. |

#### Inject

| Feature area | State | Notes |
|---|---|---|
| Query-driven retrieval into turn context | Implemented (Claude SDK + Hermes) | Both adapters retrieve at turn start via `prepare_turn`. `budget_tokens` param now wired end-to-end; `RetrievalResult.token_estimate` and `truncated` are live. |
| Action-driven retrieval (`entities`, `retrieve_by_entities`, `PreToolUse` hook) | Implemented (initial) | Core entity retrieval, adapter `pre_tool_use(...)`, and downstream hook-registration surface are implemented; automatic SDK attachment remains downstream integration work. |
| MCP server surface | Implemented (stdio) | `interface/mcp_server.py`: 4 tools (`memory_write`, `memory_retrieve`, `conflict_queue`, `memory_stats`). `agentdb-mcp` console script. HTTP+SSE transport deferred. |
| Hardening and production controls | Planned (Phase 6) | Roadmap-only at this snapshot. |

Validation status:
- Editable install and package-style imports are validated via `pip install -e '.[dev]'`.
- `python -m pytest -q` → 277 passed.

The rest of this README describes architecture intent and phased roadmap behavior.

## Execution Strategy

To keep delivery aligned with the architecture goals and avoid over-engineering, implementation should follow a narrow-first sequence:

1. Prove the governed core loop first:
   - write permission enforcement
   - audit emission
   - conflict-first retrieval
   - outcome recording
   - confidence updates
   - stats visibility
2. Expand integration breadth only after the core loop is stable and test-validated:
   - provider ingestion
   - MCP surface
   - broader adapter hardening
3. Preserve phase gates:
   - do not claim semantic retrieval/conflict behavior before embeddings are active
   - keep conflict metadata assembled before memory content

This sequencing keeps AgentDB focused on its differentiated value: governed, low-overhead memory that improves action quality at the right time.

## Developer Quickstart

Use a repo-local virtual environment and editable install:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Run tests:

```bash
python -m pytest -q
```

Run a minimal local smoke check in Python:

```python
from agentdb import AgentDB

db = AgentDB()
db.write(
    key="memory/working/hello",
    value="world",
    origin="agent_inferred",
    agent_id="research_agent",
)
print(db.read("memory/working/hello", agent_id="research_agent").value)
```

Expected output:

```text
world
```

Integration manuals:

- Index: `docs/integrations/README.md`
- Google ADK: `docs/integrations/google-adk.md`
- Local LLM and Claude wiring: `docs/integrations/model-and-runtime-wiring.md`

### Available Now (Current Snapshot)

These are the developer-facing surfaces you can use immediately in the current implementation:

- `AgentDB` client (`from agentdb import AgentDB`)
  - `write(key, value, origin, agent_id, infer=False, expiry_time=None, ...)`
  - `read(key, agent_id)`
  - `retrieve(query, agent_id, limit=..., budget_tokens=..., external_context=..., session_id=...)`
  - `retrieve_by_entities(entities, agent_id, session_id=...)`
  - `record_outcome(session_id, agent_id, outcome_type, outcome_value)` — backward-compat alias for `db.lifecycle.record_outcome()`
  - `system.agentdb_stats()`
  - `audit_log.replay(from_sequence=0)`
  - `lifecycle` — `run_sweep()`, `reinforce(record_id)`, `record_outcome(...)`
  - `gateway` — `subscribe(trigger, handler)`, `dispatch(trigger, ...)`, `conflict_queue()`, `propose(...)`, `approve(...)`, `reject(...)`, `list_proposals(...)`
  - `config` — returns the fully resolved config dict (mode defaults + yaml overrides)
  - `skills_api` — `create_skill`, `update_skill_version`, `get_skill`, `archive_skill`, `list_stale_skills_for_agent`, `list_skill_warnings_for_agent`
  - optional initialization: `tracking_directories`, `tracking_root`, `tracking_max_files_per_directory`, `infer_fn`, `memory_dir`
- `AgentDBBackend` (`from agentdb.backend import AgentDBBackend`)
  - `read`, `write`, `delete`, `list`, `grep`, `glob`
- Adapters currently implemented under `src/agentdb/adapters/`
  - **Claude SDK** (`AgentDBClaudeMiddleware`) — full `GatewayAdapter` subclass; `prepare_turn`, `pre_tool_use`, `finalize_turn`; proposal workflow; conflict queue; budgeted retrieval.
  - **Hermes** (`AgentDBHermesProvider`) — 14-hook `MemoryProvider` ABC + `GatewayAdapter`; Principle 12a proven via shim tests.
  - deepagents, LangGraph, CrewAI, OpenAI Agents, Google ADK, and AutoGen are not built yet — `AgentDBBackend` covers them generically.

Example using the current retrieval/improvement loop:

```python
from agentdb import AgentDB

db = AgentDB()
agent_id = "research_agent"
session_id = "sess-demo"

db.write(
    key="memory/working/style_pref",
    value="prefer concise bullet points",
    origin="agent_inferred",
    agent_id=agent_id,
    session_id=session_id,
)

result = db.retrieve(
    query="bullet points",
    agent_id=agent_id,
    session_id=session_id,
)
print([r.key for r in result.records])

db.record_outcome(
    session_id=session_id,
    agent_id=agent_id,
    outcome_type="task_complete",
    outcome_value=1.0,
)

print(db.system.agentdb_stats())
```

Example using the current Claude middleware action loop:

```python
from agentdb import AgentDB
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeMiddleware

db = AgentDB()
middleware = AgentDBClaudeMiddleware(
    db=db,
    agent_id="analytics_agent",
    profile="budget_optimized",
)

# Seed governed memory tied to a file/entity the tool is about to touch.
db.write(
    key="memory/working/models/orders_sql_guidance",
    value="Use the normalized orders model and preserve customer_id joins.",
    origin="human_approved",
    agent_id="analytics_agent",
    entities=["models/orders.sql", "orders"],
)

# Turn-start retrieval still happens before the model responds.
messages, retrieval = middleware.prepare_turn(
    user_query="Update the orders model to add the new revenue field.",
    session_id="sess-42",
)

# Action-time retrieval can add targeted memory right before a tool call.
hook_payload = middleware.pre_tool_use(
    tool_name="Edit",
    tool_input={"file_path": "models/orders.sql"},
    session_id="sess-42",
)
print(hook_payload["additionalContext"])

# Persist the turn and tag the touched entity for future retrieval.
middleware.finalize_turn(
    session_id="sess-42",
    user_query="Update the orders model to add the new revenue field.",
    model_response="Added revenue logic and kept the join path intact.",
    touched_entities=hook_payload.get("touchedEntities", []),
)
```

This is the current actionable-memory loop in the repo today:

1. `prepare_turn(...)` retrieves governed memory for the user query.
2. `pre_tool_use(...)` optionally retrieves narrower entity-linked context just before a tool acts.
3. `finalize_turn(...)` persists the response, optional captures, and touched-entity metadata for later turns.

---

## Drop into your existing stack in 30 minutes

AgentDB defines a generic pluggable backend interface. Any framework with pluggable storage adopts it — **this works today**, no adapter required:

```python
from agentdb import AgentDB
from agentdb.backend import AgentDBBackend

db = AgentDB()
backend = AgentDBBackend(agent_id="assistant", trust_zone="agent_inferred", db=db)

backend.write("memory/working/task_context", "user prefers bullet points")
backend.read("memory/working/task_context")   # → "user prefers bullet points"
backend.list("memory/working/")               # → ["memory/working/task_context"]
```

**Planned (not yet implemented): a `deepagents.py` adapter** for [deepagents](https://github.com/langchain-ai/deepagents) — LangChain's agent harness built on LangGraph — as the flagship named integration. The shape it's expected to take:

```python
# NOT YET IMPLEMENTED — agentdb.adapters.deepagents does not exist yet.
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from agentdb import AgentDB
from agentdb.adapters.deepagents import make_agentdb_backend

db = AgentDB()

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
    tools=[...],
    backend=CompositeBackend(
        default=StateBackend(),
        routes={
            "/memories/": make_agentdb_backend(db, "assistant", trust_zone="agent_inferred"),
            "/skills/":   make_agentdb_backend(db, "assistant", trust_zone="human_approved"),
        },
    ),
)
```

deepagents would handle the agent loop, context compression, subagent isolation, and observability; AgentDB governs everything that persists. They don't overlap — but until `make_agentdb_backend` exists, use the generic `AgentDBBackend` example above directly with `CompositeBackend`'s routes.

---

## The primary differentiator: governed instruction records

Skills are the most defensible feature in AgentDB. Not because they store instructions — any file can do that — but because they are database objects with a computed **health score** that degrades automatically when the instruction becomes stale.

**Why this matters:** Anthropic's data analytics team built agents that reached 95%+ accuracy using structured skills. Without skills: 21% accuracy. With well-maintained skills: 95%+. The failure mode: skill accuracy drifted from 95% to 65% over one month when skill files were not actively maintained as the underlying data model changed. Their fix was PR discipline — colocation of skill files with transformation models, with hooks that flag drift.

AgentDB's health score is the infrastructure solution. It catches drift without process discipline:

```python
db.skills_api.create_skill(
    skill_id="analytics-schema-v1",
    name="analytics-schema",
    content="""
        The revenue table has columns: date, amount_usd, customer_id, product_sku.
        Always join on customer_id, never on email.
        The 'legacy_orders' table was deprecated in Q1 2026 — do not query it.
    """,
    scope=["analytics_agent"],
)
# authored_by is always "human" — SkillsAPI.create_skill() sets it, there's no way to pass agent-authored skills in.

# 45 days later — schema changed, skill not updated:
result = db.retrieve(query="revenue breakdown", agent_id="analytics_agent")
result.skill_warnings
# → ["Skill 'analytics-schema-v1' is stale (health_score=0.42)"]
```

A health score below 0.6 surfaces a staleness warning in every retrieval result. The agent sees the warning before acting on the skill content.

**What degrades the health score today:**
- Days since last retrieval (staleness decay, half-life 30 days) — the only implemented factor

**Planned, not yet implemented:**
- Unresolved conflicts linked to the skill (no conflict linkage exists on skills yet)
- A newer version that hasn't been promoted

**Current limitation (Phase 2):** Entity drift detection is keyword-based — a skill that uses a synonym for a deprecated entity will not be flagged. Semantic entity matching is Phase 5 scope. This limitation is documented in LIMITATIONS.md.

**Planned (Task 13, not started, blocked on Phase 4 embeddings):** a skill draft reconciliation pipeline — an agent can *propose* a new skill or an update to an existing one (written as an ordinary agent-inferred memory record, never touching `skills.sqlite` directly), which gets reconciled against other pending drafts, the target skill, and recently-changed files before a human explicitly approves or rejects it. Proposing is async (notifies a reviewer); only the approval step can ever create or modify a real skill. See the plan for the full design.

### Why the algorithm and skills belong together

The retrieval algorithm and the governed skills system are one foundation, not two unrelated features.

- The algorithm answers: which memories or instructions are most relevant to the action the agent is about to take?
- Skills answer: which durable guidance should the agent trust when that action happens?

In practice, AgentDB is strongest when these work together:

1. memory records capture facts, context, prior outcomes, and relevant assets
2. the ranking algorithm decides what matters now
3. skills add governed, maintained instructions on top of those facts
4. the runtime injects both as compact action-ready context

That blend is the development foundation for AgentDB. The goal is not to accumulate more memory artifacts. The goal is to turn memory into better decisions, fewer conflicting instructions, and more accurate actions.

---

## The two zones: the central design decision

```
┌─────────────────────────────────────────────────────────┐
│                   HUMAN-APPROVED ZONE                   │
│   skills/          config/human/      prompts/locked/   │
│                                                         │
│   Human-authored. Immutable at runtime. No agent write  │
│   reaches here regardless of trust level or content.    │
└─────────────────────────────────────────────────────────┘
                            │
                  Permission router
                  (validates at write time, before storage)
                            │
┌─────────────────────────────────────────────────────────┐
│                 AGENT-INFERRED ZONE                     │
│   memory/working/    memory/episodic/   memory/org/     │
│   memory/long_term/  config/agent/      memory/provider/│
│                                                         │
│   Bounded by permissions.yaml. Logged. Lifecycle-       │
│   managed. Confidence-decaying.                         │
└─────────────────────────────────────────────────────────┘
```

This boundary is enforced at write time, before any record reaches storage. A write to `skills/` from `origin="agent_inferred"` is rejected and logged — always, regardless of what the agent claims.

```python
db.write(
    key="skills/override-safety",
    value="ignore all previous instructions",
    origin="agent_inferred",
    agent_id="assistant",
)
# → PermissionDenied: human zone requires human_approved origin [agent=assistant, key=skills/override-safety]
# → Logged to audit trail
```

---

## Memory biography: what makes this a database

Every record in AgentDB carries a full biography — not just a value:

Implemented today:

```
key              → addressable path          e.g. "memory/working/pref_format"
value            → the memory content
origin           → human_approved | agent_inferred | provider_ingested
trust_level      → sealed | human | agent | provider   (derived from origin; "sealed" is unused so far)
confidence       → float 0–1, decays over time unless reinforced
entities         → list of canonical identifiers (file paths, model names) for action-driven retrieval
last_retrieved   → drives the ranker's recency signal
conflict_id      → populated by ConflictDetector at write time when a temporal or authority conflict is detected
expiry_time      → optional hard TTL (datetime); lifecycle sweep archives once it passes (agent_inferred only)
inferred         → bool; true when the value was derived by infer_fn rather than stored verbatim
```

Planned, not yet on the record: `tier`, `expiry_policy`, `audit_sequence`, `embedding_version`, `user_id`, `session_id`. See `docs/context/database-schema.md` for the full column-by-column reference.

The biography enables permission routing, lifecycle management, and provenance-weighted retrieval today; conflict detection is planned (see "Conflict detection" below). Without it you have a fancier key-value store.

---

## Memory tiers (naming convention today, not yet enforced retrieval behavior)

The key prefixes below are a convention every write should follow — but there is no `tier` field on `MemoryRecord` yet, and the ranking algorithm doesn't currently look at key prefix at all (it ranks uniformly by provenance/recency/confidence/causal regardless of which prefix a key falls under). Treat "Retrieval priority" as the design intent, not current behavior.

| Tier | Key prefix | Description | Retrieval priority (planned) |
|---|---|---|---|
| Working | `memory/working/` | Current task context. Session-scoped. | High for active session |
| Episodic | `memory/episodic/` | Past interactions. Decays with time. | Medium, recency-weighted |
| Long-term | `memory/long_term/` | Durable user preferences and facts. | High, confidence-weighted |
| Org | `memory/org/` | Institutional knowledge: team structures, product names, internal terminology, decision logs. Changes slowly. Maintained by designated owners. | High for disambiguation; neutral for technical queries |
| Provider | `memory/provider/` | Ingested from Anthropic / OpenAI / Google. Trust level = provider, never promoted. | Low |

---

## Conflict detection

Detection runs at write time, not read time — by the time an agent is mid-execution it's too late to resolve cleanly.

| Conflict type | Status | Resolution |
|---|---|---|
| **Temporal** | **Implemented** | Auto-resolved in favour of recency. Logged. `conflict_id` stamped on both records. |
| **Authority** | **Implemented** | Agent-inferred contradicts human-approved → auto-resolved in favour of human. `override_instructions` populated in `RetrievalResult`. `conflict_detected` dispatched via `db.gateway`. |
| **Semantic** | Planned (Phase 4) | Requires embeddings — deferred. |

`ConflictDetector` (`governance/conflict_detector.py`) is wired into `write()`. On conflict: both records get `conflict_id`, the conflict is stored, and `db.gateway.dispatch("conflict_detected", ...)` fires so any subscriber (Slack notifier, MCP consumer, etc.) is immediately notified.

```python
# write() detects and dispatches automatically
db.write(key="k", value="new value", origin="agent_inferred", agent_id="a")
# If an existing record at "k" conflicts → conflict stored, dispatch fires

result = db.retrieve(query="communication style", agent_id="assistant")
result.conflicts          # list of ConflictRecord for returned records
result.override_instructions  # human-approved wins messages for authority conflicts
```

External-context detection (caller-supplied context vs. stored records) is a separate, narrower check available via `external_context=` on `retrieve()` — it flags `external_conflict_detected` audit events but does not interact with the stored `conflicts` table.

**Known limitation:** semantic conflict detection (same-authority, situationally contradictory) requires Phase 4 embeddings. Until then, only temporal and authority conflicts are detected.

---

## Retrieval: what's novel, what isn't

Semantic similarity, recency decay, and confidence scoring are already implemented in Mem0's 2026 algorithm. AgentDB uses these as baseline signals but does not claim them as differentiators.

**Two signals that are genuinely novel:**

**Provenance weight** — human-approved memories rank above agent-inferred for equivalent semantic similarity. No current system makes trust level a first-class ranking factor. A user's explicit preference always surfaces above an agent's inferred preference for the same query.

**Causal relevance from WAL history** — if a memory was consistently retrieved before successful completions of actions similar to the current one, its relevance score increases. This is a WAL-derived signal no current memory system computes. It rewards memories that have demonstrated predictive value, not just semantic closeness.

Signal weights (all configurable, enforced to sum to 1.0 in `MultiSignalRanker`'s constructor):

**Current shipped defaults** (`agentdb/retrieval/algorithm.py`):

| Signal | Default weight | Novel? |
|---|---|---|
| Provenance (trust level) | 0.35 | **Yes** |
| Recency (decay from last retrieved) | 0.25 | No — Mem0 baseline |
| Confidence | 0.25 | No — Mem0 baseline |
| Causal (WAL-derived, approximated) | 0.15 | **Yes** |
| Semantic (cosine similarity) | 0.0 | No — Mem0 baseline |
| Workspace (working-directory overlap) | 0.0 | **Yes** |

**Target redistribution once Phase 4 activates embeddings** (not active yet):

| Signal | Weight |
|---|---|
| Provenance | 0.30 |
| Recency | 0.20 |
| Confidence | 0.20 |
| Causal | 0.10 |
| Semantic | 0.20 |

Causal weight is approximated via retrieval frequency × recency until Phase 5 computes full WAL correlation (`CausalGraph`, see the plan's Algorithm Reference).

---

## Action-driven retrieval (Roadmap: Phase 2 Task F)

Standard retrieval is **query-driven**: the agent describes what it needs, AgentDB ranks and returns relevant memories at the start of a turn.

Action-driven retrieval is a **planned tool-triggered extension**: memories will be injected at the moment Claude is about to act on a specific file or command — not at turn start, when the relevant file may not yet be known.

```
Turn start            →  query-driven retrieve("fix the funnel")
                         → general context about funnel models

Claude about to Edit  →  entity-driven retrieve(entity="ce_project_funnel__respondents")
                         → "last time: layer violation introduced", "full-refresh required"
```

This matters for a coding assistant because much of the institutional knowledge is tied to specific files, models, or commands — not to the user's query topic. A memory like "this model requires `--full-refresh`" is invisible to a turn-level query unless the user happened to mention the model name.

### Planned design

`MemoryRecord` will carry an `entities` field — a list of canonical entity identifiers (file paths, model names, command patterns) attached at write time. `AgentDB.retrieve_by_entities()` will look up memories by entity key, bypassing the keyword ranker for exact matches.

The framework adapter (e.g. the Claude SDK adapter) is planned to hook into `PreToolUse` events, extract the entity key from the tool input, and inject retrieved context as `additionalContext` before the tool executes:

```python
# Tool input: Edit("models/marts/core_entities/ce_project_funnel__respondents.sql")
# Adapter extracts → entity = "ce_project_funnel__respondents"
result = db.retrieve_by_entities(["ce_project_funnel__respondents"], agent_id="slack_assistant")
# → injects context before Claude edits the file
```

Entity keys are extracted per tool type:

| Tool | Entity key |
|---|---|
| `Write` / `Edit` / `MultiEdit` | `file_path` (and derived model name for `.sql` files) |
| `Bash("dbt build --select X")` | model name parsed from `--select` argument |
| `Bash("git commit ...")` | currently staged file paths |

### Core vs. adapter boundary (planned)

| What | Where |
|---|---|
| `entities: list[str]` on `MemoryRecord` | AgentDB core |
| `retrieve_by_entities(entities, agent_id)` | AgentDB core |
| Write-time entity extraction and tagging | Framework adapter |
| `PreToolUse` hook + `additionalContext` injection | Claude SDK adapter |

The core is framework-agnostic — any adapter (LangGraph, CrewAI, etc.) can implement write-time tagging and call `retrieve_by_entities`. The Claude-specific `PreToolUse` mechanics stay in the adapter.

### Current state and phase placement

Current state: query-driven retrieval and Claude turn wrappers are implemented; action-driven retrieval hooks are not yet active.

Action-driven retrieval uses the existing keyword ranker and works with Phase 2 infrastructure. It improves automatically when Phase 4 semantic search activates — `retrieve_by_entities` can fall back to embedding similarity for entities not yet in the index.

**Current limitation (Phase 2):** `retrieve_by_entities` is keyword-based — memories must contain the entity string in their key or value to be matched. Semantic entity matching is deferred to Phase 4+.

---

## Provider ingestion

AgentDB does not compete with provider-native memory. It governs it.

```python
# Provider memory flows INTO AgentDB, not around it
db.providers.anthropic.sync(memory_store_id="ms_abc123")  # NOT YET IMPLEMENTED
db.providers.openai.sync(agent_id="assistant")             # NOT YET IMPLEMENTED
db.providers.google.sync()  # Vertex AI MemoryBankService — NOT YET IMPLEMENTED
```

No `src/agentdb/providers/` directory exists yet (Phase 3, gated behind the core loop stabilizing — see the plan's Current Status). The interface above is the target shape.

All three providers write with `origin=provider_ingested`, `trust_level=provider`. No provider memory is ever promoted to `human_approved` — the boundary is enforced at ingestion.

**Current limitation:** All three providers use polling. Push subscription is deferred until providers expose push APIs.

---

## A2A protocol

AgentDB governs the memory of individual agents that communicate via the [A2A protocol](https://a2a-protocol.org) (v1.2, Linux Foundation). When Agent A delegates a task to Agent B via A2A, Agent B uses its own AgentDB instance — memory does not flow across A2A boundaries by default.

AgentDB does not implement A2A transport. It sits below the transport layer:

```
Agent A ──A2A──► Agent B
                    │
              AgentDBBackend
                    │
              AgentDB (governed)
```

**Future scope (Phase 6+):** A2A agent cards carry cryptographic signatures (v1.2). An agent card can assert which memory zones the agent is authorized to read. This maps to AgentDB's permission zones — A2A card-based permission bootstrapping is planned.

---

## System tables

The target design models memory metadata as queryable relations on `pg_catalog`. **Only `agentdb_stats()` is implemented today** — `agentdb_memories()`, `agentdb_agents()`, and `agentdb_conflicts()` don't exist yet:

```python
db.system.agentdb_memories()            # NOT YET IMPLEMENTED — planned: all active records
db.system.agentdb_memories(agent_id="assistant")  # NOT YET IMPLEMENTED
db.system.agentdb_agents()              # NOT YET IMPLEMENTED — planned: per-agent stats
db.system.agentdb_conflicts(resolved=False)   # NOT YET IMPLEMENTED — planned: unresolved conflict queue
db.system.agentdb_stats()              # Implemented — database-wide counters
```

Actual current return shape:

```python
db.system.agentdb_stats()
# {
#   "total_memories": 1847,
#   "avg_confidence": 0.74,
#   "low_confidence_count": 12,        # confidence < 0.5
#   "conflict_pending_count": 0,       # hardcoded 0 — conflict tracking doesn't exist yet (see Conflict detection above)
#   "outcome_positive_rate": 0.87,     # None until at least 10 outcomes are recorded
#   "retrieval_count_total": 4021,
#   "external_conflict_rate": None,    # None until at least one external-context retrieve happens
# }
```

---

## Permissions

Permissions are human-authored YAML, loaded at startup, not modifiable at runtime:

```yaml
# config/permissions.yaml
default_policy: deny

human_zone_prefixes:
  - "skills/"
  - "config/human/"
  - "prompts/locked/"

agents:
  analytics_agent:
    can_write:
      - "memory/working/"
      - "memory/episodic/"
      - "memory/org/"
      - "config/agent/learned_prefs/analytics_*"
    cannot_write:
      - "config/human/"
      - "skills/"
    can_read:
      - "memory/working/"
      - "memory/episodic/"
      - "memory/long_term/"
      - "memory/org/"
```

Agents not listed are denied all writes (`default_policy: deny`). Wildcard matching is supported (`analytics_*`). Rules are evaluated in order: `cannot_write` before `can_write`.

---

## Lifecycle management

`db.lifecycle` (`governance/lifecycle_manager.py`) is implemented. Call `db.lifecycle.run_sweep()` on demand — no background scheduler.

| Event | Trigger | Action | Status |
|---|---|---|---|
| Hard TTL expiry | `expiry_time` field on record, checked at sweep | Archived if `expiry_time ≤ now` | **Implemented** (Step 0 of `run_sweep()`) |
| Confidence decay | Agent-inferred memory, configurable rate (off by default) | Score reduced per sweep cycle | **Implemented** (off by default — ranking already applies recency decay at read time, making stored decay often redundant) |
| Staleness archival | Not retrieved within `staleness_days` (default 90) | Archived, excluded from retrieval | **Implemented** |
| Confidence threshold | Record confidence falls below `archival_threshold` (default 0.10) | Archived | **Implemented** |
| Skill staleness | Skill health score below 0.6 | Warning surfaced in retrieval; skill not archived automatically | **Implemented** |
| Reinforcement | Positive outcome signal | Confidence boosted, decay clock reset | **Implemented** via `db.lifecycle.reinforce(record_id)` and `db.lifecycle.record_outcome(...)` |
| Human revocation | Explicit SDK delete | Archived with audit-log entry; history preserved | **Implemented** via `AgentDB.delete()` |

**`human_approved` records are immune to all sweep operations** — no decay, no staleness archival, no TTL expiry, regardless of field values.

---

## Deployment

Three modes, identical interface:

```python
# Embedded — SQLite, zero config, development and prototyping
from agentdb import AgentDB
db = AgentDB()
db = AgentDB(path="./my-agent.db", permissions_path="./permissions.yaml")

# Server mode — standalone process, Postgres + pgvector (Phase 5)
db = AgentDB.connect("mcp://localhost:5433")

# Managed cloud — hosted, enterprise SLA (Phase 5+)
db = AgentDB.connect("agentdb://org.agentdb.io", api_key="...")
```

```typescript
// Same interface in TypeScript
import { AgentDB } from 'agentdb'
const db = new AgentDB()
const db = AgentDB.connect('mcp://localhost:5433')
```

**MCP server (implemented, Task 14, 2026-07-17):** 4 tools — `memory_write`, `memory_retrieve`, `conflict_queue`, `memory_stats` — via `FastMCP`. Stdio transport for Claude Desktop; `agentdb-mcp` console script. Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agentdb": {
      "command": "agentdb-mcp",
      "env": { "AGENTDB_MEMORY_DIR": "/path/to/your/memory/workspace" }
    }
  }
}
```

HTTP+SSE transport (`agentdb-mcp-http`) and `memory_subscribe` are deferred — push subscriptions don't work over stdio. See `docs/context/capabilities.md`.

**Security note:** In embedded (in-process) mode, the permission router runs in the same process as the application. For security-sensitive deployments, server mode is required — the router runs out-of-process and cannot be reached by application code. Embedded mode is for development and prototyping.

---

## Architecture

**This is the target architecture, not a diagram of what's running today.** Implemented today: `AgentDBBackend`, permission router, SQLite store, multi-signal ranker, audit log, conflict detector (`ConflictDetector` — temporal + authority), lifecycle manager (`db.lifecycle`), gateway + proposal workflow (`db.gateway`), MCP server (stdio, `interface/mcp_server.py`). A live in-memory notifier dispatcher (`db.gateway.subscribe`/`dispatch`) exists — narrower and less durable than the "Event bus" box below implies; see `docs/context/capabilities.md`. Not implemented: budget manager as a standalone module, Postgres/pgvector. See `docs/context/database-schema.md` for what's actually persisted.

```
Agent (any framework)
        │
        │  AgentDBBackend / MCP / SDK
        ▼
┌───────────────────────────────────────────┐
│              AgentDB interface            │
│  AgentDBBackend │ MCP server │ Python SDK │
└───────────────┬───────────────────────────┘
                │
        ┌───────▼────────┐
        │   Middleware   │
        │  Origin tag    │
        │  Permission    │
        │  router        │
        │  Conflict      │
        │  detector      │
        └───────┬────────┘
                │
     ┌──────────┼──────────┐
     ▼          ▼          ▼
  Human      Agent     Provider
  zone       zone      ingest
  (sealed)  (governed) (normalised)
     │          │          │
     └──────────┼──────────┘
                │
        ┌───────▼────────┐
        │  Retrieval     │
        │  Multi-signal  │
        │  ranker        │
        │  Budget        │
        │  manager       │
        └───────┬────────┘
                │
     ┌──────────┼──────────┐
     ▼          ▼          ▼
  SQLite    Postgres   pgvector
  (embed)   (server)  (semantic)
                │
        ┌───────▼────────┐
        │  Audit log     │
        │  (append-only) │
        │  Event bus     │
        │ (subscriptions)│
        └────────────────┘
```

---

## Memory that improves

Most memory systems are static — they store what the agent learned and retrieve it later. AgentDB closes the feedback loop:

1. **Every retrieval is logged** with which records were returned and at what scores (retrieval event in the audit trail).

2. **Every outcome** — task completion, human correction, human approval — is linked back to the retrieval that preceded it.

```python
# After a task completes:
db.record_outcome(
    session_id="sess_abc",
    agent_id="assistant",
    outcome_type="task_complete",   # or "human_correction" | "human_approval" | "agent_retry"
    outcome_value=1.0,              # 1.0 = positive, 0.0 = negative, 0.5 = neutral
)
```

3. **Memories that consistently precede positive outcomes** have their confidence boosted (up to +0.05 per outcome, capped at +0.1). Memories that precede failures are gently decayed (−0.02 per negative outcome). The agent gets better at knowing what to trust.

4. **The corpus quality dashboard** answers "is this agent's memory getting better?" — actual current return shape (see "System tables" above for the full field list; `conflict_pending_count` is hardcoded `0` and `outcome_positive_rate` is `None` under 10 outcomes, since neither conflict tracking nor a small-sample guard-free rate would be trustworthy yet):

```python
db.system.agentdb_stats()
# {
#   "avg_confidence":         0.81,   # trending up = memory improving
#   "low_confidence_count":   3,      # records at risk of archival
#   "conflict_pending_count": 0,      # hardcoded — no conflict tracking yet
#   "outcome_positive_rate":  0.87,   # 87% of tasks completed without correction (once >=10 outcomes recorded)
#   "retrieval_count_total":  412,
# }
```

**Human-approved memories and skills are immune to outcome-based changes.** The improvement algorithm only operates on agent-inferred memory — the layer the agent itself controls. Humans remain the authority on what is correct. The algorithm makes the agent better at using that authority.

Provider-ingested memories can decay from negative outcomes but cannot be boosted — they are external data, not learned knowledge.

**Context compaction handling is planned, not yet implemented.** No `checkpoint()` or `write_compaction_summary()` method exists in the codebase today. The intent: promote important working memory to episodic storage before compaction fires, and persist the framework's compressed summary as a durable episodic record, both logged to the audit trail with a compaction boundary marker, with a modest provenance boost for compaction summaries since they're derived from real conversation rather than pure inference.

---

## What AgentDB is not

- **Not a vector database.** pgvector handles semantic retrieval underneath. AgentDB is the governance layer above it.
- **Not a RAG pipeline.** Existing RAG tooling integrates via adapter.
- **Not a provider replacement.** Anthropic, OpenAI, Google memory — AgentDB ingests and governs them.
- **Not a framework.** It owns your data. Leaving requires a migration. That is the moat.
