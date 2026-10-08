from pathlib import Path

import pytest

from agentdb import AgentDB


PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def test_memory_workspace_creates_default_layout(tmp_path):
    workspace_root = tmp_path / "memory-home"

    db = AgentDB(memory_dir=workspace_root, permissions_path=PERMS)

    assert db.memory_dir == workspace_root.resolve()
    assert db.memory_config_path == (workspace_root / "config" / "memory.yaml").resolve()
    assert (workspace_root / "config").is_dir()
    assert (workspace_root / "database").is_dir()
    assert (workspace_root / "conversations").is_dir()
    assert (workspace_root / "embeddings").is_dir()
    assert (workspace_root / "reconciliation").is_dir()
    assert (workspace_root / "logs").is_dir()
    assert db._path == (workspace_root / "database" / "agentdb.sqlite").resolve()
    assert db.memory_config["profile"]["name"] == "budget_optimized"


def test_memory_workspace_env_precedence_prefers_memory_dir(tmp_path, monkeypatch):
    env_root = tmp_path / "env-memory"
    env_home = tmp_path / "agentdb-home"
    monkeypatch.setenv("AGENTDB_MEMORY_DIR", str(env_root))
    monkeypatch.setenv("AGENTDB_HOME", str(env_home))

    db = AgentDB(permissions_path=PERMS)

    assert db.memory_dir == env_root.resolve()


def test_explicit_path_overrides_workspace_database_path(tmp_path):
    workspace_root = tmp_path / "memory-home"
    explicit_path = tmp_path / "custom" / "agentdb.db"

    db = AgentDB(path=explicit_path, memory_dir=workspace_root, permissions_path=PERMS)

    assert db._path == explicit_path.resolve()
    assert db.memory_dir == workspace_root.resolve()
    assert db.memory_config_path == (workspace_root / "config" / "memory.yaml").resolve()


def test_invalid_full_storage_config_requires_tracked_directories(tmp_path):
    workspace_root = tmp_path / "memory-home"
    config_dir = workspace_root / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "memory.yaml"
    config_path.write_text(
        "\n".join(
            [
                "profile:",
                "  name: full_storage",
                "tracked_directories: []",
                "capture:",
                "  conversation_jsonl: true",
                "reconciliation:",
                "  triggers:",
                "    - conversation_started",
                "database:",
                "  mode: embedded",
                "  path: database/agentdb.sqlite",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="tracked_directories"):
        AgentDB(memory_dir=workspace_root, permissions_path=PERMS)


def test_dispatch_reconciliation_trigger_runs_registered_handler(tmp_path):
    db = AgentDB(memory_dir=tmp_path / "memory-home", permissions_path=PERMS)
    calls: list[dict[str, str]] = []

    def handler(**kwargs):
        calls.append(
            {
                "agent_id": kwargs["agent_id"],
                "session_id": kwargs["session_id"],
                "user_query": kwargs["user_query"],
            }
        )

    db.register_reconciliation_handler("custom_event", handler)
    count = db.dispatch_reconciliation_trigger(
        "custom_event",
        agent_id="research_agent",
        session_id="sess-trigger",
        user_query="hello",
    )

    assert count == 1
    assert calls == [
        {
            "agent_id": "research_agent",
            "session_id": "sess-trigger",
            "user_query": "hello",
        }
    ]