import pytest
from agentdb import AgentDB
from agentdb.skills.evolution.wikiskill_adapter import WikiSkillAdapter
from agentdb.skills.skill_store import SkillLockConflict


def _adapter(tmp_path, agent_id="a1"):
    db = AgentDB(path=tmp_path / "a.sqlite")
    return WikiSkillAdapter(db, agent_id=agent_id), db


def test_list_skills_empty_initially(tmp_path):
    adapter, _ = _adapter(tmp_path)
    assert adapter.list_skills() == []


def test_list_skills_returns_names_in_agent_scope(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "How to debug.", scope=["a1"])
    db.skills_api.create_skill("testing", "Testing", "How to test.", scope=["a1"])
    db.skills_api.create_skill("other", "Other", "Not in scope.", scope=["a2"])
    names = adapter.list_skills()
    assert sorted(names) == ["debug", "testing"]


def test_skill_content_returns_content(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "How to debug.", scope=["a1"])
    assert adapter.skill_content("debug") == "How to debug."


def test_skill_content_returns_none_for_missing(tmp_path):
    adapter, _ = _adapter(tmp_path)
    assert adapter.skill_content("nonexistent") is None


def test_purpose_returns_skill_name_as_proxy(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "How to debug.", scope=["a1"])
    assert adapter.purpose("debug") == "Debugging"


def test_purpose_returns_empty_string_for_missing(tmp_path):
    adapter, _ = _adapter(tmp_path)
    assert adapter.purpose("nonexistent") == ""


def test_snapshot_captures_content_and_version(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "How to debug.", scope=["a1"])
    snap = adapter.snapshot()
    assert "debug" in snap
    assert snap["debug"]["content"] == "How to debug."
    assert snap["debug"]["version"] == "1.0.0"


def test_apply_proposal_create_adds_skill_with_origin_wikiskill(tmp_path):
    adapter, db = _adapter(tmp_path)
    result = adapter.apply_proposal({
        "action": "create",
        "name": "my-skill",
        "content": "Do the thing.",
        "scope": ["a1"],
        "validation_score": 0.91,
    })
    assert result["action"] == "create"
    fetched = db.skills_api.get_skill("my-skill")
    assert fetched is not None
    assert fetched.origin == "wikiskill"
    assert fetched.validation_score == pytest.approx(0.91)


def test_apply_proposal_patch_updates_content(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "Original.", scope=["a1"])
    result = adapter.apply_proposal({
        "action": "patch",
        "skill_name": "debug",
        "new_content": "Updated.",
        "new_version": "1.1.0",
        "validation_score": 0.85,
    })
    assert result["action"] == "patch"
    fetched = db.skills_api.get_skill("debug")
    assert fetched.content == "Updated."
    assert fetched.origin == "wikiskill"
    assert fetched.validation_score == pytest.approx(0.85)


def test_restore_archives_candidate_and_preserves_prior(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "v1 content.", scope=["a1"])
    snap = adapter.snapshot()

    adapter.apply_proposal({
        "action": "patch",
        "skill_name": "debug",
        "new_content": "v2 candidate.",
        "new_version": "1.1.0",
        "validation_score": 0.5,
    })
    adapter.restore(snap)

    fetched = db.skills_api.get_skill("debug")
    assert fetched.content == "v1 content."
    assert fetched.version == "1.0.0"


def test_restore_with_unchanged_snapshot_is_noop(tmp_path):
    # Review Focus #4: restore with snapshot matching current state must not corrupt
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "v1 content.", scope=["a1"])
    snap = adapter.snapshot()
    adapter.restore(snap)  # no proposal was applied
    fetched = db.skills_api.get_skill("debug")
    assert fetched.content == "v1 content."
    assert fetched.version == "1.0.0"


def test_restore_archives_skills_created_during_iteration(tmp_path):
    adapter, db = _adapter(tmp_path)
    snap = adapter.snapshot()  # empty — no skills yet
    adapter.apply_proposal({
        "action": "create",
        "name": "new-skill",
        "content": "Brand new.",
        "scope": ["a1"],
        "validation_score": None,
    })
    adapter.restore(snap)
    assert adapter.list_skills() == []


def test_apply_proposal_raises_on_locked_skill(tmp_path):
    # Final fix #1: apply_proposal must check is_locked before patching
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("debug", "Debugging", "v1 content.", scope=["a1"])
    db.skills.acquire_lock("debug", duration_seconds=3600)

    with pytest.raises(SkillLockConflict):
        adapter.apply_proposal({
            "action": "patch",
            "skill_name": "debug",
            "new_content": "candidate.",
            "new_version": "1.1.0",
            "validation_score": None,
        })
