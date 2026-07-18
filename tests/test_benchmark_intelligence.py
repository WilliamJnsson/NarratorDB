from __future__ import annotations

import contextlib
import io
import sqlite3
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
    CompiledClaim,
    CompiledMemory,
    CompiledSummary,
    CompilerConfigurationError,
    CompilerResponseError,
    EvidenceSpan,
)
from narratordb.compiler_cache import CompiledSessionCache
from narratordb.engine import Message, SearchResult
from narratordb.intelligence import ClaimSource, MemoryClaim
from narratordb.intelligence import _concise_excerpt


class FakeCompiler:
    fingerprint = "fake-compiler-v1"

    def __init__(self, database: str | None = None) -> None:
        self.database = database
        self.calls: list[CompileSessionInput] = []
        self.raw_counts_during_compile: list[int] = []

    def compile_session(self, session: CompileSessionInput) -> CompileResult:
        self.calls.append(session)
        if self.database is not None:
            with sqlite3.connect(self.database) as connection:
                self.raw_counts_during_compile.append(
                    int(
                        connection.execute("SELECT COUNT(*) FROM messages").fetchone()[
                            0
                        ]
                    )
                )
        source = session.messages[0]
        evidence = EvidenceSpan(
            message_id=source.message_id,
            quote=source.content,
            start=0,
            end=len(source.content),
        )
        return CompileResult(
            memory=CompiledMemory(
                session_id=session.session_id,
                summary=CompiledSummary(
                    text="The session contains an access credential.",
                    evidence=(evidence,),
                ),
                claims=(
                    CompiledClaim(
                        claim_id="credential",
                        kind="fact",
                        text="The access credential is cobalt seven.",
                        confidence=1.0,
                        status="active",
                        document_time=session.document_time,
                        event_start=None,
                        event_end=None,
                        valid_from=None,
                        valid_to=None,
                        entity_ids=(),
                        evidence=(evidence,),
                    ),
                ),
                entities=(),
                relations=(),
            )
        )


class FailingCompiler:
    fingerprint = "failing-compiler-v1"

    def compile_session(self, session: CompileSessionInput) -> CompileResult:
        raise CompilerConfigurationError("injected failure", code="injected_failure")


class RetryableFailingCompiler:
    fingerprint = "retryable-failing-compiler-v1"

    def compile_session(self, session: CompileSessionInput) -> CompileResult:
        raise CompilerResponseError(
            "injected incomplete completion",
            code="incomplete_completion",
        )


class ContentFilteredCompiler:
    fingerprint = "content-filtered-compiler-v1"

    def __init__(self) -> None:
        self.calls: list[CompileSessionInput] = []

    def compile_session(self, session: CompileSessionInput) -> CompileResult:
        self.calls.append(session)
        raise CompilerResponseError(
            "injected content filter",
            code="content_filtered",
            retryable=False,
        )


def _payload(user_id: str) -> dict:
    return {
        "user_id": user_id,
        "timestamp": 1_700_000_000,
        "metadata": {"session_id": "session-1"},
        "messages": [
            {"role": "user", "content": "The launch code is cobalt seven."},
            {"role": "assistant", "content": "I will remember the launch code."},
        ],
    }


def _pair_payload(user_id: str, timestamp: int, label: str) -> dict:
    return {
        "user_id": user_id,
        "timestamp": timestamp,
        "messages": [
            {
                "role": "user",
                "content": f"The {label} access credential is cobalt seven.",
            },
            {
                "role": "assistant",
                "content": f"I will remember the {label} credential.",
            },
        ],
    }


