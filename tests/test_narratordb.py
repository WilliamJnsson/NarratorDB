#!/usr/bin/env python3
"""Regression tests for the standalone NarratorDB package."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from narratordb import Engine, NarratorDB
from narratordb.benchmark_server import NarratorDBBenchmarkBackend
from narratordb.config import default_db_path

ROOT = Path(__file__).resolve().parents[1]


class NarratorDBTests(unittest.TestCase):
    def test_public_name(self) -> None:
        self.assertEqual(NarratorDB.__name__, "NarratorDB")
        self.assertEqual(Engine.__name__, "Engine")

    def test_explicit_path_takes_precedence(self) -> None:
        old = os.environ.get("NARRATORDB_PATH")
        try:
            os.environ["NARRATORDB_PATH"] = "~/narrator-primary.db"
            self.assertEqual(default_db_path(), str(Path("~/narrator-primary.db").expanduser()))
        finally:
            if old is None:
                os.environ.pop("NARRATORDB_PATH", None)
            else:
                os.environ["NARRATORDB_PATH"] = old

    def test_crud_health_and_typed_only_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "memory.db")
            with Engine(path, user_id="test", semantic_dedup=False) as engine:
                message_id = engine.store("user", "NarratorDB remembers durable context")
                artifact_id = engine.store_artifact("note", "Architecture", "SQLite and FTS5")
                chunk_id = engine.store_code_chunk("src/store.py", "def remember(): pass", symbol="remember")
                relation_id = engine.link_records("artifact", artifact_id, "code_chunk", chunk_id, "documents")

                self.assertIsNotNone(message_id)
                self.assertIsNotNone(relation_id)
                self.assertTrue(engine.health_check(full=True)["ok"])
                self.assertEqual(engine.clear_scope(), 1)
                self.assertTrue(engine.health_check()["ok"])

                artifact_id = engine.store_artifact("note", "Typed only", "No messages in this scope")
                chunk_id = engine.store_code_chunk("typed.py", "VALUE = 1")
                engine.link_records("artifact", artifact_id, "code_chunk", chunk_id, "references")
                self.assertEqual(engine.clear_scope(), 0)
                stats = engine.stats()
                self.assertEqual(stats["artifact_count"], 0)
                self.assertEqual(stats["code_chunk_count"], 0)
                self.assertEqual(stats["relation_count"], 0)

    def test_cleanup_removes_linked_rows_and_refreshes_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "memory.db")
            with Engine(path, user_id="cleanup", semantic_dedup=False) as engine:
                first = engine.store("user", "old record", timestamp=1.0)
                second = engine.store("user", "current record", timestamp=2.0)
                engine.link_records("message", first, "message", second, "precedes", timestamp=3.0)
                self.assertEqual(engine.cleanup(max_messages=1), 1)
                self.assertEqual(engine.stats()["relation_count"], 0)
                self.assertTrue(engine.health_check()["ok"])

    def test_schema_v2_backfills_normalized_terms_without_changing_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "legacy.db")
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    speaker TEXT NOT NULL,
                    text TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    position INTEGER NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            connection.execute(
                """
                INSERT INTO messages(user_id, speaker, text, hash, timestamp, position)
                VALUES('legacy', 'user', 'Running durable migrations safely', 'legacy-hash', 1, 0)
                """
            )
            connection.commit()
            connection.close()

            with Engine(path, user_id="legacy", semantic_dedup=False) as engine:
                columns = {
                    row[1] for row in engine._conn.execute("PRAGMA table_info(messages)").fetchall()
                }
                self.assertIn("normalized_terms", columns)
                terms = engine._conn.execute(
                    "SELECT normalized_terms FROM messages WHERE user_id = 'legacy'"
                ).fetchone()[0]
                self.assertIn("durable", terms.split())
                result = engine.search(
                    "durable migration",
                    limit=10,
                    max_context=10,
                    full_context_threshold=0,
                )
                self.assertTrue(result.messages)
                self.assertEqual(result.timings_ms["total"], result.query_ms)
                self.assertTrue(engine.health_check(full=True)["ok"])

                engine._conn.execute(
                    "UPDATE messages SET normalized_terms = NULL WHERE user_id = 'legacy'"
                )
                engine._conn.commit()
                unhealthy = engine.health_check()
                self.assertFalse(unhealthy["ok"])
                self.assertEqual(unhealthy["missing_normalized_terms"], 1)

            # A normal restart repairs a partially populated v2 migration even
            # when the per-user terms version was already committed.
            with Engine(path, user_id="legacy", semantic_dedup=False) as repaired:
                self.assertTrue(repaired.health_check()["ok"])
                self.assertEqual(repaired.stats()["normalized_terms_version"], 1)

    def test_restart_backup_and_concurrent_writers_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "memory.db")
            backup_path = str(Path(directory) / "backup" / "memory.db")
            with Engine(path, user_id="durable", semantic_dedup=False) as engine:
                engine.store("user", "record present before restart")

            with Engine(path, user_id="durable", semantic_dedup=False) as engine:
                self.assertTrue(engine.search("present before restart").messages)
                backup = engine.backup(backup_path)
                self.assertEqual(backup["integrity_check"], ["ok"])

            errors = []

            def write(worker: int) -> None:
                try:
                    with Engine(path, user_id=f"writer-{worker}", semantic_dedup=False) as engine:
                        for index in range(10):
                            engine.store("user", f"worker {worker} durable message {index}")
                except Exception as error:  # pragma: no cover - asserted below
                    errors.append(error)

            threads = [threading.Thread(target=write, args=(worker,)) for worker in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            self.assertFalse(errors)
            self.assertTrue(all(not thread.is_alive() for thread in threads))

            for worker in range(4):
                with Engine(path, user_id=f"writer-{worker}", semantic_dedup=False) as engine:
                    self.assertEqual(engine.stats()["message_count"], 10)
                    self.assertTrue(engine.health_check(full=True)["ok"])

            with Engine(backup_path, user_id="durable", semantic_dedup=False) as restored:
                self.assertEqual(restored.stats()["message_count"], 1)
                self.assertTrue(restored.health_check(full=True)["ok"])

    def test_stdio_health_uses_narratordb_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bridge.db")
            env = os.environ.copy()
            env.update(
                {
                    "NARRATORDB_PATH": path,
                    "NARRATORDB_USER_ID": "bridge-test",
                    "NARRATORDB_LOCAL_ONLY": "1",
                }
            )
            request = json.dumps({"id": 1, "method": "health", "params": {"full": True}}) + "\n"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "narratordb" / "stdio.py")],
                input=request,
                text=True,
                capture_output=True,
                env=env,
                cwd=ROOT,
                timeout=60,
                check=True,
            )
            response = json.loads(completed.stdout.strip())
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["result"]["engine_name"], "NarratorDB")
            self.assertEqual(response["result"]["db_path"], path)
            self.assertTrue(response["result"]["health"]["ok"])

    def test_official_harness_backend_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = NarratorDBBenchmarkBackend(str(Path(directory) / "official.db"))
            try:
                added = backend.add(
                    {
                        "user_id": "official-test",
                        "timestamp": 1700000000,
                        "metadata": {"session_id": "session-1"},
                        "messages": [
                            {"role": "user", "content": "The launch code is cobalt seven."},
                            {"role": "assistant", "content": "I will retain that launch code."},
                        ],
                    }
                )
                self.assertEqual(len(added["results"]), 2)
                found = backend.search(
                    {"user_id": "official-test", "query": "launch code", "limit": 10}
                )
                self.assertTrue(found["results"])
                self.assertIn("cobalt seven", "\n".join(row["memory"] for row in found["results"]))
                self.assertEqual(backend.delete("official-test")["deleted"], 2)
            finally:
                backend.close()

    def test_benchmark_backend_searches_different_users_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = NarratorDBBenchmarkBackend(str(Path(directory) / "concurrent.db"))
            try:
                for user_id in ("concurrent-a", "concurrent-b"):
                    backend.add(
                        {
                            "user_id": user_id,
                            "metadata": {"session_id": user_id},
                            "messages": [{"role": "user", "content": f"launch code for {user_id}"}],
                        }
                    )

                barrier = threading.Barrier(2)
                for user_id in ("concurrent-a", "concurrent-b"):
                    engine = backend.engine(user_id)
                    original = engine.search

                    def delayed_search(*args, _original=original, **kwargs):
                        barrier.wait(timeout=2)
                        time.sleep(0.1)
                        return _original(*args, **kwargs)

                    engine.search = delayed_search

                errors = []

                def search(user_id: str) -> None:
                    try:
                        backend.search({"user_id": user_id, "query": "launch code", "limit": 10})
                    except Exception as error:  # pragma: no cover - asserted below
                        errors.append(error)

                started = time.perf_counter()
                threads = [
                    threading.Thread(target=search, args=(user_id,))
                    for user_id in ("concurrent-a", "concurrent-b")
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
                elapsed = time.perf_counter() - started
                self.assertFalse(errors)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertLess(elapsed, 0.19)
            finally:
                backend.close()

    def test_semantic_fallback_is_not_suppressed_by_generic_or_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "semantic-gap.db")
            with Engine(path, user_id="semantic-gap", context_window=0) as engine:
                if engine._sbert is None:
                    self.skipTest("semantic extras are not installed")
                first = engine.store(
                    "user",
                    "My larger tank currently has 7 neon swimmers and 2 small catfish.",
                )
                second = engine.store(
                    "user",
                    "My smaller tank contains one betta named Comet.",
                )
                engine.store(
                    "assistant",
                    "Aquarium decorations should provide safe hiding places and open water.",
                )
                result = engine.search(
                    "How many fish are in both of my aquariums combined?",
                    limit=20,
                    max_context=20,
                    full_context_threshold=0,
                )
                direct_ids = {message.id for message in result.direct_hits}
                self.assertIn(first, direct_ids)
                self.assertIn(second, direct_ids)

    def test_temporal_intent_distinguishes_current_target_from_previous_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "temporal-intent.db")
            with Engine(
                path,
                user_id="temporal-intent",
                context_window=0,
                semantic_dedup=False,
            ) as engine:
                engine.store(
                    "user",
                    "I met Morgan, an old colleague from my previous company, at a conference.",
                    timestamp=1.0,
                )
                current_company_id = engine.store(
                    "user",
                    "Morgan, an old colleague from my previous company, is currently at Aurora Labs.",
                    timestamp=2.0,
                )
                engine.store(
                    "ops",
                    "Noor Hale handled deployment safety for the Bastion rollout.",
                    timestamp=3.0,
                )
                engine.store(
                    "ops",
                    "Qwen Sentinel took over deployment safety for the current rollout.",
                    timestamp=4.0,
                )

                current_result = engine.search(
                    "What company is Morgan, an old colleague from my previous company, currently at?",
                    limit=20,
                    max_context=20,
                    full_context_threshold=0,
                )
                self.assertIn(current_company_id, [message.id for message in current_result.direct_hits])

                previous_result = engine.search(
                    "Who handled deployment safety before Qwen Sentinel?",
                    limit=20,
                    max_context=20,
                    full_context_threshold=0,
                )
                self.assertTrue(previous_result.direct_hits)
                self.assertIn("Noor Hale", previous_result.direct_hits[0].text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
