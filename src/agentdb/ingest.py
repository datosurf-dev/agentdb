"""Conversation ingestion: accept normalised turns a harness already has.

This module is deliberately parser-free. The caller converts its own log format
into :class:`ConversationTurn` objects; AgentDB validates them, deduplicates
them and stores them where it stores everything else:

* ``<memory_dir>/conversations/<agent_id>--<session_id>.jsonl`` -- the same file
  and entry shape ``capture.conversation_jsonl`` produces, so an ingested
  session and a captured one are indistinguishable on disk.
* the memory database -- only the memories the caller extracted from a turn.

Turns themselves are never stored as memories.

Deduplication
-------------
The key is ``sha256(agent | session | role | content | timestamp)``. It is
deliberately *not* ``turn_id``: a caller may regenerate ids, or re-ingest the
same log with fresh ones, and both must be caught. Because the timestamp is part
of the key, identical text at different times is kept.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

ConversationRole = Literal["user", "assistant", "system", "tool"]

VALID_ROLES: frozenset[str] = frozenset({"user", "assistant", "system", "tool"})

_CREATE_INGEST_LEDGER = """
CREATE TABLE IF NOT EXISTS ingest_ledger (
    agent_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    turn_hash   TEXT NOT NULL,
    turn_id     TEXT NOT NULL,
    source      TEXT,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, session_id, turn_hash)
)
"""


@dataclass(frozen=True)
class ConversationTurn:
    """One normalised turn supplied by a harness.

    ``timestamp`` and ``turn_id`` are required: a log without them cannot be
    deduplicated safely and is therefore rejected rather than silently degraded.
    ``memories`` holds ``(key, value)`` pairs the caller extracted from the turn;
    they are written through the normal write path.
    """

    role: ConversationRole
    content: str
    timestamp: datetime
    turn_id: str
    memories: tuple[tuple[str, Any], ...] = ()
    tool_calls: Optional[list[dict]] = None


@dataclass(frozen=True)
class IngestResult:
    accepted: int
    skipped: int
    memory_records: tuple[str, ...] = ()
    jsonl_appended: int = 0


@dataclass(frozen=True)
class PurgeResult:
    """How much was removed from each store by ``purge_session``."""

    memories: int = 0
    conflicts: int = 0
    embeddings: int = 0
    outcomes: int = 0
    external_retrieves: int = 0
    ledger: int = 0
    session_topics: int = 0
    jsonl_lines_removed: int = 0


class IngestValidationError(ValueError):
    """Raised when a turn does not satisfy the required schema."""


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def turn_hash(agent_id: str, session_id: str, turn: ConversationTurn) -> str:
    """Stable dedup key — independent of ``turn_id`` by design."""
    payload = "\x1f".join(
        (agent_id, session_id, turn.role, turn.content, _iso(turn.timestamp))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_turns(turns: Sequence[ConversationTurn]) -> None:
    """Reject the whole batch if any turn is malformed (nothing is written)."""
    if not isinstance(turns, (list, tuple)):
        raise IngestValidationError("turns must be a sequence of ConversationTurn")
    for index, turn in enumerate(turns):
        if not isinstance(turn, ConversationTurn):
            raise IngestValidationError(
                f"turn {index}: expected ConversationTurn, got {type(turn).__name__}"
            )
        if turn.role not in VALID_ROLES:
            raise IngestValidationError(
                f"turn {index}: role must be one of {sorted(VALID_ROLES)}, got {turn.role!r}"
            )
        if not isinstance(turn.content, str):
            raise IngestValidationError(f"turn {index}: content must be a string")
        if not isinstance(turn.timestamp, datetime):
            raise IngestValidationError(f"turn {index}: timestamp must be a datetime")
        if not isinstance(turn.turn_id, str) or not turn.turn_id.strip():
            raise IngestValidationError(f"turn {index}: turn_id must be a non-empty string")
        for pair in turn.memories:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                raise IngestValidationError(
                    f"turn {index}: memories entries must be (key, value) pairs"
                )
            if not isinstance(pair[0], str) or not pair[0].strip():
                raise IngestValidationError(
                    f"turn {index}: memory key must be a non-empty string"
                )


def ensure_ingest_ledger(db_path: Path) -> None:
    """Create the ledger table if it does not exist yet."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(_CREATE_INGEST_LEDGER)


def seen_hashes(db_path: Path, agent_id: str, session_id: str, hashes: Sequence[str]) -> set[str]:
    """Return the subset of ``hashes`` already recorded for this session."""
    if not hashes:
        return set()
    found: set[str] = set()
    with sqlite3.connect(str(db_path)) as conn:
        # Chunked to stay well under SQLite's variable limit on large dumps.
        for start in range(0, len(hashes), 500):
            chunk = hashes[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT turn_hash FROM ingest_ledger "
                f"WHERE agent_id = ? AND session_id = ? AND turn_hash IN ({placeholders})",
                (agent_id, session_id, *chunk),
            ).fetchall()
            found.update(row[0] for row in rows)
    return found


def record_hashes(
    db_path: Path,
    agent_id: str,
    session_id: str,
    rows: Sequence[tuple[str, str]],
    source: str,
) -> None:
    """Insert ledger rows for accepted turns (``INSERT OR IGNORE``)."""
    if not rows:
        return
    now = _iso(datetime.now(timezone.utc))
    with sqlite3.connect(str(db_path)) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO ingest_ledger "
            "(agent_id, session_id, turn_hash, turn_id, source, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(agent_id, session_id, h, tid, source or None, now) for h, tid in rows],
        )


def conversation_log_path(memory_dir: Path, agent_id: str, session_id: str) -> Path:
    """The same path ``capture.conversation_jsonl`` writes to."""
    return Path(memory_dir) / "conversations" / f"{agent_id}--{session_id}.jsonl"


def append_conversation_entries(
    memory_dir: Path,
    agent_id: str,
    session_id: str,
    turns: Sequence[ConversationTurn],
) -> int:
    """Append turns to the conversation log, matching capture's entry shape."""
    if not turns:
        return 0
    path = conversation_log_path(memory_dir, agent_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("a", encoding="utf-8") as handle:
        for turn in turns:
            entry = {
                "session_id": session_id,
                "message_id": turn.turn_id,
                "user_query": turn.content if turn.role == "user" else None,
                "model_response": turn.content if turn.role == "assistant" else None,
                "role": turn.role,
                "timestamp": _iso(turn.timestamp),
                "ingested": True,
            }
            if turn.tool_calls:
                entry["tool_calls"] = turn.tool_calls
            handle.write(json.dumps(entry) + "\n")
            written += 1
    return written


def remove_conversation_log(memory_dir: Path, agent_id: str, session_id: str) -> int:
    """Delete the session's JSONL and return how many lines it held."""
    path = conversation_log_path(memory_dir, agent_id, session_id)
    if not path.exists():
        return 0
    lines = 0
    with path.open("r", encoding="utf-8") as handle:
        lines = sum(1 for _ in handle)
    path.unlink()
    return lines
