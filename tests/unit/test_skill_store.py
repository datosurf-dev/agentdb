from datetime import timedelta

import pytest

from agentdb.core.models import Skill
from agentdb.skills.skill_store import SkillConflict, SkillStore, SkillStoreError


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


def test_health_score_high_when_recently_retrieved_despite_old_creation(store):
    skill = _skill()
    skill.created_at = skill.created_at - timedelta(days=45)
    skill.last_retrieved = skill.created_at + timedelta(days=44)
    store.create(skill)
    result = store.get_for_agent("research_agent")
    assert len(result) == 1
    assert result[0].health_score >= 0.9


# --- search ---

def test_search_by_name(store):
    store.create(_skill(id="s-search", name="python-style", content="use type hints"))
    results = store.search("python")
    assert len(results) == 1
    assert results[0].id == "s-search"


def test_search_by_content(store):
    store.create(_skill(id="s-content", name="writing-style", content="be concise and clear"))
    results = store.search("concise")
    assert len(results) == 1
    assert results[0].id == "s-content"


def test_search_case_insensitive(store):
    store.create(_skill(id="s-case", name="Linting-rules", content="Run flake8 always"))
    results = store.search("FLAKE8")
    assert len(results) == 1


def test_search_no_match_returns_empty(store):
    store.create(_skill(id="s-nomatch", name="git-style", content="squash commits"))
    assert store.search("nonexistent_keyword_xyz") == []


def test_search_empty_query_returns_empty(store):
    store.create(_skill(id="s-empty", name="some-skill", content="some content"))
    assert store.search("") == []


def test_search_agent_id_filter(store):
    store.create(_skill(id="s-a", scope=["agent_a"], name="code-style", content="use black formatter"))
    store.create(_skill(id="s-b", scope=["agent_b"], name="code-style-b", content="use black formatter"))
    results = store.search("black", agent_id="agent_a")
    assert len(results) == 1
    assert results[0].id == "s-a"


# --- SkillConflict ---

def test_update_raises_skill_conflict_when_unresolved_versions_exist(store):
    store.create(_skill(id="s-conflict", version="1.0.0"))
    store.update("s-conflict", content="new content v2", new_version="2.0.0")
    # Second update while v2 is active (v1 not archived) raises SkillConflict
    with pytest.raises(SkillConflict):
        store.update("s-conflict", content="new content v3", new_version="3.0.0")


def test_update_allowed_after_archiving_previous_version(store):
    store.create(_skill(id="s-ok", version="1.0.0"))
    store.update("s-ok", content="v2 content", new_version="2.0.0")
    store.archive("s-ok", "2.0.0")
    # Only v1 active — update allowed
    updated = store.update("s-ok", content="v3 content", new_version="3.0.0")
    assert updated.version == "3.0.0"


# --- skill_stale 12b dispatch ---

def test_stale_crossing_callback_fires_once(store):
    """Callback fires the first time health crosses below 0.6, not on subsequent retrieves."""
    crossed_ids = []
    store.on_stale_crossing = crossed_ids.append

    stale_skill = _skill(id="s-stale-cb")
    stale_skill.created_at = stale_skill.created_at - timedelta(days=65)
    store.create(stale_skill)

    # First retrieve: computed health < 0.6, stored was 1.0 → crossing detected
    store.get_for_agent("research_agent")
    assert crossed_ids == ["s-stale-cb"]

    # Second retrieve: still < 0.6, but stored is now persisted < 0.6 → no re-fire
    store.get_for_agent("research_agent")
    assert len(crossed_ids) == 1


def test_stale_crossing_callback_not_fired_for_healthy_skill(store):
    crossed_ids = []
    store.on_stale_crossing = crossed_ids.append

    store.create(_skill(id="s-healthy"))  # just created, health = 1.0
    store.get_for_agent("research_agent")
    assert crossed_ids == []


def test_stale_crossing_no_callback_no_error(store):
    """SkillStore with default on_stale_crossing=None must not raise even on a crossing."""
    stale = _skill(id="s-nocb")
    stale.created_at = stale.created_at - timedelta(days=65)
    store.create(stale)
    store.get_for_agent("research_agent")  # must not raise
