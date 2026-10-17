# Model-Agnostic Runtime Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:writing-plans to implement this spec.

**Goal:** Make agentdb's middleware and injection layer work cleanly for all runner tiers (Claude, Fireworks, local Ollama/MLX) without breaking existing Claude integration.

**Architecture:** Introduce a capability declaration system so runners self-describe what they can do; the middleware adapts injection depth accordingly. For low-capability runners, everything the model needs is injected at turn start. Session state is tracked in-process and referenced in the JSONL transcript for restart/debug continuity.

**Tech Stack:** Python, agentdb (SQLite + optional embeddings), agentgate (asyncio runners), JSONL session files.

**Repos touched:**
- `agentdb` — middleware rename, capability system, enhanced injection, directory state API, session summary write
- `agentgate` — runner capability declarations, post-turn directory reporting, session-end hook

---

## Goals

- Skills, workspace snapshot, and work context injected into system prompt for runners that can't call tools mid-turn
- Directory state tracked per-session (temp), elevatable to permanent agentdb records
- End-of-session JSONL summary block enables warm restart and troubleshooting
- Claude runner continues to work without modification
- Fireworks runner gets skills injection; directory and task state as appropriate for its capability level
- No new required dependencies

## Non-Goals

- Multi-step task decomposition or planning within a single turn (out of scope for this design)
- Real-time tool call injection for local models (they remain single-shot)
- Changing Claude's mid-turn retrieval behavior
- Replacing or restructuring the JSONL session format (summary is appended, not replacing)

## Anti-Goals

- Injecting so much context that local model performance degrades (injection must be budgeted)
- Making agentdb aware of specific runner implementations (agentdb stays runner-agnostic; runners declare capabilities)

---

## Global Constraints

- Backward compatibility: `AgentDBClaudeMiddleware` must remain importable as an alias throughout
- No new required pip dependencies (embeddings remain optional)
- Injection budget: skill + workspace + work context combined must respect `budget_tokens` from profile config (default 4096 for injection overhead)
- Directory state records use `origin="agent_inferred"` and `confidence=0.3` (temp); elevated records use caller-supplied origin/confidence
- JSONL summary line format: single JSON object with `"type": "session_summary"` key — appended, never replacing existing lines

---

## Design

### 1. Capability Tier System

Runners declare their capabilities when calling `init_agentdb()`. The middleware uses these flags to decide what to inject.

**`RunnerCapabilities` dataclass (agentdb/adapters/capabilities.py — new file):**

```python
@dataclass
class RunnerCapabilities:
    can_call_tools_mid_turn: bool = False    # Claude: True; local: False
    can_write_mid_turn: bool = False         # Claude: True; local: False
    has_native_memory: bool = False          # Fireworks: True; others: False
    inject_skills: bool = True              # All: True by default
    inject_workspace: bool = True           # All: True by default
    inject_work_context: bool = True        # All: True by default
    max_injection_tokens: int = 4096        # Budget for all injected blocks
```

**Predefined tier constants:**

```python
CLAUDE_CAPABILITIES = RunnerCapabilities(
    can_call_tools_mid_turn=True,
    can_write_mid_turn=True,
    has_native_memory=False,
    inject_skills=False,     # Claude retrieves skills via tools
    inject_workspace=False,  # Claude navigates via tools
    inject_work_context=False,
    max_injection_tokens=0,
)

FIREWORKS_CAPABILITIES = RunnerCapabilities(
    can_call_tools_mid_turn=False,   # model-dependent, conservative default
    can_write_mid_turn=False,
    has_native_memory=True,
    inject_skills=True,
    inject_workspace=True,
    inject_work_context=False,       # Fireworks has partial native context
    max_injection_tokens=2048,
)

LOCAL_CAPABILITIES = RunnerCapabilities(
    can_call_tools_mid_turn=False,
    can_write_mid_turn=False,
    has_native_memory=False,
    inject_skills=True,
    inject_workspace=True,
    inject_work_context=True,
    max_injection_tokens=4096,
)
```

Runners pass capabilities to `init_agentdb()`:

```python
def init_agentdb(self, db, platform: PlatformConfig, capabilities: RunnerCapabilities = LOCAL_CAPABILITIES) -> None:
    ...
    self._agentdb_middleware = AgentDBMiddleware(db=db, agent_id=platform.agent_id, capabilities=capabilities)
```

---

### 2. Middleware Rename

**Rename:** `AgentDBClaudeMiddleware` → `AgentDBMiddleware`

**Location:** `agentdb/adapters/middleware.py` (rename from `claude_agent_sdk.py`)

**Backward compat:** `agentdb/adapters/claude_agent_sdk.py` becomes a one-liner shim:

