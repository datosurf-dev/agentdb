# Claude Agent SDK Async Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add async-native `run_turn_async` / `handle_turn_async` methods and a `from_sdk_client` factory to `AgentDBClaudeMiddleware` / `AgentDBClaudeRuntime` so the middleware works with `ClaudeSDKClient` (the agentic subprocess runtime) instead of only `messages.create()`, then update the Slack example to use it.

**Architecture:** The existing middleware enforces the governed turn contract (retrieve → call_model → persist → record_outcome). The `call_model` callable is currently sync; we add an async variant `call_model_async: Callable[..., Awaitable[str]]` to support `ClaudeSDKClient.query()` + `receive_response()`. A `from_sdk_client` classmethod on `AgentDBClaudeRuntime` builds this callable from a `ClaudeSDKClient` factory function. The slack-assistant's `claude_runner.py` is then wired to call `AgentDB` at the turn boundary (before `client.query()` and after `receive_response()`).

**Tech Stack:** Python 3.11+, `pytest-asyncio` (already in dev deps), `claude_agent_sdk` (available in slack-assistant venv), `agentdb` (editable install).

---

## File Map

| File | Change |
|---|---|
| `src/agentdb/adapters/claude_agent_sdk.py` | Add `prepare_turn_async`, `run_turn_async`, `finalize_turn_async`; add `from_sdk_client` factory on `AgentDBClaudeRuntime`; add `handle_turn_async` |
| `tests/integration/test_claude_agent_sdk_adapter.py` | Add async tests for new methods and `from_sdk_client` factory |
| `examples/slack_assistant_agentdb_runtime.py` | Replace `FakeClaudeClient` / `messages.create()` with async `ClaudeSDKClient`-style fake; update `SlackAgentHandler` to use `handle_turn_async` |
| `slack-assistant/claude_runner.py` | Conditional: if `slack-assistant/` exists in your workspace, wire `AgentDB` around `run_prompt`: retrieve before `client.query()`, persist response + record outcome after `receive_response()` |
| `slack-assistant/config.py` | Conditional: if `slack-assistant/` exists, add `AGENTDB_PATH` and `AGENTDB_PERMISSIONS_PATH` config constants |
| `slack-assistant/bot.py` | Conditional: if `slack-assistant/` exists, instantiate `AgentDB` at startup and pass to `claude_runner` |

If `slack-assistant/` is not present, complete adapter and integration-example files in this repository first; then apply equivalent wiring in the downstream app repository.

---

## Task 1: Add async methods to `AgentDBClaudeMiddleware`

**Files:**
- Modify: `src/agentdb/adapters/claude_agent_sdk.py`

The existing `prepare_turn`, `finalize_turn`, and `run_turn` are sync. We add async variants. `prepare_turn` only calls `db.retrieve()` which is currently sync — the async version is identical but `async def` so it can be awaited in async contexts. `finalize_turn` calls `db.write()` and `db.record_outcome()` (both sync) — same pattern. `run_turn_async` awaits an async `call_model_async` callable.

- [ ] **Step 1: Write the failing tests**

In `tests/integration/test_claude_agent_sdk_adapter.py`, add at the end of the file:

