import pytest
from agentdb import AgentDB
from agentdb.skills.skill_store import SkillStore


def _store(tmp_path):
    return SkillStore(db_path=tmp_path / "skills.sqlite")


def test_acquire_lock_returns_true_when_free(tmp_path):
    assert _store(tmp_path).acquire_lock("skill-1", duration_seconds=60) is True


def test_acquire_lock_returns_false_when_held(tmp_path):
    store = _store(tmp_path)
    store.acquire_lock("skill-1", duration_seconds=60)
    assert store.acquire_lock("skill-1", duration_seconds=60) is False


def test_release_lock_allows_reacquire(tmp_path):
    store = _store(tmp_path)
    store.acquire_lock("skill-1", duration_seconds=60)
    store.release_lock("skill-1")
    assert store.acquire_lock("skill-1", duration_seconds=60) is True


def test_expired_lock_is_not_locked(tmp_path):
    # Review Focus #5: acquire with duration=0 expires immediately
    store = _store(tmp_path)
    store.acquire_lock("skill-1", duration_seconds=0)
    assert store.is_locked("skill-1") is False


def test_expired_lock_can_be_reacquired(tmp_path):
    store = _store(tmp_path)
    store.acquire_lock("skill-1", duration_seconds=0)
    assert store.acquire_lock("skill-1", duration_seconds=60) is True


def test_is_locked_false_when_no_lock(tmp_path):
    assert _store(tmp_path).is_locked("skill-1") is False


def test_has_version_true_when_version_exists(tmp_path):
    from agentdb.core.models import Skill
    store = _store(tmp_path)
    store.create(Skill(id="s1", name="S1", content="c", authored_by="human", scope=["a1"]))
    assert store.has_version("s1", "1.0.0") is True


def test_has_version_false_when_version_missing(tmp_path):
    from agentdb.core.models import Skill
    store = _store(tmp_path)
    store.create(Skill(id="s1", name="S1", content="c", authored_by="human", scope=["a1"]))
    assert store.has_version("s1", "9.9.9") is False


def test_manager_acquire_and_release(tmp_path):
    from agentdb.skills.evolution.manager import SkillEvolutionManager
    db = AgentDB(path=tmp_path / "a.sqlite")
    mgr = SkillEvolutionManager(db, agent_id="a1")
    assert mgr.acquire_lock("s1") is True
    assert mgr.is_locked("s1") is True
    mgr.release_lock("s1")
    assert mgr.is_locked("s1") is False


def test_manager_double_acquire_returns_false(tmp_path):
    from agentdb.skills.evolution.manager import SkillEvolutionManager
    db = AgentDB(path=tmp_path / "a.sqlite")
    mgr = SkillEvolutionManager(db, agent_id="a1")
    mgr.acquire_lock("s1")
    assert mgr.acquire_lock("s1") is False