class BenchmarkIntelligenceTests(unittest.TestCase):
    def test_intelligence_commits_raw_then_compiles_and_recalls_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "intelligence.db")
            compiler = FakeCompiler(database)
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=compiler,
                context_token_budget=512,
            )
            try:
                added = backend.add(_payload("alice"))
                self.assertEqual(len(added["results"]), 2)
                self.assertEqual(compiler.raw_counts_during_compile, [2])
                self.assertEqual(len(compiler.calls), 1)
                self.assertEqual(compiler.calls[0].session_id, "session-1")

                result = backend.search(
                    {"user_id": "alice", "query": "access credential", "limit": 10}
                )
                self.assertEqual(result["query_debug"]["mode"], "intelligence")
                self.assertTrue(result["results"])
                self.assertIn(
                    "access credential is cobalt seven",
                    "\n".join(item["memory"] for item in result["results"]),
                )
                for item in result["results"]:
                    self.assertEqual(set(item), {"id", "memory", "score", "created_at"})
                self.assertEqual(backend._compiler_cache.stats().entries, 1)
                self.assertEqual(backend.delete("alice"), {"deleted": 2})
                self.assertEqual(backend._compiler_cache.stats().entries, 0)
            finally:
                backend.close()

    def test_intelligence_top_k_is_independent_of_render_token_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "budgetless-top-k.db")
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=FakeCompiler(database),
                context_token_budget=128,
                merge_max_chars=72,
            )
            payload = {
                "user_id": "alice",
                "timestamp": 1_700_000_000,
                "metadata": {"session_id": "bulk-session"},
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Budget marker archive evidence {index}: "
                            f"the unique value is item-{index}."
                        ),
                    }
                    for index in range(70)
                ],
            }
            try:
                self.assertEqual(len(backend.add(payload)["results"]), 70)
                result = backend.search(
                    {
                        "user_id": "alice",
                        "query": "budget marker archive evidence",
                        "limit": 50,
                    }
                )
                self.assertEqual(len(result["results"]), 50)
                self.assertTrue(
                    all(len(item["memory"]) <= 72 for item in result["results"])
                )
                self.assertEqual(
                    result["query_debug"]["render_token_budget"],
                    128,
                )
                self.assertTrue(result["query_debug"]["top_k_budget_independent"])
                self.assertIn(
                    "memory_fusion",
                    result["query_debug"]["timings_ms"],
                )
            finally:
                backend.close()

    def test_fusion_surfaces_assistant_sibling_ahead_of_claim_only_top_50(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "assistant-sibling.db")
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=FakeCompiler(database),
                context_token_budget=128,
            )
            try:
                backend.add(
                    {
                        "user_id": "alice",
                        "timestamp": 1_700_000_000,
                        "metadata": {"session_id": "session-1"},
                        "messages": [
                            {
                                "role": "user",
                                "content": "The launch code is cobalt seven.",
                            },
                            {
                                "role": "assistant",
                                "content": "I recorded the launch code.",
                            },
                            *[
                                message
                                for index in range(6)
                                for message in (
                                    {
                                        "role": "user",
                                        "content": f"Unrelated checkpoint {index}?",
                                    },
                                    {
                                        "role": "assistant",
                                        "content": f"Unrelated response {index}.",
                                    },
                                )
                            ],
                            {
                                "role": "user",
                                "content": "Which fusion target website should I use?",
                            },
                            {
                                "role": "assistant",
                                "content": (
                                    "Use the fusion target at "
                                    "https://mindful.example/resources."
                                ),
                            },
                        ],
                    }
                )
                engine = backend.engine("alice")
                stored_rows = engine._conn.execute(
                    """
                    SELECT id, speaker, text FROM messages
                    WHERE user_id = ? ORDER BY position
                    """,
                    ("alice",),
                ).fetchall()
                source_id = int(stored_rows[0][0])
                assistant_id = int(stored_rows[-1][0])
                assistant_text = str(stored_rows[-1][2])

                claims = [
                    MemoryClaim(
                        id=20_000 + index,
                        kind="fact",
                        text=f"Fusion target derived claim {index}.",
                        status="active",
                        confidence=1.0,
                        sources=(
                            ClaimSource(
                                message_id=source_id,
                                session_id="session-1",
                                speaker="user",
                                quote=str(stored_rows[0][2]),
                            ),
                        ),
                        score=1.0 / (index + 1),
                        channels=("claim_fts",),
                    )
                    for index in range(50)
                ]
                raw_hits = [
                    Message(
                        id=10_000 + index,
                        speaker="user",
                        text=f"Fusion target raw distractor {index}.",
                        timestamp=1_700_000_100 + index,
                        position=index,
                        provenance={"run_id": f"distractor-{index}"},
                    )
                    for index in range(50)
                ]

                def search_claims(query: str, *, limit: int = 40):
                    return claims[:limit]

                engine._memory.search_claims = search_claims
                engine.search = lambda *args, **kwargs: SearchResult(
                    messages=raw_hits,
                    query_ms=1.0,
                    total_matches=len(raw_hits),
                    direct_hits=raw_hits,
                    timings_ms={"total": 1.0},
                    scores=[1.0 - index / 100 for index in range(len(raw_hits))],
                )

                result = backend.search(
                    {
                        "user_id": "alice",
                        "query": "fusion target",
                        "limit": 50,
                    }
                )
                self.assertEqual(len(result["results"]), 50)
                result_ids = [item["id"] for item in result["results"]]
                self.assertIn(f"message:{assistant_id}", result_ids)
                self.assertLess(result_ids.index(f"message:{assistant_id}"), 10)
                assistant_memory = next(
                    item["memory"]
                    for item in result["results"]
                    if item["id"] == f"message:{assistant_id}"
                )
                self.assertIn(assistant_text, assistant_memory)
                self.assertTrue(any(item.startswith("claim:") for item in result_ids))
                self.assertTrue(any(item.startswith("message:") for item in result_ids))

                filtered = engine.search_memory_blocks(
                    "fusion target",
                    limit=50,
                    filters={"run_id": "allowed-only"},
                )
                self.assertNotIn(
                    assistant_id,
                    {
                        message_id
                        for block in filtered.blocks
                        for message_id in block.message_ids
                    },
                )
            finally:
                backend.close()

    def test_structural_excerpt_preserves_requested_item_and_url_line(self) -> None:
        numbered = "\n".join(
            f"{index}. {'target answer' if index == 27 else 'filler value'} {index}"
            for index in range(1, 61)
        )
        item_excerpt = _concise_excerpt(
            f"[assistant] Here is the complete list:\n{numbered}",
            "What was the 27th item?",
            120,
        )
        self.assertIn("27. target answer 27", item_excerpt)
        self.assertLessEqual(len(item_excerpt), 120)

        url_text = (
            "[assistant] Introductory context that is not the answer.\n"
            + ("background material\n" * 80)
            + "Official website: https://mindful.example/resources\n"
            + ("trailing material\n" * 80)
        )
        url_excerpt = _concise_excerpt(
            url_text,
            "Which website should I use?",
            160,
        )
        self.assertIn("https://mindful.example/resources", url_excerpt)
        self.assertIn("\n", url_excerpt)
        self.assertLessEqual(len(url_excerpt), 160)

    def test_persistent_cache_avoids_recompiling_an_equivalent_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_database = str(Path(directory) / "first.db")
            second_database = str(Path(directory) / "second.db")
            cache_path = Path(directory) / "compiler-cache.sqlite3"
            compiler = FakeCompiler()
            first = NarratorDBBenchmarkBackend(
                first_database,
                mode="intelligence",
                compiler=compiler,
                compiler_cache=CompiledSessionCache(cache_path),
            )
            try:
                first.add(_payload("alice"))
            finally:
                first.close()

            second = NarratorDBBenchmarkBackend(
                second_database,
                mode="intelligence",
                compiler=compiler,
                compiler_cache=CompiledSessionCache(cache_path),
            )
            try:
                second.add(_payload("bob"))
                self.assertEqual(len(compiler.calls), 1)

                bob = second.engine("bob")
                source = bob._conn.execute(
                    """
                    SELECT cs.message_id, m.user_id
                    FROM memory_claim_sources cs
                    JOIN memory_claims c ON c.id = cs.claim_id
                    JOIN messages m ON m.id = cs.message_id
                    WHERE c.user_id = 'bob'
                    ORDER BY cs.message_id LIMIT 1
                    """
                ).fetchone()
                self.assertIsNotNone(source)
                self.assertEqual(source[1], "bob")
                self.assertEqual(bob.enrichment_status()["jobs"]["complete"], 1)
            finally:
                second.close()

    def test_compiler_failure_keeps_committed_raw_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "failure.db")
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=FailingCompiler(),
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "after raw session commit"):
                    backend.add(_payload("alice"))
                engine = backend.engine("alice")
                self.assertEqual(engine.stats()["message_count"], 2)
                status = engine.enrichment_status()
                self.assertEqual(status["jobs"]["blocked"], 1)
                # A retry of the same source must not mistake a blocked job for
                # an already-complete idempotent compilation.
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"not runnable after raw session commit .*status=blocked",
                ):
                    backend.add(_payload("alice"))
                self.assertEqual(engine.stats()["message_count"], 2)
            finally:
                backend.close()

    def test_exhausted_job_is_terminal_across_exact_ingest_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "exhausted.db")
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=RetryableFailingCompiler(),
            )
            try:
                for _ in range(3):
                    with self.assertRaisesRegex(
                        RuntimeError, "after raw session commit"
                    ):
                        backend.add(_payload("alice"))
                engine = backend.engine("alice")
                state = engine._conn.execute(
                    "SELECT status, attempts, last_error FROM memory_compiler_jobs"
                ).fetchone()
                self.assertEqual(
                    tuple(state),
                    ("exhausted", 3, "incomplete_completion"),
                )

                with self.assertRaisesRegex(
                    RuntimeError,
                    r"not runnable after raw session commit .*status=exhausted",
                ):
                    backend.add(_payload("alice"))
                state = engine._conn.execute(
                    "SELECT status, attempts, last_error FROM memory_compiler_jobs"
                ).fetchone()
                self.assertEqual(
                    tuple(state),
                    ("exhausted", 3, "incomplete_completion"),
                )
                self.assertEqual(engine.stats()["message_count"], 2)
            finally:
                backend.close()

    def test_coalescing_compiles_one_full_timestamp_session_on_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "coalesced.db")
            compiler = FakeCompiler(database)
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=compiler,
                coalesce_sessions=True,
                context_token_budget=512,
            )
            try:
                first = _pair_payload("alice", 1_700_000_000, "launch")
                second = _pair_payload("alice", 1_700_000_000, "backup")
                self.assertEqual(len(backend.add(first)["results"]), 2)
                self.assertEqual(len(backend.add(second)["results"]), 2)
                self.assertEqual(backend.add(second)["results"], [])
                self.assertEqual(compiler.calls, [])

                engine = backend.engine("alice")
                session_row = engine._conn.execute(
                    "SELECT id, external_id FROM memory_sessions WHERE user_id = ?",
                    ("alice",),
                ).fetchone()
                self.assertIsNotNone(session_row)
                self.assertEqual(
                    session_row[1],
                    backend._coalesced_session_id(first, {}),
                )
                session = engine.load_compiler_session(int(session_row[0]))
                self.assertEqual(len(session["messages"]), 4)
                self.assertEqual(engine.stats()["message_count"], 4)

                result = backend.search(
                    {"user_id": "alice", "query": "access credential", "limit": 10}
                )
                self.assertTrue(result["query_debug"]["coalesce_sessions"])
                self.assertEqual(result["query_debug"]["lazy_finalized_sessions"], 1)
                self.assertGreaterEqual(result["query_debug"]["finalization_ms"], 0)
                self.assertEqual(len(compiler.calls), 1)
                self.assertEqual(len(compiler.calls[0].messages), 4)
                self.assertEqual(compiler.raw_counts_during_compile, [4])
                self.assertTrue(result["results"])

                second_search = backend.search(
                    {"user_id": "alice", "query": "access credential", "limit": 10}
                )
                self.assertEqual(
                    second_search["query_debug"]["lazy_finalized_sessions"], 0
                )
                self.assertEqual(len(compiler.calls), 1)
            finally:
                backend.close()

    def test_explicit_finalize_is_idempotent_and_keeps_search_query_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "finalize.db")
            compiler = FakeCompiler(database)
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=compiler,
                coalesce_sessions=True,
            )
            try:
                first = _pair_payload("alice", 1_700_000_000, "launch")
                second = _pair_payload("alice", 1_700_000_000, "backup")
                backend.add(first)
                backend.add(second)
                self.assertEqual(compiler.calls, [])

                session_id = backend._coalesced_session_id(first, {})
                finalized = backend.finalize("alice", session_id)
                self.assertEqual(finalized["status"], "complete")
                self.assertEqual(finalized["user_id"], "alice")
                self.assertEqual(finalized["session_id"], session_id)
                self.assertEqual(finalized["finalized_sessions"], 1)
                self.assertEqual(finalized["matched_sessions"], 1)
                self.assertEqual(finalized["complete_sessions"], 1)
                self.assertEqual(finalized["partial_sessions"], 0)
                self.assertEqual(finalized["in_progress_sessions"], 0)
                self.assertGreaterEqual(finalized["finalization_ms"], 0)
                self.assertEqual(len(compiler.calls), 1)
                self.assertEqual(len(compiler.calls[0].messages), 4)

                repeated = backend.finalize("alice", session_id)
                self.assertEqual(repeated["status"], "complete")
                self.assertEqual(repeated["finalized_sessions"], 0)
                self.assertEqual(repeated["matched_sessions"], 1)
                self.assertEqual(repeated["complete_sessions"], 1)
                self.assertEqual(len(compiler.calls), 1)

                unknown = backend.finalize("alice", "unknown-session")
                self.assertEqual(unknown["status"], "not_found")
                self.assertEqual(unknown["finalized_sessions"], 0)
                self.assertEqual(unknown["matched_sessions"], 0)
                self.assertEqual(unknown["complete_sessions"], 0)
                self.assertEqual(unknown["partial_sessions"], 0)
                self.assertEqual(unknown["in_progress_sessions"], 0)

                result = backend.search(
                    {"user_id": "alice", "query": "backup credential", "limit": 10}
                )
                self.assertEqual(result["query_debug"]["lazy_finalized_sessions"], 0)
                self.assertGreaterEqual(result["query_debug"]["finalization_ms"], 0)
                self.assertEqual(len(compiler.calls), 1)
            finally:
                backend.close()

    def test_finalize_reports_partial_terminal_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            compiler = ContentFilteredCompiler()
            backend = NarratorDBBenchmarkBackend(
                str(Path(directory) / "finalize-partial.db"),
                mode="intelligence",
                compiler=compiler,
                coalesce_sessions=True,
            )
            try:
                payload = _pair_payload("alice", 1_700_000_000, "launch")
                backend.add(payload)
                session_id = backend._coalesced_session_id(payload, {})

                finalized = backend.finalize("alice", session_id)
                self.assertEqual(finalized["status"], "complete")
                self.assertEqual(finalized["finalized_sessions"], 1)
                self.assertEqual(finalized["matched_sessions"], 1)
                self.assertEqual(finalized["complete_sessions"], 0)
                self.assertEqual(finalized["partial_sessions"], 1)
                self.assertEqual(finalized["in_progress_sessions"], 0)
                self.assertEqual(len(compiler.calls), 1)
                self.assertEqual(
                    backend.engine("alice").enrichment_status()["partial_reasons"],
                    {"content_filtered": 1},
                )

                repeated = backend.finalize("alice", session_id)
                self.assertEqual(repeated["status"], "complete")
                self.assertEqual(repeated["finalized_sessions"], 0)
                self.assertEqual(repeated["partial_sessions"], 1)
                self.assertEqual(len(compiler.calls), 1)
                self.assertEqual(
                    backend.engine("alice").enrichment_status()["partial_reasons"],
                    {"content_filtered": 1},
                )
            finally:
                backend.close()

    def test_finalize_remains_idempotent_after_backend_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "finalize-restart.db")
            compiler = FakeCompiler(database)
            payload = _pair_payload("alice", 1_700_000_000, "launch")
            session_id = NarratorDBBenchmarkBackend._coalesced_session_id(payload, {})

            first_backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=compiler,
                coalesce_sessions=True,
            )
            try:
                first_backend.add(payload)
                self.assertEqual(
                    first_backend.finalize("alice", session_id)["finalized_sessions"],
                    1,
                )
            finally:
                first_backend.close()

            second_backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=compiler,
                coalesce_sessions=True,
            )
            try:
                repeated = second_backend.finalize("alice", session_id)
                self.assertEqual(repeated["status"], "complete")
                self.assertEqual(repeated["finalized_sessions"], 0)
                self.assertEqual(repeated["matched_sessions"], 1)
                self.assertEqual(repeated["complete_sessions"], 1)
                self.assertEqual(len(compiler.calls), 1)
            finally:
                second_backend.close()

    def test_finalize_reports_job_claimed_by_another_backend_as_in_progress(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "finalize-running.db")
            compiler = FakeCompiler(database)
            first_backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=compiler,
                coalesce_sessions=True,
            )
            second_backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=compiler,
                coalesce_sessions=True,
            )
            try:
                first_backend.add(_pair_payload("alice", 1_700_000_000, "launch"))
                engine = first_backend.engine("alice")
                session = engine._conn.execute(
                    """
                    SELECT id, source_hash FROM memory_sessions
                    WHERE user_id = ?
                    """,
                    ("alice",),
                ).fetchone()
                self.assertIsNotNone(session)
                job_id = engine.enqueue_compilation(
                    int(session[0]),
                    str(session[1]),
                    first_backend._compiler.fingerprint,
                )
                self.assertIsNotNone(engine.claim_compilation_attempt(job_id))

                response = second_backend.finalize("alice")
                self.assertEqual(response["status"], "in_progress")
                self.assertEqual(response["finalized_sessions"], 0)
                self.assertEqual(response["matched_sessions"], 1)
                self.assertEqual(response["complete_sessions"], 0)
                self.assertEqual(response["partial_sessions"], 0)
                self.assertEqual(response["in_progress_sessions"], 1)
                self.assertEqual(compiler.calls, [])
            finally:
                second_backend.close()
                first_backend.close()

    def test_finalize_isolates_user_and_optional_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "finalize-isolation.db")
            compiler = FakeCompiler(database)
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=compiler,
                coalesce_sessions=True,
            )
            try:
                alice = _pair_payload("alice", 1_700_000_000, "alice")
                bob = _pair_payload("bob", 1_700_000_100, "bob")
                backend.add(alice)
                backend.add(bob)
                alice_session = backend._coalesced_session_id(alice, {})
                bob_session = backend._coalesced_session_id(bob, {})

                self.assertEqual(
                    backend.finalize("bob", alice_session)["finalized_sessions"], 0
                )
                self.assertEqual(compiler.calls, [])
                self.assertEqual(
                    backend.finalize("alice", alice_session)["finalized_sessions"],
                    1,
                )
                self.assertIn(
                    "alice access credential", compiler.calls[0].messages[0].content
                )
                self.assertEqual(
                    backend.finalize("bob", bob_session)["finalized_sessions"], 1
                )
                self.assertIn(
                    "bob access credential", compiler.calls[1].messages[0].content
                )
            finally:
                backend.close()

    def test_append_after_finalize_creates_one_new_source_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "finalize-append.db")
            compiler = FakeCompiler(database)
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=compiler,
                coalesce_sessions=True,
            )
            try:
                first = _pair_payload("alice", 1_700_000_000, "launch")
                backend.add(first)
                self.assertEqual(backend.finalize("alice")["finalized_sessions"], 1)
                self.assertEqual(len(compiler.calls), 1)

                backend.add(_pair_payload("alice", 1_700_000_000, "backup"))
                self.assertEqual(len(compiler.calls), 1)
                self.assertEqual(backend.finalize("alice")["finalized_sessions"], 1)
                self.assertEqual(len(compiler.calls), 2)
                self.assertEqual(len(compiler.calls[1].messages), 4)
                self.assertEqual(backend.finalize("alice")["finalized_sessions"], 0)

                states = (
                    backend.engine("alice")
                    ._conn.execute(
                        """
                    SELECT status, source_hash FROM memory_compiler_jobs
                    WHERE user_id = ? ORDER BY id
                    """,
                        ("alice",),
                    )
                    .fetchall()
                )
                self.assertEqual(
                    [str(row[0]) for row in states], ["obsolete", "complete"]
                )
                self.assertNotEqual(str(states[0][1]), str(states[1][1]))
                result = backend.search(
                    {"user_id": "alice", "query": "credential", "limit": 10}
                )
                self.assertEqual(result["query_debug"]["lazy_finalized_sessions"], 0)
                self.assertEqual(len(compiler.calls), 2)
            finally:
                backend.close()

    def test_http_finalize_rejects_query_and_dispatches_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = NarratorDBBenchmarkBackend(
                str(Path(directory) / "finalize-http.db"),
                mode="intelligence",
                compiler=FakeCompiler(),
                coalesce_sessions=True,
            )
            try:
                backend.add(_pair_payload("alice", 1_700_000_000, "launch"))
                handler = object.__new__(make_handler(backend))
                responses = []
                handler.path = "/memories/finalize"
                handler.send_json = lambda status, payload: responses.append(
                    (status, payload)
                )
                handler.read_json = lambda: {"user_id": "alice"}
                handler.do_POST()
                self.assertEqual(int(responses[-1][0]), 200)
                self.assertEqual(responses[-1][1]["finalized_sessions"], 1)

                handler.read_json = lambda: {
                    "user_id": "alice",
                    "query": "must not reach finalization",
                }
                handler.do_POST()
                self.assertEqual(int(responses[-1][0]), 400)
                self.assertIn("only user_id and session_id", responses[-1][1]["error"])
            finally:
                backend.close()

    def test_coalescing_timestamp_change_flushes_prior_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "boundary.db")
            compiler = FakeCompiler(database)
            backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=compiler,
                coalesce_sessions=True,
            )
            try:
                backend.add(_pair_payload("alice", 1_700_000_000, "launch"))
                self.assertEqual(compiler.calls, [])

                backend.add(_pair_payload("alice", 1_700_000_100, "backup"))
                self.assertEqual(len(compiler.calls), 1)
                self.assertEqual(len(compiler.calls[0].messages), 2)
                # The new pair commits before the prior session is compiled.
                self.assertEqual(compiler.raw_counts_during_compile, [4])

                backend.search(
                    {"user_id": "alice", "query": "access credential", "limit": 10}
                )
                self.assertEqual(len(compiler.calls), 2)
                self.assertEqual(len(compiler.calls[1].messages), 2)
                self.assertNotEqual(
                    compiler.calls[0].session_id,
                    compiler.calls[1].session_id,
                )
            finally:
                backend.close()

    def test_coalescing_reconstructs_open_session_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "restart.db")
            compiler = FakeCompiler(database)
            first_backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=compiler,
                coalesce_sessions=True,
            )
            try:
                first_backend.add(_pair_payload("alice", 1_700_000_000, "launch"))
                self.assertEqual(compiler.calls, [])
            finally:
                first_backend.close()

            second_backend = NarratorDBBenchmarkBackend(
                database,
                mode="intelligence",
                compiler=compiler,
                coalesce_sessions=True,
            )
            try:
                second_backend.add(_pair_payload("alice", 1_700_000_000, "backup"))
                self.assertEqual(compiler.calls, [])
                engine = second_backend.engine("alice")
                session_pk = int(
                    engine._conn.execute(
                        "SELECT id FROM memory_sessions WHERE user_id = ?",
                        ("alice",),
                    ).fetchone()[0]
                )
                self.assertEqual(
                    len(engine.load_compiler_session(session_pk)["messages"]),
                    4,
                )

                second_backend.search(
                    {"user_id": "alice", "query": "access credential", "limit": 10}
                )
                self.assertEqual(len(compiler.calls), 1)
                self.assertEqual(len(compiler.calls[0].messages), 4)
                self.assertEqual(second_backend.delete("alice"), {"deleted": 4})
                self.assertEqual(
                    engine._conn.execute(
                        "SELECT COUNT(*) FROM memory_sessions WHERE user_id = ?",
                        ("alice",),
                    ).fetchone()[0],
                    0,
                )
            finally:
                second_backend.close()

    def test_cli_intelligence_config_has_no_credentials(self) -> None:
        parser = build_argument_parser()
        missing_cap = parser.parse_args(
            ["--mode", "intelligence", "--compiler", "openrouter"]
        )
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                _compiler_config_from_args(missing_cap, parser)

        for invalid in ("inf", "-inf", "nan"):
            invalid_cap = parser.parse_args(
                [
                    "--mode",
                    "intelligence",
                    "--compiler",
                    "openrouter",
                    f"--compiler-max-cost-usd={invalid}",
                ]
            )
            with (
                self.subTest(invalid=invalid),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    _compiler_config_from_args(invalid_cap, parser)

        args = parser.parse_args(
            [
                "--mode",
                "intelligence",
                "--compiler",
                "openrouter",
                "--coalesce-timestamp-sessions",
                "--compiler-max-cost-usd",
                "180",
                "--output-token-parameter",
                "max_tokens",
            ]
        )
        config = _compiler_config_from_args(args, parser)
        self.assertIsNotNone(config)
        self.assertEqual(config.kind.value, "openrouter")
        self.assertEqual(config.output_token_parameter, "max_tokens")
        self.assertEqual(config.semantic_max_attempts, 2)
        self.assertEqual(config.transport_max_attempts, 1)
        self.assertEqual(config.min_request_interval_seconds, 10.0)
        self.assertTrue(config.capture_router_metadata)
        self.assertTrue(args.coalesce_sessions)
        self.assertNotIn("api_key", config.to_dict())

        allowlist_args = parser.parse_args(
            [
                "--mode",
                "intelligence",
                "--compiler",
                "openrouter",
                "--compiler-max-cost-usd",
                "250",
                "--provider-allow",
                "parasail/fp4,wafer/fp4,deepinfra/fp4",
                "--compiler-min-request-interval-seconds",
                "12",
                "--compiler-request-reservation-usd",
                "0.05",
                "--compiler-budget-safety-reserve-usd",
                "1",
            ]
        )
        allowlist_config = _compiler_config_from_args(allowlist_args, parser)
        self.assertIsNone(allowlist_config.provider)
        self.assertEqual(
            allowlist_config.provider_allowlist,
            ("parasail/fp4", "wafer/fp4", "deepinfra/fp4"),
        )
        self.assertTrue(allowlist_config.allow_fallbacks)
        self.assertEqual(allowlist_config.min_request_interval_seconds, 12.0)
        self.assertEqual(allowlist_args.compiler_request_reservation_usd, 0.05)
        self.assertEqual(allowlist_args.compiler_budget_safety_reserve_usd, 1.0)

    def test_cli_codex_compiler_wires_runtime_only_paths_without_usd_cap(self) -> None:
        parser = build_argument_parser()
        args = parser.parse_args(
            [
                "--mode",
                "intelligence",
                "--compiler",
                "codex-cli",
                "--model",
                "gpt-5.4-mini",
                "--reasoning",
                "low",
                "--codex-executable",
                "/opt/codex/bin/codex",
                "--codex-home",
                "/tmp/narratordb-codex-home",
                "--codex-cli-version",
                "codex-cli 0.144.4",
                "--codex-timeout-seconds",
                "240",
                "--codex-max-invocations",
                "100",
                "--codex-max-concurrency",
                "2",
                "--compiler-semantic-max-attempts",
                "2",
                "--compiler-retry-delay-seconds",
                "1",
                "--compiler-min-request-interval-seconds",
                "0.5",
            ]
        )

        config = _compiler_config_from_args(args, parser)

        self.assertEqual(config.kind.value, "codex-cli")
        self.assertEqual(config.codex_cli_version, "codex-cli 0.144.4")
        self.assertEqual(config.codex_timeout_seconds, 240.0)
        self.assertEqual(config.codex_max_invocations, 100)
        self.assertEqual(config.codex_max_concurrency, 2)
        self.assertNotIn("executable", config.to_dict())
        self.assertNotIn("codex_executable", config.to_dict())
        self.assertNotIn("codex_home", config.to_dict())
        self.assertIsNone(args.compiler_max_cost_usd)

        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "codex-runtime.db")
            with patch(
                "narratordb.benchmark_server.compiler_from_project_config",
                return_value=FakeCompiler(database),
            ) as build_compiler:
                backend = NarratorDBBenchmarkBackend(
                    database,
                    mode="intelligence",
                    compiler_config=config,
                    codex_executable=args.codex_executable,
                    codex_home=args.codex_home,
                )
                backend.close()
            build_compiler.assert_called_once_with(
                config,
                usage_sink=None,
                codex_executable="/opt/codex/bin/codex",
                codex_home=Path("/tmp/narratordb-codex-home"),
            )

        for invalid_option in (
            ["--provider", "Azure"],
            ["--endpoint", "http://127.0.0.1:11434/v1"],
            ["--compiler-transport-max-attempts", "1"],
            ["--compiler-capture-router-metadata"],
            ["--compiler-max-cost-usd", "10"],
            ["--compiler-request-reservation-usd", "0.05"],
            ["--max-output-tokens", "4096"],
        ):
            with self.subTest(invalid_option=invalid_option):
                invalid = parser.parse_args(
                    [
                        "--mode",
                        "intelligence",
                        "--compiler",
                        "codex-cli",
                        *invalid_option,
                    ]
                )
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        _compiler_config_from_args(invalid, parser)

        local_with_cap = parser.parse_args(
            [
                "--mode",
                "intelligence",
                "--compiler",
                "local",
                "--model",
                "local-test",
                "--endpoint",
                "http://127.0.0.1:11434/v1",
                "--compiler-max-cost-usd",
                "10",
            ]
        )
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                _compiler_config_from_args(local_with_cap, parser)

    def test_benchmark_private_mode_never_downloads_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # Exercise the optional semantic-backend branch even in the base
            # test environment, where NumPy is intentionally not required.
            with (
                patch.dict("sys.modules", {"numpy": object()}),
                patch(
                    "narratordb.engine._load_sentence_transformer",
                    return_value=(None, None),
                ) as load,
            ):
                backend = NarratorDBBenchmarkBackend(
                    str(Path(directory) / "private-local-only.db"),
                    mode="private",
                )
                try:
                    backend.engine("alice")
                finally:
                    backend.close()
            load.assert_called_once_with(local_only=True)


if __name__ == "__main__":
    unittest.main()
