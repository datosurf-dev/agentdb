# Retrieval Test Matrix

**Spec:** `docs/spec/BOUNDARY_CLEANUP.md` §5 (Workstream C), Gate C
**Test file:** `tests/unit/test_adversarial_retrieval.py`
**Fixtures:** `tests/fixtures/adversarial.py` (frozen clock, explicit-id specs,
injected similarity vectors). All offline; no LLM, no real embedding model.

## Determinism method

- **Frozen clock** patches `_utcnow` in client/store/lifecycle → recency scores
  reproducible.
- **Explicit record IDs** (`make_record_spec(id=...)`) → assertions are on IDs,
  not generated UUIDs or insertion order.
- **Injected vectors** via `controlled_embed` (substring-keyed) → deterministic
  cosine similarity without model download.
- **Ties**: records with identical provenance/confidence/created_at yield equal
  composite scores; ordering must then be stable and id-based.

## C1 — Candidate discovery

| Test | Scenario | Expected | Status |
|---|---|---|---|
| `test_c1_lexical_only_candidates` | one lexically matching, one unrelated | only matching id | PASS |
| `test_c1_semantic_only_candidate_is_discovered` | zero lexical overlap, high cosine | semantic record eligible | PASS |
| `test_c1_id_deduplication_across_branches` | record reachable lex + sem | appears exactly once | PASS |
| `test_c1_empty_results` | no match | empty list, tokens 0 | PASS |
| `test_c1_large_irrelevant_lexical_set_does_not_starve_semantic` | 60 lexical distractors + 1 semantic-only | semantic-only still returned | PASS |
| `test_c1_semantic_disabled_falls_back_to_lexical` | embeddings off | only lexical matches; paraphrase excluded | PASS |

## C2 — Eligibility and scope

| Test | Scenario | Expected | Status |
|---|---|---|---|
| `test_c2_archived_records_excluded` | one archived, one live | archived excluded | PASS |
| `test_c2_agent_scope_isolated_when_configured` | `isolate_by_agent=True` | other agent excluded | PASS |
| `test_c2_expired_records_excluded` | expired `expiry_time` | expired excluded | PASS |
| `test_c2_consolidated_excluded_when_requested` | `include_consolidated=False` | consolidated excluded | PASS |
| `test_c2_scope_parity_lexical_vs_semantic` | archived strong semantic hit | excluded despite similarity | PASS |
| `test_c2_invalid_canonical_excluded_source_falls_back` | canonical source revised | canonical excluded (`canonical_source_revised`) | PASS |

Note: `isolate_by_agent` defaults to **False**; isolation is enabled by listing
the agent under `profile.agents`. Tests use the documented configuration path,
not an invented default.

## C3 — Ranking behavior and determinism

| Test | Scenario | Expected | Status |
|---|---|---|---|
| `test_c3_relevance_ordering` | human-approved high vs low-confidence agent | higher authority first | PASS |
| `test_c3_missing_optional_signals_do_not_crash` | confidence 0.0, no embedding | still ranked | PASS |
| `test_c3_deterministic_tie_break_independent_of_input_order` | equal scores, reversed input | identical order | **FAILED pre-fix → PASS after fix** |
| `test_c3_deterministic_tie_break_through_retrieve` | two DBs written in opposite order | identical retrieval order | **FAILED pre-fix → PASS after fix** |

### Confirmed defect (fixed this release)
`MultiSignalRanker.rank()` sorted **only** by `composite_score`. For tied
records, Python's stable sort preserved candidate-arrival order, which depends on
`dict`/SQLite row iteration — not stable across runs or entry points. Proven by
loading records in reversed order: pre-fix returned `['r4','r3','r2','r1']` vs
`['r1','r2','r3','r4']`; post-fix both are id-sorted. **Fix:** secondary sort key
`(-composite_score, record.id)`. No weight changes (spec constraint honored).

## C4 — Token budgets

| Test | Scenario | Expected | Status |
|---|---|---|---|
| `test_c4_oversized_first_record_skipped_continues` | huge top-ranked + small | small selected, `token_estimate ≤ budget`, `truncated=True` | PASS |
| `test_c4_zero_budget_selects_nothing` | `budget_tokens=0` | empty, `truncated=True` | PASS |
| `test_c4_unicode_content_token_accounting` | UTF-8/Unicode value | rendered estimate ≥1 and ≤ budget, no crash | PASS |

## Coverage vs spec C1–C4

All spec-required scenarios for C1 (lexical/semantic/dedup/large-set/disabled/
empty), C2 (archive/agent/expiry/consolidated/canonical), C3 (ordering/missing
signals/determinism), and C4 (oversized/small/zero/unicode) are covered. Fake
"candidate count limits" are exercised implicitly via `limit` and the 50-cap
lexical path (`test_c1_large_irrelevant_lexical_set...`).

## Known limits / not covered here
- Determinism under a *live* wall clock: `increment_retrieval_count` updates
  `last_retrieved` on each retrieve (documented behavior); tests freeze the
  clock to isolate ordering from recency drift.
- Real semantic recall quality (needs a real embedding model) — excluded by
  spec (no live/external embedding experiments).
