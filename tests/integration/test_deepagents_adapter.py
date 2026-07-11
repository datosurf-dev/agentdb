"""
Tests the AgentDBDeepAgentsBackend against the deepagents BackendProtocol.
Does NOT require deepagents installed — tests the interface contract directly.
The BackendProtocol requires: ls, read, grep, glob, write, edit.
"""
import pytest
from pathlib import Path
from agentdb import AgentDB
from agentdb.adapters.deepagents import (
    AgentDBDeepAgentsBackend,
    CONTEXT_ASSEMBLY_ORDER,
    get_system_prompt_instruction,
    make_agentdb_backend,
)

PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"

@pytest.fixture
def db(tmp_path):
    return AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

@pytest.fixture
def backend(db):
    return AgentDBDeepAgentsBackend(db=db, agent_id="research_agent",
                                    trust_zone="agent_inferred")

def test_write_and_read(backend):
    backend.write("memory/working/pref", "markdown")
    assert backend.read("memory/working/pref") == "markdown"

def test_read_missing_returns_none(backend):
    assert backend.read("memory/working/nonexistent") is None

def test_ls_returns_keys_under_prefix(backend):
    backend.write("memory/working/a", "v1")
    backend.write("memory/working/b", "v2")
    backend.write("memory/episodic/c", "v3")
    result = backend.ls("memory/working/")
    assert "memory/working/a" in result
    assert "memory/working/b" in result
    assert "memory/episodic/c" not in result

def test_grep_returns_keys_matching_value(backend):
    backend.write("memory/working/x", "the user likes markdown format")
    backend.write("memory/working/y", "unrelated content")
    result = backend.grep("markdown", path="memory/working/")
    assert "memory/working/x" in result
    assert "memory/working/y" not in result

def test_glob_matches_fnmatch_pattern(backend):
    backend.write("memory/working/task_1", "v1")
    backend.write("memory/working/task_2", "v2")
    backend.write("memory/episodic/task_3", "v3")
    result = backend.glob("memory/working/task_*")
    assert "memory/working/task_1" in result
    assert "memory/working/task_2" in result
    assert "memory/episodic/task_3" not in result

def test_edit_updates_value_in_place(backend):
    backend.write("memory/working/note", "the user likes markdown and bullet points")
    backend.edit("memory/working/note", "markdown", "JSON")
    assert backend.read("memory/working/note") == "the user likes JSON and bullet points"

def test_edit_raises_if_old_not_found(backend):
    backend.write("memory/working/note", "some content")
    with pytest.raises(ValueError, match="not found in content"):
        backend.edit("memory/working/note", "nonexistent_string", "replacement")

def test_edit_raises_if_key_missing(backend):
    with pytest.raises(KeyError):
        backend.edit("memory/working/doesnt_exist", "old", "new")

def test_write_to_human_zone_denied(backend):
    from agentdb.governance.permission_router import PermissionDenied
    with pytest.raises(PermissionDenied):
        backend.write("skills/my-skill", "content")

def test_make_agentdb_backend_factory(db):
    b = make_agentdb_backend(db, "research_agent", "agent_inferred")
    b.write("memory/working/factory_test", "value")
    assert b.read("memory/working/factory_test") == "value"


def test_context_assembly_order_constant_exists():
    assert isinstance(CONTEXT_ASSEMBLY_ORDER, list)
    assert len(CONTEXT_ASSEMBLY_ORDER) >= 5
    assert CONTEXT_ASSEMBLY_ORDER[0].startswith("1)")


def test_system_prompt_instruction_mentions_retrieve_tool():
    instruction = get_system_prompt_instruction("memory_retrieve")
    assert "memory_retrieve" in instruction
    assert "conflict flags" in instruction
    assert "override instructions" in instruction
