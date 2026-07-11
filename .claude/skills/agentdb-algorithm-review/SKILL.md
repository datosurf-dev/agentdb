---
name: agentdb-algorithm-review
description: Use when any code change touches agentdb algorithm files — algorithm.py, lifecycle_manager.py, system_tables.py, or client.py retrieve/record_outcome methods. Triggers algorithm improvement evaluation before the change is accepted.
---

# AgentDB Algorithm Review

## Overview

Every code change to the AgentDB algorithm is an opportunity to improve it - or a risk of regressing it. This skill runs a structured review whenever algorithm-touching code is proposed, asking: does this change make the algorithm better, worse, or neutral? If neutral, should we make it better while we're here?

The algorithm has two jobs:
1. **Ranking** - given what is stored, what should the agent see? (`algorithm.py`)
2. **Improvement** - given what happened, what should the agent remember differently? (`lifecycle_manager.py`, `client.py`)

A code change that doesn't move either of these forward needs a clear reason to exist.

---

## Trigger Files

Run this skill when a proposed change touches any of:

| File | Algorithm concern |
|---|---|
| `agentdb/retrieval/algorithm.py` | Signal weights, scoring formulas, ranking logic |
| `agentdb/governance/lifecycle_manager.py` | Confidence decay, reinforcement boost/decay rates, outcome rules |
| `agentdb/interface/client.py` - `retrieve()` | Retrieval event logging, budget assembly, conflict-first ordering |
| `agentdb/interface/client.py` - `record_outcome()` | Outcome linking, session resolution |
| `agentdb/core/system_tables.py` - `agentdb_stats()` | Corpus quality metrics, outcome_positive_rate |

---

## Review Checklist

Run through every item before accepting the change. A "no" is not a blocker - it is a prompt to either improve the change or document why the status quo is correct.

### 1. Signal integrity (ranking)

- [ ] Do the signal weights still sum to 1.0? (Enforced in constructor - verify the test catches this.)
- [ ] Is the provenance ordering intact? `HUMAN (1.0) > PROVIDER (0.6) > AGENT (0.3)`. If changed, what evidence justifies the new values?
- [ ] Does the recency formula still reward frequently-retrieved memories with slower decay? Formula: `exp(-lambda*days) * (1 + log(count+1)*0.1)`
- [ ] Is the causal score still approximated by `freq_score * recency_weight` until Phase 4? If the full WAL correlation is being introduced, confirm Task A and Task B audit log prerequisites are in place.
- [ ] Is the semantic weight still 0.0 until embeddings are active? If activating it, confirm `embedding_version` is set on all records being scored.

### 2. Improvement loop integrity

- [ ] Are human-approved records (`origin=HUMAN_APPROVED`) immune to outcome-based confidence changes in **both** directions? No edge case should boost or decay them.
- [ ] Are provider-ingested records decay-only (no boost from positive outcomes)?
- [ ] Is the max single-outcome delta capped at +/-0.1? Large swings destabilize the corpus.
- [ ] Are the BOOST (0.05) and DECAY (0.02) constants asymmetric? Decay is smaller than boost by design - false negatives (forgetting useful memories) are more costly than false positives (retaining marginally useful ones). If changing either, justify the new asymmetry explicitly.
- [ ] Does neutral outcome (0.3 < value < 0.7) produce zero confidence change?

### 3. Skill health integrity

- [ ] Is the staleness half-life still 30 days? (`exp(-log(2) * days / 30)`)
- [ ] Are `health_score` and `health_score_reasons` computed at read time, never stored?
- [ ] Does a score below 0.6 surface a `staleness_warning` in the retrieval result?

### 4. Corpus quality metrics

- [ ] Does `agentdb_stats()` include `avg_confidence`, `low_confidence_count`, `conflict_pending_count`, `outcome_positive_rate`, `retrieval_count_total`?
- [ ] Is `outcome_positive_rate` returned as `None` when fewer than 10 outcomes are recorded? (Avoid misleading rates from small samples.)

### 5. Safety ordering in `retrieve()`

- [ ] Are conflict flags assembled **before** memory content? The agent must know a memory is contested before receiving it - not after.
- [ ] Is a `memory_retrieve` audit event logged after assembling the result, with `record_ids` and `signal_scores` in the payload?

---

## Improvement Opportunities

When reviewing a change, also ask: **should we improve the algorithm here?**

These are known weak points worth addressing when nearby code is already being touched:

| Weakness | Current state | Better approach |
|---|---|---|
| Causal score | Approximated via `retrieval_count * recency_weight` | Full WAL correlation: scan `memory_retrieve` -> `memory_outcome` chains per record (Phase 4) |
| Semantic weight | 0.0 until embeddings active | When activating, redistribute: provenance 0.30, recency 0.20, confidence 0.20, causal 0.10, semantic 0.20 |
| Skill entity drift | Keyword-based detection only | Semantic entity matching - a skill using a synonym for a deprecated entity won't be flagged until Phase 6 |
| Reinforcement granularity | All retrieved records in a session get the same boost/decay | Weight by signal_score at retrieval time - records that ranked higher contributed more to the outcome |
| Outcome linking | Links to most recent `memory_retrieve` in session | Could link to specific retrieval sequence if `retrieval_sequence` is passed explicitly |

If the change being reviewed is adjacent to one of these, propose the improvement. Don't silently pass a change that misses an obvious upgrade opportunity.

---

## Red Flags - Stop and Investigate

- Signal weights changed without a corresponding test demonstrating improved retrieval quality
- `HUMAN_APPROVED` records are modified by any outcome-based path
- `outcome_positive_rate` computed from fewer than 10 outcomes without the `None` guard
- Conflict flags assembled after memory content in `retrieve()`
- `health_score` being stored rather than computed at read time
- `BOOST > DECAY` asymmetry reversed (would make forgetting cheaper than learning)
- Max delta cap removed or raised above 0.1

---

## Algorithm Constants Reference

```python
# MultiSignalRanker - agentdb/retrieval/algorithm.py
WEIGHT_PROVENANCE  = 0.35   # novel signal
WEIGHT_RECENCY     = 0.25
WEIGHT_CONFIDENCE  = 0.25
WEIGHT_CAUSAL      = 0.15   # approximated until Phase 4
WEIGHT_SEMANTIC    = 0.0    # activated Phase 4

PROVENANCE_SCORES = {
    Origin.HUMAN_APPROVED:    1.0,
    Origin.PROVIDER_INGESTED: 0.6,
    Origin.AGENT_INFERRED:    0.3,
}
RECENCY_LAMBDA     = 0.01   # half-life ~= 69 days

# LifecycleManager - agentdb/governance/lifecycle_manager.py
BOOST              = 0.05   # per positive outcome
DECAY              = 0.02   # per negative outcome (smaller - false negatives costly)
MAX_DELTA          = 0.10   # cap per single outcome
CONFIDENCE_DECAY_RATE = 0.05  # per lifecycle cycle (time-based, not outcome-based)
ARCHIVAL_THRESHOLD = 0.10
STALENESS_DAYS     = 90

# SkillStore - agentdb/skills/skill_store.py
STALE_HALF_LIFE_DAYS = 30
WARN_THRESHOLD       = 0.6
```

---

## After the Review

- If all checklist items pass and no improvements were identified: accept the change.
- If a red flag was found: block the change, document the specific violation.
- If an improvement opportunity was identified: propose it as an addendum to the change. Don't block - but don't silently pass either.
- Update this skill if a new weakness or failure mode is discovered during review.
