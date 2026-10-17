from dataclasses import dataclass


@dataclass(frozen=True)
class RunnerCapabilities:
    """Frozen dataclass representing the capabilities of a runner tier."""

    can_call_tools_mid_turn: bool = False
    can_write_mid_turn: bool = False
    has_native_memory: bool = False
    inject_skills: bool = True
    inject_workspace: bool = True
    inject_work_context: bool = True
    max_injection_tokens: int = 4096
    auto_elevate_threshold: int = 3


# Claude capabilities: no injection (Claude has native context management)
CLAUDE_CAPABILITIES = RunnerCapabilities(
    can_call_tools_mid_turn=True,
    can_write_mid_turn=False,
    has_native_memory=False,
    inject_skills=False,
    inject_workspace=False,
    inject_work_context=False,
    max_injection_tokens=0,
    auto_elevate_threshold=3,
)

# Fireworks capabilities: partial injection
FIREWORKS_CAPABILITIES = RunnerCapabilities(
    can_call_tools_mid_turn=False,
    can_write_mid_turn=False,
    has_native_memory=True,
    inject_skills=True,
    inject_workspace=True,
    inject_work_context=False,
    max_injection_tokens=2048,
    auto_elevate_threshold=3,
)

# Local capabilities: full injection
LOCAL_CAPABILITIES = RunnerCapabilities(
    can_call_tools_mid_turn=False,
    can_write_mid_turn=False,
    has_native_memory=False,
    inject_skills=True,
    inject_workspace=True,
    inject_work_context=True,
    max_injection_tokens=4096,
    auto_elevate_threshold=3,
)
