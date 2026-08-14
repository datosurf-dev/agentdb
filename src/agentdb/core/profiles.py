"""
Built-in AgentDB profiles.

To add a new built-in profile: define a new Profile(...) below and add it
to BUILTIN_PROFILES. To change what "budget_optimized" means: edit the
BUDGET_OPTIMIZED instance directly -- every field is named, typed, and
lives on its own dataclass below.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LifecycleSettings:
    confidence_decay_rate: float
    archival_threshold: float
    staleness_days: int
    reinforce_rate: float


@dataclass(frozen=True)
class RankerWeights:
    provenance: float
    recency: float
    confidence: float
    causal: float
    semantic: float
    workspace: float = 0.0  # opt-in; weight 0.0 = disabled


@dataclass(frozen=True)
class RetrievalSettings:
    retrieve_limit: int
    conflict_threshold: float  # currently unused elsewhere in the codebase; reserved
    min_composite_score: float  # records ranked below this score are excluded; 0.0 = no threshold
    ranker_weights: RankerWeights


@dataclass(frozen=True)
class InjectionSettings:
    context_token_limit: int
    memory_budget_chars: int
    memory_header: str
    override_prefix: str
    record_line_format: str


@dataclass(frozen=True)
class CaptureSettings:
    conversation_jsonl: bool
    tracking_max_files_per_directory: int
    auto_persist_response: bool


@dataclass(frozen=True)
class Profile:
    name: str
    lifecycle: LifecycleSettings
    retrieval: RetrievalSettings
    injection: InjectionSettings
    capture: CaptureSettings


_SHARED_INJECTION_TEXT = dict(
    memory_header="Governed memory context (higher priority than inferred context):",
    override_prefix="Override instruction:",
    record_line_format="- {key}: {value}",
)

BUDGET_OPTIMIZED = Profile(
    name="budget_optimized",
    lifecycle=LifecycleSettings(
        confidence_decay_rate=0.0, archival_threshold=0.10, staleness_days=90, reinforce_rate=0.10,
    ),
    retrieval=RetrievalSettings(
        retrieve_limit=5, conflict_threshold=0.85, min_composite_score=0.0,
        ranker_weights=RankerWeights(provenance=0.35, recency=0.25, confidence=0.25, causal=0.15, semantic=0.0, workspace=0.0),
    ),
    injection=InjectionSettings(context_token_limit=1000, memory_budget_chars=2000, **_SHARED_INJECTION_TEXT),
    capture=CaptureSettings(
        conversation_jsonl=False, tracking_max_files_per_directory=200, auto_persist_response=False,
    ),
)

FULL_STORAGE = Profile(
    name="full_storage",
    lifecycle=LifecycleSettings(
        confidence_decay_rate=0.0, archival_threshold=0.0, staleness_days=3650, reinforce_rate=0.10,
    ),
    retrieval=RetrievalSettings(
        retrieve_limit=200, conflict_threshold=0.85, min_composite_score=0.0,
        ranker_weights=RankerWeights(provenance=0.35, recency=0.25, confidence=0.25, causal=0.15, semantic=0.0, workspace=0.0),
    ),
    injection=InjectionSettings(context_token_limit=50000, memory_budget_chars=200000, **_SHARED_INJECTION_TEXT),
    capture=CaptureSettings(
        conversation_jsonl=True, tracking_max_files_per_directory=10000, auto_persist_response=True,
    ),
)

RESEARCH = Profile(
    name="research",
    lifecycle=LifecycleSettings(
        confidence_decay_rate=0.0, archival_threshold=0.05, staleness_days=180, reinforce_rate=0.10,
    ),
    retrieval=RetrievalSettings(
        retrieve_limit=50, conflict_threshold=0.85, min_composite_score=0.0,
        ranker_weights=RankerWeights(provenance=0.25, recency=0.35, confidence=0.25, causal=0.15, semantic=0.0, workspace=0.0),
    ),
    injection=InjectionSettings(context_token_limit=8000, memory_budget_chars=24000, **_SHARED_INJECTION_TEXT),
    capture=CaptureSettings(
        conversation_jsonl=True, tracking_max_files_per_directory=500, auto_persist_response=True,
    ),
)

BUILTIN_PROFILES: dict[str, Profile] = {p.name: p for p in (BUDGET_OPTIMIZED, FULL_STORAGE, RESEARCH)}
