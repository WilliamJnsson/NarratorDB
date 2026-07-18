#!/usr/bin/env python3
"""Seal a complete r2 pair after both arms, without score-driven execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "narratordb.v18-gpt-selfjudge-paired-score.r2.v1"
TRANSPORT_SCHEMA = "narratordb.v18-gpt-selfjudge-transport-arm-audit.r2.v1"
MODEL = "openai/gpt-5.4-mini"
PROVIDER = "OpenAI"
CUTOFFS = ("top_20", "top_50")
QUESTIONS = 42
SUCCESS_CALLS = 168
MAX_TRANSIENTS = 4
TARGET_PERCENT = Decimal("95")
TARGET_MINIMUM_CORRECT = 40
PRIOR_CUMULATIVE = Decimal("1.914605682")
CAMPAIGN_CEILING = Decimal("10.00")
EXPECTED_R1_TERMINAL_SHA256 = (
    "4bdfe140a4f232b79a1e2b6121fa4a496b01ecd0d924a61a7c4e2468b0481eba"
)
TOLERANCE = Decimal("0.000000001")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"input is missing or a symbolic link: {path}")
    parsed = json.loads(
        path.read_text(encoding="utf-8"),
        parse_float=Decimal,
        parse_constant=_reject_constant,
        object_pairs_hook=_unique_object,
    )
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object: {path}")
    return parsed


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"ledger is missing or a symbolic link: {path}")
    payload = path.read_bytes()
    if not payload or not payload.endswith(b"\n"):
        raise ValueError(f"ledger must be nonempty and newline terminated: {path}")
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(payload.decode("utf-8").splitlines(), 1):
        parsed = json.loads(
            line,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
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
    return path.resolve().relative_to(root).as_posix()


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


def _verify_telemetry(document: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    if document.get("schema_version") != "narratordb.provider-key-telemetry.v2":
        raise ValueError(f"{label} telemetry schema mismatch")
    if not (
        document.get("credential_recorded") is False
        and document.get("key_label_recorded") is False
        and document.get("account_identifier_recorded") is False
        and document.get("model_content_recorded") is False
    ):
        raise ValueError(f"{label} telemetry is not content-free")
    limit = _decimal(document.get("provider_limit_usd"), label=f"{label} limit")
    usage = _decimal(document.get("provider_usage_usd"), label=f"{label} usage")
    remaining = _decimal(
        document.get("provider_remaining_usd"), label=f"{label} remaining"
    )
    if abs(limit - usage - remaining) > TOLERANCE:
        raise ValueError(f"{label} telemetry arithmetic mismatch")
    return {
        "observed_at_utc": _timestamp(
            document.get("observed_at_utc"), label=f"{label} timestamp"
        ),
        "limit": limit,
        "usage": usage,
        "remaining": remaining,
    }


def _verify_transport(document: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    failures = document.get("failures")
    policy = document.get("transport_policy")
    usage = document.get("usage")
    proxy_stop = document.get("proxy_stop")
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
        and isinstance(usage, Mapping)
        and isinstance(proxy_stop, Mapping)
        and isinstance(bindings, Mapping)
    ):
        raise ValueError(f"{label} transport audit is not authorized and complete")
    transients = usage.get("discarded_transients")
    successful = usage.get("successful_forwarded_openai_gpt54mini_stop_calls")
    policy_max = policy.get("discarded_transients_maximum")
    if (
        isinstance(transients, bool)
        or not isinstance(transients, int)
        or not 0 <= transients <= MAX_TRANSIENTS
        or successful != SUCCESS_CALLS
        or policy_max != MAX_TRANSIENTS
        or policy.get("discarded_transients_never_used") is not True
        or policy.get("operator_selective_retries") is not False
        or policy.get("full_arm_restart_on_terminal_failure") is not True
    ):
        raise ValueError(f"{label} transport retry policy mismatch")
    known_transients = usage.get("known_cost_discarded_transients")
    unknown_transients = usage.get("unknown_cost_discarded_transients")
    maximum_attempts = usage.get("maximum_physical_attempts_observed")
    if (
        isinstance(known_transients, bool)
        or not isinstance(known_transients, int)
        or isinstance(unknown_transients, bool)
        or not isinstance(unknown_transients, int)
        or known_transients + unknown_transients != transients
        or usage.get("terminal_rejections") != 0
        or usage.get("completed_logical_calls") != SUCCESS_CALLS
        or isinstance(maximum_attempts, bool)
        or not isinstance(maximum_attempts, int)
        or not 1 <= maximum_attempts <= 5
        or usage.get("retry_payload_identity_verified") is not True
        or proxy_stop.get("hidden_sdk_retry_rejections") != 0
        or proxy_stop.get("transport_failed") is not False
        or proxy_stop.get("pending_logical_calls") != 0
        or proxy_stop.get("active_logical_calls") != 0
    ):
        raise ValueError(f"{label} transport chain accounting mismatch")
    return {
        "transients": transients,
        "known_cost_transients": known_transients,
        "unknown_cost_transients": unknown_transients,
        "terminal_rejections": usage.get("terminal_rejections"),
        "completed_logical_calls": usage.get("completed_logical_calls"),
        "maximum_physical_attempts_observed": maximum_attempts,
        "retry_payload_identity_verified": usage.get(
            "retry_payload_identity_verified"
        ),
        "known_success_cost": _decimal(
            usage.get("known_success_cost_usd"), label=f"{label} success cost"
        ),
        "transient_reservation_cost": _decimal(
            usage.get("discarded_transient_booked_cost_usd"),
            label=f"{label} transient reservation cost",
        ),
        "conservative_cost": _decimal(
            usage.get("conservative_ledger_cost_usd"),
            label=f"{label} conservative cost",
        ),
        "events": usage.get("events"),
        "bindings": dict(bindings),
        "proxy_stop": dict(proxy_stop),
    }


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
        and isinstance(metrics, Mapping)
        and isinstance(evaluations, list)
        and isinstance(raw_metrics, Mapping)
        and raw.get("official_harness_score_complete") is True
        and raw.get("expected_questions") == QUESTIONS
        and raw.get("evaluated_questions") == QUESTIONS
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
            if passed is not (score == 1):
                raise ValueError(f"{label} judgment/score mismatch")
            if question_id in verdicts[cutoff]:
                raise ValueError(f"{label} duplicate question ID")
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


def _verify_precommit(document: Mapping[str, Any]) -> None:
    historical = document.get("r1_terminal_score_exposure")
    if not (
        document.get("schema_version") == 1
        and document.get("status") == "score-exposed-statically-sealed-awaiting-paid-preflight"
        and document.get("score_fields_present") is True
        and document.get("score_observed_before_r2_precommit") is True
        and document.get("same_model_self_judge") is True
        and isinstance(historical, Mapping)
        and historical.get("top_20")
        == {"correct": 40, "total": 42, "accuracy_percent": "95.23809523809523"}
        and historical.get("top_50")
        == {"correct": 41, "total": 42, "accuracy_percent": "97.61904761904762"}
        and historical.get("replication_executed") is False
        and historical.get("transport_publication_gate_passed") is False
    ):
        raise ValueError("r2 score-exposed precommit mismatch")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--report-root", type=Path, required=True)
    parser.add_argument("--precommit", type=Path, required=True)
    parser.add_argument("--expected-precommit-sha256", required=True)
    parser.add_argument("--r1-terminal", type=Path, required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--canary-result", type=Path, required=True)
    parser.add_argument("--canary-usage", type=Path, required=True)
    parser.add_argument("--telemetry-pre", type=Path, required=True)
    parser.add_argument("--telemetry-between", type=Path, required=True)
    parser.add_argument("--telemetry-post", type=Path, required=True)
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
    if (
        len(expected_precommit) != 64
        or any(character not in "0123456789abcdef" for character in expected_precommit)
        or _sha256(precommit) != expected_precommit
    ):
        raise ValueError("published precommit checksum mismatch")
    precommit_document = _load(precommit)
    _verify_precommit(precommit_document)
    script_dir = Path(__file__).resolve(strict=True).parent
    sealed_manifest = script_dir / "SEALED_FILES_SHA256SUMS"
    bound_manifest = script_dir / "BOUND_INPUTS_SHA256SUMS"
    protocol = precommit_document.get("sealed_protocol")
    if not (
        isinstance(protocol, Mapping)
        and protocol.get("sealed_files_manifest_sha256")
        == _sha256(sealed_manifest)
        and protocol.get("bound_inputs_manifest_sha256")
        == _sha256(bound_manifest)
    ):
        raise ValueError("precommit does not bind the active sealed protocol")

    r1 = _load(args.r1_terminal)
    if (
        _sha256(args.r1_terminal) != EXPECTED_R1_TERMINAL_SHA256
        or r1.get("status") != "ABORTED_AFTER_PRIMARY_AUDIT"
    ):
        raise ValueError("historical r1 terminal record mismatch")
    admission = _load(args.admission)
    admission_checks = admission.get("checks")
    if not (
        admission.get("schema_version")
        == "narratordb.v18-gpt-selfjudge-campaign-admission.r2.v1"
        and admission.get("admitted") is True
        and isinstance(admission_checks, Mapping)
        and admission_checks
        and all(value is True for value in admission_checks.values())
        and _decimal(
            admission.get("prior_r1_cumulative_conservative_exposure_usd"),
            label="admission prior exposure",
        )
        == PRIOR_CUMULATIVE
    ):
        raise ValueError("r2 dynamic admission mismatch")

    telemetry_documents = {
        "pre": _load(args.telemetry_pre),
        "between": _load(args.telemetry_between),
        "post": _load(args.telemetry_post),
    }
    telemetry = {
        label: _verify_telemetry(document, label=label)
        for label, document in telemetry_documents.items()
    }
    if not (
        telemetry["pre"]["observed_at_utc"]
        <= telemetry["between"]["observed_at_utc"]
        <= telemetry["post"]["observed_at_utc"]
        and telemetry["pre"]["usage"] - TOLERANCE
        <= telemetry["between"]["usage"]
        <= telemetry["post"]["usage"] + TOLERANCE
        and telemetry["pre"]["limit"]
        == telemetry["between"]["limit"]
        == telemetry["post"]["limit"]
    ):
        raise ValueError("provider telemetry sequence is inconsistent")

    canary_result = _load(args.canary_result)
    canary_calls = canary_result.get("calls")
    canary_events = _load_jsonl(args.canary_usage)
    if not (
        canary_result.get("schema_version") == "narratordb.route-canary.v1"
        and canary_result.get("complete") is True
        and canary_result.get("same_model_self_judge") is True
        and canary_result.get("prompt_or_completion_content_retained") is False
        and isinstance(canary_calls, list)
        and [call.get("label") for call in canary_calls] == ["answerer", "judge"]
        and len(canary_events) == 2
    ):
        raise ValueError("strict route canary is incomplete")
    canary_cost = sum(
        (_decimal(event.get("cost_usd"), label="canary event cost") for event in canary_events),
        Decimal("0"),
    )

    question_ids_document = json.loads(args.question_id_file.read_text(encoding="utf-8"))
    if not isinstance(question_ids_document, list):
        raise ValueError("question-ID file is not a list")
    question_ids = set(question_ids_document)
    if len(question_ids) != QUESTIONS or len(question_ids_document) != QUESTIONS:
        raise ValueError("question-ID scope is not exactly 42 unique IDs")

    arm_arguments = {
        "primary": (args.primary_raw_audit, args.primary_transport_audit),
        "replication": (args.replication_raw_audit, args.replication_transport_audit),
    }
    arms: dict[str, Any] = {}
    verdicts: dict[str, dict[str, dict[str, bool]]] = {}
    input_paths: list[Path] = [
        precommit,
        args.r1_terminal,
        args.admission,
        args.canary_result,
        args.canary_usage,
        args.telemetry_pre,
        args.telemetry_between,
        args.telemetry_post,
        args.question_id_file,
        sealed_manifest,
        bound_manifest,
        Path(str(precommit) + ".SHA256SUMS"),
        report / "STAGING_AUDIT.json",
        args.r1_terminal.with_name("ABORTED_AFTER_PRIMARY_AUDIT_SHA256SUMS"),
        report / "canary/evaluation/proxy.log",
        report / "canary/evaluation/proxy-health-before.json",
        report / "canary/evaluation/proxy-health-after.json",
    ]
    for label, (raw_path, transport_path) in arm_arguments.items():
        raw = _load(raw_path)
        transport_document = _load(transport_path)
        transport = _verify_transport(transport_document, label=label)
        if transport["bindings"].get("raw_evaluation_audit_file_sha256") != _sha256(
            raw_path
        ):
            raise ValueError(f"{label} raw audit hash is not transport-bound")
        official_root = report / label / "evaluation/official-harness"
        result_path = _result_path(official_root)
        scores, arm_verdicts = _verify_result(
            _load(result_path), raw, question_ids=question_ids, label=label
        )
        verdicts[label] = arm_verdicts
        usage_path = report / label / "evaluation/openrouter-usage.jsonl"
        if transport["bindings"].get("usage_log_sha256") != _sha256(usage_path):
            raise ValueError(f"{label} usage ledger hash is not transport-bound")
        evaluator_log_path = report / label / "evaluation/evaluate.log"
        proxy_log_path = report / label / "evaluation/proxy.log"
        copy_manifest_path = official_root / "frozen-copy-manifest.json"
        if (
            transport["bindings"].get("evaluator_log_sha256")
            != _sha256(evaluator_log_path)
            or transport["bindings"].get("proxy_log_sha256")
            != _sha256(proxy_log_path)
            or transport["bindings"].get("frozen_copy_manifest_sha256")
            != _sha256(copy_manifest_path)
        ):
            raise ValueError(f"{label} execution evidence is not transport-bound")
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
                "successful_forwarded_openai_gpt54mini_stop_calls": SUCCESS_CALLS,
                "discarded_transient_attempts": transport["transients"],
                "maximum_discarded_transient_attempts": MAX_TRANSIENTS,
                "known_cost_discarded_transients": transport[
                    "known_cost_transients"
                ],
                "unknown_cost_discarded_transients": transport[
                    "unknown_cost_transients"
                ],
                "terminal_rejections": transport["terminal_rejections"],
                "completed_logical_calls": transport["completed_logical_calls"],
                "maximum_physical_attempts_observed": transport[
                    "maximum_physical_attempts_observed"
                ],
                "retry_payload_identity_verified": transport[
                    "retry_payload_identity_verified"
                ],
                "hidden_sdk_retry_rejections": transport["proxy_stop"].get(
                    "hidden_sdk_retry_rejections"
                ),
                "known_success_cost_usd": str(transport["known_success_cost"]),
                "transient_reservation_cost_usd": str(
                    transport["transient_reservation_cost"]
                ),
                "conservative_ledger_cost_usd": str(
                    transport["conservative_cost"]
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
                report / label / "evaluation/proxy-health-before.json",
                report / label / "evaluation/proxy-health-after.json",
            )
        )

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
    new_cost = canary_cost + primary_cost + replication_cost
    cumulative = PRIOR_CUMULATIVE + new_cost
    if cumulative > CAMPAIGN_CEILING + TOLERANCE:
        raise ValueError("completed pair exceeds the tracked campaign ceiling")
    provider_delta = telemetry["post"]["usage"] - telemetry["pre"]["usage"]
    if provider_delta < -TOLERANCE:
        raise ValueError("provider usage moved backwards")
    rate = _decimal(admission.get("usd_per_eur"), label="USD per EUR")

    historical_metrics = r1.get("primary_score", {}).get("metrics", {})
    historical_transport = r1.get("primary_transport", {})
    historical_replication = r1.get("execution_state", {}).get("replication", {})
    historical_r1 = {
        "terminal_record_path": _relative(root, args.r1_terminal),
        "terminal_record_sha256": _sha256(args.r1_terminal),
        "score_observed_before_r2_precommit": True,
        "rows": [
            {
                "arm": "primary",
                "cutoff": cutoff,
                "correct": historical_metrics[cutoff]["correct"],
                "total": historical_metrics[cutoff]["total"],
                "accuracy_percent": historical_metrics[cutoff]["accuracy_percent"],
                "score_complete": True,
                "transport_publication_gate_passed": False,
            }
            for cutoff in CUTOFFS
        ]
        + [
            {
                "arm": "replication",
                "cutoff": cutoff,
                "correct": None,
                "total": None,
                "accuracy_percent": None,
                "score_complete": False,
                "transport_publication_gate_passed": False,
            }
            for cutoff in CUTOFFS
        ],
        "primary_invalid_completion_identities": historical_transport.get(
            "invalid_completion_identities"
        ),
        "primary_unknown_cost_attempts": historical_transport.get(
            "unknown_cost_attempts"
        ),
        "replication_executed": historical_replication.get("execution_started"),
        "r1_cumulative_conservative_exposure_usd": str(PRIOR_CUMULATIVE),
    }

    audit = {
        "schema_version": SCHEMA,
        "sealed_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "complete-target-passed" if target_passed else "complete-below-threshold",
        "benchmark": "LongMemEval-S dev42",
        "classification": (
            "post-hoc consumed development set; GPT-self-judged diagnostic only; "
            "not an untouched holdout, headline benchmark, independent-judge score, "
            "or Mem0 head-to-head"
        ),
        "score_observed_before_r2_precommit": True,
        "score_driven_execution_or_publication_branching": False,
        "same_model_self_judge": True,
        "target_accuracy_percent": str(TARGET_PERCENT),
        "target_minimum_correct_per_arm_at_top_50": TARGET_MINIMUM_CORRECT,
        "target_passed": target_passed,
        "comparability_warning": (
            "The answerer and judge are the same GPT-5.4-mini model and OpenAI "
            "provider, so self-preference risk remains."
        ),
        "score_exposed_precommit": {
            "path": _relative(root, precommit),
            "sha256": expected_precommit,
            "score_fields_present": True,
        },
        "sealed_protocol": {
            "directory": _relative(root, script_dir),
            "sealed_files_manifest_sha256": _sha256(sealed_manifest),
            "bound_inputs_manifest_sha256": _sha256(bound_manifest),
        },
        "historical_r1_terminal": historical_r1,
        "configuration": {
            "official_harness_commit": "4b61c5d31b9c668a12b4f5e78064248a02c82d2b",
            "answerer_model": MODEL,
            "answerer_provider": PROVIDER,
            "answerer_reasoning_effort": "high",
            "answerer_temperature_omitted": True,
            "judge_model": MODEL,
            "judge_provider": PROVIDER,
            "judge_reasoning_effort": "high",
            "judge_temperature_omitted": True,
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
        },
        "canary": {
            "attempts": 1,
            "passed": True,
            "role_calls": 2,
            "cost_usd": str(canary_cost),
            "result_sha256": _sha256(args.canary_result),
            "usage_sha256": _sha256(args.canary_usage),
        },
        "arms": arms,
        "pair_stability": pair_stability,
        "transport_policy": {
            "successful_role_calls_per_arm": SUCCESS_CALLS,
            "maximum_discarded_transients_per_arm": MAX_TRANSIENTS,
            "discarded_transients_never_enter_outputs": True,
            "operator_selective_retries": False,
            "full_arm_restart_after_terminal_failure": True,
            "total_discarded_transients": sum(
                arms[label]["route_usage"]["discarded_transient_attempts"]
                for label in ("primary", "replication")
            ),
        },
        "budget": {
            "prior_r1_cumulative_conservative_exposure_usd": str(PRIOR_CUMULATIVE),
            "canary_ledger_cost_usd": str(canary_cost),
            "primary_conservative_ledger_cost_usd": str(primary_cost),
            "replication_conservative_ledger_cost_usd": str(replication_cost),
            "new_r2_conservative_ledger_cost_usd": str(new_cost),
            "cumulative_conservative_campaign_exposure_usd": str(cumulative),
            "tracked_campaign_ceiling_usd": str(CAMPAIGN_CEILING),
            "remaining_to_campaign_ceiling_usd": str(CAMPAIGN_CEILING - cumulative),
            "cumulative_conservative_campaign_exposure_eur": format(
                cumulative / rate, ".8f"
            ),
            "provider_reported_r2_delta_usd": str(provider_delta),
            "provider_delta_may_lag_ledger": True,
            "provider_remaining_after_usd": str(telemetry["post"]["remaining"]),
            "ceiling_respected": True,
        },
        "telemetry_bindings": {
            "pre_sha256": _sha256(args.telemetry_pre),
            "between_sha256": _sha256(args.telemetry_between),
            "post_sha256": _sha256(args.telemetry_post),
            "dynamic_admission_sha256": _sha256(args.admission),
        },
        "fairness": {
            "same_frozen_prediction_bytes_for_both_arms": True,
            "both_full_42_question_arms_completed": True,
            "unconditional_replication_completed": True,
            "selective_question_reruns": False,
            "benchmark_answer_hardcoding": False,
            "score_driven_prompt_or_route_changes": False,
            "score_driven_execution_or_publication_branching": False,
            "headline_or_independent_judge_claim": False,
        },
    }
    audit_payload = (json.dumps(audit, indent=2, sort_keys=True) + "\n").encode()
    audit_sha = _write_new(args.output, audit_payload)

    manifest_paths = sorted(
        {path.resolve() for path in input_paths}, key=lambda path: _relative(root, path)
    )
    manifest_lines = [f"{audit_sha}  {_relative(root, args.output)}"]
    manifest_lines.extend(
        f"{_sha256(path)}  {_relative(root, path)}" for path in manifest_paths
    )
    manifest_payload = ("\n".join(manifest_lines) + "\n").encode()
    manifest_sha = _write_new(args.manifest_output, manifest_payload)
    print(
        json.dumps(
            {
                "complete": True,
                "target_passed": target_passed,
                "audit_sha256": audit_sha,
                "manifest_sha256": manifest_sha,
                "score_driven_execution_or_publication_branching": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
