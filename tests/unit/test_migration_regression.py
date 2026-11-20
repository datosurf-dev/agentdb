"""Schema migration regression tests (Workstream E; gate E).

The distilled-knowledge columns (kind/producer/derivation) are added
additively and idempotently to the ``memories`` table. These tests prove:

- A pre-migration database (old minimal schema) opens and its rows are readable.
- The new columns default correctly for legacy rows.
- Re-opening is idempotent (no error, no data change).
- "Rollback" = old code reading the table still works because it only reads
  known columns (simulated by reading a raw SELECT of legacy columns).
"""
from __future__ import annotations

import json
import sqlite3

from agentdb.core.store import MemoryStore, _CREATE_MEMORIES


def _make_legacy_db(path):
    """Create a DB with ONLY the original minimal columns + one legacy row."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE memories (
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
            entities TEXT NOT NULL DEFAULT '[]'
        )
    """)
    conn.execute(
        "INSERT INTO memories (id, key, value, origin, trust_level, agent_id, "
        "confidence, created_at, entities) VALUES (?,?,?,?,?,?,?,?,?)",
        ("legacy-1", "memory/orders/legacy", json.dumps("legacy value"),
         "agent_inferred", "agent", "a1", 0.8, "2026-01-01T00:00:00+00:00", "[]"),
    )
    conn.commit()
    conn.close()


def _columns(path):
    conn = sqlite3.connect(str(path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    conn.close()
    return cols


def test_legacy_db_opens_and_row_is_readable(tmp_path):
    db_path = tmp_path / "legacy.sqlite"
    _make_legacy_db(db_path)

    store = MemoryStore(db_path)

    rec = store.get_by_id("legacy-1")
    assert rec is not None
    assert rec.key == "memory/orders/legacy"
    # value is JSON-decoded on load, so the Python str is returned directly
    assert rec.value == "legacy value"


def test_migration_adds_new_columns_with_defaults(tmp_path):
    db_path = tmp_path / "legacy.sqlite"
    _make_legacy_db(db_path)

    MemoryStore(db_path)

    cols = _columns(db_path)
    assert {"kind", "producer", "derivation"} <= cols
    # Legacy row reads back with the column defaults.
    store = MemoryStore(db_path)
    rec = store.get_by_id("legacy-1")
    assert rec.kind == "source"
    assert rec.producer is None
    assert rec.derivation == {}


def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "legacy.sqlite"
    _make_legacy_db(db_path)

    MemoryStore(db_path)
    # Re-open several times — must not raise or duplicate/alter data.
    for _ in range(3):
        store = MemoryStore(db_path)
        assert store.get_by_id("legacy-1") is not None
    assert _columns(db_path) >= {"kind", "producer", "derivation"}


def test_rollback_old_reader_ignores_new_columns(tmp_path):
    """Simulate older code: a SELECT of only legacy columns still succeeds and
    returns the original value, proving the added columns are inert."""
    db_path = tmp_path / "legacy.sqlite"
    _make_legacy_db(db_path)
    MemoryStore(db_path)  # apply migration

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT id, key, value, origin FROM memories WHERE id = 'legacy-1'"
    ).fetchone()
    conn.close()

    assert row[0] == "legacy-1"
    assert json.loads(row[2]) == "legacy value"
