from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import ConflictRecord, MemoryRecord


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
    conflict_id TEXT,
    expiry_time TEXT,
    inferred INTEGER NOT NULL DEFAULT 0
)
"""

_MEMORIES_MIGRATION_COLUMNS = (
    ("last_retrieved", "TEXT"),
    ("conflict_id", "TEXT"),
    ("expiry_time", "TEXT"),
    ("inferred", "INTEGER NOT NULL DEFAULT 0"),
)

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
            expiry_time=(
                datetime.fromisoformat(row["expiry_time"])
                if row["expiry_time"] else None
            ),
            inferred=bool(row["inferred"]) if row["inferred"] is not None else False,
        )

    def write(self, record: MemoryRecord) -> MemoryRecord:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memories
                    (id, key, value, origin, trust_level, agent_id, confidence,
                     version, archived, retrieval_count, created_at, entities,
                     last_retrieved, conflict_id, expiry_time, inferred)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    record.expiry_time.isoformat() if record.expiry_time else None,
                    int(record.inferred),
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

    def archive_record(self, record_id: str) -> None:
        """Mark a record as archived by ID."""
        record = self._by_id.get(record_id)
        if record:
            record.archived = True
            self._persist_field(record_id, "archived", 1)

    def list_all(self) -> list[MemoryRecord]:
        return [r for r in self._by_id.values() if not r.archived]

    def list_by_agent(self, agent_id: str) -> list[MemoryRecord]:
        return [r for r in self._by_id.values() if r.agent_id == agent_id and not r.archived]

    def search(self, query: str, limit: int = 5, agent_id: Optional[str] = None) -> list[MemoryRecord]:
        """
        Simple text search: find records whose key or value contains
        any word from query (case-insensitive). agent_id, if given, is
        applied during the scan (before the `limit` slice below) so an
        isolated caller's own matches can't be truncated away by another
        agent's larger matching set.
        """
        words = [w.lower() for w in query.split() if w]
        if not words:
            return []

        results: list[MemoryRecord] = []
        for record in self._by_id.values():
            if record.archived:
                continue
            if agent_id is not None and record.agent_id != agent_id:
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

    def search_by_entities(self, entities: list[str], agent_id: Optional[str] = None) -> list[MemoryRecord]:
        """
        Find records where record.entities has overlap with the given entities.
        Fallback: search key and value for entity name substrings.
        agent_id, if given, is applied inline in BOTH passes below — not as a
        filter on the final chosen list — so an isolated caller's fallback
        search is scoped to their own records rather than being gated by
        whether OTHER agents had entity-tag hits.
        """
        if not entities:
            return []

        entity_set = set(e.lower() for e in entities)

        # First pass: exact entity match
        matches: list[MemoryRecord] = []
        for record in self._by_id.values():
            if record.archived:
                continue
            if agent_id is not None and record.agent_id != agent_id:
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
            if agent_id is not None and record.agent_id != agent_id:
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

        resolved=False (default) -> pending only (resolved = 0).
        resolved=True -> all conflicts (pending + resolved).
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
