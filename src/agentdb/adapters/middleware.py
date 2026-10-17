"""
AgentDBMiddleware — model-agnostic middleware for AgentDB integrations.

This adapter does not import any specific model SDK. Instead, it provides a
runtime contract wrapper around AgentDB so agents can enforce:

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
import os
import subprocess

from ..core.directory_tracking import collect_directory_snapshots
from ..gateway.adapter import GatewayAdapter
from .capabilities import RunnerCapabilities, LOCAL_CAPABILITIES

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


@dataclass
class DirectoryRecord:
    """Tracks file system directory access within a session."""
    path: str
    first_seen_turn: int
    last_seen_turn: int
    access_count: int
    elevated: bool = False


@dataclass(frozen=True)
class ClaudeRuntimeConfig:
    model: str
    system_prompt: Optional[str] = None
    max_tokens: int = 1024


class AgentDBMiddleware(GatewayAdapter):
    """
    Middleware-style helper for model-agnostic AgentDB integrations.

    Model calls are supplied as callbacks so this module stays runtime-agnostic.
    Inherits GatewayAdapter for access to db.gateway (reconciliation, proposals).
    """

    def __init__(
        self,
        db: "AgentDB",
        agent_id: str,
        conversation_started_callback: Optional[Callable[..., Any]] = None,
        capabilities: RunnerCapabilities = LOCAL_CAPABILITIES,
    ):
        resolved = db.config_for(agent_id)
        super().__init__(db, agent_id, profile=resolved["name"])
        self._conversation_started_callback = conversation_started_callback
        self._resolved = resolved
        self._session_working_dirs: dict[str, list[str]] = {}
        self._session_dir_state: dict[str, list[DirectoryRecord]] = {}
        self._session_work_log: dict[str, list[dict]] = {}
        self._capabilities = capabilities
        self.gateway.subscribe("conflict_detected", self._on_conflict)

    def prepare_turn(
        self,
        *,
        user_query: str,
        session_id: str,
        system_prompt: Optional[str] = None,
        external_context: Optional[list[dict[str, Any]]] = None,
        base_messages: Optional[list[dict[str, str]]] = None,
        working_dirs: Optional[list[str]] = None,
    ) -> tuple[list[dict[str, str]], "RetrievalResult"]:
        """
        Enforced pre-response hook: always retrieves governed memory first.
        Returns model messages and raw retrieval result for optional inspection.
        """
        if working_dirs is not None:
            self._session_working_dirs[session_id] = [
                os.path.realpath(d) for d in working_dirs
            ]
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
            working_dirs=self._session_working_dirs.get(session_id),
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
        session_dirs = self._session_working_dirs.get(session_id)

        explicit_writes = list(memory_writes or [])
        for item in explicit_writes:
            self._db.write(
                key=item["key"],
                value=item["value"],
                origin=item.get("origin", "agent_inferred"),
                agent_id=self._agent_id,
                session_id=session_id,
                entities=touched_entities or [],
                working_dirs=session_dirs,
            )

        if self._resolved["capture"]["auto_persist_response"] and model_response:
            self._db.write(
                key=self._response_key(session_id),
                value={
                    "response": model_response,
                    "captured_at": _utcnow().isoformat(),
                    "profile": self._resolved["name"],
                },
                origin="agent_inferred",
                agent_id=self._agent_id,
                session_id=session_id,
                entities=touched_entities or [],
                working_dirs=session_dirs,
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

    def _build_work_context_block(self, session_id: str) -> str:
        """Return a formatted markdown block of the last 5 turns from the work log, or '' if empty."""
        log = self._session_work_log.get(session_id, [])
        if not log:
            return ""
        lines: list[str] = []
        for entry in log[-5:]:
            parts: list[str] = [f"Turn {entry['turn_num']}:"]
            if entry.get("files_read"):
                parts.append(f"read {', '.join(entry['files_read'])}.")
            if entry.get("files_written"):
                parts.append(f"wrote {', '.join(entry['files_written'])}.")
            if entry.get("key_fact"):
                parts.append(entry["key_fact"])
            lines.append(" ".join(parts))
        return "\n".join(lines)

    def _trim_to_budget(self, content: str, token_budget: int) -> str:
        """Truncate content to token_budget tokens (approximated as len // 4)."""
        if len(content) // 4 <= token_budget:
            return content
        return content[: token_budget * 4] + "…"

    def _build_skills_block(self, session_id: str, user_query: str) -> str:
        """Return a formatted markdown block of relevant skills, or '' if none qualify."""
        try:
            if user_query.strip():
                skills = self._db.skills.search(query=user_query, agent_id=self._agent_id)
            else:
                skills = self._db.skills.get_for_agent(self._agent_id)

            skills = sorted(
                (s for s in skills if s.health_score >= 0.5 and not s.archived),
                key=lambda s: s.health_score,
                reverse=True,
            )[:5]

            if not skills:
                return ""

            return "\n\n".join(f"### {s.name}\n{s.content}" for s in skills)
        except Exception:
            return ""

    _WORKSPACE_EXCLUDED_DIRS = {"__pycache__", "node_modules", "dist", ".venv", "venv", "env"}

    def _build_workspace_block(self, session_id: str, working_dirs: list[str]) -> str:
        """
        Build a workspace snapshot block for injection. Best-effort: swallows all exceptions.
        Lists up to 2 levels deep, excludes hidden dirs and common noise dirs.
        Appends git branch/status when available. Includes session temp dirs.
        Returns '' if nothing collected.
        """
        sections: list[str] = []

        try:
            for raw_dir in working_dirs:
                try:
                    if not os.path.isdir(raw_dir):
                        continue

                    lines: list[str] = [raw_dir]

                    # List up to 2 levels deep
                    try:
                        for entry in sorted(os.scandir(raw_dir), key=lambda e: e.name):
                            if entry.name.startswith(".") or entry.name in self._WORKSPACE_EXCLUDED_DIRS:
                                continue
                            lines.append(f"  {entry.name}{'/' if entry.is_dir() else ''}")
                            if entry.is_dir():
                                try:
                                    for sub in sorted(os.scandir(entry.path), key=lambda e: e.name):
                                        if sub.name.startswith(".") or sub.name in self._WORKSPACE_EXCLUDED_DIRS:
                                            continue
                                        lines.append(f"    {sub.name}{'/' if sub.is_dir() else ''}")
                                except OSError:
                                    pass
                    except OSError:
                        pass

                    # Git info
                    try:
                        branch_result = subprocess.run(
                            ["git", "-C", raw_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=5,
                        )
                        if branch_result.returncode == 0:
                            branch = branch_result.stdout.strip()
                            lines.append(f"  [git branch: {branch}]")
                            status_result = subprocess.run(
                                ["git", "-C", raw_dir, "status", "--short"],
                                capture_output=True, text=True, timeout=5,
                            )
                            if status_result.returncode == 0 and status_result.stdout.strip():
                                for status_line in status_result.stdout.strip().splitlines():
                                    lines.append(f"  {status_line}")
                    except Exception:
                        pass

                    sections.append("\n".join(lines))
                except Exception:
                    pass

            # Temp dirs from session state (non-elevated only)
            try:
                records = self._session_dir_state.get(session_id, [])
                temp_lines: list[str] = []
                for record in records:
                    if not record.elevated:
                        temp_lines.append(f"  {record.path} (this session)")
                if temp_lines:
                    sections.append("Session directories:\n" + "\n".join(temp_lines))
            except Exception:
                pass

        except Exception:
            pass

        return "\n\n".join(sections)

    async def prepare_turn_async(
        self,
        *,
        user_query: str,
        session_id: str,
        system_prompt: Optional[str] = None,
        external_context: Optional[list[dict[str, Any]]] = None,
        base_messages: Optional[list[dict[str, str]]] = None,
        working_dirs: Optional[list[str]] = None,
    ) -> tuple[list[dict[str, str]], "RetrievalResult"]:
        messages, retrieval = self.prepare_turn(
            user_query=user_query,
            session_id=session_id,
            system_prompt=system_prompt,
            external_context=external_context,
            base_messages=base_messages,
            working_dirs=working_dirs,
        )

        new_blocks: list[dict[str, str]] = []

        skills_budget = int(self._capabilities.max_injection_tokens * 0.4)
        workspace_budget = int(self._capabilities.max_injection_tokens * 0.3)
        work_context_budget = int(self._capabilities.max_injection_tokens * 0.3)

        if self._capabilities.inject_skills:
            block = self._build_skills_block(session_id, user_query)
            if block:
                block = self._trim_to_budget(block, skills_budget)
            if block:
                new_blocks.append({"role": "system", "content": f"## Skills\n{block}"})

        if self._capabilities.inject_workspace:
            ws = self._build_workspace_block(session_id, working_dirs or [])
            if ws:
                ws = self._trim_to_budget(ws, workspace_budget)
            if ws:
                new_blocks.append({"role": "system", "content": f"## Workspace\n{ws}"})

        if self._capabilities.inject_work_context:
            wc = self._build_work_context_block(session_id)
            if wc:
                wc = self._trim_to_budget(wc, work_context_budget)
            if wc:
                new_blocks.append({"role": "system", "content": f"## Current work context\n{wc}"})

        if new_blocks:
            first_user = next(
                (i for i, m in enumerate(messages) if m["role"] == "user"), len(messages)
            )
            messages[first_user:first_user] = new_blocks

        return messages, retrieval

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

    def record_turn_context(
        self,
        session_id: str,
        turn_num: int,
        files_read: list[str],
        files_written: list[str],
        key_fact: str,
    ) -> None:
        """
        Record turn context to the session work log.
        Caps at 20 turns; drops oldest when exceeding limit.
        """
        if session_id not in self._session_work_log:
            self._session_work_log[session_id] = []

        entry = {
            "turn_num": turn_num,
            "files_read": files_read,
            "files_written": files_written,
            "key_fact": key_fact,
        }
        self._session_work_log[session_id].append(entry)

        # Cap at 20 turns: drop index 0 if list exceeds 20
        if len(self._session_work_log[session_id]) > 20:
            self._session_work_log[session_id].pop(0)

    def report_directories(
        self,
        session_id: str,
        dirs: list[str],
    ) -> None:
        """
        Report accessed directories for a session.
        Upserts DirectoryRecord entries; caps total list at 200 per session.
        """
        if session_id not in self._session_dir_state:
            self._session_dir_state[session_id] = []

        # Build a dict for fast lookup
        records_by_path = {r.path: r for r in self._session_dir_state[session_id]}

        for path in dirs:
            if path in records_by_path:
                # Upsert: increment access_count and update last_seen_turn
                records_by_path[path].access_count += 1
                records_by_path[path].last_seen_turn = len(self._session_dir_state[session_id])
            else:
                # New entry: check cap before inserting
                if len(self._session_dir_state[session_id]) >= 200:
                    # Drop oldest (index 0)
                    self._session_dir_state[session_id].pop(0)
                    # Rebuild dict after dropping
                    records_by_path = {r.path: r for r in self._session_dir_state[session_id]}

                # Create new record
                turn_proxy = len(self._session_dir_state[session_id])
                new_record = DirectoryRecord(
                    path=path,
                    first_seen_turn=turn_proxy,
                    last_seen_turn=turn_proxy,
                    access_count=1,
                    elevated=False,
                )
                self._session_dir_state[session_id].append(new_record)
                records_by_path[path] = new_record

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

    def elevate_directory(
        self,
        session_id: str,
        path: str,
        origin: str = "human_approved",
        confidence: float = 0.8,
    ) -> "MemoryRecord":
        """
        Persist a directory path to AgentDB and mark it elevated in session state.
        Returns the written MemoryRecord.
        """
        record = self._db.write(
            key=f"workspace.dir.{path}",
            value=path,
            origin=origin,
            agent_id=self._agent_id,
            session_id=session_id,
            confidence=confidence,
        )

        if session_id not in self._session_dir_state:
            self._session_dir_state[session_id] = []

        records_by_path = {r.path: r for r in self._session_dir_state[session_id]}
        if path in records_by_path:
            records_by_path[path].elevated = True
        else:
            new_record = DirectoryRecord(
                path=path,
                first_seen_turn=0,
                last_seen_turn=0,
                access_count=1,
                elevated=True,
            )
            self._session_dir_state[session_id].append(new_record)

        return record

    def write_session_summary(self, session_id: str, jsonl_path: str) -> None:
        """
        Append a session summary JSON line to jsonl_path (creates file if missing).
        """
        summary = {
            "type": "session_summary",
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_id": self._agent_id,
            "agentdb_checkpoint_key": f"memory/compaction/checkpoint/{session_id}",
            "working_dirs": self._session_working_dirs.get(session_id, []),
            "temp_dirs": [r.path for r in self._session_dir_state.get(session_id, []) if not r.elevated],
            "elevated_dirs": [r.path for r in self._session_dir_state.get(session_id, []) if r.elevated],
            "turn_count": len(self._session_work_log.get(session_id, [])),
            "last_work_context": self._build_work_context_block(session_id),
        }
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary) + "\n")

    async def end_session(
        self,
        session_id: str,
        jsonl_path: str,
        summary: Optional[dict] = None,
        run_consolidation: bool = False,
        llm_fn: Optional[Callable] = None,
    ) -> None:
        """
        Finalize a session: checkpoint, optionally consolidate, write summary, clear state.
        """
        self._db.checkpoint(agent_id=self._agent_id, session_id=session_id)
        if run_consolidation:
            self._db.consolidate(agent_id=self._agent_id, llm_fn=llm_fn)
        self.write_session_summary(session_id, jsonl_path)
        self._session_dir_state.pop(session_id, None)
        self._session_work_log.pop(session_id, None)
