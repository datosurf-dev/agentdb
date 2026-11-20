"""
db.gateway — governed event dispatch and conflict queue.

Gateway is the single surface for:
  - Reconciliation event subscribe/dispatch (previously scattered on AgentDB)
  - Conflict queue (read-only view into conflict/ memory records)

Skill proposal/approval workflows have been removed from AgentDB; skill
management is owned by an independent package.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from ..interface.client import AgentDB


class Gateway:
    """
    Unified governance surface attached to an AgentDB instance as `db.gateway`.

    No new tables are created.  Conflicts are memory records with key prefix
    ``conflict/`` (written by conflict detection; this surface exposes the
    read-only view).
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

    def conflict_queue(self, resolved: bool = False) -> list:
        """
        Return conflict records from the conflicts table.

        Args:
            resolved: if True, return resolved conflicts; defaults to False (pending only).
        """
        return self._db._store.list_conflicts(resolved=resolved)


from .adapter import GatewayAdapter  # noqa: E402 — keep at bottom to avoid circular import

__all__ = ["Gateway", "GatewayAdapter"]
