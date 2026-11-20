# TEST_RESULTS — Skill Extraction Phase 1

**Source commit (baseline):** `fe871c4` — `547 passed, 4 skipped`
**Working commit (post-extraction):** `c3e251f` (code) + doc commit
**Python:** 3.14.7 · pytest
**Command:** `python -m pytest -q` (editable install, `.venv`)

## Summary

| Stage | Result |
|---|---|
| Baseline (before any change) | **547 passed, 4 skipped** |
| After Stage 1 preserve (no behavior change) | 547 passed, 4 skipped |
| After Stage 2 adapters (skill injection removed) | 539 passed, 4 skipped |
| After Stages 3–6 (full removal) | **428 passed, 4 skipped** |

The 119-test delta is entirely skill-dedicated and skill-dependent tests that
were removed (10 whole skill test files deleted; skill-only tests inside mixed
files removed). No memory-behavior test failed at any stage.

## Targeted validation

| Area | Command | Result |
|---|---|---|
| Async adapters + Hermes + Claude adapter | `pytest tests/unit/test_middleware_injection.py tests/unit/test_hermes_adapter.py tests/integration/test_claude_agent_sdk_adapter.py` | 62 passed |
| Consolidation + injection flow + purge + gateway (backwards-compat) | `pytest tests/unit/test_memory_consolidation.py tests/integration/test_injection_consolidation_flow.py tests/unit/test_purge_session.py tests/unit/test_gateway.py` | 32 passed |
| Full suite | `pytest -q` | 428 passed, 4 skipped |

## Requirement checks (T01 etc. adapted to this task)

1. **No skill text injected by any adapter** — VERIFIED.
   - `test_no_skills_block_in_local_context`, `test_no_skills_block_in_claude_context`,
     `test_no_skills_block_in_claude_context` assert no `## Skills` block in system messages.
   - Manual async check (LOCAL + SERVERLESS capabilities): no `skill`/`## Skills`/
     `Governed Skills` regex match in any system message.
   - `AgentDBHermesProvider.system_prompt_block()` returns `""` (verified directly and in tests).
2. **Memory APIs still work** — VERIFIED. Full memory suite passes; a live
   `retrieve()` on `:memory:` returned the written record.
3. **Ranking/consolidation unchanged** — VERIFIED. `consolidate()` tests pass; only
   skill-candidate dispatch and skill-conflict re-check removed.
4. **Adapters produce memory-only context** — VERIFIED (see 1).
5. **Skill data exportable/inspectable** — VERIFIED. `export_skills.py` tested against a
   synthetic `skills.sqlite`: exported 1 skill to JSONL with header + decoded fields.
6. **Handoff contains all identified skill functionality** — VERIFIED (see `SOURCE_INVENTORY.md`).
7. **Tests and package imports succeed** — VERIFIED. All modules import; full suite green.
8. **Docs reflect new boundary** — VERIFIED. README, AGENTS.md, docs/context/* updated;
   `CHANGELOG.md` added.

## Known non-blocking observations

- 4 skipped tests are pre-existing skips unrelated to skills (embedding/optional deps).
- `pytest-asyncio` emits a deprecation warning about unset fixture loop scope — pre-existing.
- `consolidate()` retains `gateway` and `skill_candidate_threshold` parameters for API
  compatibility but no longer uses them (documented in the method docstring).

## Reproduce

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,mcp]'
python -m pytest -q
```
