"""
Phase 1 conflict detector — temporal and authority conflicts only.

Temporal:  same key + same origin → two records with equal authority, neither
           supersedes the other.
Authority: agent_inferred / provider_ingested tries to overwrite human_approved
           → the agent has less authority than the existing human-approved value.

Human-approved overwriting anything lower is NOT a conflict — it supersedes cleanly.
"""
from __future__ import annotations

from agentdb.core.models import ConflictRecord, MemoryRecord

# Origins that are considered "agent-tier" (lower authority than human_approved)
_AGENT_TIER_ORIGINS = {"agent_inferred", "provider_ingested"}


class ConflictDetector:
    """Checks whether a new write conflicts with an existing record."""

    def check(
        self,
        new_record: MemoryRecord,
        existing: MemoryRecord,
    ) -> ConflictRecord | None:
        """
        Returns a ConflictRecord if new_record conflicts with existing, else None.

        Rules:
        - Different keys → no conflict (caller should not invoke in this case).
        - Same key + same origin → temporal conflict.
        - New is agent_inferred/provider_ingested AND existing is human_approved
          → authority conflict.
        - New is human_approved, existing is anything lower → no conflict (supersedes).
        """
        if new_record.key != existing.key:
            return None

        new_origin = str(new_record.origin)
        existing_origin = str(existing.origin)

        # Authority conflict: agent/provider overwriting human_approved
        if existing_origin == "human_approved" and new_origin in _AGENT_TIER_ORIGINS:
            return ConflictRecord(
                record_a_id=existing.id,
                record_b_id=new_record.id,
                conflict_type="authority",
            )

        # Human supersedes lower-trust: no conflict (only when existing is NOT also human_approved)
        if new_origin == "human_approved" and existing_origin in _AGENT_TIER_ORIGINS:
            return None

        # Same origin → temporal conflict (covers human_approved vs human_approved too)
        if new_origin == existing_origin:
            return ConflictRecord(
                record_a_id=existing.id,
                record_b_id=new_record.id,
                conflict_type="temporal",
            )

        return None

    @staticmethod
    def check_semantic_batch(
        new_record: "MemoryRecord",
        candidates: list,
        embedding_store,
        threshold: float = 0.92,
    ) -> list:
        """
        Returns ConflictRecords for any candidate with cosine(new, candidate) >= threshold.
        Caps at 50 candidates. Skips if embedding_store is None or new_record has no embedding.
        """
        if embedding_store is None:
            return []

        new_vec = embedding_store.get("memories", new_record.id)
        if new_vec is None:
            return []

        from ..embeddings.provider import _cosine

        capped = [c for c in candidates if c.id != new_record.id][:50]
        if not capped:
            return []

        candidate_ids = [c.id for c in capped]
        stored = embedding_store.get_batch("memories", candidate_ids)

        conflicts = []
        for candidate in capped:
            c_vec = stored.get(candidate.id)
            if c_vec is None:
                continue
            if _cosine(new_vec, c_vec) >= threshold:
                conflicts.append(ConflictRecord(
                    record_a_id=candidate.id,
                    record_b_id=new_record.id,
                    conflict_type="semantic",
                ))
        return conflicts
