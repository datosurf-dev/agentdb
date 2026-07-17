from __future__ import annotations

import json
import os
import sqlite3
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from ..core.models import ConflictRecord, MemoryRecord, Skill
from ..core.store import MemoryStore
from ..core.system import SystemStats
from ..gateway import Gateway
from ..governance.conflict_detector import ConflictDetector
from ..governance.lifecycle_manager import LifecycleManager
from ..governance.permission_router import PermissionDenied, PermissionRouter
from ..retrieval.algorithm import MultiSignalRanker
from ..skills.skill_store import SkillStore


# ---------------------------------------------------------------------------
# Event constants
# ---------------------------------------------------------------------------

EVENT_MEMORY_WRITE = "memory_write"
EVENT_MEMORY_DELETE = "memory_delete"
EVENT_MEMORY_RETRIEVE = "memory_retrieve"
EVENT_MEMORY_OUTCOME = "memory_outcome"
EVENT_EXTERNAL_CONFLICT_DETECTED = "external_conflict_detected"
EVENT_PERMISSION_DENIED = "permission_denied"


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class AuditEntry:
    event_type: str
    payload: dict
    sequence: int


@dataclass
class RetrievalResult:
    records: list
    conflicts: list
    override_instructions: list
    skills: list
    skill_warnings: list
    token_estimate: int = 0
    truncated: bool = False


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_CREATE_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_OUTCOMES = """
CREATE TABLE IF NOT EXISTS outcomes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    outcome_type TEXT NOT NULL,
    outcome_value REAL NOT NULL,
    linked_retrieval_sequence INTEGER,
    created_at TEXT NOT NULL
)
"""

_CREATE_EXTERNAL_RETRIEVES = """
CREATE TABLE IF NOT EXISTS external_retrieves (
    session_id TEXT NOT NULL,
    had_external_context INTEGER NOT NULL,
    had_conflict INTEGER NOT NULL DEFAULT 0
)
"""

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _estimate_tokens(record: MemoryRecord) -> int:
    value_str = json.dumps(record.value) if isinstance(record.value, (dict, list)) else str(record.value)
    return max(1, len(value_str) // 4)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditLog:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_CREATE_AUDIT_LOG)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append(self, event_type: str, payload: dict) -> int:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO audit_log (event_type, payload, created_at) VALUES (?, ?, ?)",
                (event_type, json.dumps(payload), now),
            )
            return cursor.lastrowid

    def replay(self, from_sequence: int = 0) -> list[AuditEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sequence, event_type, payload FROM audit_log WHERE sequence >= ? ORDER BY sequence",
                (from_sequence,),
            ).fetchall()
        return [
            AuditEntry(
                event_type=row["event_type"],
                payload=json.loads(row["payload"]),
                sequence=row["sequence"],
            )
            for row in rows
        ]


# ---------------------------------------------------------------------------
# Skills API (high-level facade)
# ---------------------------------------------------------------------------

class SkillsAPI:
    def __init__(self, skill_store: SkillStore) -> None:
        self._store = skill_store

    def create_skill(
        self,
        skill_id: str,
        name: str,
        content: str,
        scope: list[str],
    ) -> Skill:
        skill = Skill(
            id=skill_id,
            name=name,
            content=content,
            authored_by="human",
            scope=scope,
        )
        return self._store.create(skill)

    def update_skill_version(self, skill_id: str, content: str, new_version: str) -> Skill:
        return self._store.update(skill_id, content, new_version)

    def get_skill(self, skill_id: str) -> Optional[Skill]:
        return self._store.get(skill_id)

    def archive_skill(self, skill_id: str, version: str) -> None:
        self._store.archive(skill_id, version)

    def list_stale_skills_for_agent(self, agent_id: str) -> list[Skill]:
        return self._store.list_stale(agent_id)

    def list_skill_warnings_for_agent(self, agent_id: str) -> list[str]:
        stale = self._store.list_stale(agent_id)
        return [
            f"Skill '{skill.id}' is stale (health_score={skill.health_score:.2f})"
            for skill in stale
        ]


