#!/usr/bin/env python3
"""Offline positive and adversarial tests for the prospective R3 auditors."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


HERE = Path(__file__).resolve().parent


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUDIT = _load_module("v18_r3_transport_arm_audit", "transport_arm_audit.py")
FINALIZE = _load_module("v18_r3_finalize_pair", "finalize_pair.py")


def payload_sha(index: int) -> str:
    return hashlib.sha256(f"payload-{index}".encode()).hexdigest()


def logical_id(payload: str, ordinal: int = 1) -> str:
    return hashlib.sha256(f"{payload}:{ordinal}".encode("ascii")).hexdigest()


def request_ids(payload: str, ordinal: int, attempt: int) -> tuple[str, str]:
    seed = hashlib.sha256(f"{payload}:{ordinal}:{attempt}".encode()).hexdigest()
    return f"narratordb-r3-{seed[:32]}", f"req-{seed}"


def official_cost(prompt: int, cached: int, completion: int) -> Decimal:
    return AUDIT._money(
        AUDIT.exact_official_cost_usd(
            prompt_tokens=prompt,
            cached_tokens=cached,
            completion_tokens=completion,
        )
    )


def success_event(
    payload: str,
    *,
    ordinal: int = 1,
    attempt: int = 1,
    prompt: int = 100,
    cached: int = 20,
    completion: int = 50,
    reasoning: int = 30,
) -> dict:
    client_id, upstream_id = request_ids(payload, ordinal, attempt)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "completion",
        "status": 200,
        "endpoint_identity": AUDIT.EXPECTED_ENDPOINT,
        "provider": AUDIT.EXPECTED_PROVIDER,
        "request_model": AUDIT.EXPECTED_MODEL,
        "response_model": AUDIT.EXPECTED_MODEL,
        "service_tier": AUDIT.EXPECTED_SERVICE_TIER,
        "observed_finish_class": "stop",
        "visible_content_state": "nonempty",
        "response_complete": True,
        "response_forwarded": True,
        "discarded_reason": None,
        "retryable": False,
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "cost_usd": official_cost(prompt, cached, completion),
        "unknown_cost": False,
        "request_payload_sha256": payload,
        "logical_call_id": logical_id(payload, ordinal),
        "attempt_number": attempt,
        "client_request_id": client_id,
        "upstream_request_id": upstream_id,
    }


def response_discard_event(
    payload: str,
    *,
    ordinal: int = 1,
    attempt: int = 1,
    reason: str = "empty_completion",
) -> dict:
    event = success_event(payload, ordinal=ordinal, attempt=attempt)
    event.update(
        {
            "event": "discarded_transient",
            "observed_finish_class": {
                "empty_completion": "stop",
                "contentless_provider_error": "error",
                "contentless_reasoning_exhausted": "length",
            }[reason],
            "visible_content_state": "null",
            "response_complete": False,
            "response_forwarded": False,
            "discarded_reason": reason,
            "retryable": True,
        }
    )
    if reason == "contentless_reasoning_exhausted":
        event["reasoning_tokens"] = event["completion_tokens"]
    return event


def transport_discard_event(
    payload: str,
    *,
    ordinal: int = 1,
    attempt: int = 1,
    reason: str = "upstream_timeout_or_network",
    status: int = 504,
) -> dict:
    client_id, _ = request_ids(payload, ordinal, attempt)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "discarded_transient",
        "status": status,
        "endpoint_identity": AUDIT.EXPECTED_ENDPOINT,
        "provider": AUDIT.EXPECTED_PROVIDER,
        "request_model": AUDIT.EXPECTED_MODEL,
        "response_model": "unknown",
        "service_tier": "unknown",
        "observed_finish_class": "unknown",
        "visible_content_state": "unavailable",
        "response_complete": False,
        "response_forwarded": False,
        "discarded_reason": reason,
        "retryable": True,
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": AUDIT._money(AUDIT.REQUEST_RESERVATION_USD),
        "unknown_cost": True,
        "request_payload_sha256": payload,
        "logical_call_id": logical_id(payload, ordinal),
        "attempt_number": attempt,
        "client_request_id": client_id,
        "upstream_request_id": "unknown",
    }


def clean_events(count: int = 168, *, start: int = 0) -> list[dict]:
    return [success_event(payload_sha(index)) for index in range(start, start + count)]


class TransportUsagePolicyTests(unittest.TestCase):
    def test_exact_168_official_stops_pass(self) -> None:
        summary, failures = AUDIT.classify_usage_events(clean_events())
        self.assertEqual(failures, ())
        self.assertEqual(
            summary["successful_forwarded_official_openai_stop_calls"], 168
        )
        self.assertEqual(summary["discarded_transients"], 0)
        self.assertEqual(summary["completed_logical_calls"], 168)
        self.assertTrue(summary["exact_cost_formula_reconciled"])
        self.assertTrue(summary["unique_safe_request_ids_verified"])

    def test_four_known_response_discards_then_success_passes(self) -> None:
        payload = payload_sha(0)
        chain = [
            response_discard_event(payload, attempt=attempt)
            for attempt in range(1, 5)
        ]
        chain.append(success_event(payload, attempt=5))
        summary, failures = AUDIT.classify_usage_events(chain + clean_events(167, start=1))
        self.assertEqual(failures, ())
        self.assertEqual(summary["discarded_transients"], 4)
        self.assertEqual(summary["known_cost_discarded_transients"], 4)
        self.assertEqual(summary["maximum_physical_attempts_observed"], 5)

    def test_unknown_timeout_reservation_then_recovery_passes(self) -> None:
        payload = payload_sha(0)
        events = [
            transport_discard_event(payload, attempt=1),
            success_event(payload, attempt=2),
            *clean_events(167, start=1),
        ]
        summary, failures = AUDIT.classify_usage_events(events)
        self.assertEqual(failures, ())
        self.assertEqual(summary["unknown_cost_attempts"], 1)
        self.assertEqual(summary["unknown_cost_discarded_transients"], 1)
        self.assertEqual(
            Decimal(summary["discarded_transient_booked_cost_usd"]),
            AUDIT.REQUEST_RESERVATION_USD,
        )

    def test_fifth_discard_and_missing_success_fail_closed(self) -> None:
        payload = payload_sha(0)
        events = [
            response_discard_event(payload, attempt=attempt)
            for attempt in range(1, 6)
        ]
        _, failures = AUDIT.classify_usage_events(events + clean_events(167, start=1))
        self.assertIn("discarded_transients>4", failures)
        self.assertIn(
            "successful_forwarded_official_openai_stop_calls!=168", failures
        )
        self.assertTrue(any("success_count!=1" in failure for failure in failures))

    def test_same_payload_new_ordinal_and_no_interleaving(self) -> None:
        payload = payload_sha(0)
        passing = [
            success_event(payload, ordinal=1),
            success_event(payload, ordinal=2),
            *clean_events(166, start=1),
        ]
        self.assertEqual(AUDIT.classify_usage_events(passing)[1], ())
        interleaved = [
            response_discard_event(payload, ordinal=1, attempt=1),
            success_event(payload, ordinal=2, attempt=1),
            success_event(payload, ordinal=1, attempt=2),
            *clean_events(166, start=1),
        ]
        failures = AUDIT.classify_usage_events(interleaved)[1]
        self.assertTrue(
            any("same_payload_logical_chain_interleaved" in item for item in failures)
        )

    def test_retry_attempts_payload_and_ids_are_exact(self) -> None:
        payload = payload_sha(0)
        events = [
            response_discard_event(payload, attempt=1),
            success_event(payload, attempt=3),
            *clean_events(167, start=1),
        ]
        failures = AUDIT.classify_usage_events(events)[1]
        self.assertTrue(any("attempts_not_contiguous" in item for item in failures))

        events[1]["request_payload_sha256"] = payload_sha(999)
        failures = AUDIT.classify_usage_events(events)[1]
        self.assertTrue(any("payload_hash_changed" in item for item in failures))

        events = clean_events()
        events[1]["client_request_id"] = events[0]["client_request_id"]
        failures = AUDIT.classify_usage_events(events)[1]
        self.assertIn("event[2].client_request_id_reused", failures)

        events = clean_events()
        events[0]["upstream_request_id"] = "unsafe id with spaces"
        failures = AUDIT.classify_usage_events(events)[1]
        self.assertIn("event[1].upstream_request_id_unsafe", failures)

    def test_success_identity_finish_content_and_cost_are_exact(self) -> None:
        mutations = {
            "status": 201,
            "endpoint_identity": "azure.example/v1/chat/completions",
            "response_model": "gpt-5.4-mini",
            "provider": "Azure",
            "service_tier": "priority",
            "observed_finish_class": "length",
            "visible_content_state": "blank",
            "response_forwarded": False,
            "unknown_cost": True,
            "upstream_request_id": "unknown",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                events = clean_events()
                events[0][field] = value
                self.assertTrue(AUDIT.classify_usage_events(events)[1])

        events = clean_events()
        events[0]["cost_usd"] += Decimal("0.000000001")
        failures = AUDIT.classify_usage_events(events)[1]
        self.assertIn("event[1].token_derived_cost_mismatch", failures)

    def test_transient_shape_terminal_closed_schema_and_fuse_fail(self) -> None:
        payload = payload_sha(0)
        bad = transport_discard_event(
            payload, reason="upstream_http_error", status=401, attempt=1
        )
        failures = AUDIT.classify_usage_events(
            [bad, success_event(payload, attempt=2), *clean_events(167, start=1)]
        )[1]
        self.assertIn("event[1].transport_transient_shape_mismatch", failures)

        terminal = response_discard_event(payload, attempt=1)
        terminal["event"] = "terminal_rejection"
        terminal["retryable"] = False
        failures = AUDIT.classify_usage_events(
            [terminal, success_event(payload, attempt=2), *clean_events(167, start=1)]
        )[1]
        self.assertIn("terminal_rejections!=0", failures)

        events = clean_events()
        events[0]["content"] = "forbidden"
        self.assertIn(
            "event[1].closed_schema_mismatch",
            AUDIT.classify_usage_events(events)[1],
        )

        expensive = clean_events()
        for event in expensive:
            event["prompt_tokens"] = 1_000_000
            event["cached_tokens"] = 0
            event["completion_tokens"] = 1_000_000
            event["reasoning_tokens"] = 0
            event["cost_usd"] = official_cost(1_000_000, 0, 1_000_000)
        self.assertIn(
            "ledger_cost_above_arm_fuse",
            AUDIT.classify_usage_events(expensive)[1],
        )


class ProxyLogPolicyTests(unittest.TestCase):
    @staticmethod
    def evidence(events: list[dict] | None = None) -> tuple[dict, dict, dict]:
        summary, failures = AUDIT.classify_usage_events(events or clean_events())
        if failures:
            raise AssertionError(failures)
        startup = {
            "ok": True,
            "upstream": AUDIT.EXPECTED_UPSTREAM,
            "endpoint_identity": AUDIT.EXPECTED_ENDPOINT,
            "provider_identity": AUDIT.EXPECTED_PROVIDER,
            "model": AUDIT.EXPECTED_MODEL,
            "max_completion_tokens": 4096,
            "reasoning_effort": "high",
            "service_tier": "default",
            "store": False,
            "n": 1,
            "usage_log": str(HERE / "synthetic-usage.jsonl"),
            "max_cost_usd": "2.450000000",
            "request_reservation_usd": "0.318432000",
            "safety_reserve_usd": "0.010000000",
            "upstream_timeout_seconds": Decimal("105.0"),
            "direct_upstream_networking": True,
            "environment_proxy_inheritance": False,
            "prompt_or_completion_content_retained": False,
        }
        usage = {
            "calls": 168,
            "errors": summary["unknown_cost_discarded_transients"],
            "malformed_responses": summary["known_cost_discarded_transients"],
            "terminal_rejections": 0,
            "discarded_transients": summary["discarded_transients"],
            "hidden_sdk_retry_rejections": 0,
            "cost_usd": summary["conservative_ledger_cost_usd"],
            "prompt_tokens": summary["prompt_tokens"],
            "cached_tokens": summary["cached_tokens"],
            "completion_tokens": summary["completion_tokens"],
            "reasoning_tokens": summary["reasoning_tokens"],
            "unknown_cost_attempts": summary["unknown_cost_attempts"],
            "max_cost_usd": "2.450000000",
            "request_reservation_usd": "0.318432000",
            "safety_reserve_usd": "0.010000000",
            "reserved_cost_usd": "0.000000000",
            "max_discarded_transients": 4,
            "max_logical_attempts": 5,
            "transport_failed": False,
            "fatal_reason_code": None,
            "pending_logical_calls": 0,
            "active_logical_calls": 0,
            "scope": "process",
            "enforcement": "hard_fuse",
        }
        return startup, {"stopped": True, "usage": usage}, summary

    def test_exact_proxy_startup_and_clean_stop_pass(self) -> None:
        startup, stopped, summary = self.evidence()
        _, failures = AUDIT._proxy_log_failures(
            startup,
            stopped,
            usage_summary=summary,
            usage_log=HERE / "synthetic-usage.jsonl",
        )
        self.assertEqual(failures, ())

    def test_recovered_unknown_transient_reconciles_health(self) -> None:
        payload = payload_sha(0)
        events = [
            transport_discard_event(payload, attempt=1),
            success_event(payload, attempt=2),
            *clean_events(167, start=1),
        ]
        startup, stopped, summary = self.evidence(events)
        _, failures = AUDIT._proxy_log_failures(
            startup,
            stopped,
            usage_summary=summary,
            usage_log=HERE / "synthetic-usage.jsonl",
        )
        self.assertEqual(failures, ())

    def test_proxy_route_fatal_hidden_and_cost_fail_closed(self) -> None:
        mutations = (
            ("startup", "upstream", "https://router.example/v1/chat/completions"),
            ("startup", "environment_proxy_inheritance", True),
            ("usage", "transport_failed", True),
            ("usage", "fatal_reason_code", "terminal_response"),
            ("usage", "hidden_sdk_retry_rejections", 1),
            ("usage", "cost_usd", "0.999999999"),
            ("usage", "enforcement", "soft_fuse"),
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
    def report(summary: dict) -> dict:
        unknown = summary["unknown_cost_attempts"]
        discarded = summary["discarded_transients"]
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
            "validation": {field: [] for field in AUDIT._ZERO_VALIDATION_FIELDS},
            "usage": {
                "events": summary["events"],
                "completion_calls": 168,
                "upstream_errors": 0,
                "malformed_http_200_responses": 0,
                "invalid_completion_identities": 0,
                "unknown_cost_attempts": unknown,
                "publication_ready": unknown == 0,
                "completion_provider_counts": {"OpenAI": 168},
                "request_model_counts": {AUDIT.EXPECTED_MODEL: 168 + discarded},
                "finish_reason_counts": {"unknown": 168},
                "cost_usd": float(summary["conservative_ledger_cost_usd"]),
                "prompt_tokens": summary["prompt_tokens"],
                "cached_tokens": summary["cached_tokens"],
                "completion_tokens": summary["completion_tokens"],
                "reasoning_tokens": summary["reasoning_tokens"],
            },
            "harness_log": {
                "failed_attempt_counts": ({"1": discarded} if discarded else {}),
                "timed_out_attempt_counts": {},
                "returned_none_responses": 0,
                "attempt_five_failures": 0,
            },
        }

    def test_frozen_raw_auditor_allows_recovered_unknown_transport_cost(self) -> None:
        payload = payload_sha(0)
        events = [
            transport_discard_event(payload, attempt=1),
            success_event(payload, attempt=2),
            *clean_events(167, start=1),
        ]
        summary, failures = AUDIT.classify_usage_events(events)
        self.assertEqual(failures, ())
        report = self.report(summary)
        self.assertFalse(report["complete"])
        self.assertEqual(AUDIT._audit_failures(report, usage_summary=summary), ())

    def test_harness_retry_histogram_must_reconcile(self) -> None:
        payload = payload_sha(0)
        events = [
            response_discard_event(payload, attempt=1),
            success_event(payload, attempt=2),
            *clean_events(167, start=1),
        ]
        summary, _ = AUDIT.classify_usage_events(events)
        report = self.report(summary)
        report["harness_log"]["failed_attempt_counts"] = {}
        self.assertIn(
            "harness_retry_count!=discarded_transients",
            AUDIT._audit_failures(report, usage_summary=summary),
        )

    def test_score_blind_gate_source_has_no_metric_projection(self) -> None:
        source = (HERE / "transport_arm_audit.py").read_text(encoding="utf-8")
        self.assertIn('"score_values_present": False', source)
        self.assertIn('"fatal_health_watchdog": True', source)
        self.assertIn('"discarded_transients_never_forwarded_or_scored": True', source)
        self.assertNotIn('"metrics": report.get', source)
        json.dumps({"score_values_present": False}, allow_nan=False)


def finalizer_precommit() -> dict:
    return {
        "schema_version": 1,
        "status": "score-exposed-statically-sealed-awaiting-paid-canary",
        "classification": FINALIZE.CLASSIFICATION,
        "score_fields_present": True,
        "r3_score_fields_present": False,
        "score_observed_before_r3_precommit": True,
        "same_model_self_judge": True,
        "r3_provider_or_model_calls_before_precommit": 0,
        "historical_r1_r2_provider_calls_and_scores_observed": True,
        "r2_terminal_disclosure_sha256": FINALIZE.EXPECTED_R2_DISCLOSURE_SHA256,
        "pricing_evidence_sha256": FINALIZE.EXPECTED_PRICING_SHA256,
        "sealed_protocol": {
            "directory": "benchmark_records/reproduction-r3",
            "sealed_files_manifest_sha256": "a" * 64,
            "bound_inputs_manifest_sha256": "b" * 64,
        },
        "official_evaluator": {
            "answerer_model": FINALIZE.MODEL,
            "judge_model": FINALIZE.MODEL,
            "expected_answerer_calls_per_arm": 84,
            "expected_judge_calls_per_arm": 84,
            "expected_successful_forwarded_role_calls_per_arm": 168,
        },
        "transport_policy": {
            "official_endpoint": FINALIZE.UPSTREAM,
            "direct_upstream_networking": True,
            "fatal_health_watchdog": True,
            "maximum_discarded_transients_per_arm": 4,
            "maximum_physical_attempts_per_logical_call": 5,
            "operator_selective_retries": False,
            "accepted_completion_identity": {
                "endpoint_identity": FINALIZE.ENDPOINT,
                "provider": FINALIZE.PROVIDER,
                "request_model": FINALIZE.MODEL,
                "response_model": FINALIZE.MODEL,
                "service_tier": "default",
                "observed_finish_class": "stop",
                "visible_content_state": "nonempty",
                "response_complete": True,
                "response_forwarded": True,
                "unknown_cost": False,
            },
        },
        "success_threshold": {
            "cutoff": 50,
            "minimum_passes": 40,
            "total_questions": 42,
            "execution_or_publication_branch_on_threshold": False,
        },
        "fairness": {
            "replication_unconditional_after_score_blind_primary_transport_gate": True,
            "selective_question_reruns": False,
            "benchmark_answer_hardcoding": False,
            "same_prediction_bytes_for_both_arms": True,
            "score_driven_execution_or_publication_branching": False,
            "score_driven_prompt_or_route_changes": False,
        },
    }


def finalizer_transport(events: list[dict] | None = None) -> dict:
    summary, failures = AUDIT.classify_usage_events(events or clean_events())
    if failures:
        raise AssertionError(failures)
    policy = {
        "successful_calls_required": 168,
        "successful_identity": {
            "endpoint_identity": FINALIZE.ENDPOINT,
            "provider": FINALIZE.PROVIDER,
            "request_model": FINALIZE.MODEL,
            "response_model": FINALIZE.MODEL,
            "service_tier": "default",
            "http_status": 200,
            "observed_finish_class": "stop",
            "visible_content_state": "nonempty",
            "response_complete": True,
            "response_forwarded": True,
            "unknown_cost": False,
        },
        "discarded_transients_maximum": 4,
        "physical_attempts_per_logical_call_maximum": 5,
        "discarded_transients_never_forwarded_or_scored": True,
        "operator_selective_retries": False,
        "full_arm_restart_on_terminal_failure": True,
        "internal_sdk_retries_disabled": True,
        "fatal_health_watchdog": True,
        "pricing_evidence_sha256": FINALIZE.EXPECTED_PRICING_SHA256,
        "exact_token_cost_formula_reconciled": True,
        "reasoning_tokens_billed_twice": False,
    }
    proxy = {
        "calls": 168,
        "errors": summary["unknown_cost_discarded_transients"],
        "malformed_responses": summary["known_cost_discarded_transients"],
        "terminal_rejections": 0,
        "discarded_transients": summary["discarded_transients"],
        "unknown_cost_attempts": summary["unknown_cost_attempts"],
        "hidden_sdk_retry_rejections": 0,
        "cost_usd": summary["conservative_ledger_cost_usd"],
        "reserved_cost_usd": "0.000000000",
        "transport_failed": False,
        "fatal_reason_code": None,
        "pending_logical_calls": 0,
        "active_logical_calls": 0,
        "prompt_tokens": summary["prompt_tokens"],
        "cached_tokens": summary["cached_tokens"],
        "completion_tokens": summary["completion_tokens"],
        "reasoning_tokens": summary["reasoning_tokens"],
    }
    bindings = {
        "evaluation_auditor_sha256": FINALIZE.EXPECTED_EVALUATOR_SHA256,
        "proxy_source_sha256": FINALIZE.EXPECTED_PROXY_SHA256,
        "harness_client_sha256": FINALIZE.EXPECTED_HARNESS_CLIENT_SHA256,
        "official_model_pricing_evidence_sha256": FINALIZE.EXPECTED_PRICING_SHA256,
        "frozen_copy_manifest_sha256": "1" * 64,
        "question_id_file_sha256": "2" * 64,
        "usage_log_sha256": "3" * 64,
        "evaluator_log_sha256": "4" * 64,
        "proxy_log_sha256": "5" * 64,
        "raw_evaluation_audit_canonical_sha256": "6" * 64,
        "raw_evaluation_audit_file_sha256": "7" * 64,
    }
    return {
        "schema_version": FINALIZE.TRANSPORT_SCHEMA,
        "authorized": True,
        "score_values_present": False,
        "score_driven_branching": False,
        "official_harness_score_complete": True,
        "expected_questions": 42,
        "cutoffs": ["top_20", "top_50"],
        "transport_policy": policy,
        "usage": summary,
        "proxy_stop": proxy,
        "bindings": bindings,
        "failures": [],
    }


class FinalizerPolicyTests(unittest.TestCase):
    def test_exact_r3_precommit_contract_passes(self) -> None:
        protocol = FINALIZE._verify_precommit(finalizer_precommit())
        self.assertEqual(protocol["sealed_files_manifest_sha256"], "a" * 64)

    def test_precommit_score_call_transport_and_fairness_mutations_fail(self) -> None:
        mutations = (
            ("top", "r3_score_fields_present", True),
            ("top", "r3_provider_or_model_calls_before_precommit", 1),
            ("transport", "fatal_health_watchdog", False),
            ("transport", "official_endpoint", "https://openrouter.ai/api/v1"),
            ("fairness", "selective_question_reruns", True),
            ("threshold", "minimum_passes", 39),
        )
        for target, field, value in mutations:
            with self.subTest(target=target, field=field):
                document = finalizer_precommit()
                record = {
                    "top": document,
                    "transport": document["transport_policy"],
                    "fairness": document["fairness"],
                    "threshold": document["success_threshold"],
                }[target]
                record[field] = value
                with self.assertRaises(ValueError):
                    FINALIZE._verify_precommit(document)

    def test_clean_transport_and_recovered_unknown_retry_pass(self) -> None:
        clean = FINALIZE._verify_transport(finalizer_transport(), label="primary")
        self.assertEqual(clean["transients"], 0)
        payload = payload_sha(0)
        events = [
            transport_discard_event(payload, attempt=1),
            success_event(payload, attempt=2),
            *clean_events(167, start=1),
        ]
        recovered = FINALIZE._verify_transport(
            finalizer_transport(events), label="replication"
        )
        self.assertEqual(recovered["unknown_transients"], 1)
        self.assertEqual(recovered["transient_cost"], FINALIZE.ARM_RESERVATION)

    def test_transport_fatal_hidden_identity_and_cost_mutations_fail(self) -> None:
        mutations = (
            ("policy", "fatal_health_watchdog", False),
            ("usage", "terminal_rejections", 1),
            ("usage", "unique_safe_request_ids_verified", False),
            ("proxy", "hidden_sdk_retry_rejections", 1),
            ("proxy", "fatal_reason_code", "terminal_response"),
            ("proxy", "cost_usd", "1.000000000"),
            ("bindings", "proxy_source_sha256", "0" * 64),
        )
        for target, field, value in mutations:
            with self.subTest(target=target, field=field):
                document = finalizer_transport()
                record = {
                    "policy": document["transport_policy"],
                    "usage": document["usage"],
                    "proxy": document["proxy_stop"],
                    "bindings": document["bindings"],
                }[target]
                record[field] = value
                with self.assertRaises(ValueError):
                    FINALIZE._verify_transport(document, label="arm")

    def test_finalizer_cli_and_source_exclude_openrouter_telemetry(self) -> None:
        source = (HERE / "finalize_pair.py").read_text(encoding="utf-8")
        for required in (
            'parser.add_argument("--r2-terminal"',
            'parser.add_argument("--r2-disclosure"',
            'parser.add_argument("--pricing-evidence"',
            'parser.add_argument("--admission"',
        ):
            self.assertIn(required, source)
        for forbidden in (
            "--telemetry-pre",
            "--telemetry-between",
            "--telemetry-post",
            "OpenRouter",
            "openrouter",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