```python
import pytest


@pytest.mark.asyncio
async def test_run_turn_async_full_storage_persists_and_records_outcome(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="full_storage")

    async def fake_model_async(messages):
        return "async summary response"

    response = await middleware.run_turn_async(
        user_query="summarize this",
        session_id="sess-async-1",
        call_model_async=fake_model_async,
        outcome_type="task_complete",
        outcome_value=1.0,
    )

    assert response == "async summary response"
    entries = db.audit_log.replay(from_sequence=0)
    assert any(e.event_type == EVENT_MEMORY_RETRIEVE for e in entries)
    assert any(e.event_type == EVENT_MEMORY_OUTCOME for e in entries)
    records = db._store.list_by_agent("research_agent")
    assert any(r.key.startswith("memory/episodic/claude/") for r in records)


@pytest.mark.asyncio
async def test_run_turn_async_budget_optimized_no_auto_persist(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    async def fake_model_async(messages):
        return "no persist"

    await middleware.run_turn_async(
        user_query="what did user prefer?",
        session_id="sess-async-2",
        call_model_async=fake_model_async,
    )

    records = db._store.list_by_agent("research_agent")
    assert not any(r.key.startswith("memory/episodic/claude/") for r in records)


@pytest.mark.asyncio
async def test_prepare_turn_async_retrieves_memory(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/pref",
        value="use bullets",
        origin="agent_inferred",
        agent_id="research_agent",
        session_id="sess-async-3",
    )
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    messages, retrieval = await middleware.prepare_turn_async(
        user_query="how should I respond?",
        session_id="sess-async-3",
        system_prompt="You are a Slack assistant.",
    )

    assert len(retrieval.records) >= 1
    assert any("Governed memory context" in m["content"] for m in messages if m["role"] == "system")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jm-nx/documents/github/agentdb
source .venv/bin/activate
python -m pytest tests/integration/test_claude_agent_sdk_adapter.py::test_run_turn_async_full_storage_persists_and_records_outcome tests/integration/test_claude_agent_sdk_adapter.py::test_run_turn_async_budget_optimized_no_auto_persist tests/integration/test_claude_agent_sdk_adapter.py::test_prepare_turn_async_retrieves_memory -v
```

Expected: `AttributeError: 'AgentDBClaudeMiddleware' object has no attribute 'run_turn_async'`

- [ ] **Step 3: Implement async methods on `AgentDBClaudeMiddleware`**

In `src/agentdb/adapters/claude_agent_sdk.py`, add these imports at the top:

```python
from typing import TYPE_CHECKING, Any, Callable, Awaitable, Optional
```

Then add these three methods to `AgentDBClaudeMiddleware`, after the existing `run_turn` method (before `_response_key`):

```python
    async def prepare_turn_async(
        self,
        *,
        user_query: str,
        session_id: str,
        system_prompt: Optional[str] = None,
        external_context: Optional[list[dict[str, Any]]] = None,
        base_messages: Optional[list[dict[str, str]]] = None,
    ) -> tuple[list[dict[str, str]], "RetrievalResult"]:
        return self.prepare_turn(
            user_query=user_query,
            session_id=session_id,
            system_prompt=system_prompt,
            external_context=external_context,
            base_messages=base_messages,
        )

    async def finalize_turn_async(
        self,
        *,
        session_id: str,
        model_response: str,
        memory_writes: Optional[list[dict[str, Any]]] = None,
        message_id: Optional[str] = None,
        tracked_directories: Optional[list[str]] = None,
        directory_snapshots: Optional[list[dict[str, Any]]] = None,
        tracking_base_dir: Optional[str] = None,
        max_files_per_directory: Optional[int] = None,
        outcome_type: Optional[str] = None,
        outcome_value: Optional[float] = None,
    ) -> None:
        self.finalize_turn(
            session_id=session_id,
            model_response=model_response,
            memory_writes=memory_writes,
            message_id=message_id,
            tracked_directories=tracked_directories,
            directory_snapshots=directory_snapshots,
            tracking_base_dir=tracking_base_dir,
            max_files_per_directory=max_files_per_directory,
            outcome_type=outcome_type,
            outcome_value=outcome_value,
        )

    async def run_turn_async(
        self,
        *,
        user_query: str,
        session_id: str,
        call_model_async: Callable[[list[dict[str, str]]], Awaitable[str]],
        system_prompt: Optional[str] = None,
        external_context: Optional[list[dict[str, Any]]] = None,
        base_messages: Optional[list[dict[str, str]]] = None,
        memory_writes: Optional[list[dict[str, Any]]] = None,
        message_id: Optional[str] = None,
        tracked_directories: Optional[list[str]] = None,
        directory_snapshots: Optional[list[dict[str, Any]]] = None,
        tracking_base_dir: Optional[str] = None,
        max_files_per_directory: Optional[int] = None,
        outcome_type: Optional[str] = None,
        outcome_value: Optional[float] = None,
    ) -> str:
        messages, _ = await self.prepare_turn_async(
            user_query=user_query,
            session_id=session_id,
            system_prompt=system_prompt,
            external_context=external_context,
            base_messages=base_messages,
        )
        response_text = await call_model_async(messages)
        await self.finalize_turn_async(
            session_id=session_id,
            model_response=response_text,
            memory_writes=memory_writes,
            message_id=message_id,
            tracked_directories=tracked_directories,
            directory_snapshots=directory_snapshots,
            tracking_base_dir=tracking_base_dir,
            max_files_per_directory=max_files_per_directory,
            outcome_type=outcome_type,
            outcome_value=outcome_value,
        )
        return response_text
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/integration/test_claude_agent_sdk_adapter.py::test_run_turn_async_full_storage_persists_and_records_outcome tests/integration/test_claude_agent_sdk_adapter.py::test_run_turn_async_budget_optimized_no_auto_persist tests/integration/test_claude_agent_sdk_adapter.py::test_prepare_turn_async_retrieves_memory -v
```

