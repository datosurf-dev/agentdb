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
    ) -> None:
        self.db = db
        self.agent_id = agent_id
        self._conflicts: list = []

    def pending_conflicts(self, resolved: bool = False) -> list:
        return self._conflicts
