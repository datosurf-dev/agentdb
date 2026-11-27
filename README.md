# AgentDB

**A governed memory layer for Python agents.** AgentDB stores agent memories with provenance, applies optional write policies, ranks retrieved records, tracks conflicts, and provides adapters for feeding governed context into agent runtimes.

AgentDB is a local Python library backed by SQLite. It complements an agent runtime; it is not a hosted service, a vector database, or a replacement for the model provider.

## Current implementation

The package currently provides:

- **Memory API:** `write`, `read`, `delete`, `retrieve`, and entity-based retrieval, with audit events for writes, deletes, and retrievals.
- **Provenance:** records are marked `human_approved`, `agent_inferred`, or `provider_ingested`; provider data is not promoted to human-approved data.
- **Optional write authorization:** a YAML `PermissionRouter` can authorize writes and deletes. It is only active when `permissions_path` is supplied.
- **Ranked retrieval:** provenance, recency, confidence, retrieval-frequency approximation, optional workspace overlap, and optional embedding similarity. Retrieval supports record limits and token budgets.
- **Context applicability:** `retrieve(scope=...)` accepts optional `workspace_id`, `repository_id`, `environment`, `task_type`, and `as_of`. Explicit workspace/repository/environment mismatch is inapplicable; missing scope is unknown (not a match); exact scope is a deterministic tie-break that does not change ranking weights. `as_of` gates records outside their explicit validity window (`effective_from`/`effective_until`/`expiry_time`).
- **Conflict and governance signals:** write-time conflicts, external-context overlap checks, a conflict queue, and a live in-process gateway dispatcher.
- **Lifecycle:** outcomes and reinforcement, manual lifecycle sweeps, TTL/staleness archival, and memory consolidation. On conversation start, automatic consolidation requires at least seven days and ten sessions since the previous run (defaults; configurable via `profile.consolidation`).
- **Runtime integration:** a generic `AgentDBBackend`, model-agnostic middleware, a Claude runtime wrapper, and a Hermes `MemoryProvider` adapter.
- **Optional integrations:** stdio MCP server, Anthropic message ingestor, and email/Slack notifications.

> **Scope:** AgentDB is a focused memory and context-selection library: it stores, ranks, and injects provenance-aware memory for agents.

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
# All bots registered by default: any agent_id not listed under `agents:`
# gets this scope. Remove it for strict deny-by-default.
default_agent_scope:
  can_write:
    - "memory/working/"
    - "memory/episodic/"
  cannot_write:
    - "memory/org/"
  can_read:
    - "memory/working/"
    - "memory/episodic/"
agents:
  # Disable a specific bot (deny all its memory writes/reads):
  retired_bot:
    disabled: true
  # An explicit entry overrides the default scope for one agent:
  assistant:
    can_write:
      - "memory/working/"
    cannot_write:
      - "memory/org/"
```

`default_agent_scope` makes **all bot identities registered by default** — an unlisted `agent_id` is granted that scope, so a new bot works without a manual per-bot entry. Disable a bot with an explicit `agents: <id>: { disabled: true }`. Omit `default_agent_scope` for strict deny-by-default (every identity must be listed).

Important behavior and limits:

- Without `permissions_path` (default trusted mode), the permission router is not active — no read/write authorization is enforced and the host owns its security boundary.
- With `permissions_path` (isolation mode), `read()`, `retrieve()`, and `retrieve_by_entities()` **enforce `can_read`**. Reads without a caller identity fail closed, and a derived canonical is disclosed only if **all** of its sources are readable. Authorization is evaluated independently of workspace/repository identity (those are applicability metadata, never credentials).
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

- `AgentDB`: memory writes/reads/retrieval, `consolidate()`, `write_compaction_summary()`, `record_outcome()`, `config`, and `config_for(agent_id)`.
- `db.lifecycle`: lifecycle sweeps, reinforcement, and outcome recording. Sweeps are not run by a background scheduler.
- `db.gateway`: in-process event subscriptions/dispatch and conflict queue.
- `db.system.agentdb_stats()`: corpus and outcome statistics.
- `db.audit_log.replay()`: replay persisted audit entries.
- `AgentDBBackend` (`from agentdb.backend import AgentDBBackend`): key/value `read`, `write`, `delete`, `list`, `grep`, and `glob` for a selected agent and trust zone.

### Maintainer-managed guidance

A maintainer can publish **one short instruction** for a specific repository and
task by writing an ordinary governed record and pointing the host at its key. This
is deliberately not retrieval: there is no query and no ranking, so what the agent
receives is exactly what the maintainer configured.

The division of responsibility is strict:

| Step | Owner |
|---|---|
| Publish the instruction as a record with `repository_id` and `task_type` | maintainer, via `write()` |
| Map an exact `repository_id` + `task_type` to that record key | host configuration |
| Resolve repository, task type, and caller | **host** (AgentGate) |
| Authorize the read | **AgentDB**, via `read(key, agent_id=...)` |
| Deliver at most one instruction, in the ephemeral turn context | host |
| Verify the record's own scope still matches | host |

AgentDB's part is small on purpose: it stores the record and enforces its read
policy. It does not select, rank, or interpret guidance, and a record's scope never
broadens what a caller may retrieve.

```python
db.write(
    key="memory/guidance/komper/bug-fix",
    value="Verify an upgrade from a database created by the previous release.",
    origin="human_approved",
    agent_id="maintainer",
    repository_id="komper",
    task_type="bug_fix",
)

