# Unified Profile Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify AgentDB's two disconnected configuration mechanisms (the YAML `mode`/`profiles`
system and the adapters' hardcoded `ClaudeAdapterProfile` constants) into one: a single
`profile:` root key, per-agent overrides that make isolation purely declarative, configurable
injection formatting, and load-time validation that fails loudly instead of silently
substituting a default.

**Architecture:** `src/agentdb/core/profiles.py` becomes the single, typed source of built-in
profile data. `client.py`'s `_resolve_config()` reads the new `profile:` schema and validates
strictly; a new `AgentDB.config_for(agent_id)` layers per-agent overrides on top. Every adapter
(`AgentDBClaudeMiddleware`, `AgentDBHermesProvider`) and the MCP tool stop taking
profile/budget/isolation constructor arguments and read `db.config_for(agent_id)` instead.

**Tech Stack:** Python 3.14, pytest, dataclasses, PyYAML.

## Global Constraints

- `profile.name` (top-level or under any `agents.<id>`) must be one of `budget_optimized`,
  `full_storage`, `research` — anything else raises `ConfigurationError`, never a silent
  fallback.
- An override section key not in `{retrieval, injection, lifecycle, capture}`, or a field
  inside one of those sections not present on the corresponding dataclass, or a value whose
  type doesn't match the built-in default's type for that field — all raise
  `ConfigurationError`.
- `isolate_by_agent` is never a YAML key and never a function parameter anywhere in this
  codebase — it is only ever a derived boolean returned by `AgentDB.config_for(agent_id)`
  (`True` iff `agent_id` is listed under `profile.agents`).
- `limit`/`budget_tokens` on `retrieve()`/`retrieve_by_entities()` and on the MCP
  `memory_retrieve` tool stay as optional parameters (default `None` → resolved from
  `config_for(agent_id)`) — only `isolate_by_agent` is removed as a parameter.
- Full spec: `docs/superpowers/specs/2026-07-27-unified-profile-configuration-design.md`.
- This plan covers the `agentdb` repo only. Consequential changes in `slack-assistant`
  (`PlatformConfig.isolate_memory` removal, `configure_bot()` simplification, migrating
  `agentdb-workspace/config/memory.yaml` to the new schema) are a separate follow-up plan in
  that repo, written after this one is implemented and released — editing that repo against
  an API that doesn't exist yet isn't verifiable.

---

### Task 1: `core/profiles.py` — typed built-in profiles

**Files:**
- Create: `src/agentdb/core/profiles.py`
- Test: Create `tests/unit/test_profiles.py`

**Interfaces:**
- Produces: `Profile`, `LifecycleSettings`, `RetrievalSettings`, `InjectionSettings`,
  `CaptureSettings`, `RankerWeights` dataclasses; `BUDGET_OPTIMIZED`, `FULL_STORAGE`,
  `RESEARCH` (`Profile` instances); `BUILTIN_PROFILES: dict[str, Profile]`. Task 2 imports
  `BUILTIN_PROFILES` and `Profile`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_profiles.py`:

```python
import dataclasses

from agentdb.core.profiles import BUILTIN_PROFILES, BUDGET_OPTIMIZED, FULL_STORAGE, RESEARCH


def test_builtin_profiles_has_exactly_three_entries():
    assert set(BUILTIN_PROFILES.keys()) == {"budget_optimized", "full_storage", "research"}


def test_budget_optimized_matches_documented_values():
    assert BUDGET_OPTIMIZED.retrieval.retrieve_limit == 5
    assert BUDGET_OPTIMIZED.injection.memory_budget_chars == 2000
    assert BUDGET_OPTIMIZED.lifecycle.staleness_days == 90
    assert BUDGET_OPTIMIZED.retrieval.ranker_weights.provenance == 0.35
    assert BUDGET_OPTIMIZED.capture.conversation_jsonl is False


def test_full_storage_matches_documented_values():
    assert FULL_STORAGE.retrieval.retrieve_limit == 20
    assert FULL_STORAGE.injection.memory_budget_chars == 12000
    assert FULL_STORAGE.capture.conversation_jsonl is True


def test_research_matches_documented_values():
    assert RESEARCH.retrieval.retrieve_limit == 50
    assert RESEARCH.injection.memory_budget_chars == 24000
    assert RESEARCH.lifecycle.staleness_days == 180
    assert RESEARCH.retrieval.ranker_weights.recency == 0.35


def test_profile_converts_cleanly_to_nested_dict():
    d = dataclasses.asdict(BUDGET_OPTIMIZED)
    assert d["retrieval"]["retrieve_limit"] == 5
    assert d["injection"]["memory_header"].startswith("Governed memory context")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit/test_profiles.py -v`
Expected: `ModuleNotFoundError: No module named 'agentdb.core.profiles'` for every test.

- [ ] **Step 3: Write minimal implementation**

Create `src/agentdb/core/profiles.py`:

```python
"""
Built-in AgentDB profiles.

To add a new built-in profile: define a new Profile(...) below and add it
to BUILTIN_PROFILES. To change what "budget_optimized" means: edit the
BUDGET_OPTIMIZED instance directly -- every field is named, typed, and
lives on its own dataclass below.
"""
from __future__ import annotations

from dataclasses import dataclass


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


_SHARED_INJECTION_TEXT = dict(
    memory_header="Governed memory context (higher priority than inferred context):",
    override_prefix="Override instruction:",
    record_line_format="- {key}: {value}",
)

BUDGET_OPTIMIZED = Profile(
    name="budget_optimized",
    lifecycle=LifecycleSettings(
        confidence_decay_rate=0.0, archival_threshold=0.10, staleness_days=90, reinforce_rate=0.10,
    ),
    retrieval=RetrievalSettings(
        retrieve_limit=5, conflict_threshold=0.85,
        ranker_weights=RankerWeights(provenance=0.35, recency=0.25, confidence=0.25, causal=0.15, semantic=0.0),
    ),
    injection=InjectionSettings(context_token_limit=1000, memory_budget_chars=2000, **_SHARED_INJECTION_TEXT),
    capture=CaptureSettings(conversation_jsonl=False, tracking_max_files_per_directory=200),
)

