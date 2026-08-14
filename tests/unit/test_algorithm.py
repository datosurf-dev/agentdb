from datetime import datetime, timedelta, timezone

from agentdb.core.models import MemoryRecord, Origin, TrustLevel
from agentdb.retrieval.algorithm import MultiSignalRanker


def _rec(origin=Origin.AGENT_INFERRED, trust=TrustLevel.AGENT,
         confidence=1.0, days_ago=1, retrieval_count=0, key="k"):
    r = MemoryRecord(key=key, value="v", origin=origin, trust_level=trust,
                      agent_id="a1", confidence=confidence,
                      retrieval_count=retrieval_count)
    r.last_retrieved = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return r


def test_human_approved_ranks_above_agent_inferred():
    ranker = MultiSignalRanker()
    human = _rec(origin=Origin.HUMAN_APPROVED, trust=TrustLevel.HUMAN, key="k1")
    agent = _rec(origin=Origin.AGENT_INFERRED, trust=TrustLevel.AGENT, key="k2")
    results = ranker.rank("test query", [agent, human], agent_id="a1")
    assert results[0].record.origin == Origin.HUMAN_APPROVED


def test_recent_ranks_above_stale():
    ranker = MultiSignalRanker()
    recent = _rec(days_ago=1, key="k1")
    stale = _rec(days_ago=60, key="k2")
    results = ranker.rank("test query", [stale, recent], agent_id="a1")
    assert results[0].record.key == "k1"


def test_high_confidence_ranks_above_low():
    ranker = MultiSignalRanker()
    high = _rec(confidence=0.95, days_ago=2, key="k1")
    low = _rec(confidence=0.30, days_ago=2, key="k2")
    results = ranker.rank("test query", [low, high], agent_id="a1")
    assert results[0].record.key == "k1"


def test_frequently_retrieved_ranks_higher():
    ranker = MultiSignalRanker()
    popular = _rec(retrieval_count=50, days_ago=5, key="k1")
    cold = _rec(retrieval_count=0, days_ago=5, key="k2")
    results = ranker.rank("test query", [cold, popular], agent_id="a1")
    assert results[0].record.key == "k1"


def test_result_includes_signal_breakdown():
    ranker = MultiSignalRanker()
    r = _rec()
    results = ranker.rank("query", [r], agent_id="a1")
    breakdown = results[0].signal_breakdown
    assert "provenance" in breakdown
    assert "recency" in breakdown
    assert "confidence" in breakdown
    assert "causal" in breakdown


def test_conflict_flagged_in_result():
    ranker = MultiSignalRanker()
    r = _rec()
    r.conflict_id = "some-conflict-id"
    results = ranker.rank("query", [r], agent_id="a1")
    assert results[0].conflict_flag is True


def test_no_conflict_id_means_no_conflict_flag():
    ranker = MultiSignalRanker()
    results = ranker.rank("query", [_rec()], agent_id="a1")
    assert results[0].conflict_flag is False


def test_composite_score_in_0_1():
    ranker = MultiSignalRanker()
    results = ranker.rank("query", [_rec()], agent_id="a1")
    assert 0.0 <= results[0].composite_score <= 1.0


def test_empty_candidates_returns_empty():
    ranker = MultiSignalRanker()
    assert ranker.rank("query", [], agent_id="a1") == []


def test_weights_must_sum_to_one():
    import pytest
    with pytest.raises(ValueError):
        MultiSignalRanker(weight_provenance=0.5, weight_recency=0.5,
                           weight_confidence=0.5, weight_causal=0.15)


def test_never_retrieved_record_falls_back_to_created_at_for_recency():
    ranker = MultiSignalRanker()
    r = MemoryRecord(key="k1", value="v", origin=Origin.AGENT_INFERRED,
                      trust_level=TrustLevel.AGENT, agent_id="a1")
    results = ranker.rank("query", [r], agent_id="a1")
    assert results[0].signal_breakdown["recency"] > 0.9


def _rec_with_dirs(working_dirs, **kwargs):
    """Helper: build a MemoryRecord with working_dirs set."""
    defaults = dict(
        key="memory/org/test", value="v", origin="agent_inferred",
        trust_level="agent", agent_id="ag", confidence=1.0,
    )
    defaults.update(kwargs)
    return MemoryRecord(**defaults, working_dirs=working_dirs)


def test_workspace_score_neutral_when_no_session_dirs():
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.10,
    )
    rec = _rec_with_dirs(["/home/user/repo-a"])
    # No session dirs → neutral score (0.5) for workspace signal
    score = ranker._workspace_score(rec, session_working_dirs=None)
    assert score == 0.5


def test_workspace_score_neutral_when_record_has_no_dirs():
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.10,
    )
    rec = _rec_with_dirs([])
    score = ranker._workspace_score(rec, session_working_dirs=["/home/user/repo-a"])
    assert score == 0.5


def test_workspace_score_full_overlap():
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.10,
    )
    rec = _rec_with_dirs(["/home/user/repo-a", "/home/user/repo-b"])
    score = ranker._workspace_score(
        rec, session_working_dirs=["/home/user/repo-a", "/home/user/repo-b"]
    )
    assert score == 1.0


def test_workspace_score_partial_overlap():
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.10,
    )
    rec = _rec_with_dirs(["/home/user/repo-a", "/home/user/repo-b"])
    # intersection={repo-a}, union={repo-a, repo-b, repo-c} → 1/3
    score = ranker._workspace_score(
        rec, session_working_dirs=["/home/user/repo-a", "/home/user/repo-c"]
    )
    assert abs(score - 1/3) < 1e-9


def test_workspace_score_zero_overlap():
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.10,
    )
    rec = _rec_with_dirs(["/home/user/repo-a"])
    score = ranker._workspace_score(
        rec, session_working_dirs=["/home/user/repo-b"]
    )
    assert score == 0.0


def test_workspace_boosts_matching_dir_record_above_non_matching():
    """Records written in the active working dir rank above those from other dirs."""
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.10, weight_semantic=0.0, weight_workspace=0.15,
    )
    matching = _rec_with_dirs(["/home/user/dbt"])
    different = _rec_with_dirs(["/home/user/other-repo"])

    ranked = ranker.rank(
        query="some query",
        candidates=[different, matching],
        agent_id="ag",
        session_working_dirs=["/home/user/dbt"],
    )
    # matching record should rank first
    assert ranked[0].record.working_dirs == ["/home/user/dbt"]


def test_workspace_signal_in_breakdown():
    ranker = MultiSignalRanker(
        weight_provenance=0.25, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.10,
    )
    rec = _rec_with_dirs(["/home/user/repo-a"])
    ranked = ranker.rank(
        "q", [rec], "ag", session_working_dirs=["/home/user/repo-a"]
    )
    assert "workspace" in ranked[0].signal_breakdown


def test_workspace_disabled_when_weight_zero():
    """weight_workspace=0.0 means workspace signal has no effect on scores."""
    ranker = MultiSignalRanker(
        weight_provenance=0.35, weight_recency=0.25, weight_confidence=0.25,
        weight_causal=0.15, weight_semantic=0.0, weight_workspace=0.0,
    )
    with_dirs = _rec_with_dirs(["/home/user/repo-a"])
    without_dirs = _rec_with_dirs([])

    ranked_with = ranker.rank("q", [with_dirs], "ag", session_working_dirs=["/home/user/repo-a"])
    ranked_without = ranker.rank("q", [without_dirs], "ag", session_working_dirs=["/home/user/repo-a"])
    # Scores should be identical because workspace weight is 0
    assert ranked_with[0].composite_score == ranked_without[0].composite_score
