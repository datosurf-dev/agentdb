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
    assert cfg["retrieval"]["retrieve_limit"] == 200  # full_storage's value, not research's 50


def test_agent_block_unknown_field_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError

    # Eager validation (see test_bad_agent_block_raises_at_construction_not_lazily
    # below) means this now raises during construction, not lazily on
    # config_for() -- so the AgentDB(...) call itself must be inside the
    # `with pytest.raises` block.
    with pytest.raises(ConfigurationError, match="retrieve_limitt"):
        AgentDB(
            memory_dir=_mem_dir(
                tmp_path,
                {
                    "profile": {"name": "research", "agents": {"a1": {"retrieval": {"retrieve_limitt": 10}}}},
                    "tracked_directories": [str(tmp_path)],
                },
            ),
        )


def test_agent_listed_with_bare_yaml_key_is_still_isolated(tmp_path):
    """Regression: `agents:\n  coder_agent:` (a bare key, parses to None in
    YAML, not {}) must isolate exactly like an explicit `coder_agent: {}}`."""
    from agentdb.interface.client import AgentDB
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    (d / "config" / "memory.yaml").write_text(
        "\n".join(["profile:", "  name: research", "  agents:", "    coder_agent:"]),
        encoding="utf-8",
    )
    db = AgentDB(memory_dir=d)

    cfg = db.config_for("coder_agent")

    assert cfg["isolate_by_agent"] is True


def test_agent_genuinely_absent_from_a_real_agents_map_is_not_isolated(tmp_path):
    """The untested case the bug hid in: a real agents: map exists, but this
    specific agent isn't in it -- distinct from 'no memory_dir at all'."""
    from agentdb.interface.client import AgentDB
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    (d / "config" / "memory.yaml").write_text(
        "\n".join(["profile:", "  name: research", "  agents:", "    coder_agent: {}"]),
        encoding="utf-8",
    )
    db = AgentDB(memory_dir=d)

    cfg = db.config_for("some_other_agent")

    assert cfg["isolate_by_agent"] is False


def test_bad_agent_block_raises_at_construction_not_lazily(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    (d / "config" / "memory.yaml").write_text(
        "\n".join(["profile:", "  name: research", "  agents:", "    a1:", "      retrieval:", "        retrieve_limitt: 10"]),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="retrieve_limitt"):
        AgentDB(memory_dir=d)
