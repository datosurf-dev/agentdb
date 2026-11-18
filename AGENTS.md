# AgentDB Agent Instructions

## Scope

These instructions are repository-level and apply to all agent work in this repo.

## Canonical Context Sources

1. Repository instructions: `AGENTS.md`
2. Architecture & design principles: `docs/context/architecture-principles.md`
3. Capabilities by type: `docs/context/capabilities.md`
4. Database schema and memory assets: `docs/context/database-schema.md` (verify details against source)
5. Testing strategy: `docs/context/testing-strategy.md`
6. Integration guides: `docs/integrations/README.md`
7. Algorithm review skill (when touching ranking/reinforcement surfaces): `.claude/skills/agentdb-algorithm-review/SKILL.md`

If documentation and implementation disagree, use the current source and tests as the authority.

## Algorithm Review Trigger

When changes touch algorithm-related surfaces (for example retrieval ranking, lifecycle reinforcement, retrieval/outcome flows in client, or corpus stats), run the repository skill at:

- `.claude/skills/agentdb-algorithm-review/SKILL.md`

## Architecture Intent (Always-On)

Full numbered design principles live in `docs/context/architecture-principles.md` — read it before touching backend contracts, permission routing, retrieval, or adapters. The single most load-bearing one for day-to-day agent work: **Principle 12** — an adapter capability isn't done because an API exists; it's done when a test proves it's enforced through the framework's own control flow (real-time, Principle 12a) or runs as an auditable, consumer-having background process (async, Principle 12b).

## Implementation Guardrails

- Do not claim semantic retrieval/conflict behavior before embedding-backed implementation is active.
- Keep conflict flags assembled before memory content in retrieval flows.
- Keep provider-ingested origin semantics strict; do not silently promote trust levels.
- Do not claim read authorization: the configured `PermissionRouter` currently checks writes and deletes, while `read()` and `retrieve()` do not enforce `can_read` rules.
- Permission checks are only active when `permissions_path` is supplied to `AgentDB`.
- Keep tests aligned with package-style imports and editable install workflow.
- Prefer minimal changes that match verified implementation over speculative features.
- Never document a capability as active/implemented before tests and code verify it. This repo's docs have drifted into overclaiming before (adapters, system tables, conflict detection all got described as done when they weren't) — treat every "Implemented" claim you write as something you personally checked against source, not something you inferred from a doc.

## Reporting Expectations

When finishing a task in this repo, report:

- Which feature group(s)/task(s) were touched.
- Which requested behavior was completed or deferred.
- Any limitation or decision dependency encountered.
- Test evidence validating the intended behavior.

## Packaging And Test Expectations

- Source layout is `src/`.
- Imports should be package-style (for example: `from agentdb import AgentDB`).
- Validate with editable install and venv-local pytest invocation:
  - `pip install -e '.[dev,mcp]'`
  - `python -m pytest -q`

## Current Reality Check

Treat README feature descriptions as a summary, not a substitute for checking source and tests. Verify every capability claim against the current implementation before documenting it.
