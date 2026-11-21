"""Tests for default-agent-scope permissions (spec §4).

All bots are treated as registered by default: a `default_agent_scope` grants
any agent_id not listed under `agents:` that scope. A user disables a specific
bot with an explicit `agents: <id>: { disabled: true }`. Without
`default_agent_scope`, strict deny-by-default is preserved (backward compatible).
"""
from pathlib import Path

import pytest

from agentdb.governance.permission_router import PermissionRouter, PermissionDenied


def _router(tmp_path, yaml_text):
    p = tmp_path / "perm.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    return PermissionRouter(p)


_BASE = """\
default_policy: deny
human_zone_prefixes:
  - "memory/org/"
default_agent_scope:
  can_write:
    - "memory/working/"
    - "memory/episodic/"
  cannot_write:
    - "memory/org/"
  can_read:
    - "memory/working/"
    - "memory/episodic/"
agents:
  # disable a specific bot
  retired_bot:
    disabled: true
  # an explicit entry overrides the default scope
  special_bot:
    can_write:
      - "memory/org/"
"""


def test_unlisted_agent_gets_default_scope(tmp_path):
    r = _router(tmp_path, _BASE)
    # No error: unlisted agent is "registered" via default_agent_scope.
    r.check_write("memory/working/x", "brand_new_bot", "agent_inferred")
    r.check_read("memory/episodic/y", "brand_new_bot")


def test_default_scope_still_respects_cannot_write(tmp_path):
    r = _router(tmp_path, _BASE)
    with pytest.raises(PermissionDenied):
        # memory/org/ is a human zone AND in cannot_write → denied even though
        # the default scope is present (unlisted agent).
        r.check_write("memory/org/x", "brand_new_bot", "agent_inferred")


def test_default_scope_does_not_grant_ungranted_prefix(tmp_path):
    r = _router(tmp_path, _BASE)
    with pytest.raises(PermissionDenied):
        # Not in default scope's can_write and default_policy=deny → denied.
        r.check_write("memory/long_term/x", "brand_new_bot", "agent_inferred")


def test_disabled_bot_is_denied(tmp_path):
    r = _router(tmp_path, _BASE)
    with pytest.raises(PermissionDenied, match="disabled"):
        r.check_write("memory/working/x", "retired_bot", "agent_inferred")
    with pytest.raises(PermissionDenied, match="disabled"):
        r.check_read("memory/working/x", "retired_bot")


def test_explicit_entry_overrides_default_scope(tmp_path):
    r = _router(tmp_path, _BASE)
    # special_bot is explicitly granted memory/org/ writes (human zone is still
    # blocked for agent_inferred, so use a non-human key it explicitly allows).
    r.check_write("memory/org/x", "special_bot", "human_approved")


def test_no_default_scope_preserves_strict_deny(tmp_path):
    r = _router(tmp_path, """\
default_policy: deny
agents:
  listed_bot:
    can_write:
      - "memory/working/"
""")
    # Unlisted agent with no default scope → denied (backward compatible).
    with pytest.raises(PermissionDenied):
        r.check_write("memory/working/x", "unlisted_bot", "agent_inferred")
    # Listed agent still works.
    r.check_write("memory/working/x", "listed_bot", "agent_inferred")


def test_unconfigured_agents_empty_when_default_scope(tmp_path):
    r = _router(tmp_path, _BASE)
    assert r.unconfigured_agents(["any_bot", "another"]) == []


def test_unconfigured_agents_lists_when_no_default_scope(tmp_path):
    r = _router(tmp_path, "default_policy: deny\nagents:\n  a: {}\n")
    assert r.unconfigured_agents(["a", "b", "c"]) == ["b", "c"]
