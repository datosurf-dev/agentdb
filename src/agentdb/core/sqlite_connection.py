"""Shared SQLite connection setup.

Every store in the package used to call ``PRAGMA journal_mode=WAL`` on each new
connection. That pragma asks for an exclusive lock while it runs, and
``journal_mode`` is a *persistent* property of the file: once the database is in
WAL mode it stays there across connections and processes. Re-running it on every
connect therefore bought nothing and cost real contention.

The failure mode was concrete: an ``AgentDB`` opens several connections at once
(memory store, audit log, system stats, embeddings). Two processes
pointed at the same workspace start together, their pragmas collide, and one
raises ``sqlite3.OperationalError: database is locked`` during construction.

``connect`` centralises the settings and makes the journal-mode switch
best-effort: a connection that cannot take the lock still works, because the
file is already in WAL mode from the first successful call.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

#: How long SQLite waits for a competing writer before raising "database is locked".
DEFAULT_TIMEOUT_SECONDS = 30.0

PathLike = Union[str, Path]


def connect(db_path: PathLike, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> sqlite3.Connection:
    """Open a connection with the package-wide settings applied.

    Sets ``row_factory`` for name-based column access and attempts to enable WAL.

    The WAL switch is wrapped because it needs a brief exclusive lock, which
    another process may be holding at that moment. Losing that race is harmless:
    the database is already in WAL mode, and a connection in the file's current
    journal mode works exactly as before.
    """
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # Another process holds the lock. The file keeps whatever journal mode
        # it already has (WAL after the first successful initialisation).
        pass
    return conn


@contextmanager
def connected(db_path: PathLike, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Iterator[sqlite3.Connection]:
    """Connection context manager that commits on success and rolls back on error.

    Mirrors the ``_connect`` helpers that each store used to define separately,
    including closing the connection in every case.
    """
    conn = connect(db_path, timeout=timeout)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