FULL_STORAGE = Profile(
    name="full_storage",
    lifecycle=LifecycleSettings(
        confidence_decay_rate=0.0, archival_threshold=0.10, staleness_days=90, reinforce_rate=0.10,
    ),
    retrieval=RetrievalSettings(
        retrieve_limit=20, conflict_threshold=0.85,
        ranker_weights=RankerWeights(provenance=0.35, recency=0.25, confidence=0.25, causal=0.15, semantic=0.0),
    ),
    injection=InjectionSettings(context_token_limit=3500, memory_budget_chars=12000, **_SHARED_INJECTION_TEXT),
    capture=CaptureSettings(conversation_jsonl=True, tracking_max_files_per_directory=200),
)

RESEARCH = Profile(
    name="research",
    lifecycle=LifecycleSettings(
        confidence_decay_rate=0.0, archival_threshold=0.05, staleness_days=180, reinforce_rate=0.10,
    ),
    retrieval=RetrievalSettings(
        retrieve_limit=50, conflict_threshold=0.85,
        ranker_weights=RankerWeights(provenance=0.25, recency=0.35, confidence=0.25, causal=0.15, semantic=0.0),
    ),
    injection=InjectionSettings(context_token_limit=8000, memory_budget_chars=24000, **_SHARED_INJECTION_TEXT),
    capture=CaptureSettings(conversation_jsonl=True, tracking_max_files_per_directory=500),
)

BUILTIN_PROFILES: dict[str, Profile] = {p.name: p for p in (BUDGET_OPTIMIZED, FULL_STORAGE, RESEARCH)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit/test_profiles.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/agentdb/core/profiles.py tests/unit/test_profiles.py
git commit -m "feat: add typed built-in profiles module"
```

---

### Task 2: New `profile:` schema in `client.py`, with strict validation

**Files:**
- Modify: `src/agentdb/interface/client.py` (replace `_MODE_DEFAULTS`/`_resolve_config`/`_DEFAULT_MEMORY_YAML`; update the `full_storage` tracked-directories check)
- Modify: `tests/unit/test_config.py` (full rewrite to the new schema)
- Modify: `tests/unit/test_memory_workspace.py` (2 tests reference the old flat `mode:` key)

**Interfaces:**
- Consumes: `BUILTIN_PROFILES`, `Profile` from Task 1.
- Produces: `ConfigurationError` (new public exception, importable as
  `from agentdb.interface.client import ConfigurationError`); `_builtin_profile_dict(name) ->
  dict` (also used by Task 3); `_resolve_config(yaml_dict) -> dict` (same name, new schema —
  reads `yaml_dict.get("profile", {})` instead of the old `mode`/`profiles` keys).
  `AgentDB.config` is unchanged in shape (still the fully resolved dict), now including an
  `injection` key alongside the existing `lifecycle`/`retrieval`/`capture`.

- [ ] **Step 1: Write the failing tests**

Replace the entire contents of `tests/unit/test_config.py`:

```python
"""Profile configuration resolution: single `profile:` root key, strict validation."""
from __future__ import annotations

import pytest
import yaml


def _write_yaml(path, data):
    path.write_text(yaml.dump(data), encoding="utf-8")


def _mem_dir(tmp_path, config_data):
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    _write_yaml(d / "config" / "memory.yaml", config_data)
    return d


def test_budget_optimized_defaults_lifecycle(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {"name": "budget_optimized"}, "tracked_directories": []}))
    assert db.lifecycle.STALENESS_DAYS == 90
    assert db.lifecycle.ARCHIVAL_THRESHOLD == pytest.approx(0.10)
    assert db.lifecycle.REINFORCE_RATE == pytest.approx(0.10)


def test_full_storage_profile_retrieve_limit(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(tmp_path, {"profile": {"name": "full_storage"}, "tracked_directories": [str(tmp_path)]}),
    )
    assert db.config["retrieval"]["retrieve_limit"] == 20
    assert db.config["injection"]["memory_budget_chars"] == 12000


def test_override_section_beats_profile_default(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {"profile": {"name": "budget_optimized", "lifecycle": {"staleness_days": 30}}, "tracked_directories": []},
        ),
    )
    assert db.lifecycle.STALENESS_DAYS == 30
    assert db.lifecycle.ARCHIVAL_THRESHOLD == pytest.approx(0.10)


def test_ranker_uses_config_weights(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {
                "profile": {
                    "name": "budget_optimized",
                    "retrieval": {
                        "ranker_weights": {"provenance": 0.50, "recency": 0.20, "confidence": 0.20, "causal": 0.10, "semantic": 0.0},
                    },
                },
                "tracked_directories": [],
            },
        ),
    )
    assert db._ranker.w_provenance == pytest.approx(0.50)
    assert db._ranker.w_recency == pytest.approx(0.20)


def test_no_memory_dir_uses_hardcoded_defaults(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(path=tmp_path / "test.db")
    assert db.lifecycle.STALENESS_DAYS == 90
    assert db._ranker.w_provenance == pytest.approx(0.35)


def test_config_property_returns_resolved_dict_with_injection_key(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {"name": "budget_optimized"}, "tracked_directories": []}))
    cfg = db.config
    assert cfg["lifecycle"]["staleness_days"] == 90
    assert cfg["retrieval"]["retrieve_limit"] == 5
    assert cfg["injection"]["context_token_limit"] == 1000
    assert cfg["injection"]["memory_header"].startswith("Governed memory context")


def test_research_profile_retrieve_limit(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {"name": "research"}, "tracked_directories": [str(tmp_path)]}))
    assert db.config["retrieval"]["retrieve_limit"] == 50
    assert db.config["injection"]["memory_budget_chars"] == 24000


def test_custom_override_does_not_need_a_registry(tmp_path):
    """No profiles: registry exists -- override sections directly under
    profile: are an implicit custom profile."""
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {"profile": {"name": "research", "retrieval": {"retrieve_limit": 30}}, "tracked_directories": [str(tmp_path)]},
        ),
    )
    assert db.config["retrieval"]["retrieve_limit"] == 30
    assert db.config["lifecycle"]["staleness_days"] == 180  # research's other fields untouched


def test_missing_profile_name_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError, match="profile.name"):
        AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {}, "tracked_directories": []}))


def test_unrecognized_profile_name_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError, match="nonexistent"):
        AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {"name": "nonexistent"}, "tracked_directories": []}))


