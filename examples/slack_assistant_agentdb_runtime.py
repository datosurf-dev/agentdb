"""
Reproducible Slack-style integration example for AgentDB + ClaudeSDKClient async runtime.

This shows the minimal app-layer wiring for the agentic subprocess path:
  - one AgentDB instance
  - one AgentDBClaudeRuntime built with from_sdk_client()
  - one async handler function per incoming thread message

The example uses a fake SDK client so it runs locally without credentials.
Replace FakeSdkClient / fake_sdk_factory with your real ClaudeSDKClient in production.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from agentdb import AgentDB
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeRuntime


@dataclass(frozen=True)
class IncomingSlackEvent:
    channel: str
    thread_id: str
    message_id: str
    user_id: str
    text: str


# ---------------------------------------------------------------------------
# Fake SDK client — replace with real ClaudeSDKClient in production.
# Mimics the interface: await client.query(prompt) + async-for receive_response()
# ---------------------------------------------------------------------------
class FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeAssistantMessage:
    def __init__(self, text: str) -> None:
        self.content = [FakeTextBlock(text)]


class FakeResultMessage:
    is_error = False
    result = None

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class FakeSdkClient:
    def __init__(self, thread_id: str) -> None:
        self._thread_id = thread_id
        self._prompt: str = ""

    async def query(self, prompt: str) -> None:
        self._prompt = prompt

    async def receive_response(self):
        yield FakeAssistantMessage(f"Agentic reply to: {self._prompt[:80]}")
        yield FakeResultMessage(session_id=self._thread_id)


def fake_sdk_factory(thread_id: str) -> FakeSdkClient:
    """
    In production, return the ClaudeSDKClient for this thread_id.
    The slack-assistant's claude_runner already manages one client per thread —
    pass claude_runner.get_or_create_client(thread_id, ...) here.
    """
    return FakeSdkClient(thread_id)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
class SlackAgentHandler:
    def __init__(self, runtime: AgentDBClaudeRuntime) -> None:
        self.runtime = runtime

    async def handle_event(self, event: IncomingSlackEvent) -> str:
        result = await self.runtime.handle_turn_async(
            user_query=event.text,
            session_id=event.thread_id,
            message_id=event.message_id,
            outcome_type="task_complete",
            outcome_value=1.0,
        )
        return result["response_text"]


def build_runtime(db_path: str = "./agentdb.db") -> AgentDBClaudeRuntime:
    db = AgentDB(
        path=db_path,
        tracking_directories=["docs", ".claude/skills", "agent-sessions"],
        tracking_root=".",
        tracking_max_files_per_directory=200,
    )

    # Seed a durable memory record for demo relevance.
    db.write(
        key="memory/working/slack/preferences",
        value="respond in concise bullets for routine updates",
        origin="agent_inferred",
        agent_id="research_agent",
        session_id="demo-thread-1",
    )

    return AgentDBClaudeRuntime.from_sdk_client(
        db=db,
        agent_id="research_agent",
        sdk_client_factory=fake_sdk_factory,
        profile="budget_optimized",
        system_prompt="You are the engineering Slack assistant.",
    )


async def main() -> None:
    runtime = build_runtime()
    handler = SlackAgentHandler(runtime)

    event = IncomingSlackEvent(
        channel="C123",
        thread_id="demo-thread-1",
        message_id="msg-001",
        user_id="U123",
        text="Summarize what changed in this thread and next actions.",
    )
    reply = await handler.handle_event(event)
    print(reply)


if __name__ == "__main__":
    asyncio.run(main())
