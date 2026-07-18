import json
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from narratordb.compiler import (
    CompileResult,
    CompileSessionInput,
    CompiledClaim,
    CompiledEntity,
    CompiledMemory,
    CompiledRelation,
    CompiledSummary,
    CompilerUsage,
    CompilerError,
    EvidenceSpan,
    MemoryCompiler,
    ReferenceClaim,
    SourceMessage,
)
from narratordb.compiler_cache import (
    CachedMemoryCompiler,
    CompiledSessionCache,
    compiled_session_cache_key,
    deserialize_compile_result,
    deserialize_compile_session,
    serialize_compile_result,
    serialize_compile_session,
)


SOURCE = "I moved to Kyoto. Kyoto is home now."


def make_session(session_id: str = "session-1") -> CompileSessionInput:
    return CompileSessionInput(
        session_id=session_id,
        document_time="2025-01-03T12:00:00Z",
        messages=(
            SourceMessage(
                message_id="message-1",
                role="user",
                content=SOURCE,
                occurred_at="2025-01-02T08:30:00Z",
            ),
        ),
    )


def make_result(
    session_id: str = "session-1",
    message_id: str = "message-1",
) -> CompileResult:
    move = EvidenceSpan(
        message_id=message_id,
        quote="I moved to Kyoto.",
        start=0,
        end=len("I moved to Kyoto."),
    )
    home_start = SOURCE.index("Kyoto is home now.")
    home = EvidenceSpan(
        message_id=message_id,
        quote="Kyoto is home now.",
        start=home_start,
        end=home_start + len("Kyoto is home now."),
    )
    memory = CompiledMemory(
        session_id=session_id,
        summary=CompiledSummary(
            "The user moved to Kyoto and considers it home.", (move, home)
        ),
        entities=(
            CompiledEntity("e1", "Kyoto", "place", ("Kyoto City",), (move, home)),
        ),
        claims=(
            CompiledClaim(
                "c1",
                "event",
                "The user moved to Kyoto.",
                0.99,
                "active",
                "2025-01-03T12:00:00Z",
                "2025-01-02",
                None,
                "2025-01-02",
                None,
                ("e1",),
                (move,),
            ),
            CompiledClaim(
                "c2",
                "fact",
                "The user considers Kyoto home.",
                0.97,
                "active",
                "2025-01-03T12:00:00Z",
                None,
                None,
                None,
                None,
                ("e1",),
                (home,),
            ),
        ),
        relations=(CompiledRelation("r1", "supports", "c1", "c2", 0.8, (home,)),),
    )
    usage = CompilerUsage(
        request_model="test-model",
        response_model="test-model-2025",
        provider="test-provider",
        attempt=1,
        prompt_tokens=100,
        cached_tokens=20,
        completion_tokens=30,
        reasoning_tokens=5,
        cost_usd=0.001,
        cost_source="provider",
        finish_reason="stop",
    )
    return CompileResult(memory=memory, usage=(usage,))


class FakeCompiler:
    def __init__(self, fingerprint: str = "fake:v1") -> None:
        self._fingerprint = fingerprint
        self.calls = 0

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def compile_session(self, session: CompileSessionInput) -> CompileResult:
        self.calls += 1
        return make_result(session.session_id, session.messages[0].message_id)


class BlockingCompiler(FakeCompiler):
    def __init__(self) -> None:
        super().__init__()
        self._calls_lock = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()

    def compile_session(self, session: CompileSessionInput) -> CompileResult:
        with self._calls_lock:
            self.calls += 1
        self.started.set()
        if not self.release.wait(2):
            raise AssertionError("test did not release the blocking compiler")
        return make_result(session.session_id, session.messages[0].message_id)


class FailOnceCompiler(FakeCompiler):
    def compile_session(self, session: CompileSessionInput) -> CompileResult:
        self.calls += 1
        if self.calls == 1:
            raise CompilerError("synthetic failure", code="synthetic", retryable=True)
        return make_result(session.session_id, session.messages[0].message_id)


