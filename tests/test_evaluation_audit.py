from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from narratordb.benchmarks.evaluation_audit import audit_evaluation


class EvaluationAuditTests(unittest.TestCase):
    @staticmethod
    def _prediction(question_id: str) -> dict:
        return {
            "question_id": question_id,
            "question_type": "single-session-user",
            "question": "What happened?",
            "ground_truth_answer": "It passed.",
            "retrieval": {"search_results": [{"memory": "It passed."}]},
        }

    def test_complete_evaluation_matches_frozen_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = root / "frozen"
            evaluated = root / "evaluated"
            frozen.mkdir()
            evaluated.mkdir()
            prediction = self._prediction("q1")
            (frozen / "q1.json").write_text(json.dumps(prediction), encoding="utf-8")
            completed = dict(prediction)
            completed["cutoff_results"] = {
                cutoff: {
                    "score": 1.0,
                    "judgment": "PASS",
                    "generated_answer": "It passed.",
                    "judge_raw": "yes",
                }
                for cutoff in ("top_10", "top_20", "top_50", "top_200")
            }
            (evaluated / "q1.json").write_text(json.dumps(completed), encoding="utf-8")
            usage = root / "usage.jsonl"
            usage.write_text(
                json.dumps(
                    {
                        "event": "completion",
                        "provider": "StreamLake",
                        "request_model": "deepseek/v4",
                        "status": 200,
                        "finish_reason": "stop",
                        "cost_usd": 0.01,
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "reasoning_tokens": 1,
                        "timestamp": "2026-07-14T00:00:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            evaluator_log = root / "evaluate.log"
            evaluator_log.write_text(
                "Generation attempt 1/5 failed: temporary\n"
                "Generation returned None (finish_reason=stop)\n",
                encoding="utf-8",
            )

            report = audit_evaluation(
                evaluated,
                frozen_directory=frozen,
                usage_log=usage,
                evaluator_log=evaluator_log,
                expected_questions=1,
            )

            self.assertTrue(report["complete"])
            self.assertEqual(report["metrics"]["top_10"]["accuracy"], 1.0)
            self.assertEqual(report["usage"]["completion_calls"], 1)
            self.assertEqual(report["usage"]["cached_tokens"], 0)
            self.assertEqual(report["usage"]["malformed_http_200_responses"], 0)
            self.assertEqual(report["harness_log"]["failed_attempt_counts"], {"1": 1})
            self.assertEqual(report["harness_log"]["returned_none_responses"], 1)
            self.assertEqual(report["harness_log"]["attempt_five_failures"], 0)
            self.assertEqual(report["validation"]["frozen_payload_mismatches"], [])

    def test_usage_identity_sentinel_and_unknown_cost_block_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluated = root / "evaluated"
            evaluated.mkdir()
            completed = self._prediction("q1")
            completed["cutoff_results"] = {
                cutoff: {
                    "score": 1.0,
                    "judgment": "PASS",
                    "generated_answer": "It passed.",
                    "judge_raw": "yes",
                }
                for cutoff in ("top_10", "top_20", "top_50", "top_200")
            }
            (evaluated / "q1.json").write_text(json.dumps(completed), encoding="utf-8")
            usage = root / "usage.jsonl"
            usage.write_text(
                json.dumps(
                    {
                        "event": "completion",
                        "provider": "route_mismatch",
                        "request_model": "deepseek/v4",
                        "response_model": "route_mismatch",
                        "status": 200,
                        "finish_reason": "stop",
                        "response_complete": True,
                        "cost_usd": 0.01,
                        "unknown_cost": True,
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "reasoning_tokens": 1,
                        "timestamp": "2026-07-14T00:00:00+00:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = audit_evaluation(
                evaluated,
                usage_log=usage,
                expected_questions=1,
            )

            self.assertTrue(report["official_harness_score_complete"])
            self.assertFalse(report["complete"])
            self.assertFalse(report["usage"]["publication_ready"])
            self.assertEqual(report["usage"]["invalid_completion_identities"], 1)
            self.assertEqual(report["usage"]["unknown_cost_attempts"], 1)

    def test_empty_judge_and_retrieval_mutation_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = root / "frozen"
            evaluated = root / "evaluated"
            frozen.mkdir()
            evaluated.mkdir()
            prediction = self._prediction("q1")
            (frozen / "q1.json").write_text(json.dumps(prediction), encoding="utf-8")
            completed = self._prediction("q1")
            completed["retrieval"] = {"search_results": [{"memory": "changed"}]}
            completed["cutoff_results"] = {
                cutoff: {
                    "score": 0.0,
                    "judgment": "FAIL",
                    "generated_answer": "It failed.",
                    "judge_raw": "" if cutoff == "top_10" else "no",
                }
                for cutoff in ("top_10", "top_20", "top_50", "top_200")
            }
            (evaluated / "q1.json").write_text(json.dumps(completed), encoding="utf-8")

            report = audit_evaluation(
                evaluated,
                frozen_directory=frozen,
                expected_questions=1,
            )

            self.assertFalse(report["complete"])
            self.assertEqual(len(report["validation"]["empty_judges"]), 1)
            self.assertEqual(len(report["validation"]["frozen_payload_mismatches"]), 1)

    def test_boolean_score_and_non_string_outputs_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluated = root / "evaluated"
            evaluated.mkdir()
            completed = self._prediction("q1")
            completed["cutoff_results"] = {
                cutoff: {
                    "score": 1 if cutoff == "top_10" else True,
                    "judgment": "PASS",
                    "generated_answer": {"not": "text"},
                    "judge_raw": ["not text"],
                }
                for cutoff in ("top_10", "top_20", "top_50", "top_200")
            }
            (evaluated / "q1.json").write_text(json.dumps(completed), encoding="utf-8")

            report = audit_evaluation(evaluated, expected_questions=1)

            self.assertFalse(report["complete"])
            self.assertFalse(report["official_harness_score_complete"])
            self.assertEqual(len(report["validation"]["invalid_scores"]), 3)
            self.assertEqual(len(report["validation"]["empty_answers"]), 1)
            self.assertEqual(len(report["validation"]["empty_judges"]), 1)

    def test_official_score_can_retain_an_empty_model_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evaluated = Path(directory)
            completed = self._prediction("q1")
            completed["cutoff_results"] = {
                cutoff: {
                    "score": 0,
                    "judgment": "FAIL",
                    "generated_answer": "" if cutoff == "top_50" else "answer",
                    "judge_raw": "valid official judge output",
                }
                for cutoff in ("top_10", "top_20", "top_50", "top_200")
            }
            (evaluated / "q1.json").write_text(json.dumps(completed), encoding="utf-8")

            report = audit_evaluation(evaluated, expected_questions=1)

            self.assertFalse(report["complete"])
            self.assertTrue(report["official_harness_score_complete"])
            self.assertEqual(
                report["validation"]["empty_answers"],
                [{"question_id": "q1", "cutoff": "top_50"}],
            )

    def test_filename_and_positive_denominator_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evaluated = Path(directory)
            (evaluated / "wrong-name.json").write_text(
                json.dumps(self._prediction("q1")), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "does not match"):
                audit_evaluation(evaluated, expected_questions=1)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "must be positive"):
                audit_evaluation(Path(directory), expected_questions=0)

    def test_question_scope_reports_only_the_declared_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = root / "frozen"
            evaluated = root / "evaluated"
            frozen.mkdir()
            evaluated.mkdir()
            for question_id, score in (("development", 0.0), ("holdout", 1.0)):
                prediction = self._prediction(question_id)
                (frozen / f"{question_id}.json").write_text(
                    json.dumps(prediction), encoding="utf-8"
                )
                completed = dict(prediction)
                completed["cutoff_results"] = {
                    cutoff: {
                        "score": score,
                        "judgment": "PASS" if score else "FAIL",
                        "generated_answer": "answer",
                        "judge_raw": "verdict",
                    }
                    for cutoff in ("top_10", "top_20", "top_50", "top_200")
                }
                (evaluated / f"{question_id}.json").write_text(
                    json.dumps(completed), encoding="utf-8"
                )

            report = audit_evaluation(
                evaluated,
                frozen_directory=frozen,
                question_ids={"holdout"},
            )

            self.assertTrue(report["complete"])
            self.assertTrue(report["scoped_question_subset"])
            self.assertEqual(report["expected_questions"], 1)
            self.assertEqual(report["metrics"]["top_10"]["accuracy"], 1.0)

            with self.assertRaisesRegex(ValueError, "does not match"):
                audit_evaluation(
                    evaluated,
                    frozen_directory=frozen,
                    expected_questions=2,
                    question_ids={"holdout"},
                )

    def test_nonlegacy_cutoffs_must_be_explicitly_declared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evaluated = Path(directory)
            completed = self._prediction("q1")
            completed["cutoff_results"] = {
                cutoff: {
                    "score": 1,
                    "judgment": "PASS",
                    "generated_answer": "answer",
                    "judge_raw": "verdict",
                }
                for cutoff in ("top_20", "top_50")
            }
            (evaluated / "q1.json").write_text(json.dumps(completed), encoding="utf-8")

            legacy_default = audit_evaluation(evaluated, expected_questions=1)
            declared = audit_evaluation(
                evaluated,
                expected_questions=1,
                cutoffs=(20, "top_50"),
            )
            missing_declared = audit_evaluation(
                evaluated,
                expected_questions=1,
                cutoffs=(10, 20, 50),
            )

            self.assertFalse(legacy_default["complete"])
            self.assertEqual(
                legacy_default["cutoffs"],
                ["top_10", "top_20", "top_50", "top_200"],
            )
            self.assertTrue(declared["official_harness_score_complete"])
            self.assertFalse(missing_declared["official_harness_score_complete"])
            self.assertEqual(
                missing_declared["validation"]["missing_cutoffs"],
                [{"question_id": "q1", "cutoff": "top_10"}],
            )

    def test_frozen_evaluation_fields_are_ignored_on_both_sides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = root / "frozen"
            evaluated = root / "evaluated"
            frozen.mkdir()
            evaluated.mkdir()
            prediction = self._prediction("q1")
            frozen_payload = dict(prediction)
            frozen_payload["cutoff_results"] = {"top_10": {"old": "evaluation"}}
            (frozen / "q1.json").write_text(
                json.dumps(frozen_payload), encoding="utf-8"
            )
            completed = dict(prediction)
            completed["cutoff_results"] = {
                "top_50": {
                    "score": 1,
                    "judgment": "PASS",
                    "generated_answer": "answer",
                    "judge_raw": "verdict",
                }
            }
            (evaluated / "q1.json").write_text(json.dumps(completed), encoding="utf-8")

            report = audit_evaluation(
                evaluated,
                frozen_directory=frozen,
                expected_questions=1,
                cutoffs=(50,),
            )

            self.assertTrue(report["complete"])
            self.assertEqual(report["validation"]["frozen_payload_mismatches"], [])


if __name__ == "__main__":
    unittest.main()
