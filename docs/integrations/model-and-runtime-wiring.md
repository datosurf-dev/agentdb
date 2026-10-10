# Model And Runtime Wiring

AgentDB is model-agnostic: it governs memory writes/reads, while your runtime selects the LLM.

Use this guide to wire AgentDB with:

- a local LLM runtime
- a cloud model such as Claude

## Key Principle

Pick the integration path by runtime capability:

1. Runtime supports pluggable storage backend -> use `AgentDBBackend`
2. Runtime has a dedicated adapter in `src/agentdb/adapters` -> use that adapter

The model provider (local or cloud) is orthogonal to AgentDB.

## A) Cloud Model (Claude) Via Deepagents

**`agentdb.adapters.deepagents` does not exist yet** (see the plan's Task 15) — the module and `make_agentdb_backend` below are the target shape, not something you can import today. Use the generic `AgentDBBackend` directly instead (works now):

```python
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend

from agentdb import AgentDB
from agentdb.backend import AgentDBBackend

db = AgentDB(path="./agentdb.db", permissions_path="./permissions.yaml")

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
    tools=[...],
    backend=CompositeBackend(
        default=StateBackend(),
        routes={
            "/memories/": AgentDBBackend(agent_id="assistant", trust_zone="agent_inferred", db=db),
            "/skills/": AgentDBBackend(agent_id="assistant", trust_zone="human_approved", db=db),
        },
    ),
)
```

Once `agentdb.adapters.deepagents` ships, `make_agentdb_backend(db, "assistant", trust_zone=...)` would just be a thin convenience wrapper over the same `AgentDBBackend` construction above.

Use this when you want cloud-hosted reasoning and governed memory boundaries.

## A.1) Claude Agent SDK Middleware Adapter

The repository includes a runtime-agnostic adapter scaffold for Claude SDK style flows:

- `src/agentdb/adapters/claude_agent_sdk.py`
- `AgentDBClaudeMiddleware`
- `AgentDBClaudeRuntime` (simplified one-call wrapper for app handlers)

This adapter enforces the runtime contract:

1. pre-response hook: always call `db.retrieve(...)`
2. post-response hook: conditionally call `db.write(...)`
3. post-outcome hook: call `db.record_outcome(...)`
4. optional directory tracking capture: persist governed directory snapshots (for example skills/docs/history folders) tied to session/message IDs

Use:

- `prepare_turn(...)` / `finalize_turn(...)` for custom SDK orchestration
- `run_turn(...)` for a single enforced wrapper call
- `AgentDBClaudeRuntime.handle_turn(...)` for sync `messages.create(...)` style integration
- `AgentDBClaudeRuntime.handle_turn_async(...)` for async `ClaudeSDKClient` style integration

Current status notes:

- Query-driven turn retrieval is implemented in both sync and async wrapper paths.
- Initial action-driven retrieval support is implemented:
    - core `retrieve_by_entities(...)`
    - adapter `pre_tool_use(...)`
    - downstream hook-registration surface via `runtime.get_sdk_hooks()`
- Actual SDK-specific automatic `PreToolUse` attachment remains downstream integration work.

### Simplest Operational Integration (Slack/Event Handler)

```python
from agentdb import AgentDB
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeRuntime

db = AgentDB(
    path="./agentdb.db",
    permissions_path="./permissions.yaml",
    tracking_directories=["docs", ".claude/skills"],
    tracking_root=".",
)

runtime = AgentDBClaudeRuntime.from_claude_client(
    db=db,
    agent_id="research_agent",
    client=claude_client,  # client with messages.create(...)
    model="claude-sonnet-4-6",
    profile="budget_optimized",
    system_prompt="You are the team Slack assistant.",
)

result = runtime.handle_turn(
    user_query=incoming_text,
    session_id=slack_thread_id,
    message_id=slack_message_id,
    outcome_type="task_complete",
    outcome_value=1.0,
)

reply_text = result["response_text"]
```

### Claude Runtime Contract Example (Using Adapter)

```python
from agentdb import AgentDB
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeMiddleware

db = AgentDB(
    path="./agentdb.db",
    permissions_path="./permissions.yaml",
    tracking_directories=["docs", ".claude/skills", "agent-transcripts"],
    tracking_root=".",
    tracking_max_files_per_directory=200,
)
middleware = AgentDBClaudeMiddleware(
    db=db,
    agent_id="assistant",
    profile="budget_optimized",  # or "full_storage"
)

response = middleware.run_turn(
    user_query="Summarize account changes for user_123",
    session_id="sess-claude-001",
    message_id="msg-001",
    call_model=lambda messages: claude_client.run(messages),  # pseudocode
    tracked_directories=["docs", ".claude/skills"],
    tracking_base_dir=".",
    outcome_type="task_complete",
    outcome_value=1.0,
)
```

### Async ClaudeSDKClient Integration (Current Adapter Surface)

```python
from agentdb import AgentDB
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeRuntime

db = AgentDB(path="./agentdb.db", permissions_path="./permissions.yaml")

runtime = AgentDBClaudeRuntime.from_sdk_client(
    db=db,
    agent_id="research_agent",
    sdk_client_factory=get_or_create_sdk_client,  # callable(session_id) -> client
    profile="budget_optimized",
    system_prompt="You are the team Slack assistant.",
)

result = await runtime.handle_turn_async(
    user_query=incoming_text,
    session_id=slack_thread_id,
    message_id=slack_message_id,
    outcome_type="task_complete",
    outcome_value=1.0,
)

reply_text = result["response_text"]
```

### Downstream Hook Registration For Action-Timed Context

When your downstream Claude SDK runtime supports a `PreToolUse`-style hook, use the runtime-facing hook map rather than reimplementing entity extraction yourself:

```python
runtime = AgentDBClaudeRuntime.from_sdk_client(
    db=db,
    agent_id="research_agent",
    sdk_client_factory=get_or_create_sdk_client,
    profile="budget_optimized",
    system_prompt="You are the team Slack assistant.",
)

hooks = runtime.get_sdk_hooks()

# Downstream app/runtime code attaches this to the SDK's actual hook system.
pre_tool_use = hooks["PreToolUse"]

payload = pre_tool_use(
    tool_name="Edit",
    tool_input={"file_path": "models/marts/core/report_model.sql"},
    session_id="thread-123",
)

# payload shape:
# {
#   "additionalContext": "Governed action context: ...",
#   "touchedEntities": ["models/marts/core/report_model.sql", "report_model"],
# }
```

For a full downstream app example, see [examples/claude_agent_sdk_hook_registration.py](examples/claude_agent_sdk_hook_registration.py). The key app-owned step is attaching the handlers when you create or look up the SDK client for a thread:

```python
class ClaudeSdkApp:
    def __init__(self) -> None:
        self.runtime = AgentDBClaudeRuntime.from_sdk_client(
            db=db,
            agent_id="research_agent",
            sdk_client_factory=self.get_or_create_client,
            profile="budget_optimized",
            system_prompt="You are the engineering Slack assistant.",
        )
        self._clients = {}

    def get_or_create_client(self, thread_id: str):
        client = self._clients.get(thread_id)
        if client is None:
            client = build_real_claude_sdk_client(thread_id)
            for hook_name, handler in self.runtime.get_sdk_hooks().items():
                client.register_hook(hook_name, handler)
            self._clients[thread_id] = client
        return client
```

That keeps ownership boundaries explicit:

- AgentDB owns retrieval, entity extraction, and hook payload shape.
- Your Claude SDK app owns session creation and actual hook registration.

Current status:

- The hook-registration surface is implemented.
- The adapter does not import Claude SDK directly.
- Actual SDK-specific attachment is owned by downstream runtime code.
- Full automatic `PreToolUse` attachment remains integration work in the downstream app.

### Directory Tracking

For high-value repository areas (skills, docs, conversation-history folders), the Claude middleware can persist directory snapshots under:

- `memory/episodic/claude/{agent_id}/{session_id}/directory_tracking/...`

Each snapshot includes:

- tracked directory path
- existence marker
- indexed file entries (path, size, modified_at)
- `session_id`
- optional `message_id`
- truncation marker for large directories

When `tracked_directories` is set on `AgentDB(...)`, middleware calls can omit
directory arguments and use that default policy automatically.

## B) Local LLM Runtime

For local models, use the runtime you prefer (for example a local serving stack), and keep AgentDB as memory/storage backend.

Pattern:

```python
from agentdb import AgentDB
from agentdb.backend import AgentDBBackend

db = AgentDB(path="./agentdb.db", permissions_path="./permissions.yaml")
memory_backend = AgentDBBackend(
    agent_id="local_assistant",
    trust_zone="agent_inferred",
    db=db,
)

# Attach `memory_backend` to your local runtime's memory/storage extension point.
# Your local model selection remains independent from AgentDB.
```

If your runtime expects a framework-specific memory interface: only `adapters/claude_agent_sdk.py` exists today (see "A.1" above). `adapters/langgraph.py`, `autogen.py`, `crewai.py`, and `openai_agents.py` are **not implemented** — see the plan's Task 15. Until one exists for your framework, use the generic `AgentDBBackend` pattern shown above; it works with any runtime that supports pluggable key-value storage, adapter or not.

## B.1) Enabling Semantic Search (Embeddings)

Embeddings are **off by default**. When active, they unlock semantic retrieval scoring and semantic conflict detection. Three activation paths:

### Via memory.yaml (recommended for persistent deployments)

In your workspace's `config/memory.yaml`:

```yaml
embeddings:
  enabled: true
  # model: sentence-transformers/all-MiniLM-L6-v2   # optional; this is the default
```

Requires: `pip install 'agentdatabase[local-embed]'`

### Via constructor flag

```python
db = AgentDB(memory_dir="./memory", local_embed=True)
# requires: pip install 'agentdatabase[local-embed]'
```

### Via custom embed function (any embedding API)

```python
import openai

def embed(text: str) -> list[float]:
    return openai.embeddings.create(input=text, model="text-embedding-3-small").data[0].embedding

db = AgentDB(memory_dir="./memory", embed_fn=embed)
```

Precedence when multiple paths are set: `embed_fn=` > `local_embed=True` > `memory.yaml enabled: true`.

---

## C) Making Memory Effective (Both Local And Cloud)

For strong behavior, use this loop:

1. write durable facts/preferences during tasks (`db.write`)
2. retrieve context before key decisions (`db.retrieve`)
3. record outcomes after task completion (`db.record_outcome`)
4. monitor corpus quality (`db.system.agentdb_stats()`)

This enables memory governance and gradual confidence calibration over time.

## C.1) Skill Drift Management (Available Now)

AgentDB now exposes a high-level skills lifecycle API for drift-oriented operations:

- `db.skills_api.create_skill(...)`
- `db.skills_api.update_skill_version(...)`
- `db.skills_api.list_skills_for_agent(...)`
- `db.skills_api.list_stale_skills_for_agent(...)`
- `db.skills_api.list_skill_warnings_for_agent(...)`

Example:

```python
from agentdb import AgentDB

db = AgentDB(path="./agentdb.db", permissions_path="./permissions.yaml")

db.skills_api.create_skill(
    skill_id="analytics-style",
    name="analytics-style",
    content="Use concise bullet points and include assumptions.",
    scope=["research_agent"],
)

skills = db.skills_api.list_skills_for_agent(agent_id="research_agent")
warnings = db.skills_api.list_skill_warnings_for_agent(agent_id="research_agent")
stale = db.skills_api.list_stale_skills_for_agent(agent_id="research_agent")

if stale:
    db.skills_api.update_skill_version(
        skill_id=stale[0].id,
        content="Updated guidance after schema change.",
        new_version="1.1.0",
    )
```

## D) Two Operating Modes: Full Storage vs Budget Optimized

Use one explicit profile per runtime to avoid overlap and token spikes.

### Profile 1: `full_storage`

Use when:

- debugging or auditing behavior
- high-compliance workflows
- lower request volume / larger context budgets

Behavior:

- persist most user/assistant/tool events as episodic/session memories
- always retrieve with higher limits
- include richer memory context in prompts

Suggested defaults:

- `retrieve_limit`: 15-30
- write threshold: low (store most reusable turns)
- include session summaries + candidate preference updates each turn

Trade-off:

- best observability and recall
- highest storage growth and token usage

### Profile 2: `budget_optimized`

Use when:

- production latency/cost sensitivity
- high throughput
- strict token ceilings

Behavior:

- store only objective high-value items:
  - explicit user preferences
  - confirmed decisions
  - concise end-of-session summaries
- always call retrieve, but inject only top compact items

Suggested defaults:

- `retrieve_limit`: 3-8
- strict minimum score gate
- per-turn memory token budget (for example 10-20 percent of prompt budget)
- summarize/compact episodic memory before injection

Trade-off:

- lower latency and token usage
- reduced long-tail recall unless summaries are maintained well

### Practical Recommendation

Start with `full_storage` in pilot, measure:

- `retrieval_count_total`
- `outcome_positive_rate`
- `external_conflict_rate`

Then migrate to `budget_optimized` with explicit thresholds based on observed quality and cost.
