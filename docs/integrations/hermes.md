# Hermes Integration

**`src/agentdb/adapters/hermes.py` is implemented** (2026-07-14, commits 4ba16eb..6940fa7).
`AgentDBHermesProvider` subclasses both `MemoryProvider` and `GatewayAdapter`. All 14
hooks are mapped; Principle 12a compliance proven via `HermesMemoryManagerShim` tests.
This document now serves as the reference guide for the shipped adapter — hook names and
lifecycle method signatures are verified against `agent/memory_provider.py`.

## Why this adapter, and why generalized

Hermes is the closest platform to a purpose-built AgentDB integration point: it is
Python-native, ships a formal `MemoryProvider` abstract base class, and enforces a
one-external-provider limit that maps directly onto AgentDB's single-instance model.
The ABC's 14 lifecycle hooks cover every stage AgentDB needs to govern — from
session initialization through context compaction to cross-session outcome recording.

The adapter must be built generically against the `MemoryProvider` ABC, not hardcoded
to a specific Hermes profile or platform. One adapter class, parameterized by
`agent_id` (Hermes profile name) and `session_id` (Hermes session/thread key), usable
by any agent running under Hermes — the same pattern as
`AgentDBClaudeRuntime.from_sdk_client(agent_id=..., ...)` today.

## Integration shape

Unlike OpenClaw (which requires a language bridge), Hermes is Python. The integration
is a **direct in-process import** — no sidecar, no subprocess, no HTTP. AgentDB runs
in the same process as Hermes.

```python
# agentdb/adapters/hermes.py (target shape)
from agentdb import AgentDB
from agent.memory_provider import MemoryProvider

class AgentDBHermesProvider(MemoryProvider):
    def __init__(self, db: AgentDB, agent_id: str, profile: str = "budget_optimized"):
        self._db = db
        self._agent_id = agent_id
        self._profile = profile
        self._session_id: str = ""

    # implement all required hooks below
```

Hermes's `MemoryManager` enforces exactly one external provider at a time — attempting
to register a second is rejected with a warning. This is intentional: it prevents tool
schema bloat and conflicting backends. `AgentDBHermesProvider` is that one provider.

## Governance approach: wrapper, not replacement

Hermes ships its own built-in memory system (FTS5 session search, MEMORY.md bootstrap,
daily working notes). AgentDB does not replace this — it wraps it: enforcing trust
zones on what gets written, adding provenance and conflict detection on top of what
gets retrieved, and injecting governed skills alongside Hermes's own context. The
built-in memory format stays intact.

This is integration shape **(C)** from the codebase assessment
(`docs/integrations/openclaw-hermes-assessment.md`).

## Hook mapping

Full mapping from Hermes's `MemoryProvider` ABC to AgentDB's API surface. All hook
names are verified against `agent/memory_provider.py`.

| Hermes hook | When called | AgentDB call | Type |
|---|---|---|---|
| `initialize(session_id, **kwargs)` | Once at agent startup | `AgentDB.__init__()` (if not pre-constructed); store `session_id`, register reconciliation handlers | Setup |
| `system_prompt_block()` | During system prompt assembly | `db.retrieve(query="", agent_id=...).skill_warnings` + inject governed skill content | 12a |
| `prefetch(query, *, session_id)` | Before each API call | `db.retrieve(query, agent_id, session_id)` — returns governed context for the turn | 12a |
| `queue_prefetch(query, *, session_id)` | After each turn — pre-warms next turn | No-op or background `db.retrieve()` to stamp `last_retrieved`; see Open questions | 12a |
| `sync_turn(user, asst, *, session_id)` | After each turn completes | `db.write(key=..., value=asst, origin="agent_inferred", agent_id=..., session_id=...)` | 12a |
| `get_tool_schemas()` | Tool schema assembly | Expose `memory_search` / `memory_get` equivalents backed by `db.retrieve()` / `db.read()` | 12a |
| `handle_tool_call(name, args, *, session_id)` | Model calls a memory tool | Route to `db.read()` / `db.retrieve()` / `db.write()` with permission enforcement | 12a |
| `shutdown()` | Clean exit | Flush any in-flight writes; no persistent state to close (SQLite closes with the process) | Cleanup |
| `on_turn_start(turn, message, **kwargs)` | Per-turn tick with runtime context | Pre-turn governance check; dispatch `conversation_started` reconciliation trigger | 12a |
| `on_session_end(messages)` | End-of-session extraction | `db.record_outcome(session_id, agent_id, outcome_type, outcome_value)` + archive working memory | 12b |
| `on_session_switch(new_session_id, **kwargs)` | Mid-process session rotation | Re-scope `_session_id`; carry forward in-memory session→record linkage | 12a |
| `on_pre_compress(messages) -> str` | Before context compression | Call configured summarizer callable if set; write summary as `memory/episodic/` record; return summary string | 12b |
| `on_memory_write(action, target, content, metadata)` | Mirrors every built-in memory write | Run `PermissionRouter.check_write()`; write governed record if allowed; emit `memory_write` audit event | 12a (write-time governance) |
| `on_delegation(task, result, **kwargs)` | Parent sees subagent task outcome | Write subagent result as `agent_inferred` record with entity tagging; see Open questions | 12b |

