import sys
import math
import pytest
from unittest.mock import patch
from agentdb.embeddings.provider import (
    EmbeddingProvider, FunctionEmbeddingProvider, LocalEmbeddingProvider, _cosine
)


def test_function_provider_satisfies_protocol():
    provider = FunctionEmbeddingProvider(fn=lambda t: [0.1, 0.2, 0.3])
    assert isinstance(provider, EmbeddingProvider)


def test_function_provider_embed_returns_fn_result():
    provider = FunctionEmbeddingProvider(fn=lambda t: [1.0, 2.0])
    assert provider.embed("hello") == [1.0, 2.0]


def test_function_provider_dimensions_probed_once():
    call_count = 0
    def counting_fn(text):
        nonlocal call_count
        call_count += 1
        return [0.1, 0.2]
    provider = FunctionEmbeddingProvider(fn=counting_fn)
    _ = provider.dimensions
    _ = provider.dimensions
    assert call_count == 1
    assert provider.dimensions == 2


def test_function_provider_embed_batch_calls_fn_per_text():
    calls = []
    def recording_fn(text):
        calls.append(text)
        return [0.5]
    provider = FunctionEmbeddingProvider(fn=recording_fn)
    result = provider.embed_batch(["a", "b", "c"])
    assert calls == ["a", "b", "c"]
    assert len(result) == 3


def test_function_provider_model_name_default():
    provider = FunctionEmbeddingProvider(fn=lambda t: [0.0])
    assert provider.model_name == "custom"


def test_function_provider_model_name_custom():
    provider = FunctionEmbeddingProvider(fn=lambda t: [0.0], model_name="openai-ada")
    assert provider.model_name == "openai-ada"


def test_cosine_orthogonal_unit_vectors():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert abs(_cosine(a, b)) < 1e-9


def test_cosine_identical_unit_vectors():
    a = [1.0, 0.0, 0.0]
    assert abs(_cosine(a, a) - 1.0) < 1e-9


def test_cosine_zero_vector_returns_zero():
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_unnormalized_vectors():
    # [3, 4] has norm 5; [4, 3] has norm 5; dot = 12+12 = 24; cosine = 24/25
    a = [3.0, 4.0]
    b = [4.0, 3.0]
    assert abs(_cosine(a, b) - 24.0 / 25.0) < 1e-6


def test_local_provider_raises_runtime_error_when_package_missing():
    with patch.dict("sys.modules", {"sentence_transformers": None}):
        original_models = dict(LocalEmbeddingProvider._models)
        LocalEmbeddingProvider._models.clear()
        try:
            provider = LocalEmbeddingProvider()
            with pytest.raises(RuntimeError, match="local-embed"):
                provider.embed("hello")
        finally:
            LocalEmbeddingProvider._models.update(original_models)


def test_local_provider_model_name():
    provider = LocalEmbeddingProvider()
    assert "MiniLM" in provider.model_name


def test_local_provider_dimensions():
    provider = LocalEmbeddingProvider()
    assert provider.dimensions == 384
