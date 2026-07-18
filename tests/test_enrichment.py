"""Reliability tests for resumable intelligence-mode enrichment."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
import sqlite3
from pathlib import Path

from narratordb.compiler import (
    CompileResult,
    CompiledClaim,
    CompiledMemory,
    CompiledSummary,
    CompilerResponseError,
    CompilerTransportError,
    CompilerUsage,
    EvidenceSpan,
)
from narratordb.engine import Engine
from narratordb.enrichment import (
    BackgroundEnricher,
    EnrichmentRunner,
    _safe_retry_metadata,
    aggregate_usage,
    build_compile_input,
)


def _usage() -> CompilerUsage:
    return CompilerUsage(
        request_model="test-model",
        response_model="test-model",
        provider="local-test",
        attempt=1,
        prompt_tokens=10,
        cached_tokens=2,
        completion_tokens=4,
        reasoning_tokens=1,
        cost_usd=0.000034,
        cost_source="estimated",
        finish_reason="stop",
    )


class _SuccessfulCompiler:
    fingerprint = "test-model:compiler-v1"

    def compile_session(self, session):
        source = session.messages[0]
        return CompileResult(
            memory=CompiledMemory(
                session_id=session.session_id,
                summary=CompiledSummary(text=""),
                claims=(
                    CompiledClaim(
                        claim_id="claim-1",
                        kind="fact",
                        text="The project codename is firefly.",
                        confidence=1.0,
                        status="active",
                        document_time=None,
                        event_start=None,
                        event_end=None,
                        valid_from=None,
                        valid_to=None,
                        entity_ids=(),
                        evidence=(
                            EvidenceSpan(
                                message_id=source.message_id,
                                quote="project codename is firefly",
                                start=4,
                                end=31,
                            ),
                        ),
                    ),
                ),
                entities=(),
                relations=(),
            ),
            usage=(_usage(),),
        )


class _FailingCompiler:
    fingerprint = "test-model:failing-v1"

    def __init__(self, *, retryable: bool):
        self.retryable = retryable
        self.calls = 0

    def compile_session(self, session):
        self.calls += 1
        raise CompilerResponseError(
            "safe test failure",
            code="test_failure",
            retryable=self.retryable,
        ).attach_usage((_usage(),))


class _RateLimitedCompiler:
    fingerprint = "test-model:rate-limited-v1"

    def compile_session(self, session):
        raise CompilerTransportError(
            "safe rate limit",
            code="http_429",
            retryable=True,
            status=429,
            provider_name="DeepInfra",
        )


class _ContentFilteredCompiler:
    fingerprint = "test-model:content-filtered-v1"

    def compile_session(self, session):
        raise CompilerResponseError(
            "provider filtered the test completion",
            code="content_filtered",
            retryable=False,
        ).attach_usage((_usage(),))


class _BlockingBulkCompiler:
    fingerprint = "test-model:concurrent-v1"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def compile_session(self, session):
        source = session.messages[0]
        evidence = EvidenceSpan(
            message_id=source.message_id,
            quote=source.content,
            start=0,
            end=len(source.content),
        )
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test compiler was not released")
        return CompileResult(
            memory=CompiledMemory(
                session_id=session.session_id,
                summary=CompiledSummary(text=""),
                claims=tuple(
                    CompiledClaim(
                        claim_id=f"claim-{index}",
                        kind="fact",
                        text=f"Concurrent derived claim {index}.",
                        confidence=1.0,
                        status="active",
                        document_time=None,
                        event_start=None,
                        event_end=None,
                        valid_from=None,
                        valid_to=None,
                        entity_ids=(),
                        evidence=(evidence,),
                    )
                    for index in range(128)
                ),
                entities=(),
                relations=(),
            )
        )


class _ResidenceCompiler:
    fingerprint = "test-model:residence-v1"

    def __init__(self) -> None:
        self.inputs = []

    def compile_session(self, session):
        self.inputs.append(session)
        source = session.messages[0]
        city = "Tokyo" if "Tokyo" in source.content else "Kyoto"
        return CompileResult(
            memory=CompiledMemory(
                session_id=session.session_id,
                summary=CompiledSummary(text=""),
                claims=(
                    CompiledClaim(
                        claim_id=f"residence-{city.casefold()}",
                        kind="fact",
                        text=f"The user lives in {city}.",
                        subject="the user",
                        predicate="lives in",
                        object_text=city,
                        memory_key="user.residence.current_city",
                        confidence=1.0,
                        status="active",
                        document_time=session.document_time,
                        event_start=None,
                        event_end=None,
                        valid_from=session.document_time,
                        valid_to=None,
                        entity_ids=(),
                        evidence=(
                            EvidenceSpan(
                                message_id=source.message_id,
                                quote=source.content,
                                start=0,
                                end=len(source.content),
                            ),
                        ),
                    ),
                ),
                entities=(),
                relations=(),
            )
        )


class EnrichmentRunnerTests(unittest.TestCase):
    def _queued_engine(self, directory: str) -> tuple[Engine, int]:
        engine = Engine(
            str(Path(directory) / "memory.db"),
            user_id="test-user",
            semantic_dedup=False,
            local_only=True,
        )
        stored = engine.store_session(
            [{"role": "user", "content": "The project codename is firefly."}],
            session_id="session-1",
        )
        job_id = engine.enqueue_compilation(
            stored["session_pk"],
            stored["source_hash"],
            "test-config:v1",
        )
        return engine, job_id

    def test_success_applies_claim_and_records_content_free_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, job_id = self._queued_engine(directory)
            try:
                outcomes = EnrichmentRunner(engine, _SuccessfulCompiler()).run_pending()
                self.assertEqual(len(outcomes), 1)
                self.assertTrue(outcomes[0]["ok"])
                self.assertEqual(outcomes[0]["job_id"], job_id)
                status = engine.enrichment_status()
                self.assertEqual(status["jobs"], {"complete": 1})
                self.assertEqual(status["claim_count"], 1)
                self.assertEqual(status["usage"]["input_tokens"], 10)
                self.assertAlmostEqual(status["usage"]["cost_usd"], 0.000034)
            finally:
                engine.close()

    def test_new_session_receives_related_prior_claim_as_reference_context(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(
                str(Path(directory) / "memory.db"),
                user_id="test-user",
                semantic_dedup=False,
                local_only=True,
            )
            compiler = _ResidenceCompiler()
            runner = EnrichmentRunner(engine, compiler)
            try:
                prior = engine.store_session(
                    [{"role": "user", "content": "I live in Tokyo."}],
                    session_id="prior-session",
                    occurred_at=1_700_000_000.0,
                )
                engine.enqueue_compilation(
                    prior["session_pk"], prior["source_hash"], compiler.fingerprint
                )
                self.assertTrue(runner.run_pending()[0]["ok"])

                current = engine.store_session(
                    [{"role": "user", "content": "I moved to Kyoto."}],
                    session_id="current-session",
                    occurred_at=1_710_000_000.0,
                )
                engine.enqueue_compilation(
                    current["session_pk"], current["source_hash"], compiler.fingerprint
                )
                self.assertTrue(runner.run_pending()[0]["ok"])

                references = compiler.inputs[-1].reference_claims
                self.assertEqual(len(references), 1)
                self.assertEqual(
                    references[0].memory_key, "user.residence.current_city"
                )
                self.assertEqual(references[0].text, "The user lives in Tokyo.")
                rows = engine._conn.execute(
                    """
                    SELECT text, status FROM memory_claims
                    WHERE user_id = ? AND memory_key = ? ORDER BY id
                    """,
                    ("test-user", "user.residence.current_city"),
                ).fetchall()
                self.assertEqual(
                    [(str(row[0]), str(row[1])) for row in rows],
                    [
                        ("The user lives in Tokyo.", "superseded"),
                        ("The user lives in Kyoto.", "active"),
                    ],
                )
            finally:
                engine.close()

    def test_nonretryable_error_is_blocked_and_usage_survives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, _ = self._queued_engine(directory)
            compiler = _FailingCompiler(retryable=False)
            try:
                outcome = EnrichmentRunner(engine, compiler).run_pending()[0]
                self.assertFalse(outcome["ok"])
                self.assertFalse(outcome["retryable"])
                self.assertEqual(engine.enrichment_status()["jobs"], {"blocked": 1})
                self.assertEqual(
                    engine.enrichment_status()["usage"]["input_tokens"], 10
                )
                self.assertEqual(engine.pending_compilations(), [])
            finally:
                engine.close()

    def test_content_filter_degrades_to_partial_raw_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, job_id = self._queued_engine(directory)
            try:
                outcome = EnrichmentRunner(
                    engine, _ContentFilteredCompiler()
                ).run_pending()[0]

                self.assertTrue(outcome["ok"])
                self.assertEqual(outcome["job_id"], job_id)
                self.assertEqual(outcome["status"], "partial")
                self.assertEqual(outcome["code"], "content_filtered")
                self.assertEqual(outcome["claims_stored"], 0)
                self.assertEqual(outcome["warnings"], ["content_filtered"])
                status = engine.enrichment_status()
                self.assertEqual(status["jobs"], {"partial": 1})
                self.assertEqual(status["partial_reasons"], {"content_filtered": 1})
                self.assertEqual(status["claim_count"], 0)
                self.assertEqual(status["usage"]["input_tokens"], 10)
                self.assertEqual(engine.pending_compilations(), [])
                self.assertTrue(engine.search("project codename").messages)
                state = engine.compilation_job_state(job_id)
                self.assertIsNotNone(state)
                self.assertEqual(state["last_error"], "content_filtered")
            finally:
                engine.close()

    def test_partial_reason_never_persists_arbitrary_warning_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, job_id = self._queued_engine(directory)
            try:
                attempt = engine.claim_compilation_attempt(job_id)
                self.assertIsNotNone(attempt)
                applied = engine.apply_compilation(
                    job_id,
                    {"summary": "", "claims": [], "entities": [], "relations": []},
                    processor="test-compiler",
                    processor_version="test-v1",
                    prompt_version="test",
                    expected_attempt=attempt,
                    compiler_warnings=("private source warning text",),
                )

                self.assertEqual(applied["status"], "partial")
                self.assertEqual(
                    engine.enrichment_status()["partial_reasons"],
                    {"compiler_warning": 1},
                )
                state = engine.compilation_job_state(job_id)
                self.assertIsNotNone(state)
                self.assertEqual(state["last_error"], "compiler_warning")
            finally:
                engine.close()

    def test_running_job_cannot_be_claimed_twice_until_it_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, job_id = self._queued_engine(directory)
            try:
                self.assertTrue(engine.mark_compilation_running(job_id))
                self.assertFalse(engine.mark_compilation_running(job_id))
                running = engine._conn.execute(
                    """
                    SELECT session_id, source_hash, compiler_fingerprint,
                           attempts, updated_at
                    FROM memory_compiler_jobs WHERE id = ?
                    """,
                    (job_id,),
                ).fetchone()
                self.assertEqual(running[3], 1)
                self.assertEqual(
                    engine.enqueue_compilation(
                        int(running[0]), str(running[1]), str(running[2])
                    ),
                    job_id,
                )
                unchanged = engine._conn.execute(
                    "SELECT status, attempts, updated_at FROM memory_compiler_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                self.assertEqual(tuple(unchanged), ("running", 1, running[4]))

                engine._conn.execute(
                    "UPDATE memory_compiler_jobs SET updated_at = ? WHERE id = ?",
                    (time.time() - 1_000, job_id),
                )
                engine._conn.commit()
                self.assertTrue(
                    engine.mark_compilation_running(job_id, stale_after_seconds=10)
                )
                attempts = engine._conn.execute(
                    "SELECT attempts FROM memory_compiler_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()[0]
                self.assertEqual(attempts, 2)
            finally:
                engine.close()

    def test_retryable_jobs_stop_after_three_worker_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, _ = self._queued_engine(directory)
            compiler = _FailingCompiler(retryable=True)
            runner = EnrichmentRunner(engine, compiler)
            try:
                for _ in range(3):
                    self.assertEqual(len(runner.run_pending()), 1)
                self.assertEqual(compiler.calls, 3)
                self.assertEqual(runner.run_pending(), [])
                self.assertEqual(engine.enrichment_status()["jobs"], {"exhausted": 1})
                exhausted = engine._conn.execute(
                    """
                    SELECT id, session_id, source_hash, compiler_fingerprint,
                           status, attempts, last_error, updated_at
                    FROM memory_compiler_jobs
                    """
                ).fetchone()
                self.assertEqual(
                    exhausted[4:7],
                    ("exhausted", 3, "test_failure"),
                )
                self.assertEqual(
                    engine.enqueue_compilation(
                        int(exhausted[1]), str(exhausted[2]), str(exhausted[3])
                    ),
                    int(exhausted[0]),
                )
                unchanged = engine._conn.execute(
                    """
                    SELECT status, attempts, last_error, updated_at
                    FROM memory_compiler_jobs
                    """
                ).fetchone()
                self.assertEqual(
                    tuple(unchanged),
                    ("exhausted", 3, "test_failure", exhausted[7]),
                )
            finally:
                engine.close()

    def test_retry_after_is_durable_and_gates_worker_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "memory.db")
            engine, job_id = self._queued_engine(directory)
            try:
                before = time.time()
                outcome = EnrichmentRunner(
                    engine, _RateLimitedCompiler()
                ).run_pending()[0]
                self.assertEqual(outcome["status"], "failed")
                self.assertEqual(outcome["retry_after_seconds"], 60.0)
                state = engine.compilation_job_state(job_id)
                self.assertIsNotNone(state)
                self.assertGreaterEqual(state["next_attempt_at"], before + 59.0)
                self.assertEqual(engine.pending_compilations(), [])
                self.assertIsNone(engine.claim_compilation_attempt(job_id))
                row = engine._conn.execute(
                    """
                    SELECT session_id, source_hash, compiler_fingerprint,
                           status, attempts, last_error, next_attempt_at, updated_at
                    FROM memory_compiler_jobs WHERE id = ?
                    """,
                    (job_id,),
                ).fetchone()
                duplicate = engine.enqueue_compilation(
                    int(row[0]), str(row[1]), str(row[2])
                )
                self.assertEqual(duplicate, job_id)
                preserved = engine._conn.execute(
                    """
                    SELECT status, attempts, last_error, next_attempt_at, updated_at
                    FROM memory_compiler_jobs WHERE id = ?
                    """,
                    (job_id,),
                ).fetchone()
                self.assertEqual(tuple(preserved), tuple(row[3:]))
            finally:
                engine.close()

            reopened = Engine(
                database,
                user_id="test-user",
                semantic_dedup=False,
                local_only=True,
            )
            try:
                self.assertEqual(reopened.pending_compilations(), [])
                reopened._conn.execute(
                    "UPDATE memory_compiler_jobs SET next_attempt_at = ? WHERE id = ?",
                    (time.time() - 1.0, job_id),
                )
                reopened._conn.commit()
                second_started = time.time()
                second = EnrichmentRunner(
                    reopened, _RateLimitedCompiler()
                ).run_pending()[0]
                self.assertEqual(second["retry_after_seconds"], 120.0)
                state = reopened.compilation_job_state(job_id)
                self.assertEqual(state["attempts"], 2)
                self.assertGreaterEqual(
                    state["next_attempt_at"], second_started + 119.0
                )

                reopened._conn.execute(
                    "UPDATE memory_compiler_jobs SET next_attempt_at = ? WHERE id = ?",
                    (time.time() - 1.0, job_id),
                )
                reopened._conn.commit()
                third_attempt = reopened.claim_compilation_attempt(job_id)
                self.assertEqual(third_attempt, 3)
                lifecycle = reopened._conn.execute(
                    "SELECT status, next_attempt_at, finished_at "
                    "FROM memory_compiler_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                self.assertEqual(tuple(lifecycle), ("running", None, None))
            finally:
                reopened.close()

    def test_worker_backoff_schedule_is_attempt_aware(self) -> None:
        rate_limited = CompilerTransportError(
            "safe rate limit",
            code="http_429",
            retryable=True,
            status=429,
            attempted_providers=("Parasail", "Wafer"),
            attempt_statuses=(429, 429),
        )
        network = CompilerTransportError(
            "safe network error",
            code="network_error",
            retryable=True,
        )

        self.assertEqual(
            _safe_retry_metadata(rate_limited, 1)["retry_after_seconds"], 60.0
        )
        self.assertEqual(
            _safe_retry_metadata(rate_limited, 2)["retry_after_seconds"], 120.0
        )
        self.assertEqual(
            _safe_retry_metadata(rate_limited, 2)["route_attempts"],
            [
                {"provider": "Parasail", "status": 429},
                {"provider": "Wafer", "status": 429},
            ],
        )
        self.assertEqual(_safe_retry_metadata(network, 1)["retry_after_seconds"], 2.0)
        self.assertEqual(_safe_retry_metadata(network, 2)["retry_after_seconds"], 8.0)

    def test_aggregate_usage_rejects_free_form_identity_metadata(self) -> None:
        secret = "sk-or-v1-abc123def456"
        unsafe = CompilerUsage(
            request_model="configured/model",
            response_model=secret,
            provider=secret,
            attempt=1,
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            reasoning_tokens=0,
            cost_usd=0.01,
            cost_source="provider",
            finish_reason=secret,
        )

        aggregated = aggregate_usage((unsafe,))

        self.assertEqual(aggregated["model"], "route_mismatch")
        self.assertEqual(aggregated["provider"], "unknown")
        self.assertNotIn(secret, str(aggregated))

    def test_v2_job_schema_auto_migrates_to_retry_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "memory.db")
            engine = Engine(
                database,
                user_id="test-user",
                semantic_dedup=False,
                local_only=True,
            )
            engine.close()
            with sqlite3.connect(database) as connection:
                connection.execute("DROP INDEX idx_memory_compiler_jobs_retry_due")
                connection.execute(
                    "ALTER TABLE memory_compiler_jobs DROP COLUMN next_attempt_at"
                )
                connection.execute(
                    "UPDATE metadata SET value = '2' "
                    "WHERE key = 'derived_schema_version'"
                )

            migrated = Engine(
                database,
                user_id="test-user",
                semantic_dedup=False,
                local_only=True,
            )
            try:
                columns = {
                    str(row[1])
                    for row in migrated._conn.execute(
                        "PRAGMA table_info(memory_compiler_jobs)"
                    ).fetchall()
                }
                self.assertIn("next_attempt_at", columns)
                self.assertEqual(
                    migrated._conn.execute(
                        "SELECT value FROM metadata "
                        "WHERE key = 'derived_schema_version'"
                    ).fetchone()[0],
                    "3",
                )
                self.assertTrue(migrated.health_check()["ok"])
            finally:
                migrated.close()

            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE metadata SET value = '999' "
                    "WHERE key = 'derived_schema_version'"
                )
            with self.assertRaisesRegex(RuntimeError, "schema is newer"):
                Engine(
                    database,
                    user_id="test-user",
                    semantic_dedup=False,
                    local_only=True,
                )

    def test_background_drain_treats_exhausted_failure_as_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, _ = self._queued_engine(directory)
            compiler = _FailingCompiler(retryable=True)
            worker = BackgroundEnricher(
                EnrichmentRunner(engine, compiler),
                poll_seconds=0.05,
            )
            try:
                status = worker.drain(timeout=5)
                self.assertEqual(compiler.calls, 3)
                self.assertEqual(status["jobs"], {"exhausted": 1})
            finally:
                worker.close()
                engine.close()

    def test_stale_running_job_is_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, job_id = self._queued_engine(directory)
            try:
                engine.mark_compilation_running(job_id)
                engine._conn.execute(
                    "UPDATE memory_compiler_jobs SET updated_at = ? WHERE id = ?",
                    (time.time() - 1_000, job_id),
                )
                engine._conn.commit()
                pending = engine._memory.next_jobs(stale_after_seconds=10)
                self.assertEqual([job["id"] for job in pending], [job_id])
            finally:
                engine.close()

    def test_stale_final_attempt_is_terminalized_instead_of_stuck_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, job_id = self._queued_engine(directory)
            try:
                for attempt in range(1, 4):
                    self.assertTrue(engine.mark_compilation_running(job_id))
                    if attempt < 3:
                        self.assertEqual(
                            engine.mark_compilation_failed(
                                job_id,
                                f"attempt_{attempt}_failed",
                                retryable=True,
                            ),
                            "failed",
                        )
                engine._conn.execute(
                    "UPDATE memory_compiler_jobs SET updated_at = ? WHERE id = ?",
                    (time.time() - 1_000, job_id),
                )
                engine._conn.commit()

                self.assertEqual(
                    engine._memory.next_jobs(stale_after_seconds=10),
                    [],
                )
                self.assertFalse(engine._conn.in_transaction)
                state = engine._conn.execute(
                    "SELECT status, attempts, last_error FROM memory_compiler_jobs"
                ).fetchone()
                self.assertEqual(
                    tuple(state),
                    (
                        "exhausted",
                        3,
                        "worker_interrupted_after_final_attempt",
                    ),
                )
            finally:
                engine.close()

    def test_late_failure_cannot_revive_an_obsolete_source_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, old_job_id = self._queued_engine(directory)
            try:
                old_attempt = engine.claim_compilation_attempt(old_job_id)
                self.assertEqual(old_attempt, 1)
                replacement = engine.store_session(
                    [
                        {
                            "role": "user",
                            "content": "The replacement project codename is aurora.",
                        }
                    ],
                    session_id="session-1",
                )
                new_job_id = engine.enqueue_compilation(
                    replacement["session_pk"],
                    replacement["source_hash"],
                    "test-config:v1",
                )
                self.assertNotEqual(new_job_id, old_job_id)
                self.assertEqual(
                    engine.mark_compilation_failed(
                        old_job_id,
                        "late_failure",
                        retryable=True,
                        expected_attempt=old_attempt,
                    ),
                    "stale",
                )
                states = engine._conn.execute(
                    "SELECT id, status FROM memory_compiler_jobs ORDER BY id"
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in states],
                    [(old_job_id, "obsolete"), (new_job_id, "pending")],
                )
            finally:
                engine.close()

    def test_reclaimed_attempt_rejects_the_older_worker_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, job_id = self._queued_engine(directory)
            try:
                first_attempt = engine.claim_compilation_attempt(job_id)
                self.assertEqual(first_attempt, 1)
                engine._conn.execute(
                    "UPDATE memory_compiler_jobs SET updated_at = ? WHERE id = ?",
                    (time.time() - 1_000, job_id),
                )
                engine._conn.commit()
                second_attempt = engine.claim_compilation_attempt(
                    job_id,
                    stale_after_seconds=10,
                )
                self.assertEqual(second_attempt, 2)

                result = _SuccessfulCompiler().compile_session(
                    build_compile_input(engine, 1)
                )
                stale = engine.apply_compilation(
                    job_id,
                    result.memory,
                    processor="stale-test",
                    processor_version="v1",
                    prompt_version="test",
                    expected_attempt=first_attempt,
                )
                self.assertEqual(stale["status"], "stale")
                self.assertEqual(engine.enrichment_status()["claim_count"], 0)
                state = engine._conn.execute(
                    "SELECT status, attempts FROM memory_compiler_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                self.assertEqual(tuple(state), ("running", 2))

                applied = engine.apply_compilation(
                    job_id,
                    result.memory,
                    processor="current-test",
                    processor_version="v2",
                    prompt_version="test",
                    expected_attempt=second_attempt,
                )
                self.assertEqual(applied["status"], "complete")
                self.assertEqual(engine.enrichment_status()["claim_count"], 1)
            finally:
                engine.close()

    def test_heartbeat_prevents_reclaim_of_a_healthy_long_compile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, job_id = self._queued_engine(directory)
            compiler = _BlockingBulkCompiler()
            runner = EnrichmentRunner(
                engine,
                compiler,
                heartbeat_seconds=0.05,
            )
            outcomes = []
            worker = threading.Thread(
                target=lambda: outcomes.append(
                    runner.run_job(engine.pending_compilations(limit=1)[0])
                )
            )
            try:
                worker.start()
                self.assertTrue(compiler.started.wait(timeout=5))
                time.sleep(1.2)
                self.assertEqual(
                    engine._memory.next_jobs(stale_after_seconds=1),
                    [],
                )
                state = engine._conn.execute(
                    "SELECT status, attempts FROM memory_compiler_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                self.assertEqual(tuple(state), ("running", 1))

                compiler.release.set()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(outcomes), 1)
                self.assertTrue(outcomes[0]["ok"])
                self.assertEqual(outcomes[0]["status"], "complete")
            finally:
                compiler.release.set()
                worker.join(timeout=5)
                engine.close()

    def test_background_worker_drains_a_queued_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, _ = self._queued_engine(directory)
            worker = BackgroundEnricher(
                EnrichmentRunner(engine, _SuccessfulCompiler()),
                poll_seconds=0.05,
            )
            try:
                status = worker.drain(timeout=5)
                self.assertEqual(status["jobs"], {"complete": 1})
            finally:
                worker.close()
                engine.close()

    def test_background_worker_owns_connection_during_foreground_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, _ = self._queued_engine(directory)
            compiler = _BlockingBulkCompiler()
            apply_started = threading.Event()
            allow_apply = threading.Event()
            worker = BackgroundEnricher(
                EnrichmentRunner(engine, compiler),
                poll_seconds=0.05,
            )
            try:
                self.assertTrue(compiler.started.wait(timeout=5))
                worker_engine = worker._worker_engine
                self.assertIsNotNone(worker_engine)
                self.assertIsNot(worker_engine, engine)
                self.assertIsNot(worker_engine._conn, engine._conn)

                original_apply = worker_engine.apply_compilation

                def gated_apply(*args, **kwargs):
                    apply_started.set()
                    if not allow_apply.wait(timeout=5):
                        raise TimeoutError("test apply was not released")
                    return original_apply(*args, **kwargs)

                worker_engine.apply_compilation = gated_apply
                compiler.release.set()
                self.assertTrue(apply_started.wait(timeout=5))

                # Release a large derived write at the same time as a
                # foreground raw transaction on the caller-owned connection.
                allow_apply.set()
                stored = engine.store_batch(
                    [
                        {
                            "speaker": "user",
                            "text": f"Foreground record {index} is still available.",
                        }
                        for index in range(120)
                    ]
                )
                self.assertEqual(stored, 120)
                self.assertTrue(engine.search("foreground record", limit=10).messages)
                self.assertGreaterEqual(engine.stats()["message_count"], 121)

                status = worker.drain(timeout=10)
                self.assertEqual(status["jobs"], {"complete": 1})
                self.assertEqual(status["claim_count"], 128)
                self.assertTrue(engine.health_check()["ok"])
            finally:
                compiler.release.set()
                allow_apply.set()
                worker.close()
                engine.close()

    def test_background_worker_rejects_unshareable_memory_database(self) -> None:
        engine = Engine(":memory:", user_id="memory-worker", semantic_dedup=False)
        try:
            with self.assertRaisesRegex(ValueError, "file-backed Engine"):
                BackgroundEnricher(EnrichmentRunner(engine, _SuccessfulCompiler()))
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
