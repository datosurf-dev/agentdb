"""
Tests for auto skill-draft proposals triggered by consolidation of ungoverned clusters.

The reconciliation pipeline:
  1. Agent writes memories about an ungoverned domain
  2. consolidate() clusters them + dispatches untracked_skill_candidate
  3. AgentDB._on_untracked_skill_candidate calls gateway.auto_propose_from_cluster()
  4. A proposal is created and skill_draft_proposed is dispatched
"""
from agentdb import AgentDB


def _db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


# ------ helpers ------

def _write_cluster(db, prefix, count, value, agent_id, confidence=0.7):
    """Write `count` memories whose keys share two 4+ char words with each other."""
    for i in range(count):
        db.write(
            f"memory/{prefix}/record-{i}",
            value,
            origin="agent_inferred",
            agent_id=agent_id,
            confidence=confidence,
        )


def _consolidate(db, agent_id, threshold=3):
    """Consolidate with since_hours=0 so freshly written records are included."""
    db.consolidate(agent_id, since_hours=0, min_cluster_size=2,
                   skill_candidate_threshold=threshold)


# ------ Task 1 tests ------

def test_consolidate_proposes_draft_when_no_governing_skill(tmp_path):
    db = _db(tmp_path)
    _write_cluster(db, "orders-schema", 3, "orders table schema updated", "agent1")

    proposed = []
    db.gateway.subscribe("skill_draft_proposed", lambda **kw: proposed.append(kw))
    _consolidate(db, "agent1")

    assert len(proposed) >= 1
    assert "name" in proposed[0]


def test_skill_draft_proposed_includes_proposal_id_and_agent_id(tmp_path):
    db = _db(tmp_path)
    _write_cluster(db, "orders-schema", 3, "orders table schema updated", "agent1")

    proposed = []
    db.gateway.subscribe("skill_draft_proposed", lambda **kw: proposed.append(kw))
    _consolidate(db, "agent1")

    assert len(proposed) >= 1
    assert "proposal_id" in proposed[0]
    assert proposed[0]["agent_id"] == "agent1"


def test_proposal_appears_in_list_proposals(tmp_path):
    db = _db(tmp_path)
    _write_cluster(db, "orders-schema", 3, "orders table schema updated", "agent1")
    _consolidate(db, "agent1")

    proposals = db.gateway.list_proposals()
    assert len(proposals) >= 1
    assert all("name" in p for p in proposals)


def test_duplicate_proposals_are_not_created(tmp_path):
    db = _db(tmp_path)
    _write_cluster(db, "orders-schema", 3, "orders table schema updated", "agent1")
    _consolidate(db, "agent1")
    proposals_after_first = len(db.gateway.list_proposals())

    # Write fresh records for the same domain and consolidate again
    _write_cluster(db, "orders-schema", 3, "orders table schema updated", "agent1")
    _consolidate(db, "agent1")
    proposals_after_second = len(db.gateway.list_proposals())

    assert proposals_after_second == proposals_after_first, (
        "A second consolidation of the same domain must not create a duplicate proposal"
    )


def test_existing_skill_suppresses_auto_proposal(tmp_path):
    db = _db(tmp_path)
    # Create a skill that governs the "orders-schema" domain
    db.skills_api.create_skill(
        "orders-schema", "Orders Schema", "Orders table docs.", scope=["agent1"]
    )
    _write_cluster(db, "orders-schema", 3, "orders table schema updated", "agent1")

    proposed = []
    db.gateway.subscribe("skill_draft_proposed", lambda **kw: proposed.append(kw))
    _consolidate(db, "agent1")

    assert proposed == [], "governed cluster must not auto-propose a draft"


def test_two_independent_clusters_each_propose_once(tmp_path):
    db = _db(tmp_path)
    # Use key patterns that share NO structural words across the two clusters.
    # _lc_cluster groups by min_overlap=2 word overlap on key words (4+ chars).
    # Cluster A keys share: orders, table, schema  (no overlap with cluster B)
    # Cluster B keys share: revenue, source, pipeline  (no overlap with cluster A)
    for i in range(3):
        db.write(f"orders/table/schema-{i}", "orders table schema updated",
                 origin="agent_inferred", agent_id="agent1", confidence=0.7)
        db.write(f"revenue/source/pipeline-{i}", "revenue source pipeline updated",
                 origin="agent_inferred", agent_id="agent1", confidence=0.7)

    proposed = []
    db.gateway.subscribe("skill_draft_proposed", lambda **kw: proposed.append(kw))
    db.consolidate("agent1", since_hours=0, min_cluster_size=2, skill_candidate_threshold=3)

    names = {p["name"] for p in proposed}
    assert len(names) == 2, f"Expected 2 distinct proposals, got {names}"


def test_auto_proposal_can_be_approved(tmp_path):
    db = _db(tmp_path)
    _write_cluster(db, "orders-schema", 3, "orders table schema updated", "agent1")
    _consolidate(db, "agent1")

    proposals = db.gateway.list_proposals()
    assert len(proposals) >= 1
    proposal_id = proposals[0]["proposal_id"]

    db.gateway.approve(proposal_id)
    skills = db.skills.get_for_agent("agent1")
    assert any(s.id == proposal_id for s in skills)


def test_no_proposal_when_no_ungoverned_cluster(tmp_path):
    db = _db(tmp_path)
    # Only 1 record — can't form a cluster
    db.write("memory/orders-schema/single", "one record only",
             origin="agent_inferred", agent_id="agent1", confidence=0.7)
    proposed = []
    db.gateway.subscribe("skill_draft_proposed", lambda **kw: proposed.append(kw))
    _consolidate(db, "agent1", threshold=2)
    assert proposed == []
