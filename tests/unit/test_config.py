"""Task 10D: centralized memory.yaml configuration wiring."""
from __future__ import annotations

import pytest
import yaml


def _write_yaml(path, data):
    path.write_text(yaml.dump(data), encoding="utf-8")


def _mem_dir(tmp_path, config_data):
    """Create a memory_dir with the given config dict and return its path."""
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    _write_yaml(d / "config" / "memory.yaml", config_data)
    return d


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_budget_optimized_defaults_lifecycle(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(memory_dir=_mem_dir(tmp_path, {"mode": "budget_optimized", "tracked_directories": []}))
    assert db.lifecycle.STALENESS_DAYS == 90
    assert db.lifecycle.ARCHIVAL_THRESHOLD == pytest.approx(0.10)
    assert db.lifecycle.REINFORCE_RATE == pytest.approx(0.10)


def test_full_storage_mode_retrieve_limit(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {"mode": "full_storage", "tracked_directories": [str(tmp_path)]},
        )
    )
    assert db.config["retrieval"]["retrieve_limit"] == 20
    assert db.config["retrieval"]["memory_budget_chars"] == 12000


def test_yaml_field_override_beats_mode_default(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {
                "mode": "budget_optimized",
                "tracked_directories": [],
                "lifecycle": {"staleness_days": 30},
            },
        )
    )
    assert db.lifecycle.STALENESS_DAYS == 30
    assert db.lifecycle.ARCHIVAL_THRESHOLD == pytest.approx(0.10)


def test_ranker_uses_config_weights(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {
                "mode": "budget_optimized",
                "tracked_directories": [],
                "retrieval": {
                    "ranker_weights": {
                        "provenance": 0.50,
                        "recency": 0.20,
                        "confidence": 0.20,
                        "causal": 0.10,
                        "semantic": 0.0,
                    }
                },
            },
        )
    )
    assert db._ranker.w_provenance == pytest.approx(0.50)
    assert db._ranker.w_recency == pytest.approx(0.20)
    assert db._ranker.w_confidence == pytest.approx(0.20)
    assert db._ranker.w_causal == pytest.approx(0.10)


def test_no_memory_dir_uses_hardcoded_defaults(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(path=tmp_path / "test.db")
    assert db.lifecycle.STALENESS_DAYS == 90
    assert db.lifecycle.ARCHIVAL_THRESHOLD == pytest.approx(0.10)
    assert db._ranker.w_provenance == pytest.approx(0.35)


def test_config_property_returns_resolved_dict(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(memory_dir=_mem_dir(tmp_path, {"mode": "budget_optimized", "tracked_directories": []}))
    cfg = db.config
    assert cfg["lifecycle"]["staleness_days"] == 90
    assert cfg["retrieval"]["retrieve_limit"] == 5
    assert cfg["retrieval"]["ranker_weights"]["provenance"] == pytest.approx(0.35)
