# Skill Cleanup Report

**Release:** boundary-cleanup & retrieval-correctness hardening
**Repository:** `datosurf-dev/agentdb`
**Branch:** `release/boundary-cleanup`
**Base revision:** `ffe1e36` (main)
**Examined/tested revision:** `3330c6e` and later (this branch)
**Spec:** `docs/spec/BOUNDARY_CLEANUP.md` §3 (Workstream A), Gate A

## 1. Objective

Confirm that AgentDB contains **no active or public skill-management
functionality**, no skill-injection capability, and no skill-database
dependency at normal startup, while memory functionality is preserved.

## 2. Inventory of residual skill references

Searched the whole repository for `Skill`, `SkillStore`, `SkillsAPI`,
`skill_store`, `skills_api`, `inject_skills`, skill proposals/approvals,
skill evolution/staleness, skill review notifications, skill-specific
consolidation fields, and skill persistence/CLI/MCP/exports/dependencies.

| Occurrence | Location | Classification | Action |
|---|---|---|---|
| `skills are no longer managed or injected` (docstring) | `src/agentdb/adapters/hermes.py:117` | Documentation residue (accurate) | **Kept** — correctly states the boundary |
| `Skill proposal/approval workflows have been removed…` (module docstring) | `src/agentdb/gateway/__init__.py:8` | Documentation residue (accurate) | **Kept** |
| `skill_candidate_threshold` param (unused) | `governance/lifecycle_manager.py:consolidate` | Obsolete public API (dead skill flag) | **Removed** |
| `skill_candidate_threshold` docstring mention | same | Documentation residue | **Removed/updated** |
| `tracking_directories=["docs", ".claude/skills"]` | `docs/integrations/*`, `examples/*` | Generic functionality (directory tracking of a `skills/` *folder*, not skill management) | **Kept** — not skill management |
| `docs/spec/open-model-skills.md` | AgentGate repo | **Different product** (AgentGate's own runtime skills feature) | **Out of scope** — not AgentDB |

**No active skill-specific runtime logic, no public skill API, no skill
injection helper, no skill notifications, no skill CLI/MCP endpoint, and no
skill dependency remain in AgentDB.** Functional removal was completed in the
prior extraction release (0.5.0); this release removes the last dead parameter.

## 3. Change made (this release)

### 3.1 Removed the dead `skill_candidate_threshold` parameter
`LifecycleManager.consolidate()` declared `skill_candidate_threshold: int = 5`
that was accepted and ignored. It is a skill-specific configuration flag that
could misleadingly imply skill management still exists. Removed from the
signature. The general `gateway=` parameter is unrelated to skills (governance
surface) and is retained.

### 3.2 Backward-compatible deprecation shim
Removing the parameter from `LifecycleManager.consolidate()` alone would make
`AgentDB.consolidate(agent_id, skill_candidate_threshold=N)` raise `TypeError`
(it forwards `**kwargs`). To avoid an unnecessary breaking change
(spec: "preserve public API wherever practical"), the public
`AgentDB.consolidate()` now **accepts, warns on, and drops** the obsolete kwarg:

```python
if "skill_candidate_threshold" in kwargs:
    kwargs.pop("skill_candidate_threshold")
    warnings.warn("consolidate(skill_candidate_threshold=...) is obsolete and ignored: "
                  "AgentDB no longer manages skills.", DeprecationWarning, stacklevel=2)
```

It reactivates nothing. Removal of the kwarg entirely is deferred to a future
major/policy-approved release (see `OPEN_DECISIONS.md`).

### 3.3 Documentation correctness
Corrected the stale `AgentDB.consolidate()` docstring which still claimed the
method "mark[s] the surviving canonical as consolidated=True" — the current
behavior creates a NEW `derived_canonical` record and soft-archives sources.

## 4. Legacy data preservation (spec §A3)

- No skill databases were created, modified, or deleted by this work.
- AgentDB no longer requires or touches any `skills.sqlite` at startup.
- Skill-data export/migration schemas live in the prior handoff material under
  `docs/spec/migration-handoff/` (historical, gitignored local docs) and are not
  part of this release's runtime.

## 5. Packaging / docs

- `pyproject.toml` optional-dependencies: `dev`, `mcp`, `local-embed`,
  `notifications`, `providers` — **no** `wikiskill`/`skillopt`/`evolution`.
- README/CHANGELOG: no skill-registration/injection/staleness/approval/evolution
  claims remain (verified by grep). CHANGELOG `[Unreleased]` notes the param
  removal.
- (Local, untracked build artifacts — `src/agentdatabase.egg-info/` and
  `tests/unit/__pycache__/test_skill_*.pyc` — still contain skill strings; these
  are regenerated on install and are not tracked in git. See §6.)

## 6. Non-tracked residue (informational)

Stale local artifacts from the pre-extraction era exist but are **not tracked**:
`src/agentdatabase.egg-info/PKG-INFO` (old README/extras) and
`tests/unit/__pycache__/test_skill_*.pyc` (deleted tests' bytecode). They are
ignored by `.gitignore` (`*.egg-info/`, `__pycache__/`) and regenerate on
`pip install -e`. They will not ship. Optionally cleaned with
`find . -name '*.pyc' -delete && rm -rf src/agentdatabase.egg-info` (not run
here to avoid touching build outputs).

## 7. Test evidence

- `tests/unit/test_skill_cleanup_compat.py`: dead kwarg accepted+warned (no
  raise); `agentdb.Skill/SkillStore/SkillsAPI` absent; `db.skills*` absent;
  `import agentdb.skills` raises `ImportError`.
- Full suite green: **504 passed, 4 skipped, 1 xfailed**.

## 8. Gate A status

**PASS.**
- No active or public AgentDB skill-management functionality. ✓
- No default skill-injection capability. ✓ (adapters/middleware emit no skill text; `test_adapter_*` + integration suite)
- No skill-database dependency at normal startup. ✓
- Current docs/packaging match behavior. ✓
- Archived skill data remains recoverable (untouched). ✓
