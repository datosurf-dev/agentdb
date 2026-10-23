"""
Claude Agent SDK runtime adapter (backward-compat shim).

AgentDBClaudeMiddleware has been renamed to AgentDBMiddleware and moved to
`agentdb.adapters.middleware`. This module re-exports it under the old name
for backward compatibility and retains AgentDBClaudeRuntime.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from .middleware import AgentDBMiddleware as AgentDBClaudeMiddleware  # noqa: F401
from .middleware import AgentDBMiddleware  # noqa: F401
from .middleware import _extract_text_from_model_response
from .capabilities import CLAUDE_CAPABILITIES

if TYPE_CHECKING:
    from ..interface.client import AgentDB, RetrievalResult


class AgentDBClaudeRuntime:
    """
    Higher-level runtime wrapper for easy integration.

    Developers can instantiate once and call `handle_turn(...)` from their
    Slack/event loop without manually orchestrating middleware hooks.
    """

    def __init__(
        self,
        *,
        middleware: AgentDBMiddleware,
        invoke_model: Callable[[list[dict[str, str]]], Any],
        default_system_prompt: Optional[str] = None,
        response_to_text: Callable[[Any], str] = _extract_text_from_model_response,
    ):
        self._middleware = middleware
        self._invoke_model = invoke_model
        self._default_system_prompt = default_system_prompt
        self._response_to_text = response_to_text
        self._sdk_client_factory = None

    @classmethod
    def from_claude_client(
        cls,
        *,
        db: "AgentDB",
        agent_id: str,
        client: Any,
        model: str,
        system_prompt: Optional[str] = None,
        max_tokens: int = 1024,
    ) -> "AgentDBClaudeRuntime":
        middleware = AgentDBMiddleware(db=db, agent_id=agent_id, capabilities=CLAUDE_CAPABILITIES)

        def _invoke(messages: list[dict[str, str]]) -> Any:
            system_blocks = [m["content"] for m in messages if m.get("role") == "system"]
            chat_messages = [m for m in messages if m.get("role") != "system"]
            if hasattr(client, "messages") and hasattr(client.messages, "create"):
                payload: dict[str, Any] = {
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": chat_messages,
                }
                if system_blocks:
                    payload["system"] = "\n\n".join(system_blocks)
                return client.messages.create(**payload)
            if hasattr(client, "run"):
                return client.run(model=model, messages=messages, max_tokens=max_tokens)
            raise ValueError(
                "Unsupported client: expected `messages.create(...)` or `run(...)` API."
            )

        return cls(
            middleware=middleware,
            invoke_model=_invoke,
            default_system_prompt=system_prompt,
            response_to_text=_extract_text_from_model_response,
        )

    @classmethod
    def from_sdk_client(
        cls,
        *,
        db: "AgentDB",
        agent_id: str,
        sdk_client_factory: Callable[[str], Any],
        system_prompt: Optional[str] = None,
    ) -> "AgentDBClaudeRuntime":
        """
        Build a runtime that wraps a ClaudeSDKClient (agentic subprocess).

        sdk_client_factory: callable(thread_id: str) -> client
            The client must support: await client.query(prompt) and
            async-for client.receive_response() yielding messages with
            .content (list of blocks with .text) or .is_error/.result.
        """
        middleware = AgentDBMiddleware(db=db, agent_id=agent_id, capabilities=CLAUDE_CAPABILITIES)
        instance = cls(
            middleware=middleware,
            invoke_model=lambda messages: "",
            default_system_prompt=system_prompt,
        )
        instance._sdk_client_factory = sdk_client_factory
        return instance

    async def handle_turn_async(
        self,
        *,
        user_query: str,
        session_id: str,
        message_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        external_context: Optional[list[dict[str, Any]]] = None,
        base_messages: Optional[list[dict[str, str]]] = None,
        memory_writes: Optional[list[dict[str, Any]]] = None,
        tracked_directories: Optional[list[str]] = None,
        directory_snapshots: Optional[list[dict[str, Any]]] = None,
        tracking_base_dir: Optional[str] = None,
        max_files_per_directory: Optional[int] = None,
        outcome_type: Optional[str] = None,
        outcome_value: Optional[float] = None,
    ) -> dict[str, Any]:
        sdk_factory = getattr(self, "_sdk_client_factory", None)
        if sdk_factory is None:
            raise RuntimeError(
                "handle_turn_async requires a runtime built with from_sdk_client(). "
                "Use handle_turn() for the messages.create() path."
            )

        messages, retrieval = await self._middleware.prepare_turn_async(
            user_query=user_query,
            session_id=session_id,
            system_prompt=system_prompt if system_prompt is not None else self._default_system_prompt,
            external_context=external_context,
            base_messages=base_messages,
        )

        system_blocks = [m["content"] for m in messages if m.get("role") == "system"]
        user_messages = [m for m in messages if m.get("role") == "user"]
        prompt_parts = system_blocks + [m["content"] for m in user_messages]
        prompt = "\n\n".join(prompt_parts)

        client = sdk_factory(session_id)
        await client.query(prompt)

        text_parts: list[str] = []
        async for message in client.receive_response():
            is_error = getattr(message, "is_error", False)
            if is_error:
                error_msg = getattr(message, "result", "Claude SDK error")
                raise RuntimeError(error_msg or "Claude SDK returned an error")
            content = getattr(message, "content", None)
            if content is not None:
                for block in content:
                    text = getattr(block, "text", None)
                    if text is not None:
                        text_parts.append(text)

        response_text = "\n".join(text_parts).strip()

        await self._middleware.finalize_turn_async(
            session_id=session_id,
            model_response=response_text,
            memory_writes=memory_writes,
            message_id=message_id,
            tracked_directories=tracked_directories,
            directory_snapshots=directory_snapshots,
            tracking_base_dir=tracking_base_dir,
            max_files_per_directory=max_files_per_directory,
            outcome_type=outcome_type,
            outcome_value=outcome_value,
        )

        return {
            "response_text": response_text,
            "raw_response": None,
            "retrieval": retrieval,
            "messages": messages,
        }

    def handle_turn(
        self,
        *,
        user_query: str,
        session_id: str,
        message_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        external_context: Optional[list[dict[str, Any]]] = None,
        base_messages: Optional[list[dict[str, str]]] = None,
        memory_writes: Optional[list[dict[str, Any]]] = None,
        tracked_directories: Optional[list[str]] = None,
        directory_snapshots: Optional[list[dict[str, Any]]] = None,
        tracking_base_dir: Optional[str] = None,
        max_files_per_directory: Optional[int] = None,
        outcome_type: Optional[str] = None,
        outcome_value: Optional[float] = None,
    ) -> dict[str, Any]:
        messages, retrieval = self._middleware.prepare_turn(
            user_query=user_query,
            session_id=session_id,
            system_prompt=system_prompt if system_prompt is not None else self._default_system_prompt,
            external_context=external_context,
            base_messages=base_messages,
        )
        raw_response = self._invoke_model(messages)
        response_text = self._response_to_text(raw_response)
        self._middleware.finalize_turn(
            session_id=session_id,
            model_response=response_text,
            memory_writes=memory_writes,
            message_id=message_id,
            tracked_directories=tracked_directories,
            directory_snapshots=directory_snapshots,
            tracking_base_dir=tracking_base_dir,
            max_files_per_directory=max_files_per_directory,
            outcome_type=outcome_type,
            outcome_value=outcome_value,
        )
        return {
            "response_text": response_text,
            "raw_response": raw_response,
            "retrieval": retrieval,
            "messages": messages,
        }

    def get_sdk_hooks(self) -> dict[str, Any]:
        """
        Return hook handlers from the middleware for Claude SDK integration.

        Returns:
            {"PreToolUse": callable}
        """
        return self._middleware.build_hook_handlers()
