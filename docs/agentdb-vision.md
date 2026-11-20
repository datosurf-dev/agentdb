# AgentDB Vision

This document states the product vision for AgentDB and assesses, against the current source, where the implementation already follows it and where it needs work. It is deliberately honest about the gap between intent and code — per the repository rule in `AGENTS.md`, nothing here should be read as "implemented" unless it is verified against source and tests.

If this document and the code disagree, **the code wins**, and the discrepancy is a bug in this document (or a tension to resolve — see the last section).

---

## The vision in one paragraph

AgentDB exists to make an agent's context **relevant, distilled, and cheap to inject**, and to protect the human's intent when the agent's memory starts to drift away from it. Two things are ranked for injection: **memory records** and **distilled knowledge**. **Skills** are different: they are the human-authored baseline of *how work should be done*, and they are **never injected**. Their job is to be the fixed point against which incoming ideas are checked — and when a recent, relevant memory (or a piece of distilled knowledge) conflicts with a skill, AgentDB **raises a flag and involves the human**.

---

## The three pillars

### 1. Memory and distilled knowledge → ranked for context injection

The algorithm's primary job is **selection for injection**: given everything stored, what is the most relevant, most distilled context this model should see right now — without wasting tokens?

- A multi-signal ranker scores candidate records (provenance, recency, confidence, causal/usage, optional semantic similarity, optional workspace overlap) and returns a bounded, token-aware package.
- **Distilled knowledge** — consolidated canonicals that fold many near-duplicate records into one high-confidence summary — is ranked and injected alongside raw memories, so the model gets the gist, not the repetition.
- Token consumption is a first-class constraint: retrieval honors record limits and token budgets, and injected context is trimmed to a budget.

### 2. Skills → not injected; the external check against memory

Skills represent how work should be done — human intent made durable. They serve the agent **indirectly**:

- They are **not** injected into the model's context. The model does not receive skill text as prompt augmentation.
- They act as a **reference standard**: when the agent writes (or consolidates) memory that overlaps a scoped skill — by keyword or by embedding similarity — and that overlap signals possible drift, AgentDB **dispatches a conflict signal** (`skill_review_needed`) for a human to review.
- Skills also **decay**: staleness tracking flags skills that may no longer reflect current reality (`skill_stale`), again for human attention.

The output of the skill system is **a flag to a human**, never a silent reweighting of what the model sees.

### 3. The algorithm ties memory and skills together

The correlation between the algorithm and skills is the heart of the product:

