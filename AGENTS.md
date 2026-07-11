# AgentDB Agent Instructions

## Scope

These instructions are repository-level and apply to all agent work in this repo, including runs that do not invoke superpowers skills.

## Canonical Context Sources

1. Global instructions: `/Users/jm/Documents/github/agentdb/AGENTS.md`
2. Project context document: `/Users/jm/Documents/github/agentdb/docs/context/agentdb-context.md`
3. Superpowers plan: `/Users/jm/Documents/github/agentdb/docs/superpowers/plans/2026-06-06-agentdb-revised.md`
4. Algorithm review skill (when touching ranking/reinforcement surfaces): `/Users/jm/Documents/github/agentdb/.claude/skills/agentdb-algorithm-review/SKILL.md`

If guidance conflicts, follow this file first, then the superpowers plan.

## Algorithm Review Trigger

When changes touch algorithm-related surfaces (for example retrieval ranking, lifecycle reinforcement, retrieval/outcome flows in client, or corpus stats), run the repository skill at:

- `/Users/jm/Documents/github/agentdb/.claude/skills/agentdb-algorithm-review/SKILL.md`

## Architecture Intent (Always-On)

- AgentDB is a governed memory database, not a prompt-only framework.
- Keep a framework-agnostic backend contract (`AgentDBBackend`) as the integration center.
- Enforce write-time governance boundaries (human-approved vs agent-inferred) via permission routing.
- Treat audit trail and event bus as separate subsystems.
- Preserve phase-gated behavior for retrieval and conflict detection.

## Implementation Guardrails

- Do not claim semantic retrieval/conflict behavior before embedding-backed implementation is active.
- Keep conflict flags assembled before memory content in retrieval flows.
- Keep provider-ingested origin semantics strict; do not silently promote trust levels.
- Keep tests aligned with package-style imports and editable install workflow.
- Prefer minimal, phase-consistent changes over speculative full-phase implementations.

## Packaging And Test Expectations

- Source layout is `src/`.
- Imports should be package-style (for example: `from agentdb import AgentDB`).
- Validate with editable install and venv-local pytest invocation:
  - `pip install -e '.[dev]'`
  - `python -m pytest -q`

## Current Reality Check

Repository implementation is partial; use tests and on-disk modules as truth before assuming planned components exist.
