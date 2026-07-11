import pytest
from datetime import datetime

from agentdb.core.models import (
    ConflictRecord,
    ExpiryPolicy,
    MemoryRecord,
    MemoryTier,
    Origin,
    Skill,
    TrustLevel,
)


def test_memory_record_defaults() -> None:
    r = MemoryRecord(
        key="prefs/format",
        value="markdown",
        origin=Origin.AGENT_INFERRED,
        trust_level=TrustLevel.AGENT,
        agent_id="assistant",
    )
    assert r.id is not None
    assert r.confidence == 1.0
    assert r.version == 1
    assert r.archived is False
    assert r.retrieval_count == 0
    assert isinstance(r.created_at, datetime)


def test_memory_record_requires_key_value_origin() -> None:
    with pytest.raises(TypeError):
        MemoryRecord()  # type: ignore[call-arg]


def test_conflict_record_defaults() -> None:
    c = ConflictRecord(record_a_id="a", record_b_id="b", conflict_type="temporal")
    assert c.id is not None
    assert c.resolved is False
    assert c.auto_resolved is False


def test_origin_enum_values() -> None:
    assert Origin.HUMAN_APPROVED == "human_approved"
    assert Origin.AGENT_INFERRED == "agent_inferred"
    assert Origin.PROVIDER_INGESTED == "provider_ingested"


def test_trust_level_ordering() -> None:
    order = [TrustLevel.SEALED, TrustLevel.HUMAN, TrustLevel.AGENT, TrustLevel.PROVIDER]
    assert len(order) == 4


def test_expiry_policy_values() -> None:
    assert ExpiryPolicy.PERMANENT == "permanent"
    assert ExpiryPolicy.EPISODIC == "episodic"
    assert ExpiryPolicy.SESSION == "session"


def test_memory_tier_values() -> None:
    assert MemoryTier.WORKING == "working"
    assert MemoryTier.EPISODIC == "episodic"
    assert MemoryTier.LONG_TERM == "long_term"
    assert MemoryTier.ORG == "org"


def test_skill_dataclass() -> None:
    s = Skill(
        id="s1",
        name="python-style",
        content="use type hints",
        authored_by="human",
        scope=["coder_agent"],
    )
    assert s.version == "1.0.0"
    assert s.archived is False
    assert s.health_score == 1.0
    assert isinstance(s.health_score_reasons, list)
