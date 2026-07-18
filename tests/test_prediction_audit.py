from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from narratordb.benchmarks.prediction_audit import audit_predictions


class PredictionAuditTests(unittest.TestCase):
    def test_prediction_directory_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    [
                        {
                            "question_id": "q1",
                            "answer_session_ids": ["answer-1"],
                            "haystack_session_ids": ["answer-1", "noise"],
                            "haystack_sessions": [
                                [{"role": "user", "content": "answer fact"}],
                                [{"role": "user", "content": "noise fact"}],
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            predictions = root / "predictions"
            predictions.mkdir()
            (predictions / "q1.json").write_text(
                json.dumps(
                    {
                        "question_id": "q1",
                        "question_type": "single-session-user",
                        "retrieval": {
                            "search_latency_ms": 2.0,
                            "search_results": [{"memory": "answer fact"}],
                            "query_debug": {
                                "query_ms": 1.0,
                                "backend_ms": 1.5,
                                "timings_ms": {"fts": 0.25, "total": 1.0},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            (predictions / "_ingestion_q1.json").write_text(
                json.dumps({"total_pairs_processed": 2, "total_pairs_failed": 0}),
                encoding="utf-8",
            )

            report = audit_predictions(predictions, dataset, require_all=True)

            self.assertTrue(report["complete"])
            self.assertEqual(report["questions"], 1)
            self.assertTrue(report["ingestion"]["complete"])
            self.assertEqual(report["ingestion"]["pairs_processed"], 2)
            self.assertEqual(report["evidence_session_coverage"]["10"]["recall_all"], 1)
            self.assertEqual(report["latency_ms"]["engine"]["mean"], 1.0)
            self.assertTrue(report["latency_ms"]["query_debug_retained_by_harness"])
            by_type = report["by_question_type"]["single-session-user"]
            self.assertEqual(by_type["questions"], 1)
            self.assertEqual(by_type["evidence_session_coverage"]["10"]["recall_all"], 1)
            self.assertEqual(by_type["official_harness_latency_ms"]["mean"], 2.0)

    def test_declared_cutoffs_and_ingestion_are_part_of_completeness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    [
                        {
                            "question_id": "q1",
                            "answer_session_ids": ["answer-1"],
                            "haystack_session_ids": ["answer-1"],
                            "haystack_sessions": [
                                [{"role": "user", "content": "answer fact"}]
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            predictions = root / "predictions"
            predictions.mkdir()
            (predictions / "q1.json").write_text(
                json.dumps(
                    {
                        "question_id": "q1",
                        "retrieval": {
                            "search_latency_ms": 2.0,
                            "search_results": [{"memory": "answer fact"}],
                        },
                    }
                ),
                encoding="utf-8",
            )

            report = audit_predictions(predictions, dataset, cutoffs=(20, 50))

            self.assertFalse(report["complete"])
            self.assertEqual(report["cutoffs"], [20, 50])
            self.assertTrue(report["evidence_session_coverage"]["20"]["recall_all"])
            self.assertFalse(report["ingestion"]["complete"])

    def test_unknown_prediction_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    [
                        {
                            "question_id": "q1",
                            "answer_session_ids": [],
                            "haystack_session_ids": [],
                            "haystack_sessions": [],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            predictions = root / "predictions"
            predictions.mkdir()
            (predictions / "not-in-dataset.json").write_text(
                json.dumps({"question_id": "not-in-dataset"}), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "absent from dataset"):
                audit_predictions(predictions, dataset)
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                audit_predictions(predictions, dataset, question_ids=set())

    def test_missing_query_debug_is_unavailable_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    [
                        {
                            "question_id": "q1",
                            "answer_session_ids": ["answer-1"],
                            "haystack_session_ids": ["answer-1"],
                            "haystack_sessions": [
                                [{"role": "user", "content": "answer fact"}]
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            predictions = root / "predictions"
            predictions.mkdir()
            (predictions / "q1.json").write_text(
                json.dumps(
                    {
                        "question_id": "q1",
                        "retrieval": {
                            "search_latency_ms": 2.0,
                            "search_results": [{"memory": "answer fact"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (predictions / "_ingestion_q1.json").write_text(
                json.dumps({"total_pairs_processed": 1, "total_pairs_failed": 0}),
                encoding="utf-8",
            )

            report = audit_predictions(predictions, dataset, require_all=True)

            self.assertIsNone(report["latency_ms"]["engine"])
            self.assertIsNone(report["latency_ms"]["backend"])
            self.assertFalse(report["latency_ms"]["query_debug_retained_by_harness"])

    def test_question_scope_can_audit_an_unseen_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset.json"
            dataset.write_text(
                json.dumps(
                    [
                        {
                            "question_id": question_id,
                            "answer_session_ids": [f"answer-{question_id}"],
                            "haystack_session_ids": [f"answer-{question_id}"],
                            "haystack_sessions": [
                                [{"role": "user", "content": f"fact {question_id}"}]
                            ],
                        }
                        for question_id in ("development", "holdout")
                    ]
                ),
                encoding="utf-8",
            )
            predictions = root / "predictions"
            predictions.mkdir()
            for question_id in ("development", "holdout"):
                (predictions / f"{question_id}.json").write_text(
                    json.dumps(
                        {
                            "question_id": question_id,
                            "retrieval": {
                                "search_latency_ms": 1.0,
                                "search_results": [{"memory": f"fact {question_id}"}],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                (predictions / f"_ingestion_{question_id}.json").write_text(
                    json.dumps({"total_pairs_processed": 1, "total_pairs_failed": 0}),
                    encoding="utf-8",
                )

            report = audit_predictions(
                predictions,
                dataset,
                require_all=True,
                question_ids={"holdout"},
            )

            self.assertEqual(report["dataset_questions"], 2)
            self.assertEqual(report["questions"], 1)
            self.assertTrue(report["scoped_question_subset"])
            self.assertTrue(report["complete"])


if __name__ == "__main__":
    unittest.main()
