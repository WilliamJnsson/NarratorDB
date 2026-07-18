#!/usr/bin/env python3
"""Mem0-compatible HTTP adapter for reproducible benchmark harnesses.

The adapter deliberately implements the OSS endpoints consumed by
``mem0ai/memory-benchmarks`` so NarratorDB can be evaluated by the exact same
dataset loader, prompts, top-k handling, answerer, judge, and scoring code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .compiler import (
    ContentFreeUsageLedger,
    MemoryCompiler,
    compiler_from_project_config,
)
from .compiler_cache import CachedMemoryCompiler, CompiledSessionCache
from .config import (
    DEFAULT_CODEX_CLI_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_OUTPUT_TOKEN_PARAMETER,
    SUPPORTED_OUTPUT_TOKEN_PARAMETERS,
    CompilerConfig,
    CompilerKind,
    MemoryMode,
    normalize_mode,
)
from .engine import Engine
from .enrichment import EnrichmentRunner


DEFAULT_INTELLIGENCE_TOKEN_BUDGET = 6000


def merge_adjacent_hits(hits: list, gap: int = 1, max_chars: int = 1200) -> list:
    """Group position-adjacent same-session hits into one evidence block.

    hits: ranked Message objects. Hits sharing a provenance run_id whose
    positions chain within ``gap`` of an already-grouped member are merged
    into a single returned memory (position order, "[speaker]: text" lines,
    bounded by ``max_chars``). Each block keeps the rank of its best member,
    so downstream top-k slots carry coherent evidence instead of fragments.

    Returns dicts: {"id", "memory", "timestamp"} in final rank order.
    """
    consumed = [False] * len(hits)
    merged = []
    for index, hit in enumerate(hits):
        if consumed[index]:
            continue
        consumed[index] = True
        run_id = (hit.provenance or {}).get("run_id")
        members = [hit]
        if run_id is not None and gap >= 0:
            positions = {hit.position}
            total_chars = len(hit.text)
            grew = True
            while grew:
                grew = False
                for other_index in range(index + 1, len(hits)):
                    if consumed[other_index]:
                        continue
                    other = hits[other_index]
                    if (other.provenance or {}).get("run_id") != run_id:
                        continue
                    near = any(abs(other.position - p) <= gap for p in positions)
                    if not near:
                        continue
                    if total_chars + len(other.text) + 16 > max_chars:
                        continue
                    consumed[other_index] = True
                    members.append(other)
                    positions.add(other.position)
                    total_chars += len(other.text) + 16
                    grew = True
        members.sort(key=lambda m: m.position)
        if len(members) == 1:
            # Never truncate an unmerged hit: max_chars bounds merging, not
            # the underlying stored message.
            memory = hit.text
        else:
            memory = "\n".join(f"[{m.speaker}]: {m.text}" for m in members)[:max_chars]
        merged.append(
            {
                "id": str(hit.id),
                "memory": memory,
                "timestamp": hit.timestamp,
            }
        )
    return merged


class NarratorDBBenchmarkBackend:
    def __init__(
        self,
        database: str,
        merge_adjacent: bool = True,
        merge_gap: int = 1,
        merge_max_chars: int = 1200,
        *,
        mode: str | MemoryMode = MemoryMode.PRIVATE,
        compiler_config: CompilerConfig | None = None,
        compiler: MemoryCompiler | None = None,
        compiler_cache: CompiledSessionCache | None = None,
        compiler_usage_sink: ContentFreeUsageLedger | None = None,
        codex_executable: str = "codex",
        codex_home: Path | None = None,
        context_token_budget: int = DEFAULT_INTELLIGENCE_TOKEN_BUDGET,
        coalesce_sessions: bool = False,
        existing_derived_fingerprint: str | None = None,
    ):
        self.database = database
        self.merge_adjacent = merge_adjacent
        self.merge_gap = merge_gap
        self.merge_max_chars = merge_max_chars
        self.mode = normalize_mode(mode)
        self.compiler_config = compiler_config
        self.coalesce_sessions = bool(coalesce_sessions)
        replay_fingerprint = (
            str(existing_derived_fingerprint).strip()
            if existing_derived_fingerprint is not None
            else None
        )
        if replay_fingerprint == "":
            raise ValueError("existing-derived replay fingerprint cannot be empty")
        if replay_fingerprint is not None and len(replay_fingerprint) > 512:
            raise ValueError("existing-derived replay fingerprint is too long")
        self.existing_derived_fingerprint = replay_fingerprint
        if self.coalesce_sessions and self.mode is not MemoryMode.INTELLIGENCE:
            raise ValueError("session coalescing requires intelligence benchmark mode")
        if (
            self.existing_derived_fingerprint is not None
            and self.mode is not MemoryMode.INTELLIGENCE
        ):
            raise ValueError(
                "existing-derived replay requires intelligence benchmark mode"
            )
        if context_token_budget < 128:
            raise ValueError("context_token_budget must be at least 128")
        self.context_token_budget = int(context_token_budget)
        self._compiler_cache: CompiledSessionCache | None = None
        if self.existing_derived_fingerprint is not None:
            if any(
                value is not None
                for value in (
                    compiler_config,
                    compiler,
                    compiler_cache,
                    compiler_usage_sink,
                )
            ):
                raise ValueError(
                    "existing-derived replay does not accept compiler, cache, or "
                    "usage options"
                )
            # Deliberately construct neither a compiler nor a compiler cache.
            # Search is allowed only after the copied scope proves that every
            # current source already has a terminal job for the declared
            # fingerprint. No fallback can compile or rematerialize data.
            self._compiler = None
        elif self.mode is MemoryMode.PRIVATE:
            if (
                compiler_config is not None
                or compiler is not None
                or compiler_cache is not None
            ):
                raise ValueError(
                    "private benchmark mode does not accept compiler options"
                )
            self._compiler = None
        else:
            if compiler is not None and compiler_config is not None:
                raise ValueError("pass compiler or compiler_config, not both")
            if compiler is None:
                if compiler_config is None:
                    raise ValueError("intelligence benchmark mode requires a compiler")
                compiler_options = {}
                if compiler_config.kind is CompilerKind.CODEX_CLI:
                    compiler_options = {
                        "codex_executable": codex_executable,
                        "codex_home": codex_home,
                    }
                compiler = compiler_from_project_config(
                    compiler_config,
                    usage_sink=compiler_usage_sink,
                    **compiler_options,
                )
            cache_path = (
                ":memory:"
                if database == ":memory:"
                else f"{database}.compiler-cache.sqlite3"
            )
            self._compiler_cache = compiler_cache or CompiledSessionCache(cache_path)
            self._compiler = CachedMemoryCompiler(compiler, self._compiler_cache)
        self._engines: dict[str, Engine] = {}
        self._engine_locks: dict[str, threading.RLock] = {}
        self._registry_lock = threading.RLock()

    def engine_and_lock(self, user_id: str) -> tuple[Engine, threading.RLock]:
        with self._registry_lock:
            engine = self._engines.get(user_id)
            engine_lock = self._engine_locks.setdefault(user_id, threading.RLock())
        if engine is not None:
            return engine, engine_lock

        # Construct different user engines concurrently; serialize only callers
        # racing to create the same user scope.
        with engine_lock:
            with self._registry_lock:
                engine = self._engines.get(user_id)
            if engine is None:
                engine = Engine(
                    self.database,
                    user_id=user_id,
                    context_window=0,
                    semantic_search_mode=(
                        "hybrid"
                        if self.mode is MemoryMode.INTELLIGENCE
                        else "fallback_only"
                    ),
                    # A memory mode must never trigger an embedding download.
                    # Hosted compiler egress is separate and explicitly opted in.
                    local_only=True,
                )
                with self._registry_lock:
                    self._engines[user_id] = engine
        return engine, engine_lock

    def engine(self, user_id: str) -> Engine:
        return self.engine_and_lock(user_id)[0]

    def _reject_existing_derived_mutation(self, operation: str) -> None:
        if self.existing_derived_fingerprint is not None:
            raise ValueError(f"{operation} is disabled in existing-derived replay mode")

    def _existing_derived_diagnostics(self, engine: Engine) -> dict:
        """Return content-free readiness for one copied intelligence scope."""

        fingerprint = self.existing_derived_fingerprint
        if fingerprint is None:
            raise RuntimeError("existing-derived replay is not enabled")
        rows = engine._conn.execute(
            """
            SELECT j.status
            FROM memory_sessions s
            LEFT JOIN memory_compiler_jobs j
              ON j.user_id = s.user_id
             AND j.session_id = s.id
             AND j.source_hash = s.source_hash
             AND j.compiler_fingerprint = ?
            WHERE s.user_id = ?
            ORDER BY s.id
            """,
            (fingerprint, engine.user_id),
        ).fetchall()
        status_counts: dict[str, int] = {}
        for row in rows:
            status = "missing" if row[0] is None else str(row[0])
            status_counts[status] = status_counts.get(status, 0) + 1
        complete = status_counts.get("complete", 0)
        partial = status_counts.get("partial", 0)
        terminal = complete + partial
        registered = len(rows)
        return {
            "enabled": True,
            "compiler_fingerprint": fingerprint,
            "current_source_only": True,
            "registered_sessions": registered,
            "terminal_sessions": terminal,
            "complete_sessions": complete,
            "partial_sessions": partial,
            "nonterminal_sessions": registered - terminal,
            "nonterminal_statuses": {
                status: count
                for status, count in sorted(status_counts.items())
                if status not in {"complete", "partial"}
            },
            # An empty scope is never replay-ready: it usually means the
            # harness supplied a new run ID and therefore a different user ID.
            "ready": registered > 0 and terminal == registered,
            "compiler_constructed": False,
            "compiler_cache_constructed": False,
            "mutations_allowed": False,
        }

    def existing_derived_diagnostics(self, user_id: str) -> dict:
        """Inspect replay readiness without returning memory or identifiers."""

        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        engine, engine_lock = self.engine_and_lock(normalized_user_id)
        with engine_lock:
            return self._existing_derived_diagnostics(engine)

    def add(self, payload: dict) -> dict:
        self._reject_existing_derived_mutation("add")
        user_id = str(payload.get("user_id") or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        timestamp = payload.get("timestamp")
        metadata = (
            payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        )
        rows = []
        chunk_id = self._chunk_id(payload) if self.coalesce_sessions else None
        session_id = (
            (
                self._coalesced_session_id(payload, metadata)
                if self.coalesce_sessions
                else self._session_id(payload, metadata)
            )
            if self.mode is MemoryMode.INTELLIGENCE
            else str(
                metadata.get("session_id") or metadata.get("run_id") or "benchmark"
            )
        )
        for index, message in enumerate(payload.get("messages") or []):
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            role = str(message.get("role") or "memory")
            provenance = {
                "provider": "official-memory-benchmarks",
                "run_id": session_id,
                "metadata": {
                    **metadata,
                    "chunk_index": index,
                    **({"benchmark_chunk_id": chunk_id} if chunk_id else {}),
                },
            }
            rows.append(
                {
                    "speaker": role,
                    "text": content,
                    "timestamp": float(timestamp) + (index / 1000)
                    if timestamp is not None
                    else time.time(),
                    "provenance": provenance,
                }
            )
        engine, engine_lock = self.engine_and_lock(user_id)
        with engine_lock:
            if not rows:
                stored_count = 0
                stored = None
            elif self.mode is MemoryMode.INTELLIGENCE:
                stored = engine.store_session(
                    rows,
                    session_id=session_id,
                    occurred_at=float(timestamp) if timestamp is not None else None,
                    metadata=metadata,
                    append=self.coalesce_sessions,
                )
                stored_count = int(stored["stored"])
            else:
                before = engine.stats()["message_count"]
                engine.store_batch(rows)
                after = engine.stats()["message_count"]
                stored_count = max(0, int(after) - int(before))
            # Keep the in-memory embedding matrix warm so the scope's first
            # search does not pay a cold full-corpus index build.
            engine._ensure_embedding_index()
            if self.mode is MemoryMode.INTELLIGENCE and stored is not None:
                if self.coalesce_sessions:
                    self._compile_registered_sessions(
                        engine,
                        exclude_session_pk=int(stored["session_pk"]),
                    )
                else:
                    self._compile_stored_session(engine, stored)
        return {
            "results": [
                {"event": "ADD", "memory": row["text"]} for row in rows[:stored_count]
            ]
        }

    @staticmethod
    def _session_id(payload: dict, metadata: dict) -> str:
        explicit = metadata.get("session_id") or metadata.get("run_id")
        if explicit is not None and str(explicit).strip():
            return str(explicit).strip()
        # The fallback is stable and depends only on the content being added,
        # never on a benchmark question or later search query.
        source = {
            "timestamp": payload.get("timestamp"),
            "messages": [
                {
                    "role": str(message.get("role") or "memory"),
                    "content": str(message.get("content") or ""),
                }
                for message in payload.get("messages") or []
                if isinstance(message, dict)
            ],
        }
        encoded = json.dumps(
            source, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return f"benchmark-{hashlib.sha256(encoded).hexdigest()[:24]}"

    @classmethod
    def _coalesced_session_id(cls, payload: dict, metadata: dict) -> str:
        explicit = metadata.get("session_id") or metadata.get("run_id")
        if explicit is not None and str(explicit).strip():
            return str(explicit).strip()
        timestamp = payload.get("timestamp")
        if timestamp is None:
            # Without either explicit identity or a timestamp, retaining the
            # pair-level content identity is safer than merging unrelated adds.
            return cls._session_id(payload, metadata)
        normalized_timestamp = float(timestamp)
        if not math.isfinite(normalized_timestamp):
            raise ValueError("timestamp must be finite for session coalescing")
        if normalized_timestamp == 0:
            normalized_timestamp = 0.0
        encoded = json.dumps(
            {"timestamp_hex": normalized_timestamp.hex()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"benchmark-session-{hashlib.sha256(encoded).hexdigest()[:24]}"

    @staticmethod
    def _chunk_id(payload: dict) -> str:
        source = [
            {
                "role": str(message.get("role") or "memory"),
                "content": str(message.get("content") or "").strip(),
            }
            for message in payload.get("messages") or []
            if isinstance(message, dict) and str(message.get("content") or "").strip()
        ]
        encoded = json.dumps(
            source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]

    def _compile_stored_session(self, engine: Engine, stored: dict) -> bool:
        assert self._compiler is not None
        job_id = engine.enqueue_compilation(
            int(stored["session_pk"]),
            str(stored["source_hash"]),
            self._compiler.fingerprint,
        )
        job = next(
            (
                candidate
                for candidate in engine.pending_compilations(limit=100_000)
                if int(candidate["id"]) == job_id
            ),
            None,
        )
        if job is None:
            state = engine._conn.execute(
                """
                SELECT status, attempts FROM memory_compiler_jobs
                WHERE id = ? AND user_id = ?
                """,
                (job_id, engine.user_id),
            ).fetchone()
            if state is not None and str(state[0]) in {"complete", "partial"}:
                # A completed idempotent job is intentionally absent from the
                # actionable queue and requires no compiler call.
                return False
            if state is not None and str(state[0]) == "running":
                # Another process can claim the same current-source job after
                # our dirty-session scan. That is healthy in-progress work,
                # not an ingestion or finalization failure.
                return False
            status = str(state[0]) if state is not None else "missing"
            attempts = int(state[1]) if state is not None else 0
            raise RuntimeError(
                "memory compiler job is not runnable after raw session commit "
                f"(job {job_id}, status={status}, attempts={attempts})"
            )
        outcome = EnrichmentRunner(engine, self._compiler).run_job(job)
        if not outcome.get("ok"):
            code = str(outcome.get("code") or "compiler_failed")
            raise RuntimeError(
                f"memory compiler failed after raw session commit (job {job_id}, {code})"
            )
        return True

    def _compile_registered_sessions(
        self,
        engine: Engine,
        *,
        exclude_session_pk: int | None = None,
        session_id: str | None = None,
    ) -> int:
        """Compile dirty current sources and return the materialized count."""

        assert self._compiler is not None
        clauses = ["s.user_id = ?"]
        parameters: list = [engine.user_id]
        if session_id is not None:
            clauses.append("s.external_id = ?")
            parameters.append(session_id)
        parameters.append(self._compiler.fingerprint)
        parameters.append(time.time() - 300.0)
        rows = engine._conn.execute(
            f"""
            SELECT s.id, s.source_hash FROM memory_sessions s
            WHERE {" AND ".join(clauses)} AND NOT EXISTS (
                SELECT 1 FROM memory_compiler_jobs j
                WHERE j.user_id = s.user_id
                  AND j.session_id = s.id
                  AND j.source_hash = s.source_hash
                  AND j.compiler_fingerprint = ?
                  AND (
                      j.status IN ('complete', 'partial')
                      OR (j.status = 'running' AND j.updated_at >= ?)
                  )
            )
            ORDER BY COALESCE(s.occurred_at, s.created_at), s.id
            """,
            parameters,
        ).fetchall()
        materialized = 0
        for row in rows:
            session_pk = int(row[0])
            if exclude_session_pk is not None and session_pk == exclude_session_pk:
                continue
            applied = self._compile_stored_session(
                engine,
                {"session_pk": session_pk, "source_hash": str(row[1])},
            )
            materialized += int(applied)
        return materialized

    def _finalization_observability(
        self,
        engine: Engine,
        *,
        session_id: str | None,
    ) -> dict[str, int]:
        """Summarize active-fingerprint jobs for each current source hash."""

        assert self._compiler is not None
        clauses = ["s.user_id = ?"]
        parameters: list = [self._compiler.fingerprint, engine.user_id]
        if session_id is not None:
            clauses.append("s.external_id = ?")
            parameters.append(session_id)
        rows = engine._conn.execute(
            f"""
            SELECT j.status
            FROM memory_sessions s
            LEFT JOIN memory_compiler_jobs j
              ON j.user_id = s.user_id
             AND j.session_id = s.id
             AND j.source_hash = s.source_hash
             AND j.compiler_fingerprint = ?
            WHERE {" AND ".join(clauses)}
            ORDER BY s.id
            """,
            parameters,
        ).fetchall()
        statuses = [str(row[0]) if row[0] is not None else "missing" for row in rows]
        return {
            "matched_sessions": len(statuses),
            "complete_sessions": statuses.count("complete"),
            "partial_sessions": statuses.count("partial"),
            "in_progress_sessions": sum(
                status in {"missing", "pending", "failed", "running"}
                for status in statuses
            ),
        }

    def finalize(self, user_id: str, session_id: str | None = None) -> dict:
        """Synchronously materialize dirty sessions without receiving a query."""

        self._reject_existing_derived_mutation("finalize")
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required")
        normalized_session_id = None
        if session_id is not None:
            normalized_session_id = str(session_id).strip()
            if not normalized_session_id:
                raise ValueError("session_id cannot be empty")

        started = time.perf_counter()
        engine, engine_lock = self.engine_and_lock(normalized_user_id)
        with engine_lock:
            finalized_sessions = 0
            observability = {
                "matched_sessions": 0,
                "complete_sessions": 0,
                "partial_sessions": 0,
                "in_progress_sessions": 0,
            }
            if self.mode is MemoryMode.INTELLIGENCE:
                finalized_sessions = self._compile_registered_sessions(
                    engine,
                    session_id=normalized_session_id,
                )
                observability = self._finalization_observability(
                    engine,
                    session_id=normalized_session_id,
                )
        status = "complete"
        if normalized_session_id is not None and not observability["matched_sessions"]:
            status = "not_found"
        elif observability["in_progress_sessions"]:
            status = "in_progress"
        return {
            "status": status,
            "user_id": normalized_user_id,
            "session_id": normalized_session_id,
            "finalized_sessions": finalized_sessions,
            **observability,
            "finalization_ms": (time.perf_counter() - started) * 1000,
        }

    def search(self, payload: dict) -> dict:
        user_id = str(payload.get("user_id") or "").strip()
        query = str(payload.get("query") or "").strip()
        limit = max(1, int(payload.get("limit") or 200))
        if not user_id or not query:
            raise ValueError("user_id and query are required")
        started = time.perf_counter()
        finalization_ms = 0.0
        lazy_finalized_sessions = 0
        engine, engine_lock = self.engine_and_lock(user_id)
        with engine_lock:
            if self.mode is MemoryMode.INTELLIGENCE:
                replay_diagnostics = None
                if self.existing_derived_fingerprint is not None:
                    replay_diagnostics = self._existing_derived_diagnostics(engine)
                    if not replay_diagnostics["ready"]:
                        raise ValueError(
                            "existing-derived replay scope is not ready: "
                            f"registered={replay_diagnostics['registered_sessions']}, "
                            f"terminal={replay_diagnostics['terminal_sessions']}, "
                            f"nonterminal={replay_diagnostics['nonterminal_sessions']}"
                        )
                else:
                    finalization_started = time.perf_counter()
                    lazy_finalized_sessions = self._compile_registered_sessions(engine)
                    finalization_ms = (
                        time.perf_counter() - finalization_started
                    ) * 1000
                ranked = engine.search_memory_blocks(
                    query,
                    limit=limit,
                    include_derived=True,
                    max_chars=self.merge_max_chars,
                )
                message_ids = {
                    message_id
                    for block in ranked.blocks
                    for message_id in block.message_ids
                }
                timestamps: dict[int, float] = {}
                if message_ids:
                    placeholders = ",".join("?" for _ in message_ids)
                    rows = engine._conn.execute(
                        f"SELECT id, timestamp FROM messages WHERE user_id = ? "
                        f"AND id IN ({placeholders})",
                        [user_id, *sorted(message_ids)],
                    ).fetchall()
                    timestamps = {int(row[0]): float(row[1]) for row in rows}
                now = time.time()
                entries = [
                    {
                        "id": (
                            block.composite_id
                            if block.composite_id is not None
                            else f"claim:{block.claim_id}"
                            if block.claim_id is not None
                            else f"message:{block.message_ids[0]}"
                            if block.message_ids
                            else f"context:{index}"
                        ),
                        "memory": block.text,
                        "timestamp": max(
                            (
                                timestamps.get(message_id, now)
                                for message_id in block.message_ids
                            ),
                            default=now,
                        ),
                    }
                    for index, block in enumerate(ranked.blocks)
                ]
                query_ms = ranked.query_ms
                total_matches = ranked.total_matches
                timings_ms = ranked.timings_ms
                engine_scores = [block.score for block in ranked.blocks]
            else:
                replay_diagnostics = None
                result = engine.search(
                    query,
                    limit=limit,
                    max_context=limit,
                    full_context_threshold=0,
                )
                hits = result.direct_hits[:limit]
                if self.merge_adjacent:
                    entries = merge_adjacent_hits(
                        hits, gap=self.merge_gap, max_chars=self.merge_max_chars
                    )[:limit]
                else:
                    entries = [
                        {"id": str(m.id), "memory": m.text, "timestamp": m.timestamp}
                        for m in hits
                    ]
                query_ms = result.query_ms
                total_matches = result.total_matches
                timings_ms = result.timings_ms
                engine_scores = result.scores[: len(hits)]
        denominator = max(len(entries), 1)
        return {
            "results": [
                {
                    "id": entry["id"],
                    "memory": entry["memory"],
                    "score": 1.0 - (index / denominator),
                    "created_at": datetime.fromtimestamp(
                        entry["timestamp"], tz=timezone.utc
                    ).isoformat(),
                }
                for index, entry in enumerate(entries)
            ],
            "query_debug": {
                "engine": "NarratorDB",
                "mode": self.mode.value,
                **({"coalesce_sessions": True} if self.coalesce_sessions else {}),
                **(
                    {
                        "render_token_budget": self.context_token_budget,
                        "top_k_budget_independent": True,
                    }
                    if self.mode is MemoryMode.INTELLIGENCE
                    else {}
                ),
                **(
                    {"existing_derived_replay": replay_diagnostics}
                    if replay_diagnostics is not None
                    else {}
                ),
                "query_ms": query_ms,
                "finalization_ms": finalization_ms,
                "lazy_finalized_sessions": lazy_finalized_sessions,
                "total_matches": total_matches,
                "timings_ms": timings_ms,
                "engine_scores": engine_scores,
                "backend_ms": (time.perf_counter() - started) * 1000,
            },
        }

    def delete(self, user_id: str) -> dict:
        self._reject_existing_derived_mutation("delete")
        engine, engine_lock = self.engine_and_lock(user_id)
        with engine_lock:
            deleted = engine.clear_scope()
            if self._compiler_cache is not None:
                # Cache keys deliberately omit user IDs, so scoped source
                # deletion requires project-wide invalidation.
                self._compiler_cache.clear()
        return {"deleted": deleted}

    def close(self) -> None:
        with self._registry_lock:
            entries = [
                (engine, self._engine_locks[user_id])
                for user_id, engine in self._engines.items()
            ]
            self._engines.clear()
            self._engine_locks.clear()
        for engine, engine_lock in entries:
            with engine_lock:
                engine.close()
        if self._compiler_cache is not None:
            self._compiler_cache.close()
            self._compiler_cache = None


def make_handler(backend: NarratorDBBenchmarkBackend):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NarratorDBBenchmark/1.0"

        def log_message(self, fmt: str, *args) -> None:
            return

        def send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            return payload

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "engine": "NarratorDB",
                        "mode": backend.mode.value,
                        "existing_derived_replay": (
                            {
                                "enabled": True,
                                "compiler_fingerprint": (
                                    backend.existing_derived_fingerprint
                                ),
                                "compiler_constructed": False,
                                "compiler_cache_constructed": False,
                                "mutations_allowed": False,
                            }
                            if backend.existing_derived_fingerprint is not None
                            else None
                        ),
                    },
                )
            elif parsed.path == "/replay/diagnostics":
                if backend.existing_derived_fingerprint is None:
                    self.send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "existing-derived replay is not enabled"},
                    )
                    return
                user_id = (parse_qs(parsed.query).get("user_id") or [""])[0]
                try:
                    result = backend.existing_derived_diagnostics(user_id)
                except ValueError as error:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                self.send_json(HTTPStatus.OK, result)
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                path = urlparse(self.path).path
                payload = self.read_json()
                if path == "/memories":
                    result = backend.add(payload)
                elif path == "/memories/finalize":
                    unexpected = set(payload) - {"user_id", "session_id"}
                    if unexpected:
                        raise ValueError(
                            "memory finalization accepts only user_id and session_id"
                        )
                    result = backend.finalize(
                        str(payload.get("user_id") or ""),
                        payload.get("session_id"),
                    )
                elif path == "/search":
                    result = backend.search(payload)
                else:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                self.send_json(HTTPStatus.OK, result)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception as error:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/memories":
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            user_id = (parse_qs(parsed.query).get("user_id") or [""])[0]
            if not user_id:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "user_id is required"})
                return
            self.send_json(HTTPStatus.OK, backend.delete(user_id))

    return Handler


def _provider_allowlist(value: str) -> tuple[str, ...]:
    providers = tuple(item.strip() for item in value.split(",") if item.strip())
    if not providers:
        raise argparse.ArgumentTypeError("must contain at least one provider")
    if len({provider.casefold() for provider in providers}) != len(providers):
        raise argparse.ArgumentTypeError("providers must be unique")
    return providers


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8889)
    parser.add_argument("--database", type=Path)
    parser.add_argument(
        "--merge-adjacent",
        dest="merge_adjacent",
        action="store_true",
        default=True,
        help="Merge position-adjacent same-session hits into one memory block (default)",
    )
    parser.add_argument(
        "--no-merge-adjacent",
        dest="merge_adjacent",
        action="store_false",
        help="Return one memory per stored message (pre-1.3 behavior)",
    )
    parser.add_argument("--merge-gap", type=int, default=1)
    parser.add_argument("--merge-max-chars", type=int, default=1200)
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in MemoryMode],
        default=MemoryMode.PRIVATE.value,
        help="private raw retrieval (default) or synchronous intelligence compilation",
    )
    parser.add_argument(
        "--coalesce-timestamp-sessions",
        "--coalesce-sessions",
        dest="coalesce_sessions",
        action="store_true",
        help=(
            "in intelligence mode, combine pair-level adds sharing an explicit "
            "session ID or timestamp and compile at session boundaries/search"
        ),
    )
    parser.add_argument(
        "--compiler",
        choices=[kind.value for kind in CompilerKind],
        help="required in intelligence mode; credentials come only from the environment",
    )
    parser.add_argument("--model", help="compiler model slug or Codex model name")
    parser.add_argument("--endpoint", help="local loopback OpenAI-compatible endpoint")
    provider_group = parser.add_mutually_exclusive_group()
    provider_group.add_argument(
        "--provider", help="pinned OpenRouter provider (model-specific default: Azure)"
    )
    provider_group.add_argument(
        "--provider-allow",
        type=_provider_allowlist,
        help="ordered OpenRouter endpoint-slug allowlist with contained fallbacks",
    )
    parser.add_argument(
        "--reasoning",
        help=(
            "hosted or Codex CLI reasoning effort "
            "(official OpenAI/Codex GPT-5.4 Mini: low; OpenRouter: minimal)"
        ),
    )
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument(
        "--output-token-parameter",
        choices=sorted(SUPPORTED_OUTPUT_TOKEN_PARAMETERS),
        default=DEFAULT_OUTPUT_TOKEN_PARAMETER,
        help="OpenAI-compatible output limit field accepted by the selected route",
    )
    parser.add_argument(
        "--context-token-budget",
        type=int,
        default=DEFAULT_INTELLIGENCE_TOKEN_BUDGET,
    )
    parser.add_argument(
        "--compiler-max-cost-usd",
        type=float,
        help="required cumulative local spend fuse for hosted benchmark runs",
    )
    parser.add_argument("--compiler-semantic-max-attempts", type=int)
    parser.add_argument("--compiler-transport-max-attempts", type=int)
    parser.add_argument("--compiler-retry-delay-seconds", type=float)
    parser.add_argument("--compiler-min-request-interval-seconds", type=float)
    parser.add_argument(
        "--compiler-capture-router-metadata",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="request content-free OpenRouter route-attempt metadata (hosted default: on)",
    )
    parser.add_argument(
        "--compiler-request-reservation-usd",
        type=float,
        help="per-request atomic budget reservation (hosted default: 0.05 USD)",
    )
    parser.add_argument(
        "--compiler-budget-safety-reserve-usd",
        type=float,
        help="unspendable local headroom below the cap (hosted default: 1 USD)",
    )
    parser.add_argument(
        "--codex-executable",
        help="runtime-only Codex CLI executable path or command (default: codex)",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="runtime-only isolated CODEX_HOME containing ChatGPT authentication",
    )
    parser.add_argument(
        "--codex-cli-version",
        help="required Codex CLI version identity to persist and verify",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=float,
        help="hard timeout for each Codex CLI invocation (default: 300)",
    )
    parser.add_argument(
        "--codex-max-invocations",
        type=int,
        help="optional aggregate invocation fuse for the Codex compiler",
    )
    parser.add_argument(
        "--codex-max-concurrency",
        type=int,
        help="maximum concurrent Codex CLI processes (default: 1)",
    )
    parser.add_argument(
        "--existing-derived-replay-fingerprint",
        help=(
            "read-only intelligence replay over already-materialized derived memory; "
            "requires the exact recorded compiler fingerprint and constructs no compiler"
        ),
    )
    return parser


def _compiler_config_from_args(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> CompilerConfig | None:
    mode = normalize_mode(args.mode)
    replay_fingerprint = (
        str(args.existing_derived_replay_fingerprint).strip()
        if args.existing_derived_replay_fingerprint is not None
        else None
    )
    if args.existing_derived_replay_fingerprint is not None and not replay_fingerprint:
        parser.error("--existing-derived-replay-fingerprint cannot be empty")
    if mode is MemoryMode.PRIVATE:
        if (
            args.compiler
            or args.model
            or args.endpoint
            or args.provider
            or args.provider_allow
            or args.reasoning
            or args.coalesce_sessions
            or replay_fingerprint is not None
            or args.compiler_max_cost_usd is not None
            or args.compiler_semantic_max_attempts is not None
            or args.compiler_transport_max_attempts is not None
            or args.compiler_retry_delay_seconds is not None
            or args.compiler_min_request_interval_seconds is not None
            or args.compiler_capture_router_metadata is not None
            or args.compiler_request_reservation_usd is not None
            or args.compiler_budget_safety_reserve_usd is not None
            or args.codex_executable is not None
            or args.codex_home is not None
            or args.codex_cli_version is not None
            or args.codex_timeout_seconds is not None
            or args.codex_max_invocations is not None
            or args.codex_max_concurrency is not None
        ):
            parser.error(
                "compiler and session-coalescing options require --mode intelligence"
            )
        return None
    if replay_fingerprint is not None:
        if (
            any(
                value is not None
                for value in (
                    args.compiler,
                    args.model,
                    args.endpoint,
                    args.provider,
                    args.provider_allow,
                    args.reasoning,
                    args.compiler_max_cost_usd,
                    args.compiler_semantic_max_attempts,
                    args.compiler_transport_max_attempts,
                    args.compiler_retry_delay_seconds,
                    args.compiler_min_request_interval_seconds,
                    args.compiler_request_reservation_usd,
                    args.compiler_budget_safety_reserve_usd,
                    args.codex_executable,
                    args.codex_home,
                    args.codex_cli_version,
                    args.codex_timeout_seconds,
                    args.codex_max_invocations,
                    args.codex_max_concurrency,
                )
            )
            or args.compiler_capture_router_metadata is not None
        ):
            parser.error(
                "--existing-derived-replay-fingerprint cannot be combined with "
                "compiler, route, or compiler-cost options"
            )
        if args.context_token_budget < 128:
            parser.error("--context-token-budget must be at least 128")
        return None
    if not args.compiler:
        parser.error("--compiler is required with --mode intelligence")
    if args.context_token_budget < 128:
        parser.error("--context-token-budget must be at least 128")
    if args.compiler_max_cost_usd is not None and (
        not math.isfinite(args.compiler_max_cost_usd) or args.compiler_max_cost_usd <= 0
    ):
        parser.error("--compiler-max-cost-usd must be a positive finite number")
    for option, value in (
        ("--compiler-semantic-max-attempts", args.compiler_semantic_max_attempts),
        ("--compiler-transport-max-attempts", args.compiler_transport_max_attempts),
        ("--codex-max-invocations", args.codex_max_invocations),
        ("--codex-max-concurrency", args.codex_max_concurrency),
    ):
        if value is not None and value < 1:
            parser.error(f"{option} must be positive")
    for option, value in (
        ("--compiler-retry-delay-seconds", args.compiler_retry_delay_seconds),
        (
            "--compiler-min-request-interval-seconds",
            args.compiler_min_request_interval_seconds,
        ),
        (
            "--compiler-request-reservation-usd",
            args.compiler_request_reservation_usd,
        ),
        (
            "--compiler-budget-safety-reserve-usd",
            args.compiler_budget_safety_reserve_usd,
        ),
    ):
        if value is not None and (not math.isfinite(value) or value < 0):
            parser.error(f"{option} must be a non-negative finite number")
    if args.codex_timeout_seconds is not None and (
        not math.isfinite(args.codex_timeout_seconds)
        or args.codex_timeout_seconds <= 0
    ):
        parser.error("--codex-timeout-seconds must be a positive finite number")
    if args.codex_executable is not None and not args.codex_executable.strip():
        parser.error("--codex-executable cannot be empty")
    if args.codex_cli_version is not None and not args.codex_cli_version.strip():
        parser.error("--codex-cli-version cannot be empty")
    if args.provider_allow and len(
        {provider.casefold() for provider in args.provider_allow}
    ) != len(args.provider_allow):
        parser.error("--provider-allow entries must be unique")
    if args.compiler == CompilerKind.LOCAL.value:
        if not args.model or not args.endpoint:
            parser.error("the local compiler requires --model and --endpoint")
        if (
            args.provider
            or args.provider_allow
            or args.reasoning
            or args.compiler_capture_router_metadata is not None
        ):
            parser.error("OpenRouter provider options require --compiler openrouter")
        if any(
            value is not None
            for value in (
                args.compiler_max_cost_usd,
                args.compiler_request_reservation_usd,
                args.compiler_budget_safety_reserve_usd,
            )
        ):
            parser.error("compiler USD options require --compiler openrouter")
        if any(
            value is not None
            for value in (
                args.codex_executable,
                args.codex_home,
                args.codex_cli_version,
                args.codex_timeout_seconds,
                args.codex_max_invocations,
                args.codex_max_concurrency,
            )
        ):
            parser.error("Codex CLI options require --compiler codex-cli")
        return CompilerConfig.local(
            model=args.model,
            endpoint=args.endpoint,
            max_output_tokens=args.max_output_tokens,
            output_token_parameter=args.output_token_parameter,
            semantic_max_attempts=args.compiler_semantic_max_attempts,
            transport_max_attempts=args.compiler_transport_max_attempts,
            retry_delay_seconds=args.compiler_retry_delay_seconds,
            min_request_interval_seconds=(
                args.compiler_min_request_interval_seconds or 0.0
            ),
        )
    if args.compiler == CompilerKind.CODEX_CLI.value:
        if args.endpoint:
            parser.error("--endpoint is valid only with --compiler local")
        if (
            args.provider
            or args.provider_allow
            or args.compiler_capture_router_metadata is not None
        ):
            parser.error("OpenRouter provider options require --compiler openrouter")
        if any(
            value is not None
            for value in (
                args.compiler_max_cost_usd,
                args.compiler_request_reservation_usd,
                args.compiler_budget_safety_reserve_usd,
            )
        ):
            parser.error("compiler USD options require --compiler openrouter")
        if args.compiler_transport_max_attempts is not None:
            parser.error(
                "--compiler-transport-max-attempts is invalid with "
                "--compiler codex-cli"
            )
        if args.max_output_tokens != 8192:
            parser.error(
                "--max-output-tokens is invalid with --compiler codex-cli"
            )
        if args.output_token_parameter != DEFAULT_OUTPUT_TOKEN_PARAMETER:
            parser.error(
                "--output-token-parameter is invalid with --compiler codex-cli"
            )
        return CompilerConfig.codex_cli(
            model=args.model or DEFAULT_CODEX_CLI_MODEL,
            reasoning=args.reasoning or "low",
            cli_version=args.codex_cli_version,
            timeout_seconds=(
                args.codex_timeout_seconds
                if args.codex_timeout_seconds is not None
                else 300.0
            ),
            max_invocations=args.codex_max_invocations,
            max_concurrency=(
                args.codex_max_concurrency
                if args.codex_max_concurrency is not None
                else 1
            ),
            semantic_max_attempts=args.compiler_semantic_max_attempts or 2,
            retry_delay_seconds=(
                args.compiler_retry_delay_seconds
                if args.compiler_retry_delay_seconds is not None
                else 0.25
            ),
            min_request_interval_seconds=(
                args.compiler_min_request_interval_seconds
                if args.compiler_min_request_interval_seconds is not None
                else 0.0
            ),
        )
    if args.compiler == CompilerKind.OPENAI.value:
        if args.endpoint:
            parser.error("--endpoint is valid only with --compiler local")
        if (
            args.provider
            or args.provider_allow
            or args.compiler_capture_router_metadata is not None
        ):
            parser.error("OpenRouter provider options require --compiler openrouter")
        if any(
            value is not None
            for value in (
                args.codex_executable,
                args.codex_home,
                args.codex_cli_version,
                args.codex_timeout_seconds,
                args.codex_max_invocations,
                args.codex_max_concurrency,
            )
        ):
            parser.error("Codex CLI options require --compiler codex-cli")
        if args.compiler_max_cost_usd is None:
            parser.error("--compiler-max-cost-usd is required with --compiler openai")
        return CompilerConfig.openai(
            model=args.model or DEFAULT_OPENAI_MODEL,
            reasoning=args.reasoning or "low",
            max_output_tokens=args.max_output_tokens,
            output_token_parameter=args.output_token_parameter,
            semantic_max_attempts=args.compiler_semantic_max_attempts or 2,
            transport_max_attempts=args.compiler_transport_max_attempts or 2,
            retry_delay_seconds=(
                args.compiler_retry_delay_seconds
                if args.compiler_retry_delay_seconds is not None
                else 0.25
            ),
            min_request_interval_seconds=(
                args.compiler_min_request_interval_seconds
                if args.compiler_min_request_interval_seconds is not None
                else 0.0
            ),
        )
    if args.endpoint:
        parser.error("--endpoint is valid only with --compiler local")
    if any(
        value is not None
        for value in (
            args.codex_executable,
            args.codex_home,
            args.codex_cli_version,
            args.codex_timeout_seconds,
            args.codex_max_invocations,
            args.codex_max_concurrency,
        )
    ):
        parser.error("Codex CLI options require --compiler codex-cli")
    if args.compiler_max_cost_usd is None:
        parser.error("--compiler-max-cost-usd is required with --compiler openrouter")
    return CompilerConfig.openrouter(
        model=args.model or DEFAULT_OPENROUTER_MODEL,
        provider=args.provider,
        provider_allowlist=args.provider_allow or (),
        allow_fallbacks=bool(args.provider_allow),
        reasoning=args.reasoning,
        max_output_tokens=args.max_output_tokens,
        output_token_parameter=args.output_token_parameter,
        semantic_max_attempts=args.compiler_semantic_max_attempts or 2,
        transport_max_attempts=args.compiler_transport_max_attempts or 1,
        retry_delay_seconds=(
            args.compiler_retry_delay_seconds
            if args.compiler_retry_delay_seconds is not None
            else 0.25
        ),
        min_request_interval_seconds=(
            args.compiler_min_request_interval_seconds
            if args.compiler_min_request_interval_seconds is not None
            else 10.0
        ),
        capture_router_metadata=(
            True
            if args.compiler_capture_router_metadata is None
            else args.compiler_capture_router_metadata
        ),
    )


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    compiler_config = _compiler_config_from_args(args, parser)

    temporary = None
    if args.database:
        database = str(args.database.expanduser().resolve())
    else:
        temporary = tempfile.TemporaryDirectory(prefix="narratordb_official_bench_")
        database = str(Path(temporary.name) / "memory.db")

    usage_ledger = None
    if compiler_config is not None:
        metered_hosted = compiler_config.kind in {
            CompilerKind.OPENAI,
            CompilerKind.OPENROUTER,
        }
        usage_ledger = ContentFreeUsageLedger(
            Path(f"{database}.compiler-usage.jsonl"),
            max_cost_usd=(
                args.compiler_max_cost_usd if metered_hosted else None
            ),
            request_reservation_usd=(
                args.compiler_request_reservation_usd
                if args.compiler_request_reservation_usd is not None
                else (0.05 if metered_hosted else 0.0)
            ),
            safety_reserve_usd=(
                args.compiler_budget_safety_reserve_usd
                if args.compiler_budget_safety_reserve_usd is not None
                else (1.0 if metered_hosted else 0.0)
            ),
        )
    backend = NarratorDBBenchmarkBackend(
        database,
        merge_adjacent=args.merge_adjacent,
        merge_gap=args.merge_gap,
        merge_max_chars=args.merge_max_chars,
        mode=args.mode,
        compiler_config=compiler_config,
        compiler_usage_sink=usage_ledger,
        codex_executable=args.codex_executable or "codex",
        codex_home=(
            args.codex_home.expanduser().resolve()
            if args.codex_home is not None
            else None
        ),
        context_token_budget=args.context_token_budget,
        coalesce_sessions=args.coalesce_sessions,
        existing_derived_fingerprint=args.existing_derived_replay_fingerprint,
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(backend))
    print(
        json.dumps(
            {
                "ok": True,
                "engine": "NarratorDB",
                "url": f"http://{args.host}:{args.port}",
                "database": database,
                "mode": args.mode,
                **({"coalesce_sessions": True} if args.coalesce_sessions else {}),
                "compiler": compiler_config.to_dict() if compiler_config else None,
                "existing_derived_replay": (
                    {
                        "enabled": True,
                        "compiler_fingerprint": args.existing_derived_replay_fingerprint,
                        "compiler_constructed": False,
                        "compiler_cache_constructed": False,
                        "mutations_allowed": False,
                    }
                    if args.existing_derived_replay_fingerprint is not None
                    else None
                ),
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        backend.close()
        if temporary:
            temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
