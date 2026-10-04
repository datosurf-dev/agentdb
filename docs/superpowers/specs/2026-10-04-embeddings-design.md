# Embeddings Layer — Design Spec

**Date:** 2026-10-04
**Status:** Approved — ready for implementation planning

---

## Goal

Add an embedding-backed semantic layer to agentdb that activates semantic retrieval and semantic conflict detection, without forcing any new dependency on users who don't opt in. This unblocks two previously deferred features: semantic conflict detection and the skill reconciliation pipeline (Task 13).

## Architecture Overview

### Module structure

```
src/agentdb/embeddings/
├── __init__.py      # re-exports EmbeddingProvider, LocalEmbeddingProvider, EmbeddingStore
├── provider.py      # Protocol + FunctionProvider + LocalProvider (sentence-transformers)
└── store.py         # sidecar SQLite — metadata table + embeddings table + session_topics table
```

One new optional dependency group:

```toml
[project.optional-dependencies]
local-embed = ["sentence-transformers>=2.7,<4.0"]
```

Zero new imports on the critical path when embeddings are disabled.

### Constructor API

```python
# Built-in local model (requires pip install agentdb[local-embed])
db = AgentDB(local_embed=True)

# Pluggable — user supplies any callable: (str) -> list[float]
db = AgentDB(embed_fn=lambda text: my_openai_client.embed(text))

# Default — no embeddings, zero overhead, all existing behaviour unchanged
db = AgentDB()
```

`embed_fn` takes priority over `local_embed`. If neither is set, no `EmbeddingStore` is created, `weight_semantic` stays `0.0`, and all existing code paths are unchanged.

### Data flow — write path

```
AgentDB.write(key, value, ...)
  → MemoryStore.put(record)                   # existing, memories.db
  → EmbeddingStore.put(                       # new, embeddings.db
        table="memories",
        record_id=record.id,
        embedding=provider.embed(_embed_text_for_record(record)),
        working_dirs=record.working_dirs
    )
    [failure: logged and swallowed — write never blocked by embed failure]
```

### Data flow — retrieve path

```
AgentDB.retrieve(query, agent_id, session_id, ...)
  → Query gate: len(query.split()) < min_query_tokens
                OR query.lower() in stop_phrases
      YES → skip all embedding work; weight_semantic stays 0.0
      NO  → query_embedding = provider.embed(query)
  → _store.search(query, limit=50)            # existing keyword candidates
  → EmbeddingStore.get_batch("memories", record_ids)
  → Lazy regen: records with no stored embedding are embedded now and stored
  → Session topic filter: drop candidates where
        cosine(candidate_embedding, session_topic_vector) < session_topic_threshold
        [records without embedding pass through; no record silently dropped for infra reasons]
  → record.embedding = embeddings.get(record.id)  [transient field, not persisted]
  → _ranker.rank(query, candidates, query_embedding=query_embedding)
      [weight_semantic > 0 when provider is active]
  → budget cap, return RetrievalResult
```

### Data flow — session topic anchoring

```
gateway.dispatch("conversation_started", session_id=..., opening_message=...)
  → AgentDB._on_conversation_started(session_id, opening_message)
      → vec = provider.embed(opening_message)
      → EmbeddingStore.put_session_topic(session_id, vec)
```

Both existing adapters (Claude SDK, Hermes) already dispatch `conversation_started`. No adapter changes needed.

### Data flow — archive and lifecycle cleanup

```
skills_api.archive_skill(skill_id, version)
  → SkillStore.archive(skill_id, version)
  → EmbeddingStore.delete("skills", f"{skill_id}:{version}")

LifecycleManager.run_sweep()
  → [existing sweep logic]
  → EmbeddingStore.prune_orphans("memories", active_memory_ids)
  → EmbeddingStore.prune_orphans("skills", active_skill_ids)
```

---

## `embeddings/provider.py`

### Protocol

```python
class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...
    @property
    def dimensions(self) -> int: ...
    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
```

### `FunctionEmbeddingProvider`

Wraps any `Callable[[str], list[float]]`. `embed_batch` calls `embed()` sequentially by default — fast enough for API providers that batch server-side. `dimensions` is probed once via a throwaway embed on first access, then cached.

### `LocalEmbeddingProvider`

Uses `sentence-transformers` with `all-MiniLM-L6-v2`:
- 22M parameters, ~80 MB resident, 384-dimensional output
- **Lazy load** — model not loaded until first `embed()` call; one-time log: `"agentdb: loading local embedding model (first use)"`
- **Class-level singleton** — model stored as class variable; multiple `AgentDB` instances share one copy
- **Class-level `threading.Lock`** — wraps both model load and every `encode()` call; `sentence-transformers` encode is not thread-safe on a shared instance; documented; users needing concurrent throughput should use an API provider via `embed_fn`
- **`normalize_embeddings=True`** — pre-normalises to unit vectors; cosine similarity reduces to dot product

