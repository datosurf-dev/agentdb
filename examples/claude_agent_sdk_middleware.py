"""
Minimal example: Claude-style runtime orchestration with AgentDB middleware.

This script uses a local callable in place of a real Claude SDK client so the
flow is runnable without external credentials.
"""
from __future__ import annotations

from agentdb import AgentDB
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeMiddleware


def fake_claude_call(messages: list[dict[str, str]]) -> str:
    # Replace this with your claude_agent_sdk call.
    user_prompt = [m["content"] for m in messages if m["role"] == "user"][-1]
    return f"Model response to: {user_prompt}"


def main() -> None:
    db = AgentDB(
        path="./agentdb.db",
        tracking_directories=["docs", ".claude/skills"],
        tracking_root=".",
        tracking_max_files_per_directory=200,
    )
    middleware = AgentDBClaudeMiddleware(
        db=db,
        agent_id="research_agent",
        profile="budget_optimized",  # or "full_storage"
    )

    # Example governed write for durable memory.
    db.write(
        key="memory/working/demo/preference",
        value="user prefers concise bullets",
        origin="agent_inferred",
        agent_id="research_agent",
        session_id="sess-demo-001",
    )

    response = middleware.run_turn(
        user_query="Summarize the current project status",
        session_id="sess-demo-001",
        message_id="msg-demo-001",
        call_model=fake_claude_call,
        system_prompt="You are a precise engineering assistant.",
        # tracked_directories omitted intentionally: middleware will use AgentDB defaults.
        outcome_type="task_complete",
        outcome_value=1.0,
    )

    print(response)
    print(db.system.agentdb_stats())


if __name__ == "__main__":
    main()
