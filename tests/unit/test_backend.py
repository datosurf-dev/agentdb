import pytest
from pathlib import Path
from agentdb import AgentDB
from agentdb.backend import AgentDBBackend
from agentdb.governance.permission_router import PermissionDenied

PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"

@pytest.fixture
def backend(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    return AgentDBBackend(agent_id="research_agent", trust_zone="agent_inferred", db=db)


def test_write_and_read(backend):
    backend.write("memory/working/task1", "context about task")
    assert backend.read("memory/working/task1") == "context about task"


def test_read_missing_returns_none(backend):
    assert backend.read("memory/working/nonexistent") is None


def test_delete_removes_record(backend):
    backend.write("memory/working/to_delete", "value")
    backend.delete("memory/working/to_delete")
    assert backend.read("memory/working/to_delete") is None


def test_delete_appends_to_audit_log(backend):
    backend.write("memory/working/audit_delete", "value")
    backend.delete("memory/working/audit_delete")
    entries = backend.db.audit_log.replay(from_sequence=0)
    assert any(entry.event_type == "memory_delete" for entry in entries)


def test_delete_denied_for_key_outside_permission(backend):
    with pytest.raises(PermissionDenied):
        backend.delete("skills/blocked")


def test_delete_denied_appends_permission_event(backend):
    with pytest.raises(PermissionDenied):
        backend.delete("skills/blocked")
    entries = backend.db.audit_log.replay(from_sequence=0)
    assert any(entry.event_type == "permission_denied" for entry in entries)


def test_delete_denied_leaves_record_intact(tmp_path):
    perms = tmp_path / "permissions.allow.skills.yaml"
    perms.write_text(
        "\n".join(
            [
                "default_policy: deny",
                "human_zone_prefixes:",
                "  - \"skills/\"",
                "agents:",
                "  research_agent:",
                "    can_write:",
                "      - \"skills/\"",
                "    can_read:",
                "      - \"skills/\"",
            ]
        )
    )
    db = AgentDB(path=tmp_path / "test.db", permissions_path=perms)
    human_backend = AgentDBBackend(agent_id="research_agent", trust_zone="human_approved", db=db)
    human_backend.write("skills/python-style", "use type hints")

    agent_backend = AgentDBBackend(agent_id="research_agent", trust_zone="agent_inferred", db=db)
    with pytest.raises(PermissionDenied):
        agent_backend.delete("skills/python-style")
    assert human_backend.read("skills/python-style") == "use type hints"


def test_proposal_prefix_is_writable_without_explicit_grant(tmp_path):
    """
    proposal/ is framework-managed (the gateway's own proposal workflow),
    not agent business data — it should be writable by default even for an
    agent with no explicit can_write grant for it. coder_agent in the shared
    fixture has no proposal/ entry, so this proves the default, not a grant.
    """
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    backend = AgentDBBackend(agent_id="coder_agent", trust_zone="agent_inferred", db=db)
    backend.write("proposal/some-id", {"name": "x"})
    assert backend.read("proposal/some-id") == {"name": "x"}


def test_cannot_write_can_still_deny_proposal_prefix(tmp_path):
    """Framework-managed defaults are an escape hatch, not an unconditional grant."""
    perms = tmp_path / "permissions.deny.proposal.yaml"
    perms.write_text(
        "\n".join(
            [
                "default_policy: deny",
                "agents:",
                "  coder_agent:",
                "    can_write:",
                "      - \"memory/working/\"",
                "    cannot_write:",
                "      - \"proposal/\"",
            ]
        )
    )
    db = AgentDB(path=tmp_path / "test.db", permissions_path=perms)
    backend = AgentDBBackend(agent_id="coder_agent", trust_zone="agent_inferred", db=db)
    with pytest.raises(PermissionDenied):
        backend.write("proposal/some-id", {"name": "x"})


def test_list_returns_matching_paths(backend):
    backend.write("memory/working/a", "v1")
    backend.write("memory/working/b", "v2")
    backend.write("memory/episodic/c", "v3")
    paths = backend.list("memory/working/")
    assert "memory/working/a" in paths
    assert "memory/working/b" in paths
    assert "memory/episodic/c" not in paths


def test_write_to_human_zone_denied_for_agent_trust(backend):
    with pytest.raises(PermissionDenied):
        backend.write("skills/my-skill", "content")


def test_human_trust_zone_backend_can_write_skills(tmp_path):
    perms = tmp_path / "permissions.allow.skills.yaml"
    perms.write_text(
        "\n".join(
            [
                "default_policy: deny",
                "human_zone_prefixes:",
                "  - \"skills/\"",
                "agents:",
                "  research_agent:",
                "    can_write:",
                "      - \"skills/\"",
                "    can_read:",
                "      - \"skills/\"",
            ]
        )
    )
    db = AgentDB(path=tmp_path / "test.db", permissions_path=perms)
    human_backend = AgentDBBackend(agent_id="research_agent", trust_zone="human_approved", db=db)
    human_backend.write("skills/python-style", "use type hints")
    assert human_backend.read("skills/python-style") == "use type hints"


def test_write_appends_to_audit_log(backend):
    backend.write("memory/working/audit_check", "value")
    entries = backend.db.audit_log.replay(from_sequence=0)
    assert any(entry.event_type == "memory_write" for entry in entries)


def test_write_denied_appends_permission_event(backend):
    with pytest.raises(PermissionDenied):
        backend.write("skills/blocked", "x")
    entries = backend.db.audit_log.replay(from_sequence=0)
    assert any(entry.event_type == "permission_denied" for entry in entries)


def test_grep_returns_keys_matching_value(backend):
    backend.write("memory/working/a", "the user likes markdown")
    backend.write("memory/working/b", "the user prefers bullet points")
    backend.write("memory/working/c", "unrelated context")
    keys = backend.grep("markdown", prefix="memory/working/")
    assert "memory/working/a" in keys
    assert "memory/working/b" not in keys
    assert "memory/working/c" not in keys

def test_grep_empty_prefix_scans_all(backend):
    backend.write("memory/working/x", "needle in a haystack")
    backend.write("memory/episodic/y", "needle found here too")
    backend.write("memory/working/z", "no match here")
    keys = backend.grep("needle")
    assert "memory/working/x" in keys
    assert "memory/episodic/y" in keys
    assert "memory/working/z" not in keys

def test_glob_matches_fnmatch_patterns(backend):
    backend.write("memory/working/task_1", "v1")
    backend.write("memory/working/task_2", "v2")
    backend.write("memory/episodic/task_3", "v3")
    keys = backend.glob("memory/working/task_*")
    assert "memory/working/task_1" in keys
    assert "memory/working/task_2" in keys
    assert "memory/episodic/task_3" not in keys

def test_glob_no_match_returns_empty(backend):
    assert backend.glob("memory/working/nonexistent_*") == []
