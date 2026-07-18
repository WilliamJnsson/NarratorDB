#!/usr/bin/env python3
"""Finalize the complete prospective R3 pair without a score-driven branch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "narratordb.v18-gpt-selfjudge-paired-score.r3.v1"
TRANSPORT_SCHEMA = "narratordb.v18-gpt-selfjudge-transport-arm-audit.r3.v1"
ADMISSION_SCHEMA = "narratordb.v18-gpt-selfjudge-campaign-admission.r3.v1"
MODEL = "gpt-5.4-mini-2026-03-17"
PROVIDER = "OpenAI"
ENDPOINT = "api.openai.com/v1/chat/completions"
UPSTREAM = "https://api.openai.com/v1/chat/completions"
CUTOFFS = ("top_20", "top_50")
QUESTIONS = 42
SUCCESS_CALLS = 168
MAX_TRANSIENTS = 4
MAX_ATTEMPTS = 5
TARGET_PERCENT = Decimal("95")
TARGET_MINIMUM_CORRECT = 40
PRIOR_TRACKED_MAXIMUM = Decimal("2.878283432")
CAMPAIGN_CEILING = Decimal("30.00")
CANARY_FUSE = Decimal("0.611152")
ARM_FUSE = Decimal("2.45")
ARM_RESERVATION = Decimal("0.318432")
SAFETY_RESERVE = Decimal("0.01")
INPUT_USD_PER_MTOK = Decimal("0.75")
CACHED_INPUT_USD_PER_MTOK = Decimal("0.075")
OUTPUT_USD_PER_MTOK = Decimal("4.50")
ONE_MILLION = Decimal("1000000")
MONEY_QUANTUM = Decimal("0.000000001")
TOLERANCE = Decimal("0.000000001")
EXPECTED_R1_TERMINAL_SHA256 = (
    "4bdfe140a4f232b79a1e2b6121fa4a496b01ecd0d924a61a7c4e2468b0481eba"
)
EXPECTED_R2_TERMINAL_SHA256 = (
    "808d84f547fbd42587a2c1ac17e8b3fd8bf3853ec34221344bc58e94c0a14b9d"
)
EXPECTED_R2_DISCLOSURE_SHA256 = (
    "715698e49cbf421046063f5537642be955d3da6091f67a8d0674fe6631ce080c"
)
EXPECTED_PRICING_SHA256 = (
    "41e6f74aab48e82f3854fff2c6a6425a4b7c13879dc3006674526d9190a41870"
)
EXPECTED_PROXY_SHA256 = (
    "90a342bb7f97162a7af448d26ed191a78c5618a56a8106b9d11868a6a128c253"
)
EXPECTED_EVALUATOR_SHA256 = (
    "011708f614bee9cfc15209986bc68969f3c70191a06f540d3a86a2d7f74aeefc"
)
EXPECTED_HARNESS_CLIENT_SHA256 = (
    "b0dc8f4172ed11f7f4161df47c77ca83dd5996b075494cc39bd6a4d0a1f93701"
)
FORMULA = (
    "((prompt_tokens - cached_tokens) * 0.75 + cached_tokens * 0.075 + "
    "completion_tokens * 4.50) / 1000000"
)
CLASSIFICATION = (
    "score-exposed post-hoc consumed development-set diagnostic; same-model "
    "self-judge; not blind, not an untouched holdout, not an independent-judge "
    "score, not a headline benchmark, and not a Mem0 head-to-head"
)
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_CLIENT_REQUEST_ID = re.compile(r"narratordb-r3-[0-9a-f]{32}")
_EVENT_FIELDS = {
    "timestamp",
    "event",
    "status",
    "endpoint_identity",
    "provider",
    "request_model",
    "response_model",
    "service_tier",
    "observed_finish_class",
    "visible_content_state",
    "response_complete",
    "response_forwarded",
    "discarded_reason",
    "retryable",
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cost_usd",
    "unknown_cost",
    "request_payload_sha256",
    "logical_call_id",
    "attempt_number",
    "client_request_id",
    "upstream_request_id",
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _strict_json(text: str, *, label: str) -> Any:
    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"invalid strict JSON in {label}: {error}") from error


def _load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"input is missing or a symbolic link: {path}")
    parsed = _strict_json(path.read_text(encoding="utf-8"), label=str(path))
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object: {path}")
    return parsed


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"ledger is missing or a symbolic link: {path}")
    payload = path.read_bytes()
    if not payload or not payload.endswith(b"\n"):
        raise ValueError(f"ledger must be nonempty and newline terminated: {path}")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"ledger is not UTF-8: {path}") from error
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        parsed = _strict_json(line, label=f"{path} line {line_number}")
        if not isinstance(parsed, dict):
            raise ValueError(f"ledger line {line_number} is not an object")
        events.append(parsed)
    return events


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha_string(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _exact_nonnegative_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} is boolean")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{label} is not numeric") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return parsed


def _timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} lacks a timezone")
    return parsed


def _percentage(numerator: int, denominator: int) -> str:
    return format(Decimal(numerator) * Decimal(100) / Decimal(denominator), "f")


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError(f"path escapes repository root: {path}") from error


def _write_new(path: Path, payload: bytes) -> str:
    if path.exists() or path.is_symlink():
        raise ValueError(f"output must start absent: {path}")
    if not path.parent.is_dir():
        raise ValueError(f"output parent does not exist: {path.parent}")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o444,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.fchmod(descriptor, 0o444)
    finally:
        os.close(descriptor)
    return hashlib.sha256(payload).hexdigest()


def _official_cost(prompt: int, cached: int, completion: int) -> Decimal:
    if not all(_exact_nonnegative_int(value) for value in (prompt, cached, completion)):
        raise ValueError("official token accounting is not integral")
    if cached > prompt:
        raise ValueError("cached tokens exceed prompt tokens")
    return (
        Decimal(prompt - cached) * INPUT_USD_PER_MTOK
        + Decimal(cached) * CACHED_INPUT_USD_PER_MTOK
        + Decimal(completion) * OUTPUT_USD_PER_MTOK
    ) / ONE_MILLION


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM)


def _verify_pricing(path: Path) -> dict[str, Any]:
    document = _load(path)
    if _sha256(path) != EXPECTED_PRICING_SHA256:
        raise ValueError("official pricing evidence checksum mismatch")
    expected = {
        "schema_version": "narratordb.openai-model-pricing-evidence.v1",
        "model_snapshot": MODEL,
        "input_usd_per_million_tokens": "0.75",
        "cached_input_usd_per_million_tokens": "0.075",
        "output_usd_per_million_tokens": "4.50",
        "pricing_currency": "USD",
        "cost_formula": FORMULA,
        "completion_reasoning_accounting": (
            "reasoning_tokens are a subset of completion_tokens and are not "
            "charged a second time"
        ),
    }
    if any(document.get(field) != value for field, value in expected.items()):
        raise ValueError("official pricing evidence fields mismatch")
    return document


def _verify_precommit(document: Mapping[str, Any]) -> Mapping[str, Any]:
    protocol = document.get("sealed_protocol")
    evaluator = document.get("official_evaluator")
    transport = document.get("transport_policy")
    threshold = document.get("success_threshold")
    fairness = document.get("fairness")
    accepted = transport.get("accepted_completion_identity") if isinstance(transport, Mapping) else None
    endpoint = None
    if isinstance(transport, Mapping):
        endpoint = transport.get("official_endpoint") or transport.get("upstream")
    if endpoint is None and isinstance(accepted, Mapping):
        endpoint = accepted.get("official_endpoint") or accepted.get("endpoint")
    if not (
        document.get("schema_version") == 1
        and document.get("status") == "score-exposed-statically-sealed-awaiting-paid-canary"
        and document.get("classification") == CLASSIFICATION
        and document.get("score_fields_present") is True
        and document.get("r3_score_fields_present") is False
        and document.get("score_observed_before_r3_precommit") is True
        and document.get("same_model_self_judge") is True
        and document.get("r3_provider_or_model_calls_before_precommit") == 0
        and document.get("historical_r1_r2_provider_calls_and_scores_observed") is True
        and document.get("r2_terminal_disclosure_sha256")
        == EXPECTED_R2_DISCLOSURE_SHA256
        and document.get("pricing_evidence_sha256") == EXPECTED_PRICING_SHA256
        and isinstance(protocol, Mapping)
        and isinstance(protocol.get("directory"), str)
        and _sha_string(protocol.get("sealed_files_manifest_sha256"))
        and _sha_string(protocol.get("bound_inputs_manifest_sha256"))
        and isinstance(evaluator, Mapping)
        and evaluator.get("answerer_model") == MODEL
        and evaluator.get("judge_model") == MODEL
        and evaluator.get("expected_answerer_calls_per_arm") == 84
        and evaluator.get("expected_judge_calls_per_arm") == 84
        and evaluator.get("expected_successful_forwarded_role_calls_per_arm")
        == SUCCESS_CALLS
        and isinstance(transport, Mapping)
        and endpoint == UPSTREAM
        and transport.get("direct_upstream_networking") is True
        and transport.get("fatal_health_watchdog") is True
        and transport.get("maximum_discarded_transients_per_arm") == MAX_TRANSIENTS
        and transport.get("maximum_physical_attempts_per_logical_call") == MAX_ATTEMPTS
        and transport.get("operator_selective_retries") is False
        and isinstance(accepted, Mapping)
        and accepted.get("endpoint_identity") == ENDPOINT
        and accepted.get("provider") == PROVIDER
        and accepted.get("request_model") == MODEL
        and accepted.get("response_model") == MODEL
        and accepted.get("service_tier") == "default"
        and accepted.get("observed_finish_class") == "stop"
        and accepted.get("visible_content_state") == "nonempty"
        and accepted.get("response_complete") is True
        and accepted.get("response_forwarded") is True
        and accepted.get("unknown_cost") is False
        and isinstance(threshold, Mapping)
        and threshold.get("cutoff") == 50
        and threshold.get("minimum_passes") == TARGET_MINIMUM_CORRECT
        and threshold.get("total_questions") == QUESTIONS
        and threshold.get("execution_or_publication_branch_on_threshold") is False
        and isinstance(fairness, Mapping)
        and fairness.get(
            "replication_unconditional_after_score_blind_primary_transport_gate"
        )
        is True
        and fairness.get("selective_question_reruns") is False
        and fairness.get("benchmark_answer_hardcoding") is False
        and fairness.get("same_prediction_bytes_for_both_arms") is True
        and fairness.get("score_driven_execution_or_publication_branching") is False
        and fairness.get("score_driven_prompt_or_route_changes") is False
    ):
        raise ValueError("R3 score-exposed precommit invariants mismatch")
    return protocol


def _verify_history(
    r1_path: Path, r2_path: Path, disclosure_path: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    r1 = _load(r1_path)
    r2 = _load(r2_path)
    disclosure = _load(disclosure_path)
    r1_metrics = r1.get("primary_score", {}).get("metrics", {})
    if not (
        _sha256(r1_path) == EXPECTED_R1_TERMINAL_SHA256
        and r1.get("schema_version")
        == "narratordb.v18-gpt-selfjudge-paid-pair-aborted-after-primary-audit.v1"
        and r1.get("status") == "ABORTED_AFTER_PRIMARY_AUDIT"
        and r1.get("terminal_reason", {}).get("score_values_used_to_decide_abort")
        is False
        and r1_metrics.get("top_20", {}).get("correct") == 40
        and r1_metrics.get("top_20", {}).get("total") == QUESTIONS
        and r1_metrics.get("top_50", {}).get("correct") == 41
        and r1_metrics.get("top_50", {}).get("total") == QUESTIONS
    ):
        raise ValueError("historical R1 terminal record mismatch")
    if not (
        _sha256(r2_path) == EXPECTED_R2_TERMINAL_SHA256
        and r2.get("schema_version")
        == "narratordb.v18-gpt-selfjudge-r2-terminal-execution.v1"
        and r2.get("status") == "TERMINAL_INCOMPLETE"
        and r2.get("failed_phase") == "unconditional-replication"
        and r2.get("score_driven_branching") is False
        and r2.get("failure_decision_used_score_values") is False
        and r2.get("score_values_embedded_in_terminal_record") is False
    ):
        raise ValueError("historical R2 terminal record mismatch")
    if not (
        _sha256(disclosure_path) == EXPECTED_R2_DISCLOSURE_SHA256
        and disclosure.get("schema_version")
        == "narratordb.v18-r2-terminal-disclosure.v1"
        and disclosure.get("status") == "TERMINAL_INCOMPLETE"
        and disclosure.get("classification")
        == "score-exposed post-hoc consumed-development history; not a paired score"
        and disclosure.get("terminal_execution_status_sha256")
        == EXPECTED_R2_TERMINAL_SHA256
        and disclosure.get("cumulative_campaign_conservative_exposure_usd_after_r2")
        == str(PRIOR_TRACKED_MAXIMUM)
        and disclosure.get("provider_billing_reconciled") is False
        and disclosure.get("primary", {}).get("score_complete") is True
        and disclosure.get("replication", {}).get("transport_score_complete") is False
        and disclosure.get("replication", {}).get("failure_decision_used_score_values")
        is False
    ):
        raise ValueError("historical R2 disclosure mismatch")
    return r1, r2, disclosure


def _verify_admission(document: Mapping[str, Any]) -> dict[str, Decimal]:
    checks = document.get("checks")
    attestation = document.get("balance_attestation")
    if not (
        document.get("schema_version") == ADMISSION_SCHEMA
        and document.get("admitted") is True
        and isinstance(checks, Mapping)
        and checks
        and all(value is True for value in checks.values())
        and document.get("model_snapshot") == MODEL
        and document.get("official_openai_endpoint") == UPSTREAM
        and document.get("pricing_evidence_sha256") == EXPECTED_PRICING_SHA256
        and document.get("cost_formula") == FORMULA
        and document.get("reasoning_tokens_billed_twice") is False
        and document.get("prior_r1_terminal_sha256") == EXPECTED_R1_TERMINAL_SHA256
        and document.get("prior_r2_terminal_sha256") == EXPECTED_R2_TERMINAL_SHA256
        and document.get("r2_terminal_disclosure_sha256")
        == EXPECTED_R2_DISCLOSURE_SHA256
        and document.get("credential_recorded") is False
        and document.get("model_content_recorded") is False
        and document.get("provider_telemetry_performed") is False
        and isinstance(attestation, Mapping)
        and attestation.get("available_usd") == "30.00"
        and attestation.get("source") == "user-provided"
        and attestation.get("verification") == "not_api_verified"
        and attestation.get("provider_balance_endpoint_called") is False
        and attestation.get("organization_admin_key_required_or_used") is False
        and _sha_string(document.get("fx_sha256"))
    ):
        raise ValueError("R3 local admission mismatch")
    values = {
        "prior": _decimal(
            document.get("prior_tracked_cumulative_maximum_usd"),
            label="admission prior exposure",
        ),
        "canary_fuse": _decimal(
            document.get("new_canary_process_fuse_usd"), label="canary fuse"
        ),
        "arm_fuses": _decimal(
            document.get("new_arm_process_fuses_usd"), label="arm fuses"
        ),
        "allocation": _decimal(
            document.get("new_allocation_usd"), label="new allocation"
        ),
        "tracked": _decimal(
            document.get("tracked_cumulative_maximum_usd"), label="tracked maximum"
        ),
        "ceiling": _decimal(
            document.get("tracked_campaign_ceiling_usd"), label="campaign ceiling"
        ),
        "rate": _decimal(document.get("usd_per_eur"), label="USD per EUR"),
    }
    if values != {
        "prior": PRIOR_TRACKED_MAXIMUM,
        "canary_fuse": CANARY_FUSE,
        "arm_fuses": ARM_FUSE * 2,
        "allocation": CANARY_FUSE + ARM_FUSE * 2,
        "tracked": PRIOR_TRACKED_MAXIMUM + CANARY_FUSE + ARM_FUSE * 2,
        "ceiling": CAMPAIGN_CEILING,
        "rate": values["rate"],
    } or values["rate"] <= 0:
        raise ValueError("R3 local admission budget arithmetic mismatch")
    return values


def _verify_transport(document: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    failures = document.get("failures")
    policy = document.get("transport_policy")
    identity = policy.get("successful_identity") if isinstance(policy, Mapping) else None
    usage = document.get("usage")
    proxy = document.get("proxy_stop")
    bindings = document.get("bindings")
    if not (
        document.get("schema_version") == TRANSPORT_SCHEMA
        and document.get("authorized") is True
        and document.get("score_values_present") is False
        and document.get("score_driven_branching") is False
        and document.get("official_harness_score_complete") is True
        and document.get("expected_questions") == QUESTIONS
        and document.get("cutoffs") == list(CUTOFFS)
        and failures == []
        and isinstance(policy, Mapping)
        and policy.get("successful_calls_required") == SUCCESS_CALLS
        and policy.get("discarded_transients_maximum") == MAX_TRANSIENTS
        and policy.get("physical_attempts_per_logical_call_maximum") == MAX_ATTEMPTS
        and policy.get("discarded_transients_never_forwarded_or_scored") is True
        and policy.get("operator_selective_retries") is False
        and policy.get("full_arm_restart_on_terminal_failure") is True
        and policy.get("internal_sdk_retries_disabled") is True
        and policy.get("fatal_health_watchdog") is True
        and policy.get("pricing_evidence_sha256") == EXPECTED_PRICING_SHA256
        and policy.get("exact_token_cost_formula_reconciled") is True
        and policy.get("reasoning_tokens_billed_twice") is False
        and isinstance(identity, Mapping)
        and identity.get("endpoint_identity") == ENDPOINT
        and identity.get("provider") == PROVIDER
        and identity.get("request_model") == MODEL
        and identity.get("response_model") == MODEL
        and identity.get("service_tier") == "default"
        and identity.get("http_status") == 200
        and identity.get("observed_finish_class") == "stop"
        and identity.get("visible_content_state") == "nonempty"
        and identity.get("response_complete") is True
        and identity.get("response_forwarded") is True
        and identity.get("unknown_cost") is False
        and isinstance(usage, Mapping)
        and isinstance(proxy, Mapping)
        and isinstance(bindings, Mapping)
    ):
        raise ValueError(f"{label} transport audit is not authorized and complete")

    transients = usage.get("discarded_transients")
    known = usage.get("known_cost_discarded_transients")
    unknown = usage.get("unknown_cost_discarded_transients")
    maximum_attempts = usage.get("maximum_physical_attempts_observed")
    if not (
        _exact_nonnegative_int(transients)
        and 0 <= transients <= MAX_TRANSIENTS
        and _exact_nonnegative_int(known)
        and _exact_nonnegative_int(unknown)
        and known + unknown == transients
        and usage.get("unknown_cost_attempts") == unknown
        and usage.get("successful_forwarded_official_openai_stop_calls")
        == SUCCESS_CALLS
        and usage.get("terminal_rejections") == 0
        and usage.get("unknown_events") == 0
        and usage.get("completed_logical_calls") == SUCCESS_CALLS
        and _exact_nonnegative_int(maximum_attempts)
        and 1 <= maximum_attempts <= MAX_ATTEMPTS
        and usage.get("retry_payload_identity_verified") is True
        and usage.get("unique_safe_request_ids_verified") is True
        and usage.get("exact_cost_formula_reconciled") is True
        and usage.get("reasoning_tokens_billed_twice") is False
    ):
        raise ValueError(f"{label} transport chain accounting mismatch")
    tokens: dict[str, int] = {}
    for field in ("prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens"):
        value = usage.get(field)
        if not _exact_nonnegative_int(value):
            raise ValueError(f"{label} {field} is invalid")
        tokens[field] = value
    if tokens["cached_tokens"] > tokens["prompt_tokens"] or tokens["reasoning_tokens"] > tokens["completion_tokens"]:
        raise ValueError(f"{label} aggregate token accounting mismatch")
    success_cost = _decimal(usage.get("known_success_cost_usd"), label=f"{label} success cost")
    transient_cost = _decimal(
        usage.get("discarded_transient_booked_cost_usd"),
        label=f"{label} transient cost",
    )
    conservative = _decimal(
        usage.get("conservative_ledger_cost_usd"), label=f"{label} ledger cost"
    )
    derived = _decimal(
        usage.get("token_derived_official_openai_cost_usd"),
        label=f"{label} derived cost",
    )
    if (
        abs(success_cost + transient_cost - conservative) > TOLERANCE
        or abs(derived - conservative) > TOLERANCE
        or conservative > ARM_FUSE + TOLERANCE
        or _decimal(usage.get("request_reservation_usd"), label="reservation")
        != ARM_RESERVATION
    ):
        raise ValueError(f"{label} conservative cost reconciliation mismatch")
    proxy_expected = {
        "calls": SUCCESS_CALLS,
        "errors": unknown,
        "malformed_responses": known,
        "terminal_rejections": 0,
        "discarded_transients": transients,
        "unknown_cost_attempts": unknown,
        "transport_failed": False,
        "fatal_reason_code": None,
        "pending_logical_calls": 0,
        "active_logical_calls": 0,
        "hidden_sdk_retry_rejections": 0,
        **tokens,
    }
    if any(proxy.get(field) != value for field, value in proxy_expected.items()):
        raise ValueError(f"{label} proxy stop summary mismatch")
    if (
        _decimal(proxy.get("cost_usd"), label=f"{label} proxy cost")
        != conservative
        or _decimal(proxy.get("reserved_cost_usd"), label=f"{label} reserved cost")
        != 0
    ):
        raise ValueError(f"{label} proxy stop cost mismatch")
    required_bindings = {
        "evaluation_auditor_sha256": EXPECTED_EVALUATOR_SHA256,
        "proxy_source_sha256": EXPECTED_PROXY_SHA256,
        "harness_client_sha256": EXPECTED_HARNESS_CLIENT_SHA256,
        "official_model_pricing_evidence_sha256": EXPECTED_PRICING_SHA256,
    }
    if any(bindings.get(field) != value for field, value in required_bindings.items()):
        raise ValueError(f"{label} source binding mismatch")
    for field in (
        "frozen_copy_manifest_sha256",
        "question_id_file_sha256",
        "usage_log_sha256",
        "evaluator_log_sha256",
        "proxy_log_sha256",
        "raw_evaluation_audit_canonical_sha256",
        "raw_evaluation_audit_file_sha256",
    ):
        if not _sha_string(bindings.get(field)):
            raise ValueError(f"{label} binding {field} is invalid")
    return {
        "transients": transients,
        "known_transients": known,
        "unknown_transients": unknown,
        "maximum_attempts": maximum_attempts,
        "success_cost": success_cost,
        "transient_cost": transient_cost,
        "conservative_cost": conservative,
        "tokens": tokens,
        "bindings": dict(bindings),
        "proxy_stop": dict(proxy),
        "events": usage.get("events"),
    }


def _verify_health(
    document: Mapping[str, Any],
    *,
    label: str,
    max_cost: Decimal,
    reservation: Decimal,
    max_completion_tokens: int,
    expected_usage: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    usage = document.get("usage")
    if not (
        document.get("ok") is True
        and document.get("upstream") == UPSTREAM
        and document.get("endpoint_identity") == ENDPOINT
        and document.get("provider_identity") == PROVIDER
        and document.get("model") == MODEL
        and document.get("max_completion_tokens") == max_completion_tokens
        and document.get("reasoning_effort") == "high"
        and document.get("service_tier") == "default"
        and document.get("store") is False
        and document.get("n") == 1
        and _decimal(document.get("upstream_timeout_seconds"), label=f"{label} timeout")
        == Decimal("105")
        and document.get("max_request_bytes") == 20 * 1024 * 1024
        and document.get("max_response_bytes") == 4 * 1024 * 1024
        and document.get("direct_upstream_networking") is True
        and document.get("environment_proxy_inheritance") is False
        and document.get("inbound_retry_count_policy") == "absent-or-zero-only"
        and document.get("local_caller_auth_required") is True
        and document.get("prompt_or_completion_content_retained") is False
        and isinstance(usage, Mapping)
        and _decimal(usage.get("max_cost_usd"), label=f"{label} max cost") == max_cost
        and _decimal(
            usage.get("request_reservation_usd"), label=f"{label} reservation"
        )
        == reservation
        and _decimal(usage.get("safety_reserve_usd"), label=f"{label} safety")
        == SAFETY_RESERVE
        and usage.get("max_discarded_transients") == MAX_TRANSIENTS
        and usage.get("max_logical_attempts") == MAX_ATTEMPTS
        and usage.get("hidden_sdk_retry_rejections") == 0
        and usage.get("transport_failed") is False
        and usage.get("fatal_reason_code") is None
        and usage.get("terminal_rejections") == 0
        and usage.get("pending_logical_calls") == 0
        and usage.get("active_logical_calls") == 0
        and usage.get("scope") == "process"
        and usage.get("enforcement") == "hard_fuse"
        and _decimal(usage.get("reserved_cost_usd"), label=f"{label} reserved") == 0
    ):
        raise ValueError(f"{label} official proxy health mismatch")
    if expected_usage is None:
        zero_fields = (
            "calls",
            "errors",
            "malformed_responses",
            "discarded_transients",
            "unknown_cost_attempts",
            "prompt_tokens",
            "cached_tokens",
            "completion_tokens",
            "reasoning_tokens",
        )
        if any(usage.get(field) != 0 for field in zero_fields) or _decimal(
            usage.get("cost_usd"), label=f"{label} cost"
        ) != 0:
            raise ValueError(f"{label} proxy was not fresh")
    else:
        if any(usage.get(field) != value for field, value in expected_usage.items()):
            raise ValueError(f"{label} proxy health accounting mismatch")
    return usage


def _verify_proxy_log(
    path: Path,
    *,
    usage_path: Path,
    max_cost: Decimal,
    reservation: Decimal,
    max_completion_tokens: int,
    after_usage: Mapping[str, Any],
) -> None:
    records = _load_jsonl(path)
    if len(records) != 2:
        raise ValueError(f"proxy log must have startup and stop only: {path}")
    startup, stopped = records
    expected = {
        "ok": True,
        "upstream": UPSTREAM,
        "endpoint_identity": ENDPOINT,
        "provider_identity": PROVIDER,
        "model": MODEL,
        "max_completion_tokens": max_completion_tokens,
        "reasoning_effort": "high",
        "service_tier": "default",
        "store": False,
        "n": 1,
        "direct_upstream_networking": True,
        "environment_proxy_inheritance": False,
        "prompt_or_completion_content_retained": False,
    }
    if (
        any(startup.get(field) != value for field, value in expected.items())
        or Path(str(startup.get("usage_log") or "")).resolve() != usage_path.resolve()
        or _decimal(startup.get("max_cost_usd"), label="proxy log max cost")
        != max_cost
        or _decimal(
            startup.get("request_reservation_usd"), label="proxy log reservation"
        )
        != reservation
        or _decimal(startup.get("safety_reserve_usd"), label="proxy log safety")
        != SAFETY_RESERVE
        or _decimal(startup.get("upstream_timeout_seconds"), label="proxy log timeout")
        != Decimal("105")
        or stopped.get("stopped") is not True
        or stopped.get("usage") != after_usage
    ):
        raise ValueError(f"proxy startup/stop log mismatch: {path}")


def _verify_canary(
    *,
    result_path: Path,
    usage_path: Path,
    health_before_path: Path,
    health_after_path: Path,
    proxy_log_path: Path,
) -> dict[str, Any]:
    result = _load(result_path)
    events = _load_jsonl(usage_path)
    calls = result.get("calls")
    if not (
        result.get("schema_version") == "narratordb.route-canary.v1"
        and result.get("complete") is True
        and result.get("same_model_self_judge") is True
        and result.get("prompt_or_completion_content_retained") is False
        and isinstance(calls, list)
        and [call.get("label") for call in calls] == ["answerer", "judge"]
        and len(events) == 2
    ):
        raise ValueError("strict official route canary is incomplete")
    client_ids: set[str] = set()
    upstream_ids: set[str] = set()
    total_cost = Decimal("0")
    tokens = {
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
    }
    for index, (call, event) in enumerate(zip(calls, events, strict=True), 1):
        if set(event) != _EVENT_FIELDS:
            raise ValueError(f"canary event {index} closed schema mismatch")
        prompt = event.get("prompt_tokens")
        cached = event.get("cached_tokens")
        completion = event.get("completion_tokens")
        reasoning = event.get("reasoning_tokens")
        if not (
            all(_exact_nonnegative_int(value) for value in (prompt, cached, completion, reasoning))
            and prompt > 0
            and completion > 0
            and cached <= prompt
            and reasoning <= completion
        ):
            raise ValueError(f"canary event {index} token accounting mismatch")
        expected_cost = _money(_official_cost(prompt, cached, completion))
        cost = _decimal(event.get("cost_usd"), label=f"canary event {index} cost")
        payload = event.get("request_payload_sha256")
        expected_logical = (
            hashlib.sha256(f"{payload}:1".encode("ascii")).hexdigest()
            if _sha_string(payload)
            else None
        )
        client_id = event.get("client_request_id")
        upstream_id = event.get("upstream_request_id")
        if not (
            event.get("event") == "completion"
            and event.get("status") == 200
            and event.get("endpoint_identity") == ENDPOINT
            and event.get("provider") == PROVIDER
            and event.get("request_model") == MODEL
            and event.get("response_model") == MODEL
            and event.get("service_tier") == "default"
            and event.get("observed_finish_class") == "stop"
            and event.get("visible_content_state") == "nonempty"
            and event.get("response_complete") is True
            and event.get("response_forwarded") is True
            and event.get("discarded_reason") is None
            and event.get("retryable") is False
            and event.get("unknown_cost") is False
            and event.get("attempt_number") == 1
            and event.get("logical_call_id") == expected_logical
            and isinstance(client_id, str)
            and _CLIENT_REQUEST_ID.fullmatch(client_id) is not None
            and client_id not in client_ids
            and isinstance(upstream_id, str)
            and upstream_id != "unknown"
            and _SAFE_REQUEST_ID.fullmatch(upstream_id) is not None
            and upstream_id not in upstream_ids
            and cost == expected_cost
            and call.get("request_model") == MODEL
            and call.get("response_model") == MODEL
            and call.get("endpoint_provider_identity") == PROVIDER
            and call.get("http_status") == 200
            and call.get("finish_reason") == "stop"
            and call.get("output_exact") is True
            and call.get("usage_validated_and_cost_computed") is True
            and _decimal(
                call.get("computed_cost_usd"), label=f"canary call {index} cost"
            )
            == cost
            and call.get("max_completion_tokens") == 128
            and call.get("reasoning_effort") == "high"
            and call.get("service_tier") == "default"
            and call.get("store") is False
            and call.get("temperature_omitted") is True
            and call.get("content_retained") is False
        ):
            raise ValueError(f"canary role {index} identity/accounting mismatch")
        client_ids.add(client_id)
        upstream_ids.add(upstream_id)
        total_cost += cost
        for field in tokens:
            tokens[field] += int(event[field])
    if total_cost > CANARY_FUSE + TOLERANCE:
        raise ValueError("canary ledger cost exceeds its fuse")
    before = _load(health_before_path)
    after = _load(health_after_path)
    _verify_health(
        before,
        label="canary-before",
        max_cost=CANARY_FUSE,
        reservation=Decimal("0.300576"),
        max_completion_tokens=128,
        expected_usage=None,
    )
    expected_after = {
        "calls": 2,
        "errors": 0,
        "malformed_responses": 0,
        "terminal_rejections": 0,
        "discarded_transients": 0,
        "unknown_cost_attempts": 0,
        "hidden_sdk_retry_rejections": 0,
        "transport_failed": False,
        "fatal_reason_code": None,
        "pending_logical_calls": 0,
        "active_logical_calls": 0,
        "cost_usd": format(total_cost, "f"),
        **tokens,
    }
    after_usage = _verify_health(
        after,
        label="canary-after",
        max_cost=CANARY_FUSE,
        reservation=Decimal("0.300576"),
        max_completion_tokens=128,
        expected_usage=expected_after,
    )
    _verify_proxy_log(
        proxy_log_path,
        usage_path=usage_path,
        max_cost=CANARY_FUSE,
        reservation=Decimal("0.300576"),
        max_completion_tokens=128,
        after_usage=after_usage,
    )
    return {"cost": total_cost, "tokens": tokens}


def _result_path(official_root: Path) -> Path:
    results = sorted(official_root.glob("longmemeval_results_*.json"))
    if len(results) != 1 or results[0].is_symlink():
        raise ValueError(f"expected exactly one official result in {official_root}")
    return results[0]


def _verify_result(
    result: Mapping[str, Any],
    raw: Mapping[str, Any],
    *,
    question_ids: set[str],
    label: str,
) -> tuple[dict[str, Any], dict[str, dict[str, bool]]]:
    metadata = result.get("metadata")
    metrics = result.get("metrics_by_cutoff")
    evaluations = result.get("evaluations")
    raw_metrics = raw.get("metrics")
    if not (
        isinstance(metadata, Mapping)
        and metadata.get("benchmark") == "longmemeval"
        and metadata.get("run_id") == "v7m42a1"
        and metadata.get("mode") == "answerer"
        and metadata.get("answerer_model") == MODEL
        and metadata.get("judge_model") == MODEL
        and metadata.get("provider") == "openai"
        and metadata.get("top_k") == 200
        and metadata.get("top_k_cutoffs") == list(CUTOFFS)
        and metadata.get("total_questions") == QUESTIONS
        and metadata.get("seed") == 42
        and metadata.get("evaluate_only") is True
        and metadata.get("all_questions") is True
        and isinstance(metrics, Mapping)
        and isinstance(evaluations, list)
        and isinstance(raw_metrics, Mapping)
        and raw.get("official_harness_score_complete") is True
        and raw.get("expected_questions") == QUESTIONS
        and raw.get("evaluated_questions") == QUESTIONS
        and raw.get("frozen_questions") == QUESTIONS
        and raw.get("scoped_question_subset") is True
        and raw.get("cutoffs") == list(CUTOFFS)
    ):
        raise ValueError(f"{label} official result metadata is incomplete")
    verdicts: dict[str, dict[str, bool]] = {cutoff: {} for cutoff in CUTOFFS}
    for item in evaluations:
        if not isinstance(item, Mapping):
            raise ValueError(f"{label} evaluation is not an object")
        question_id = item.get("question_id")
        cutoff_results = item.get("cutoff_results")
        if (
            not isinstance(question_id, str)
            or question_id not in question_ids
            or not isinstance(cutoff_results, Mapping)
        ):
            raise ValueError(f"{label} evaluation scope mismatch")
        for cutoff in CUTOFFS:
            cutoff_result = cutoff_results.get(cutoff)
            if not isinstance(cutoff_result, Mapping):
                raise ValueError(f"{label} missing {cutoff} verdict")
            judgment = cutoff_result.get("judgment")
            score = _decimal(
                cutoff_result.get("score"), label=f"{label} {question_id} score"
            )
            if judgment not in {"PASS", "FAIL"} or score not in {
                Decimal("0"),
                Decimal("1"),
            }:
                raise ValueError(f"{label} invalid {cutoff} verdict")
            passed = judgment == "PASS"
            if passed is not (score == 1) or question_id in verdicts[cutoff]:
                raise ValueError(f"{label} duplicate or inconsistent verdict")
            verdicts[cutoff][question_id] = passed
    if any(set(by_id) != question_ids for by_id in verdicts.values()):
        raise ValueError(f"{label} did not evaluate exactly the frozen 42 IDs")
    scores: dict[str, Any] = {}
    for cutoff in CUTOFFS:
        correct = sum(verdicts[cutoff].values())
        official = metrics.get(cutoff)
        overall = official.get("overall") if isinstance(official, Mapping) else None
        raw_metric = raw_metrics.get(cutoff)
        if not (
            isinstance(overall, Mapping)
            and isinstance(raw_metric, Mapping)
            and overall.get("correct") == correct
            and overall.get("total") == QUESTIONS
            and raw_metric.get("correct") == correct
            and raw_metric.get("total") == QUESTIONS
        ):
            raise ValueError(f"{label} {cutoff} metric mismatch")
        scores[cutoff] = {
            "correct": correct,
            "total": QUESTIONS,
            "accuracy_fraction": f"{correct}/{QUESTIONS}",
            "accuracy_percent": _percentage(correct, QUESTIONS),
            "failed_question_ids": sorted(
                question_id
                for question_id, passed in verdicts[cutoff].items()
                if not passed
            ),
        }
    return scores, verdicts


def _question_ids(path: Path) -> set[str]:
    parsed = _strict_json(path.read_text(encoding="utf-8"), label=str(path))
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) and item.strip() for item in parsed
    ):
        raise ValueError("question-ID file is not a string array")
    normalized = {item.strip() for item in parsed}
    if len(parsed) != QUESTIONS or len(normalized) != QUESTIONS:
        raise ValueError("question-ID scope is not exactly 42 unique IDs")
    return normalized


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--report-root", type=Path, required=True)
    parser.add_argument("--precommit", type=Path, required=True)
    parser.add_argument("--expected-precommit-sha256", required=True)
    parser.add_argument("--r1-terminal", type=Path, required=True)
    parser.add_argument("--r2-terminal", type=Path, required=True)
    parser.add_argument("--r2-disclosure", type=Path, required=True)
    parser.add_argument("--pricing-evidence", type=Path, required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--canary-result", type=Path, required=True)
    parser.add_argument("--canary-usage", type=Path, required=True)
    parser.add_argument("--primary-raw-audit", type=Path, required=True)
    parser.add_argument("--primary-transport-audit", type=Path, required=True)
    parser.add_argument("--replication-raw-audit", type=Path, required=True)
    parser.add_argument("--replication-transport-audit", type=Path, required=True)
    parser.add_argument("--question-id-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    args = parser.parse_args(argv)

    root = args.repository_root.resolve(strict=True)
    report = args.report_root.resolve(strict=True)
    precommit = args.precommit.resolve(strict=True)
    expected_precommit = args.expected_precommit_sha256.lower()
    if not _sha_string(expected_precommit) or _sha256(precommit) != expected_precommit:
        raise ValueError("published R3 precommit checksum mismatch")
    precommit_document = _load(precommit)
    protocol = _verify_precommit(precommit_document)
    script_dir = Path(__file__).resolve(strict=True).parent
    sealed_manifest = script_dir / "SEALED_FILES_SHA256SUMS"
    bound_manifest = script_dir / "BOUND_INPUTS_SHA256SUMS"
    if not (
        Path(str(protocol.get("directory"))).as_posix()
        == _relative(root, script_dir)
        and protocol.get("sealed_files_manifest_sha256") == _sha256(sealed_manifest)
        and protocol.get("bound_inputs_manifest_sha256") == _sha256(bound_manifest)
    ):
        raise ValueError("precommit does not bind the active sealed R3 protocol")

    _verify_pricing(args.pricing_evidence)
    r1, r2, disclosure = _verify_history(
        args.r1_terminal, args.r2_terminal, args.r2_disclosure
    )
    admission_document = _load(args.admission)
    admission = _verify_admission(admission_document)

    fx_path = report / "preflight/ecb-usd-eur.json"
    fx_raw_path = report / "preflight/ecb-eurofxref-daily.xml"
    fx = _load(fx_path)
    if not (
        _sha256(fx_path) == admission_document.get("fx_sha256")
        and fx.get("schema_version") == "narratordb.ecb-usd-eur-observation.v1"
        and fx.get("publisher") == "European Central Bank"
        and fx.get("base_currency") == "EUR"
        and fx.get("quote_currency") == "USD"
        and _decimal(fx.get("usd_per_eur"), label="FX rate") == admission["rate"]
        and fx.get("credential_recorded") is False
        and fx.get("model_content_recorded") is False
        and fx.get("raw_xml_sha256") == _sha256(fx_raw_path)
        and fx.get("raw_xml_bytes") == fx_raw_path.stat().st_size
        and fx.get("raw_xml_path") == _relative(root, fx_raw_path)
        and fx.get("parser_sha256") == _sha256(script_dir / "admit_openai_campaign.py")
    ):
        raise ValueError("local admission FX evidence mismatch")

    canary_health_before = report / "canary/evaluation/proxy-health-before.json"
    canary_health_after = report / "canary/evaluation/proxy-health-after.json"
    canary_proxy_log = report / "canary/evaluation/proxy.log"
    canary = _verify_canary(
        result_path=args.canary_result,
        usage_path=args.canary_usage,
        health_before_path=canary_health_before,
        health_after_path=canary_health_after,
        proxy_log_path=canary_proxy_log,
    )
    question_ids = _question_ids(args.question_id_file)

    input_paths: list[Path] = [
        precommit,
        Path(str(precommit) + ".SHA256SUMS"),
        args.r1_terminal,
        args.r1_terminal.with_name("ABORTED_AFTER_PRIMARY_AUDIT_SHA256SUMS"),
        args.r2_terminal,
        args.r2_disclosure,
        args.pricing_evidence,
        args.admission,
        fx_path,
        fx_raw_path,
        args.canary_result,
        args.canary_usage,
        canary_health_before,
        canary_health_after,
        canary_proxy_log,
        args.question_id_file,
        sealed_manifest,
        bound_manifest,
    ]
    arm_arguments = {
        "primary": (args.primary_raw_audit, args.primary_transport_audit),
        "replication": (args.replication_raw_audit, args.replication_transport_audit),
    }
    arms: dict[str, Any] = {}
    verdicts: dict[str, dict[str, dict[str, bool]]] = {}
    copy_documents: dict[str, Mapping[str, Any]] = {}
    for label, (raw_path, transport_path) in arm_arguments.items():
        raw = _load(raw_path)
        transport_document = _load(transport_path)
        transport = _verify_transport(transport_document, label=label)
        bindings = transport["bindings"]
        if bindings.get("raw_evaluation_audit_file_sha256") != _sha256(raw_path):
            raise ValueError(f"{label} raw audit hash is not transport-bound")
        official_root = report / label / "evaluation/official-harness"
        result_path = _result_path(official_root)
        scores, arm_verdicts = _verify_result(
            _load(result_path), raw, question_ids=question_ids, label=label
        )
        verdicts[label] = arm_verdicts
        usage_path = report / label / "evaluation/openai-usage.jsonl"
        evaluator_log_path = report / label / "evaluation/evaluate.log"
        proxy_log_path = report / label / "evaluation/proxy.log"
        copy_manifest_path = official_root / "frozen-copy-manifest.json"
        health_before_path = report / label / "evaluation/proxy-health-before.json"
        health_after_path = report / label / "evaluation/proxy-health-after.json"
        artifact_bindings = {
            "usage_log_sha256": usage_path,
            "evaluator_log_sha256": evaluator_log_path,
            "proxy_log_sha256": proxy_log_path,
            "frozen_copy_manifest_sha256": copy_manifest_path,
            "question_id_file_sha256": args.question_id_file,
        }
        for field, path in artifact_bindings.items():
            if bindings.get(field) != _sha256(path):
                raise ValueError(f"{label} {field} is not transport-bound")
        copy_documents[label] = _load(copy_manifest_path)
        before = _load(health_before_path)
        after = _load(health_after_path)
        _verify_health(
            before,
            label=f"{label}-before",
            max_cost=ARM_FUSE,
            reservation=ARM_RESERVATION,
            max_completion_tokens=4096,
            expected_usage=None,
        )
        expected_after = {
            "calls": SUCCESS_CALLS,
            "errors": transport["unknown_transients"],
            "malformed_responses": transport["known_transients"],
            "terminal_rejections": 0,
            "discarded_transients": transport["transients"],
            "unknown_cost_attempts": transport["unknown_transients"],
            "hidden_sdk_retry_rejections": 0,
            "transport_failed": False,
            "fatal_reason_code": None,
            "pending_logical_calls": 0,
            "active_logical_calls": 0,
            "cost_usd": format(transport["conservative_cost"], "f"),
            **transport["tokens"],
        }
        after_usage = _verify_health(
            after,
            label=f"{label}-after",
            max_cost=ARM_FUSE,
            reservation=ARM_RESERVATION,
            max_completion_tokens=4096,
            expected_usage=expected_after,
        )
        for field, value in transport["proxy_stop"].items():
            if field in after_usage and after_usage.get(field) != value:
                raise ValueError(f"{label} health/stop reconciliation mismatch")
        _verify_proxy_log(
            proxy_log_path,
            usage_path=usage_path,
            max_cost=ARM_FUSE,
            reservation=ARM_RESERVATION,
            max_completion_tokens=4096,
            after_usage=after_usage,
        )
        arms[label] = {
            "official_output_complete": True,
            "official_score_complete": True,
            "raw_evaluation_audit_sha256": _sha256(raw_path),
            "transport_audit_sha256": _sha256(transport_path),
            "official_result_path": _relative(root, result_path),
            "official_result_sha256": _sha256(result_path),
            "scores": scores,
            "route_usage": {
                "events": transport["events"],
                "successful_forwarded_official_openai_stop_calls": SUCCESS_CALLS,
                "discarded_transient_attempts": transport["transients"],
                "maximum_discarded_transient_attempts": MAX_TRANSIENTS,
                "known_cost_discarded_transients": transport["known_transients"],
                "unknown_cost_discarded_transients": transport["unknown_transients"],
                "terminal_rejections": 0,
                "hidden_sdk_retry_rejections": 0,
                "maximum_physical_attempts_observed": transport["maximum_attempts"],
                "retry_payload_identity_verified": True,
                "unique_safe_request_ids_verified": True,
                "exact_token_cost_formula_reconciled": True,
                "reasoning_tokens_billed_twice": False,
                **transport["tokens"],
                "known_success_cost_usd": format(transport["success_cost"], "f"),
                "transient_reservation_or_known_cost_usd": format(
                    transport["transient_cost"], "f"
                ),
                "conservative_ledger_cost_usd": format(
                    transport["conservative_cost"], "f"
                ),
                "usage_sha256": _sha256(usage_path),
            },
        }
        input_paths.extend(
            (
                raw_path,
                transport_path,
                result_path,
                usage_path,
                evaluator_log_path,
                proxy_log_path,
                copy_manifest_path,
                health_before_path,
                health_after_path,
            )
        )

    primary_copy = copy_documents["primary"]
    replication_copy = copy_documents["replication"]
    if not (
        primary_copy.get("schema_version") == "narratordb.paired-evaluation-copy.v1"
        and replication_copy.get("schema_version")
        == "narratordb.paired-evaluation-copy.v1"
        and primary_copy.get("expected_questions") == QUESTIONS
        and replication_copy.get("expected_questions") == QUESTIONS
        and primary_copy.get("prediction_file_count") == QUESTIONS
        and replication_copy.get("prediction_file_count") == QUESTIONS
        and primary_copy.get("file_count") == 84
        and replication_copy.get("file_count") == 84
        and primary_copy.get("frozen_directory")
        == replication_copy.get("frozen_directory")
        and primary_copy.get("files") == replication_copy.get("files")
    ):
        raise ValueError("the two arms do not bind identical frozen prediction bytes")

    pair_stability: dict[str, Any] = {}
    for cutoff in CUTOFFS:
        primary_correct = arms["primary"]["scores"][cutoff]["correct"]
        replication_correct = arms["replication"]["scores"][cutoff]["correct"]
        agreement = sum(
            verdicts["primary"][cutoff][question_id]
            == verdicts["replication"][cutoff][question_id]
            for question_id in question_ids
        )
        difference = abs(primary_correct - replication_correct)
        pair_stability[cutoff] = {
            "primary_accuracy_percent": _percentage(primary_correct, QUESTIONS),
            "replication_accuracy_percent": _percentage(
                replication_correct, QUESTIONS
            ),
            "absolute_score_difference_percentage_points": _percentage(
                difference, QUESTIONS
            ),
            "question_verdict_agreement": agreement,
            "question_verdict_total": QUESTIONS,
            "question_verdict_agreement_percent": _percentage(agreement, QUESTIONS),
            "both_arms_at_least_95_percent": (
                primary_correct >= TARGET_MINIMUM_CORRECT
                and replication_correct >= TARGET_MINIMUM_CORRECT
            ),
        }
    target_passed = pair_stability["top_50"]["both_arms_at_least_95_percent"]

    primary_cost = _decimal(
        arms["primary"]["route_usage"]["conservative_ledger_cost_usd"],
        label="primary cost",
    )
    replication_cost = _decimal(
        arms["replication"]["route_usage"]["conservative_ledger_cost_usd"],
        label="replication cost",
    )
    new_cost = canary["cost"] + primary_cost + replication_cost
    cumulative = PRIOR_TRACKED_MAXIMUM + new_cost
    if (
        new_cost > admission["allocation"] + TOLERANCE
        or cumulative > CAMPAIGN_CEILING + TOLERANCE
    ):
        raise ValueError("completed pair exceeds the admitted local budget")

    r1_metrics = r1["primary_score"]["metrics"]
    historical = {
        "classification": "score-exposed history; neither R1 nor R2 is a paired score",
        "r1": {
            "terminal_record_path": _relative(root, args.r1_terminal),
            "terminal_record_sha256": EXPECTED_R1_TERMINAL_SHA256,
            "status": r1["status"],
            "primary_scores": {cutoff: r1_metrics[cutoff] for cutoff in CUTOFFS},
            "replication_executed": False,
            "transport_publication_gate_passed": False,
            "failure_decision_used_score_values": False,
        },
        "r2": {
            "terminal_record_path": _relative(root, args.r2_terminal),
            "terminal_record_sha256": EXPECTED_R2_TERMINAL_SHA256,
            "disclosure_path": _relative(root, args.r2_disclosure),
            "disclosure_sha256": EXPECTED_R2_DISCLOSURE_SHA256,
            "status": r2["status"],
            "primary": disclosure["primary"],
            "replication": disclosure["replication"],
            "paired_score_complete": False,
            "provider_billing_reconciled": False,
        },
        "prior_cumulative_conservative_exposure_usd": str(PRIOR_TRACKED_MAXIMUM),
    }

    audit = {
        "schema_version": SCHEMA,
        "sealed_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "complete-target-passed" if target_passed else "complete-below-threshold",
        "benchmark": "LongMemEval-S dev42",
        "classification": CLASSIFICATION,
        "score_observed_before_r3_precommit": True,
        "score_driven_execution_or_publication_branching": False,
        "same_model_self_judge": True,
        "target_accuracy_percent": str(TARGET_PERCENT),
        "target_minimum_correct_per_arm_at_top_50": TARGET_MINIMUM_CORRECT,
        "target_passed": target_passed,
        "comparability_warning": (
            "The answerer and judge are the same GPT-5.4-mini snapshot on the "
            "official OpenAI endpoint, so self-preference risk remains."
        ),
        "score_exposed_precommit": {
            "path": _relative(root, precommit),
            "sha256": expected_precommit,
            "score_fields_present": True,
            "r3_score_fields_present": False,
        },
        "sealed_protocol": {
            "directory": _relative(root, script_dir),
            "sealed_files_manifest_sha256": _sha256(sealed_manifest),
            "bound_inputs_manifest_sha256": _sha256(bound_manifest),
        },
        "historical_r1_r2_terminal_disclosure": historical,
        "configuration": {
            "official_harness_commit": "4b61c5d31b9c668a12b4f5e78064248a02c82d2b",
            "official_endpoint": UPSTREAM,
            "endpoint_identity": ENDPOINT,
            "answerer_model": MODEL,
            "answerer_provider": PROVIDER,
            "answerer_reasoning_effort": "high",
            "answerer_temperature_omitted": True,
            "judge_model": MODEL,
            "judge_provider": PROVIDER,
            "judge_reasoning_effort": "high",
            "judge_temperature_omitted": True,
            "service_tier": "default",
            "store": False,
            "fallbacks_allowed": False,
            "top_k": 200,
            "cutoffs": [20, 50],
            "questions": QUESTIONS,
            "workers": 2,
            "rpm": 30,
            "seed": 42,
            "rejudge": True,
            "operator_selective_retries": False,
            "replication_unconditional_after_score_blind_transport_gate": True,
            "maximum_discarded_transients_per_arm": MAX_TRANSIENTS,
            "maximum_physical_attempts_per_logical_call": MAX_ATTEMPTS,
            "fatal_health_watchdog": True,
        },
        "official_model_and_pricing": {
            "model_snapshot": MODEL,
            "evidence_sha256": EXPECTED_PRICING_SHA256,
            "cost_formula": FORMULA,
            "reasoning_tokens_billed_twice": False,
        },
        "canary": {
            "attempts": 1,
            "passed": True,
            "role_calls": 2,
            "cost_usd": format(canary["cost"], "f"),
            **canary["tokens"],
            "result_sha256": _sha256(args.canary_result),
            "usage_sha256": _sha256(args.canary_usage),
            "fatal_or_unknown_events": 0,
        },
        "arms": arms,
        "pair_stability": pair_stability,
        "transport_policy": {
            "successful_role_calls_per_arm": SUCCESS_CALLS,
            "maximum_discarded_transients_per_arm": MAX_TRANSIENTS,
            "discarded_transients_never_forwarded_or_scored": True,
            "unknown_transport_cost_booked_at_full_reservation": True,
            "successful_completions_all_known_token_derived_cost": True,
            "operator_selective_retries": False,
            "full_arm_restart_after_terminal_failure": True,
            "terminal_rejections": 0,
            "hidden_sdk_retry_rejections": 0,
            "fatal_health_watchdog": True,
            "total_discarded_transients": sum(
                arms[label]["route_usage"]["discarded_transient_attempts"]
                for label in ("primary", "replication")
            ),
        },
        "budget": {
            "accounting_source": (
                "local official-response token ledger plus full reservations for "
                "any unforwarded unknown-cost transport transients"
            ),
            "provider_account_telemetry_performed": False,
            "provider_billing_reconciled": False,
            "balance_attestation_verification": "not_api_verified",
            "prior_r1_r2_cumulative_conservative_exposure_usd": str(
                PRIOR_TRACKED_MAXIMUM
            ),
            "canary_ledger_cost_usd": format(canary["cost"], "f"),
            "primary_conservative_ledger_cost_usd": format(primary_cost, "f"),
            "replication_conservative_ledger_cost_usd": format(
                replication_cost, "f"
            ),
            "new_r3_conservative_ledger_cost_usd": format(new_cost, "f"),
            "cumulative_conservative_campaign_exposure_usd": format(
                cumulative, "f"
            ),
            "tracked_campaign_ceiling_usd": str(CAMPAIGN_CEILING),
            "remaining_to_campaign_ceiling_usd": format(
                CAMPAIGN_CEILING - cumulative, "f"
            ),
            "cumulative_conservative_campaign_exposure_eur": format(
                cumulative / admission["rate"], ".8f"
            ),
            "ceiling_respected": True,
            "local_admission_sha256": _sha256(args.admission),
        },
        "fairness": {
            "same_frozen_prediction_bytes_for_both_arms": True,
            "both_full_42_question_arms_completed": True,
            "unconditional_replication_completed": True,
            "selective_question_reruns": False,
            "benchmark_answer_hardcoding": False,
            "score_driven_prompt_or_route_changes": False,
            "score_driven_execution_or_publication_branching": False,
            "official_endpoint_and_snapshot_exact": True,
            "headline_or_independent_judge_claim": False,
            "mem0_head_to_head_claim": False,
        },
    }
    audit_payload = (json.dumps(audit, indent=2, sort_keys=True) + "\n").encode()
    output = args.output.resolve()
    manifest_output = args.manifest_output.resolve()
    audit_sha = _write_new(output, audit_payload)

    manifest_paths = sorted(
        {path.resolve(strict=True) for path in input_paths},
        key=lambda path: _relative(root, path),
    )
    manifest_lines = [f"{audit_sha}  {_relative(root, output)}"]
    manifest_lines.extend(
        f"{_sha256(path)}  {_relative(root, path)}" for path in manifest_paths
    )
    manifest_payload = ("\n".join(manifest_lines) + "\n").encode()
    manifest_sha = _write_new(manifest_output, manifest_payload)
    print(
        json.dumps(
            {
                "complete": True,
                "target_passed": target_passed,
                "audit_sha256": audit_sha,
                "manifest_sha256": manifest_sha,
                "score_driven_execution_or_publication_branching": False,
                "provider_telemetry_performed": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
