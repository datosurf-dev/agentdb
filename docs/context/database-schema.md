# AgentDB Database Schema & Memory Assets

This document describes AgentDB's persisted tables and on-disk memory assets. It may lag the implementation; verify schema and behavior against the current source. Fields or stores that are aspirational rather than implemented should be called out explicitly.

## Storage topology

AgentDB writes to **up to three separate SQLite files** (the third, `embeddings.db`, only when embeddings are enabled), plus a set of plain files under the memory workspace:

```
<memory_dir>/                          (default ~/.agentdb/memory/, or wherever `path=`/`memory_dir=` points)
├── config/
│   └── memory.yaml                    # runtime config (profile.name + overrides, reconciliation, embeddings, notifications)
├── database/
│   ├── agentdb.sqlite                 # memories, conflicts, audit_log, outcomes, external_retrieves, consolidation_state, ingest_ledger
│   └── embeddings.db                  # metadata, embeddings, session_topics — separate file, only when embeddings are active
├── conversations/
│   └── {agent_id}--{session_id}.jsonl # capture log + ingest_turns target; written when capture.conversation_jsonl is enabled or ingest_turns() is called
├── embeddings/                        # created on first run, unused (embeddings are written to database/embeddings.db, not here)
├── reconciliation/                    # created on first run, unused (dispatcher is in-memory; nothing persists here)
└── logs/                              # created on first run, unused
```

Two SQLite files are in play: `agentdb.sqlite` (memories + governance bookkeeping) and — only when an embedding provider is configured — `embeddings.db` (vectors). `embeddings.db` lives next to `agentdb.sqlite` (`self._path.parent / "embeddings.db"`, `interface/client.py`). (Skill storage moved to a separate package; see `migration-handoff/skill-extraction/`.)

If `AgentDB(path=...)` is used directly (no `memory_dir`), `agentdb.sqlite` and (when embeddings are active) `embeddings.db` still end up as sibling files (`path.parent / "embeddings.db"`) — the file split holds regardless of which construction style is used.

**Why separate files:** Embeddings get their own file because they are derived, regenerable, and can be wholesale wiped on a model/dimension change without touching governed memory. Everything else (memories, conflicts, outcomes, external-retrieve stats, consolidation bookkeeping, ingest ledger) shares one file and one connection pattern because they're all facets of the same governed-memory loop and need to correlate via the audit log's sequence counter.

---

## `agentdb.sqlite`

### `memories` table
Backs `MemoryRecord` (`core/models.py`). One row per record version; a `delete()` sets `archived=1`, it does not remove the row.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID, generated at construction |
| `key` | TEXT | addressable path, e.g. `memory/working/pref_format` — governs permission-router zone matching |
| `value` | TEXT | JSON-encoded; arbitrary Python value |
| `origin` | TEXT | `human_approved` \| `agent_inferred` \| `provider_ingested` |
| `trust_level` | TEXT | derived from origin: `human` \| `agent` \| `provider` (`sealed` exists as an enum value but nothing assigns it yet) |
| `agent_id` | TEXT | which agent wrote this |
| `confidence` | REAL | 0–1, default 1.0. Adjusted by `record_outcome()`; human-approved records are immune |
| `version` | INTEGER | default 1 — present but nothing currently creates a v2 of a memory record |
| `archived` | INTEGER (bool) | soft-delete flag |
| `retrieval_count` | INTEGER | incremented every time `retrieve()`/`retrieve_by_entities()` returns this record |
| `created_at` | TEXT (ISO datetime) | |
| `entities` | TEXT (JSON list) | canonical entity identifiers (file paths, model names) attached at write time — used by `retrieve_by_entities()` and the Claude adapter's action-driven retrieval |
| `last_retrieved` | TEXT (ISO datetime, nullable) | added 2026-07-11 for the ranking algorithm's recency signal; stamped by `increment_retrieval_count()` |
| `conflict_id` | TEXT (nullable) | stamped by `ConflictDetector` at write time when a temporal or authority conflict is detected; both the incoming and existing record receive the same `conflict_id` |
| `expiry_time` | TEXT (ISO datetime, nullable) | added 2026-07-17 (Task 10C); `lifecycle.run_sweep()` Step 0 archives records where `expiry_time ≤ now`; `human_approved` records are immune |
| `inferred` | INTEGER (bool) | added 2026-07-17 (Task 10C); set to 1 when `write(infer=True)` is used — value was passed through a caller-supplied `infer_fn` before storage |
| `working_dirs` | TEXT (JSON list) | added 2026-08-14 (Task 6); normalized absolute paths active at write time. Empty by default. Used by workspace_overlap ranker signal. |
| `consolidated` | INTEGER (bool) | set to 1 on the canonical record kept by `consolidate()`; lower-confidence copies are archived. `retrieve(include_consolidated=...)` filters on it. Added alongside the consolidation work (see `consolidation_state` below). |
| `source_record_ids` | TEXT (JSON list) | on a consolidated canonical, the ids of the records folded into it (also maintained on `MemoryRecord.source_record_ids`) |
| `session_id` | TEXT (nullable) | the `session_id` the record was written under. Persisted by `write()` (`store.write()`) and enables cross-restart session→record linkage via `_session_record_ids_for()` (`SELECT id FROM memories WHERE agent_id=? AND session_id=?`). Added after the original schema doc; no longer in-memory-only. |

