import json
import pytest
from agentdb import AgentDB
from agentdb.skills.evolution.skillopt_adapter import SkillOptAdapter


def _adapter(tmp_path):
    db = AgentDB(path=tmp_path / "a.sqlite")
    return SkillOptAdapter(db, agent_id="a1"), db


def _write_output(out_root, best_step, action, content, score=0.93):
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "best_skill.md").write_text(content, encoding="utf-8")
    (out_root / "runtime_state.json").write_text(
        json.dumps({"best_step": best_step, "gate_result": {"action": action, "score": score}}),
        encoding="utf-8",
    )


def test_push_result_writes_to_agentdb_on_accept_new_best(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    _write_output(out_root, best_step=5, action="accept_new_best", content="optimized.")

    result = adapter.push_result("my-skill", out_root)

    assert result is not None
    assert result.content == "optimized."
    assert result.version == "skillopt_step_5"
    assert result.origin == "skillopt"
    assert result.validation_score == pytest.approx(0.93)


def test_push_result_no_write_on_action_accept(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    _write_output(out_root, best_step=3, action="accept", content="tweaked.")

    result = adapter.push_result("my-skill", out_root)

    assert result is None
    assert db.skills_api.get_skill("my-skill").content == "original."


def test_push_result_no_write_on_action_reject(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    _write_output(out_root, best_step=3, action="reject", content="worse.")

    assert adapter.push_result("my-skill", out_root) is None


def test_push_result_idempotent_same_version_is_noop(tmp_path):
    # Review Focus #3: second push with same best_step returns None, no SkillConflict
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    _write_output(out_root, best_step=5, action="accept_new_best", content="optimized.")

    adapter.push_result("my-skill", out_root)
    result2 = adapter.push_result("my-skill", out_root)

    assert result2 is None


def test_push_result_missing_runtime_state_returns_none(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    out_root.mkdir()
    assert adapter.push_result("my-skill", out_root) is None


def test_push_result_missing_best_skill_md_returns_none(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    out_root.mkdir()
    (out_root / "runtime_state.json").write_text(
        json.dumps({"best_step": 1, "gate_result": {"action": "accept_new_best", "score": 0.9}})
    )
    assert adapter.push_result("my-skill", out_root) is None


def test_run_and_push_acquires_lock_during_subprocess(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    _write_output(out_root, best_step=7, action="accept_new_best", content="best version.")

    lock_states_during_run = []

    def fake_subprocess(config, init_content, out_root_path):
        lock_states_during_run.append(db.skills.is_locked("my-skill"))

    adapter._run_subprocess = fake_subprocess
    adapter.run_and_push("my-skill", config={}, out_root=out_root)

    assert lock_states_during_run == [True]
    assert not db.skills.is_locked("my-skill")


def test_run_and_push_releases_lock_even_on_subprocess_error(tmp_path):
    adapter, db = _adapter(tmp_path)
    db.skills_api.create_skill("my-skill", "My Skill", "original.", scope=["a1"])
    out_root = tmp_path / "run"
    out_root.mkdir()

    def failing_subprocess(config, init_content, out_root_path):
        raise RuntimeError("training crashed")

    adapter._run_subprocess = failing_subprocess
    with pytest.raises(RuntimeError):
        adapter.run_and_push("my-skill", config={}, out_root=out_root)

    assert not db.skills.is_locked("my-skill")
