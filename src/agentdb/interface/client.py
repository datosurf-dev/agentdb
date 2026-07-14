from __future__ import annotations

import json
import os
import sqlite3
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from ..core.models import ConflictRecord, MemoryRecord, Skill
from ..gateway import Gateway
from ..governance.conflict_detector import ConflictDetector
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

_CREATE_MEMORIES = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    origin TEXT NOT NULL,
    trust_level TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    version INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    retrieval_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    entities TEXT NOT NULL DEFAULT '[]',
    last_retrieved TEXT,
    conflict_id TEXT
)
"""

_MEMORIES_MIGRATION_COLUMNS = (
    ("last_retrieved", "TEXT"),
    ("conflict_id", "TEXT"),
)

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

_CREATE_CONFLICTS = """
CREATE TABLE IF NOT EXISTS conflicts (
    id          TEXT PRIMARY KEY,
    record_a_id TEXT NOT NULL,
    record_b_id TEXT NOT NULL,
    conflict_type TEXT NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0,
    auto_resolved INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
)
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _estimate_tokens(record: MemoryRecord) -> int:
    value_str = json.dumps(record.value) if isinstance(record.value, (dict, list)) else str(record.value)
    return max(1, len(value_str) // 4)


# ---------------------------------------------------------------------------
# In-memory store backed by SQLite
# ---------------------------------------------------------------------------

class MemoryStore:
    """SQLite-backed store for MemoryRecord objects."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._by_id: dict[str, MemoryRecord] = {}
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_CREATE_MEMORIES)
            conn.execute(_CREATE_CONFLICTS)
            for column, coltype in _MEMORIES_MIGRATION_COLUMNS:
                try:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {column} {coltype}")
                except sqlite3.OperationalError:
                    pass  # column already exists (pre-existing database)
        # Load existing records into memory
        self._reload()

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

    def _reload(self) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM memories").fetchall()
        for row in rows:
            record = self._row_to_record(row)
            self._by_id[record.id] = record

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            key=row["key"],
            value=json.loads(row["value"]),
            origin=row["origin"],
            trust_level=row["trust_level"],
            agent_id=row["agent_id"],
            confidence=row["confidence"],
            version=row["version"],
            archived=bool(row["archived"]),
            retrieval_count=row["retrieval_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
            entities=json.loads(row["entities"]),
            last_retrieved=(
                datetime.fromisoformat(row["last_retrieved"])
                if row["last_retrieved"] else None
            ),
            conflict_id=row["conflict_id"],
        )

    def write(self, record: MemoryRecord) -> MemoryRecord:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memories
                    (id, key, value, origin, trust_level, agent_id, confidence,
                     version, archived, retrieval_count, created_at, entities,
                     last_retrieved, conflict_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.key,
                    json.dumps(record.value),
                    str(record.origin),
                    str(record.trust_level),
                    record.agent_id,
                    record.confidence,
                    record.version,
                    int(record.archived),
                    record.retrieval_count,
                    record.created_at.isoformat(),
                    json.dumps(record.entities),
                    record.last_retrieved.isoformat() if record.last_retrieved else None,
                    record.conflict_id,
                ),
            )
        self._by_id[record.id] = record
        return record

    def get_by_key(self, key: str) -> Optional[MemoryRecord]:
        for record in self._by_id.values():
            if record.key == key and not record.archived:
                return record
        return None

    def get_by_id(self, record_id: str) -> Optional[MemoryRecord]:
        return self._by_id.get(record_id)

    def delete_by_key(self, key: str) -> None:
        record = self.get_by_key(key)
        if record is None:
            return
        record.archived = True
        self._persist_field(record.id, "archived", 1)

    def _persist_field(self, record_id: str, field_name: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                f"UPDATE memories SET {field_name} = ? WHERE id = ?",
                (value, record_id),
            )

    def increment_retrieval_count(self, record_id: str) -> None:
        record = self._by_id.get(record_id)
        if record:
            record.retrieval_count += 1
            self._persist_field(record_id, "retrieval_count", record.retrieval_count)
            record.last_retrieved = _utcnow()
            self._persist_field(record_id, "last_retrieved", record.last_retrieved.isoformat())

    def update_confidence(self, record_id: str, new_confidence: float) -> None:
        record = self._by_id.get(record_id)
        if record:
            record.confidence = round(new_confidence, 10)
            self._persist_field(record_id, "confidence", record.confidence)

    def list_all(self) -> list[MemoryRecord]:
        return [r for r in self._by_id.values() if not r.archived]

    def list_by_agent(self, agent_id: str) -> list[MemoryRecord]:
        return [r for r in self._by_id.values() if r.agent_id == agent_id and not r.archived]

    def search(self, query: str, limit: int = 5) -> list[MemoryRecord]:
        """
        Simple text search: find records whose key or value contains
        any word from query (case-insensitive).
        """
        words = [w.lower() for w in query.split() if w]
        if not words:
            return []

        results: list[MemoryRecord] = []
        for record in self._by_id.values():
            if record.archived:
                continue
            key_lower = record.key.lower()
            value_str = (
                json.dumps(record.value) if isinstance(record.value, (dict, list)) else str(record.value)
            ).lower()
            for word in words:
                if word in key_lower or word in value_str:
                    results.append(record)
                    break

        return results[:limit]

    def search_by_entities(self, entities: list[str]) -> list[MemoryRecord]:
        """
        Find records where record.entities has overlap with the given entities.
        Fallback: search key and value for entity name substrings.
        """
        if not entities:
            return []

        entity_set = set(e.lower() for e in entities)

        # First pass: exact entity match
        matches: list[MemoryRecord] = []
        for record in self._by_id.values():
            if record.archived:
                continue
            record_entities = set(e.lower() for e in (record.entities or []))
            if record_entities & entity_set:
                matches.append(record)

        if matches:
            return matches

        # Fallback: substring search in key and value
        fallback: list[MemoryRecord] = []
        for record in self._by_id.values():
            if record.archived:
                continue
            key_lower = record.key.lower()
            value_str = (
                json.dumps(record.value) if isinstance(record.value, (dict, list)) else str(record.value)
            ).lower()
            for entity in entities:
                entity_lower = entity.lower()
                if entity_lower in key_lower or entity_lower in value_str:
                    fallback.append(record)
                    break

        return fallback

    # ------------------------------------------------------------------
    # Conflict storage
    # ------------------------------------------------------------------

    def store_conflict(self, conflict: ConflictRecord) -> None:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO conflicts
                    (id, record_a_id, record_b_id, conflict_type, resolved, auto_resolved, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conflict.id,
                    conflict.record_a_id,
                    conflict.record_b_id,
                    conflict.conflict_type,
                    int(conflict.resolved),
                    int(conflict.auto_resolved),
                    now,
                ),
            )

    def get_conflicts_for_records(self, record_ids: list[str]) -> list[ConflictRecord]:
        if not record_ids:
            return []
        placeholders = ",".join("?" * len(record_ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, record_a_id, record_b_id, conflict_type, resolved, auto_resolved
                FROM conflicts
                WHERE record_a_id IN ({placeholders}) OR record_b_id IN ({placeholders})
                """,
                record_ids + record_ids,
            ).fetchall()
        return [
            ConflictRecord(
                id=row["id"],
                record_a_id=row["record_a_id"],
                record_b_id=row["record_b_id"],
                conflict_type=row["conflict_type"],
                resolved=bool(row["resolved"]),
                auto_resolved=bool(row["auto_resolved"]),
            )
            for row in rows
        ]

    def list_conflicts(self, resolved: bool = False) -> list[ConflictRecord]:
        """
        Return conflict records from the conflicts table.

        resolved=False (default) → pending only (resolved = 0).
        resolved=True → all conflicts (pending + resolved).
        """
        query = (
            "SELECT id, record_a_id, record_b_id, conflict_type, resolved, auto_resolved "
            "FROM conflicts"
        )
        with self._connect() as conn:
            if not resolved:
                query += " WHERE resolved = 0"
            rows = conn.execute(query).fetchall()
        return [
            ConflictRecord(
                id=row["id"],
                record_a_id=row["record_a_id"],
                record_b_id=row["record_b_id"],
                conflict_type=row["conflict_type"],
                resolved=bool(row["resolved"]),
                auto_resolved=bool(row["auto_resolved"]),
            )
            for row in rows
        ]

    def update_conflict_id(self, record_id: str, conflict_id: str) -> None:
        self._persist_field(record_id, "conflict_id", conflict_id)
        record = self._by_id.get(record_id)
        if record is not None:
            record.conflict_id = conflict_id


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
# System stats
# ---------------------------------------------------------------------------

