# AgentDB mechanisms — what runs when, and why

Reference for developers integrating AgentDB into an agent harness. It maps
every mechanism to **where it is declared**, **when it runs**, **what it
injects or stores**, and **whether the user can customise it**.

This is deliberately implementation-anchored: each row names the file and
function so a claim can be checked against source. If this document and the
code disagree, the code wins.

---

## 1. The two stores

AgentDB keeps **two independent stores** with different purposes. Confusing
them is the most common integration error.

| Store | Location | Written by | Read back? | Purpose |
|---|---|---|---|---|
| **Memory DB** (SQLite) | `<memory_dir>/database/agentdb.sqlite` | `store.write()` via `AgentDB.write()` | Yes (`retrieve()`) | Governed knowledge: facts, preferences, outcomes, conflicts |
| **Conversation JSONL** | `<memory_dir>/conversations/<agent_id>--<session_id>.jsonl` | `AgentDBMiddleware.finalize_turn` | **No** — write-only audit trail | Raw `user_query` / `model_response` per turn |

`memory_dir` is resolved from the constructor argument, then `AGENTDB_MEMORY_DIR`,
then `AGENTDB_HOME`. On construction only `config/` and `database/` are created
eagerly — they hold the config file and the SQLite DB. The optional
`conversations/`, `embeddings/`, `reconciliation/`, and `logs/` dirs are created
lazily on first use by their writers (`interface/client.py`, `AgentDB.__init__`,
0.7.0 §5). Opt back into eager creation with `workspace.eager_dirs: true`
(memory.yaml) or `AGENTDB_WORKSPACE_EAGER_DIRS=true`.

---

## 2. Mechanisms by type

### A. Injection — what the model sees before it answers

| Mechanism | Declared in | When it runs | Configurable? |
|---|---|---|---|
| **Memory injection** | `adapters/middleware.py` → `prepare_turn` / `prepare_turn_async` | Every turn, called by the harness | Shape: `RunnerCapabilities` (code). Size: `injection.context_token_limit`, `memory_budget_chars` (`memory.yaml`) |
| **Workspace injection** | same, gated by `capabilities.inject_workspace` | Every turn (async path only) | `inject_workspace` flag + 30% of budget |
| **Work context injection** | same + `_build_work_context_block()` | Every turn (async path only) | `inject_work_context` flag + 30% of budget |

**What is injected is not prior transcripts.** Memory records come from
`retrieve()` (ranked by provenance, recency, confidence, causal overlap,
optionally semantic similarity). Work context is built from
`_session_work_log` — **the last 5 turns of the current session only**
(`_build_work_context_block`), formatted as `Turn N: read X. wrote Y. <key fact>`.

The memory header is configurable text, e.g.
`"Background context from previous sessions (supplemental — current conversation and user request take priority):"`.

**Customisation:** capability flags are chosen in harness code (`CLAUDE_CAPABILITIES`,
`SERVERLESS_CAPABILITIES`, `LOCAL_CAPABILITIES`, or your own `RunnerCapabilities(...)`);
budgets and header text come from the profile in `memory.yaml`.

### B. Extraction and distillation — turning turns into knowledge

| Mechanism | Declared in | When it runs | Configurable? |
|---|---|---|---|
| **Turn write** (`write`) | `interface/client.py` → `AgentDB.write()` | Whenever the harness calls it (typically `finalize_turn`) | Caller decides key/value/origin/confidence |
| **Auto-persist response** | `capture.auto_persist_response` in profile | End of turn, if enabled | Profile field |
| **Conflict detection** | `AgentDB.write()` → conflict checks | On write | Threshold in profile (`retrieval.conflict_threshold`, currently reserved) |

Note the trust model: `origin` decides `trust_level` (`human_approved`,
`agent_inferred`, `provider_ingested`). Provider-ingested data is never promoted.

### C. Summarisation — four distinct mechanisms

| # | Mechanism | Declared in | When it runs | Uses an LLM? |
|---|---|---|---|---|
| 1 | **Work-context block** | `middleware._build_work_context_block()` | Every `prepare_turn_async` | No — last 5 work-log entries |
| 2 | **Consolidation** | `client.consolidate()` → `governance/lifecycle_manager.consolidate()` | Auto at `conversation_started` when **≥7 days AND ≥10 sessions** (defaults; configurable via `profile.consolidation` — `days`, `min_sessions`, `enabled`); or manual | **Only if `llm_fn` supplied** |
| 3 | **Compaction summary** | `client.write_compaction_summary()` | Harness calls it before truncating context | No — caller supplies the text |
| 4 | **Session summary (JSONL)** | `middleware.write_session_summary()` via `end_session()` | Session close | No |

**Consolidation details (the one that matters most):**

- Operates on **stored records**, not transcripts: filters
  `agent_id`, `not archived`, `origin != human_approved`, and
  **`created_at <= now - since_hours`** (`since_hours` defaults to **24**).
- Therefore **records written in the last 24h are invisible to a default
  `consolidate()` call**. Pass `since_hours=0` to include fresh records.
- Clusters by key-word overlap (`_lc_cluster`), then a **compatibility gate**:
  all members must share a key-domain slug and have no temporal/authority
  conflict, so related-but-distinct or contradictory records are not merged.
- Creates a **NEW `derived_canonical` record** (`kind=derived_canonical`,
  `origin=agent_inferred`, `source_record_ids` + per-source digests in
  `derivation`). Source records are **never overwritten** — only soft-archived.
