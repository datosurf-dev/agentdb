# Unified profile configuration (single YAML, no exposed adapter arguments)

## Context

Investigating a real bug (`profile="research"` passed to `AgentDBClaudeMiddleware` silently
degrading to `budget_optimized`) surfaced that AgentDB has **two parallel, disconnected
configuration mechanisms**:

1. `AgentDB.__init__`'s own YAML-driven `mode`/`profiles`/`extends` system
   (`_resolve_config()` in `client.py`), which resolves `lifecycle`/`retrieval`/`capture`
   settings from `memory.yaml` — but only `ranker_weights`, `lifecycle`, and
   `capture.tracking_max_files_per_directory` are actually wired into anything.
   `retrieval.retrieve_limit`, `retrieval.memory_budget_chars`, and
   `retrieval.conflict_threshold` are computed into `self.config` and never read again anywhere
   in the codebase.
2. `AgentDBClaudeMiddleware`'s own hardcoded `ClaudeAdapterProfile` /
   `FULL_STORAGE_PROFILE` / `BUDGET_OPTIMIZED_PROFILE` constants (`adapters/claude_agent_sdk.py`),
   selected via a `profile: str` constructor kwarg that only recognizes the literal
   `"full_storage"` — anything else, including the very real mode name `"research"`, silently
   falls back to `budget_optimized`, a 10x smaller retrieval budget than `"research"` mode
   actually specifies (`retrieve_limit: 50` vs `5`).

This spec unifies these into one system: a single YAML file, one resolution mechanism, and no
adapter-level constructor arguments that could drift from or conflict with it. It also adds
real configurability for how retrieved memory gets formatted for injection (previously fully
hardcoded), and makes agent-level memory isolation (`isolate_by_agent`, added in a prior spec)
a property of the YAML rather than a second, Python-side source of truth.

## Goals

- One YAML file is the only place profile/retrieval/injection/isolation settings live. No
  Python constructor kwarg on any adapter can override or duplicate what the YAML says.
- Keep the existing "built-in mode + optional overrides" ergonomics, but collapse the
  `mode:`/`profiles:` split into one concept: `profile:`.
- Support per-agent overrides (including switching to an entirely different base profile)
  within the same file, since one `AgentDB`/`memory.yaml` is already shared by multiple bot
  processes (Slack + every Telegram bot) in `slack-assistant`.
- Make memory isolation between agents purely declarative: an agent is isolated if and only if
  it's listed under `profile.agents`. No boolean flag, anywhere, overrides this.
- Make the previously-hardcoded injection formatting (header text, override-instruction
  prefix, per-record line format, and the token/char budgets used when building it)
  YAML-configurable.
- Fail loudly on misconfiguration (unrecognized profile name, unknown key, wrong type) instead
  of silently substituting a default — the exact failure mode that caused the bug this spec
  starts from.
- Make built-in profiles easy for a developer to find and extend.

## Non-goals

- **Notifications for configuration errors or skill/instruction approvals** (a separate,
  already-discussed feature request) — this spec makes misconfiguration *raise*, which is the
  necessary foundation for that, but *delivering* a notification when it raises (or when a
  skill proposal needs approval) is a distinct design, covered separately.
- Changing `retrieve()`/`retrieve_by_entities()`'s `limit`/`budget_tokens` parameters — these
  stay as optional overrides (default `None` → resolved from `config_for(agent_id)`), matching
  Design Principle 1's "database-first, `SELECT`-style retrieval" — ad-hoc per-query overrides
  are a `SELECT ... LIMIT n`-shaped feature, not the "adapter exposes no config" problem this
  spec is solving. Only `isolate_by_agent` is removed as a parameter (see below) — that one is
  a pure yes/no derived from the YAML with no legitimate ad-hoc override case.
- Changing where `runner_fireworks.py` (in `slack-assistant`) positions the ephemeral memory
  blob relative to conversation history (always "right before the last user message" today) —
  that's a structural invariant of that runner's message model, not a tunable preference. Only
  the *content* of what gets positioned there changes (via the injection formatting settings
  below), which that runner already inherits automatically — see "Consumer-side impact."

