# OpenClaw & Hermes — Codebase Assessment

**Purpose:** Cross-platform memory assessment to inform AgentDB adapter design for both
OpenClaw and Hermes. Covers memory architecture, unique capabilities, overlapping
features (and which ones are user-configurable), and the recommended integration
strategy. Written before either adapter exists — treat as the design brief that precedes
`openclaw.md`'s open-question resolution and the not-yet-written `hermes.md`.

---

## OpenClaw

**Repo:** [openclaw/openclaw](https://github.com/openclaw/openclaw)
**Language:** TypeScript / Node
**Plugin SDK:** `src/plugin-sdk/*`, `api.registerTool` / `defineTool` / `defineToolPlugin`

### Memory Architecture

OpenClaw's memory is **file-first**: the agent workspace is a set of Markdown files the
model can read, write, and reference directly. There is no hidden state — the model only
remembers what lands on disk.

**Three file types compose the memory structure:**

| File | Role | Lifetime |
|---|---|---|
| `MEMORY.md` | Persistent knowledge base — durable facts, preferences, decisions | Permanent; bootstrapped at every session start |
| `memory/YYYY-MM-DD.md` | Daily working notes — session context, observations | Today's and yesterday's auto-load on `/new` or `/reset` |
| `DREAMS.md` | Consolidation summaries and dream diary for human review | Written by the dreaming pass; optional |

**Storage backend:** SQLite with hybrid FTS5/BM25 keyword + vector search.
No external dependencies by default. Alternative backends are documented and supported:
LanceDB (OpenAI-compatible embeddings), QMD (local-first sidecar), Honcho
(AI-native cross-session), and community plugins (e.g. Mem0 REST backend).

**Retrieval tools exposed to the model:**
- `memory_search` — semantic + keyword hybrid search across all notes
- `memory_get` — direct file or line-range retrieval

**Embedding providers:** configurable — OpenAI (default), Gemini, Voyage, Mistral,
Bedrock, GGUF (local), Ollama.

### Lifecycle Features

**Dreaming (optional):** A background pass that promotes qualified short-term notes from
daily files into `MEMORY.md` based on recall frequency and query diversity thresholds.
Analogous to sleep consolidation — observations that surface repeatedly across different
queries earn promotion to long-term memory. Opt-in; configurable thresholds.

**Pre-compaction flush:** Before context window compression, the system reminds the agent
to save important context to disk. Ensures working memory survives compaction rather than
disappearing into the compressed summary.

**Bootstrap truncation handling:** If `MEMORY.md` exceeds the bootstrap token budget, the
full file stays on disk but is truncated in context. Resolution options: move detail to
daily files, keep only summaries in long-term memory, or increase bootstrap limits.

### Plugin / Extension Model

The memory backend is replaceable via a plugin slot (`kind: "memory"`). A plugin
registered in this slot fully replaces memory-core for the agents that opt in. The
platform ships with:
- `memory-wiki` — provenance-rich wiki vault alongside the active memory engine
- Community: `openclaw-memory-mem0`, `openclaw-honcho`, `openclaw-engram`

### Language Boundary

OpenClaw is TypeScript/Node. AgentDB is Python. There is no shared runtime — any
AgentDB integration crosses a process boundary. Two viable shapes:

1. **Local sidecar** — a small Python process runs one `AgentDB` instance behind a
   local HTTP/RPC port (analogous to OpenClaw's own Gateway at `127.0.0.1:18789`).
   The OpenClaw plugin is a thin HTTP client. Better latency; more moving parts.
2. **Subprocess-per-call** — the plugin shells out to a short-lived Python invocation
   per hook. Simpler to stand up; incompatible with low-latency inline hooks.

The sidecar is the correct shape for Principle 12a compliance (real-time enforcement
without visibly taxing the tool-call path).

---

## Hermes

**Repo:** [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
**Language:** Python
**Memory interface:** `agent/memory_provider.py` — formal `MemoryProvider` ABC
**Memory orchestrator:** `agent/memory_manager.py` — single integration point in `run_agent.py`

### Memory Architecture

Hermes is **provider-agnostic**: memory is whatever `MemoryProvider` returns. The built-in
provider handles default behaviour; an external plugin fully replaces it. The
`MemoryManager` enforces a one-external-provider limit to prevent tool schema bloat and
conflicting backends.

**`MemoryProvider` ABC — full lifecycle hook surface:**

| Hook | When called | AgentDB mapping |
|---|---|---|
| `initialize(session_id, **kwargs)` | Once at agent startup | `AgentDB.__init__()` + session setup |
| `system_prompt_block()` | During system prompt assembly | Skill injection — governed instruction records |
| `prefetch(query, *, session_id)` | Before each API call | `db.retrieve(query, agent_id, session_id)` |
| `queue_prefetch(query, *, session_id)` | After each turn — pre-loads next turn's context | Background prefetch of next retrieval |
| `sync_turn(user, asst, *, session_id)` | After each turn completes | `db.write()` + `finalize_turn()` equivalent |
| `get_tool_schemas()` | Tool schema assembly | Expose `memory_search` / `memory_get` equivalents |
| `handle_tool_call(name, args, *, session_id)` | Model calls a memory tool | Route to `db.read()` / `db.retrieve()` / `db.write()` |
| `shutdown()` | Clean exit | Flush in-flight writes, close connections |
| `on_turn_start(turn, message, **kwargs)` | Per-turn tick with runtime context | Pre-turn governance check |
| `on_session_end(messages)` | End-of-session extraction | `db.record_outcome()` + archive working memory |
| `on_session_switch(new_session_id, **kwargs)` | Mid-process session rotation | Re-scope session tracking |
| `on_pre_compress(messages) -> str` | Before context compression | Compaction hook → `checkpoint()` / summarizer callable |
| `on_memory_write(action, target, content, metadata)` | Mirrors built-in memory writes | Write-time permission router — governance at the source |
| `on_delegation(task, result, **kwargs)` | Parent-side observation of subagent work | Multi-agent memory boundary tracking |
| `backup_paths() -> list[str]` | Extra paths for `hermes backup` | Return AgentDB database files |

**Platform support:** Telegram, Discord, Slack, WhatsApp, Signal, CLI — all through a
single gateway process.

**Skills:** Hermes has its own skill system — autonomous skill creation after complex
tasks, self-improvement during use, and compatibility with the
[agentskills.io](https://agentskills.io) open standard. User skills from OpenClaw
migrate automatically to `~/.hermes/skills/openclaw-imports/`, confirming the two
platforms share lineage and skill format.

**FTS5 session search with LLM summarization:** cross-session recall using full-text
search combined with AI-generated summaries. The summarisation model is configurable.

---

## Side-by-Side Comparison

| Dimension | OpenClaw | Hermes |
|---|---|---|
| **Language** | TypeScript / Node | Python |
| **AgentDB integration shape** | Local sidecar (HTTP bridge) | Direct in-process import |
| **Memory format** | Markdown files on disk | Abstract provider — format is the provider's choice |
| **Default backend** | SQLite + FTS5/BM25 + vectors | Built-in + pluggable |
| **Embeddings** | Configurable (OpenAI default, Gemini, Voyage, local) | Via provider plugin |
| **Plugin model** | `kind: "memory"` slot | Formal `MemoryProvider` ABC (11+ hooks) |
| **One external provider** | Yes | Yes (enforced by MemoryManager) |
| **Dreaming / consolidation** | "Dreaming" pass → MEMORY.md | `on_pre_compress` + `on_session_end` hooks |
| **Pre-compaction handling** | Built-in flush reminder | `on_pre_compress()` hook — implementer-controlled |
| **Skills** | Basic | Autonomous creation, self-improvement, agentskills.io |
| **Multi-agent** | Not documented | `on_delegation()` hook (parent sees subagent outcomes) |
| **Platform channels** | Own gateway | Telegram, Slack, Discord, WhatsApp, Signal, CLI |
| **Lineage** | Original platform | Evolved from OpenClaw; skill-compatible |

---

## Overlapping Capabilities (User-Configured)

The following features exist in both platforms but are **opt-in and independently
configurable** by the user — neither platform forces them, and AgentDB's equivalents
should follow the same pattern.

| Feature | OpenClaw config | Hermes config | AgentDB equivalent | Notes |
|---|---|---|---|---|
| **Dreaming / lifecycle promotion** | Dreaming pass enabled/disabled; thresholds configurable | `on_session_end` + `on_pre_compress` hooks; provider opts in by implementing them | `db.lifecycle.run_sweep()` — on-demand only, off by default | All three are optional. User decides whether short-term memory gets promoted. AgentDB's sweep is the governance gate for both platforms if wired. |
| **Embeddings** | Provider-level config (model name, API key) | Provider-level config | `embedding_model` in `memory.yaml`, configurable callable | All three treat the embedding model as a config detail, not a hard dependency. |
| **Skills** | Basic; no versioning or health score | Autonomous creation; agentskills.io format | Versioned records, computed health score, staleness warnings | AgentDB's skills are governance-grade; OpenClaw/Hermes skills are agent-authored. The governance layer sits above, not replacing. |
| **Pre-compaction summarization** | Built-in flush reminder (no summarization) | `on_pre_compress()` returns a string summary | Summarizer callable (`AgentDB(summarizer=fn)`) — None by default (truncation fallback) | The summarizer callable is the shared abstraction: Hermes calls it via `on_pre_compress`, OpenClaw via the sidecar's compaction endpoint. |
| **Cross-session recall** | FTS5 hybrid search across daily files | FTS5 + LLM summarization | Retrieval algorithm (provenance + recency + confidence + causal + workspace_overlap) | AgentDB's retrieval adds governance signals on top of both platforms' own search. workspace_overlap is opt-in (weight 0.0 by default). |

---

## What Each Platform Uniquely Offers

### OpenClaw only

- **Markdown-first, human-readable memory** — `MEMORY.md` is the canonical state.
  Agents read and write it directly. This is load-bearing: the bootstrap budget, the
  dreaming pass, and the pre-compaction flush all assume Markdown as the wire format.
  AgentDB does not replace this — it governs what flows into and out of it.
- **DREAMS.md consolidation diary** — a human-reviewable log of what the dreaming pass
  promoted. No equivalent in Hermes or AgentDB.

### Hermes only

- **Formal `MemoryProvider` ABC** — the most complete plugin contract in either platform.
  Eleven hooks cover every stage from session start to cross-session recall. This is
  exactly the surface AgentDB's adapter needs to implement — no guessing at API names.
- **`on_delegation` hook** — parent agent observes subagent task outcomes. Enables
  multi-agent memory: what a subagent learned in a delegated task can be surfaced to the
  parent's governed memory store. No equivalent in OpenClaw.
- **`on_memory_write` hook** — mirrors every built-in memory write to the external
  provider. This is the natural attachment point for AgentDB's write-time permission
  router — governance fires at the same moment the agent writes, not after.
- **agentskills.io compatibility** — skills follow an open standard, making them portable
  across runtimes that adopt the same format.
- **`queue_prefetch`** — background recall queued after each turn, consumed on the next.
  Maps cleanly onto AgentDB's planned prefetch optimisation without requiring a new API.

---

## AgentDB Integration Strategy

### Governance approach: (C) wrapper, not replacement

For both platforms, AgentDB acts as a **governance wrapper** — not replacing the
platform's own memory backend, but sitting above it to enforce trust zones, track
provenance, detect conflicts, and manage skills. The platform's native format
(Markdown for OpenClaw, provider output for Hermes) stays intact. AgentDB adds:

- Write-time permission enforcement (what gets written, by whom, to which zone)
- Provenance tagging (human-approved vs agent-inferred)
- Multi-signal ranking on retrieval (provenance 0.35, recency 0.25, confidence 0.25, causal 0.15, workspace_overlap 0.0 opt-in)
- Governed skills with computed health scores
- Conflict detection and audit trail
- Outcome-based confidence reinforcement

### Hermes adapter (`agentdb/adapters/hermes.py`) — build first

**Why first:** direct Python import (no sidecar), formal ABC eliminates API guessing,
`on_memory_write` + `on_pre_compress` + `on_delegation` are uniquely clean attachment
points for AgentDB's core governance loop.

**Implementation approach:** implement `MemoryProvider`, register via Hermes plugin
system. One `AgentDB` instance per Hermes agent identity (`agent_id` = Hermes profile
name; `session_id` = Hermes session/thread key — direct mapping, same as the Claude SDK
adapter).

**Conflict notification:** `on_session_end` can call `db.record_outcome()` and dispatch
`conflict_detected` triggers. Hermes's own gateway channels (Telegram, Slack, Discord)
are then natural consumers of the notification handler — register via
`db.register_reconciliation_handler("conflict_detected", handler)` during `initialize()`.

### OpenClaw adapter (`agentdb/adapters/openclaw.py`) — build second

**Shape:** local Python sidecar (HTTP/RPC) running one `AgentDB` instance. OpenClaw
plugin is a thin Node HTTP client intercepting `before_tool_call` and result middleware.

**Same AgentDB instance:** if both Hermes and OpenClaw run in the same environment, the
sidecar can serve both — Hermes in-process via direct import, OpenClaw via HTTP to the
same sidecar. One governed memory store, two adapter surfaces.

**Open questions** (from `openclaw.md`, unchanged):
1. Exact turn-lifecycle hook names for pre-response/post-response in the OpenClaw plugin SDK.
2. Sidecar latency cost inside `before_tool_call`'s inline path — confirm against OpenClaw docs.
3. Migration/backfill story from memory-core's existing SQLite index for opted-in agents.

---

## Recommended Build Order

1. `agentdb/adapters/hermes.py` — implement `MemoryProvider` ABC. Cleanest integration
   path; proves the governance wrapper pattern on a well-specified interface before
   tackling the sidecar complexity.
2. `agentdb/adapters/openclaw.py` — local sidecar pointing at the same AgentDB instance
   the Hermes adapter uses. Resolve the three open questions above before writing code.

Both adapters share the governance wrapper approach (C), the summarizer callable, the
optional lifecycle sweep, and the conflict notification handler pattern.

## Adapter Design Briefs

Full implementation design briefs — hook mappings, open questions, Principle 12
compliance bars, and session/key mappings — live in dedicated docs:

- **Hermes:** `docs/integrations/hermes.md` — direct Python integration via
  `MemoryProvider` ABC; all hook names verified against Hermes source. Build first.
- **OpenClaw:** `docs/integrations/openclaw.md` — Node/TypeScript sidecar integration;
  several hook names still need confirmation against OpenClaw plugin SDK. Build second.
