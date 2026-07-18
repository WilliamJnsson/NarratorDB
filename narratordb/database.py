"""NarratorDB synchronous Python API.

Canonical direct library interface for the real SQLite/FTS5 engine.
"""

from __future__ import annotations

import getpass
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from .config import (
    CapturePolicy,
    CompilerConfig,
    ConfigurationError,
    FeatureUnavailableError,
    MemoryMode,
    ProjectConfig,
    ProjectConfigStore,
    default_data_dir,
    default_db_path,
    default_user_id,
    normalize_mode,
    normalize_capture_policy,
)
from .engine import ENGINE_NAME, Engine
from .intelligence import ContextBundle


@dataclass
class RecallResult:
    text: str
    facts: list[dict] = field(default_factory=list)
    fact_count: int = 0
    recall_ms: float = 0.0
    cache_hit: bool = False
    sources: dict = field(default_factory=dict)
    entities: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)


@dataclass
class IngestResult:
    facts_extracted: int
    facts_stored: int
    entities: list[str]
    ingest_ms: float
    duplicate: bool = False
    facts: list[dict] = field(default_factory=list)
    memory_types: dict = field(default_factory=dict)
    message_id: Optional[int] = None


@dataclass
class SessionIngestResult:
    session_id: str
    session_pk: int
    message_ids: list[int]
    stored_message_ids: list[int]
    messages_stored: int
    source_hash: str
    ingest_ms: float
    compiler_job_id: int | None = None
    enrichment_status: str = "disabled"
    enrichment: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArtifactResult:
    artifact_id: Optional[int]
    stored: bool
    ingest_ms: float


@dataclass
class ArtifactRecallResult:
    artifacts: list[dict] = field(default_factory=list)
    query_ms: float = 0.0
    total_matches: int = 0


@dataclass
class CodeChunkResult:
    chunk_id: Optional[int]
    stored: bool
    ingest_ms: float


@dataclass
class CodeChunkRecallResult:
    chunks: list[dict] = field(default_factory=list)
    query_ms: float = 0.0
    total_matches: int = 0


@dataclass
class RelationResult:
    relation_id: Optional[int]
    stored: bool
    ingest_ms: float