- With `llm_fn`, the canonical value is synthesised; otherwise it is a copy of
  the highest-confidence source value.
- A canonical is **invalidated** when any source's digest changes; stale
  canonicals are excluded from selection. `retrieve()` may re-inject a source
  that holds a query-critical detail the canonical omits (`unique_detail`).
- `human_approved` records are immune.

**Compaction summary** writes to `memory/compaction/summary/<session_id>`,
defaulting to `origin="agent_inferred"` (machine-produced → lifecycle-decayable).
Only an explicit `origin="human_approved"` (verified human action) survives all
sweeps. Retrievable by any future session for that agent (`client.py`:
`write_compaction_summary`).

There is no longer a `checkpoint()` companion. It wrote
`memory/compaction/checkpoint/<session_id>` whose entire value was
`{session_id, type}`, no code path read it, and because retrieval returns every
eligible record it surfaced as a content-free line in each injection. The summary
written at the same moment is the durable record of the session.

### D. Lifecycle and governance — decay, archival, signals

| Mechanism | Declared in | When it runs | Configurable? |
|---|---|---|---|
| **Lifecycle sweep** | `db.lifecycle.run_sweep()` | Manual — **no background scheduler** | `lifecycle.*` profile fields |
| **Reinforcement / outcomes** | `db.lifecycle.reinforce()`, `record_outcome()` | Harness calls them | `lifecycle.reinforce_rate` |
| **Gateway dispatch** | `db.gateway.subscribe/dispatch` | On events (`conversation_started`, `conflict_detected`, …) | `reconciliation.triggers` |
| **Notifications** | `db.gateway` handlers | On subscribed events | `notifications:` block in `memory.yaml` |
| **Audit log** | `db.audit_log` | Every write/delete/retrieval | `db.audit_log.replay()` |

Sweep steps (`lifecycle_manager.run_sweep`): **step 0** archive records whose
`expiry_time` has passed → **step 1** optional confidence decay → **step 2**
archive below `archival_threshold` → **step 3** archive past `staleness_days`.
`human_approved` records are immune to every step.

### E. Storage — the optional raw conversation log

| Mechanism | Declared in | When it runs | Configurable? |
|---|---|---|---|
| **Conversation JSONL** | `middleware.finalize_turn` | End of turn | `capture.conversation_jsonl` (profile) |

Entry shape: `{session_id, message_id, user_query, model_response}`.
Write-only — nothing in AgentDB reads it back. Enabled by default in
`full_storage` and `research`; disabled in `budget_optimized`.

---

## 3. Profiles — the knobs

Three built-in profiles (`core/profiles.py`), selectable in `memory.yaml` and
overridable per section:

| Field | `budget_optimized` | `full_storage` | `research` |
|---|---|---|---|
| `retrieval.retrieve_limit` | 5 | 200 | 50 |
| `injection.context_token_limit` | 1000 | 50000 | 8000 |
| `injection.memory_budget_chars` | 2000 | 200000 | 24000 |
| `lifecycle.staleness_days` | 90 | 3650 | 180 |
| `lifecycle.archival_threshold` | 0.10 | 0.0 | 0.05 |
| `capture.conversation_jsonl` | **false** | **true** | **true** |
| `capture.auto_persist_response` | false | true | true |

Any section can be declared alongside `profile.name` to override just those
fields. Invalid configuration raises `ConfigurationError` rather than silently
falling back to a default.

---

## 4. Runtime responsibilities

| Layer | Owns | Does not own |
|---|---|---|
| **Harness** (AgentGate, Claude SDK, Hermes, …) | When to call `prepare_turn` / `finalize_turn` / `end_session`; which `RunnerCapabilities` apply; how to parse its own logs | Memory ranking, trust levels, governance |
| **AgentDB** | Ranking, trust model, conflicts, lifecycle, injection assembly | The transcript format of any given harness |

**Capabilities are the only contract between them.** AgentDB owns the mechanism;
the harness supplies the values.

---

## 5. Gaps and known limitations

- `retrieval.conflict_threshold` is currently reserved (not used elsewhere).
- When a `permissions_path` is supplied (isolation mode), `read()`, `retrieve()`,
  and `retrieve_by_entities()` **enforce `can_read`** — reads without a caller
  identity fail closed, and a derived canonical is disclosed only if **all** its
  sources are readable. Without `permissions_path` (default trusted mode), no
  read/write authorization is enforced and the host owns its security boundary.
- No background scheduler: `run_sweep()` and `consolidate()` must be triggered.
- Conversation JSONL is write-only from AgentDB's own retrieval perspective, **but** `AgentDB.ingest_turns()` now provides an explicit ingestion door: the harness normalises its own turns into `ConversationTurn` objects and `ingest_turns()` validates, dedupes (via `ingest_ledger`) and writes them. There is still no parser that reads the raw JSONL back into memory automatically — see [`ingest-turns-design.md`](ingest-turns-design.md) for the design that landed.
- `delete(key)` is a **soft** delete (`archived = 1`); session content also lives in `conversations/`, embeddings, outcomes, `external_retrieves` and the audit log. `AgentDB.purge_session(agent_id, session_id, require_confirmation=False)` is the implemented **hard** removal that clears all of those for one session (keeping a content-free `session_purged` audit event). See [`ingest-turns-design.md`](ingest-turns-design.md).
- Injection covers memory records, workspace and current-session work
  context — **not** prior-session transcripts.
