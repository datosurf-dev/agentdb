"""Tests for YAML-configurable embeddings (embeddings: block in memory.yaml)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agentdb import AgentDB
from agentdb.embeddings.provider import FunctionEmbeddingProvider


def _write_config(tmp_path: Path, overrides: dict) -> None:
    cfg = {
        "profile": {"name": "budget_optimized"},
        "reconciliation": {"triggers": ["conversation_started"]},
        "database": {"mode": "embedded", "path": "database/agentdb.sqlite"},
    }
    cfg.update(overrides)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "memory.yaml").write_text(yaml.dump(cfg), encoding="utf-8")


class _FakeProvider:
    """Minimal EmbeddingProvider that avoids loading sentence-transformers."""
    model_name = "sentence-transformers/all-MiniLM-L6-v2"
    dimensions = 384

    def embed(self, text: str) -> list[float]:
        return [0.0] * 384

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


def test_no_embeddings_block_no_provider(tmp_path):
    db = AgentDB(memory_dir=str(tmp_path))
    assert db._embedding_provider is None


def test_explicit_disabled_no_provider(tmp_path):
    _write_config(tmp_path, {"embeddings": {"enabled": False}})
    db = AgentDB(memory_dir=str(tmp_path))
    assert db._embedding_provider is None


def test_yaml_enabled_no_model_instantiates_local_provider(tmp_path):
    _write_config(tmp_path, {"embeddings": {"enabled": True}})
    fake = _FakeProvider()
    with patch("agentdb.embeddings.LocalEmbeddingProvider", return_value=fake) as mock_cls:
        db = AgentDB(memory_dir=str(tmp_path))
    mock_cls.assert_called_once_with(model_name=None)
    assert db._embedding_provider is fake


def test_yaml_enabled_with_model_passes_model_name(tmp_path):
    _write_config(tmp_path, {
        "embeddings": {"enabled": True, "model": "sentence-transformers/all-mpnet-base-v2"}
    })
    fake = _FakeProvider()
    with patch("agentdb.embeddings.LocalEmbeddingProvider", return_value=fake) as mock_cls:
        db = AgentDB(memory_dir=str(tmp_path))
    mock_cls.assert_called_once_with(model_name="sentence-transformers/all-mpnet-base-v2")
    assert db._embedding_provider is fake


def test_embed_fn_kwarg_overrides_yaml_enabled(tmp_path):
    _write_config(tmp_path, {"embeddings": {"enabled": True}})

    def fn(text: str) -> list[float]:
        return [0.1] * 384

    with patch("agentdb.embeddings.LocalEmbeddingProvider") as mock_cls:
        db = AgentDB(memory_dir=str(tmp_path), embed_fn=fn)
    mock_cls.assert_not_called()
    assert isinstance(db._embedding_provider, FunctionEmbeddingProvider)


def test_local_embed_flag_overrides_yaml_model(tmp_path):
    """local_embed=True uses default model ignoring YAML model specification."""
    _write_config(tmp_path, {
        "embeddings": {"enabled": True, "model": "some/other-model"}
    })
    fake = _FakeProvider()
    with patch("agentdb.embeddings.LocalEmbeddingProvider", return_value=fake) as mock_cls:
        db = AgentDB(memory_dir=str(tmp_path), local_embed=True)
    # local_embed branch calls LocalEmbeddingProvider() with no arguments
    mock_cls.assert_called_once_with()
    assert db._embedding_provider is fake


def test_yaml_enabled_activates_semantic_ranker_weight(tmp_path):
    _write_config(tmp_path, {"embeddings": {"enabled": True}})
    fake = _FakeProvider()
    with patch("agentdb.embeddings.LocalEmbeddingProvider", return_value=fake):
        db = AgentDB(memory_dir=str(tmp_path))
    assert db._ranker.w_semantic > 0.0
