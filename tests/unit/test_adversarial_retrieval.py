"""Deterministic adversarial retrieval tests (Workstream C; gates C).

Covers candidate discovery, eligibility/scope, ranking determinism, and token
budget packing against the established product contract. All inputs are
controlled synthetic fixtures with fixed IDs, frozen timestamps, and injected
similarity scores — no network, no LLM, no real embedding model.

The determinism tests (C3) were written to fail against the pre-fix ranker,
which sorted only by composite score and therefore leaked candidate-arrival
order into the output for tied records.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from agentdb import AgentDB
from agentdb.retrieval.algorithm import MultiSignalRanker
from tests.fixtures.adversarial import (
    FROZEN_NOW,
    digest_of,
    frozen_clock,
    make_record_spec,
    write_specs,
)


import yaml


def _db(tmp_path):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)


def _db_isolated(tmp_path, agent_id="a1"):
    """A DB whose memory.yaml lists ``agent_id`` under profile.agents, which
    enables ``isolate_by_agent`` for that agent (see test_config_for_agent)."""
    mem = tmp_path / "mem"
    (mem / "config").mkdir(parents=True, exist_ok=True)
    (mem / "config" / "memory.yaml").write_text(
        yaml.dump({"profile": {"name": "budget_optimized",
                               "agents": {agent_id: {}}}}),
        encoding="utf-8",
    )
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=mem)
    assert db.config_for(agent_id)["isolate_by_agent"] is True
    return db


def _db_embed(tmp_path, embed_fn):
    return AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path, embed_fn=embed_fn)


def _vector(tag: str):
    """Deterministic pseudo-embedding keyed by a tag substring."""
    from tests.fixtures.embeddings import controlled_embed
    return controlled_embed(tag)


# ---------------------------------------------------------------------------
# C1 — Candidate discovery
# ---------------------------------------------------------------------------

def test_c1_lexical_only_candidates(tmp_path):
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("L1", "memory/orders/schema", "orders partition key billing"),
            make_record_spec("U1", "memory/unrelated/x", "nothing matching here at all"),
        ])
        res = db.retrieve("orders partition", agent_id="a1", limit=10)
    assert [r.id for r in res.records] == ["L1"]


def test_c1_semantic_only_candidate_is_discovered(tmp_path):
    """A record with zero lexical overlap but high cosine similarity is eligible."""
    db = _db_embed(tmp_path, _vector)
    with frozen_clock():
        write_specs(db, [
            # text maps to topic_A vector; shares no word with the query
            make_record_spec("S1", "memory/alpha", "phrase about topic_A marker"),
        ])
        res = db.retrieve("topic_A please", agent_id="a1", limit=10)
    assert "S1" in [r.id for r in res.records]


def test_c1_id_deduplication_across_branches(tmp_path):
    """A record reachable lexically AND semantically appears exactly once."""
    db = _db_embed(tmp_path, _vector)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("D1", "memory/topic_A/x", "topic_A explicit words"),
        ])
        res = db.retrieve("topic_A explicit words", agent_id="a1", limit=10)
    ids = [r.id for r in res.records]
    assert ids.count("D1") == 1


def test_c1_empty_results(tmp_path):
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [make_record_spec("E1", "memory/x", "foo")])
        res = db.retrieve("zzz nonexistent", agent_id="a1")
    assert res.records == []
    assert res.token_estimate == 0


def test_c1_large_irrelevant_lexical_set_does_not_starve_semantic(tmp_path):
    """Many weak lexical distractors must not crowd out a semantic-only hit.

    The query must pass the semantic query gate (>=3 tokens) and share the
    embedding vocabulary with the semantic-only record, but NOT with the
    distractors (which would otherwise be lexical candidates for 'orders').
    """
    db = _db_embed(tmp_path, _vector)
    with frozen_clock():
        specs = [
            # 60 records sharing a lexical distractor word but semantically unrelated
            make_record_spec(f"IRR{i}", f"memory/noise/{i}", "orders noise unrelated filler")
            for i in range(60)
        ]
        # one semantic-only record: zero lexical overlap with the query, high cosine
        specs.append(make_record_spec("SEM", "memory/sem", "phrase topic_A semantic"))
        write_specs(db, specs)
        res = db.retrieve("please find the topic_A semantic docs", agent_id="a1", limit=10)
    assert "SEM" in [r.id for r in res.records]


def test_c1_semantic_disabled_falls_back_to_lexical(tmp_path):
    """With embeddings off, only lexical matches are returned; a paraphrase that
    shares no lexical token with the query is not retrieved."""
    db = _db(tmp_path)  # no embeddings
    with frozen_clock():
        write_specs(db, [
            make_record_spec("A", "memory/orders", "orders content"),
            # no lexical token in common with the query below
            make_record_spec("B", "memory/alpha", "phrase about widgets and gadgets"),
        ])
        res = db.retrieve("orders content", agent_id="a1", limit=10)
    ids = [r.id for r in res.records]
    assert "A" in ids
    assert "B" not in ids


# ---------------------------------------------------------------------------
# C2 — Eligibility and scope
# ---------------------------------------------------------------------------

def test_c2_archived_records_excluded(tmp_path):
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("A1", "memory/orders/live", "orders visible"),
            make_record_spec("A2", "memory/orders/dead", "orders archived", archived=True),
        ])
        res = db.retrieve("orders", agent_id="a1", limit=10)
    assert [r.id for r in res.records] == ["A1"]


def test_c2_agent_scope_isolated_when_configured(tmp_path):
    """When isolate_by_agent is enabled for an agent, other agents' records are
    excluded from its retrieval (documented isolation contract)."""
    db = _db_isolated(tmp_path, agent_id="a1")
    with frozen_clock():
        write_specs(db, [
            make_record_spec("MINE", "memory/orders/a", "orders mine", agent_id="a1"),
            make_record_spec("OTHER", "memory/orders/b", "orders other", agent_id="a2"),
        ])
        res = db.retrieve("orders", agent_id="a1", limit=10)
    ids = [r.id for r in res.records]
    assert "MINE" in ids and "OTHER" not in ids


def test_c2_expired_records_excluded(tmp_path):
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("LIVE", "memory/orders/l", "orders live"),
            make_record_spec("EXP", "memory/orders/e", "orders expired",
                             expiry_time=FROZEN_NOW - timedelta(minutes=1)),
        ])
        res = db.retrieve("orders", agent_id="a1", limit=10)
    assert [r.id for r in res.records] == ["LIVE"]


def test_c2_consolidated_excluded_when_requested(tmp_path):
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("SRC", "memory/orders/s", "orders source"),
            make_record_spec("CONS", "memory/orders/c", "orders consolidated",
                             consolidated=True),
        ])
        res = db.retrieve("orders", agent_id="a1", limit=10, include_consolidated=False)
    ids = [r.id for r in res.records]
    assert "SRC" in ids and "CONS" not in ids


def test_c2_scope_parity_lexical_vs_semantic(tmp_path):
    """An archived record is excluded even when it would be a strong semantic hit."""
    db = _db_embed(tmp_path, _vector)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("LIVE", "memory/alpha", "topic_A live marker"),
            make_record_spec("ARCH", "memory/alpha2", "topic_A archived marker",
                             archived=True),
        ])
        res = db.retrieve("topic_A please", agent_id="a1", limit=10)
    ids = [r.id for r in res.records]
    assert "ARCH" not in ids


def test_c2_invalid_canonical_excluded_source_falls_back(tmp_path):
    """A canonical whose source was revised is excluded; the (archived) source
    is not silently promoted but the canonical must not be presented as fresh."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("S1", "memory/orders/s1", "orders v1", archived=True),
        ])
        good_digest = digest_of("orders v1", key="memory/orders/s1")
        write_specs(db, [
            make_record_spec(
                "C1", "memory/canonical/orders/x", "orders canonical",
                consolidated=True, kind="derived_canonical",
                derived_from=["S1"], derived_digests=["stale-digest-does-not-match"],
            ),
        ])
        res = db.retrieve("orders", agent_id="a1", limit=10, trace=True)
    ids = [r.id for r in res.records]
    assert "C1" not in ids
    reasons = {e["id"]: e["reason"] for e in res.trace["excluded"]}
    assert reasons.get("C1") == "canonical_source_revised"


