from __future__ import annotations

from pathlib import Path
from typing import Any


def collect_directory_snapshots(
    directories: list[str],
    base_dir: Any = ".",
    max_files_per_directory: int = 200,
) -> list[dict]:
    """
    Collect a snapshot of each directory (relative to base_dir).

    Returns a list of dicts:
        {directory, exists, file_count, entries: [{path, ...}]}

    paths in entries are relative to base_dir.
    If a directory doesn't exist: {directory, exists=False, file_count=0, entries=[]}
    """
    base = Path(base_dir)
    results: list[dict] = []

    for directory in directories:
        full_path = base / directory
        if not full_path.exists():
            results.append(
                {
                    "directory": directory,
                    "exists": False,
                    "file_count": 0,
                    "entries": [],
                }
            )
            continue

        entries: list[dict] = []
        count = 0
        for item in sorted(full_path.rglob("*")):
            if item.is_file():
                if count >= max_files_per_directory:
                    break
                try:
                    rel = item.relative_to(base)
                    entries.append({"path": str(rel)})
                except ValueError:
                    entries.append({"path": str(item)})
                count += 1

        results.append(
            {
                "directory": directory,
                "exists": True,
                "file_count": count,
                "entries": entries,
            }
        )

    return results