# ---------------------------------------------------------------------------
# Default memory.yaml content
# ---------------------------------------------------------------------------

_DEFAULT_MEMORY_YAML = """\
mode: budget_optimized
tracked_directories: []
capture:
  conversation_jsonl: false
reconciliation:
  triggers:
    - conversation_started
database:
  mode: embedded
  path: database/agentdb.sqlite
"""


# ---------------------------------------------------------------------------
# Main AgentDB class
# ---------------------------------------------------------------------------

class AgentDB:
    def __init__(
        self,
        path: Optional[Any] = None,
        memory_dir: Optional[Any] = None,
        permissions_path: Optional[Any] = None,
        tracking_directories: Optional[list[str]] = None,
        tracking_root: Any = ".",
        tracking_max_files_per_directory: int = 200,
        db_path: Optional[Any] = None,
        summarizer: Optional[Callable] = None,
        infer_fn: Optional[Callable] = None,
    ) -> None:
        # db_path is an alias for path (used by adapter tests)
        if db_path is not None and path is None:
            path = db_path
        self._summarizer = summarizer
        self._infer_fn = infer_fn
        # Resolve memory_dir from args or env vars
        resolved_memory_dir: Optional[Path] = None
        if memory_dir is not None:
            resolved_memory_dir = Path(memory_dir).resolve()
        else:
            env_memory_dir = os.environ.get("AGENTDB_MEMORY_DIR")
            env_home = os.environ.get("AGENTDB_HOME")
            if env_memory_dir:
                resolved_memory_dir = Path(env_memory_dir).resolve()
            elif env_home:
                resolved_memory_dir = Path(env_home).resolve()

        self.memory_dir: Optional[Path] = resolved_memory_dir

        # Set up workspace directories if memory_dir is provided
        self.memory_config_path: Optional[Path] = None
        self.memory_config: dict = {}

        if resolved_memory_dir is not None:
            # Create workspace layout
            for subdir in ("config", "database", "conversations", "embeddings", "reconciliation", "logs"):
                (resolved_memory_dir / subdir).mkdir(parents=True, exist_ok=True)

            config_path = resolved_memory_dir / "config" / "memory.yaml"
            self.memory_config_path = config_path.resolve()

            if not config_path.exists():
                config_path.write_text(_DEFAULT_MEMORY_YAML, encoding="utf-8")

            with config_path.open("r", encoding="utf-8") as f:
                self.memory_config = yaml.safe_load(f) or {}

            # Validate full_storage mode
            if self.memory_config.get("mode") == "full_storage":
                td = self.memory_config.get("tracked_directories", [])
                if not td:
                    raise ValueError(
                        "tracked_directories must be non-empty when mode is 'full_storage'"
                    )

        # Determine DB path
        if path is not None:
            db_path = Path(path).resolve()
        elif resolved_memory_dir is not None:
            db_path = (resolved_memory_dir / "database" / "agentdb.sqlite").resolve()
        else:
            db_path = Path(":memory:")

        self._path = db_path

        # Set up permissions router
        self._permissions: Optional[PermissionRouter] = None
        if permissions_path is not None:
            self._permissions = PermissionRouter(permissions_path)

        # Initialize stores using shared DB path
        self._store = MemoryStore(db_path)
        self.audit_log = AuditLog(db_path)
        self._system = SystemStats(db_path)

        # Skills store - use separate DB or same
        skills_db_path = db_path.parent / "skills.sqlite" if db_path != Path(":memory:") else db_path
        self.skills = SkillStore(db_path=skills_db_path)
        self.skills_api = SkillsAPI(self.skills)

        # Initialize outcomes table
        self._init_outcomes_table()

        # Tracking config
        self.tracking_directories: list[str] = tracking_directories or []
        self.tracking_root: Path = Path(tracking_root).resolve()
        self.tracking_max_files_per_directory: int = tracking_max_files_per_directory

        # Session tracking: session_id -> list of record ids retrieved
        self._session_record_ids: dict[str, list[str]] = {}
        self._ranker = MultiSignalRanker()

        # Gateway surface (initialized after all dependencies are ready)
        self._gateway: Optional[Gateway] = None

        # Lifecycle manager — initialized after _session_record_ids is created
        self._lifecycle = LifecycleManager(
            store=self._store,
            system=self._system,
            session_record_ids=self._session_record_ids,
        )

    @property
    def _reconciliation_handlers(self) -> dict[str, list[Callable]]:  # type: ignore[override]
        """Deprecated — handlers now live in db.gateway._handlers."""
        warnings.warn(
            "AgentDB._reconciliation_handlers is deprecated and will be removed in a future version. "
            "Use db.gateway._handlers instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.gateway._handlers

    @property
    def gateway(self) -> Gateway:
        """Unified governance surface: reconciliation, conflict queue, proposals."""
        if self._gateway is None:
            self._gateway = Gateway(self)
        return self._gateway

    @property
    def system(self) -> SystemStats:
        """System statistics surface."""
        return self._system

    @property
    def lifecycle(self) -> LifecycleManager:
        """Lifecycle manager: sweep, reinforce, record_outcome."""
        return self._lifecycle

    def _init_outcomes_table(self) -> None:
        with self._store._connect() as conn:
            conn.execute(_CREATE_OUTCOMES)
            conn.execute(_CREATE_EXTERNAL_RETRIEVES)

    def _derive_trust_level(self, origin: str) -> str:
        if origin == "human_approved":
            return "human"
        elif origin == "provider_ingested":
            return "provider"
        else:
            return "agent"

    def _derive_trust_zone(self, origin: str) -> str:
        """Derive the trust zone from origin for permission checks."""
        if origin == "human_approved":
            return "human_approved"
        return "agent_inferred"

    def write(
        self,
        key: str,
        value: Any,
        origin: str,
        agent_id: str,
        session_id: Optional[str] = None,
        entities: Optional[list] = None,
        confidence: Optional[float] = None,
        infer: bool = False,
        expiry_time: Optional["datetime"] = None,
    ) -> MemoryRecord:
        if infer:
            if self._infer_fn is None:
                raise ValueError(
                    "infer=True requires an infer_fn — pass infer_fn=<callable> to AgentDB()"
                )
            value = self._infer_fn(value)

        trust_zone = self._derive_trust_zone(origin)

        # Permission check
        if self._permissions is not None:
            try:
                self._permissions.check_write(key, agent_id, trust_zone)
            except PermissionDenied as e:
                self.audit_log.append(
                    EVENT_PERMISSION_DENIED,
                    {
                        "key": key,
                        "agent_id": agent_id,
                        "trust_zone": trust_zone,
                        "reason": str(e),
                    },
                )
                raise

        trust_level = self._derive_trust_level(origin)
        record = MemoryRecord(
            key=key,
            value=value,
            origin=origin,
            trust_level=trust_level,
            agent_id=agent_id,
            confidence=confidence if confidence is not None else 1.0,
            entities=list(entities) if entities else [],
            expiry_time=expiry_time,
            inferred=infer,
        )

        # Conflict detection: check before persisting
        existing = self._store.get_by_key(key)
        if existing is not None and not existing.archived:
            conflict = ConflictDetector().check(new_record=record, existing=existing)
            if conflict is not None:
                record.conflict_id = conflict.id
                existing.conflict_id = conflict.id
                self._store.update_conflict_id(existing.id, conflict.id)
                self._store.store_conflict(conflict)
                self.gateway.dispatch(
                    "conflict_detected",
                    conflict_id=conflict.id,
                    conflict_type=conflict.conflict_type,
                    key=key,
                    agent_id=agent_id,
                    session_id=session_id,
                )
                self.audit_log.append(
                    "conflict_detected",
                    {
                        "conflict_id": conflict.id,
                        "conflict_type": conflict.conflict_type,
                        "key": key,
                        "record_a_id": existing.id,
                        "record_b_id": record.id,
                        "agent_id": agent_id,
                    },
                )

        self._store.write(record)
        self.audit_log.append(
            EVENT_MEMORY_WRITE,
            {
                "key": key,
                "agent_id": agent_id,
                "session_id": session_id,
                "record_id": record.id,
            },
        )
        return record

    def read(self, key: str, agent_id: Optional[str] = None) -> Optional[MemoryRecord]:
        """Raw read by key, no permission check."""
        return self._store.get_by_key(key)

    def delete(self, key: str, agent_id: str, trust_zone: str) -> None:
        """Archive a record by key. Raises PermissionDenied if not allowed."""
        if self._permissions is not None:
            try:
                self._permissions.check_write(key, agent_id, trust_zone)
            except PermissionDenied as e:
                self.audit_log.append(
                    EVENT_PERMISSION_DENIED,
                    {
                        "key": key,
                        "agent_id": agent_id,
                        "trust_zone": trust_zone,
                        "reason": str(e),
                    },
                )
                raise

        self._store.delete_by_key(key)
        self.audit_log.append(
            EVENT_MEMORY_DELETE,
            {"key": key, "agent_id": agent_id},
        )

    def retrieve(
        self,
        query: str,
        agent_id: str,
        limit: int = 5,
        session_id: Optional[str] = None,
        external_context: Optional[list[dict]] = None,
        budget_tokens: Optional[int] = None,
    ) -> RetrievalResult:
        candidates = self._store.search(query, limit=max(limit, 50))
        ranked = self._ranker.rank(query, candidates, agent_id=agent_id)[:limit]
        ranked_records = [rr.record for rr in ranked]

        # Budget cap
        selected: list[MemoryRecord] = []
        token_total = 0
        truncated = False
        for record in ranked_records:
            est = _estimate_tokens(record)
            if budget_tokens is not None and token_total + est > budget_tokens:
                truncated = True
                break
            selected.append(record)
            token_total += est
        records = selected

        # Increment retrieval counts
        for record in records:
            self._store.increment_retrieval_count(record.id)

        record_ids = [r.id for r in records]
        signal_scores = {rr.record.id: rr.composite_score for rr in ranked}

        # Track for outcome recording
        if session_id is not None:
            if session_id not in self._session_record_ids:
                self._session_record_ids[session_id] = []
            self._session_record_ids[session_id].extend(record_ids)

        seq = self.audit_log.append(
            EVENT_MEMORY_RETRIEVE,
            {
                "query": query,
                "record_ids": record_ids,
                "signal_scores": signal_scores,
                "session_id": session_id,
                "trigger_source": "query",
                "reconciliation_run_id": None,
            },
        )

        # Handle external context conflict detection
        had_conflict = False
        if external_context is not None:
            # Check for overlap: any content word matches a record value
            ext_words = set()
            for ctx in external_context:
                content = ctx.get("content", "")
                ext_words.update(w.lower() for w in content.split())

            overlap_records = []
            for record in records:
                value_str = (
                    json.dumps(record.value)
                    if isinstance(record.value, (dict, list))
                    else str(record.value)
                ).lower()
                for word in ext_words:
                    if word in value_str:
                        overlap_records.append(record)
                        break

            if overlap_records:
                had_conflict = True
                self.audit_log.append(
                    EVENT_EXTERNAL_CONFLICT_DETECTED,
                    {
                        "query": query,
                        "count": len(overlap_records),
                        "sources": [ctx.get("source", "") for ctx in external_context],
                        "session_id": session_id,
                        "trigger_source": "query",
                        "reconciliation_run_id": None,
                    },
                )

            # Record external retrieve stats
            with self._store._connect() as conn:
                conn.execute(
                    "INSERT INTO external_retrieves (session_id, had_external_context, had_conflict) VALUES (?, ?, ?)",
                    (session_id or "", 1, int(had_conflict)),
                )

        # Retrieve matching skills for this agent
        all_skills = self.skills.get_for_agent(agent_id)
        skill_warnings: list[str] = []
        for skill in all_skills:
            self.skills.mark_retrieved(skill.id)
            if skill.health_score < 0.6 or "staleness" in skill.health_score_reasons:
                skill_warnings.append(
                    f"Skill '{skill.id}' is stale (health_score={skill.health_score:.2f})"
                )

        # Fetch conflicts for returned records
        conflicts = self._store.get_conflicts_for_records([r.id for r in records])

        # Build override instructions for authority conflicts
        override_instructions = []
        for c in conflicts:
            if c.conflict_type == "authority":
                override_instructions.append(
                    f"Authority conflict on a retrieved memory (conflict_id={c.id}): "
                    "the human-approved version takes precedence over any agent-inferred value for this key."
                )

        return RetrievalResult(
            records=records,
            conflicts=conflicts,
            override_instructions=override_instructions,
            skills=all_skills,
            skill_warnings=skill_warnings,
            token_estimate=token_total,
            truncated=truncated,
        )

    def retrieve_by_entities(
        self,
        entities: list[str],
        agent_id: str,
        session_id: Optional[str] = None,
    ) -> RetrievalResult:
        records = self._store.search_by_entities(entities)

        record_ids = [r.id for r in records]

        # Track for outcome recording
        if session_id is not None:
            if session_id not in self._session_record_ids:
                self._session_record_ids[session_id] = []
            self._session_record_ids[session_id].extend(record_ids)

        self.audit_log.append(
            EVENT_MEMORY_RETRIEVE,
            {
                "query": "",
                "record_ids": record_ids,
                "signal_scores": {r.id: 1.0 for r in records},
                "session_id": session_id,
                "trigger_source": "entity",
                "entities": entities,
                "reconciliation_run_id": None,
            },
        )

        all_skills = self.skills.get_for_agent(agent_id)
        skill_warnings: list[str] = []
        for skill in all_skills:
            self.skills.mark_retrieved(skill.id)
            if skill.health_score < 0.6 or "staleness" in skill.health_score_reasons:
                skill_warnings.append(
                    f"Skill '{skill.id}' is stale (health_score={skill.health_score:.2f})"
                )

        # Fetch conflicts for returned records
        conflicts = self._store.get_conflicts_for_records([r.id for r in records])

        override_instructions = []
        for c in conflicts:
            if c.conflict_type == "authority":
                override_instructions.append(
                    f"Authority conflict on a retrieved memory (conflict_id={c.id}): "
                    "the human-approved version takes precedence over any agent-inferred value for this key."
                )

        return RetrievalResult(
            records=records,
            conflicts=conflicts,
            override_instructions=override_instructions,
            skills=all_skills,
            skill_warnings=skill_warnings,
        )

    def record_outcome(
        self,
        session_id: str,
        agent_id: str,
        outcome_type: str,
        outcome_value: float,
    ) -> int:
        """Backward-compat alias — delegates to db.lifecycle.record_outcome()."""
        adjusted_ids = self.lifecycle.record_outcome(
            session_id=session_id,
            agent_id=agent_id,
            outcome_type=outcome_type,
            outcome_value=outcome_value,
        )
        self.audit_log.append(
            EVENT_MEMORY_OUTCOME,
            {
                "session_id": session_id,
                "outcome_type": outcome_type,
                "outcome_value": float(outcome_value),
                "linked_retrieval_sequence": None,
                "adjusted_record_ids": adjusted_ids,
            },
        )
        return len(adjusted_ids)

    def register_reconciliation_handler(self, trigger: str, handler: Callable) -> None:
        """Backward-compat alias → db.gateway.subscribe(trigger, handler)."""
        self.gateway.subscribe(trigger, handler)

    def dispatch_reconciliation_trigger(self, trigger: str, **kwargs) -> int:
        """Backward-compat alias → db.gateway.dispatch(trigger, **kwargs)."""
        return self.gateway.dispatch(trigger, **kwargs)
