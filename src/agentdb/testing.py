"""
Test doubles for AgentDB adapters, maintained alongside the real classes so
downstream integrations don't need to hand-roll and manually keep a fake in
sync with real constructor signatures. tests/unit/test_testing_fakes.py
asserts the fakes' curated params still exist on the real adapter — any
drift is caught here, in this repo's own suite, not downstream in
production (see docs/integrations/claude-agent-sdk-gateway-guide.md for the
incident that motivated this module).
"""
from __future__ import annotations

from typing import Any, Optional


class FakeAgentDBClaudeMiddleware:
    """
    Minimal in-memory stand-in for AgentDBClaudeMiddleware, for downstream
    apps testing their OWN wiring code (e.g. "did we pass the right
    constructor args?") without needing a real AgentDB/SQLite instance or
    re-testing AgentDB's own retrieval/persistence logic.

    Deliberately does not implement prepare_turn/finalize_turn/run_turn —
    those exercise real retrieval and persistence behavior already covered
    by AgentDB's own test suite; faking them here would just be
    re-implementing (and risking diverging from) that logic. Use a real
    AgentDBClaudeMiddleware backed by a tmp-path SQLite AgentDB instance if
    you need to test turn behavior itself.
    """

    def __init__(
        self,
        db: Any,
        agent_id: str,
        profile: str = "budget_optimized",
        memory_budget_tokens: Optional[int] = None,
    ) -> None:
        self.db = db
        self.agent_id = agent_id
        self.profile = profile
        self.memory_budget_tokens = memory_budget_tokens
        self._conflicts: list = []
        self._proposals: dict[str, dict] = {}

    def pending_conflicts(self, resolved: bool = False) -> list:
        return self._conflicts

    def propose_skill(
        self,
        name: str,
        content: str,
        scope: list[str],
        entities: Optional[list[str]] = None,
        session_id: Optional[str] = None,
    ) -> str:
        proposal_id = f"fake-proposal-{len(self._proposals) + 1}"
        self._proposals[proposal_id] = {
            "proposal_id": proposal_id,
            "name": name,
            "content": content,
            "scope": scope,
            "status": "pending",
        }
        return proposal_id

    def approve_proposal(self, proposal_id: str, resolution: Optional[str] = None) -> None:
        self._proposals[proposal_id]["status"] = "approved"

    def reject_proposal(self, proposal_id: str, reason: Optional[str] = None) -> None:
        self._proposals[proposal_id]["status"] = "rejected"

    def list_proposals(self, status: str = "pending") -> list[dict]:
        return [p for p in self._proposals.values() if p["status"] == status]
