"""Persistent, credential-free cache for query-independent memory compilation.

The cache identity is the compiler behavior fingerprint plus the canonical
session source and exact prior-claim reference context. Session and message IDs
are deliberately excluded because they are scope-local storage identities, not
source content. Evidence IDs are stored as deterministic ordinal placeholders
and rebound to the current IDs after a cache hit.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .compiler import (
    COMPILED_MEMORY_SCHEMA_VERSION,
    CompileResult,
    CompileSessionInput,
    CompiledMemory,
    CompilerError,
    CompilerUsage,
    MemoryCompiler,
    ReferenceClaim,
    SourceMessage,
    parse_compiled_memory,
)


CACHE_FORMAT_VERSION = 3
_CANONICAL_SESSION_ID = "narratordb-compiled-session-cache"


@dataclass(frozen=True, slots=True)
class CompilerCacheStats:
    """Per-cache-instance counters plus the persistent entry count."""

    hits: int
    misses: int
    writes: int
    corruptions: int
    entries: int


def source_message_to_dict(message: SourceMessage) -> dict[str, Any]:
    """Convert a source message to a JSON-compatible mapping."""

    if not isinstance(message, SourceMessage):
        raise TypeError("message must be a SourceMessage")
    return {
        "message_id": message.message_id,
        "role": message.role,
        "content": message.content,
        "occurred_at": message.occurred_at,
    }


def source_message_from_dict(payload: Mapping[str, Any]) -> SourceMessage:
    """Strictly reconstruct a source message from decoded JSON."""

    value = _mapping(payload, "source_message")
    _expect_keys(
        value, {"message_id", "role", "content", "occurred_at"}, "source_message"
    )
    return SourceMessage(
        message_id=_string(value["message_id"], "source_message.message_id"),
        role=_string(value["role"], "source_message.role"),  # type: ignore[arg-type]
        content=_string(value["content"], "source_message.content", allow_empty=True),
        occurred_at=_optional_string(
            value["occurred_at"], "source_message.occurred_at"
        ),
    )


def reference_claim_to_dict(claim: ReferenceClaim) -> dict[str, Any]:
    """Convert non-evidentiary reference context to JSON-compatible data."""

    if not isinstance(claim, ReferenceClaim):
        raise TypeError("claim must be a ReferenceClaim")
    return {
        "claim_id": claim.claim_id,
        "memory_key": claim.memory_key,
        "text": claim.text,
        "document_time": claim.document_time,
        "event_start": claim.event_start,
        "event_end": claim.event_end,
        "valid_from": claim.valid_from,
        "valid_to": claim.valid_to,
    }


def reference_claim_from_dict(payload: Mapping[str, Any]) -> ReferenceClaim:
    """Strictly reconstruct non-evidentiary reference context."""

    value = _mapping(payload, "reference_claim")
    expected = {
        "claim_id",
        "memory_key",
        "text",
        "document_time",
        "event_start",
        "event_end",
        "valid_from",
        "valid_to",
    }
    _expect_keys(value, expected, "reference_claim")
    return ReferenceClaim(
        claim_id=_string(value["claim_id"], "reference_claim.claim_id"),
        memory_key=_string(
            value["memory_key"], "reference_claim.memory_key", allow_empty=True
        ),
        text=_string(value["text"], "reference_claim.text"),
        document_time=_optional_string(
            value["document_time"], "reference_claim.document_time"
        ),
        event_start=_optional_string(
            value["event_start"], "reference_claim.event_start"
        ),
        event_end=_optional_string(value["event_end"], "reference_claim.event_end"),
        valid_from=_optional_string(value["valid_from"], "reference_claim.valid_from"),
        valid_to=_optional_string(value["valid_to"], "reference_claim.valid_to"),
    )


def compile_session_to_dict(session: CompileSessionInput) -> dict[str, Any]:
    """Convert a compiler input, including its storage identity, to JSON data."""

    if not isinstance(session, CompileSessionInput):
        raise TypeError("session must be a CompileSessionInput")
    return {
        "session_id": session.session_id,
        "messages": [source_message_to_dict(message) for message in session.messages],
        "document_time": session.document_time,
        "reference_claims": [
            reference_claim_to_dict(claim) for claim in session.reference_claims
        ],
    }


def compile_session_from_dict(payload: Mapping[str, Any]) -> CompileSessionInput:
    """Strictly reconstruct a compiler input from decoded JSON."""

    value = _mapping(payload, "compile_session")
    _expect_keys(
        value,
        {"session_id", "messages", "document_time", "reference_claims"},
        "compile_session",
    )
    messages = _list(value["messages"], "compile_session.messages")
    reference_claims = _list(
        value["reference_claims"], "compile_session.reference_claims"
    )
    return CompileSessionInput(
        session_id=_string(value["session_id"], "compile_session.session_id"),
        messages=tuple(source_message_from_dict(message) for message in messages),
        document_time=_optional_string(
            value["document_time"], "compile_session.document_time"
        ),
        reference_claims=tuple(
            reference_claim_from_dict(claim) for claim in reference_claims
        ),
    )


def compiled_memory_to_dict(memory: CompiledMemory) -> dict[str, Any]:
    """Convert every nested compiled-memory dataclass to JSON-compatible data."""

    if not isinstance(memory, CompiledMemory):
        raise TypeError("memory must be a CompiledMemory")
    return _json_round_trip(asdict(memory))


def compiled_memory_from_dict(
    payload: Mapping[str, Any],
    session: CompileSessionInput,
) -> CompiledMemory:
    """Strictly restore and source-validate cached compiled memory.

    The stored session ID is validated as a string but is not required to equal
    the current ID.  ``parse_compiled_memory`` validates all nested types,
    timestamps, entity/claim references, and exact evidence spans, and binds the
    result to ``session.session_id``.
    """

    if not isinstance(session, CompileSessionInput):
        raise TypeError("session must be a CompileSessionInput")
    value = _mapping(payload, "compiled_memory")
    expected = {
        "session_id",
        "summary",
        "claims",
        "entities",
        "relations",
        "schema_version",
    }
    _expect_keys(value, expected, "compiled_memory")
    _string(value["session_id"], "compiled_memory.session_id")
    schema_version = _string(value["schema_version"], "compiled_memory.schema_version")
    if schema_version != COMPILED_MEMORY_SCHEMA_VERSION:
        raise ValueError(f"unsupported compiled-memory schema: {schema_version!r}")
    model_payload = {
        "summary": value["summary"],
        "claims": value["claims"],
        "entities": value["entities"],
        "relations": value["relations"],
    }
    return parse_compiled_memory(model_payload, session)


def compiler_usage_to_dict(usage: CompilerUsage) -> dict[str, Any]:
    """Convert content-free compiler usage to JSON-compatible data."""

    if not isinstance(usage, CompilerUsage):
        raise TypeError("usage must be CompilerUsage")
    return _json_round_trip(asdict(usage))


def compiler_usage_from_dict(payload: Mapping[str, Any]) -> CompilerUsage:
    """Strictly reconstruct content-free compiler usage from decoded JSON."""

    value = _mapping(payload, "compiler_usage")
    required = {
        "request_model",
        "response_model",
        "provider",
        "attempt",
        "prompt_tokens",
        "cached_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cost_usd",
        "cost_source",
        "finish_reason",
    }
    optional = {
        "unknown_cost",
        "router_attempt",
        "attempted_providers",
        "attempt_statuses",
    }
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required - optional)
    if missing or extra:
        raise ValueError(
            f"compiler_usage has invalid keys (missing={missing}, extra={extra})"
        )
    cost_source = _string(value["cost_source"], "compiler_usage.cost_source")
    if cost_source not in {"provider", "estimated", "subscription", "unavailable"}:
        raise ValueError("compiler_usage.cost_source is invalid")
    raw_attempted_providers = _list(
        value.get("attempted_providers", []),
        "compiler_usage.attempted_providers",
    )
    raw_attempt_statuses = _list(
        value.get("attempt_statuses", []),
        "compiler_usage.attempt_statuses",
    )
    if len(raw_attempted_providers) != len(raw_attempt_statuses):
        raise ValueError(
            "compiler_usage provider and status attempt metadata must align"
        )
    return CompilerUsage(
        request_model=_string(
            value["request_model"], "compiler_usage.request_model", allow_empty=True
        ),
        response_model=_string(
            value["response_model"], "compiler_usage.response_model", allow_empty=True
        ),
        provider=_string(
            value["provider"], "compiler_usage.provider", allow_empty=True
        ),
        attempt=_positive_int(value["attempt"], "compiler_usage.attempt"),
        prompt_tokens=_nonnegative_int(
            value["prompt_tokens"], "compiler_usage.prompt_tokens"
        ),
        cached_tokens=_nonnegative_int(
            value["cached_tokens"], "compiler_usage.cached_tokens"
        ),
        completion_tokens=_nonnegative_int(
            value["completion_tokens"], "compiler_usage.completion_tokens"
        ),
        reasoning_tokens=_nonnegative_int(
            value["reasoning_tokens"], "compiler_usage.reasoning_tokens"
        ),
        cost_usd=_nonnegative_float(value["cost_usd"], "compiler_usage.cost_usd"),
        cost_source=cost_source,  # type: ignore[arg-type]
        finish_reason=_string(
            value["finish_reason"], "compiler_usage.finish_reason", allow_empty=True
        ),
        unknown_cost=_boolean(
            value.get("unknown_cost", False), "compiler_usage.unknown_cost"
        ),
        router_attempt=(
            None
            if value.get("router_attempt") is None
            else _nonnegative_int(
                value["router_attempt"], "compiler_usage.router_attempt"
            )
        ),
        attempted_providers=tuple(
            _string(
                provider,
                f"compiler_usage.attempted_providers[{index}]",
            )
            for index, provider in enumerate(raw_attempted_providers)
        ),
        attempt_statuses=tuple(
            _nonnegative_int(
                status,
                f"compiler_usage.attempt_statuses[{index}]",
            )
            for index, status in enumerate(raw_attempt_statuses)
        ),
    )


def compile_result_to_dict(result: CompileResult) -> dict[str, Any]:
    """Convert a complete typed compiler result to JSON-compatible data."""

    if not isinstance(result, CompileResult):
        raise TypeError("result must be a CompileResult")
    return {
        "memory": compiled_memory_to_dict(result.memory),
        "usage": [compiler_usage_to_dict(event) for event in result.usage],
    }


def compile_result_from_dict(
    payload: Mapping[str, Any],
    session: CompileSessionInput,
) -> CompileResult:
    """Strictly reconstruct a complete typed compiler result from decoded JSON."""

    value = _mapping(payload, "compile_result")
    _expect_keys(value, {"memory", "usage"}, "compile_result")
    usage = _list(value["usage"], "compile_result.usage")
    return CompileResult(
        memory=compiled_memory_from_dict(
            _mapping(value["memory"], "compile_result.memory"), session
        ),
        usage=tuple(compiler_usage_from_dict(event) for event in usage),
    )


def serialize_compile_session(session: CompileSessionInput) -> str:
    """Serialize a compiler input as deterministic strict JSON."""

    return _json_dumps(compile_session_to_dict(session))


def deserialize_compile_session(serialized: str) -> CompileSessionInput:
    """Deserialize strict JSON into a compiler input."""

    return compile_session_from_dict(
        _mapping(_json_loads(serialized), "compile_session")
    )


def serialize_compile_result(result: CompileResult) -> str:
    """Serialize a complete compiler result as deterministic strict JSON."""

    return _json_dumps(compile_result_to_dict(result))


def deserialize_compile_result(
    serialized: str, session: CompileSessionInput
) -> CompileResult:
    """Deserialize and source-validate a complete compiler result."""

    return compile_result_from_dict(
        _mapping(_json_loads(serialized), "compile_result"), session
    )


def compiled_session_cache_key(
    compiler_fingerprint: str,
    session: CompileSessionInput,
) -> str:
    """Return the deterministic cache key without exposing source or fingerprint."""

    compiler_hash, source_hash = _cache_identity(compiler_fingerprint, session)
    material = f"narratordb-compiler-cache-v{CACHE_FORMAT_VERSION}:{compiler_hash}:{source_hash}"
    return hashlib.sha256(material.encode("ascii")).hexdigest()


class CompiledSessionCache:
    """SQLite-backed compiled-memory cache safe for threads and processes."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        busy_timeout_ms: int = 10_000,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        raw_path = os.fspath(path)
        self.path = Path(raw_path)
        self._in_memory = raw_path == ":memory:"
        if raw_path != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._corruptions = 0
        self._connection = sqlite3.connect(
            raw_path if raw_path == ":memory:" else self.path,
            timeout=busy_timeout_ms / 1000,
            check_same_thread=False,
        )
        self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA secure_delete = ON")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS compiled_session_cache (
                cache_key TEXT PRIMARY KEY,
                compiler_hash TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                cache_format_version INTEGER NOT NULL,
                memory_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_accessed_at REAL NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS compiler_cache_source_idx "
            "ON compiled_session_cache(compiler_hash, source_hash)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS compiler_cache_leases (
                cache_key TEXT PRIMARY KEY,
                owner_token TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._connection.commit()

    def get(
        self,
        compiler_fingerprint: str,
        session: CompileSessionInput,
    ) -> CompiledMemory | None:
        """Return validated memory on a hit, or ``None`` on a miss/corruption."""

        return self._get(compiler_fingerprint, session, count_miss=True)

    def _get(
        self,
        compiler_fingerprint: str,
        session: CompileSessionInput,
        *,
        count_miss: bool,
    ) -> CompiledMemory | None:
        """Internal lookup that can avoid counting repeated singleflight polls."""

        compiler_hash, source_hash = _cache_identity(compiler_fingerprint, session)
        cache_key = compiled_session_cache_key(compiler_fingerprint, session)
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT cache_format_version, memory_json FROM compiled_session_cache "
                "WHERE cache_key = ? AND compiler_hash = ? AND source_hash = ?",
                (cache_key, compiler_hash, source_hash),
            ).fetchone()
            if row is None:
                if count_miss:
                    self._misses += 1
                return None
            try:
                if row[0] != CACHE_FORMAT_VERSION:
                    raise ValueError("unsupported compiler-cache format")
                memory_payload = _mapping(_json_loads(row[1]), "compiled_memory")
                canonical_session, _to_canonical, to_current = _canonical_session(
                    session
                )
                canonical_memory = compiled_memory_from_dict(
                    memory_payload, canonical_session
                )
                rebound_payload = _remap_memory_payload(
                    compiled_memory_to_dict(canonical_memory),
                    to_current,
                    session_id=session.session_id,
                )
                memory = compiled_memory_from_dict(rebound_payload, session)
            except (
                CompilerError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
            ):
                with self._connection:
                    self._connection.execute(
                        "DELETE FROM compiled_session_cache WHERE cache_key = ?",
                        (cache_key,),
                    )
                if count_miss:
                    self._misses += 1
                self._corruptions += 1
                return None
            now = time.time()
            with self._connection:
                self._connection.execute(
                    "UPDATE compiled_session_cache SET last_accessed_at = ?, "
                    "hit_count = hit_count + 1 WHERE cache_key = ?",
                    (now, cache_key),
                )
            self._hits += 1
            return memory

    def _try_acquire_compile_lease(
        self,
        cache_key: str,
        owner_token: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        """Atomically acquire a missing or expired content-free compile lease."""

        _validate_hashed_cache_key(cache_key)
        _validate_owner_token(owner_token)
        _positive_finite_float(ttl_seconds, "ttl_seconds")
        now = time.time()
        expires_at = now + ttl_seconds
        with self._lock:
            self._ensure_open()
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO compiler_cache_leases (
                        cache_key, owner_token, acquired_at, expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        owner_token = excluded.owner_token,
                        acquired_at = excluded.acquired_at,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    WHERE compiler_cache_leases.expires_at <= excluded.acquired_at
                    """,
                    (cache_key, owner_token, now, expires_at, now),
                )
                row = self._connection.execute(
                    "SELECT owner_token FROM compiler_cache_leases WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
        return row is not None and row[0] == owner_token

    def _renew_compile_lease(
        self,
        cache_key: str,
        owner_token: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        """Extend a live lease only while it is still owned and unexpired."""

        _validate_hashed_cache_key(cache_key)
        _validate_owner_token(owner_token)
        _positive_finite_float(ttl_seconds, "ttl_seconds")
        now = time.time()
        with self._lock:
            self._ensure_open()
            with self._connection:
                cursor = self._connection.execute(
                    """
                    UPDATE compiler_cache_leases
                    SET expires_at = ?, updated_at = ?
                    WHERE cache_key = ? AND owner_token = ? AND expires_at > ?
                    """,
                    (now + ttl_seconds, now, cache_key, owner_token, now),
                )
        return cursor.rowcount == 1

    def _release_compile_lease(self, cache_key: str, owner_token: str) -> bool:
        """Release only the caller's lease, never a newer recovery owner's lease."""

        _validate_hashed_cache_key(cache_key)
        _validate_owner_token(owner_token)
        with self._lock:
            self._ensure_open()
            with self._connection:
                cursor = self._connection.execute(
                    "DELETE FROM compiler_cache_leases "
                    "WHERE cache_key = ? AND owner_token = ?",
                    (cache_key, owner_token),
                )
        return cursor.rowcount == 1

    def put(
        self,
        compiler_fingerprint: str,
        session: CompileSessionInput,
        memory: CompiledMemory,
    ) -> None:
        """Validate and atomically upsert one compiled-memory entry."""

        if not isinstance(memory, CompiledMemory):
            raise TypeError("memory must be a CompiledMemory")
        if memory.session_id != session.session_id:
            raise ValueError(
                "compiled memory must belong to the compiled input session"
            )
        compiler_hash, source_hash = _cache_identity(compiler_fingerprint, session)
        cache_key = compiled_session_cache_key(compiler_fingerprint, session)
        payload = compiled_memory_to_dict(memory)
        validated_memory = compiled_memory_from_dict(payload, session)
        canonical_session, to_canonical, _to_current = _canonical_session(session)
        canonical_payload = _remap_memory_payload(
            compiled_memory_to_dict(validated_memory),
            to_canonical,
            session_id=_CANONICAL_SESSION_ID,
        )
        compiled_memory_from_dict(canonical_payload, canonical_session)
        memory_json = _json_dumps(canonical_payload)
        now = time.time()
        with self._lock:
            self._ensure_open()
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO compiled_session_cache (
                        cache_key, compiler_hash, source_hash, cache_format_version,
                        memory_json, created_at, updated_at, last_accessed_at, hit_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        compiler_hash = excluded.compiler_hash,
                        source_hash = excluded.source_hash,
                        cache_format_version = excluded.cache_format_version,
                        memory_json = excluded.memory_json,
                        updated_at = excluded.updated_at,
                        last_accessed_at = excluded.last_accessed_at
                    """,
                    (
                        cache_key,
                        compiler_hash,
                        source_hash,
                        CACHE_FORMAT_VERSION,
                        memory_json,
                        now,
                        now,
                        now,
                    ),
                )
            self._writes += 1

    def clear(self) -> int:
        """Delete entries and leases, then compact content-bearing SQLite pages."""

        with self._lock:
            self._ensure_open()
            with self._connection:
                count = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM compiled_session_cache"
                    ).fetchone()[0]
                )
                self._connection.execute("DELETE FROM compiled_session_cache")
                self._connection.execute("DELETE FROM compiler_cache_leases")
            if not self._in_memory:
                self._checkpoint_and_compact()
            return count

    def stats(self) -> CompilerCacheStats:
        """Return local hit/miss counters and current persistent entry count."""

        with self._lock:
            self._ensure_open()
            entries = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM compiled_session_cache"
                ).fetchone()[0]
            )
            return CompilerCacheStats(
                hits=self._hits,
                misses=self._misses,
                writes=self._writes,
                corruptions=self._corruptions,
                entries=entries,
            )

    def close(self) -> None:
        """Close this cache connection.  Calling twice is harmless."""

        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def __enter__(self) -> CompiledSessionCache:
        self._ensure_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("compiled-session cache is closed")

    def _checkpoint_and_compact(self) -> None:
        """Best-effort compaction after destructive privacy operations."""

        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        try:
            # VACUUM must run outside the deletion transaction.  It eliminates
            # free pages that may previously have held derived quotes.
            self._connection.execute("VACUUM")
        except sqlite3.OperationalError as error:
            if (
                "locked" not in str(error).casefold()
                and "busy" not in str(error).casefold()
            ):
                raise
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()


