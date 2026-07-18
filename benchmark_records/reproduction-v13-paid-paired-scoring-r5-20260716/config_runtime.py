"""Runtime paths and durable project configuration.

The project configuration deliberately lives in NarratorDB's existing SQLite
``metadata`` table.  It therefore travels with normal database backups without
introducing a second configuration file that could drift from the data it
describes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DATA_DIR = Path("~/.narratordb").expanduser()
PROJECT_CONFIG_VERSION = 2
DEFAULT_OPENROUTER_MODEL = "openai/gpt-5.4-mini"
LUNA_PRO_OPENROUTER_MODEL = "openai/gpt-5.6-luna-pro"
DEFAULT_OUTPUT_TOKEN_PARAMETER = "max_completion_tokens"
SUPPORTED_OUTPUT_TOKEN_PARAMETERS = frozenset(
    {DEFAULT_OUTPUT_TOKEN_PARAMETER, "max_tokens"}
)

_CONFIG_PREFIX = "narratordb.project."
_MODE_KEY = f"{_CONFIG_PREFIX}mode"
_COMPILER_KEY = f"{_CONFIG_PREFIX}compiler"
_VERSION_KEY = f"{_CONFIG_PREFIX}config_version"
_CREATED_AT_KEY = f"{_CONFIG_PREFIX}created_at"
_UPDATED_AT_KEY = f"{_CONFIG_PREFIX}updated_at"
_MIGRATED_FROM_KEY = f"{_CONFIG_PREFIX}migrated_from"
_OPENROUTER_PROVIDER_ROUTE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,63})?"
)


class ConfigurationError(ValueError):
    """Base class for invalid or missing project configuration."""


class ConfigurationRequiredError(ConfigurationError):
    """Raised when a new database has not been assigned a memory mode."""


class ModeConflictError(ConfigurationError):
    """Raised when construction would silently change a configured mode."""


class FeatureUnavailableError(RuntimeError):
    """Raised when an optional derived-memory capability is not installed."""


class MemoryMode(str, Enum):
    PRIVATE = "private"
    INTELLIGENCE = "intelligence"


class CompilerKind(str, Enum):
    LOCAL = "local"
    OPENROUTER = "openrouter"


def _enum_value(value: str | Enum) -> str:
    return str(value.value if isinstance(value, Enum) else value).strip().lower()


def normalize_mode(value: str | MemoryMode) -> MemoryMode:
    try:
        return MemoryMode(_enum_value(value))
    except ValueError as error:
        choices = ", ".join(mode.value for mode in MemoryMode)
        raise ConfigurationError(f"mode must be one of: {choices}") from error


def normalize_compiler_kind(value: str | CompilerKind) -> CompilerKind:
    try:
        return CompilerKind(_enum_value(value))
    except ValueError as error:
        choices = ", ".join(kind.value for kind in CompilerKind)
        raise ConfigurationError(f"compiler kind must be one of: {choices}") from error


def normalize_output_token_parameter(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if normalized not in SUPPORTED_OUTPUT_TOKEN_PARAMETERS:
        choices = ", ".join(sorted(SUPPORTED_OUTPUT_TOKEN_PARAMETERS))
        raise ConfigurationError(f"output_token_parameter must be one of: {choices}")
    return normalized


def normalize_openrouter_provider_route(value: Any) -> str:
    """Return one unambiguous OpenRouter provider route selector."""

    if not isinstance(value, str):
        raise ConfigurationError("OpenRouter provider routes must be strings")
    normalized = value.strip()
    if not _OPENROUTER_PROVIDER_ROUTE.fullmatch(normalized):
        raise ConfigurationError(
            "OpenRouter provider routes must be non-empty provider or provider/endpoint slugs"
        )
    return normalized


def openrouter_provider_family_identity(value: str) -> str:
    family = value.split("/", 1)[0]
    return "".join(character for character in family.casefold() if character.isalnum())


def normalize_openrouter_provider_allowlist(value: Any) -> tuple[str, ...]:
    """Validate ordered routes and reject ambiguous family attestations."""

    if not isinstance(value, (tuple, list)):
        raise ConfigurationError("OpenRouter provider allowlist must be an array")
    normalized = tuple(normalize_openrouter_provider_route(item) for item in value)
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise ConfigurationError("OpenRouter provider allowlist entries must be unique")
    families = tuple(openrouter_provider_family_identity(item) for item in normalized)
    if len(set(families)) != len(families):
        raise ConfigurationError(
            "OpenRouter provider allowlist entries must have distinct provider families"
        )
    return normalized


def _is_loopback_endpoint(endpoint: str) -> bool:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        return False
    return (
        parsed.username is None
        and parsed.password is None
        and (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
    )


@dataclass(frozen=True)
class CompilerConfig:
    """Serializable compiler selection; credentials are intentionally absent.

    ``local`` endpoints are restricted to loopback HTTP(S) so choosing
    a local compiler cannot silently send raw memory to another machine.
    OpenRouter credentials are read only from ``OPENROUTER_API_KEY`` by the
    runtime adapter and are never represented by this type.
    """

    kind: CompilerKind
    model: str | None = None
    endpoint: str | None = None
    provider: str | None = None
    provider_allowlist: tuple[str, ...] = ()
    allow_fallbacks: bool = False
    reasoning: str | None = None
    max_output_tokens: int = 8192
    output_token_parameter: str = DEFAULT_OUTPUT_TOKEN_PARAMETER
    seed: int | None = 0
    zero_data_retention: bool = True
    data_collection: str = "deny"
    transport_max_attempts: int | None = None
    semantic_max_attempts: int | None = None
    retry_delay_seconds: float | None = None
    min_request_interval_seconds: float = 0.0
    capture_router_metadata: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", normalize_compiler_kind(self.kind))
        model = self.model.strip() if isinstance(self.model, str) else None
        endpoint = self.endpoint.strip() if isinstance(self.endpoint, str) else None
        if self.kind is CompilerKind.OPENROUTER:
            provider = (
                normalize_openrouter_provider_route(self.provider)
                if self.provider is not None
                else None
            )
            provider_allowlist = normalize_openrouter_provider_allowlist(
                self.provider_allowlist
            )
        else:
            provider = self.provider.strip() if isinstance(self.provider, str) else None
            provider_allowlist = tuple(
                item.strip()
                for item in self.provider_allowlist
                if isinstance(item, str) and item.strip()
            )
        reasoning = (
            self.reasoning.strip().lower() if isinstance(self.reasoning, str) else None
        )
        object.__setattr__(self, "model", model or None)
        object.__setattr__(self, "endpoint", endpoint or None)
        object.__setattr__(self, "provider", provider or None)
        object.__setattr__(self, "provider_allowlist", provider_allowlist)
        object.__setattr__(self, "reasoning", reasoning or None)
        object.__setattr__(
            self,
            "output_token_parameter",
            normalize_output_token_parameter(self.output_token_parameter),
        )

        if self.kind is CompilerKind.OPENROUTER:
            if not self.model:
                raise ConfigurationError("an OpenRouter compiler requires a model")
            if self.provider and provider_allowlist:
                raise ConfigurationError(
                    "configure either one OpenRouter provider or a provider allowlist"
                )
            if not self.provider and not provider_allowlist:
                raise ConfigurationError(
                    "an OpenRouter compiler requires a provider or provider allowlist"
                )
            if self.allow_fallbacks and not provider_allowlist:
                raise ConfigurationError(
                    "OpenRouter fallbacks require an explicit provider allowlist"
                )
        if (
            self.kind is CompilerKind.LOCAL
            and self.endpoint
            and not _is_loopback_endpoint(self.endpoint)
        ):
            raise ConfigurationError(
                "a local compiler endpoint must use HTTP(S) on localhost or a loopback IP"
            )
        if self.max_output_tokens < 1:
            raise ConfigurationError("max_output_tokens must be positive")
        if self.data_collection not in {"deny", "allow"}:
            raise ConfigurationError("data_collection must be 'deny' or 'allow'")
        if self.transport_max_attempts is not None and self.transport_max_attempts < 1:
            raise ConfigurationError("transport_max_attempts must be positive")
        if self.semantic_max_attempts is not None and self.semantic_max_attempts < 1:
            raise ConfigurationError("semantic_max_attempts must be positive")
        if self.retry_delay_seconds is not None and (
            not isinstance(self.retry_delay_seconds, (int, float))
            or not math.isfinite(float(self.retry_delay_seconds))
            or self.retry_delay_seconds < 0
        ):
            raise ConfigurationError(
                "retry_delay_seconds must be a non-negative finite number"
            )
        if (
            not isinstance(self.min_request_interval_seconds, (int, float))
            or not math.isfinite(float(self.min_request_interval_seconds))
            or self.min_request_interval_seconds < 0
        ):
            raise ConfigurationError(
                "min_request_interval_seconds must be a non-negative finite number"
            )

    @classmethod
    def local(
        cls,
        *,
        model: str | None = None,
        endpoint: str | None = None,
        max_output_tokens: int = 8192,
        output_token_parameter: str = DEFAULT_OUTPUT_TOKEN_PARAMETER,
        semantic_max_attempts: int | None = None,
        transport_max_attempts: int | None = None,
        retry_delay_seconds: float | None = None,
        min_request_interval_seconds: float = 0.0,
    ) -> "CompilerConfig":
        return cls(
            kind=CompilerKind.LOCAL,
            model=model,
            endpoint=endpoint,
            max_output_tokens=max_output_tokens,
            output_token_parameter=output_token_parameter,
            semantic_max_attempts=semantic_max_attempts,
            transport_max_attempts=transport_max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            min_request_interval_seconds=min_request_interval_seconds,
        )

    @classmethod
    def openrouter(
        cls,
        *,
        model: str = DEFAULT_OPENROUTER_MODEL,
        provider: str | None = None,
        provider_allowlist: tuple[str, ...] = (),
        allow_fallbacks: bool | None = None,
        reasoning: str | None = None,
        max_output_tokens: int = 8192,
        output_token_parameter: str = DEFAULT_OUTPUT_TOKEN_PARAMETER,
        seed: int | None = 0,
        transport_max_attempts: int | None = None,
        semantic_max_attempts: int | None = None,
        retry_delay_seconds: float | None = None,
        min_request_interval_seconds: float = 0.0,
        capture_router_metadata: bool = False,
    ) -> "CompilerConfig":
        # OpenRouter's current Luna Pro metadata does not advertise the
        # `minimal` effort supported by GPT-5.4 Mini. Keep the common Azure
        # route, but select the lowest supported nonzero Luna effort.
        normalized_allowlist = tuple(provider_allowlist)
        resolved_provider = provider or (None if normalized_allowlist else "Azure")
        resolved_reasoning = reasoning or (
            "low" if model == LUNA_PRO_OPENROUTER_MODEL else "minimal"
        )
        return cls(
            kind=CompilerKind.OPENROUTER,
            model=model,
            provider=resolved_provider,
            provider_allowlist=normalized_allowlist,
            allow_fallbacks=(
                bool(normalized_allowlist)
                if allow_fallbacks is None
                else bool(allow_fallbacks)
            ),
            reasoning=resolved_reasoning,
            max_output_tokens=max_output_tokens,
            output_token_parameter=output_token_parameter,
            seed=seed,
            zero_data_retention=True,
            data_collection="deny",
            transport_max_attempts=transport_max_attempts,
            semantic_max_attempts=semantic_max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            min_request_interval_seconds=min_request_interval_seconds,
            capture_router_metadata=capture_router_metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        serialized = {
            "kind": self.kind.value,
            "model": self.model,
            "endpoint": self.endpoint,
            "provider": self.provider,
            "reasoning": self.reasoning,
            "max_output_tokens": self.max_output_tokens,
            "output_token_parameter": self.output_token_parameter,
            "seed": self.seed,
            "zero_data_retention": self.zero_data_retention,
            "data_collection": self.data_collection,
        }
        if self.provider_allowlist:
            serialized["provider_allowlist"] = list(self.provider_allowlist)
            serialized["allow_fallbacks"] = self.allow_fallbacks
        if self.transport_max_attempts is not None:
            serialized["transport_max_attempts"] = self.transport_max_attempts
        if self.semantic_max_attempts is not None:
            serialized["semantic_max_attempts"] = self.semantic_max_attempts
        if self.retry_delay_seconds is not None:
            serialized["retry_delay_seconds"] = self.retry_delay_seconds
        if self.min_request_interval_seconds:
            serialized["min_request_interval_seconds"] = (
                self.min_request_interval_seconds
            )
        if self.capture_router_metadata:
            serialized["capture_router_metadata"] = True
        return serialized

    @property
    def fingerprint(self) -> str:
        """Stable, credential-free identity for resumable compiler jobs."""

        behavior = self.to_dict()
        # Preserve existing compiler/cache identities for the default wire
        # behavior while ensuring an explicit compatibility override receives
        # a distinct fingerprint.
        if self.output_token_parameter == DEFAULT_OUTPUT_TOKEN_PARAMETER:
            behavior.pop("output_token_parameter")
        payload = json.dumps(behavior, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"{self.kind.value}:{digest[:20]}"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CompilerConfig":
        allowed = {
            "kind",
            "model",
            "endpoint",
            "provider",
            "provider_allowlist",
            "allow_fallbacks",
            "reasoning",
            "max_output_tokens",
            "output_token_parameter",
            "seed",
            "zero_data_retention",
            "data_collection",
            "transport_max_attempts",
            "semantic_max_attempts",
            "retry_delay_seconds",
            "min_request_interval_seconds",
            "capture_router_metadata",
        }
        try:
            return cls(**{key: item for key, item in value.items() if key in allowed})
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                "stored compiler configuration is invalid"
            ) from error


@dataclass(frozen=True)
class ProjectConfig:
    mode: MemoryMode
    compiler: CompilerConfig | None = None
    config_version: int = PROJECT_CONFIG_VERSION
    created_at: str | None = None
    updated_at: str | None = None
    migrated_from: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", normalize_mode(self.mode))
        if self.config_version != PROJECT_CONFIG_VERSION:
            raise ConfigurationError(
                f"unsupported project config version {self.config_version}; "
                f"expected {PROJECT_CONFIG_VERSION}"
            )
        if self.mode is MemoryMode.PRIVATE and self.compiler is not None:
            raise ConfigurationError("private mode cannot configure a memory compiler")
        if self.mode is MemoryMode.INTELLIGENCE and self.compiler is None:
            raise ConfigurationError(
                "intelligence mode requires a local or hosted compiler"
            )
        if (
            self.mode is MemoryMode.INTELLIGENCE
            and self.compiler is not None
            and self.compiler.kind is CompilerKind.LOCAL
            and (not self.compiler.model or not self.compiler.endpoint)
        ):
            raise ConfigurationError(
                "local intelligence mode requires both a model and a loopback HTTP endpoint"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "compiler": self.compiler.to_dict() if self.compiler else None,
            "config_version": self.config_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "migrated_from": self.migrated_from,
        }


class ProjectConfigStore:
    """Read and atomically update mode configuration in a NarratorDB file."""

    def __init__(self, db_path: str):
        self.db_path = os.path.expanduser(db_path)

    @property
    def is_memory_database(self) -> bool:
        return self.db_path == ":memory:"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _table_names(self) -> set[str]:
        if self.is_memory_database or not Path(self.db_path).is_file():
            return set()
        try:
            with self._connect() as connection:
                return {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                    ).fetchall()
                }
        except sqlite3.DatabaseError as error:
            raise ConfigurationError(
                f"database is not a readable SQLite file: {self.db_path}"
            ) from error

    def is_legacy_database(self) -> bool:
        tables = self._table_names()
        return (
            "messages" in tables
            or "metadata" in tables
            and bool(
                tables.intersection(
                    {"embeddings", "artifacts", "code_chunks", "relations"}
                )
            )
        )

    def load(self) -> ProjectConfig | None:
        if self.is_memory_database or "metadata" not in self._table_names():
            return None
        with self._connect() as connection:
            rows = dict(
                connection.execute(
                    "SELECT key, value FROM metadata WHERE key LIKE ?",
                    (f"{_CONFIG_PREFIX}%",),
                ).fetchall()
            )
        mode = rows.get(_MODE_KEY)
        if not mode:
            return None
        compiler_raw = rows.get(_COMPILER_KEY)
        compiler = None
        if compiler_raw:
            try:
                compiler_value = json.loads(compiler_raw)
            except json.JSONDecodeError as error:
                raise ConfigurationError(
                    "stored compiler configuration is invalid JSON"
                ) from error
            if not isinstance(compiler_value, dict):
                raise ConfigurationError(
                    "stored compiler configuration must be an object"
                )
            compiler = CompilerConfig.from_dict(compiler_value)
        try:
            stored_config_version = int(rows.get(_VERSION_KEY, 1))
        except ValueError as error:
            raise ConfigurationError(
                "stored project config version is invalid"
            ) from error
        if stored_config_version not in {1, PROJECT_CONFIG_VERSION}:
            raise ConfigurationError(
                f"unsupported project config version {stored_config_version}; "
                f"expected {PROJECT_CONFIG_VERSION}"
            )
        config = ProjectConfig(
            mode=normalize_mode(mode),
            compiler=compiler,
            config_version=PROJECT_CONFIG_VERSION,
            created_at=rows.get(_CREATED_AT_KEY) or None,
            updated_at=rows.get(_UPDATED_AT_KEY) or None,
            migrated_from=rows.get(_MIGRATED_FROM_KEY) or None,
        )
        if stored_config_version == 1:
            # Persist the compatibility barrier before derived-schema v3 can be
            # initialized. Frozen v1 builds reject this version instead of
            # opening a database whose retry-job semantics they do not know.
            config = self.save(config)
        return config

    def save(self, config: ProjectConfig) -> ProjectConfig:
        if self.is_memory_database:
            return config
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        created_at = config.created_at or now
        stored = ProjectConfig(
            mode=config.mode,
            compiler=config.compiler,
            config_version=config.config_version,
            created_at=created_at,
            updated_at=now,
            migrated_from=config.migrated_from,
        )
        values = {
            _MODE_KEY: stored.mode.value,
            _COMPILER_KEY: json.dumps(stored.compiler.to_dict(), sort_keys=True)
            if stored.compiler
            else "",
            _VERSION_KEY: str(stored.config_version),
            _CREATED_AT_KEY: stored.created_at or now,
            _UPDATED_AT_KEY: stored.updated_at or now,
            _MIGRATED_FROM_KEY: stored.migrated_from or "",
        }
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.executemany(
                """
                INSERT INTO metadata(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                values.items(),
            )
        return stored

    def resolve(
        self,
        *,
        mode: str | MemoryMode | None,
        compiler: CompilerConfig | None,
    ) -> ProjectConfig:
        requested_mode = normalize_mode(mode) if mode is not None else None
        current = self.load()
        if current is not None:
            if requested_mode is not None and requested_mode is not current.mode:
                raise ModeConflictError(
                    f"database is configured for {current.mode.value!r}; use set_mode() "
                    f"to switch to {requested_mode.value!r} explicitly"
                )
            if compiler is not None and compiler != current.compiler:
                raise ModeConflictError(
                    "database already has a different compiler configuration; use set_mode()"
                )
            return current

        if self.is_legacy_database():
            migrated = self.save(
                ProjectConfig(mode=MemoryMode.PRIVATE, migrated_from="legacy-1.x")
            )
            if requested_mode not in {None, MemoryMode.PRIVATE}:
                raise ModeConflictError(
                    "existing NarratorDB databases migrate to private mode; open it and call "
                    "set_mode('intelligence', compiler=...) before backfilling"
                )
            if compiler is not None:
                raise ConfigurationError(
                    "private mode cannot configure a memory compiler"
                )
            return migrated

        if requested_mode is None:
            raise ConfigurationRequiredError(
                "this is a new NarratorDB database; choose mode='private' or "
                "mode='intelligence' explicitly (or run `narratordb init`)"
            )
        return self.save(ProjectConfig(mode=requested_mode, compiler=compiler))


def default_data_dir() -> str:
    """Return NarratorDB's independent data directory."""

    explicit = os.getenv("NARRATORDB_DATA_DIR")
    if explicit:
        return os.path.expanduser(explicit)
    return str(DATA_DIR)


def default_db_path() -> str:
    """Return NarratorDB's canonical database path."""

    explicit = os.getenv("NARRATORDB_PATH") or os.getenv("NARRATORDB_DB_PATH")
    if explicit:
        return os.path.expanduser(explicit)
    return str(Path(default_data_dir()) / "memory.db")


def default_user_id(fallback: str) -> str:
    """Return the configured NarratorDB identity."""

    return os.getenv("NARRATORDB_USER_ID") or fallback
