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

        # Human write supersedes anything — not a conflict
        if new_origin == "human_approved":
            return None

        # Same origin → temporal conflict
        if new_origin == existing_origin:
            return ConflictRecord(
                record_a_id=existing.id,
                record_b_id=new_record.id,
                conflict_type="temporal",
            )

        # Agent-tier trying to overwrite human-approved → authority conflict
        if new_origin in _AGENT_TIER_ORIGINS and existing_origin == "human_approved":
            return ConflictRecord(
                record_a_id=existing.id,
                record_b_id=new_record.id,
                conflict_type="authority",
            )

        return None
