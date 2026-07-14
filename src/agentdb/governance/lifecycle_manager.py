from __future__ import annotations

import uuid
from datetime import datetime, timezone

from agentdb.core.models import Origin
from agentdb.core.store import MemoryStore
from agentdb.core.system import SystemStats


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _origin_value(origin) -> str:
    """Return the string value of an origin, whether it's an Origin enum or a raw string."""
    if isinstance(origin, Origin):
        return origin.value
    return str(origin)


class LifecycleManager:
    CONFIDENCE_DECAY_RATE: float = 0.0   # off by default; set > 0 to enable
    ARCHIVAL_THRESHOLD: float = 0.10     # archive if confidence falls below this
    STALENESS_DAYS: int = 90             # archive if not retrieved for this many days
    REINFORCE_RATE: float = 0.10         # confidence boost per reinforce call

    def __init__(
        self,
        store: MemoryStore,
        system: SystemStats,
        session_record_ids: dict | None = None,
    ) -> None:
        self._store = store
        self._system = system
        # Shared reference to AgentDB._session_record_ids so record_outcome
        # can find what was retrieved in a given session.
        self._session_record_ids: dict[str, list[str]] = (
            session_record_ids if session_record_ids is not None else {}
        )

    # ------------------------------------------------------------------
    # Sweep
    # ------------------------------------------------------------------

    def run_sweep(self) -> dict:
        """
        On-demand lifecycle sweep.

        1. If CONFIDENCE_DECAY_RATE > 0: apply decay to agent_inferred +
           provider_ingested records (human_approved is immune).
        2. Archive records whose confidence is below ARCHIVAL_THRESHOLD.
        3. Archive records not retrieved for more than STALENESS_DAYS.

        Returns {"decayed": N, "archived": N}.
        human_approved records are immune to all lifecycle operations.
        """
        now = _utcnow()
        decayed = 0
        archived_ids: set[str] = set()

        records = self._store.list_all()

        # --- Step 1: optional decay ---
        if self.CONFIDENCE_DECAY_RATE > 0:
            for record in records:
                origin = _origin_value(record.origin)
                if origin == Origin.HUMAN_APPROVED.value:
                    continue
                new_conf = max(0.0, record.confidence - self.CONFIDENCE_DECAY_RATE)
                if new_conf != record.confidence:
                    self._store.update_confidence(record.id, new_conf)
                    decayed += 1

        # Re-read after potential mutations so archival thresholds are current
        records = self._store.list_all()

        # --- Step 2: archive below threshold ---
        for record in records:
            if record.id in archived_ids:
                continue
            origin = _origin_value(record.origin)
            if origin == Origin.HUMAN_APPROVED.value:
                continue
            if record.confidence < self.ARCHIVAL_THRESHOLD:
                self._store.archive_record(record.id)
                archived_ids.add(record.id)

        # --- Step 3: archive stale records ---
        records = self._store.list_all()
        for record in records:
            if record.id in archived_ids:
                continue
            origin = _origin_value(record.origin)
            if origin == Origin.HUMAN_APPROVED.value:
                continue

            # Use last_retrieved if available, else created_at
            reference_time = record.last_retrieved or record.created_at
            # Ensure timezone-aware for comparison
            if reference_time.tzinfo is None:
                reference_time = reference_time.replace(tzinfo=timezone.utc)
            days_since = (now - reference_time).days
            if days_since >= self.STALENESS_DAYS:
                self._store.archive_record(record.id)
                archived_ids.add(record.id)

        return {"decayed": decayed, "archived": len(archived_ids)}

    # ------------------------------------------------------------------
    # Reinforce
    # ------------------------------------------------------------------

    def reinforce(self, record_id: str) -> None:
        """Boost confidence toward 1.0 and reset last_retrieved."""
        record = self._store.get_by_id(record_id)
        if record is None or record.archived:
            return
        if _origin_value(record.origin) == Origin.HUMAN_APPROVED.value:
            return  # immune

        new_conf = min(1.0, record.confidence + self.REINFORCE_RATE)
        self._store.update_confidence(record_id, new_conf)
        self._store.increment_retrieval_count(record_id)

    # ------------------------------------------------------------------
    # Record outcome (core logic, moved from AgentDB)
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        session_id: str,
        agent_id: str,
        outcome_type: str,
        outcome_value: float,
    ) -> list[str]:
        """
        Adjust confidence for records retrieved in this session.

        Rules by origin:
        - agent_inferred:    positive (+0.05 * outcome_value, cap +0.1),
                             negative (-0.02 * (1 - outcome_value), cap -0.1)
        - provider_ingested: positive (no change), negative (same as above)
        - human_approved:    never adjusted

        Clears the session record IDs after adjusting.
        Returns list of adjusted record IDs (for audit log use by the caller).
        """
        seen_ids = list(dict.fromkeys(self._session_record_ids.get(session_id, [])))
        is_positive = outcome_value >= 0.5
        adjusted_ids: list[str] = []

        for record_id in seen_ids:
            record = self._store.get_by_id(record_id)
            if record is None:
                continue

            origin = _origin_value(record.origin)
            if origin == Origin.HUMAN_APPROVED.value:
                continue

            if origin == Origin.AGENT_INFERRED.value:
                if is_positive:
                    delta = min(0.05 * outcome_value, 0.1)
                    new_conf = min(1.0, record.confidence + delta)
                else:
                    delta = min(0.02 * (1 - outcome_value), 0.1)
                    new_conf = max(0.0, record.confidence - delta)
                self._store.update_confidence(record_id, new_conf)
                adjusted_ids.append(record_id)
            elif origin == Origin.PROVIDER_INGESTED.value:
                if not is_positive:
                    delta = min(0.02 * (1 - outcome_value), 0.1)
                    new_conf = max(0.0, record.confidence - delta)
                    self._store.update_confidence(record_id, new_conf)
                    adjusted_ids.append(record_id)

        # Clear session record IDs so the next retrieve starts fresh
        if session_id in self._session_record_ids:
            self._session_record_ids[session_id] = []

        # Persist outcome row
        now = _utcnow().isoformat()
        with self._store._connect() as conn:
            conn.execute(
                "INSERT INTO outcomes "
                "(id, session_id, agent_id, outcome_type, outcome_value, linked_retrieval_sequence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), session_id, agent_id, outcome_type, outcome_value, None, now),
            )

        return adjusted_ids
