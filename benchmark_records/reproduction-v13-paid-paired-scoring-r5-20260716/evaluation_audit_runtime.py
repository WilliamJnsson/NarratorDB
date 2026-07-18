#!/usr/bin/env python3
"""Audit a LongMemEval evaluate-only run against its frozen predictions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DEFAULT_CUTOFFS = ("top_10", "top_20", "top_50", "top_200")
_IDENTITY_SENTINELS = {"unknown", "route_mismatch"}


def _normalize_cutoff(value: str | int) -> str:
    rendered = str(value).strip().lower().replace("-", "_")
    if rendered.isdigit():
        rendered = f"top_{rendered}"
    match = re.fullmatch(r"top_([1-9]\d*)", rendered)
    if not match:
        raise ValueError(f"invalid cutoff {value!r}; expected an integer or top_N")
    return f"top_{int(match.group(1))}"


def _normalize_cutoffs(values: tuple[str | int, ...]) -> tuple[str, ...]:
    normalized = tuple(_normalize_cutoff(value) for value in values)
    if not normalized:
        raise ValueError("at least one cutoff is required")
    if len(set(normalized)) != len(normalized):
        raise ValueError("cutoffs contain duplicates")
    return tuple(sorted(normalized, key=lambda value: int(value.removeprefix("top_"))))


def _prediction_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Strip fields added by evaluate-only from either side of a freeze audit."""

    payload = dict(data)
    payload.pop("cutoff_results", None)
    return payload


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return data


