from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import yaml


class PermissionDenied(Exception):
    pass


# Prefixes owned by AgentDB's own framework machinery (not agent business
# data) — writable by default regardless of default_policy/can_write,
# because gating them requires every integrating deployment to remember a
# permissions.yaml edit before the feature works at all. An explicit
# cannot_write entry still overrides this (see check_write below) — this is
# a default, not an unconditional bypass.
FRAMEWORK_MANAGED_WRITE_PREFIXES: tuple[str, ...] = ("proposal/",)


class PermissionRouter:
    """
    Routes permission checks based on a YAML policy file.

    YAML format:
        default_policy: deny
        human_zone_prefixes:
          - "skills/"
        agents:
          agent_id:
            can_write:
              - "memory/working/"
            cannot_write:
              - "skills/"
            can_read:
              - "memory/working/"
    """

    def __init__(self, permissions_path: Any) -> None:
        path = Path(permissions_path)
        with path.open("r", encoding="utf-8") as f:
            self._config: dict = yaml.safe_load(f) or {}

        self._default_policy: str = self._config.get("default_policy", "deny")
        self._human_zone_prefixes: list[str] = self._config.get("human_zone_prefixes", [])
        self._agents: dict = self._config.get("agents", {})

    def _matches_any(self, key: str, patterns: list[str]) -> bool:
        for pattern in patterns:
            # Treat patterns ending with '/' as prefix matches,
            # also support glob patterns
            if pattern.endswith("/"):
                if key.startswith(pattern):
                    return True
            elif fnmatch.fnmatch(key, pattern + "*") or fnmatch.fnmatch(key, pattern):
                return True
        return False

    def is_human_zone(self, key: str) -> bool:
        for prefix in self._human_zone_prefixes:
            if key.startswith(prefix):
                return True
        return False

    def check_write(self, key: str, agent_id: str, trust_zone: str) -> None:
        """
        Raises PermissionDenied if the agent is not allowed to write the key.

        Rules:
        - If trust_zone == "agent_inferred" and key is in a human_zone_prefix: deny
        - Check cannot_write before can_write
        - default_policy: deny means deny unless explicitly allowed
        """
        # Agent-inferred writes to human zones are always denied
        if trust_zone == "agent_inferred" and self.is_human_zone(key):
            raise PermissionDenied(
                f"Agent '{agent_id}' cannot write to human zone key '{key}' "
                f"with trust_zone '{trust_zone}'"
            )

        agent_config = self._agents.get(agent_id, {})

        # Check cannot_write first
        cannot_write = agent_config.get("cannot_write", [])
        if self._matches_any(key, cannot_write):
            raise PermissionDenied(
                f"Agent '{agent_id}' is explicitly denied write access to '{key}'"
            )

        # Check can_write
        can_write = agent_config.get("can_write", [])
        if self._matches_any(key, can_write):
            return  # allowed

        # Framework-managed prefixes are writable by default (cannot_write,
        # checked above, still overrides this).
        if self._matches_any(key, FRAMEWORK_MANAGED_WRITE_PREFIXES):
            return  # allowed

        # Fall through to default policy
        if self._default_policy == "deny":
            raise PermissionDenied(
                f"Agent '{agent_id}' has no explicit write permission for '{key}' "
                f"and default_policy is 'deny'"
            )

    def check_read(self, key: str, agent_id: str) -> None:
        """
        Raises PermissionDenied if the agent is not allowed to read the key.
        """
        agent_config = self._agents.get(agent_id, {})

        can_read = agent_config.get("can_read", [])
        if self._matches_any(key, can_read):
            return  # allowed

        if self._default_policy == "deny":
            raise PermissionDenied(
                f"Agent '{agent_id}' has no explicit read permission for '{key}' "
                f"and default_policy is 'deny'"
            )
