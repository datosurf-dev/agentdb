from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def compute_file_sha256(path: Any) -> str:
    """Return the SHA-256 hex digest (64 chars) of the file at path."""
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class FileObservation:
    path: str
    sha256: str
    size_bytes: int
    captured_at: str
    modified_at: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "captured_at": self.captured_at,
            "modified_at": self.modified_at,
        }


def observe_file(path: Any, base_dir: Optional[Any] = None) -> FileObservation:
    """
    Observe a single file and return a FileObservation.

    path: the file to observe (absolute or relative)
    base_dir: if provided, the returned .path is relative to base_dir
    """
    p = Path(path)
    sha = compute_file_sha256(p)
    stat = p.stat()
    size_bytes = stat.st_size
    captured_at = _utcnow().isoformat()
    modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()

    if base_dir is not None:
        try:
            rel_path = str(p.relative_to(Path(base_dir)))
        except ValueError:
            rel_path = str(p)
    else:
        rel_path = str(p)

    return FileObservation(
        path=rel_path,
        sha256=sha,
        size_bytes=size_bytes,
        captured_at=captured_at,
        modified_at=modified_at,
    )


def observe_files(paths: list) -> list[dict]:
    """Observe multiple files and return a list of to_dict() results."""
    results = []
    for p in paths:
        obs = observe_file(p)
        results.append(obs.to_dict())
    return results
