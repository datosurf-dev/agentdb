"""
Multi-signal memory ranking algorithm.

This is the algorithmic core of AgentDB. It is not the marketing lead —
it is what makes the database worth using.

Signals (weights are starting defaults, all configurable):
  provenance  (0.35) — human_approved > provider_ingested > agent_inferred
                        Novel: trust level as a first-class ranking factor.
  recency     (0.25) — exponential decay from last_retrieved (falling back
                        to created_at for a record never retrieved),
                        modulated by retrieval frequency. Frequently-
                        retrieved memories decay slower.
  confidence  (0.25) — direct passthrough of record.confidence (0-1).
  causal      (0.15) — how often was this memory retrieved before
                        successful task completions? Phase 2
                        approximation: retrieval_count weighted by
                        recency. Full WAL-correlation in Phase 4.

Semantic signal (cosine similarity against query embedding) is added in
Phase 3 when embeddings are active. Until then, semantic weight is 0 and
the remaining four signals carry full weight.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.models import MemoryRecord, Origin


@dataclass
class RankedRecord:
    record: MemoryRecord
    composite_score: float
    signal_breakdown: dict
    conflict_flag: bool
    staleness_warning: bool


_PROVENANCE_SCORES = {
    Origin.HUMAN_APPROVED: 1.0,
    Origin.PROVIDER_INGESTED: 0.6,
    Origin.AGENT_INFERRED: 0.3,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class MultiSignalRanker:
    """
    All weights must sum to 1.0.
    semantic_weight is 0.0 until Phase 3 activates embeddings.
    """

    def __init__(
        self,
        weight_provenance: float = 0.35,
        weight_recency: float = 0.25,
        weight_confidence: float = 0.25,
        weight_causal: float = 0.15,
        weight_semantic: float = 0.0,  # activated in Phase 3
        weight_workspace: float = 0.0,  # Jaccard overlap with session working dirs
        recency_lambda: float = 0.01,  # decay rate; 0.01 ~= half-life 69 days
        staleness_days: int = 90,
        provenance_scores: Optional[dict] = None,
    ):
        total = (
            weight_provenance + weight_recency + weight_confidence
            + weight_causal + weight_semantic + weight_workspace
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"Signal weights must sum to 1.0, got {total}")
        self.w_provenance = weight_provenance
        self.w_recency = weight_recency
        self.w_confidence = weight_confidence
        self.w_causal = weight_causal
        self.w_semantic = weight_semantic
        self.w_workspace = weight_workspace
        self._lambda = recency_lambda
        self._staleness_days = staleness_days
        self._provenance = provenance_scores or _PROVENANCE_SCORES

    def rank(
        self,
        query: str,
        candidates: list,
        agent_id: str,
        query_embedding: Optional[list] = None,
        session_id: Optional[str] = None,
        session_working_dirs: Optional[list] = None,
    ) -> list:
        if not candidates:
            return []

        now = _utcnow()
        results = []
        for record in candidates:
            prov = self._provenance_score(record)
            rec = self._recency_score(record, now)
            conf = record.confidence
            causal = self._causal_score(record, now)
            embedding = getattr(record, "embedding", None)
            sem = (
                self._semantic_score(query_embedding, embedding)
                if query_embedding and embedding else 0.0
            )
            workspace = self._workspace_score(record, session_working_dirs)

            composite = (
                self.w_provenance * prov
                + self.w_recency * rec
                + self.w_confidence * conf
                + self.w_causal * causal
                + self.w_semantic * sem
                + self.w_workspace * workspace
            )

            ref_time = self._reference_time(record)
            days_since = (now - ref_time).total_seconds() / 86400
            results.append(RankedRecord(
                record=record,
                composite_score=round(min(max(composite, 0.0), 1.0), 6),
                signal_breakdown={
                    "provenance": round(prov, 4),
                    "recency": round(rec, 4),
                    "confidence": round(conf, 4),
                    "causal": round(causal, 4),
                    "semantic": round(sem, 4),
                    "workspace": round(workspace, 4),
                },
                conflict_flag=bool(getattr(record, "conflict_id", None)),
                staleness_warning=days_since > self._staleness_days,
            ))

        results.sort(key=lambda x: x.composite_score, reverse=True)
        return results

    def _reference_time(self, record: MemoryRecord) -> datetime:
        ref = getattr(record, "last_retrieved", None) or record.created_at
        return _aware(ref)

    def _provenance_score(self, record: MemoryRecord) -> float:
        return self._provenance.get(record.origin, 0.3)

    def _recency_score(self, record: MemoryRecord, now: datetime) -> float:
        """
        Exponential decay from last_retrieved (or created_at if never
        retrieved). Frequently-retrieved memories decay slower:
          score = exp(-lambda * days) * (1 + log(retrieval_count + 1) * 0.1)
        Capped at 1.0.
        """
        ref_time = self._reference_time(record)
        days = max((now - ref_time).total_seconds() / 86400, 0)
        base = math.exp(-self._lambda * days)
        freq_boost = 1.0 + math.log(record.retrieval_count + 1) * 0.1
        return min(base * freq_boost, 1.0)

    def _causal_score(self, record: MemoryRecord, now: datetime) -> float:
        """
        Phase 2 approximation: retrieval frequency weighted by recency.
        High retrieval_count + recent last_retrieved = high causal
        relevance. Full WAL-derived correlation deferred to Phase 4.
        """
        if record.retrieval_count == 0:
            return 0.0
        freq_score = min(math.log(record.retrieval_count + 1) / math.log(101), 1.0)
        recency_weight = self._recency_score(record, now)
        return freq_score * recency_weight

    def _workspace_score(
        self, record: "MemoryRecord", session_working_dirs: Optional[list]
    ) -> float:
        """Jaccard similarity between record's working_dirs and session's working_dirs.

        Returns 0.5 (neutral) when either side has no data, so legacy records
        are not penalized and sessions with no working_dirs don't distort scores.
        """
        rec_dirs = set(getattr(record, "working_dirs", []) or [])
        sess_dirs = set(session_working_dirs or [])
        if not rec_dirs or not sess_dirs:
            return 0.5
        intersection = rec_dirs & sess_dirs
        union = rec_dirs | sess_dirs
        return len(intersection) / len(union)

    def _semantic_score(self, query_embedding: list, record_embedding: list) -> float:
        """Cosine similarity. Uses numpy if available, falls back to pure
        Python otherwise."""
        try:
            import numpy as np
            a = np.array(query_embedding)
            b = np.array(record_embedding)
            denom = np.linalg.norm(a) * np.linalg.norm(b)
            return float(np.dot(a, b) / denom) if denom > 0 else 0.0
        except ImportError:
            dot = sum(x * y for x, y in zip(query_embedding, record_embedding))
            mag_a = math.sqrt(sum(x * x for x in query_embedding))
            mag_b = math.sqrt(sum(x * x for x in record_embedding))
            return max(dot / (mag_a * mag_b), 0.0) if mag_a and mag_b else 0.0
