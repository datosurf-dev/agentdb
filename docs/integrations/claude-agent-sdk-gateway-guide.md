# Claude Agent SDK — Gateway & Budgeted Retrieval Guide

Lessons from a real production integration (a Telegram/Slack assistant) of the
Claude SDK adapter's `memory_budget_tokens` and `db.gateway` conflict-queue
wiring — the pieces added on top of the base adapter covered in
`model-and-runtime-wiring.md` section A.1. Read that first for basic setup.

## `memory_budget_tokens`: start loose, tighten from data

`retrieve(budget_tokens=...)` and the profile's `memory_budget_chars` are two
different truncation mechanisms — token-based at retrieval time, char-based
at render time in `_build_memory_context`. If you set the token budget to
roughly match what the char budget already implies (`memory_budget_chars /
4`), you risk the token filter becoming the *actual* binding constraint
instead of the char one, changing what content reaches the model in ways
that are hard to predict (the two truncation points don't measure the same
thing — one operates on raw record values, the other on formatted
`"- key: value"` lines).

**Recommendation:** set `memory_budget_tokens` *looser* than the
char-budget-implied estimate at first (e.g. 2x headroom). This keeps
existing render behavior unchanged while making `retrieval.truncated` /
`retrieval.token_estimate` meaningful instead of permanently `False`/full.
Tighten later based on observed values, not a guess — same philosophy as the
`full_storage` → `budget_optimized` migration path already documented in
`model-and-runtime-wiring.md`.

## `agent_id` is a public property on `GatewayAdapter`

`GatewayAdapter` (and therefore both `AgentDBClaudeMiddleware` and
`AgentDBHermesProvider`) exposes `adapter.agent_id` directly, so a downstream
app can scope its own gateway calls without reaching into `_agent_id` or
duplicating the id in a module-level global.

## Conflict queue

`db.gateway.conflict_queue(resolved=False)` returns pending conflict records;
`adapter.pending_conflicts()` is the adapter-scoped convenience wrapper. Route
these to whichever review surface your deployment uses (Slack/Telegram, a
dashboard, etc.) — the gateway dispatcher is in-process and not durable.

## Test double

`agentdb.testing.FakeAgentDBClaudeMiddleware` ships with the package for
downstream apps testing their own wiring. It covers the curated constructor
params (`db`, `agent_id`) — not turn behavior, which needs a real
tmp-path-backed `AgentDBClaudeMiddleware`.
`tests/unit/test_testing_fakes.py` asserts those curated params still exist on
the real class via `inspect.signature`, so a signature change fails in *this*
repo's suite, not silently downstream.

## Related

- `docs/integrations/model-and-runtime-wiring.md` — basic adapter wiring
- `docs/integrations/openclaw.md`, `hermes.md` — other adapters' design briefs
- `docs/context/architecture-principles.md` — Principle 13 (surface
  consolidation, which is what `db.gateway` implements) and Principle 12
  (the enforcement bar every adapter capability is held to)
