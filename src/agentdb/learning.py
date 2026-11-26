"""Scoped learned knowledge — claim lifecycle and strict retrieval.

Learned guidance is knowledge distilled from scoped observations and published by
an explicit host action. It is deliberately *not* the same thing as a derived
canonical:

===================  ==========================  =========================
                     ``derived_canonical``       ``learned_claim``
===================  ==========================  =========================
Created by           ``consolidate()``           an explicit ``propose`` +
                                                 ``activate``
Injected into        generic ``retrieve()``      **only** ``retrieve_learned()``
  prompts
Scope                inferred from sources       **required**, host-supplied,
                                                 matched exactly
Revision model       one record, soft-archived   immutable revisions,
                     sources                     one active at a time
===================  ==========================  =========================

Why a separate kind in the existing table
-----------------------------------------
A learned claim reuses ``memories`` rather than a second table, because the
guarantees this feature needs already live there: the scope columns, the
permission check in ``_can_read_record``, lineage by ``derivation["sources"]``,
and the additive-migration mechanism that keeps an existing database working.
A parallel table would have to re-implement all four, and would drift from them.

The cost is that every generic path must now be told to skip the new kind. That
is handled in one place per path (``_eligibility_reason`` for retrieval,
``_is_learned_claim`` guards for writes and deletion), so the exclusion is
explicit and testable rather than implied by table separation.

Invariants (the spec's contract, enforced here)
----------------------------------------------
1. A revision is **immutable** once proposed: claim text, scope and the evidence
   snapshot never change. Lifecycle state lives in separate fields.
2. **At most one active revision** per knowledge ID; replacement is atomic.
3. Evidence is validated **at proposal and again at activation**, and re-checked
   on every strict read.
4. A claim is eligible **only** when its repository and task type match the
   caller's exactly. Scope is never inferred or broadened.
5. Source readability is enforced for the requesting caller: losing access to a
   source hides the claim.

Scope-digest note
-----------------
``_record_digest`` (lifecycle_manager) hashes ``key``, ``value`` and ``origin``
only. It therefore cannot see a source being *rescoped* with identical text — a
case the spec explicitly requires invalidating. ``_evidence_digest`` here adds
the scope fields, and learned claims use it exclusively.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .core.models import MemoryRecord

#: ``MemoryRecord.kind`` for a learned-claim revision.
LEARNED_KIND = "learned_claim"

#: Prefix for the record key. Kept under ``memory/`` so existing key-domain
#: policies and permission rules continue to apply.
KEY_PREFIX = "memory/learned/"

#: Lifecycle states stored in ``derivation["lifecycle"]["status"]``.
STATUS_PROPOSED = "proposed"
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_SUPERSEDED = "superseded"


class LearnedKnowledgeError(ValueError):
    """A claim operation was rejected. Message states why, and is safe to log."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClaimScope:
    """The exact applicability of a claim. Both fields are required.

    ``task_type`` is an **opaque host-supplied string**. AgentDB does not know
    the taxonomy, does not validate against a fixed set, and never normalises
    it beyond requiring a non-empty string. Matching is exact and
    case-sensitive: the host owns the vocabulary, so the host owns its casing.
    """

    repository_id: str
    task_type: str

    def __post_init__(self) -> None:
        for name in ("repository_id", "task_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise LearnedKnowledgeError(
                    f"{name} must be a non-empty string; got {value!r}. "
                    "Learned knowledge is always scoped — an unresolved scope "
                    "means no proposal and no retrieval."
                )

    def matches(self, record: MemoryRecord) -> bool:
        return (
            getattr(record, "repository_id", None) == self.repository_id
            and getattr(record, "task_type", None) == self.task_type
        )

    def as_dict(self) -> dict:
        return {"repository_id": self.repository_id, "task_type": self.task_type}


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def _evidence_digest(record: MemoryRecord) -> str:
    """Content + scope digest of a piece of evidence.

    Extends ``lifecycle_manager._record_digest`` with the applicability fields,
    because a source can be *rescoped with identical text* and must still
    invalidate the claim. ``key``/``value``/``origin`` alone cannot detect that.
    """
    payload = json.dumps(
        {
            "key": record.key,
            "value": record.value,
            "origin": str(record.origin),
            "repository_id": getattr(record, "repository_id", None),
            "task_type": getattr(record, "task_type", None),
            "workspace_id": getattr(record, "workspace_id", None),
            "environment": getattr(record, "environment", None),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evidence_snapshot(records: list[MemoryRecord]) -> list[dict]:
    return [
        {
            "id": r.id,
            "key": r.key,
            "digest": _evidence_digest(r),
            "repository_id": getattr(r, "repository_id", None),
            "task_type": getattr(r, "task_type", None),
        }
        for r in records
    ]


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

def _lifecycle(record: MemoryRecord) -> dict:
    return dict((getattr(record, "derivation", {}) or {}).get("lifecycle", {}))


def is_learned_claim(record: MemoryRecord) -> bool:
    """True for any learned-claim revision, whatever its lifecycle state."""
    return getattr(record, "kind", "source") == LEARNED_KIND


def is_active_claim(record: MemoryRecord) -> bool:
    return is_learned_claim(record) and _lifecycle(record).get("status") == STATUS_ACTIVE


def knowledge_id_of(record: MemoryRecord) -> Optional[str]:
    """The stable knowledge ID shared by every revision of one claim."""
    if not is_learned_claim(record):
        return None
    return (getattr(record, "derivation", {}) or {}).get("knowledge_id")


def revision_of(record: MemoryRecord) -> Optional[int]:
    if not is_learned_claim(record):
        return None
    return (getattr(record, "derivation", {}) or {}).get("revision")


def _with_lifecycle(
    record: MemoryRecord,
    *,
    status: str,
    actor: Optional[str] = None,
    at: Optional[datetime] = None,
    reason: Optional[str] = None,
) -> dict:
    """Return a new ``derivation`` with an appended lifecycle event.

    The claim, scope and evidence snapshot are untouched: lifecycle changes are
    recorded *beside* the immutable revision content, never by rewriting it.
    """
    derivation = dict(getattr(record, "derivation", {}) or {})
    history = list(derivation.get("history", []))
    event = {"status": status, "at": (at or _utcnow()).isoformat()}
    if actor:
        event["actor"] = actor
    if reason:
        event["reason"] = reason
    history.append(event)
    derivation["history"] = history
    lifecycle = dict(derivation.get("lifecycle", {}))
    lifecycle.update(event)
    derivation["lifecycle"] = lifecycle
    return derivation


# ---------------------------------------------------------------------------
# Evidence validation
# ---------------------------------------------------------------------------

def validate_evidence(
    *,
    source_ids: list[str],
    scope: ClaimScope,
    store: Any,
    can_read: Any,
    agent_id: Optional[str],
    require_same_scope: bool = True,
    required: bool = True,
) -> list[MemoryRecord]:
    """Resolve and validate the observations a claim is built from.

    Every referenced observation must exist, be readable by the caller, and —
    when ``require_same_scope`` — belong to the exact scope the claim is being
    proposed for. An observation with *missing* scope is ineligible as evidence:
    "unknown" is not "matches".

    ``required=False`` makes an empty list valid, for contradicting evidence:
    a claim must be supported by something, but nothing forces a contradiction to
    exist.

    Raises :class:`LearnedKnowledgeError` naming the first failure, so a host can
    report it to an operator without leaking record contents.
    """
    if not source_ids:
        if required:
            raise LearnedKnowledgeError(
                "a claim needs at least one supporting observation"
            )
        return []

    resolved: list[MemoryRecord] = []
    for source_id in source_ids:
        record = store.get_by_id(source_id)
        if record is None:
            raise LearnedKnowledgeError(f"evidence {source_id!r} does not exist")
        if getattr(record, "archived", False):
            raise LearnedKnowledgeError(f"evidence {source_id!r} is archived")
        if not can_read(record, agent_id):
            # Deliberately does not distinguish "unreadable" from "missing" in
            # the caller-facing wording: either way the caller may not use it.
            raise LearnedKnowledgeError(
                f"evidence {source_id!r} is not readable by this caller"
            )
        if require_same_scope and not scope.matches(record):
            raise LearnedKnowledgeError(
                f"evidence {source_id!r} is scoped to "
                f"repository_id={getattr(record, 'repository_id', None)!r}, "
                f"task_type={getattr(record, 'task_type', None)!r}; the claim is "
                f"{scope.repository_id!r}/{scope.task_type!r}"
            )
        resolved.append(record)
    return resolved


def _live_records_for_key(store: Any, key: str) -> list:
    """Every unarchived record stored under ``key``.

    A repeat write of an existing key does not update in place: ``AgentDB.write``
    scores a conflict and stores a **new** record, leaving the original live. So a
    key can have several live records, and the newest one is the current logical
    observation.
    """
    keyed = [r for r in store.list_all() if r.key == key and not getattr(r, "archived", False)]
    # created_at is the write time; id breaks ties deterministically.
    keyed.sort(key=lambda r: (r.created_at, r.id))
    return keyed


def evidence_invalidation_reason(
    record: MemoryRecord,
    *,
    store: Any,
) -> Optional[str]:
    """Why an active revision's evidence no longer holds, or ``None``.

    Evidence is **pinned to source IDs**, so an unrelated record that merely
    shares a key cannot withdraw a claim. Invalidation fires only when the pinned
    observation itself is genuinely contested or gone:

    * ``evidence_missing``  — the pinned record was archived or deleted;
    * ``evidence_revised``  — the pinned record's digest changed (content or
      scope, including same-text rescoping);
    * ``evidence_conflicted`` — a newer live record now shares the pinned
      record's key, i.e. a genuine conflict contests the same logical
      observation.

    Resolving the conflict, deleting the sibling, or restoring the original
    content clears nothing by itself: the revision must be explicitly revalidated
    and reactivated, because this function only reports.
    """
    sources = (getattr(record, "derivation", {}) or {}).get("evidence", {}).get("supporting", [])
    for src in sources:
        pinned = store.get_by_id(src.get("id"))
        if pinned is None or getattr(pinned, "archived", False):
            return "evidence_missing"
        if _evidence_digest(pinned) != src.get("digest"):
            return "evidence_revised"
        # A newer live record on the same key is a contested observation.
        live = _live_records_for_key(store, pinned.key)
        if live and live[-1].id != pinned.id:
            return "evidence_conflicted"
    return None


def build_proposal(
    *,
    knowledge_id: Optional[str],
    revision: int,
    claim: str,
    scope: ClaimScope,
    supporting: list[MemoryRecord],
    contradicting: list[MemoryRecord],
    agent_id: str,
    actor: Optional[str] = None,
    fingerprint: Optional[str] = None,
) -> MemoryRecord:
    """Build an immutable proposal revision (not yet retrievable)."""
    if not isinstance(claim, str) or not claim.strip():
        raise LearnedKnowledgeError("claim text must be a non-empty string")

    kid = knowledge_id or str(uuid.uuid4())
    derivation = {
        "knowledge_id": kid,
        "revision": revision,
        # Content identity of this revision, for idempotent proposals.
        "fingerprint": fingerprint,
        "scope": scope.as_dict(),
        "evidence": {
            "supporting": _evidence_snapshot(supporting),
            "contradicting": _evidence_snapshot(contradicting),
        },
        "lifecycle": {
            "status": STATUS_PROPOSED,
            "at": _utcnow().isoformat(),
            **({"actor": actor} if actor else {}),
        },
        "history": [
            {
                "status": STATUS_PROPOSED,
                "at": _utcnow().isoformat(),
                **({"actor": actor} if actor else {}),
            }
        ],
    }

    return MemoryRecord(
        key=f"{KEY_PREFIX}{kid}/r{revision}",
        value=claim,
        # A claim is distilled by an agent from observations; it cannot acquire
        # human authority implicitly. Verified human approval is a separate,
        # explicit host action (contract P-01).
        origin="agent_inferred",
        trust_level="agent",
        agent_id=agent_id,
        confidence=1.0,
        kind=LEARNED_KIND,
        producer="learned_knowledge",
        derivation=derivation,
        repository_id=scope.repository_id,
        task_type=scope.task_type,
    )
