# AgentDB Agent Instructions

## Scope

These instructions are repository-level and apply to all agent work in this repo, including runs that do not invoke superpowers skills.

## Canonical Context Sources

1. Global instructions: `/Users/jm/Documents/github/agentdb/AGENTS.md`
2. Superpowers plan (active/open work): `/Users/jm/Documents/github/agentdb/docs/superpowers/plans/2026-06-06-agentdb-revised.md`
3. Completed work tracker: `/Users/jm/Documents/github/agentdb/docs/superpowers/plans/completed-tasks.md`
4. Architecture & design principles: `/Users/jm/Documents/github/agentdb/docs/context/architecture-principles.md`
5. Capabilities by type: `/Users/jm/Documents/github/agentdb/docs/context/capabilities.md`
6. Database schema and memory assets: `/Users/jm/Documents/github/agentdb/docs/context/database-schema.md`
7. Algorithm review skill (when touching ranking/reinforcement surfaces): `/Users/jm/Documents/github/agentdb/.claude/skills/agentdb-algorithm-review/SKILL.md`

If guidance conflicts, follow this file first, then the superpowers plan.

## Algorithm Review Trigger

When changes touch algorithm-related surfaces (for example retrieval ranking, lifecycle reinforcement, retrieval/outcome flows in client, or corpus stats), run the repository skill at:

- `/Users/jm/Documents/github/agentdb/.claude/skills/agentdb-algorithm-review/SKILL.md`

## Architecture Intent (Always-On)

Full numbered design principles live in `docs/context/architecture-principles.md` — read it before touching backend contracts, permission routing, retrieval, or adapters. The single most load-bearing one for day-to-day agent work: **Principle 12** — an adapter capability isn't done because an API exists; it's done when a test proves it's enforced through the framework's own control flow (real-time, Principle 12a) or runs as an auditable, consumer-having background process (async, Principle 12b).

## Implementation Guardrails

- Do not claim semantic retrieval/conflict behavior before embedding-backed implementation is active.
- Keep conflict flags assembled before memory content in retrieval flows.
- Keep provider-ingested origin semantics strict; do not silently promote trust levels.
- Keep tests aligned with package-style imports and editable install workflow.
- Prefer minimal, phase-consistent changes over speculative full-phase implementations.
- Never document a capability as active/implemented before tests and code verify it. This repo's docs have drifted into overclaiming before (adapters, system tables, conflict detection all got described as done when they weren't) — treat every "Implemented" claim you write as something you personally checked against source, not something you inferred from a doc.

## Reporting Expectations

When finishing a task in this repo, report:

- Which feature group(s)/task(s) were touched.
- Which phase requirement(s) were satisfied or deferred.
- Any limitation or decision dependency encountered.
- Test evidence validating the intended behavior.

## Packaging And Test Expectations

- Source layout is `src/`.
- Imports should be package-style (for example: `from agentdb import AgentDB`).
- Validate with editable install and venv-local pytest invocation:
  - `pip install -e '.[dev]'`
  - `python -m pytest -q`

## Current Reality Check

Repository implementation is partial; use tests and on-disk modules as truth before assuming planned components exist.
