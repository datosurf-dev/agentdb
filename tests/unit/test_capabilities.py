from agentdb.adapters.capabilities import (
    RunnerCapabilities, CLAUDE_CAPABILITIES, FIREWORKS_CAPABILITIES, LOCAL_CAPABILITIES
)


def test_runner_capabilities_defaults():
    caps = RunnerCapabilities()
    assert caps.inject_skills is True
    assert caps.inject_workspace is True
    assert caps.inject_work_context is True
    assert caps.max_injection_tokens == 4096
    assert caps.auto_elevate_threshold == 3


def test_claude_capabilities_disables_all_injection():
    assert CLAUDE_CAPABILITIES.inject_skills is False
    assert CLAUDE_CAPABILITIES.inject_workspace is False
    assert CLAUDE_CAPABILITIES.inject_work_context is False
    assert CLAUDE_CAPABILITIES.max_injection_tokens == 0
    assert CLAUDE_CAPABILITIES.can_call_tools_mid_turn is True


def test_fireworks_capabilities():
    assert FIREWORKS_CAPABILITIES.inject_skills is True
    assert FIREWORKS_CAPABILITIES.inject_work_context is False
    assert FIREWORKS_CAPABILITIES.max_injection_tokens == 2048
    assert FIREWORKS_CAPABILITIES.has_native_memory is True


def test_local_capabilities_full_injection():
    assert LOCAL_CAPABILITIES.inject_skills is True
    assert LOCAL_CAPABILITIES.inject_workspace is True
    assert LOCAL_CAPABILITIES.inject_work_context is True
    assert LOCAL_CAPABILITIES.max_injection_tokens == 4096
    assert LOCAL_CAPABILITIES.can_call_tools_mid_turn is False