- The algorithm decides **what is relevant** (which memories and distilled knowledge matter right now).
- Skills decide **what is correct** (the human's intended way of working).
- The value is in the intersection: when the algorithm surfaces (or the agent writes) relevant, recent memory that contradicts a skill, that is exactly the moment AgentDB must **stop and ask the human**. The flag is only meaningful because the conflicting memory is relevant and recent — the algorithm is what makes the skill check worth running.

---

## Assessment: vision vs. current implementation

Legend: **Followed** = implemented and consistent with the vision. **Needs review** = partial, or implemented in a way that conflicts with the vision. **Not built** = absent.

### Pillar 1 — Memory/distilled knowledge ranked for injection

| Aspect | Status | Evidence |
|---|---|---|
| Multi-signal ranking of memory records | **Followed** | `src/agentdb/retrieval/algorithm.py` (`MultiSignalRanker`); weights provenance/recency/confidence/causal + optional semantic/workspace. |
| Token-aware retrieval | **Followed** | `retrieve()` returns `token_estimate`; budgets from `injection.context_token_limit` / `memory_budget_chars`; injected blocks trimmed to budget. |
| Distilled knowledge ranked/injected | **Followed** | `consolidate()` marks canonicals (`consolidated=True`, `source_record_ids`); `retrieve(include_consolidated=...)` controls inclusion. |
| Distilled knowledge is *prioritized* over raw duplicates | **Needs review** | Consolidation archives lower-confidence copies, but the ranker has no explicit "canonical beats its sources" boost — prioritization emerges from confidence/archival, not a dedicated signal. Worth confirming this is intentional. |

### Pillar 2 — Skills not injected; conflict + staleness flags only

| Aspect | Status | Evidence |
|---|---|---|
| Skill conflict detection (keyword) | **Followed** | `_check_skill_conflict()` in `interface/client.py` dispatches `skill_review_needed` on key/entity/repo word overlap with scoped skills. |
| Skill conflict detection (semantic) | **Followed** | Same path, cosine similarity via embedding store when embeddings are active and keyword did not match. |
| Human involvement on conflict | **Followed** | `skill_review_needed` is a `NotificationDispatcher` event routed to email/Slack (`notifications/dispatcher.py`). |
| Skill staleness flags | **Followed** | Health score from `updated_at` age; crossing 0.6 dispatches `skill_stale`. |
| **Skills are NOT injected** | **Needs review — conflicts with vision** | `adapters/middleware.py::_build_skills_block()` **injects skill text** when `capabilities.inject_skills` is true, which is the **default for `LOCAL_CAPABILITIES` and `SERVERLESS_CAPABILITIES`** (only `CLAUDE_CAPABILITIES` sets it false). This directly contradicts the vision ("skills stay not injected") and also contradicts Design Principle 4a. See Development Tensions below. |

### Pillar 3 — Algorithm ↔ skills correlation

| Aspect | Status | Evidence |
|---|---|---|
| Skill conflict is triggered by recent/relevant memory | **Partly followed** | Conflict checks fire from `write()` and `consolidate()` — i.e., on memory that is by definition recent. Relevance is approximated by keyword/embedding overlap against skill name/id. |
| Conflict check triggered by *retrieval-time relevance* | **Not built** | The flag does not consider what `retrieve()` actually surfaced to the model; it fires on write, independent of retrieval. "Relevant and recent" is thus modeled as "just-written and overlapping," not "the algorithm just ranked this as highly relevant." |
| Skills influence ranking / injection weighting | **Not built (by design)** | `MultiSignalRanker` has zero skill awareness; `RetrievalResult` contains no skills. This matches the "skills not injected" vision — listed here so the boundary is explicit. |
| Skill scope = codebase / task classification | **Not built** | `scope` is a list of **agent_ids** only (`SkillStore.get_for_agent`). There is no notion of "applies to repo X" or "applies to task type Y (PR review, web tests in language Z)." `domain_words` on proposals is not a task/codebase taxonomy. |

---

## Development tensions the vision must resolve

These are the open questions where the stated vision and the current code are not yet reconciled. Each is a decision to make before the vision can be called implemented.

### T1. Skill injection vs. "skills not injected" (the load-bearing conflict)

The vision says skills are never injected — they only produce flags. But `_build_skills_block()` injects skill text today for local/serverless runners (`inject_skills=True` by default), and `README.md` / `capabilities.md` / `mechanisms.md` all describe skill injection as an available feature. Meanwhile Design Principle 4a already states "Skills are NOT injected."

**Decision needed:** Is skill injection (a) removed entirely, (b) defaulted off and kept as an explicit opt-in, or (c) reclassified as a deliberate, documented exception? The docs cannot claim "skills not injected" until the code default and this principle agree.

### T2. "Relevant and recent memory" — write-time vs. retrieval-time

The vision says the human is flagged when skills conflict with **relevant and recent** memory. Today the check fires at **write/consolidation time** on keyword/semantic overlap. It never consults what the algorithm actually surfaced during `retrieve()`.

**Decision needed:** Is write-time overlap "recent + relevant enough"? Or should the conflict check also run against the ranked, retrieved set (so the flag fires for *what the model actually saw*)? The second is closer to the vision's phrasing and would tie the algorithm and skills more tightly, but it moves the check to the retrieval path and changes its cost profile.

### T3. Skill scope is agent-only, but the vision implies codebase/task scope

`scope` currently means "which agents." The vision (and the earlier discussion) implies skills scoped to a **codebase/repo** or a **task type** (e.g. "PR review", "web-app tests in Python"). "Which skill is relevant to this memory?" is currently answered by agent membership plus token overlap, not by structured scope.

**Decision needed:** Model scope as (a) repo identity, (b) task-type tags, (c) both, or (d) leave agent-only and rely on overlap matching? Structured scope would sharpen conflict relevance but adds schema and matching surface.

### T4. Distilled-knowledge prioritization is implicit

Consolidation keeps a canonical and archives copies, but the ranker has no explicit "canonical beats sources" signal. If the vision wants distilled knowledge *prioritized* for injection, that may warrant a dedicated ranking signal — which is an algorithm change (triggers the `agentdb-algorithm-review` skill).

**Decision needed:** Is confidence+archival sufficient, or should distilled knowledge get an explicit ranking priority?

### T5. Recent-activity signal for skills — deliberately absent

Earlier framing suggested skills prioritized by "recent user activity." The corrected vision drops this, and the current code deliberately excludes retrieval frequency from skill health (`_compute_health`: "retrieval frequency is not a signal of content freshness"). This is **consistent with the corrected vision** and listed only to record that it was considered and rejected, so no one re-introduces it by accident.

---

## What "done" looks like for the vision

The vision is fully realized when, and only when:

1. The ranker + token budgets deliver relevant, distilled, token-lean context — **and** distilled knowledge is explicitly favored (T4 resolved).
2. **Skills are not injected in any default configuration**, and the conflict/staleness signals are the only skill outputs (T1 resolved; Principle 4a and code agree).
3. A conflict flag fires specifically when *relevant and recent* memory contradicts a skill — with a clear, documented answer to whether that is write-time or retrieval-time (T2 resolved), and with a documented skill-scope model (T3 resolved).

Until then, this document's Assessment table is the source of truth for what is real; do not describe a pillar as implemented in `README.md` or elsewhere until its "Needs review" rows are "Followed."
