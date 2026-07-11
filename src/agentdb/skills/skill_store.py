from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from ..core.models import Skill


class SkillStoreError(Exception):
    pass


_CREATE_SKILLS_TABLE = """
CREATE TABLE IF NOT EXISTS skills (
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    authored_by TEXT NOT NULL,
    scope TEXT NOT NULL,
    version TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    health_score REAL NOT NULL DEFAULT 1.0,
    health_score_reasons TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_retrieved TEXT,
    parent_skill_id TEXT,
    PRIMARY KEY (id, version)
)
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_skill(row: sqlite3.Row) -> Skill:
    return Skill(
        id=row["id"],
        name=row["name"],
        content=row["content"],
        authored_by=row["authored_by"],
        scope=json.loads(row["scope"]),
        version=row["version"],
        archived=bool(row["archived"]),
        health_score=row["health_score"],
        health_score_reasons=json.loads(row["health_score_reasons"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_retrieved=datetime.fromisoformat(row["last_retrieved"]) if row["last_retrieved"] else None,
        parent_skill_id=row["parent_skill_id"],
    )


def _compute_health(skill: Skill) -> tuple[float, list[str]]:
    """Compute health score based on staleness since last retrieval (or
    creation, for a skill that has never been retrieved)."""
    now = _utcnow()
    reference = skill.last_retrieved or skill.created_at
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_days = (now - reference).days
    health_score = 1.0
    reasons: list[str] = []
    if age_days > 30:
        # Decay formula: steeper than linear to ensure > 45 days gives health < 0.6
        # max(0.1, 1.0 - (age_days - 30) * 0.03)
        health_score = max(0.1, 1.0 - (age_days - 30) * 0.03)
        reasons.append("staleness")
    return health_score, reasons


class SkillStore:
    def __init__(self, db_path: Any) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_CREATE_SKILLS_TABLE)

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

    def create(self, skill: Skill) -> Skill:
        if skill.authored_by != "human":
            raise SkillStoreError(
                f"Skills may only be authored by humans, got '{skill.authored_by}'"
            )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO skills
                    (id, name, content, authored_by, scope, version, archived,
                     health_score, health_score_reasons, created_at, updated_at,
                     last_retrieved, parent_skill_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill.id,
                    skill.name,
                    skill.content,
                    skill.authored_by,
                    json.dumps(skill.scope),
                    skill.version,
                    int(skill.archived),
                    skill.health_score,
                    json.dumps(skill.health_score_reasons),
                    skill.created_at.isoformat(),
                    skill.updated_at.isoformat(),
                    skill.last_retrieved.isoformat() if skill.last_retrieved else None,
                    skill.parent_skill_id,
                ),
            )
        return skill

    def get(self, skill_id: str) -> Optional[Skill]:
        """
        Returns the latest non-archived version of the skill.
        Falls back to oldest non-archived if all newer versions are archived.
        """
        with self._connect() as conn:
            # Get all non-archived versions, ordered newest first
            rows = conn.execute(
                """
                SELECT * FROM skills
                WHERE id = ? AND archived = 0
                ORDER BY created_at DESC
                """,
                (skill_id,),
            ).fetchall()

        if not rows:
            return None

        # Return the first (newest) non-archived row
        skill = _row_to_skill(rows[0])
        return skill

    def update(self, skill_id: str, content: str, new_version: str) -> Skill:
        """
        Create a new version of the skill with parent_skill_id=skill_id.
        """
        original = self.get(skill_id)
        if original is None:
            raise SkillStoreError(f"Skill '{skill_id}' not found")

        now = _utcnow()
        new_skill = Skill(
            id=skill_id,
            name=original.name,
            content=content,
            authored_by=original.authored_by,
            scope=original.scope,
            version=new_version,
            archived=False,
            health_score=1.0,
            health_score_reasons=[],
            created_at=now,
            updated_at=now,
            last_retrieved=None,
            parent_skill_id=skill_id,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO skills
                    (id, name, content, authored_by, scope, version, archived,
                     health_score, health_score_reasons, created_at, updated_at,
                     last_retrieved, parent_skill_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_skill.id,
                    new_skill.name,
                    new_skill.content,
                    new_skill.authored_by,
                    json.dumps(new_skill.scope),
                    new_skill.version,
                    int(new_skill.archived),
                    new_skill.health_score,
                    json.dumps(new_skill.health_score_reasons),
                    new_skill.created_at.isoformat(),
                    new_skill.updated_at.isoformat(),
                    None,
                    new_skill.parent_skill_id,
                ),
            )
        return new_skill

    def get_for_agent(self, agent_id: str) -> list[Skill]:
        """
        Returns non-archived skills where agent_id is in scope.
        Computes health_score based on staleness.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM skills WHERE archived = 0
                ORDER BY created_at DESC
                """
            ).fetchall()

        results: list[Skill] = []
        seen_ids: set[str] = set()
        for row in rows:
            skill = _row_to_skill(row)
            # Only return the latest version per skill id
            if skill.id in seen_ids:
                continue
            scope = skill.scope
            if agent_id in scope:
                seen_ids.add(skill.id)
                # Compute health score
                health_score, reasons = _compute_health(skill)
                skill.health_score = health_score
                skill.health_score_reasons = reasons
                results.append(skill)

        return results

    def mark_retrieved(self, skill_id: str) -> None:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE skills SET last_retrieved = ?
                WHERE id = ? AND archived = 0
                """,
                (now, skill_id),
            )

    def archive(self, skill_id: str, version: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE skills SET archived = 1
                WHERE id = ? AND version = ?
                """,
                (skill_id, version),
            )

    def list_stale(self, agent_id: str, days: int = 30) -> list[Skill]:
        """Returns non-archived skills for agent_id that are older than `days`."""
        skills = self.get_for_agent(agent_id)
        now = _utcnow()
        stale = []
        for skill in skills:
            created_at = skill.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age_days = (now - created_at).days
            if age_days > days:
                stale.append(skill)
        return stale
