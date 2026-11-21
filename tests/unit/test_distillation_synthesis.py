"""Tests for structured, scoped-know-how distillation synthesis (spec §8).

When an llm_fn is supplied, consolidation asks for a JSON object with
know_how + retrieval metadata (workspace_id/repository_id/task_type/environment).
Plain-text answers still work (treated as know_how). The resulting canonical
carries the metadata and is preferred over its archived raw sources.
"""
import json

from agentdb import AgentDB
from agentdb.governance.lifecycle_manager import _parse_synthesis, _extract_json_object


def _two_clusterable(db, agent="a1", **kw):
    db.write("memory/komper-scraper-run-note",
             "run pipeline.runner from repo root", origin="agent_inferred",
             agent_id=agent, **kw)
    db.write("memory/komper-scraper-run-update",
             "run pipeline.runner from repo root confirmed", origin="agent_inferred",
             agent_id=agent, **kw)


def test_parse_synthesis_json():
    raw = json.dumps({
        "know_how": "Run `python -m pipeline.runner` from the repo root.",
        "workspace_id": "komper",
        "repository_id": "github/komper",
        "task_type": "ops",
        "environment": "dev",
        "confidence": 0.8,
    })
    value, meta = _parse_synthesis(raw, "fallback")
    assert value == "Run `python -m pipeline.runner` from the repo root."
    assert meta["workspace_id"] == "komper"
    assert meta["repository_id"] == "github/komper"
    assert meta["task_type"] == "ops"
    assert meta["environment"] == "dev"
    assert meta["confidence"] == 0.8


def test_parse_synthesis_fenced_and_null_fields():
    raw = "```json\n" + json.dumps({
        "know_how": "Category 8 returns 206; 17 returns 400 null-ref.",
        "workspace_id": "komper", "task_type": None, "environment": "null",
    }) + "\n```"
    value, meta = _parse_synthesis(raw, "x")
    assert value.startswith("Category 8")
    assert meta["workspace_id"] == "komper"
    assert "task_type" not in meta  # null ignored
    assert "environment" not in meta  # "null" ignored


def test_parse_synthesis_plain_text_fallback():
    value, meta = _parse_synthesis("just some plain text know-how", "fb")
    assert value == "just some plain text know-how"
    assert meta == {}


def test_parse_synthesis_json_without_know_how_uses_raw():
    raw = json.dumps({"workspace_id": "x"})  # no know_how
    value, meta = _parse_synthesis(raw, "fb")
    assert value == raw  # raw text preserved
    assert meta == {}


def test_extract_json_from_prose():
    text = 'Here you go: {"know_how": "do x"} — thanks'
    obj = _extract_json_object(text)
    assert obj == {"know_how": "do x"}


def test_canonical_carries_synthesized_metadata(tmp_path):
    db = AgentDB(path=tmp_path / "t.db")
    _two_clusterable(db)
    payload = json.dumps({
        "know_how": "Run scraper from repo root with `python -m pipeline.runner`.",
        "workspace_id": "komper",
        "repository_id": "github/komper",
        "task_type": "ops",
        "environment": "dev",
        "confidence": 0.9,
    })
    db.consolidate("a1", since_hours=0, min_cluster_size=2, llm_fn=lambda prompt: payload)
    canon = [r for r in db._store.list_all()
             if r.agent_id == "a1" and getattr(r, "kind", "source") == "derived_canonical"]
    assert len(canon) == 1
    c = canon[0]
    assert c.value.startswith("Run scraper from repo root")
    assert c.workspace_id == "komper"
    assert c.repository_id == "github/komper"
    assert c.task_type == "ops"
    assert c.environment == "dev"
    assert c.confidence == 0.9
    assert c.derivation["strategy"] == "cluster_synthesis"


def test_canonical_inherits_scope_from_source_when_model_omits_it(tmp_path):
    db = AgentDB(path=tmp_path / "t.db")
    _two_clusterable(db, workspace_id="komper", task_type="ops")
    # Model returns only know_how — metadata should inherit from source.
    db.consolidate("a1", since_hours=0, min_cluster_size=2,
                   llm_fn=lambda prompt: json.dumps({"know_how": "reusable rule"}))
    c = [r for r in db._store.list_all()
         if getattr(r, "kind", "source") == "derived_canonical"][0]
    assert c.value == "reusable rule"
    assert c.workspace_id == "komper"   # inherited from source
    assert c.task_type == "ops"


def test_retrieval_prefers_canonical_over_archived_raw(tmp_path):
    db = AgentDB(path=tmp_path / "t.db")
    _two_clusterable(db)
    db.consolidate("a1", since_hours=0, min_cluster_size=2,
                   llm_fn=lambda prompt: json.dumps({"know_how": "run from repo root"}))
    # Raw sources are now archived; retrieval returns the distilled canonical.
    result = db.retrieve("how to run the scraper from repo root", agent_id="a1")
    kinds = [getattr(r, "kind", "source") for r in result.records]
    assert "derived_canonical" in kinds
    # No active (non-archived) raw source for this topic is returned.
    assert all(getattr(r, "kind", "source") == "derived_canonical" or not r.archived
               for r in result.records)
    # The distilled value is present, not the raw source text.
    assert any("run from repo root" in str(r.value) for r in result.records)
