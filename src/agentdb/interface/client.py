from __future__ import annotations

import dataclasses
import json
import os
import warnings
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


def _extract_repo(working_dirs) -> "Optional[str]":
    if not working_dirs:
        return None
    last = (working_dirs[-1] or "").rstrip("/")
    segment = last.split("/")[-1] if "/" in last else last
    return segment or None


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
    ) -> None:
        # db_path is an alias for path (used by adapter tests)
        if db_path is not None and path is None:
            path = db_path
        self._summarizer = summarizer
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
        if path is not None:
            db_path = Path(path).resolve()
        elif resolved_memory_dir is not None:
            db_path = (resolved_memory_dir / "database" / "agentdb.sqlite").resolve()
        else:
            db_path = Path(":memory:")

        self._path = db_path

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
    ) -> MemoryRecord:
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
        )

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
        """Raw read by key, no permission check."""
        return self._store.get_by_key(key)

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
        """Cluster near-duplicate records, archive lower-confidence copies, mark the
        surviving canonical as consolidated=True with source_record_ids provenance.
        If llm_fn is provided, synthesizes the canonical value."""
        result = self.lifecycle.consolidate(
            agent_id, gateway=self.gateway, llm_fn=llm_fn, **kwargs
        )
        try:
            self.lifecycle._reset_consolidation_state(agent_id)
        except Exception:
            pass
        return result

    def checkpoint(self, agent_id: str, session_id: str) -> None:
        """Write a compaction checkpoint record that survives lifecycle sweeps.

        Call this at the end of a session before context is compacted. The record
        is stored as human_approved so it persists across sweeps and is retrievable
        by any future session for the same agent.
        """
        self.write(
            key=f"memory/compaction/checkpoint/{session_id}",
            value={"session_id": session_id, "type": "checkpoint"},
            origin="human_approved",
            agent_id=agent_id,
            session_id=session_id,
            confidence=1.0,
        )

    def write_compaction_summary(
        self, agent_id: str, session_id: str, summary: str
    ) -> None:
        """Write a compaction summary so agents survive context truncation.

        The summary should describe the session's completed work and what to do
        next. Stored as human_approved so it survives lifecycle sweeps and is
        retrievable by any future session for the same agent.
        """
        self.write(
            key=f"memory/compaction/summary/{session_id}",
            value=summary,
            origin="human_approved",
            agent_id=agent_id,
            session_id=session_id,
            confidence=1.0,
        )

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
    ) -> RetrievalResult:
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

        candidates = self._store.search(
            query, limit=max(resolved_limit, 50), agent_id=agent_id if isolate_by_agent else None,
        )

        # Scope episodic records to the current session. Episodic records are
        # session-local by design; cross-session insights should be promoted to
        # structured non-episodic keys. Non-episodic records pass through unfiltered.
        _EPISODIC_PREFIX = "memory/episodic/"
        if session_id is not None:
            candidates = [
                r for r in candidates
                if _EPISODIC_PREFIX not in r.key or f"/{session_id}/" in r.key
            ]

        # Repo filter: only records from repos this session has touched (or with no repo)
        if session_id and session_id in self._session_profiles:
            _profile = self._session_profiles[session_id]
            if _profile.repos:
                candidates = [
                    r for r in candidates
                    if not r.working_dirs
                    or _extract_repo(r.working_dirs) in _profile.repos
                ]

        # Consolidated filter
        if not include_consolidated:
            candidates = [r for r in candidates if not r.consolidated]

        # Embedding hydration + session topic filter
        if _query_passes_gate and self._embedding_store is not None:
            from ..embeddings.provider import _cosine
            from ..embeddings.text import _embed_text_for_record

            record_ids = [r.id for r in candidates]
            stored_embeddings = self._embedding_store.get_batch("memories", record_ids)

            # Lazy regen: embed records that have no stored vector
            missing = [r for r in candidates if r.id not in stored_embeddings]
            if missing:
                try:
                    vecs = self._embedding_provider.embed_batch(
                        [_embed_text_for_record(r) for r in missing]
                    )
                    for rec, vec in zip(missing, vecs):
                        self._embedding_store.put(
                            "memories", rec.id, vec, working_dirs=rec.working_dirs
                        )
                        stored_embeddings[rec.id] = vec
                except Exception:
                    pass

            # Session topic filter
            if session_id:
                session_topic = self._embedding_store.get_session_topic(session_id)
                if session_topic is not None:
                    _threshold = float(
                        self._raw_embeddings_cfg.get("session_topic_threshold", 0.35)
                    )
                    candidates = [
                        r for r in candidates
                        if r.id not in stored_embeddings  # no embedding → always pass
                        or _cosine(stored_embeddings[r.id], session_topic) >= _threshold
                    ]

            # Attach embeddings as transient field before ranking
            for r in candidates:
                if r.id in stored_embeddings:
                    r.embedding = stored_embeddings[r.id]

        ranked_all = self._ranker.rank(
            query, candidates, agent_id=agent_id,
            query_embedding=query_embedding,
            session_working_dirs=working_dirs,
        )

        # Drop records that don't meet the minimum relevance threshold.
        min_score = agent_config["retrieval"]["min_composite_score"]
        if min_score > 0.0:
            ranked_all = [rr for rr in ranked_all if rr.composite_score >= min_score]

        ranked = ranked_all[:resolved_limit]

        # Budget cap
        selected: list[MemoryRecord] = []
        token_total = 0
        truncated = False
        for rr in ranked:
            record = rr.record
            est = _estimate_tokens(record)
            if resolved_budget_tokens is not None and token_total + est > resolved_budget_tokens:
                truncated = True
                break
            selected.append(record)
            token_total += est
        records = selected

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

        return RetrievalResult(
            records=records,
            conflicts=conflicts,
            override_instructions=override_instructions,
            token_estimate=token_total,
            truncated=truncated,
        )

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

        return RetrievalResult(
            records=records,
            conflicts=conflicts,
            override_instructions=override_instructions,
        )

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
