from __future__ import annotations

import dataclasses
import json
import os
import warnings
from types import SimpleNamespace
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

import yaml

from ..core.models import MemoryRecord, SessionProfile
from ..core.profiles import BUILTIN_PROFILES
from ..core.store import MemoryStore
from ..core.system import SystemStats
from ..gateway import Gateway
from ..governance.conflict_detector import ConflictDetector
from ..governance.lifecycle_manager import LifecycleManager
from ..governance.permission_router import PermissionDenied, PermissionRouter
from ..retrieval.algorithm import MultiSignalRanker
from ..core.sqlite_connection import connected

if TYPE_CHECKING:
    from ..governance.lifecycle_manager import ConsolidationResult
    from ..ingest import IngestResult, PurgeResult


# ---------------------------------------------------------------------------
# Event constants
# ---------------------------------------------------------------------------

EVENT_MEMORY_WRITE = "memory_write"
EVENT_MEMORY_DELETE = "memory_delete"
EVENT_MEMORY_RETRIEVE = "memory_retrieve"
EVENT_MEMORY_OUTCOME = "memory_outcome"
EVENT_EXTERNAL_CONFLICT_DETECTED = "external_conflict_detected"
EVENT_PERMISSION_DENIED = "permission_denied"


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class AuditEntry:
    event_type: str
    payload: dict
    sequence: int


@dataclass
class RetrievalResult:
    records: list
    conflicts: list
    override_instructions: list
    token_estimate: int = 0
    truncated: bool = False
    # Structured diagnostic trace (ADB-10): per-candidate origin and exclusion
    # reasons, plus the selected/excluded record ids. Not part of ranking; for
    # tests and diagnostics only. Empty when tracing is disabled.
    trace: dict = dataclasses.field(default_factory=dict)


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_CREATE_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_OUTCOMES = """
CREATE TABLE IF NOT EXISTS outcomes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    outcome_type TEXT NOT NULL,
    outcome_value REAL NOT NULL,
    linked_retrieval_sequence INTEGER,
    created_at TEXT NOT NULL
)
"""

_CREATE_EXTERNAL_RETRIEVES = """
CREATE TABLE IF NOT EXISTS external_retrieves (
    session_id TEXT NOT NULL,
    had_external_context INTEGER NOT NULL,
    had_conflict INTEGER NOT NULL DEFAULT 0
)
"""

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _estimate_tokens(record: MemoryRecord) -> int:
    value_str = json.dumps(record.value) if isinstance(record.value, (dict, list)) else str(record.value)
    return max(1, len(value_str) // 4)


# ---------------------------------------------------------------------------
# Session metadata helpers
# ---------------------------------------------------------------------------

_REQUEST_TYPE_RULES: list = [
    ({"review", "feedback", "diff", "pr", "pull request"}, "code_review"),
    ({"implement", "build", "create", "feat", "feature", "add"}, "code_generation"),
    ({"debug", "fix", "error", "bug", "broken", "failing", "traceback"}, "debugging"),
    ({"doc", "readme", "explain", "document", "overview"}, "documentation"),
    ({"research", "brainstorm", "explore", "investigate", "survey"}, "research"),
    ({"plan", "design", "architect", "strategy", "roadmap"}, "planning"),
]

# Stop words excluded from "unique detail" term matching (T06).
_UNIQUE_DETAIL_STOP = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "what", "which",
    "does", "did", "was", "were", "are", "has", "have", "been", "into",
    "about", "over", "when", "then", "them", "they", "your", "you",
})


def _extract_repo(working_dirs) -> "Optional[str]":
    if not working_dirs:
        return None
    last = (working_dirs[-1] or "").rstrip("/")
    segment = last.split("/")[-1] if "/" in last else last
    return segment or None


def _parse_as_of(value):
    """Coerce an as_of spec value (datetime or ISO string) to datetime or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _classify_request_type(text: str) -> str:
    text_lower = text.lower()
    for keywords, request_type in _REQUEST_TYPE_RULES:
        if any(kw in text_lower for kw in keywords):
            return request_type
    return "general"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditLog:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_CREATE_AUDIT_LOG)

    @contextmanager
    def _connect(self):
        with connected(self._db_path) as conn:
            yield conn

    def append(self, event_type: str, payload: dict) -> int:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO audit_log (event_type, payload, created_at) VALUES (?, ?, ?)",
                (event_type, json.dumps(payload), now),
            )
            return cursor.lastrowid

    def replay(self, from_sequence: int = 0) -> list[AuditEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sequence, event_type, payload FROM audit_log WHERE sequence >= ? ORDER BY sequence",
                (from_sequence,),
            ).fetchall()
        return [
            AuditEntry(
                event_type=row["event_type"],
                payload=json.loads(row["payload"]),
                sequence=row["sequence"],
            )
            for row in rows
        ]


# ---------------------------------------------------------------------------
# Profile-based defaults and config resolution
# ---------------------------------------------------------------------------

class ConfigurationError(Exception):
    """Raised for any invalid memory.yaml profile configuration. Never
    silently substituted for a default."""


_OVERRIDE_SECTIONS = ("retrieval", "injection", "lifecycle", "capture")


def _builtin_profile_dict(name: str) -> dict:
    if name not in BUILTIN_PROFILES:
        raise ConfigurationError(
            f"profile.name '{name}' is not a recognized built-in profile "
            f"(expected one of {sorted(BUILTIN_PROFILES.keys())})"
        )
    return dataclasses.asdict(BUILTIN_PROFILES[name])


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _validate_and_merge_overrides(base: dict, profile_block: dict, where: str) -> dict:
    if not isinstance(profile_block, dict):
        raise ConfigurationError(
            f"{where}: expected a mapping (e.g. '{where}:\n  name: ...'), got "
            f"{type(profile_block).__name__} ({profile_block!r})"
        )
    result = dict(base)
    for key in profile_block:
        if key in ("name", "agents"):
            continue
        if key not in _OVERRIDE_SECTIONS:
            raise ConfigurationError(
                f"{where}: unknown key '{key}' (expected one of {_OVERRIDE_SECTIONS}, 'name', or 'agents')"
            )
        section_override = profile_block[key]
        if not isinstance(section_override, dict):
            raise ConfigurationError(
                f"{where}.{key}: expected a mapping, got {type(section_override).__name__} ({section_override!r})"
            )
        base_section = result.get(key, {})
        for field, value in section_override.items():
            if field not in base_section:
                raise ConfigurationError(f"{where}.{key}: unknown field '{field}'")
            base_value = base_section[field]
            if isinstance(base_value, dict):
                if not isinstance(value, dict):
                    raise ConfigurationError(
                        f"{where}.{key}.{field}: expected dict, got {type(value).__name__} ({value!r})"
                    )
                unknown = set(value) - set(base_value)
                if unknown:
                    raise ConfigurationError(f"{where}.{key}.{field}: unknown field(s) {sorted(unknown)}")
                for sub_field, sub_value in value.items():
                    expected_sub_type = type(base_value[sub_field])
                    if not isinstance(sub_value, expected_sub_type):
                        raise ConfigurationError(
                            f"{where}.{key}.{field}.{sub_field}: expected {expected_sub_type.__name__}, "
                            f"got {type(sub_value).__name__} ({sub_value!r})"
                        )
            else:
                expected_type = type(base_value)
                if not isinstance(value, expected_type):
                    raise ConfigurationError(
                        f"{where}.{key}.{field}: expected {expected_type.__name__}, got {type(value).__name__} ({value!r})"
                    )
        result[key] = _deep_merge(base_section, section_override)
    return result


_LEGACY_ROOT_KEYS = ("mode", "profiles", "retrieval", "injection", "lifecycle", "capture")


def _resolve_config(yaml_dict: dict) -> dict:
    """
    Resolve the final config dict from the new single-root `profile:` schema.
    Priority (low -> high): built-in profile defaults, then override sections
    (retrieval/injection/lifecycle/capture) declared directly under profile:.
    Raises ConfigurationError on any unrecognized name, key, or wrong type --
    never silently substitutes a default.
    """
    if yaml_dict:
        stale_keys = [k for k in _LEGACY_ROOT_KEYS if k in yaml_dict]
        if stale_keys:
            raise ConfigurationError(
                f"memory.yaml has old-schema root key(s) {stale_keys} -- these must nest under "
                f"a single top-level 'profile:' key now (e.g. 'capture:' becomes "
                f"'profile.capture:'); consult README.md for the supported configuration schema"
            )
    profile_block = yaml_dict.get("profile", {}) if yaml_dict else {}
    if not isinstance(profile_block, dict):
        raise ConfigurationError(
            f"profile: expected a mapping (e.g. 'profile:\n  name: ...'), got "
            f"{type(profile_block).__name__} ({profile_block!r})"
        )
    name = profile_block.get("name")
    if not name:
        raise ConfigurationError("profile.name is required")
    base = _builtin_profile_dict(name)
    return _validate_and_merge_overrides(base, profile_block, where="profile")


# ---------------------------------------------------------------------------
# Default memory.yaml content
# ---------------------------------------------------------------------------

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

# Uncomment to enable semantic retrieval and conflict detection.
# Requires: pip install 'agentdatabase[local-embed]'
# embeddings:
#   enabled: false
#   # model: sentence-transformers/all-MiniLM-L6-v2   # optional; this is the default

# Uncomment to enable governance notifications.
# notifications:
#   # events: omit to receive the default, or list a subset:
#   #   conflict_detected
#   channels:
#     - type: email
#       to: ops@example.com
#       # smtp_host: localhost     # default: localhost
#       # smtp_port: 25            # default: 25
#       # from_address: agentdb@localhost
#       # smtp_user: user
#       # smtp_password: secret
#       # use_tls: false
#     - type: slack
#       bot_token: "xoxb-..."     # requires: pip install agentdatabase[notifications]
#       channel: "#ops-alerts"    # channel name, ID, or user ID
"""


# ---------------------------------------------------------------------------
# Main AgentDB class
# ---------------------------------------------------------------------------

