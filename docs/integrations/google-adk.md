# Google ADK Integration (Current Snapshot)

This guide shows how to use AgentDB as governed memory for a Google ADK agent through:

- `src/agentdb/adapters/google_adk.py`
- `AgentDBMemoryService`

## What This Adapter Does

- Stores session events into AgentDB keys:
  - `memory/working/adk/{app_name}/{user_id}/{session_id}/{event_index}`
- Supports memory lookup for a specific app+user:
  - `search_memory(app_name, user_id, query)`

Current search behavior is keyword-based (Phase 1), not semantic embeddings.

## 1) Wire AgentDB Into ADK Runner

```python
from agentdb import AgentDB
from agentdb.adapters.google_adk import AgentDBMemoryService
from google.adk import Runner

db = AgentDB(path="./agentdb.db", permissions_path="./permissions.yaml")
memory_service = AgentDBMemoryService(db=db, agent_id="support_agent")

runner = Runner(
    agent=agent,
    memory_service=memory_service,
)
```

## 2) Permission Requirements

Your ADK agent identity must be able to write/read the memory zone used by the adapter.

Minimum practical access:

- write: `memory/working/`
- read: `memory/working/`

## 3) Session Persistence Pattern

Persist conversation events to memory after or during each session:

```python
memory_service.add_session_to_memory(session)
```

Retrieve relevant memory snippets before high-stakes responses:

```python
resp = memory_service.search_memory(
    app_name="banking_assistant",
    user_id="user_123",
    query="card replacement address",
)
```

## 4) Effective Guidance Pattern

Use two layers together:

1. Raw session memory through ADK adapter (`add_session_to_memory`, `search_memory`)
2. Durable canonical memory through AgentDB client (`write`, `retrieve`, `record_outcome`)

Example durable write:

```python
db.write(
    key="memory/long_term/user_123/alert_channel",
    value="email",
    origin="agent_inferred",
    agent_id="support_agent",
    session_id=session.id,
)
```

## 5) Operational Notes

- Adapter and provider ingestion are different concerns:
  - adapter (`adapters/google_adk.py`): AgentDB is ADK runtime memory backend
  - provider (`providers/google.py`, planned): external GCP memory sync into AgentDB
- Keep terminology consistent in prompts/queries until semantic retrieval phase is active.
