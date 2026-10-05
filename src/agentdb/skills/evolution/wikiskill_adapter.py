from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..skill_store import SkillLockConflict

if TYPE_CHECKING:
    from ...interface.client import AgentDB
    from ...core.models import Skill


class WikiSkillAdapter:
    """Implements WikiSkill's SkillsLayer protocol backed by agentdb SkillStore.

    Pass an instance of this class as the `skills` argument to WikiSkill's
    EvolutionOrchestrator in place of the filesystem-backed SkillsLayer.
    """

    def __init__(self, db: "AgentDB", agent_id: str) -> None:
        self._db = db
        self._agent_id = agent_id

    def list_skills(self) -> list[str]:
        """Return IDs of all non-archived skills in this agent's scope."""
        skills = self._db.skills.get_for_agent(self._agent_id)
        return [s.id for s in skills]

    def skill_content(self, name: str) -> Optional[str]:
        skill = self._db.skills_api.get_skill(name)
        return skill.content if skill is not None else None

    def purpose(self, name: str) -> str:
        """Return skill's name as a purpose proxy; empty string if not found."""
        skill = self._db.skills_api.get_skill(name)
        return skill.name if skill is not None else ""

    def active_skills_context(self) -> str:
        skills = self._db.skills.get_for_agent(self._agent_id)
        parts = [f"## {s.id}\n{s.content}" for s in skills]
        return "\n\n".join(parts)

    def snapshot(self) -> dict[str, dict]:
        """Capture {skill_id: {content, version}} for the current active state."""
        skills = self._db.skills.get_for_agent(self._agent_id)
        return {s.id: {"content": s.content, "version": s.version} for s in skills}

    def apply_proposal(self, proposal: dict) -> dict:
        """Apply a WikiSkill proposal to agentdb.

        proposal keys:
          action="create": name, content, scope, validation_score
          action="patch":  skill_name, new_content, new_version, validation_score
        """
        action = proposal["action"]
        vs = proposal.get("validation_score")

        if action == "create":
            name = proposal["name"]
            skill = self._db.skills_api.create_skill(
                skill_id=name,
                name=name,
                content=proposal["content"],
                scope=proposal.get("scope", [self._agent_id]),
                origin="wikiskill",
                validation_score=vs,
                metadata={"source": "wikiskill"},
            )
            return {"skill": skill, "action": "create", "diff": None}

        if action == "patch":
            skill_name = proposal["skill_name"]
            if self._db.skills.is_locked(skill_name):
                raise SkillLockConflict(
                    f"Skill '{skill_name}' is held by an advisory lock; concurrent modification prevented."
                )
            skill = self._db.skills_api.update_skill_version(
                skill_id=skill_name,
                content=proposal["new_content"],
                new_version=proposal["new_version"],
                origin="wikiskill",
                validation_score=vs,
                metadata={"source": "wikiskill"},
            )
            return {"skill": skill, "action": "patch", "diff": None}

        raise ValueError(f"Unknown proposal action: {action!r}")

    def restore(self, snap: dict[str, dict]) -> None:
        """Roll back the iteration by archiving candidate versions.

        For each active skill NOT in snap: archive it (created during iteration).
        For each skill in snap: if current version differs, archive current.
        """
        current_names = set(self.list_skills())

        # Archive skills created during the iteration (not in snapshot)
        for name in current_names:
            if name not in snap:
                skill = self._db.skills_api.get_skill(name)
                if skill is not None:
                    self._db.skills_api.archive_skill(name, skill.version)

        # Restore changed skills to their prior version
        for name, prior in snap.items():
            skill = self._db.skills_api.get_skill(name)
            if skill is None:
                continue
            if skill.version != prior["version"]:
                self._db.skills_api.archive_skill(name, skill.version)
