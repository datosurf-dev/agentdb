"""
AnthropicProvider — ingest recent Anthropic conversation messages into AgentDB.

Usage:
    import anthropic
    from agentdb.providers.anthropic import AnthropicProvider

    client = anthropic.Anthropic(api_key="...")
    provider = AnthropicProvider(db=db, client=client)
    ingested = provider.sync(agent_id="my-agent", limit=50)

Requires the `anthropic` package (optional extra: pip install agentdb[providers]).
Records are written with origin=provider_ingested so they are subject to
lifecycle management. human_approved records with the same key are never
overwritten.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..interface.client import AgentDB


class AnthropicProvider:
    """Ingest recent Anthropic assistant messages into AgentDB as memories."""

    def __init__(self, db: "AgentDB", client: Any) -> None:
        self._db = db
        self._client = client

    @property
    def db(self) -> "AgentDB":
        return self._db

    def sync(self, agent_id: str, limit: int = 50) -> int:
        """
        Fetch up to `limit` recent messages and write text content as memories.

        - Only text content blocks are ingested; tool_use, image, etc. are skipped.
        - Skips any key where a human_approved record already exists.
        - Returns the count of records actually written.

        Args:
            agent_id: the agent identity to associate with ingested records.
            limit: maximum number of messages to fetch from the API.

        Returns:
            Number of new memories written.
        """
        page = self._client.messages.list(limit=limit)
        written = 0
        for msg in page.data:
            for block in (msg.content or []):
                if getattr(block, "type", None) != "text":
                    continue
                text = getattr(block, "text", "").strip()
                if not text:
                    continue
                key = f"provider/{msg.id}"
                existing = self._db.read(key)
                if existing is not None and existing.origin == "human_approved":
                    continue
                self._db.write(
                    key=key,
                    value=text,
                    origin="provider_ingested",
                    agent_id=agent_id,
                    confidence=0.6,
                )
                written += 1
        return written
