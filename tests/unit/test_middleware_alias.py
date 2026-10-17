from agentdb.adapters.claude_agent_sdk import AgentDBClaudeMiddleware, AgentDBClaudeRuntime
from agentdb.adapters.middleware import AgentDBMiddleware


def test_claude_middleware_is_alias():
    assert AgentDBClaudeMiddleware is AgentDBMiddleware


def test_agentdb_claude_runtime_still_importable():
    assert AgentDBClaudeRuntime is not None


def test_middleware_accepts_capabilities_param(tmp_path):
    from agentdb import AgentDB
    from agentdb.adapters.capabilities import CLAUDE_CAPABILITIES
    from pathlib import Path
    perms = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"
    db = AgentDB(path=tmp_path / "t.db", permissions_path=str(perms))
    mw = AgentDBMiddleware(db=db, agent_id="test_agent", capabilities=CLAUDE_CAPABILITIES)
    assert mw._capabilities is CLAUDE_CAPABILITIES


def test_middleware_default_capabilities_is_local(tmp_path):
    from agentdb import AgentDB
    from agentdb.adapters.capabilities import LOCAL_CAPABILITIES
    from pathlib import Path
    perms = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"
    db = AgentDB(path=tmp_path / "t.db", permissions_path=str(perms))
    mw = AgentDBMiddleware(db=db, agent_id="test_agent")
    assert mw._capabilities is LOCAL_CAPABILITIES
