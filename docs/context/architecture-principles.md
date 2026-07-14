# AgentDB Architecture & Design Principles

This is the single canonical source for AgentDB's architecture pitch and numbered design principles. `AGENTS.md`, `README.md`, and the superpowers plan each reference this document rather than repeating its content — if you're editing one of the principles below, this is the only file that should change; the others just point here.

## What AgentDB is

A governed memory layer that keeps agent memory actionable. It governs what memory is trusted, keeps relevant memory assets in sync, and injects the right context or instructions when an agent is about to act. Every write is validated before it lands. Every read returns a governed context package. The boundary is enforced at the memory layer — not by convention, not by prompt instructions, not by hoping agents behave.

AgentDB complements existing runtime memory instead of replacing it by default; its job is to make memory more trustworthy, more relevant, and more actionable. The core product loop is: track relevant memory assets, reconcile them proactively, rank what matters, and inject compact context or instructions before or during action.

## Design Principles

These principles define the implementation constraints for all AgentDB work.

1. **Database-first identity.** The product is a memory database with governance built in — not a governance framework with storage attached. API surface mirrors database conventions: `SELECT`-style retrieval, `GRANT`/`REVOKE`-style permissions, system tables for introspection.

1a. **Actionability over asset accumulation.** AgentDB should not add memory-related overhead unless it improves the timing, quality, or trustworthiness of context supplied to an agent action.

2. **Framework-agnostic backend interface first.** AgentDB defines a generic pluggable backend interface (`AgentDBBackend`). Frameworks adopt it — AgentDB does not adapt to them. deepagents is the flagship integration and named first-class example, but the interface is not deepagents-specific.

3. **Skills are database objects, not prompt files.** Skills (versioned instruction records) are first-class database primitives with computed health scores. A skill with a health score that degrades when underlying knowledge drifts is a database feature — not prompt management.

4. **Algorithm as foundation, not feature.** The multi-signal ranking algorithm (provenance weight + causal WAL signal) is the technical moat. It is not marketed as a differentiator — it is what makes the database worth using.

4a. **Algorithm + skills blend.** The ranking algorithm and governed skills are a single development foundation: memory records capture facts and prior outcomes, the algorithm ranks what matters now, and skills add maintained instructions the runtime can inject with that context.

5. **Three providers at launch.** Anthropic, OpenAI, Google (Vertex AI Agent Engine MemoryBankService). All three ship in Phase 3.

6. **Org knowledge is a memory tier, not an expiry policy.** Institutional knowledge (team structures, product names, decision logs) lives under `memory/org/` prefix with its own retrieval priority. It is not an ExpiryPolicy enum value — ExpiryPolicy governs TTL, not knowledge category.

7. **WAL split.** Audit trail (append-only, immutable) and the notifier/event bus (subscriptions, delivery) are separate subsystems conceptually — but durability and replay are not the default for the notifier. Start with a live, in-memory dispatcher (register a handler for a trigger, dispatch calls it — `AgentDB.register_reconciliation_handler`/`dispatch_reconciliation_trigger` already is this mechanism); add a shared sequence counter, persistence, and replay-from-sequence only for a specific consumer that would lose real value from a missed notification while the process was down (e.g., a human-review queue). During testing, and for any capability where a missed notification costs nothing (a dashboard refresh, a debug log, a same-process reconciliation callback), a live-only dispatcher is not a stopgap on the way to something durable — it is the correct, permanent design. Speculatively building durability nobody needs is exactly the overhead Principle 1a warns against.

8. **Conflict detection is either real or silent.** Phase 1 ships temporal + authority conflict detection (exact key + origin comparison). Semantic conflict detection is not shipped until embeddings are active (Phase 4). The system never pretends to detect semantic conflicts it cannot detect.

9. **Budget manager safety fix.** Conflict flags are assembled BEFORE human-approved memories, not after. An agent must know a memory is contested before it receives the memory content.

10. **Narrow-first delivery.** Prioritize proving the governed retrieval/improvement loop before broad integration breadth. Provider ingestion, MCP surface expansion, and adapter hardening should follow only after the core loop has measurable quality signals and stable tests.

