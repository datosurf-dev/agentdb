"""
Tests for agentdb.testing — maintained fakes for downstream integration
tests. The signature-sync tests are the point of this module: they make it
impossible for AgentDBClaudeMiddleware.__init__ to drift away from the fake
without agentdb's OWN test suite catching it, instead of a downstream repo
finding out via a TypeError in production (see docs/integrations/
claude-agent-sdk-gateway-guide.md for the incident that motivated this).
"""
from __future__ import annotations

import inspect

from agentdb.adapters.claude_agent_sdk import AgentDBClaudeMiddleware
from agentdb.testing import FakeAgentDBClaudeMiddleware

# The fake deliberately covers only the params relevant to gateway/proposal
# testing and constructor-wiring assertions — not conversation_started_callback,
# which only affects turn behavior (prepare_turn/finalize_turn) the fake
# doesn't implement. profile/retrieve_limit/memory_budget_chars/
# memory_budget_tokens/isolate_by_agent no longer exist on the real
# constructor at all — config is resolved entirely from db.config_for(agent_id).
CURATED_PARAMS = ("db", "agent_id")


def test_fake_curated_params_still_exist_on_real_middleware():
    real_params = inspect.signature(AgentDBClaudeMiddleware.__init__).parameters
    for name in CURATED_PARAMS:
        assert name in real_params, (
            f"AgentDBClaudeMiddleware.__init__ no longer has '{name}' — "
            f"update FakeAgentDBClaudeMiddleware and this test to match"
        )


def test_fake_accepts_the_same_curated_params():
    fake_params = inspect.signature(FakeAgentDBClaudeMiddleware.__init__).parameters
    for name in CURATED_PARAMS:
        assert name in fake_params, f"FakeAgentDBClaudeMiddleware is missing '{name}'"


def test_fake_construction_stores_curated_params():
    fake = FakeAgentDBClaudeMiddleware(db=object(), agent_id="test-agent")
    assert fake.agent_id == "test-agent"


def test_fake_pending_conflicts_defaults_empty():
    fake = FakeAgentDBClaudeMiddleware(db=object(), agent_id="a")
    assert fake.pending_conflicts() == []


def test_fake_propose_then_list_pending():
    fake = FakeAgentDBClaudeMiddleware(db=object(), agent_id="a")
    proposal_id = fake.propose_skill(name="n", content="c", scope=["a"])
    pending = fake.list_proposals(status="pending")
    assert any(p["proposal_id"] == proposal_id for p in pending)


def test_fake_approve_removes_from_pending():
    fake = FakeAgentDBClaudeMiddleware(db=object(), agent_id="a")
    proposal_id = fake.propose_skill(name="n", content="c", scope=["a"])
    fake.approve_proposal(proposal_id)
    assert not any(p["proposal_id"] == proposal_id for p in fake.list_proposals(status="pending"))


def test_fake_reject_removes_from_pending():
    fake = FakeAgentDBClaudeMiddleware(db=object(), agent_id="a")
    proposal_id = fake.propose_skill(name="n", content="c", scope=["a"])
    fake.reject_proposal(proposal_id, reason="not needed")
    assert not any(p["proposal_id"] == proposal_id for p in fake.list_proposals(status="pending"))
