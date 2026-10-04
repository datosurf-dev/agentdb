from __future__ import annotations

import fnmatch
from typing import Any, Optional

from .governance.permission_router import PermissionDenied
from .interface.client import AgentDB


class AgentDBBackend:
    """
    A simple key-value interface over AgentDB for a specific agent.

    trust_zone: "agent_inferred" or "human_approved"
    """

    def __init__(
        self,
        agent_id: str,
        trust_zone: str,
        db: AgentDB,
    ) -> None:
        self._agent_id = agent_id
        self._trust_zone = trust_zone
        self.db = db

    def write(self, key: str, value: Any) -> None:
        """Write a value. Raises PermissionDenied if not allowed."""
        # Derive origin from trust_zone
        origin = "human_approved" if self._trust_zone == "human_approved" else "agent_inferred"
        self.db.write(
            key=key,
            value=value,
            origin=origin,
            agent_id=self._agent_id,
        )

    def read(self, key: str) -> Optional[Any]:
        """Read a value by key. Returns None if not found."""
        record = self.db.read(key, agent_id=self._agent_id)
        if record is None:
            return None
        return record.value

    def delete(self, key: str) -> None:
        """Delete/archive a record by key. Raises PermissionDenied if not allowed."""
        self.db.delete(key, agent_id=self._agent_id, trust_zone=self._trust_zone)

    def list(self, prefix: str) -> list[str]:
        """List all keys that start with prefix, scoped to this agent."""
        records = self.db._store.list_by_agent(self._agent_id)
        return [r.key for r in records if r.key.startswith(prefix)]

    def grep(self, pattern: str, prefix: str = "") -> list[str]:
        """Return keys whose values contain the given string pattern, scoped to this agent."""
        records = self.db._store.list_by_agent(self._agent_id)
        results: list[str] = []
        pattern_lower = pattern.lower()
        for record in records:
            if prefix and not record.key.startswith(prefix):
                continue
            import json
            value_str = (
                json.dumps(record.value)
                if isinstance(record.value, (dict, list))
                else str(record.value)
            ).lower()
            if pattern_lower in value_str:
                results.append(record.key)
        return results

    def glob(self, pattern: str) -> list[str]:
        """Return keys matching an fnmatch glob pattern, scoped to this agent."""
        records = self.db._store.list_by_agent(self._agent_id)
        return [r.key for r in records if fnmatch.fnmatch(r.key, pattern)]
