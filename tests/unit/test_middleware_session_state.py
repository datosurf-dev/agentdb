import pytest
from pathlib import Path
from agentdb import AgentDB
from agentdb.adapters.middleware import AgentDBMiddleware

PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"

@pytest.fixture
def mw(tmp_path):
    db = AgentDB(path=tmp_path / "t.db", permissions_path=str(PERMS))
    return AgentDBMiddleware(db=db, agent_id="test_agent")

def test_record_turn_context_appends_entry(mw):
    mw.record_turn_context(
        session_id="s1", turn_num=1,
        files_read=["README.md"], files_written=[], key_fact="read the readme"
    )
    log = mw._session_work_log["s1"]
    assert len(log) == 1
    assert log[0]["turn_num"] == 1
    assert log[0]["files_read"] == ["README.md"]
    assert log[0]["key_fact"] == "read the readme"

def test_record_turn_context_caps_at_20(mw):
    for i in range(25):
        mw.record_turn_context("s1", i, [], [], f"fact {i}")
    assert len(mw._session_work_log["s1"]) == 20
    # oldest dropped — first entry should be turn 5
    assert mw._session_work_log["s1"][0]["turn_num"] == 5

def test_report_directories_upserts(mw):
    mw.report_directories("s1", ["/tmp/foo", "/tmp/bar"])
    mw.report_directories("s1", ["/tmp/foo"])
    records = {r.path: r for r in mw._session_dir_state["s1"]}
    assert records["/tmp/foo"].access_count == 2
    assert records["/tmp/bar"].access_count == 1

def test_report_directories_caps_at_200(mw):
    dirs = [f"/tmp/dir{i}" for i in range(210)]
    mw.report_directories("s1", dirs)
    assert len(mw._session_dir_state["s1"]) == 200
