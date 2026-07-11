"""
Reproducible Slack-style integration example for AgentDB + ClaudeSDKClient async runtime.

This shows the minimal app-layer wiring for the agentic subprocess path:
    - one AgentDB instance
    - one app-owned get_or_create_client(thread_id)
    - one AgentDBClaudeRuntime built with from_sdk_client()
    - one async handler function per incoming thread message

The example uses a fake SDK client so it runs locally without credentials.
Replace FakeSdkClient with your real ClaudeSDKClient in production.
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
        self._hooks: dict[str, Any] = {}

    def register_hook(self, name: str, handler: Any) -> None:
        self._hooks[name] = handler

    def invoke_pre_tool_use(self, *, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        handler = self._hooks.get("PreToolUse")
        if handler is None:
            return {}
        return handler(
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=self._thread_id,
        )

    async def query(self, prompt: str) -> None:
        self._prompt = prompt

    async def receive_response(self):
        yield FakeAssistantMessage(f"Agentic reply to: {self._prompt[:80]}")
        yield FakeResultMessage(session_id=self._thread_id)


class SlackAgentHandler:
    def __init__(self, db_path: str = "./agentdb.db") -> None:
        self.db = AgentDB(
            path=db_path,
            tracking_directories=["docs", ".claude/skills", "agent-sessions"],
            tracking_root=".",
            tracking_max_files_per_directory=200,
        )
        self._clients: dict[str, FakeSdkClient] = {}

        # Seed durable memory for demo relevance.
        self.db.write(
            key="memory/working/slack/preferences",
            value="respond in concise bullets for routine updates",
            origin="agent_inferred",
            agent_id="research_agent",
            session_id="demo-thread-1",
        )
        self.db.write(
            key="memory/working/slack/report_model_guidance",
            value="Preserve the customer_id join when editing report_model.",
            origin="human_approved",
            agent_id="research_agent",
            entities=["models/marts/core/report_model.sql", "report_model"],
            session_id="demo-thread-1",
        )

        self.runtime = AgentDBClaudeRuntime.from_sdk_client(
            db=self.db,
            agent_id="research_agent",
            sdk_client_factory=self.get_or_create_client,
            profile="budget_optimized",
            system_prompt="You are the engineering Slack assistant.",
        )

    def get_or_create_client(self, thread_id: str) -> FakeSdkClient:
        """
        In production, return the ClaudeSDKClient for this thread_id.
        The Slack assistant's claude_runner already manages one client per thread;
        attach AgentDB's hooks at client creation time.
        """
        client = self._clients.get(thread_id)
        if client is None:
            client = FakeSdkClient(thread_id)
            for hook_name, handler in self.runtime.get_sdk_hooks().items():
                client.register_hook(hook_name, handler)
            self._clients[thread_id] = client
        return client

    def pre_tool_use(self, tool_name: str, tool_input: dict[str, Any], thread_id: str) -> dict[str, Any]:
        """
        This mirrors the downstream SDK callback that fires immediately before a tool runs.
        """
        client = self.get_or_create_client(thread_id)
        return client.invoke_pre_tool_use(tool_name=tool_name, tool_input=tool_input)

    async def handle_event(self, event: IncomingSlackEvent) -> str:
        result = await self.runtime.handle_turn_async(
            user_query=event.text,
            session_id=event.thread_id,
            message_id=event.message_id,
            outcome_type="task_complete",
            outcome_value=1.0,
        )
        return result["response_text"]


async def main() -> None:
    handler = SlackAgentHandler()

    # Example of downstream pre-tool hook usage.
    pre_tool_payload = handler.pre_tool_use(
        "Edit",
        {"file_path": "models/marts/core/report_model.sql"},
        "demo-thread-1",
    )
    print(pre_tool_payload)

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