class CachedMemoryCompiler:
    """A ``MemoryCompiler`` decorator that skips usage and cost on cache hits."""

    def __init__(
        self,
        compiler: MemoryCompiler,
        cache: CompiledSessionCache,
        *,
        lease_ttl_seconds: float = 300.0,
        wait_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if not isinstance(compiler, MemoryCompiler):
            raise TypeError("compiler must implement MemoryCompiler")
        if not isinstance(cache, CompiledSessionCache):
            raise TypeError("cache must be a CompiledSessionCache")
        _positive_finite_float(lease_ttl_seconds, "lease_ttl_seconds")
        _positive_finite_float(wait_timeout_seconds, "wait_timeout_seconds")
        _positive_finite_float(poll_interval_seconds, "poll_interval_seconds")
        self.compiler = compiler
        self.cache = cache
        self.lease_ttl_seconds = float(lease_ttl_seconds)
        self.wait_timeout_seconds = float(wait_timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)

    @property
    def fingerprint(self) -> str:
        return self.compiler.fingerprint

    def compile_session(self, session: CompileSessionInput) -> CompileResult:
        if not isinstance(session, CompileSessionInput):
            raise TypeError("session must be a CompileSessionInput")
        fingerprint = self.fingerprint
        memory = self.cache.get(fingerprint, session)
        if memory is not None:
            return CompileResult(memory=memory, usage=())
        cache_key = compiled_session_cache_key(fingerprint, session)
        owner_token = uuid.uuid4().hex
        deadline = time.monotonic() + self.wait_timeout_seconds

        while True:
            try:
                acquired = self.cache._try_acquire_compile_lease(
                    cache_key,
                    owner_token,
                    ttl_seconds=self.lease_ttl_seconds,
                )
            except (RuntimeError, sqlite3.Error) as error:
                raise _singleflight_error(
                    "compiler cache coordination is unavailable"
                ) from error

            if acquired:
                return self._compile_with_lease(
                    session,
                    fingerprint=fingerprint,
                    cache_key=cache_key,
                    owner_token=owner_token,
                )

            try:
                memory = self.cache._get(fingerprint, session, count_miss=False)
            except (RuntimeError, sqlite3.Error) as error:
                raise _singleflight_error(
                    "compiler cache coordination is unavailable"
                ) from error
            if memory is not None:
                return CompileResult(memory=memory, usage=())

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _singleflight_error(
                    "timed out waiting for an active memory compilation"
                )
            time.sleep(min(self.poll_interval_seconds, remaining))

    def _compile_with_lease(
        self,
        session: CompileSessionInput,
        *,
        fingerprint: str,
        cache_key: str,
        owner_token: str,
    ) -> CompileResult:
        """Double-check, compile once, and always release the acquired lease."""

        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._renew_lease_until_stopped,
            args=(cache_key, owner_token, heartbeat_stop),
            name="narratordb-compiler-cache-lease",
            daemon=True,
        )
        try:
            memory = self.cache._get(fingerprint, session, count_miss=False)
            if memory is not None:
                return CompileResult(memory=memory, usage=())
            heartbeat.start()
            result = self.compiler.compile_session(session)
            self.cache.put(fingerprint, session, result.memory)
            return result
        finally:
            heartbeat_stop.set()
            if heartbeat.is_alive():
                heartbeat.join(timeout=min(1.0, self.lease_ttl_seconds))
            try:
                self.cache._release_compile_lease(cache_key, owner_token)
            except (RuntimeError, sqlite3.Error):
                # The row expires safely if the cache was closed or unavailable.
                pass

    def _renew_lease_until_stopped(
        self,
        cache_key: str,
        owner_token: str,
        stop: threading.Event,
    ) -> None:
        interval = min(self.lease_ttl_seconds / 3.0, 30.0)
        while not stop.wait(interval):
            try:
                renewed = self.cache._renew_compile_lease(
                    cache_key,
                    owner_token,
                    ttl_seconds=self.lease_ttl_seconds,
                )
            except (RuntimeError, sqlite3.Error):
                return
            if not renewed:
                return

    def cache_stats(self) -> CompilerCacheStats:
        return self.cache.stats()


