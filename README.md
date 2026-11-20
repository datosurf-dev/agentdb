# AgentDB

**A governed memory layer for Python agents.** AgentDB stores agent memories with provenance, applies optional write policies, ranks retrieved records, tracks conflicts, and provides adapters for feeding governed context into agent runtimes.

AgentDB is a local Python library backed by SQLite. It complements an agent runtime; it is not a hosted service, a vector database, or a replacement for the model provider.

## Current implementation

The package currently provides:

- **Memory API:** `write`, `read`, `delete`, `retrieve`, and entity-based retrieval, with audit events for writes, deletes, and retrievals.
- **Provenance:** records are marked `human_approved`, `agent_inferred`, or `provider_ingested`; provider data is not promoted to human-approved data.
- **Optional write authorization:** a YAML `PermissionRouter` can authorize writes and deletes. It is only active when `permissions_path` is supplied.
- **Ranked retrieval:** provenance, recency, confidence, retrieval-frequency approximation, optional workspace overlap, and optional embedding similarity. Retrieval supports record limits and token budgets.
- **Conflict and governance signals:** write-time conflicts, external-context overlap checks, a conflict queue, and a live in-process gateway dispatcher.
- **Lifecycle:** outcomes and reinforcement, manual lifecycle sweeps, TTL/staleness archival, and memory consolidation. On conversation start, automatic consolidation requires at least seven days and ten sessions since the previous run.
- **Runtime integration:** a generic `AgentDBBackend`, model-agnostic middleware, a Claude runtime wrapper, and a Hermes `MemoryProvider` adapter.
- **Optional integrations:** stdio MCP server, Anthropic message ingestor, and email/Slack notifications.

> **Scope:** AgentDB is a memory and context-selection library. It does **not** manage or inject skills — skill registration, versioning, staleness, proposals and evolution were removed and now belong to a separate package.

The current storage implementation is embedded SQLite. There is no Postgres/cloud backend or HTTP MCP transport. The gateway dispatcher is in-memory; the audit log is stored separately in SQLite.

## Install

For development:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,mcp]'
```

Optional extras (the distribution name is `agentdatabase`):

```bash
python -m pip install 'agentdatabase[local-embed]'  # local sentence-transformer embeddings
python -m pip install 'agentdatabase[mcp]'          # stdio MCP server
python -m pip install 'agentdatabase[providers]'    # Anthropic provider client
python -m pip install 'agentdatabase[notifications]' # Slack notifications
```

Email notifications use the Python standard library. The `notifications` extra is needed for Slack.

## Quick start

Pass a `memory_dir` or `path` when constructing the client:

```python
from agentdb import AgentDB

# Creates config/, database/, and other workspace directories as needed.
db = AgentDB(memory_dir="./agentdb-memory")

record = db.write(
    key="memory/working/response_style",
    value="Prefer concise bullet points.",
    origin="agent_inferred",
    agent_id="assistant",
)

result = db.retrieve("How should I format the response?", agent_id="assistant")
print([record.value for record in result.records])
```

The default workspace configuration is written to `config/memory.yaml`. Set `AGENTDB_MEMORY_DIR` or `AGENTDB_HOME` to choose a workspace through the environment.

## Permissions and trust

Provide `permissions_path` to enable write/delete checks:

```python
db = AgentDB(
    memory_dir="./agentdb-memory",
    permissions_path="./permissions.yaml",
)
```

Example policy:

```yaml
default_policy: deny
human_zone_prefixes:
  - "memory/org/"
agents:
  assistant:
    can_write:
      - "memory/working/"
    cannot_write:
      - "memory/org/"
```

Important current limits:

- Without `permissions_path`, the permission router is not active.
- `read()` and `retrieve()` do not enforce `can_read`; do not treat that field as a read-access boundary.
- Agent-scoped retrieval is enabled for agents configured under `profile.agents` in `memory.yaml`; otherwise retrieval is not filtered by `agent_id`.
- The router governs calls through the client API; direct access to internal storage is outside that control path.
- The no-argument `AgentDB()` constructor uses a workspace (`AGENTDB_MEMORY_DIR` / `AGENTDB_HOME` / `agentdb-workspace/`) or SQLite `:memory:`. `AgentDB(path=":memory:")` uses a process-wide in-memory database (no file created); two in-memory instances in one process share it.

## Configuration and embeddings

`memory.yaml` uses a named profile with optional overrides. Built-in profiles are `budget_optimized`, `full_storage`, and `research`:

```yaml
profile:
  name: budget_optimized
  agents:
    assistant:
      retrieval:
        retrieve_limit: 10

