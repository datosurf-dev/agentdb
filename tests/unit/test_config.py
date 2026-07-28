"""Profile configuration resolution: single `profile:` root key, strict validation."""
from __future__ import annotations

import pytest
import yaml


def _write_yaml(path, data):
    path.write_text(yaml.dump(data), encoding="utf-8")


def _mem_dir(tmp_path, config_data):
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    _write_yaml(d / "config" / "memory.yaml", config_data)
    return d


def test_budget_optimized_defaults_lifecycle(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {"name": "budget_optimized"}, "tracked_directories": []}))
    assert db.lifecycle.STALENESS_DAYS == 90
    assert db.lifecycle.ARCHIVAL_THRESHOLD == pytest.approx(0.10)
    assert db.lifecycle.REINFORCE_RATE == pytest.approx(0.10)


def test_full_storage_profile_retrieve_limit(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(tmp_path, {"profile": {"name": "full_storage"}, "tracked_directories": [str(tmp_path)]}),
    )
    assert db.config["retrieval"]["retrieve_limit"] == 20
    assert db.config["injection"]["memory_budget_chars"] == 12000


def test_override_section_beats_profile_default(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {"profile": {"name": "budget_optimized", "lifecycle": {"staleness_days": 30}}, "tracked_directories": []},
        ),
    )
    assert db.lifecycle.STALENESS_DAYS == 30
    assert db.lifecycle.ARCHIVAL_THRESHOLD == pytest.approx(0.10)


def test_ranker_uses_config_weights(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {
                "profile": {
                    "name": "budget_optimized",
                    "retrieval": {
                        "ranker_weights": {"provenance": 0.50, "recency": 0.20, "confidence": 0.20, "causal": 0.10, "semantic": 0.0},
                    },
                },
                "tracked_directories": [],
            },
        ),
    )
    assert db._ranker.w_provenance == pytest.approx(0.50)
    assert db._ranker.w_recency == pytest.approx(0.20)


def test_no_memory_dir_uses_hardcoded_defaults(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(path=tmp_path / "test.db")
    assert db.lifecycle.STALENESS_DAYS == 90
    assert db._ranker.w_provenance == pytest.approx(0.35)


def test_config_property_returns_resolved_dict_with_injection_key(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {"name": "budget_optimized"}, "tracked_directories": []}))
    cfg = db.config
    assert cfg["lifecycle"]["staleness_days"] == 90
    assert cfg["retrieval"]["retrieve_limit"] == 5
    assert cfg["injection"]["context_token_limit"] == 1000
    assert cfg["injection"]["memory_header"].startswith("Governed memory context")


def test_research_profile_retrieve_limit(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {"name": "research"}, "tracked_directories": [str(tmp_path)]}))
    assert db.config["retrieval"]["retrieve_limit"] == 50
    assert db.config["injection"]["memory_budget_chars"] == 24000


def test_custom_override_does_not_need_a_registry(tmp_path):
    """No profiles: registry exists -- override sections directly under
    profile: are an implicit custom profile."""
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {"profile": {"name": "research", "retrieval": {"retrieve_limit": 30}}, "tracked_directories": [str(tmp_path)]},
        ),
    )
    assert db.config["retrieval"]["retrieve_limit"] == 30
    assert db.config["lifecycle"]["staleness_days"] == 180  # research's other fields untouched


def test_missing_profile_name_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError, match="profile.name"):
        AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {}, "tracked_directories": []}))


def test_unrecognized_profile_name_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError, match="nonexistent"):
        AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {"name": "nonexistent"}, "tracked_directories": []}))


def test_unknown_override_section_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError, match="retrievel"):
        AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {"name": "budget_optimized", "retrievel": {}}, "tracked_directories": []}))


def test_unknown_field_in_override_section_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError, match="retrieve_limitt"):
        AgentDB(
            memory_dir=_mem_dir(
                tmp_path,
                {"profile": {"name": "budget_optimized", "retrieval": {"retrieve_limitt": 10}}, "tracked_directories": []},
            ),
        )


def test_wrong_type_in_override_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError, match="retrieve_limit"):
        AgentDB(
            memory_dir=_mem_dir(
                tmp_path,
                {"profile": {"name": "budget_optimized", "retrieval": {"retrieve_limit": "five"}}, "tracked_directories": []},
            ),
        )


def test_empty_memory_yaml_raises_configuration_error_not_silent_fallback(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    workspace_root = tmp_path / "memory-home"
    config_dir = workspace_root / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "memory.yaml").write_text("", encoding="utf-8")  # blank file, parses to None

    with pytest.raises(ConfigurationError, match="profile.name"):
        AgentDB(memory_dir=workspace_root)


def test_legacy_root_level_capture_key_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError, match="capture"):
        AgentDB(
            memory_dir=_mem_dir(
                tmp_path,
                {"profile": {"name": "budget_optimized"}, "capture": {"conversation_jsonl": True}, "tracked_directories": []},
            ),
        )


def test_profile_as_bare_scalar_raises_configuration_error_not_attribute_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError):
        AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": "research", "tracked_directories": [str(tmp_path)]}))


def test_empty_override_section_raises_configuration_error_not_attribute_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError):
        AgentDB(
            memory_dir=_mem_dir(
                tmp_path,
                {"profile": {"name": "budget_optimized", "retrieval": None}, "tracked_directories": []},
            ),
        )


def test_unknown_ranker_weight_field_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError, match="provenence"):
        AgentDB(
            memory_dir=_mem_dir(
                tmp_path,
                {
                    "profile": {"name": "budget_optimized", "retrieval": {"ranker_weights": {"provenence": 0.9}}},
                    "tracked_directories": [],
                },
            ),
        )