# A discoverable alternate spelling for callers that think in decorators.
CachingMemoryCompiler = CachedMemoryCompiler


def _singleflight_error(message: str) -> CompilerError:
    return CompilerError(
        message,
        code="compiler_singleflight_unavailable",
        retryable=True,
    )


def _cache_identity(
    compiler_fingerprint: str,
    session: CompileSessionInput,
) -> tuple[str, str]:
    if not isinstance(compiler_fingerprint, str) or not compiler_fingerprint.strip():
        raise ValueError("compiler fingerprint must be a non-empty string")
    if not isinstance(session, CompileSessionInput):
        raise TypeError("session must be a CompileSessionInput")
    compiler_hash = hashlib.sha256(compiler_fingerprint.encode("utf-8")).hexdigest()
    source = {
        "document_time": session.document_time,
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "occurred_at": message.occurred_at,
            }
            for message in session.messages
        ],
        "reference_claims": [
            reference_claim_to_dict(claim) for claim in session.reference_claims
        ],
    }
    source_hash = hashlib.sha256(_json_dumps(source).encode("utf-8")).hexdigest()
    return compiler_hash, source_hash


def _validate_hashed_cache_key(cache_key: str) -> None:
    if (
        not isinstance(cache_key, str)
        or len(cache_key) != 64
        or any(character not in "0123456789abcdef" for character in cache_key)
    ):
        raise ValueError("cache_key must be a lowercase SHA-256 digest")