**Build order within the adapter:** start with `initialize`, `prefetch`, `sync_turn`,
and `on_turn_start` — these four cover the core governed memory loop end-to-end and
produce a Principle-12a-compliant proof. Add the remaining hooks incrementally.

## Session and agent key mapping

Hermes already tracks agents by profile name and sessions by session/thread key.
These map directly onto AgentDB's existing parameter names — no new key scheme needed.

| Hermes concept | AgentDB parameter |
|---|---|
| Profile name (e.g. `"coder"`, `"assistant"`) | `agent_id` |
| Session / thread key | `session_id` |
| Platform message ID | `message_id` (where available via `kwargs`) |
| `agent_context == "subagent"` | Use `parent_session_id` from `kwargs` to scope writes |

The `initialize()` kwargs always include `hermes_home`, `platform`, and optionally
`agent_context`, `agent_identity`, `agent_workspace`, `parent_session_id`, `user_id`.
Use `agent_identity` as `agent_id` if set; fall back to the profile name.

## Skill coexistence

Hermes has its own skill system — autonomous skill creation after complex tasks,
self-improvement during use, and [agentskills.io](https://agentskills.io) format
compatibility. AgentDB's skills are governance-grade: versioned records, computed
health scores, human-approved origin, staleness warnings.

These are not the same thing and should not be merged:

- **Hermes skills** are agent-authored, live in the agent's workspace, and represent
  learned procedural knowledge. They are `agent_inferred` from AgentDB's perspective.
- **AgentDB skills** are human-approved instruction records with computed health scores.
  They surface via `system_prompt_block()` as governed guidance alongside (not
  replacing) Hermes's own skill injection.

Concretely: `system_prompt_block()` should inject AgentDB governed skill content in a
clearly labelled section. Hermes's own skill injection continues independently. No
AgentDB code path should touch Hermes's skill files, and no Hermes skill creation path
should write directly to `skills.sqlite`.

## Dreaming and lifecycle sweep coexistence

Both Hermes's dreaming pass (promotes short-term notes to MEMORY.md based on recall
frequency + query diversity) and AgentDB's lifecycle sweep (`db.lifecycle.run_sweep()`,
archival-only, on-demand) are **optional and independently user-configured**.

When both are enabled:
- Dreaming governs Hermes's own markdown files — it decides what promotes to MEMORY.md.
- AgentDB's sweep governs AgentDB's own `memories` table — it archives stale
  `agent_inferred` records below the confidence/staleness threshold.
- They do not interact directly. A note promoted by dreaming may or may not also exist
  as an AgentDB memory record; that is governed by whether `on_memory_write` fired for
  it.

The user decides which runs, via Hermes's dreaming config and AgentDB's `memory.yaml`
lifecycle settings. Neither is a prerequisite for the other.

## Context compaction and summarization

`on_pre_compress(messages)` is the cleanest attachment point for AgentDB's summarizer
callable. When called:

1. If `AgentDB` was constructed with a `summarizer` callable — call it with the
   messages to get a compressed summary string.