Expected: `3 passed`

- [ ] **Step 5: Run full test suite to check no regressions**

```bash
python -m pytest -q
```

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
cd /Users/jm-nx/documents/github/agentdb
git add src/agentdb/adapters/claude_agent_sdk.py tests/integration/test_claude_agent_sdk_adapter.py
git commit -m "feat: add async turn methods to AgentDBClaudeMiddleware"
```

---

## Task 2: Add `from_sdk_client` factory and `handle_turn_async` to `AgentDBClaudeRuntime`

**Files:**
- Modify: `src/agentdb/adapters/claude_agent_sdk.py`
- Modify: `tests/integration/test_claude_agent_sdk_adapter.py`

`from_sdk_client` accepts a callable that provides a `ClaudeSDKClient` (or equivalent) for a given `thread_id`. It builds the async `call_model_async` by calling `client.query(prompt)` + iterating `client.receive_response()`. The callable pattern (factory rather than a bare client) mirrors how `claude_runner.py` manages one client per thread.

The `ClaudeSDKClient` interface expected:
- `await client.query(prompt: str)` — sends the prompt
- `async for message in client.receive_response()` — yields `AssistantMessage` / `ResultMessage`
- `AssistantMessage.content` — list of blocks with `.text`
- `ResultMessage.session_id` — str; `ResultMessage.is_error` — bool; `ResultMessage.result` — str | None

These are already imported in `claude_runner.py`:
```python
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
```

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_claude_agent_sdk_adapter.py`:

```python
@pytest.mark.asyncio
async def test_from_sdk_client_handle_turn_async(tmp_path):
    from types import SimpleNamespace

    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

    # Fake ClaudeSDKClient that mimics query() + receive_response()
    class FakeTextBlock:
        def __init__(self, text):
            self.text = text

    class FakeAssistantMessage:
        def __init__(self, text):
            self.content = [FakeTextBlock(text)]

    class FakeResultMessage:
        session_id = "sdk-sess-001"
        is_error = False
        result = None

    class FakeSdkClient:
        async def query(self, prompt):
            self._last_prompt = prompt

        async def receive_response(self):
            yield FakeAssistantMessage("sdk agentic response")
            yield FakeResultMessage()

    def sdk_client_factory(thread_id):
        return FakeSdkClient()

    runtime = AgentDBClaudeRuntime.from_sdk_client(
        db=db,
        agent_id="research_agent",
        sdk_client_factory=sdk_client_factory,
        profile="full_storage",
        system_prompt="You are a Slack assistant.",
    )

    result = await runtime.handle_turn_async(
        user_query="summarize this thread",
        session_id="sdk-sess-001",
        message_id="msg-sdk-001",
        outcome_type="task_complete",
        outcome_value=1.0,
    )

    assert "sdk agentic response" in result["response_text"]
    entries = db.audit_log.replay(from_sequence=0)
    assert any(e.event_type == EVENT_MEMORY_RETRIEVE for e in entries)
    assert any(e.event_type == EVENT_MEMORY_OUTCOME for e in entries)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/integration/test_claude_agent_sdk_adapter.py::test_from_sdk_client_handle_turn_async -v
```

Expected: `AttributeError: type object 'AgentDBClaudeRuntime' has no attribute 'from_sdk_client'`

- [ ] **Step 3: Implement `from_sdk_client` and `handle_turn_async` on `AgentDBClaudeRuntime`**