# Embeddings are off unless enabled or supplied through the constructor.
embeddings:
  enabled: true
  # model: sentence-transformers/all-MiniLM-L6-v2
```

Embeddings can also be enabled with `AgentDB(local_embed=True)` or a custom `embed_fn(text) -> list[float]`. They enable semantic ranking and embedding-based conflict checks. Local embeddings require the `local-embed` extra; embedding support is disabled by default.

## Main API surfaces

- `AgentDB`: memory writes/reads/retrieval, `consolidate()`, `checkpoint()`, `write_compaction_summary()`, `record_outcome()`, `config`, and `config_for(agent_id)`.
- `db.lifecycle`: lifecycle sweeps, reinforcement, and outcome recording. Sweeps are not run by a background scheduler.
- `db.gateway`: in-process event subscriptions/dispatch and conflict queue.
- `db.system.agentdb_stats()`: corpus and outcome statistics.
- `db.audit_log.replay()`: replay persisted audit entries.
- `AgentDBBackend` (`from agentdb.backend import AgentDBBackend`): key/value `read`, `write`, `delete`, `list`, `grep`, and `glob` for a selected agent and trust zone.

Retrieval returns `RetrievalResult` with records, conflicts, authority override instructions, a token estimate, a truncation flag, and an optional `trace` (pass `trace=True`) exposing candidate sources, exclusions, and per-candidate ranking evidence. Candidates are the **union of lexical and (optional) semantic matches**, ranked by the existing multi-signal ranker and packed within a rendered-token budget that skips oversized records rather than stopping. `retrieve_by_entities()` uses the same eligibility filters, ranking, and budget policy. Semantic matching requires embeddings; without them, retrieval uses the lexical path and the other non-semantic signals. Set `embeddings.semantic_candidates: false` to force lexical-only retrieval even when embeddings are configured.

Consolidation produces a **derived `derived_canonical` record** with full source lineage; the original source records are never overwritten (only archived from default selection), and a canonical is invalidated when its sources change. Compaction summaries and checkpoints default to `agent_inferred` provenance — pass `origin="human_approved"` only for a verified human action.

## Runtime integrations

### Model-agnostic middleware

`AgentDBMiddleware` (`from agentdb.adapters import AgentDBMiddleware`) provides `prepare_turn` / `finalize_turn` and `run_turn`, plus async variants. The async preparation path can inject workspace information and recent work context according to `RunnerCapabilities`; the synchronous path prepares memory context. Neither path injects skills.

### Claude

`AgentDBClaudeRuntime` supports synchronous model clients and an async Claude SDK client factory. `AgentDBClaudeMiddleware` remains available as a backward-compatible import. The adapter exposes a `PreToolUse` hook map through `get_sdk_hooks()`; the integrating application is responsible for registering that hook with its SDK/runtime.

### Hermes

`AgentDBHermesProvider` implements Hermes's `MemoryProvider` interface. It supplies retrieval, turn persistence, and lifecycle hooks (`system_prompt_block()` returns an empty string — AgentDB does not inject skills). Governance applies to AgentDB records; a post-write Hermes hook cannot veto a write already made to Hermes-native storage.

### Other frameworks

No dedicated deepagents, LangGraph, CrewAI, OpenAI Agents, Google ADK, or AutoGen adapter is included. Use `AgentDBBackend` where the framework supports a pluggable key/value backend, or integrate `AgentDBMiddleware` at the runtime's own control points.

See [`docs/integrations/README.md`](docs/integrations/README.md) for integration guides.

## Optional services

- **MCP:** `agentdb-mcp` starts a stdio server with `memory_write`, `memory_retrieve`, `conflict_queue`, and `memory_stats`. Set `AGENTDB_MEMORY_DIR` to select its workspace. HTTP transport is not implemented.
- **Anthropic ingestion:** `AnthropicProvider(db, client).sync(agent_id, limit=50)` ingests text message blocks as `provider_ingested` records.
- **Notifications:** configure email or Slack channels in `memory.yaml`; dispatch is live and in-process, not a durable queue.

## Tests

```bash
python -m pytest -q
```

See [`docs/context/testing-strategy.md`](docs/context/testing-strategy.md) for focused test commands and [`docs/context/architecture-principles.md`](docs/context/architecture-principles.md) for the design constraints. The repository uses a `src/` layout and package imports such as `from agentdb import AgentDB`.
