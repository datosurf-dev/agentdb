"""
Tests AgentDBClaudeMiddleware runtime contract for Claude-style integrations.
No claude SDK dependency required.
"""
import json
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


def test_prepare_turn_orders_override_instructions_before_memory_content(tmp_path):
    """Principle 9: conflict flags must be assembled BEFORE human-approved
    memories, not after — an agent must know a memory is contested before it
    receives the memory content itself."""
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write("memory/working/pref_lang", "English", origin="human_approved", agent_id="research_agent")
    db.write("memory/working/pref_lang", "French", origin="agent_inferred", agent_id="research_agent")

    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")
    messages, retrieval = middleware.prepare_turn(
        user_query="lang", session_id="sess-order-1",
    )

    assert retrieval.override_instructions, "test setup must produce a real authority conflict"
    override_index = next(
        i for i, m in enumerate(messages)
        if m["role"] == "system" and m["content"].startswith("Override instruction:")
    )
    memory_index = next(
        i for i, m in enumerate(messages)
        if m["role"] == "system" and "Governed memory context" in m["content"]
    )
    assert override_index < memory_index


def test_prepare_turn_isolate_by_agent_true_excludes_other_agent_records(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/other_agent_note",
        value="use concise bullets",
        origin="agent_inferred",
        agent_id="coder_agent",
    )
    db.write(
        key="memory/working/mine_note",
        value="use concise bullets",
        origin="agent_inferred",
        agent_id="research_agent",
    )

    middleware = AgentDBClaudeMiddleware(
        db=db, agent_id="research_agent", profile="budget_optimized", isolate_by_agent=True,
    )
    _, retrieval = middleware.prepare_turn(
        user_query="concise bullets", session_id="sess-isolated-1",
    )

    assert [r.key for r in retrieval.records] == ["memory/working/mine_note"]


def test_prepare_turn_isolate_by_agent_default_false_returns_mixed_pool(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/other_agent_note",
        value="use concise bullets",
        origin="agent_inferred",
        agent_id="coder_agent",
    )
    db.write(
        key="memory/working/mine_note",
        value="use concise bullets",
        origin="agent_inferred",
        agent_id="research_agent",
    )

    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")
    _, retrieval = middleware.prepare_turn(
        user_query="concise bullets", session_id="sess-mixed-1",
    )

    assert {r.key for r in retrieval.records} == {
        "memory/working/other_agent_note",
        "memory/working/mine_note",
    }


