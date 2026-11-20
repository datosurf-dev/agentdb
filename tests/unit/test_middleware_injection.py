import os
import tempfile

import pytest
from pathlib import Path
from agentdb import AgentDB
from agentdb.adapters.middleware import AgentDBMiddleware
from agentdb.adapters.capabilities import LOCAL_CAPABILITIES, CLAUDE_CAPABILITIES

PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


@pytest.fixture
def db(tmp_path):
    return AgentDB(path=tmp_path / "t.db", permissions_path=str(PERMS))


async def test_no_skills_block_in_local_context(db):
    """Skills are no longer managed or injected by AgentDB (contract P-10)."""
    mw = AgentDBMiddleware(db=db, agent_id="agent1", capabilities=LOCAL_CAPABILITIES)
    messages, _ = await mw.prepare_turn_async(
        user_query="how do I commit this?", session_id="s1"
    )
    system_contents = [m["content"] for m in messages if m["role"] == "system"]
    assert not any("## Skills" in c for c in system_contents)


async def test_no_skills_block_in_claude_context(db):
    mw = AgentDBMiddleware(db=db, agent_id="agent1", capabilities=CLAUDE_CAPABILITIES)
    messages, _ = await mw.prepare_turn_async(
        user_query="how do I commit this?", session_id="s1"
    )
    system_contents = [m["content"] for m in messages if m["role"] == "system"]
    assert not any("## Skills" in c for c in system_contents)


@pytest.mark.asyncio
async def test_workspace_block_injected_for_local(db, tmp_path):
    mw = AgentDBMiddleware(db=db, agent_id="a1", capabilities=LOCAL_CAPABILITIES)
    messages, _ = await mw.prepare_turn_async(
        user_query="show me the files",
        session_id="s1",
        working_dirs=[str(tmp_path)],
    )
    system_contents = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "## Workspace" in system_contents
    assert str(tmp_path) in system_contents


@pytest.mark.asyncio
async def test_workspace_includes_temp_dirs(db, tmp_path):
    mw = AgentDBMiddleware(db=db, agent_id="a1", capabilities=LOCAL_CAPABILITIES)
    mw.report_directories("s1", ["/tmp/new-feature"])
    messages, _ = await mw.prepare_turn_async(
        user_query="what dirs", session_id="s1", working_dirs=[str(tmp_path)]
    )
    system_contents = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "new-feature" in system_contents
    assert "this session" in system_contents


@pytest.mark.asyncio
async def test_workspace_not_injected_for_claude(db, tmp_path):
    mw = AgentDBMiddleware(db=db, agent_id="a1", capabilities=CLAUDE_CAPABILITIES)
    messages, _ = await mw.prepare_turn_async(
        user_query="files", session_id="s1", working_dirs=[str(tmp_path)]
    )
    system_contents = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "## Workspace" not in system_contents


@pytest.mark.asyncio
async def test_work_context_injected_for_local(db):
    mw = AgentDBMiddleware(db=db, agent_id="a1", capabilities=LOCAL_CAPABILITIES)
    mw.record_turn_context("s1", 1, ["models/foo.sql"], [], "compiled foo model")
    mw.record_turn_context("s1", 2, [], ["models/bar.sql"], "wrote bar model")
    messages, _ = await mw.prepare_turn_async(user_query="what next", session_id="s1")
    system_contents = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "## Current work context" in system_contents
    assert "foo.sql" in system_contents
    assert "bar.sql" in system_contents


@pytest.mark.asyncio
async def test_budget_enforcement_trims_workspace_block(db, tmp_path):
    from agentdb.adapters.capabilities import RunnerCapabilities
    tiny_caps = RunnerCapabilities(inject_workspace=True,
                                   inject_work_context=False, max_injection_tokens=10)
    mw = AgentDBMiddleware(db=db, agent_id="a1", capabilities=tiny_caps)
    messages, _ = await mw.prepare_turn_async(
        user_query="files", session_id="s1", working_dirs=[str(tmp_path)]
    )
    # Block must be absent or truncated — content must not exceed budget
    ws_content = next((m["content"] for m in messages
                       if m["role"] == "system" and "## Workspace" in m["content"]), "")
    token_estimate = len(ws_content) // 4
    assert token_estimate <= 12  # budget 10 plus header line


@pytest.mark.asyncio
async def test_injection_order_workspace_before_context(db, tmp_path):
    mw = AgentDBMiddleware(db=db, agent_id="a1", capabilities=LOCAL_CAPABILITIES)
    mw.record_turn_context("s1", 1, ["f.py"], [], "fact")
    messages, _ = await mw.prepare_turn_async(
        user_query="files", session_id="s1", working_dirs=[str(tmp_path)]
    )
    sys_msgs = [m["content"] for m in messages if m["role"] == "system"]
    idxs = {
        "workspace": next((i for i, c in enumerate(sys_msgs) if "## Workspace" in c), -1),
        "context": next((i for i, c in enumerate(sys_msgs) if "## Current work context" in c), -1),
    }
    assert idxs["workspace"] < idxs["context"]
