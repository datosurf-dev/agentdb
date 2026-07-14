"""
db.gateway — governed event dispatch, conflict queue, and proposal workflow.

Gateway is the single surface for:
  - Reconciliation event subscribe/dispatch (previously scattered on AgentDB)
  - Conflict queue (read-only view into conflict/ memory records)
  - Proposal workflow: propose → approve/reject → list_proposals
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from ..interface.client import AgentDB


class Gateway:
    """
    Unified governance surface attached to an AgentDB instance as `db.gateway`.

    No new tables are created.  Proposals are stored as agent_inferred memory
    records with key prefix ``proposal/<proposal_id>``.  Conflicts are memory
    records with key prefix ``conflict/`` (written by a future conflict-detection
    task; this surface exposes the read-only view).
    """

    def __init__(self, db: "AgentDB") -> None:
        self._db = db
        # Reconciliation handlers: trigger -> [callables]
        self._handlers: dict[str, list[Callable]] = {}

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def subscribe(self, trigger: str, handler: Callable) -> None:
        """Register a handler for a named reconciliation trigger."""
        self._handlers.setdefault(trigger, []).append(handler)

    def dispatch(self, trigger: str, **kwargs) -> int:
        """Fire all handlers for *trigger*.  Returns the handler count."""
        handlers = self._handlers.get(trigger, [])
        for handler in handlers:
            handler(**kwargs)
        return len(handlers)

    # ------------------------------------------------------------------
    # Conflict queue
    # ------------------------------------------------------------------

    def conflict_queue(self, resolved: bool = False) -> list[dict]:
        """
        Return conflict records from the memories table.

        Conflict records have keys starting with ``conflict/``.  They are
        written by the conflict-detection subsystem (Task 12 / public-client
        gap); this method exposes the read-only queue view.

        Args:
            resolved: if True, include archived (resolved) conflict records;
                      defaults to False (pending only).
        """
        results: list[dict] = []
        for record in self._db._store._by_id.values():
            if not record.key.startswith("conflict/"):
                continue
            if record.archived and not resolved:
                continue
            results.append(
                {
                    "conflict_id": record.key.removeprefix("conflict/"),
                    "key": record.key,
                    "value": record.value,
                    "resolved": record.archived,
                    "created_at": record.created_at.isoformat(),
                }
            )
        return results

    # ------------------------------------------------------------------
    # Proposal workflow
    # ------------------------------------------------------------------

    def propose(
        self,
        name: str,
        content: str,
        scope: list[str],
        agent_id: str,
        entities: Optional[list[str]] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """
        Draft a skill proposal.

        Writes the draft as an agent_inferred memory record under
        ``proposal/<proposal_id>`` and returns the proposal_id.
        """
        proposal_id = str(uuid.uuid4())
        self._db.write(
            key=f"proposal/{proposal_id}",
            value={
                "proposal_id": proposal_id,
                "name": name,
                "content": content,
                "scope": scope,
                "agent_id": agent_id,
                "status": "pending",
            },
            origin="agent_inferred",
            agent_id=agent_id,
            session_id=session_id,
            entities=entities or [],
        )
        self._db.audit_log.append(
            "proposal_created",
            {"proposal_id": proposal_id, "name": name, "agent_id": agent_id},
        )
        return proposal_id

    def approve(self, proposal_id: str, resolution: Optional[str] = None) -> None:
        """
        Approve a pending proposal.

        Promotes the draft to a skill via skills_api.create_skill and archives
        the proposal memory record.
        """
        record = self._db.read(f"proposal/{proposal_id}")
        if record is None:
            raise KeyError(f"Proposal not found: {proposal_id}")

        payload = record.value
        self._db.skills_api.create_skill(
            skill_id=proposal_id,
            name=payload["name"],
            content=payload["content"],
            scope=payload["scope"],
        )

        # Mark approved in a new record then archive via delete
        self._db._store.delete_by_key(f"proposal/{proposal_id}")

        self._db.audit_log.append(
            "proposal_approved",
            {
                "proposal_id": proposal_id,
                "resolution": resolution,
                "agent_id": payload.get("agent_id"),
            },
        )

    def reject(self, proposal_id: str, reason: Optional[str] = None) -> None:
        """
        Reject a pending proposal.

        Archives the proposal memory record without touching skills_api.
        """
        record = self._db.read(f"proposal/{proposal_id}")
        if record is None:
            raise KeyError(f"Proposal not found: {proposal_id}")

        payload = record.value
        self._db._store.delete_by_key(f"proposal/{proposal_id}")

        self._db.audit_log.append(
            "proposal_rejected",
            {
                "proposal_id": proposal_id,
                "reason": reason,
                "agent_id": payload.get("agent_id"),
            },
        )

    def list_proposals(self, status: str = "pending") -> list[dict]:
        """
        Return proposals filtered by status.

        Status is encoded in the record value.  Approved and rejected proposals
        have their memory records archived; we use the audit log for those.

        For simplicity:
          - ``pending``  — active (non-archived) memory records with key prefix proposal/
          - ``approved`` — read from audit_log events of type proposal_approved
          - ``rejected`` — read from audit_log events of type proposal_rejected
        """
        if status == "pending":
            results: list[dict] = []
            for record in self._db._store._by_id.values():
                if record.key.startswith("proposal/") and not record.archived:
                    results.append(dict(record.value))
            return results

        event_type = f"proposal_{status}"  # proposal_approved / proposal_rejected
        entries = self._db.audit_log.replay()
        return [
            {
                "proposal_id": e.payload["proposal_id"],
                "agent_id": e.payload.get("agent_id"),
                "status": status,
            }
            for e in entries
            if e.event_type == event_type
        ]


from .adapter import GatewayAdapter  # noqa: E402 — keep at bottom to avoid circular import

__all__ = ["Gateway", "GatewayAdapter"]