2. Write the summary as a `memory/episodic/hermes/{agent_id}/{session_id}/compaction-{ts}`
   record with `origin="agent_inferred"` and a modest confidence boost (it is derived
   from real conversation, not pure inference).
3. Return the summary string to Hermes, which uses it as the compaction output.
4. If no summarizer is configured — return `""` and let Hermes handle compaction
   natively. Do not error.

The summarizer callable is always optional:
```python
db = AgentDB(summarizer=None)       # default — truncation fallback, Hermes handles it
db = AgentDB(summarizer=my_llm_fn)  # Hermes calls on_pre_compress → summarizer → governed record
```

## Conflict notification

`AgentDBHermesProvider.initialize()` subscribes `_on_conflict` via `db.gateway.subscribe`:

```python
def initialize(self, session_id: str, **kwargs) -> None:
    self._session_id = session_id
    self.gateway.subscribe("conflict_detected", self._on_conflict)
    self.gateway.dispatch("conversation_started", session_id=session_id)

def _on_conflict(self, **payload) -> None:
    # Route to Hermes's own notification surface (Telegram, Slack, Discord, etc.)
    # The handler is platform-agnostic — Hermes's gateway routes it.
    ...
```

The same `conflict_detected` trigger that notifies via slack-assistant's
Slack/Telegram channels also fires inside any Hermes agent that registers this handler —
one dispatch mechanism, multiple consumers, no new plumbing. Note: `db.gateway.subscribe`/`dispatch`
is the canonical API; `register_reconciliation_handler`/`dispatch_reconciliation_trigger`
are pass-through aliases that still work but are deprecated.

## Principle 12 compliance bar

Not done when `AgentDBHermesProvider` *can* call `db.retrieve` / `db.write` — done when
a test proves Hermes's own `MemoryManager` triggers the hook, and that the hook changes
the context or governed content the agent sees at that checkpoint.

Each hook must be labelled:
- **12a** (real-time, synchronous, wired into Hermes's own turn flow) — `prefetch`,
  `sync_turn`, `on_turn_start`, `on_memory_write`, `handle_tool_call`, `system_prompt_block`
- **12b** (async, decoupled from the current turn) — `on_session_end`, `on_pre_compress`,
  `on_delegation`, reconciliation handler registration

A test must exist for each 12a hook proving the framework's own execution path — not
direct adapter calls from test code — triggers AgentDB governance.

## Known limitations and follow-up items

These were open questions before implementation. They are documented here as known
limitations of the shipped adapter, not pre-build decisions.

1. **`queue_prefetch` is a no-op.** AgentDB retrieval is always a live query (nothing
   cached), so a background call would usefully stamp `last_retrieved` but the current
   implementation skips it. A future improvement could run a background retrieve to
   improve the causal ranking signal.

2. **`on_memory_write` governance is scoped to `MEMORY.md` and `DREAMS.md` targets.**
   Daily-note appends are skipped — running the permission router on every high-frequency
   ephemeral append was not worth the overhead. Expand scope if a governed key prefix
   needs to cover daily notes.

3. **`on_delegation` writes the result as an `agent_inferred` episodic record** tagged
   with the task's entity set. If the result is very large or low-signal, consider
   gating on a confidence threshold before writing.

4. **`system_prompt_block()` injects a `## Governed Skills` section.** Verify this
   header does not conflict with any Hermes-reserved section header in the target profile.

5. **`on_memory_write` cannot veto Hermes-native writes.** The hook fires after the
   write has already landed in Hermes's own store — it is a 12a governance point for
   AgentDB's own records only. Callers must not assume AgentDB can block a Hermes-native
   write.

## Related context

- Existing adapter to mirror: `src/agentdb/adapters/claude_agent_sdk.py`,
  `docs/integrations/model-and-runtime-wiring.md` section A.1.
- Hermes `MemoryProvider` ABC: `agent/memory_provider.py` in
  [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
- Hermes `MemoryManager` (wiring point in `run_agent.py`): `agent/memory_manager.py`
- Cross-platform comparison (build rationale):
  `docs/integrations/openclaw-hermes-assessment.md`