---

## `embeddings/store.py`

### Schema

```sql
-- Records provider identity; mismatch on startup triggers auto-wipe + warning
CREATE TABLE metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)

-- Primary key is (table_name, record_id)
-- embedding stored as struct.pack float32 BLOB (~1.5 KB per record at 384 dims)
-- working_dirs stored as JSON for workspace-awareness during conflict detection
CREATE TABLE embeddings (
    table_name   TEXT NOT NULL,
    record_id    TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    working_dirs TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (table_name, record_id)
)

-- One row per session; used for generic-query filtering
CREATE TABLE session_topics (
    session_id TEXT PRIMARY KEY,
    embedding  BLOB NOT NULL,
    created_at TEXT NOT NULL
)
```

Binary encoding: `struct.pack(f'{n}f', *vector)` / `struct.unpack` — not JSON. 384 floats → 1,536 bytes vs ~4 KB for JSON text.

### Public interface

| Method | Purpose |
|---|---|
| `put(table, record_id, embedding, working_dirs)` | Upsert at write-time |
| `get(table, record_id) → list[float] \| None` | Single lookup; `None` triggers lazy regen by caller |
| `get_batch(table, record_ids) → dict[str, list[float]]` | One `WHERE id IN (...)` query — no N+1 |
| `delete(table, record_id)` | Called at archive-time |
| `prune_orphans(table, valid_ids) → int` | Maintenance sweep; returns rows deleted |
| `put_session_topic(session_id, embedding)` | Stores session anchor vector |
| `get_session_topic(session_id) → list[float] \| None` | Retrieved before session filter |

### `_validate_or_reset(model_name, dimensions)`

Called in `__init__`. Reads `metadata`, compares stored model name and dimensions against the current provider. On mismatch: logs a clear warning, `DELETE FROM embeddings`, `DELETE FROM session_topics`, writes new metadata. Retrieval falls back to `weight_semantic=0` until records are re-embedded lazily — no errors, no data loss on the memory records themselves.

### Lazy-cache property

Embeddings are treated as a pure cache of the record's text content. A `None` from `get()` means "regenerate" — not an error. This makes the store self-healing against:
- Write-time embed failures
- Records written before the embedding layer was added
- Model swaps (all stored vectors wiped; lazy regen rebuilds on access)

---

## Retrieve path — detail

### Query gate

Before any embedding work:

```python
tokens = query.split()
passes_gate = (
    len(tokens) >= cfg["min_query_tokens"]
    and query.strip().lower() not in cfg["generic_query_stop_phrases"]
)
```

If gate fails: `query_embedding = None`, `weight_semantic` effectively 0, fall through to existing keyword ranker. No model call, no cost.

### Embedding hydration

After keyword candidates fetched:

```python
embeddings = embedding_store.get_batch("memories", [r.id for r in candidates])

missing = [r for r in candidates if r.id not in embeddings]
if missing:
    vecs = provider.embed_batch([_embed_text_for_record(r) for r in missing])
    for record, vec in zip(missing, vecs):
        embedding_store.put("memories", record.id, vec, working_dirs=record.working_dirs)
        embeddings[record.id] = vec
```

### Session topic filter

```python
if session_id and session_topic is not None:
    threshold = cfg["session_topic_threshold"]
    candidates = [
        r for r in candidates
        if r.id not in embeddings                              # no embedding → pass through
        or _cosine(embeddings[r.id], session_topic) >= threshold
    ]
```

Threshold is deliberately permissive (`0.35` default) — removes clear cross-domain contamination, not marginal cases. The ranker handles nuance.

### Text representation for embedding

```python
def _embed_text_for_record(r: MemoryRecord) -> str:
    key_readable = r.key.replace("/", " ").strip()
    value_str = (
        json.dumps(r.value) if isinstance(r.value, (dict, list)) else str(r.value)
    )[:2000]
    return f"{key_readable}: {value_str}"

def _embed_text_for_skill(skill: Skill) -> str:
    return f"{skill.name}: {skill.content}"[:2000]
```

Capped at 2000 chars — `all-MiniLM-L6-v2` truncates at 256 tokens; early truncation is predictable.

### `MemoryRecord` transient field

```python
@dataclass
class MemoryRecord:
    # ... existing fields unchanged ...
    embedding: Optional[list[float]] = field(default=None, repr=False)
```

Never persisted. Set on candidates before passing to ranker. The ranker already uses `getattr(record, "embedding", None)` — this makes it explicit.

---

## Write path — skills

Skill embeddings stored with record ID `f"{skill.id}:{skill.version}"` (composite primary key, mirrors `skills.sqlite`).

Embedding happens in `skills_api.create_skill()` and `skills_api.update_skill_version()` — immediately after the store write, same failure-tolerant pattern as memory records.

