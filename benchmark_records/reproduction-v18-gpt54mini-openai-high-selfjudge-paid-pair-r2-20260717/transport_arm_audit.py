#!/usr/bin/env python3
"""Audit one frozen V18 arm under the sealed r2 retry-transport policy.

The official harness and OpenRouter proxy are unchanged.  The proxy never
forwards an empty/malformed upstream HTTP-200 body: it records content-free
accounting metadata, returns HTTP 502 to the client, and lets the unchanged
client retry.  This wrapper therefore permits a small, precommitted number of
those discarded attempts while requiring exactly 168 independently successful
OpenAI GPT-5.4-mini ``stop`` completions for every accepted arm.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Sequence


SCHEMA = "narratordb.v18-gpt-selfjudge-transport-arm-audit.r2.v1"
COPY_SCHEMA = "narratordb.paired-evaluation-copy.v1"
EXPECTED_EVALUATION_AUDITOR_SHA256 = (
    "011708f614bee9cfc15209986bc68969f3c70191a06f540d3a86a2d7f74aeefc"
)
# Updated to the final sealed sibling proxy digest before protocol sealing.
EXPECTED_PROXY_SHA256 = (
    "db7b5c6f3c874f2b7a43aef625f0031eced5b4cd95f603d8d5e57d6319cec285"
)
EXPECTED_HARNESS_CLIENT_SHA256 = (
    "b0dc8f4172ed11f7f4161df47c77ca83dd5996b075494cc39bd6a4d0a1f93701"
)
EXPECTED_MODEL = "openai/gpt-5.4-mini"
EXPECTED_PROVIDER = "OpenAI"
EXPECTED_SUCCESSFUL_STOPS = 168
MAX_DISCARDED_TRANSIENTS = 4
REQUEST_RESERVATION_USD = Decimal("0.05")
ARM_PROCESS_FUSE_USD = Decimal("2.45")
EXPECTED_CUTOFFS = ("top_20", "top_50")
EXPECTED_QUESTIONS = 42
_COST_TOLERANCE = Decimal("0.000000001")
_INTEGER_FIELDS = (
    "status",
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "reasoning_tokens",
)
_COMPLETION_FIELDS = {
    "timestamp",
    "event",
    "status",
    "request_model",
    "response_model",
    "provider",
    "finish_reason",
    "response_complete",
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cost_usd",
    "unknown_cost",
    "request_payload_sha256",
    "logical_call_id",
    "attempt_number",
    "response_forwarded",
    "discarded_reason",
    "retryable",
}
_RETRYABLE_HTTP_STATUSES = {408, 409, 429, 500, 502, 503, 504}
_RETRYABLE_REASONS = {
    "empty_completion",
    "upstream_http_error",
    "upstream_timeout_or_network",
}
_ZERO_VALIDATION_FIELDS = (
    "missing_cutoffs",
    "empty_answers",
    "empty_judges",
    "invalid_scores",
    "inconsistent_verdicts",
    "missing_evaluated_ids",
    "missing_frozen_ids",
    "extra_evaluated_ids",
    "frozen_payload_mismatches",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


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


def _strict_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"symbolic link is forbidden: {path}")
    parsed = _strict_json(path.read_text(encoding="utf-8"), label=str(path))
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object: {path}")
    return parsed


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


def _load_events(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink():
        raise ValueError("usage ledger may not be a symbolic link")
    payload = path.read_bytes()
    if not payload or not payload.endswith(b"\n"):
        raise ValueError("usage ledger must be nonempty and newline terminated")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("usage ledger is not UTF-8") from error
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        parsed = _strict_json(line, label=f"usage line {line_number}")
        if not isinstance(parsed, dict):
            raise ValueError(f"usage line {line_number} is not an object")
        events.append(parsed)
    return events


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _exact_nonnegative_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _sha256_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def classify_usage_events(
    events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Classify r2 physical attempts and prove every transient was recovered."""

    failures: list[str] = []
    successful = 0
    discarded = 0
    terminal = 0
    unknown_cost_discards = 0
    known_cost_discards = 0
    total_cost = Decimal("0")
    known_success_cost = Decimal("0")
    discarded_booked_cost = Decimal("0")
    groups: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    payload_event_order: dict[str, list[str]] = {}
    discarded_attempt_counts: dict[str, int] = {}

    for index, event in enumerate(events, 1):
        prefix = f"event[{index}]"
        if set(event) != _COMPLETION_FIELDS:
            failures.append(f"{prefix}.closed_schema_mismatch")
        if not _valid_timestamp(event.get("timestamp")):
            failures.append(f"{prefix}.timestamp_invalid")
        for field in (*_INTEGER_FIELDS, "attempt_number"):
            if not _exact_nonnegative_int(event.get(field)):
                failures.append(f"{prefix}.{field}_invalid")
        attempt_number = event.get("attempt_number")
        if _exact_nonnegative_int(attempt_number) and not 1 <= attempt_number <= 5:
            failures.append(f"{prefix}.attempt_number_out_of_range")
        if event.get("request_model") != EXPECTED_MODEL:
            failures.append(f"{prefix}.request_model_mismatch")
        payload_sha = event.get("request_payload_sha256")
        logical_call_id = event.get("logical_call_id")
        if not _sha256_string(payload_sha):
            failures.append(f"{prefix}.request_payload_sha256_invalid")
        if not _sha256_string(logical_call_id):
            failures.append(f"{prefix}.logical_call_id_invalid")
        if _sha256_string(payload_sha) and _sha256_string(logical_call_id):
            groups.setdefault(str(logical_call_id), []).append((index, event))
            order = payload_event_order.setdefault(str(payload_sha), [])
            if not order or order[-1] != logical_call_id:
                if logical_call_id in order:
                    failures.append(f"{prefix}.same_payload_logical_chain_interleaved")
                order.append(str(logical_call_id))

        try:
            cost = _decimal(event.get("cost_usd"), label=f"{prefix}.cost_usd")
        except ValueError:
            failures.append(f"{prefix}.cost_usd_invalid")
            cost = Decimal("0")
        total_cost += cost
        event_kind = event.get("event")

        if event_kind == "completion":
            successful += 1
            known_success_cost += cost
            if event.get("status") != 200:
                failures.append(f"{prefix}.successful_status_not_http_200")
            if event.get("response_model") != EXPECTED_MODEL:
                failures.append(f"{prefix}.successful_response_model_mismatch")
            if event.get("provider") != EXPECTED_PROVIDER:
                failures.append(f"{prefix}.successful_provider_mismatch")
            if event.get("finish_reason") != "stop":
                failures.append(f"{prefix}.successful_finish_reason_not_stop")
            if event.get("response_complete") is not True:
                failures.append(f"{prefix}.successful_response_incomplete")
            if event.get("response_forwarded") is not True:
                failures.append(f"{prefix}.successful_response_not_forwarded")
            if event.get("discarded_reason") is not None:
                failures.append(f"{prefix}.successful_discard_reason_present")
            if event.get("retryable") is not False:
                failures.append(f"{prefix}.successful_retryable_not_false")
            if event.get("unknown_cost") is not False:
                failures.append(f"{prefix}.successful_cost_unknown")
            if not _exact_nonnegative_int(event.get("prompt_tokens")) or event.get(
                "prompt_tokens", 0
            ) <= 0:
                failures.append(f"{prefix}.successful_prompt_tokens_invalid")
            if not _exact_nonnegative_int(
                event.get("completion_tokens")
            ) or event.get("completion_tokens", 0) <= 0:
                failures.append(f"{prefix}.successful_completion_tokens_invalid")
            if cost <= 0:
                failures.append(f"{prefix}.successful_cost_not_positive")

        elif event_kind == "discarded_transient":
            discarded += 1
            if _exact_nonnegative_int(attempt_number):
                rendered_attempt = str(attempt_number)
                discarded_attempt_counts[rendered_attempt] = (
                    discarded_attempt_counts.get(rendered_attempt, 0) + 1
                )
            discarded_booked_cost += cost
            reason = event.get("discarded_reason")
            status = event.get("status")
            if event.get("response_forwarded") is not False:
                failures.append(f"{prefix}.discarded_response_forwarded")
            if event.get("response_complete") is not False:
                failures.append(f"{prefix}.discarded_response_complete")
            if event.get("retryable") is not True:
                failures.append(f"{prefix}.discarded_retryable_not_true")
            if reason not in _RETRYABLE_REASONS:
                failures.append(f"{prefix}.discarded_reason_not_allowlisted")
            elif reason == "empty_completion" and status != 200:
                failures.append(f"{prefix}.empty_completion_status_not_200")
            elif reason == "upstream_http_error" and status not in _RETRYABLE_HTTP_STATUSES:
                failures.append(f"{prefix}.http_error_status_not_retryable")
            elif (
                reason == "upstream_timeout_or_network"
                and status not in _RETRYABLE_HTTP_STATUSES
            ):
                failures.append(f"{prefix}.network_status_not_retryable")
            if event.get("finish_reason") != "unknown":
                failures.append(f"{prefix}.discarded_finish_reason_not_unknown")
            if event.get("response_model") not in {
                EXPECTED_MODEL,
                "unknown",
                "route_mismatch",
            }:
                failures.append(f"{prefix}.discarded_response_model_invalid")
            if event.get("provider") not in {
                EXPECTED_PROVIDER,
                "unknown",
                "route_mismatch",
            }:
                failures.append(f"{prefix}.discarded_provider_invalid")
            if event.get("unknown_cost") is True:
                unknown_cost_discards += 1
                if cost != REQUEST_RESERVATION_USD:
                    failures.append(
                        f"{prefix}.unknown_discard_not_charged_at_reservation"
                    )
            elif event.get("unknown_cost") is False:
                known_cost_discards += 1
                if cost < REQUEST_RESERVATION_USD:
                    failures.append(
                        f"{prefix}.known_discard_below_reservation_charge"
                    )
            else:
                failures.append(f"{prefix}.discarded_unknown_cost_invalid")

        else:
            terminal += 1
            failures.append(f"{prefix}.terminal_or_unknown_event_present")

    if successful != EXPECTED_SUCCESSFUL_STOPS:
        failures.append("successful_forwarded_stop_calls!=168")
    if discarded > MAX_DISCARDED_TRANSIENTS:
        failures.append("discarded_transients>4")
    if terminal:
        failures.append("terminal_rejections!=0")
    if len(events) != successful + discarded + terminal:
        failures.append("event_partition_incomplete")
    if total_cost > ARM_PROCESS_FUSE_USD + _COST_TOLERANCE:
        failures.append("ledger_cost_above_arm_fuse")

    max_attempts_observed = 0
    completed_groups = 0
    for logical_call_id, group in sorted(groups.items()):
        attempts = [event.get("attempt_number") for _, event in group]
        max_attempts_observed = max(max_attempts_observed, len(group))
        if attempts != list(range(1, len(group) + 1)):
            failures.append(f"logical_call[{logical_call_id}].attempts_not_contiguous")
        payloads = {event.get("request_payload_sha256") for _, event in group}
        if len(payloads) != 1:
            failures.append(f"logical_call[{logical_call_id}].payload_hash_changed")
            continue
        payload_sha = next(iter(payloads))
        payload_order = payload_event_order.get(str(payload_sha), [])
        try:
            ordinal = payload_order.index(logical_call_id) + 1
        except ValueError:
            failures.append(f"logical_call[{logical_call_id}].ordinal_missing")
        else:
            expected_id = hashlib.sha256(
                f"{payload_sha}:{ordinal}".encode("ascii")
            ).hexdigest()
            if expected_id != logical_call_id:
                failures.append(
                    f"logical_call[{logical_call_id}].logical_id_derivation_mismatch"
                )
        group_successes = [
            event for _, event in group if event.get("event") == "completion"
        ]
        if len(group_successes) != 1:
            failures.append(f"logical_call[{logical_call_id}].success_count!=1")
        elif group[-1][1].get("event") != "completion":
            failures.append(f"logical_call[{logical_call_id}].success_not_final")
        else:
            completed_groups += 1
        if any(
            event.get("event") != "discarded_transient"
            for _, event in group[:-1]
        ):
            failures.append(f"logical_call[{logical_call_id}].prefix_not_discarded")
        if len(group) > 5:
            failures.append(f"logical_call[{logical_call_id}].physical_attempts>5")

    if len(groups) != EXPECTED_SUCCESSFUL_STOPS:
        failures.append("logical_call_groups!=168")
    if completed_groups != EXPECTED_SUCCESSFUL_STOPS:
        failures.append("completed_logical_calls!=168")

    retry_identity_failures = any(
        "payload_hash_changed" in failure
        or "attempts_not_contiguous" in failure
        or "same_payload_logical_chain_interleaved" in failure
        or "logical_id_derivation_mismatch" in failure
        for failure in failures
    )
    summary = {
        "events": len(events),
        "successful_forwarded_openai_gpt54mini_stop_calls": successful,
        "discarded_transients": discarded,
        "discarded_attempt_counts": dict(sorted(discarded_attempt_counts.items())),
        "maximum_discarded_transients": MAX_DISCARDED_TRANSIENTS,
        "terminal_rejections": terminal,
        "completed_logical_calls": completed_groups,
        "maximum_physical_attempts_observed": max_attempts_observed,
        "maximum_physical_attempts_per_logical_call": 5,
        "retry_payload_identity_verified": not retry_identity_failures,
        "known_cost_discarded_transients": known_cost_discards,
        "unknown_cost_discarded_transients": unknown_cost_discards,
        "request_reservation_usd": str(REQUEST_RESERVATION_USD),
        "discarded_transient_booked_cost_usd": str(discarded_booked_cost),
        "known_success_cost_usd": str(known_success_cost),
        "conservative_ledger_cost_usd": str(total_cost),
        "arm_process_fuse_usd": str(ARM_PROCESS_FUSE_USD),
    }
    return summary, tuple(dict.fromkeys(failures))