**Fields planned but not present:** `user_id`, `expiry_policy`, `tier`, `audit_sequence`, `embedding`, `embedding_version`. `ExpiryPolicy`/`MemoryTier` enums exist in `core/models.py` but nothing attaches them to a record yet. Embeddings live in a separate DB (see `embeddings.db` below), not as a column here.

### `conflicts` table
Backs `ConflictRecord` (`core/models.py`), written by `MemoryStore.store_conflict()` (`core/store.py`) whenever `ConflictDetector` flags a temporal or authority conflict at write time. This is the durable backing for the "conflict queue" / `agentdb_stats().conflict_pending_count` and `MemoryStore.list_conflicts()`.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID |
| `record_a_id` | TEXT | one side of the conflict |
| `record_b_id` | TEXT | the other side (the incoming vs. existing record for a write-time conflict) |
| `conflict_type` | TEXT | e.g. temporal / authority / semantic |
| `resolved` | INTEGER (bool) | `list_conflicts(resolved=False)` (default) returns only `resolved = 0`; `resolved=True` returns all |
| `auto_resolved` | INTEGER (bool) | set when the conflict was resolved automatically (e.g. temporal recency) rather than flagged for review |
| `created_at` | TEXT (ISO datetime) | |

Note the two-level linkage: the `memories.conflict_id` column stamps *which* conflict a record belongs to on both records, while this table holds the conflict row itself (`record_a_id`/`record_b_id`, type, resolution state).

### `audit_log` table
Backs `AuditLog`. Append-only in practice (nothing ever updates or deletes a row), but implemented as a plain SQLite table with an autoincrementing sequence — not the NDJSON file originally planned.

| Column | Type | Notes |
|---|---|---|
| `sequence` | INTEGER PK AUTOINCREMENT | the shared sequence counter referenced by Design Principle 7 |
| `event_type` | TEXT | see event types below |
| `payload` | TEXT (JSON) | shape varies per event type |
| `created_at` | TEXT (ISO datetime) | |

Event types currently emitted: `memory_write`, `memory_delete`, `memory_retrieve` (payload includes `record_ids` and real `signal_scores` from the ranker), `memory_outcome`, `external_conflict_detected`, `permission_denied`.

### `outcomes` table
One row per `record_outcome()` call.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | |
| `session_id` | TEXT | |
| `agent_id` | TEXT | |
| `outcome_type` | TEXT | e.g. `task_complete`, `human_correction`, `human_approval`, `agent_retry` — no validation enforced on the value today |
| `outcome_value` | REAL | 0–1; ≥0.7 boosts linked records, ≤0.3 decays them, 0.3–0.7 is neutral |
| `linked_retrieval_sequence` | INTEGER (nullable) | intended to link back to the `audit_log` retrieval event; not populated by current code |
| `created_at` | TEXT (ISO datetime) | |

**How outcomes link to records today:** not via this table's `linked_retrieval_sequence` column — `db.lifecycle.record_outcome()` reads an in-memory `AgentDB._session_record_ids: dict[session_id, list[record_id]]` populated during `retrieve()`/`retrieve_by_entities()`. This is why outcome linkage is lost on process restart (see Known Gaps). Note the distinction from `purge_session()`: that path uses the **persisted** `memories.session_id` column (via `_session_record_ids_for()`), which *does* survive a restart. So the persisted column exists and is used for purging/cleanup, but `record_outcome()` itself still relies on the in-memory dict.