Add to the `AgentDBClaudeRuntime` class in `src/agentdb/adapters/claude_agent_sdk.py`, after the existing `from_claude_client` classmethod:

```python
    @classmethod
    def from_sdk_client(
        cls,
        *,
        db: "AgentDB",
        agent_id: str,
        sdk_client_factory: Callable[[str], Any],
        profile: str = "budget_optimized",
        system_prompt: Optional[str] = None,
        retrieve_limit: Optional[int] = None,
        memory_budget_chars: Optional[int] = None,
    ) -> "AgentDBClaudeRuntime":
        """
        Build a runtime that wraps a ClaudeSDKClient (agentic subprocess).

        sdk_client_factory: callable(thread_id: str) -> ClaudeSDKClient
            Called once per handle_turn_async invocation with the session_id.
            The client must support: await client.query(prompt) and
            async-for client.receive_response() yielding AssistantMessage /
            ResultMessage objects.
        """
        middleware = AgentDBClaudeMiddleware(
            db=db,
            agent_id=agent_id,
            profile=profile,
            retrieve_limit=retrieve_limit,
            memory_budget_chars=memory_budget_chars,
        )
        # Store factory on instance for use by handle_turn_async.
        # invoke_model is set to a no-op sentinel; SDK path uses handle_turn_async.
        instance = cls(
            middleware=middleware,
            invoke_model=lambda messages: "",
            default_system_prompt=system_prompt,
        )
        instance._sdk_client_factory = sdk_client_factory
        return instance

    async def handle_turn_async(
        self,
        *,
        user_query: str,
        session_id: str,
        message_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        external_context: Optional[list[dict[str, Any]]] = None,
        base_messages: Optional[list[dict[str, str]]] = None,
        memory_writes: Optional[list[dict[str, Any]]] = None,
        tracked_directories: Optional[list[str]] = None,
        directory_snapshots: Optional[list[dict[str, Any]]] = None,
        tracking_base_dir: Optional[str] = None,
        max_files_per_directory: Optional[int] = None,
        outcome_type: Optional[str] = None,
        outcome_value: Optional[float] = None,
    ) -> dict[str, Any]:
        sdk_factory = getattr(self, "_sdk_client_factory", None)
        if sdk_factory is None:
            raise RuntimeError(
                "handle_turn_async requires a runtime built with from_sdk_client(). "
                "Use handle_turn() for the messages.create() path."
            )

        messages, retrieval = await self._middleware.prepare_turn_async(
            user_query=user_query,
            session_id=session_id,
            system_prompt=system_prompt if system_prompt is not None else self._default_system_prompt,
            external_context=external_context,
            base_messages=base_messages,
        )

        # Build the prompt from the assembled messages (memory context already injected).
        # The system blocks become the leading context; user query is the final message.
        system_blocks = [m["content"] for m in messages if m.get("role") == "system"]
        user_messages = [m for m in messages if m.get("role") == "user"]
        prompt_parts = system_blocks + [m["content"] for m in user_messages]
        prompt = "\n\n".join(prompt_parts)

        client = sdk_factory(session_id)
        await client.query(prompt)

        text_parts: list[str] = []
        async for message in client.receive_response():
            # Import guard: only import these types when the SDK is available.
            # The isinstance checks use duck-typing fallbacks for testability.
            content = getattr(message, "content", None)
            if content is not None:
                for block in content:
                    text = getattr(block, "text", None)
                    if text is not None:
                        text_parts.append(text)
            is_error = getattr(message, "is_error", False)
            if is_error:
                error_msg = getattr(message, "result", "Claude SDK error")
                raise RuntimeError(error_msg or "Claude SDK returned an error")

        response_text = "\n".join(text_parts).strip()

        await self._middleware.finalize_turn_async(
            session_id=session_id,
            model_response=response_text,
            memory_writes=memory_writes,
            message_id=message_id,
            tracked_directories=tracked_directories,
            directory_snapshots=directory_snapshots,
            tracking_base_dir=tracking_base_dir,
            max_files_per_directory=max_files_per_directory,
            outcome_type=outcome_type,
            outcome_value=outcome_value,
        )

        return {
            "response_text": response_text,
            "retrieval": retrieval,
            "messages": messages,
        }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/integration/test_claude_agent_sdk_adapter.py::test_from_sdk_client_handle_turn_async -v
```