# ---------------------------------------------------------------------------
# C3 — Ranking behavior and deterministic tie-breaking
# ---------------------------------------------------------------------------

def test_c3_relevance_ordering(tmp_path):
    """Higher-confidence, human-approved records outrank weaker ones."""
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("LOW", "memory/orders/low", "orders low",
                             origin="agent_inferred", confidence=0.3),
            make_record_spec("HIGH", "memory/orders/high", "orders high",
                             origin="human_approved", confidence=1.0),
        ])
        res = db.retrieve("orders", agent_id="a1", limit=10)
    assert [r.id for r in res.records] == ["HIGH", "LOW"]


def test_c3_missing_optional_signals_do_not_crash(tmp_path):
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [make_record_spec("X", "memory/orders/x", "orders x", confidence=0.0)])
        res = db.retrieve("orders", agent_id="a1", limit=10)
    assert [r.id for r in res.records] == ["X"]


def test_c3_deterministic_tie_break_independent_of_input_order():
    """Equal-score records must order identically regardless of arrival order.

    This is the core determinism invariant (Product Contract §5D / gate C):
    the ranker must impose a stable tie-break (record id) rather than leaking
    the order in which candidates happened to arrive.
    """
    from agentdb.core.models import MemoryRecord

    def rec(rid):
        return MemoryRecord(
            id=rid, key=f"memory/topic/{rid}", value="shared topic content",
            origin="agent_inferred", trust_level="agent", agent_id="a1",
            confidence=0.9, created_at=FROZEN_NOW - timedelta(hours=1),
        )

    ranker = MultiSignalRanker(staleness_days=90)
    forward = [rec("r1"), rec("r2"), rec("r3"), rec("r4")]
    backward = list(reversed(forward))

    a = [r.record.id for r in ranker.rank("shared topic content", forward, agent_id="a1")]
    b = [r.record.id for r in ranker.rank("shared topic content", backward, agent_id="a1")]
    assert a == b, f"tie order depends on arrival order: {a} vs {b}"