### `external_retrieves` table
One row per `retrieve(..., external_context=...)` call where external context was supplied.

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT | |
| `had_external_context` | INTEGER (bool) | |
| `had_conflict` | INTEGER (bool) | whether any external content overlapped an existing memory's value |

This is one of two conflict-detection paths. The other is `ConflictDetector`, wired into `write()` — it checks the newly written record against existing stored records for temporal conflicts (same key, overlapping time window) and authority conflicts (agent vs. human-approved). The `external_retrieves` table only covers the narrower case of externally-supplied context overlapping stored values during `retrieve()` — it is not the same code path.

### `consolidation_state` table
Backs the auto-consolidation trigger (`governance/lifecycle_manager.py`, `_init_consolidation_table` / `_increment_session_count` / `_reset_consolidation_state`). Keyed by `agent_id`, it records how many sessions have accumulated since the last consolidation so the "≥7 days AND ≥10 sessions" gate documented in `mechanisms.md` can be evaluated without scanning history.

| Column | Type | Notes |
|---|---|---|
| `agent_id` | TEXT PK | one row per agent |
| `last_run_at` | TEXT (ISO datetime, nullable) | when consolidation last ran for this agent |
| `session_count` | INTEGER | sessions seen since the last run; incremented at `conversation_started`, reset after `consolidate()` |

### `ingest_ledger` table
Backs `AgentDB.ingest_turns()` deduplication (`agentdb/ingest.py`, `_CREATE_INGEST_LEDGER`). One row per accepted turn hash, so repeated `ingest_turns()` calls over an overlapping dump are idempotent (the same turn is never written twice).

| Column | Type | Notes |
|---|---|---|
| `agent_id` | TEXT | part of the PK |
| `session_id` | TEXT | part of the PK |
| `turn_hash` | TEXT | hash of the normalised turn; part of the PK |
| `turn_id` | TEXT | caller-supplied stable id for the turn |
| `source` | TEXT (nullable) | free-text source label passed to `ingest_turns(source=...)` |
| `ingested_at` | TEXT (ISO datetime) | |

Composite PK is `(agent_id, session_id, turn_hash)`. `purge_session()` deletes this session's ledger rows.

---

## `embeddings.db`

Only created when an embedding provider is active (`embed_fn=`, `local_embed=True`, or `embeddings.enabled: true`) **and** the DB path is not `:memory:`. Lives beside `agentdb.sqlite` (`database/embeddings.db`). Owned by `EmbeddingStore` (`embeddings/store.py`). Vectors are stored as packed `float32` BLOBs, not JSON.

### `metadata` table
Key/value store: `model_name` and `dimensions` of the active embedding model.

| Column | Type | Notes |
|---|---|---|
| `key` | TEXT PK | `model_name` or `dimensions` |
| `value` | TEXT | |

On open, `_validate_or_reset()` compares the stored `model_name`/`dimensions` against the active provider. If **either changed**, it logs a warning and **wipes `embeddings` and `session_topics`** so vectors regenerate lazily under the new model — mixing dimensions across models would silently corrupt similarity. Dimensions are treated as changed only when both sides are known and differ.

### `embeddings` table
One row per embedded record.

| Column | Type | Notes |
|---|---|---|
| `table_name` | TEXT | source namespace, e.g. `memories` |
| `record_id` | TEXT | id within that namespace |
| `embedding` | BLOB | packed `float32` vector |
| `working_dirs` | TEXT (JSON list) | workspace paths active when embedded; used by the workspace-overlap signal |
| `created_at` | TEXT (ISO datetime) | |

Composite PK `(table_name, record_id)`. `prune_orphans()` deletes rows whose `record_id` is no longer valid for the table.

### `session_topics` table
One embedding per session, built from the opening message, used for session-topic relevance.

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT PK | |
| `embedding` | BLOB | packed `float32` vector of the session topic |
| `created_at` | TEXT (ISO datetime) | |

---

## Memory assets outside the SQL tables

### `config/memory.yaml`
Written on first run if absent, from `_DEFAULT_MEMORY_YAML` in `interface/client.py`:

```yaml
profile:
  name: budget_optimized

reconciliation:
  triggers:
    - conversation_started
database:
  mode: embedded
  path: database/agentdb.sqlite

# Uncomment to enable semantic retrieval and conflict detection.
# embeddings:
#   enabled: false
#   # model: sentence-transformers/all-MiniLM-L6-v2
```

The config uses a single top-level `profile:` root. `profile.name` selects a built-in (`budget_optimized`, `full_storage`, `research`); any of `retrieval:`/`injection:`/`lifecycle:`/`capture:` nested under `profile:` override individual fields, and `profile.agents.<id>` holds per-agent overrides. **Legacy root keys** (`mode`, `profiles`, `retrieval`, `injection`, `lifecycle`, `capture` at the top level) cause `_resolve_config()` to raise `ConfigurationError` — they must nest under `profile:`. `profile.name: full_storage` requires a non-empty `tracked_directories` (validated at `AgentDB.__init__`, raises `ValueError` otherwise). Read into `AgentDB.memory_config` (a plain dict) at construction; nothing reloads it if the file changes later.

### `conversations/{agent_id}--{session_id}.jsonl`
One JSON object per line, appended by the Claude adapter's `finalize_turn`/`finalize_turn_async`, gated on `memory_config["capture"]["conversation_jsonl"]` being `true` **and** `memory_dir` being configured (silently skipped otherwise — no error, no fallback path):

```json
{"session_id": "...", "message_id": "...", "user_query": "...", "model_response": "..."}
```

This is a flat capture log, not indexed or queried by anything else in the codebase today — it exists for external tooling / manual inspection, not for AgentDB's own retrieval. `AgentDB.ingest_turns()` also appends to this same file/shape (writing JSONL first, then memories, then the `ingest_ledger` dedup row) when a harness brings its own transcript.

### Directory tracking snapshots
Not a separate store — persisted **as ordinary `memories` rows**. `core/directory_tracking.collect_directory_snapshots()` walks each configured tracked directory (capped at `tracking_max_files_per_directory` files, default 200) and returns `{directory, exists, file_count, entries: [{path}, ...]}` per directory. The Claude adapter's `finalize_turn` writes one memory record per snapshot:

- key: `memory/episodic/claude/{agent_id}/{session_id}/directory_tracking/{timestamp}-{idx}`
- value: `{session_id, message_id, directory_snapshot: {...}, captured_at}`
- origin: `agent_inferred`

This is exactly what you see as "governed memory context" injected at the top of a slack-assistant session — those are `retrieve()` hits against these directory-tracking memory records.

### `core/file_integrity.py` — built, not wired in
`observe_file()`/`observe_files()` compute a `FileObservation` (path, SHA-256, size, captured/modified timestamps) for a given file. Fully implemented and tested (`tests/unit/test_file_integrity.py`), but **no code path calls it** — it isn't invoked from the client, the adapter, or directory tracking. It exists as a building block, most plausibly for a future content-change-aware `file_modified` reconciliation trigger (directory tracking today only lists paths, not whether their contents actually changed) — but that wiring hasn't been written.

### `embeddings/`, `reconciliation/`, `logs/`
Created on first run by `AgentDB.__init__`. None are written to today:
- `embeddings/` — **unused**; embeddings are actually persisted to `database/embeddings.db` (see above), so this directory is a leftover from the original layout.
- `reconciliation/` — **unused**; the reconciliation dispatcher is in-memory (`db.gateway`), nothing is queued or checkpointed to disk.
- `logs/` — **unused**.

### In-memory-only state (not on disk, lost on process restart)
- `AgentDB._session_record_ids: dict[session_id, list[record_id]]` — the link used by `record_outcome()` to attribute an outcome to the records a `retrieve()` returned. (The persisted `memories.session_id` column is the durable alternative, but `record_outcome()` does not currently read it — `purge_session()` does.)
- `AgentDB._session_profiles: dict[session_id, SessionProfile]` — per-session metadata (repos, request types) seeded at `conversation_started`; reconstructed on the next `conversation_started` after a restart.
- `AgentDB._reconciliation_handlers: dict[trigger_type, list[Callable]]` — registered callbacks, not data; harmless to lose on restart, but means any registration must happen again each process start. This is also the core mechanism behind `docs/context/architecture-principles.md` Principle 12b (async integrity checks) — reused for any named trigger, not just reconciliation. By design (see Principle 7) it's meant to stay in-memory-only unless a specific consumer would lose real value from a missed notification across a restart.

