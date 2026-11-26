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
    expiry_time: Optional[datetime] = None
    inferred: bool = False
    working_dirs: list = field(default_factory=list)
    embedding: Optional[list[float]] = field(default=None, repr=False)
    consolidated: bool = False
    source_record_ids: list = field(default_factory=list)
    session_id: Optional[str] = None
    # Distilled-knowledge lineage (ADB-06). ``kind`` distinguishes an original
    # source record from a derived canonical. ``producer`` records what created
    # the record (user/agent/provider/consolidation). ``derivation`` holds
    # per-source digests and evidence when this record is a derived canonical.
    # "source" | "derived_canonical" | "learned_claim". A learned claim is an
    # immutable revision of host-approved guidance; see agentdb/learning.py.
    kind: str = "source"
    producer: Optional[str] = None
    derivation: dict = field(default_factory=dict)
    # Verified approval evidence (OD-2). When present and well-formed this
    # upgrades an origin="human_approved" record from *asserted* to *verified*
    # human authority. Shape: {principal, mechanism, timestamp, digest}. A caller
    # cannot set this by passing an origin string alone — only the trusted-host
    # approval pathway (AgentDB.approve_authority) populates it.
    approval: dict = field(default_factory=dict)
    # Optional explicit context (CONTEXTUAL-SPEC §7). These support applicability
    # decisions across workspaces/repos/environments/tasks. All are optional;
    # missing context is NOT treated as a match (see eligibility rules).
    environment: Optional[str] = None  # e.g. "production", "staging", "dev"
    task_type: Optional[str] = None    # soft label; never authorizes/excludes alone
    # Stable, host-supplied execution context identities (FINAL-HARDENING §3).
    # Independent of each other and of directory basenames. Applicability
    # metadata only — never credentials / never grant authorization.
    workspace_id: Optional[str] = None
    repository_id: Optional[str] = None
    # Explicit temporal validity (FINAL-HARDENING §7). Distinct from created_at
    # and expiry_time; missing effective_from is NOT substituted with created_at.
    observed_at: Optional[datetime] = None
    effective_from: Optional[datetime] = None
    effective_until: Optional[datetime] = None
    # Stable identity of a derived canonical over its normalized source set +
    # scope (FINAL-HARDENING §6, OD-6). Enables atomic idempotency so concurrent
    # consolidations cannot create duplicate active canonicals.
    canonical_fingerprint: Optional[str] = None

    # -- authority helpers (OD-2) ------------------------------------------
    APPROVAL_DIGEST_VERSION = "2"

    def approval_fingerprint(self) -> str:
        """Versioned canonical fingerprint of approval-relevant content.

        Binds: record id, key, value, agent/source identity, workspace,
        repository, environment, and any approval-relevant content metadata.
        Excludes volatile fields (retrieval counters, last-access timestamps).
        FINAL-HARDENING §4.
        """
        import hashlib
        import json as _json
        payload = {
            "v": self.APPROVAL_DIGEST_VERSION,
            "id": self.id,
            "key": self.key,
            "value": self.value,
            "agent_id": self.agent_id,
            "workspace_id": self.workspace_id,
            "repository_id": self.repository_id,
            "environment": self.environment,
        }
        return hashlib.sha256(
            _json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def has_verified_approval(self) -> bool:
        """True only when this record carries a *host-verified* approval that is
        still current. Requires: origin human_approved; approved==True; the
        versioned canonical fingerprint matches the current content; and the
        approval has not been revoked. Legacy/summary approvals (no verified
        flag or a different digest version) are NOT verified. FINAL-HARDENING §4.
        """
        if str(self.origin) != "human_approved":
            return False
        ap = self.approval or {}
        if ap.get("revoked"):
            return False
        if not ap.get("verified"):
            return False
        if ap.get("version") != self.APPROVAL_DIGEST_VERSION:
            return False
        if not (ap.get("principal") and ap.get("mechanism") and ap.get("timestamp")):
            return False
        return ap.get("fingerprint") == self.approval_fingerprint()

    def authority(self) -> str:
        """Effective authority class: 'verified_human' | 'asserted_human' |
        'provider' | 'agent'. Legacy origin='human_approved' without a current
        host-verified approval is 'asserted_human' (not silently verified)."""
        origin = str(self.origin)
        if origin == "human_approved":
            return "verified_human" if self.has_verified_approval() else "asserted_human"
        if origin == "provider_ingested":
            return "provider"
        return "agent"


@dataclass
class ConflictRecord:
    record_a_id: str
    record_b_id: str
    conflict_type: str
    id: str = field(default_factory=_new_uuid)
    resolved: bool = False
    auto_resolved: bool = False


@dataclass
class SessionProfile:
    session_id: str
    agent_id: Optional[str] = None
    repos: set = field(default_factory=set)
    request_types: set = field(default_factory=set)
