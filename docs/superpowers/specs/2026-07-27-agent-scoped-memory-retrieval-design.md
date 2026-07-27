# Opt-in agent-scoped memory retrieval

## Context

`slack-assistant` (a separate repo, `/Users/jm/Documents/github/slack-assistant`) is adding
support for running multiple independent Telegram bot identities as separate processes,
each with its own `agent_id` (e.g. `telegram_assistant_personal`, `telegram_assistant_work`),
sharing one `agentdb.db` file — the same pattern already used to keep the existing Slack and
Telegram bots' memory attributed separately today.

While designing that, we traced what `agent_id` actually does on the retrieval path and found
it does **not** isolate memory between agents:

- `AgentDB.retrieve()` (`src/agentdb/interface/client.py:660`) calls
  `self._store.search(query, limit=...)` with no agent filter at all, then
  `self._ranker.rank(query, candidates, agent_id=agent_id, ...)`. `MultiSignalRanker.rank()`
  (`src/agentdb/retrieval/algorithm.py:89`) accepts `agent_id` but never references it in the
  method body — it has no effect on filtering or scoring.
- `AgentDB.retrieve_by_entities()` (`client.py:785`) has the same shape: calls
  `self._store.search_by_entities(entities)` with no agent filter, uses `agent_id` only to
  merge in `skills.get_for_agent(agent_id)`.
- This matches the documented data flow in `docs/context/database-schema.md`: `retrieve()`
  is described as searching all memory rows and merging in per-agent skills — agent_id is
  documented as "which agent wrote this" (attribution), not an access boundary.
- By contrast, `Skill` has an explicit `scope: list — agent_ids this skill applies to` field,
  and `skills.get_for_agent()` does filter by it. Skills have a deliberate, explicit
  cross-agent sharing mechanism; `MemoryRecord` has no equivalent, because it was never
  meant to need one — it's designed as one shared, governed memory pool per AgentDB
  instance, with `agent_id`/origin/confidence used for trust-weighting and outcome
  attribution, not isolation.
- `MemoryStore.list_by_agent()` (`src/agentdb/core/store.py:210`) already does hard
  agent-filtering (`r.agent_id == agent_id and not r.archived`) — just not on the retrieval
  path used by `prepare_turn`/`prepare_turn_async`.

**This is working as designed, not a bug.** Today's Slack and Telegram bots already share
one memory pool across their distinct agent_ids, and that hasn't been flagged as a problem —
cross-agent sharing with provenance-based trust weighting (`human_approved` >
`provider_ingested` > `agent_inferred`) looks like the intended value of a "governed memory"
system: agents learn from each other's human-approved memories, weighted by trust.

Multiple Telegram bot identities change the calculus: they may represent genuinely separate
contexts (e.g. a personal project bot and a work bot) where cross-contamination of episodic
memory is undesirable, even though skill-sharing or shared-pool behavior might be exactly
right for other consumers (e.g. Slack + Telegram acting as one team assistant today). So this
needs to be a per-consumer choice, not a global behavior change.

## Goal

Add an **opt-in, off-by-default** way for a caller to get hard agent-isolated retrieval,
without changing behavior for any existing caller that doesn't ask for it.

## Non-goals

- Not changing the default (shared-pool) behavior for any existing consumer.
- Not adding a general multi-agent "scope list" to `MemoryRecord` (unlike skills, there's no
  known need yet for a memory to be shared with an explicit subset of agents rather than
  either fully shared or fully private to its author). If that need shows up later, it's a
  separate design.
- Not deciding which `slack-assistant` bots actually turn this on — that policy lives in
  `slack-assistant`'s own bot-telegram multi-bot design, a separate spec in that repo. This
  spec only builds the capability and the plumbing hook that design will use.

## Design

### 1. `AgentDB.retrieve()` / `retrieve_by_entities()` — `isolate_by_agent` parameter

Both gain a new keyword-only parameter:

```python
def retrieve(
    self,
    query: str,
    agent_id: str,
    limit: int = 5,
    session_id: Optional[str] = None,
    external_context: Optional[list[dict]] = None,
    budget_tokens: Optional[int] = None,
    isolate_by_agent: bool = False,
) -> RetrievalResult:
```

