import os
import tempfile

import pytest
from pathlib import Path
from agentdb import AgentDB
from agentdb.adapters.middleware import AgentDBMiddleware
from agentdb.adapters.capabilities import LOCAL_CAPABILITIES, CLAUDE_CAPABILITIES
from agentdb.core.models import Skill

PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


@pytest.fixture
def db(tmp_path):
    return AgentDB(path=tmp_path / "t.db", permissions_path=str(PERMS))


async def test_skills_injected_for_local_when_keyword_matches(db):
    db.skills.create(Skill(
        id="git-workflow",
        name="git-workflow",
        content="Always run git status before committing.",
        authored_by="human",
        scope=["agent1"],
    ))
    mw = AgentDBMiddleware(db=db, agent_id="agent1", capabilities=LOCAL_CAPABILITIES)
    messages, _ = await mw.prepare_turn_async(
        user_query="how do I commit this?", session_id="s1"
    )
    system_contents = [m["content"] for m in messages if m["role"] == "system"]
    assert any("## Skills" in c for c in system_contents)
    assert any("git-workflow" in c for c in system_contents)


async def test_skills_not_injected_for_claude(db):
    db.skills.create(Skill(
        id="git-workflow",
        name="git-workflow",
        content="Always run git status before committing.",
        authored_by="human",
        scope=["agent1"],
    ))
    mw = AgentDBMiddleware(db=db, agent_id="agent1", capabilities=CLAUDE_CAPABILITIES)
    messages, _ = await mw.prepare_turn_async(
        user_query="how do I commit this?", session_id="s1"
    )
    system_contents = [m["content"] for m in messages if m["role"] == "system"]
    assert not any("## Skills" in c for c in system_contents)


async def test_no_skills_block_when_no_skills_exist(db):
    mw = AgentDBMiddleware(db=db, agent_id="agent1", capabilities=LOCAL_CAPABILITIES)
    messages, _ = await mw.prepare_turn_async(
        user_query="what is 2+2", session_id="s1"
    )
    system_contents = [m["content"] for m in messages if m["role"] == "system"]
    assert not any("## Skills" in c for c in system_contents)


async def test_session_start_injects_top_skills_by_health(db):
    db.skills.create(Skill(
        id="skill-a",
        name="skill-a",
        content="content A",
        authored_by="human",
        scope=["a1"],
    ))
    db.skills.create(Skill(
        id="skill-b",
        name="skill-b",
        content="content B",
        authored_by="human",
        scope=["a1"],
    ))
    mw = AgentDBMiddleware(db=db, agent_id="a1", capabilities=LOCAL_CAPABILITIES)
    messages, _ = await mw.prepare_turn_async(user_query="", session_id="s1")
    system_contents = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "## Skills" in system_contents


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
