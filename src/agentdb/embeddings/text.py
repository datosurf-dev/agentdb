from __future__ import annotations

import json

from ..core.models import MemoryRecord


def _embed_text_for_record(r: MemoryRecord) -> str:
    """Build the text representation of a memory record for embedding."""
    key_readable = r.key.replace("/", " ").strip()
    if isinstance(r.value, (dict, list)):
        value_str = json.dumps(r.value)[:2000]
    else:
        value_str = str(r.value)[:2000]
    return f"{key_readable}: {value_str}"