```python
from agentdb.adapters.middleware import AgentDBMiddleware as AgentDBClaudeMiddleware  # noqa: F401
```

`AgentDBMiddleware.__init__` gains a `capabilities: RunnerCapabilities = LOCAL_CAPABILITIES` parameter. When `capabilities` is not passed (existing callers), it defaults to `LOCAL_CAPABILITIES` — injecting everything. Claude runner explicitly passes `CLAUDE_CAPABILITIES` to restore current behavior.

---

### 3. Enhanced Injection in `prepare_turn_async`

`prepare_turn_async` currently builds a messages list with memory blocks. It gains three new optional blocks, each guarded by the capability flag and token budget.

**Injection order in system prompt (each block is a `## Heading` section):**

1. `## Memory from previous sessions` (existing, always injected)
2. `## Skills` (new — if `capabilities.inject_skills`)
3. `## Workspace` (new — if `capabilities.inject_workspace`)
4. `## Current work context` (new — if `capabilities.inject_work_context`)
5. `## Relevant memory for this turn` (existing per-turn block)

**Skills injection (`_build_skills_block`):**

```python
async def _build_skills_block(self, session_id: str) -> str:
    skills = await self._db.skills.get_for_agent(self._agent_id)
    # Filter: health_score >= 0.5, not archived, sorted by health_score desc
    # Format: "### {skill.name}\n{skill.content}"
    # Budget: trim to token budget proportional share
```

**Workspace injection (`_build_workspace_block`):**

```python
async def _build_workspace_block(self, session_id: str, working_dirs: list[str]) -> str:
    # 1. List top-level entries for each working_dir (max 2 levels deep, hidden dirs excluded)
    # 2. Append git branch + status if working_dir is a git repo (git rev-parse + git status --short)
    # 3. Append temp directory state for this session (from _session_dir_state[session_id])
    # Format: path, branch, status lines, temp dirs labeled "(this session)"
```

**Work context injection (`_build_work_context_block`):**

```python
async def _build_work_context_block(self, session_id: str) -> str:
    # Reads self._session_work_log[session_id]: list of TurnSummary
    # TurnSummary: {turn_num, files_read, files_written, dirs_visited, key_facts}
    # Returns last N turns (default 5) as a compact summary
    # "Turn 3: read README.md, pyproject.toml. Wrote agentdb/adapters/middleware.py."
```

**Token budget enforcement:**
All three blocks are built, then trimmed proportionally if their combined token estimate exceeds `capabilities.max_injection_tokens`. Skills trimmed first (health-score order), then workspace (deepest dirs first), then work context (oldest turns first).

