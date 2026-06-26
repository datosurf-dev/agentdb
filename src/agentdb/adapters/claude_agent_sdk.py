"""
Claude Agent SDK runtime adapter (middleware-style).

This adapter does not import claude_agent_sdk directly. Instead, it provides a
runtime contract wrapper around AgentDB so Claude SDK users can enforce:

1) pre-response retrieval (always call AgentDB.retrieve)
2) post-response persistence policy
3) outcome recording for reinforcement

Use `run_turn(...)` for the simplest enforced flow, or call
`prepare_turn(...)` / `finalize_turn(...)` manually if your runtime has custom
callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional
import json

from ..core.directory_tracking import collect_directory_snapshots

if TYPE_CHECKING:
    from ..interface.client import AgentDB, RetrievalResult


@dataclass(frozen=True)
class ClaudeAdapterProfile:
    name: str
    retrieve_limit: int
    memory_budget_chars: int
    auto_persist_response: bool


FULL_STORAGE_PROFILE = ClaudeAdapterProfile(
    name="full_storage",
    retrieve_limit=20,
    memory_budget_chars=12000,
    auto_persist_response=True,
)

BUDGET_OPTIMIZED_PROFILE = ClaudeAdapterProfile(
    name="budget_optimized",
    retrieve_limit=5,
    memory_budget_chars=2000,
    auto_persist_response=False,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _extract_text_from_model_response(raw_response: Any) -> str:
    if isinstance(raw_response, str):
        return raw_response
    if isinstance(raw_response, dict):
        if "output_text" in raw_response:
            return str(raw_response["output_text"])
        content = raw_response.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if text is not None:
                        parts.append(str(text))
                else:
                    text = getattr(part, "text", None)
                    if text is not None:
                        parts.append(str(text))
            if parts:
                return "\n".join(parts)
    content_attr = getattr(raw_response, "content", None)
    if isinstance(content_attr, list):
        parts: list[str] = []
        for part in content_attr:
            if isinstance(part, dict):
                text = part.get("text")
                if text is not None:
                    parts.append(str(text))
            else:
                text = getattr(part, "text", None)
                if text is not None:
                    parts.append(str(text))
        if parts:
            return "\n".join(parts)
    return str(raw_response)


@dataclass(frozen=True)
class ClaudeRuntimeConfig:
    model: str
    profile: str = "budget_optimized"
    system_prompt: Optional[str] = None
    max_tokens: int = 1024


class AgentDBClaudeMiddleware:
    """
    Middleware-style helper for Claude Agent SDK integrations.

    Model calls are supplied as callbacks so this module stays runtime-agnostic.
    """

    def __init__(
        self,
        db: "AgentDB",
        agent_id: str,
        profile: str = "budget_optimized",
        retrieve_limit: Optional[int] = None,
        memory_budget_chars: Optional[int] = None,
    ):
        self._db = db
        self._agent_id = agent_id
        if profile == "full_storage":
            base = FULL_STORAGE_PROFILE
        else:
            base = BUDGET_OPTIMIZED_PROFILE
        self.profile = ClaudeAdapterProfile(
            name=base.name,
            retrieve_limit=retrieve_limit if retrieve_limit is not None else base.retrieve_limit,
            memory_budget_chars=(
                memory_budget_chars if memory_budget_chars is not None else base.memory_budget_chars
            ),
            auto_persist_response=base.auto_persist_response,
        )

    def prepare_turn(
        self,
        *,
        user_query: str,
        session_id: str,
        system_prompt: Optional[str] = None,
        external_context: Optional[list[dict[str, Any]]] = None,
        base_messages: Optional[list[dict[str, str]]] = None,
    ) -> tuple[list[dict[str, str]], "RetrievalResult"]:
        """
        Enforced pre-response hook: always retrieves governed memory first.
        Returns model messages and raw retrieval result for optional inspection.
        """
        retrieval = self._db.retrieve(
            query=user_query,
            agent_id=self._agent_id,
            limit=self.profile.retrieve_limit,
            external_context=external_context,
            session_id=session_id,
        )

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        memory_context = self._build_memory_context(retrieval)
        if memory_context:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Governed memory context (higher priority than inferred context):\n"
                        f"{memory_context}"
                    ),
                }
            )

        for instruction in retrieval.override_instructions:
            messages.append({"role": "system", "content": f"Override instruction: {instruction}"})

        for msg in base_messages or []:
            messages.append(msg)
        messages.append({"role": "user", "content": user_query})
        return messages, retrieval

    def finalize_turn(
        self,
        *,
        session_id: str,
        model_response: str,
        memory_writes: Optional[list[dict[str, Any]]] = None,
        message_id: Optional[str] = None,
        tracked_directories: Optional[list[str]] = None,
        directory_snapshots: Optional[list[dict[str, Any]]] = None,
        tracking_base_dir: Optional[str] = None,
        max_files_per_directory: Optional[int] = None,
        outcome_type: Optional[str] = None,
        outcome_value: Optional[float] = None,
    ) -> None:
        """
        Post-response hook: persist memory updates and optional outcome signal.
        """
        writes = list(memory_writes or [])
        if self.profile.auto_persist_response and model_response:
            writes.append(
                {
                    "key": self._response_key(session_id),
                    "value": {
                        "response": model_response,
                        "captured_at": _utcnow().isoformat(),
                        "profile": self.profile.name,
                    },
                    "origin": "agent_inferred",
                }
            )

        for item in writes:
            self._db.write(
                key=item["key"],
                value=item["value"],
                origin=item.get("origin", "agent_inferred"),
                agent_id=self._agent_id,
                session_id=session_id,
            )

        normalized_snapshots = list(directory_snapshots or [])
        effective_tracked_directories = (
            tracked_directories
            if tracked_directories is not None
            else getattr(self._db, "tracking_directories", [])
        )
        effective_tracking_base_dir = (
            tracking_base_dir
            if tracking_base_dir is not None
            else str(getattr(self._db, "tracking_root", "."))
        )
        effective_max_files = (
            max_files_per_directory
            if max_files_per_directory is not None
            else int(getattr(self._db, "tracking_max_files_per_directory", 200))
        )
        if effective_tracked_directories:
            normalized_snapshots.extend(
                collect_directory_snapshots(
                    effective_tracked_directories,
                    base_dir=effective_tracking_base_dir,
                    max_files_per_directory=effective_max_files,
                )
            )
        if normalized_snapshots:
            ts = _utcnow().strftime("%Y%m%dT%H%M%S%f")
            for idx, snapshot in enumerate(normalized_snapshots):
                self._db.write(
                    key=f"memory/episodic/claude/{self._agent_id}/{session_id}/directory_tracking/{ts}-{idx:04d}",
                    value={
                        "session_id": session_id,
                        "message_id": message_id,
                        "directory_snapshot": snapshot,
                        "captured_at": _utcnow().isoformat(),
                    },
                    origin="agent_inferred",
                    agent_id=self._agent_id,
                    session_id=session_id,
                )

        if outcome_type is not None and outcome_value is not None:
            self._db.record_outcome(
                session_id=session_id,
                agent_id=self._agent_id,
                outcome_type=outcome_type,
                outcome_value=outcome_value,
            )

    def run_turn(
        self,
        *,
        user_query: str,
        session_id: str,
        call_model: Callable[[list[dict[str, str]]], str],
        system_prompt: Optional[str] = None,
        external_context: Optional[list[dict[str, Any]]] = None,
        base_messages: Optional[list[dict[str, str]]] = None,
        memory_writes: Optional[list[dict[str, Any]]] = None,
        message_id: Optional[str] = None,
        tracked_directories: Optional[list[str]] = None,
        directory_snapshots: Optional[list[dict[str, Any]]] = None,
        tracking_base_dir: Optional[str] = None,
        max_files_per_directory: Optional[int] = None,
        outcome_type: Optional[str] = None,
        outcome_value: Optional[float] = None,
    ) -> str:
        """
        End-to-end enforced turn wrapper:
        retrieve -> call_model -> persist -> record_outcome.
        """
        messages, _ = self.prepare_turn(
            user_query=user_query,
            session_id=session_id,
            system_prompt=system_prompt,
            external_context=external_context,
            base_messages=base_messages,
        )
        response_text = call_model(messages)
        self.finalize_turn(
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
        return response_text

    async def prepare_turn_async(
        self,
        *,
        user_query: str,
        session_id: str,
        system_prompt: Optional[str] = None,
        external_context: Optional[list[dict[str, Any]]] = None,
        base_messages: Optional[list[dict[str, str]]] = None,
    ) -> tuple[list[dict[str, str]], "RetrievalResult"]:
        return self.prepare_turn(
            user_query=user_query,
            session_id=session_id,
            system_prompt=system_prompt,
            external_context=external_context,
            base_messages=base_messages,
        )

    async def finalize_turn_async(
        self,
        *,
        session_id: str,
        model_response: str,
        memory_writes: Optional[list[dict[str, Any]]] = None,
        message_id: Optional[str] = None,
        tracked_directories: Optional[list[str]] = None,
        directory_snapshots: Optional[list[dict[str, Any]]] = None,
        tracking_base_dir: Optional[str] = None,
        max_files_per_directory: Optional[int] = None,
        outcome_type: Optional[str] = None,
        outcome_value: Optional[float] = None,
    ) -> None:
        self.finalize_turn(
            session_id=session_id,
            model_response=model_response,
            memory_writes=memory_writes,
            message_id=message_id,
            tracked_directories=tracked_directories,
            directory_snapshots=directory_snapshots,
            tracking_base_dir=tracking_base_dir,
            max_files_per_directory=max_files_per_directory,
            outcome_type=outcome_type,
            outcome_value=outcome_value,
        )

    async def run_turn_async(
        self,
        *,
        user_query: str,
        session_id: str,
        call_model_async: Callable[[list[dict[str, str]]], Awaitable[str]],
        system_prompt: Optional[str] = None,
        external_context: Optional[list[dict[str, Any]]] = None,
        base_messages: Optional[list[dict[str, str]]] = None,
        memory_writes: Optional[list[dict[str, Any]]] = None,
        message_id: Optional[str] = None,
        tracked_directories: Optional[list[str]] = None,
        directory_snapshots: Optional[list[dict[str, Any]]] = None,
        tracking_base_dir: Optional[str] = None,
        max_files_per_directory: Optional[int] = None,
        outcome_type: Optional[str] = None,
        outcome_value: Optional[float] = None,
    ) -> str:
        messages, _ = await self.prepare_turn_async(
            user_query=user_query,
            session_id=session_id,
            system_prompt=system_prompt,
            external_context=external_context,
            base_messages=base_messages,
        )
        response_text = await call_model_async(messages)
        await self.finalize_turn_async(
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
        return response_text

    def _response_key(self, session_id: str) -> str:
        ts = _utcnow().strftime("%Y%m%dT%H%M%S%f")
        return f"memory/episodic/claude/{self._agent_id}/{session_id}/{ts}"

    def _build_memory_context(self, retrieval: "RetrievalResult") -> str:
        if not retrieval.records:
            return ""
        remaining = self.profile.memory_budget_chars
        chunks: list[str] = []
        for record in retrieval.records:
            value = record.value
            if isinstance(value, (dict, list)):
                value_text = json.dumps(value, ensure_ascii=True)
            else:
                value_text = str(value)
            line = f"- {record.key}: {value_text}"
            if len(line) > remaining:
                break
            chunks.append(line)
            remaining -= len(line)
        return "\n".join(chunks)


class AgentDBClaudeRuntime:
    """
    Higher-level runtime wrapper for easy integration.

    Developers can instantiate once and call `handle_turn(...)` from their
    Slack/event loop without manually orchestrating middleware hooks.
    """

    def __init__(
        self,
        *,
        middleware: AgentDBClaudeMiddleware,
        invoke_model: Callable[[list[dict[str, str]]], Any],
        default_system_prompt: Optional[str] = None,
        response_to_text: Callable[[Any], str] = _extract_text_from_model_response,
    ):
        self._middleware = middleware
        self._invoke_model = invoke_model
        self._default_system_prompt = default_system_prompt
        self._response_to_text = response_to_text

    @classmethod
    def from_claude_client(
        cls,
        *,
        db: "AgentDB",
        agent_id: str,
        client: Any,
        model: str,
        profile: str = "budget_optimized",
        system_prompt: Optional[str] = None,
        retrieve_limit: Optional[int] = None,
        memory_budget_chars: Optional[int] = None,
        max_tokens: int = 1024,
    ) -> "AgentDBClaudeRuntime":
        middleware = AgentDBClaudeMiddleware(
            db=db,
            agent_id=agent_id,
            profile=profile,
            retrieve_limit=retrieve_limit,
            memory_budget_chars=memory_budget_chars,
        )

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
        profile: str = "budget_optimized",
        system_prompt: Optional[str] = None,
        retrieve_limit: Optional[int] = None,
        memory_budget_chars: Optional[int] = None,
    ) -> "AgentDBClaudeRuntime":
        """
        Build a runtime that wraps a ClaudeSDKClient (agentic subprocess).

        sdk_client_factory: callable(thread_id: str) -> client
            The client must support: await client.query(prompt) and
            async-for client.receive_response() yielding messages with
            .content (list of blocks with .text) or .is_error/.result.
        """
        middleware = AgentDBClaudeMiddleware(
            db=db,
            agent_id=agent_id,
            profile=profile,
            retrieve_limit=retrieve_limit,
            memory_budget_chars=memory_budget_chars,
        )
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
            content = getattr(message, "content", None)
            if content is not None:
                for block in content:
                    text = getattr(block, "text", None)
                    if text is not None:
                        text_parts.append(text)
            is_error = getattr(message, "is_error", False)
            if is_error:
                error_msg = getattr(message, "result", "Claude SDK error")
                raise RuntimeError(error_msg or "Claude SDK returned an error")

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
