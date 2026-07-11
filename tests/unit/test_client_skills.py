from datetime import timedelta
from pathlib import Path

from agentdb import AgentDB
from agentdb.core.models import Skill


PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def test_retrieve_returns_skills_and_warnings(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    stale = Skill(
        id="style-skill",
        name="style",
        content="respond in concise bullets",
        authored_by="human",
        scope=["research_agent"],
    )
    stale.created_at = stale.created_at - timedelta(days=45)
    db.skills.create(stale)

    db.write(
        key="memory/working/context",
        value="answer style is relevant",
        origin="agent_inferred",
        agent_id="research_agent",
        session_id="sess-skill-1",
    )

    result = db.retrieve(query="style", agent_id="research_agent", session_id="sess-skill-1")
    assert any(skill.id == "style-skill" for skill in result.skills)
    assert len(result.skill_warnings) == 1
    assert "style-skill" in result.skill_warnings[0]


def test_skill_retrieval_marks_last_retrieved(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.skills.create(
        Skill(
            id="ops-skill",
            name="ops",
            content="check logs before replying",
            authored_by="human",
            scope=["research_agent"],
        )
    )
    before = db.skills.get("ops-skill")
    assert before is not None
    assert before.last_retrieved is None

    db.retrieve(query="logs", agent_id="research_agent", session_id="sess-skill-2")

    after = db.skills.get("ops-skill")
    assert after is not None
    assert after.last_retrieved is not None
