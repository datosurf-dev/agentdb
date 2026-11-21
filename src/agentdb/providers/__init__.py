"""
Provider ingestors — pull conversation history from AI providers into AgentDB.

Each provider wraps a vendor SDK and writes memories with origin=provider_ingested.
human_approved records are never overwritten.

Available providers:
  - AnthropicProvider: sync recent messages from the Anthropic Messages API

Consolidation helper:
  - build_llm_fn: build an ``llm_fn`` (from a model id + connection params) for
    ``AgentDB.consolidate(..., llm_fn=...)`` synthesis.
"""
from .anthropic import AnthropicProvider
from .consolidation import build_llm_fn

__all__ = ["AnthropicProvider", "build_llm_fn"]
