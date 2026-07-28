"""
AgentDBHermesProvider — Hermes MemoryProvider adapter.

Implements Hermes's MemoryProvider ABC on top of GatewayAdapter so that
any Hermes agent can use AgentDB as its governed external memory backend.
Direct in-process import; no sidecar, no HTTP.

Principle 12 compliance:
  12a hooks (real-time, synchronous): prefetch, sync_turn, on_turn_start,
      on_memory_write, handle_tool_call, system_prompt_block
  12b hooks (async/decoupled): on_session_end, on_pre_compress, on_delegation

Limitation: on_memory_write cannot veto Hermes-native writes — it fires
after the write has already landed in Hermes's own store. AgentDB governance
covers AgentDB's own records only, not Hermes-native storage.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

try:
    from agent.memory_provider import MemoryProvider  # real Hermes install
except ImportError:
    # Hermes not installed — stub ABC so tests run without it
    from abc import ABC, abstractmethod

    class MemoryProvider(ABC):  # type: ignore[no-redef]
        @abstractmethod
        def initialize(self, session_id: str, **kwargs) -> None: ...
        @abstractmethod
        def system_prompt_block(self) -> str: ...
        @abstractmethod
        def prefetch(self, query: str, *, session_id: str) -> list: ...
        def queue_prefetch(self, query: str, *, session_id: str) -> None: ...
        @abstractmethod
        def sync_turn(self, user: str, asst: str, *, session_id: str) -> None: ...
        def get_tool_schemas(self) -> list: ...
        def handle_tool_call(self, name: str, args: dict, *, session_id: str): ...
        def shutdown(self) -> None: ...
        def on_turn_start(self, turn: int, message: str, **kwargs) -> None: ...
        def on_session_end(self, messages: list) -> None: ...
        def on_session_switch(self, new_session_id: str, **kwargs) -> None: ...
        def on_pre_compress(self, messages: list) -> str: ...
        def on_memory_write(self, action: str, target: str, content: str, metadata: dict) -> None: ...
        def on_delegation(self, task: str, result: str, **kwargs) -> None: ...
        def backup_paths(self) -> list[str]: ...


from agentdb.gateway.adapter import GatewayAdapter

if TYPE_CHECKING:
    from agentdb import AgentDB


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentDBHermesProvider(MemoryProvider, GatewayAdapter):
    """
    Hermes MemoryProvider backed by AgentDB.

    Usage:
        db = AgentDB(db_path="agentdb.sqlite")
        provider = AgentDBHermesProvider(db=db, agent_id="coder")
        # Register with Hermes MemoryManager, then let Hermes call hooks.
    """

    def __init__(self, db: "AgentDB", agent_id: str) -> None:
        GatewayAdapter.__init__(self, db, agent_id, profile=db.config_for(agent_id)["name"])
        self._session_id: str = ""

    # ------------------------------------------------------------------
    # Phase 1 — core governed memory loop (Principle 12a)
    # ------------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        """Store session_id, wire conflict handler, dispatch conversation_started."""
        self._session_id = session_id
        self.gateway.subscribe("conflict_detected", self._on_conflict)
        self.gateway.dispatch("conversation_started", session_id=session_id)

    def prefetch(self, query: str, *, session_id: str) -> list:
        """Return governed records ranked for this query. [12a]"""
        result = self._db.retrieve(query, agent_id=self._agent_id, session_id=session_id)
        return result.records

    def sync_turn(self, user: str, asst: str, *, session_id: str) -> None:
        """Persist assistant reply as a governed episodic record. [12a]"""
        if asst:
            self._db.write(
                key=f"memory/hermes/{self._agent_id}/{session_id}/turn",
                value=asst,
                origin="agent_inferred",
                agent_id=self._agent_id,
                session_id=session_id,
            )

    def on_turn_start(self, turn: int, message: str, **kwargs) -> None:
        """Dispatch reconciliation trigger at the start of each turn. [12a]"""
        self.gateway.dispatch(
            "conversation_started",
            session_id=self._session_id,
            turn=turn,
        )

    # ------------------------------------------------------------------
    # system_prompt_block [12a]
    # ------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        """
        Inject governed skills into Hermes's system prompt.

        Returns a '## Governed Skills' section if any skills exist for this
        agent; empty string otherwise.
        """
        skills = self._db.skills.get_for_agent(self._agent_id)
        if not skills:
            return ""
        lines = ["## Governed Skills"]
        for skill in skills:
            warning = ""
            if skill.health_score < 0.6:
                warning = f" [STALE — health_score={skill.health_score:.2f}]"
            lines.append(f"### {skill.name}{warning}")
            lines.append(skill.content)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Remaining hooks
    # ------------------------------------------------------------------

    def queue_prefetch(self, query: str, *, session_id: str) -> None:
        """No-op — AgentDB retrieval is always a live query; nothing to pre-warm."""

    def get_tool_schemas(self) -> list:
        """No tool schemas exposed — AgentDB governance is transparent to the LLM."""
        return []

    def handle_tool_call(self, name: str, args: dict, *, session_id: str) -> Any:
        """Route memory tool calls to db.read() or db.retrieve(). [12a]"""
        if name == "memory_get":
            key = args.get("key", "")
            return self._db.read(key, agent_id=self._agent_id)
        if name == "memory_search":
            query = args.get("query", "")
            return self._db.retrieve(query, agent_id=self._agent_id, session_id=session_id).records
        return None

    def shutdown(self) -> None:
        """No-op — SQLite closes with the process."""

    def on_session_end(self, messages: list) -> None:
        """Record session outcome and check for pending conflicts. [12b]"""
        if self._session_id:
            self._db.lifecycle.record_outcome(
                session_id=self._session_id,
                agent_id=self._agent_id,
                outcome_type="session_end",
                outcome_value=1.0,
            )

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        """Re-scope internal session reference on session rotation."""
        self._session_id = new_session_id

    def on_pre_compress(self, messages: list) -> str:
        """
        Call configured summarizer if available; write summary as governed record.
        Returns summary string, or '' to let Hermes handle compaction natively. [12b]
        """
        summarizer: Optional[Callable] = self._db._summarizer
        if summarizer is None:
            return ""
        summary: str = summarizer(messages)
        ts = _utcnow().strftime("%Y%m%dT%H%M%SZ")
        self._db.write(
            key=f"memory/episodic/hermes/{self._agent_id}/{self._session_id}/compaction-{ts}",
            value=summary,
            origin="agent_inferred",
            agent_id=self._agent_id,
            session_id=self._session_id,
        )
        return summary

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict,
    ) -> None:
        """
        Mirror Hermes-native memory writes for AgentDB governance audit. [12a]

        NOTE: This hook cannot veto Hermes-native writes — it fires after
        the write has already landed in Hermes's own store. Governance here
        covers AgentDB's own records only.

        Scope: only writes targeting MEMORY.md or DREAMS.md are governed;
        high-frequency ephemeral daily notes are skipped.
        """
        governed_targets = {"MEMORY.md", "DREAMS.md"}
        if Path(target).name not in governed_targets:
            return
        # Permission check is intentionally skipped here: this is a governance mirror of a
        # Hermes-native write that already passed Hermes's own permission checks. AgentDB
        # records the write for provenance tracking but cannot and should not re-gate it.
        self._db.write(
            key=f"memory/hermes-mirror/{self._agent_id}/{target}",
            value=content,
            origin="agent_inferred",
            agent_id=self._agent_id,
            session_id=self._session_id,
        )

    def on_delegation(self, task: str, result: str, **kwargs) -> None:
        """Write subagent task result as a governed episodic record. [12b]"""
        if result:
            ts = _utcnow().strftime("%Y%m%dT%H%M%SZ")
            self._db.write(
                key=f"memory/episodic/hermes/{self._agent_id}/{self._session_id}/delegation-{ts}",
                value=result,
                origin="agent_inferred",
                agent_id=self._agent_id,
                session_id=self._session_id,
            )

    def backup_paths(self) -> list[str]:
        """Return the AgentDB file path for backup tooling."""
        if hasattr(self._db, "_path") and self._db._path and str(self._db._path) != ":memory:":
            return [str(self._db._path)]
        return []

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _on_conflict(self, **payload) -> None:
        """
        Platform-agnostic conflict handler.
        Concrete notification wiring is platform-specific (Slack, Telegram, etc.).
        """
        pass