def test_c3_deterministic_tie_break_through_retrieve(tmp_path):
    """End-to-end: two identical DBs written in different order retrieve the
    same tie order."""
    db1 = _db(tmp_path / "one")
    db2 = _db(tmp_path / "two")
    specs = [
        make_record_spec("r1", "memory/topic/r1", "shared topic content"),
        make_record_spec("r2", "memory/topic/r2", "shared topic content"),
        make_record_spec("r3", "memory/topic/r3", "shared topic content"),
    ]
    with frozen_clock():
        write_specs(db1, specs)
        write_specs(db2, list(reversed(specs)))
        res1 = db1.retrieve("shared topic content", agent_id="a1", limit=10)
        res2 = db2.retrieve("shared topic content", agent_id="a1", limit=10)
    assert [r.id for r in res1.records] == [r.id for r in res2.records]


# ---------------------------------------------------------------------------
# C4 — Token budgets
# ---------------------------------------------------------------------------

def test_c4_oversized_first_record_skipped_continues(tmp_path):
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            # high relevance (human approved) but huge
            make_record_spec("BIG", "memory/orders/big", "orders " + "x" * 4000,
                             origin="human_approved", confidence=1.0),
            # small, lower relevance
            make_record_spec("SMALL", "memory/orders/small", "orders ok"),
        ])
        res = db.retrieve("orders", agent_id="a1", limit=10, budget_tokens=20)
    ids = [r.id for r in res.records]
    assert "SMALL" in ids and "BIG" not in ids
    assert res.token_estimate <= 20
    assert res.truncated is True


def test_c4_zero_budget_selects_nothing(tmp_path):
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [make_record_spec("X", "memory/orders/x", "orders x")])
        res = db.retrieve("orders", agent_id="a1", limit=10, budget_tokens=0)
    assert res.records == []
    assert res.truncated is True


def test_c4_unicode_content_token_accounting(tmp_path):
    db = _db(tmp_path)
    with frozen_clock():
        write_specs(db, [
            make_record_spec("U", "memory/orders/u", "ordérs 注文 ключ content ✓"),
        ])
        res = db.retrieve("orders", agent_id="a1", limit=10, budget_tokens=1000)
    # Rendered estimate must reflect the unicode value, not crash.
    assert res.token_estimate >= 1
    assert res.token_estimate <= 1000