Expected: `1 passed`

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest -q
```

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add src/agentdb/adapters/claude_agent_sdk.py tests/integration/test_claude_agent_sdk_adapter.py
git commit -m "feat: add from_sdk_client factory and handle_turn_async to AgentDBClaudeRuntime"
```

---

## Task 3: Update the Slack example to use the async SDK path

**Files:**
- Modify: `examples/slack_assistant_agentdb_runtime.py`

Replace `FakeClaudeClient` (which uses `messages.create()`) with a fake that mimics `ClaudeSDKClient`'s async interface. Update `SlackAgentHandler` to be async and call `handle_turn_async`.

- [ ] **Step 1: Rewrite `examples/slack_assistant_agentdb_runtime.py`**

Replace the entire file content with:

```python
"""
Reproducible Slack-style integration example for AgentDB + ClaudeSDKClient async runtime.

This shows the minimal app-layer wiring for the agentic subprocess path:
  - one AgentDB instance
  - one AgentDBClaudeRuntime built with from_sdk_client()
  - one async handler function per incoming thread message

The example uses a fake SDK client so it runs locally without credentials.
Replace FakeSdkClient / fake_sdk_factory with your real ClaudeSDKClient in production.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from agentdb import AgentDB
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeRuntime


@dataclass(frozen=True)
class IncomingSlackEvent:
    channel: str
    thread_id: str
    message_id: str
    user_id: str
    text: str


# ---------------------------------------------------------------------------
# Fake SDK client — replace with real ClaudeSDKClient in production.
# Mimics the interface: await client.query(prompt) + async-for receive_response()
# ---------------------------------------------------------------------------
class FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeAssistantMessage:
    def __init__(self, text: str) -> None:
        self.content = [FakeTextBlock(text)]


class FakeResultMessage:
    is_error = False
    result = None

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class FakeSdkClient:
    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id
        self._prompt: str = ""

    async def query(self, prompt: str) -> None:
        self._prompt = prompt

    async def receive_response(self):
        yield FakeAssistantMessage(f"Agentic reply to: {self._prompt[:80]}")
        yield FakeResultMessage(session_id=self._thread_id)


def fake_sdk_factory(thread_id: str) -> FakeSdkClient:
    """
    In production, return the ClaudeSDKClient for this thread_id.
    The slack-assistant's claude_runner already manages one client per thread —
    pass claude_runner.get_or_create_client(thread_id, ...) here.
    """
    return FakeSdkClient(thread_id)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
class SlackAgentHandler:
    def __init__(self, runtime: AgentDBClaudeRuntime) -> None:
        self.runtime = runtime

    async def handle_event(self, event: IncomingSlackEvent) -> str:
        result = await self.runtime.handle_turn_async(
            user_query=event.text,
            session_id=event.thread_id,
            message_id=event.message_id,
            outcome_type="task_complete",
            outcome_value=1.0,
        )
        return result["response_text"]


def build_runtime(db_path: str = "./agentdb.db") -> AgentDBClaudeRuntime:
    db = AgentDB(
        path=db_path,
        tracking_directories=["docs", ".claude/skills", "agent-sessions"],
        tracking_root=".",
        tracking_max_files_per_directory=200,
    )

    # Seed a durable memory record for demo relevance.
    db.write(
        key="memory/working/slack/preferences",
        value="respond in concise bullets for routine updates",
        origin="agent_inferred",
        agent_id="research_agent",
        session_id="demo-thread-1",
    )

    return AgentDBClaudeRuntime.from_sdk_client(
        db=db,
        agent_id="research_agent",
        sdk_client_factory=fake_sdk_factory,
        profile="budget_optimized",
        system_prompt="You are the engineering Slack assistant.",
    )


async def main() -> None:
    runtime = build_runtime()
    handler = SlackAgentHandler(runtime)

    event = IncomingSlackEvent(
        channel="C123",
        thread_id="demo-thread-1",
        message_id="msg-001",
        user_id="U123",
        text="Summarize what changed in this thread and next actions.",
    )
    reply = await handler.handle_event(event)
    print(reply)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run the example to verify it works**

```bash
cd /Users/jm-nx/documents/github/agentdb
source .venv/bin/activate
python examples/slack_assistant_agentdb_runtime.py
```

Expected output (contains):
```
Agentic reply to: You are the engineering Slack assistant.
```

- [ ] **Step 3: Run full test suite**

```bash
python -m pytest -q
```

Expected: all tests pass

- [ ] **Step 4: Commit**

```bash
git add examples/slack_assistant_agentdb_runtime.py
git commit -m "feat: update slack example to use ClaudeSDKClient async path"
```

---

## Task 4: Wire AgentDB into `slack-assistant/claude_runner.py`

**Files:**
- Modify: `slack-assistant/config.py`
- Modify: `slack-assistant/claude_runner.py`
- Modify: `slack-assistant/bot.py`

This wires the real integration. `AgentDB` is instantiated once at bot startup (in `bot.py`) and passed into `claude_runner`. In `run_prompt`, retrieve runs before `client.query()` and finalize runs after `receive_response()` completes.

Note: `claude_agent_sdk` is already installed in the slack-assistant venv. `agentdb` needs to be installed there too (see step 1).

- [ ] **Step 1: Install agentdb into the slack-assistant venv**

```bash
cd /Users/jm-nx/Documents/nx/slack-assistant
source .venv/bin/activate
pip install -e /Users/jm-nx/documents/github/agentdb
```

Expected: `Successfully installed agentdb-0.1.0`

- [ ] **Step 2: Add AgentDB config constants to `slack-assistant/config.py`**

Add at the end of the `PATHS & TIMEOUTS` section (after `REAPER_INTERVAL_SECONDS`):

```python
# -------------------------
# AGENTDB
# -------------------------
AGENTDB_PATH = os.environ.get("AGENTDB_PATH", "agent-sessions/agentdb.db")
AGENTDB_PERMISSIONS_PATH = os.environ.get(
    "AGENTDB_PERMISSIONS_PATH",
    # Falls back to the test fixture so the bot runs without a custom permissions file.
    str(Path(__file__).resolve().parent / "permissions.yaml"),
)
```

Also add `from pathlib import Path` at the top of `config.py` if not already present.

- [ ] **Step 3: Create `slack-assistant/permissions.yaml`**

Create this file in the slack-assistant directory:

```yaml
default_policy: deny