def test_unknown_override_section_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError, match="retrievel"):
        AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {"name": "budget_optimized", "retrievel": {}}, "tracked_directories": []}))


def test_unknown_field_in_override_section_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError, match="retrieve_limitt"):
        AgentDB(
            memory_dir=_mem_dir(
                tmp_path,
                {"profile": {"name": "budget_optimized", "retrieval": {"retrieve_limitt": 10}}, "tracked_directories": []},
            ),
        )


def test_wrong_type_in_override_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    with pytest.raises(ConfigurationError, match="retrieve_limit"):
        AgentDB(
            memory_dir=_mem_dir(
                tmp_path,
                {"profile": {"name": "budget_optimized", "retrieval": {"retrieve_limit": "five"}}, "tracked_directories": []},
            ),
        )
```

Update `tests/unit/test_memory_workspace.py`:

```python
def test_memory_workspace_creates_default_layout(tmp_path):
    workspace_root = tmp_path / "memory-home"

    db = AgentDB(memory_dir=workspace_root, permissions_path=PERMS)

    assert db.memory_dir == workspace_root.resolve()
    assert db.memory_config_path == (workspace_root / "config" / "memory.yaml").resolve()
    assert (workspace_root / "config").is_dir()
    assert (workspace_root / "database").is_dir()
    assert (workspace_root / "conversations").is_dir()
    assert (workspace_root / "embeddings").is_dir()
    assert (workspace_root / "reconciliation").is_dir()
    assert (workspace_root / "logs").is_dir()
    assert db._path == (workspace_root / "database" / "agentdb.sqlite").resolve()
    assert db.memory_config["profile"]["name"] == "budget_optimized"
