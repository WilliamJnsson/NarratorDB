from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from narratordb.compiler import (
    CompileSessionInput,
    SourceMessage,
    parse_compiled_memory,
)
from narratordb.engine import Engine


class IntelligenceStoreTests(unittest.TestCase):
    def test_compiler_a_to_b_to_a_rematerializes_the_requested_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="compiler-lineage",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source_text = "The source-backed value is cobalt."
                stored = engine.store_session(
                    [{"role": "user", "content": source_text}],
                    session_id="lineage-session",
                )
                evidence = {
                    "message_id": stored["message_ids"][0],
                    "quote": source_text,
                }

                def materialize(fingerprint: str, label: str) -> int:
                    job_id = engine.enqueue_compilation(
                        stored["session_pk"],
                        stored["source_hash"],
                        fingerprint,
                    )
                    attempt = engine.claim_compilation_attempt(job_id)
                    self.assertIsNotNone(attempt)
                    result = engine.apply_compilation(
                        job_id,
                        {
                            "claims": [
                                {
                                    "kind": "fact",
                                    "text": f"Compiled by {label}.",
                                    "evidence": [evidence],
                                }
                            ]
                        },
                        processor=label,
                        processor_version=fingerprint,
                        prompt_version="test",
                        expected_attempt=attempt,
                    )
                    self.assertEqual(result["status"], "complete")
                    return job_id

                first_a = materialize("compiler-a", "A")
                compiler_b = materialize("compiler-b", "B")
                self.assertEqual(
                    engine._conn.execute(
                        "SELECT status FROM memory_compiler_jobs WHERE id = ?",
                        (first_a,),
                    ).fetchone()[0],
                    "obsolete",
                )

                reopened_a = engine.enqueue_compilation(
                    stored["session_pk"],
                    stored["source_hash"],
                    "compiler-a",
                )
                self.assertEqual(reopened_a, first_a)
                state = engine._conn.execute(
                    """
                    SELECT status, attempts, last_error, started_at, finished_at
                    FROM memory_compiler_jobs WHERE id = ?
                    """,
                    (reopened_a,),
                ).fetchone()
                self.assertEqual(tuple(state), ("pending", 0, None, None, None))
                self.assertEqual(
                    engine._conn.execute(
                        "SELECT status FROM memory_compiler_jobs WHERE id = ?",
                        (compiler_b,),
                    ).fetchone()[0],
                    "obsolete",
                )

                materialize("compiler-a", "A-again")
                claim = engine._conn.execute(
                    "SELECT text, processor_version FROM memory_claims"
                ).fetchone()
                self.assertEqual(tuple(claim), ("Compiled by A-again.", "compiler-a"))

    def test_reingest_same_session_replaces_membership_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="session-replacement",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                original = engine.store_session(
                    [
                        {"role": "user", "content": "The old first turn."},
                        {"role": "assistant", "content": "The old second turn."},
                    ],
                    session_id="replace-me",
                    occurred_at=100.0,
                )
                old_job = engine.enqueue_compilation(
                    original["session_pk"], original["source_hash"], "compiler-v1"
                )

                replacement = engine.store_session(
                    [{"role": "user", "content": "Only the replacement turn."}],
                    session_id="replace-me",
                    occurred_at=200.0,
                )
                self.assertEqual(replacement["session_pk"], original["session_pk"])
                self.assertEqual(len(replacement["message_ids"]), 1)
                self.assertTrue(
                    set(original["message_ids"]).isdisjoint(replacement["message_ids"])
                )
                self.assertNotEqual(replacement["source_hash"], original["source_hash"])

                current = engine.load_compiler_session(replacement["session_pk"])
                self.assertEqual(
                    [message["content"] for message in current["messages"]],
                    ["Only the replacement turn."],
                )
                # Replacement changes only derived membership; canonical raw
                # history remains available for audit and ordinary retrieval.
                self.assertEqual(engine.stats()["message_count"], 3)

                new_job = engine.enqueue_compilation(
                    replacement["session_pk"], replacement["source_hash"], "compiler-v1"
                )
                old_status = engine._conn.execute(
                    "SELECT status FROM memory_compiler_jobs WHERE id = ?", (old_job,)
                ).fetchone()[0]
                self.assertEqual(old_status, "obsolete")

                idempotent = engine.store_session(
                    [{"role": "user", "content": "Only the replacement turn."}],
                    session_id="replace-me",
                    occurred_at=200.0,
                )
                self.assertEqual(idempotent["stored"], 0)
                self.assertEqual(idempotent["message_ids"], replacement["message_ids"])
                self.assertEqual(idempotent["source_hash"], replacement["source_hash"])
                self.assertEqual(engine.stats()["message_count"], 3)
                self.assertEqual(
                    engine.enqueue_compilation(
                        idempotent["session_pk"],
                        idempotent["source_hash"],
                        "compiler-v1",
                    ),
                    new_job,
                )

    def test_store_session_rolls_back_raw_rows_if_registration_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="session-atomicity",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                register_session = engine._memory.register_session

                def fail_registration(*args, **kwargs):
                    raise RuntimeError("injected registration failure")

                engine._memory.register_session = fail_registration
                with self.assertRaisesRegex(
                    RuntimeError, "injected registration failure"
                ):
                    engine.store_session(
                        [{"role": "user", "content": "Must roll back atomically."}],
                        session_id="atomic-session",
                    )
                engine._memory.register_session = register_session

                self.assertEqual(engine.stats()["message_count"], 0)
                self.assertEqual(
                    engine._conn.execute(
                        "SELECT COUNT(*) FROM memory_sessions"
                    ).fetchone()[0],
                    0,
                )
                retry = engine.store_session(
                    [{"role": "user", "content": "Must roll back atomically."}],
                    session_id="atomic-session",
                )
                self.assertEqual(retry["stored"], 1)

    def test_exact_evidence_span_and_memory_keys_survive_claim_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="claim-hydration",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                text = "red then red and cobalt"
                session = engine.store_session(
                    [{"role": "user", "content": text}],
                    session_id="claim-session",
                )
                message_id = session["message_ids"][0]
                second_red = text.rindex("red")
                cobalt = text.index("cobalt")
                job_id = engine.enqueue_compilation(
                    session["session_pk"], session["source_hash"], "compiler-v1"
                )
                result = engine.apply_compilation(
                    job_id,
                    {
                        "claims": [
                            {
                                "kind": "preference",
                                "text": "The favorite marker is the second red token.",
                                "memory_key": "user.preference.marker",
                                "evidence": [
                                    {
                                        "message_id": message_id,
                                        "quote": "red",
                                        "start": second_red,
                                        "end": second_red + len("red"),
                                    }
                                ],
                            },
                            {
                                "kind": "fact",
                                "text": "The related datum is cobalt.",
                                "memory_key": "user.fact.related_datum",
                                "evidence": [
                                    {
                                        "message_id": message_id,
                                        "quote": "cobalt",
                                        "start": cobalt,
                                        "end": cobalt + len("cobalt"),
                                    }
                                ],
                            },
                        ],
                        "relations": [
                            {"source_index": 0, "target_index": 1, "type": "supports"}
                        ],
                    },
                    processor="test",
                    processor_version="1",
                    prompt_version="1",
                )
                self.assertEqual(result["status"], "complete")
                span = engine._conn.execute(
                    """
                    SELECT span_start, span_end FROM memory_claim_sources
                    WHERE quote = 'red'
                    """
                ).fetchone()
                self.assertEqual((span[0], span[1]), (second_red, second_red + 3))

                claims = engine._memory.search_claims("favorite marker", limit=10)
                by_text = {claim.text: claim for claim in claims}
                self.assertEqual(
                    by_text["The favorite marker is the second red token."].memory_key,
                    "user.preference.marker",
                )
                self.assertEqual(
                    by_text["The related datum is cobalt."].memory_key,
                    "user.fact.related_datum",
                )

    def test_public_compiler_dataclasses_round_trip_into_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="compiler-integration",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                session = engine.store_session(
                    [{"role": "user", "content": "I moved to Kyoto on 2025-01-02."}],
                    session_id="compiled-session",
                    occurred_at=1735862400.0,
                )
                message_id = str(session["message_ids"][0])
                compiled = parse_compiled_memory(
                    {
                        "summary": {
                            "text": "The user moved to Kyoto.",
                            "evidence": [
                                {
                                    "message_id": message_id,
                                    "quote": "I moved to Kyoto on 2025-01-02.",
                                    "start": None,
                                    "end": None,
                                }
                            ],
                        },
                        "entities": [
                            {
                                "entity_id": "e1",
                                "name": "Kyoto",
                                "entity_type": "place",
                                "aliases": [],
                                "evidence": [
                                    {
                                        "message_id": message_id,
                                        "quote": "Kyoto",
                                        "start": None,
                                        "end": None,
                                    }
                                ],
                            }
                        ],
                        "claims": [
                            {
                                "claim_id": "c1",
                                "kind": "event",
                                "text": "The user moved to Kyoto on 2025-01-02.",
                                "subject": "the user",
                                "predicate": "moved to",
                                "object_text": "Kyoto",
                                "memory_key": "user.residence.current_city",
                                "confidence": 0.99,
                                "status": "active",
                                "document_time": "2025-01-03T00:00:00Z",
                                "event_start": "2025-01-02",
                                "event_end": None,
                                "valid_from": "2025-01-02",
                                "valid_to": None,
                                "entity_ids": ["e1"],
                                "evidence": [
                                    {
                                        "message_id": message_id,
                                        "quote": "I moved to Kyoto on 2025-01-02.",
                                        "start": None,
                                        "end": None,
                                    }
                                ],
                            }
                        ],
                        "relations": [],
                    },
                    CompileSessionInput(
                        session_id="compiled-session",
                        document_time="2025-01-03T00:00:00Z",
                        messages=(
                            SourceMessage(
                                message_id=message_id,
                                role="user",
                                content="I moved to Kyoto on 2025-01-02.",
                            ),
                        ),
                    ),
                )
                job_id = engine.enqueue_compilation(
                    session["session_pk"], session["source_hash"], "compiler-v1"
                )
                stored = engine.apply_compilation(
                    job_id,
                    compiled,
                    processor="openrouter",
                    processor_version="luna-pro",
                    prompt_version="v1",
                )
                self.assertEqual(stored["claims_stored"], 2)
                row = engine._conn.execute(
                    "SELECT event_start FROM memory_claims WHERE kind = 'event'"
                ).fetchone()
                self.assertIsInstance(row[0], float)
                self.assertIn(
                    "Kyoto", engine.recall_context("Where did the user move?").text
                )

    def test_session_compilation_is_source_linked_and_current_aware(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="intelligence",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                session = engine.store_session(
                    [
                        {"role": "user", "content": "I used to live in Oslo."},
                        {"role": "user", "content": "I now live in Tokyo."},
                    ],
                    session_id="session-1",
                    occurred_at=100.0,
                )
                job_id = engine.enqueue_compilation(
                    session["session_pk"], session["source_hash"], "compiler-v1"
                )
                engine.mark_compilation_running(job_id)
                result = engine.apply_compilation(
                    job_id,
                    {
                        "summary": "The user moved from Oslo to Tokyo.",
                        "claims": [
                            {
                                "kind": "status",
                                "text": "The user lived in Oslo.",
                                "subject": "user",
                                "predicate": "city",
                                "object_text": "Oslo",
                                "evidence": [
                                    {
                                        "message_id": session["message_ids"][0],
                                        "quote": "I used to live in Oslo.",
                                    }
                                ],
                            },
                            {
                                "kind": "status",
                                "text": "The user currently lives in Tokyo.",
                                "subject": "user",
                                "predicate": "city",
                                "object_text": "Tokyo",
                                "evidence": [
                                    {
                                        "message_id": session["message_ids"][1],
                                        "quote": "I now live in Tokyo.",
                                    }
                                ],
                            },
                        ],
                        "relations": [
                            {"source_index": 1, "target_index": 0, "type": "updates"}
                        ],
                    },
                    processor="test",
                    processor_version="1",
                    prompt_version="1",
                    usage={
                        "provider": "test",
                        "model": "compiler",
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cost_usd": 0.01,
                    },
                )
                self.assertIn(result["status"], {"complete", "partial"})
                self.assertEqual(result["claims_stored"], 3)

                bundle = engine.recall_context(
                    "Where does the user currently live?",
                    token_budget=600,
                    explain=True,
                )
                self.assertLessEqual(bundle.token_count, 600)
                self.assertIn("Tokyo", bundle.text)
                self.assertNotIn("claim:1] The user lived in Oslo", bundle.text)
                self.assertIn("message:", bundle.text)
                self.assertGreaterEqual(bundle.debug["claim_blocks"], 1)

                status = engine.enrichment_status()
                self.assertEqual(status["jobs"][result["status"]], 1)
                self.assertEqual(status["usage"]["cost_usd"], 0.01)
                self.assertTrue(engine.health_check(full=True)["ok"])

    def test_stable_memory_key_supersedes_prior_session_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="cross-session-update",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                for index, city in enumerate(("Oslo", "Tokyo"), start=1):
                    text = f"I now live in {city}."
                    session = engine.store_session(
                        [{"role": "user", "content": text}],
                        session_id=f"session-{index}",
                        occurred_at=float(index * 100),
                    )
                    job_id = engine.enqueue_compilation(
                        session["session_pk"], session["source_hash"], "compiler-v1"
                    )
                    engine.apply_compilation(
                        job_id,
                        {
                            "claims": [
                                {
                                    "kind": "status",
                                    "text": f"The user currently lives in {city}.",
                                    "subject": "user",
                                    "predicate": "lives in",
                                    "object_text": city,
                                    "memory_key": "user.residence.current_city",
                                    "evidence": [
                                        {
                                            "message_id": session["message_ids"][0],
                                            "quote": text,
                                        }
                                    ],
                                }
                            ]
                        },
                        processor="test",
                        processor_version="1",
                        prompt_version="1",
                    )

                rows = engine._conn.execute(
                    "SELECT object_text, status, valid_to FROM memory_claims "
                    "WHERE memory_key = ? ORDER BY id",
                    ("user.residence.current_city",),
                ).fetchall()
                self.assertEqual(
                    [(row[0], row[1]) for row in rows],
                    [
                        ("Oslo", "superseded"),
                        ("Tokyo", "active"),
                    ],
                )
                self.assertEqual(rows[0][2], 200.0)
                relation = engine._conn.execute(
                    "SELECT relation_type FROM memory_claim_relations"
                ).fetchone()
                self.assertEqual(relation[0], "updates")

                current = engine.recall_context(
                    "Where does the user currently live?", token_budget=400
                )
                self.assertIn("Tokyo", current.text)
                self.assertNotIn(
                    "claim:1] The user currently lives in Oslo", current.text
                )

                implicit_current = engine.recall_context(
                    "Where does the user live?", token_budget=400
                )
                self.assertIn("Tokyo", implicit_current.text)
                self.assertNotIn(
                    "claim:1] The user currently lives in Oslo", implicit_current.text
                )

                history = engine.recall_context(
                    "Where did the user live before Tokyo?", token_budget=600
                )
                self.assertIn("Oslo", history.text)

    def test_memory_key_format_drift_still_consolidates_cross_session_status(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="formatted-cross-session-update",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                fixtures = (
                    (
                        "waiting",
                        100.0,
                        "The parcel is waiting for pickup.",
                        "User/Tasks/Parcel-State",
                    ),
                    (
                        "collected",
                        200.0,
                        "The parcel was collected.",
                        "user.tasks.parcel state",
                    ),
                )
                for state, occurred_at, text, memory_key in fixtures:
                    session = engine.store_session(
                        [{"role": "user", "content": text}],
                        session_id=f"parcel-{state}",
                        occurred_at=occurred_at,
                    )
                    job_id = engine.enqueue_compilation(
                        session["session_pk"], session["source_hash"], "compiler-v1"
                    )
                    engine.apply_compilation(
                        job_id,
                        {
                            "claims": [
                                {
                                    "kind": "status",
                                    "text": f"The parcel status is {state}.",
                                    "subject": "parcel",
                                    "predicate": "status",
                                    "object_text": state,
                                    "memory_key": memory_key,
                                    "evidence": [
                                        {
                                            "message_id": session["message_ids"][0],
                                            "quote": text,
                                        }
                                    ],
                                }
                            ]
                        },
                        processor="test",
                        processor_version="1",
                        prompt_version="1",
                    )

                rows = engine._conn.execute(
                    "SELECT memory_key, object_text, status FROM memory_claims "
                    "ORDER BY document_time"
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in rows],
                    [
                        ("user.tasks.parcel_state", "waiting", "superseded"),
                        ("user.tasks.parcel_state", "collected", "active"),
                    ],
                )
                current = engine._memory.search_claims("parcel status", limit=10)
                self.assertEqual(
                    [(claim.object_text, claim.status) for claim in current],
                    [("collected", "active")],
                )

    def test_memory_key_format_drift_consolidates_within_one_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="formatted-intra-session-update",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                text = "The parcel was waiting and was then collected."
                session = engine.store_session(
                    [{"role": "user", "content": text}],
                    session_id="parcel-history",
                    occurred_at=100.0,
                )
                job_id = engine.enqueue_compilation(
                    session["session_pk"], session["source_hash"], "compiler-v1"
                )
                engine.apply_compilation(
                    job_id,
                    {
                        "claims": [
                            {
                                "kind": "status",
                                "text": "The parcel status was waiting.",
                                "object_text": "waiting",
                                "memory_key": "User/Tasks/Parcel-State",
                                "evidence": [
                                    {
                                        "message_id": session["message_ids"][0],
                                        "quote": text,
                                    }
                                ],
                            },
                            {
                                "kind": "status",
                                "text": "The parcel status is collected.",
                                "object_text": "collected",
                                "memory_key": "user.tasks.parcel state",
                                "evidence": [
                                    {
                                        "message_id": session["message_ids"][0],
                                        "quote": text,
                                    }
                                ],
                            },
                        ]
                    },
                    processor="test",
                    processor_version="1",
                    prompt_version="1",
                )

                rows = engine._conn.execute(
                    "SELECT memory_key, object_text, status, valid_to "
                    "FROM memory_claims ORDER BY id"
                ).fetchall()
                self.assertEqual(
                    [(row[0], row[1], row[2]) for row in rows],
                    [
                        ("user.tasks.parcel_state", "waiting", "superseded"),
                        ("user.tasks.parcel_state", "collected", "active"),
                    ],
                )
                self.assertEqual(rows[0][3], 100.0)
                self.assertIsNone(rows[1][3])

    def test_legacy_memory_keys_migrate_per_user_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "memory.db")
            with Engine(
                database_path,
                user_id="legacy-key-migration",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                claim_ids = []
                for index, state in enumerate(("waiting", "collected"), start=1):
                    text = f"The parcel state is {state}."
                    session = engine.store_session(
                        [{"role": "user", "content": text}],
                        session_id=f"legacy-{state}",
                        occurred_at=float(index * 100),
                    )
                    job_id = engine.enqueue_compilation(
                        session["session_pk"], session["source_hash"], "compiler-v1"
                    )
                    engine.apply_compilation(
                        job_id,
                        {
                            "claims": [
                                {
                                    "kind": "status",
                                    "text": text,
                                    "object_text": state,
                                    "memory_key": f"legacy.parcel.{state}",
                                    "evidence": [
                                        {
                                            "message_id": session["message_ids"][0],
                                            "quote": text,
                                        }
                                    ],
                                }
                            ]
                        },
                        processor="test",
                        processor_version="1",
                        prompt_version="1",
                    )
                    claim_ids.append(
                        int(
                            engine._conn.execute(
                                "SELECT id FROM memory_claims WHERE session_id = ?",
                                (session["session_pk"],),
                            ).fetchone()[0]
                        )
                    )

                legacy_keys = (
                    "User/Tasks/Parcel-State",
                    "user.tasks.parcel state",
                )
                for claim_id, legacy_key in zip(claim_ids, legacy_keys):
                    engine._conn.execute(
                        "UPDATE memory_claims SET memory_key = ?, status = 'active', "
                        "valid_to = NULL WHERE id = ?",
                        (legacy_key, claim_id),
                    )
                    engine._conn.execute(
                        f"UPDATE {engine._memory.fts_table} SET status = 'active' "
                        "WHERE rowid = ?",
                        (claim_id,),
                    )
                engine._conn.execute(
                    "DELETE FROM metadata WHERE key = ?",
                    (f"{engine._memory.fts_table}_memory_key_version",),
                )
                engine._conn.commit()

            with Engine(
                database_path,
                user_id="legacy-key-migration",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                rows = engine._conn.execute(
                    "SELECT memory_key, object_text, status FROM memory_claims "
                    "ORDER BY document_time"
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in rows],
                    [
                        ("user.tasks.parcel_state", "waiting", "superseded"),
                        ("user.tasks.parcel_state", "collected", "active"),
                    ],
                )
                self.assertEqual(
                    engine.enrichment_status()["memory_key_format_version"], 2
                )
                self.assertEqual(
                    engine.health_check()["derived_memory"][
                        "memory_key_format_version"
                    ],
                    {"expected": 2, "actual": 2},
                )

    def test_late_older_session_cannot_supersede_newer_active_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="late-cross-session-update",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                sessions = {}
                for name, city, occurred_at in (
                    ("older", "Oslo", 100.0),
                    ("newer", "Tokyo", 200.0),
                ):
                    text = f"I now live in {city}."
                    sessions[name] = {
                        "city": city,
                        "text": text,
                        "stored": engine.store_session(
                            [{"role": "user", "content": text}],
                            session_id=f"session-{name}",
                            occurred_at=occurred_at,
                        ),
                    }

                # Enrichment completes out of order: the historical session is late.
                for name in ("newer", "older"):
                    item = sessions[name]
                    stored = item["stored"]
                    job_id = engine.enqueue_compilation(
                        stored["session_pk"], stored["source_hash"], "compiler-v1"
                    )
                    engine.apply_compilation(
                        job_id,
                        {
                            "claims": [
                                {
                                    "kind": "status",
                                    "text": (
                                        f"The user currently lives in {item['city']}."
                                    ),
                                    "subject": "user",
                                    "predicate": "lives in",
                                    "object_text": item["city"],
                                    "memory_key": "user.residence.current_city",
                                    "evidence": [
                                        {
                                            "message_id": stored["message_ids"][0],
                                            "quote": item["text"],
                                        }
                                    ],
                                }
                            ]
                        },
                        processor="test",
                        processor_version="1",
                        prompt_version="1",
                    )

                rows = engine._conn.execute(
                    """
                    SELECT s.external_id, c.object_text, c.status, c.valid_to
                    FROM memory_claims c
                    JOIN memory_sessions s ON s.id = c.session_id
                    WHERE c.memory_key = ?
                    ORDER BY s.occurred_at
                    """,
                    ("user.residence.current_city",),
                ).fetchall()
                self.assertEqual(
                    [(row[0], row[1], row[2]) for row in rows],
                    [
                        ("session-older", "Oslo", "superseded"),
                        ("session-newer", "Tokyo", "active"),
                    ],
                )
                self.assertEqual(rows[0][3], 200.0)
                self.assertIsNone(rows[1][3])
                relation = engine._conn.execute(
                    """
                    SELECT source.object_text, target.object_text
                    FROM memory_claim_relations relation
                    JOIN memory_claims source ON source.id = relation.source_claim_id
                    JOIN memory_claims target ON target.id = relation.target_claim_id
                    WHERE relation.relation_type = 'updates'
                    """
                ).fetchall()
                self.assertEqual(
                    [(row[0], row[1]) for row in relation], [("Tokyo", "Oslo")]
                )

    def test_ranked_claims_keep_compact_text_and_typed_storage_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="numeric-evidence-rendering",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                fixtures = (
                    ("alpha", "Volume Alpha contains 120 pages.", "120 pages"),
                    ("beta", "Volume Beta contains 80 pages.", "80 pages"),
                )
                for index, (name, text, quantity) in enumerate(fixtures, start=1):
                    session = engine.store_session(
                        [{"role": "user", "content": text}],
                        session_id=f"volume-{name}",
                        occurred_at=float(index * 100),
                    )
                    job_id = engine.enqueue_compilation(
                        session["session_pk"], session["source_hash"], "compiler-v1"
                    )
                    engine.apply_compilation(
                        job_id,
                        {
                            "claims": [
                                {
                                    "kind": "fact",
                                    "text": text,
                                    "subject": f"volume {name}",
                                    "predicate": "page count",
                                    "object_text": quantity,
                                    "memory_key": "",
                                    "evidence": [
                                        {
                                            "message_id": session["message_ids"][0],
                                            "quote": text,
                                        }
                                    ],
                                }
                            ]
                        },
                        processor="test",
                        processor_version="1",
                        prompt_version="1",
                    )

                blocks = engine._memory.rank_memories(
                    "page count",
                    [],
                    limit=10,
                    max_chars=500,
                    include_session_siblings=False,
                )
                claims = [block for block in blocks if block.kind == "claim"]
                self.assertEqual(len(claims), 2)
                self.assertEqual(
                    {block.text for block in claims},
                    {
                        "[active | fact] Volume Alpha contains 120 pages.",
                        "[active | fact] Volume Beta contains 80 pages.",
                    },
                )
                self.assertTrue(all(len(block.text) <= 500 for block in claims))
                self.assertTrue(
                    all("typed_evidence" not in block.channels for block in claims)
                )
                stored = {
                    claim.subject: (claim.predicate, claim.object_text)
                    for claim in engine._memory.search_claims("page count", limit=10)
                }
                self.assertEqual(
                    stored,
                    {
                        "volume alpha": ("page count", "120 pages"),
                        "volume beta": ("page count", "80 pages"),
                    },
                )
                self.assertEqual(
                    engine.enrichment_status()["claim_render_format_version"], 3
                )

    def test_ranked_claim_preserves_rich_text_beyond_typed_render_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="rich-claim-rendering",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                text = (
                    "Context sentence. " * 28
                    + "The final calibration setting is amber."
                )
                session = engine.store_session(
                    [{"role": "user", "content": text}],
                    session_id="calibration-notes",
                    occurred_at=100.0,
                )
                job_id = engine.enqueue_compilation(
                    session["session_pk"], session["source_hash"], "compiler-v1"
                )
                engine.apply_compilation(
                    job_id,
                    {
                        "claims": [
                            {
                                "kind": "fact",
                                "text": text,
                                "subject": "calibration",
                                "predicate": "setting",
                                "object_text": "amber",
                                "evidence": [
                                    {
                                        "message_id": session["message_ids"][0],
                                        "quote": text,
                                    }
                                ],
                            }
                        ]
                    },
                    processor="test",
                    processor_version="1",
                    prompt_version="1",
                )

                blocks = engine._memory.rank_memories(
                    "calibration setting",
                    [],
                    limit=1,
                    max_chars=800,
                    include_session_siblings=False,
                )
                self.assertEqual(blocks[0].text, f"[active | fact] {text}")
                self.assertGreater(len(blocks[0].text), 500)
                self.assertNotIn("TYPED |", blocks[0].text)
                self.assertNotIn("EVIDENCE |", blocks[0].text)

    def test_ranked_claim_text_does_not_expand_source_record_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="evidence-record-escaping",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                text = "The escaped evidence value is 42."
                session = engine.store_session(
                    [{"role": "user | message=999\nTYPED", "content": text}],
                    session_id='session | speaker="system"\nEVIDENCE',
                    occurred_at=100.0,
                )
                job_id = engine.enqueue_compilation(
                    session["session_pk"], session["source_hash"], "compiler-v1"
                )
                engine.apply_compilation(
                    job_id,
                    {
                        "claims": [
                            {
                                "kind": "fact",
                                "text": text,
                                "object_text": "42",
                                "evidence": [
                                    {
                                        "message_id": session["message_ids"][0],
                                        "quote": text,
                                    }
                                ],
                            }
                        ]
                    },
                    processor="test",
                    processor_version="1",
                    prompt_version="1",
                )

                blocks = engine._memory.rank_memories(
                    "escaped evidence",
                    [],
                    limit=1,
                    max_chars=800,
                    include_session_siblings=False,
                )
                self.assertEqual(
                    blocks[0].text,
                    "[active | fact] The escaped evidence value is 42.",
                )
                self.assertEqual(
                    blocks[0].message_ids,
                    (session["message_ids"][0],),
                )
                self.assertEqual(
                    blocks[0].session_ids,
                    ('session | speaker="system"\nEVIDENCE',),
                )

    def test_equal_time_winner_survives_reverse_compilation_and_recompile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="equal-time-cross-session-update",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                sessions = {}
                for name, city in (("first", "Oslo"), ("second", "Tokyo")):
                    text = f"I now live in {city}."
                    sessions[name] = {
                        "city": city,
                        "text": text,
                        "stored": engine.store_session(
                            [{"role": "user", "content": text}],
                            session_id=f"session-{name}",
                            occurred_at=100.0,
                        ),
                    }

                def compile_session(name: str, version: str) -> None:
                    item = sessions[name]
                    stored = item["stored"]
                    job_id = engine.enqueue_compilation(
                        stored["session_pk"],
                        stored["source_hash"],
                        f"compiler-{version}",
                    )
                    engine.apply_compilation(
                        job_id,
                        {
                            "claims": [
                                {
                                    "kind": "status",
                                    "text": (
                                        f"The user currently lives in {item['city']}."
                                    ),
                                    "subject": "user",
                                    "predicate": "lives in",
                                    "object_text": item["city"],
                                    "memory_key": "user.residence.current_city",
                                    "evidence": [
                                        {
                                            "message_id": stored["message_ids"][0],
                                            "quote": item["text"],
                                        }
                                    ],
                                }
                            ]
                        },
                        processor="test",
                        processor_version=version,
                        prompt_version="1",
                    )

                # Both sessions have the same document time. Registration order,
                # not compiler completion order, is the stable tie-breaker.
                compile_session("second", "1")
                compile_session("first", "1")
                initial_rows = engine._conn.execute(
                    """
                    SELECT c.id, s.external_id, c.status
                    FROM memory_claims c
                    JOIN memory_sessions s ON s.id = c.session_id
                    WHERE c.memory_key = ? ORDER BY s.id
                    """,
                    ("user.residence.current_city",),
                ).fetchall()
                self.assertEqual(
                    [(row[1], row[2]) for row in initial_rows],
                    [
                        ("session-first", "superseded"),
                        ("session-second", "active"),
                    ],
                )

                compile_session("first", "2")
                compile_session("second", "2")
                recompiled_rows = engine._conn.execute(
                    """
                    SELECT c.id, s.external_id, c.status, c.valid_to
                    FROM memory_claims c
                    JOIN memory_sessions s ON s.id = c.session_id
                    WHERE c.memory_key = ? ORDER BY s.id
                    """,
                    ("user.residence.current_city",),
                ).fetchall()
                self.assertEqual(
                    [(row[1], row[2]) for row in recompiled_rows],
                    [
                        ("session-first", "superseded"),
                        ("session-second", "active"),
                    ],
                )
                self.assertEqual(recompiled_rows[0][3], 100.0)
                self.assertIsNone(recompiled_rows[1][3])
                self.assertNotEqual(
                    [row[0] for row in initial_rows],
                    [row[0] for row in recompiled_rows],
                )
                relation = engine._conn.execute(
                    """
                    SELECT source.object_text, target.object_text
                    FROM memory_claim_relations relation
                    JOIN memory_claims source ON source.id = relation.source_claim_id
                    JOIN memory_claims target ON target.id = relation.target_claim_id
                    WHERE relation.relation_type = 'updates'
                    """
                ).fetchall()
                self.assertEqual(
                    [(row[0], row[1]) for row in relation], [("Tokyo", "Oslo")]
                )

    def test_invalid_evidence_is_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="evidence",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                session = engine.store_session(
                    [{"role": "user", "content": "The code is cobalt."}],
                    session_id="session-evidence",
                )
                job_id = engine.enqueue_compilation(
                    session["session_pk"], session["source_hash"], "compiler-v1"
                )
                result = engine.apply_compilation(
                    job_id,
                    {
                        "claims": [
                            {
                                "kind": "fact",
                                "text": "The code is fabricated.",
                                "evidence": [
                                    {
                                        "message_id": session["message_ids"][0],
                                        "quote": "This quote does not exist.",
                                    }
                                ],
                            }
                        ]
                    },
                    processor="test",
                    processor_version="1",
                    prompt_version="1",
                )
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["claims_stored"], 0)
                self.assertEqual(engine.enrichment_status()["claim_count"], 0)

    def test_exact_evidence_preserves_trailing_whitespace_at_resolved_offsets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="evidence-whitespace",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                text = "First line \nSecond line"
                quote = "First line \n"
                stored = engine.store_session(
                    [{"role": "user", "content": text}],
                    session_id="session-evidence-whitespace",
                )
                job_id = engine.enqueue_compilation(
                    stored["session_pk"], stored["source_hash"], "compiler-v1"
                )

                result = engine.apply_compilation(
                    job_id,
                    {
                        "claims": [
                            {
                                "kind": "fact",
                                "text": "The source contains a first line.",
                                "evidence": [
                                    {
                                        "message_id": stored["message_ids"][0],
                                        "quote": quote,
                                        "start": 0,
                                        "end": len(quote),
                                    }
                                ],
                            }
                        ]
                    },
                    processor="test",
                    processor_version="1",
                    prompt_version="1",
                )

                self.assertEqual(result["status"], "complete")
                source = engine._conn.execute(
                    "SELECT quote, span_start, span_end FROM memory_claim_sources"
                ).fetchone()
                self.assertEqual(tuple(source), (quote, 0, len(quote)))

    def test_duplicate_derived_claim_is_skipped_without_aborting_materialization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="duplicate-derived-claim",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                text = "The user prefers Kyoto."
                stored = engine.store_session(
                    [{"role": "user", "content": text}],
                    session_id="session-1",
                )
                job_id = engine.enqueue_compilation(
                    stored["session_pk"], stored["source_hash"], "compiler-v1"
                )
                claim = {
                    "kind": "preference",
                    "text": "The user prefers Kyoto.",
                    "subject": "user",
                    "predicate": "prefers",
                    "object_text": "Kyoto",
                    "memory_key": "user.preference.city",
                    "evidence": [
                        {
                            "message_id": stored["message_ids"][0],
                            "quote": text,
                        }
                    ],
                }

                result = engine.apply_compilation(
                    job_id,
                    {"claims": [claim, dict(claim)]},
                    processor="test",
                    processor_version="1",
                    prompt_version="1",
                )

                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["claims_stored"], 1)
                self.assertIn("duplicate derived claim", result["warnings"][0])
                count = engine._conn.execute(
                    "SELECT COUNT(*) FROM memory_claims"
                ).fetchone()[0]
                self.assertEqual(count, 1)

    def test_full_context_respects_hard_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="context-cap",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                for index in range(10):
                    engine.store("user", f"small corpus message {index}")
                result = engine.search(
                    "small corpus",
                    max_context=3,
                    full_context_threshold=100,
                )
                self.assertEqual(len(result.messages), 3)
                self.assertEqual(result.total_matches, 10)

    def test_semantic_candidates_obey_provenance_filters_before_fusion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="semantic-filter",
                semantic_dedup=False,
                semantic_search_mode="hybrid",
                local_only=True,
            ) as engine:
                allowed = engine.store(
                    "user",
                    "Cobalt launch detail.",
                    provenance={"run_id": "allowed"},
                )
                blocked = engine.store(
                    "user",
                    "Amber launch detail.",
                    provenance={"run_id": "blocked"},
                )
                rows = engine._conn.execute(
                    "SELECT id, position, timestamp FROM messages WHERE user_id = ? ORDER BY id",
                    (engine.user_id,),
                ).fetchall()
                by_id = {int(row[0]): row for row in rows}
                engine._sbert = object()
                engine._semantic_search = lambda query, limit=15: [
                    (blocked, by_id[blocked][1], by_id[blocked][2], -0.99),
                    (allowed, by_id[allowed][1], by_id[allowed][2], -0.98),
                ]
                result = engine.search(
                    "unmatched vocabulary",
                    limit=10,
                    max_context=10,
                    full_context_threshold=0,
                    filters={"run_id": "allowed"},
                )
                ids = {message.id for message in result.messages}
                self.assertIn(allowed, ids)
                self.assertNotIn(blocked, ids)


if __name__ == "__main__":
    unittest.main()
