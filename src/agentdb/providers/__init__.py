"""
Provider ingestors — pull conversation history from AI providers into AgentDB.

Each provider wraps a vendor SDK and writes memories with origin=provider_ingested.
human_approved records are never overwritten.

Available providers:
  - AnthropicProvider: sync recent messages from the Anthropic Messages API
"""
from .anthropic import AnthropicProvider

__all__ = ["AnthropicProvider"]
