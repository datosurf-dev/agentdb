# AgentDB Testing Strategy

## Purpose

Keep test intent explicit by separating behavioral tests from contract tests, so regressions are easier to locate and fix.

## Environment Standard

Run tests in a repo-local virtual environment:

1. `python3 -m venv .venv`
2. `source .venv/bin/activate`
3. `python -m pip install -e '.[dev]'`
4. `python -m pytest -q`

This keeps package imports and pytest plugin behavior (including `pytest-asyncio`) consistent with project expectations.

## Current Unit Test Split

- `tests/unit/test_backend.py`
  - Backend CRUD/list/grep/glob behavior and permission routing boundaries.
- `tests/unit/test_client_loop.py`
  - Retrieval + outcome behavioral loop (confidence updates, origin constraints).
- `tests/unit/test_audit_contracts.py`
  - Stable payload schema contracts for audit events.
- `tests/unit/test_system_stats.py`
  - Stable output contracts for `agentdb_stats()` metrics and thresholds.
- `tests/unit/test_models.py`
  - Dataclass defaults, enum values, and model-shape invariants.

## Design Rules

1. Keep one primary concern per test module.
2. Put payload/output shape checks in contract files, not behavior files.
3. Keep tests phase-accurate: do not assert semantic/embedding behavior before that phase is active.
4. Prefer package-style imports (`from agentdb import AgentDB`).
5. Add tests for additive fields before changing production callers.

## Suggested Command Sets

- Fast core regression:
  - `python -m pytest -q tests/unit/test_backend.py tests/unit/test_client_loop.py`
- Contract regression:
  - `python -m pytest -q tests/unit/test_audit_contracts.py tests/unit/test_system_stats.py`
- Current focused suite used during loop work:
  - `python -m pytest -q tests/unit/test_audit_contracts.py tests/unit/test_system_stats.py tests/unit/test_client_loop.py tests/unit/test_backend.py tests/unit/test_models.py`
