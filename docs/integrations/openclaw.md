# OpenClaw Integration (Planned — Not Yet Implemented)

**`src/agentdb/adapters/openclaw.py` does not exist yet.** This is a design brief for
whoever builds it next, not something importable or runnable today. Nothing below has
been verified against OpenClaw's actual plugin SDK source — treat the "needs
confirmation" markers below as blocking questions to answer before writing code, not as
settled API surface.

## Why this adapter, and why generalized

The immediate driver is `data-helper` (an OpenClaw agent under MetricMaven/MavenData),
but this must be built generically against OpenClaw's agent/plugin model, not hardcoded
to `data-helper` — one plugin, parameterized by `agent_id`, usable by any OpenClaw agent
(`data-helper`, `maven-data`, `maven-delivery`, `taghelper`, future ones) the same way
`AgentDBClaudeRuntime.from_sdk_client(agent_id=..., ...)` is parameterized per caller
today.

## Two integration shapes — pick one, don't build both (Principle 1a)

OpenClaw already ships its own memory system ("memory-core": per-agent SQLite,
`MEMORY.md` + `memory/*.md` indexed with hybrid FTS5/BM25 + embeddings retrieval,
exposed to agents via `memory_search`/`memory_get`). AgentDB does not enter an empty
slot — it enters next to an existing memory system, and there are two structurally
different ways to attach:

**A) Backend replacement/supplement.** OpenClaw's memory-core already documents
"alternative backend/plugin paths" (LanceDB, QMD-oriented options) as a supported
extension point. Register `AgentDBBackend` as one of those alternative backends, making
AgentDB the memory of record for the agents that opt in. Needs a migration/backfill
story from memory-core's existing SQLite index — don't silently orphan existing memory.

**B) Tool-call governance layer.** Leave memory-core in place; hook AgentDB's
`retrieve`/`write`/`record_outcome` into OpenClaw's plugin lifecycle
(`before_tool_call`, result middleware), mirroring how the Claude Agent SDK adapter
attaches to `PreToolUse`. Additive, not a replacement — but risks two systems both
indexing/governing the same `MEMORY.md`/`memory/*.md` content with no arbitration
between them if scope isn't kept explicitly separate.

These are not equivalent and the choice changes almost everything downstream (what gets
migrated, what a passing test looks like, what "done" means for Principle 12). Resolve
this before writing code.

## The language boundary this adapter must cross

AgentDB is a Python package (`from agentdb import AgentDB`); OpenClaw is a Node/TypeScript
service with its own plugin SDK (`openclaw/plugin-sdk`, `openclaw/plugin-sdk/tool-plugin`,
`openclaw/extension-api` — `api.registerTool(...)`/`defineTool(...)`/
`defineToolPlugin(...)`). Unlike the existing Claude Agent SDK adapter (same-language,
in-process, direct import), there is no shared runtime here — the OpenClaw side needs a
thin Node plugin that calls out to Python across a process boundary. Two shapes:

1. **Local sidecar.** A small Python process runs one `AgentDB` instance behind a local
   HTTP/RPC surface — analogous to how OpenClaw's own Gateway already binds a local port
   (`127.0.0.1:18789`) for its control plane. The OpenClaw plugin is a thin HTTP client.
   Better latency, more moving parts to operate.
2. **Subprocess-per-call.** The plugin shells out to a short-lived Python invocation per
   hook firing. Simpler to stand up, but `before_tool_call` fires inline in the tool-call
   path — a process spawn on every tool call is a real latency cost, likely incompatible
   with the `budget_optimized` profile's latency/cost bar.

(1) is the more likely correct answer given Principle 12a wants enforcement wired into
the framework's own control flow without visibly taxing it, but confirm — check
`docs.openclaw.ai/plugins/building-plugins` for whether any existing OpenClaw plugin
already talks to a local sidecar process; precedent would settle this fast.

## OpenClaw hook points this maps onto

Mapped from the existing four-hook contract in `src/agentdb/adapters/claude_agent_sdk.py`:

| AgentDB hook (Claude SDK adapter) | OpenClaw equivalent | Confidence |
|---|---|---|
| pre-response hook → `db.retrieve(...)` | A turn-start lifecycle hook, firing before the Gateway assembles system/runtime instructions + tool policy for the turn | **Needs confirmation** — exact hook name unverified; check `docs.openclaw.ai/plugins/sdk-entrypoints` |
| post-response hook → `db.write(...)` | "Result middleware" (the plugin SDK is documented to expose this concept) | **Needs confirmation** of exact API shape |
| post-outcome hook → `db.record_outcome(...)` | No native OpenClaw signal identified — caller-supplied, same as today's adapter (`outcome_type`/`outcome_value` passed explicitly by whatever triggers the turn) | Confirmed pattern, but it's the existing adapter's pattern, not something OpenClaw provides |
| `PreToolUse` hook → `retrieve_by_entities(...)` | `before_tool_call` hook (confirmed to exist; runs inside OpenClaw's deny-wins, fail-closed tool-policy pipeline) | **High** — direct match, build this first |

Start with `before_tool_call` — it's the clearest 1:1 match to the existing
`PreToolUse` pattern and the fastest path to a Principle-12a-compliant proof: a test
showing OpenClaw's own hook mechanism triggers retrieval and changes what the model
sees or is allowed to do at that checkpoint.

## Session/agent key mapping

OpenClaw's Gateway already routes conversation state "by agent and session/thread
key." This maps directly onto AgentDB's existing `agent_id` + `session_id` +
`message_id` parameters (see the Claude SDK adapter and
`examples/slack_assistant_agentdb_runtime.py`) — no new key scheme needed. Use
OpenClaw's own agent name (`data-helper`, `maven-data`, ...) as `agent_id` and its
session/thread key as `session_id`.

## Principle 12 compliance bar

Not done when the plugin *can* call `db.retrieve`/`db.write` — done when a test proves
OpenClaw's own hook mechanism (`before_tool_call`, or whichever turn-lifecycle hook gets
confirmed) actually fires it, changing the context or tool availability the agent sees
at that checkpoint. State explicitly which pieces are 12a (real-time) vs. 12b (async),
same as every other adapter in this repo.

## Open questions, in priority order

1. Backend-replacement (A) vs. governance-layer (B) — pick one.
2. Exact turn-lifecycle hook name(s) for pre-response/post-response in the OpenClaw
   plugin SDK — confirm via `docs.openclaw.ai/plugins/{sdk-overview,sdk-entrypoints,
   building-plugins}`; don't guess at API names.
3. Sidecar vs. subprocess-per-call for the Python/Node bridge, and its latency cost
   inside `before_tool_call`'s inline path.
4. If (A): how to avoid duplicate governance/indexing against OpenClaw's existing
   memory-core (FTS5/BM25 + embeddings hybrid) — replace it per opted-in agent, or scope
   AgentDB to a namespace that doesn't overlap `MEMORY.md`/`memory/*.md`.

## Related context

- Existing adapter to mirror: `src/agentdb/adapters/claude_agent_sdk.py`,
  `docs/integrations/model-and-runtime-wiring.md` section A.1.
- OpenClaw architecture (Gateway, plugin SDK, tool-policy pipeline, memory-core) is
  documented in the `agent-platform-profile` repo's `.openclaw/README.md`
  (`MavenData/agent-platform-profile`) — read that first, it's the source for
  everything OpenClaw-specific in this doc.
