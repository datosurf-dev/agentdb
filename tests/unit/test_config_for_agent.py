from pathlib import Path

import pytest
import yaml


def _mem_dir(tmp_path, config_data):
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    (d / "config" / "memory.yaml").write_text(yaml.dump(config_data), encoding="utf-8")
    return d


def test_agent_not_listed_gets_global_config_and_not_isolated(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {"name": "research"}, "tracked_directories": [str(tmp_path)]}))

    cfg = db.config_for("some_agent")

    assert cfg["isolate_by_agent"] is False
    assert cfg["retrieval"]["retrieve_limit"] == 50  # research's global value, untouched


def test_agent_listed_is_isolated_and_can_override_fields(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {
                "profile": {
                    "name": "research",
                    "agents": {"telegram_assistant_personal": {"retrieval": {"retrieve_limit": 30}}},
                },
                "tracked_directories": [str(tmp_path)],
            },
        ),
    )

    cfg = db.config_for("telegram_assistant_personal")

    assert cfg["isolate_by_agent"] is True
    assert cfg["retrieval"]["retrieve_limit"] == 30
    assert cfg["lifecycle"]["staleness_days"] == 180  # research's other fields, untouched


def test_agent_can_switch_to_a_different_base_profile(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {
                "profile": {"name": "research", "agents": {"telegram_assistant_work": {"name": "full_storage"}}},
                "tracked_directories": [str(tmp_path)],
            },
        ),
    )

    cfg = db.config_for("telegram_assistant_work")

    assert cfg["isolate_by_agent"] is True
    assert cfg["retrieval"]["retrieve_limit"] == 20  # full_storage's value, not research's 50


def test_agent_block_unknown_field_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {
                "profile": {"name": "research", "agents": {"a1": {"retrieval": {"retrieve_limitt": 10}}}},
                "tracked_directories": [str(tmp_path)],
            },
        ),
    )

    with pytest.raises(ConfigurationError, match="retrieve_limitt"):
        db.config_for("a1")
