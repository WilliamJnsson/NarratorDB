"""Write-time compiler orchestration for intelligence mode."""

from __future__ import annotations

import dataclasses
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .compiler import (
    COMPILER_PROMPT_VERSION,
    CompileResult,
    CompileSessionInput,
    CompilerError,
    CompilerTransportError,
    MAX_RETRY_AFTER_SECONDS,
    MemoryCompiler,
    ReferenceClaim,
    SourceMessage,
)
from .engine import Engine


_SAFE_PROVIDER_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._/-]{0,31}"
    r"(?: [A-Za-z0-9][A-Za-z0-9._/-]{0,31}){0,4}"
)
_SAFE_CODE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")


def _safe_code_token(value: Any, *, max_length: int) -> str:
    if not isinstance(value, str) or len(value) > max_length:
        return "unknown"
    if value.casefold().startswith(("sk-", "sk_", "bearer")):
        return "unknown"
    return value if _SAFE_CODE_TOKEN.fullmatch(value) else "unknown"


def _safe_provider(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_PROVIDER_NAME.fullmatch(value):
        return "unknown"
    if value.casefold().startswith(("sk-", "sk_", "bearer")):
        return "unknown"
    return value


def _route_attempt_pairs(
    providers: Any,
    statuses: Any,
) -> list[dict[str, int | str]]:
    if not isinstance(providers, (list, tuple)) or not isinstance(
        statuses, (list, tuple)
    ):
        return []
    if len(providers) != len(statuses):
        return []
    pairs: list[dict[str, int | str]] = []
    for provider, status in zip(providers[:16], statuses[:16]):
        safe_provider = _safe_provider(provider)
        if safe_provider == "unknown":
            continue
        if isinstance(status, bool) or not isinstance(status, int) or status < 0:
            continue
        pairs.append({"provider": safe_provider, "status": status})
    return pairs


def _safe_retry_metadata(error: CompilerError, attempt: int) -> dict[str, Any]:
    retry_after = error.retry_after_seconds
    if retry_after is None and isinstance(error, CompilerTransportError):
        if error.rate_limit_reset_at is not None:
            retry_after = max(0.0, error.rate_limit_reset_at - time.time())
        elif error.status == 429:
            retry_after = 60.0 * (2 ** (max(1, int(attempt)) - 1))
        else:
            retry_after = 2.0 * (4 ** (max(1, int(attempt)) - 1))
    if retry_after is not None:
        retry_after = min(float(MAX_RETRY_AFTER_SECONDS), max(0.0, retry_after))
    metadata: dict[str, Any] = {}
    optional = {
        "upstream_status": error.status,
        "retry_after_seconds": retry_after,
        "error_type": error.error_type,
        "provider": error.provider_name,
        "provider_code": error.provider_code,
        "router_attempt": error.router_attempt,
    }
    metadata.update(
        {key: value for key, value in optional.items() if value is not None}
    )
    route_attempts = _route_attempt_pairs(
        error.attempted_providers,
        error.attempt_statuses,
    )
    if route_attempts:
        metadata["route_attempts"] = route_attempts
    return metadata


def _iso_timestamp(value: float | int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def build_compile_input(engine: Engine, session_pk: int) -> CompileSessionInput:
    session = engine.load_compiler_session(session_pk)
    messages = []
    for message in session["messages"]:
        raw_role = str(message.get("role") or "user").lower()
        role = (
            raw_role if raw_role in {"user", "assistant", "system", "tool"} else "user"
        )
        messages.append(
            SourceMessage(
                message_id=str(message["id"]),
                role=role,
                content=str(message["content"]),
                occurred_at=_iso_timestamp(message.get("timestamp")),
            )
        )
    return CompileSessionInput(
        session_id=str(session["session_id"]),
        messages=tuple(messages),
        document_time=_iso_timestamp(session.get("occurred_at")),
        reference_claims=tuple(
            ReferenceClaim(
                claim_id=str(claim["claim_id"]),
                memory_key=str(claim.get("memory_key") or ""),
                text=str(claim["text"]),
                document_time=_iso_timestamp(claim.get("document_time")),
                event_start=_iso_timestamp(claim.get("event_start")),
                event_end=_iso_timestamp(claim.get("event_end")),
                valid_from=_iso_timestamp(claim.get("valid_from")),
                valid_to=_iso_timestamp(claim.get("valid_to")),
            )
            for claim in engine.load_compiler_reference_claims(session_pk)
        ),
    )


def aggregate_usage(result_or_events: CompileResult | tuple | list) -> dict[str, Any]:
    if isinstance(result_or_events, CompileResult):
        events = result_or_events.usage
    else:
        events = tuple(result_or_events)
    if not events:
        return {}
    serialized = [dataclasses.asdict(event) for event in events]
    last = serialized[-1]
    request_model = _safe_code_token(last.get("request_model"), max_length=160)
    response_model = _safe_code_token(last.get("response_model"), max_length=160)
    aggregated: dict[str, Any] = {
        "provider": _safe_provider(last.get("provider")),
        "model": (
            request_model if response_model == request_model else "route_mismatch"
        ),
        "input_tokens": sum(
            int(event.get("prompt_tokens") or 0) for event in serialized
        ),
        "output_tokens": sum(
            int(event.get("completion_tokens") or 0) for event in serialized
        ),
        "reasoning_tokens": sum(
            int(event.get("reasoning_tokens") or 0) for event in serialized
        ),
        "cached_tokens": sum(
            int(event.get("cached_tokens") or 0) for event in serialized
        ),
        "cost_usd": sum(float(event.get("cost_usd") or 0.0) for event in serialized),
        "unknown_cost_attempts": sum(
            int(event.get("unknown_cost") is True) for event in serialized
        ),
    }
    route_attempts: list[dict[str, int | str]] = []
    for event in serialized:
        route_attempts.extend(
            _route_attempt_pairs(
                event.get("attempted_providers"),
                event.get("attempt_statuses"),
            )
        )
        if len(route_attempts) >= 16:
            route_attempts = route_attempts[:16]
            break
    if route_attempts:
        aggregated["router_attempts"] = route_attempts
    return aggregated


class EnrichmentRunner:
    """Run queued compiler jobs while keeping raw ingestion independent."""

    def __init__(
        self,
        engine: Engine,
        compiler: MemoryCompiler,
        *,
        heartbeat_seconds: float = 30.0,
    ):
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        self.engine = engine
        self.compiler = compiler
        self.heartbeat_seconds = float(heartbeat_seconds)

    def _lost_lease_outcome(self, job_id: int) -> dict[str, Any]:
        state = self.engine.compilation_job_state(job_id)
        if state is not None and state["status"] in {"complete", "partial"}:
            return {
                "job_id": job_id,
                "ok": True,
                "status": str(state["status"]),
                "claims_stored": 0,
                "warnings": ["compiler job completed by another worker"],
                "usage": {},
            }
        lifecycle = str(state["status"]) if state is not None else "missing"
        return {
            "job_id": job_id,
            "ok": False,
            "status": "in_progress" if lifecycle == "running" else lifecycle,
            "code": "compiler_job_lease_lost",
            "retryable": lifecycle in {"pending", "failed", "running"},
            "usage": {},
        }

    def run_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = int(job["id"])
        attempt_token = self.engine.claim_compilation_attempt(job_id)
        if attempt_token is None:
            return self._lost_lease_outcome(job_id)
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(self.heartbeat_seconds):
                try:
                    if not self.engine.heartbeat_compilation(job_id, attempt_token):
                        return
                except Exception:
                    # Final apply/failure is fenced by the attempt token even
                    # if a transient heartbeat itself cannot be written.
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"narratordb-job-heartbeat-{job_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            compiler_input = build_compile_input(self.engine, int(job["session_pk"]))
            result = self.compiler.compile_session(compiler_input)
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=max(1.0, self.heartbeat_seconds * 2))
            usage = aggregate_usage(result)
            applied = self.engine.apply_compilation(
                job_id,
                result.memory,
                processor=type(self.compiler).__name__,
                processor_version=self.compiler.fingerprint,
                prompt_version=COMPILER_PROMPT_VERSION,
                usage=usage or None,
                expected_attempt=attempt_token,
            )
            if applied.get("status") == "stale":
                return self._lost_lease_outcome(job_id)
            return {"job_id": job_id, "ok": True, **applied, "usage": usage}
        except CompilerError as error:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=max(1.0, self.heartbeat_seconds * 2))
            usage = aggregate_usage(error.usage)
            if error.code in {"content_filtered", "model_refusal"}:
                try:
                    applied = self.engine.apply_compilation(
                        job_id,
                        {"summary": "", "claims": [], "entities": [], "relations": []},
                        processor=type(self.compiler).__name__,
                        processor_version=self.compiler.fingerprint,
                        prompt_version=COMPILER_PROMPT_VERSION,
                        usage=usage or None,
                        expected_attempt=attempt_token,
                        compiler_warnings=(error.code,),
                    )
                except Exception as apply_error:
                    failure_status = self.engine.mark_compilation_failed(
                        job_id,
                        type(apply_error).__name__,
                        True,
                        expected_attempt=attempt_token,
                    )
                    if failure_status == "stale":
                        return self._lost_lease_outcome(job_id)
                    return {
                        "job_id": job_id,
                        "ok": False,
                        "status": failure_status,
                        "code": "internal_compiler_error",
                        "retryable": True,
                        "usage": usage,
                    }
                if applied.get("status") == "stale":
                    return self._lost_lease_outcome(job_id)
                return {
                    "job_id": job_id,
                    "ok": True,
                    **applied,
                    "code": error.code,
                    "usage": usage,
                }
            if usage:
                self.engine.record_compiler_usage(job_id, usage)
            retry_metadata = _safe_retry_metadata(error, attempt_token)
            failure_status = self.engine.mark_compilation_failed(
                job_id,
                error.code,
                error.retryable,
                expected_attempt=attempt_token,
                retry_after_seconds=retry_metadata.get("retry_after_seconds"),
            )
            if failure_status == "stale":
                return self._lost_lease_outcome(job_id)
            return {
                "job_id": job_id,
                "ok": failure_status == "stale",
                "status": failure_status,
                "code": error.code,
                "retryable": error.retryable,
                "usage": usage,
                **retry_metadata,
            }
        except Exception as error:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=max(1.0, self.heartbeat_seconds * 2))
            failure_status = self.engine.mark_compilation_failed(
                job_id,
                type(error).__name__,
                True,
                expected_attempt=attempt_token,
            )
            if failure_status == "stale":
                return self._lost_lease_outcome(job_id)
            return {
                "job_id": job_id,
                "ok": failure_status == "stale",
                "status": failure_status,
                "code": "internal_compiler_error",
                "retryable": True,
            }
        finally:
            heartbeat_stop.set()

    def run_pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return [
            self.run_job(job) for job in self.engine.pending_compilations(limit=limit)
        ]