class AgentDB:
    def __init__(
        self,
        path: Optional[Any] = None,
        memory_dir: Optional[Any] = None,
        permissions_path: Optional[Any] = None,
        tracking_directories: Optional[list[str]] = None,
        tracking_root: Any = ".",
        tracking_max_files_per_directory: Optional[int] = None,
        db_path: Optional[Any] = None,
        summarizer: Optional[Callable] = None,
        infer_fn: Optional[Callable] = None,
        embed_fn: Optional[Callable] = None,
        local_embed: bool = False,
        authority_verifier: Optional[Callable] = None,
    ) -> None:
        # db_path is an alias for path (used by adapter tests)
        if db_path is not None and path is None:
            path = db_path
        self._summarizer = summarizer
        # FINAL-HARDENING §4: trusted-host authority verifier. Configured by the
        # host at init (NOT passed by the requesting agent). Receives an approval
        # claim dict and returns an affirmative allow/deny decision. Without it,
        # verified authority cannot be created (fail closed).
        self._authority_verifier = authority_verifier
        self._infer_fn = infer_fn
        # Resolve memory_dir from args or env vars
        resolved_memory_dir: Optional[Path] = None
        if memory_dir is not None:
            resolved_memory_dir = Path(memory_dir).resolve()
        else:
            env_memory_dir = os.environ.get("AGENTDB_MEMORY_DIR")
            env_home = os.environ.get("AGENTDB_HOME")
            if env_memory_dir:
                resolved_memory_dir = Path(env_memory_dir).resolve()
            elif env_home:
                resolved_memory_dir = Path(env_home).resolve()

        self.memory_dir: Optional[Path] = resolved_memory_dir

        # Set up workspace directories if memory_dir is provided
        self.memory_config_path: Optional[Path] = None
        self.memory_config: dict = {}

        if resolved_memory_dir is not None:
            # Create workspace layout
            for subdir in ("config", "database", "conversations", "embeddings", "reconciliation", "logs"):
                (resolved_memory_dir / subdir).mkdir(parents=True, exist_ok=True)

            config_path = resolved_memory_dir / "config" / "memory.yaml"
            self.memory_config_path = config_path.resolve()

            if not config_path.exists():
                config_path.write_text(_DEFAULT_MEMORY_YAML, encoding="utf-8")

            with config_path.open("r", encoding="utf-8") as f:
                self.memory_config = yaml.safe_load(f) or {}

            # Validate full_storage profile. profile may be a non-dict (e.g. a
            # bare scalar like `profile: research`) here -- that's a config
            # error, but it's _resolve_config's job (below) to raise it with a
            # proper ConfigurationError; this check just needs to not crash on
            # its way there.
            _profile_block_for_check = self.memory_config.get("profile", {})
            if not isinstance(_profile_block_for_check, dict):
                _profile_block_for_check = {}
            if _profile_block_for_check.get("name") == "full_storage":
                td = self.memory_config.get("tracked_directories", [])
                if not td:
                    raise ValueError(
                        "tracked_directories must be non-empty when profile.name is 'full_storage'"
                    )

        # Determine DB path
        # ``:memory:`` is SQLite's in-memory sentinel — it must NOT be run
        # through Path.resolve() (which would turn it into a literal ./:memory:
        # file). Preserve it verbatim so a no-file, process-local DB is used.
        if path is not None and str(path) == ":memory:":
            db_path = Path(":memory:")
        elif path is not None:
            db_path = Path(path).resolve()
        elif resolved_memory_dir is not None:
            db_path = (resolved_memory_dir / "database" / "agentdb.sqlite").resolve()
        else:
            db_path = Path(":memory:")

        self._path = db_path

        # For an in-memory DB, hold one anchor connection for the lifetime of
        # this AgentDB so the shared-cache database (and its tables) survives
        # between the per-store connections. Without a live connection, an
        # in-memory database is discarded and later connections see no tables.
        self._mem_anchor = None
        if str(db_path) == ":memory:":
            from ..core.sqlite_connection import connect as _connect_anchor
            self._mem_anchor = _connect_anchor(db_path)
            self._mem_anchor.execute(
                "CREATE TABLE IF NOT EXISTS _agentdb_anchor (id INTEGER PRIMARY KEY)"
            )
            self._mem_anchor.commit()

        # Set up permissions router
        self._permissions: Optional[PermissionRouter] = None
        if permissions_path is not None:
            self._permissions = PermissionRouter(permissions_path)

        # Initialize stores using shared DB path
        self._store = MemoryStore(db_path)
        self.audit_log = AuditLog(db_path)
        self._system = SystemStats(db_path)

        # Initialize outcomes table
        self._init_outcomes_table()

        # Resolve profile-based config. When no memory_dir was provided at
        # all, there is no config file to speak of and we fall back to the
        # budget_optimized built-in directly -- this is "no config given"
        # rather than "invalid config". But if memory_dir WAS provided, any
        # memory_config content (including a blank/empty file that parses to
        # None/{}) goes through _resolve_config's strict profile.name
        # validation -- never a silent fallback.
        if self.memory_dir is not None:
            self._resolved_config = _resolve_config(self.memory_config)
        else:
            self._resolved_config = _builtin_profile_dict("budget_optimized")

        # Eagerly validate every profile.agents.* block now, so a typo in any
        # bot's config is a startup failure, not a failure the first time
        # that specific bot happens to retrieve.
        agents = self.memory_config.get("profile", {}).get("agents", {}) if self.memory_config else {}
        for agent_id in agents:
            self.config_for(agent_id)

        # Tracking config
        self.tracking_directories: list[str] = tracking_directories or []
        self.tracking_root: Path = Path(tracking_root).resolve()
        self.tracking_max_files_per_directory: int = (
            tracking_max_files_per_directory
            if tracking_max_files_per_directory is not None
            else self._resolved_config["capture"]["tracking_max_files_per_directory"]
        )

        # Session tracking: session_id -> list of record ids retrieved
        self._session_record_ids: dict[str, list[str]] = {}
        # Session profiles: session_id -> SessionProfile (metadata + repo filter)
        self._session_profiles: dict[str, SessionProfile] = {}

        # Embedding layer (optional) — must be initialized before ranker
        self._raw_embeddings_cfg: dict = (self.memory_config or {}).get("embeddings", {})
        self._embedding_provider = None
        self._embedding_store = None
        if embed_fn is not None:
            from ..embeddings import FunctionEmbeddingProvider, EmbeddingStore
            self._embedding_provider = FunctionEmbeddingProvider(embed_fn)
        elif local_embed:
            from ..embeddings import LocalEmbeddingProvider, EmbeddingStore
            self._embedding_provider = LocalEmbeddingProvider()
        elif self._raw_embeddings_cfg.get("enabled"):
            from ..embeddings import LocalEmbeddingProvider, EmbeddingStore
            self._embedding_provider = LocalEmbeddingProvider(
                model_name=self._raw_embeddings_cfg.get("model")
            )
        if self._embedding_provider is not None and str(self._path) != ":memory:":
            from ..embeddings import EmbeddingStore
            embed_db_path = self._path.parent / "embeddings.db"
            self._embedding_store = EmbeddingStore(
                db_path=embed_db_path,
                model_name=self._embedding_provider.model_name,
                dimensions=self._embedding_provider.dimensions,
            )

        # Ranker with weights from resolved config; semantic weight activated when provider present
        self._ranker = self._build_ranker()

        # Gateway surface (initialized after all dependencies are ready)
        self._gateway: Optional[Gateway] = None

        # Register session topic handler when embedding is active
        if self._embedding_provider is not None:
            self.gateway.subscribe("conversation_started", self._handle_conversation_started_embedding)
        # Metadata handler always active (repo filter + auto-trigger — no embeddings required)
        self.gateway.subscribe("conversation_started", self._handle_conversation_started_metadata)

        # Lifecycle manager — initialized after _session_record_ids is created
        lc = self._resolved_config["lifecycle"]
        self._lifecycle = LifecycleManager(
            store=self._store,
            system=self._system,
            session_record_ids=self._session_record_ids,
            embedding_store=self._embedding_store,
            confidence_decay_rate=lc["confidence_decay_rate"],
            archival_threshold=lc["archival_threshold"],
            staleness_days=lc["staleness_days"],
            reinforce_rate=lc["reinforce_rate"],
        )

        # Notifications — wired last so all gateway subscribers are already registered
        self._setup_notifications()

    def _setup_notifications(self) -> None:
        """Build and attach NotificationDispatcher from memory.yaml notifications: block."""
        cfg = (self.memory_config or {}).get("notifications")
        if not cfg:
            return

        from ..notifications import EmailChannel, SlackChannel, NotificationDispatcher

        channels = []
        for ch_cfg in cfg.get("channels", []):
            ch_type = ch_cfg.get("type")
            if ch_type == "email":
                to = ch_cfg.get("to")
                if not to:
                    raise ConfigurationError("notifications.channels[email]: 'to' is required")
                channels.append(EmailChannel(
                    to_address=to,
                    from_address=ch_cfg.get("from_address", "agentdb@localhost"),
                    smtp_host=ch_cfg.get("smtp_host", "localhost"),
                    smtp_port=int(ch_cfg.get("smtp_port", 25)),
                    smtp_user=ch_cfg.get("smtp_user"),
                    smtp_password=ch_cfg.get("smtp_password"),
                    use_tls=bool(ch_cfg.get("use_tls", False)),
                ))
            elif ch_type == "slack":
                token = ch_cfg.get("bot_token")
                channel = ch_cfg.get("channel")
                if not token or not channel:
                    raise ConfigurationError(
                        "notifications.channels[slack]: 'bot_token' and 'channel' are required"
                    )
                channels.append(SlackChannel(bot_token=token, channel=channel))
            else:
                raise ConfigurationError(
                    f"notifications.channels: unknown type {ch_type!r} (supported: email, slack)"
                )

        if not channels:
            return

        events = cfg.get("events")  # None → dispatcher uses its default list
        dispatcher = NotificationDispatcher(self.gateway, channels=channels, events=events)
        dispatcher.attach()

    def _build_ranker(self) -> "MultiSignalRanker":
        rw = self._resolved_config["retrieval"]["ranker_weights"]
        lc = self._resolved_config["lifecycle"]
        staleness = lc["staleness_days"]
        if self._embedding_provider is not None:
            w = float(self._raw_embeddings_cfg.get("semantic_ranker_weight", 0.20))
            scale = 1.0 - w
            return MultiSignalRanker(
                weight_provenance=rw["provenance"] * scale,
                weight_recency=rw["recency"] * scale,
                weight_confidence=rw["confidence"] * scale,
                weight_causal=rw["causal"] * scale,
                weight_semantic=w,
                weight_workspace=rw.get("workspace", 0.0) * scale,
                staleness_days=staleness,
            )
        return MultiSignalRanker(
            weight_provenance=rw["provenance"],
            weight_recency=rw["recency"],
            weight_confidence=rw["confidence"],
            weight_causal=rw["causal"],
            weight_semantic=rw["semantic"],
            weight_workspace=rw.get("workspace", 0.0),
            staleness_days=staleness,
        )

    def _handle_conversation_started_embedding(self, **kwargs) -> None:
        session_id = kwargs.get("session_id")
        opening_message = kwargs.get("opening_message", "")
        if not session_id or not opening_message:
            return
        try:
            vec = self._embedding_provider.embed(opening_message)
            self._embedding_store.put_session_topic(session_id, vec)
        except Exception:
            pass

    def _handle_conversation_started_metadata(self, **kwargs) -> None:
        session_id = kwargs.get("session_id")
        if not session_id:
            return
        agent_id = kwargs.get("agent_id")
        opening_message = kwargs.get("opening_message", "")
        working_dirs = kwargs.get("working_dirs") or []
        repo = _extract_repo(working_dirs)
        rt = _classify_request_type(opening_message) if opening_message else "general"
        profile = SessionProfile(session_id=session_id, agent_id=agent_id)
        if repo:
            profile.repos.add(repo)
        profile.request_types.add(rt)
        self._session_profiles[session_id] = profile
        # Auto-consolidation: increment session count; trigger if threshold reached
        if agent_id:
            try:
                self.lifecycle._increment_session_count(agent_id)
                if self.lifecycle._should_auto_consolidate(agent_id):
                    self.consolidate(agent_id, since_hours=0)
            except Exception:
                pass

    @property
    def _reconciliation_handlers(self) -> dict[str, list[Callable]]:  # type: ignore[override]
        """Deprecated — handlers now live in db.gateway._handlers."""
        warnings.warn(
            "AgentDB._reconciliation_handlers is deprecated and will be removed in a future version. "
            "Use db.gateway._handlers instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.gateway._handlers

    @property
    def gateway(self) -> Gateway:
        """Unified governance surface: reconciliation and conflict queue."""
        if self._gateway is None:
            self._gateway = Gateway(self)
        return self._gateway

    @property
    def system(self) -> SystemStats:
        """System statistics surface."""
        return self._system

    @property
    def lifecycle(self) -> LifecycleManager:
        """Lifecycle manager: sweep, reinforce, record_outcome."""
        return self._lifecycle

    @property
    def config(self) -> dict:
        """Fully resolved config: profile defaults merged with memory.yaml overrides."""
        return self._resolved_config

    def config_for(self, agent_id: str) -> dict:
        """Resolved config for a specific agent: the global profile resolution,
        with this agent's own overrides (if listed under profile.agents) merged
        on top. isolate_by_agent is derived here -- True iff agent_id is listed
        -- and is never accepted as an input anywhere in this codebase."""
        agents = self.memory_config.get("profile", {}).get("agents", {}) if self.memory_config else {}
        if agent_id not in agents:
            return {**self.config, "isolate_by_agent": False}

        agent_block = agents[agent_id] or {}
        base = _builtin_profile_dict(agent_block["name"]) if "name" in agent_block else self.config
        resolved = _validate_and_merge_overrides(base, agent_block, where=f"profile.agents.{agent_id}")
        return {**resolved, "isolate_by_agent": True}

    def _init_outcomes_table(self) -> None:
        with self._store._connect() as conn:
            conn.execute(_CREATE_OUTCOMES)
            conn.execute(_CREATE_EXTERNAL_RETRIEVES)

    def _derive_trust_level(self, origin: str) -> str:
        if origin == "human_approved":
            return "human"
        elif origin == "provider_ingested":
            return "provider"
        else:
            return "agent"

    def _derive_trust_zone(self, origin: str) -> str:
        """Derive the trust zone from origin for permission checks."""
        if origin == "human_approved":
            return "human_approved"
        return "agent_inferred"

    def write(
        self,
        key: str,
        value: Any,
        origin: str,
        agent_id: str,
        session_id: Optional[str] = None,
        entities: Optional[list] = None,
        confidence: Optional[float] = None,
        infer: bool = False,
        expiry_time: Optional["datetime"] = None,
        working_dirs: Optional[list[str]] = None,
        approval: Optional[dict] = None,
        environment: Optional[str] = None,
        task_type: Optional[str] = None,
        workspace_id: Optional[str] = None,
        repository_id: Optional[str] = None,
        observed_at: Optional["datetime"] = None,
        effective_from: Optional["datetime"] = None,
        effective_until: Optional["datetime"] = None,
    ) -> MemoryRecord:
        """Write a memory record.

        ``approval`` (OD-2): a trusted host may submit verified approval evidence
        as ``{principal, mechanism, timestamp, digest}``. If present and its
        ``digest`` matches the value being written, the record gains *verified*
        human authority. Without it, ``origin="human_approved"`` is only
        *asserted* authority. A caller cannot escalate to verified authority by
        passing an origin string alone.
        """
        if infer:
            if self._infer_fn is None:
                raise ValueError(
                    "infer=True requires an infer_fn — pass infer_fn=<callable> to AgentDB()"
                )
            value = self._infer_fn(value)

        trust_zone = self._derive_trust_zone(origin)

        # Permission check
        if self._permissions is not None:
            try:
                self._permissions.check_write(key, agent_id, trust_zone)
            except PermissionDenied as e:
                self.audit_log.append(
                    EVENT_PERMISSION_DENIED,
                    {
                        "key": key,
                        "agent_id": agent_id,
                        "trust_zone": trust_zone,
                        "reason": str(e),
                    },
                )
                raise

        trust_level = self._derive_trust_level(origin)
        record = MemoryRecord(
            key=key,
            value=value,
            origin=origin,
            trust_level=trust_level,
            agent_id=agent_id,
            confidence=confidence if confidence is not None else 1.0,
            entities=list(entities) if entities else [],
            expiry_time=expiry_time,
            inferred=infer,
            working_dirs=[os.path.realpath(d) for d in (working_dirs or [])],
            session_id=session_id,
            environment=environment,
            task_type=task_type,
            workspace_id=workspace_id,
            repository_id=repository_id,
            observed_at=observed_at,
            effective_from=effective_from,
            effective_until=effective_until,
        )

        # FINAL-HARDENING §4: caller-supplied approval metadata is stored as
        # *unverified* evidence only. Ordinary writes can NEVER create verified
        # authority — any verified/version/fingerprint claims are stripped and
        # the record is marked unverified. Verified authority is granted solely
        # through approve_authority() + the trusted-host verifier.
        if approval:
            ap = dict(approval)
            ap.pop("verified", None)
            ap.pop("version", None)
            ap.pop("fingerprint", None)
            ap.pop("revoked", None)
            ap["verified"] = False
            record.approval = ap

        # Conflict detection: check before persisting
        existing = self._store.get_by_key(key)
        if existing is not None and not existing.archived:
            conflict = ConflictDetector().check(new_record=record, existing=existing)
            if conflict is not None:
                record.conflict_id = conflict.id
                existing.conflict_id = conflict.id
                self._store.update_conflict_id(existing.id, conflict.id)
                self._store.store_conflict(conflict)
                self.gateway.dispatch(
                    "conflict_detected",
                    conflict_id=conflict.id,
                    conflict_type=conflict.conflict_type,
                    key=key,
                    agent_id=agent_id,
                    session_id=session_id,
                )
                self.audit_log.append(
                    "conflict_detected",
                    {
                        "conflict_id": conflict.id,
                        "conflict_type": conflict.conflict_type,
                        "key": key,
                        "record_a_id": existing.id,
                        "record_b_id": record.id,
                        "agent_id": agent_id,
                    },
                )

        self._store.write(record)

        # Expand session profile with this write's repo and request type
        if session_id and session_id in self._session_profiles:
            _repo = _extract_repo(record.working_dirs)
            _rt = _classify_request_type(f"{key} {record.value!s}")
            _profile = self._session_profiles[session_id]
            if _repo:
                _profile.repos.add(_repo)
            _profile.request_types.add(_rt)

        # Store embedding (non-blocking — failures never propagate to caller)
        if self._embedding_provider is not None and self._embedding_store is not None:
            try:
                from ..embeddings.text import _embed_text_for_record
                vec = self._embedding_provider.embed(_embed_text_for_record(record))
                self._embedding_store.put(
                    "memories", record.id, vec, working_dirs=record.working_dirs
                )
            except Exception:
                pass

            # Semantic conflict detection (requires embedding to be stored first)
            try:
                threshold = float(
                    self._raw_embeddings_cfg.get("semantic_conflict_threshold", 0.92)
                )
                recent = [r for r in self._store.list_all()
                          if r.agent_id == agent_id and r.id != record.id and not r.archived]
                recent.sort(key=lambda r: r.created_at, reverse=True)
                sem_conflicts = ConflictDetector.check_semantic_batch(
                    new_record=record,
                    candidates=recent,
                    embedding_store=self._embedding_store,
                    threshold=threshold,
                )
                for sc in sem_conflicts:
                    self._store.store_conflict(sc)
            except Exception:
                pass

        self.audit_log.append(
            EVENT_MEMORY_WRITE,
            {
                "key": key,
                "agent_id": agent_id,
                "session_id": session_id,
                "record_id": record.id,
            },
        )
        return record

    def read(self, key: str, agent_id: Optional[str] = None) -> Optional[MemoryRecord]:
        """Read a record by key.

        OD-3: when a permissions policy is configured (isolation mode), read
        authorization is enforced. A caller without identity, or whose identity
        may not read ``key``, gets ``PermissionDenied`` (fail-closed). In the
        default trusted mode (no ``permissions_path``) behavior is unchanged.
        """
        record = self._store.get_by_key(key)
        if self._permissions is not None:
            if record is not None:
                # Authorize against the actual record (handles canonical
                # source-visibility, not just the key prefix).
                if not self._can_read_record(record, agent_id):
                    # Audit + raise, mirroring key-based denial.
                    self._authorize_read_key(key, agent_id)
                    raise PermissionDenied(f"read denied for '{key}'")
            else:
                # No record: still enforce identity/key policy so a denied key
                # fails closed (and audits) even when absent.
                self._authorize_read_key(key, agent_id)
        return record

    def _authorize_read_key(self, key: str, agent_id: Optional[str]) -> None:
        """Enforce configured read authorization for a single key (OD-3).

        No-op in the default trusted mode. In isolation mode (a permissions
        policy is present) a missing caller identity or a denied key raises
        :class:`PermissionDenied`.
        """
        if self._permissions is None:
            return
        if not agent_id:
            raise PermissionDenied(
                "read authorization requires a caller identity (agent_id) when a "
                "permissions policy is configured"
            )
        try:
            self._permissions.check_read(key, agent_id)
        except PermissionDenied as e:
            self.audit_log.append(
                EVENT_PERMISSION_DENIED,
                {"action": "read", "key": key, "agent_id": agent_id, "reason": str(e)},
            )
            raise

    def _can_read_record(self, record: MemoryRecord, agent_id: Optional[str]) -> bool:
        """Return True if *agent_id* may read *record* under the configured policy.

        Trusted mode (no policy) → always True. Isolation mode → False when the
        caller has no identity or the key is denied. A derived canonical is only
        readable if ALL of its sources are readable (no leak through summaries).
        """
        if self._permissions is None:
            return True
        if not agent_id:
            return False
        # Derived canonicals live under memory/canonical/... rather than an
        # agent scope. Their visibility is defined by their *sources*: a caller
        # may see a canonical iff it may read every contributing source. (OD-3
        # critical rule — no leak of unreadable source content via a summary.)
        if getattr(record, "kind", "source") == "derived_canonical":
            sources = record.source_record_ids or []
            if not sources:
                # No lineage → cannot verify source visibility → deny.
                return False
            for sid in sources:
                src = self._store.get_by_id(sid)
                if src is None:
                    continue
                if not self._can_read_key_simple(src.key, agent_id):
                    return False
            return True
        try:
            self._permissions.check_read(record.key, agent_id)
        except PermissionDenied:
            return False
        return True

    def _can_read_key_simple(self, key: str, agent_id: str) -> bool:
        try:
            self._permissions.check_read(key, agent_id)
            return True
        except PermissionDenied:
            return False

    # ------------------------------------------------------------------
    # OD-2 — trusted-host approval pathway
    # ------------------------------------------------------------------

    @staticmethod
    def _content_digest(value) -> str:
        import hashlib
        payload = json.dumps(value, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _require_verifier(self, action: str) -> None:
        """Fail closed when no trusted-host authority verifier is configured."""
        if self._authority_verifier is None:
            raise PermissionDenied(
                f"{action} requires a trusted-host authority_verifier configured "
                "at AgentDB initialization; verified authority cannot be created "
                "or revoked without one (fail closed)."
            )

    def _verify_claim(self, claim: dict) -> bool:
        """Ask the host verifier for an affirmative allow/deny decision.

        The verifier receives the claim (record identity, scope, content
        fingerprint, requested action, principal, mechanism) and must return a
        truthy value to ALLOW. Any exception or falsy value is a deny.
        """
        try:
            decision = self._authority_verifier(claim)
        except Exception:
            return False
        return bool(decision)

    def approve_authority(
        self,
        record_id: str,
        *,
        principal: str,
        mechanism: str,
        approved_at: Optional["datetime"] = None,
    ) -> MemoryRecord:
        """Create **verified** human authority via the trusted-host verifier.

        FINAL-HARDENING §4: a valid digest proves content consistency, not human
        authorization. Verified authority is granted ONLY when a host-configured
        ``authority_verifier`` returns an affirmative decision for this claim.
        Without a verifier, or on denial, this raises :class:`PermissionDenied`
        (fail closed). Callers cannot manufacture verified authority by passing
        metadata.

        Returns the updated record.
        """
        self._require_verifier("approve_authority")
        record = self._store.get_by_id(record_id)
        if record is None:
            raise ValueError(f"no record with id {record_id!r}")

        fingerprint = record.approval_fingerprint()
        claim = {
            "action": "approve",
            "record_id": record.id,
            "key": record.key,
            "agent_id": record.agent_id,
            "workspace_id": record.workspace_id,
            "repository_id": record.repository_id,
            "environment": record.environment,
            "fingerprint": fingerprint,
            "principal": str(principal),
            "mechanism": str(mechanism),
        }
        if not self._verify_claim(claim):
            self.audit_log.append(
                EVENT_PERMISSION_DENIED,
                {"action": "approve_authority", "record_id": record_id,
                 "principal": principal, "reason": "verifier_denied"},
            )
            raise PermissionDenied(
                f"authority verifier denied approval for record {record_id!r}"
            )

        ts = approved_at or _utcnow()
        prev = record.approval or {}
        history = list(prev.get("history", []))
        history.append({
            "action": "approved",
            "principal": str(principal),
            "mechanism": str(mechanism),
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "fingerprint": fingerprint,
        })
        record.approval = {
            "verified": True,
            "version": record.APPROVAL_DIGEST_VERSION,
            "principal": str(principal),
            "mechanism": str(mechanism),
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "fingerprint": fingerprint,
            "history": history,
        }
        record.origin = "human_approved"
        record.trust_level = "human"
        self._store.write(record)
        self.audit_log.append(
            EVENT_MEMORY_WRITE,
            {
                "action": "approve_authority",
                "record_id": record_id,
                "principal": principal,
                "mechanism": mechanism,
                "agent_id": record.agent_id,
                "fingerprint": fingerprint,
            },
        )
        return record

    def revoke_authority(
        self,
        record_id: str,
        *,
        principal: str,
        mechanism: str,
        revoked_at: Optional["datetime"] = None,
    ) -> MemoryRecord:
        """Revoke verified authority through the trusted-host verifier (§4).

        Requires an affirmative verifier decision. Removes effective verified
        authority (origin reverts to asserted), preserves the memory and an
        auditable revocation history. Does not delete the record.
        """
        self._require_verifier("revoke_authority")
        record = self._store.get_by_id(record_id)
        if record is None:
            raise ValueError(f"no record with id {record_id!r}")
        claim = {
            "action": "revoke",
            "record_id": record.id,
            "key": record.key,
            "agent_id": record.agent_id,
            "workspace_id": record.workspace_id,
            "repository_id": record.repository_id,
            "environment": record.environment,
            "fingerprint": record.approval_fingerprint(),
            "principal": str(principal),
            "mechanism": str(mechanism),
        }
        if not self._verify_claim(claim):
            self.audit_log.append(
                EVENT_PERMISSION_DENIED,
                {"action": "revoke_authority", "record_id": record_id,
                 "principal": principal, "reason": "verifier_denied"},
            )
            raise PermissionDenied(
                f"authority verifier denied revocation for record {record_id!r}"
            )

        ts = revoked_at or _utcnow()
        ap = dict(record.approval or {})
        history = list(ap.get("history", []))
        history.append({
            "action": "revoked",
            "principal": str(principal),
            "mechanism": str(mechanism),
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        })
        ap["verified"] = False
        ap["revoked"] = True
        ap["revoked_at"] = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        ap["history"] = history
        record.approval = ap
        # Effective authority removed: revert asserted human origin.
        record.origin = "agent_inferred"
        record.trust_level = "agent"
        self._store.write(record)
        self.audit_log.append(
            EVENT_MEMORY_WRITE,
            {"action": "revoke_authority", "record_id": record_id,
             "principal": principal, "mechanism": mechanism, "agent_id": record.agent_id},
        )
        return record

    def delete(self, key: str, agent_id: str, trust_zone: str) -> None:
        """Archive a record by key. Raises PermissionDenied if not allowed."""
        if self._permissions is not None:
            try:
                self._permissions.check_write(key, agent_id, trust_zone)
            except PermissionDenied as e:
                self.audit_log.append(
                    EVENT_PERMISSION_DENIED,
                    {
                        "key": key,
                        "agent_id": agent_id,
                        "trust_zone": trust_zone,
                        "reason": str(e),
                    },
                )
                raise

        self._store.delete_by_key(key)
        self.audit_log.append(
            EVENT_MEMORY_DELETE,
            {"key": key, "agent_id": agent_id},
        )


    # ------------------------------------------------------------------
    # Conversation ingestion and session purge
    # ------------------------------------------------------------------

    def ingest_turns(
        self,
        *,
        agent_id: str,
        session_id: str,
        turns: "Sequence[Any]",
        source: str = "",
    ) -> "IngestResult":
        """Accept normalised conversation turns from a harness.

        The caller owns its log format and converts it into
        :class:`agentdb.ingest.ConversationTurn` objects; AgentDB validates,
        deduplicates and stores them. Call it per turn, per batch or once for a
        whole dump — repeated calls are safe.

        Order is JSONL first, then memories, then the dedup ledger, so the
        durable record of what was ingested exists before derived writes.

        Turns are never stored as memories. Only the ``memories`` the caller
        attached to a turn are, and ``role="tool"`` turns never contribute
        memories (high token, low knowledge, common secret vector).
        """
        from ..ingest import (
            IngestResult,
            append_conversation_entries,
            ensure_ingest_ledger,
            record_hashes,
            seen_hashes,
            turn_hash,
            validate_turns,
        )

        if self.memory_dir is None:
            raise ValueError(
                "ingest_turns requires a workspace: construct AgentDB(memory_dir=...) "
                "so the conversation log has a location."
            )

        # 1. Validate the whole batch up front — nothing is written on failure.
        validate_turns(turns)
        if not turns:
            return IngestResult(accepted=0, skipped=0)

        ensure_ingest_ledger(self._store._db_path)

        # 2. Deduplicate against the ledger.
        hashes = [turn_hash(agent_id, session_id, turn) for turn in turns]
        already = seen_hashes(self._store._db_path, agent_id, session_id, hashes)
        accepted: list[tuple[Any, str]] = []
        for turn, digest in zip(turns, hashes):
            if digest not in already:
                accepted.append((turn, digest))
        skipped = len(turns) - len(accepted)
        if not accepted:
            return IngestResult(accepted=0, skipped=skipped)

        # 3. Conversation log first (same file and shape as capture).
        appended = append_conversation_entries(
            self.memory_dir, agent_id, session_id, [turn for turn, _ in accepted]
        )

        # 4. Memories the caller extracted, through the normal write path.
        record_ids: list[str] = []
        for turn, _digest in accepted:
            if turn.role == "tool" and turn.memories:
                import warnings

                warnings.warn(
                    "ingest_turns: memories attached to a tool turn are ignored; "
                    "attach them to the assistant turn that interpreted the result.",
                    stacklevel=2,
                )
            if turn.role == "tool":
                continue
            for key, value in turn.memories:
                record = self.write(
                    key=key,
                    value=value,
                    origin="provider_ingested",
                    agent_id=agent_id,
                    session_id=session_id,
                )
                record_ids.append(record.id)

        # 5. Ledger rows.
        record_hashes(
            self._store._db_path, agent_id, session_id,
            [(digest, turn.turn_id) for turn, digest in accepted], source,
        )

        return IngestResult(
            accepted=len(accepted),
            skipped=skipped,
            memory_records=tuple(record_ids),
            jsonl_appended=appended,
        )

    def purge_session(
        self,
        agent_id: str,
        session_id: str,
        *,
        require_confirmation: bool = True,
    ) -> "PurgeResult":
        """Hard-remove one session from every store that holds its content.

        Unlike :meth:`delete`, which only sets ``archived = 1``, this removes the
        bytes: memories, conflicts, embeddings, session topics, outcomes,
        external retrieves, the conversation log and the ingest ledger.

        The audit log keeps a single ``session_purged`` event with counts and no
        content, so deletion is evidenced without retaining what was deleted.

        Explicit only: never called from lifecycle sweeps.
        """
        from ..ingest import _CREATE_INGEST_LEDGER, PurgeResult, remove_conversation_log

        if require_confirmation:
            raise ValueError(
                "purge_session is irreversible. Pass require_confirmation=False "
                "to proceed."
            )

        record_ids = self._session_record_ids_for(agent_id, session_id)

        counts = {"memories": 0, "conflicts": 0, "embeddings": 0, "outcomes": 0,
                  "external_retrieves": 0, "ledger": 0, "session_topics": 0}

        with self._store._connect() as conn:
            if record_ids:
                placeholders = ",".join("?" for _ in record_ids)
                cur = conn.execute(
                    f"DELETE FROM conflicts WHERE record_a_id IN ({placeholders}) "
                    f"OR record_b_id IN ({placeholders})",
                    (*record_ids, *record_ids),
                )
                counts["conflicts"] = cur.rowcount or 0
                cur = conn.execute(
                    f"DELETE FROM memories WHERE id IN ({placeholders})", record_ids
                )
                counts["memories"] = cur.rowcount or 0
            cur = conn.execute("DELETE FROM outcomes WHERE session_id = ?", (session_id,))
            counts["outcomes"] = cur.rowcount or 0
            cur = conn.execute("DELETE FROM external_retrieves WHERE session_id = ?", (session_id,))
            counts["external_retrieves"] = cur.rowcount or 0
            # The ledger table only exists once ingest_turns() has run; create
            # it if absent so a purge never fails on a DB that was populated
            # only through write().
            conn.execute(_CREATE_INGEST_LEDGER)
            cur = conn.execute(
                "DELETE FROM ingest_ledger WHERE agent_id = ? AND session_id = ?",
                (agent_id, session_id),
            )
            counts["ledger"] = cur.rowcount or 0

        # Embeddings live in a separate DB (database/embeddings.db), created
        # only when an embedding provider was configured. Reuse the live store
        # so we target the correct path and don't reconstruct it with a
        # missing model/dimensions. If it isn't active, there is nothing to
        # purge here.
        if self._embedding_store is not None and record_ids:
            try:
                for record_id in record_ids:
                    self._embedding_store.delete("memories", record_id)
                counts["embeddings"] = len(record_ids)
            except Exception:
                pass
            try:
                cur = self._embedding_store.delete_session_topic(session_id)
                counts["session_topics"] = cur
            except Exception:
                pass

        jsonl_lines = 0
        if self.memory_dir is not None:
            jsonl_lines = remove_conversation_log(self.memory_dir, agent_id, session_id)

        # Keep the fact, drop the content.
        self.audit_log.append(
            "session_purged",
            {"agent_id": agent_id, "session_id": session_id, **counts,
             "jsonl_lines_removed": jsonl_lines},
        )

        self._store._reload()

        return PurgeResult(
            memories=counts["memories"],
            conflicts=counts["conflicts"],
            embeddings=counts["embeddings"],
            outcomes=counts["outcomes"],
            external_retrieves=counts["external_retrieves"],
            ledger=counts["ledger"],
            session_topics=counts["session_topics"],
            jsonl_lines_removed=jsonl_lines,
        )

    def _session_record_ids_for(self, agent_id: str, session_id: str) -> list[str]:
        """Record ids persisted for this session (survives restarts)."""
        try:
            with self._store._connect() as conn:
                rows = conn.execute(
                    "SELECT id FROM memories WHERE agent_id = ? AND session_id = ?",
                    (agent_id, session_id),
                ).fetchall()
            return [row["id"] for row in rows]
        except Exception:
            return []

    def consolidate(self, agent_id: str, llm_fn=None, **kwargs) -> "ConsolidationResult":
        """Cluster near-duplicate records into a NEW derived canonical record.

        Source records are never overwritten; they are soft-archived (still
        recoverable) and their lineage is recorded on the canonical via
        ``source_record_ids`` and ``derivation``. A valid canonical may be
        selected in place of its archived sources. If ``llm_fn`` is provided it
        synthesizes the canonical value; otherwise the highest-confidence source
        value is copied. Unrelated or contradictory records are not merged."""
        # Obsolete skill-era keyword, retained as a no-op for backward
        # compatibility: it no longer selects or ranks anything (skill
        # management was removed from AgentDB). Accept it, warn, and drop it so
        # callers that still pass it do not hard-fail.
        if "skill_candidate_threshold" in kwargs:
            kwargs.pop("skill_candidate_threshold")
            warnings.warn(
                "consolidate(skill_candidate_threshold=...) is obsolete and ignored: "
                "AgentDB no longer manages skills.",
                DeprecationWarning,
                stacklevel=2,
            )
        result = self.lifecycle.consolidate(
            agent_id, gateway=self.gateway, llm_fn=llm_fn, **kwargs
        )
        try:
            self.lifecycle._reset_consolidation_state(agent_id)
        except Exception:
            pass
        return result

    def validate_canonicals(self, agent_id: Optional[str] = None) -> list[dict]:
        """Validate existing derived canonicals against current evidence (§5).

        Repeatable, non-destructive: reports each canonical's validity and a
        reason, WITHOUT rewriting or deleting anything. Invalid canonicals are
        already excluded from retrieval by ``_canonical_invalidation_reason``
        (source revised/missing) and are re-checked here against the tightened
        value/scope compatibility rules (a canonical whose sources are now
        incompatible is flagged ``canonical_sources_incompatible``).

        Returns a list of ``{canonical_id, valid, reason}`` dicts.
        """
        report: list[dict] = []
        for rec in list(self._store._by_id.values()):
            if getattr(rec, "kind", "source") != "derived_canonical":
                continue
            if agent_id is not None and rec.agent_id != agent_id:
                continue
            # Existing invalidation: source revised / missing / no lineage.
            reason = self._canonical_invalidation_reason(rec)
            if reason is None:
                # Re-check value/scope compatibility across the live sources.
                sources = [
                    self._store.get_by_id(sid) for sid in (rec.source_record_ids or [])
                ]
                sources = [s for s in sources if s is not None]
                if len(sources) >= 2 and not self.lifecycle._merge_compatible(sources):
                    reason = "canonical_sources_incompatible"
            report.append({
                "canonical_id": rec.id,
                "valid": reason is None,
                "reason": reason,
            })
        return report

    def checkpoint(
        self, agent_id: str, session_id: str, origin: str = "agent_inferred"
    ) -> None:
        """Write a compaction checkpoint record that survives lifecycle sweeps.

        Call this at the end of a session before context is compacted. The record
        persists across sweeps and is retrievable by any future session for the
        same agent.

        Provenance (contract P-01): the checkpoint is machine/agent-produced, so
        it defaults to ``origin="agent_inferred"``. It is **not**
        ``human_approved`` unless a verified human action explicitly passes
        ``origin="human_approved"``. Agent-originated checkpoints therefore
        remain subject to normal lifecycle decay/archival.
        """
        self.write(
            key=f"memory/compaction/checkpoint/{session_id}",
            value={"session_id": session_id, "type": "checkpoint"},
            origin=origin,
            agent_id=agent_id,
            session_id=session_id,
            confidence=1.0,
        )

    def write_compaction_summary(
        self, agent_id: str, session_id: str, summary: str, origin: str = "agent_inferred"
    ) -> None:
        """Write a compaction summary so agents survive context truncation.

        The summary should describe the session's completed work and what to do
        next. It is retrievable by any future session for the same agent.

        Provenance (contract P-01, ADB-03): a summary is machine/agent-produced,
        so it defaults to ``origin="agent_inferred"`` and never acquires
        ``human_approved`` implicitly. Only a verified human action may pass
        ``origin="human_approved"`` explicitly. This prevents untrusted
        agent input from being laundered into human authority.
        """
        self.write(
            key=f"memory/compaction/summary/{session_id}",
            value=summary,
            origin=origin,
            agent_id=agent_id,
            session_id=session_id,
            confidence=1.0,
        )

    # ------------------------------------------------------------------
    # Retrieval internals (shared by retrieve() and retrieve_by_entities())
    # ------------------------------------------------------------------

    _EPISODIC_PREFIX = "memory/episodic/"

    def _eligibility_reason(self, record: MemoryRecord, *, agent_id: str, isolate_by_agent: bool,
                            session_id: Optional[str], include_consolidated: bool,
                            scope: Optional[dict] = None) -> Optional[str]:
        """Return a human-readable exclusion reason, or None if the record is eligible.

        Applies the SAME rules regardless of which candidate branch (lexical vs
        semantic) produced the record, so a record cannot be filtered in one
        branch and admitted via another (ADB-05 scope-parity, T09).
        """
        if record.archived:
            return "archived"
        # OD-3: read authorization (isolation mode only; no-op in trusted mode).
        # Applied before ranking so unauthorized records never surface, and a
        # canonical is excluded if any of its sources is unreadable.
        if not self._can_read_record(record, agent_id):
            return "read_denied"
        if isolate_by_agent and record.agent_id != agent_id:
            return "agent_scope"
        # CONTEXTUAL-SPEC §7: explicit environment mismatch is inapplicable.
        # Missing scope on EITHER side is not a mismatch (never excluded here),
        # and task_type is deliberately NOT an exclusion criterion.
        if scope:
            want_env = scope.get("environment")
            rec_env = getattr(record, "environment", None)
            if want_env and rec_env and want_env != rec_env:
                return "environment_mismatch"
            # FINAL-HARDENING §3: explicit stable-identity mismatch is
            # inapplicable. Missing identity on either side is unknown (not a
            # mismatch). Workspace and repository are evaluated independently:
            # if EITHER explicitly conflicts, it is not an exact-context match.
            want_ws = scope.get("workspace_id")
            if want_ws and getattr(record, "workspace_id", None) and want_ws != record.workspace_id:
                return "workspace_mismatch"
            want_repo_id = scope.get("repository_id")
            if want_repo_id and getattr(record, "repository_id", None) and want_repo_id != record.repository_id:
                return "repository_mismatch"
            # FINAL-HARDENING §7: explicit temporal applicability. When an as_of
            # time is supplied, records explicitly outside their validity window
            # are inapplicable for current queries. Unknown validity stays
            # eligible (no exclusion, no bonus). created_at is never used as a
            # substitute for effective_from.
            as_of = scope.get("as_of")
            if as_of is not None and self._temporal_inapplicable(record, as_of):
                return "temporal_inapplicable"
        # Episodic records are session-local; cross-session episodic is excluded.
        if session_id is not None and self._EPISODIC_PREFIX in record.key \
                and f"/{session_id}/" not in record.key:
            return "episodic_other_session"
        # Repo filter scoped to repos this session has touched.
        if session_id and session_id in self._session_profiles:
            prof = self._session_profiles[session_id]
            if prof.repos and record.working_dirs \
                    and _extract_repo(record.working_dirs) not in prof.repos:
                return "repo_scope"
        if not include_consolidated and record.consolidated:
            return "consolidated_excluded"
        # Derived canonicals are invalidated when any source has been revised
        # since derivation (T04): a stale canonical must not be presented as
        # fresh. Fall back to the (audit-retained) sources instead.
        if getattr(record, "kind", "source") == "derived_canonical":
            invalid = self._canonical_invalidation_reason(record)
            if invalid is not None:
                return invalid
        # Expired records are ineligible for selection.
        if record.expiry_time is not None:
            exp = record.expiry_time
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= _utcnow():
                return "expired"
        return None

    @staticmethod
    def _applicability_match(record: MemoryRecord, scope: Optional[dict]) -> int:
        """Score how specifically a record matches the query scope (0 or more).

        Used only as a ranking tie-break (never changes composite weights, never
        grants authorization, never justifies consolidation). Missing scope on
        either side contributes 0 (an unscoped record gets NO exact-scope bonus).
        """
        if not scope:
            return 0
        score = 0
        rec_env = getattr(record, "environment", None)
        want_env = scope.get("environment")
        if want_env and rec_env and want_env == rec_env:
            score += 1
        # Explicit stable identities (FINAL-HARDENING §3): exact match counts;
        # missing identity contributes 0 (unknown != match).
        want_ws = scope.get("workspace_id")
        if want_ws and getattr(record, "workspace_id", None) and want_ws == record.workspace_id:
            score += 1
        want_repo_id = scope.get("repository_id")
        if want_repo_id and getattr(record, "repository_id", None) and want_repo_id == record.repository_id:
            score += 1
        # Legacy soft hint: directory basename match via working_dirs. NEVER
        # overrides an explicit identity mismatch (handled in eligibility), and
        # only when no explicit repository_id is set on the record.
        want_repo = scope.get("repo")
        if (want_repo and not getattr(record, "repository_id", None)
                and not want_repo_id and getattr(record, "working_dirs", None)):
            if _extract_repo(record.working_dirs) == want_repo:
                score += 1
        return score

    @staticmethod
    def _temporal_inapplicable(record: MemoryRecord, as_of) -> bool:
        """True if *record* is explicitly outside its validity window at *as_of*.

        Uses explicit effective_from / effective_until / expiry_time only.
        Unknown bounds are treated as open (eligible), and created_at is never
        substituted for effective_from. Timezone-safe comparison.
        """
        def _aware(dt):
            if dt is None:
                return None
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt

        ref = _aware(as_of if isinstance(as_of, datetime) else _parse_as_of(as_of))
        if ref is None:
            return False
        ef = _aware(getattr(record, "effective_from", None))
        eu = _aware(getattr(record, "effective_until", None))
        ex = _aware(record.expiry_time)
        # Explicitly not-yet-effective.
        if ef is not None and ref < ef:
            return True
        # Explicitly past its effective-until.
        if eu is not None and ref > eu:
            return True
        # Explicitly expired.
        if ex is not None and ref > ex:
            return True
        return False

    def _canonical_invalidation_reason(self, canonical: MemoryRecord) -> Optional[str]:
        """Return a reason string if a derived canonical is invalidated, else None.

        A canonical is invalidated when a source it was derived from no longer
        exists or its content digest no longer matches the recorded derivation
        digest (i.e. the source was revised). (ADB-06/T04)
        """
        from ..governance.lifecycle_manager import _record_digest

        derivation = getattr(canonical, "derivation", {}) or {}
        sources = derivation.get("sources", [])
        if not sources:
            # No lineage recorded: treat as unverifiable and skip selection.
            return "canonical_no_lineage"
        for src in sources:
            rec = self._store.get_by_id(src.get("id"))
            if rec is None:
                return "canonical_source_missing"
            if _record_digest(rec) != src.get("digest"):
                return "canonical_source_revised"
        return None

    def _hybrid_candidates(
        self,
        query: str,
        *,
        agent_id: str,
        query_embedding: Optional[list[float]],
        lexical_cap: int,
        trace: dict,
    ) -> tuple[dict, dict]:
        """Union lexical + optional semantic candidates, deduplicated by id.

        Returns (records_by_id, embedding_by_id). Semantic candidates are drawn
        from the whole corpus of stored embeddings (not just lexical hits) so a
        strongly-relevant record with zero lexical overlap is still eligible
        (ADB-05/T07). Record embeddings are hydrated for ranking.
        """
        lexical = self._store.search(query, limit=lexical_cap, agent_id=None)
        lexical_ids = {r.id for r in lexical}
        records: dict[str, MemoryRecord] = {r.id: r for r in lexical}
        embeddings: dict[str, list[float]] = {}
        trace.setdefault("lexical_count", len(lexical))
        trace.setdefault("semantic_count", 0)

        # Operational kill-switch: allow forcing lexical-only retrieval even when
        # embeddings are configured (deterministic fallback for operators).
        _semantic_enabled = bool(self._raw_embeddings_cfg.get("semantic_candidates", True))
        if (
            _semantic_enabled
            and query_embedding is not None
            and self._embedding_store is not None
            and self._embedding_provider is not None
        ):
            from ..embeddings.provider import _cosine
            from ..embeddings.text import _embed_text_for_record

            all_vecs = self._embedding_store.get_all("memories")
            # Add semantic candidates (whole corpus up to a cap by similarity).
            sem_cap = int(self._raw_embeddings_cfg.get("semantic_candidate_cap", max(lexical_cap, 50)))
            sims: list[tuple[str, float]] = []
            for rid, vec in all_vecs.items():
                try:
                    sims.append((rid, _cosine(query_embedding, vec)))
                except Exception:
                    continue
            sims.sort(key=lambda t: t[1], reverse=True)
            added = 0
            for rid, _sim in sims[:sem_cap]:
                if rid in records:
                    continue
                rec = self._store._by_id.get(rid)
                if rec is None:
                    continue
                records[rid] = rec
                embeddings[rid] = all_vecs[rid]
                added += 1
            trace["semantic_count"] = added

            # Hydrate embeddings for lexical hits and lazily embed missing ones.
            missing = [r for rid, r in records.items() if rid not in all_vecs]
            if missing:
                try:
                    vecs = self._embedding_provider.embed_batch(
                        [_embed_text_for_record(r) for r in missing]
                    )
                    for rec, vec in zip(missing, vecs):
                        self._embedding_store.put("memories", rec.id, vec, working_dirs=rec.working_dirs)
                        embeddings[rec.id] = vec
                except Exception:
                    pass
            # Ensure all semantically-found vectors are present.
            for rid, vec in all_vecs.items():
                if rid in records and rid not in embeddings:
                    embeddings[rid] = vec

        trace["candidate_total"] = len(records)
        trace["semantic_only"] = len([rid for rid in records if rid not in lexical_ids])
        return records, embeddings

    def _pack_budget(
        self, ranked: list, budget_tokens: Optional[int]
    ) -> tuple[list, int, bool, list]:
        """Greedily select ranked records within a rendered-token budget.

        Skips an oversized record and CONTINUES to smaller eligible ones rather
        than stopping at the first overflow (ADB-08/T10). Budget applies to the
        estimated rendered size of each selected record. Returns
        (selected_records, token_total, truncated, excluded_ids).
        """
        selected: list[MemoryRecord] = []
        excluded_ids: list[str] = []
        token_total = 0
        truncated = False
        for rr in ranked:
            record = rr.record
            est = _estimate_tokens(record)
            if budget_tokens is not None and token_total + est > budget_tokens:
                truncated = True
                excluded_ids.append(record.id)
                continue
            selected.append(record)
            token_total += est
        return selected, token_total, truncated, excluded_ids

    def _unique_detail_sources(
        self, canonical: MemoryRecord, query: str
    ) -> list[MemoryRecord]:
        """Archived sources of a canonical carrying a query-critical detail the
        canonical's value omits (ADB-07 / T06). A source qualifies when it still
        exists, is archived, shares a query term that is absent from the
        canonical value, and has strictly greater query overlap than the
        canonical. Lets "what is E4097" surface the source holding E4097 even
        though the covering canonical dropped it."""
        source_ids = getattr(canonical, "source_record_ids", []) or []
        if not source_ids:
            return []
        query_terms = self._detail_terms(query)
        if not query_terms:
            return []
        canon_val = canonical.value
        canon_text = json.dumps(canon_val) if isinstance(canon_val, (dict, list)) else str(canon_val)
        # The canonical's representation includes its key (which names the fact
        # domain), so a term the key already covers is NOT a missing detail.
        canon_terms = self._detail_terms(canonical.key + " " + canon_text)
        canon_overlap = len(query_terms & canon_terms)

        hits: list = []
        for sid in source_ids:
            src = self._store.get_by_id(sid)
            if src is None or not src.archived:
                continue
            src_val = json.dumps(src.value) if isinstance(src.value, (dict, list)) else str(src.value)
            src_terms = self._detail_terms(src.key + " " + src_val)
            src_overlap = len(query_terms & src_terms)
            detail = (query_terms & src_terms) - canon_terms
            if detail and src_overlap > canon_overlap:
                hits.append((src_overlap, src))
        hits.sort(key=lambda t: (-t[0], t[1].id))
        return [s for _n, s in hits]

    @staticmethod
    def _detail_terms(text: str) -> set:
        cleaned = str(text).replace("/", " ").replace("-", " ").replace("_", " ").lower()
        return {w for w in cleaned.split() if len(w) >= 3 and w not in _UNIQUE_DETAIL_STOP}

    def retrieve(
        self,
        query: str,
        agent_id: str,
        limit: Optional[int] = None,
        session_id: Optional[str] = None,
        external_context: Optional[list[dict]] = None,
        budget_tokens: Optional[int] = None,
        working_dirs: Optional[list[str]] = None,
        include_consolidated: bool = True,
        trace: bool = False,
        scope: Optional[dict] = None,
    ) -> RetrievalResult:
        """Retrieve relevant memory for a query.

        ``scope`` (CONTEXTUAL-SPEC §7) optionally supplies explicit context:
        ``{"environment": str, "workspace": str, "repo": str, "task_type": str,
        "effective_time": datetime}``. Only ``environment`` is used as a hard
        applicability filter (explicit mismatch excludes a record); ``task_type``
        is a soft label and never excludes on its own. Missing scope is not a
        match. Existing ranking weights are unchanged.
        """
        agent_config = self.config_for(agent_id)
        isolate_by_agent = agent_config["isolate_by_agent"]
        resolved_limit = limit if limit is not None else agent_config["retrieval"]["retrieve_limit"]
        resolved_budget_tokens = budget_tokens if budget_tokens is not None else agent_config["injection"]["context_token_limit"]

        # Query gate — skip embedding work for generic/short queries
        _stop_phrases = frozenset(
            self._raw_embeddings_cfg.get("generic_query_stop_phrases", [
                "ok", "go ahead", "sounds good", "yes", "no", "sure",
                "proceed", "continue", "let's review", "sounds great",
            ])
        )
        _min_tokens = int(self._raw_embeddings_cfg.get("min_query_tokens", 3))
        _query_passes_gate = (
            self._embedding_provider is not None
            and len(query.split()) >= _min_tokens
            and query.strip().lower() not in _stop_phrases
        )
        query_embedding: Optional[list[float]] = None
        if _query_passes_gate:
            try:
                query_embedding = self._embedding_provider.embed(query)
            except Exception:
                query_embedding = None

        # --- Hybrid candidate generation: lexical ∪ semantic, deduped by id ---
        trace_info: dict = {}
        cand_by_id, _cand_embeddings = self._hybrid_candidates(
            query,
            agent_id=agent_id,
            query_embedding=query_embedding,
            lexical_cap=max(resolved_limit, 50),
            trace=trace_info,
        )

        # --- Shared eligibility: identical filter for both candidate branches ---
        excluded_reasons: dict[str, str] = {}
        eligible: list[MemoryRecord] = []
        for rec in cand_by_id.values():
            reason = self._eligibility_reason(
                rec, agent_id=agent_id, isolate_by_agent=isolate_by_agent,
                session_id=session_id, include_consolidated=include_consolidated,
                scope=scope,
            )
            if reason is None:
                eligible.append(rec)
            else:
                excluded_reasons[rec.id] = reason

        # --- Session-topic semantic filter (operates on embeddings, post-eligibility) ---
        if query_embedding is not None and self._embedding_store is not None and session_id:
            session_topic = self._embedding_store.get_session_topic(session_id)
            if session_topic is not None:
                from ..embeddings.provider import _cosine
                _threshold = float(self._raw_embeddings_cfg.get("session_topic_threshold", 0.35))
                kept: list[MemoryRecord] = []
                for r in eligible:
                    vec = _cand_embeddings.get(r.id)
                    if vec is None:
                        kept.append(r)  # no embedding → always pass
                        continue
                    if _cosine(vec, session_topic) >= _threshold:
                        kept.append(r)
                    else:
                        excluded_reasons[r.id] = "session_topic"
                eligible = kept

        # Attach embeddings as transient field before ranking (reuse cached vectors).
        for r in eligible:
            vec = _cand_embeddings.get(r.id)
            if vec is not None:
                r.embedding = vec

        ranked_all = self._ranker.rank(
            query, eligible, agent_id=agent_id,
            query_embedding=query_embedding,
            session_working_dirs=working_dirs,
        )

        # CONTEXTUAL-SPEC §7: exact scope matches improve *applicability* as a
        # deterministic tie-break, WITHOUT altering the weighted composite score
        # (weights preserved). Order: composite desc, exact-scope match first,
        # then record id for determinism.
        ranked_all.sort(
            key=lambda rr: (
                -rr.composite_score,
                -self._applicability_match(rr.record, scope),
                rr.record.id,
            )
        )

        # Drop records that don't meet the minimum relevance threshold.
        min_score = agent_config["retrieval"]["min_composite_score"]
        if min_score > 0.0:
            kept_ranked = []
            for rr in ranked_all:
                if rr.composite_score >= min_score:
                    kept_ranked.append(rr)
                else:
                    excluded_reasons[rr.record.id] = "below_min_score"
            ranked_all = kept_ranked

        ranked = ranked_all[:resolved_limit]
        for rr in ranked_all[resolved_limit:]:
            excluded_reasons.setdefault(rr.record.id, "over_limit")

        # --- Budget packing: skip oversized, continue with smaller (ADB-08/T10) ---
        selected, tok_total, truncated, budget_excluded = self._pack_budget(ranked, resolved_budget_tokens)
        for rid in budget_excluded:
            excluded_reasons.setdefault(rid, "over_budget")

        # --- Unique-detail re-injection (ADB-07 / T06) ---
        # For each selected canonical, re-inject an archived source only when it
        # carries a query term the canonical value omits. Fit-permitting, added
        # after primary selection and surfaced via the trace with a reason.
        unique_detail_ids = set()
        if selected:
            already = {r.id for r in selected}
            for rec in list(selected):
                if getattr(rec, "kind", "source") != "derived_canonical":
                    continue
                for src in self._unique_detail_sources(rec, query):
                    if src.id in already:
                        continue
                    est = _estimate_tokens(src)
                    if resolved_budget_tokens is not None and tok_total + est > resolved_budget_tokens:
                        excluded_reasons.setdefault(src.id, "unique_detail_over_budget")
                        continue
                    selected.append(src)
                    already.add(src.id)
                    tok_total += est
                    unique_detail_ids.add(src.id)
        records = selected
        token_total = tok_total

        # Increment retrieval counts
        for record in records:
            self._store.increment_retrieval_count(record.id)

        record_ids = [r.id for r in records]
        signal_scores = {rr.record.id: rr.composite_score for rr in ranked}

        # Track for outcome recording
        if session_id is not None:
            if session_id not in self._session_record_ids:
                self._session_record_ids[session_id] = []
            self._session_record_ids[session_id].extend(record_ids)

        self.audit_log.append(
            EVENT_MEMORY_RETRIEVE,
            {
                "query": query,
                "record_ids": record_ids,
                "signal_scores": signal_scores,
                "session_id": session_id,
                "trigger_source": "query",
                "reconciliation_run_id": None,
                "isolate_by_agent": isolate_by_agent,
            },
        )

        # Handle external context conflict detection
        had_conflict = False
        if external_context is not None:
            # Check for overlap: any content word matches a record value
            ext_words = set()
            for ctx in external_context:
                content = ctx.get("content", "")
                ext_words.update(w.lower() for w in content.split())

            overlap_records = []
            for record in records:
                value_str = (
                    json.dumps(record.value)
                    if isinstance(record.value, (dict, list))
                    else str(record.value)
                ).lower()
                for word in ext_words:
                    if word in value_str:
                        overlap_records.append(record)
                        break

            if overlap_records:
                had_conflict = True
                self.audit_log.append(
                    EVENT_EXTERNAL_CONFLICT_DETECTED,
                    {
                        "query": query,
                        "count": len(overlap_records),
                        "sources": [ctx.get("source", "") for ctx in external_context],
                        "session_id": session_id,
                        "trigger_source": "query",
                        "reconciliation_run_id": None,
                    },
                )

            # Record external retrieve stats
            with self._store._connect() as conn:
                conn.execute(
                    "INSERT INTO external_retrieves (session_id, had_external_context, had_conflict) VALUES (?, ?, ?)",
                    (session_id or "", 1, int(had_conflict)),
                )

        # Fetch conflicts for returned records
        conflicts = self._store.get_conflicts_for_records([r.id for r in records])

        # Build override instructions for authority conflicts
        override_instructions = []
        for c in conflicts:
            if c.conflict_type == "authority":
                override_instructions.append(
                    f"Authority conflict on a retrieved memory (conflict_id={c.id}): "
                    "the human-approved version takes precedence over any agent-inferred value for this key."
                )

        trace_out: dict = {}
        if trace:
            trace_out = {
                "query": query,
                "candidate_sources": dict(trace_info),
                "selected_ids": [r.id for r in records],
                "unique_detail_ids": sorted(unique_detail_ids),
                "excluded": [
                    {"id": rid, "reason": reason}
                    for rid, reason in excluded_reasons.items()
                    if rid not in {r.id for r in records}
                ],
                "ranked": [
                    {
                        "id": rr.record.id,
                        "composite_score": rr.composite_score,
                        "signals": rr.signal_breakdown,
                        "conflict_flag": rr.conflict_flag,
                    }
                    for rr in ranked_all
                ],
                "budget_requested": resolved_budget_tokens,
                "estimated_tokens": token_total,
                "truncated": truncated,
            }

        return RetrievalResult(
            records=records,
            conflicts=conflicts,
            override_instructions=override_instructions,
            token_estimate=token_total,
            truncated=truncated,
            trace=trace_out,
        )

    def retrieve_by_entities(
        self,
        entities: list[str],
        agent_id: str,
        session_id: Optional[str] = None,
        budget_tokens: Optional[int] = None,
        include_consolidated: bool = True,
        trace: bool = False,
        scope: Optional[dict] = None,
    ) -> RetrievalResult:
        """Entity-driven retrieval.

        Uses the SAME eligibility filters and budget packing as :meth:`retrieve`
        (ADB-09/T11 parity): agent scope, episodic session scoping, repo scope,
        consolidated filter, expiry, and rendered-token budget are applied here
        too. Candidate discovery is entity-based (entity tags + key/value
        substring fallback) rather than lexical/semantic.
        """
        agent_config = self.config_for(agent_id)
        isolate_by_agent = agent_config["isolate_by_agent"]
        resolved_budget_tokens = (
            budget_tokens if budget_tokens is not None
            else agent_config["injection"]["context_token_limit"]
        )

        raw = self._store.search_by_entities(
            entities, agent_id=agent_id if isolate_by_agent else None,
        )

        # Shared eligibility (identical rules to retrieve()).
        excluded_reasons: dict[str, str] = {}
        eligible: list[MemoryRecord] = []
        for rec in raw:
            reason = self._eligibility_reason(
                rec, agent_id=agent_id, isolate_by_agent=isolate_by_agent,
                session_id=session_id, include_consolidated=include_consolidated,
                scope=scope,
            )
            if reason is None:
                eligible.append(rec)
            else:
                excluded_reasons[rec.id] = reason

        # Shared budget packing (skip oversized, continue with smaller).
        # Rank entity matches through the SAME multi-signal ranker used by
        # retrieve() — with no query embedding, so ordering is driven by
        # provenance/recency/confidence/causal (semantic contributes 0). This
        # replaces the previous ad-hoc recency-only ordering.
        ranked = self._ranker.rank(
            "", eligible, agent_id=agent_id,
            query_embedding=None,
            session_working_dirs=None,
        )
        records, token_total, truncated, budget_excluded = self._pack_budget(
            ranked, resolved_budget_tokens
        )
        for rid in budget_excluded:
            excluded_reasons.setdefault(rid, "over_budget")

        record_ids = [r.id for r in records]

        # Track for outcome recording
        if session_id is not None:
            if session_id not in self._session_record_ids:
                self._session_record_ids[session_id] = []
            self._session_record_ids[session_id].extend(record_ids)

        self.audit_log.append(
            EVENT_MEMORY_RETRIEVE,
            {
                "query": "",
                "record_ids": record_ids,
                "signal_scores": {r.id: 1.0 for r in records},
                "session_id": session_id,
                "trigger_source": "entity",
                "entities": entities,
                "reconciliation_run_id": None,
                "isolate_by_agent": isolate_by_agent,
            },
        )

        # Fetch conflicts for returned records
        conflicts = self._store.get_conflicts_for_records([r.id for r in records])

        override_instructions = []
        for c in conflicts:
            if c.conflict_type == "authority":
                override_instructions.append(
                    f"Authority conflict on a retrieved memory (conflict_id={c.id}): "
                    "the human-approved version takes precedence over any agent-inferred value for this key."
                )

        trace_out: dict = {}
        if trace:
            trace_out = {
                "query": "",
                "trigger_source": "entity",
                "selected_ids": record_ids,
                "excluded": [
                    {"id": rid, "reason": reason}
                    for rid, reason in excluded_reasons.items()
                    if rid not in set(record_ids)
                ],
                "budget_requested": resolved_budget_tokens,
                "estimated_tokens": token_total,
                "truncated": truncated,
            }

        return RetrievalResult(
            records=records,
            conflicts=conflicts,
            override_instructions=override_instructions,
            token_estimate=token_total,
            truncated=truncated,
            trace=trace_out,
        )

    # ------------------------------------------------------------------
    # Context rendering (CONTEXTUAL-SPEC §8)
    # ------------------------------------------------------------------

    def render_context(self, result: RetrievalResult, *, max_records: Optional[int] = None) -> str:
        """Render a retrieval result as concise, model-facing context.

        Default: one concise natural-language line per selected record. A
        derived canonical line includes a compact ``Scope:`` qualifier only when
        it has one, and an ``Evidence:`` source-id list. If selected records carry
        a material (unresolved) conflict, a short disagreement block is appended
        instead of silently merging the claims. Verbose metadata is not emitted
        unless it is present and relevant. (Does not exceed what was already
        budgeted in ``result``.)
        """
        records = list(result.records)
        if max_records is not None:
            records = records[:max_records]
        lines: list[str] = []
        for r in records:
            value = r.value
            text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            line = text.strip()
            if getattr(r, "kind", "source") == "derived_canonical":
                quals = []
                env = getattr(r, "environment", None)
                repo = _extract_repo(getattr(r, "working_dirs", None) or [])
                scope_bits = [b for b in (repo, env) if b]
                if scope_bits:
                    quals.append("Scope: " + " / ".join(scope_bits) + ".")
                src_ids = getattr(r, "source_record_ids", []) or []
                if src_ids:
                    quals.append("Evidence: " + ", ".join(str(s) for s in src_ids) + ".")
                if quals:
                    line = line + " " + " ".join(quals)
            lines.append(line)

        block = self._render_conflict_block(result, records)
        if block:
            lines.append(block)
        return "\n".join(lines)

    def _render_conflict_block(self, result: RetrievalResult, records: list) -> str:
        """Render a compact "unresolved disagreement" block for material conflicts
        among the selected records, if any. Returns '' when none."""
        sel = {r.id for r in records}
        by_id = {r.id: r for r in records}
        material = []
        for c in (result.conflicts or []):
            if c.record_a_id in sel and c.record_b_id in sel:
                material.append(c)
        if not material:
            return ""
        out = ["Unresolved disagreement:"]
        def _val(rid):
            r = by_id.get(rid)
            if r is None:
                return "?"
            v = r.value
            return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        for c in material:
            out.append(f"- {_val(c.record_a_id)}: {c.record_a_id}")
            out.append(f"- {_val(c.record_b_id)}: {c.record_b_id}")
        out.append("No verified supersession found.")
        return "\n".join(out)

    def record_outcome(
        self,
        session_id: str,
        agent_id: str,
        outcome_type: str,
        outcome_value: float,
    ) -> int:
        """Backward-compat alias — delegates to db.lifecycle.record_outcome()."""
        adjusted_ids = self.lifecycle.record_outcome(
            session_id=session_id,
            agent_id=agent_id,
            outcome_type=outcome_type,
            outcome_value=outcome_value,
        )
        self.audit_log.append(
            EVENT_MEMORY_OUTCOME,
            {
                "session_id": session_id,
                "outcome_type": outcome_type,
                "outcome_value": float(outcome_value),
                "linked_retrieval_sequence": None,
                "adjusted_record_ids": adjusted_ids,
            },
        )
        return len(adjusted_ids)

    def register_reconciliation_handler(self, trigger: str, handler: Callable) -> None:
        """Backward-compat alias → db.gateway.subscribe(trigger, handler)."""
        self.gateway.subscribe(trigger, handler)

    def dispatch_reconciliation_trigger(self, trigger: str, **kwargs) -> int:
        """Backward-compat alias → db.gateway.dispatch(trigger, **kwargs)."""
        return self.gateway.dispatch(trigger, **kwargs)