human_zone_prefixes:
  - "skills/"
  - "config/human/"
  - "prompts/locked/"

agents:
  slack_assistant:
    can_write:
      - "memory/working/"
      - "memory/episodic/"
      - "memory/org/"
      - "config/agent/learned_prefs/"
    cannot_write:
      - "config/human/"
      - "skills/"
    can_read:
      - "memory/working/"
      - "memory/episodic/"
      - "memory/long_term/"
      - "memory/org/"
```

- [ ] **Step 4: Update `slack-assistant/claude_runner.py` to accept and use AgentDB**

At the top of the file, add the import after the existing imports:

```python
from agentdb import AgentDB
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeMiddleware
```

Add a module-level optional AgentDB reference (after `_locks`):

```python
_agentdb: AgentDB | None = None
_agentdb_middleware: AgentDBClaudeMiddleware | None = None
```

Add an init function (after `_lock_for`):

```python
def init_agentdb(db: AgentDB) -> None:
    global _agentdb, _agentdb_middleware
    _agentdb = db
    _agentdb_middleware = AgentDBClaudeMiddleware(
        db=db,
        agent_id="slack_assistant",
        profile="budget_optimized",
    )
```

In `run_prompt`, add the AgentDB retrieve call before `client.query()` and finalize after `receive_response()` completes. Replace the body of the `try` block inside `async with _lock_for(thread_id):` with:

```python
        async with _lock_for(thread_id):
            try:
                client = await _get_or_create_client(
                    thread_id, repo_path, resume_session_id, post_fn
                )

                # --- AgentDB: retrieve governed memory before the turn ---
                retrieval = None
                if _agentdb_middleware is not None:
                    _, retrieval_result = await _agentdb_middleware.prepare_turn_async(
                        user_query=prompt,
                        session_id=thread_id,
                    )
                    retrieval = retrieval_result

                await client.query(prompt)

                text_parts: list[str] = []
                session_id = resume_session_id

                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text_parts.append(block.text)
                    elif isinstance(message, ResultMessage):
                        session_id = message.session_id
                        if message.is_error:
                            raise RuntimeError(message.result or "Claude returned an error")

                response_text = "\n".join(text_parts).strip()

                # --- AgentDB: finalize turn (persist + record outcome) ---
                if _agentdb_middleware is not None:
                    await _agentdb_middleware.finalize_turn_async(
                        session_id=thread_id,
                        model_response=response_text,
                        message_id=thread_id,
                        outcome_type="task_complete",
                        outcome_value=1.0,
                    )

                return response_text, session_id

            except RuntimeError:
                raise
            except Exception as e:
                await close_client(thread_id)
                raise RuntimeError(f"Claude SDK error: {e}") from e
