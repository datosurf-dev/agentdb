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
from ..gateway.adapter import GatewayAdapter

if TYPE_CHECKING:
    from ..interface.client import AgentDB, RetrievalResult


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
    system_prompt: Optional[str] = None
    max_tokens: int = 1024


class AgentDBClaudeMiddleware(GatewayAdapter):
    """
    Middleware-style helper for Claude Agent SDK integrations.

    Model calls are supplied as callbacks so this module stays runtime-agnostic.
    Inherits GatewayAdapter for access to db.gateway (reconciliation, proposals).
    """

    def __init__(
        self,
        db: "AgentDB",
        agent_id: str,
        conversation_started_callback: Optional[Callable[..., Any]] = None,
    ):
        resolved = db.config_for(agent_id)
        super().__init__(db, agent_id, profile=resolved["name"])
        self._conversation_started_callback = conversation_started_callback
        self._resolved = resolved
        self.gateway.subscribe("conflict_detected", self._on_conflict)

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
        if self._conversation_started_callback is not None:
            self._conversation_started_callback(
                db=self._db,
                agent_id=self._agent_id,
                session_id=session_id,
                user_query=user_query,
            )
        self.gateway.dispatch("conversation_started", session_id=session_id)

        retrieval = self._db.retrieve(
            query=user_query,
            agent_id=self._agent_id,
            limit=self._resolved["retrieval"]["retrieve_limit"],
            external_context=external_context,
            session_id=session_id,
            budget_tokens=self._resolved["injection"]["context_token_limit"],
        )

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # Principle 9: conflict flags must be assembled BEFORE human-approved
        # memories, not after — an agent must know a memory is contested
        # before it receives the memory content itself.
        override_prefix = self._resolved["injection"]["override_prefix"]
        for instruction in retrieval.override_instructions:
            messages.append({"role": "system", "content": f"{override_prefix} {instruction}"})

        memory_context = self._build_memory_context(retrieval)
        if memory_context:
            messages.append(
                {
                    "role": "system",
                    "content": f"{self._resolved['injection']['memory_header']}\n{memory_context}",
                }
            )

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
        touched_entities: Optional[list[str]] = None,
        user_query: Optional[str] = None,
    ) -> None:
        """
        Post-response hook: persist memory updates and optional outcome signal.
        """
        writes = list(memory_writes or [])
        if self._resolved["capture"]["auto_persist_response"] and model_response:
            writes.append(
                {
                    "key": self._response_key(session_id),
                    "value": {
                        "response": model_response,
                        "captured_at": _utcnow().isoformat(),
                        "profile": self._resolved["name"],
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
                entities=touched_entities or [],
            )

        # Write conversation JSONL if enabled
        if user_query is not None:
            capture_cfg = self._resolved["capture"]
            if capture_cfg.get("conversation_jsonl", False):
                memory_dir = getattr(self._db, "memory_dir", None)
                if memory_dir is not None:
                    import json as _json
                    from pathlib import Path as _Path
                    conv_dir = _Path(memory_dir) / "conversations"
                    conv_dir.mkdir(parents=True, exist_ok=True)
                    filename = f"{self._agent_id}--{session_id}.jsonl"
                    filepath = conv_dir / filename
                    entry = _json.dumps({
                        "session_id": session_id,
                        "message_id": message_id,
                        "user_query": user_query,
                        "model_response": model_response,
                    })
                    with filepath.open("a", encoding="utf-8") as f:
                        f.write(entry + "\n")

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
        touched_entities: Optional[list[str]] = None,
        user_query: Optional[str] = None,
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
            touched_entities=touched_entities,
            user_query=user_query,
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

    def _on_conflict(self, **payload) -> None:
        """
        Platform-agnostic conflict handler.
        Concrete notification wiring is platform-specific (Slack, Telegram, etc.).
        """
        pass

    def pending_conflicts(self, resolved: bool = False) -> list:
        """Read-only view into db.gateway's conflict queue."""
        return self.gateway.conflict_queue(resolved=resolved)

    def propose_skill(
        self,
        name: str,
        content: str,
        scope: list[str],
        entities: Optional[list[str]] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """Draft a skill proposal via db.gateway.propose(), scoped to this adapter's agent_id."""
        return self.gateway.propose(
            name=name,
            content=content,
            scope=scope,
            agent_id=self._agent_id,
            entities=entities,
            session_id=session_id,
        )

    def approve_proposal(self, proposal_id: str, resolution: Optional[str] = None) -> None:
        """Approve a pending proposal via db.gateway.approve()."""
        self.gateway.approve(proposal_id, resolution=resolution)

    def reject_proposal(self, proposal_id: str, reason: Optional[str] = None) -> None:
        """Reject a pending proposal via db.gateway.reject()."""
        self.gateway.reject(proposal_id, reason=reason)

    def list_proposals(self, status: str = "pending") -> list[dict]:
        """List proposals by status via db.gateway.list_proposals()."""
        return self.gateway.list_proposals(status=status)

    def _response_key(self, session_id: str) -> str:
        ts = _utcnow().strftime("%Y%m%dT%H%M%S%f")
        return f"memory/episodic/claude/{self._agent_id}/{session_id}/{ts}"

    # Keys matching these substrings are stored for reconciliation but
    # excluded from context injection — they're operational metadata,
    # not useful memories for the model.
    _INJECTION_EXCLUDED_KEY_PATTERNS = ("/directory_tracking/",)

    # Episodic response records can be very large; cap them so one turn's
    # response doesn't exhaust the whole memory budget.
    _MAX_EPISODIC_VALUE_CHARS = 2000

    def _build_memory_context(self, retrieval: "RetrievalResult") -> str:
        if not retrieval.records:
            return ""
        remaining = self._resolved["injection"]["memory_budget_chars"]
        line_format = self._resolved["injection"]["record_line_format"]
        chunks: list[str] = []
        for record in retrieval.records:
            if any(pat in record.key for pat in self._INJECTION_EXCLUDED_KEY_PATTERNS):
                continue
            value = record.value
            if isinstance(value, (dict, list)):
                value_text = json.dumps(value, ensure_ascii=True)
            else:
                value_text = str(value)
            if len(value_text) > self._MAX_EPISODIC_VALUE_CHARS:
                value_text = value_text[: self._MAX_EPISODIC_VALUE_CHARS] + "…"
            line = line_format.format(key=record.key, value=value_text)
            if len(line) > remaining:
                break
            chunks.append(line)
            remaining -= len(line)
        return "\n".join(chunks)

    def _extract_entities_from_tool(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> list[str]:
        """
        Extract entity strings from a tool call.

        Edit -> file_path
        Bash -> parse --select X flag or model names from command
        """
        entities: list[str] = []
        if tool_name == "Edit":
            file_path = tool_input.get("file_path")
            if file_path:
                entities.append(str(file_path))
        elif tool_name == "Bash":
            command = tool_input.get("command", "")
            # Parse --select <value> (dbt-style)
            import re
            select_match = re.search(r"--select\s+(\S+)", command)
            if select_match:
                entities.append(select_match.group(1))
            # Also look for model names in the command (words that look like identifiers)
            # Extract words that are likely model/file references (not flags)
            words = command.split()
            for word in words:
                if word.startswith("-"):
                    continue
                # Skip common shell commands and dbt subcommands
                if word in ("dbt", "build", "run", "test", "compile", "echo", "ls",
                            "cat", "grep", "python", "pip", "cd", "export"):
                    continue
                # Accept words that look like identifiers (letters, digits, underscores, dots, slashes)
                if re.match(r"^[a-zA-Z][a-zA-Z0-9_./]*$", word):
                    entities.append(word)
        return entities

    def build_action_context(
        self, tool_name: str, tool_input: dict[str, Any], session_id: str
    ) -> str:
        """
        Build governed action context for a tool call.

        Returns a formatted context string if matching memories are found,
        otherwise returns empty string.
        """
        entities = self._extract_entities_from_tool(tool_name, tool_input)
        if not entities:
            return ""

        retrieval = self._db.retrieve_by_entities(
            entities=entities,
            agent_id=self._agent_id,
            session_id=session_id,
        )

        if not retrieval.records:
            return ""

        lines = ["Governed action context:"]
        for record in retrieval.records:
            value = record.value
            if isinstance(value, (dict, list)):
                value_text = json.dumps(value, ensure_ascii=True)
            else:
                value_text = str(value)
            lines.append(f"- {record.key}: {value_text}")

        return "\n".join(lines)

    def pre_tool_use(
        self, tool_name: str, tool_input: dict[str, Any], session_id: str
    ) -> dict[str, Any]:
        """
        Pre-tool-use hook: extract context and touched entities.

        Returns:
            {"additionalContext": str, "touchedEntities": list[str]}
            or {} if no matching context.
        """
        entities = self._extract_entities_from_tool(tool_name, tool_input)
        if not entities:
            return {}

        retrieval = self._db.retrieve_by_entities(
            entities=entities,
            agent_id=self._agent_id,
            session_id=session_id,
        )

        if not retrieval.records:
            return {}

        lines = ["Governed action context:"]
        touched: list[str] = []
        for record in retrieval.records:
            value = record.value
            if isinstance(value, (dict, list)):
                value_text = json.dumps(value, ensure_ascii=True)
            else:
                value_text = str(value)
            lines.append(f"- {record.key}: {value_text}")
            touched.extend(record.entities or [])

        # Also include the original entities we searched for
        all_touched = list(dict.fromkeys(entities + touched))

        return {
            "additionalContext": "\n".join(lines),
            "touchedEntities": all_touched,
        }

    def build_hook_handlers(self) -> dict[str, Any]:
        """
        Build a dict of hook handlers for use with Claude SDK.

        Returns:
            {"PreToolUse": callable(tool_name, tool_input, session_id) -> dict}
        """
        return {
            "PreToolUse": lambda tool_name, tool_input, session_id: self.pre_tool_use(
                tool_name=tool_name,
                tool_input=tool_input,
                session_id=session_id,
            )
        }


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
        middleware = AgentDBClaudeMiddleware(db=db, agent_id=agent_id)

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
        middleware = AgentDBClaudeMiddleware(db=db, agent_id=agent_id)
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
