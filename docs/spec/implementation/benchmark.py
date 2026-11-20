#!/usr/bin/env python3
"""Retrieval benchmark harness for AgentDB alignment work.

Measures recall@5, nDCG@5, semantic recall@5 (paraphrase queries with zero
lexical overlap), mean rendered tokens, duplicate keys injected, and P50/P95
retrieval latency.

Run against a specific checkout:
    PYTHONPATH=/path/to/agentdb/src python benchmark.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from pathlib import Path

CONCEPTS = ["billing", "orders", "auth", "infra"]
WORDS = {
    "billing": ["invoice", "payment", "charge", "refund", "receipt", "billing"],
    "orders": ["purchase", "cart", "checkout", "shipment", "orders"],
    "auth": ["login", "credential", "token", "session", "password", "auth"],
    "infra": ["deploy", "server", "database", "cluster", "migration", "infra"],
}
PARAPHRASE = {
    "billing": "financial remittance settlement",
    "orders": "buying acquisition commerce",
    "auth": "identity verification access",
    "infra": "hosting platform provisioning",
}
BUCKETS = {
    "billing": ["billing", "invoice", "payment", "charge", "refund", "receipt",
                "financial", "remittance", "settlement"],
    "orders": ["orders", "purchase", "cart", "checkout", "shipment",
               "buying", "acquisition", "commerce"],
    "auth": ["auth", "login", "credential", "token", "session", "password",
             "identity", "verification", "access"],
    "infra": ["infra", "deploy", "server", "database", "cluster", "migration",
              "hosting", "platform", "provisioning"],
}


def embed_fn(text):
    vec = [0.0, 0.0, 0.0, 0.0]
    t = text.lower()
    for i, words in enumerate(BUCKETS.values()):
        if any(w in t for w in words):
            vec[i] += 1.0
    if all(v == 0.0 for v in vec):
        vec[0] = 0.01
    return vec


def build_db(workdir, embed):
    from agentdb import AgentDB
    if embed:
        return AgentDB(path=workdir / "agentdb.sqlite", memory_dir=workdir, embed_fn=embed_fn)
    return AgentDB(path=workdir / "agentdb.sqlite", memory_dir=workdir)


def build_corpus(db, per_concept=8):
    recs = {}
    for concept in CONCEPTS:
        recs[concept] = []
        for i in range(per_concept):
            w = WORDS[concept][i % len(WORDS[concept])]
            r = db.write(
                "memory/%s/item-%d" % (concept, i),
                "%s note number %d about %s handling details" % (w, i, concept),
                origin="agent_inferred", agent_id="a1",
                confidence=0.5 + 0.05 * (i % 10),
            )
            recs[concept].append(r)
    return recs


def recall_at_k(retrieved, relevant, k):
    if not relevant:
        return 0.0
    return len([r for r in retrieved[:k] if r in relevant]) / len(relevant)


def ndcg_at_k(retrieved, relevant, k):
    rel = set(relevant)
    dcg = sum(1.0 / math.log2(i + 2) for i, key in enumerate(retrieved[:k]) if key in rel)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(rel), k)))
    return dcg / idcg if idcg > 0 else 0.0


def run(embed):
    agg = {"recall@5": [], "ndcg@5": [], "tokens": [], "dup_tokens": [],
           "latency_ms": [], "semantic_recall@5": []}
    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        db = build_db(workdir, embed)
        recs = build_corpus(db)
        for concept in CONCEPTS:
            relevant = {r.key for r in recs[concept]}
            q = " ".join(WORDS[concept][:3]) + " details"
            t0 = time.perf_counter()
            res = db.retrieve(q, agent_id="a1", limit=5)
            agg["latency_ms"].append((time.perf_counter() - t0) * 1000)
            keys = [r.key for r in res.records]
            agg["recall@5"].append(recall_at_k(keys, relevant, 5))
            agg["ndcg@5"].append(ndcg_at_k(keys, relevant, 5))
            agg["tokens"].append(res.token_estimate)
            seen, dup = set(), 0
            for r in res.records:
                if r.key in seen:
                    dup += 1
                seen.add(r.key)
            agg["dup_tokens"].append(dup)
            res2 = db.retrieve(PARAPHRASE[concept], agent_id="a1", limit=5)
            keys2 = [r.key for r in res2.records]
            agg["semantic_recall@5"].append(recall_at_k(keys2, relevant, 5))

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0
    lat = sorted(agg["latency_ms"])
    p50 = lat[len(lat)//2] if lat else 0.0
    p95 = lat[min(len(lat)-1, int(len(lat)*0.95))] if lat else 0.0
    return {
        "embed": embed,
        "recall@5_mean": round(mean(agg["recall@5"]), 4),
        "ndcg@5_mean": round(mean(agg["ndcg@5"]), 4),
        "semantic_recall@5_mean": round(mean(agg["semantic_recall@5"]), 4),
        "mean_tokens": round(mean(agg["tokens"]), 2),
        "mean_dup_tokens": round(mean(agg["dup_tokens"]), 3),
        "latency_p50_ms": round(p50, 3),
        "latency_p95_ms": round(p95, 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    out = {"lexical_only": run(False), "with_embeddings": run(True)}
    print(json.dumps(out, indent=2))
    if args.json:
        args.json.write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
