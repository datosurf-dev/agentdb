# Open Decisions

**Release:** boundary-cleanup & retrieval-correctness hardening
**Date:** 2026-11-20
**Spec:** `docs/spec/BOUNDARY_CLEANUP.md` §2, §7, §10
**Purpose:** Record decisions that require human authority, involve irreversible
migrations, or change public API/product policy. None are silently chosen here.

Legend: ⛔ open (needs owner) · 🟡 currently safe-approximated · ✅ resolved

---

## OD-1 — Cross-key contradictory-value consolidation ⛔ open

**What:** Records in one key *domain* but different qualifier keys with mutually
exclusive values (e.g. `db-version-note`="use postgres 15" vs
`db-version-update`="use postgres 16") are currently **merged** into one
canonical. Contradiction detection only fires for **identical keys**.

**What holds today (safe bound):** sources preserved intact and recoverable;
lineage keeps both IDs; no trust escalation; no deletion. The *canonical* merely
presents one contested value.

**Why it's open:** distinguishing "near-duplicate partial facts" (which the
existing dedup test expects to merge) from "true contradictions" deterministically
requires a policy — e.g. value-token conflict detection, temporal scope, or
manual review. Product Contract **P-04** ("contradictory ... facts must remain
distinguishable") and spec §6 require a decision. This is a product policy, not
a bug to fix silently.

**Alternatives:**
- **(a) Keep as-is** — merge, sources recoverable; contradiction not surfaced.
- **(b) Deterministic value-conflict guard** — within a domain, if values differ
  only by a numeric/enumerable token in the same slot (e.g. version numbers),
  treat as temporal conflict → do not merge. Adds heuristic; needs acceptance tests.
- **(c) Same-key-only merges** — restrict merging to identical keys. Safer but
  would stop merging the qualifier-suffixed duplicates the existing suite expects.
- **(d) Advisory flag** — merge but emit a `conflict/` record flagging the values.

**Recommendation:** (b) as a narrowly-scoped, deterministic, test-backed guard —
*after* explicit approval. Until then, behavior is fenced by
`test_d6_cross_key_contradiction_should_not_merge` (`xfail(strict=True)`).

---

## OD-2 — Trust boundary: caller-supplied `origin="human_approved"` ⛔ open

**What:** `write()`, `write_compaction_summary()`, and `checkpoint()` honor an
explicit `origin="human_approved"` **without independent verification**. Any
caller can assert human authority.

**Context:** P-01 ("trust must be earned") is satisfied only in the sense that
*machine-produced* summaries no longer *default* to `human_approved`; the caller
may still *claim* it. The code documents this ("only a verified human action may
pass `origin=human_approved`") but does not enforce it.

**Why it's open:** enforcing verification requires an authorization mechanism —
explicitly a "new authorization architecture" the spec forbids introducing
here. Spec §7: "If the public API permits callers to claim human approval
without verification, report this as a trust-boundary decision."

**Alternatives:**
- **(a) Document only** (current) — trust the caller; downstream frameworks
  mediate approval.
- **(b) Verified approval token** — require an approval handle minted by a
  trusted human-action path before allowing `human_approved`. New auth surface.
- **(c) Separate approved-write API** — a distinct method gated by the
  `PermissionRouter`, leaving `write()` machine-origin-only.

**Recommendation:** (a) for this release (documented + tested as a visible
boundary in `test_provenance_regression.py::test_e_trust_boundary_...`).
Mechanism choice (b/c) needs owner sign-off; it is a security design decision.

---

## OD-3 — Read authorization (ADB-11) ⛔ open (documented, not changed)

**What:** `read()` and `retrieve()` do **not** enforce `can_read`. The
`PermissionRouter` checks writes/deletes only, and only when `permissions_path`
is supplied.

**Status:** Documented accurately (Product Contract P-11). No silent security
change made. Whether to enforce read isolation is a scoped security decision for
a separate, clearly-disclosed release.

**Recommendation:** keep documentation-accurate; schedule as a dedicated
security release with its own tests/approval.

---

## OD-4 — Release versioning for this change set ⛔ open (release manager)

**What:** Changes are a backward-compatible bug fix (determinism) plus removal
of a dead skill param with a deprecation shim (no hard break).

**Alternatives:** PATCH `0.5.0 → 0.5.1` (recommended, per pre-1.0 policy) vs
fold into an unreleased `0.6.0`. Version left to the release manager; CHANGELOG
has an `[Unreleased]` section.

---

## OD-5 — Removal timeline for the `skill_candidate_threshold` shim ⛔ open

**What:** `AgentDB.consolidate()` still accepts `skill_candidate_threshold` and
warns (DeprecationWarning). Fully removing it is a (minor) API break.

**Alternatives:** remove now (breaking) vs remove at next major (current, safe).
**Recommendation:** remove at the next versioned breaking point; keep the shim
until then.

---

## OD-6 — Concurrent consolidation creates duplicate canonicals ⛔ open

**What:** N concurrent `consolidate()` calls racing over the same cluster each
read the sources as still-active (before any run archives them) and each write a
`derived_canonical`, producing **N canonicals for the same source set**. A single
sequential repeat is safe (the second run sees archived sources and creates
nothing); only true concurrency violates idempotence.

**What holds:** source records are never destroyed; values/lineage intact. The
defect is duplicate derived records + double `records_archived` accounting.

**Why it's open:** a correct fix requires choosing a concurrency-control design
with storage implications — exactly a "report the alternatives" decision, not a
silent redesign:
- **(a) Unique index** on a lineage fingerprint (e.g. hash of sorted source ids)
  in `memories`, with `INSERT OR IGNORE`. Rejects true duplicates but needs an
  additive migration and a lineage-fingerprint column.
- **(b) Consolidation advisory lock** (per-agent row in `consolidation_state`
  acquired with `BEGIN IMMEDIATE`). Serializes runs; no schema change beyond
  existing table.
- **(c) Check-and-insert idempotency** — re-read source state inside the write
  transaction and skip if an identical canonical already exists. Cheapest, but
  must be airtight against the read-then-write race.
- **(d) Document as unsupported** — consolidate() is not concurrency-safe;
  callers must serialize. Zero code change.

**Recommendation:** (b) or (c) after explicit approval — they preserve the public
API and add no user-visible schema. Until then, fenced by
`test_d10_concurrent_consolidation_no_duplicates` (`xfail(strict=True)`).

---

## Resolved in this release ✅

- **OD-R1 Retrieval tie-break determinism** — fixed (id-based secondary sort;
  no weight change). See `TEST_RESULTS.md`.
- **OD-R2 Dead `skill_candidate_threshold` on `LifecycleManager`** — removed;
  compat shim on public `AgentDB.consolidate()`.