11. **Memory assets are subordinate.** Embeddings, JSONL transcripts, databases, graphs, and derived indexes are productively in scope only when they improve recall, reduce conflicting guidance, infer reusable instructions, or otherwise raise action accuracy.

12. **Adapter enforcement must be a checkable fact, not an API surface.** Every adapter must document the exact runtime point(s) where AgentDB's context/governance actually executes inside that framework's own control flow — not merely that a callable exists. A capability is only "done" when a test proves it changes what the agent sees or is allowed to do, driven by the framework's own execution path, not by whether application code happened to call AgentDB correctly. This splits into two distinct types, each with its own bar:

    - **12a. Real-time enforcement** — synchronous, wired into the framework's own control flow (e.g. Claude SDK's `PreToolUse` hook + turn-start `prepare_turn`), so governed context reaches the agent before or during its action regardless of whether the agent "remembers" to ask for it. Because every current retrieval call is a live query (nothing is cached), a real-time hook already sees the freshest possible state on every firing — no event bus or push mechanism is needed for this type. Done means: a test proves the framework's own hook mechanism triggers it, and that it changes the context/instructions reaching the agent at that checkpoint.

    - **12b. Async integrity checks** — decoupled from any single agent's turn: conflict-queue notifications, corpus-wide staleness/lifecycle sweeps (Task 9), reconciliation triggers (`file_modified`, `cron`), the skill draft reconciliation pipeline (Task 13 — proposing/flagging is 12b; the actual commit to a skill is write-time governance, a separate, more restrictive gate), cross-agent "something you rely on just changed" notices. The concrete mechanism is `AgentDB.register_reconciliation_handler(trigger, handler)` / `dispatch_reconciliation_trigger(trigger, **kwargs)` (see Design Principle 7) — a live, in-memory register/dispatch pair already implemented, not a new subsystem to build. What's missing today is dispatch call sites for new trigger types (e.g. `conflict_detected`, `skill_stale`) at the places those events actually happen, and consumers (an MCP tool, a Slack/Telegram notifier in a host app) registering handlers against them. These exist to catch what real-time enforcement structurally cannot: a different agent, in a different session, changing something the current agent is relying on, with no upcoming hook checkpoint to pick it up. Done means: it runs independent of any agent's turn, it's auditable (an audit-log entry, not silent), and it has a named consumer (a queue, a dispatcher, a notification target) registered via this mechanism. Publishing an event with no subscriber wired to an enforcement point is telemetry, not enforcement.

    Every adapter, and every new memory-freshness capability, must state which of these two types it is and meet that type's bar before being marked done.

13. **Surface consolidation over API proliferation.** When new capabilities serve the same consumer type — human reviewer, framework adapter, lifecycle governor, provider sync — they belong on a named sub-surface rather than as scattered top-level methods on `AgentDB`. A proliferation of top-level methods serving the same consumer is the signal a sub-surface is needed.

    The sub-surface is a property on `AgentDB`, not a separate process or package — Principle 1's database-first identity still applies; sub-surfaces are namespaces, not architectural seams. Standard shape: a lightweight class initialized once in `AgentDB.__init__`, exposed via a property, with `AgentDB` keeping backward-compatible pass-through aliases on any methods that were public before the sub-surface existed.

    Confirmed sub-surfaces and their owners:

    | Sub-surface | Consumer | Status |
    |---|---|---|
    | `db.gateway` | Reconciliation dispatcher + proposal workflow + conflict queue + adapter base | Task 16 — not started |
    | `db.lifecycle` | Archival sweep + outcome reinforcement | Task 9 — not started |
    | `db.providers` | Anthropic / OpenAI / Google provider sync | Task 10 — not started |
    | `db.system` | Introspection tables | Implemented (partial) |
    | `db.skills_api` | Skill CRUD | Implemented |
    | `db.audit_log` | Audit trail replay | Implemented |

    New capabilities that span more than one method and serve a distinct consumer must land on the matching sub-surface or open a discussion about adding one — not on the `AgentDB` top-level namespace.