**`_build_workspace_block` receives `working_dirs` as a direct argument** (passed through from `prepare_turn_async`'s own `working_dirs` param — not looked up from session state). This avoids ordering issues when the session state hasn't been set yet for the current call.

**`get_for_agent` is synchronous** — call it directly inside the async methods (no executor). This matches the existing codebase pattern throughout `AgentDBClaudeMiddleware`.

**`record_turn_context` signature** (defined on `AgentDBMiddleware`, called by agentgate post-turn):

```python
def record_turn_context(
    self,
    session_id: str,
    turn_num: int,
    files_read: list[str],
    files_written: list[str],
    key_fact: str,
) -> None:
    # Appends a TurnSummary to self._session_work_log[session_id]
    # Caps list at 20 entries (drop oldest) to bound memory
```

**`sync prepare_turn` stays a no-op** — injection only happens in `prepare_turn_async`. The sync path is not used by any runner; it remains for interface compatibility only.

---

### 4. Directory State: Temp → Permanent

**In-process session state** (no DB writes for temp):

```python
# In AgentDBMiddleware:
_session_dir_state: dict[str, list[DirectoryRecord]] = {}

@dataclass
class DirectoryRecord:
    path: str
    first_seen_turn: int
    last_seen_turn: int
    access_count: int
    elevated: bool = False
```

**Reporting new directories** — runners call this post-turn:

```python
async def report_directories(self, session_id: str, dirs: list[str]) -> None:
    # Upserts into _session_dir_state[session_id]
    # Does NOT write to agentdb — stays temp
```

**Elevation** — explicit call to make a directory permanent:

```python
async def elevate_directory(
    self,
    session_id: str,
    path: str,
    origin: str = "human_approved",
    confidence: float = 0.8,
) -> MemoryRecord:
    # Calls db.write() with key=f"workspace.dir.{path}", value=path
    # Marks DirectoryRecord.elevated = True in session state
    # Returns MemoryRecord
```

Agentgate can expose elevation via a bot command (e.g., `/remember dir`) or the runner can auto-elevate directories visited more than N times in a session (configurable threshold, default: 3).

---

### 5. Session JSONL Summary

At session end (teardown or idle timeout), the runner calls:

```python
async def write_session_summary(self, session_id: str, jsonl_path: str) -> None:
```

This writes a single line to the JSONL file:

```json
{
  "type": "session_summary",
  "session_id": "abc123",
  "timestamp": "2026-10-17T14:32:00Z",
  "agent_id": "surfer",
  "agentdb_checkpoint_key": "memory/compaction/checkpoint/abc123",
  "working_dirs": ["/path/to/workspace"],
  "temp_dirs": ["/path/to/workspace/new-feature"],
  "elevated_dirs": [],
  "turn_count": 12,
  "skills_used": ["git-workflow", "dbt-compile"],
  "last_work_context": "Working on agentdb middleware refactor. Last file written: agentdb/adapters/middleware.py."
}
```

`agentdb_checkpoint_key` uses the actual agentdb key format (`memory/compaction/checkpoint/<session_id>`) so it's directly queryable via `db.read(key)`.

**On warm restart:** the runner reads the last `session_summary` line from the prior session's JSONL and passes its `last_work_context` string as an entry in `external_context` to `prepare_turn_async`. The existing `external_context` parameter already handles injecting supplemental text alongside retrieved memory — no signature change needed. The runner may also call `db.read(agentdb_checkpoint_key)` to recover the full checkpoint record if available.

**JSONL path convention:** agentgate already stores sessions at `agent-sessions/<platform>/<bot_name>/<user_id>/`. The summary line is appended to the existing transcript file — no new file, no format change.

---

### 6. `end_session` Facade

New method on `AgentDBMiddleware` that runners call on teardown:

```python
async def end_session(
    self,
    session_id: str,
    jsonl_path: str,
    summary: str | None = None,
    run_consolidation: bool = False,
    llm_fn: Callable | None = None,
) -> None:
    # 1. db.checkpoint(agent_id=self._agent_id, session_id=session_id)
    # 2. Optionally: db.consolidate(agent_id=self._agent_id, llm_fn=llm_fn) if run_consolidation
    # 3. write_session_summary(session_id, jsonl_path)
    # 4. Clear _session_dir_state[session_id] and _session_work_log[session_id]
```

---

### 7. Post-Turn Work Log (agentgate side)

After each model response, the local runner parses the response text to extract:
- Files mentioned (heuristic: paths matching `/` or relative paths with extensions)
- Directories visited (from reported dirs + bash output parsing)
- A short fact: first sentence of the response, truncated to 120 chars

This is intentionally heuristic and cheap — not LLM-assisted. Accuracy matters less than coverage.

```python
async def _record_turn(self, session_id: str, prompt: str, response: str, turn_num: int) -> None:
    dirs = _extract_dirs(response)  # regex: absolute paths + relative paths
    files = _extract_files(response)
    await self._agentdb_middleware.report_directories(session_id, dirs)
    await self._agentdb_middleware.record_turn_context(
        session_id=session_id,
        turn_num=turn_num,
        files_read=files,
        key_fact=response[:120].split(".")[0],
    )
```

---

## What's Left to the Builder

- Exact token counting method (character-based estimate is acceptable: `len(text) / 4`)
- Skill injection format (markdown fenced blocks vs plain text) — builder's call, should be readable
- Workspace snapshot depth and exclusion patterns (`.git`, `__pycache__`, `node_modules`, `dist`, `.venv`) — builder adds reasonable defaults
- Git status parsing — use subprocess, swallow errors if not a git repo
- `_extract_dirs` / `_extract_files` regex patterns — builder writes reasonable heuristics
- Auto-elevation threshold (dirs visited ≥ 3 times) — exposed as `auto_elevate_threshold: int = 3` in `RunnerCapabilities`
- Agentgate: which session JSONL file path to pass to `end_session` — follow existing `agent-sessions/` path convention

---

## Review Focus

1. **Token budget overflow** — if skills + workspace + work context + existing memory all inject at max, total system prompt could exceed local model context window. Budget enforcement must be tested with a realistic injection scenario, not just unit-tested in isolation.
2. **Claude regression** — `CLAUDE_CAPABILITIES` disables all new injection; ensure existing Claude runner tests still pass with the middleware rename and the new default parameter.
3. **Temp dir state memory leak** — `_session_dir_state` grows unboundedly within a long session. `end_session` clears it, but a crashed runner won't call it. Needs a max-entries cap per session (e.g., 200 dirs).
4. **JSONL append atomicity** — appending a line to an existing file that another process might be writing to. Simple `open(path, "a")` is sufficient for single-process runners, but worth noting the assumption.
5. **Work context heuristic quality** — the file/dir extraction regex will produce false positives (e.g., `x/y` in math expressions). The builder should weight toward recall over precision here; a few spurious entries are less harmful than missing real ones.
