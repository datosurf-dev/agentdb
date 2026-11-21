"""Tests for opt-in directory tracking (spec §6).

Directory snapshots are write-only operational breadcrumbs (not read by
retrieval/ranking/consolidation). They are captured only when
`capture.track_directories` is enabled; the default is OFF.
"""
from pathlib import Path

from agentdb import AgentDB
from agentdb.adapters.middleware import AgentDBMiddleware

_AGENT = "test_agent"
_PERMS = f"""\
default_policy: deny
agents:
  {_AGENT}:
    can_write:
      - "memory/episodic/"
      - "memory/working/"
    can_read:
      - "memory/episodic/"
      - "memory/working/"
"""


def _db(tmp_path, extra_capture=None):
    perms = tmp_path / "perms.yaml"
    perms.write_text(_PERMS, encoding="utf-8")
    ws = tmp_path / "mem"
    cfg = ws / "config"
    cfg.mkdir(parents=True)
    lines = ["profile:", "  name: budget_optimized", "  capture:", "    auto_persist_response: true"]
    if extra_capture:
        lines += extra_capture
    (cfg / "memory.yaml").write_text("\n".join(lines), encoding="utf-8")
    return AgentDB(memory_dir=ws, permissions_path=str(perms))


def _tracked_dir(tmp_path):
    d = tmp_path / "skills"
    d.mkdir(exist_ok=True)
    (d / "a.md").write_text("x", encoding="utf-8")
    return d


def _snapshots(db):
    return [r for r in db._store.list_all() if "/directory_tracking/" in r.key]


def test_default_off_writes_no_snapshots(tmp_path):
    db = _db(tmp_path)
    mw = AgentDBMiddleware(db=db, agent_id=_AGENT)
    mw.finalize_turn(
        session_id="s1", model_response="done",
        tracked_directories=[str(_tracked_dir(tmp_path))],
    )
    assert _snapshots(db) == []


def test_enabled_writes_snapshots(tmp_path):
    db = _db(tmp_path, extra_capture=["    track_directories: true"])
    mw = AgentDBMiddleware(db=db, agent_id=_AGENT)
    mw.finalize_turn(
        session_id="s1", model_response="done",
        tracked_directories=[str(_tracked_dir(tmp_path))],
    )
    snaps = _snapshots(db)
    assert len(snaps) == 1
    assert snaps[0].value["directory_snapshot"]["exists"] is True


def test_explicit_directory_snapshots_honored_even_when_off(tmp_path):
    # Caller-supplied directory_snapshots are still persisted regardless of flag.
    db = _db(tmp_path)  # track_directories defaults off
    mw = AgentDBMiddleware(db=db, agent_id=_AGENT)
    mw.finalize_turn(
        session_id="s1", model_response="done",
        directory_snapshots=[{"directory": "given", "exists": True, "file_count": 0, "entries": []}],
    )
    snaps = _snapshots(db)
    assert len(snaps) == 1
    assert snaps[0].value["directory_snapshot"]["directory"] == "given"
