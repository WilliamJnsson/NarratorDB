from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narratordb.benchmark_server import (
    NarratorDBBenchmarkBackend,
    _compiler_config_from_args,
    build_argument_parser,
    make_handler,
)
from narratordb.compiler import (
    CompileResult,
    CompileSessionInput,
    CompiledMemory,
    CompiledSummary,
)


class EmptyFixtureCompiler:
    fingerprint = "fixture-existing-derived-v1"

    def __init__(self) -> None:
        self.calls = 0

    def compile_session(self, session: CompileSessionInput) -> CompileResult:
        self.calls += 1
        return CompileResult(
            memory=CompiledMemory(
                session_id=session.session_id,
                summary=CompiledSummary(text="", evidence=()),
                claims=(),
                entities=(),
                relations=(),
            )
        )


def _payload() -> dict:
    return {
        "user_id": "alice",
        "timestamp": 1_700_000_000,
        "metadata": {"session_id": "session-1"},
        "messages": [
            {"role": "user", "content": "The durable marker is cobalt."},
            {"role": "assistant", "content": "I recorded the marker."},
        ],
    }


def _materialize(database: str) -> EmptyFixtureCompiler:
    compiler = EmptyFixtureCompiler()
    backend = NarratorDBBenchmarkBackend(
        database,
        mode="intelligence",
        compiler=compiler,
    )
    try:
        backend.add(_payload())
    finally:
        backend.close()
    return compiler


