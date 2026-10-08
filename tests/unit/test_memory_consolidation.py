import pytest
from datetime import datetime, timezone, timedelta
from agentdb import AgentDB


def _db(tmp_path):
    return AgentDB(path=tmp_path / "test.db")


# ── Core clustering and archival ─────────────────────────────────────────────

def test_consolidate_merges_near_duplicate_records(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    result = db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert result.clusters_found >= 1
    assert result.records_archived >= 1


def test_consolidate_archives_lower_confidence_records(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "low conf", origin="agent_inferred",
             agent_id="a1", confidence=0.4)
    db.write("memory/analytics-schema-update", "high conf", origin="agent_inferred",
             agent_id="a1", confidence=0.9)
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    active = [r for r in db._store.list_all() if r.agent_id == "a1"]
    assert len(active) == 1
    assert active[0].confidence == 0.9


def test_canonical_marked_consolidated(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    active = [r for r in db._store.list_all() if r.agent_id == "a1"]
    assert len(active) == 1
    assert active[0].consolidated is True


def test_source_record_ids_traced_on_canonical(tmp_path):
    db = _db(tmp_path)
    r1 = db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    r2 = db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    active = [r for r in db._store.list_all() if r.agent_id == "a1"]
    assert len(active) == 1
    assert set(active[0].source_record_ids) == {r1.id, r2.id}


# ── Injection control ─────────────────────────────────────────────────────────

def test_include_consolidated_false_excludes_canonical(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    result = db.retrieve("analytics schema", agent_id="a1", include_consolidated=False)
    assert all(not r.consolidated for r in result.records)


def test_include_consolidated_true_returns_canonical_by_default(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    result = db.retrieve("analytics schema", agent_id="a1")
    assert any(r.consolidated for r in result.records)


# ── Skill candidate dispatch ──────────────────────────────────────────────────

def test_consolidate_dispatches_untracked_skill_candidate(tmp_path):
    db = _db(tmp_path)
    for i in range(5):
        db.write(f"memory/analytics-schema-record-{i}", f"value {i}",
                 origin="agent_inferred", agent_id="a1")
    fired = []
    db.gateway.subscribe("untracked_skill_candidate", lambda **kw: fired.append(kw))
    db.consolidate("a1", since_hours=0, min_cluster_size=2, skill_candidate_threshold=3)
    assert len(fired) >= 1
    assert "agent_id" in fired[0]
    assert "dominant_words" in fired[0]
    assert "sample_keys" in fired[0]


def test_consolidate_no_dispatch_when_skill_exists(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill("analytics-schema", "Analytics Schema", "Revenue.", scope=["a1"])
    for i in range(5):
        db.write(f"memory/analytics-schema-record-{i}", f"value {i}",
                 origin="agent_inferred", agent_id="a1")
    fired = []
    db.gateway.subscribe("untracked_skill_candidate", lambda **kw: fired.append(kw))
    db.consolidate("a1", since_hours=0, min_cluster_size=2, skill_candidate_threshold=3)
    assert fired == []


# ── Correctness and safety ────────────────────────────────────────────────────

def test_consolidate_human_approved_immune(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-policy", "authoritative rule",
             origin="human_approved", agent_id="a1")
    db.write("memory/analytics-schema-note", "agent observation",
             origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    human = [r for r in db._store.list_all() if r.origin == "human_approved"]
    assert len(human) == 1
    assert not human[0].archived


def test_consolidate_only_touches_agent_records(tmp_path):
    db = _db(tmp_path)
    db.write("memory/orders-model-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/orders-model-update", "v2", origin="agent_inferred", agent_id="other")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    other = [r for r in db._store.list_all() if r.agent_id == "other"]
    assert len(other) == 1


def test_consolidate_idempotent(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2)
    r2 = db.consolidate("a1", since_hours=0, min_cluster_size=2)
    assert r2.records_archived == 0


# ── LLM synthesis ─────────────────────────────────────────────────────────────

def test_consolidate_with_llm_fn_synthesizes_value(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "old note", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "old update", origin="agent_inferred", agent_id="a1")
    db.consolidate("a1", since_hours=0, min_cluster_size=2,
                   llm_fn=lambda prompt: "synthesized canonical memory")
    active = [r for r in db._store.list_all() if not r.archived and r.agent_id == "a1"]
    assert any("synthesized" in str(r.value) for r in active)


def test_llm_canonical_triggers_skill_conflict_recheck(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue schema.", scope=["a1"],
    )
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    fired.clear()  # discard fires from write() calls above
    db.consolidate("a1", since_hours=0, min_cluster_size=2,
                   llm_fn=lambda prompt: "analytics schema synthesized")
    assert any(f.get("reason") == "keyword_overlap" for f in fired)


def test_non_llm_consolidation_does_not_recheck_skill_conflict(tmp_path):
    db = _db(tmp_path)
    db.skills_api.create_skill(
        "analytics-schema", "Analytics Schema", "Revenue schema.", scope=["a1"],
    )
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    fired = []
    db.gateway.subscribe("skill_review_needed", lambda **kw: fired.append(kw))
    fired.clear()
    db.consolidate("a1", since_hours=0, min_cluster_size=2)  # no llm_fn
    assert fired == []


# ── Auto-trigger ──────────────────────────────────────────────────────────────

def test_auto_trigger_fires_after_threshold(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with db._store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO consolidation_state (agent_id, last_run_at, session_count) VALUES (?, ?, ?)",
            ("a1", old_ts, 9),
        )
    # 10th session — both conditions met
    db.gateway.dispatch("conversation_started", session_id="s10", agent_id="a1",
                        opening_message="let's review")
    active = [r for r in db._store.list_all() if r.agent_id == "a1"]
    assert len(active) <= 1  # cluster consolidated


def test_auto_trigger_does_not_fire_before_session_threshold(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with db._store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO consolidation_state (agent_id, last_run_at, session_count) VALUES (?, ?, ?)",
            ("a1", old_ts, 4),  # only 4 sessions — under 10
        )
    db.gateway.dispatch("conversation_started", session_id="s5", agent_id="a1",
                        opening_message="let's review")
    active = [r for r in db._store.list_all() if r.agent_id == "a1"]
    assert len(active) == 2  # untouched


def test_auto_trigger_resets_session_count(tmp_path):
    db = _db(tmp_path)
    db.write("memory/analytics-schema-note", "v1", origin="agent_inferred", agent_id="a1")
    db.write("memory/analytics-schema-update", "v2", origin="agent_inferred", agent_id="a1")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with db._store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO consolidation_state (agent_id, last_run_at, session_count) VALUES (?, ?, ?)",
            ("a1", old_ts, 9),
        )
    db.gateway.dispatch("conversation_started", session_id="s10", agent_id="a1",
                        opening_message="review")
    with db._store._connect() as conn:
        row = conn.execute(
            "SELECT session_count FROM consolidation_state WHERE agent_id = ?", ("a1",)
        ).fetchone()
    assert row is not None
    assert row["session_count"] == 0
