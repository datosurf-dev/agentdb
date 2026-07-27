"""
AgentDB MCP server — stdio transport (Claude Desktop).

HTTP+SSE transport: same tool definitions, transport="sse",
X-AgentDB-Key auth middleware, agentdb-mcp-http entry point.
"""
from __future__ import annotations

import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from agentdb import AgentDB

mcp = FastMCP("AgentDB")
_db: Optional[AgentDB] = None


def _get_db() -> AgentDB:
    global _db
    if _db is None:
        _db = AgentDB(memory_dir=os.environ.get("AGENTDB_MEMORY_DIR"))
    return _db


@mcp.tool()
def memory_write(
    key: str,
    value: str,
    origin: str,
    agent_id: str,
    session_id: Optional[str] = None,
    confidence: Optional[float] = None,
    entities: Optional[list] = None,
) -> dict:
    """Write a governed memory record."""
    record = _get_db().write(
        key=key,
        value=value,
        origin=origin,
        agent_id=agent_id,
        session_id=session_id,
        confidence=confidence,
        entities=entities or [],
    )
    return {"id": record.id, "key": record.key, "origin": str(record.origin)}


@mcp.tool()
def memory_retrieve(
    query: str,
    agent_id: str,
    limit: int = 5,
    session_id: Optional[str] = None,
    budget_tokens: Optional[int] = None,
    isolate_by_agent: bool = False,
) -> dict:
    """Retrieve governed memory records ranked by multi-signal ranker."""
    result = _get_db().retrieve(
        query=query,
        agent_id=agent_id,
        limit=limit,
        session_id=session_id,
        budget_tokens=budget_tokens,
        isolate_by_agent=isolate_by_agent,
    )
    return {
        "records": [
            {"id": r.id, "key": r.key, "value": r.value, "origin": str(r.origin)}
            for r in result.records
        ],
        "conflicts": [str(c) for c in result.conflicts],
        "override_instructions": result.override_instructions,
        "token_estimate": result.token_estimate,
        "truncated": result.truncated,
    }


@mcp.tool()
def conflict_queue(resolved: bool = False) -> dict:
    """Return pending conflicts from the governance gateway."""
    conflicts = _get_db().gateway.conflict_queue(resolved=resolved)
    return {"conflicts": conflicts, "count": len(conflicts)}


@mcp.tool()
def memory_stats() -> dict:
    """Return database-wide statistics."""
    return _get_db().system.agentdb_stats()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
