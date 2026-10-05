from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from ...interface.client import AgentDB
    from ...core.models import Skill


class SkillOptAdapter:
    """Post-training bridge that reads SkillOpt output and writes into agentdb.

    Usage::

        adapter = SkillOptAdapter(db, agent_id="my-agent")
        # After SkillOpt training finishes:
        new_skill = adapter.push_result("my-skill-id", out_root="./skillopt-runs/my-skill")
    """

    def __init__(self, db: "AgentDB", agent_id: str) -> None:
        self._db = db
        self._agent_id = agent_id

    def push_result(
        self,
        skill_id: str,
        out_root: Union[str, Path],
    ) -> Optional["Skill"]:
        """Read SkillOpt output and write to agentdb when gate accepted a new best.

        Returns the new Skill version on success, None if no write occurred.
        """
        out_root = Path(out_root)
        runtime_path = out_root / "runtime_state.json"
        best_skill_path = out_root / "best_skill.md"

        if not runtime_path.exists() or not best_skill_path.exists():
            return None

        try:
            state = json.loads(runtime_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        gate_result = state.get("gate_result", {})
        if gate_result.get("action") != "accept_new_best":
            return None

        best_step = state.get("best_step")
        if best_step is None:
            return None

        target_version = f"skillopt_step_{best_step}"

        # Idempotency: if this version already exists, do nothing
        if self._db.skills.has_version(skill_id, target_version):
            return None

        content = best_skill_path.read_text(encoding="utf-8")
        score = gate_result.get("score")
        validation_score = float(score) if score is not None else None

        return self._db.skills_api.update_skill_version(
            skill_id=skill_id,
            content=content,
            new_version=target_version,
            origin="skillopt",
            validation_score=validation_score,
            metadata={"best_step": best_step, "source": "skillopt"},
        )

    def _run_subprocess(
        self,
        config: dict,
        init_content: str,
        out_root: Path,
    ) -> None:
        """Run SkillOpt training as a subprocess.

        Writes `init_content` to `{out_root}/skill_init.md`, then invokes
        the `skillopt` CLI. Extracted as a method so tests can monkeypatch it.
        """
        import subprocess

        out_root.mkdir(parents=True, exist_ok=True)
        init_path = out_root / "skill_init.md"
        init_path.write_text(init_content, encoding="utf-8")

        config_json = out_root / "config.json"
        config_json.write_text(json.dumps(config), encoding="utf-8")

        subprocess.run(
            [
                "skillopt", "train",
                "--config", str(config_json),
                "--skill-init", str(init_path),
                "--out-root", str(out_root),
            ],
            check=True,
        )

    def run_and_push(
        self,
        skill_id: str,
        config: dict,
        out_root: Union[str, Path],
    ) -> Optional["Skill"]:
        """Acquire lock, run SkillOpt training, push the result, release lock.

        Returns the new Skill version if accepted, None otherwise.
        Releases the lock even if training raises.
        """
        out_root = Path(out_root)
        if not self._db.skills.acquire_lock(skill_id):
            return None

        skill = self._db.skills_api.get_skill(skill_id)
        init_content = skill.content if skill is not None else ""

        try:
            self._run_subprocess(config, init_content, out_root)
            return self.push_result(skill_id, out_root)
        finally:
            self._db.skills.release_lock(skill_id)
