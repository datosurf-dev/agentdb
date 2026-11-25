"""Regression tests for workspace (`working_dirs`) population (spec §7).

`working_dirs` is the only workspace linkage for a memory. It was empty on every
record because (a) `write_compaction_summary()` never forwarded
it, and (b) middleware persist paths only had dirs if `prepare_turn(working_dirs=)`
ran first in the same instance. Now: summary accepts and forwards
`working_dirs`, and finalize/end_session accept an explicit `working_dirs` that
falls back to the remembered ones.
"""
from pathlib import Path

import pytest

from agentdb import AgentDB
from agentdb.adapters.middleware import AgentDBMiddleware

_AGENT = "test_agent"

_PERMS_YAML = f"""\
default_policy: deny
agents:
  {_AGENT}:
    can_write:
      - "memory/working/"
      - "memory/episodic/"
      - "memory/compaction/"
    can_read:
      - "memory/working/"
      - "memory/episodic/"
      - "memory/compaction/"
"""


@pytest.fixture
def perms(tmp_path):
    p = tmp_path / "perms.yaml"
    p.write_text(_PERMS_YAML, encoding="utf-8")
    return str(p)


def _db(tmp_path, perms):
    return AgentDB(path=tmp_path / "t.db", permissions_path=perms)


def _store_record_dirs(db, key_substr):
    for r in db._store.list_all():
        if key_substr in r.key:
            return r.working_dirs
    return None


def test_write_compaction_summary_forwards_working_dirs(tmp_path, perms):
    db = _db(tmp_path, perms)
    db.write_compaction_summary(
        agent_id=_AGENT, session_id="s1", summary="did work", working_dirs=["/tmp/ws"]
    )
    assert _store_record_dirs(db, "memory/compaction/summary/s1") == [
        str(Path("/tmp/ws").resolve())
    ]


def test_finalize_turn_persists_explicit_working_dirs(tmp_path, perms):
    db = _db(tmp_path, perms)
    mw = AgentDBMiddleware(db=db, agent_id=_AGENT)
    # Force episodic persistence regardless of profile.
    mw._resolved["capture"]["auto_persist_response"] = True

    mw.finalize_turn(session_id="s1", model_response="hello", working_dirs=["/tmp/ws"])
    record = next(
        r for r in db._store.list_all()
        if r.session_id == "s1" and "memory/episodic/" in r.key
    )
    assert record.working_dirs == [str(Path("/tmp/ws").resolve())]


def test_finalize_turn_falls_back_to_prepare_dirs(tmp_path, perms):
    db = _db(tmp_path, perms)
    mw = AgentDBMiddleware(db=db, agent_id=_AGENT)
    mw._resolved["capture"]["auto_persist_response"] = True

    mw.prepare_turn(user_query="q", session_id="s1", working_dirs=["/tmp/ws"])
    mw.finalize_turn(session_id="s1", model_response="hi")  # no explicit dirs
    record = next(
        r for r in db._store.list_all()
        if r.session_id == "s1" and "memory/episodic/" in r.key
    )
    assert record.working_dirs == [str(Path("/tmp/ws").resolve())]


@pytest.mark.asyncio
async def test_end_session_records_working_dirs_in_the_summary_line(tmp_path, perms):
    """The checkpoint record is gone; the session-summary line carries the dirs."""
    import json

    db = _db(tmp_path, perms)
    mw = AgentDBMiddleware(db=db, agent_id=_AGENT)
    jsonl = tmp_path / "summary.jsonl"
    await mw.end_session("s1", str(jsonl), working_dirs=["/tmp/ws"])

    last = json.loads(jsonl.read_text().strip().splitlines()[-1])
    assert last["working_dirs"] == [str(Path("/tmp/ws").resolve())]
