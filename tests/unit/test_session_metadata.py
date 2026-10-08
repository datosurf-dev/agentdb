import pytest
from agentdb import AgentDB
from agentdb.interface.client import _extract_repo, _classify_request_type


def _db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


# ── Helper unit tests ────────────────────────────────────────────────────────

def test_extract_repo_from_path():
    assert _extract_repo(["/Users/x/agentdb"]) == "agentdb"
    assert _extract_repo(["/Users/x/dbt/models"]) == "models"
    assert _extract_repo([]) is None
    assert _extract_repo(None) is None


def test_classify_request_type_rules():
    assert _classify_request_type("review this PR") == "code_review"
    assert _classify_request_type("implement the new feature") == "code_generation"
    assert _classify_request_type("debug the error traceback") == "debugging"
    assert _classify_request_type("write documentation for this") == "documentation"
    assert _classify_request_type("research options for caching") == "research"
    assert _classify_request_type("hello") == "general"


# ── Session profile seeding ──────────────────────────────────────────────────

def test_session_profile_seeded_at_conversation_started(tmp_path):
    db = _db(tmp_path)
    db.gateway.dispatch(
        "conversation_started",
        session_id="s1",
        agent_id="a1",
        opening_message="review this pull request",
        working_dirs=["/Users/x/agentdb"],
    )
    profile = db._session_profiles.get("s1")
    assert profile is not None
    assert "agentdb" in profile.repos
    assert "code_review" in profile.request_types
    assert profile.agent_id == "a1"


def test_session_profile_created_with_no_working_dirs(tmp_path):
    db = _db(tmp_path)
    db.gateway.dispatch(
        "conversation_started",
        session_id="s2",
        opening_message="implement this feature",
    )
    profile = db._session_profiles.get("s2")
    assert profile is not None
    assert "code_generation" in profile.request_types
    assert profile.repos == set()


# ── Profile expansion on write ───────────────────────────────────────────────

def test_repos_expand_on_write(tmp_path):
    db = _db(tmp_path)
    db.gateway.dispatch("conversation_started", session_id="s1", agent_id="a1",
                        opening_message="review PR", working_dirs=["/Users/x/agentdb"])
    db.write("memory/task", "val", origin="agent_inferred", agent_id="a1",
             session_id="s1", working_dirs=["/Users/x/dbt"])
    profile = db._session_profiles["s1"]
    assert "agentdb" in profile.repos
    assert "dbt" in profile.repos


def test_request_types_expand_on_write(tmp_path):
    db = _db(tmp_path)
    db.gateway.dispatch("conversation_started", session_id="s1", agent_id="a1",
                        opening_message="review PR", working_dirs=["/Users/x/agentdb"])
    db.write("memory/debug-task", "debug error traceback",
             origin="agent_inferred", agent_id="a1", session_id="s1")
    profile = db._session_profiles["s1"]
    assert "code_review" in profile.request_types
    assert "debugging" in profile.request_types


def test_write_without_session_id_does_not_create_profile(tmp_path):
    db = _db(tmp_path)
    db.write("memory/task", "val", origin="agent_inferred", agent_id="a1")
    assert db._session_profiles == {}


# ── Repo filter at retrieve ───────────────────────────────────────────────────

def test_repo_filter_excludes_other_repo_records(tmp_path):
    db = _db(tmp_path)
    db.write("memory/dbt-task", "dbt value", origin="agent_inferred",
             agent_id="a1", working_dirs=["/Users/x/dbt"])
    db.gateway.dispatch("conversation_started", session_id="s1", agent_id="a1",
                        opening_message="implement feature",
                        working_dirs=["/Users/x/agentdb"])
    result = db.retrieve("task", agent_id="a1", session_id="s1")
    keys = [r.key for r in result.records]
    assert "memory/dbt-task" not in keys


def test_repo_filter_passes_records_with_no_working_dirs(tmp_path):
    db = _db(tmp_path)
    db.write("memory/org/policy", "always true", origin="human_approved", agent_id="a1")
    db.gateway.dispatch("conversation_started", session_id="s1", agent_id="a1",
                        opening_message="implement feature",
                        working_dirs=["/Users/x/agentdb"])
    result = db.retrieve("policy", agent_id="a1", session_id="s1")
    keys = [r.key for r in result.records]
    assert "memory/org/policy" in keys


def test_repo_filter_inactive_without_session_id(tmp_path):
    db = _db(tmp_path)
    db.write("memory/dbt-task", "value", origin="agent_inferred",
             agent_id="a1", working_dirs=["/Users/x/dbt"])
    result = db.retrieve("dbt task", agent_id="a1")
    assert len(result.records) >= 1


def test_multi_repo_session_retrieves_from_both(tmp_path):
    db = _db(tmp_path)
    db.write("memory/agentdb-note", "agentdb val", origin="agent_inferred",
             agent_id="a1", working_dirs=["/Users/x/agentdb"])
    db.write("memory/dbt-note", "dbt val", origin="agent_inferred",
             agent_id="a1", working_dirs=["/Users/x/dbt"])
    db.gateway.dispatch("conversation_started", session_id="s1", agent_id="a1",
                        opening_message="implement feature",
                        working_dirs=["/Users/x/agentdb"])
    # Mid-session pivot: also write a dbt record to expand the profile
    db.write("memory/dbt-task", "new dbt work", origin="agent_inferred",
             agent_id="a1", session_id="s1", working_dirs=["/Users/x/dbt"])
    result = db.retrieve("note", agent_id="a1", session_id="s1")
    keys = [r.key for r in result.records]
    assert "memory/agentdb-note" in keys
    assert "memory/dbt-note" in keys
