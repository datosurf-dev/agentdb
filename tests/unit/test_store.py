import pytest
import sqlite3
from pathlib import Path
from agentdb.core.models import MemoryRecord
from agentdb.core.store import MemoryStore


def test_memory_record_has_working_dirs_field():
    rec = MemoryRecord(
        key="memory/org/test",
        value="v",
        origin="agent_inferred",
        trust_level="agent",
        agent_id="test_agent",
    )
    assert rec.working_dirs == []


def test_memory_record_working_dirs_stored_and_retrieved(tmp_path):
    store = MemoryStore(tmp_path / "test.db")
    rec = MemoryRecord(
        key="memory/org/test",
        value="v",
        origin="agent_inferred",
        trust_level="agent",
        agent_id="test_agent",
        working_dirs=["/home/user/project-a", "/home/user/project-b"],
    )
    store.write(rec)
    retrieved = store.list_all()
    match = next(r for r in retrieved if r.key == "memory/org/test")
    assert match.working_dirs == ["/home/user/project-a", "/home/user/project-b"]


def test_legacy_record_missing_column_defaults_to_empty_list(tmp_path):
    """Schema migration: existing DB rows without working_dirs column return []."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    # Create schema WITHOUT working_dirs column (simulates pre-migration DB)
    conn.execute("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, key TEXT NOT NULL, value TEXT NOT NULL,
            origin TEXT NOT NULL, trust_level TEXT NOT NULL, agent_id TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0, version INTEGER NOT NULL DEFAULT 1,
            archived INTEGER NOT NULL DEFAULT 0, retrieval_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, entities TEXT NOT NULL DEFAULT '[]',
            last_retrieved TEXT, conflict_id TEXT, expiry_time TEXT,
            inferred INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "INSERT INTO memories VALUES (?,?,?,?,?,?,1.0,1,0,0,?,?,NULL,NULL,NULL,0)",
        ("id1", "memory/org/old", '"value"', "agent_inferred", "agent", "ag",
         "2025-01-01T00:00:00", "[]"),
    )
    conn.commit()
    conn.close()

    # Opening this DB through MemoryStore should migrate and return working_dirs=[]
    store = MemoryStore(db_path)
    records = store.list_all()
    assert records[0].working_dirs == []
