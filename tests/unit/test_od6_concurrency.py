"""Workstream D — OD-6 transactional idempotency (FINAL-HARDENING §6).

Concurrent consolidation over the same source set must not create multiple
active equivalent canonicals. Uses a stable canonical fingerprint + a
BEGIN IMMEDIATE transaction + a partial unique index. Also: repeated runs are
idempotent, changed sources yield a distinguishable canonical, and a failed
transaction never leaves sources archived without a valid replacement.
"""
from __future__ import annotations

import threading

from agentdb import AgentDB


def _seed(db, agent_id="a1"):
    db.write("memory/analytics-schema-note", "analytics schema columnar layout",
             origin="agent_inferred", agent_id=agent_id)
    db.write("memory/analytics-schema-update", "analytics schema columnar layout confirmed",
             origin="agent_inferred", agent_id=agent_id)


def _canonicals(db):
    return [r for r in db._store._by_id.values() if r.kind == "derived_canonical"]


def _db(tmp_path, name="a"):
    return AgentDB(path=tmp_path / f"{name}.sqlite", memory_dir=tmp_path)


def test_two_concurrent_consolidations_single_canonical(tmp_path):
    db = _db(tmp_path)
    _seed(db)
    errors = []
    def run():
        try:
            db.consolidate("a1", since_hours=0, min_cluster_size=2)
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors
    assert len(_canonicals(db)) == 1


def test_many_concurrent_consolidations_single_canonical(tmp_path):
    db = _db(tmp_path)
    _seed(db)
    errors = []
    def run():
        try:
            db.consolidate("a1", since_hours=0, min_cluster_size=2)
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=run) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors
    assert len(_canonicals(db)) == 1


def test_separate_instances_shared_db_single_canonical(tmp_path):
    """Two independent AgentDB instances over the same DB file do not duplicate."""
    db_path = tmp_path / "shared.sqlite"
    d1 = AgentDB(path=db_path, memory_dir=tmp_path)
    _seed(d1)
    d2 = AgentDB(path=db_path, memory_dir=tmp_path)
    errors = []
    def run(db):
        try:
            db.consolidate("a1", since_hours=0, min_cluster_size=2)
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=run, args=(d,)) for d in (d1, d2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors
    # Fresh read sees exactly one active canonical on disk.
    d3 = AgentDB(path=db_path, memory_dir=tmp_path)
    assert len([r for r in d3._store._by_id.values()
               if r.kind == "derived_canonical" and not r.archived]) == 1


def test_repeated_consolidation_is_idempotent(tmp_path):
    db = _db(tmp_path)
    _seed(db)
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    n1 = len(_canonicals(db))
    # Second and third runs over the (now archived) unchanged sources.
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert len(_canonicals(db)) == n1 == 1


def test_source_mutation_yields_distinguishable_canonical(tmp_path):
    db = _db(tmp_path)
    _seed(db)
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    first = _canonicals(db)[0]
    # A DIFFERENT source set (different content) → different fingerprint.
    db.write("memory/analytics-schema-extra", "analytics schema columnar layout confirmed",
             origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-more", "analytics schema columnar layout confirmed",
             origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    canons = _canonicals(db)
    fps = {c.canonical_fingerprint for c in canons}
    assert first.canonical_fingerprint in fps
    assert len(fps) == len(canons)  # each canonical has a distinct fingerprint


def test_fingerprint_stable_for_same_sources(tmp_path):
    db = _db(tmp_path)
    _seed(db)
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    fp1 = _canonicals(db)[0].canonical_fingerprint
    # Re-running over unchanged sources returns the same canonical/fingerprint.
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert _canonicals(db)[0].canonical_fingerprint == fp1


def test_transaction_failure_does_not_archive_sources(tmp_path, monkeypatch):
    """If canonical creation fails inside the transaction, sources must NOT be
    left archived (no orphaned archival)."""
    db = _db(tmp_path)
    _seed(db)
    # Force the atomic insert to fail.
    def boom(*a, **k):
        raise RuntimeError("simulated db failure")
    monkeypatch.setattr(db._store, "write_canonical_atomic", boom)
    try:
        db.consolidate("a1", since_hours=0, min_cluster_size=2)
    except Exception:
        pass
    # No canonical, and BOTH sources remain active (not archived).
    assert _canonicals(db) == []
    assert len([r for r in db._store.list_all() if r.agent_id == "a1"]) == 2