def _require_hash(path: Path, expected: str, *, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing or a symbolic link")
    actual = _sha256(path)
    if actual != expected:
        raise ValueError(f"{label} checksum mismatch: {actual} != {expected}")
    return actual


def _load_evaluator(path: Path) -> ModuleType:
    _require_hash(
        path,
        EXPECTED_EVALUATION_AUDITOR_SHA256,
        label="evaluation auditor",
    )
    spec = importlib.util.spec_from_file_location("v18_r2_evaluation_audit", path)
    if spec is None or spec.loader is None:
        raise ValueError("could not load the sealed evaluation auditor")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _question_ids(path: Path) -> set[str]:
    parsed = _strict_json(path.read_text(encoding="utf-8"), label=str(path))
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) and item.strip() for item in parsed
    ):
        raise ValueError("question-ID file is not a string array")
    normalized = {item.strip() for item in parsed}
    if len(normalized) != EXPECTED_QUESTIONS or len(normalized) != len(parsed):
        raise ValueError("question-ID scope is not exactly 42 unique IDs")
    return normalized


def _snapshot(directory: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symbolic link in prediction tree: {path}")
        if path.is_file():
            entries.append(
                {
                    "path": path.relative_to(directory).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    return entries


def _verify_copy_manifest(
    path: Path, *, frozen_directory: Path, evaluated_directory: Path
) -> str:
    manifest = _strict_object(path)
    if manifest.get("schema_version") != COPY_SCHEMA:
        raise ValueError("frozen-copy manifest schema mismatch")
    if Path(str(manifest.get("frozen_directory") or "")).resolve() != frozen_directory:
        raise ValueError("frozen-copy manifest source mismatch")
    if Path(str(manifest.get("evaluated_directory") or "")).resolve() != evaluated_directory:
        raise ValueError("frozen-copy manifest destination mismatch")
    if manifest.get("expected_questions") != EXPECTED_QUESTIONS:
        raise ValueError("frozen-copy manifest question count mismatch")
    if manifest.get("prediction_file_count") != EXPECTED_QUESTIONS:
        raise ValueError("frozen-copy manifest prediction file count mismatch")
    if manifest.get("file_count") != 84:
        raise ValueError("frozen-copy manifest total file count mismatch")
    if manifest.get("files") != _snapshot(frozen_directory):
        raise ValueError("frozen source changed after r2 staging")
    return _sha256(path)


def _mapping_counts(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, int] = {}
    for key, count in value.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        result[str(key)] = count
    return result


def _audit_failures(
    report: Mapping[str, Any],
    *,
    usage_summary: Mapping[str, Any],
) -> tuple[str, ...]:
    failures: list[str] = []
    discarded = int(usage_summary["discarded_transients"])
    unknown_discards = int(usage_summary["unknown_cost_discarded_transients"])
    events = int(usage_summary["events"])
    cost = Decimal(str(usage_summary["conservative_ledger_cost_usd"]))

    if report.get("official_harness_score_complete") is not True:
        failures.append("official_harness_score_complete=false")
    for field in ("expected_questions", "evaluated_questions", "frozen_questions"):
        if report.get(field) != EXPECTED_QUESTIONS:
            failures.append(f"{field}!=42")
    if report.get("scoped_question_subset") is not True:
        failures.append("scoped_question_subset=false")
    if report.get("cutoffs") != list(EXPECTED_CUTOFFS):
        failures.append("cutoffs_mismatch")

    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        failures.append("metrics_invalid")
    else:
        for cutoff in EXPECTED_CUTOFFS:
            metric = metrics.get(cutoff)
            if not isinstance(metric, Mapping) or metric.get("total") != 42:
                failures.append(f"metrics.{cutoff}.total!=42")

    validation = report.get("validation")
    if not isinstance(validation, Mapping):
        failures.append("validation_invalid")
    else:
        for field in _ZERO_VALIDATION_FIELDS:
            value = validation.get(field)
            if not isinstance(value, list) or value:
                failures.append(f"validation.{field}_not_empty")

    usage = report.get("usage")
    if not isinstance(usage, Mapping):
        failures.append("raw_usage_invalid")
        return tuple(failures)
    exact_scalars = {
        "events": events,
        "completion_calls": EXPECTED_SUCCESSFUL_STOPS,
        "upstream_errors": 0,
        "malformed_http_200_responses": 0,
        "invalid_completion_identities": 0,
        "unknown_cost_attempts": unknown_discards,
    }
    for field, expected in exact_scalars.items():
        if usage.get(field) != expected:
            failures.append(f"raw_usage.{field}_mismatch")
    if usage.get("publication_ready") is not (unknown_discards == 0):
        failures.append("raw_usage.publication_ready_mismatch")
    if _mapping_counts(usage.get("completion_provider_counts")) != {
        EXPECTED_PROVIDER: EXPECTED_SUCCESSFUL_STOPS
    }:
        failures.append("raw_usage.completion_provider_counts_mismatch")
    if _mapping_counts(usage.get("request_model_counts")) != {
        EXPECTED_MODEL: EXPECTED_SUCCESSFUL_STOPS + discarded
    }:
        failures.append("raw_usage.request_model_counts_mismatch")
    if _mapping_counts(usage.get("finish_reason_counts")) != {
        "stop": EXPECTED_SUCCESSFUL_STOPS
    }:
        failures.append("raw_usage.finish_reason_counts_mismatch")
    try:
        raw_cost = _decimal(usage.get("cost_usd"), label="raw usage cost")
    except ValueError:
        failures.append("raw_usage.cost_usd_invalid")
    else:
        if abs(raw_cost - cost) > _COST_TOLERANCE:
            failures.append("raw_usage.cost_usd_mismatch")

    harness_log = report.get("harness_log")
    if not isinstance(harness_log, Mapping):
        failures.append("harness_log_invalid")
    else:
        if harness_log.get("attempt_five_failures") != 0:
            failures.append("harness_terminal_retry_failure")
        if harness_log.get("returned_none_responses") != 0:
            failures.append("harness_returned_none")
        failed_counts = _mapping_counts(harness_log.get("failed_attempt_counts"))
        timed_counts = _mapping_counts(harness_log.get("timed_out_attempt_counts"))
        if failed_counts is None or timed_counts is None:
            failures.append("harness_retry_counts_invalid")
        elif sum(failed_counts.values()) + sum(timed_counts.values()) != discarded:
            failures.append("harness_retry_count!=discarded_transients")
        else:
            observed_attempts = dict(failed_counts)
            for attempt, count in timed_counts.items():
                observed_attempts[attempt] = observed_attempts.get(attempt, 0) + count
            if observed_attempts != usage_summary.get("discarded_attempt_counts"):
                failures.append("harness_retry_attempt_histogram_mismatch")
    return tuple(failures)


def _load_proxy_log(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if path.is_symlink():
        raise ValueError("proxy log may not be a symbolic link")
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    if len(lines) != 2:
        raise ValueError("proxy log must contain exactly startup and stop records")
    startup = _strict_json(lines[0], label="proxy startup")
    stopped = _strict_json(lines[1], label="proxy stop")
    if not isinstance(startup, dict) or not isinstance(stopped, dict):
        raise ValueError("proxy log records must be JSON objects")
    return startup, stopped


def _proxy_log_failures(
    startup: Mapping[str, Any],
    stopped: Mapping[str, Any],
    *,
    usage_summary: Mapping[str, Any],
    usage_log: Path,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    failures: list[str] = []
    discarded = int(usage_summary["discarded_transients"])
    unknown_discards = int(usage_summary["unknown_cost_discarded_transients"])
    cost = Decimal(str(usage_summary["conservative_ledger_cost_usd"]))
    if startup.get("ok") is not True:
        failures.append("proxy_startup.ok=false")
    exact_configuration = {
        "provider_only": EXPECTED_PROVIDER,
        "provider_allow": [],
        "model_routes": {EXPECTED_MODEL: [EXPECTED_PROVIDER]},
        "model_output_token_parameters": {},
        "model_omit_temperature": [EXPECTED_MODEL],
        "model_reasoning_efforts": {EXPECTED_MODEL: "high"},
        "reasoning_effort": None,
        "public_benchmark": True,
    }
    for field, expected in exact_configuration.items():
        if startup.get(field) != expected:
            failures.append(f"proxy_startup.{field}_mismatch")
    if Path(str(startup.get("usage_log") or "")).resolve() != usage_log.resolve():
        failures.append("proxy_startup.usage_log_mismatch")
    for field, expected in {
        "max_cost_usd": Decimal("2.45"),
        "request_reservation_usd": Decimal("0.05"),
        "budget_safety_reserve_usd": Decimal("0.01"),
    }.items():
        if startup.get(field) != expected:
            failures.append(f"proxy_startup.{field}_mismatch")
    if startup.get("upstream_timeout_seconds") != Decimal("105.0"):
        failures.append("proxy_startup.timeout!=105")
    if startup.get("direct_upstream_networking") is not True:
        failures.append("proxy_startup.direct_upstream_networking=false")
    if startup.get("inbound_retry_count_policy") != "absent-or-zero-only":
        failures.append("proxy_startup.retry_count_policy_mismatch")
    if startup.get("local_caller_auth_required") is not True:
        failures.append("proxy_startup.local_caller_auth_required=false")
    if startup.get("max_request_bytes") != 20 * 1024 * 1024:
        failures.append("proxy_startup.max_request_bytes_mismatch")
    if startup.get("max_response_bytes") != 4 * 1024 * 1024:
        failures.append("proxy_startup.max_response_bytes_mismatch")
    if stopped.get("stopped") is not True:
        failures.append("proxy_stop.stopped=false")
    usage = stopped.get("usage")
    if not isinstance(usage, Mapping):
        failures.append("proxy_stop.usage_invalid")
        return {}, tuple(failures)

    expected = {
        "calls": EXPECTED_SUCCESSFUL_STOPS,
        "discarded_transients": discarded,
        "max_discarded_transients": MAX_DISCARDED_TRANSIENTS,
        "max_logical_attempts": 5,
        "transport_failed": False,
        "pending_logical_calls": 0,
        "active_logical_calls": 0,
        "hidden_sdk_retry_rejections": 0,
        "unknown_cost_attempts": unknown_discards,
        "max_cost_usd": Decimal("2.45"),
        "request_reservation_usd": Decimal("0.05"),
        "safety_reserve_usd": Decimal("0.01"),
        "scope": "process",
        "enforcement": "soft_fuse",
    }
    for field, value in expected.items():
        if usage.get(field) != value:
            failures.append(f"proxy_stop.{field}_mismatch")
    errors = usage.get("errors")
    malformed = usage.get("malformed_responses")
    if (
        not _exact_nonnegative_int(errors)
        or not _exact_nonnegative_int(malformed)
        or errors + malformed != discarded
    ):
        failures.append("proxy_stop.discard_partition_mismatch")
    try:
        stop_cost = _decimal(usage.get("cost_usd"), label="proxy stop cost")
        reserved = _decimal(
            usage.get("reserved_cost_usd"), label="proxy reserved cost"
        )
    except ValueError:
        failures.append("proxy_stop.cost_invalid")
    else:
        if abs(stop_cost - cost) > _COST_TOLERANCE:
            failures.append("proxy_stop.cost_mismatch")
        if reserved != 0:
            failures.append("proxy_stop.reserved_cost_not_zero")
    safe_summary = {
        field: usage.get(field)
        for field in (
            "calls",
            "errors",
            "malformed_responses",
            "discarded_transients",
            "unknown_cost_attempts",
            "cost_usd",
            "prompt_tokens",
            "cached_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "reserved_cost_usd",
            "transport_failed",
            "pending_logical_calls",
            "active_logical_calls",
            "hidden_sdk_retry_rejections",
        )
    }
    return safe_summary, tuple(failures)


def audit_transport_arm(
    *,
    evaluated_directory: Path,
    frozen_directory: Path,
    usage_log: Path,
    evaluator_log: Path,
    proxy_log: Path,
    question_id_file: Path,
    copy_manifest: Path,
    evaluation_auditor: Path,
    proxy_source: Path,
    harness_client_source: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the raw official audit and score-blind r2 transport audit."""

    evaluated_directory = evaluated_directory.resolve()
    frozen_directory = frozen_directory.resolve()
    usage_log = usage_log.resolve()
    evaluator_log = evaluator_log.resolve()
    proxy_log = proxy_log.resolve()
    question_id_file = question_id_file.resolve()
    copy_manifest = copy_manifest.resolve()
    evaluation_auditor = evaluation_auditor.resolve()
    proxy_source = proxy_source.resolve()
    harness_client_source = harness_client_source.resolve()

    evaluator = _load_evaluator(evaluation_auditor)
    proxy_sha = _require_hash(proxy_source, EXPECTED_PROXY_SHA256, label="proxy source")
    harness_sha = _require_hash(
        harness_client_source,
        EXPECTED_HARNESS_CLIENT_SHA256,
        label="harness LLM client",
    )
    manifest_sha = _verify_copy_manifest(
        copy_manifest,
        frozen_directory=frozen_directory,
        evaluated_directory=evaluated_directory,
    )
    events = _load_events(usage_log)
    usage_summary, usage_failures = classify_usage_events(events)
    proxy_startup, proxy_stopped = _load_proxy_log(proxy_log)
    proxy_stop_summary, proxy_failures = _proxy_log_failures(
        proxy_startup,
        proxy_stopped,
        usage_summary=usage_summary,
        usage_log=usage_log,
    )

    report = evaluator.audit_evaluation(
        evaluated_directory,
        frozen_directory=frozen_directory,
        usage_log=usage_log,
        evaluator_log=evaluator_log,
        expected_questions=EXPECTED_QUESTIONS,
        cutoffs=EXPECTED_CUTOFFS,
        question_ids=_question_ids(question_id_file),
    )
    audit_failures = _audit_failures(report, usage_summary=usage_summary)
    failures = tuple(
        dict.fromkeys((*usage_failures, *audit_failures, *proxy_failures))
    )
    gate = {
        "schema_version": SCHEMA,
        "authorized": not failures,
        "score_values_present": False,
        "score_driven_branching": False,
        "official_harness_score_complete": (
            report.get("official_harness_score_complete") is True
        ),
        "expected_questions": EXPECTED_QUESTIONS,
        "cutoffs": list(EXPECTED_CUTOFFS),
        "transport_policy": {
            "successful_calls_required": EXPECTED_SUCCESSFUL_STOPS,
            "successful_identity": {
                "request_model": EXPECTED_MODEL,
                "response_model": EXPECTED_MODEL,
                "provider": EXPECTED_PROVIDER,
                "http_status": 200,
                "finish_reason": "stop",
                "response_complete": True,
                "unknown_cost": False,
            },
            "discarded_transients_maximum": MAX_DISCARDED_TRANSIENTS,
            "physical_attempts_per_logical_call_maximum": 5,
            "discarded_transients_never_used": True,
            "never_used_basis": (
                "checksum-pinned proxy records every allowlisted empty-HTTP-200, "
                "retryable upstream HTTP, or network transient as response_forwarded=false; "
                "it emits only a generic local error with x-should-retry=false and never "
                "forwards the rejected upstream body to the harness"
            ),
            "discarded_charge_policy": (
                "unknown cost equals reservation; known cost equals at least reservation"
            ),
            "request_reservation_usd": str(REQUEST_RESERVATION_USD),
            "operator_selective_retries": False,
            "full_arm_restart_on_terminal_failure": True,
            "internal_sdk_retries_disabled": True,
            "sole_retry_owner": "frozen official harness outer max_retries=5",
        },
        "usage": usage_summary,
        "proxy_stop": proxy_stop_summary,
        "bindings": {
            "evaluation_auditor_sha256": EXPECTED_EVALUATION_AUDITOR_SHA256,
            "proxy_source_sha256": proxy_sha,
            "harness_client_sha256": harness_sha,
            "frozen_copy_manifest_sha256": manifest_sha,
            "question_id_file_sha256": _sha256(question_id_file),
            "usage_log_sha256": _sha256(usage_log),
            "evaluator_log_sha256": _sha256(evaluator_log),
            "proxy_log_sha256": _sha256(proxy_log),
            "raw_evaluation_audit_canonical_sha256": _canonical_sha256(report),
        },
        "failures": list(failures),
    }
    return report, gate


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _write_new_read_only(path: Path, value: Mapping[str, Any]) -> str:
    payload = (
        json.dumps(_json_ready(value), indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"output parent does not exist: {path.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o444)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.fchmod(descriptor, 0o444)
    finally:
        os.close(descriptor)
    return hashlib.sha256(payload).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluated-directory", type=Path, required=True)
    parser.add_argument("--frozen-directory", type=Path, required=True)
    parser.add_argument("--usage-log", type=Path, required=True)
    parser.add_argument("--evaluator-log", type=Path, required=True)
    parser.add_argument("--proxy-log", type=Path, required=True)
    parser.add_argument("--question-id-file", type=Path, required=True)
    parser.add_argument("--copy-manifest", type=Path, required=True)
    parser.add_argument("--evaluation-auditor", type=Path, required=True)
    parser.add_argument("--proxy-source", type=Path, required=True)
    parser.add_argument("--harness-client-source", type=Path, required=True)
    parser.add_argument("--raw-audit-output", type=Path, required=True)
    parser.add_argument("--transport-audit-output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        report, gate = audit_transport_arm(
            evaluated_directory=args.evaluated_directory,
            frozen_directory=args.frozen_directory,
            usage_log=args.usage_log,
            evaluator_log=args.evaluator_log,
            proxy_log=args.proxy_log,
            question_id_file=args.question_id_file,
            copy_manifest=args.copy_manifest,
            evaluation_auditor=args.evaluation_auditor,
            proxy_source=args.proxy_source,
            harness_client_source=args.harness_client_source,
        )
        raw_sha = _write_new_read_only(args.raw_audit_output.resolve(), report)
        gate["bindings"]["raw_evaluation_audit_file_sha256"] = raw_sha
        gate_sha = _write_new_read_only(
            args.transport_audit_output.resolve(), gate
        )
    except (FileNotFoundError, OSError, ValueError) as error:
        print(f"transport arm audit failed before preservation: {error}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "authorized": gate["authorized"],
                "transport_audit_sha256": gate_sha,
                "successful_stop_calls": gate["usage"][
                    "successful_forwarded_openai_gpt54mini_stop_calls"
                ],
                "discarded_transients": gate["usage"][
                    "discarded_transients"
                ],
            },
            sort_keys=True,
        )
    )
    return 0 if gate["authorized"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
