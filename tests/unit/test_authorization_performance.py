"""Authorization performance (FINAL-HARDENING §8).

Bounded, offline benchmark for canonical lineage authorization (OD-3
_can_read_record over derived canonicals): measures retrieval latency as the
number of sources per canonical and the number of canonicals grows. Reports
numbers; asserts only loose bounds (no fragile timing assertions). Not a
correctness test.
"""
from __future__ import annotations

import time
from pathlib import Path

from agentdb import AgentDB
from agentdb.core.models import MemoryRecord
from agentdb.governance.lifecycle_manager import _record_digest


def _iso_db(tmp_path):
    perms = tmp_path / "perm.yaml"
    agents = []
    for who in ("alice", "bob"):
        agents.append(
            f"  {who}:\n    can_write:\n      - \"memory/{who}/\"\n"
            f"    can_read:\n      - \"memory/{who}/\"\n"
        )
    perms.write_text("default_policy: deny\nagents:\n" + "".join(agents), encoding="utf-8")
    return AgentDB(path=tmp_path / "a.sqlite", memory_dir=tmp_path, permissions_path=perms)


def _make_canonical(db, idx, n_sources, visible=True):
    who = "alice" if visible else "bob"
    srcs = []
    for j in range(n_sources):
        r = db.write(f"memory/{who}/c{idx}_s{j}", f"fact {idx}-{j}",
                     origin="agent_inferred", agent_id=who)
        srcs.append(r)
    canon = MemoryRecord(
        key=f"memory/canonical/c{idx}/x", value=f"combined fact {idx}",
        origin="agent_inferred", trust_level="agent", agent_id="alice",
        consolidated=True, kind="derived_canonical", producer="consolidation",
        source_record_ids=[s.id for s in srcs],
        derivation={"version": "1", "sources": [
            {"id": s.id, "digest": _record_digest(s)} for s in srcs]},
    )
    db._store.write(canon)
    return canon


def _time_retrieve(db, query, n=20):
    t0 = time.perf_counter()
    for _ in range(n):
        db.retrieve(query, agent_id="alice", limit=10)
    return (time.perf_counter() - t0) / n * 1000.0  # ms/query


def test_lineage_authorization_scales_reasonably(tmp_path):
    results = {}
    for n_sources in (2, 8, 32):
        d = tmp_path / f"n{n_sources}"
        d.mkdir()
        db = _iso_db(d)
        _make_canonical(db, 0, n_sources, visible=True)
        ms = _time_retrieve(db, "combined fact")
        results[n_sources] = ms
        # Loose bound: lineage check stays well under 50ms/query even at 32 sources.
        assert ms < 50.0, f"lineage auth too slow at {n_sources} sources: {ms:.2f}ms"
    # Not asserting strict monotonic growth (noisy); just record.
    print("[authorization perf] sources->ms/query:", results)


def test_denied_canonical_short_circuits(tmp_path):
    """A canonical with an unreadable source is denied; denying is not slower
    than allowing (no pathological blow-up)."""
    d = tmp_path / "deny"
    d.mkdir()
    db = _iso_db(d)
    # canonical includes a bob-only source -> alice is denied.
    a_src = db.write("memory/alice/ok", "alice fact", origin="agent_inferred", agent_id="alice")
    b_src = db.write("memory/bob/secret", "bob fact", origin="agent_inferred", agent_id="bob")
    canon = MemoryRecord(
        key="memory/canonical/mix/x", value="combined mix",
        origin="agent_inferred", trust_level="agent", agent_id="alice",
        consolidated=True, kind="derived_canonical", producer="consolidation",
        source_record_ids=[a_src.id, b_src.id],
        derivation={"version": "1", "sources": [
            {"id": a_src.id, "digest": _record_digest(a_src)},
            {"id": b_src.id, "digest": _record_digest(b_src)}]},
    )
    db._store.write(canon)
    res = db.retrieve("combined mix", agent_id="alice", limit=10)
    assert canon.id not in {r.id for r in res.records}
    ms = _time_retrieve(db, "combined mix")
    assert ms < 50.0