def test_prepare_turn_conversation_started_trigger_can_influence_retrieval(tmp_path):
    workspace_root = tmp_path / "memory-home"
    db = AgentDB(memory_dir=workspace_root, permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(
        db=db,
        agent_id="research_agent",
        profile="budget_optimized",
        conversation_started_callback=lambda **kwargs: kwargs["db"].write(
            key="memory/working/turn_start_hint",
            value="always mention the tracked workspace note",
            origin="agent_inferred",
            agent_id=kwargs["agent_id"],
            session_id=kwargs["session_id"],
        ),
    )

    messages, retrieval = middleware.prepare_turn(
        user_query="tracked workspace note",
        session_id="sess-trigger-1",
        system_prompt="You are a helpful assistant.",
    )

    assert any(record.key == "memory/working/turn_start_hint" for record in retrieval.records)
    assert any(
        "turn_start_hint" in message["content"]
        for message in messages
        if message["role"] == "system"
    )


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


def test_prepare_turn_passes_memory_budget_tokens_and_reports_truncation(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    for i in range(5):
        db.write(
            key=f"memory/working/note_{i}",
            value="note " + ("x" * 200),
            origin="agent_inferred",
            agent_id="research_agent",
            session_id="sess-budget-1",
        )

    middleware = AgentDBClaudeMiddleware(
        db=db,
        agent_id="research_agent",
        profile="full_storage",
        memory_budget_tokens=60,
    )

    _, retrieval = middleware.prepare_turn(
        user_query="note",
        session_id="sess-budget-1",
    )

    assert retrieval.truncated is True
    assert retrieval.token_estimate <= 60


def test_prepare_turn_without_memory_budget_tokens_does_not_truncate(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/note_0",
        value="note " + ("x" * 200),
        origin="agent_inferred",
        agent_id="research_agent",
        session_id="sess-budget-2",
    )

    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="full_storage")

    _, retrieval = middleware.prepare_turn(
        user_query="note",
        session_id="sess-budget-2",
    )

    assert retrieval.truncated is False


def test_middleware_init_registers_conflict_handler(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")
    assert "conflict_detected" in db.gateway._handlers


def test_prepare_turn_dispatches_conversation_started_trigger(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    dispatched = []
    db.gateway.subscribe("conversation_started", lambda **kw: dispatched.append(kw))
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    middleware.prepare_turn(user_query="hi", session_id="sess-gateway-1")

    assert len(dispatched) >= 1
    assert dispatched[0]["session_id"] == "sess-gateway-1"


def test_pending_conflicts_returns_gateway_conflict_queue(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    assert middleware.pending_conflicts() == db.gateway.conflict_queue(resolved=False)


def test_propose_skill_creates_pending_proposal(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    proposal_id = middleware.propose_skill(
        name="dbt-style",
        content="Use concise bullet points.",
        scope=["research_agent"],
    )

    pending = middleware.list_proposals(status="pending")
    assert any(p["proposal_id"] == proposal_id for p in pending)


def test_approve_proposal_creates_skill(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")
    proposal_id = middleware.propose_skill(
        name="dbt-style",
        content="Use concise bullet points.",
        scope=["research_agent"],
    )

    middleware.approve_proposal(proposal_id)

    skills = db.skills.get_for_agent("research_agent")
    assert any(s.id == proposal_id for s in skills)


def test_reject_proposal_does_not_create_skill(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")
    proposal_id = middleware.propose_skill(
        name="dbt-style",
        content="Use concise bullet points.",
        scope=["research_agent"],
    )

    middleware.reject_proposal(proposal_id, reason="not needed")

    skills = db.skills.get_for_agent("research_agent")
    assert not any(s.id == proposal_id for s in skills)
    rejected = middleware.list_proposals(status="rejected")
    assert any(p["proposal_id"] == proposal_id for p in rejected)


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


def test_finalize_turn_tags_persisted_records_with_touched_entities(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="full_storage")

    middleware.finalize_turn(
        session_id="sess-claude-entities-1",
        model_response="stored response",
        touched_entities=["models/marts/core/report_model.sql", "report_model"],
    )

    records = db._store.list_by_agent("research_agent")
    persisted = [record for record in records if record.key.startswith("memory/episodic/claude/")]
    assert len(persisted) == 1
    assert persisted[0].entities == ["models/marts/core/report_model.sql", "report_model"]


def test_finalize_turn_applies_touched_entities_to_explicit_memory_writes(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    middleware.finalize_turn(
        session_id="sess-claude-entities-2",
        model_response="not auto persisted",
        touched_entities=["report_model"],
        memory_writes=[
            {
                "key": "memory/episodic/research_agent/notes/report_model",
                "value": "remember downstream contract",
                "origin": "agent_inferred",
            }
        ],
    )

    stored = db.read("memory/episodic/research_agent/notes/report_model", agent_id="research_agent")
    assert stored is not None
    assert stored.entities == ["report_model"]


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


def test_finalize_turn_writes_conversation_jsonl_when_enabled(tmp_path):
    workspace_root = tmp_path / "memory-home"
    config_dir = workspace_root / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "memory.yaml").write_text(
        "\n".join(
            [
                "mode: budget_optimized",
                "tracked_directories: []",
                "capture:",
                "  conversation_jsonl: true",
                "reconciliation:",
                "  triggers:",
                "    - conversation_started",
                "database:",
                "  mode: embedded",
                "  path: database/agentdb.sqlite",
            ]
        ),
        encoding="utf-8",
    )
    db = AgentDB(memory_dir=workspace_root, permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    middleware.finalize_turn(
        session_id="sess-jsonl-1",
        user_query="summarize the change",
        model_response="Here is the summary.",
        message_id="msg-jsonl-1",
    )

    output_path = workspace_root / "conversations" / "research_agent--sess-jsonl-1.jsonl"
    assert output_path.exists() is True
    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["session_id"] == "sess-jsonl-1"
    assert payload["message_id"] == "msg-jsonl-1"
    assert payload["user_query"] == "summarize the change"
    assert payload["model_response"] == "Here is the summary."


def test_finalize_turn_skips_conversation_jsonl_when_disabled(tmp_path):
    workspace_root = tmp_path / "memory-home"
    db = AgentDB(memory_dir=workspace_root, permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    middleware.finalize_turn(
        session_id="sess-jsonl-2",
        user_query="summarize the change",
        model_response="Here is the summary.",
        message_id="msg-jsonl-2",
    )

    output_path = workspace_root / "conversations" / "research_agent--sess-jsonl-2.jsonl"
    assert output_path.exists() is False


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


def test_runtime_wrapper_from_claude_client_threads_memory_budget_tokens(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    for i in range(5):
        db.write(
            key=f"memory/working/note_{i}",
            value="note " + ("x" * 200),
            origin="agent_inferred",
            agent_id="research_agent",
            session_id="sess-runtime-budget-1",
        )

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
        memory_budget_tokens=60,
    )
    result = runtime.handle_turn(
        user_query="note",
        session_id="sess-runtime-budget-1",
    )

    assert result["retrieval"].truncated is True
    assert result["retrieval"].token_estimate <= 60


def test_build_action_context_uses_edit_file_path_and_model_name(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/dbt/model_hint",
        value="run this model with full-refresh",
        origin="agent_inferred",
        agent_id="research_agent",
        entities=["models/marts/core/report_model.sql", "report_model"],
    )
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    context = middleware.build_action_context(
        tool_name="Edit",
        tool_input={"file_path": "models/marts/core/report_model.sql"},
        session_id="sess-action-1",
    )

    assert "Governed action context" in context
    assert "model_hint" in context


def test_build_action_context_uses_bash_dbt_select_target(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/dbt/model_hint",
        value="always validate downstream exposures",
        origin="agent_inferred",
        agent_id="research_agent",
        entities=["report_model"],
    )
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    context = middleware.build_action_context(
        tool_name="Bash",
        tool_input={"command": "dbt build --select report_model"},
        session_id="sess-action-2",
    )

    assert "Governed action context" in context
    assert "validate downstream exposures" in context


def test_build_action_context_is_noop_when_tool_has_no_entity(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    context = middleware.build_action_context(
        tool_name="Bash",
        tool_input={"command": "echo hello"},
        session_id="sess-action-3",
    )

    assert context == ""


def test_pre_tool_use_returns_additional_context_and_touched_entities(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/dbt/model_hint",
        value="review downstream contracts before editing",
        origin="agent_inferred",
        agent_id="research_agent",
        entities=["models/marts/core/report_model.sql", "report_model"],
    )
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    payload = middleware.pre_tool_use(
        tool_name="Edit",
        tool_input={"file_path": "models/marts/core/report_model.sql"},
        session_id="sess-pretool-1",
    )

    assert payload["touchedEntities"] == ["models/marts/core/report_model.sql", "report_model"]
    assert "Governed action context" in payload["additionalContext"]
    assert "review downstream contracts" in payload["additionalContext"]


def test_pre_tool_use_is_noop_when_no_matching_context(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    payload = middleware.pre_tool_use(
        tool_name="Edit",
        tool_input={"file_path": "models/marts/core/unknown_model.sql"},
        session_id="sess-pretool-2",
    )

    assert payload == {}


def test_build_hook_handlers_exposes_pre_tool_use_contract(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.write(
        key="memory/working/dbt/model_hint",
        value="review downstream contracts before editing",
        origin="agent_inferred",
        agent_id="research_agent",
        entities=["models/marts/core/report_model.sql", "report_model"],
    )
    middleware = AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")

    hooks = middleware.build_hook_handlers()

    assert "PreToolUse" in hooks
    payload = hooks["PreToolUse"](
        tool_name="Edit",
        tool_input={"file_path": "models/marts/core/report_model.sql"},
        session_id="sess-hook-1",
    )
    assert payload["touchedEntities"] == ["models/marts/core/report_model.sql", "report_model"]
    assert "Governed action context" in payload["additionalContext"]


def test_runtime_get_sdk_hooks_delegates_to_middleware(tmp_path):
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
        profile="budget_optimized",
    )

    hooks = runtime.get_sdk_hooks()

    assert "PreToolUse" in hooks
    assert callable(hooks["PreToolUse"])


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


@pytest.mark.asyncio
async def test_from_sdk_client_threads_memory_budget_tokens(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    for i in range(5):
        db.write(
            key=f"memory/working/note_{i}",
            value="note " + ("x" * 200),
            origin="agent_inferred",
            agent_id="research_agent",
            session_id="sess-sdk-budget-1",
        )

    class FakeAssistantMessage:
        def __init__(self, text):
            self.content = [type("Block", (), {"text": text})()]

    class FakeResultMessage:
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
        memory_budget_tokens=60,
    )

    result = await runtime.handle_turn_async(
        user_query="note",
        session_id="sess-sdk-budget-1",
    )

    assert result["retrieval"].truncated is True
    assert result["retrieval"].token_estimate <= 60