```

- [ ] **Step 5: Instantiate AgentDB in `slack-assistant/bot.py` and call `init_agentdb`**

Add the import at the top of `bot.py` (after `import claude_runner`):

```python
from agentdb import AgentDB
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeMiddleware
import config
```

In the `main()` function, after `os.makedirs(config.BASE, exist_ok=True)`, add:

```python
    # Initialize AgentDB governed memory
    from pathlib import Path
    agentdb_permissions = Path(config.AGENTDB_PERMISSIONS_PATH)
    if not agentdb_permissions.exists():
        # Fall back to agentdb's test fixture if no local permissions file found
        import agentdb as _adb_pkg
        agentdb_permissions = Path(_adb_pkg.__file__).resolve().parents[3] / "tests" / "fixtures" / "permissions.test.yaml"
    db = AgentDB(
        path=config.AGENTDB_PATH,
        permissions_path=agentdb_permissions,
        tracking_directories=["agent-sessions"],
        tracking_root=".",
    )
    claude_runner.init_agentdb(db)
    print(f"[startup] AgentDB initialized at {config.AGENTDB_PATH}")
```

- [ ] **Step 6: Smoke-test the bot starts without errors**

```bash
cd /Users/jm-nx/Documents/nx/slack-assistant
source .venv/bin/activate
python -c "
import config
from agentdb import AgentDB
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeMiddleware
db = AgentDB(path='agent-sessions/agentdb.db', permissions_path='permissions.yaml')
import claude_runner
claude_runner.init_agentdb(db)
print('AgentDB wired OK')
print(db.system.agentdb_stats())
"
```

Expected output:
```
AgentDB wired OK
{'total_memories': 0, 'avg_confidence': 0.0, ...}
```

- [ ] **Step 7: Commit**

```bash
cd /Users/jm-nx/Documents/nx/slack-assistant
git add config.py claude_runner.py bot.py permissions.yaml
git commit -m "feat: wire AgentDB governed memory into slack-assistant bot"
```

---

## Self-Review

**Spec coverage:**
- ✅ Async `prepare_turn_async` / `finalize_turn_async` / `run_turn_async` on middleware — Task 1
- ✅ `from_sdk_client` factory on `AgentDBClaudeRuntime` — Task 2
- ✅ `handle_turn_async` on `AgentDBClaudeRuntime` — Task 2
- ✅ Slack example updated to SDK path — Task 3
- ✅ `claude_runner.py` wired with retrieve-before / finalize-after — Task 4
- ✅ `permissions.yaml` for the bot — Task 4 step 3
- ✅ `config.py` constants for AgentDB path — Task 4 step 2

**Placeholder scan:** None found — all steps contain concrete code.

**Type consistency:**
- `_agentdb_middleware` used as `AgentDBClaudeMiddleware | None` consistently across Task 4 steps.
- `from_sdk_client` stores factory as `instance._sdk_client_factory`; `handle_turn_async` reads it via `getattr` with a `None` guard.
- `prepare_turn_async` returns `tuple[list[dict], RetrievalResult]` — matches the `retrieval_result` variable name in Task 4.
- `finalize_turn_async` signature matches `finalize_turn` parameter names exactly.
