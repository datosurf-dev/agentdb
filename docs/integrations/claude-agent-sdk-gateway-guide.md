# Claude Agent SDK — Gateway & Budgeted Retrieval Guide

This captures lessons from a real production integration (a Telegram/Slack
assistant) of the Claude SDK adapter's `memory_budget_tokens` and `db.gateway`
wiring (conflict queue, proposal workflow) — the pieces added on top of the
base adapter covered in `model-and-runtime-wiring.md` section A.1. Read that
one first for basic setup; this one is about what broke or needed a judgment
call the first time these specific capabilities got used for real.

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

## Permission gotcha: `proposal/` write access isn't automatic

`gateway.propose()` writes to `proposal/<id>` through the normal `write()`
path, gated by the standard permission router like any other key. **This
does not surface until the first call** — if your `permissions.yaml` doesn't
grant the calling `agent_id` write access under `proposal/`, `propose_skill()`
raises `PermissionDenied` the first time anyone tries to use it, in
production, with no earlier warning.

**Recommendation:** any agent_id that will call `propose_skill()` needs an
explicit `proposal/` entry under `can_write` — treat it the same as you would
`memory/working/` or any other prefix the agent needs. Don't assume it's
covered by an existing grant.

## `agent_id` is the natural default scope for chat-originated proposals

Chat commands don't have a natural user-facing "scope" concept to expose —
asking "what scope should this proposal have?" in a Telegram/Slack command
is not a good UX. **Recommendation:** default `scope=[adapter.agent_id]`
rather than exposing scope as a parameter, and use the new public `agent_id`
property (below) rather than threading the agent_id through your own
parallel global.

## New: `agent_id` is now a public property on `GatewayAdapter`

Previously only `_agent_id` (private) existed. A downstream app needing to
scope its own gateway calls had to either reach into the underscore-prefixed
attribute or duplicate the agent_id in its own module-level global — which is
what the reference integration did before this was added. `GatewayAdapter`
(and therefore both `AgentDBClaudeMiddleware` and `AgentDBHermesProvider`)
now expose `adapter.agent_id` directly; no workaround needed.

## Authorization tiers: propose vs. approve/reject are not the same risk

`propose_skill()` only drafts — reversible, no effect on what the agent
actually sees until approved. `approve_proposal()` promotes a draft into a
skill the agent will be served on every future turn; `reject_proposal()`
discards it. These are not the same risk tier.

**Recommendation:** gate `approve`/`reject` behind a separate, fail-closed
allowlist distinct from whatever general access control already gates chat
access to the bot — general chat access implies "can talk to the agent," not
"can promote arbitrary content into what the agent is told to do." Read-only
listing (`pending_conflicts()`, `list_proposals()`) and drafting
(`propose_skill()`) can reasonably stay on the general allowlist; committing
a change to the skill corpus should not.

## Friction points — resolved 2026-07-14

1. **`gateway.propose()`'s internal write no longer requires a manual
   permission grant.** `proposal/` is now a framework-managed prefix
   (`governance/permission_router.py`'s `FRAMEWORK_MANAGED_WRITE_PREFIXES`) —
   writable by default for any agent, checked after `cannot_write` (still a
   valid escape hatch for a deployment that wants to lock it down) and before
   `can_write`/`default_policy`. This is a default, not an unconditional
   bypass: unlike Hermes's `on_memory_write`, which skips permission checking
   entirely because it's mirroring a write that already happened elsewhere,
   `propose()` is the *actual* write — an explicit `cannot_write: ["proposal/"]`
   still blocks it. Regression guard: `tests/unit/test_gateway.py` now
   includes a test with a real `permissions_path` configured and zero
   explicit grant — the existing gateway tests use no permissions file at
   all, which is exactly why this class of bug wasn't caught earlier.
2. **`agentdb.testing.FakeAgentDBClaudeMiddleware` now ships with the
   package.** Covers the curated params relevant to gateway/proposal testing
   (`db`, `agent_id`, `profile`, `memory_budget_tokens`) — not
   `retrieve_limit`/`memory_budget_chars`/`conversation_started_callback`,
   which only matter for turn behavior the fake doesn't implement (use a real
   tmp-path-backed `AgentDBClaudeMiddleware` for that instead).
   `tests/unit/test_testing_fakes.py` asserts those curated params still
   exist on the real class via `inspect.signature`, so a future signature
   change fails in *this* repo's suite, not silently downstream.

## Related

- `docs/integrations/model-and-runtime-wiring.md` — basic adapter wiring
- `docs/integrations/openclaw.md`, `hermes.md` — other adapters' design briefs
- `docs/context/architecture-principles.md` — Principle 13 (surface
  consolidation, which is what `db.gateway` implements) and Principle 12
  (the enforcement bar every adapter capability is held to)
