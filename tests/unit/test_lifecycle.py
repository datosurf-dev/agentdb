import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from agentdb.core.models import MemoryRecord, Origin, TrustLevel
from agentdb.core.store import MemoryStore          # extracted in pre-step
from agentdb.core.system import SystemStats         # extracted in pre-step
from agentdb.governance.lifecycle_manager import LifecycleManager


@pytest.fixture
def store(tmp_path):
    return MemoryStore(db_path=tmp_path / "test.db")


@pytest.fixture
def manager(store, tmp_path):
    sys = SystemStats(db_path=tmp_path / "test.db")
    return LifecycleManager(store=store, system=sys)


def _rec(agent_id="a1", key="k", origin=Origin.AGENT_INFERRED,
         trust=TrustLevel.AGENT, confidence=1.0, days_since_retrieved=0):
    r = MemoryRecord(key=key, value="v", origin=origin,
                     trust_level=trust, agent_id=agent_id,
                     confidence=confidence)
    offset = datetime.now(timezone.utc) - timedelta(days=days_since_retrieved)
    r.last_retrieved = offset
    r.updated_at = offset if hasattr(r, "updated_at") else None
    return r


def test_human_approved_never_archived(store, manager):
    r = store.write(_rec(origin=Origin.HUMAN_APPROVED, trust=TrustLevel.HUMAN,
                         confidence=0.01, days_since_retrieved=200))
    manager.run_sweep()
    assert store.get_by_key("k") is not None  # must survive


def test_below_threshold_gets_archived(store, manager):
    manager.ARCHIVAL_THRESHOLD = 0.5
    store.write(_rec(confidence=0.45))
    manager.run_sweep()
    assert store.get_by_key("k") is None  # archived


def test_stale_record_archived(store, manager):
    manager.STALENESS_DAYS = 30
    store.write(_rec(days_since_retrieved=35))
    manager.run_sweep()
    assert store.get_by_key("k") is None


def test_active_record_not_archived(store, manager):
    store.write(_rec(confidence=0.9, days_since_retrieved=1))
    manager.run_sweep()
    assert store.get_by_key("k") is not None


def test_reinforce_boosts_confidence(store, manager):
    r = store.write(_rec(confidence=0.5))
    manager.reinforce(r.id)
    updated = store.get_by_id(r.id)
    assert updated.confidence > 0.5


def test_run_sweep_returns_summary(store, manager):
    store.write(_rec(key="k1"))
    summary = manager.run_sweep()
    assert "archived" in summary


def test_decay_skipped_when_rate_is_zero(store, manager):
    # CONFIDENCE_DECAY_RATE defaults to 0.0 — no mutation
    r = store.write(_rec(confidence=1.0, days_since_retrieved=60))
    manager.run_sweep()
    updated = store.get_by_id(r.id)
    # may be archived if stale, but if not — confidence unchanged
    if updated is not None:
        assert updated.confidence == 1.0


def test_decay_applied_when_rate_nonzero(store, manager):
    manager.CONFIDENCE_DECAY_RATE = 0.05
    manager.ARCHIVAL_THRESHOLD = 0.0  # prevent archival so we can check decay
    r = store.write(_rec(confidence=1.0, days_since_retrieved=0))
    manager.run_sweep()
    updated = store.get_by_id(r.id)
    assert updated is not None
    assert updated.confidence < 1.0


def test_reinforce_skips_archived_record(store, manager):
    r = store.write(_rec(confidence=0.5))
    store.archive_record(r.id)
    manager.reinforce(r.id)
    updated = store.get_by_id(r.id)
    # confidence must be unchanged after reinforcing an archived record
    assert updated.confidence == 0.5
