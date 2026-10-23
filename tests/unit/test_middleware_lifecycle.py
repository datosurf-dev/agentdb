import asyncio
import json
import pytest
from pathlib import Path
from agentdb import AgentDB
from agentdb.adapters.middleware import AgentDBMiddleware
from agentdb.adapters.capabilities import LOCAL_CAPABILITIES


@pytest.fixture
def mw(tmp_path):
    db = AgentDB(path=tmp_path / "t.db")
    return AgentDBMiddleware(db=db, agent_id="test_agent"), db


def test_elevate_directory_writes_to_agentdb(mw, tmp_path):
    middleware, db = mw
    middleware.report_directories("s1", ["/tmp/feature"])
    record = middleware.elevate_directory("s1", "/tmp/feature")
    assert record.key == "workspace.dir./tmp/feature"
    assert record.confidence == 0.8
    # session state reflects elevation
    elevated = [r for r in middleware._session_dir_state["s1"] if r.elevated]
    assert any(r.path == "/tmp/feature" for r in elevated)


def test_write_session_summary_creates_file_if_missing(mw, tmp_path):
    middleware, db = mw
    middleware.record_turn_context("s1", 1, ["a.py"], [], "read a.py")
    jsonl_path = str(tmp_path / "session.jsonl")
    middleware.write_session_summary("s1", jsonl_path)
    assert Path(jsonl_path).exists()
    lines = Path(jsonl_path).read_text().strip().split("\n")
    last = json.loads(lines[-1])
    assert last["type"] == "session_summary"
    assert last["session_id"] == "s1"
    assert last["agent_id"] == "test_agent"
    assert "agentdb_checkpoint_key" in last
    assert "last_work_context" in last


def test_write_session_summary_appends_to_existing(mw, tmp_path):
    middleware, db = mw
    jsonl_path = str(tmp_path / "session.jsonl")
    Path(jsonl_path).write_text('{"type":"prior_line"}\n')
    middleware.write_session_summary("s1", jsonl_path)
    lines = Path(jsonl_path).read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "prior_line"
    assert json.loads(lines[1])["type"] == "session_summary"


def test_end_session_clears_session_state(mw, tmp_path):
    middleware, db = mw
    middleware.report_directories("s1", ["/tmp/foo"])
    middleware.record_turn_context("s1", 1, [], [], "fact")
    jsonl_path = str(tmp_path / "session.jsonl")
    asyncio.run(middleware.end_session("s1", jsonl_path))
    assert "s1" not in middleware._session_dir_state
    assert "s1" not in middleware._session_work_log
