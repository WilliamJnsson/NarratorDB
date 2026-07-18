"""Focused tests for explicit private/intelligence project configuration."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from narratordb import (
    CapturePolicy,
    CompilerConfig,
    ConfigurationError,
    ConfigurationRequiredError,
    Engine,
    MemoryMode,
    ModeConflictError,
    NarratorDB,
    ProjectConfig,
    __version__,
)
from narratordb.compiler import (
    CompileResult,
    CompiledClaim,
    CompiledMemory,
    CompiledSummary,
    CompilerResponseError,
    CompilerTransportError,
    EvidenceSpan,
)
from narratordb.cli import _compiler_from_args, build_parser
from narratordb.config import (
    DEFAULT_OUTPUT_TOKEN_PARAMETER,
    PROJECT_CONFIG_VERSION,
    ProjectConfigStore,
)


ROOT = Path(__file__).resolve().parents[1]


class NarratorDBModeTests(unittest.TestCase):
    def test_project_config_v1_migrates_to_v3_capture_policy_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "config-v1.db")
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES(?, ?)",
                    (
                        ("narratordb.project.mode", "private"),
                        ("narratordb.project.compiler", ""),
                        ("narratordb.project.config_version", "1"),
                    ),
                )

            config = ProjectConfigStore(path).load()

            self.assertIsNotNone(config)
            assert config is not None
            self.assertEqual(config.config_version, PROJECT_CONFIG_VERSION)
            self.assertIs(config.capture_policy, CapturePolicy.PREFERENCES)
            with sqlite3.connect(path) as connection:
                stored_version = connection.execute(
                    "SELECT value FROM metadata WHERE key = ?",
                    ("narratordb.project.config_version",),
                ).fetchone()[0]
            self.assertEqual(stored_version, "3")

            def frozen_v1_loader(version: str) -> None:
                if int(version) != 1:
                    raise ConfigurationError("unsupported project config version")

            with self.assertRaisesRegex(ConfigurationError, "unsupported"):
                frozen_v1_loader(stored_version)

    def test_future_project_config_version_is_rejected_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "config-future.db")
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES(?, ?)",
                    (
                        ("narratordb.project.mode", "private"),
                        ("narratordb.project.compiler", ""),
                        ("narratordb.project.config_version", "999"),
                    ),
                )

            with self.assertRaisesRegex(ConfigurationError, "version 999"):
                ProjectConfigStore(path).load()
            with sqlite3.connect(path) as connection:
                stored_version = connection.execute(
                    "SELECT value FROM metadata WHERE key = ?",
                    ("narratordb.project.config_version",),
                ).fetchone()[0]
            self.assertEqual(stored_version, "999")

    def test_new_database_requires_explicit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "new.db")
            with self.assertRaises(ConfigurationRequiredError):
                NarratorDB(db_path=path)
            self.assertFalse(Path(path).exists())

    def test_private_mode_persists_and_reopens_without_reselection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "private.db")
            with NarratorDB(
                db_path=path, mode="private", semantic_dedup=False
            ) as memory:
                self.assertIs(memory.mode, MemoryMode.PRIVATE)
                memory.remember("raw private memory")

            with NarratorDB(db_path=path, semantic_dedup=False) as reopened:
                self.assertIs(reopened.mode, MemoryMode.PRIVATE)
                self.assertIs(reopened.capture_policy, CapturePolicy.PREFERENCES)
                self.assertIn(
                    "raw private memory", reopened.recall("private memory").text
                )

    def test_capture_policy_round_trips_and_survives_mode_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "capture-policy.db")
            compiler = CompilerConfig.local(
                model="capture-policy-test",
                endpoint="http://127.0.0.1:11434/v1",
            )
            with NarratorDB(
                db_path=path,
                mode="private",
                capture_policy="manual",
            ) as memory:
                self.assertIs(memory.capture_policy, CapturePolicy.MANUAL)
                memory.set_capture_policy("sessions")
                memory.set_mode("intelligence", compiler=compiler)
                self.assertIs(memory.capture_policy, CapturePolicy.SESSIONS)
                memory.set_mode("private", derived_data="retain")
                self.assertIs(memory.capture_policy, CapturePolicy.SESSIONS)

            with NarratorDB(db_path=path) as reopened:
                self.assertIs(reopened.capture_policy, CapturePolicy.SESSIONS)
                self.assertEqual(
                    reopened.project_status()["capture_policy"], "sessions"
                )

    def test_session_ingest_queues_only_in_intelligence_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private_path = str(Path(directory) / "private-session.db")
            with NarratorDB(
                db_path=private_path,
                mode="private",
                semantic_dedup=False,
            ) as memory:
                result = memory.ingest_session(
                    [{"role": "user", "content": "The launch code is amber nine."}],
                    session_id="session-private",
                    occurred_at=1_700_000_000,
                )
                self.assertEqual(result.messages_stored, 1)
                self.assertIsNone(result.compiler_job_id)
                self.assertEqual(result.enrichment_status, "disabled")
                bundle = memory.recall_context("launch code", token_budget=100)
                self.assertEqual(bundle.mode, "private")
                self.assertIn("amber nine", bundle.text)

            intelligence_path = str(Path(directory) / "intelligence-session.db")
            compiler = CompilerConfig.local(
                model="local-test",
                endpoint="http://127.0.0.1:11434/v1",
            )
            with NarratorDB(
                db_path=intelligence_path,
                mode="intelligence",
                compiler=compiler,
                semantic_dedup=False,
            ) as memory:
                result = memory.ingest_session(
                    [{"role": "user", "content": "The project codename is firefly."}],
                    session_id="session-intelligence",
                )
                self.assertIsNotNone(result.compiler_job_id)
                self.assertEqual(result.enrichment_status, "queued")
                self.assertEqual(
                    memory.project_status()["enrichment"]["jobs"]["pending"], 1
                )

    def test_waited_enrichment_is_source_linked_and_private_mode_ignores_it(
        self,
    ) -> None:
        class FakeCompiler:
            fingerprint = "fake:compiler-v1"

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
                                text="The user's project codename is firefly.",
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
                    )
                )

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "compiled.db")
            compiler = CompilerConfig.local(
                model="local-test",
                endpoint="http://127.0.0.1:11434/v1",
            )
            with NarratorDB(
                db_path=path,
                mode="intelligence",
                compiler=compiler,
                semantic_dedup=False,
            ) as memory:
                memory._compiler_runtime = FakeCompiler()
                result = memory.ingest_session(
                    [{"role": "user", "content": "The project codename is firefly."}],
                    session_id="compiled-session",
                    wait_for_enrichment=True,
                )
                self.assertEqual(result.enrichment_status, "complete")
                self.assertEqual(result.enrichment["completed"], 1)
                intelligent = memory.recall_context(
                    "project codename", token_budget=300
                )
                self.assertIn("claim:", intelligent.text)
                self.assertTrue(
                    any(
                        "score_fusion" in block.channels for block in intelligent.blocks
                    )
                )

                memory.set_mode("private", derived_data="retain")
                private = memory.recall_context("project codename", token_budget=300)
                self.assertEqual(private.mode, "private")
                self.assertNotIn("claim:", private.text)
                self.assertIn("firefly", private.text)

    def test_waited_exact_retry_reports_terminal_job_instead_of_idle(self) -> None:
        class TerminalCompiler:
            fingerprint = "fake:terminal-compiler-v1"

            def compile_session(self, session):
                raise CompilerResponseError(
                    "injected terminal response",
                    code="injected_terminal",
                    retryable=False,
                )

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "terminal.db")
            with NarratorDB(
                db_path=path,
                mode="intelligence",
                compiler=CompilerConfig.local(
                    model="local-test",
                    endpoint="http://127.0.0.1:11434/v1",
                ),
                semantic_dedup=False,
            ) as memory:
                memory._compiler_runtime = TerminalCompiler()
                first = memory.ingest_session(
                    [{"role": "user", "content": "The launch code is amber nine."}],
                    session_id="terminal-session",
                    wait_for_enrichment=True,
                )
                self.assertEqual(first.enrichment_status, "failed")
                self.assertEqual(first.enrichment["failed"], 1)
                self.assertEqual(
                    first.enrichment["jobs"][0]["status"],
                    "blocked",
                )

                retry = memory.ingest_session(
                    [{"role": "user", "content": "The launch code is amber nine."}],
                    session_id="terminal-session",
                    wait_for_enrichment=True,
                )
                self.assertEqual(retry.enrichment_status, "failed")
                self.assertEqual(retry.enrichment["processed"], 0)
                self.assertEqual(retry.enrichment["failed"], 1)
                self.assertEqual(
                    retry.enrichment["jobs"][0]["error_code"],
                    "injected_terminal",
                )
                self.assertEqual(
                    memory.project_status()["enrichment"]["jobs"],
                    {"blocked": 1},
                )
                self.assertEqual(memory.stats()["message_count"], 1)

    def test_waited_exact_retry_reports_durable_cooldown_as_deferred(self) -> None:
        class DeferredCompiler:
            fingerprint = "fake:deferred-compiler-v1"

            def compile_session(self, session):
                raise CompilerTransportError(
                    "injected safe rate limit",
                    code="http_429",
                    retryable=True,
                    status=429,
                    retry_after_seconds=60.0,
                )

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "deferred.db")
            with NarratorDB(
                db_path=path,
                mode="intelligence",
                compiler=CompilerConfig.local(
                    model="local-test",
                    endpoint="http://127.0.0.1:11434/v1",
                ),
                semantic_dedup=False,
            ) as memory:
                memory._compiler_runtime = DeferredCompiler()
                first = memory.ingest_session(
                    [{"role": "user", "content": "Synthetic cooldown session."}],
                    session_id="deferred-session",
                    wait_for_enrichment=True,
                )
                self.assertEqual(first.enrichment_status, "failed")

                retry = memory.ingest_session(
                    [{"role": "user", "content": "Synthetic cooldown session."}],
                    session_id="deferred-session",
                    wait_for_enrichment=True,
                )

                self.assertEqual(retry.enrichment_status, "deferred")
                self.assertEqual(retry.enrichment["processed"], 0)
                self.assertEqual(retry.enrichment["failed"], 0)
                detail = retry.enrichment["jobs"][0]
                self.assertEqual(detail["status"], "deferred")
                self.assertTrue(detail["retryable"])
                self.assertGreater(detail["retry_after_seconds"], 0)
                self.assertGreater(detail["next_attempt_at"], time.time())

    def test_facade_cache_reuses_compilation_and_project_purge_covers_all_scopes(
        self,
    ) -> None:
        class CountingCompiler:
            fingerprint = "fake:cache-compiler-v1"

            def __init__(self):
                self.calls = 0

            def compile_session(self, session):
                self.calls += 1
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
                    )
                )

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "cached.db")
            compiler = CountingCompiler()
            with patch(
                "narratordb.compiler.compiler_from_project_config",
                return_value=compiler,
            ):
                with NarratorDB(
                    db_path=path,
                    user_id="alpha",
                    mode="intelligence",
                    compiler=CompilerConfig.openrouter(),
                    semantic_dedup=False,
                ) as memory:
                    for user_id in ("alpha", "beta"):
                        result = memory.ingest_session(
                            [
                                {
                                    "role": "user",
                                    "content": "The project codename is firefly.",
                                }
                            ],
                            session_id=f"session-{user_id}",
                            occurred_at=1_700_000_000,
                            user_id=user_id,
                            wait_for_enrichment=True,
                        )
                        self.assertEqual(
                            result.enrichment_status,
                            "complete",
                            result.enrichment,
                        )

                    self.assertEqual(compiler.calls, 1)
                    self.assertEqual(memory._compiler_cache.stats().hits, 1)
                    purged = memory.purge_derived()
                    self.assertEqual(purged["deleted"], 2)
                    self.assertEqual(purged["scopes_purged"], 2)
                    self.assertEqual(purged["compiler_cache_entries_deleted"], 1)
                    self.assertEqual(
                        memory.project_status(user_id="alpha")["enrichment"][
                            "claim_count"
                        ],
                        0,
                    )
                    self.assertEqual(
                        memory.project_status(user_id="beta")["enrichment"][
                            "claim_count"
                        ],
                        0,
                    )
                    self.assertIn(
                        "firefly", memory.recall("codename", user_id="beta").text
                    )

                    recompiled = memory.ingest_session(
                        [
                            {
                                "role": "user",
                                "content": "The project codename is firefly.",
                            }
                        ],
                        session_id="session-beta",
                        occurred_at=1_700_000_000,
                        user_id="beta",
                        wait_for_enrichment=True,
                    )
                    self.assertEqual(recompiled.enrichment_status, "complete")
                    self.assertEqual(memory._compiler_cache.stats().entries, 1)
                    self.assertEqual(
                        memory.forget(
                            user_id="beta",
                            message_id=recompiled.message_ids[0],
                        ),
                        1,
                    )
                    self.assertEqual(memory._compiler_cache.stats().entries, 0)

                    rebuilt = memory.ingest_session(
                        [
                            {
                                "role": "user",
                                "content": "The project codename is firefly.",
                            }
                        ],
                        session_id="session-beta-rebuilt",
                        occurred_at=1_700_000_001,
                        user_id="beta",
                        wait_for_enrichment=True,
                    )
                    self.assertEqual(rebuilt.enrichment_status, "complete")
                    self.assertEqual(memory._compiler_cache.stats().entries, 1)
                    self.assertEqual(memory.forget(user_id="beta"), 1)
                    self.assertEqual(memory._compiler_cache.stats().entries, 0)

    def test_hosted_production_runtime_has_only_an_explicit_cost_quota(self) -> None:
        class FakeCompiler:
            fingerprint = "fake:hosted-runtime-v1"

            def compile_session(self, session):
                raise AssertionError("cost-policy test must not compile a session")

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "hosted-cost.db")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("NARRATORDB_COMPILER_MAX_COST_USD", None)
                with patch(
                    "narratordb.compiler.compiler_from_project_config",
                    return_value=FakeCompiler(),
                ):
                    with NarratorDB(
                        db_path=path,
                        mode="intelligence",
                        compiler=CompilerConfig.openrouter(),
                    ) as memory:
                        memory._get_compiler_runtime()
                        self.assertIsNone(memory._compiler_usage_ledger.max_cost_usd)
                        self.assertEqual(
                            memory._compiler_usage_ledger.request_reservation_usd,
                            0.0,
                        )
                        self.assertEqual(
                            memory._compiler_usage_ledger.safety_reserve_usd,
                            0.0,
                        )

            with patch.dict(
                os.environ,
                {"NARRATORDB_COMPILER_MAX_COST_USD": "37.5"},
                clear=False,
            ):
                os.environ.pop("NARRATORDB_COMPILER_REQUEST_RESERVATION_USD", None)
                os.environ.pop("NARRATORDB_COMPILER_BUDGET_SAFETY_RESERVE_USD", None)
                with patch(
                    "narratordb.compiler.compiler_from_project_config",
                    return_value=FakeCompiler(),
                ):
                    with NarratorDB(db_path=path) as memory:
                        memory._get_compiler_runtime()
                        self.assertEqual(
                            memory._compiler_usage_ledger.max_cost_usd,
                            37.5,
                        )
                        self.assertEqual(
                            memory._compiler_usage_ledger.request_reservation_usd,
                            0.05,
                        )
                        self.assertEqual(
                            memory._compiler_usage_ledger.safety_reserve_usd,
                            1.0,
                        )

            for invalid in ("inf", "-inf", "nan"):
                with (
                    self.subTest(invalid=invalid),
                    patch.dict(
                        os.environ,
                        {"NARRATORDB_COMPILER_MAX_COST_USD": invalid},
                        clear=False,
                    ),
                ):
                    with NarratorDB(db_path=path) as memory:
                        with self.assertRaises(ConfigurationError):
                            memory._get_compiler_runtime()

            with patch.dict(
                os.environ,
                {
                    "NARRATORDB_COMPILER_MAX_COST_USD": "37.5",
                    "NARRATORDB_COMPILER_REQUEST_RESERVATION_USD": "nan",
                },
                clear=False,
            ):
                with NarratorDB(db_path=path) as memory:
                    with self.assertRaises(ConfigurationError):
                        memory._get_compiler_runtime()

    def test_cli_wires_production_route_retry_and_pacing_policy(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "init",
                "--mode",
                "intelligence",
                "--compiler",
                "openrouter",
                "--provider-allow",
                "parasail/fp4,wafer/fp4,deepinfra/fp4",
                "--semantic-max-attempts",
                "1",
                "--transport-max-attempts",
                "1",
                "--retry-delay-seconds",
                "2",
                "--min-request-interval-seconds",
                "10",
                "--capture-router-metadata",
            ]
        )

        config = _compiler_from_args(args, MemoryMode.INTELLIGENCE)

        self.assertEqual(
            config.provider_allowlist,
            ("parasail/fp4", "wafer/fp4", "deepinfra/fp4"),
        )
        self.assertTrue(config.allow_fallbacks)
        self.assertEqual(config.semantic_max_attempts, 1)
        self.assertEqual(config.transport_max_attempts, 1)
        self.assertEqual(config.retry_delay_seconds, 2.0)
        self.assertEqual(config.min_request_interval_seconds, 10.0)
        self.assertTrue(config.capture_router_metadata)

        private = parser.parse_args(
            ["init", "--mode", "private", "--provider-allow", "parasail/fp4"]
        )
        with self.assertRaises(ConfigurationError):
            _compiler_from_args(private, MemoryMode.PRIVATE)

        for compiler_option in (
            ["--max-output-tokens", "8192"],
            ["--output-token-parameter", DEFAULT_OUTPUT_TOKEN_PARAMETER],
        ):
            with self.subTest(private_compiler_option=compiler_option):
                private = parser.parse_args(
                    ["init", "--mode", "private", *compiler_option]
                )
                with self.assertRaises(ConfigurationError):
                    _compiler_from_args(private, MemoryMode.PRIVATE)

        local = parser.parse_args(
            [
                "init",
                "--mode",
                "intelligence",
                "--compiler",
                "local",
                "--model",
                "local-test",
                "--endpoint",
                "http://localhost:11434/v1",
                "--capture-router-metadata",
            ]
        )
        with self.assertRaises(ConfigurationError):
            _compiler_from_args(local, MemoryMode.INTELLIGENCE)

    def test_cli_wires_codex_subscription_compiler_without_route_options(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "init",
                "--mode",
                "intelligence",
                "--compiler",
                "codex-cli",
                "--model",
                "gpt-5.4-mini",
                "--reasoning",
                "low",
                "--codex-cli-version",
                "codex-cli 0.144.4",
                "--codex-timeout-seconds",
                "240",
                "--codex-max-invocations",
                "100",
                "--codex-max-concurrency",
                "2",
                "--semantic-max-attempts",
                "2",
                "--retry-delay-seconds",
                "1",
                "--min-request-interval-seconds",
                "0.5",
            ]
        )

        config = _compiler_from_args(args, MemoryMode.INTELLIGENCE)

        self.assertEqual(config.kind.value, "codex-cli")
        self.assertEqual(config.model, "gpt-5.4-mini")
        self.assertEqual(config.reasoning, "low")
        self.assertEqual(config.codex_cli_version, "codex-cli 0.144.4")
        self.assertEqual(config.codex_timeout_seconds, 240.0)
        self.assertEqual(config.codex_max_invocations, 100)
        self.assertEqual(config.codex_max_concurrency, 2)
        self.assertEqual(config.semantic_max_attempts, 2)
        self.assertEqual(config.retry_delay_seconds, 1.0)
        self.assertEqual(config.min_request_interval_seconds, 0.5)
        self.assertNotIn("executable", config.to_dict())
        self.assertNotIn("codex_executable", config.to_dict())
        self.assertNotIn("codex_home", config.to_dict())

        for invalid_option in (
            ["--provider", "Azure"],
            ["--endpoint", "http://127.0.0.1:11434/v1"],
            ["--transport-max-attempts", "1"],
            ["--capture-router-metadata"],
            ["--max-output-tokens", "4096"],
            ["--max-output-tokens", "8192"],
            ["--output-token-parameter", DEFAULT_OUTPUT_TOKEN_PARAMETER],
        ):
            with self.subTest(invalid_option=invalid_option):
                invalid = parser.parse_args(
                    [
                        "init",
                        "--mode",
                        "intelligence",
                        "--compiler",
                        "codex-cli",
                        *invalid_option,
                    ]
                )
                with self.assertRaises(ConfigurationError):
                    _compiler_from_args(invalid, MemoryMode.INTELLIGENCE)

    def test_cli_rejects_zero_retry_attempts_instead_of_replacing_them(self) -> None:
        parser = build_parser()
        cases = (
            ("openai", "--semantic-max-attempts"),
            ("openai", "--transport-max-attempts"),
            ("openrouter", "--semantic-max-attempts"),
            ("openrouter", "--transport-max-attempts"),
            ("codex-cli", "--semantic-max-attempts"),
        )
        for compiler, option in cases:
            with self.subTest(compiler=compiler, option=option):
                args = parser.parse_args(
                    [
                        "init",
                        "--mode",
                        "intelligence",
                        "--compiler",
                        compiler,
                        option,
                        "0",
                    ]
                )
                with self.assertRaises(ConfigurationError):
                    _compiler_from_args(args, MemoryMode.INTELLIGENCE)

    def test_facade_never_downloads_embeddings_and_intelligence_uses_hybrid_search(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private_path = str(Path(directory) / "private-local.db")
            with (
                patch.dict("sys.modules", {"numpy": object()}),
                patch(
                    "narratordb.engine._load_sentence_transformer",
                    return_value=(None, None),
                ) as load,
            ):
                with NarratorDB(db_path=private_path, mode="private") as memory:
                    engine = memory._get_engine()
                    self.assertEqual(engine.semantic_search_mode, "fallback_only")
                load.assert_called_once_with(local_only=True)

            intelligence_path = str(Path(directory) / "intelligence-local.db")
            with (
                patch.dict("sys.modules", {"numpy": object()}),
                patch(
                    "narratordb.engine._load_sentence_transformer",
                    return_value=(None, None),
                ) as load,
            ):
                with NarratorDB(
                    db_path=intelligence_path,
                    mode="intelligence",
                    compiler=CompilerConfig.local(
                        model="local-test",
                        endpoint="http://127.0.0.1:11434/v1",
                    ),
                ) as memory:
                    engine = memory._get_engine()
                    self.assertEqual(engine.semantic_search_mode, "hybrid")
                load.assert_called_once_with(local_only=True)

    def test_existing_v1_database_migrates_to_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "legacy.db")
            with Engine(path, user_id="legacy", semantic_dedup=False) as engine:
                engine.remember("legacy raw memory")

            with NarratorDB(
                db_path=path,
                user_id="legacy",
                semantic_dedup=False,
            ) as migrated:
                self.assertIs(migrated.mode, MemoryMode.PRIVATE)
                self.assertEqual(migrated.project_config.migrated_from, "legacy-1.x")

            with self.assertRaises(ModeConflictError):
                NarratorDB(
                    db_path=path,
                    mode="intelligence",
                    compiler=CompilerConfig.local(
                        model="test-model",
                        endpoint="http://127.0.0.1:11434/v1",
                    ),
                )

    def test_intelligence_requires_compiler_and_stores_no_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "intelligence.db")
            with self.assertRaises(ConfigurationError):
                NarratorDB(db_path=path, mode="intelligence")

            compiler = CompilerConfig.openrouter()
            with NarratorDB(
                db_path=path, mode="intelligence", compiler=compiler
            ) as memory:
                status = memory.project_status()
                self.assertEqual(status["mode"], "intelligence")
                self.assertEqual(status["compiler"]["kind"], "openrouter")
                serialized = json.dumps(status).lower()
                for forbidden in ("api_key", "authorization", "password", "secret"):
                    self.assertNotIn(forbidden, serialized)

    def test_local_compiler_rejects_non_loopback_endpoint(self) -> None:
        with self.assertRaises(ConfigurationError):
            CompilerConfig.local(endpoint="https://models.example.com/v1")
        local = CompilerConfig.local(endpoint="http://127.0.0.1:11434/v1")
        self.assertEqual(local.endpoint, "http://127.0.0.1:11434/v1")

    def test_local_intelligence_requires_model_and_loopback_endpoint(self) -> None:
        incomplete = (
            CompilerConfig.local(),
            CompilerConfig.local(model="local-test"),
            CompilerConfig.local(endpoint="http://127.0.0.1:11434/v1"),
        )
        for compiler in incomplete:
            with self.subTest(compiler=compiler.to_dict()):
                with self.assertRaisesRegex(
                    ConfigurationError,
                    "requires both a model and a loopback HTTP endpoint",
                ):
                    ProjectConfig(mode="intelligence", compiler=compiler)

        configured = ProjectConfig(
            mode="intelligence",
            compiler=CompilerConfig.local(
                model="local-test",
                endpoint="http://localhost:11434/v1",
            ),
        )
        self.assertEqual(configured.compiler.model, "local-test")

    def test_mode_change_requires_explicit_derived_data_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "switch.db")
            with NarratorDB(db_path=path, mode="private") as memory:
                engine = memory._get_engine()
                self.assertEqual(engine.semantic_search_mode, "fallback_only")
                memory.set_mode(
                    "intelligence",
                    compiler=CompilerConfig.local(
                        model="local-test",
                        endpoint="http://127.0.0.1:11434/v1",
                    ),
                )
                self.assertIs(memory.mode, MemoryMode.INTELLIGENCE)
                self.assertEqual(engine.semantic_search_mode, "hybrid")
                with self.assertRaises(ConfigurationError):
                    memory.set_mode("private")
                memory.set_mode("private", derived_data="retain")
                self.assertIs(memory.mode, MemoryMode.PRIVATE)
                self.assertEqual(engine.semantic_search_mode, "fallback_only")

            with NarratorDB(db_path=path) as reopened:
                self.assertIs(reopened.mode, MemoryMode.PRIVATE)
                self.assertIsNone(reopened.compiler_config)

    def test_compiler_change_obsoletes_incompatible_jobs_across_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "compiler-change.db")
            old = CompilerConfig.local(
                model="old-model",
                endpoint="http://127.0.0.1:11434/v1",
            )
            new = CompilerConfig.local(
                model="new-model",
                endpoint="http://127.0.0.1:11434/v1",
            )
            with NarratorDB(
                db_path=path,
                mode="intelligence",
                compiler=old,
                semantic_dedup=False,
            ) as memory:
                for user_id in ("alpha", "beta"):
                    result = memory.ingest_session(
                        [{"role": "user", "content": f"Queued source for {user_id}."}],
                        session_id=f"session-{user_id}",
                        user_id=user_id,
                    )
                    self.assertEqual(result.enrichment_status, "queued")

                # Ensure invalidation is database-wide, not limited to live
                # facade connections.
                memory._engines.pop("beta").close()

                memory.set_mode("intelligence", compiler=new)

                for user_id in ("alpha", "beta"):
                    engine = memory._get_engine(user_id)
                    self.assertEqual(
                        engine.enrichment_status()["jobs"], {"obsolete": 1}
                    )
                    self.assertEqual(engine.pending_compilations(), [])

    def test_environment_user_id_is_honored_by_facade(self) -> None:
        old = os.environ.get("NARRATORDB_USER_ID")
        try:
            os.environ["NARRATORDB_USER_ID"] = "environment-user"
            with tempfile.TemporaryDirectory() as directory:
                with NarratorDB(
                    db_path=str(Path(directory) / "env.db"),
                    mode="private",
                ) as memory:
                    self.assertEqual(
                        memory.project_status()["default_user_id"], "environment-user"
                    )
        finally:
            if old is None:
                os.environ.pop("NARRATORDB_USER_ID", None)
            else:
                os.environ["NARRATORDB_USER_ID"] = old

    def test_cli_init_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "cli.db")
            base = [sys.executable, "-m", "narratordb.cli", "--path", path, "--json"]
            initialized = subprocess.run(
                [*base, "init", "--mode", "private"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(initialized.stdout)["mode"], "private")
            self.assertEqual(
                json.loads(initialized.stdout)["capture_policy"], "preferences"
            )
            status = subprocess.run(
                [*base, "status"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(status.stdout)["mode"], "private")

            changed = subprocess.run(
                [*base, "capture-policy", "manual"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(changed.stdout)["capture_policy"], "manual")

    def test_cli_local_intelligence_requires_complete_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "cli-intelligence.db")
            base = [sys.executable, "-m", "narratordb.cli", "--path", path, "--json"]
            missing_endpoint = subprocess.run(
                [
                    *base,
                    "init",
                    "--mode",
                    "intelligence",
                    "--compiler",
                    "local",
                    "--model",
                    "local-test",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_endpoint.returncode, 2)
            self.assertIn(
                "requires both --model and --endpoint", missing_endpoint.stderr
            )
            self.assertFalse(Path(path).exists())

            initialized = subprocess.run(
                [*base, "init", "--mode", "private"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(initialized.stdout)["mode"], "private")
            invalid_change = subprocess.run(
                [
                    *base,
                    "mode",
                    "intelligence",
                    "--compiler",
                    "local",
                    "--endpoint",
                    "http://127.0.0.1:11434/v1",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(invalid_change.returncode, 2)
            self.assertIn("requires both --model and --endpoint", invalid_change.stderr)
            status = subprocess.run(
                [*base, "status"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(status.stdout)["mode"], "private")

    def test_public_version_matches_major_release(self) -> None:
        self.assertEqual(__version__, "2.2.1")


if __name__ == "__main__":
    unittest.main()
