#!/usr/bin/env python3
"""Offline invariants for the r2 budget, precommit, and score-blind finalizer."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUDGET = _load("v18_r2_budget", "verify_campaign_budget.py")
FINALIZER = _load("v18_r2_finalizer", "finalize_pair.py")


class BudgetPolicyTests(unittest.TestCase):
    def test_cumulative_exposure_includes_terminal_r1(self) -> None:
        self.assertEqual(BUDGET.PRIOR_CUMULATIVE, Decimal("1.914605682"))
        self.assertEqual(BUDGET.NEW_ALLOCATION, Decimal("4.979484568"))
        self.assertEqual(BUDGET.TRACKED_CUMULATIVE, Decimal("6.894090250"))
        self.assertLess(BUDGET.TRACKED_CUMULATIVE, BUDGET.TRACKED_CEILING)

    def test_terminal_r1_hash_and_scores_are_exact(self) -> None:
        path = (
            ROOT
            / "reports/longmemeval-intelligence-dev42-v18-gpt54mini-openai-high-selfjudge-paid-pair-r1-20260717/ABORTED_AFTER_PRIMARY_AUDIT.json"
        )
        self.assertEqual(BUDGET._sha256(path), BUDGET.EXPECTED_R1_TERMINAL_SHA256)
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["primary_score"]["metrics"]["top_20"]["correct"], 40)
        self.assertEqual(document["primary_score"]["metrics"]["top_50"]["correct"], 41)
        self.assertFalse(document["execution_state"]["replication"]["execution_started"])

    def test_synthetic_content_free_admission_uses_updated_cumulative(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        r1 = (
            ROOT
            / "reports/longmemeval-intelligence-dev42-v18-gpt54mini-openai-high-selfjudge-paid-pair-r1-20260717/ABORTED_AFTER_PRIMARY_AUDIT.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            telemetry = temporary / "telemetry.json"
            fx = temporary / "fx.json"
            output = temporary / "admission.json"
            telemetry.write_text(
                json.dumps(
                    {
                        "schema_version": "narratordb.provider-key-telemetry.v2",
                        "observed_at_utc": now,
                        "provider_limit_usd": "250",
                        "provider_usage_usd": "100",
                        "provider_remaining_usd": "150",
                        "capture_tool_sha256": BUDGET._sha256(
                            HERE / "capture_provider_telemetry.py"
                        ),
                        "credential_recorded": False,
                        "key_label_recorded": False,
                        "account_identifier_recorded": False,
                        "model_content_recorded": False,
                    }
                ),
                encoding="utf-8",
            )
            fx.write_text(
                json.dumps(
                    {
                        "schema_version": "narratordb.ecb-usd-eur-observation.v1",
                        "publisher": "European Central Bank",
                        "base_currency": "EUR",
                        "quote_currency": "USD",
                        "usd_per_eur": "1.2",
                        "retrieved_at_utc": now,
                        "parser_sha256": BUDGET._sha256(
                            HERE / "verify_campaign_budget.py"
                        ),
                        "credential_recorded": False,
                        "model_content_recorded": False,
                    }
                ),
                encoding="utf-8",
            )
            argv = [
                "verify_campaign_budget.py",
                "--telemetry",
                str(telemetry),
                "--fx",
                str(fx),
                "--prior-r1-terminal",
                str(r1),
                "--output",
                str(output),
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                self.assertEqual(BUDGET.main(), 0)
            admission = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(admission["admitted"])
            self.assertEqual(
                admission["tracked_cumulative_maximum_usd"], "6.894090250"
            )


class ScoreBlindFinalizerTests(unittest.TestCase):
    def _transport(self, *, transients: int = 0, authorized: bool = True) -> dict:
        return {
            "schema_version": FINALIZER.TRANSPORT_SCHEMA,
            "authorized": authorized,
            "score_values_present": False,
            "score_driven_branching": False,
            "official_harness_score_complete": True,
            "expected_questions": 42,
            "cutoffs": ["top_20", "top_50"],
            "transport_policy": {
                "discarded_transients_maximum": 4,
                "discarded_transients_never_used": True,
                "operator_selective_retries": False,
                "full_arm_restart_on_terminal_failure": True,
            },
            "usage": {
                "events": 168 + transients,
                "successful_forwarded_openai_gpt54mini_stop_calls": 168,
                "discarded_transients": transients,
                "known_cost_discarded_transients": 0,
                "unknown_cost_discarded_transients": transients,
                "terminal_rejections": 0,
                "completed_logical_calls": 168,
                "maximum_physical_attempts_observed": 1 if transients == 0 else 2,
                "retry_payload_identity_verified": True,
                "known_success_cost_usd": "0.70",
                "discarded_transient_booked_cost_usd": str(
                    Decimal("0.05") * transients
                ),
                "conservative_ledger_cost_usd": str(
                    Decimal("0.70") + Decimal("0.05") * transients
                ),
            },
            "bindings": {"usage_log_sha256": "0" * 64},
            "proxy_stop": {
                "hidden_sdk_retry_rejections": 0,
                "transport_failed": False,
                "pending_logical_calls": 0,
                "active_logical_calls": 0,
            },
            "failures": [],
        }

    def test_zero_through_four_discarded_transients_are_precommitted(self) -> None:
        for count in range(5):
            with self.subTest(count=count):
                result = FINALIZER._verify_transport(
                    self._transport(transients=count), label="synthetic"
                )
                self.assertEqual(result["transients"], count)

    def test_score_exposed_precommit_discloses_r1_without_r2_score(self) -> None:
        path = (
            ROOT
            / "benchmark_records/precommits/longmemeval_dev42_v18_gpt54mini_openai_high_selfjudge_paid_pair_r2_precommit_20260717.json"
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        FINALIZER._verify_precommit(document)
        self.assertTrue(document["score_observed_before_r2_precommit"])
        self.assertTrue(document["score_fields_present"])
        self.assertFalse(document["r2_score_fields_present"])
        self.assertEqual(document["r2_provider_or_model_calls_before_precommit"], 0)

    def test_result_parser_reproduces_disclosed_r1_scores(self) -> None:
        r1 = (
            ROOT
            / "reports/longmemeval-intelligence-dev42-v18-gpt54mini-openai-high-selfjudge-paid-pair-r1-20260717"
        )
        result_path = next(
            (r1 / "primary/evaluation/official-harness").glob(
                "longmemeval_results_*.json"
            )
        )
        result = FINALIZER._load(result_path)
        raw = FINALIZER._load(r1 / "primary/evaluation-audit.json")
        question_ids = set(
            json.loads(
                (
                    ROOT
                    / "reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/dev42_question_ids.json"
                ).read_text(encoding="utf-8")
            )
        )
        scores, verdicts = FINALIZER._verify_result(
            result, raw, question_ids=question_ids, label="historical-r1"
        )
        self.assertEqual(scores["top_20"]["correct"], 40)
        self.assertEqual(scores["top_50"]["correct"], 41)
        self.assertEqual(len(verdicts["top_50"]), 42)

    def test_fifth_transient_or_unauthorized_arm_fails(self) -> None:
        with self.assertRaises(ValueError):
            FINALIZER._verify_transport(
                self._transport(transients=5), label="synthetic"
            )
        with self.assertRaises(ValueError):
            FINALIZER._verify_transport(
                self._transport(authorized=False), label="synthetic"
            )

    def test_threshold_is_reporting_only_after_both_arms(self) -> None:
        self.assertEqual(FINALIZER.TARGET_MINIMUM_CORRECT, 40)
        self.assertEqual(FINALIZER._percentage(40, 42), "95.23809523809523809523809524")
        source = (HERE / "finalize_pair.py").read_text(encoding="utf-8")
        tail = source[source.index("target_passed ="):]
        self.assertIn('"status": "complete-target-passed" if target_passed', tail)
        self.assertIn("return 0", tail)
        self.assertNotIn("return 1 if target_passed", tail)

    def test_orchestrator_has_no_score_branch_between_arms(self) -> None:
        source = (HERE / "orchestrate_paid_pair.sh").read_text(encoding="utf-8")
        primary = source.index('launch_with_openrouter_key.sh" primary')
        replication = source.index('launch_with_openrouter_key.sh" replication')
        between = source[primary:replication]
        self.assertIn("transport_arm_audit.py", between)
        self.assertIn('launch_with_openrouter_key.sh" telemetry-between', between)
        self.assertNotIn("accuracy", between.lower())
        self.assertNotIn("target_passed", between)


if __name__ == "__main__":
    unittest.main(verbosity=2)
