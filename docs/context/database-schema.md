# AgentDB Database Schema & Memory Assets

This document describes what is actually persisted by AgentDB today — every table, its columns, and every on-disk memory asset outside the SQL tables — and how they interact. It reflects the code as of 2026-07-17 (see `docs/superpowers/plans/2026-06-06-agentdb-revised.md` → Current Status for what's built vs planned). Where a field or store is aspirational rather than implemented, that's called out explicitly.

## Storage topology

AgentDB writes to **two separate SQLite files**, plus a set of plain files under the memory workspace:

```
<memory_dir>/                          (default ~/.agentdb/memory/, or wherever `path=`/`memory_dir=` points)
├── config/
│   └── memory.yaml                    # runtime config (mode, tracked_directories, capture, reconciliation)
├── database/
│   ├── agentdb.sqlite                 # memories, audit_log, outcomes, external_retrieves tables
│   └── skills.sqlite                  # skills table — separate file, separate trust boundary
├── conversations/
│   └── {agent_id}--{session_id}.jsonl # optional, only if capture.conversation_jsonl is enabled
├── embeddings/                        # placeholder dir, unused (Phase 4)
├── reconciliation/                    # placeholder dir, unused (no queue/checkpoint files written yet)
└── logs/                              # placeholder dir, unused
```

If `AgentDB(path=...)` is used directly (no `memory_dir`), `agentdb.sqlite` and `skills.sqlite` still end up as sibling files (skills.sqlite = `path.parent / "skills.sqlite"`) — the two-file split holds regardless of which construction style is used.

**Why two files, not one:** skills live in the human-approved governance zone conceptually (`authored_by` must be `"human"`, enforced in `SkillStore.create()`); giving them a physically separate SQLite file is a stronger boundary than a shared file with a `WHERE authored_by='human'` filter would be. Everything else (memories, audit log, outcomes, external-retrieve stats) shares one file and one connection pattern because they're all facets of the same governed-memory loop and need to correlate via the audit log's sequence counter.

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
| `version` | INTEGER | default 1 — present but nothing currently creates a v2 of a memory record (unlike skills, which do version) |
| `archived` | INTEGER (bool) | soft-delete flag |
| `retrieval_count` | INTEGER | incremented every time `retrieve()`/`retrieve_by_entities()` returns this record |
| `created_at` | TEXT (ISO datetime) | |
| `entities` | TEXT (JSON list) | canonical entity identifiers (file paths, model names) attached at write time — used by `retrieve_by_entities()` and the Claude adapter's action-driven retrieval |
| `last_retrieved` | TEXT (ISO datetime, nullable) | added 2026-07-11 for the ranking algorithm's recency signal; stamped by `increment_retrieval_count()` |
| `conflict_id` | TEXT (nullable) | stamped by `ConflictDetector` at write time when a temporal or authority conflict is detected; both the incoming and existing record receive the same `conflict_id` |
| `expiry_time` | TEXT (ISO datetime, nullable) | added 2026-07-17 (Task 10C); `lifecycle.run_sweep()` Step 0 archives records where `expiry_time ≤ now`; `human_approved` records are immune |
| `inferred` | INTEGER (bool) | added 2026-07-17 (Task 10C); set to 1 when `write(infer=True)` is used — value was passed through a caller-supplied `infer_fn` before storage |
| `working_dirs` | TEXT (JSON list) | added 2026-08-14 (Task 6); normalized absolute paths active at write time. Empty by default. Used by workspace_overlap ranker signal. |

**Fields planned but not present:** `user_id`, `session_id` (as a column — session linkage today is in-memory only, see Known Gaps), `expiry_policy`, `tier`, `audit_sequence`, `embedding`, `embedding_version`. `ExpiryPolicy`/`MemoryTier` enums exist in `core/models.py` but nothing attaches them to a record yet.

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

**How outcomes link to records today:** not via this table's `linked_retrieval_sequence` column — via an in-memory `AgentDB._session_record_ids: dict[session_id, list[record_id]]` populated during `retrieve()`/`retrieve_by_entities()`. This is why outcome linkage is lost on process restart (see Known Gaps).

### `external_retrieves` table
One row per `retrieve(..., external_context=...)` call where external context was supplied.

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT | |
| `had_external_context` | INTEGER (bool) | |
| `had_conflict` | INTEGER (bool) | whether any external content overlapped an existing memory's value |

This is one of two conflict-detection paths. The other is `ConflictDetector`, wired into `write()` — it checks the newly written record against existing stored records for temporal conflicts (same key, overlapping time window) and authority conflicts (agent vs. human-approved). The `external_retrieves` table only covers the narrower case of externally-supplied context overlapping stored values during `retrieve()` — it is not the same code path.

---

## `skills.sqlite`

### `skills` table
Backs `Skill` (`core/models.py`), managed by `SkillStore`/`SkillsAPI`. Composite primary key `(id, version)` — updates create a new row rather than overwriting, so old versions are retained and queryable.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT | stable skill identifier across versions |
| `version` | TEXT | e.g. `"1.0.0"`, `"1.1.0"` |
| `name` | TEXT | |
| `content` | TEXT | the instruction text itself |
| `authored_by` | TEXT | must be `"human"` — `SkillStore.create()` raises if not |
| `scope` | TEXT (JSON list) | agent_ids this skill applies to |
| `archived` | INTEGER (bool) | |
| `health_score` | REAL | **not trusted from this column** — always recomputed at read time in `get_for_agent()`, per the algorithm-review skill's own rule ("computed at read time, never stored") |
| `health_score_reasons` | TEXT (JSON list) | recomputed alongside `health_score` |
| `created_at` | TEXT (ISO datetime) | |
| `updated_at` | TEXT (ISO datetime) | |
| `last_retrieved` | TEXT (ISO datetime, nullable) | fixed 2026-07-11 to actually drive staleness decay (previously staleness was computed from `created_at` regardless of retrieval activity — a real correctness bug) |
| `parent_skill_id` | TEXT (nullable) | set to the original `id` when `update()` creates a new version |

**Health score formula (current):** `age_days = now - (last_retrieved or created_at)`; if `age_days > 30`, `health_score = max(0.1, 1.0 - (age_days - 30) * 0.03)`. Below 0.6 surfaces a `staleness_warning` in retrieval results.

**Not implemented:** entity-drift penalty, unresolved-conflict penalty, a `SkillConflict` exception, and a `search()` method — all were in the original spec but never built.

---

## Memory assets outside the SQL tables

### `config/memory.yaml`
Written on first run if absent, from this template:

```yaml
mode: budget_optimized
tracked_directories: []
capture:
  conversation_jsonl: false
reconciliation:
  triggers:
    - conversation_started
database:
  mode: embedded
  path: database/agentdb.sqlite
```

`mode: full_storage` requires a non-empty `tracked_directories` — validated at `AgentDB.__init__` time, raises `ValueError` otherwise. Read into `AgentDB.memory_config` (a plain dict) at construction; nothing reloads it if the file changes later.

### `conversations/{agent_id}--{session_id}.jsonl`
One JSON object per line, appended by the Claude adapter's `finalize_turn`/`finalize_turn_async`, gated on `memory_config["capture"]["conversation_jsonl"]` being `true` **and** `memory_dir` being configured (silently skipped otherwise — no error, no fallback path):

```json
{"session_id": "...", "message_id": "...", "user_query": "...", "model_response": "..."}
```

This is a flat capture log, not indexed or queried by anything else in the codebase today — it exists for external tooling / manual inspection, not for AgentDB's own retrieval.

### Directory tracking snapshots
Not a separate store — persisted **as ordinary `memories` rows**. `core/directory_tracking.collect_directory_snapshots()` walks each configured tracked directory (capped at `tracking_max_files_per_directory` files, default 200) and returns `{directory, exists, file_count, entries: [{path}, ...]}` per directory. The Claude adapter's `finalize_turn` writes one memory record per snapshot:

- key: `memory/episodic/claude/{agent_id}/{session_id}/directory_tracking/{timestamp}-{idx}`
- value: `{session_id, message_id, directory_snapshot: {...}, captured_at}`
- origin: `agent_inferred`

This is exactly what you see as "governed memory context" injected at the top of a slack-assistant session — those are `retrieve()` hits against these directory-tracking memory records.

### `core/file_integrity.py` — built, not wired in
`observe_file()`/`observe_files()` compute a `FileObservation` (path, SHA-256, size, captured/modified timestamps) for a given file. Fully implemented and tested (`tests/unit/test_file_integrity.py`), but **no code path calls it** — it isn't invoked from the client, the adapter, or directory tracking. It exists as a building block, most plausibly for a future content-change-aware `file_modified` reconciliation trigger (directory tracking today only lists paths, not whether their contents actually changed) — but that wiring hasn't been written.

### `embeddings/`, `reconciliation/`, `logs/`
Created on first run, otherwise empty. No code writes into them yet — they're placeholders for Phase 4 (embedding index artifacts) and the reconciliation dispatcher (queue/checkpoint state), neither of which persists anything today.

### In-memory-only state (not on disk, lost on process restart)
- `AgentDB._session_record_ids: dict[session_id, list[record_id]]` — the only current link between a `retrieve()` call and a later `record_outcome()` call.
- `AgentDB._reconciliation_handlers: dict[trigger_type, list[Callable]]` — registered callbacks, not data; harmless to lose on restart, but means any registration must happen again each process start. This is also the core mechanism behind `docs/context/architecture-principles.md` Principle 12b (async integrity checks) — reused for any named trigger, not just reconciliation. By design (see Principle 7) it's meant to stay in-memory-only unless a specific consumer would lose real value from a missed notification across a restart.

---

## How it all connects (data flow)

```
write(key, value, origin, agent_id, entities=...)
    → PermissionRouter.check_write()  (raises PermissionDenied + audit-logs on failure)
    → ConflictDetector.check(): temporal conflicts auto-resolve by recency; authority conflicts stamp conflict_id on both records
    → memories row inserted
    → db.gateway.dispatch("conflict_detected", ...) if conflict detected
    → audit_log: memory_write

retrieve(query, agent_id, ...)
    → memories rows matching query (substring/keyword search, pool of up to max(limit, 50))
    → MultiSignalRanker.rank(): reads origin, last_retrieved, confidence, retrieval_count, conflict_id
    → top `limit` selected; increment_retrieval_count() stamps retrieval_count + last_retrieved
    → audit_log: memory_retrieve (real per-record signal_scores)
    → skills.get_for_agent(agent_id) merged in as skill_warnings (separate skills.sqlite lookup)
    → _session_record_ids[session_id] extended with returned record ids (in-memory only)

record_outcome(session_id, agent_id, outcome_type, outcome_value)
    → looks up _session_record_ids[session_id]
    → applies confidence delta per origin (human_approved immune, provider_ingested decay-only, agent_inferred full range)
    → outcomes row inserted
    → audit_log: memory_outcome

Claude adapter finalize_turn(...)
    → optional memory_writes  → write() (as above)
    → optional conversation JSONL append (conversations/*.jsonl)
    → optional directory-tracking snapshots → write() (as above, under memory/episodic/.../directory_tracking/...)
    → optional record_outcome (via finalize_turn_async's outcome recording)
```

Skills are retrieved alongside memories in the same `retrieve()` call, but they are a structurally separate governance zone: separate file, separate write-authorization rule (`authored_by="human"` only), separate health-score lifecycle that doesn't touch `confidence`/`record_outcome` at all.

---

## Known gaps this schema reveals

These are already tracked in the plan's Current Status, but are easiest to see clearly from the schema itself:

- **Session→record linkage is in-memory only.** A process restart between `retrieve()` and `record_outcome()` silently loses the ability to attribute the outcome to specific records — the `outcomes.linked_retrieval_sequence` column exists precisely to fix this and is unused.
- **`file_integrity.py` is dead code** from the product's perspective — built and tested, never called.
- **`reconciliation/` and `embeddings/` are empty placeholders** — the reconciliation dispatcher and embedding pipeline don't persist anything yet, despite the directories existing from day one.
