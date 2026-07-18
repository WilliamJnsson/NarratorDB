#!/usr/bin/env python3
"""Replay archived LongMemEval retrieval without making model calls.

This is a development/audit tool. It compares retrieval rankings and labeled
evidence-session coverage, but it does not generate answers or judge accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from narratordb.engine import Engine


DEFAULT_CUTOFFS = (10, 20, 50, 200)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def nearest_rank(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile + 0.999999) - 1))
    return ordered[index]


def latency_summary(values: Iterable[float]) -> dict:
    samples = [float(value) for value in values]
    if not samples:
        return {"samples": 0, "mean": 0.0, "median": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0}
    return {
        "samples": len(samples),
        "mean": round(statistics.mean(samples), 6),
        "median": round(statistics.median(samples), 6),
        "p95": round(nearest_rank(samples, 0.95), 6),
        "min": round(min(samples), 6),
        "max": round(max(samples), 6),
    }


def _text_session_map(sample: dict) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for session_id, session in zip(
        sample.get("haystack_session_ids") or [],
        sample.get("haystack_sessions") or [],
    ):
        for message in session:
            content = str(message.get("content") or "")
            mapping.setdefault(content, set()).add(str(session_id))
    return mapping


def _evidence_ranks(texts: list[str], sample: dict) -> dict[str, int | None]:
    mapping = _text_session_map(sample)
    return {
        str(session_id): next(
            (
                rank
                for rank, text in enumerate(texts, 1)
                if str(session_id) in mapping.get(text, set())
            ),
            None,
        )
        for session_id in sorted(sample.get("answer_session_ids") or [])
    }


def _coverage(ranks: dict[str, int | None], cutoff: int) -> tuple[bool, bool]:
    found = {session_id for session_id, rank in ranks.items() if rank is not None and rank <= cutoff}
    targets = set(ranks)
    return bool(found), bool(targets) and found == targets


def replay(
    *,
    database: Path,
    baseline_results: Path,
    dataset: Path,
    cutoffs: tuple[int, ...] = DEFAULT_CUTOFFS,
) -> dict:
    baseline = json.loads(baseline_results.read_text(encoding="utf-8"))
    samples = json.loads(dataset.read_text(encoding="utf-8"))
    samples_by_id = {str(sample["question_id"]): sample for sample in samples}

    baseline_latency = []
    current_latency = []
    stage_values: dict[str, list[float]] = {}
    coverage = {
        label: {str(cutoff): {"recall_any": 0, "recall_all": 0} for cutoff in cutoffs}
        for label in ("baseline", "current")
    }
    evaluations = []
    all_scopes_healthy = True

    for evaluation in baseline.get("evaluations") or []:
        question_id = str(evaluation["question_id"])
        sample = samples_by_id.get(question_id)
        if sample is None:
            raise ValueError(f"question is missing from dataset: {question_id}")

        engine = Engine(
            str(database),
            user_id=str(evaluation["user_id"]),
            context_window=0,
        )
        try:
            result = engine.search(
                str(evaluation["question"]),
                limit=max(cutoffs),
                max_context=max(cutoffs),
                full_context_threshold=0,
            )
            all_scopes_healthy = all_scopes_healthy and bool(engine.health_check()["ok"])
        finally:
            engine.close()

        old_hits = (evaluation.get("retrieval") or {}).get("search_results") or []
        old_ids = [str(hit.get("id")) for hit in old_hits]
        old_texts = [str(hit.get("memory") or "") for hit in old_hits]
        new_ids = [str(message.id) for message in result.direct_hits]
        new_texts = [message.text for message in result.direct_hits]
        old_ranks = _evidence_ranks(old_texts, sample)
        new_ranks = _evidence_ranks(new_texts, sample)

        for label, ranks in (("baseline", old_ranks), ("current", new_ranks)):
            for cutoff in cutoffs:
                recall_any, recall_all = _coverage(ranks, cutoff)
                coverage[label][str(cutoff)]["recall_any"] += int(recall_any)
                coverage[label][str(cutoff)]["recall_all"] += int(recall_all)

        old_latency = float((evaluation.get("retrieval") or {}).get("search_latency_ms") or 0.0)
        baseline_latency.append(old_latency)
        current_latency.append(result.query_ms)
        for stage, value in result.timings_ms.items():
            stage_values.setdefault(stage, []).append(value)

        evaluations.append(
            {
                "question_id": question_id,
                "question_type": str(evaluation.get("question_type") or ""),
                "baseline_result_count": len(old_ids),
                "current_result_count": len(new_ids),
                "ranking_changed": old_ids != new_ids,
                "baseline_evidence_session_ranks": old_ranks,
                "current_evidence_session_ranks": new_ranks,
                "baseline_query_ms": round(old_latency, 6),
                "current_query_ms": round(result.query_ms, 6),
                "current_stage_ms": {
                    stage: round(value, 6) for stage, value in result.timings_ms.items()
                },
                "current_direct_hits": [
                    {
                        "rank": rank,
                        "id": str(message.id),
                        "text_sha256": hashlib.sha256(message.text.encode("utf-8")).hexdigest(),
                    }
                    for rank, message in enumerate(result.direct_hits, 1)
                ],
            }
        )

    total = len(evaluations)
    for label in coverage:
        for cutoff in cutoffs:
            metrics = coverage[label][str(cutoff)]
            metrics["questions"] = total
            metrics["recall_any_rate"] = round(metrics["recall_any"] / total, 6) if total else 0.0
            metrics["recall_all_rate"] = round(metrics["recall_all"] / total, 6) if total else 0.0

    baseline_summary = latency_summary(baseline_latency)
    current_summary = latency_summary(current_latency)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "classification": "development retrieval replay; not an unbiased answer/judge score",
        "inputs": {
            "database": str(database),
            "database_sha256_after_migration": sha256_file(database),
            "baseline_results": str(baseline_results),
            "baseline_results_sha256": sha256_file(baseline_results),
            "dataset": str(dataset),
            "dataset_sha256": sha256_file(dataset),
        },
        "questions": total,
        "cutoffs": list(cutoffs),
        "all_scopes_healthy": all_scopes_healthy,
        "ranking_changed_questions": sum(item["ranking_changed"] for item in evaluations),
        "evidence_session_coverage": coverage,
        "latency_ms": {
            "baseline": baseline_summary,
            "current": current_summary,
            "speedup": {
                metric: round(baseline_summary[metric] / current_summary[metric], 6)
                if current_summary[metric]
                else None
                for metric in ("mean", "median", "p95")
            },
            "current_stages": {
                stage: latency_summary(values) for stage, values in stage_values.items()
            },
        },
        "evaluations": evaluations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--baseline-results", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = replay(
        database=args.database.expanduser().resolve(),
        baseline_results=args.baseline_results.expanduser().resolve(),
        dataset=args.dataset.expanduser().resolve(),
    )
    destination = args.output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: output[key] for key in ("questions", "all_scopes_healthy", "ranking_changed_questions", "latency_ms")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
