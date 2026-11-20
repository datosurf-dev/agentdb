"""ADB-12: in-memory database initialization must work and not create a file."""
from agentdb import AgentDB


def test_memory_path_uses_in_memory_db_without_file(tmp_path, monkeypatch):
    # Run from an empty cwd so any accidental file creation is visible.
    monkeypatch.chdir(tmp_path)
    db = AgentDB(path=":memory:")
    assert str(db._path) == ":memory:"

    db.write("memory/x", "y", origin="agent_inferred", agent_id="a1")
    assert len(db.retrieve("x", agent_id="a1").records) == 1
    assert db.system.agentdb_stats()["total_memories"] == 1

    # No literal ':memory:' file may be created.
    assert not (tmp_path / ":memory:").exists()


def test_memory_path_shares_process_wide_db(tmp_path, monkeypatch):
    """`:memory:` uses a process-wide shared-cache DB so the multiple per-store
    connections AgentDB opens see the same tables. Two in-memory AgentDB
    instances in the same process therefore share that DB (documented)."""
    monkeypatch.chdir(tmp_path)
    db1 = AgentDB(path=":memory:")
    db1.write("memory/only-in-db1", "v", origin="agent_inferred", agent_id="a1")
    db2 = AgentDB(path=":memory:")
    assert db2.read("memory/only-in-db1") is not None
