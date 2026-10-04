from __future__ import annotations

import json
import logging
import sqlite3
import struct
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("agentdb")

_CREATE_METADATA = """
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_CREATE_EMBEDDINGS = """
CREATE TABLE IF NOT EXISTS embeddings (
    table_name   TEXT NOT NULL,
    record_id    TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    working_dirs TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (table_name, record_id)
)
"""

_CREATE_SESSION_TOPICS = """
CREATE TABLE IF NOT EXISTS session_topics (
    session_id TEXT PRIMARY KEY,
    embedding  BLOB NOT NULL,
    created_at TEXT NOT NULL
)
"""


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class EmbeddingStore:
    def __init__(self, db_path, model_name: str, dimensions: int) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._model_name = model_name
        self._dimensions = dimensions
        with self._connect() as conn:
            conn.execute(_CREATE_METADATA)
            conn.execute(_CREATE_EMBEDDINGS)
            conn.execute(_CREATE_SESSION_TOPICS)
        self._validate_or_reset()

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

    def _validate_or_reset(self) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key = 'model_name'"
            ).fetchone()
            stored = row["value"] if row else None

            if stored is None:
                conn.execute(
                    "INSERT OR REPLACE INTO metadata (key, value) VALUES ('model_name', ?)",
                    (self._model_name,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO metadata (key, value) VALUES ('dimensions', ?)",
                    (str(self._dimensions),),
                )
                return

            if stored != self._model_name:
                logger.warning(
                    "agentdb: embedding model changed from '%s' to '%s' — "
                    "wiping all stored embeddings; they will regenerate lazily.",
                    stored,
                    self._model_name,
                )
                conn.execute("DELETE FROM embeddings")
                conn.execute("DELETE FROM session_topics")
                conn.execute(
                    "INSERT OR REPLACE INTO metadata (key, value) VALUES ('model_name', ?)",
                    (self._model_name,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO metadata (key, value) VALUES ('dimensions', ?)",
                    (str(self._dimensions),),
                )

    def put(
        self,
        table: str,
        record_id: str,
        embedding: list[float],
        working_dirs: list[str],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO embeddings
                    (table_name, record_id, embedding, working_dirs, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (table, record_id, _pack(embedding), json.dumps(working_dirs), _utcnow()),
            )

    def get(self, table: str, record_id: str) -> list[float] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT embedding FROM embeddings WHERE table_name = ? AND record_id = ?",
                (table, record_id),
            ).fetchone()
        return _unpack(row["embedding"]) if row else None

    def get_batch(self, table: str, record_ids: list[str]) -> dict[str, list[float]]:
        if not record_ids:
            return {}
        placeholders = ",".join("?" * len(record_ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT record_id, embedding FROM embeddings "
                f"WHERE table_name = ? AND record_id IN ({placeholders})",
                [table] + list(record_ids),
            ).fetchall()
        return {row["record_id"]: _unpack(row["embedding"]) for row in rows}

    def delete(self, table: str, record_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM embeddings WHERE table_name = ? AND record_id = ?",
                (table, record_id),
            )

    def prune_orphans(self, table: str, valid_ids: list[str]) -> int:
        if not valid_ids:
            return 0
        placeholders = ",".join("?" * len(valid_ids))
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM embeddings WHERE table_name = ? "
                f"AND record_id NOT IN ({placeholders})",
                [table] + list(valid_ids),
            )
            return cursor.rowcount

    def put_session_topic(self, session_id: str, embedding: list[float]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO session_topics (session_id, embedding, created_at)
                VALUES (?, ?, ?)
                """,
                (session_id, _pack(embedding), _utcnow()),
            )

    def get_session_topic(self, session_id: str) -> list[float] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT embedding FROM session_topics WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return _unpack(row["embedding"]) if row else None
