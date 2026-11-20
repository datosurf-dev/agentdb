# Retrieval Benchmarks — Before / After

**Date:** 2026-11-20
**Baseline:** `main` @ `3dce7ba` (lexical-only retrieval)
**Aligned:** `feat/core-product-alignment`
**Harness:** `docs/spec/implementation/benchmark.py` (public API only, deterministic corpus)
**Method:** identical synthetic corpus + query set run against both checkouts via `PYTHONPATH`; 3 repetitions; latency stable within ~0.4 ms.

## Corpus & queries

- 4 concepts (billing, orders, auth, infra), 8 `agent_inferred` records each (32 total).
- **Lexical queries** share words with the concept's records.
- **Paraphrase queries** are deliberately word-disjoint from record text but map to the same concept in the embedding function (tests semantic recall without lexical overlap).

## Results

| Metric | Baseline (lexical_only) | Aligned (lexical_only) | Baseline (embeddings) | Aligned (embeddings) |
|---|---:|---:|---:|---:|
| recall@5 | 0.1562 | 0.1562 | 0.625 | 0.625 |
| nDCG@5 | 0.25 | 0.25 | 1.0 | 1.0 |
| **semantic recall@5** | 0.0 | 0.0 | **0.0** | **0.625** |
| mean rendered tokens | 64.0 | 64.0 | 61.75 | 61.75 |
| mean duplicate keys | 0.0 | 0.0 | 0.0 | 0.0 |
| latency P50 (ms) | 5.8 | 5.7 | 6.8 | 6.8 |
| latency P95 (ms) | 6.1 | 6.2 | 7.3 | 7.0 |

### Headline result — ADB-05

With embeddings enabled, **semantic recall on paraphrase-only queries rose from 0.0 to 0.625**. Before this change, a record with strong semantic relevance but zero lexical overlap was *unreachable*: candidates were capped at a lexical pass of 50, and embeddings only re-ranked lexical hits. Now semantic candidates are generated from the whole corpus and unioned with lexical hits.

No regressions: lexical recall@5, nDCG@5, token counts, and duplicate counts are unchanged; P50/P95 latency is within noise.

### Budget compliance (ADB-08)

| Scenario | Result |
|---|---|
| Oversized first record + small record, `budget_tokens=20` | small record selected; oversized skipped; `token_estimate=3 ≤ 20`; `truncated=True` |
| `budget_tokens=0` | no records; `truncated=True` |

Previously the loop `break`ed on the first oversized record, so a smaller eligible record after it was never considered.

### Canonical/source dedup (ADB-07 / T06)

| Scenario | Result |
|---|---|
| 2 near-duplicate records consolidated | 1 `derived_canonical` created |
| retrieve after consolidation | canonical injected; **0** of its source ids re-injected |

## Thresholds

Per the specification, numerical targets are **derived from this measured baseline**, not invented:

- **Semantic recall@5 (embeddings on):** must exceed baseline (0.0). Achieved 0.625.
- **Lexical recall@5 / nDCG@5:** must not regress. Unchanged.
- **Budget:** rendered token estimate must never exceed `budget_tokens`. Enforced.
- **Latency:** P95 must not regress beyond ~50% of baseline. Measured within noise (+0%).

## Caveats

- Synthetic corpus with a controlled embedding function; real semantic recall depends on the actual embedding model.
- recall@5 is bounded by 5/8 = 0.625 given 8 relevant records and limit=5; ranking quality (not candidate generation) caps the lexical-query numbers for both baseline and aligned.
- Latency measured on a warm in-process SQLite; not a network/embedded-model benchmark.
