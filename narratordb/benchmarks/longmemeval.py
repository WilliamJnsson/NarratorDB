#!/usr/bin/env python3
"""Evidence-retrieval evaluation on official LongMemEval_S.

This adapter intentionally reports retrieval metrics, not LLM-judge accuracy.
It uses the same cleaned 500-question dataset currently consumed by Mem0's
public memory-benchmarks repository and the evidence session identifiers from
the original LongMemEval release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import statistics
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


from narratordb.engine import Engine


DATASET_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/"
    "resolve/main/longmemeval_s_cleaned.json"
)
DEFAULT_DATASET = Path("~/.cache/narratordb/benchmarks/longmemeval_s_cleaned.json").expanduser()
QUESTION_TYPES = (
    "knowledge-update",
    "multi-session",
    "single-session-assistant",
    "single-session-preference",
    "single-session-user",
    "temporal-reasoning",
)


def download_dataset(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    print(f"Downloading LongMemEval_S to {path}", flush=True)
    with urllib.request.urlopen(DATASET_URL, timeout=120) as response, temporary.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    temporary.replace(path)


def dataset_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_date(value: str) -> float:
    try:
        cleaned = re.sub(r"\s*\([A-Za-z]+\)\s*", " ", value).strip()
        return datetime.strptime(cleaned, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def sample_questions(rows: list[dict], per_type: int | None, seed: int) -> list[dict]:
    if per_type is None:
        return sorted(rows, key=lambda row: row["question_id"])
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["question_type"]].append(row)
    rng = random.Random(seed)
    selected = []
    for question_type in QUESTION_TYPES:
        group = sorted(groups[question_type], key=lambda row: row["question_id"])
        selected.extend(rng.sample(group, min(per_type, len(group))))
    return sorted(selected, key=lambda row: row["question_id"])


def flatten_messages(row: dict) -> list[dict]:
    messages = []
    for session_id, date, session in zip(
        row["haystack_session_ids"], row["haystack_dates"], row["haystack_sessions"]
    ):
        timestamp = parse_date(date)
        for turn_index, turn in enumerate(session):
            content = str(turn.get("content") or "").strip()
            if not content:
                continue
            role = str(turn.get("role") or "memory")
            messages.append(
                {
                    "speaker": role,
                    "text": content,
                    "timestamp": timestamp + (turn_index / 1000),
                    "provenance": {
                        "provider": "longmemeval",
                        "run_id": session_id,
                        "metadata": {
                            "session_id": session_id,
                            "session_date": date,
                            "turn_index": turn_index,
                        },
                    },
                }
            )
    return messages


def unique_retrieved_sessions(messages) -> list[str]:
    ordered = []
    seen = set()
    for message in messages:
        provenance = message.provenance or {}
        metadata = provenance.get("metadata") or {}
        session_id = metadata.get("session_id") or provenance.get("run_id")
        if session_id and session_id not in seen:
            seen.add(session_id)
            ordered.append(str(session_id))
    return ordered


def retrieval_metrics(retrieved: list[str], evidence: list[str], cutoff: int) -> dict:
    ranked = retrieved[:cutoff]
    evidence_set = set(evidence)
    hits = [index for index, item in enumerate(ranked) if item in evidence_set]
    recall_any = float(bool(hits))
    recall_all = float(evidence_set.issubset(ranked))
    reciprocal_rank = 1.0 / (hits[0] + 1) if hits else 0.0
    dcg = sum(1.0 / math.log2(index + 2) for index in hits)
    ideal_hits = min(len(evidence_set), cutoff)
    ideal_dcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_hits))
    return {
        "recall_any": recall_any,
        "recall_all": recall_all,
        "mrr": reciprocal_rank,
        "ndcg": dcg / ideal_dcg if ideal_dcg else 0.0,
    }


def summarize(items: list[dict], cutoffs: list[int]) -> dict:
    summary = {"questions": len(items)}
    for cutoff in cutoffs:
        label = f"at_{cutoff}"
        summary[label] = {
            metric: round(statistics.fmean(item["metrics"][label][metric] for item in items), 6)
            for metric in ("recall_any", "recall_all", "mrr", "ndcg")
        }
    latencies = [item["query_ms"] for item in items]
    ingests = [item["ingest_ms"] for item in items]
    if latencies:
        ordered = sorted(latencies)
        summary["query_ms"] = {
            "mean": round(statistics.fmean(latencies), 3),
            "p50": round(statistics.median(latencies), 3),
            "p95": round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)], 3),
        }
        summary["ingest_ms"] = {
            "mean": round(statistics.fmean(ingests), 3),
            "total": round(sum(ingests), 3),
        }
    return summary


def remove_database_files(path: Path) -> None:
    for candidate in path.parent.glob(path.name + "*"):
        candidate.unlink(missing_ok=True)


def run(dataset: Path, per_type: int | None, seed: int, cutoffs: list[int]) -> dict:
    rows = json.loads(dataset.read_text(encoding="utf-8"))
    selected = sample_questions(rows, per_type=per_type, seed=seed)
    max_cutoff = max(cutoffs)
    results = []

    with tempfile.TemporaryDirectory(prefix="narratordb_longmemeval_") as directory:
        directory_path = Path(directory)
        for index, row in enumerate(selected, start=1):
            db_path = directory_path / f"{row['question_id']}.db"
            messages = flatten_messages(row)
            engine = Engine(str(db_path), user_id=row["question_id"], context_window=0)
            ingest_start = time.monotonic()
            stored = engine.store_batch(messages)
            ingest_ms = (time.monotonic() - ingest_start) * 1000
            result = engine.search(
                row["question"],
                limit=max_cutoff * 4,
                max_context=max_cutoff * 4,
                full_context_threshold=0,
                profile="default",
            )
            retrieved_sessions = unique_retrieved_sessions(result.direct_hits)
            metrics = {
                f"at_{cutoff}": retrieval_metrics(
                    retrieved_sessions, row["answer_session_ids"], cutoff
                )
                for cutoff in cutoffs
            }
            results.append(
                {
                    "question_id": row["question_id"],
                    "question_type": row["question_type"],
                    "question": row["question"],
                    "answer": row["answer"],
                    "evidence_sessions": row["answer_session_ids"],
                    "retrieved_sessions": retrieved_sessions[:max_cutoff],
                    "stored_messages": stored,
                    "ingest_ms": round(ingest_ms, 3),
                    "query_ms": round(result.query_ms, 3),
                    "metrics": metrics,
                }
            )
            engine.close()
            remove_database_files(db_path)
            print(
                f"[{index:>3}/{len(selected)}] {row['question_type']:<28} "
                f"R-any@5={metrics.get('at_5', metrics[f'at_{cutoffs[0]}'])['recall_any']:.0f} "
                f"query={result.query_ms:.1f}ms",
                flush=True,
            )

    by_type = {
        question_type: summarize(
            [item for item in results if item["question_type"] == question_type], cutoffs
        )
        for question_type in QUESTION_TYPES
        if any(item["question_type"] == question_type for item in results)
    }
    return {
        "benchmark": "LongMemEval_S evidence retrieval",
        "engine": "NarratorDB",
        "methodology": "session-level evidence recall; no answerer or LLM judge",
        "dataset_url": DATASET_URL,
        "dataset_sha256": dataset_sha256(dataset),
        "seed": seed,
        "sample_per_type": per_type,
        "cutoffs": cutoffs,
        "summary": summarize(results, cutoffs),
        "by_question_type": by_type,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--per-type", type=int, default=2, help="Deterministic sample per type")
    parser.add_argument("--all", action="store_true", help="Run all 500 questions")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cutoffs", default="5,10,20,50")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-recall-any-5", type=float, default=0.0)
    args = parser.parse_args()

    dataset = args.dataset.expanduser().resolve()
    if not dataset.exists():
        download_dataset(dataset)
    cutoffs = sorted({int(value) for value in args.cutoffs.split(",") if int(value) > 0})
    report = run(dataset, None if args.all else args.per_type, args.seed, cutoffs)
    print(json.dumps(report["summary"], indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {args.output}")

    recall_at_5 = report["summary"].get("at_5", {}).get("recall_any", 0.0)
    if recall_at_5 < args.min_recall_any_5:
        print(
            f"FAIL: recall_any@5 {recall_at_5:.4f} < {args.min_recall_any_5:.4f}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
