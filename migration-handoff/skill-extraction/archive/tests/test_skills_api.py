from datetime import timedelta
from pathlib import Path

from agentdb import AgentDB


PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def test_skills_api_create_update_get_archive(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

    created = db.skills_api.create_skill(
        skill_id="ops-style",
        name="ops-style",
        content="check logs first",
        scope=["research_agent"],
    )
    assert created.id == "ops-style"
    assert created.version == "1.0.0"

    updated = db.skills_api.update_skill_version(
        skill_id="ops-style",
        content="check logs and traces first",
        new_version="1.1.0",
    )
    assert updated.version == "1.1.0"

    latest = db.skills_api.get_skill(skill_id="ops-style")
    assert latest is not None
    assert latest.version == "1.1.0"

    db.skills_api.archive_skill(skill_id="ops-style", version="1.1.0")
    archived_latest = db.skills_api.get_skill(skill_id="ops-style")
    assert archived_latest is not None
    assert archived_latest.version == "1.0.0"


def test_skills_api_stale_and_warning_views(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    created = db.skills_api.create_skill(
        skill_id="stale-skill",
        name="stale-skill",
        content="prefer concise bullets",
        scope=["research_agent"],
    )

    stale_time = created.created_at - timedelta(days=45)
    with db.skills._connect() as conn:
        conn.execute(
            "UPDATE skills SET created_at = ?, updated_at = ? WHERE id = ? AND version = ?",
            (stale_time.isoformat(), stale_time.isoformat(), "stale-skill", "1.0.0"),
        )

    stale_skills = db.skills_api.list_stale_skills_for_agent(agent_id="research_agent")
    warnings = db.skills_api.list_skill_warnings_for_agent(agent_id="research_agent")
    assert len(stale_skills) == 1
    assert stale_skills[0].id == "stale-skill"
    assert len(warnings) == 1
    assert "stale-skill" in warnings[0]