When `True`, candidates are hard-filtered to `record.agent_id == agent_id` before ranking —
reusing the exact filter `MemoryStore.list_by_agent()` already applies elsewhere, just
applied at the point `retrieve()` currently skips it:

```python
candidates = self._store.search(query, limit=max(limit, 50))
if isolate_by_agent:
    candidates = [r for r in candidates if r.agent_id == agent_id]
```

Same shape for `retrieve_by_entities()`:

```python
def retrieve_by_entities(
    self,
    entities: list[str],
    agent_id: str,
    session_id: Optional[str] = None,
    isolate_by_agent: bool = False,
) -> RetrievalResult:
    records = self._store.search_by_entities(entities)
    if isolate_by_agent:
        records = [r for r in records if r.agent_id == agent_id]
    ...
```

Default `False` on both — every existing call site (including anything outside
`AgentDBClaudeMiddleware`) keeps exactly today's shared-pool behavior with no code changes
required on its part.

Skills behavior (`skills.get_for_agent(agent_id)`) is unaffected either way — it's already
scoped, and stays scoped.

### 2. `AgentDBClaudeMiddleware` — thread the flag through construction

`AgentDBClaudeMiddleware.__init__()` gains `isolate_by_agent: bool = False`, stored as
`self._isolate_by_agent`. `prepare_turn()` (`adapters/claude_agent_sdk.py`, the method
`prepare_turn_async` delegates to) passes it into the `self._db.retrieve(...)` call it
already makes:

```python
retrieval = self._db.retrieve(
    query=user_query,
    agent_id=self._agent_id,
    limit=self.profile.retrieve_limit,
    external_context=external_context,
    session_id=session_id,
    budget_tokens=self.profile.memory_budget_tokens,
    isolate_by_agent=self._isolate_by_agent,
)
```

This is a per-middleware-instance setting, not a global one. Each consumer process
constructs its own `AgentDBClaudeMiddleware` instance already (one per bot process in
`slack-assistant`'s design), so this gives per-process control for free — no per-call
threading needed beyond what already exists.

### 3. Consumer-side hook (implemented in `slack-assistant`, described here for context)

`slack-assistant`'s `PlatformConfig` (in `runner_claude.py`) will gain a field, e.g.
`isolate_memory: bool = False`, and `init_agentdb()` will pass it through:

```python
_agentdb_middleware = AgentDBClaudeMiddleware(
    db=db,
    agent_id=platform.agent_id,
    profile="research",
    memory_budget_tokens=1000,
    isolate_by_agent=platform.isolate_memory,
)
```

Deciding which bots set `isolate_memory=True` (e.g. every named `--bot`, or independently
configurable per bot) is out of scope for this spec — that's the `slack-assistant`
bot-telegram multi-bot design's job. This spec only needs to land the parameter on
`PlatformConfig`/`init_agentdb()` so that design has something to set.

## Testing

- `test_algorithm.py`: existing tests construct records/rank calls that already implicitly
  belong to one agent (`"a1"`) and never assert on cross-agent behavior — unaffected by this
  change. Add cases: same-agent record retrieved with `isolate_by_agent=True`; different-agent
  record excluded with `isolate_by_agent=True`; different-agent record included (today's
  behavior, unchanged) with `isolate_by_agent=False`/default.
- `client.py` retrieve/retrieve_by_entities: add a test with two agent_ids writing records,
  confirming `isolate_by_agent=True` returns only the calling agent's records for both
  methods, and `isolate_by_agent=False` (or omitted) returns the current mixed-pool result.
- `AgentDBClaudeMiddleware`: add a test confirming `isolate_by_agent` passed at construction
  reaches the `db.retrieve()` call unchanged.

## Risks / open questions

- None identified that block implementation — this is a narrow, additive, default-preserving
  change. The one judgment call worth surfacing to a human reviewer before merge: confirming
  the "shared governed pool by design" reading of the current code is correct and not itself
  a latent bug nobody had noticed yet. This spec proceeds on the evidence in `Context` above
  (the documented schema, the skills/memory asymmetry, existing shared Slack+Telegram usage)
  but wasn't confirmed against original design notes beyond what's in this repo's own docs.