```

(only the last assertion line changes — `mode` → `profile`/`name`, same test name/structure otherwise)

```python
def test_invalid_full_storage_config_requires_tracked_directories(tmp_path):
    workspace_root = tmp_path / "memory-home"
    config_dir = workspace_root / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "memory.yaml"
    config_path.write_text(
        "\n".join(
            [
                "profile:",
                "  name: full_storage",
                "tracked_directories: []",
                "capture:",
                "  conversation_jsonl: true",
                "reconciliation:",
                "  triggers:",
                "    - conversation_started",
                "database:",
                "  mode: embedded",
                "  path: database/agentdb.sqlite",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="tracked_directories"):
        AgentDB(memory_dir=workspace_root, permissions_path=PERMS)
```

(only the first two list entries change: `"mode: full_storage"` → `"profile:"` + `"  name: full_storage"`, rest unchanged — this test is about the *tracked_directories* validation, not the profile schema itself, so it stays a `ValueError`, not `ConfigurationError`)

- [ ] **Step 2: Run tests to verify they fail**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit/test_config.py tests/unit/test_memory_workspace.py -v 2>&1 | tail -60`
Expected: most `test_config.py` tests fail with `KeyError`/`AttributeError` (old `_resolve_config` doesn't understand a `profile:` key at all — it looks for `mode`/`profiles`, which are absent, so it silently resolves to `budget_optimized` regardless of what's under `profile:`, meaning assertions expecting `research`/`full_storage`/override values fail); the two `ConfigurationError`-expecting tests fail with `ImportError: cannot import name 'ConfigurationError'`; the two `test_memory_workspace.py` tests fail their changed assertions.

- [ ] **Step 3: Write minimal implementation**

In `src/agentdb/interface/client.py`, add the import (near the top, alongside existing imports):

```python
from .core.profiles import BUILTIN_PROFILES, Profile
```

(adjust the relative import path to match this file's actual location relative to `core/profiles.py` — `interface/client.py` → `core/profiles.py` is `from ..core.profiles import ...` if `interface` and `core` are sibling packages under `agentdb/`; confirm against how `client.py` already imports from `core.models`/`core.store` today and mirror that exact style.)

Add, near where `_MODE_DEFAULTS` currently lives:

```python
class ConfigurationError(Exception):
    """Raised for any invalid memory.yaml profile configuration. Never
    silently substituted for a default -- see Design Principle in
    docs/superpowers/specs/2026-07-27-unified-profile-configuration-design.md."""


_OVERRIDE_SECTIONS = ("retrieval", "injection", "lifecycle", "capture")


def _builtin_profile_dict(name: str) -> dict:
    if name not in BUILTIN_PROFILES:
        raise ConfigurationError(
            f"profile.name '{name}' is not a recognized built-in profile "
            f"(expected one of {sorted(BUILTIN_PROFILES.keys())})"
        )
    return dataclasses.asdict(BUILTIN_PROFILES[name])


def _validate_and_merge_overrides(base: dict, profile_block: dict, where: str) -> dict:
    result = dict(base)
    for key in profile_block:
        if key in ("name", "agents"):
            continue
        if key not in _OVERRIDE_SECTIONS:
            raise ConfigurationError(
                f"{where}: unknown key '{key}' (expected one of {_OVERRIDE_SECTIONS}, 'name', or 'agents')"
            )
        section_override = profile_block[key]
        base_section = result.get(key, {})
        for field, value in section_override.items():
            if field not in base_section:
                raise ConfigurationError(f"{where}.{key}: unknown field '{field}'")
            expected_type = type(base_section[field])
            if not isinstance(value, expected_type) and not (isinstance(value, dict) and isinstance(base_section[field], dict)):
                raise ConfigurationError(
                    f"{where}.{key}.{field}: expected {expected_type.__name__}, got {type(value).__name__} ({value!r})"
                )
        result[key] = _deep_merge(base_section, section_override)
    return result


def _resolve_config(yaml_dict: dict) -> dict:
    """
    Resolve the final config dict from the new single-root `profile:` schema.
    Priority (low -> high): built-in profile defaults, then override sections
    (retrieval/injection/lifecycle/capture) declared directly under profile:.
    Raises ConfigurationError on any unrecognized name, key, or wrong type --
    never silently substitutes a default.
    """
    profile_block = yaml_dict.get("profile", {}) if yaml_dict else {}
    name = profile_block.get("name")
    if not name:
        raise ConfigurationError("profile.name is required")
    base = _builtin_profile_dict(name)
    return _validate_and_merge_overrides(base, profile_block, where="profile")
```

Remove the old `_MODE_DEFAULTS` dict literal and the old `_resolve_config` function entirely
(they're fully superseded by the above). Add `import dataclasses` to the top of the file if
not already present.

Update `_DEFAULT_MEMORY_YAML`:

```python
_DEFAULT_MEMORY_YAML = """\
# profile.name: budget_optimized | full_storage | research
# Any of retrieval:/injection:/lifecycle:/capture: declared alongside name
# override just those fields on top of the named profile.
profile:
  name: budget_optimized

reconciliation:
  triggers:
    - conversation_started
database:
  mode: embedded
  path: database/agentdb.sqlite
"""
```

Update the `full_storage` tracked-directories check in `AgentDB.__init__`:

```python
            # Validate full_storage mode
            if self.memory_config.get("mode") == "full_storage":
```

becomes:

```python
            # Validate full_storage profile
            if self.memory_config.get("profile", {}).get("name") == "full_storage":
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit/test_config.py tests/unit/test_memory_workspace.py tests/unit/test_profiles.py -v`
Expected: all passing (13 in test_config.py, existing count in test_memory_workspace.py, 5 in test_profiles.py).

- [ ] **Step 5: Run the full test suite to check for other breakage**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit tests/integration/test_claude_agent_sdk_adapter.py -q 2>&1 | tail -40`
Expected: failures in `test_client_isolation.py`, `test_claude_agent_sdk_adapter.py`, `test_hermes_adapter.py`, `test_mcp_server.py` (all still using the old `mode`/`profiles`/`isolate_by_agent=` schema and kwargs) — these are Task 3/4/5/6's job to fix, not this task's. Confirm no *other* unexpected failures appear.

- [ ] **Step 6: Commit**

```bash
git add src/agentdb/interface/client.py tests/unit/test_config.py tests/unit/test_memory_workspace.py
git commit -m "feat: replace mode/profiles YAML schema with single profile: root key, strict validation"
```

---

### Task 3: `AgentDB.config_for(agent_id)` — per-agent overrides and derived isolation

**Files:**
- Modify: `src/agentdb/interface/client.py`
- Test: Create `tests/unit/test_config_for_agent.py`

**Interfaces:**
- Consumes: `_builtin_profile_dict`, `_validate_and_merge_overrides`, `ConfigurationError` from Task 2.
- Produces: `AgentDB.config_for(agent_id: str) -> dict` — Tasks 4 and 5 call this.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_config_for_agent.py`:

```python
from pathlib import Path

import pytest
import yaml


def _mem_dir(tmp_path, config_data):
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    (d / "config" / "memory.yaml").write_text(yaml.dump(config_data), encoding="utf-8")
    return d


def test_agent_not_listed_gets_global_config_and_not_isolated(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(memory_dir=_mem_dir(tmp_path, {"profile": {"name": "research"}, "tracked_directories": [str(tmp_path)]}))

    cfg = db.config_for("some_agent")

    assert cfg["isolate_by_agent"] is False
    assert cfg["retrieval"]["retrieve_limit"] == 50  # research's global value, untouched


def test_agent_listed_is_isolated_and_can_override_fields(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {
                "profile": {
                    "name": "research",
                    "agents": {"telegram_assistant_personal": {"retrieval": {"retrieve_limit": 30}}},
                },
                "tracked_directories": [str(tmp_path)],
            },
        ),
    )

    cfg = db.config_for("telegram_assistant_personal")

    assert cfg["isolate_by_agent"] is True
    assert cfg["retrieval"]["retrieve_limit"] == 30
    assert cfg["lifecycle"]["staleness_days"] == 180  # research's other fields, untouched


def test_agent_can_switch_to_a_different_base_profile(tmp_path):
    from agentdb.interface.client import AgentDB
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {
                "profile": {"name": "research", "agents": {"telegram_assistant_work": {"name": "full_storage"}}},
                "tracked_directories": [str(tmp_path)],
            },
        ),
    )

    cfg = db.config_for("telegram_assistant_work")

    assert cfg["isolate_by_agent"] is True
    assert cfg["retrieval"]["retrieve_limit"] == 20  # full_storage's value, not research's 50


def test_agent_block_unknown_field_raises_configuration_error(tmp_path):
    from agentdb.interface.client import AgentDB, ConfigurationError
    db = AgentDB(
        memory_dir=_mem_dir(
            tmp_path,
            {
                "profile": {"name": "research", "agents": {"a1": {"retrieval": {"retrieve_limitt": 10}}}},
                "tracked_directories": [str(tmp_path)],
            },
        ),
    )

    with pytest.raises(ConfigurationError, match="retrieve_limitt"):
        db.config_for("a1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit/test_config_for_agent.py -v`
Expected: `AttributeError: 'AgentDB' object has no attribute 'config_for'` for every test.

- [ ] **Step 3: Write minimal implementation**

Add to the `AgentDB` class in `client.py`, near the existing `config` property:

```python
    def config_for(self, agent_id: str) -> dict:
        """Resolved config for a specific agent: the global profile resolution,
        with this agent's own overrides (if listed under profile.agents) merged
        on top. isolate_by_agent is derived here -- True iff agent_id is listed
        -- and is never accepted as an input anywhere in this codebase."""
        agents = self.memory_config.get("profile", {}).get("agents", {}) if self.memory_config else {}
        agent_block = agents.get(agent_id)
        if agent_block is None:
            return {**self.config, "isolate_by_agent": False}

        base = _builtin_profile_dict(agent_block["name"]) if "name" in agent_block else self.config
        resolved = _validate_and_merge_overrides(base, agent_block, where=f"profile.agents.{agent_id}")
        return {**resolved, "isolate_by_agent": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit/test_config_for_agent.py -v`
Expected: 4 passed.

- [ ] **Step 5: Run the full unit suite**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit -q 2>&1 | tail -20`
Expected: same failures as Task 2 Step 5 identified (still-unfixed `test_client_isolation.py`/`test_hermes_adapter.py`/`test_mcp_server.py`), nothing new.

- [ ] **Step 6: Commit**

```bash
git add src/agentdb/interface/client.py tests/unit/test_config_for_agent.py
git commit -m "feat: add AgentDB.config_for(agent_id) for per-agent config + derived isolation"
```

---

### Task 4: `retrieve()`/`retrieve_by_entities()` — drop `isolate_by_agent`, config-driven defaults

**Files:**
- Modify: `src/agentdb/interface/client.py`
- Modify: `tests/unit/test_client_isolation.py`

**Interfaces:**
- Consumes: `AgentDB.config_for(agent_id)` from Task 3.
- Produces: `retrieve(query, agent_id, limit: Optional[int] = None, ..., budget_tokens: Optional[int] = None) -> RetrievalResult`
  and `retrieve_by_entities(entities, agent_id, ...)` — both **without** an `isolate_by_agent`
  parameter. Task 5 and Task 6 call these without that parameter.

- [ ] **Step 1: Write the failing tests**

Replace the contents of `tests/unit/test_client_isolation.py` (the truncation-order regression
test from the prior spec stays exactly as-is at the `MemoryStore.search()` level — that's
unaffected by this task, since it's below `retrieve()`; only the `retrieve()`/
`retrieve_by_entities()`-level tests below it change):

```python
from pathlib import Path

from agentdb import AgentDB
from agentdb.interface.client import EVENT_MEMORY_RETRIEVE


PERMS = Path(__file__).parent.parent / "fixtures" / "permissions.test.yaml"


def test_search_isolation_survives_truncation_by_other_agents_volume(tmp_path):
    """Unchanged from the prior spec -- MemoryStore.search()'s own agent_id
    filter is unaffected by retrieve()'s signature change."""
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)

    for i in range(60):
        db.write(key=f"memory/working/other_{i}", value="a widget note", origin="agent_inferred", agent_id="research_agent")
    db.write(key="memory/working/mine", value="a widget note", origin="agent_inferred", agent_id="coder_agent")

    results = db._store.search("widget", limit=5, agent_id="coder_agent")

    assert [r.key for r in results] == ["memory/working/mine"]


def _mem_dir_with_isolated_agent(tmp_path, agent_id):
    d = tmp_path / "mem"
    d.mkdir()
    (d / "config").mkdir()
    import yaml
    (d / "config" / "memory.yaml").write_text(
        yaml.dump({"profile": {"name": "budget_optimized", "agents": {agent_id: {}}}}), encoding="utf-8",
    )
    return d


def _write_two_agents(db, key_suffix="note", **write_kwargs):
    db.write(key=f"memory/working/{key_suffix}", value="a widget note", origin="agent_inferred", agent_id="research_agent", **write_kwargs)
    db.write(key=f"memory/working/{key_suffix}_mine", value="a widget note", origin="agent_inferred", agent_id="coder_agent", **write_kwargs)


def test_retrieve_isolates_when_agent_is_listed_under_profile_agents(tmp_path):
    db = AgentDB(memory_dir=_mem_dir_with_isolated_agent(tmp_path, "coder_agent"), permissions_path=PERMS)
    _write_two_agents(db)

    result = db.retrieve("widget", agent_id="coder_agent")

    assert [r.key for r in result.records] == ["memory/working/note_mine"]


def test_retrieve_returns_mixed_pool_when_agent_not_listed(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)  # no memory.yaml at all -> never isolated
    _write_two_agents(db)

    result = db.retrieve("widget", agent_id="coder_agent")

    assert {r.key for r in result.records} == {"memory/working/note", "memory/working/note_mine"}


def test_retrieve_explicit_limit_overrides_config_default(tmp_path):
    db = AgentDB(memory_dir=_mem_dir_with_isolated_agent(tmp_path, "coder_agent"), permissions_path=PERMS)
    for i in range(10):
        db.write(key=f"memory/working/note_{i}", value="a widget note", origin="agent_inferred", agent_id="coder_agent")

    result = db.retrieve("widget", agent_id="coder_agent", limit=2)

    assert len(result.records) == 2


def test_retrieve_by_entities_isolates_when_agent_is_listed(tmp_path):
    db = AgentDB(memory_dir=_mem_dir_with_isolated_agent(tmp_path, "coder_agent"), permissions_path=PERMS)
    _write_two_agents(db, entities=["widget_entity"])

    result = db.retrieve_by_entities(["widget_entity"], agent_id="coder_agent")

    assert [r.key for r in result.records] == ["memory/working/note_mine"]


def test_retrieve_by_entities_returns_mixed_pool_when_agent_not_listed(tmp_path):
    db = AgentDB(path=tmp_path / "test.db", permissions_path=PERMS)
    _write_two_agents(db, entities=["widget_entity"])

    result = db.retrieve_by_entities(["widget_entity"], agent_id="coder_agent")

    assert {r.key for r in result.records} == {"memory/working/note", "memory/working/note_mine"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit/test_client_isolation.py -v`
Expected: the truncation test still passes (untouched code path); the `_mem_dir_with_isolated_agent`-based tests fail because `retrieve()`/`retrieve_by_entities()` don't yet call `config_for(agent_id)` — the isolate tests will show a mixed pool (since today's `retrieve()` still defaults `isolate_by_agent=False` and never consults config), and the explicit-limit test should already pass (unaffected). Confirm the isolation-specific assertions are the ones failing.

- [ ] **Step 3: Write minimal implementation**

In `client.py`, change:

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
        candidates = self._store.search(
            query, limit=max(limit, 50), agent_id=agent_id if isolate_by_agent else None,
        )
        ranked = self._ranker.rank(query, candidates, agent_id=agent_id)[:limit]
```

to:

```python
    def retrieve(
        self,
        query: str,
        agent_id: str,
        limit: Optional[int] = None,
        session_id: Optional[str] = None,
        external_context: Optional[list[dict]] = None,
        budget_tokens: Optional[int] = None,
    ) -> RetrievalResult:
        agent_config = self.config_for(agent_id)
        isolate_by_agent = agent_config["isolate_by_agent"]
        resolved_limit = limit if limit is not None else agent_config["retrieval"]["retrieve_limit"]
        resolved_budget_tokens = budget_tokens if budget_tokens is not None else agent_config["injection"]["context_token_limit"]

        candidates = self._store.search(
            query, limit=max(resolved_limit, 50), agent_id=agent_id if isolate_by_agent else None,
        )
        ranked = self._ranker.rank(query, candidates, agent_id=agent_id)[:resolved_limit]
```

Then further down in the same method, replace every remaining use of the old parameter names
`limit`/`budget_tokens` with `resolved_limit`/`resolved_budget_tokens` (the budget-cap loop
that builds `selected`/`token_total`, and the `"isolate_by_agent": isolate_by_agent` audit-log
field — that field still gets logged with the *effective* boolean, unchanged in spirit from
the prior spec, just now sourced from `agent_config` instead of a parameter).

Apply the equivalent change to `retrieve_by_entities()`:

```python
    def retrieve_by_entities(
        self,
        entities: list[str],
        agent_id: str,
        session_id: Optional[str] = None,
    ) -> RetrievalResult:
        agent_config = self.config_for(agent_id)
        isolate_by_agent = agent_config["isolate_by_agent"]
        records = self._store.search_by_entities(
            entities, agent_id=agent_id if isolate_by_agent else None,
        )
```

(the rest of the method already just uses `records`/`isolate_by_agent` for the audit-log field
— no further signature-shaped changes needed there since it never had a `limit`/`budget_tokens`
parameter to begin with)

- [ ] **Step 4: Run tests to verify they pass**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit/test_client_isolation.py -v`
Expected: 7 passed.

- [ ] **Step 5: Run the full unit suite**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit -q 2>&1 | tail -30`
Expected: failures now confined to `test_hermes_adapter.py` and `test_mcp_server.py` (both still
passing the now-removed `isolate_by_agent=` kwarg directly to `retrieve()` via their adapter
code) — Task 5/6's job. `test_audit_contracts.py`'s retrieve-payload-schema test should still
pass unchanged (the `isolate_by_agent` audit field's presence/shape didn't change, only its
source).

- [ ] **Step 6: Commit**

```bash
git add src/agentdb/interface/client.py tests/unit/test_client_isolation.py
git commit -m "feat: drop isolate_by_agent parameter from retrieve()/retrieve_by_entities(), derive from config_for()"
```

---

### Task 5: Simplify `AgentDBClaudeMiddleware` and `AgentDBHermesProvider`

**Files:**
- Modify: `src/agentdb/adapters/claude_agent_sdk.py`
- Modify: `src/agentdb/adapters/hermes.py`
- Modify: `tests/integration/test_claude_agent_sdk_adapter.py`
- Modify: `tests/unit/test_hermes_adapter.py`

**Interfaces:**
- Consumes: `AgentDB.config_for(agent_id)` from Task 3; `retrieve()`'s new signature from Task 4.
- Produces: `AgentDBClaudeMiddleware(db, agent_id, conversation_started_callback=None)` and
  `AgentDBHermesProvider(db, agent_id)` — neither takes `profile`/`retrieve_limit`/
  `memory_budget_chars`/`memory_budget_tokens`/`isolate_by_agent` anymore.

- [ ] **Step 1: Write the failing tests**

In `tests/integration/test_claude_agent_sdk_adapter.py`, every existing construction of
`AgentDBClaudeMiddleware(db=db, agent_id=..., profile=..., ...)` needs its `profile=`/
`retrieve_limit=`/`memory_budget_chars=`/`memory_budget_tokens=` kwargs removed, and every test
that relies on `isolate_by_agent=True` being passed to the constructor instead needs a
`memory_dir=`-backed `db` with that agent listed under `profile.agents` (same pattern as Task
4's `_mem_dir_with_isolated_agent`). Concretely:

- `test_prepare_turn_enforces_retrieve_and_builds_messages`: change
  `AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")` to
  `AgentDBClaudeMiddleware(db=db, agent_id="research_agent")`.
- `test_prepare_turn_isolate_by_agent_true_excludes_other_agent_records` and
  `test_prepare_turn_isolate_by_agent_default_false_returns_mixed_pool`: rename to
  `test_prepare_turn_isolates_when_agent_listed_under_profile_agents` and
  `test_prepare_turn_returns_mixed_pool_when_agent_not_listed`; construct `db` via
  `AgentDB(memory_dir=..., permissions_path=PERMS)` with (for the isolated case)
  `{"profile": {"name": "budget_optimized", "agents": {"research_agent": {}}}}` written to
  `memory.yaml` first, and construct the middleware as
  `AgentDBClaudeMiddleware(db=db, agent_id="research_agent")` (no `isolate_by_agent=` kwarg —
  it no longer exists).
- `test_prepare_turn_orders_override_instructions_before_memory_content`: change
  `AgentDBClaudeMiddleware(db=db, agent_id="research_agent", profile="budget_optimized")` to
  `AgentDBClaudeMiddleware(db=db, agent_id="research_agent")`.
- `test_prepare_turn_passes_memory_budget_tokens_and_reports_truncation` and
  `test_prepare_turn_without_memory_budget_tokens_does_not_truncate`: these constructed the
  middleware with an explicit `memory_budget_tokens=` kwarg to force truncation at a known
  size. Rewrite both to build a `memory_dir`-backed `db` with
  `{"profile": {"name": "budget_optimized", "injection": {"context_token_limit": <small number>}}}`
  for the truncating case, and the plain default profile (no override) for the
  non-truncating case, constructing `AgentDBClaudeMiddleware(db=db, agent_id=...)` with no
  budget kwarg in either case.
- All other existing tests in this file that construct `AgentDBClaudeMiddleware` with only
  `db=`/`agent_id=`/`profile="budget_optimized"` (no other override kwargs): just drop the
  `profile=` kwarg, no other change.

In `tests/unit/test_hermes_adapter.py`: the `isolated_provider` fixture
(`AgentDBHermesProvider(db=db, agent_id="test-agent", isolate_by_agent=True)`) needs to
become a `db` (via `memory_dir=`) with `"test-agent"` listed under `profile.agents`, and
`AgentDBHermesProvider(db=db, agent_id="test-agent")` (no kwarg). The plain `provider` fixture
just drops nothing (it never passed `isolate_by_agent`) but needs to point at a `db` that
either has no `memory_dir` at all or has `memory_dir` with `"test-agent"` *not* listed, to keep
testing the non-isolated path explicitly (use the existing no-`memory_dir`
`AgentDB(db_path=tmp_path / "test.db")` construction — unaffected, never isolated by
definition since there's no `profile.agents` at all).

- [ ] **Step 2: Run tests to verify they fail**

Run:
```
source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb
python3 -m pytest tests/integration/test_claude_agent_sdk_adapter.py tests/unit/test_hermes_adapter.py -v 2>&1 | tail -60
```
Expected: `TypeError: __init__() got an unexpected keyword argument 'profile'` (or
`'isolate_by_agent'`) for the tests that still pass now-removed kwargs; tests already rewritten
to the new construction pattern fail differently (e.g. isolation tests showing a mixed pool)
because the adapter code itself hasn't changed yet.

- [ ] **Step 3: Write minimal implementation**

In `src/agentdb/adapters/claude_agent_sdk.py`, delete `ClaudeAdapterProfile`,
`FULL_STORAGE_PROFILE`, `BUDGET_OPTIMIZED_PROFILE` entirely. Change:

```python
    def __init__(
        self,
        db: "AgentDB",
        agent_id: str,
        profile: str = "budget_optimized",
        retrieve_limit: Optional[int] = None,
        memory_budget_chars: Optional[int] = None,
        memory_budget_tokens: Optional[int] = None,
        conversation_started_callback: Optional[Callable[..., Any]] = None,
        isolate_by_agent: bool = False,
    ):
        super().__init__(db, agent_id, profile)
        self._conversation_started_callback = conversation_started_callback
        self._isolate_by_agent = isolate_by_agent
        if profile == "full_storage":
            base = FULL_STORAGE_PROFILE
        else:
            base = BUDGET_OPTIMIZED_PROFILE
        self.profile = ClaudeAdapterProfile(
            name=base.name,
            retrieve_limit=retrieve_limit if retrieve_limit is not None else base.retrieve_limit,
            memory_budget_chars=(
                memory_budget_chars if memory_budget_chars is not None else base.memory_budget_chars
            ),
            auto_persist_response=base.auto_persist_response,
            memory_budget_tokens=(
                memory_budget_tokens if memory_budget_tokens is not None else base.memory_budget_tokens
            ),
        )
```

to:

```python
    def __init__(
        self,
        db: "AgentDB",
        agent_id: str,
        conversation_started_callback: Optional[Callable[..., Any]] = None,
    ):
        resolved = db.config_for(agent_id)
        super().__init__(db, agent_id, profile=resolved["name"])
        self._conversation_started_callback = conversation_started_callback
        self._resolved = resolved
```

(`GatewayAdapter.__init__` only stores `profile` as `self.profile_name` for informational
purposes — confirmed via `grep -rn "profile_name"` that nothing else in the codebase reads it —
so passing the resolved profile's own `name` field, computed one line earlier, is correct and
needs no other change to the base class.)

Update `prepare_turn()`'s retrieve call:

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

to:

```python
        retrieval = self._db.retrieve(
            query=user_query,
            agent_id=self._agent_id,
            limit=self._resolved["retrieval"]["retrieve_limit"],
            external_context=external_context,
            session_id=session_id,
            budget_tokens=self._resolved["injection"]["context_token_limit"],
        )
```

Update the message-building block (already reordered per the Principle 9 fix) to use the
configured header/prefix instead of the hardcoded literals:

```python
        for instruction in retrieval.override_instructions:
            messages.append({"role": "system", "content": f"Override instruction: {instruction}"})

        memory_context = self._build_memory_context(retrieval)
        if memory_context:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Governed memory context (higher priority than inferred context):\n"
                        f"{memory_context}"
                    ),
                }
            )
```

to:

```python
        override_prefix = self._resolved["injection"]["override_prefix"]
        for instruction in retrieval.override_instructions:
            messages.append({"role": "system", "content": f"{override_prefix} {instruction}"})

        memory_context = self._build_memory_context(retrieval)
        if memory_context:
            messages.append(
                {
                    "role": "system",
                    "content": f"{self._resolved['injection']['memory_header']}\n{memory_context}",
                }
            )
```

Update `_build_memory_context()`:

```python
    def _build_memory_context(self, retrieval: "RetrievalResult") -> str:
        if not retrieval.records:
            return ""
        remaining = self.profile.memory_budget_chars
        chunks: list[str] = []
        for record in retrieval.records:
            value = record.value
            if isinstance(value, (dict, list)):
                value_text = json.dumps(value, ensure_ascii=True)
            else:
                value_text = str(value)
            line = f"- {record.key}: {value_text}"
            if len(line) > remaining:
```

to:

```python
    def _build_memory_context(self, retrieval: "RetrievalResult") -> str:
        if not retrieval.records:
            return ""
        remaining = self._resolved["injection"]["memory_budget_chars"]
        line_format = self._resolved["injection"]["record_line_format"]
        chunks: list[str] = []
        for record in retrieval.records:
            value = record.value
            if isinstance(value, (dict, list)):
                value_text = json.dumps(value, ensure_ascii=True)
            else:
                value_text = str(value)
            line = line_format.format(key=record.key, value=value_text)
            if len(line) > remaining:
```

(leave the rest of the loop body below this point unchanged — only the two lines shown above,
computing `remaining`/`line_format` and building `line`, change)

Apply the equivalent constructor simplification to `AgentDBHermesProvider` in
`src/agentdb/adapters/hermes.py`:

```python
    def __init__(
        self,
        db: "AgentDB",
        agent_id: str,
        profile: str = "budget_optimized",
        isolate_by_agent: bool = False,
    ) -> None:
        GatewayAdapter.__init__(self, db, agent_id, profile)
        self._session_id: str = ""
        self._isolate_by_agent = isolate_by_agent
```

to:

```python
    def __init__(self, db: "AgentDB", agent_id: str) -> None:
        GatewayAdapter.__init__(self, db, agent_id, profile=db.config_for(agent_id)["name"])
        self._session_id: str = ""
```

Update `prefetch()` and `handle_tool_call()`'s `"memory_search"` branch to drop the
`isolate_by_agent=self._isolate_by_agent` kwarg from their `self._db.retrieve(...)` calls
entirely (the parameter no longer exists on `retrieve()`).

- [ ] **Step 4: Run tests to verify they pass**

Run:
```
source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb
python3 -m pytest tests/integration/test_claude_agent_sdk_adapter.py tests/unit/test_hermes_adapter.py -v
```
Expected: all passing (same counts as before this task started — 37 in the adapter file, 16 in
the hermes file, per the prior spec's final state).

- [ ] **Step 5: Run the full unit + this integration file**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit tests/integration/test_claude_agent_sdk_adapter.py -q 2>&1 | tail -20`
Expected: only `test_mcp_server.py` still failing (Task 6's job).

- [ ] **Step 6: Commit**

```bash
git add src/agentdb/adapters/claude_agent_sdk.py src/agentdb/adapters/hermes.py tests/integration/test_claude_agent_sdk_adapter.py tests/unit/test_hermes_adapter.py
git commit -m "feat: simplify AgentDBClaudeMiddleware/AgentDBHermesProvider to read all config from db.config_for(agent_id)"
```

---

### Task 6: MCP `memory_retrieve` tool — drop `isolate_by_agent` parameter

**Files:**
- Modify: `src/agentdb/interface/mcp_server.py`
- Modify: `tests/unit/test_mcp_server.py`

**Interfaces:**
- Consumes: `retrieve()`'s new signature from Task 4.
- Produces: `memory_retrieve(query, agent_id, limit=None, session_id=None, budget_tokens=None) -> dict`
  — no `isolate_by_agent` parameter.

- [ ] **Step 1: Write the failing test changes**

In `tests/unit/test_mcp_server.py`, change:

```python
def test_memory_retrieve_isolates_by_agent_when_requested(db):
    from agentdb.interface.mcp_server import memory_write, memory_retrieve
    memory_write(key="mine", value="widget note", origin="agent_inferred", agent_id="a")
    memory_write(key="other", value="widget note", origin="agent_inferred", agent_id="b")

    result = memory_retrieve(query="widget", agent_id="a", isolate_by_agent=True)

    assert [r["key"] for r in result["records"]] == ["mine"]
```

to:

```python
def test_memory_retrieve_isolates_when_agent_listed_under_profile_agents(db, monkeypatch):
    from agentdb.interface import mcp_server
    from agentdb import AgentDB
    import yaml

    mem_dir = db.memory_dir if getattr(db, "memory_dir", None) else None
    # This fixture's `db` (see reset_server_db) has no memory_dir -- build a
    # fresh one with agent "a" listed under profile.agents, and point the
    # module-level singleton at it for this test only.
    import pathlib
    d = pathlib.Path(str(db._path)).parent / "isolated_mem"
    (d / "config").mkdir(parents=True)
    (d / "config" / "memory.yaml").write_text(
        yaml.dump({"profile": {"name": "budget_optimized", "agents": {"a": {}}}}), encoding="utf-8",
    )
    isolated_db = AgentDB(memory_dir=d)
    monkeypatch.setattr(mcp_server, "_db", isolated_db)

    from agentdb.interface.mcp_server import memory_write, memory_retrieve
    memory_write(key="mine", value="widget note", origin="agent_inferred", agent_id="a")
    memory_write(key="other", value="widget note", origin="agent_inferred", agent_id="b")

    result = memory_retrieve(query="widget", agent_id="a")

    assert [r["key"] for r in result["records"]] == ["mine"]
```

Leave `test_memory_retrieve_default_returns_mixed_pool` exactly as-is (it never passed
`isolate_by_agent=` — it already tests the non-isolated default path, still correct after this
task since a `db` with no `memory_dir` is never isolated for any agent).

- [ ] **Step 2: Run tests to verify they fail**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit/test_mcp_server.py -v`
Expected: the rewritten test fails with `TypeError: memory_retrieve() got an unexpected keyword argument` — wait, it no longer passes that kwarg, so instead it should fail because `memory_retrieve` doesn't yet call `config_for`/pass through isolation at all — confirm the actual failure is the isolation assertion (mixed pool returned) rather than a signature error, since the tool function itself hasn't changed yet this step.

- [ ] **Step 3: Write minimal implementation**

In `src/agentdb/interface/mcp_server.py`, change:

```python
def memory_retrieve(
    query: str,
    agent_id: str,
    limit: int = 5,
    session_id: Optional[str] = None,
    budget_tokens: Optional[int] = None,
    isolate_by_agent: bool = False,
) -> dict:
    """Retrieve governed memory records ranked by multi-signal ranker."""
    result = _get_db().retrieve(
        query=query,
        agent_id=agent_id,
        limit=limit,
        session_id=session_id,
        budget_tokens=budget_tokens,
        isolate_by_agent=isolate_by_agent,
    )
```

to:

```python
def memory_retrieve(
    query: str,
    agent_id: str,
    limit: Optional[int] = None,
    session_id: Optional[str] = None,
    budget_tokens: Optional[int] = None,
) -> dict:
    """Retrieve governed memory records ranked by multi-signal ranker."""
    result = _get_db().retrieve(
        query=query,
        agent_id=agent_id,
        limit=limit,
        session_id=session_id,
        budget_tokens=budget_tokens,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb && python3 -m pytest tests/unit/test_mcp_server.py -v`
Expected: all passing.

- [ ] **Step 5: Run the full test suite one final time**

Run:
```
source /Users/jm/Documents/github/slack-assistant/.venv/bin/activate && cd /Users/jm/Documents/github/agentdb
python3 -m pytest tests/unit tests/integration/test_claude_agent_sdk_adapter.py -q
```
Expected: all passing, zero failures.

- [ ] **Step 6: Commit**

```bash
git add src/agentdb/interface/mcp_server.py tests/unit/test_mcp_server.py
git commit -m "feat: drop isolate_by_agent parameter from memory_retrieve MCP tool"
```

## Verification Summary

- `pytest tests/unit tests/integration/test_claude_agent_sdk_adapter.py -q` — all green, zero regressions, at every task boundary.
- `grep -rn "ClaudeAdapterProfile\|_MODE_DEFAULTS\|isolate_by_agent" src/agentdb/` after Task 6 should show: no `ClaudeAdapterProfile`/`_MODE_DEFAULTS` references anywhere (fully deleted), and `isolate_by_agent` appearing only as a dict *key* produced by `config_for()` and consumed inside `retrieve()`/`retrieve_by_entities()`/the audit-log payload — never as a function *parameter* anywhere.
- Manually construct an `AgentDB(memory_dir=...)` with a `profile.agents` entry and confirm `db.config_for(that_agent_id)["isolate_by_agent"] is True`, and for an agent not listed, `is False` — the derived-not-parameterized contract this whole plan exists to establish.
