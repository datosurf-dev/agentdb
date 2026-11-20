from pathlib import Path

from agentdb import AgentDB
from agentdb.core.models import Skill


PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def test_retrieve_does_not_include_skills(tmp_path):
    """Skills are no longer injected via retrieve() — RetrievalResult has no skill fields."""
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    db.skills.create(
        Skill(
            id="style-skill",
            name="style",
            content="respond in concise bullets",
            authored_by="human",
            scope=["research_agent"],
        )
    )
    db.write(
        key="memory/working/context",
        value="answer style is relevant",
        origin="agent_inferred",
        agent_id="research_agent",
        session_id="sess-skill-1",
    )

    result = db.retrieve(query="style", agent_id="research_agent", session_id="sess-skill-1")
    assert not hasattr(result, "skills")
    assert not hasattr(result, "skill_warnings")
