from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path

from narratordb.benchmarks.arm_gate import (
    ArmGateError,
    arm_gate_failures,
    audit_and_gate_arm,
    main,
    score_blind_gate_report,
)


class ArmGateTests(unittest.TestCase):
    @staticmethod
    def _clean_report() -> dict:
        return {
            "complete": True,
            "official_harness_score_complete": True,
            "expected_questions": 2,
            "evaluated_questions": 2,
            "frozen_questions": 2,
            "cutoffs": ["top_20", "top_50"],
            "metrics": {
                "top_20": {"correct": 1, "total": 2, "accuracy": 0.5},
                "top_50": {"correct": 1, "total": 2, "accuracy": 0.5},
            },
            "validation": {
                "empty_answers": [],
                "empty_judges": [],
                "frozen_payload_mismatches": [],
                "missing_evaluated_ids": [],
                "missing_frozen_ids": [],
                "extra_evaluated_ids": [],
                "missing_cutoffs": [],
                "invalid_scores": [],
                "inconsistent_verdicts": [],
            },
            "usage": {
                "publication_ready": True,
                "unknown_cost_attempts": 0,
                "invalid_completion_identities": 0,
                "malformed_http_200_responses": 0,
                "cost_usd": 0.25,
                "provider_counts": {"DeepInfra": 4},
                "request_model_counts": {"answerer": 2, "judge": 2},
            },
        }

    @staticmethod
    def _prediction(question_id: str) -> dict:
        return {
            "question_id": question_id,
            "question_type": "single-session-user",
            "question": "What happened?",
            "ground_truth_answer": "It passed.",
            "retrieval": {"search_results": [{"memory": "It passed."}]},
        }

    def test_clean_report_passes(self) -> None:
        failures = arm_gate_failures(
            self._clean_report(),
            expected_questions=2,
            cutoffs=(20, 50),
            allowed_request_models={"answerer", "judge"},
            allowed_providers={"DeepInfra"},
            max_cost_usd=Decimal("1.25"),
        )
        self.assertEqual(failures, ())

    def test_completeness_scope_and_zero_counter_failures(self) -> None:
        cases = {
            "complete": (lambda report: report.__setitem__("complete", False), "complete=false"),
            "official": (
                lambda report: report.__setitem__("official_harness_score_complete", False),
                "official_harness_score_complete=false",
            ),
            "denominator": (
                lambda report: report["metrics"]["top_50"].__setitem__("total", 1),
                "metrics.top_50.total!=expected_questions",
            ),
            "cutoffs": (
                lambda report: report.__setitem__("cutoffs", ["top_50", "top_20"]),
                "cutoffs_mismatch",
            ),
            "empty_answer": (
                lambda report: report["validation"]["empty_answers"].append({}),
                "validation.empty_answers=1",
            ),
            "empty_judge": (
                lambda report: report["validation"]["empty_judges"].append({}),
                "validation.empty_judges=1",
            ),
            "identity": (
                lambda report: report["usage"].__setitem__(
                    "invalid_completion_identities", 1
                ),
                "usage.invalid_completion_identities!=0",
            ),
            "unknown_cost": (
                lambda report: report["usage"].__setitem__("unknown_cost_attempts", 1),
                "usage.unknown_cost_attempts!=0",
            ),
        }
        for name, (mutate, expected) in cases.items():
            with self.subTest(name=name):
                report = copy.deepcopy(self._clean_report())
                mutate(report)
                self.assertIn(
                    expected,
                    arm_gate_failures(
                        report, expected_questions=2, cutoffs=(20, 50)
                    ),
                )

    def test_recovered_malformed_responses_are_telemetry_not_gate_failures(self) -> None:
        report = self._clean_report()
        report["usage"]["malformed_http_200_responses"] = 15
        self.assertEqual(
            arm_gate_failures(report, expected_questions=2, cutoffs=(20, 50)),
            (),
        )

    def test_persisted_gate_report_is_score_blind(self) -> None:
        report = self._clean_report()
        report["validation"]["invalid_scores"] = [
            {"question_id": "q1", "cutoff": "top_50", "score": "secret-score"}
        ]
        report["validation"]["inconsistent_verdicts"] = [
            {
                "question_id": "q2",
                "cutoff": "top_20",
                "score": "secret-score",
                "judgment": "secret-verdict",
            }
        ]
        report["benchmark_string"] = "What happened? It passed. answer verdict"
        sanitized = score_blind_gate_report(
            report,
            failures=("validation.invalid_scores=1",),
        )
        rendered = json.dumps(sanitized, sort_keys=True)
        forbidden_keys = {
            "correct",
            "accuracy",
            "score",
            "judgment",
            "generated_answer",
            "judge_raw",
            "question",
            "ground_truth_answer",
            "retrieval",
            "benchmark_string",
        }

        def keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value) | {key for item in value.values() for key in keys(item)}
            if isinstance(value, list):
                return {key for item in value for key in keys(item)}
            return set()

        self.assertTrue(forbidden_keys.isdisjoint(keys(sanitized)))
        for content in (
            "secret-score",
            "secret-verdict",
            "What happened?",
            "It passed.",
            '"answer"',
            '"verdict"',
        ):
            self.assertNotIn(content, rendered)
        self.assertEqual(sanitized["denominators"], {"top_20": 2, "top_50": 2})
        self.assertEqual(
            sanitized["validation"]["invalid_scores"],
            [{"question_id": "q1", "cutoff": "top_50"}],
        )

    def test_usage_allowlists_and_cost_cap_fail_closed(self) -> None:
        report = self._clean_report()
        report["usage"]["provider_counts"] = {"unexpected": 1}
        report["usage"]["request_model_counts"] = {"unexpected": 1}
        report["usage"]["cost_usd"] = 1.26
        failures = arm_gate_failures(
            report,
            expected_questions=2,
            cutoffs=(20, 50),
            allowed_request_models={"answerer", "judge"},
            allowed_providers={"DeepInfra"},
            max_cost_usd=Decimal("1.25"),
        )
        self.assertIn("usage.providers_outside_allowlist", failures)
        self.assertIn("usage.request_models_outside_allowlist", failures)
        self.assertIn("usage.cost_usd_above_cap", failures)

        report["usage"]["cost_usd"] = float("nan")
        self.assertIn(
            "usage.cost_usd_invalid",
            arm_gate_failures(report, expected_questions=2, cutoffs=(20, 50)),
        )
        sanitized = score_blind_gate_report(
            report,
            failures=("usage.cost_usd_invalid",),
        )
        self.assertIsNone(sanitized["usage"]["cost_usd"])
        self.assertNotIn("NaN", json.dumps(sanitized, allow_nan=False))

    def _write_arm(self, root: Path, *, empty_answer: bool) -> dict[str, Path]:
        frozen = root / "frozen"
        evaluated = root / "evaluated"
        frozen.mkdir()
        evaluated.mkdir()
        prediction = self._prediction("q1")
        (frozen / "q1.json").write_text(json.dumps(prediction), encoding="utf-8")
        completed = dict(prediction)
        completed["cutoff_results"] = {
            cutoff: {
                "score": 0,
                "judgment": "FAIL",
                "generated_answer": "" if empty_answer and cutoff == "top_50" else "answer",
                "judge_raw": "verdict",
            }
            for cutoff in ("top_20", "top_50")
        }
        (evaluated / "q1.json").write_text(json.dumps(completed), encoding="utf-8")
        usage = root / "usage.jsonl"
        usage.write_text(
            json.dumps(
                {
                    "event": "completion",
                    "provider": "DeepInfra",
                    "request_model": "answerer",
                    "response_model": "answerer",
                    "status": 200,
                    "finish_reason": "stop",
                    "response_complete": True,
                    "unknown_cost": False,
                    "cost_usd": 0.01,
                    "timestamp": "2026-07-16T00:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        evaluator_log = root / "evaluate.log"
        evaluator_log.write_text("evaluation complete\n", encoding="utf-8")
        question_ids = root / "question-ids.json"
        question_ids.write_text('["q1"]\n', encoding="utf-8")
        return {
            "frozen": frozen,
            "evaluated": evaluated,
            "usage": usage,
            "evaluator_log": evaluator_log,
            "question_ids": question_ids,
        }

    def test_audit_and_gate_arm_stops_on_empty_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_arm(Path(directory), empty_answer=True)
            with self.assertRaises(ArmGateError) as raised:
                audit_and_gate_arm(
                    paths["evaluated"],
                    frozen_directory=paths["frozen"],
                    usage_log=paths["usage"],
                    evaluator_log=paths["evaluator_log"],
                    expected_questions=1,
                    cutoffs=(20, 50),
                    question_ids={"q1"},
                    allowed_request_models={"answerer"},
                    allowed_providers={"DeepInfra"},
                    max_cost_usd=Decimal("1.25"),
                )
            self.assertIn("complete=false", raised.exception.failures)
            self.assertIn("validation.empty_answers=1", raised.exception.failures)
            self.assertNotIn("metrics", raised.exception.report)

    def test_audit_and_gate_arm_allows_a_complete_arm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_arm(Path(directory), empty_answer=False)
            report = audit_and_gate_arm(
                paths["evaluated"],
                frozen_directory=paths["frozen"],
                usage_log=paths["usage"],
                evaluator_log=paths["evaluator_log"],
                expected_questions=1,
                cutoffs=(20, 50),
                question_ids={"q1"},
                allowed_request_models={"answerer"},
                allowed_providers={"DeepInfra"},
                max_cost_usd=Decimal("1.25"),
            )
            self.assertTrue(report["complete"])

    def test_cli_preserves_failed_audit_read_only_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._write_arm(root, empty_answer=True)
            output = root / "arm-audit.json"
            argv = [
                "--evaluated-directory",
                str(paths["evaluated"]),
                "--frozen-directory",
                str(paths["frozen"]),
                "--usage-log",
                str(paths["usage"]),
                "--evaluator-log",
                str(paths["evaluator_log"]),
                "--expected-questions",
                "1",
                "--cutoffs",
                "20,50",
                "--question-id-file",
                str(paths["question_ids"]),
                "--output",
                str(output),
                "--allowed-request-model",
                "answerer",
                "--allowed-provider",
                "DeepInfra",
                "--max-cost-usd",
                "1.25",
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(argv)
            self.assertEqual(status, 1)
            self.assertTrue(output.is_file())
            self.assertEqual(output.stat().st_mode & 0o222, 0)
            document = json.loads(output.read_text())
            self.assertEqual(json.loads(stdout.getvalue()), document)
            self.assertEqual(
                document["schema_version"], "narratordb.arm-evaluation-gate.v1"
            )
            self.assertFalse(document["authorized"])
            self.assertFalse(document["complete"])
            self.assertNotIn("metrics", document)
            self.assertNotIn("by_question_type", document)
            self.assertIn("validation.empty_answers=1", stderr.getvalue())

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(argv), 2)


if __name__ == "__main__":
    unittest.main()
