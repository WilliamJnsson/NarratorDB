#!/usr/bin/env python3
"""Fail closed on one evaluated benchmark arm before another arm may start."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evaluation_audit import (
    _load_question_ids,
    _normalize_cutoffs,
    audit_evaluation,
)


ZERO_VALIDATION_FIELDS = (
    "empty_answers",
    "empty_judges",
    "frozen_payload_mismatches",
    "missing_evaluated_ids",
    "missing_frozen_ids",
    "extra_evaluated_ids",
    "missing_cutoffs",
    "invalid_scores",
    "inconsistent_verdicts",
)


class ArmGateError(RuntimeError):
    """Raised when an arm audit cannot authorize the next benchmark arm."""

    def __init__(self, failures: Sequence[str], report: Mapping[str, Any]) -> None:
        self.failures = tuple(failures)
        self.audit_report = dict(report)
        self.report = score_blind_gate_report(report, failures=self.failures)
        super().__init__("arm evaluation gate failed: " + ", ".join(self.failures))


def _is_exact_int(value: Any, expected: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value == expected


def _mapping_keys(value: Any) -> set[str] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key) for key in value}


def arm_gate_failures(
    report: Mapping[str, Any],
    *,
    expected_questions: int,
    cutoffs: Sequence[str | int],
    require_usage: bool = True,
    allowed_request_models: set[str] | None = None,
    allowed_providers: set[str] | None = None,
    max_cost_usd: Decimal | None = None,
) -> tuple[str, ...]:
    """Return stable, content-free reasons an evaluation arm must stop.

    The accuracy numerator never authorizes another arm. Only completeness,
    fixed scope, identity, and cost-integrity evidence are gates.
    """

    if expected_questions <= 0:
        raise ValueError("expected_questions must be positive")
    if max_cost_usd is not None and (
        not max_cost_usd.is_finite() or max_cost_usd <= 0
    ):
        raise ValueError("max_cost_usd must be finite and positive")
    normalized_cutoffs = _normalize_cutoffs(tuple(cutoffs))
    failures: list[str] = []

    if report.get("complete") is not True:
        failures.append("complete=false")
    if report.get("official_harness_score_complete") is not True:
        failures.append("official_harness_score_complete=false")

    for field in ("expected_questions", "evaluated_questions", "frozen_questions"):
        if not _is_exact_int(report.get(field), expected_questions):
            failures.append(f"{field}!=expected_questions")

    if report.get("cutoffs") != list(normalized_cutoffs):
        failures.append("cutoffs_mismatch")

    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        failures.append("metrics_invalid")
    else:
        for cutoff in normalized_cutoffs:
            metric = metrics.get(cutoff)
            if not isinstance(metric, Mapping):
                failures.append(f"metrics.{cutoff}_missing")
            elif not _is_exact_int(metric.get("total"), expected_questions):
                failures.append(f"metrics.{cutoff}.total!=expected_questions")

    validation = report.get("validation")
    if not isinstance(validation, Mapping):
        failures.append("validation_invalid")
    else:
        for field in ZERO_VALIDATION_FIELDS:
            findings = validation.get(field)
            if not isinstance(findings, list):
                failures.append(f"validation.{field}_invalid")
            elif findings:
                failures.append(f"validation.{field}={len(findings)}")

    usage = report.get("usage")
    if usage is None:
        if require_usage:
            failures.append("usage_missing")
    elif not isinstance(usage, Mapping):
        failures.append("usage_invalid")
    else:
        if usage.get("publication_ready") is not True:
            failures.append("usage.publication_ready=false")
        for field in ("unknown_cost_attempts", "invalid_completion_identities"):
            if not _is_exact_int(usage.get(field), 0):
                failures.append(f"usage.{field}!=0")

        raw_cost = usage.get("cost_usd")
        try:
            if isinstance(raw_cost, bool):
                raise InvalidOperation
            cost = Decimal(str(raw_cost))
        except (InvalidOperation, TypeError, ValueError):
            failures.append("usage.cost_usd_invalid")
        else:
            if not cost.is_finite() or cost < 0:
                failures.append("usage.cost_usd_invalid")
            elif max_cost_usd is not None and cost > max_cost_usd:
                failures.append("usage.cost_usd_above_cap")

        if allowed_request_models is not None:
            observed = _mapping_keys(usage.get("request_model_counts"))
            if observed is None:
                failures.append("usage.request_model_counts_invalid")
            elif not observed <= allowed_request_models:
                failures.append("usage.request_models_outside_allowlist")
        if allowed_providers is not None:
            observed = _mapping_keys(usage.get("provider_counts"))
            if observed is None:
                failures.append("usage.provider_counts_invalid")
            elif not observed <= allowed_providers:
                failures.append("usage.providers_outside_allowlist")

    return tuple(failures)


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _strict_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _strict_json_value(value: Any) -> Any:
    """Replace non-finite floats before hashing or publishing JSON."""

    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _strict_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strict_json_value(item) for item in value]
    return value


def _sanitized_validation_findings(value: Any) -> list[Any] | None:
    if not isinstance(value, list):
        return None
    sanitized: list[Any] = []
    for finding in value:
        if isinstance(finding, Mapping):
            sanitized.append(
                {
                    key: finding[key]
                    for key in ("question_id", "cutoff")
                    if key in finding
                }
            )
        elif isinstance(finding, str):
            sanitized.append(finding)
        else:
            sanitized.append({"invalid_finding_type": type(finding).__name__})
    return sanitized


def score_blind_gate_report(
    report: Mapping[str, Any], *, failures: Sequence[str]
) -> dict[str, Any]:
    """Return the only report shape safe to persist before score release."""

    metrics = report.get("metrics")
    denominators: dict[str, Any] = {}
    if isinstance(metrics, Mapping):
        for cutoff in report.get("cutoffs") or ():
            metric = metrics.get(cutoff)
            denominators[str(cutoff)] = (
                metric.get("total") if isinstance(metric, Mapping) else None
            )

    validation = report.get("validation")
    sanitized_validation: dict[str, Any] = {}
    validation_counts: dict[str, int | None] = {}
    if isinstance(validation, Mapping):
        for field in ZERO_VALIDATION_FIELDS:
            findings = _sanitized_validation_findings(validation.get(field))
            sanitized_validation[field] = findings
            validation_counts[field] = len(findings) if findings is not None else None

    usage = report.get("usage")
    sanitized_usage: dict[str, Any] | None = None
    if isinstance(usage, Mapping):
        safe_usage_fields = (
            "events",
            "completion_calls",
            "upstream_errors",
            "malformed_http_200_responses",
            "invalid_completion_identities",
            "unknown_cost_attempts",
            "publication_ready",
            "provider_counts",
            "completion_provider_counts",
            "error_provider_counts",
            "request_model_counts",
            "error_status_counts",
            "finish_reason_counts",
            "cost_usd",
            "prompt_tokens",
            "cached_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "first_timestamp",
            "last_timestamp",
        )
        sanitized_usage = {field: usage.get(field) for field in safe_usage_fields}

    gate_report = {
        "schema_version": "narratordb.arm-evaluation-gate.v1",
        "authorized": not failures,
        "complete": report.get("complete") is True,
        "official_harness_score_complete": (
            report.get("official_harness_score_complete") is True
        ),
        "expected_questions": report.get("expected_questions"),
        "evaluated_questions": report.get("evaluated_questions"),
        "frozen_questions": report.get("frozen_questions"),
        "scoped_question_subset": report.get("scoped_question_subset"),
        "cutoffs": report.get("cutoffs"),
        "denominators": denominators,
        "validation": sanitized_validation,
        "validation_counts": validation_counts,
        "usage": sanitized_usage,
        "harness_log": report.get("harness_log"),
        "failures": list(failures),
        "internal_evaluation_audit_sha256": _canonical_sha256(report),
    }
    strict_report = _strict_json_value(gate_report)
    assert isinstance(strict_report, dict)
    return strict_report


def audit_and_gate_arm(
    evaluated_directory: Path,
    *,
    frozen_directory: Path,
    usage_log: Path,
    evaluator_log: Path,
    expected_questions: int,
    cutoffs: Sequence[str | int],
    question_ids: set[str],
    allowed_request_models: set[str] | None = None,
    allowed_providers: set[str] | None = None,
    max_cost_usd: Decimal | None = None,
) -> dict[str, Any]:
    """Audit one arm and raise before the caller can authorize a later arm."""

    report = audit_evaluation(
        evaluated_directory,
        frozen_directory=frozen_directory,
        usage_log=usage_log,
        evaluator_log=evaluator_log,
        expected_questions=expected_questions,
        cutoffs=tuple(cutoffs),
        question_ids=question_ids,
    )
    failures = arm_gate_failures(
        report,
        expected_questions=expected_questions,
        cutoffs=cutoffs,
        allowed_request_models=allowed_request_models,
        allowed_providers=allowed_providers,
        max_cost_usd=max_cost_usd,
    )
    if failures:
        raise ArmGateError(failures, report)
    return report


def _write_new_read_only(path: Path, payload: bytes) -> None:
    path = path.expanduser().resolve()
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


def _positive_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("expected a decimal USD amount") from error
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError("USD cap must be finite and positive")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluated-directory", type=Path, required=True)
    parser.add_argument("--frozen-directory", type=Path, required=True)
    parser.add_argument("--usage-log", type=Path, required=True)
    parser.add_argument("--evaluator-log", type=Path, required=True)
    parser.add_argument("--expected-questions", type=int, required=True)
    parser.add_argument("--cutoffs", required=True)
    parser.add_argument("--question-id-file", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allowed-request-model", action="append", default=[])
    parser.add_argument("--allowed-provider", action="append", default=[])
    parser.add_argument("--max-cost-usd", type=_positive_decimal)
    args = parser.parse_args(argv)

    cutoffs = tuple(value.strip() for value in args.cutoffs.split(",") if value.strip())
    try:
        report = audit_and_gate_arm(
            args.evaluated_directory,
            frozen_directory=args.frozen_directory,
            usage_log=args.usage_log,
            evaluator_log=args.evaluator_log,
            expected_questions=args.expected_questions,
            cutoffs=cutoffs,
            question_ids=_load_question_ids(args.question_id_file),
            allowed_request_models=(
                set(args.allowed_request_model) if args.allowed_request_model else None
            ),
            allowed_providers=(set(args.allowed_provider) if args.allowed_provider else None),
            max_cost_usd=args.max_cost_usd,
        )
        failures: tuple[str, ...] = ()
    except ArmGateError as error:
        gate_report = error.report
        failures = error.failures
    except (FileNotFoundError, ValueError) as error:
        parser.error(str(error))

    else:
        gate_report = score_blind_gate_report(report, failures=failures)

    rendered = (
        json.dumps(gate_report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    if args.output:
        try:
            _write_new_read_only(args.output, rendered)
        except OSError as error:
            print(f"cannot preserve arm audit: {error}", file=sys.stderr)
            return 2
    sys.stdout.write(rendered.decode("utf-8"))
    if failures:
        print("arm evaluation gate failed: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
