import pytest
from agentdb import AgentDB
from agentdb.core.models import Skill
from agentdb.skills.skill_store import SkillStore


def test_skill_defaults_have_origin_human(tmp_path):
    db = AgentDB(path=tmp_path / "a.sqlite")
    skill = db.skills_api.create_skill("s1", "Skill One", "Do things.", scope=["a1"])
    assert skill.origin == "human"
    assert skill.validation_score is None
    assert skill.metadata == {}


def test_skill_origin_wikiskill_persists_round_trip(tmp_path):
    db = AgentDB(path=tmp_path / "a.sqlite")
    db.skills_api.create_skill(
        "s1", "Skill One", "Do things.", scope=["a1"],
        origin="wikiskill", validation_score=0.88, metadata={"iteration": 3},
    )
    fetched = db.skills_api.get_skill("s1")
    assert fetched.origin == "wikiskill"
    assert fetched.validation_score == pytest.approx(0.88)
    assert fetched.metadata == {"iteration": 3}


def test_update_skill_version_with_origin_skillopt(tmp_path):
    db = AgentDB(path=tmp_path / "a.sqlite")
    db.skills_api.create_skill("s1", "Skill One", "v1 content.", scope=["a1"])
    db.skills_api.update_skill_version(
        "s1", "v2 content.", "2.0.0",
        origin="skillopt", validation_score=0.95, metadata={"best_step": 12},
    )
    fetched = db.skills_api.get_skill("s1")
    assert fetched.origin == "skillopt"
    assert fetched.validation_score == pytest.approx(0.95)
    assert fetched.metadata == {"best_step": 12}


def test_health_blends_validation_score_when_present(tmp_path):
    db = AgentDB(path=tmp_path / "a.sqlite")
    db.skills_api.create_skill(
        "s1", "Skill One", "Do things.", scope=["a1"],
        validation_score=0.5,
    )
    skills = db.skills.get_for_agent("a1")
    # usage_score=1.0 (fresh), validation=0.5 → 0.4*0.5 + 0.6*1.0 = 0.80
    assert skills[0].health_score == pytest.approx(0.80, abs=0.01)
    assert "validation_blended" in skills[0].health_score_reasons


def test_health_unchanged_without_validation_score(tmp_path):
    db = AgentDB(path=tmp_path / "a.sqlite")
    db.skills_api.create_skill("s1", "Skill One", "Do things.", scope=["a1"])
    skills = db.skills.get_for_agent("a1")
    assert skills[0].health_score == pytest.approx(1.0)
    assert "validation_blended" not in skills[0].health_score_reasons


def test_validation_score_zero_is_not_null_and_blends(tmp_path):
    # Review Focus #2: validation_score=0.0 must blend (is not None), not fall through
    db = AgentDB(path=tmp_path / "a.sqlite")
    db.skills_api.create_skill(
        "s1", "S1", "content", scope=["a1"], validation_score=0.0
    )
    skills = db.skills.get_for_agent("a1")
    # 0.4*0.0 + 0.6*1.0 = 0.60, NOT 1.0
    assert skills[0].health_score == pytest.approx(0.60, abs=0.01)
    assert "validation_blended" in skills[0].health_score_reasons


def test_existing_db_reopens_without_error(tmp_path):
    # Review Focus #1 (idempotent migration): opening same DB twice does not raise
    db_path = tmp_path / "skills.sqlite"
    store1 = SkillStore(db_path=db_path)
    store2 = SkillStore(db_path=db_path)
    store2.create(Skill(id="s1", name="S1", content="c", authored_by="human", scope=["a1"]))
    assert store2.get("s1") is not None


def test_metadata_null_in_row_reads_as_empty_dict(tmp_path):
    # Review Focus #1: rows with NULL metadata (pre-migration) must not crash
    import sqlite3
    db_path = tmp_path / "skills.sqlite"
    store = SkillStore(db_path=db_path)
    store.create(Skill(id="s1", name="S1", content="c", authored_by="human", scope=["a1"]))
    # Manually corrupt the metadata column to NULL
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE skills SET metadata = NULL WHERE id = 's1'")
    conn.commit()
    conn.close()
    fetched = store.get("s1")
    assert fetched.metadata == {}