def _validate_owner_token(owner_token: str) -> None:
    if not isinstance(owner_token, str) or not owner_token:
        raise ValueError("owner_token must be a non-empty string")


def _positive_finite_float(value: float, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{path} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{path} must be a finite positive number")
    return result


def _canonical_message_id(index: int) -> str:
    return f"source-{index:08d}"


def _canonical_session(
    session: CompileSessionInput,
) -> tuple[CompileSessionInput, dict[str, str], dict[str, str]]:
    """Replace scope-local IDs with stable ordinals while preserving source order."""

    canonical_messages: list[SourceMessage] = []
    to_canonical: dict[str, str] = {}
    to_current: dict[str, str] = {}
    for index, message in enumerate(session.messages):
        canonical_id = _canonical_message_id(index)
        to_canonical[message.message_id] = canonical_id
        to_current[canonical_id] = message.message_id
        canonical_messages.append(
            SourceMessage(
                message_id=canonical_id,
                role=message.role,
                content=message.content,
                occurred_at=message.occurred_at,
            )
        )
    return (
        CompileSessionInput(
            session_id=_CANONICAL_SESSION_ID,
            messages=tuple(canonical_messages),
            document_time=session.document_time,
            reference_claims=session.reference_claims,
        ),
        to_canonical,
        to_current,
    )


def _remap_memory_payload(
    payload: Mapping[str, Any],
    message_ids: Mapping[str, str],
    *,
    session_id: str,
) -> dict[str, Any]:
    """Deep-copy a validated memory payload and replace every evidence ID."""

    result = _json_round_trip(payload)
    result["session_id"] = session_id
    evidence_lists = [result["summary"]["evidence"]]
    evidence_lists.extend(entity["evidence"] for entity in result["entities"])
    evidence_lists.extend(claim["evidence"] for claim in result["claims"])
    evidence_lists.extend(relation["evidence"] for relation in result["relations"])
    for evidence_list in evidence_lists:
        for evidence in evidence_list:
            current_id = evidence["message_id"]
            try:
                evidence["message_id"] = message_ids[current_id]
            except KeyError as error:
                raise ValueError(
                    f"compiled evidence references unmappable message ID: {current_id!r}"
                ) from error
    return result


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_loads(serialized: str) -> Any:
    if not isinstance(serialized, str):
        raise TypeError("serialized JSON must be a string")

    def strict_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(
        serialized,
        object_pairs_hook=strict_object,
        parse_constant=reject_constant,
    )


def _json_round_trip(value: Any) -> Any:
    return _json_loads(_json_dumps(value))


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must be an object")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{path} must be an array")
    return value


def _expect_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{path} has invalid keys (missing={missing}, extra={extra})")


def _string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        suffix = "a string" if allow_empty else "a non-empty string"
        raise TypeError(f"{path} must be {suffix}")
    return value


def _optional_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, path)


def _nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"{path} must be a non-negative integer")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    return value


def _positive_int(value: Any, path: str) -> int:
    result = _nonnegative_int(value, path)
    if result == 0:
        raise ValueError(f"{path} must be positive")
    return result


def _nonnegative_float(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{path} must be a non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{path} must be a finite non-negative number")
    return result
