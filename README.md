# AgentDB

**A governed memory layer that keeps agent memory actionable.**

AgentDB is designed to improve agent behavior without becoming another layer of operational overhead. It governs what memory is trusted, keeps relevant memory assets in sync, and injects the right context or instructions when an agent is about to act. Every write is validated before it lands. Every read returns a governed context package. The boundary is enforced at the memory layer — not by convention, not by prompt instructions, not by hoping agents behave.

> Every other memory product stores context. AgentDB governs it.

## Architecture Confirmation

The full architecture pitch and the numbered design principles (now including Principle 12's real-time-enforcement vs. async-integrity-check split for adapters) live in one canonical place — this section just points to it instead of repeating it:

- Architecture & design principles: `/Users/jm/Documents/github/agentdb/docs/context/architecture-principles.md`
- Global agent instructions: `/Users/jm/Documents/github/agentdb/AGENTS.md`
- Project context document: `/Users/jm/Documents/github/agentdb/docs/context/agentdb-context.md`
- Plan: `/Users/jm/Documents/github/agentdb/docs/superpowers/plans/2026-06-06-agentdb-revised.md`

## Current Repository Status (As Of 2026-07-11)

Current implementation is functional for the governed memory loop and adapter integrations, with initial proactive-memory slices now in place and later-phase hardening still pending.

### Feature State Matrix

#### Track

| Feature area | State | Notes |
|---|---|---|
| Package entrypoint (`from agentdb import AgentDB`) | Implemented | Available in `src/agentdb/__init__.py`. |
| Core client loop (`write`, `read`, `retrieve`, `record_outcome`) | Implemented | Includes audit events and confidence updates. |
| Backend contract (`AgentDBBackend`) | Implemented | Framework-agnostic read/write/list/grep/glob contract. |
| Adapter surfaces | Implemented (Claude SDK only) | Only `adapters/claude_agent_sdk.py` exists. deepagents, LangGraph, CrewAI, OpenAI Agents, Google ADK, and AutoGen adapters are **not implemented** — their integration test files exist but fail to collect (`ModuleNotFoundError`). `deepagents.py` is the next one planned (flagship per Design Principle 2); the rest are deferred until a specific framework's users ask for one, since `AgentDBBackend` already works with any framework generically (see "Drop into your existing stack" below). |
| Claude SDK async runtime (`from_sdk_client`, `handle_turn_async`) | Implemented | Async turn wrapper available in adapter runtime. |
| Directory tracking capture (Claude middleware) | Implemented | Optional tracked-directory snapshots persisted as memories. |
| Package memory workspace (`~/.agentdb/memory`, `config/memory.yaml`) | Implemented (initial) | Workspace resolution, default materialization, config loading, and backward-compatible `path=` override are in place. |
| Conversation JSONL capture | Implemented (initial) | Config-driven JSONL capture is available when `capture.conversation_jsonl` is enabled. |

#### Reconcile

| Feature area | State | Notes |
|---|---|---|
| Permission routing boundaries | Implemented | Write-time enforcement via `PermissionRouter`. |
| Priority enforcement additive fields | Partial | External conflict flags (against caller-supplied context) are implemented. `override_instructions` is dead code — the field exists on `RetrievalResult` but no code path populates it, so "explicit override instructions on conflict" is not actually implemented, only detection is. |
| Reconciliation trigger orchestration (`conversation_started`, `file_modified`, `cron`) | Implemented (initial) | Internal dispatcher plus `conversation_started` path are in place; broader trigger coverage remains pending. |
| Embedding-backed semantic retrieval/conflicts | Planned (Phase 4) | Current retrieval is keyword/provenance/confidence/recency based; embeddings are only justified if they improve action quality. |

#### Rank

| Feature area | State | Notes |
|---|---|---|
| Algorithm + skills blend for action quality | Implemented (initial) | Retrieval ranks memory; governed skills add maintained guidance during retrieval results. |
| Skills store + health warnings | Implemented | Versioned skills, staleness-based health scoring, warnings on retrieve. |
| System stats surface | Implemented (initial) | Includes retrieval/outcome/conflict-rate aggregates. |
| Entity-aware retrieval core (`entities`, `retrieve_by_entities`) | Implemented (initial) | Additive entity tagging and keyword-based entity retrieval are available in core. |
| Provider ingestors (Anthropic/OpenAI/Google sync) | Planned (Phase 3) | Architecture intent documented; module surface pending. |

#### Inject

| Feature area | State | Notes |
|---|---|---|
| Query-driven retrieval into turn context | Implemented (Claude SDK path only) | The Claude adapter's `prepare_turn` retrieves at turn start. No deepagents adapter exists yet to have an equivalent path — see Adapter surfaces above. |
| Action-driven retrieval (`entities`, `retrieve_by_entities`, `PreToolUse` hook) | Implemented (initial) | Core entity retrieval, adapter `pre_tool_use(...)`, and downstream hook-registration surface are implemented; automatic SDK attachment remains downstream integration work. |
| MCP server surface | Planned (Phase 5) | Not implemented in current source tree. |
| Hardening and production controls | Planned (Phase 6) | Roadmap-only at this snapshot. |

Validation status:
- Editable install and package-style imports are validated via `pip install -e '.[dev]'`.
- `python -m pytest -q tests/unit tests/integration/test_claude_agent_sdk_adapter.py` → 96 passed.
- Plain `python -m pytest -q` (no path filter) currently **fails collection** with 6 `ModuleNotFoundError`s, one per adapter that doesn't exist yet (`autogen`, `crewai`, `deepagents`, `google_adk`, `langgraph`, `openai_agents`). This isn't a regression to fix — it's expected until those adapters are built (or their test files are skip-marked).

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
  - `write(key, value, origin, agent_id, ...)`
  - `read(key, agent_id)`
  - `retrieve(query, agent_id, limit=..., external_context=..., session_id=...)`
  - `record_outcome(session_id, agent_id, outcome_type, outcome_value)`
  - `system.agentdb_stats()`
  - `audit_log.replay(from_sequence=0)`
  - `skills_api` for skill lifecycle/drift workflows:
    - `create_skill`, `update_skill_version`, `list_skills_for_agent`,
      `list_stale_skills_for_agent`, `list_skill_warnings_for_agent`
  - optional initialization policy for directory tracking:
    - `tracking_directories`
    - `tracking_root`
    - `tracking_max_files_per_directory`
- `AgentDBBackend` (`from agentdb.backend import AgentDBBackend`)
  - `read`, `write`, `delete`, `list`, `grep`, `glob`
- Adapters currently implemented under `src/agentdb/adapters/`
  - Claude Agent SDK middleware only. deepagents, LangGraph, CrewAI, OpenAI Agents, Google ADK, and AutoGen are not built yet — see the Feature State Matrix above.

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
- A `search()` method and `SkillConflict` exception

**Current limitation (Phase 2):** Entity drift detection is keyword-based — a skill that uses a synonym for a deprecated entity will not be flagged. Semantic entity matching is Phase 5 scope. This limitation is documented in LIMITATIONS.md.

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
conflict_id      → column exists (added to support ranker conflict-flagging); nothing populates it yet
```

Planned, not yet on the record: `tier`, `expiry_policy`, `audit_sequence`, `embedding_version`, `user_id`, `session_id`. See `docs/context/database-schema.md` for the full column-by-column reference across every table, not just `MemoryRecord`.

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

## Conflict detection (design — not yet implemented against stored records)

The table below is the target design: detection running at write time, not read time, because by the time an agent is mid-execution it's too late to resolve cleanly.

| Conflict type | Example | Resolution |
|---|---|---|
| **Temporal** | Old instruction contradicts newer one on same key | Auto-resolved in favour of recency. Logged. |
| **Authority** | Agent-inferred contradicts human-approved | Auto-resolved in favour of human. Agent notified. |
| **Semantic** | "Be concise" vs "always explain your reasoning" — same authority, situationally contradictory | Queued for human review. Agent served older instruction with conflict flag. |

**None of this exists yet.** `write()` has no conflict-checking code path, and `retrieve()` hardcodes `conflicts=[]` for records already in the store. `MemoryRecord.conflict_id` exists as a column (added to support the ranker's conflict-flagging logic) but nothing populates it.

**What is implemented today** is narrower: an *external*-context check. When you pass `external_context` into `retrieve()`, AgentDB does a keyword-overlap check between that supplied context and your already-stored memory values, and flags an `external_conflict_detected` audit event + `external_conflict_rate` stat if they overlap. It does not compare stored records against each other.

```python
result = db.retrieve(query="communication style", agent_id="assistant", external_context=[...])
result.conflicts       # checked first — currently always [] regardless of external_context
result.records         # received after knowing conflict state
```

**Conflict flags are still assembled before memory content in the `RetrievalResult` shape**, preserving the safety property, even though nothing populates `conflicts` yet.

**Known limitation:** semantic conflict detection additionally requires embeddings (active in Phase 4) on top of the write-time detector above not existing yet. See `docs/context/database-schema.md` for the full picture of what's stored vs planned.

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

**The background lifecycle manager described below does not exist yet** (Task 9 in the plan, not started). The table is the target design, with two rows already true today via other mechanisms and one row's design under active reconsideration:

| Event | Trigger | Action | Status |
|---|---|---|---|
| Confidence decay | Agent-inferred memory not retrieved within window | Score reduced 5% per cycle | Not implemented — and may not be needed: ranking already applies exponential recency decay to a record's effective relevance on every retrieval, without needing a scheduled job to mutate stored confidence. See the plan's Task 9 for the current thinking. |
| Staleness archival | Not retrieved within 90 days | Moved to cold storage, excluded from standard retrieval | Not implemented |
| Skill staleness | Skill health score below 0.6 | Warning surfaced in retrieval; skill not archived automatically | **Implemented** — this one works today |
| Reinforcement | Explicit positive outcome signal | Confidence boosted, decay clock reset | **Implemented** via `record_outcome()` (reactive, not a background process) |
| Human revocation | Explicit SDK delete | Archived with audit-log entry; history preserved | **Implemented** via `AgentDB.delete()` (2026-07-11: now routes through the permission router + audit log, not a direct store call) |

**human_approved records never decay.** Only `agent_inferred` and `provider_ingested` records are subject to confidence decay, and today that only happens reactively through `record_outcome()`, not on a schedule.

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

**MCP server (not yet implemented, Phase 5):** the target design is 3 tools — `memory_write`, `memory_retrieve`, `conflict_queue` — as a native integration point for any MCP-compatible framework. A 4th tool, `memory_subscribe`, is deferred along with the event bus it would depend on (never built — see `docs/superpowers/plans/2026-06-06-agentdb-revised.md` Task 12) until a concrete consumer needs push notifications instead of polling.

**Security note:** In embedded (in-process) mode, the permission router runs in the same process as the application. For security-sensitive deployments, server mode is required — the router runs out-of-process and cannot be reached by application code. Embedded mode is for development and prototyping.

---

## Architecture

**This is the target architecture, not a diagram of what's running today.** Implemented today: `AgentDBBackend`, the permission router, the SQLite store, the multi-signal ranker, and the audit log. Not implemented: MCP server, conflict detector, budget manager, Postgres/pgvector, and the event bus. See `docs/context/database-schema.md` for what's actually persisted.

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
