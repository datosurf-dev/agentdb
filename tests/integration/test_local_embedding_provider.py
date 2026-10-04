import math
import pytest
from agentdb.embeddings.provider import LocalEmbeddingProvider


@pytest.fixture(autouse=True)
def reset_singleton():
    """Ensure each test starts with a clean singleton state."""
    original = LocalEmbeddingProvider._model
    yield
    LocalEmbeddingProvider._model = original


@pytest.mark.slow
def test_local_provider_returns_384_dimensional_vector():
    provider = LocalEmbeddingProvider()
    vec = provider.embed("hello world")
    assert isinstance(vec, list)
    assert len(vec) == 384
    assert all(isinstance(x, float) for x in vec)


@pytest.mark.slow
def test_local_provider_output_is_unit_normalized():
    provider = LocalEmbeddingProvider()
    vec = provider.embed("the quick brown fox")
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-5


@pytest.mark.slow
def test_local_provider_embed_and_embed_batch_agree():
    provider = LocalEmbeddingProvider()
    text = "governance memory layer for AI agents"
    single = provider.embed(text)
    batch = provider.embed_batch([text])
    assert len(batch) == 1
    for a, b in zip(single, batch[0]):
        assert abs(a - b) < 1e-5


@pytest.mark.slow
def test_local_provider_singleton_shared_across_instances():
    p1 = LocalEmbeddingProvider()
    _ = p1.embed("warmup")  # loads model
    p2 = LocalEmbeddingProvider()
    assert p1._model is p2._model