# The host reads it with the caller's identity; AgentDB applies its read policy.
record = db.read("memory/guidance/komper/bug-fix", agent_id="agentgate")
```

Guidance is **omitted** when the scope is unresolved, the record is missing,
inaccessible, ambiguous, or does not itself match the requested scope, and when the
maintainer withdraws it by removing the selection. A turn without applicable
guidance is normal, not an error.

A repeated `write()` on an existing key leaves sibling records, so give each
published instruction an unambiguous key. Identical text published for two
repositories is two independent records, not one shared one.

**Deferred:** a publishing/activation lifecycle, automatic synthesis, embeddings for
guidance, and query-based discovery. See `docs/spec/managed-repository-guidance.md`. 

Retrieval returns `RetrievalResult` with records, conflicts, authority override instructions, a token estimate, a truncation flag, and an optional `trace` (pass `trace=True`) exposing candidate sources, exclusions, and per-candidate ranking evidence. Candidates are the **union of lexical and (optional) semantic matches**, ranked by the existing multi-signal ranker and packed within a rendered-token budget that skips oversized records rather than stopping. `retrieve_by_entities()` uses the same eligibility filters, ranking, and budget policy. Semantic matching requires embeddings; without them, retrieval uses the lexical path and the other non-semantic signals. Set `embeddings.semantic_candidates: false` to force lexical-only retrieval even when embeddings are configured.

Consolidation produces a **derived `derived_canonical` record** with full source lineage; the original source records are never overwritten (only archived from default selection), and a canonical is invalidated when its sources change. Only equivalent or compatible-complementary values are merged — contradictory values (including negations and mutually-exclusive values) and scope-incompatible facts stay separate. `validate_canonicals()` reports invalid canonicals non-destructively. Concurrent/repeated consolidation is idempotent (a stable source fingerprint prevents duplicate active canonicals).

### Consolidation synthesis (`llm_fn`)

By default consolidation is **dedup-only** (`cluster_copy`): the canonical value
is a verbatim copy of the highest-confidence source in a cluster. To synthesize a
new summary instead (`cluster_synthesis`), pass an `llm_fn` to `consolidate()` /
`end_session(run_consolidation=True, ...)`:

```python
db.consolidate(agent_id="my-agent", llm_fn=my_llm_fn)
```

- **Signature:** `llm_fn(prompt: str) -> str` — receives one synthesis prompt,
  returns the synthesized text. It is a **plain callable / API argument only**
  (not settable in `memory.yaml`).
- **Optional:** when `llm_fn` is `None`, consolidation still runs and produces
  `cluster_copy` (verbatim copy of the top source), **not** a synthesized
  summary. Model-call errors are caught and fall back to `cluster_copy`.
- **Helper (recommended):** instead of hand-rolling the chat-completions call,
  use `build_llm_fn`, which constructs an OpenAI-compatible client from a model
  id + connection params and returns a ready `llm_fn`:

  ```python
  from agentdb.providers.consolidation import build_llm_fn

  llm_fn = build_llm_fn(
      model="meta-llama/Meta-Llama-3.1-8B-Instruct",
      base_url="https://api.deepinfra.com/v1/openai",
      api_key="...",              # placeholder ok for local servers
      # max_tokens=..., temperature=..., timeout=...  (all optional)
  )
  db.consolidate(agent_id="my-agent", llm_fn=llm_fn)
  ```

  `build_llm_fn` needs the `openai` package: install it with
  `pip install 'agentdatabase[llm]'`.
- **Structured know-how:** when `llm_fn` is supplied, the package asks the model
  for a JSON object — `{"know_how", "workspace_id", "repository_id",
  "task_type", "environment", "confidence"}` — so the canonical is reusable,
  workspace-scoped know-how (not a session narrative) and carries metadata used
  for scoped retrieval. A plain-text answer is still accepted (treated as
  `know_how`). Metadata the model omits is inherited from the top source. After
  consolidation, retrieval returns the distilled canonical in place of its
  archived raw sources (raw is re-surfaced only when it holds a query detail the
  canonical dropped).

Compaction summaries default to `agent_inferred` provenance. A bare `origin="human_approved"` is **asserted** (not verified) authority. **Verified** human authority requires a host-configured `authority_verifier` and an affirmative decision through `approve_authority()`; it is bound to a versioned content/scope fingerprint (edits invalidate it) and can be removed via `revoke_authority()` with an audit trail. AgentDB does not defend against a malicious host process with full code/DB access — the enforceable boundary is between ordinary agent APIs and the host-configured trusted approval pathway.

## Runtime integrations

### Model-agnostic middleware

`AgentDBMiddleware` (`from agentdb.adapters import AgentDBMiddleware`) provides `prepare_turn` / `finalize_turn` and `run_turn`, plus async variants. The async preparation path can inject workspace information and recent work context according to `RunnerCapabilities`; the synchronous path prepares memory context.

### Claude

`AgentDBClaudeRuntime` supports synchronous model clients and an async Claude SDK client factory. `AgentDBClaudeMiddleware` remains available as a backward-compatible import. The adapter exposes a `PreToolUse` hook map through `get_sdk_hooks()`; the integrating application is responsible for registering that hook with its SDK/runtime.

### Hermes

`AgentDBHermesProvider` implements Hermes's `MemoryProvider` interface. It supplies retrieval, turn persistence, and lifecycle hooks. Governance applies to AgentDB records; a post-write Hermes hook cannot veto a write already made to Hermes-native storage.

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
