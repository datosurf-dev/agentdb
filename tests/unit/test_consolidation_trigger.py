"""Regression tests for the consolidation-trigger `agent_id` dispatch (spec §1).

`AgentDBMiddleware.prepare_turn` must dispatch `conversation_started` WITH the
agent_id, so the per-agent session counter in `consolidation_state` increments.
Previously it dispatched only `session_id`, so the handler saw `agent_id=None`
and skipped `_increment_session_count` — auto-consolidation could never fire.
"""
import sqlite3
from pathlib import Path

import pytest

from agentdb import AgentDB
from agentdb.adapters.middleware import AgentDBMiddleware

PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def _session_count(db_path: Path, agent_id: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT session_count FROM consolidation_state WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        return row[0] if row else 0
    finally:
        con.close()


def test_prepare_turn_increments_session_count_for_agent(tmp_path):
    db_path = tmp_path / "t.db"
    db = AgentDB(path=db_path, permissions_path=str(PERMS))
    mw = AgentDBMiddleware(db=db, agent_id="test_agent")

    # Stay below the default auto-consolidate threshold (10) so the counter is
    # not reset by a triggered consolidation.
    for i in range(3):
        mw.prepare_turn(user_query=f"q{i}", session_id=f"s{i}")

    assert _session_count(db_path, "test_agent") == 3


def test_prepare_turn_dispatch_carries_agent_id(tmp_path):
    db = AgentDB(path=tmp_path / "t.db", permissions_path=str(PERMS))
    mw = AgentDBMiddleware(db=db, agent_id="agent-x")

    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)

    mw.gateway.subscribe("conversation_started", _capture)
    mw.prepare_turn(user_query="hi", session_id="s1")

    assert seen.get("agent_id") == "agent-x"
    assert seen.get("session_id") == "s1"