---

## How it all connects (data flow)

```
write(key, value, origin, agent_id, entities=..., session_id=...)
    → PermissionRouter.check_write()  (raises PermissionDenied + audit-logs on failure)
    → ConflictDetector.check(): temporal conflicts auto-resolve by recency; authority conflicts stamp conflict_id on both records + row in conflicts table
    → memories row inserted (session_id persisted when supplied)
    → db.gateway.dispatch("conflict_detected", ...) if conflict detected
    → audit_log: memory_write

retrieve(query, agent_id, ...)
    → memories rows matching query (substring/keyword search, pool of up to max(limit, 50))
    → MultiSignalRanker.rank(): reads origin, last_retrieved, confidence, retrieval_count, conflict_id
    → top `limit` selected; increment_retrieval_count() stamps retrieval_count + last_retrieved
    → audit_log: memory_retrieve (real per-record signal_scores)
    → _session_record_ids[session_id] extended with returned record ids (in-memory; persisted linkage is via the session_id column)

record_outcome(session_id, agent_id, outcome_type, outcome_value)
    → looks up _session_record_ids[session_id]  (in-memory only — see Known Gaps)
    → applies confidence delta per origin (human_approved immune, provider_ingested decay-only, agent_inferred full range)
    → outcomes row inserted
    → audit_log: memory_outcome

ingest_turns(agent_id, session_id, turns, source=...)
    → validate all turns (nothing written on failure)
    → dedup against ingest_ledger (agent_id, session_id, turn_hash)
    → append accepted turns to conversations/*.jsonl
    → write() each caller-extracted memory (origin=provider_ingested; tool-turn memories ignored)
    → insert ingest_ledger rows

purge_session(agent_id, session_id)   # hard delete; requires require_confirmation=False
    → _session_record_ids_for(): SELECT id FROM memories WHERE agent_id=? AND session_id=?  (persisted)
    → DELETE conflicts / memories by those record ids; DELETE outcomes / external_retrieves / ingest_ledger by session
    → DELETE memory-embeddings (via self._embedding_store → database/embeddings.db) + the session_topic row
    → remove conversation JSONL
    → audit_log: session_purged (counts only, no content)
    → _store._reload()

Claude adapter finalize_turn(...)
    → optional memory_writes  → write() (as above)
    → optional conversation JSONL append (conversations/*.jsonl)
    → optional directory-tracking snapshots → write() (as above, under memory/episodic/.../directory_tracking/...)
    → optional record_outcome (via finalize_turn_async's outcome recording)
```

The embedding purge reuses the live `self._embedding_store` (correct path `database/embeddings.db`, correct model/dimensions) rather than reconstructing a store; when no embedding provider is active there is nothing to purge. The `ingest_ledger` delete creates the table if absent, so a purge never fails on a DB populated only via `write()`.

Skill management (registry, versioning, staleness, proposals, injection) is no longer part of AgentDB — it was extracted to an independent package. See `migration-handoff/skill-extraction/`.

---

## Known gaps this schema reveals

The following implementation gaps are visible from the schema and current source:

- **Outcome→record linkage is still in-memory (for `record_outcome`).** `record_outcome()` attributes confidence changes via the in-memory `_session_record_ids` dict, so a process restart between `retrieve()` and `record_outcome()` loses that attribution — the `outcomes.linked_retrieval_sequence` column exists to fix this and remains unpopulated. (Contrast `purge_session()`, which uses the persisted `memories.session_id` column and does survive restarts.)
- **`file_integrity.py` is dead code** from the product's perspective — built and tested, never called by the client, adapter, or directory tracking.
- **`reconciliation/` and `logs/` are empty placeholders; `embeddings/` is a stale placeholder.** The reconciliation dispatcher is in-memory and persists nothing; embeddings actually live in `database/embeddings.db`, not in the `embeddings/` directory the original layout created.
- **`ingest_turns()` and `purge_session()` are now implemented** (`interface/client.py`, backed by `ingest_ledger`), so the older "proposed door" framing no longer applies — see `ingest-turns-design.md` for the design that landed.
