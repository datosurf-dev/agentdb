from pathlib import Path

from agentdb.core.directory_tracking import collect_directory_snapshots


def test_collect_directory_snapshots_tracks_entries(tmp_path):
    root = tmp_path / "repo"
    skills = root / ".claude" / "skills"
    docs = root / "docs"
    skills.mkdir(parents=True)
    docs.mkdir(parents=True)
    (skills / "skill.md").write_text("skill", encoding="utf-8")
    (docs / "guide.md").write_text("guide", encoding="utf-8")

    snapshots = collect_directory_snapshots(
        [".claude/skills", "docs"],
        base_dir=root,
        max_files_per_directory=10,
    )

    assert len(snapshots) == 2
    assert snapshots[0]["exists"] is True
    assert snapshots[1]["exists"] is True
    assert snapshots[0]["file_count"] == 1
    assert snapshots[1]["file_count"] == 1
    tracked_paths = {
        entry["path"]
        for snap in snapshots
        for entry in snap["entries"]
    }
    assert ".claude/skills/skill.md" in tracked_paths
    assert "docs/guide.md" in tracked_paths


def test_collect_directory_snapshots_marks_missing_directory(tmp_path):
    snapshots = collect_directory_snapshots(["missing-dir"], base_dir=tmp_path)
    assert len(snapshots) == 1
    assert snapshots[0]["exists"] is False
    assert snapshots[0]["file_count"] == 0
    assert snapshots[0]["entries"] == []
