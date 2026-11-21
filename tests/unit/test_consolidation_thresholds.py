"""Tests for configurable consolidation thresholds (spec §3).

`profile.consolidation` exposes enabled/min_sessions/days/min_cluster_size/
since_hours in memory.yaml. Previously these were hard-coded defaults.
"""
from pathlib import Path

import pytest

from agentdb import AgentDB


def _db_with_consolidation(tmp_path, consolidation_yaml: str):
    ws = tmp_path / "mem"
    cfg = ws / "config"
    cfg.mkdir(parents=True)
    (cfg / "memory.yaml").write_text(
        "profile:\n  name: budget_optimized\n  consolidation:\n" + consolidation_yaml,
        encoding="utf-8",
    )
    return AgentDB(memory_dir=ws)


def test_config_resolves_into_db_config(tmp_path):
    db = _db_with_consolidation(
        tmp_path,
        "    enabled: true\n    min_sessions: 3\n    days: 1\n"
        "    min_cluster_size: 2\n    since_hours: 48\n",
    )
    c = db.config["consolidation"]
    assert c["enabled"] is True
    assert c["min_sessions"] == 3
    assert c["days"] == 1
    assert c["since_hours"] == 48


def test_unknown_consolidation_field_rejected(tmp_path):
    ws = tmp_path / "mem"
    cfg = ws / "config"
    cfg.mkdir(parents=True)
    (cfg / "memory.yaml").write_text(
        "profile:\n  name: budget_optimized\n  consolidation:\n    bogus: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception) as ctx:
        AgentDB(memory_dir=ws)
    assert "consolidation" in str(ctx.value).lower()


def test_defaults_match_previous_hardcoded_values(tmp_path):
    db = AgentDB(path=tmp_path / "t.db")  # no memory_dir -> built-in defaults
    c = db.config["consolidation"]
    assert c["enabled"] is True
    assert c["min_sessions"] == 10
    assert c["days"] == 7
    assert c["min_cluster_size"] == 2
    assert c["since_hours"] == 24


def test_disabled_blocks_auto_consolidation(tmp_path):
    db = _db_with_consolidation(
        tmp_path, "    enabled: false\n    min_sessions: 1\n"
    )
    # Seed two clusterable records for agent a1.
    db.write("memory/analytics-schema-note", "analytics schema layout", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "analytics schema layout confirmed", origin="agent_inferred", agent_id="a1")
    # Fire enough conversation_started events to trigger if enabled.
    for i in range(3):
        db._handle_conversation_started_metadata(session_id=f"s{i}", agent_id="a1")
    # No canonical / no archival because consolidation is disabled.
    assert all(not r.consolidated for r in db._store.list_all() if r.agent_id == "a1")


def test_min_sessions_drives_auto_consolidation(tmp_path):
    db = _db_with_consolidation(
        tmp_path, "    enabled: true\n    min_sessions: 2\n    days: 0\n"
    )
    db.write("memory/analytics-schema-note", "analytics schema layout", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "analytics schema layout confirmed", origin="agent_inferred", agent_id="a1")
    # Two conversation_started events -> reaches min_sessions=2 and (days=0) runs.
    for i in range(2):
        db._handle_conversation_started_metadata(session_id=f"s{i}", agent_id="a1")
    # A derived canonical now exists for a1.
    canon = [r for r in db._store.list_all()
             if r.agent_id == "a1" and getattr(r, "kind", "source") == "derived_canonical"]
    assert len(canon) == 1


def test_manual_consolidate_uses_config_since_hours(tmp_path):
    # since_hours huge => nothing older than cutoff qualifies => no clusters.
    db = _db_with_consolidation(tmp_path, "    since_hours: 100000\n")
    db.write("memory/analytics-schema-note", "analytics schema layout", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "analytics schema layout confirmed", origin="agent_inferred", agent_id="a1")
    # No since_hours passed -> pulls 100000 from config -> both records too new.
    result = db.consolidate("a1")
    assert result.clusters_found == 0
