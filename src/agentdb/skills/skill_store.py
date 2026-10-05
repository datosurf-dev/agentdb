from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from ..core.models import Skill


class SkillStoreError(Exception):
    pass


class SkillConflict(SkillStoreError):
    """Raised when update() is called while multiple active versions exist.

    Resolve or archive the existing active versions before adding a new one.
    """
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

_CREATE_SKILL_LOCKS_TABLE = """
CREATE TABLE IF NOT EXISTS skill_locks (
    skill_id TEXT PRIMARY KEY,
    locked_until TEXT NOT NULL
)
"""

_MIGRATION_STMTS = [
    "ALTER TABLE skills ADD COLUMN origin TEXT NOT NULL DEFAULT 'human'",
    "ALTER TABLE skills ADD COLUMN validation_score REAL",
    "ALTER TABLE skills ADD COLUMN metadata TEXT",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_skill(row: sqlite3.Row) -> Skill:
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except (json.JSONDecodeError, TypeError):
        metadata = {}
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
        origin=row["origin"] or "human",
        validation_score=row["validation_score"],
        metadata=metadata,
    )


_ALPHA = 0.4


def _compute_health(skill: Skill) -> tuple[float, list[str]]:
    """Compute health score based on staleness and optional validation score."""
    now = _utcnow()
    reference = skill.last_retrieved or skill.created_at
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_days = (now - reference).days
    usage_score = 1.0
    reasons: list[str] = []
    if age_days > 30:
        usage_score = max(0.1, 1.0 - (age_days - 30) * 0.03)
        reasons.append("staleness")
    if skill.validation_score is not None:
        health = _ALPHA * skill.validation_score + (1 - _ALPHA) * usage_score
        reasons.append("validation_blended")
    else:
        health = usage_score
    return health, reasons


class SkillStore:
    def __init__(
        self,
        db_path: Any,
        on_stale_crossing: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self.on_stale_crossing = on_stale_crossing
        with self._connect() as conn:
            conn.execute(_CREATE_SKILLS_TABLE)
            conn.execute(_CREATE_SKILL_LOCKS_TABLE)
            for stmt in _MIGRATION_STMTS:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already exists

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
                     last_retrieved, parent_skill_id,
                     origin, validation_score, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    skill.origin,
                    skill.validation_score,
                    json.dumps(skill.metadata),
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

    def update(
        self,
        skill_id: str,
        content: str,
        new_version: str,
        origin: str = "human",
        validation_score: Optional[float] = None,
        metadata: Optional[dict] = None,
    ) -> Skill:
        """
        Create a new version of the skill with parent_skill_id=skill_id.
        Raises SkillConflict if more than one active version already exists.
        The count check and INSERT run in the same transaction to avoid a
        TOCTOU window between the guard and the write.
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
            origin=origin,
            validation_score=validation_score,
            metadata=metadata or {},
        )
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM skills WHERE id = ? AND archived = 0",
                (skill_id,),
            ).fetchone()[0]
            if count > 1:
                raise SkillConflict(
                    f"Skill '{skill_id}' has unresolved active versions — "
                    f"archive or reconcile existing versions before updating"
                )
            conn.execute(
                """
                INSERT INTO skills
                    (id, name, content, authored_by, scope, version, archived,
                     health_score, health_score_reasons, created_at, updated_at,
                     last_retrieved, parent_skill_id,
                     origin, validation_score, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    new_skill.origin,
                    new_skill.validation_score,
                    json.dumps(new_skill.metadata),
                ),
            )
        return new_skill

    def get_for_agent(self, agent_id: str) -> list[Skill]:
        """
        Returns non-archived skills where agent_id is in scope.
        Computes health_score based on staleness. Fires on_stale_crossing
        callback exactly once when health first crosses below 0.6.
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
            if skill.id in seen_ids:
                continue
            if agent_id not in skill.scope:
                continue
            seen_ids.add(skill.id)

            stored_health = skill.health_score  # from DB — reflects last persisted score
            new_health, reasons = _compute_health(skill)
            skill.health_score = new_health
            skill.health_score_reasons = reasons

            # Detect first-time crossing below 0.6: persist so it won't re-fire
            if stored_health >= 0.6 and new_health < 0.6:
                self._persist_health_score(skill.id, skill.version, new_health)
                if self.on_stale_crossing is not None:
                    self.on_stale_crossing(skill.id)

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

    def _persist_health_score(self, skill_id: str, version: str, health_score: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE skills SET health_score = ? WHERE id = ? AND version = ?",
                (health_score, skill_id, version),
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

    def list_all_active_ids(self) -> list[str]:
        """Return composite IDs 'id:version' for all non-archived skills."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, version FROM skills WHERE archived = 0"
            ).fetchall()
        return [f"{row['id']}:{row['version']}" for row in rows]

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

    def search(self, query: str, agent_id: Optional[str] = None) -> list[Skill]:
        """Case-insensitive keyword search across skill name and content.

        If agent_id is given, only returns skills where agent_id is in scope.
        Returns [] for an empty query.
        """
        words = [w.lower() for w in query.split() if w]
        if not words:
            return []

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skills WHERE archived = 0 ORDER BY created_at DESC"
            ).fetchall()

        seen_ids: set[str] = set()
        results: list[Skill] = []
        for row in rows:
            skill = _row_to_skill(row)
            if skill.id in seen_ids:
                continue
            if agent_id is not None and agent_id not in skill.scope:
                continue
            name_lower = skill.name.lower()
            content_lower = skill.content.lower()
            for word in words:
                if word in name_lower or word in content_lower:
                    seen_ids.add(skill.id)
                    new_health, reasons = _compute_health(skill)
                    skill.health_score = new_health
                    skill.health_score_reasons = reasons
                    results.append(skill)
                    break
        return results