class SystemStats:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

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

    def agentdb_stats(self) -> dict:
        with self._connect() as conn:
            # Total memories (non-archived)
            total = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE archived = 0"
            ).fetchone()[0]

            # Average confidence
            avg_conf_row = conn.execute(
                "SELECT AVG(confidence) FROM memories WHERE archived = 0"
            ).fetchone()
            avg_confidence = avg_conf_row[0] if avg_conf_row[0] is not None else 0.0

            # Low confidence count (< 0.5)
            low_conf = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE archived = 0 AND confidence < 0.5"
            ).fetchone()[0]

            # Retrieval count total (sum of retrieval_count across all memories)
            retrieval_total_row = conn.execute(
                "SELECT SUM(retrieval_count) FROM memories WHERE archived = 0"
            ).fetchone()
            retrieval_total = retrieval_total_row[0] if retrieval_total_row[0] is not None else 0

            # Conflict pending count - from audit log
            conflict_pending = 0  # We don't track ConflictRecord table; use 0

            # Outcome positive rate
            outcome_rate = None
            try:
                outcomes_rows = conn.execute(
                    "SELECT outcome_value FROM outcomes"
                ).fetchall()
                if len(outcomes_rows) >= 10:
                    positive = sum(1 for r in outcomes_rows if r["outcome_value"] >= 0.5)
                    outcome_rate = positive / len(outcomes_rows)
            except Exception:
                pass

            # External conflict rate
            external_conflict_rate = None
            try:
                ext_rows = conn.execute(
                    "SELECT had_external_context, had_conflict FROM external_retrieves"
                ).fetchall()
                external_retrieves = [r for r in ext_rows if r["had_external_context"]]
                if external_retrieves:
                    conflicts = sum(1 for r in external_retrieves if r["had_conflict"])
                    external_conflict_rate = conflicts / len(external_retrieves)
            except Exception:
                pass

        return {
            "total_memories": total,
            "avg_confidence": avg_confidence or 0.0,
            "low_confidence_count": low_conf,
            "conflict_pending_count": conflict_pending,
            "outcome_positive_rate": outcome_rate,
            "retrieval_count_total": retrieval_total,
            "external_conflict_rate": external_conflict_rate,
        }


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
    ) -> None:
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
        self.system = SystemStats(db_path)

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
    ) -> MemoryRecord:
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
        """
        Adjust confidence for records retrieved in this session.

        Rules by origin:
        - agent_inferred: positive (+0.05), negative (-0.02)
        - provider_ingested: positive (+0.0), negative (-0.02)
        - human_approved: never adjusted

        Consumes and clears the session record IDs after recording the outcome.
        """
        # Deduplicate record IDs — each unique record is adjusted once
        seen_ids = list(dict.fromkeys(self._session_record_ids.get(session_id, [])))

        is_positive = outcome_value >= 0.5
        adjusted_ids: list[str] = []

        for record_id in seen_ids:
            record = self._store.get_by_id(record_id)
            if record is None:
                continue

            origin = str(record.origin)
            if origin == "human_approved":
                continue  # never adjusted

            if origin == "agent_inferred":
                if is_positive:
                    new_conf = record.confidence + 0.05
                else:
                    new_conf = record.confidence - 0.02
                self._store.update_confidence(record_id, new_conf)
                adjusted_ids.append(record_id)
            elif origin == "provider_ingested":
                if is_positive:
                    pass  # NOT adjusted for positive
                else:
                    new_conf = record.confidence - 0.02
                    self._store.update_confidence(record_id, new_conf)
                    adjusted_ids.append(record_id)

        # Clear session record IDs so next retrieve starts fresh
        if session_id in self._session_record_ids:
            self._session_record_ids[session_id] = []

        # Persist outcome
        now = _utcnow().isoformat()
        with self._store._connect() as conn:
            conn.execute(
                "INSERT INTO outcomes (id, session_id, agent_id, outcome_type, outcome_value, linked_retrieval_sequence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    session_id,
                    agent_id,
                    outcome_type,
                    outcome_value,
                    None,
                    now,
                ),
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
