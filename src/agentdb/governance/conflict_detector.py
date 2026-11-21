"""
Conflict detector — temporal, authority, and semantic conflicts.

Temporal:  same key + same origin → two records with equal authority, neither
           supersedes the other.
Authority: agent_inferred / provider_ingested tries to overwrite human_approved
           → the agent has less authority than the existing human-approved value.
Semantic:  `check_semantic_batch` flags near-duplicate/contradictory values by
           embedding cosine similarity — active only when an embedding store is
           supplied (otherwise it is a no-op).

Human-approved overwriting anything lower is NOT a conflict — it supersedes cleanly.
"""
from __future__ import annotations

import re

from agentdb.core.models import ConflictRecord, MemoryRecord

# Origins that are considered "agent-tier" (lower authority than human_approved)
_AGENT_TIER_ORIGINS = {"agent_inferred", "provider_ingested"}

# Value-compatibility classifications (see CONTEXTUAL-SPEC §4 and
# FINAL-HARDENING §5). Only EQUIVALENT and COMPATIBLE_COMPLEMENT may merge.
COMPATIBLE = "compatible"           # kept for backward-compat callers
CONTEXT_SPECIFIC_DIFFERENCE = "context_specific_difference"
VERIFIED_SUPERSESSION = "verified_supersession"
UNRESOLVED_CONTRADICTION = "unresolved_contradiction"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"
# FINAL-HARDENING §5 consolidation-policy outcomes (aliases / additions):
EQUIVALENT = "equivalent"
COMPATIBLE_COMPLEMENT = "compatible_complement"
CONFLICTING = "conflicting"
SCOPE_INCOMPATIBLE = "scope_incompatible"

# Only these outcomes permit consolidation (FINAL-HARDENING §5).
MERGEABLE_OUTCOMES = frozenset({EQUIVALENT, COMPATIBLE_COMPLEMENT})

# Negation / modality markers. A value and its negation are never compatible.
_NEGATIONS = frozenset({"not", "no", "never", "without", "nor", "none", "cannot", "mustnt", "dont", "doesnt", "isnt", "wont"})
_NEG_CONTRACTIONS = re.compile(r"(?:n't|n’t)$", re.IGNORECASE)


def _negated(tokens: list[str]) -> bool:
    """True if the token list contains a negation marker (incl. contractions)."""
    for t in tokens:
        if t in _NEGATIONS:
            return True
        if _NEG_CONTRACTIONS.search(t):
            return True
    return False


def _strip_negation(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in _NEGATIONS and not _NEG_CONTRACTIONS.search(t)]


# Tiny stop-word set so subject overlap is measured on content words.
_STOP = frozenset({"the", "a", "an", "is", "are", "be", "to", "of", "in", "on",
                   "for", "and", "or", "with", "as", "at", "by", "it", "this",
                   "that", "uses", "use", "using"})


def _content_tokens(tokens: list[str]) -> set:
    return {t for t in tokens if t not in _STOP and not _is_value_token(t)}


def _shares_subject(ta: list[str], tb: list[str]) -> bool:
    """True if two token lists share at least two non-trivial content tokens
    (evidence they describe the same subject), so a prose-only difference is a
    compatible complement rather than novel unrelated content."""
    ca, cb = _content_tokens(ta), _content_tokens(tb)
    return len(ca & cb) >= 2

# Tokens that denote a *value* (a version, number, or hyphenated enum/identifier
# such as "us-east", "eu-west", "port-8080") rather than prose. A single
# differing value-token in otherwise-parallel assertions is a scope-specific
# value difference / unresolved contradiction, NOT a safe merge. Narrow on
# purpose: no natural-language contradiction engine.
_VALUE_TOKEN_RE = re.compile(r"^(v?\d+(?:\.\d+)*|[a-z]?\d+[a-z0-9]*|[a-z0-9]+(?:-[a-z0-9]+)+)$", re.IGNORECASE)


def _norm_tokens(value) -> list[str]:
    """Tokenize a record value deterministically for comparison.

    Whitespace/dotted/comma separated; hyphenated compounds ("us-east") stay a
    single token so they compare as one value slot.
    """
    text = str(value).lower()
    return [t for t in re.split(r"[^a-z0-9.\-]+", text) if t]


def _is_value_token(tok: str) -> bool:
    return bool(_VALUE_TOKEN_RE.match(tok))


