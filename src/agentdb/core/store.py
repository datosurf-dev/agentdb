from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import ConflictRecord, MemoryRecord
from .sqlite_connection import connected


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
    inferred INTEGER NOT NULL DEFAULT 0,
    working_dirs TEXT NOT NULL DEFAULT '[]',
    session_id TEXT
)
"""

_MEMORIES_MIGRATION_COLUMNS = (
    ("last_retrieved", "TEXT"),
    ("conflict_id", "TEXT"),
    ("expiry_time", "TEXT"),
    ("inferred", "INTEGER NOT NULL DEFAULT 0"),
    ("working_dirs", "TEXT NOT NULL DEFAULT '[]'"),
    ("consolidated", "INTEGER NOT NULL DEFAULT 0"),
    ("source_record_ids", "TEXT NOT NULL DEFAULT '[]'"),
    ("session_id", "TEXT"),
    ("kind", "TEXT NOT NULL DEFAULT 'source'"),
    ("producer", "TEXT"),
    ("derivation", "TEXT NOT NULL DEFAULT '{}'"),
    ("approval", "TEXT NOT NULL DEFAULT '{}'"),
    ("environment", "TEXT"),
    ("task_type", "TEXT"),
    ("workspace_id", "TEXT"),
    ("repository_id", "TEXT"),
    ("observed_at", "TEXT"),
    ("effective_from", "TEXT"),
    ("effective_until", "TEXT"),
    ("canonical_fingerprint", "TEXT"),
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


def _parse_dt(value) -> Optional[datetime]:
    """Parse an ISO datetime string, tolerating None and already-parsed values."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


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
            # OD-6: a uniqueness guard for active derived canonicals by fingerprint.
            # Partial unique index (only active, fingerprinted canonicals). Creation
            # is best-effort: if legacy duplicate canonicals already exist the
            # index cannot be built, so we skip it and rely on the atomic
            # check-and-insert path (which is still race-safe via BEGIN IMMEDIATE).
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_memories_canonical_fp "
                    "ON memories(canonical_fingerprint) "
                    "WHERE kind = 'derived_canonical' AND archived = 0 "
                    "AND canonical_fingerprint IS NOT NULL"
                )
            except sqlite3.OperationalError:
                pass  # pre-existing duplicates or unsupported; atomic path still applies
        # Load existing records into memory
        self._reload()

    @contextmanager
    def _connect(self):
        with connected(self._db_path) as conn:
            yield conn

    def _reload(self) -> None:
        """Rebuild the in-memory cache from disk (drops rows deleted elsewhere)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM memories").fetchall()
        self._by_id.clear()
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
            working_dirs=json.loads(row["working_dirs"] if "working_dirs" in row.keys() else "[]"),
            consolidated=bool(row["consolidated"]) if "consolidated" in row.keys() else False,
            source_record_ids=json.loads(
                row["source_record_ids"] if "source_record_ids" in row.keys() else "[]"
            ),
            session_id=row["session_id"] if "session_id" in row.keys() else None,
            kind=row["kind"] if "kind" in row.keys() and row["kind"] else "source",
            producer=row["producer"] if "producer" in row.keys() else None,
            derivation=json.loads(
                row["derivation"] if "derivation" in row.keys() and row["derivation"] else "{}"
            ),
            approval=json.loads(
                row["approval"] if "approval" in row.keys() and row["approval"] else "{}"
            ),
            environment=(row["environment"] if "environment" in row.keys() else None),
            task_type=(row["task_type"] if "task_type" in row.keys() else None),
            workspace_id=(row["workspace_id"] if "workspace_id" in row.keys() else None),
            repository_id=(row["repository_id"] if "repository_id" in row.keys() else None),
            observed_at=(_parse_dt(row["observed_at"]) if "observed_at" in row.keys() else None),
            effective_from=(_parse_dt(row["effective_from"]) if "effective_from" in row.keys() else None),
            effective_until=(_parse_dt(row["effective_until"]) if "effective_until" in row.keys() else None),
            canonical_fingerprint=(row["canonical_fingerprint"] if "canonical_fingerprint" in row.keys() else None),
        )

    def write(self, record: MemoryRecord) -> MemoryRecord:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memories
                    (id, key, value, origin, trust_level, agent_id, confidence,
                     version, archived, retrieval_count, created_at, entities,
                     last_retrieved, conflict_id, expiry_time, inferred, working_dirs,
                     consolidated, source_record_ids, session_id, kind, producer, derivation,
                     approval, environment, task_type, workspace_id, repository_id,
                     observed_at, effective_from, effective_until, canonical_fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    json.dumps(record.working_dirs),
                    int(record.consolidated),
                    json.dumps(record.source_record_ids),
                    getattr(record, "session_id", None),
                    getattr(record, "kind", "source"),
                    getattr(record, "producer", None),
                    json.dumps(getattr(record, "derivation", {}) or {}),
                    json.dumps(getattr(record, "approval", {}) or {}),
                    getattr(record, "environment", None),
                    getattr(record, "task_type", None),
                    getattr(record, "workspace_id", None),
                    getattr(record, "repository_id", None),
                    (record.observed_at.isoformat() if getattr(record, "observed_at", None) else None),
                    (record.effective_from.isoformat() if getattr(record, "effective_from", None) else None),
                    (record.effective_until.isoformat() if getattr(record, "effective_until", None) else None),
                    getattr(record, "canonical_fingerprint", None),
                ),
            )
        self._by_id[record.id] = record
        return record

    _INSERT_SQL = """
        INSERT OR REPLACE INTO memories
            (id, key, value, origin, trust_level, agent_id, confidence,
             version, archived, retrieval_count, created_at, entities,
             last_retrieved, conflict_id, expiry_time, inferred, working_dirs,
             consolidated, source_record_ids, session_id, kind, producer, derivation,
             approval, environment, task_type, workspace_id, repository_id,
             observed_at, effective_from, effective_until, canonical_fingerprint)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    @staticmethod
    def _record_params(record: "MemoryRecord") -> tuple:
        return (
            record.id, record.key, json.dumps(record.value), str(record.origin),
            str(record.trust_level), record.agent_id, record.confidence, record.version,
            int(record.archived), record.retrieval_count, record.created_at.isoformat(),
            json.dumps(record.entities),
            record.last_retrieved.isoformat() if record.last_retrieved else None,
            record.conflict_id,
            record.expiry_time.isoformat() if record.expiry_time else None,
            int(record.inferred), json.dumps(record.working_dirs), int(record.consolidated),
            json.dumps(record.source_record_ids), getattr(record, "session_id", None),
            getattr(record, "kind", "source"), getattr(record, "producer", None),
            json.dumps(getattr(record, "derivation", {}) or {}),
            json.dumps(getattr(record, "approval", {}) or {}),
            getattr(record, "environment", None), getattr(record, "task_type", None),
            getattr(record, "workspace_id", None), getattr(record, "repository_id", None),
            (record.observed_at.isoformat() if getattr(record, "observed_at", None) else None),
            (record.effective_from.isoformat() if getattr(record, "effective_from", None) else None),
            (record.effective_until.isoformat() if getattr(record, "effective_until", None) else None),
            getattr(record, "canonical_fingerprint", None),
        )

    def write_canonical_atomic(self, canonical: "MemoryRecord", source_ids: list[str]) -> "MemoryRecord":
        """Atomically create a derived canonical AND archive its source records.

        OD-6 / FINAL-HARDENING §6. Uses a single ``BEGIN IMMEDIATE`` transaction
        (write lock) so concurrent consolidations cannot create duplicate active
        canonicals for the same fingerprint:

        1. Inside the transaction, look for an existing active canonical with the
           same ``canonical_fingerprint`` (idempotency). If found, return it and
           do NOT archive anything (the prior run already did).
        2. Otherwise insert the canonical and soft-archive the sources in the
           same transaction. If the partial unique index rejects the insert (a
           concurrent writer won the race), roll back and return the winner.

        On any failure the transaction rolls back, so sources are NEVER left
        archived without a valid replacement canonical.

        Returns the effective canonical (new or the existing/winning one).
        """
        import sqlite3 as _sqlite3
        from .sqlite_connection import connect as _connect

        fp = getattr(canonical, "canonical_fingerprint", None)
        conn = _connect(self._db_path, timeout=30.0)
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                # (1) Idempotency: existing active canonical for this fingerprint.
                if fp:
                    row = conn.execute(
                        "SELECT * FROM memories WHERE canonical_fingerprint = ? "
                        "AND kind = 'derived_canonical' AND archived = 0 LIMIT 1",
                        (fp,),
                    ).fetchone()
                    if row is not None:
                        conn.execute("COMMIT")
                        existing = self._row_to_record(row)
                        self._by_id[existing.id] = existing
                        return existing
                # (2) Insert canonical + archive sources atomically.
                conn.execute(self._INSERT_SQL, self._record_params(canonical))
                conn.executemany(
                    "UPDATE memories SET archived = 1 WHERE id = ?",
                    [(sid,) for sid in source_ids],
                )
                conn.execute("COMMIT")
            except _sqlite3.IntegrityError:
                # A concurrent writer inserted the same fingerprint; return theirs.
                conn.execute("ROLLBACK")
                if fp:
                    row = conn.execute(
                        "SELECT * FROM memories WHERE canonical_fingerprint = ? "
                        "AND kind = 'derived_canonical' AND archived = 0 LIMIT 1",
                        (fp,),
                    ).fetchone()
                    if row is not None:
                        existing = self._row_to_record(row)
                        self._by_id[existing.id] = existing
                        return existing
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

        # Reflect committed changes in the in-memory cache.
        self._by_id[canonical.id] = canonical
        for sid in source_ids:
            rec = self._by_id.get(sid)
            if rec is not None:
                rec.archived = True
        return canonical

    def get_by_key(self, key: str) -> Optional[MemoryRecord]:
        for record in self._by_id.values():
            if record.key == key and not record.archived:
                return record
        return None

    def select_unique_live_by_key(self, key: str) -> Optional[MemoryRecord]:
        """The single unarchived record for ``key``, or ``None`` if not exactly one.

        Uniqueness and the returned record are resolved from **one** database read,
        not from the in-memory snapshot and not from two separate queries. Splitting
        them (count, then fetch) lets a concurrent write land in between and returns
        a record whose uniqueness was never true of the same state.

        A repeat ``write()`` on an existing key stores a sibling and leaves the
        original live, so a key can legitimately have several live records; that is
        reported as no result rather than an arbitrary pick. Reading from the
        database also means a withdrawal committed by another connection is
        observed, which the in-memory snapshot cannot guarantee.
        """
        from .sqlite_connection import connect as _connect

        conn = _connect(self._db_path, timeout=30.0)
        try:
            rows = conn.execute(
                "SELECT * FROM memories WHERE key = ? AND archived = 0 LIMIT 2",
                (key,),
            ).fetchall()
        finally:
            conn.close()

        if len(rows) != 1:
            return None
        return self._row_to_record(rows[0])

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
