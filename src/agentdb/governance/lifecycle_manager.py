from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone  # noqa: F401 — timezone used in run_sweep

from agentdb.core.models import MemoryRecord, Origin
from agentdb.core.store import MemoryStore
from agentdb.core.system import SystemStats


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _record_digest(record) -> str:
    """Stable content digest of a source record (for lineage/invalidation)."""
    payload = json.dumps(
        {"key": record.key, "value": record.value, "origin": str(record.origin)},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _key_slug(key: str) -> str:
    """Return a normalized domain slug for a record key.

    Records are only merge candidates if their keys share this slug. The slug is
    the *domain* portion of the key — the last path segment with a trailing
    qualifier token (``note``/``update``/``v2``/...) removed — so
    ``analytics-schema-note`` and ``analytics-schema-update`` share the domain
    ``analytics-schema``, while ``orders-note`` and ``billing-note`` do not.
    Word overlap alone is NOT sufficient — related-but-distinct records must not
    be merged (T05).
    """
    parts = [p for p in str(key).replace(":", "/").split("/") if p]
    if parts and parts[0] == "memory":
        parts = parts[1:]
    seg = parts[-1] if parts else str(key)
    tokens = [t for t in seg.replace(".", "-").replace("_", "-").split("-") if t]
    # Drop a trailing qualifier token if present, keeping at least 2 tokens.
    _QUALIFIERS = {"note", "notes", "update", "updates", "draft", "tmp", "temp",
                   "old", "new", "v1", "v2", "v3", "copy"}
    if len(tokens) >= 3 and tokens[-1].lower() in _QUALIFIERS:
        tokens = tokens[:-1]
    return "-".join(tokens).lower() if tokens else seg.lower()


def _origin_value(origin) -> str:
    """Return the string value of an origin, whether it's an Origin enum or a raw string."""
    if isinstance(origin, Origin):
        return origin.value
    return str(origin)


@dataclass
class ConsolidationResult:
    clusters_found: int = 0
    records_merged: int = 0
    records_archived: int = 0
    synthesized_record_ids: list = field(default_factory=list)


_LC_STOP_WORDS = frozenset({
    "with", "from", "this", "that", "have", "been", "will",
    "your", "into", "they", "them", "then", "when", "what",
})


def _lc_words(text: str) -> set:
    cleaned = str(text).replace("/", " ").replace("-", " ").replace("_", " ").lower()
    return {w for w in cleaned.split() if len(w) >= 4 and w not in _LC_STOP_WORDS}


def _lc_cluster(records, min_overlap=2):
    clusters, assigned = [], set()
    for i, rec in enumerate(records):
        if i in assigned:
            continue
        group = [rec]
        assigned.add(i)
        rw = _lc_words(rec.key)
        for j, other in enumerate(records):
            if j <= i or j in assigned:
                continue
            if len(rw & _lc_words(other.key)) >= min_overlap:
                group.append(other)
                assigned.add(j)
        if len(group) >= 2:
            clusters.append(group)
    return clusters


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
        embedding_store=None,
        confidence_decay_rate: float | None = None,
        archival_threshold: float | None = None,
        staleness_days: int | None = None,
        reinforce_rate: float | None = None,
    ) -> None:
        self._store = store
        self._system = system
        self._session_record_ids: dict[str, list[str]] = (
            session_record_ids if session_record_ids is not None else {}
        )
        self._embedding_store = embedding_store
        cls = type(self)
        self.CONFIDENCE_DECAY_RATE = confidence_decay_rate if confidence_decay_rate is not None else cls.CONFIDENCE_DECAY_RATE
        self.ARCHIVAL_THRESHOLD = archival_threshold if archival_threshold is not None else cls.ARCHIVAL_THRESHOLD
        self.STALENESS_DAYS = staleness_days if staleness_days is not None else cls.STALENESS_DAYS
        self.REINFORCE_RATE = reinforce_rate if reinforce_rate is not None else cls.REINFORCE_RATE
        self._init_consolidation_table()

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

        # --- Step 0: archive expired records (expiry_time has passed) ---
        for record in records:
            if _origin_value(record.origin) == Origin.HUMAN_APPROVED.value:
                continue  # human_approved records never expire
            if record.expiry_time is not None:
                expiry = record.expiry_time
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= now:
                    self._store.archive_record(record.id)
                    archived_ids.add(record.id)

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

        # Embedding orphan cleanup
        if self._embedding_store is not None:
            active_memory_ids = [r.id for r in self._store.list_all()]
            self._embedding_store.prune_orphans("memories", active_memory_ids)

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

    # ------------------------------------------------------------------
    # Consolidation state
    # ------------------------------------------------------------------

    def _init_consolidation_table(self) -> None:
        with self._store._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS consolidation_state (
                    agent_id TEXT PRIMARY KEY,
                    last_run_at TEXT,
                    session_count INTEGER NOT NULL DEFAULT 0
                )
            """)

    def _increment_session_count(self, agent_id: str) -> None:
        with self._store._connect() as conn:
            conn.execute(
                "INSERT INTO consolidation_state (agent_id, session_count) VALUES (?, 1) "
                "ON CONFLICT(agent_id) DO UPDATE SET session_count = session_count + 1",
                (agent_id,),
            )

    def _should_auto_consolidate(self, agent_id: str, days: int = 7, min_sessions: int = 10) -> bool:
        from datetime import timedelta
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT last_run_at, session_count FROM consolidation_state WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if row is None or row["session_count"] < min_sessions:
            return False
        if row["last_run_at"] is None:
            return True
        cutoff = _utcnow() - timedelta(days=days)
        last = datetime.fromisoformat(row["last_run_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return last < cutoff

    def _reset_consolidation_state(self, agent_id: str) -> None:
        with self._store._connect() as conn:
            conn.execute(
                "UPDATE consolidation_state SET last_run_at = ?, session_count = 0 WHERE agent_id = ?",
                (_utcnow().isoformat(), agent_id),
            )

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------

    DERIVATION_VERSION = "1"

    def _merge_compatible(self, group: list) -> bool:
        """Conservative factual-compatibility gate for a cluster (T05).

        Require (a) all records share the same key domain slug, and (b) no pair
        forms a temporal/authority conflict. Records that merely share words or
        represent temporal versions of the same fact are NOT auto-merged.
        """
        if len({_key_slug(r.key) for r in group}) != 1:
            return False
        from agentdb.governance.conflict_detector import ConflictDetector
        det = ConflictDetector()
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if det.check(group[i], group[j]) is not None:
                    return False
                if det.check(group[j], group[i]) is not None:
                    return False
        return True

    def _build_canonical(self, cluster: list, agent_id: str, llm_fn) -> "MemoryRecord | None":
        """Create a NEW derived canonical record from a cluster.

        Sources are never mutated. The canonical is a fresh record whose
        provenance is ``agent_inferred`` (a derived record cannot acquire
        human authority from mixed sources — contract P-03 / decision A3).
        Returns the canonical, or None if synthesis is impossible.
        """
        cluster_sorted = sorted(cluster, key=lambda r: r.confidence, reverse=True)
        top_conf = cluster_sorted[0].confidence
        sources = [
            {"id": r.id, "key": r.key, "digest": _record_digest(r), "origin": _origin_value(r.origin)}
            for r in cluster_sorted
        ]

        # Canonical value: synthesized when an llm_fn is supplied and succeeds;
        # otherwise the highest-confidence source value (unchanged copy).
        canonical_value = cluster_sorted[0].value
        if llm_fn is not None:
            try:
                prompt = (
                    f"Synthesize one canonical memory from these {len(cluster)} records:\n"
                    + "\n".join(f"- {r.key}: {r.value}" for r in cluster_sorted)
                    + "\nReturn only the synthesized content, no explanation."
                )
                synthesized = llm_fn(prompt)
                if synthesized:
                    canonical_value = synthesized
            except Exception:
                pass  # fall back to the top source value; never block on LLM

        canonical = MemoryRecord(
            key=f"memory/canonical/{_key_slug(cluster_sorted[0].key)}/{uuid.uuid4()}",
            value=canonical_value,
            origin="agent_inferred",  # derived; never implicitly human_approved
            trust_level="agent",
            agent_id=agent_id,
            confidence=top_conf,
            consolidated=True,
            source_record_ids=[r.id for r in cluster_sorted],
            kind="derived_canonical",
            producer="consolidation",
            derivation={
                "version": self.DERIVATION_VERSION,
                "created_at": _utcnow().isoformat(),
                "strategy": "cluster_synthesis" if llm_fn is not None else "cluster_copy",
                "sources": sources,
            },
            entities=list(cluster_sorted[0].entities or []),
            working_dirs=list(cluster_sorted[0].working_dirs or []),
        )
        return canonical

    def consolidate(
        self,
        agent_id: str,
        since_hours: int = 24,
        min_cluster_size: int = 2,
        skill_candidate_threshold: int = 5,
        llm_fn=None,
        gateway=None,
    ) -> ConsolidationResult:
        """Cluster near-duplicate records into a derived canonical.

        Behavior (ADB-06/07):
        - Source records are **never** mutated or overwritten; their values stay
          exact and inspectable (T03).
        - The canonical is a NEW ``derived_canonical`` record with full source
          lineage (``source_record_ids`` + per-source digests in ``derivation``).
        - Only factually-compatible clusters are merged; related-but-distinct or
          temporal/contradictory records are left untouched (T05).
        - Sources are archived (soft) from default selection, not deleted.

        ``gateway`` and ``skill_candidate_threshold`` are accepted for API
        compatibility but unused.
        """
        from datetime import timedelta
        cutoff = _utcnow() - timedelta(hours=since_hours)
        all_records = [
            r for r in self._store.list_all()
            if r.agent_id == agent_id
            and not r.archived
            and _origin_value(r.origin) != "human_approved"
            and r.kind != "derived_canonical"  # do not re-consolidate canonicals
            and r.created_at <= cutoff
        ]
        raw_clusters = _lc_cluster(all_records, min_overlap=2)
        result = ConsolidationResult(clusters_found=len(raw_clusters))

        for cluster in raw_clusters:
            if len(cluster) < min_cluster_size:
                continue
            if not self._merge_compatible(cluster):
                # Distinct/temporal/contradictory — leave sources untouched.
                continue

            canonical = self._build_canonical(cluster, agent_id, llm_fn)
            if canonical is None:
                continue
            if llm_fn is not None:
                result.synthesized_record_ids.append(canonical.id)

            try:
                self._store.write(canonical)
            except Exception:
                continue

            for rec in cluster:
                try:
                    # Soft-archive the source from default selection; its value
                    # and lineage remain intact and audit-recoverable.
                    self._store.archive_record(rec.id)
                    result.records_archived += 1
                except Exception:
                    pass
            result.records_merged += len(cluster)

        return result
