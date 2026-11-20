from __future__ import annotations

import json

from ..core.models import MemoryRecord, Skill


def _embed_text_for_record(r: MemoryRecord) -> str:
    """Build the text representation of a memory record for embedding."""
    key_readable = r.key.replace("/", " ").strip()
    if isinstance(r.value, (dict, list)):
        value_str = json.dumps(r.value)[:2000]
    else:
        value_str = str(r.value)[:2000]
    return f"{key_readable}: {value_str}"


def _embed_text_for_skill(skill: Skill) -> str:
    """Build the text representation of a skill for embedding."""
    return f"{skill.name}: {skill.content}"[:2000]