class ObservedContenderCache(CompiledSessionCache):
    def __init__(self, path: str | Path) -> None:
        self.contended = threading.Event()
        super().__init__(path)

    def _try_acquire_compile_lease(
        self,
        cache_key: str,
        owner_token: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        acquired = super()._try_acquire_compile_lease(
            cache_key,
            owner_token,
            ttl_seconds=ttl_seconds,
        )
        if not acquired:
            self.contended.set()
        return acquired


class CompilerCacheTests(unittest.TestCase):
    def test_strict_json_round_trip_restores_every_nested_dataclass(self):
        session = replace(
            make_session(),
            reference_claims=(
                ReferenceClaim(
                    claim_id="prior-9",
                    memory_key="user.residence.current_city",
                    text="The user lived in Tokyo.",
                    document_time="2024-12-01T00:00:00Z",
                ),
            ),
        )
        result = make_result()

        self.assertEqual(
            deserialize_compile_session(serialize_compile_session(session)), session
        )
        self.assertEqual(
            deserialize_compile_result(serialize_compile_result(result), session),
            result,
        )
        routed = replace(
            result,
            usage=(
                replace(
                    result.usage[0],
                    router_attempt=2,
                    attempted_providers=("Parasail", "Wafer"),
                    attempt_statuses=(429, 200),
                ),
            ),
        )
        self.assertEqual(
            deserialize_compile_result(serialize_compile_result(routed), session),
            routed,
        )

        legacy = json.loads(serialize_compile_result(result))
        for field in ("router_attempt", "attempted_providers", "attempt_statuses"):
            legacy["usage"][0].pop(field)
        self.assertEqual(
            deserialize_compile_result(json.dumps(legacy), session),
            result,
        )

        with self.assertRaises(ValueError):
            deserialize_compile_session(
                '{"session_id":"s","session_id":"duplicate","messages":[],"document_time":null}'
            )

    def test_reference_context_participates_in_cache_identity(self):
        session = make_session()
        tokyo = replace(
            session,
            reference_claims=(
                ReferenceClaim(
                    claim_id="prior-1",
                    memory_key="user.residence.current_city",
                    text="The user lived in Tokyo.",
                ),
            ),
        )
        osaka = replace(
            session,
            reference_claims=(
                ReferenceClaim(
                    claim_id="prior-2",
                    memory_key="user.residence.current_city",
                    text="The user lived in Osaka.",
                ),
            ),
        )

        self.assertNotEqual(
            compiled_session_cache_key("fake:v1", session),
            compiled_session_cache_key("fake:v1", tokyo),
        )
        self.assertNotEqual(
            compiled_session_cache_key("fake:v1", tokyo),
            compiled_session_cache_key("fake:v1", osaka),
        )

    def test_wrapper_hits_persistent_cache_without_new_usage_or_cost(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "compiled.sqlite3"
            session = make_session()
            compiler = FakeCompiler()
            with CompiledSessionCache(path) as cache:
                cached = CachedMemoryCompiler(compiler, cache)
                self.assertIsInstance(cached, MemoryCompiler)

                miss = cached.compile_session(session)
                hit = cached.compile_session(session)

                self.assertEqual(compiler.calls, 1)
                self.assertEqual(miss.usage, make_result().usage)
                self.assertEqual(hit.usage, ())
                self.assertEqual(hit.memory, miss.memory)
                self.assertEqual(
                    cache.stats(),
                    cache.stats().__class__(
                        hits=1, misses=1, writes=1, corruptions=0, entries=1
                    ),
                )

            fresh_compiler = FakeCompiler()
            with CompiledSessionCache(path) as reopened:
                persisted_hit = CachedMemoryCompiler(
                    fresh_compiler, reopened
                ).compile_session(session)
                self.assertEqual(fresh_compiler.calls, 0)
                self.assertEqual(persisted_hit.usage, ())
                self.assertEqual(reopened.stats().entries, 1)

    def test_source_identity_excludes_session_id_but_rebinds_hit(self):
        original = replace(
            make_session("original-session"),
            messages=(
                replace(make_session().messages[0], message_id="scope-a-message-101"),
            ),
        )
        rebound = replace(
            make_session("another-scope-session"),
            messages=(
                replace(
                    make_session().messages[0], message_id="other-scope-message-938"
                ),
            ),
        )
        self.assertEqual(
            compiled_session_cache_key("fake:v1", original),
            compiled_session_cache_key("fake:v1", rebound),
        )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.sqlite3"
            compiler = FakeCompiler()
            with CompiledSessionCache(path) as cache:
                cached = CachedMemoryCompiler(compiler, cache)
                cached.compile_session(original)
                result = cached.compile_session(rebound)
                with sqlite3.connect(path) as inspection:
                    stored_memory = inspection.execute(
                        "SELECT memory_json FROM compiled_session_cache"
                    ).fetchone()[0]

        self.assertIn("source-00000000", stored_memory)
        self.assertNotIn("scope-a-message-101", stored_memory)
        self.assertNotIn("other-scope-message-938", stored_memory)

        self.assertEqual(compiler.calls, 1)
        self.assertEqual(result.memory.session_id, "another-scope-session")
        self.assertEqual(result.usage, ())
        evidence = (
            result.memory.summary.evidence
            + result.memory.entities[0].evidence
            + result.memory.claims[0].evidence
            + result.memory.relations[0].evidence
        )
        self.assertTrue(evidence)
        self.assertEqual(
            {span.message_id for span in evidence}, {"other-scope-message-938"}
        )

    def test_source_and_compiler_behavior_changes_are_cache_misses(self):
        baseline = make_session()
        changed_content = replace(
            baseline,
            messages=(replace(baseline.messages[0], content=SOURCE + " Really."),),
        )
        changed_time = replace(baseline, document_time="2025-01-04T12:00:00Z")
        self.assertNotEqual(
            compiled_session_cache_key("fake:v1", baseline),
            compiled_session_cache_key("fake:v1", changed_content),
        )
        self.assertNotEqual(
            compiled_session_cache_key("fake:v1", baseline),
            compiled_session_cache_key("fake:v1", changed_time),
        )
        self.assertNotEqual(
            compiled_session_cache_key("fake:v1", baseline),
            compiled_session_cache_key("fake:v2", baseline),
        )

    def test_database_never_stores_raw_compiler_fingerprint(self):
        # Assemble the fake credential shape at runtime so publication-time
        # archive scanners do not need an exception for this source fixture.
        fake_key = "".join(("sk", "-or", "-v1", "-credential"))
        secret_fingerprint = f"compiler:{fake_key}-that-must-not-be-stored"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.sqlite3"
            compiler = FakeCompiler(secret_fingerprint)
            with CompiledSessionCache(path) as cache:
                CachedMemoryCompiler(compiler, cache).compile_session(make_session())

            database_bytes = path.read_bytes()
            self.assertNotIn(secret_fingerprint.encode(), database_bytes)
            self.assertNotIn(fake_key.encode(), database_bytes)

    def test_clear_transactionally_removes_all_derived_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.sqlite3"
            with CompiledSessionCache(path) as cache:
                fingerprint = "fake:v1"
                session = make_session()
                cache.put(fingerprint, session, make_result().memory)
                cache_key = compiled_session_cache_key(fingerprint, session)
                self.assertTrue(
                    cache._try_acquire_compile_lease(
                        cache_key,
                        "owner-being-cleared",
                        ttl_seconds=60,
                    )
                )
                self.assertEqual(cache.stats().entries, 1)

                self.assertEqual(cache.clear(), 1)
                self.assertEqual(cache.stats().entries, 0)
                self.assertEqual(
                    cache._connection.execute(
                        "SELECT COUNT(*) FROM compiler_cache_leases"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(cache.clear(), 0)
                self.assertIsNone(cache.get(fingerprint, session))

                wal_path = Path(f"{path}-wal")
                if wal_path.exists():
                    self.assertEqual(wal_path.stat().st_size, 0)
                self.assertNotIn(SOURCE.encode(), path.read_bytes())

    def test_two_connections_interoperate_with_upsert_and_wal(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.sqlite3"
            first = CompiledSessionCache(path)
            second = CompiledSessionCache(path)
            try:
                session = make_session()
                first.put("fake:v1", session, make_result().memory)
                self.assertEqual(second.get("fake:v1", session), make_result().memory)
                second.put("fake:v1", session, make_result().memory)
                self.assertEqual(first.get("fake:v1", session), make_result().memory)
                mode = (
                    sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0]
                )
                self.assertEqual(mode.casefold(), "wal")
                self.assertEqual(first.stats().entries, 1)
            finally:
                first.close()
                second.close()

    def test_concurrent_misses_make_exactly_one_underlying_compiler_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "singleflight.sqlite3"
            owner_cache = CompiledSessionCache(path)
            contender_cache = ObservedContenderCache(path)
            compiler = BlockingCompiler()
            owner = CachedMemoryCompiler(
                compiler,
                owner_cache,
                lease_ttl_seconds=0.15,
                wait_timeout_seconds=2,
                poll_interval_seconds=0.005,
            )
            contender = CachedMemoryCompiler(
                compiler,
                contender_cache,
                lease_ttl_seconds=0.15,
                wait_timeout_seconds=2,
                poll_interval_seconds=0.005,
            )
            results: dict[str, CompileResult] = {}
            errors: dict[str, BaseException] = {}

            def invoke(name: str, cached: CachedMemoryCompiler) -> None:
                try:
                    results[name] = cached.compile_session(make_session())
                except BaseException as error:
                    errors[name] = error

            owner_thread = threading.Thread(target=invoke, args=("owner", owner))
            contender_thread = threading.Thread(
                target=invoke, args=("contender", contender)
            )
            try:
                owner_thread.start()
                self.assertTrue(compiler.started.wait(1))
                contender_thread.start()
                self.assertTrue(contender_cache.contended.wait(1))
                self.assertEqual(compiler.calls, 1)

                # Keep the owner alive beyond its initial TTL to exercise renewal.
                time.sleep(0.2)
                self.assertEqual(compiler.calls, 1)
                compiler.release.set()
                owner_thread.join(2)
                contender_thread.join(2)

                self.assertFalse(owner_thread.is_alive())
                self.assertFalse(contender_thread.is_alive())
                self.assertEqual(errors, {})
                self.assertEqual(compiler.calls, 1)
                self.assertEqual(set(results), {"owner", "contender"})
                self.assertEqual(results["owner"].usage, make_result().usage)
                self.assertEqual(results["contender"].usage, ())
                with sqlite3.connect(path) as inspection:
                    leases = inspection.execute(
                        "SELECT COUNT(*) FROM compiler_cache_leases"
                    ).fetchone()[0]
                self.assertEqual(leases, 0)
            finally:
                compiler.release.set()
                owner_thread.join(2)
                contender_thread.join(2)
                owner_cache.close()
                contender_cache.close()

    def test_compiler_failure_releases_lease_for_immediate_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "failure.sqlite3"
            compiler = FailOnceCompiler()
            with CompiledSessionCache(path) as cache:
                cached = CachedMemoryCompiler(compiler, cache)
                with self.assertRaisesRegex(CompilerError, "synthetic failure"):
                    cached.compile_session(make_session())
                leases = cache._connection.execute(
                    "SELECT COUNT(*) FROM compiler_cache_leases"
                ).fetchone()[0]
                self.assertEqual(leases, 0)

                recovered = cached.compile_session(make_session())
                self.assertEqual(recovered.memory, make_result().memory)
                self.assertEqual(compiler.calls, 2)

    def test_expired_lease_is_recovered_without_raw_identity_columns(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stale.sqlite3"
            compiler = FakeCompiler()
            session = make_session()
            cache_key = compiled_session_cache_key(compiler.fingerprint, session)
            with CompiledSessionCache(path) as cache:
                stale = time.time() - 60
                with cache._connection:
                    cache._connection.execute(
                        """
                        INSERT INTO compiler_cache_leases (
                            cache_key, owner_token, acquired_at, expires_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (cache_key, "crashed-owner", stale - 10, stale, stale - 10),
                    )
                columns = {
                    row[1]
                    for row in cache._connection.execute(
                        "PRAGMA table_info(compiler_cache_leases)"
                    ).fetchall()
                }
                self.assertEqual(
                    columns,
                    {
                        "cache_key",
                        "owner_token",
                        "acquired_at",
                        "expires_at",
                        "updated_at",
                    },
                )

                result = CachedMemoryCompiler(compiler, cache).compile_session(session)
                self.assertEqual(result.memory, make_result().memory)
                self.assertEqual(compiler.calls, 1)
                self.assertEqual(
                    cache._connection.execute(
                        "SELECT COUNT(*) FROM compiler_cache_leases"
                    ).fetchone()[0],
                    0,
                )

    def test_active_lease_timeout_is_retryable_and_never_calls_compiler(self):
        compiler = FakeCompiler()
        session = make_session()
        with CompiledSessionCache(":memory:") as cache:
            cache_key = compiled_session_cache_key(compiler.fingerprint, session)
            self.assertTrue(
                cache._try_acquire_compile_lease(
                    cache_key,
                    "active-owner",
                    ttl_seconds=10,
                )
            )
            cached = CachedMemoryCompiler(
                compiler,
                cache,
                wait_timeout_seconds=0.03,
                poll_interval_seconds=0.005,
            )
            with self.assertRaises(CompilerError) as raised:
                cached.compile_session(session)

            self.assertEqual(raised.exception.code, "compiler_singleflight_unavailable")
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(compiler.calls, 0)
            self.assertTrue(cache._release_compile_lease(cache_key, "active-owner"))

    def test_in_memory_cache_supports_leases_and_normal_hits(self):
        compiler = FakeCompiler()
        with CompiledSessionCache(":memory:") as cache:
            cached = CachedMemoryCompiler(compiler, cache)
            first = cached.compile_session(make_session())
            second = cached.compile_session(make_session())

            self.assertEqual(first.memory, second.memory)
            self.assertEqual(first.usage, make_result().usage)
            self.assertEqual(second.usage, ())
            self.assertEqual(compiler.calls, 1)
            self.assertEqual(
                cache._connection.execute(
                    "SELECT COUNT(*) FROM compiler_cache_leases"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
