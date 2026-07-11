from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class Origin(str, Enum):
    HUMAN_APPROVED = "human_approved"
    AGENT_INFERRED = "agent_inferred"
    PROVIDER_INGESTED = "provider_ingested"


class TrustLevel(str, Enum):
    SEALED = "sealed"
    HUMAN = "human"
    AGENT = "agent"
    PROVIDER = "provider"


class ExpiryPolicy(str, Enum):
    PERMANENT = "permanent"
    EPISODIC = "episodic"
    SESSION = "session"


class MemoryTier(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    LONG_TERM = "long_term"
    ORG = "org"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


@dataclass
class MemoryRecord:
    key: str
    value: Any
    origin: str  # Origin enum value or raw string
    trust_level: str  # TrustLevel enum value or raw string
    agent_id: str
    id: str = field(default_factory=_new_uuid)
    confidence: float = 1.0
    version: int = 1
    archived: bool = False
    retrieval_count: int = 0
    created_at: datetime = field(default_factory=_utcnow)
    entities: list = field(default_factory=list)
    last_retrieved: Optional[datetime] = None
    conflict_id: Optional[str] = None


@dataclass
class ConflictRecord:
    record_a_id: str
    record_b_id: str
    conflict_type: str
    id: str = field(default_factory=_new_uuid)
    resolved: bool = False
    auto_resolved: bool = False


@dataclass
class Skill:
    id: str
    name: str
    content: str
    authored_by: str
    scope: list
    version: str = "1.0.0"
    archived: bool = False
    health_score: float = 1.0
    health_score_reasons: list = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    last_retrieved: Optional[datetime] = None
    parent_skill_id: Optional[str] = None
