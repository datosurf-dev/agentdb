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


# -- repository identity is derived at write time ---------------------------
#
# Before this, `write()` accepted `repository_id` but nothing ever passed it, so
# every record was written unscoped. `retrieve(scope={"repository_id": ...})`
# excludes only on an explicit mismatch, so an unscoped record was an immortal
# candidate: it could never be filtered out, whatever the session was working on.


def test_finalize_turn_derives_repository_id(tmp_path, perms):
    """A turn's record carries the repository its working dirs point at."""
    import asyncio

    db = _db(tmp_path, perms)
    mw = AgentDBMiddleware(db=db, agent_id=_AGENT)
    mw.prepare_turn(
        user_query="work in the komper repo", session_id="s1",
        working_dirs=["/tmp/agent-sessions/slack/T1/workspace", "/tmp/repos/komper"],
    )
    mw._resolved["capture"]["auto_persist_response"] = True
    asyncio.run(mw.finalize_turn_async(
        session_id="s1", model_response="done. " * 8, message_id="m1",
        outcome_type="task_complete", outcome_value=1.0,
    ))

    record = next(
        r for r in db._store.list_all()
        if r.session_id == "s1" and "memory/episodic/" in r.key
    )
    assert record.repository_id == "komper"


def test_no_working_dirs_means_no_invented_identity(tmp_path, perms):
    """An absent identity stays absent — it must not be guessed.

    `retrieve()` treats a missing identity as "unknown", which is not a match. A
    placeholder would turn "unknown" into a false claim about the session.
    """
    import asyncio

    db = _db(tmp_path, perms)
    mw = AgentDBMiddleware(db=db, agent_id=_AGENT)
    mw._resolved["capture"]["auto_persist_response"] = True
    asyncio.run(mw.finalize_turn_async(
        session_id="s1", model_response="done. " * 8, message_id="m1",
        outcome_type="task_complete", outcome_value=1.0,
    ))
    assert mw._session_scope("s1") == (None, None)


def test_scope_excludes_a_foreign_repository(tmp_path):
    """The read side the write side exists for: scope filters, unscoped does not."""
    from agentdb import AgentDB

    db = AgentDB(memory_dir=str(tmp_path))
    for repo in ("komper", "agentgate"):
        db.write(
            key=f"memory/fact/{repo}", value=f"{repo} uses its own storage",
            origin="agent_inferred", agent_id=_AGENT, session_id="s1",
            working_dirs=[f"/tmp/repos/{repo}"], repository_id=repo,
        )

    query = "what storage does the project use"
    unscoped = db.retrieve(query=query, agent_id=_AGENT, session_id="s2")
    assert len(unscoped.records) == 2

    scoped = db.retrieve(query=query, agent_id=_AGENT, session_id="s2",
                         scope={"repository_id": "komper"})
    assert [r.key for r in scoped.records] == ["memory/fact/komper"]


def test_compaction_summary_can_carry_repository_id(tmp_path):
    """Summaries are scoped the same way, or they stay immortal candidates."""
    from agentdb import AgentDB

    db = AgentDB(memory_dir=str(tmp_path))
    db.write_compaction_summary(
        agent_id=_AGENT, session_id="s1", summary="implemented the milestone",
        working_dirs=["/tmp/repos/komper"], repository_id="komper",
    )
    record = db.read("memory/compaction/summary/s1")
    assert record.repository_id == "komper"