## YAML schema

Single root key: `profile:`. Everything else nests under it.

```yaml
profile:
  name: research              # required: budget_optimized | full_storage | research

  retrieval:                  # optional — presence here = an implicit custom profile;
    retrieve_limit: 30        # no separate profiles: registry exists
    conflict_threshold: 0.85
    ranker_weights:
      provenance: 0.25
      recency: 0.35
      confidence: 0.25
      causal: 0.15
      semantic: 0.0
      workspace: 0.0    # working-directory overlap (Jaccard). Opt-in: set > 0 and reduce other weights to maintain sum=1.0

  injection:                  # optional
    context_token_limit: 4000     # was the memory_budget_tokens adapter kwarg
    memory_budget_chars: 24000    # moved here from retrieval — sizes the rendered text
    memory_header: "Governed memory context (higher priority than inferred context):"
    override_prefix: "Override instruction:"
    record_line_format: "- {key}: {value}"

  agents:                      # optional, keyed by agent_id — the exact string already
                                # passed to write()/retrieve()/AgentDBClaudeMiddleware(agent_id=...).
                                # No discovery mechanism; the caller already knows this string
                                # and already passes it for everything else.
    telegram_assistant_personal:
      # Listed here => isolated. No separate isolate_by_agent flag anywhere.
      retrieval:
        retrieve_limit: 30
      injection:
        context_token_limit: 4000

    telegram_assistant_work:
      name: full_storage       # an agent can switch to a completely different base profile
```

Rules:
- `profile.name` must be one of the three built-in names. There is no mechanism for a
  wholly-invented base name — customization happens via the override sections, not by
  registering new named bases.
- An `agents.<agent_id>` entry either gives `name:` (switch base profile entirely) and/or
  `retrieval:`/`injection:` overrides (applied on top of whichever base — the outer resolved
  profile if no `name:` given, or the newly-selected one if given). Same override mechanic as
  the top level, applied one layer deeper.
- An agent_id **not** listed under `profile.agents` uses the top-level resolution and is
  **not** isolated. There is no way to express "isolated but using the exact global settings
  with zero overrides" other than listing the agent with an empty block (`{}`) — that's
  intentional and fine, not a gap.

## Code changes — `agentdb`

### 1. New module: `src/agentdb/core/profiles.py`

Replaces the `_MODE_DEFAULTS` dict literal (currently 73 lines inside `client.py`) with typed
dataclasses, so a developer adding or tuning a built-in profile has one small, self-contained
file with real field names instead of a nested dict literal to reverse-engineer:

```python
"""
Built-in AgentDB profiles.

To add a new built-in profile: define a new Profile(...) below and add it
to BUILTIN_PROFILES. To change what "budget_optimized" means: edit the
BUDGET_OPTIMIZED instance directly — every field is named, typed, and
documented on its dataclass.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LifecycleSettings:
    confidence_decay_rate: float
    archival_threshold: float
    staleness_days: int
    reinforce_rate: float


@dataclass(frozen=True)
class RankerWeights:
    provenance: float
    recency: float
    confidence: float
    causal: float
    semantic: float


@dataclass(frozen=True)
class RetrievalSettings:
    retrieve_limit: int
    conflict_threshold: float  # currently unused elsewhere in the codebase; reserved
    ranker_weights: RankerWeights


@dataclass(frozen=True)
class InjectionSettings:
    context_token_limit: int
    memory_budget_chars: int
    memory_header: str
    override_prefix: str
    record_line_format: str


@dataclass(frozen=True)
class CaptureSettings:
    conversation_jsonl: bool
    tracking_max_files_per_directory: int


@dataclass(frozen=True)
class Profile:
    name: str
    lifecycle: LifecycleSettings
    retrieval: RetrievalSettings
    injection: InjectionSettings
    capture: CaptureSettings


_SHARED_INJECTION_DEFAULTS = dict(
    memory_header="Governed memory context (higher priority than inferred context):",
    override_prefix="Override instruction:",
    record_line_format="- {key}: {value}",
)

BUDGET_OPTIMIZED = Profile(
    name="budget_optimized",
    lifecycle=LifecycleSettings(confidence_decay_rate=0.0, archival_threshold=0.10, staleness_days=90, reinforce_rate=0.10),
    retrieval=RetrievalSettings(retrieve_limit=5, conflict_threshold=0.85, ranker_weights=RankerWeights(0.35, 0.25, 0.25, 0.15, 0.0)),
    injection=InjectionSettings(context_token_limit=1000, memory_budget_chars=2000, **_SHARED_INJECTION_DEFAULTS),
    capture=CaptureSettings(conversation_jsonl=False, tracking_max_files_per_directory=200),
)

FULL_STORAGE = Profile(
    name="full_storage",
    lifecycle=LifecycleSettings(confidence_decay_rate=0.0, archival_threshold=0.10, staleness_days=90, reinforce_rate=0.10),
    retrieval=RetrievalSettings(retrieve_limit=20, conflict_threshold=0.85, ranker_weights=RankerWeights(0.35, 0.25, 0.25, 0.15, 0.0)),
    injection=InjectionSettings(context_token_limit=3500, memory_budget_chars=12000, **_SHARED_INJECTION_DEFAULTS),
    capture=CaptureSettings(conversation_jsonl=True, tracking_max_files_per_directory=200),
)

RESEARCH = Profile(
    name="research",
    lifecycle=LifecycleSettings(confidence_decay_rate=0.0, archival_threshold=0.05, staleness_days=180, reinforce_rate=0.10),
    retrieval=RetrievalSettings(retrieve_limit=50, conflict_threshold=0.85, ranker_weights=RankerWeights(0.25, 0.35, 0.25, 0.15, 0.0)),
    injection=InjectionSettings(context_token_limit=8000, memory_budget_chars=24000, **_SHARED_INJECTION_DEFAULTS),
    capture=CaptureSettings(conversation_jsonl=True, tracking_max_files_per_directory=500),
)

BUILTIN_PROFILES: dict[str, Profile] = {p.name: p for p in (BUDGET_OPTIMIZED, FULL_STORAGE, RESEARCH)}
```

(`context_token_limit` defaults above are new judgment calls, scaled proportionally to each
profile's existing `retrieve_limit`/`memory_budget_chars` generosity — there's no prior value
to preserve since this setting didn't exist in the YAML before, only as the adapter's
`memory_budget_tokens` Python kwarg, which itself was only ever set to `1000` by the one
caller in `slack-assistant`, for every profile, regardless of which one was nominally
selected.)

### 2. `client.py` — new resolution logic

- `ConfigurationError(Exception)` — new, raised (not returned, not logged-and-defaulted) for
  every case in "Validation" below.
