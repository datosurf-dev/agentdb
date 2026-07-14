"""
GatewayAdapter — base class for all AgentDB runtime adapters.

Subclasses (e.g. AgentDBClaudeMiddleware) inherit a direct reference to the
db.gateway surface so they can subscribe to triggers, dispatch events, and
interact with the proposal workflow without importing Gateway directly.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..interface.client import AgentDB
    from . import Gateway


class GatewayAdapter:
    """
    Base class that wires an adapter to db.gateway.

    Subclasses should call super().__init__(db, agent_id, profile) and may
    override prepare_turn, pre_tool_use, and finalize_turn.
    """

    def __init__(
        self,
        db: "AgentDB",
        agent_id: str,
        profile: str = "budget_optimized",
    ) -> None:
        self._db = db
        self._agent_id = agent_id
        self._profile = profile
        self.gateway: "Gateway" = db.gateway

    def prepare_turn(self, user_query: str, session_id: str) -> None:  # pragma: no cover
        pass

    def pre_tool_use(self, tool_name: str, tool_input: dict, session_id: str) -> None:  # pragma: no cover
        pass

    def finalize_turn(self, session_id: str, **kwargs) -> None:  # pragma: no cover
        pass
