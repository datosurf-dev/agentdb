import pytest
from agentdb import AgentDB


def _db(tmp_path, embed_fn=None):
    return AgentDB(path=tmp_path / "test.db", embed_fn=embed_fn)


def test_keyword_overlap_fires_skill_review_needed(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table docs.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/analytics-schema-notes", "analytics schema changed",
             origin="agent_inferred", agent_id="agent1")
    assert len(fired) == 1
    assert fired[0]["skill_id"] == "analytics-schema"
    assert fired[0]["reason"] == "keyword_overlap"
    assert fired[0]["agent_id"] == "agent1"


def test_no_overlap_no_dispatch(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/meeting-notes", "standup went well",
             origin="agent_inferred", agent_id="agent1")
    assert fired == []


def test_short_words_do_not_match(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill("sql-db", "SQL DB", "Use proper joins.", scope=["agent1"])
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/anything", "some value", origin="agent_inferred", agent_id="agent1")
    assert fired == []


def test_entity_list_overlap_fires(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "orders-model", "Orders Model", "The orders table.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/task", "updated the model", origin="agent_inferred",
             agent_id="agent1", entities=["models/orders.sql", "orders"])
    assert len(fired) == 1
    assert fired[0]["skill_id"] == "orders-model"


def test_repo_segment_overlap_fires(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "agentdb-schema", "AgentDB Schema", "Governance rules.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/schema-note", "updated schema",
             origin="agent_inferred", agent_id="agent1",
             working_dirs=["/Users/x/agentdb"])
    assert len(fired) == 1
    assert fired[0]["skill_id"] == "agentdb-schema"


def test_no_dispatch_when_agent_has_no_scoped_skills(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    # agent2 has no scoped skills — no conflict possible
    db.write("memory/working/analytics-schema-note", "analytics schema ref",
             origin="agent_inferred", agent_id="agent2")
    assert fired == []


def test_archived_skill_does_not_fire(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table.", scope=["agent1"],
    )
    db.skills_api.archive_skill("analytics-schema", "1.0.0")
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/analytics-schema-task", "analytics ref",
             origin="agent_inferred", agent_id="agent1")
    assert fired == []


def test_out_of_scope_skill_does_not_fire(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue table.", scope=["agent2"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/analytics-schema-task", "analytics ref",
             origin="agent_inferred", agent_id="agent1")
    assert fired == []


def test_dispatch_fires_at_most_once_per_skill(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue analytics schema.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/analytics-schema-notes", "analytics schema update",
             origin="agent_inferred", agent_id="agent1")
    assert len(fired) == 1


def test_no_semantic_check_without_provider(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill("topic-skill", "Topic Skill", "Some domain.", scope=["agent1"])
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    db.write("memory/working/unrelated", "unrelated value",
             origin="agent_inferred", agent_id="agent1")
    assert [f for f in fired if f.get("reason") == "semantic_overlap"] == []


def test_keyword_and_semantic_both_covered_once_per_skill(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue analytics schema.", scope=["agent1"],
    )
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    # Key overlaps on keyword — should fire exactly once, not twice
    db.write("memory/working/analytics-schema-notes", "analytics schema update",
             origin="agent_inferred", agent_id="agent1")
    assert len([f for f in fired if f["skill_id"] == "analytics-schema"]) == 1


def test_multiple_skills_each_fire_independently(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill("analytics-schema", "Analytics Schema", "Revenue.", scope=["agent1"])
    db.skills_api.create_skill("orders-model", "Orders Model", "Orders table.", scope=["agent1"])
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    # Key overlaps with both skills
    db.write("memory/working/analytics-schema-orders-model-task", "multi-domain ref",
             origin="agent_inferred", agent_id="agent1")
    skill_ids = {f["skill_id"] for f in fired}
    assert "analytics-schema" in skill_ids
    assert "orders-model" in skill_ids