class ConflictDetector:
    """Checks whether a new write conflicts with an existing record."""

    def check(
        self,
        new_record: MemoryRecord,
        existing: MemoryRecord,
    ) -> ConflictRecord | None:
        """
        Returns a ConflictRecord if new_record conflicts with existing, else None.

        Rules:
        - Different keys → no conflict (caller should not invoke in this case).
        - Same key + same origin → temporal conflict.
        - New is agent_inferred/provider_ingested AND existing is human_approved
          → authority conflict.
        - New is human_approved, existing is anything lower → no conflict (supersedes).
        """
        if new_record.key != existing.key:
            return None

        new_origin = str(new_record.origin)
        existing_origin = str(existing.origin)

        # Authority conflict: agent/provider overwriting human_approved
        if existing_origin == "human_approved" and new_origin in _AGENT_TIER_ORIGINS:
            return ConflictRecord(
                record_a_id=existing.id,
                record_b_id=new_record.id,
                conflict_type="authority",
            )

        # Human supersedes lower-trust: no conflict (only when existing is NOT also human_approved)
        if new_origin == "human_approved" and existing_origin in _AGENT_TIER_ORIGINS:
            return None

        # Same origin → temporal conflict (covers human_approved vs human_approved too)
        if new_origin == existing_origin:
            return ConflictRecord(
                record_a_id=existing.id,
                record_b_id=new_record.id,
                conflict_type="temporal",
            )

        return None

    @staticmethod
    def classify_value_compatibility(
        a_value,
        b_value,
        *,
        same_key: bool = False,
        explicit_supersession: bool = False,
    ) -> str:
        """Classify whether two record *values* are safe to combine into one
        derived canonical (CONTEXTUAL-SPEC §4). Deterministic; no NL engine.

        Returns one of: COMPATIBLE, CONTEXT_SPECIFIC_DIFFERENCE,
        VERIFIED_SUPERSESSION, UNRESOLVED_CONTRADICTION, INSUFFICIENT_EVIDENCE.

        Rules (narrow, value-oriented):
        - Identical values → COMPATIBLE.
        - Differ in exactly one token position, and that differing token is a
          value token (version / number / identifier) on both sides, with the
          remaining tokens identical → the two are *the same assertion with a
          differing value*. That is a contradiction unless explicit supersession
          is established → UNRESOLVED_CONTRADICTION (or VERIFIED_SUPERSESSION
          if ``explicit_supersession``).
        - Differ only by prose (non-value tokens) → COMPATIBLE (near-duplicate).
        - Otherwise (differ in multiple positions, or a value token appears /
          disappears) → UNRESOLVED_CONTRADICTION when the difference is
          value-bearing, else INSUFFICIENT_EVIDENCE (default = do not merge).
        """
        if a_value == b_value:
            return COMPATIBLE

        ta, tb = _norm_tokens(a_value), _norm_tokens(b_value)
        if not ta or not tb:
            return INSUFFICIENT_EVIDENCE
        if ta == tb:
            # Same tokens, different raw formatting → same assertion.
            return COMPATIBLE
        # Token multisets identical (reordering / repetition) → same assertion.
        if sorted(ta) == sorted(tb):
            return COMPATIBLE

        # Negation / modality mismatch: "X is required" vs "X is not required",
        # "always request approval" vs "deploy without approval". If, after
        # removing negation markers, the two are the same assertion but exactly
        # one side is negated → CONFLICTING (never merge). FINAL-HARDENING §5.
        neg_a, neg_b = _negated(ta), _negated(tb)
        if neg_a != neg_b:
            if sorted(_strip_negation(ta)) == sorted(_strip_negation(tb)):
                return CONFLICTING
            # Even when wording differs, opposite polarity on an otherwise
            # similar assertion is not safe to merge.
            sa, sb = _strip_negation(ta), _strip_negation(tb)
            if sa and sb and len(set(sa) & set(sb)) >= max(1, min(len(sa), len(sb)) - 1):
                return CONFLICTING

        # Aligned comparison: only meaningful when token counts match so a
        # single position can be identified as the discriminating value.
        if len(ta) == len(tb):
            diffs = [i for i, (x, y) in enumerate(zip(ta, tb)) if x != y]
            if len(diffs) == 1:
                i = diffs[0]
                if _is_value_token(ta[i]) and _is_value_token(tb[i]):
                    # A parallel assertion requires a shared subject/context
                    # (at least one identical non-value token besides the
                    # differing value). "use postgres 15" vs "use postgres 16"
                    # qualifies; bare labels "v1" vs "v2" do not (no shared
                    # context) and fall through to insufficient evidence.
                    shared_context = any(
                        (x == y and not _is_value_token(x))
                        for k, (x, y) in enumerate(zip(ta, tb))
                        if k != i
                    )
                    if not shared_context:
                        return INSUFFICIENT_EVIDENCE
                    if explicit_supersession:
                        return VERIFIED_SUPERSESSION
                    return UNRESOLVED_CONTRADICTION
                # Differing token(s) are prose, not a value → near-duplicate.
                return COMPATIBLE
            # Multiple differing positions.
            value_bearing = any(
                _is_value_token(ta[i]) or _is_value_token(tb[i]) for i in diffs
            )
            if value_bearing:
                return UNRESOLVED_CONTRADICTION
            # Prose-only differences: safe complement iff they share subject
            # context (novel prose alone is not evidence of equivalence).
            return COMPATIBLE if _shares_subject(ta, tb) else INSUFFICIENT_EVIDENCE

        # Different lengths: a value token appearing/disappearing is a real
        # distinction; a purely additive/dropped prose token that still shares
        # subject context is a compatible complement.
        value_diff = {t for t in ta if _is_value_token(t)} ^ {
            t for t in tb if _is_value_token(t)
        }
        if value_diff:
            return UNRESOLVED_CONTRADICTION
        return COMPATIBLE if _shares_subject(ta, tb) else INSUFFICIENT_EVIDENCE

    @staticmethod
    def check_semantic_batch(
        new_record: "MemoryRecord",
        candidates: list,
        embedding_store,
        threshold: float = 0.92,
    ) -> list:
        """
        Returns ConflictRecords for any candidate with cosine(new, candidate) >= threshold.
        Caps at 50 candidates. Skips if embedding_store is None or new_record has no embedding.
        """
        if embedding_store is None:
            return []

        new_vec = embedding_store.get("memories", new_record.id)
        if new_vec is None:
            return []

        from ..embeddings.provider import _cosine

        capped = [c for c in candidates if c.id != new_record.id][:50]
        if not capped:
            return []

        candidate_ids = [c.id for c in capped]
        stored = embedding_store.get_batch("memories", candidate_ids)

        conflicts = []
        for candidate in capped:
            c_vec = stored.get(candidate.id)
            if c_vec is None:
                continue
            if _cosine(new_vec, c_vec) >= threshold:
                conflicts.append(ConflictRecord(
                    record_a_id=candidate.id,
                    record_b_id=new_record.id,
                    conflict_type="semantic",
                ))
        return conflicts