- `_resolve_config(yaml_dict: dict) -> dict`: reads `yaml_dict.get("profile", {})`, validates
  `name` against `BUILTIN_PROFILES` (raise `ConfigurationError` if missing or unrecognized),
  converts the matched `Profile` to a plain dict (`dataclasses.asdict`), deep-merges
  `retrieval`/`injection`/`lifecycle`/`capture` override sections from the `profile:` block on
  top (raising on any key not in that set, and on any leaf whose override value's type doesn't
  match the built-in default's type for that field). Returns the resolved dict — this is what
  `AgentDB.config` already exposes today, same shape as now plus the new `injection` key.
- Extract the built-in-name-to-dict step of `_resolve_config` into a small shared helper,
  `_builtin_profile_dict(name: str) -> dict` (validates against `BUILTIN_PROFILES`, raises
  `ConfigurationError`, returns `dataclasses.asdict(...)`) — `_resolve_config` calls it for the
  top-level `profile.name`, and the new method below calls it again for an agent's own `name`,
  so both paths share one validation/conversion point instead of two copies.
- New method `AgentDB.config_for(agent_id: str) -> dict`: looks up
  `yaml_dict["profile"].get("agents", {}).get(agent_id)`. If absent: returns
  `{**self.config, "isolate_by_agent": False}`. If present: the base is
  `_builtin_profile_dict(agent_block["name"])` if the agent block gives its own `name`,
  otherwise `self.config` (the already-resolved top-level profile). The agent's own
  `retrieval`/`injection` override sections (same validation rules as the top level) are
  deep-merged on top of that base, and the result is returned with `"isolate_by_agent": True`
  added. This `isolate_by_agent` key is **only** ever produced by this method — it is derived,
  never accepted as an input anywhere.

### 3. `client.py` — `retrieve()`/`retrieve_by_entities()`

- `isolate_by_agent` parameter is **removed** from both signatures. Internally, both call
  `self.config_for(agent_id)["isolate_by_agent"]` to decide whether to pass an `agent_id`
  filter down to `MemoryStore.search()`/`search_by_entities()` — no caller, anywhere, can
  override this per-call.
- `limit`/`budget_tokens` stay as explicit optional parameters, but their defaults change from
  hardcoded literals (`limit: int = 5`) to `Optional[...] = None`, resolving to
  `self.config_for(agent_id)["retrieval"]["retrieve_limit"]` /
  `self.config_for(agent_id)["injection"]["context_token_limit"]` when not given. An explicit
  value from a caller still wins for that one call — this is the `SELECT ... LIMIT n` case
  Design Principle 1 argues for keeping.

### 4. `adapters/claude_agent_sdk.py`

- Delete `ClaudeAdapterProfile`, `FULL_STORAGE_PROFILE`, `BUDGET_OPTIMIZED_PROFILE` entirely —
  superseded by `core/profiles.py`.
- `AgentDBClaudeMiddleware.__init__(self, db, agent_id, profile="budget_optimized", retrieve_limit=None, memory_budget_chars=None, memory_budget_tokens=None, conversation_started_callback=None, isolate_by_agent=False)`
  becomes `__init__(self, db, agent_id, conversation_started_callback=None)`. Internally:
  `self._resolved = db.config_for(agent_id)`, computed once (matches today's `self.profile`
  being set once at construction — `memory.yaml` isn't hot-reloaded, so this doesn't need to
  be recomputed per call).
- `prepare_turn()`'s `self._db.retrieve(limit=self.profile.retrieve_limit, ..., budget_tokens=self.profile.memory_budget_tokens)`
  becomes `self._db.retrieve(limit=self._resolved["retrieval"]["retrieve_limit"], budget_tokens=self._resolved["injection"]["context_token_limit"], ...)`
  — no `isolate_by_agent=` kwarg passed at all (removed from `retrieve()`'s signature per #3).
- The hardcoded strings in `prepare_turn()`/`_build_memory_context()` (`"Governed memory
  context (higher priority than inferred context):\n"`, `"Override instruction: {instruction}"`,
  `f"- {record.key}: {value_text}"`) become
  `self._resolved["injection"]["memory_header"]`, `.format()`-style use of
  `self._resolved["injection"]["override_prefix"]`, and `.format()` of
  `self._resolved["injection"]["record_line_format"]` respectively.
  `self.profile.memory_budget_chars` becomes `self._resolved["injection"]["memory_budget_chars"]`.
- Same constructor simplification for `AgentDBHermesProvider` (drop `isolate_by_agent` kwarg,
  same `self._resolved = db.config_for(agent_id)` pattern, same call-site updates in
  `prefetch()`/`handle_tool_call()`).

### 5. `interface/mcp_server.py`

`memory_retrieve`'s `isolate_by_agent: bool = False` parameter is removed (the tool always
resolves it server-side via `config_for(agent_id)`, same as the adapters). `limit`/
`budget_tokens` stay as optional per-call parameters, same `None`-means-config-driven
treatment as `retrieve()` itself — matching the "primitive stays flexible" principle, since an
MCP tool call is itself an ad-hoc query, not a fixed adapter wiring point.

## Validation (fail loudly)

Every case below raises `ConfigurationError` at the point the bad value is first read (`AgentDB.__init__`
for top-level `profile.name`/keys; `config_for(agent_id)` for an agent's own `name`/keys) —
never a silent substitution:

- `profile.name` (top-level or under any `agents.<id>`) missing, or not one of
  `budget_optimized`/`full_storage`/`research`.
- An override section under `profile:` or `profile.agents.<id>` containing a key that isn't
  `retrieval`/`injection`/`lifecycle`/`capture` (catches typos like `retrievel:`).
  Same check one level deeper: a field inside one of those sections that doesn't exist on the
  corresponding `Profile` sub-dataclass.
- A field's override value whose type doesn't match the built-in default's type for that field
  (e.g. `retrieve_limit: "fifty"`).

Malformed YAML syntax itself already raises via `yaml.safe_load` — unchanged, not something
this spec needs to add.

## Consumer-side impact — `slack-assistant` (described here for context; not this spec's
implementation target, since it's a different repo, but the cross-repo consequence needs to be
visible before this lands)

- `runner_claude.py`: `PlatformConfig.isolate_memory` field is **removed** — it was added in a
  prior spec specifically because `isolate_by_agent` was a constructor kwarg; now that it's
  derived purely from the shared `memory.yaml`'s `profile.agents`, keeping a parallel
  Python-side flag would reintroduce the exact "two sources of truth for one decision"
  problem this spec exists to close. `init_agentdb()`'s
  `AgentDBClaudeMiddleware(db=db, agent_id=platform.agent_id, profile="research", memory_budget_tokens=1000)`
  becomes `AgentDBClaudeMiddleware(db=db, agent_id=platform.agent_id)` — no config kwargs left
  to pass.
- `bot-telegram.py`: `configure_bot()`'s `isolate_memory=bot_name is not None` line is removed
  from the `PlatformConfig(...)` construction (the field no longer exists).
- Practical effect: isolating a named bot's memory is no longer automatic — it requires adding
  that bot's `agent_id` (e.g. `telegram_assistant_personal`) under `profile.agents` in the
  shared `agentdb-workspace/config/memory.yaml`. This is a one-time manual step per bot, traded
  for having exactly one place that decision lives.
- `runner_fireworks.py` needs no changes for injection formatting — it already extracts and
  rejoins whatever system-message strings `prepare_turn_async()` builds, so new
  YAML-configurable header/prefix/format text flows through automatically. Its insertion
  *position* (right before the last user message) is unaffected — see Non-goals.

## Testing

- `core/profiles.py`: no logic to test beyond "the module imports and `BUILTIN_PROFILES` has
  exactly three entries with the expected names" — it's data, not behavior.
- `_resolve_config`/`config_for`: one test per validation rule above (raises
  `ConfigurationError` with a message naming the bad field/value); one test per built-in
  profile confirming the resolved dict matches that profile's dataclass values exactly; one
  test for an agent-level override changing just the overridden field while leaving siblings at
  the base profile's values; one test for an agent switching `name:` entirely; one test
  confirming an agent NOT listed under `profile.agents` gets `isolate_by_agent: False` and the
  top-level resolution untouched.
- `retrieve()`/`retrieve_by_entities()`: update existing isolation tests (from the prior spec)
  to stop passing `isolate_by_agent=` as a kwarg — instead, set up the YAML/`profile.agents`
  state the test needs and call `retrieve()` with no such parameter at all, confirming the
  removed-parameter API surface. Add a test that an explicit `limit=`/`budget_tokens=` value
  still overrides the config-derived default for one call.
- `AgentDBClaudeMiddleware`/`AgentDBHermesProvider`: update existing tests to construct with
  just `db=`/`agent_id=` (no `profile=`/`retrieve_limit=`/etc.), confirming behavior is now
  driven entirely by the `memory.yaml` state each test sets up.
- `mcp_server.py`: update existing `isolate_by_agent=True` test call to remove that kwarg,
  relying on YAML state instead; keep the `limit=`/`budget_tokens=` override tests as-is (those
  parameters aren't removed).
