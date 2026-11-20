"""Deterministic, offline adversarial retrieval fixtures.

These helpers build controlled synthetic corpora with explicit record IDs,
provenance classes, eligibility, relevance grades, token sizes, and frozen
timestamps. Nothing here calls a network, an LLM, or a real embedding model.

Design rules (see docs/spec/BOUNDARY_CLEANUP.md §5–§8):
- Every record is written with an explicit ``id`` so tests can assert on IDs,
  not on insertion order or generated UUIDs.
- Timestamps are frozen so recency scores are reproducible run-to-run.
- Similarity scores are injected as fixed vectors (no model download).
- A single :class:`FrozenClock` is monkeypatched into the client and store so
  that ``_utcnow()`` returns a stable instant for the whole test.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest


# A fixed instant used across the suite: 2026-11-20T00:00:00Z.
FROZEN_NOW = datetime(2026, 11, 20, 0, 0, 0, tzinfo=timezone.utc)


class FrozenClock:
    """A deterministic clock.

    ``now`` is the instant returned by ``_utcnow()`` replacements.
    """

    def __init__(self, now: datetime = FROZEN_NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


@contextmanager
def frozen_clock(clock: Optional[FrozenClock] = None):
    """Patch every ``_utcnow`` in the retrieval/lifecycle/store code paths.

    Usage::

        with frozen_clock() as clk:
            ...  # all writes/retrieval see clk.now
            clk.advance(hours=1)

    Patching is scoped so other tests are unaffected.
    """
    clock = clock or FrozenClock()
    # Imported lazily so the fixture module does not require agentdb at import.
    import agentdb.interface.client as client_mod
    import agentdb.core.store as store_mod
    import agentdb.governance.lifecycle_manager as lc_mod

    targets = []
    for mod, name in (
        (client_mod, "_utcnow"),
        (store_mod, "_utcnow"),
        (lc_mod, "_utcnow"),
    ):
        if hasattr(mod, name):
            targets.append((mod, name, getattr(mod, name)))
            setattr(mod, name, clock)
    try:
        yield clock
    finally:
        for mod, name, original in targets:
            setattr(mod, name, original)


def make_record_spec(
    id: str,
    key: str,
    value,
    *,
    origin: str = "agent_inferred",
    agent_id: str = "a1",
    confidence: float = 0.9,
    created_at: Optional[datetime] = None,
    archived: bool = False,
    consolidated: bool = False,
    kind: str = "source",
    derived_from: Optional[list] = None,
    derived_digests: Optional[list] = None,
    working_dirs: Optional[list] = None,
    entities: Optional[list] = None,
    expiry_time: Optional[datetime] = None,
) -> dict:
    """A declarative record spec consumed by :func:`write_specs`.

    ``derived_from`` + ``derived_digests`` let a test construct a
    ``derived_canonical`` record whose lineage matches (or deliberately
    mismatches) its sources, to exercise invalidation logic.
    """
    if created_at is None:
        created_at = FROZEN_NOW - timedelta(hours=1)
    spec = dict(
        id=id,
        key=key,
        value=value,
        origin=origin,
        agent_id=agent_id,
        confidence=confidence,
        created_at=created_at,
        archived=archived,
        consolidated=consolidated,
        kind=kind,
        working_dirs=working_dirs or [],
        entities=entities or [],
        expiry_time=expiry_time,
    )
    if kind == "derived_canonical":
        from agentdb.governance.lifecycle_manager import _record_digest
        from agentdb.core.models import MemoryRecord

        sources = []
        for sid, digest in zip(derived_from or [], derived_digests or []):
            sources.append({"id": sid, "digest": digest})
        spec["source_record_ids"] = list(derived_from or [])
        spec["producer"] = "consolidation"
        spec["derivation"] = {"version": "1", "sources": sources}
    return spec


def write_specs(db, specs: list[dict]) -> None:
    """Write record specs directly through the store, bypassing inference.

    Uses ``MemoryStore.write`` so IDs, timestamps, archive flags, and lineage
    are preserved exactly. Tests must call this inside :func:`frozen_clock`.
    """
    from agentdb.core.models import MemoryRecord

    for spec in specs:
        rec = MemoryRecord(
            id=spec["id"],
            key=spec["key"],
            value=spec["value"],
            origin=spec["origin"],
            trust_level=spec.get("trust_level", "agent"),
            agent_id=spec["agent_id"],
            confidence=spec["confidence"],
            created_at=spec["created_at"],
            archived=spec["archived"],
            consolidated=spec["consolidated"],
            kind=spec["kind"],
            producer=spec.get("producer"),
            derivation=spec.get("derivation", {}),
            source_record_ids=spec.get("source_record_ids", []),
            working_dirs=spec.get("working_dirs", []),
            entities=spec.get("entities", []),
            expiry_time=spec.get("expiry_time"),
        )
        db._store.write(rec)


def digest_of(value, *, key: str, origin: str = "agent_inferred") -> str:
    """Compute the lineage digest for a would-be source (matches _record_digest)."""
    from agentdb.governance.lifecycle_manager import _record_digest
    from agentdb.core.models import MemoryRecord
    rec = MemoryRecord(
        key=key, value=value, origin=origin, trust_level="agent",
        agent_id="a1", created_at=FROZEN_NOW,
    )
    return _record_digest(rec)


@pytest.fixture
def frozen_now():
    """Fixture: run test body under a frozen clock at FROZEN_NOW."""
    with frozen_clock() as clk:
        yield clk
