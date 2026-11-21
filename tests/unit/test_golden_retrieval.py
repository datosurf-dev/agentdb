"""Golden retrieval validation (CONTEXTUAL-SPEC §9).

A small deterministic golden corpus with explicit relevance grades, measuring
Recall@K and nDCG@K. Offline, no LLM, fixed embeddings, frozen relevance grades.
A passing score here is evidence the pipeline behaves as specified on the curated
set — NOT proof of universal retrieval optimality.
"""
from __future__ import annotations

import math
from datetime import timedelta

import pytest

from agentdb import AgentDB
from tests.fixtures.adversarial import FROZEN_NOW, frozen_clock, make_record_spec, write_specs


def _golden_db(tmp_path):
    db = AgentDB(path=tmp_path / "agentdb.sqlite", memory_dir=tmp_path)
    return db


# (query, {record_id: relevance_grade}) — grades: 2 relevant, 1 partial, 0 irrelevant
GOLDEN = [
    (
        "postgres partition key",
        {"db_pg": 2, "db_pg2": 1},
    ),
    (
        "auth token expiry",
        {"auth_1": 2, "auth_2": 1},
    ),
    (
        "billing invoice rounding",
        {"bill_1": 2},
    ),
]


def _seed(db):
    with frozen_clock():
        ws = FROZEN_NOW - timedelta(hours=1)
        specs = [
            make_record_spec("db_pg", "memory/db/partition", "postgres partition key strategy", created_at=ws),
            make_record_spec("db_pg2", "memory/db/partition2", "postgres partitioning notes", created_at=ws),
            make_record_spec("db_other", "memory/db/other", "postgres connection pooling", created_at=ws),
            make_record_spec("auth_1", "memory/auth/expiry", "auth token expiry policy", created_at=ws),
            make_record_spec("auth_2", "memory/auth/expiry2", "auth token refresh schedule", created_at=ws),
            make_record_spec("auth_other", "memory/auth/other", "auth provider config", created_at=ws),
            make_record_spec("bill_1", "memory/billing/rounding", "billing invoice rounding rules", created_at=ws),
            make_record_spec("bill_other", "memory/billing/tax", "billing tax jurisdiction", created_at=ws),
        ]
        write_specs(db, specs)


def recall_at_k(ranked_ids, relevant, k):
    rel = {i for i, g in relevant.items() if g >= 1}
    if not rel:
        return 1.0
    got = set(ranked_ids[:k]) & rel
    return len(got) / len(rel)


def ndcg_at_k(ranked_ids, relevant, k):
    dcg = 0.0
    for i, rid in enumerate(ranked_ids[:k]):
        gain = relevant.get(rid, 0)
        dcg += (2**gain - 1) / math.log2(i + 2)
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 1.0


@pytest.mark.parametrize("query,grades", GOLDEN)
def test_golden_recall_and_ndcg(tmp_path, query, grades):
    db = _golden_db(tmp_path)
    _seed(db)
    with frozen_clock():
        res = db.retrieve(query, agent_id="a1", limit=5)
    ranked_ids = [r.id for r in res.records]
    r5 = recall_at_k(ranked_ids, grades, 5)
    n5 = ndcg_at_k(ranked_ids, grades, 5)
    # Curated-set thresholds (measured on this corpus, not universal claims).
    assert r5 >= 0.5, f"recall@5 too low for {query!r}: {r5} (ranked={ranked_ids})"
    assert n5 > 0.0, f"nDCG@5 zero for {query!r}"


def test_golden_relevance_order_puts_top_grades_first(tmp_path):
    db = _golden_db(tmp_path)
    _seed(db)
    with frozen_clock():
        res = db.retrieve("postgres partition key", agent_id="a1", limit=5)
    ids = [r.id for r in res.records]
    # The grade-2 record should out-rank the grade-1 partial when both relevant.
    if "db_pg" in ids and "db_pg2" in ids:
        assert ids.index("db_pg") < ids.index("db_pg2")


def test_golden_deterministic_repeat(tmp_path):
    def run(sub):
        d = tmp_path / sub
        d.mkdir()
        db = AgentDB(path=d / "agentdb.sqlite", memory_dir=d)
        _seed(db)
        with frozen_clock():
            return [r.id for r in db.retrieve("auth token expiry", agent_id="a1", limit=5).records]
    assert run("x") == run("y")
