from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


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
