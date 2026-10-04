"""
Embedding test double — controlled vectors for unit tests.
Keys in VECTORS are matched by substring against the input text.
No sentence-transformers dependency.
"""

VECTORS: dict[str, list[float]] = {
    "topic_A": [1.0] + [0.0] * 383,
    "topic_B": [0.0, 1.0] + [0.0] * 382,
    "similar_to_A": [0.9999] + [0.0] * 383,   # cosine ≈ 0.9999 with topic_A
    "unrelated":    [0.0] * 191 + [1.0] + [0.0] * 192,  # cosine ≈ 0.0 with topic_A
    "semantic_dup": [0.9998] + [0.0] * 383,   # cosine ≈ 0.9998 with topic_A — above 0.92
    "different":    [0.0, 0.0, 1.0] + [0.0] * 381,  # cosine ≈ 0.0 with topic_A
}

_DEFAULT = [0.0] * 384


def controlled_embed(text: str) -> list[float]:
    """Look up a pre-set vector by key substring, or return the default zero vector."""
    for key, vec in VECTORS.items():
        if key in text:
            return list(vec)
    return list(_DEFAULT)