class ExistingDerivedReplayTests(unittest.TestCase):
    def test_replay_constructs_no_compiler_or_cache_and_requires_nonempty_scope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "narratordb.benchmark_server.compiler_from_project_config",
            side_effect=AssertionError("compiler construction is forbidden"),
        ), patch(
            "narratordb.benchmark_server.CompiledSessionCache",
            side_effect=AssertionError("cache construction is forbidden"),
        ):
            database = str(Path(directory) / "empty.db")
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                existing_derived_fingerprint="recorded-v7-fingerprint",
            )
            try:
                diagnostics = backend.existing_derived_diagnostics("new-run-user")
                self.assertFalse(diagnostics["ready"])
                self.assertEqual(diagnostics["registered_sessions"], 0)
                self.assertFalse(diagnostics["compiler_constructed"])
                self.assertFalse(diagnostics["compiler_cache_constructed"])
                with self.assertRaisesRegex(ValueError, "scope is not ready"):
                    backend.search(
                        {
                            "user_id": "new-run-user",
                            "query": "marker",
                            "limit": 10,
                        }
                    )
            finally:
                backend.close()
            self.assertFalse(Path(f"{database}.compiler-cache.sqlite3").exists())

    def test_terminal_current_sources_can_be_replayed_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "complete.db")
            compiler = _materialize(database)
            cache_path = Path(f"{database}.compiler-cache.sqlite3")
            cache_path.unlink()

            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                existing_derived_fingerprint=compiler.fingerprint,
            )
            try:
                diagnostics = backend.existing_derived_diagnostics("alice")
                self.assertTrue(diagnostics["ready"])
                self.assertEqual(diagnostics["registered_sessions"], 1)
                self.assertEqual(diagnostics["complete_sessions"], 1)
                self.assertEqual(diagnostics["partial_sessions"], 0)
                self.assertEqual(diagnostics["nonterminal_sessions"], 0)

                result = backend.search(
                    {"user_id": "alice", "query": "marker", "limit": 10}
                )
                replay = result["query_debug"]["existing_derived_replay"]
                self.assertTrue(replay["ready"])
                self.assertEqual(replay["compiler_fingerprint"], compiler.fingerprint)
                self.assertEqual(result["query_debug"]["lazy_finalized_sessions"], 0)
                self.assertTrue(result["results"])
                self.assertEqual(compiler.calls, 1)

                for operation in (
                    lambda: backend.add(_payload()),
                    lambda: backend.finalize("alice"),
                    lambda: backend.delete("alice"),
                ):
                    with self.assertRaisesRegex(
                        ValueError, "disabled in existing-derived replay mode"
                    ):
                        operation()
            finally:
                backend.close()
            self.assertFalse(cache_path.exists())

    def test_replay_fails_closed_on_fingerprint_or_lifecycle_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "mismatch.db")
            compiler = _materialize(database)

            wrong = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                existing_derived_fingerprint="different-fingerprint",
            )
            try:
                diagnostics = wrong.existing_derived_diagnostics("alice")
                self.assertFalse(diagnostics["ready"])
                self.assertEqual(diagnostics["nonterminal_statuses"], {"missing": 1})
                with self.assertRaisesRegex(ValueError, "scope is not ready"):
                    wrong.search(
                        {"user_id": "alice", "query": "marker", "limit": 10}
                    )
            finally:
                wrong.close()

            pending = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                existing_derived_fingerprint=compiler.fingerprint,
            )
            try:
                engine = pending.engine("alice")
                engine._conn.execute(
                    "UPDATE memory_compiler_jobs SET status = 'pending' "
                    "WHERE user_id = ?",
                    ("alice",),
                )
                engine._conn.commit()
                diagnostics = pending.existing_derived_diagnostics("alice")
                self.assertFalse(diagnostics["ready"])
                self.assertEqual(diagnostics["nonterminal_statuses"], {"pending": 1})
                with self.assertRaisesRegex(ValueError, "scope is not ready"):
                    pending.search(
                        {"user_id": "alice", "query": "marker", "limit": 10}
                    )
            finally:
                pending.close()

    def test_partial_is_an_explicit_terminal_replay_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "partial.db")
            compiler = _materialize(database)
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                existing_derived_fingerprint=compiler.fingerprint,
            )
            try:
                engine = backend.engine("alice")
                engine._conn.execute(
                    "UPDATE memory_compiler_jobs SET status = 'partial' "
                    "WHERE user_id = ?",
                    ("alice",),
                )
                engine._conn.commit()
                diagnostics = backend.existing_derived_diagnostics("alice")
                self.assertTrue(diagnostics["ready"])
                self.assertEqual(diagnostics["complete_sessions"], 0)
                self.assertEqual(diagnostics["partial_sessions"], 1)
                backend.search(
                    {"user_id": "alice", "query": "marker", "limit": 10}
                )
            finally:
                backend.close()

    def test_http_diagnostics_are_content_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "diagnostics.db")
            compiler = _materialize(database)
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                existing_derived_fingerprint=compiler.fingerprint,
            )
            try:
                handler = object.__new__(make_handler(backend))
                responses = []
                handler.path = "/replay/diagnostics?user_id=alice"
                handler.send_json = lambda status, payload: responses.append(
                    (int(status), payload)
                )
                handler.do_GET()
                status, payload = responses[-1]
                self.assertEqual(status, 200)
                self.assertTrue(payload["ready"])
                self.assertEqual(payload["registered_sessions"], 1)
                self.assertNotIn("user_id", payload)
                self.assertNotIn("session_id", payload)
                self.assertNotIn("messages", payload)
            finally:
                backend.close()

    def test_cli_replay_is_mutually_exclusive_with_compiler_options(self) -> None:
        parser = build_argument_parser()
        args = parser.parse_args(
            [
                "--mode",
                "intelligence",
                "--existing-derived-replay-fingerprint",
                "recorded-v7-fingerprint",
            ]
        )
        self.assertIsNone(_compiler_config_from_args(args, parser))

        conflicting = parser.parse_args(
            [
                "--mode",
                "intelligence",
                "--existing-derived-replay-fingerprint",
                "recorded-v7-fingerprint",
                "--compiler",
                "openrouter",
                "--compiler-max-cost-usd",
                "1",
            ]
        )
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                _compiler_config_from_args(conflicting, parser)

        private = parser.parse_args(
            [
                "--existing-derived-replay-fingerprint",
                "recorded-v7-fingerprint",
            ]
        )
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                _compiler_config_from_args(private, parser)


if __name__ == "__main__":
    unittest.main()
