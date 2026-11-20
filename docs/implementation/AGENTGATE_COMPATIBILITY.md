# AgentGate Compatibility

**Release:** boundary-cleanup & retrieval-correctness hardening
**Repository:** `jorgemario503/agentgate`
**Branch:** `chore/agentdb-0.5-drop-skills` (base `5bc5c5a`)
**Spec:** `docs/spec/BOUNDARY_CLEANUP.md` §4 (Workstream B), Gate B

## 1. Objective

Confirm AgentGate removes obsolete AgentDB skill-management commands, keeps
memory functionality, and passes with a **real** AgentDB dependency (not mocks).
Verified against the tested AgentDB version and source revision.

## 2. Dependency under test

| Field | Value |
|---|---|
| Distribution | `agentdatabase` |
| Pinned constraint | `agentdatabase>=0.5.0` (`requirements.txt`) |
| Installed in AgentGate venv | `0.5.0` (site-packages) |
| AgentDB source revision exercised | `3330c6e` on `release/boundary-cleanup` (via `PYTHONPATH`) |

Because a compatible release (`0.5.0`) is already published and pinned, the
production dependency was **not** changed. Compatibility with the release
branch was additionally exercised by prepending the local `src/` to
`PYTHONPATH`, and the new tests run against **both** the installed 0.5.0 and the
local branch.

## 3. Inventory — AgentGate skill coupling

| Category searched | Finding |
|---|---|
| Skill proposal/approval commands | **None** (removed in 0.5.disable branch) |
| AgentDB middleware forwarding methods for skills | **None** |
| Skill-specific callbacks/notifications | **None** |
| Help text describing obsolete skill commands | **None** |
| Tests/mocks relying on removed skill APIs | **None** |

Python reference count for `skill` in AgentGate `core/`, `runners/`, `bots/`,
`tests/`: **0**.

Remaining textual matches are **not** AgentDB skill-management:
- `core/approvals.py` — human **tool-execution** approval workflow (must be
  preserved per spec; unrelated to skills).
- `permissions.yaml` — `skills/` directory write-scope entries (directory
  allow-list for tracking/reporting, not skill management).
- `docs/spec/open-model-skills.md` — AgentGate's **own** open-model runtime
  skills feature (a `skill_read` tool for local models). This is AgentGate
  product, **not** an AgentDB dependency, and is explicitly out of scope
  (spec: "Do not remove unrelated AgentGate tool-execution ... functionality";
  "Do not introduce ... Skill Governance interfaces").

## 4. Preserved behavior (verified)

| Preserved surface | Evidence |
|---|---|
| AgentDB memory writes | `tests/test_agentdb_memory_integration.py::test_memory_write_retrieve_roundtrip` |
| Memory retrieval / context injection | same + `test_agentdb_init.py::test_healthy_wires_runner_and_runs_sweep` |
| Memory compaction (session persistence) | `test_compaction_summary_persists_across_sessions` |
| Session persistence / lifecycle sweep | `test_lifecycle_sweep_runs` |
| Human approval workflows | `core/approvals.py` untouched; `tests/` green |
| Context-efficiency functionality | untouched; full suite green |

## 5. New real-dependency integration test

`tests/test_agentdb_memory_integration.py` (5 tests) uses the **real**
`agentdb` package (skips gracefully if absent):
- reports tested `agentdb` version + module path;
- write → retrieve roundtrip;
- compaction summary persists across sessions with machine origin
  (`agent_inferred`, not silently `human_approved`);
- `lifecycle.run_sweep()` runs without error;
- no removed skill surface (`Skill`, `SkillStore`, `SkillsAPI`, `db.skills`,
  `db.skills_api`) is referenced.

This complements the pre-existing `test_agentdb_init.py`, which already builds a
**real** AgentDB instance and runs a sweep (not a mock).

## 6. Test evidence

| Run | Context | Result |
|---|---|---|
| Baseline (installed 0.5.0) | full AgentGate suite | **509 passed, 3 skipped** |
| Against agentdb `3330c6e` (PYTHONPATH) — AgentDB tests | `test_agentdb_*` + caps | **10 passed** |
| Against agentdb `3330c6e` (PYTHONPATH) — full suite | full AgentGate suite | **509 passed, 3 skipped** |
| Installed 0.5.0 with new test | full suite | **514 passed, 3 skipped** |

No regressions in unrelated approval or context-efficiency behavior.

## 7. Gate B status

**PASS.**
- Obsolete AgentDB skill-specific commands removed (none present). ✓
- AgentGate memory integration passes with a real AgentDB dependency. ✓
- No regression in unrelated approval or context-efficiency behavior. ✓
