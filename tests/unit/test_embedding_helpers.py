import json
import pytest
from agentdb.core.models import MemoryRecord, Skill
from agentdb.embeddings.text import _embed_text_for_record, _embed_text_for_skill


def _rec(key="user/pref/format", value="markdown"):
    return MemoryRecord(
        key=key, value=value, origin="agent_inferred",
        trust_level="agent", agent_id="a1"
    )


def test_memory_record_embedding_field_defaults_none():
    r = _rec()
    assert r.embedding is None


def test_memory_record_embedding_field_can_be_set():
    r = _rec()
    r.embedding = [0.1, 0.2, 0.3]
    assert r.embedding == [0.1, 0.2, 0.3]


def test_embed_text_for_record_includes_readable_key():
    r = _rec(key="user/pref/format", value="markdown")
    text = _embed_text_for_record(r)
    assert "user pref format" in text


def test_embed_text_for_record_includes_string_value():
    r = _rec(key="k", value="hello world")
    text = _embed_text_for_record(r)
    assert "hello world" in text


def test_embed_text_for_record_json_encodes_dict_value():
    r = _rec(key="k", value={"mode": "fast", "level": 3})
    text = _embed_text_for_record(r)
    assert "mode" in text
    assert "fast" in text


def test_embed_text_for_record_json_encodes_list_value():
    r = _rec(key="k", value=["a", "b", "c"])
    text = _embed_text_for_record(r)
    assert "a" in text


def test_embed_text_for_record_truncates_value_at_2000_chars():
    r = _rec(key="k", value="x" * 3000)
    text = _embed_text_for_record(r)
    assert len(text) <= 2010


def test_embed_text_for_skill_includes_name_and_content():
    skill = Skill(
        id="s1", name="code review", content="Review all PRs carefully before merging.",
        authored_by="human", scope=["agent1"]
    )
    text = _embed_text_for_skill(skill)
    assert "code review" in text
    assert "Review all PRs" in text


def test_embed_text_for_skill_truncates_at_2000():
    skill = Skill(id="s1", name="n", content="x" * 3000, authored_by="human", scope=[])
    text = _embed_text_for_skill(skill)
    assert len(text) <= 2000
