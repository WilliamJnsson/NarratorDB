#!/usr/bin/env python3
"""Audit an official LongMemEval predict-only directory without model calls."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from narratordb.benchmarks.replay import (
    DEFAULT_CUTOFFS,
    _coverage,
    _evidence_ranks,
    latency_summary,
    sha256_file,
)


def _normalize_cutoffs(cutoffs: tuple[int, ...]) -> tuple[int, ...]:
    if not cutoffs or any(
        isinstance(cutoff, bool) or not isinstance(cutoff, int) or cutoff <= 0
        for cutoff in cutoffs
    ):
        raise ValueError("cutoffs must be positive integers")
    if len(set(cutoffs)) != len(cutoffs):
        raise ValueError("cutoffs contain duplicates")
    return tuple(sorted(cutoffs))


def audit_predictions(
    prediction_dir: Path,
    dataset: Path,
    *,
    cutoffs: tuple[int, ...] = DEFAULT_CUTOFFS,
    require_all: bool = False,
    question_ids: set[str] | None = None,
) -> dict:
    cutoffs = _normalize_cutoffs(cutoffs)
    samples = json.loads(dataset.read_text(encoding="utf-8"))
    if not isinstance(samples, list) or not samples:
        raise ValueError("dataset must be a non-empty JSON array")
    samples_by_id = {str(sample["question_id"]): sample for sample in samples}
    if len(samples_by_id) != len(samples):
        raise ValueError("dataset contains duplicate question IDs")
    dataset_ids = set(samples_by_id)
    expected_ids = set(question_ids) if question_ids is not None else dataset_ids
    if question_ids is not None and not expected_ids:
        raise ValueError("question scope must not be empty")
    unknown_ids = expected_ids - dataset_ids
    if unknown_ids:
        raise ValueError(f"question scope contains IDs absent from dataset: {sorted(unknown_ids)[:10]}")
    all_result_paths = sorted(
        path
        for path in prediction_dir.glob("*.json")
        if not path.name.startswith("_")
    )
    unknown_prediction_ids = {
        path.stem for path in all_result_paths if path.stem not in dataset_ids
    }
    if unknown_prediction_ids:
        raise ValueError(
            "prediction directory contains IDs absent from dataset: "
            f"{sorted(unknown_prediction_ids)[:10]}"
        )
    result_paths = [path for path in all_result_paths if path.stem in expected_ids]
    result_ids = {path.stem for path in result_paths}
    if require_all and result_ids != expected_ids:
        missing = sorted(expected_ids - result_ids)
        unexpected = sorted(result_ids - expected_ids)
        raise ValueError(
            f"prediction set mismatch: missing={missing[:10]} ({len(missing)} total), "
            f"unexpected={unexpected[:10]} ({len(unexpected)} total)"
        )

    coverage = {
        str(cutoff): {"recall_any": 0, "recall_all": 0}
        for cutoff in cutoffs
    }
    harness_latency = []
    engine_latency = []
    backend_latency = []
    stage_values: dict[str, list[float]] = {}
    result_counts = []
    evaluations = []
    type_buckets: dict[str, dict] = {}

    for path in result_paths:
        result = json.loads(path.read_text(encoding="utf-8"))
        question_id = str(result.get("question_id") or path.stem)
        if question_id != path.stem:
            raise ValueError(f"question ID does not match prediction filename: {path.name}")
        sample = samples_by_id.get(question_id)
        if sample is None:
            raise ValueError(f"prediction question is absent from dataset: {question_id}")
        question_type = str(result.get("question_type") or sample.get("question_type") or "unknown")
        type_bucket = type_buckets.setdefault(
            question_type,
            {
                "questions": 0,
                "result_counts": [],
                "harness_latency": [],
                "coverage": {
                    str(cutoff): {"recall_any": 0, "recall_all": 0}
                    for cutoff in cutoffs
                },
            },
        )
        type_bucket["questions"] += 1
        retrieval = result.get("retrieval") or {}
        hits = retrieval.get("search_results") or []
        texts = [str(hit.get("memory") or "") for hit in hits]
        ranks = _evidence_ranks(texts, sample)
        for cutoff in cutoffs:
            recall_any, recall_all = _coverage(ranks, cutoff)
            coverage[str(cutoff)]["recall_any"] += int(recall_any)
            coverage[str(cutoff)]["recall_all"] += int(recall_all)
            type_bucket["coverage"][str(cutoff)]["recall_any"] += int(recall_any)
            type_bucket["coverage"][str(cutoff)]["recall_all"] += int(recall_all)

        query_debug = retrieval.get("query_debug") or {}
        timings = query_debug.get("timings_ms") or {}
        harness_ms = float(retrieval.get("search_latency_ms") or 0.0)
        engine_value = query_debug.get("query_ms", timings.get("total"))
        backend_value = query_debug.get("backend_ms")
        engine_ms = float(engine_value) if isinstance(engine_value, (int, float)) else None
        backend_ms = float(backend_value) if isinstance(backend_value, (int, float)) else None
        harness_latency.append(harness_ms)
        type_bucket["harness_latency"].append(harness_ms)
        if engine_ms is not None:
            engine_latency.append(engine_ms)
        if backend_ms is not None:
            backend_latency.append(backend_ms)
        for stage, value in timings.items():
            if isinstance(value, (int, float)):
                stage_values.setdefault(str(stage), []).append(float(value))
        result_counts.append(len(hits))
        type_bucket["result_counts"].append(len(hits))
        evaluations.append(
            {
                "question_id": question_id,
                "question_type": question_type,
                "result_count": len(hits),
                "evidence_session_ranks": ranks,
                "harness_search_ms": harness_ms,
                "engine_query_ms": engine_ms,
                "backend_ms": backend_ms,
                "stage_ms": timings,
                "prediction_sha256": sha256_file(path),
            }
        )

    total = len(result_paths)
    for cutoff in cutoffs:
        metrics = coverage[str(cutoff)]
        metrics["questions"] = total
        metrics["recall_any_rate"] = round(metrics["recall_any"] / total, 6) if total else 0.0
        metrics["recall_all_rate"] = round(metrics["recall_all"] / total, 6) if total else 0.0

    by_question_type = {}
    for question_type, bucket in sorted(type_buckets.items()):
        questions = int(bucket["questions"])
        type_coverage = bucket["coverage"]
        for cutoff in cutoffs:
            metrics = type_coverage[str(cutoff)]
            metrics["questions"] = questions
            metrics["recall_any_rate"] = (
                round(metrics["recall_any"] / questions, 6) if questions else 0.0
            )
            metrics["recall_all_rate"] = (
                round(metrics["recall_all"] / questions, 6) if questions else 0.0
            )
        counts = bucket["result_counts"]
        by_question_type[question_type] = {
            "questions": questions,
            "result_counts": {
                "min": min(counts) if counts else 0,
                "median": statistics.median(counts) if counts else 0,
                "max": max(counts) if counts else 0,
                "mean": round(statistics.mean(counts), 6) if counts else 0.0,
            },
            "evidence_session_coverage": type_coverage,
            "official_harness_latency_ms": latency_summary(bucket["harness_latency"]),
        }

    ingestion_paths = sorted(
        path
        for path in prediction_dir.glob("_ingestion_*.json")
        if path.stem.removeprefix("_ingestion_") in expected_ids
    )
    ingestion_records = [json.loads(path.read_text(encoding="utf-8")) for path in ingestion_paths]
    ingestion_ids = {
        path.stem.removeprefix("_ingestion_") for path in ingestion_paths
    }
    progress_paths = sorted(
        path
        for path in prediction_dir.glob("_progress_*.json")
        if path.stem.removeprefix("_progress_") in expected_ids
    )
    failed_pairs = sum(int(record.get("total_pairs_failed") or 0) for record in ingestion_records)
    processed_pairs = sum(int(record.get("total_pairs_processed") or 0) for record in ingestion_records)
    ingestion_complete = ingestion_ids == expected_ids and not progress_paths
    if require_all and not ingestion_complete:
        missing = sorted(expected_ids - ingestion_ids)
        unexpected = sorted(ingestion_ids - expected_ids)
        raise ValueError(
            f"ingestion set mismatch: missing={missing[:10]} ({len(missing)} total), "
            f"unexpected={unexpected[:10]} ({len(unexpected)} total), "
            f"partial_progress_files={len(progress_paths)}"
        )
    if require_all and failed_pairs:
        raise ValueError(f"official ingestion recorded {failed_pairs} failed pairs")

    return {
        "schema_version": 1,
        "classification": "key-free official-harness retrieval diagnostic; no answer/judge score",
        "prediction_directory": str(prediction_dir),
        "dataset": str(dataset),
        "dataset_sha256": sha256_file(dataset),
        "dataset_questions": len(samples_by_id),
        "questions": total,
        "expected_questions": len(expected_ids),
        "scoped_question_subset": question_ids is not None,
        "complete": (
            result_ids == expected_ids
            and ingestion_complete
            and failed_pairs == 0
        ),
        "cutoffs": list(cutoffs),
        "ingestion": {
            "completed_scopes": len(ingestion_records),
            "complete": ingestion_complete,
            "pairs_processed": processed_pairs,
            "pairs_failed": failed_pairs,
            "partial_progress_files": len(progress_paths),
        },
        "result_counts": {
            "min": min(result_counts) if result_counts else 0,
            "median": statistics.median(result_counts) if result_counts else 0,
            "max": max(result_counts) if result_counts else 0,
            "mean": round(statistics.mean(result_counts), 6) if result_counts else 0.0,
        },
        "evidence_session_coverage": coverage,
        "by_question_type": by_question_type,
        "latency_ms": {
            "official_harness_observed": latency_summary(harness_latency),
            "engine": latency_summary(engine_latency) if engine_latency else None,
            "backend": latency_summary(backend_latency) if backend_latency else None,
            "engine_stages": {
                stage: latency_summary(values) for stage, values in stage_values.items()
            },
            "query_debug_retained_by_harness": bool(engine_latency or backend_latency),
        },
        "evaluations": evaluations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prediction-dir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-all", action="store_true")
    parser.add_argument(
        "--cutoffs",
        default=",".join(str(cutoff) for cutoff in DEFAULT_CUTOFFS),
        help="Comma-separated retrieval cutoffs to audit",
    )
    parser.add_argument(
        "--question-id-file",
        type=Path,
        help="Optional JSON array or newline-delimited question-ID scope",
    )
    args = parser.parse_args()

    question_ids = None
    if args.question_id_file:
        scope_path = args.question_id_file.expanduser().resolve()
        raw_scope = scope_path.read_text(encoding="utf-8")
        try:
            parsed_scope = json.loads(raw_scope)
        except json.JSONDecodeError:
            parsed_scope = [line.strip() for line in raw_scope.splitlines() if line.strip()]
        if not isinstance(parsed_scope, list) or not all(
            isinstance(item, str) and item.strip() for item in parsed_scope
        ):
            parser.error("--question-id-file must contain a JSON string array or one ID per line")
        question_ids = {item.strip() for item in parsed_scope}
        if len(question_ids) != len(parsed_scope):
            parser.error("--question-id-file contains duplicate IDs")

    try:
        cutoffs = tuple(int(value.strip()) for value in args.cutoffs.split(","))
        report = audit_predictions(
            args.prediction_dir.expanduser().resolve(),
            args.dataset.expanduser().resolve(),
            cutoffs=cutoffs,
            require_all=args.require_all,
            question_ids=question_ids,
        )
    except ValueError as error:
        parser.error(str(error))
    destination = args.output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "questions",
                    "complete",
                    "ingestion",
                    "result_counts",
                    "evidence_session_coverage",
                    "latency_ms",
                )
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