class NarratorDB:
    """Canonical synchronous interface for NarratorDB."""

    def __init__(
        self,
        data_dir: str | None = None,
        user_id: str | None = None,
        workspace_id: str | None = None,
        db_path: str | None = None,
        context_window: int = 5,
        semantic_dedup: bool = True,
        dedup_threshold: float = 0.82,
        dedup_window: int = 100,
        mode: str | MemoryMode | None = None,
        compiler: CompilerConfig | None = None,
        capture_policy: str | CapturePolicy | None = None,
    ):
        self.data_dir = os.path.expanduser(data_dir or default_data_dir())
        self.db_path = os.path.expanduser(
            db_path
            or (
                os.path.join(self.data_dir, "memory.db")
                if data_dir
                else default_db_path()
            )
        )
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.context_window = context_window
        self.semantic_dedup = semantic_dedup
        self.dedup_threshold = dedup_threshold
        self.dedup_window = dedup_window
        self._engines: dict[str, Engine] = {}
        self._compiler_runtime = None
        self._compiler_usage_ledger = None
        self._compiler_cache = None

        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        self._config_store = ProjectConfigStore(self.db_path)
        self.project_config = self._config_store.resolve(
            mode=mode,
            compiler=compiler,
            capture_policy=capture_policy,
        )

    @property
    def mode(self) -> MemoryMode:
        """The explicitly selected memory mode for this database."""

        return self.project_config.mode

    @property
    def compiler_config(self) -> CompilerConfig | None:
        """Return the persisted, credential-free compiler selection."""

        return self.project_config.compiler

    @property
    def capture_policy(self) -> CapturePolicy:
        """Return the persisted lifecycle auto-capture policy."""

        return self.project_config.capture_policy

    def _resolve_user_id(self, user_id: str | None = None) -> str:
        return user_id or self.user_id or default_user_id(getpass.getuser())

    def _resolve_workspace_id(self, workspace_id: str | None = None) -> str | None:
        return workspace_id if workspace_id is not None else self.workspace_id

    def _scope_key(
        self, user_id: str | None = None, workspace_id: str | None = None
    ) -> str:
        uid = self._resolve_user_id(user_id)
        wid = self._resolve_workspace_id(workspace_id)
        return f"{uid}::workspace::{wid}" if wid else uid

    def _get_engine(
        self, user_id: str | None = None, workspace_id: str | None = None
    ) -> Engine:
        scope_key = self._scope_key(user_id, workspace_id)
        engine = self._engines.get(scope_key)
        if engine is None:
            engine = Engine(
                db_path=self.db_path,
                user_id=scope_key,
                context_window=self.context_window,
                semantic_dedup=self.semantic_dedup,
                dedup_threshold=self.dedup_threshold,
                dedup_window=self.dedup_window,
                semantic_search_mode=(
                    "hybrid"
                    if self.mode is MemoryMode.INTELLIGENCE
                    else "fallback_only"
                ),
                # Embeddings are an optional local optimization in both
                # modes. A mode choice must never trigger a model download.
                local_only=True,
            )
            self._engines[scope_key] = engine
        return engine

    def remember(
        self,
        text: str,
        user_id: str | None = None,
        source: str = "user",
        workspace_id: str | None = None,
        provenance: dict | None = None,
    ) -> IngestResult:
        t0 = time.monotonic()
        engine = self._get_engine(user_id, workspace_id)
        msg_id = engine.remember(text=text, speaker=source, provenance=provenance)
        ingest_ms = (time.monotonic() - t0) * 1000
        stored = msg_id is not None
        return IngestResult(
            facts_extracted=1 if stored else 0,
            facts_stored=1 if stored else 0,
            entities=[],
            ingest_ms=ingest_ms,
            duplicate=not stored,
            facts=[],
            memory_types={"raw_message": 1 if stored else 0},
            message_id=msg_id,
        )

    def remember_automatic(
        self,
        text: str,
        *,
        memory_key: str,
        memory_value: str,
        rule_id: str,
        user_id: str | None = None,
        source: str = "user",
        workspace_id: str | None = None,
        provenance: dict | None = None,
    ) -> IngestResult:
        """Upsert one typed automatic memory without replacing explicit writes."""

        engine = self._get_engine(user_id, workspace_id)
        existing = engine.automatic_memory(memory_key)
        if (
            existing is not None
            and existing["memory_value"].casefold() == str(memory_value).casefold()
        ):
            return IngestResult(
                facts_extracted=0,
                facts_stored=0,
                entities=[],
                ingest_ms=0.0,
                duplicate=True,
                memory_types={"automatic_memory": 0},
                message_id=int(existing["message_id"]),
            )

        exact = engine.exact_message(text, speaker=source)
        if exact is not None:
            exact_message_id = int(exact["message_id"])
            if exact["memory_key"] == memory_key:
                replaced_message_id = engine.record_automatic_memory(
                    memory_key=memory_key,
                    memory_value=memory_value,
                    message_id=exact_message_id,
                    rule_id=rule_id,
                )
                if (
                    replaced_message_id is not None
                    and replaced_message_id != exact_message_id
                ):
                    engine.forget(replaced_message_id)
            elif (
                existing is not None and int(existing["message_id"]) != exact_message_id
            ):
                # The corrected value was already saved explicitly. Remove
                # only the stale automatic evidence and leave the explicit
                # memory as the authoritative record.
                engine.forget(int(existing["message_id"]))
            return IngestResult(
                facts_extracted=0,
                facts_stored=0,
                entities=[],
                ingest_ms=0.0,
                duplicate=True,
                memory_types={"automatic_memory": 0},
                message_id=exact_message_id,
            )

        t0 = time.monotonic()
        message_id = engine.remember(
            text=text,
            speaker=source,
            provenance=provenance,
            # A corrected value often differs by only one noun. Exact dedup
            # still applies, but semantic similarity must not suppress it.
            semantic_dedup=False,
        )
        if message_id is None:
            return IngestResult(
                facts_extracted=0,
                facts_stored=0,
                entities=[],
                ingest_ms=(time.monotonic() - t0) * 1000,
                duplicate=True,
                memory_types={"automatic_memory": 0},
                message_id=None,
            )
        replaced_message_id = engine.record_automatic_memory(
            memory_key=memory_key,
            memory_value=memory_value,
            message_id=message_id,
            rule_id=rule_id,
        )
        if replaced_message_id is not None and replaced_message_id != message_id:
            engine.forget(replaced_message_id)
        return IngestResult(
            facts_extracted=1,
            facts_stored=1,
            entities=[],
            ingest_ms=(time.monotonic() - t0) * 1000,
            duplicate=False,
            memory_types={"automatic_memory": 1},
            message_id=message_id,
        )

    def ingest_session(
        self,
        messages: list[dict],
        *,
        session_id: str,
        occurred_at: float | None = None,
        user_id: str | None = None,
        workspace_id: str | None = None,
        metadata: dict | None = None,
        provenance: dict | None = None,
        wait_for_enrichment: bool = False,
    ) -> SessionIngestResult:
        """Commit a complete source session, then optionally queue compilation.

        Raw storage commits before any compiler work is attempted.  In private
        mode no job is created. Intelligence mode queues an idempotent job keyed
        by the source hash and credential-free compiler fingerprint.
        """

        started = time.monotonic()
        prepared_messages = []
        for message in messages:
            row = dict(message)
            if provenance:
                row["provenance"] = {**provenance, **dict(row.get("provenance") or {})}
            prepared_messages.append(row)

        engine = self._get_engine(user_id, workspace_id)
        stored = engine.store_session(
            prepared_messages,
            session_id=session_id,
            occurred_at=occurred_at,
            metadata=metadata,
        )
        job_id = None
        enrichment_status = "disabled"
        enrichment: dict[str, Any] = {}
        if self.mode is MemoryMode.INTELLIGENCE:
            if (
                self.compiler_config is None
            ):  # defensive: ProjectConfig rejects this state
                raise ConfigurationError(
                    "intelligence mode requires a configured compiler"
                )
            job_id = engine.enqueue_compilation(
                int(stored["session_pk"]),
                str(stored["source_hash"]),
                self._compiler_job_fingerprint(),
            )
            enrichment_status = "queued"
            enrichment = {"job_id": job_id}
            if wait_for_enrichment:
                processed = self.process_enrichment(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    limit=1,
                    job_id=job_id,
                )
                enrichment = processed
                enrichment_status = str(processed.get("status") or "processed")

        return SessionIngestResult(
            session_id=str(stored["session_id"]),
            session_pk=int(stored["session_pk"]),
            message_ids=[int(value) for value in stored["message_ids"]],
            stored_message_ids=[int(value) for value in stored["stored_message_ids"]],
            messages_stored=int(stored["stored"]),
            source_hash=str(stored["source_hash"]),
            ingest_ms=(time.monotonic() - started) * 1000,
            compiler_job_id=job_id,
            enrichment_status=enrichment_status,
            enrichment=enrichment,
        )

    def recall(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 5,
        workspace_id: str | None = None,
        max_context: int = 120,
        full_context_threshold: int = 0,
        filters: dict | None = None,
        profile: str = "default",
    ) -> RecallResult:
        engine = self._get_engine(user_id, workspace_id)
        result = engine.recall(
            query=query,
            limit=limit,
            max_context=max_context,
            full_context_threshold=full_context_threshold,
            filters=filters,
            profile=profile,
        )
        messages = [
            {
                "id": m.id,
                "speaker": m.speaker,
                "text": m.text,
                "timestamp": m.timestamp,
                "position": m.position,
                "provenance": m.provenance,
            }
            for m in result.messages
        ]
        text = "\n".join(f"{m['speaker']}: {m['text']}" for m in messages)
        return RecallResult(
            text=text,
            fact_count=result.total_matches,
            recall_ms=result.query_ms,
            cache_hit=False,
            sources={
                "sqlite_fts5": result.total_matches,
                "semantic": 1 if engine._sbert else 0,
            },
            messages=messages,
        )

    def recall_context(
        self,
        query: str,
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
        token_budget: int = 6000,
        filters: dict | None = None,
        profile: str = "default",
        explain: bool = False,
    ) -> ContextBundle:
        """Compose a bounded local context bundle with source citations."""

        engine = self._get_engine(user_id, workspace_id)
        bundle = engine.recall_context(
            query,
            token_budget=token_budget,
            filters=filters,
            profile=profile,
            explain=explain,
            include_derived=self.mode is MemoryMode.INTELLIGENCE,
        )
        bundle.mode = self.mode.value
        return bundle

    def search(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 10,
        workspace_id: str | None = None,
        max_context: int = 120,
        full_context_threshold: int = 0,
        filters: dict | None = None,
        profile: str = "default",
    ) -> list[dict]:
        engine = self._get_engine(user_id, workspace_id)
        result = engine.search(
            query=query,
            limit=limit,
            max_context=max_context,
            full_context_threshold=full_context_threshold,
            filters=filters,
            profile=profile,
        )
        return [
            {
                "id": m.id,
                "speaker": m.speaker,
                "content": m.text,
                "timestamp": m.timestamp,
                "position": m.position,
                "provenance": m.provenance,
            }
            for m in result.messages
        ]

    def store_artifact(
        self,
        kind: str,
        title: str,
        content: str,
        summary: str = "",
        tags: list[str] | None = None,
        user_id: str | None = None,
        workspace_id: str | None = None,
        provenance: dict | None = None,
    ) -> ArtifactResult:
        t0 = time.monotonic()
        engine = self._get_engine(user_id, workspace_id)
        artifact_id = engine.store_artifact(
            kind=kind,
            title=title,
            content=content,
            summary=summary,
            tags=tags,
            provenance=provenance,
        )
        return ArtifactResult(
            artifact_id=artifact_id,
            stored=artifact_id is not None,
            ingest_ms=(time.monotonic() - t0) * 1000,
        )

    def store_code_chunk(
        self,
        path: str,
        content: str,
        symbol: str = "",
        language: str = "",
        kind: str = "chunk",
        summary: str = "",
        start_line: int | None = None,
        end_line: int | None = None,
        tags: list[str] | None = None,
        user_id: str | None = None,
        workspace_id: str | None = None,
        provenance: dict | None = None,
    ) -> CodeChunkResult:
        t0 = time.monotonic()
        engine = self._get_engine(user_id, workspace_id)
        chunk_id = engine.store_code_chunk(
            path=path,
            content=content,
            symbol=symbol,
            language=language,
            kind=kind,
            summary=summary,
            start_line=start_line,
            end_line=end_line,
            tags=tags,
            provenance=provenance,
        )
        return CodeChunkResult(
            chunk_id=chunk_id,
            stored=chunk_id is not None,
            ingest_ms=(time.monotonic() - t0) * 1000,
        )

    def ingest_code_file(
        self,
        path: str,
        content: str,
        user_id: str | None = None,
        workspace_id: str | None = None,
        language: str = "",
        provenance: dict | None = None,
        chunk_lines: int = 120,
    ) -> IngestResult:
        t0 = time.monotonic()
        engine = self._get_engine(user_id, workspace_id)
        stored = engine.ingest_code_file(
            path=path,
            content=content,
            language=language,
            provenance=provenance,
            chunk_lines=chunk_lines,
        )
        ingest_ms = (time.monotonic() - t0) * 1000
        return IngestResult(
            facts_extracted=stored,
            facts_stored=stored,
            entities=[],
            ingest_ms=ingest_ms,
            duplicate=stored == 0,
            facts=[],
            memory_types={"code_chunk": stored},
            message_id=None,
        )

    def search_code(
        self,
        query: str,
        user_id: str | None = None,
        workspace_id: str | None = None,
        limit: int = 10,
        filters: dict | None = None,
        profile: str = "code",
    ) -> CodeChunkRecallResult:
        engine = self._get_engine(user_id, workspace_id)
        result = engine.search_code(
            query=query, limit=limit, filters=filters, profile=profile
        )
        return CodeChunkRecallResult(
            chunks=[
                {
                    "id": chunk.id,
                    "path": chunk.path,
                    "language": chunk.language,
                    "kind": chunk.kind,
                    "symbol": chunk.symbol,
                    "content": chunk.content,
                    "summary": chunk.summary,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "timestamp": chunk.timestamp,
                    "tags": chunk.tags,
                    "provenance": chunk.provenance,
                }
                for chunk in result.chunks
            ],
            query_ms=result.query_ms,
            total_matches=result.total_matches,
        )

    def search_artifacts(
        self,
        query: str,
        user_id: str | None = None,
        workspace_id: str | None = None,
        limit: int = 10,
        filters: dict | None = None,
    ) -> ArtifactRecallResult:
        engine = self._get_engine(user_id, workspace_id)
        result = engine.search_artifacts(query=query, limit=limit, filters=filters)
        return ArtifactRecallResult(
            artifacts=[
                {
                    "id": artifact.id,
                    "kind": artifact.kind,
                    "title": artifact.title,
                    "content": artifact.content,
                    "summary": artifact.summary,
                    "timestamp": artifact.timestamp,
                    "tags": artifact.tags,
                    "provenance": artifact.provenance,
                }
                for artifact in result.artifacts
            ],
            query_ms=result.query_ms,
            total_matches=result.total_matches,
        )

    def link_records(
        self,
        source_type: str,
        source_id: int,
        target_type: str,
        target_id: int,
        relation_type: str,
        user_id: str | None = None,
        workspace_id: str | None = None,
        weight: float = 1.0,
        metadata: dict | None = None,
    ) -> RelationResult:
        t0 = time.monotonic()
        engine = self._get_engine(user_id, workspace_id)
        relation_id = engine.link_records(
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            relation_type=relation_type,
            weight=weight,
            metadata=metadata,
        )
        return RelationResult(
            relation_id=relation_id,
            stored=relation_id is not None,
            ingest_ms=(time.monotonic() - t0) * 1000,
        )

    def related_records(
        self,
        record_type: str,
        record_id: int,
        user_id: str | None = None,
        workspace_id: str | None = None,
        relation_type: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        engine = self._get_engine(user_id, workspace_id)
        relations = engine.related_records(
            record_type=record_type,
            record_id=record_id,
            relation_type=relation_type,
            limit=limit,
        )
        return [
            {
                "id": relation.id,
                "source_type": relation.source_type,
                "source_id": relation.source_id,
                "target_type": relation.target_type,
                "target_id": relation.target_id,
                "relation_type": relation.relation_type,
                "weight": relation.weight,
                "timestamp": relation.timestamp,
                "metadata": relation.metadata,
            }
            for relation in relations
        ]

    def forget(
        self,
        user_id: str | None = None,
        workspace_id: str | None = None,
        message_id: int | None = None,
    ) -> int:
        engine = self._get_engine(user_id, workspace_id)
        if message_id is not None:
            deleted = 1 if engine.forget(message_id) else 0
            if deleted:
                # Cache entries are content-addressed across scopes, so a
                # source-level deletion must invalidate the complete cache.
                self._clear_compiler_cache()
            return deleted
        deleted = engine.clear_scope()
        self._clear_compiler_cache()
        scope_key = self._scope_key(user_id, workspace_id)
        if deleted:
            engine.close()
            self._engines.pop(scope_key, None)
        return deleted

    def stats(
        self, user_id: str | None = None, workspace_id: str | None = None
    ) -> dict:
        engine = self._get_engine(user_id, workspace_id)
        stats = engine.get_stats()
        stats["engine_name"] = ENGINE_NAME
        stats["db_path"] = self.db_path
        stats["memory_mode"] = self.mode.value
        stats["capture_policy"] = self.capture_policy.value
        stats["compiler"] = (
            self.compiler_config.to_dict() if self.compiler_config else None
        )
        stats["default_user_id"] = self._resolve_user_id(user_id)
        stats["workspace_id"] = self._resolve_workspace_id(workspace_id)
        return stats

    def message_counts(
        self, user_id: str | None = None, workspace_id: str | None = None
    ) -> dict[str, int]:
        """Return selected-scope, logical-user, and whole-database counts."""

        logical_user_id = self._resolve_user_id(user_id)
        selected_scope_key = self._scope_key(user_id, workspace_id)
        engine = self._get_engine(user_id, workspace_id)
        return engine.message_counts(
            logical_user_id=logical_user_id,
            selected_scope_key=selected_scope_key,
        )

    def project_status(
        self,
        user_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Return mode and enrichment state without exposing credentials."""

        enrichment: dict[str, Any] = {
            "available": False,
            "status": "disabled" if self.mode is MemoryMode.PRIVATE else "unavailable",
        }
        engine = self._get_engine(user_id, workspace_id)
        status_method = getattr(engine, "enrichment_status", None)
        if callable(status_method):
            reported = status_method()
            enrichment = (
                reported if isinstance(reported, dict) else {"status": str(reported)}
            )
            enrichment.setdefault("available", True)
            enrichment["enabled"] = self.mode is MemoryMode.INTELLIGENCE
            if self.mode is MemoryMode.INTELLIGENCE:
                enrichment.setdefault("status", "active")
            elif int(enrichment.get("claim_count") or 0) > 0:
                enrichment.setdefault("status", "retained_ignored")
            else:
                enrichment.setdefault("status", "disabled")
        return {
            **self.project_config.to_dict(),
            "db_path": self.db_path,
            "default_user_id": self._resolve_user_id(user_id),
            "workspace_id": self._resolve_workspace_id(workspace_id),
            "enrichment": enrichment,
        }

    status = project_status

    def set_mode(
        self,
        mode: str | MemoryMode,
        *,
        compiler: CompilerConfig | None = None,
        derived_data: Literal["retain", "purge"] | None = None,
    ) -> ProjectConfig:
        """Explicitly switch modes and persist the new project configuration.

        Leaving intelligence mode requires a deliberate retain/purge choice.
        A purge is completed before the mode changes, so a failed purge cannot
        leave the database claiming to be private while derived records remain.
        """

        target = normalize_mode(mode)
        current = self.project_config
        if target is current.mode:
            if compiler is None or compiler == current.compiler:
                return current
            if target is MemoryMode.PRIVATE:
                raise ConfigurationError(
                    "private mode cannot configure a memory compiler"
                )

        if target is MemoryMode.PRIVATE:
            if current.mode is MemoryMode.INTELLIGENCE:
                if derived_data not in {"retain", "purge"}:
                    raise ConfigurationError(
                        "switching to private mode requires derived_data='retain' or 'purge'"
                    )
                if derived_data == "purge":
                    self.purge_derived()
            compiler = None
        elif compiler is None:
            compiler = current.compiler
            if compiler is None:
                raise ConfigurationError(
                    "switching to intelligence mode requires a local or hosted compiler"
                )

        candidate = ProjectConfig(
            mode=target,
            compiler=compiler,
            capture_policy=current.capture_policy,
            created_at=current.created_at,
            migrated_from=current.migrated_from,
        )
        updated = self._config_store.save(candidate)
        self.project_config = updated

        active_fingerprint = (
            self._compiler_job_fingerprint_for(updated.compiler)
            if updated.mode is MemoryMode.INTELLIGENCE
            else None
        )
        if self.db_path == ":memory:":
            for engine in self._engines.values():
                engine.obsolete_compilations_except(active_fingerprint)
        else:
            engine = next(iter(self._engines.values()), None) or self._get_engine()
            engine.obsolete_compilations_except(active_fingerprint, all_scopes=True)

        self._compiler_runtime = None
        self._compiler_usage_ledger = None
        if self._compiler_cache is not None:
            self._compiler_cache.close()
            self._compiler_cache = None

        for engine in self._engines.values():
            engine.semantic_search_mode = (
                "hybrid" if updated.mode is MemoryMode.INTELLIGENCE else "fallback_only"
            )
            callback = getattr(engine, "on_project_config_changed", None)
            if callable(callback):
                callback(updated)
        return updated

    def set_capture_policy(self, policy: str | CapturePolicy) -> ProjectConfig:
        """Persist an explicit lifecycle auto-capture choice.

        The policy is independent of the memory mode: Private mode never gains
        a compiler, while Intelligence mode retains its configured compiler.
        """

        target = normalize_capture_policy(policy)
        current = self.project_config
        if target is current.capture_policy:
            return current
        updated = self._config_store.save(
            ProjectConfig(
                mode=current.mode,
                compiler=current.compiler,
                capture_policy=target,
                created_at=current.created_at,
                migrated_from=current.migrated_from,
            )
        )
        self.project_config = updated
        for engine in self._engines.values():
            callback = getattr(engine, "on_project_config_changed", None)
            if callable(callback):
                callback(updated)
        return updated

    def backfill(
        self,
        user_id: str | None = None,
        workspace_id: str | None = None,
        *,
        process: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Start or resume derived-memory backfill in intelligence mode."""

        if self.mode is not MemoryMode.INTELLIGENCE:
            raise ConfigurationError("backfill is available only in intelligence mode")
        engine = self._get_engine(user_id, workspace_id)
        method = getattr(engine, "backfill_derived", None) or getattr(
            engine, "backfill", None
        )
        if not callable(method):
            raise FeatureUnavailableError(
                "this installation does not provide a derived-memory backfill engine"
            )
        result = method(compiler_fingerprint=self._compiler_job_fingerprint())
        queued = result if isinstance(result, dict) else {"status": str(result)}
        if process:
            queued["processing"] = self.process_enrichment(
                user_id=user_id,
                workspace_id=workspace_id,
                limit=limit,
            )
        return queued

    def _get_compiler_runtime(self):
        if self.mode is not MemoryMode.INTELLIGENCE or self.compiler_config is None:
            raise ConfigurationError(
                "a compiler is available only in intelligence mode"
            )
        if self._compiler_runtime is not None:
            return self._compiler_runtime

        from .compiler import ContentFreeUsageLedger, compiler_from_project_config
        from .compiler_cache import CachedMemoryCompiler, CompiledSessionCache

        configured_cap = os.getenv("NARRATORDB_COMPILER_MAX_COST_USD")
        if configured_cap:
            try:
                max_cost_usd = float(configured_cap)
            except ValueError as error:
                raise ConfigurationError(
                    "NARRATORDB_COMPILER_MAX_COST_USD must be a positive number"
                ) from error
            if not math.isfinite(max_cost_usd) or max_cost_usd <= 0:
                raise ConfigurationError(
                    "NARRATORDB_COMPILER_MAX_COST_USD must be a positive finite number"
                )
        else:
            # Production projects do not inherit an experiment-specific spend
            # ceiling. Operators can set a per-process soft fuse explicitly with
            # NARRATORDB_COMPILER_MAX_COST_USD; benchmark runners keep their
            # own mandatory, disclosed fuse.
            max_cost_usd = None

        request_reservation_usd = 0.0
        safety_reserve_usd = 0.0
        if max_cost_usd is not None:
            budget_policy = (
                (
                    "NARRATORDB_COMPILER_REQUEST_RESERVATION_USD",
                    0.05,
                ),
                (
                    "NARRATORDB_COMPILER_BUDGET_SAFETY_RESERVE_USD",
                    1.0,
                ),
            )
            parsed_policy: dict[str, float] = {}
            for name, default in budget_policy:
                raw_value = os.getenv(name)
                try:
                    value = default if raw_value is None else float(raw_value)
                except ValueError as error:
                    raise ConfigurationError(
                        f"{name} must be a non-negative number"
                    ) from error
                if not math.isfinite(value) or value < 0:
                    raise ConfigurationError(
                        f"{name} must be a non-negative finite number"
                    )
                parsed_policy[name] = value
            request_reservation_usd = parsed_policy[
                "NARRATORDB_COMPILER_REQUEST_RESERVATION_USD"
            ]
            safety_reserve_usd = parsed_policy[
                "NARRATORDB_COMPILER_BUDGET_SAFETY_RESERVE_USD"
            ]

        db_path = Path(self.db_path)
        ledger_path = None
        if self.db_path != ":memory:":
            ledger_path = db_path.with_name(f"{db_path.name}.compiler-usage.jsonl")
        self._compiler_usage_ledger = ContentFreeUsageLedger(
            ledger_path,
            max_cost_usd=max_cost_usd,
            request_reservation_usd=request_reservation_usd,
            safety_reserve_usd=safety_reserve_usd,
        )
        runtime = compiler_from_project_config(
            self.compiler_config,
            usage_sink=self._compiler_usage_ledger,
        )
        if self.db_path != ":memory:":
            cache_path = db_path.with_name(f"{db_path.name}.compiler-cache.sqlite3")
            self._compiler_cache = CompiledSessionCache(cache_path)
            runtime = CachedMemoryCompiler(runtime, self._compiler_cache)
        self._compiler_runtime = runtime
        return self._compiler_runtime

    def _compiler_job_fingerprint(self) -> str:
        """Version queued work by config, prompt, and compiled-memory schema."""

        if self.compiler_config is None:
            raise ConfigurationError("intelligence mode requires a configured compiler")
        return self._compiler_job_fingerprint_for(self.compiler_config)

    @staticmethod
    def _compiler_job_fingerprint_for(compiler_config: CompilerConfig | None) -> str:
        if compiler_config is None:
            raise ConfigurationError("intelligence mode requires a configured compiler")
        from .compiler import (
            COMPILED_MEMORY_SCHEMA_VERSION,
            COMPILER_PROMPT_VERSION,
            COMPILER_RETRY_TOPOLOGY_VERSION,
        )

        return (
            f"{compiler_config.fingerprint}:"
            f"{COMPILER_PROMPT_VERSION}:{COMPILED_MEMORY_SCHEMA_VERSION}:"
            f"{COMPILER_RETRY_TOPOLOGY_VERSION}"
        )

    def process_enrichment(
        self,
        user_id: str | None = None,
        workspace_id: str | None = None,
        *,
        limit: int = 100,
        job_id: int | None = None,
    ) -> dict[str, Any]:
        """Process queued compiler jobs synchronously for a worker or CLI.

        Failures are persisted on their jobs and do not roll back raw session
        ingestion. The returned details contain no message or model content.
        """

        if limit < 1:
            raise ConfigurationError("enrichment limit must be positive")
        engine = self._get_engine(user_id, workspace_id)
        engine.obsolete_compilations_except(self._compiler_job_fingerprint())
        compiler = self._get_compiler_runtime()
        from .enrichment import EnrichmentRunner

        scan_limit = max(limit, 100_000 if job_id is not None else limit)
        jobs = engine.pending_compilations(limit=scan_limit)
        if job_id is not None:
            jobs = [job for job in jobs if int(job["id"]) == int(job_id)]
        jobs = jobs[:limit]
        if job_id is not None and not jobs:
            state = engine.compilation_job_state(job_id)
            usage = (
                self._compiler_usage_ledger.summary()
                if self._compiler_usage_ledger is not None
                else {}
            )
            if state is None:
                return {
                    "status": "missing",
                    "processed": 0,
                    "completed": 0,
                    "partial": 0,
                    "failed": 1,
                    "jobs": [
                        {
                            "job_id": int(job_id),
                            "status": "missing",
                            "error_code": "compiler_job_not_found",
                            "retryable": False,
                        }
                    ],
                    "usage": usage,
                }

            lifecycle = str(state["status"])
            detail = {
                "job_id": int(state["id"]),
                "session_id": str(state["session_id"]),
                "status": lifecycle,
                "attempts": int(state["attempts"]),
                "idempotent": lifecycle in {"complete", "partial"},
            }
            if lifecycle == "complete":
                return {
                    "status": "complete",
                    "processed": 0,
                    "completed": 1,
                    "partial": 0,
                    "failed": 0,
                    "jobs": [detail],
                    "usage": usage,
                }
            if lifecycle == "partial":
                return {
                    "status": "partial",
                    "processed": 0,
                    "completed": 0,
                    "partial": 1,
                    "failed": 0,
                    "jobs": [detail],
                    "usage": usage,
                }
            if lifecycle == "failed" and state.get("next_attempt_at") is not None:
                next_attempt_at = float(state["next_attempt_at"])
                retry_after_seconds = max(0.0, next_attempt_at - time.time())
                detail.update(
                    {
                        "status": "deferred",
                        "error_code": "compiler_retry_deferred",
                        "retryable": True,
                        "next_attempt_at": next_attempt_at,
                        "retry_after_seconds": retry_after_seconds,
                    }
                )
                return {
                    "status": "deferred",
                    "processed": 0,
                    "completed": 0,
                    "partial": 0,
                    "failed": 0,
                    "jobs": [detail],
                    "usage": usage,
                }
            if lifecycle in {"blocked", "exhausted"}:
                detail.update(
                    {
                        "error_code": str(state.get("last_error") or lifecycle),
                        "retryable": False,
                    }
                )
                return {
                    "status": "failed",
                    "processed": 0,
                    "completed": 0,
                    "partial": 0,
                    "failed": 1,
                    "jobs": [detail],
                    "usage": usage,
                }
            if lifecycle == "running":
                return {
                    "status": "in_progress",
                    "processed": 0,
                    "completed": 0,
                    "partial": 0,
                    "failed": 0,
                    "jobs": [detail],
                    "usage": usage,
                }
            if lifecycle == "obsolete":
                return {
                    "status": "stale",
                    "processed": 0,
                    "completed": 0,
                    "partial": 0,
                    "failed": 0,
                    "jobs": [detail],
                    "usage": usage,
                }
            detail.update(
                {
                    "error_code": "compiler_job_not_runnable",
                    "retryable": False,
                }
            )
            return {
                "status": "failed",
                "processed": 0,
                "completed": 0,
                "partial": 0,
                "failed": 1,
                "jobs": [detail],
                "usage": usage,
            }
        completed = 0
        partial = 0
        failed = 0
        details: list[dict[str, Any]] = []

        runner = EnrichmentRunner(engine, compiler)
        for job in jobs:
            outcome = runner.run_job(job)
            if outcome["ok"]:
                outcome_status = str(outcome.get("status") or "complete")
                if outcome_status == "complete":
                    completed += 1
                else:
                    partial += 1
                details.append(
                    {
                        "job_id": int(job["id"]),
                        "session_id": str(job["session_id"]),
                        "status": outcome_status,
                        "claims_stored": int(outcome.get("claims_stored") or 0),
                        "warning_count": len(outcome.get("warnings") or []),
                    }
                )
            else:
                failed += 1
                details.append(
                    {
                        "job_id": int(job["id"]),
                        "session_id": str(job["session_id"]),
                        "status": str(outcome.get("status") or "failed"),
                        "error_code": str(outcome.get("code") or "compiler_error")[
                            :200
                        ],
                        "retryable": bool(outcome.get("retryable", True)),
                    }
                )

        if failed and not completed and not partial:
            status = "failed"
        elif failed or partial:
            status = "partial"
        elif jobs:
            status = "complete"
        else:
            status = "idle"
        return {
            "status": status,
            "processed": len(jobs),
            "completed": completed,
            "partial": partial,
            "failed": failed,
            "jobs": details,
            "usage": self._compiler_usage_ledger.summary()
            if self._compiler_usage_ledger is not None
            else {},
        }

    def purge_derived(
        self,
        user_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Purge derived records while preserving canonical raw messages.

        With no explicit scope, every derived scope in the project database is
        purged. This matches the database-wide mode setting and prevents a
        private-mode switch from leaving another user's compiled records behind.
        """

        explicit_scope = user_id is not None or workspace_id is not None
        if explicit_scope:
            scope_keys = [self._scope_key(user_id, workspace_id)]
        else:
            discovery = self._get_engine()
            scope_keys = sorted(
                {
                    str(row[0])
                    for row in discovery._conn.execute(
                        """
                        SELECT user_id FROM memory_sessions
                        UNION SELECT user_id FROM memory_claims
                        UNION SELECT user_id FROM memory_entities
                        UNION SELECT user_id FROM memory_compiler_jobs
                        """
                    ).fetchall()
                }
            )

        deleted = 0
        for scope_key in scope_keys:
            engine = self._engines.get(scope_key)
            owns_engine = engine is None
            if engine is None:
                engine = Engine(
                    db_path=self.db_path,
                    user_id=scope_key,
                    context_window=self.context_window,
                    semantic_dedup=self.semantic_dedup,
                    dedup_threshold=self.dedup_threshold,
                    dedup_window=self.dedup_window,
                    semantic_search_mode=(
                        "hybrid"
                        if self.mode is MemoryMode.INTELLIGENCE
                        else "fallback_only"
                    ),
                    local_only=True,
                )
            method = getattr(engine, "purge_derived", None)
            if not callable(method):
                if owns_engine:
                    engine.close()
                raise FeatureUnavailableError(
                    "this installation does not provide derived-memory storage to purge"
                )
            deleted += int(method() or 0)
            if owns_engine:
                engine.close()

        response: dict[str, Any] = {
            "deleted": deleted,
            "scopes_purged": len(scope_keys),
        }
        response["compiler_cache_entries_deleted"] = self._clear_compiler_cache()
        return response

    def _clear_compiler_cache(self) -> int:
        """Clear the project-wide content-bearing compiler cache if it exists."""

        if self.db_path != ":memory:":
            from .compiler_cache import CompiledSessionCache

            cache = self._compiler_cache
            owns_cache = cache is None
            if cache is None:
                db_path = Path(self.db_path)
                cache_path = db_path.with_name(f"{db_path.name}.compiler-cache.sqlite3")
                if cache_path.exists():
                    cache = CompiledSessionCache(cache_path)
            if cache is not None:
                deleted = cache.clear()
                if owns_cache:
                    cache.close()
                return deleted
        return 0

    def health_check(
        self,
        user_id: str | None = None,
        workspace_id: str | None = None,
        full: bool = False,
    ) -> dict:
        """Run a non-destructive SQLite and index consistency check."""

        return self._get_engine(user_id, workspace_id).health_check(full=full)

    def backup(self, destination: str) -> dict:
        """Create and verify a consistent backup of the complete database."""

        return self._get_engine().backup(destination)

    def close(self):
        for engine in self._engines.values():
            engine.close()
        self._engines.clear()
        if self._compiler_cache is not None:
            self._compiler_cache.close()
            self._compiler_cache = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __repr__(self):
        return (
            f"NarratorDB(engine={ENGINE_NAME!r}, db_path={self.db_path!r}, "
            f"mode={self.mode.value!r}, default_user_id={self._resolve_user_id()!r})"
        )
