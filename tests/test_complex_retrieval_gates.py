from __future__ import annotations

import unittest

from tests.stress.complex_retrieval import evaluate_gates


class ComplexRetrievalGateTests(unittest.TestCase):
    @staticmethod
    def _summary() -> dict:
        return {
            "custom_complex": {
                "pass_rate": 1.0,
                "time_filter_check": {"passed": True},
            },
            "locomo_retrieval": {
                "retrieval_recall": 0.85,
                "answers_in_context": 1309,
                "query_latency_ms": {"p95": 50.0},
            },
            "scale": {
                "stored_messages": 5006,
                "query_latency_ms": {"p95": 25.0},
                "cases": [
                    {"name": "first", "hit_rate": 1.0},
                    {"name": "second", "hit_rate": 1.0},
                ],
            },
        }

    def test_passing_configured_gates(self) -> None:
        failures = evaluate_gates(
            self._summary(),
            min_custom_pass_rate=1.0,
            require_time_filter=True,
            min_locomo_recall=0.844,
            min_locomo_answers=1301,
            max_locomo_p95_ms=100.0,
            min_scale_stored_messages=5000,
            min_scale_hit_rate=1.0,
            max_scale_p95_ms=100.0,
        )

        self.assertEqual(failures, [])

    def test_each_gate_failure_is_reported(self) -> None:
        summary = self._summary()
        summary["custom_complex"]["pass_rate"] = 0.5
        summary["custom_complex"]["time_filter_check"]["passed"] = False
        summary["locomo_retrieval"]["retrieval_recall"] = 0.5
        summary["locomo_retrieval"]["answers_in_context"] = 10
        summary["locomo_retrieval"]["query_latency_ms"]["p95"] = 500.0
        summary["scale"]["stored_messages"] = 10
        summary["scale"]["cases"][0]["hit_rate"] = 0.5
        summary["scale"]["query_latency_ms"]["p95"] = 500.0

        failures = evaluate_gates(
            summary,
            min_custom_pass_rate=1.0,
            require_time_filter=True,
            min_locomo_recall=0.8,
            min_locomo_answers=100,
            max_locomo_p95_ms=100.0,
            min_scale_stored_messages=100,
            min_scale_hit_rate=1.0,
            max_scale_p95_ms=100.0,
        )

        self.assertEqual(len(failures), 8)

    def test_skipped_locomo_fails_when_a_locomo_gate_is_configured(self) -> None:
        summary = self._summary()
        summary["locomo_retrieval"] = None

        failures = evaluate_gates(summary, min_locomo_answers=1301)

        self.assertEqual(
            failures,
            ["LoCoMo suite was skipped while LoCoMo gates were configured"],
        )


if __name__ == "__main__":
    unittest.main()
