import dataclasses

from agentdb.core.profiles import BUILTIN_PROFILES, BUDGET_OPTIMIZED, FULL_STORAGE, RESEARCH


def test_builtin_profiles_has_exactly_three_entries():
    assert set(BUILTIN_PROFILES.keys()) == {"budget_optimized", "full_storage", "research"}


def test_budget_optimized_matches_documented_values():
    assert BUDGET_OPTIMIZED.retrieval.retrieve_limit == 5
    assert BUDGET_OPTIMIZED.injection.memory_budget_chars == 2000
    assert BUDGET_OPTIMIZED.lifecycle.staleness_days == 90
    assert BUDGET_OPTIMIZED.retrieval.ranker_weights.provenance == 0.35
    assert BUDGET_OPTIMIZED.capture.conversation_jsonl is False


def test_full_storage_matches_documented_values():
    assert FULL_STORAGE.retrieval.retrieve_limit == 200
    assert FULL_STORAGE.injection.memory_budget_chars == 200000
    assert FULL_STORAGE.lifecycle.archival_threshold == 0.0
    assert FULL_STORAGE.lifecycle.staleness_days == 3650
    assert FULL_STORAGE.capture.conversation_jsonl is True


def test_research_matches_documented_values():
    assert RESEARCH.retrieval.retrieve_limit == 50
    assert RESEARCH.injection.memory_budget_chars == 24000
    assert RESEARCH.lifecycle.staleness_days == 180
    assert RESEARCH.retrieval.ranker_weights.recency == 0.35


def test_profile_converts_cleanly_to_nested_dict():
    d = dataclasses.asdict(BUDGET_OPTIMIZED)
    assert d["retrieval"]["retrieve_limit"] == 5
    assert d["injection"]["memory_header"].startswith("Governed memory context")
