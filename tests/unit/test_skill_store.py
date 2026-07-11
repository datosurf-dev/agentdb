from datetime import timedelta

import pytest

from agentdb.core.models import Skill
from agentdb.skills.skill_store import SkillStore, SkillStoreError


def _skill(**kwargs) -> Skill:
    base = Skill(
        id="s1",
        name="writing-style",
        content="use concise bullet points",
        authored_by="human",
        scope=["research_agent"],
    )
    for k, v in kwargs.items():
        setattr(base, k, v)
    return base


@pytest.fixture
def store(tmp_path):
    return SkillStore(db_path=tmp_path / "skills.db")


def test_create_and_get_skill(store):
    created = store.create(_skill())
    fetched = store.get(created.id)
    assert fetched is not None
    assert fetched.id == "s1"
    assert fetched.content == "use concise bullet points"


def test_non_human_skill_rejected(store):
    with pytest.raises(SkillStoreError):
        store.create(_skill(authored_by="agent"))


def test_update_creates_new_version(store):
    store.create(_skill(version="1.0.0"))
    updated = store.update("s1", content="use strict markdown", new_version="1.1.0")
    assert updated.version == "1.1.0"
    assert updated.parent_skill_id == "s1"


def test_get_for_agent_filters_scope(store):
    store.create(_skill(id="s1", scope=["research_agent"]))
    store.create(_skill(id="s2", name="coding-style", scope=["coder_agent"]))
    skills = store.get_for_agent("research_agent")
    assert len(skills) == 1
    assert skills[0].id == "s1"


def test_health_score_degrades_for_stale_skill(store):
    stale_skill = _skill()
    stale_skill.created_at = stale_skill.created_at - timedelta(days=45)
    store.create(stale_skill)
    result = store.get_for_agent("research_agent")
    assert len(result) == 1
    assert result[0].health_score < 0.6
    assert any("staleness" in reason for reason in result[0].health_score_reasons)