class BackgroundEnricher:
    """Small, resumable background worker for one scope/compiler pair.

    The worker opens its own file-backed ``Engine`` connection.  The caller's
    engine therefore remains foreground-owned while SQLite WAL and busy-timeout
    coordination serialize writes across the two connections.
    """

    def __init__(self, runner: EnrichmentRunner, *, poll_seconds: float = 1.0):
        if runner.engine.db_path == ":memory:":
            raise ValueError(
                "BackgroundEnricher requires a file-backed Engine; "
                "use EnrichmentRunner synchronously for :memory: databases"
            )
        self.runner = runner
        self.poll_seconds = max(0.05, float(poll_seconds))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._idle = threading.Event()
        self._failure: Exception | None = None
        self._worker_engine: Engine | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"narratordb-enrichment-{runner.engine.user_id}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=30.0):
            self.close(wait=False)
            raise RuntimeError("timed out opening the background enrichment connection")
        self._raise_worker_failure()

    def notify(self) -> None:
        self._idle.clear()
        self._wake.set()

    @property
    def running(self) -> bool:
        """Return whether the worker is alive and has not failed."""

        return self._thread.is_alive() and self._failure is None

    @property
    def failure_type(self) -> str | None:
        """Return a content-free failure classification for health output."""

        return type(self._failure).__name__ if self._failure is not None else None

    def _run(self) -> None:
        worker_engine = None
        try:
            source = self.runner.engine
            worker_engine = Engine(
                db_path=source.db_path,
                user_id=source.user_id,
                stop_words=source.stop_words,
                context_window=0,
                semantic_dedup=False,
                semantic_search_mode="disabled",
                local_only=True,
            )
            self._worker_engine = worker_engine
            worker_runner = EnrichmentRunner(worker_engine, self.runner.compiler)
            self._ready.set()
            while not self._stop.is_set():
                self._idle.clear()
                jobs = worker_runner.run_pending(limit=20)
                if jobs:
                    continue
                self._idle.set()
                self._wake.wait(self.poll_seconds)
                self._wake.clear()
        except Exception as error:
            self._failure = error
            self._ready.set()
            self._wake.set()
        finally:
            if worker_engine is not None:
                worker_engine.close()
            self._worker_engine = None

    def _raise_worker_failure(self) -> None:
        if self._failure is not None:
            raise RuntimeError("background enrichment worker failed") from self._failure

    def close(self, *, wait: bool = True) -> None:
        self._stop.set()
        self._wake.set()
        if wait and self._thread.is_alive():
            self._thread.join(timeout=30.0)

    def drain(self, *, timeout: float = 300.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout)
        self.notify()
        while time.monotonic() < deadline:
            self._raise_worker_failure()
            status = self.runner.engine.enrichment_status()
            pending = int(status["jobs"].get("pending", 0))
            retry_waiting = int(status["jobs"].get("failed", 0))
            running = int(status["jobs"].get("running", 0))
            actionable = bool(self.runner.engine.pending_compilations(limit=1))
            if (
                pending == 0
                and retry_waiting == 0
                and running == 0
                and not actionable
                and self._idle.is_set()
            ):
                return status
            time.sleep(0.05)
        self._raise_worker_failure()
        raise TimeoutError("timed out waiting for NarratorDB enrichment")
