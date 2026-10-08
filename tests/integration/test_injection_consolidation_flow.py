"""
Integration tests that narrate the injection + consolidation flow end-to-end.
Run these to see exactly what an agent receives at each phase:
  - repo filter: which memories survive a cross-repo session boundary
  - pre/post consolidation: what the injection set looks like before and after
  - include_consolidated=False: how to get only raw, unmerged memories
  - LLM synthesis: canonical value + full provenance trace
"""
import pytest
from agentdb import AgentDB


def test_session_repo_filter_narration(tmp_path):
    """
    Agent has memories from two repos. Opening a session on repo A injects
    only repo-A memories (plus records with no working_dirs, e.g. org policies).
    """
    db = AgentDB(path=tmp_path / "test.db")

    db.write("memory/dbt-table-note", "Revenue table has a 3-day lag",
             origin="agent_inferred", agent_id="a1", working_dirs=["/Users/x/dbt"])
    db.write("memory/agentdb-schema-note", "Governance schema requires origin field",
             origin="agent_inferred", agent_id="a1", working_dirs=["/Users/x/agentdb"])
    db.write("memory/org/policy", "Schema governance policy: all models must have a description",
             origin="human_approved", agent_id="a1")  # no working_dirs → always passes

    db.gateway.dispatch("conversation_started", session_id="s1", agent_id="a1",
                        opening_message="implement a new feature",
                        working_dirs=["/Users/x/agentdb"])

    result = db.retrieve("schema governance", agent_id="a1", session_id="s1")
    injected_keys = [r.key for r in result.records]

    assert "memory/agentdb-schema-note" in injected_keys, "same-repo memory must be injected"
    assert "memory/org/policy" in injected_keys, "org policy (no working_dirs) must pass through"
    assert "memory/dbt-table-note" not in injected_keys, "cross-repo memory must be blocked"


def test_session_pivot_expands_repo_filter(tmp_path):
    """
    If the agent pivots mid-session and writes a record from a second repo,
    the repo filter expands and records from that repo are now retrievable.
    """
    db = AgentDB(path=tmp_path / "test.db")

    db.write("memory/dbt-pipeline-note", "dbt pipeline runs at 3am",
             origin="agent_inferred", agent_id="a1", working_dirs=["/Users/x/dbt"])

    db.gateway.dispatch("conversation_started", session_id="s1", agent_id="a1",
                        opening_message="implement feature", working_dirs=["/Users/x/agentdb"])

    # Before pivot: dbt record is excluded
    before = db.retrieve("pipeline", agent_id="a1", session_id="s1")
    assert "memory/dbt-pipeline-note" not in [r.key for r in before.records]

    # Pivot: write something in the dbt repo during this session
    db.write("memory/dbt-task", "checking pipeline schedule",
             origin="agent_inferred", agent_id="a1", session_id="s1",
             working_dirs=["/Users/x/dbt"])

    # After pivot: dbt record is now included
    after = db.retrieve("pipeline", agent_id="a1", session_id="s1")
    assert "memory/dbt-pipeline-note" in [r.key for r in after.records]


def test_pre_and_post_consolidation_injection(tmp_path):
    """
    Before consolidation: multiple near-duplicate records can all surface.
    After consolidation: one canonical (marked consolidated=True) with source trace.
    """
    db = AgentDB(path=tmp_path / "test.db")

    r1 = db.write("memory/analytics-schema-note", "Revenue table was rebuilt last Tuesday",
                  origin="agent_inferred", agent_id="a1", confidence=0.6)
    r2 = db.write("memory/analytics-schema-update", "Revenue table schema changed",
                  origin="agent_inferred", agent_id="a1", confidence=0.8)
    r3 = db.write("memory/analytics-schema-v2", "New revenue table has date_trunc grouping",
                  origin="agent_inferred", agent_id="a1", confidence=0.7)

    pre = db.retrieve("analytics schema", agent_id="a1")
    assert len(pre.records) >= 1  # at least one surfaces pre-consolidation

    result = db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert result.clusters_found >= 1
    assert result.records_archived >= 1

    active = [r for r in db._store.list_all() if r.agent_id == "a1"]
    assert len(active) < 3, "consolidation should reduce the active record count"

    canonical = next((r for r in active if r.consolidated), None)
    assert canonical is not None, "surviving record must be marked consolidated=True"
    assert set(canonical.source_record_ids) == {r1.id, r2.id, r3.id}


def test_include_consolidated_controls_injection(tmp_path):
    """
    After consolidation, include_consolidated=False lets the caller retrieve only
    raw, non-consolidated memories — useful when the agent wants to re-evaluate
    from the raw signal rather than the merged summary.
    """
    db = AgentDB(path=tmp_path / "test.db")
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)

    with_consolidated = db.retrieve("analytics schema", agent_id="a1")
    without_consolidated = db.retrieve("analytics schema", agent_id="a1",
                                       include_consolidated=False)

    assert any(r.consolidated for r in with_consolidated.records), \
        "default retrieve must include the consolidated canonical"
    assert not any(r.consolidated for r in without_consolidated.records), \
        "include_consolidated=False must filter out consolidated records"


def test_llm_consolidation_synthesizes_and_retraces(tmp_path):
    """
    With an LLM synthesizer the canonical value becomes a synthesis of all cluster
    members. source_record_ids still traces all originals. The synthesized canonical
    triggers a skill conflict re-check so governance catches domain drift.
    """
    db = AgentDB(path=tmp_path / "test.db")
    db.skills_api.create_skill(
        "orders-model", "Orders Model", "The orders domain.", scope=["a1"],
    )
    r1 = db.write("memory/orders-model-note", "Orders table was refactored",
                  origin="agent_inferred", agent_id="a1", confidence=0.7)
    r2 = db.write("memory/orders-model-update", "Orders table has new partition key",
                  origin="agent_inferred", agent_id="a1", confidence=0.9)

    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    fired.clear()  # discard write() signals

    db.consolidate(
        "a1",
        since_hours=0,
        min_cluster_size=2,
        llm_fn=lambda prompt: "Orders model was refactored with a new date-based partition key.",
    )

    active = [r for r in db._store.list_all() if r.agent_id == "a1"]
    assert len(active) == 1
    canonical = active[0]
    assert "partition" in str(canonical.value), "LLM value should be the synthesized text"
    assert set(canonical.source_record_ids) == {r1.id, r2.id}
    assert canonical.consolidated is True
    # Skill conflict re-check fired for the canonical
    assert any(f["skill_id"] == "orders-model" for f in fired), \
        "skill conflict must be re-checked after LLM synthesis"
