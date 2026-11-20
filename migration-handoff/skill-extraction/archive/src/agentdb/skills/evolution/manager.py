from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...interface.client import AgentDB
    from .wikiskill_adapter import WikiSkillAdapter
    from .skillopt_adapter import SkillOptAdapter


class SkillEvolutionManager:
    def __init__(self, db: "AgentDB", agent_id: str) -> None:
        self._db = db
        self._agent_id = agent_id

    def acquire_lock(self, skill_id: str, duration_seconds: int = 3600) -> bool:
        return self._db.skills.acquire_lock(skill_id, duration_seconds)

    def release_lock(self, skill_id: str) -> None:
        self._db.skills.release_lock(skill_id)

    def is_locked(self, skill_id: str) -> bool:
        return self._db.skills.is_locked(skill_id)

    def wikiskill_adapter(self) -> "WikiSkillAdapter":
        from .wikiskill_adapter import WikiSkillAdapter
        return WikiSkillAdapter(self._db, self._agent_id)

    def skillopt_adapter(self) -> "SkillOptAdapter":
        from .skillopt_adapter import SkillOptAdapter
        return SkillOptAdapter(self._db, self._agent_id)
