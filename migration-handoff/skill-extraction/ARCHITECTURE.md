# ARCHITECTURE — Skill Subsystem as Removed from AgentDB

This describes how skill functionality was structured inside AgentDB at the
baseline commit, for a future independent package. **No new package design is
proposed here** — only the shape of what was extracted.

## Layering (as it existed)

```
                       ┌───────────────────────────────────────────┐
                       │             AgentDB (client.py)            │
                       │  self.skills ── SkillStore                  │
                       │  self.skills_api ── SkillsAPI (facade)      │
                       │  _check_skill_conflict() ── write() hook    │
                       │  untracked_skill_candidate ── auto-propose  │
                       └───────────────┬───────────────────────────┘
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        │                              │                              │
 ┌──────▼──────┐               ┌───────▼────────┐              ┌──────▼───────┐
 │ Gateway     │               │ Notifications  │              │ Adapters     │
 │ propose/    │               │ skill_stale /  │              │ inject_skills│
 │ approve/    │               │ skill_review / │              │ _build_skills│
 │ reject /    │               │ untracked_...  │              │ _block /     │
 │ auto_prop.  │               └────────────────┘              │ hermes block │
 └──────┬──────┘                                               └──────────────┘
        │  approve() calls skills_api.create_skill()
        │
 ┌──────▼─────────────────────────────────────────────────────────────┐
 │ Persistence: skills.sqlite (separate file)                         │
 │   skills(id,version) primary key; skill_locks; health scoring       │
 │ Vector embeddings: embeddings.db namespace "skills", key id:version │
 └────────────────────────────────────────────────────────────────────┘
```

## Key responsibilities (extracted)

1. **Skill registry / versioning** — `SkillStore`: `(id, version)` primary key,
   one active version, `SkillConflict` when ambiguous. Human-authored only.
2. **Health & staleness** — `_compute_health` blends `validation_score` with an
   age-based usage proxy (staleness from `updated_at`, not `last_retrieved`);
   fires `on_stale_crossing` once below 0.6.
3. **Proposal / approval workflow** — Gateway stored proposals as
   `proposal/<uuid>` memory records (a coupling to the memory store that the
   future package must not inherit); approval promoted a proposal to a skill.
4. **Skill-content injection** — adapters rendered skill text into context.
   *This is the behavior removed from AgentDB (contract P-10).*
5. **Skill–memory conflict signaling** — on memory write, keyword + semantic
   overlap with scoped skills dispatched review events.
6. **Evolution bridges** — WikiSkill/SkillOpt adapters + advisory locks for
   concurrent skill evolution.
7. **Notifications** — channel routing for skill lifecycle events.

## Couplings into AgentDB internals (break these in the new package)

- Proposals stored as **AgentDB memory records** (`proposal/` key prefix) and
  `read()`/`delete_by_key()` on the memory store.
- `approve()` calls `SkillsAPI.create_skill` directly.
- `consolidate()` computed skill-governance coverage and dispatched
  `untracked_skill_candidate` (i.e. skill logic bled into the consolidation path).
- `SystemStats` read skill counts from a second DB file.
- Skill embeddings shared the `EmbeddingStore`/namespace machinery.
- `Skill` model lived in AgentDB `core/models.py` and was exported at package root.

## What AgentDB keeps (memory is NOT affected)

Memory ranking (`retrieval/algorithm.py`), candidate discovery, consolidation
algorithm (minus skill-candidate dispatch), provenance, permissions, audit,
lifecycle reinforcement/expiry — all untouched by this extraction.
