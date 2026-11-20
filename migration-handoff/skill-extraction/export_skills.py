#!/usr/bin/env python3
"""Export AgentDB skill data to a neutral JSONL file.

This tool is standalone: it reads the ``skills`` (and optional ``skill_locks``)
tables directly from a SQLite file and writes JSONL, so existing user skill
data can be preserved for a future independent Skill-Governance package.

It does NOT import ``agentdb`` and does not require the skill modules to exist,
so it remains usable after AgentDB removes skill support.

Usage:
    python export_skills.py /path/to/skills.sqlite -o skills_export.jsonl
    python export_skills.py /path/to/agentdb.sqlite -o out.jsonl   # :memory:/single-db case

Exit codes:
    0  success (even if zero skills found — reports count on stderr)
    2  the database file has no skmable 'skills' table
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_SKILL_COLUMNS = (
    "id", "name", "content", "authored_by", "scope", "version", "archived",
    "health_score", "health_score_reasons", "created_at", "updated_at",
    "last_retrieved", "parent_skill_id", "origin", "validation_score", "metadata",
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def export(db_path: Path, out_path: Path) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "skills"):
            raise LookupError(
                f"No 'skills' table in {db_path} — nothing to export. "
                "Check you passed skills.sqlite (or the combined DB)."
            )
        cols = _existing_columns(conn, "skills")
        select_cols = [c for c in _SKILL_COLUMNS if c in cols]
        rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM skills ORDER BY id, version"
        ).fetchall()

        locks = []
        if _table_exists(conn, "skill_locks"):
            locks = [dict(r) for r in conn.execute("SELECT * FROM skill_locks").fetchall()]

        count = 0
        with out_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "_type": "skill_export_header",
                "source_db": str(db_path),
                "columns": select_cols,
                "skill_locks": locks,
                "schema_version": 1,
            }) + "\n")
            for row in rows:
                record = dict(row)
                # Decode JSON-encoded fields back to native structures where possible.
                for k in ("scope", "health_score_reasons", "metadata"):
                    if k in record and isinstance(record[k], str):
                        try:
                            record[k] = json.loads(record[k])
                        except (json.JSONDecodeError, TypeError):
                            pass
                record["_type"] = "skill"
                f.write(json.dumps(record) + "\n")
                count += 1
        return {"skills": count, "locks": len(locks), "columns": select_cols}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export AgentDB skills to JSONL.")
    ap.add_argument("db_path", type=Path, help="skills.sqlite or combined AgentDB DB")
    ap.add_argument("-o", "--out", type=Path, default=Path("skills_export.jsonl"))
    args = ap.parse_args(argv)
    try:
        summary = export(args.db_path, args.out)
    except LookupError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(
        f"Exported {summary['skills']} skill row(s) and {summary['locks']} lock row(s) "
        f"to {args.out} (columns: {', '.join(summary['columns'])})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
