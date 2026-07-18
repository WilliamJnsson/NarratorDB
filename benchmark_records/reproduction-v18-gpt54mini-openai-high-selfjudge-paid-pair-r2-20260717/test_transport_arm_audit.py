#!/usr/bin/env python3
"""Offline positive and adversarial tests for the sealed r2 arm audit."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "v18_r2_transport_arm_audit", HERE / "transport_arm_audit.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load sealed transport arm audit")
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def payload_sha(index: int) -> str:
    return hashlib.sha256(f"payload-{index}".encode()).hexdigest()


def logical_id(payload: str, ordinal: int = 1) -> str:
    return hashlib.sha256(f"{payload}:{ordinal}".encode("ascii")).hexdigest()


def success_event(
    payload: str,
    *,
    ordinal: int = 1,
    attempt: int = 1,
    cost: str = "0.001",
) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "completion",
        "status": 200,
        "request_model": AUDIT.EXPECTED_MODEL,
        "response_model": AUDIT.EXPECTED_MODEL,
        "provider": AUDIT.EXPECTED_PROVIDER,
        "finish_reason": "stop",
        "response_complete": True,
        "prompt_tokens": 10,
        "cached_tokens": 0,
        "completion_tokens": 5,
        "reasoning_tokens": 2,
        "cost_usd": Decimal(cost),
        "unknown_cost": False,
        "request_payload_sha256": payload,
        "logical_call_id": logical_id(payload, ordinal),
        "attempt_number": attempt,
        "response_forwarded": True,
        "discarded_reason": None,
        "retryable": False,
    }


def discard_event(
    payload: str,
    *,
    ordinal: int = 1,
    attempt: int = 1,
    reason: str = "empty_completion",
    status: int = 200,
    known_cost: bool = False,
) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "discarded_transient",
        "status": status,
        "request_model": AUDIT.EXPECTED_MODEL,
        "response_model": AUDIT.EXPECTED_MODEL,
        "provider": AUDIT.EXPECTED_PROVIDER,
        "finish_reason": "unknown",
        "response_complete": False,
        "prompt_tokens": 10 if known_cost else 0,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": Decimal("0.07" if known_cost else "0.05"),
        "unknown_cost": not known_cost,
        "request_payload_sha256": payload,
        "logical_call_id": logical_id(payload, ordinal),
        "attempt_number": attempt,
        "response_forwarded": False,
        "discarded_reason": reason,
        "retryable": True,
    }


def clean_events(count: int = 168, *, start: int = 0) -> list[dict]:
    return [success_event(payload_sha(index)) for index in range(start, start + count)]


class TransportUsagePolicyTests(unittest.TestCase):
    def test_exact_168_forwarded_stops_pass(self) -> None:
        summary, failures = AUDIT.classify_usage_events(clean_events())
        self.assertEqual(failures, ())
        self.assertEqual(
            summary["successful_forwarded_openai_gpt54mini_stop_calls"], 168
        )
        self.assertEqual(summary["discarded_transients"], 0)
        self.assertEqual(summary["completed_logical_calls"], 168)

    def test_four_discards_then_fifth_success_passes(self) -> None:
        payload = payload_sha(0)
        chain = [
            discard_event(payload, attempt=attempt) for attempt in range(1, 5)
        ]
        chain.append(success_event(payload, attempt=5))
        summary, failures = AUDIT.classify_usage_events(chain + clean_events(167, start=1))
        self.assertEqual(failures, ())
        self.assertEqual(summary["discarded_transients"], 4)
        self.assertEqual(summary["maximum_physical_attempts_observed"], 5)
        self.assertTrue(summary["retry_payload_identity_verified"])

    def test_fifth_discard_and_missing_success_fail_closed(self) -> None:
        payload = payload_sha(0)
        events = [
            discard_event(payload, attempt=attempt) for attempt in range(1, 6)
        ]
        _, failures = AUDIT.classify_usage_events(events + clean_events(167, start=1))
        self.assertIn("discarded_transients>4", failures)
        self.assertIn("successful_forwarded_stop_calls!=168", failures)
        self.assertTrue(any("success_count!=1" in failure for failure in failures))

    def test_same_payload_after_completion_is_new_ordinal(self) -> None:
        payload = payload_sha(0)
        events = [
            success_event(payload, ordinal=1),
            success_event(payload, ordinal=2),
            *clean_events(166, start=1),
        ]
        _, failures = AUDIT.classify_usage_events(events)
        self.assertEqual(failures, ())

    def test_same_payload_chains_cannot_interleave(self) -> None:
        payload = payload_sha(0)
        events = [
            discard_event(payload, ordinal=1, attempt=1),
            success_event(payload, ordinal=2, attempt=1),
            success_event(payload, ordinal=1, attempt=2),
            *clean_events(166, start=1),
        ]
        _, failures = AUDIT.classify_usage_events(events)
        self.assertTrue(
            any("same_payload_logical_chain_interleaved" in failure for failure in failures)
        )

    def test_retry_payload_and_attempts_must_be_identical_and_contiguous(self) -> None:
        payload = payload_sha(0)
        events = [
            discard_event(payload, attempt=1),
            success_event(payload, attempt=3),
            *clean_events(167, start=1),
        ]
        _, failures = AUDIT.classify_usage_events(events)
        self.assertTrue(any("attempts_not_contiguous" in failure for failure in failures))

        events[1]["request_payload_sha256"] = payload_sha(999)
        _, failures = AUDIT.classify_usage_events(events)
        self.assertTrue(any("payload_hash_changed" in failure for failure in failures))

    def test_known_and_unknown_discard_cost_rules(self) -> None:
        payload = payload_sha(0)
        events = [
            discard_event(payload, attempt=1, known_cost=True),
            success_event(payload, attempt=2),
            *clean_events(167, start=1),
        ]
        summary, failures = AUDIT.classify_usage_events(events)
        self.assertEqual(failures, ())
        self.assertEqual(summary["known_cost_discarded_transients"], 1)

        events[0]["unknown_cost"] = True
        _, failures = AUDIT.classify_usage_events(events)
        self.assertIn(
            "event[1].unknown_discard_not_charged_at_reservation", failures
        )

    def test_terminal_or_nonallowlisted_transient_fails(self) -> None:
        payload = payload_sha(0)
        bad = discard_event(
            payload, reason="upstream_http_error", status=401, attempt=1
        )
        _, failures = AUDIT.classify_usage_events(
            [bad, success_event(payload, attempt=2), *clean_events(167, start=1)]
        )
        self.assertIn("event[1].http_error_status_not_retryable", failures)

        bad["event"] = "terminal_rejection"
        bad["retryable"] = False
        _, failures = AUDIT.classify_usage_events(
            [bad, success_event(payload, attempt=2), *clean_events(167, start=1)]
        )
        self.assertIn("terminal_rejections!=0", failures)

    def test_success_identity_forwarding_and_cost_are_exact(self) -> None:
        mutations = {
            "status": 201,
            "response_model": "openai/gpt-5.4",
            "provider": "Azure",
            "finish_reason": "length",
            "response_forwarded": False,
            "unknown_cost": True,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": Decimal("0"),
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                events = clean_events()
                events[0][field] = value
                _, failures = AUDIT.classify_usage_events(events)
                self.assertTrue(failures)

    def test_closed_schema_and_arm_cost_fuse(self) -> None:
        events = clean_events()
        events[0]["content"] = "forbidden"
        _, failures = AUDIT.classify_usage_events(events)
        self.assertIn("event[1].closed_schema_mismatch", failures)

        _, failures = AUDIT.classify_usage_events(
            [
                success_event(payload_sha(index), cost="0.02")
                for index in range(168)
            ]
        )
        self.assertIn("ledger_cost_above_arm_fuse", failures)


class ProxyLogPolicyTests(unittest.TestCase):
    @staticmethod
    def evidence() -> tuple[dict, dict, dict]:
        startup = {
            "ok": True,
            "provider_only": AUDIT.EXPECTED_PROVIDER,
            "provider_allow": [],
            "model_routes": {
                AUDIT.EXPECTED_MODEL: [AUDIT.EXPECTED_PROVIDER]
            },
            "model_output_token_parameters": {},
            "model_omit_temperature": [AUDIT.EXPECTED_MODEL],
            "model_reasoning_efforts": {AUDIT.EXPECTED_MODEL: "high"},
            "reasoning_effort": None,
            "public_benchmark": True,
            "usage_log": str(HERE / "synthetic-usage.jsonl"),
            "max_cost_usd": Decimal("2.45"),
            "request_reservation_usd": Decimal("0.05"),
            "budget_safety_reserve_usd": Decimal("0.01"),
            "upstream_timeout_seconds": Decimal("105.0"),
            "direct_upstream_networking": True,
            "inbound_retry_count_policy": "absent-or-zero-only",
            "local_caller_auth_required": True,
            "max_request_bytes": 20 * 1024 * 1024,
            "max_response_bytes": 4 * 1024 * 1024,
        }
        usage = {
            "calls": 168,
            "errors": 0,
            "malformed_responses": 0,
            "discarded_transients": 0,
            "max_discarded_transients": 4,
            "max_logical_attempts": 5,
            "transport_failed": False,
            "pending_logical_calls": 0,
            "active_logical_calls": 0,
            "hidden_sdk_retry_rejections": 0,
            "unknown_cost_attempts": 0,
            "cost_usd": Decimal("0.168"),
            "reserved_cost_usd": Decimal("0"),
            "max_cost_usd": Decimal("2.45"),
            "request_reservation_usd": Decimal("0.05"),
            "safety_reserve_usd": Decimal("0.01"),
            "scope": "process",
            "enforcement": "soft_fuse",
            "prompt_tokens": 1680,
            "cached_tokens": 0,
            "completion_tokens": 840,
            "reasoning_tokens": 336,
        }
        stopped = {"stopped": True, "usage": usage}
        summary = {
            "discarded_transients": 0,
            "unknown_cost_discarded_transients": 0,
            "conservative_ledger_cost_usd": "0.168",
        }
        return startup, stopped, summary

    def test_exact_proxy_startup_and_stop_evidence_passes(self) -> None:
        startup, stopped, summary = self.evidence()
        _, failures = AUDIT._proxy_log_failures(
            startup,
            stopped,
            usage_summary=summary,
            usage_log=HERE / "synthetic-usage.jsonl",
        )
        self.assertEqual(failures, ())

    def test_proxy_evidence_route_retry_fuse_and_cost_fail_closed(self) -> None:
        mutations = (
            ("startup", "provider_only", "not-openai"),
            ("startup", "inbound_retry_count_policy", "any"),
            ("usage", "transport_failed", True),
            ("usage", "hidden_sdk_retry_rejections", 1),
            ("usage", "cost_usd", Decimal("0.169")),
        )
        for target, field, value in mutations:
            with self.subTest(target=target, field=field):
                startup, stopped, summary = self.evidence()
                record = startup if target == "startup" else stopped["usage"]
                record[field] = value
                _, failures = AUDIT._proxy_log_failures(
                    startup,
                    stopped,
                    usage_summary=summary,
                    usage_log=HERE / "synthetic-usage.jsonl",
                )
                self.assertTrue(failures)


class RawAuditCompatibilityTests(unittest.TestCase):
    @staticmethod
    def report(*, discarded: int = 0, unknown: int = 0) -> dict:
        return {
            "complete": unknown == 0,
            "official_harness_score_complete": True,
            "expected_questions": 42,
            "evaluated_questions": 42,
            "frozen_questions": 42,
            "scoped_question_subset": True,
            "cutoffs": ["top_20", "top_50"],
            "metrics": {
                "top_20": {"correct": 0, "total": 42, "accuracy": 0},
                "top_50": {"correct": 0, "total": 42, "accuracy": 0},
            },
            "validation": {
                field: [] for field in AUDIT._ZERO_VALIDATION_FIELDS
            },
            "usage": {
                "events": 168 + discarded,
                "completion_calls": 168,
                "upstream_errors": 0,
                "malformed_http_200_responses": 0,
                "invalid_completion_identities": 0,
                "unknown_cost_attempts": unknown,
                "publication_ready": unknown == 0,
                "completion_provider_counts": {"OpenAI": 168},
                "request_model_counts": {
                    AUDIT.EXPECTED_MODEL: 168 + discarded
                },
                "finish_reason_counts": {"stop": 168},
                "cost_usd": 0.168 + 0.05 * discarded,
            },
            "harness_log": {
                "failed_attempt_counts": (
                    {"1": discarded} if discarded else {}
                ),
                "timed_out_attempt_counts": {},
                "returned_none_responses": 0,
                "attempt_five_failures": 0,
            },
        }

    def test_frozen_raw_auditor_shape_is_accepted_without_trusting_complete(self) -> None:
        summary = {
            "events": 169,
            "discarded_transients": 1,
            "discarded_attempt_counts": {"1": 1},
            "unknown_cost_discarded_transients": 1,
            "conservative_ledger_cost_usd": "0.218",
        }
        report = self.report(discarded=1, unknown=1)
        self.assertFalse(report["complete"])
        self.assertEqual(
            AUDIT._audit_failures(report, usage_summary=summary), ()
        )

    def test_retry_warning_count_must_equal_discarded_count(self) -> None:
        summary = {
            "events": 169,
            "discarded_transients": 1,
            "discarded_attempt_counts": {"1": 1},
            "unknown_cost_discarded_transients": 1,
            "conservative_ledger_cost_usd": "0.218",
        }
        report = self.report(discarded=1, unknown=1)
        report["harness_log"]["failed_attempt_counts"] = {}
        self.assertIn(
            "harness_retry_count!=discarded_transients",
            AUDIT._audit_failures(report, usage_summary=summary),
        )

    def test_score_blind_gate_schema_has_no_metric_values(self) -> None:
        source = (HERE / "transport_arm_audit.py").read_text(encoding="utf-8")
        self.assertIn('"score_values_present": False', source)
        self.assertNotIn('"metrics": report.get', source)
        json.dumps({"score_values_present": False}, allow_nan=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
