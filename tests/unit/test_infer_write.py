"""
TDD tests for Task 10C: infer flag and expiry_time on write().
All tests must fail before the implementation is added.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentdb import AgentDB


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


@pytest.fixture
def db_with_extractor(tmp_path):
    return AgentDB(
        path=tmp_path / "test.db",
        infer_fn=lambda raw: f"extracted:{raw}",
    )


# ---------------------------------------------------------------------------
# infer flag
# ---------------------------------------------------------------------------

def test_infer_false_stores_verbatim(db):
    db.write(key="k", value="raw signal", origin="agent_inferred",
             agent_id="a", infer=False)
    assert db.read("k", agent_id="a").value == "raw signal"


def test_infer_true_calls_extractor(db_with_extractor):
    db_with_extractor.write(key="k", value="raw", origin="agent_inferred",
                             agent_id="a", infer=True)
    assert db_with_extractor.read("k", agent_id="a").value == "extracted:raw"


def test_infer_true_without_extractor_raises(db):
    with pytest.raises(ValueError, match="infer_fn"):
        db.write(key="k", value="raw", origin="agent_inferred",
                 agent_id="a", infer=True)


def test_inferred_flag_set_on_record(db_with_extractor):
    record = db_with_extractor.write(key="k", value="raw", origin="agent_inferred",
                                      agent_id="a", infer=True)
    assert record.inferred is True


def test_inferred_flag_false_when_not_inferred(db):
    record = db.write(key="k", value="raw", origin="agent_inferred", agent_id="a")
    assert record.inferred is False


# ---------------------------------------------------------------------------
# expiry_time
# ---------------------------------------------------------------------------

def test_expiry_time_archived_by_sweep(db):
    past = _utcnow() - timedelta(seconds=1)
    db.write(key="k", value="v", origin="agent_inferred",
             agent_id="a", expiry_time=past)
    db.lifecycle.run_sweep()
    assert db.read("k", agent_id="a") is None


def test_expiry_time_not_archived_if_future(db):
    future = _utcnow() + timedelta(hours=1)
    db.write(key="k", value="v", origin="agent_inferred",
             agent_id="a", expiry_time=future)
    db.lifecycle.run_sweep()
    assert db.read("k", agent_id="a") is not None


def test_human_approved_not_expired_regardless(db):
    past = _utcnow() - timedelta(seconds=1)
    db.write(key="memory/long_term/s", value="v", origin="human_approved",
             agent_id="a", expiry_time=past)
    db.lifecycle.run_sweep()
    assert db.read("memory/long_term/s", agent_id="a") is not None


def test_expiry_time_none_not_archived_prematurely(db):
    db.write(key="k", value="v", origin="agent_inferred",
             agent_id="a", expiry_time=None)
    db.lifecycle.run_sweep()
    assert db.read("k", agent_id="a") is not None


def test_expiry_time_persisted_on_record(db):
    future = _utcnow() + timedelta(hours=1)
    record = db.write(key="k", value="v", origin="agent_inferred",
                      agent_id="a", expiry_time=future)
    assert record.expiry_time is not None