def _canonical_sha256(data: dict[str, Any]) -> str:
    payload = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _question_files(directory: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        data = _load_json(path)
        question_id = str(data.get("question_id") or "").strip()
        if not question_id or not data.get("question_type"):
            continue
        if path.stem != question_id:
            raise ValueError(
                f"question ID does not match evaluation filename: {path.name} != {question_id}"
            )
        if question_id in files:
            raise ValueError(f"duplicate question_id {question_id}: {path}")
        files[question_id] = path
    return files


def _load_question_ids(path: Path) -> set[str]:
    raw_scope = path.read_text(encoding="utf-8")
    try:
        parsed_scope = json.loads(raw_scope)
    except json.JSONDecodeError:
        parsed_scope = [line.strip() for line in raw_scope.splitlines() if line.strip()]
    if not isinstance(parsed_scope, list) or not all(
        isinstance(item, str) and item.strip() for item in parsed_scope
    ):
        raise ValueError(
            "question-ID file must contain a JSON string array or one ID per line"
        )
    question_ids = {item.strip() for item in parsed_scope}
    if not question_ids:
        raise ValueError("question-ID file must not be empty")
    if len(question_ids) != len(parsed_scope):
        raise ValueError("question-ID file contains duplicate IDs")
    return question_ids


def _usage_summary(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    events = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError(f"usage event {line_number} is not a JSON object")
        events.append(event)

    completion_events = [
        event for event in events if event.get("event") == "completion"
    ]
    error_events = [event for event in events if event.get("event") == "upstream_error"]
    providers = Counter(str(event.get("provider") or "unknown") for event in events)
    completion_providers = Counter(
        str(event.get("provider") or "unknown") for event in completion_events
    )
    error_providers = Counter(
        str(event.get("provider") or "unknown") for event in error_events
    )
    models = Counter(str(event.get("request_model") or "unknown") for event in events)
    error_statuses = Counter(
        str(event.get("status") or "unknown") for event in error_events
    )
    finish_reasons = Counter(
        str(event.get("finish_reason") or "unknown") for event in completion_events
    )
    malformed_http_200 = sum(
        1
        for event in completion_events
        if event.get("response_complete") is False
        or (
            "response_complete" not in event
            and not event.get("provider")
            and not event.get("response_model")
            and not event.get("finish_reason")
        )
    )
    invalid_completion_identities = sum(
        1
        for event in completion_events
        if event.get("response_model") in _IDENTITY_SENTINELS
        or event.get("provider") in _IDENTITY_SENTINELS
    )
    unknown_cost_attempts = sum(
        1 for event in events if event.get("unknown_cost") is True
    )
    for index, event in enumerate(events, 1):
        if "unknown_cost" in event and not isinstance(event["unknown_cost"], bool):
            raise ValueError(f"usage event {index} unknown_cost must be boolean")
    return {
        "events": len(events),
        "completion_calls": len(completion_events),
        "upstream_errors": len(error_events),
        "malformed_http_200_responses": malformed_http_200,
        "invalid_completion_identities": invalid_completion_identities,
        "unknown_cost_attempts": unknown_cost_attempts,
        "publication_ready": (
            invalid_completion_identities == 0 and unknown_cost_attempts == 0
        ),
        "provider_counts": dict(sorted(providers.items())),
        "completion_provider_counts": dict(sorted(completion_providers.items())),
        "error_provider_counts": dict(sorted(error_providers.items())),
        "request_model_counts": dict(sorted(models.items())),
        "error_status_counts": dict(sorted(error_statuses.items())),
        "finish_reason_counts": dict(sorted(finish_reasons.items())),
        "cost_usd": round(
            sum(float(event.get("cost_usd") or 0.0) for event in events), 12
        ),
        "prompt_tokens": sum(int(event.get("prompt_tokens") or 0) for event in events),
        "cached_tokens": sum(int(event.get("cached_tokens") or 0) for event in events),
        "completion_tokens": sum(
            int(event.get("completion_tokens") or 0) for event in events
        ),
        "reasoning_tokens": sum(
            int(event.get("reasoning_tokens") or 0) for event in events
        ),
        "first_timestamp": events[0].get("timestamp") if events else None,
        "last_timestamp": events[-1].get("timestamp") if events else None,
    }


def _harness_log_summary(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    failed_attempts = Counter(
        int(attempt)
        for attempt in re.findall(r"Generation attempt ([1-5])/5 failed:", text)
    )
    timed_out_attempts = Counter(
        int(attempt)
        for attempt in re.findall(r"Generation attempt ([1-5])/5 timed out", text)
    )
    returned_none = len(re.findall(r"Generation returned None", text))
    return {
        "failed_attempt_counts": {
            str(attempt): failed_attempts[attempt]
            for attempt in sorted(failed_attempts)
        },
        "timed_out_attempt_counts": {
            str(attempt): timed_out_attempts[attempt]
            for attempt in sorted(timed_out_attempts)
        },
        "returned_none_responses": returned_none,
        "attempt_five_failures": failed_attempts[5] + timed_out_attempts[5],
    }


def audit_evaluation(
    evaluated_directory: Path,
    *,
    frozen_directory: Path | None = None,
    usage_log: Path | None = None,
    evaluator_log: Path | None = None,
    expected_questions: int | None = None,
    cutoffs: tuple[str | int, ...] = DEFAULT_CUTOFFS,
    question_ids: set[str] | None = None,
) -> dict[str, Any]:
    evaluated_directory = evaluated_directory.expanduser().resolve()
    frozen_directory = (
        frozen_directory.expanduser().resolve() if frozen_directory else None
    )
    usage_log = usage_log.expanduser().resolve() if usage_log else None
    evaluator_log = evaluator_log.expanduser().resolve() if evaluator_log else None

    all_evaluated_files = _question_files(evaluated_directory)
    all_frozen_files = _question_files(frozen_directory) if frozen_directory else {}
    expected_ids = set(question_ids) if question_ids is not None else None
    if expected_questions is not None and expected_questions <= 0:
        raise ValueError("expected_questions must be positive")
    if not expected_ids and expected_ids is not None:
        raise ValueError("question scope must not be empty")
    if (
        expected_ids is not None
        and expected_questions is not None
        and expected_questions != len(expected_ids)
    ):
        raise ValueError(
            "expected_questions does not match the declared question scope: "
            f"{expected_questions} != {len(expected_ids)}"
        )
    if expected_ids is not None:
        evaluated_files = {
            question_id: path
            for question_id, path in all_evaluated_files.items()
            if question_id in expected_ids
        }
        frozen_files = {
            question_id: path
            for question_id, path in all_frozen_files.items()
            if question_id in expected_ids
        }
    else:
        evaluated_files = all_evaluated_files
        frozen_files = all_frozen_files
    cutoffs = _normalize_cutoffs(cutoffs)
    expected_questions = (
        len(expected_ids)
        if expected_ids is not None
        else (500 if expected_questions is None else expected_questions)
    )
    missing_cutoffs: list[dict[str, str]] = []
    empty_answers: list[dict[str, str]] = []
    empty_judges: list[dict[str, str]] = []
    invalid_scores: list[dict[str, Any]] = []
    inconsistent_verdicts: list[dict[str, Any]] = []
    frozen_mismatches: list[dict[str, str]] = []
    totals = {cutoff: {"correct": 0, "total": 0} for cutoff in cutoffs}
    by_type: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: {cutoff: {"correct": 0, "total": 0} for cutoff in cutoffs}
    )

    for question_id, evaluated_path in sorted(evaluated_files.items()):
        data = _load_json(evaluated_path)
        question_type = str(data.get("question_type") or "unknown")
        cutoff_results = data.get("cutoff_results")
        if not isinstance(cutoff_results, dict):
            cutoff_results = {}

        for cutoff in cutoffs:
            result = cutoff_results.get(cutoff)
            if not isinstance(result, dict):
                missing_cutoffs.append({"question_id": question_id, "cutoff": cutoff})
                continue
            score = result.get("score")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or float(score) not in {0.0, 1.0}
            ):
                invalid_scores.append(
                    {"question_id": question_id, "cutoff": cutoff, "score": score}
                )
                continue
            score = float(score)
            totals[cutoff]["total"] += 1
            by_type[question_type][cutoff]["total"] += 1
            if score >= 0.5:
                totals[cutoff]["correct"] += 1
                by_type[question_type][cutoff]["correct"] += 1

            generated_answer = result.get("generated_answer")
            judge_raw = result.get("judge_raw")
            if not isinstance(generated_answer, str) or not generated_answer.strip():
                empty_answers.append({"question_id": question_id, "cutoff": cutoff})
            if not isinstance(judge_raw, str) or not judge_raw.strip():
                empty_judges.append({"question_id": question_id, "cutoff": cutoff})
            expected_verdict = "PASS" if score >= 0.5 else "FAIL"
            if str(result.get("judgment") or "").upper() != expected_verdict:
                inconsistent_verdicts.append(
                    {
                        "question_id": question_id,
                        "cutoff": cutoff,
                        "score": score,
                        "judgment": result.get("judgment"),
                    }
                )

        if frozen_directory:
            frozen_path = frozen_files.get(question_id)
            if frozen_path is None:
                frozen_mismatches.append(
                    {"question_id": question_id, "reason": "missing frozen prediction"}
                )
            else:
                evaluated_payload = _prediction_payload(data)
                frozen_payload = _prediction_payload(_load_json(frozen_path))
                if _canonical_sha256(evaluated_payload) != _canonical_sha256(
                    frozen_payload
                ):
                    frozen_mismatches.append(
                        {
                            "question_id": question_id,
                            "reason": "retrieval payload changed",
                        }
                    )

    if expected_ids is not None:
        missing_evaluated_ids = sorted(expected_ids - set(evaluated_files))
        missing_frozen_ids = (
            sorted(expected_ids - set(frozen_files)) if frozen_directory else []
        )
        extra_evaluated_ids: list[str] = []
    else:
        missing_evaluated_ids = sorted(set(frozen_files) - set(evaluated_files))
        missing_frozen_ids = []
        extra_evaluated_ids = (
            sorted(set(evaluated_files) - set(frozen_files)) if frozen_directory else []
        )

    metric_summary: dict[str, dict[str, float | int]] = {}
    for cutoff, values in totals.items():
        total = values["total"]
        correct = values["correct"]
        metric_summary[cutoff] = {
            "correct": correct,
            "total": total,
            "accuracy": round(correct / total, 6) if total else 0.0,
        }

    type_summary: dict[str, Any] = {}
    for question_type, cutoff_values in sorted(by_type.items()):
        type_summary[question_type] = {}
        for cutoff, values in cutoff_values.items():
            total = values["total"]
            correct = values["correct"]
            type_summary[question_type][cutoff] = {
                "correct": correct,
                "total": total,
                "accuracy": round(correct / total, 6) if total else 0.0,
            }

    usage = _usage_summary(usage_log)
    official_harness_score_complete = (
        len(evaluated_files) == expected_questions
        and not missing_cutoffs
        and not invalid_scores
        and not inconsistent_verdicts
        and not missing_evaluated_ids
        and not missing_frozen_ids
        and not extra_evaluated_ids
        and not frozen_mismatches
    )
    complete = (
        official_harness_score_complete
        and not empty_answers
        and not empty_judges
        and (usage is None or usage["publication_ready"])
    )
    return {
        "schema_version": 1,
        "complete": complete,
        "official_harness_score_complete": official_harness_score_complete,
        "expected_questions": expected_questions,
        "evaluated_questions": len(evaluated_files),
        "frozen_questions": len(frozen_files) if frozen_directory else None,
        "scoped_question_subset": question_ids is not None,
        "cutoffs": list(cutoffs),
        "metrics": metric_summary,
        "by_question_type": type_summary,
        "validation": {
            "missing_cutoffs": missing_cutoffs,
            "empty_answers": empty_answers,
            "empty_judges": empty_judges,
            "invalid_scores": invalid_scores,
            "inconsistent_verdicts": inconsistent_verdicts,
            "missing_evaluated_ids": missing_evaluated_ids,
            "missing_frozen_ids": missing_frozen_ids,
            "extra_evaluated_ids": extra_evaluated_ids,
            "frozen_payload_mismatches": frozen_mismatches,
        },
        "usage": usage,
        "harness_log": _harness_log_summary(evaluator_log),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluated-directory", type=Path, required=True)
    parser.add_argument("--frozen-directory", type=Path)
    parser.add_argument("--usage-log", type=Path)
    parser.add_argument("--evaluator-log", type=Path)
    parser.add_argument("--expected-questions", type=int)
    parser.add_argument(
        "--cutoffs",
        default=",".join(value.removeprefix("top_") for value in DEFAULT_CUTOFFS),
        help=(
            "Comma-separated declared cutoffs (for example 20,50). "
            "Defaults to the legacy 10,20,50,200 family."
        ),
    )
    parser.add_argument(
        "--question-id-file",
        type=Path,
        help="Optional JSON array or newline-delimited question-ID scope",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--require-official-score-complete", action="store_true")
    args = parser.parse_args()

    try:
        question_ids = (
            _load_question_ids(args.question_id_file.expanduser().resolve())
            if args.question_id_file
            else None
        )
        report = audit_evaluation(
            args.evaluated_directory,
            frozen_directory=args.frozen_directory,
            usage_log=args.usage_log,
            evaluator_log=args.evaluator_log,
            expected_questions=args.expected_questions,
            cutoffs=tuple(
                value.strip() for value in args.cutoffs.split(",") if value.strip()
            ),
            question_ids=question_ids,
        )
    except ValueError as error:
        parser.error(str(error))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if args.require_complete and not report["complete"]:
        return 1
    if (
        args.require_official_score_complete
        and not report["official_harness_score_complete"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
