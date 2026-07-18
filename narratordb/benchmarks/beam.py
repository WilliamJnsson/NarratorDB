#!/usr/bin/env python3
"""Retrieval diagnostic on official ICLR 2026 BEAM.

BEAM's official headline score requires an answer model and rubric-judge model.
This key-free adapter measures storage/query latency plus transparent lexical
coverage and embedding similarity between retrieved messages and rubric
nuggets. These proxy metrics must not be presented as official BEAM pass rate.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import statistics
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


from narratordb.engine import Engine, STOP_WORDS_EN


DATASET_NAME = "Mohammadta/BEAM"
VALID_SIZES = ("100K", "500K", "1M")


def parse_chat(chat) -> list[list[dict]]:
    if not chat:
        return []
    if isinstance(chat, list) and chat and isinstance(chat[0], list):
        return chat
    if isinstance(chat, list) and chat and isinstance(chat[0], dict):
        if "turns" in chat[0]:
            return [batch.get("turns", []) for batch in chat]
        if "role" in chat[0] or "content" in chat[0]:
            return [chat]
    return []


def parse_anchor(value: str | None) -> float:
    if not value:
        return 0.0
    for fmt in ("%B-%d-%Y", "%B %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.replace(",", ""), fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


def flatten_messages(conversation: dict) -> list[dict]:
    messages = []
    for batch_index, batch in enumerate(parse_chat(conversation.get("chat"))):
        for turn_index, turn in enumerate(batch):
            content = str(turn.get("content") or "").strip()
            if not content:
                continue
            timestamp = parse_anchor(turn.get("time_anchor")) + (turn_index / 1000)
            messages.append(
                {
                    "speaker": str(turn.get("role") or "memory"),
                    "text": content,
                    "timestamp": timestamp,
                    "provenance": {
                        "provider": "beam",
                        "run_id": f"batch-{batch_index}",
                        "metadata": {"batch_index": batch_index, "turn_index": turn_index},
                    },
                }
            )
    return messages


def probing_questions(conversation: dict, per_type: int | None) -> list[dict]:
    raw = conversation.get("probing_questions") or {}
    if isinstance(raw, str):
        raw = ast.literal_eval(raw)
    questions = []
    for question_type in sorted(raw):
        values = raw[question_type]
        if isinstance(values, dict):
            values = [values]
        for value in values[:per_type] if per_type is not None else values:
            if isinstance(value, str):
                value = {"question": value, "rubric": []}
            questions.append({**value, "question_type": question_type})
    return questions


def rubric_nuggets(question: dict) -> list[str]:
    rubric = question.get("rubric") or []
    if isinstance(rubric, dict):
        rubric = rubric.get("nuggets") or []
    nuggets = []
    for item in rubric:
        if isinstance(item, dict):
            item = item.get("description") or item.get("text") or str(item)
        text = str(item).strip()
        if text:
            nuggets.append(text)
    return nuggets


def content_tokens(text: str) -> set[str]:
    import re

    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in STOP_WORDS_EN
    }


def nugget_scores(engine: Engine, nuggets: list[str], retrieved: list[str]) -> dict:
    if not nuggets:
        return {"nuggets": 0, "token_coverage": None, "max_semantic_similarity": None}
    combined_tokens = content_tokens("\n".join(retrieved))
    coverages = []
    for nugget in nuggets:
        tokens = content_tokens(nugget)
        coverages.append(len(tokens & combined_tokens) / len(tokens) if tokens else 0.0)

    similarities = []
    if engine._sbert and retrieved:
        import numpy as np

        retrieved_embeddings = engine._sbert.encode(
            retrieved, normalize_embeddings=True, batch_size=64, show_progress_bar=False
        )
        nugget_embeddings = engine._sbert.encode(
            nuggets, normalize_embeddings=True, batch_size=64, show_progress_bar=False
        )
        matrix = np.matmul(nugget_embeddings, retrieved_embeddings.T)
        similarities = [float(value) for value in matrix.max(axis=1)]
    return {
        "nuggets": len(nuggets),
        "token_coverage": round(statistics.fmean(coverages), 6),
        "max_semantic_similarity": round(statistics.fmean(similarities), 6) if similarities else None,
    }


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def summarize(results: list[dict]) -> dict:
    scored = [item for item in results if item["proxy"]["nuggets"]]
    query_ms = [item["query_ms"] for item in results]
    return {
        "questions": len(results),
        "questions_with_rubrics": len(scored),
        "mean_token_coverage": round(
            statistics.fmean(item["proxy"]["token_coverage"] for item in scored), 6
        ) if scored else None,
        "mean_max_semantic_similarity": round(
            statistics.fmean(
                item["proxy"]["max_semantic_similarity"]
                for item in scored
                if item["proxy"]["max_semantic_similarity"] is not None
            ),
            6,
        ) if any(item["proxy"]["max_semantic_similarity"] is not None for item in scored) else None,
        "query_ms": {
            "mean": round(statistics.fmean(query_ms), 3),
            "p50": round(statistics.median(query_ms), 3),
            "p95": round(percentile(query_ms, 0.95), 3),
        } if query_ms else {},
    }


def run(size: str, conversation_limit: int | None, per_type: int | None, top_k: int) -> dict:
    try:
        from datasets import load_dataset
    except ImportError as error:
        raise SystemExit("BEAM requires the optional `datasets` package") from error

    dataset = load_dataset(DATASET_NAME, split=size)
    if conversation_limit is not None:
        dataset = dataset.select(range(min(conversation_limit, len(dataset))))

    results = []
    ingest_summaries = []
    by_type: dict[str, list[dict]] = defaultdict(list)
    with tempfile.TemporaryDirectory(prefix="narratordb_beam_") as directory:
        for conversation_index, conversation in enumerate(dataset):
            db_path = Path(directory) / f"beam-{size}-{conversation_index}.db"
            engine = Engine(str(db_path), user_id=f"beam-{size}-{conversation_index}", context_window=0)
            messages = flatten_messages(conversation)
            started = time.monotonic()
            stored = engine.store_batch(messages)
            ingest_ms = (time.monotonic() - started) * 1000
            ingest_summaries.append({"stored_messages": stored, "ingest_ms": round(ingest_ms, 3)})

            questions = probing_questions(conversation, per_type)
            for question_index, question in enumerate(questions):
                question_text = str(question.get("question") or question.get("question_text") or "").strip()
                if not question_text:
                    continue
                search = engine.search(
                    question_text,
                    limit=top_k,
                    max_context=top_k,
                    full_context_threshold=0,
                )
                retrieved = [message.text for message in search.direct_hits[:top_k]]
                proxy = nugget_scores(engine, rubric_nuggets(question), retrieved)
                item = {
                    "conversation_index": conversation_index,
                    "question_index": question_index,
                    "question_type": question["question_type"],
                    "question": question_text,
                    "query_ms": round(search.query_ms, 3),
                    "retrieved": len(retrieved),
                    "proxy": proxy,
                }
                results.append(item)
                by_type[question["question_type"]].append(item)
            engine.close()
            for candidate in db_path.parent.glob(db_path.name + "*"):
                candidate.unlink(missing_ok=True)
            print(
                f"[{conversation_index + 1}/{len(dataset)}] stored={stored} "
                f"questions={len(questions)} ingest={ingest_ms:.1f}ms",
                flush=True,
            )

    return {
        "benchmark": "BEAM retrieval proxy",
        "engine": "NarratorDB",
        "dataset": DATASET_NAME,
        "size": size,
        "methodology": "key-free retrieval diagnostic; not official BEAM answer/judge pass rate",
        "top_k": top_k,
        "conversation_limit": conversation_limit,
        "questions_per_type": per_type,
        "summary": {
            **summarize(results),
            "stored_messages": sum(item["stored_messages"] for item in ingest_summaries),
            "ingest_ms_total": round(sum(item["ingest_ms"] for item in ingest_summaries), 3),
        },
        "by_question_type": {key: summarize(value) for key, value in sorted(by_type.items())},
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", choices=VALID_SIZES, default="100K")
    parser.add_argument("--conversations", type=int, default=1)
    parser.add_argument("--questions-per-type", type=int, default=1)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--all", action="store_true", help="Run every conversation and question in the selected split")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = run(
        args.size,
        None if args.all else args.conversations,
        None if args.all else args.questions_per_type,
        args.top_k,
    )
    print(json.dumps(report["summary"], indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