---

## Ranker weight activation

Profiles all have `semantic=0.0`. When a provider is active, the semantic weight is overlaid at init time via proportional scaling:

```python
def _build_ranker(self, rw: dict) -> RetrievalAlgorithm:
    if self._embedding_provider is not None:
        w = self._config["embeddings"]["semantic_ranker_weight"]  # default 0.20
        scale = 1.0 - w
        return RetrievalAlgorithm(
            weight_provenance=rw["provenance"] * scale,
            weight_recency=rw["recency"] * scale,
            weight_confidence=rw["confidence"] * scale,
            weight_causal=rw["causal"] * scale,
            weight_workspace=rw.get("workspace", 0.0) * scale,
            weight_semantic=w,
        )
    return RetrievalAlgorithm(**rw)
```

Weights always sum to 1.0. Profiles unchanged. Setting `semantic_ranker_weight: 0.0` in config disables semantic ranking even when a provider is configured.

---

## Semantic conflict detection

The existing `ConflictDetector` gets `EmbeddingStore` and `EmbeddingProvider` injected as optional dependencies. When both are present, `detect()` adds a third check after temporal and authority:

- Fetch the new record's embedding (just written)
- `get_batch` for all active records from the same agent (capped at 50 most-recently-written to bound write latency)
- `cosine(new, existing) >= semantic_conflict_threshold` → `ConflictRecord(conflict_type="semantic")`

Default threshold: `0.92`. Configurable in `memory.yaml`.

---

## Config — new keys and defaults

```yaml
embeddings:
  semantic_ranker_weight: 0.20
  semantic_conflict_threshold: 0.92

retrieval:
  min_query_tokens: 3
  session_topic_threshold: 0.35
  generic_query_stop_phrases:
    - "ok"
    - "go ahead"
    - "sounds good"
    - "yes"
    - "no"
    - "sure"
    - "proceed"
    - "continue"
    - "let's review"
    - "sounds great"
```

All keys are optional — safe defaults apply if absent. The `retrieval.*` keys (stop-phrase gate, token count) apply on every retrieve regardless of whether embeddings are active.

---

## Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| Orphaned embeddings (archive without deletion) | High | `delete()` at archive-time; `prune_orphans()` in lifecycle sweep |
| Cold-start latency (model load 1–3s) | High | Lazy load; one-time log message |
| Write amplification on bulk ingestion | High | `embed_batch()` on all providers; single encode call for N records |
| Thread safety under concurrent inference | High | Class-level `threading.Lock` on `LocalEmbeddingProvider` |
| Generic query context pollution | High | Query gate (token count + stop-phrase list) + session topic filter |
| Cross-project context contamination | High | Session topic filter (cosine threshold per session) |
| Model swap invalidates all vectors silently | Medium | Metadata table detects mismatch at startup; auto-wipe + warn; lazy rebuild |
| Semantic conflict threshold tuning | Medium | Configurable in `memory.yaml`; default 0.92 is conservative |
| Memory footprint (~90 MB for local model) | Medium | Documented; API-based providers via `embed_fn` have no resident footprint |
| Storage growth over time | Low | BLOB encoding (1.5 KB/record); `prune_orphans()` at sweep |

---

## Testing strategy

### Test double

All unit tests use `AgentDB(embed_fn=controlled_embed)` where `controlled_embed` maps known phrases to pre-set unit vectors. No model download in CI.

`LocalEmbeddingProvider` tests live in `tests/integration/`, marked `@pytest.mark.slow`, skipped unless `AGENTDB_SLOW_TESTS=1`.

### Test files

| File | Tests | Focus |
|---|---|---|
| `tests/unit/test_embedding_provider.py` | ~8 | Protocol conformance, singleton, lazy load, ImportError |
| `tests/unit/test_embedding_store.py` | ~14 | Round-trips, batch, delete, prune, session topic, model-swap wipe |
| `tests/unit/test_retrieve_with_embeddings.py` | ~12 | Query gate, lazy regen, session filter, ranker handoff |
| `tests/unit/test_semantic_conflict.py` | ~6 | Conflict detected/not-detected, threshold config, agent scoping |
| `tests/unit/test_ranker_weight_activation.py` | ~4 | Weights sum to 1.0, semantic weight configurable |
| `tests/integration/test_local_embedding_provider.py` | ~3 | Real model loads, 384-dim output, batch consistency |

**Total: ~44 unit tests + 3 integration tests**

---

## What this unblocks

| Feature | Status after this spec |
|---|---|
| Semantic retrieval (`weight_semantic > 0`) | Unblocked |
| Semantic conflict detection | Unblocked |
| Skill reconciliation pipeline (Task 13) | Unblocked — skill embeddings available for draft comparison |
| `SkillStore.search()` semantic upgrade | Unblocked — `controlled_embed` fixture covers skill search tests |
