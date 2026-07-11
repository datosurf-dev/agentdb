"""
Downstream Claude SDK hook-registration example for AgentDB action-time context.

This shows the app-layer step that AgentDB intentionally leaves to the runtime
owner: attach the `PreToolUse` handler returned by `runtime.get_sdk_hooks()` to
the SDK client/session you create per thread.

The fake SDK classes make the example runnable without external credentials.
Replace `FakeHookableSdkClient` with your real Claude SDK client or session in
production.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from agentdb import AgentDB
from agentdb.adapters.claude_agent_sdk import AgentDBClaudeRuntime


@dataclass(frozen=True)
class FakeTextBlock:
    text: str


@dataclass(frozen=True)
class FakeAssistantMessage:
    content: list[FakeTextBlock]


@dataclass(frozen=True)
class FakeResultMessage:
    session_id: str
    is_error: bool = False
    result: None = None


class FakeHookableSdkClient:
    """
    Minimal hook-capable SDK session stand-in.

    Production mapping:
    - create or look up your Claude SDK client/session per thread
    - register AgentDB's PreToolUse handler on that client/session
    - let the SDK invoke the handler before the tool actually runs
    """

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self._prompt: str = ""
        self._hooks: dict[str, Callable[..., dict[str, Any]]] = {}

    def register_hook(self, name: str, handler: Callable[..., dict[str, Any]]) -> None:
        self._hooks[name] = handler

    def invoke_pre_tool_use(self, *, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        handler = self._hooks.get("PreToolUse")
        if handler is None:
            return {}
        return handler(
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=self.thread_id,
        )

    async def query(self, prompt: str) -> None:
        self._prompt = prompt

    async def receive_response(self):
        yield FakeAssistantMessage([FakeTextBlock(f"Agentic reply to: {self._prompt[:80]}")])
        yield FakeResultMessage(session_id=self.thread_id)


class ClaudeSdkApp:
    def __init__(self, db_path: str = "./agentdb.db") -> None:
        self.db = AgentDB(path=db_path)
        self.agent_id = "research_agent"
        self._clients: dict[str, FakeHookableSdkClient] = {}

        # Seed entity-linked memory so the hook can retrieve narrow action context.
        self.db.write(
            key="memory/working/dbt/report_model_guidance",
            value="Preserve the customer_id join and keep revenue normalized.",
            origin="human_approved",
            agent_id=self.agent_id,
            entities=["models/marts/core/report_model.sql", "report_model"],
            session_id="demo-thread-1",
        )

        self.runtime = AgentDBClaudeRuntime.from_sdk_client(
            db=self.db,
            agent_id=self.agent_id,
            sdk_client_factory=self.get_or_create_client,
            profile="budget_optimized",
            system_prompt="You are the engineering Slack assistant.",
        )

    def get_or_create_client(self, thread_id: str) -> FakeHookableSdkClient:
        client = self._clients.get(thread_id)
        if client is None:
            client = FakeHookableSdkClient(thread_id)
            self.attach_agentdb_hooks(client)
            self._clients[thread_id] = client
        return client

    def attach_agentdb_hooks(self, client: FakeHookableSdkClient) -> None:
        for hook_name, handler in self.runtime.get_sdk_hooks().items():
            client.register_hook(hook_name, handler)

    async def run_demo(self, thread_id: str) -> None:
        client = self.get_or_create_client(thread_id)

        # This is the downstream SDK-side callback point immediately before a tool runs.
        hook_payload = client.invoke_pre_tool_use(
            tool_name="Edit",
            tool_input={"file_path": "models/marts/core/report_model.sql"},
        )
        print("PreToolUse payload:")
        print(hook_payload)

        result = await self.runtime.handle_turn_async(
            user_query="Update report_model and summarize the change.",
            session_id=thread_id,
            message_id="msg-demo-001",
            outcome_type="task_complete",
            outcome_value=1.0,
        )
        print("Runtime reply:")
        print(result["response_text"])


async def main() -> None:
    app = ClaudeSdkApp()
    await app.run_demo("demo-thread-1")


if __name__ == "__main__":
    asyncio.run(main())
