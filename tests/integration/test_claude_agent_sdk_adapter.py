"""
Tests AgentDBClaudeMiddleware runtime contract for Claude-style integrations.
No claude SDK dependency required.
"""
import pytest
from pathlib import Path
from types import SimpleNamespace

from agentdb import AgentDB
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeMiddleware, AgentDBClaudeRuntime
from agentdb.interface.client import EVENT_MEMORY_OUTCOME, EVENT_MEMORY_RETRIEVE, EVENT_MEMORY_WRITE


PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def test_prepare_turn_enforces_retrieve_and_builds_messages(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/user_style",
        value="use concise bullets",
        origin="agent_inferred",
        agent_id="research_agent",
        session_id="sess-claude-1",
    )

    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")
    messages, retrieval = middleware.prepare_turn(
        user_query="how should I answer?",
        session_id="sess-claude-1",
        system_prompt="You are a helpful assistant.",
    )

    assert len(retrieval.records) >= 1
    assert messages[0]["role"] == "system"
    assert any("Governed memory context" in m["content"] for m in messages if m["role"] == "system")
    entries = db.audit_log.replay(from_sequence=0)
    assert any(entry.event_type == EVENT_MEMORY_RETRIEVE for entry in entries)


def test_run_turn_full_storage_auto_persists_and_records_outcome(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="full_storage")

    response = middleware.run_turn(
        user_query="summarize this",
        session_id="sess-claude-2",
        call_model=lambda messages: "Here is a summary.",
        outcome_type="task_complete",
        outcome_value=1.0,
    )

    assert response == "Here is a summary."
    entries = db.audit_log.replay(from_sequence=0)
    assert any(entry.event_type == EVENT_MEMORY_RETRIEVE for entry in entries)
    assert any(entry.event_type == EVENT_MEMORY_OUTCOME for entry in entries)

    raw_records = db._store.list_by_agent("research_agent")
    assert any(record.key.startswith("memory/episodic/claude/") for record in raw_records)
    assert any(entry.event_type == EVENT_MEMORY_WRITE for entry in entries)


def test_budget_optimized_no_auto_persist_without_explicit_writes(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    middleware.run_turn(
        user_query="what did user prefer?",
        session_id="sess-claude-3",
        call_model=lambda messages: "No durable write needed.",
    )

    records = db._store.list_by_agent("research_agent")
    assert not any(record.key.startswith("memory/episodic/claude/") for record in records)


def test_finalize_turn_persists_explicit_memory_writes(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    middleware.finalize_turn(
        session_id="sess-claude-4",
        model_response="not persisted automatically",
        memory_writes=[
            {
                "key": "memory/episodic/research_agent/preferences/format",
                "value": "markdown bullets",
                "origin": "agent_inferred",
            }
        ],
    )

    stored = db.read("memory/episodic/research_agent/preferences/format", agent_id="research_agent")
    assert stored is not None
    assert stored.value == "markdown bullets"


def test_finalize_turn_persists_directory_tracking_with_message_link(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")
    tracked_dir = tmp_path / "skills"
    tracked_dir.mkdir()
    (tracked_dir / "a.md").write_text("critical content", encoding="utf-8")

    middleware.finalize_turn(
        session_id="sess-claude-5",
        model_response="done",
        message_id="msg-001",
        tracked_directories=[str(tracked_dir)],
    )

    records = db._store.list_by_agent("research_agent")
    tracked_records = [
        r for r in records if "/directory_tracking/" in r.key and r.key.startswith("memory/episodic/claude/")
    ]
    assert len(tracked_records) == 1
    payload = tracked_records[0].value
    assert payload["session_id"] == "sess-claude-5"
    assert payload["message_id"] == "msg-001"
    assert payload["directory_snapshot"]["exists"] is True
    assert payload["directory_snapshot"]["file_count"] == 1


def test_finalize_turn_uses_agentdb_default_tracking_directories(tmp_path):
    tracked_dir = tmp_path / "skills"
    tracked_dir.mkdir()
    (tracked_dir / "skill.md").write_text("skill", encoding="utf-8")
    db = AgentDB(
        path=tmp_path / "test.db",
        permissions_path=PERMS,
        tracking_directories=["skills"],
        tracking_root=tmp_path,
        tracking_max_files_per_directory=50,
    )
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    middleware.finalize_turn(
        session_id="sess-claude-6",
        model_response="done",
        message_id="msg-002",
    )

    records = db._store.list_by_agent("research_agent")
    tracked_records = [
        r for r in records if "/directory_tracking/" in r.key and r.key.startswith("memory/episodic/claude/")
    ]
    assert len(tracked_records) == 1
    snapshot = tracked_records[0].value["directory_snapshot"]
    assert snapshot["directory"] == "skills"
    assert snapshot["exists"] is True
    assert snapshot["file_count"] == 1


def test_runtime_wrapper_from_claude_client_minimal_flow(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

    class FakeMessagesAPI:
        def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(text="runtime wrapper response")])

    fake_client = SimpleNamespace(messages=FakeMessagesAPI())
    runtime = AgentDBClaudeRuntime.from_claude_client(
        db=db,
        agent_id="research_agent",
        client=fake_client,
        model="claude-sonnet-test",
        profile="full_storage",
        system_prompt="You are a Slack assistant.",
    )
    result = runtime.handle_turn(
        user_query="summarize this slack thread",
        session_id="sess-runtime-1",
        message_id="msg-runtime-1",
        outcome_type="task_complete",
        outcome_value=1.0,
    )

    assert result["response_text"] == "runtime wrapper response"
    entries = db.audit_log.replay(from_sequence=0)
    assert any(e.event_type == EVENT_MEMORY_RETRIEVE for e in entries)
    assert any(e.event_type == EVENT_MEMORY_OUTCOME for e in entries)
    assert any(e.event_type == EVENT_MEMORY_WRITE for e in entries)


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


@pytest.mark.asyncio
async def test_from_sdk_client_handle_turn_async(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

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
