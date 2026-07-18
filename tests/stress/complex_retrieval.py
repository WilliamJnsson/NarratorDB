#!/usr/bin/env python3
"""Complex validation suite for NarratorDB.

Runs against `narratordb/engine.py` only.

Coverage:
1. Curated complex retrieval scenarios
2. Full LoCoMo retrieval diagnostic (answer-in-context, no LLM)
3. Scale/latency test with noisy corpora
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from narratordb.engine import Engine, stem

ROOT = Path(__file__).resolve().parents[2]

LOCOMO_PATH = ROOT / "tests" / "data" / "locomo10.json"
TMP_DIR = Path("/tmp/narratordb_complex")

MONTH_NUMS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    xs = sorted(data)
    k = (len(xs) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (k - f) * (xs[c] - xs[f])


def normalize_tokens(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9']+", str(text).lower())
    return [stem(word) for word in words if len(word) > 1]


def answer_in_context(answer_gt: str, context_text: str, threshold: float = 0.6) -> tuple[bool, float]:
    gt = normalize_tokens(answer_gt)
    ctx = set(normalize_tokens(context_text))
    if not gt:
        return True, 1.0
    matched = sum(1 for token in gt if token in ctx)
    ratio = matched / len(gt)
    return ratio >= threshold, ratio


def parse_session_dt(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    m = re.search(
        r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)[,\s]+(\d{4})",
        date_str,
        re.IGNORECASE,
    )
    if m:
        return datetime(int(m.group(3)), MONTH_NUMS[m.group(2).lower()], int(m.group(1)))
    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})[,\s]+(\d{4})",
        date_str,
        re.IGNORECASE,
    )
    if m:
        return datetime(int(m.group(3)), MONTH_NUMS[m.group(1).lower()], int(m.group(2)))
    return None


def fmt_date(dt: datetime) -> str:
    return dt.strftime("%-d %B %Y")


def resolve_dates(text: str, session_dt: datetime | None) -> str:
    if not session_dt:
        return text

    def month_offset(dt: datetime, offset: int) -> datetime:
        month = dt.month + offset
        year = dt.year
        while month > 12:
            month -= 12
            year += 1
        while month <= 0:
            month += 12
            year -= 1
        return dt.replace(year=year, month=month, day=1)

    substitutions = [
        (r"\bthe day before yesterday\b", fmt_date(session_dt - timedelta(days=2))),
        (r"\byesterday\b", fmt_date(session_dt - timedelta(days=1))),
        (r"\btoday\b", fmt_date(session_dt)),
        (r"\btomorrow\b", fmt_date(session_dt + timedelta(days=1))),
        (r"\blast week\b", fmt_date(session_dt - timedelta(weeks=1))),
        (r"\bnext week\b", fmt_date(session_dt + timedelta(weeks=1))),
        (r"\bthis week\b", fmt_date(session_dt)),
        (r"\blast month\b", month_offset(session_dt, -1).strftime("%B %Y")),
        (r"\bnext month\b", month_offset(session_dt, 1).strftime("%B %Y")),
        (r"\bthis month\b", session_dt.strftime("%B %Y")),
        (r"\blast year\b", str(session_dt.year - 1)),
        (r"\bnext year\b", str(session_dt.year + 1)),
        (r"\bthis year\b", str(session_dt.year)),
    ]
    for pattern, replacement in substitutions:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    def n_ago(match: re.Match[str]) -> str:
        n = int(match.group(1))
        unit = match.group(2).lower()
        if "day" in unit:
            return fmt_date(session_dt - timedelta(days=n))
        if "week" in unit:
            return fmt_date(session_dt - timedelta(weeks=n))
        if "month" in unit:
            return month_offset(session_dt, -n).strftime("%B %Y")
        if "year" in unit:
            return str(session_dt.year - n)
        return match.group(0)

    text = re.sub(r"\b(\d+)\s+(days?|weeks?|months?|years?)\s+ago\b", n_ago, text, flags=re.IGNORECASE)

    day_pat = "|".join(WEEKDAYS.keys())

    def last_weekday(match: re.Match[str]) -> str:
        target = WEEKDAYS.get(match.group(1).lower())
        if target is None:
            return match.group(0)
        days_back = (session_dt.weekday() - target) % 7 or 7
        return fmt_date(session_dt - timedelta(days=days_back))

    def next_weekday(match: re.Match[str]) -> str:
        target = WEEKDAYS.get(match.group(1).lower())
        if target is None:
            return match.group(0)
        days_forward = (target - session_dt.weekday()) % 7 or 7
        return fmt_date(session_dt + timedelta(days=days_forward))

    text = re.sub(rf"\blast\s+({day_pat})\b", last_weekday, text, flags=re.IGNORECASE)
    text = re.sub(rf"\bnext\s+({day_pat})\b", next_weekday, text, flags=re.IGNORECASE)
    return text


def make_temp_db(name: str) -> Path:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    return TMP_DIR / f"{name}.db"


def reset_db(path: Path) -> None:
    for suffix in ("", "-shm", "-wal"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


def run_custom_complex_suite() -> dict:
    db_path = make_temp_db("custom_complex")
    reset_db(db_path)
    engine = Engine(
        db_path=str(db_path),
        user_id="custom_complex",
        context_window=3,
        semantic_dedup=False,
        semantic_search_mode="disabled",
        local_only=True,
    )

    messages = [
        {"speaker": "system", "text": "NarratorDB is an independent long-term memory database."},
        {"speaker": "architect", "text": "NarratorDB uses SQLite FTS5 for lexical retrieval and SBERT embeddings for semantic search."},
        {"speaker": "architect", "text": "The old unified engine should be treated as OLD and is no longer the source of truth."},
        {"speaker": "ops", "text": "Noor Hale handled deployment safety for the Bastion rollout in January 2026."},
        {"speaker": "ops", "text": "Qwen Sentinel took over final approval and deployment safety on 3 March 2026."},
        {"speaker": "ops", "text": "Project Bastion was merged into Sentinel Gate after the March hardening review."},
        {"speaker": "security", "text": "On 14 February 2026 the staging API key was rotated and secrets moved into Doppler."},
        {"speaker": "finance", "text": "NarratorDB charged 79 dollars per month during the February preview."},
        {"speaker": "finance", "text": "On 10 March 2026, NarratorDB pricing changed to 99 dollars per month."},
        {"speaker": "design", "text": "The UI uses DM Sans, and model labels use Source Code Pro."},
        {"speaker": "code", "text": "Function build_context_envelope assembles memory context before provider calls."},
        {"speaker": "sports", "text": "William trains kickboxing twice a week for conditioning."},
        {"speaker": "release", "text": "Rollback plans are approved by Claude Steward before a release is marked production ready."},
    ]

    t0 = time.perf_counter()
    stored = engine.store_batch(messages)
    ingest_ms = (time.perf_counter() - t0) * 1000

    queries = [
        {
            "name": "current_approver",
            "query": "Who currently handles final approval?",
            "expected": "Qwen Sentinel took over final approval and deployment safety on 3 March 2026.",
        },
        {
            "name": "previous_deployment_owner",
            "query": "Who handled deployment safety before Qwen Sentinel?",
            "expected": "Noor Hale handled deployment safety for the Bastion rollout in January 2026.",
        },
        {
            "name": "semantic_sports_gap",
            "query": "What martial arts does William train?",
            "expected": "William trains kickboxing twice a week for conditioning.",
        },
        {
            "name": "pricing_latest",
            "query": "How much does NarratorDB cost now?",
            "expected": "On 10 March 2026, NarratorDB pricing changed to 99 dollars per month.",
        },
        {
            "name": "project_merge",
            "query": "What did Project Bastion get merged into?",
            "expected": "Project Bastion was merged into Sentinel Gate after the March hardening review.",
        },
        {
            "name": "fonts",
            "query": "What fonts were chosen for the UI and the model labels?",
            "expected": "The UI uses DM Sans, and model labels use Source Code Pro.",
        },
        {
            "name": "function_lookup",
            "query": "Which function assembles memory context before provider calls?",
            "expected": "Function build_context_envelope assembles memory context before provider calls.",
        },
        {
            "name": "secret_manager",
            "query": "Where were the secrets moved after the staging API key rotation?",
            "expected": "On 14 February 2026 the staging API key was rotated and secrets moved into Doppler.",
        },
    ]

    search_results = []
    search_latencies = []
    pass_count = 0
    for case in queries:
        start = time.perf_counter()
        result = engine.search(case["query"], limit=8, max_context=20, full_context_threshold=0)
        latency_ms = (time.perf_counter() - start) * 1000
        context = "\n".join(f"{m.speaker}: {m.text}" for m in result.messages)
        found, ratio = answer_in_context(case["expected"], context, threshold=0.75)
        if found:
            pass_count += 1
        search_latencies.append(latency_ms)
        search_results.append(
            {
                "name": case["name"],
                "query": case["query"],
                "expected": case["expected"],
                "passed": found,
                "match_ratio": round(ratio, 3),
                "query_ms": round(result.query_ms, 3),
                "measured_ms": round(latency_ms, 3),
                "total_matches": result.total_matches,
                "top_messages": [asdict(m) for m in result.messages[:5]],
            }
        )

    after_timestamp = datetime(2026, 3, 1).timestamp()
    filtered = engine.search("deployment safety", limit=6, after=after_timestamp, full_context_threshold=0)
    filtered_context = "\n".join(f"{m.speaker}: {m.text}" for m in filtered.messages)
    filtered_pass, filtered_ratio = answer_in_context(
        "Qwen Sentinel took over final approval and deployment safety on 3 March 2026.",
        filtered_context,
        threshold=0.75,
    )

    stats = engine.stats()
    engine.close()

    return {
        "suite": "custom_complex",
        "stored_messages": stored,
        "ingest_ms": round(ingest_ms, 3),
        "pass_count": pass_count,
        "case_count": len(queries),
        "pass_rate": round(pass_count / len(queries), 4),
        "search_latency_ms": {
            "avg": round(statistics.mean(search_latencies), 3),
            "p50": round(percentile(search_latencies, 50), 3),
            "p95": round(percentile(search_latencies, 95), 3),
            "max": round(max(search_latencies), 3),
        },
        "time_filter_check": {
            "passed": filtered_pass,
            "match_ratio": round(filtered_ratio, 3),
            "query_ms": round(filtered.query_ms, 3),
            "returned_messages": len(filtered.messages),
        },
        "engine_stats": stats,
        "cases": search_results,
    }


def run_locomo_retrieval(limit_questions: int | None) -> dict:
    db_path = make_temp_db("locomo_retrieval")
    reset_db(db_path)

    if not LOCOMO_PATH.exists():
        raise FileNotFoundError(f"LoCoMo fixture missing: {LOCOMO_PATH}")

    with LOCOMO_PATH.open() as handle:
        data = json.load(handle)

    category_names = {
        1: "single-hop",
        2: "temporal",
        3: "multi-hop",
        4: "open-domain",
        5: "adversarial",
    }
    found_by_cat: dict[str, list[int]] = defaultdict(list)
    engine_failures_by_cat: dict[str, int] = defaultdict(int)
    inference_by_cat: dict[str, int] = defaultdict(int)
    slowest_queries: list[dict] = []
    total_questions = 0
    total_found = 0
    total_engine_failures = 0
    total_inference_needed = 0
    query_latencies: list[float] = []
    ingest_latencies: list[float] = []

    stop = False
    for conv_idx, conv_data in enumerate(data):
        if stop:
            break
        conv = conv_data["conversation"]
        qa_list = conv_data["qa"]

        conv_db = Path(f"{db_path}.{conv_idx}")
        reset_db(conv_db)
        engine = Engine(
            db_path=str(conv_db),
            user_id=f"locomo_{conv_idx}",
            context_window=8,
            semantic_dedup=False,
            semantic_search_mode="disabled",
            local_only=True,
        )

        messages = []
        raw_corpus = []
        for key in sorted(conv.keys()):
            if not key.startswith("session_") or key.endswith("_time") or key.endswith("_date_time"):
                continue
            session = conv.get(key)
            if not session or not isinstance(session, list):
                continue
            date_key = f"{key}_date_time"
            session_dt = parse_session_dt(conv.get(date_key, ""))
            date_label = session_dt.strftime("%-d %B %Y") if session_dt else ""
            for turn in session:
                speaker = turn.get("speaker", "")
                text = turn.get("text", "")
                if session_dt:
                    text = resolve_dates(text, session_dt)
                if date_label:
                    text = f"[{date_label}] {text}"
                messages.append({"speaker": speaker, "text": text})
                raw_corpus.append(f"{speaker}: {text}")

        t0 = time.perf_counter()
        engine.store_batch(messages)
        ingest_latencies.append((time.perf_counter() - t0) * 1000)
        full_corpus = "\n".join(raw_corpus)

        for qa_idx, qa in enumerate(qa_list):
            if limit_questions is not None and total_questions >= limit_questions:
                stop = True
                break
            question = qa.get("question", "")
            answer_gt = qa.get("answer", "")
            category = qa.get("category", 0)
            if not question or answer_gt == "":
                continue
            if category == 5:
                continue

            total_questions += 1
            cat_name = category_names.get(category, f"cat-{category}")
            start = time.perf_counter()
            result = engine.search(question, limit=20, max_context=120, full_context_threshold=0)
            elapsed_ms = (time.perf_counter() - start) * 1000
            query_latencies.append(elapsed_ms)
            context = "\n".join(f"{m.speaker}: {m.text}" for m in result.messages)

            found, search_ratio = answer_in_context(answer_gt, context)
            found_by_cat[cat_name].append(1 if found else 0)
            if found:
                total_found += 1
            else:
                corpus_found, _ = answer_in_context(answer_gt, full_corpus)
                if corpus_found:
                    total_engine_failures += 1
                    engine_failures_by_cat[cat_name] += 1
                else:
                    total_inference_needed += 1
                    inference_by_cat[cat_name] += 1

            slowest_queries.append(
                {
                    "conv": conv_idx,
                    "qa_index": qa_idx + 1,
                    "category": cat_name,
                    "question": question,
                    "answer": str(answer_gt),
                    "elapsed_ms": round(elapsed_ms, 3),
                    "engine_query_ms": round(result.query_ms, 3),
                    "found": found,
                    "match_ratio": round(search_ratio, 3),
                    "retrieved_messages": len(result.messages),
                }
            )

        engine.close()

    slowest_queries.sort(key=lambda item: item["elapsed_ms"], reverse=True)

    per_category = {}
    for cat, values in sorted(found_by_cat.items()):
        total = len(values)
        found = sum(values)
        per_category[cat] = {
            "questions": total,
            "found": found,
            "retrieval_recall": round(found / total, 4) if total else 0.0,
            "engine_failures": engine_failures_by_cat.get(cat, 0),
            "inference_needed": inference_by_cat.get(cat, 0),
        }

    return {
        "suite": "locomo_retrieval",
        "questions_evaluated": total_questions,
        "answers_in_context": total_found,
        "retrieval_recall": round(total_found / total_questions, 4) if total_questions else 0.0,
        "engine_failures": total_engine_failures,
        "inference_needed": total_inference_needed,
        "ingest_latency_ms": {
            "avg": round(statistics.mean(ingest_latencies), 3) if ingest_latencies else 0.0,
            "p50": round(percentile(ingest_latencies, 50), 3) if ingest_latencies else 0.0,
            "max": round(max(ingest_latencies), 3) if ingest_latencies else 0.0,
        },
        "query_latency_ms": {
            "avg": round(statistics.mean(query_latencies), 3) if query_latencies else 0.0,
            "p50": round(percentile(query_latencies, 50), 3) if query_latencies else 0.0,
            "p95": round(percentile(query_latencies, 95), 3) if query_latencies else 0.0,
            "p99": round(percentile(query_latencies, 99), 3) if query_latencies else 0.0,
            "max": round(max(query_latencies), 3) if query_latencies else 0.0,
        },
        "per_category": per_category,
        "slowest_queries": slowest_queries[:15],
    }


def build_noise_messages(count: int) -> list[dict]:
    rng = random.Random(42)
    words = [
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
        "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey", "xray",
        "yankee", "zulu", "adapter", "router", "token", "render", "layout", "memory",
        "agent", "swarm", "context", "approval", "sandbox", "provider", "workspace",
    ]
    speakers = ["user", "assistant", "system", "agent"]
    messages = []
    for i in range(count):
        token_count = rng.randint(12, 30)
        text = " ".join(rng.choice(words) for _ in range(token_count))
        text = f"noise-{i} {text}"
        messages.append({"speaker": rng.choice(speakers), "text": text})
    return messages


def run_scale_suite(noise_messages: int, queries_per_pass: int) -> dict:
    db_path = make_temp_db("scale_suite")
    reset_db(db_path)
    engine = Engine(
        db_path=str(db_path),
        user_id="scale_suite",
        context_window=2,
        semantic_dedup=False,
        semantic_search_mode="disabled",
        local_only=True,
    )

    targets = [
        {"speaker": "target", "text": "Project Helios runs on SQLite FTS5 and semantic search with SBERT embeddings."},
        {"speaker": "target", "text": "The release approver for Project Helios is Mira Hart."},
        {"speaker": "target", "text": "The backend hardening owner is Otto Vale."},
        {"speaker": "target", "text": "The semantic context assembler function is named build_context_envelope."},
        {"speaker": "target", "text": "Pricing moved from 79 dollars to 99 dollars in March 2026."},
        {"speaker": "target", "text": "Kickboxing is William's chosen martial art for conditioning."},
    ]
    noise = build_noise_messages(noise_messages)
    corpus = noise + targets
    random.Random(1337).shuffle(corpus)

    t0 = time.perf_counter()
    stored = engine.store_batch(corpus)
    ingest_ms = (time.perf_counter() - t0) * 1000

    queries = [
        ("db_stack", "What retrieval stack does Project Helios use?", "Project Helios runs on SQLite FTS5 and semantic search with SBERT embeddings."),
        ("approver", "Who approves Project Helios releases?", "The release approver for Project Helios is Mira Hart."),
        ("owner", "Who owns backend hardening?", "The backend hardening owner is Otto Vale."),
        ("semantic_gap", "What martial art does William train?", "Kickboxing is William's chosen martial art for conditioning."),
        ("function", "Which function assembles semantic context?", "The semantic context assembler function is named build_context_envelope."),
        ("pricing", "What is the latest pricing?", "Pricing moved from 79 dollars to 99 dollars in March 2026."),
    ]

    all_timings = []
    case_results = []
    for name, query, expected in queries:
        timings = []
        hit_count = 0
        first_query_ms = None
        for idx in range(queries_per_pass):
            start = time.perf_counter()
            result = engine.search(query, limit=12, max_context=24, full_context_threshold=0)
            elapsed_ms = (time.perf_counter() - start) * 1000
            if idx == 0:
                first_query_ms = elapsed_ms
            timings.append(elapsed_ms)
            all_timings.append(elapsed_ms)
            context = "\n".join(f"{m.speaker}: {m.text}" for m in result.messages)
            found, _ = answer_in_context(expected, context, threshold=0.75)
            if found:
                hit_count += 1
        case_results.append(
            {
                "name": name,
                "query": query,
                "hit_rate": round(hit_count / queries_per_pass, 4),
                "first_query_ms": round(first_query_ms or 0.0, 3),
                "avg_ms": round(statistics.mean(timings), 3),
                "p95_ms": round(percentile(timings, 95), 3),
            }
        )

    stats = engine.stats()
    engine.close()

    return {
        "suite": "scale",
        "noise_messages": noise_messages,
        "stored_messages": stored,
        "db_size_bytes": stats["db_size_bytes"],
        "ingest_ms": round(ingest_ms, 3),
        "throughput_msgs_per_sec": round(stored / max(ingest_ms / 1000.0, 1e-6), 2),
        "query_latency_ms": {
            "avg": round(statistics.mean(all_timings), 3),
            "p50": round(percentile(all_timings, 50), 3),
            "p95": round(percentile(all_timings, 95), 3),
            "p99": round(percentile(all_timings, 99), 3),
            "max": round(max(all_timings), 3),
        },
        "cases": case_results,
    }


def evaluate_gates(
    summary: dict,
    *,
    min_custom_pass_rate: float | None = None,
    require_time_filter: bool = False,
    min_locomo_recall: float | None = None,
    min_locomo_answers: int | None = None,
    max_locomo_p95_ms: float | None = None,
    min_scale_stored_messages: int | None = None,
    min_scale_hit_rate: float | None = None,
    max_scale_p95_ms: float | None = None,
) -> list[str]:
    """Return human-readable gate failures for a completed report."""

    failures: list[str] = []
    custom = summary["custom_complex"]
    if (
        min_custom_pass_rate is not None
        and float(custom["pass_rate"]) < min_custom_pass_rate
    ):
        failures.append(
            "custom pass rate "
            f"{float(custom['pass_rate']):.4f} < {min_custom_pass_rate:.4f}"
        )
    if require_time_filter and not custom["time_filter_check"]["passed"]:
        failures.append("custom time-filter check failed")

    locomo = summary.get("locomo_retrieval")
    locomo_gate_configured = any(
        value is not None
        for value in (min_locomo_recall, min_locomo_answers, max_locomo_p95_ms)
    )
    if locomo is None and locomo_gate_configured:
        failures.append("LoCoMo suite was skipped while LoCoMo gates were configured")
    elif locomo is not None:
        if (
            min_locomo_recall is not None
            and float(locomo["retrieval_recall"]) < min_locomo_recall
        ):
            failures.append(
                "LoCoMo recall "
                f"{float(locomo['retrieval_recall']):.4f} < {min_locomo_recall:.4f}"
            )
        if (
            min_locomo_answers is not None
            and int(locomo["answers_in_context"]) < min_locomo_answers
        ):
            failures.append(
                "LoCoMo answers in context "
                f"{int(locomo['answers_in_context'])} < {min_locomo_answers}"
            )
        locomo_p95 = float(locomo["query_latency_ms"]["p95"])
        if max_locomo_p95_ms is not None and locomo_p95 > max_locomo_p95_ms:
            failures.append(
                f"LoCoMo query p95 {locomo_p95:.3f}ms > {max_locomo_p95_ms:.3f}ms"
            )

    scale = summary["scale"]
    if (
        min_scale_stored_messages is not None
        and int(scale["stored_messages"]) < min_scale_stored_messages
    ):
        failures.append(
            "scale stored-message count "
            f"{int(scale['stored_messages'])} < {min_scale_stored_messages}"
        )
    if min_scale_hit_rate is not None:
        failing_cases = [
            case
            for case in scale["cases"]
            if float(case["hit_rate"]) < min_scale_hit_rate
        ]
        if failing_cases:
            rendered = ", ".join(
                f"{case['name']}={float(case['hit_rate']):.4f}"
                for case in failing_cases
            )
            failures.append(
                f"scale hit rate below {min_scale_hit_rate:.4f}: {rendered}"
            )
    scale_p95 = float(scale["query_latency_ms"]["p95"])
    if max_scale_p95_ms is not None and scale_p95 > max_scale_p95_ms:
        failures.append(
            f"scale query p95 {scale_p95:.3f}ms > {max_scale_p95_ms:.3f}ms"
        )
    return failures


def _validate_gate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    for name in ("min_custom_pass_rate", "min_locomo_recall", "min_scale_hit_rate"):
        value = getattr(args, name)
        if value is not None and not 0.0 <= value <= 1.0:
            parser.error(f"--{name.replace('_', '-')} must be between 0 and 1")
    for name in ("max_locomo_p95_ms", "max_scale_p95_ms"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    for name in ("min_locomo_answers", "min_scale_stored_messages"):
        value = getattr(args, name)
        if value is not None and value < 0:
            parser.error(f"--{name.replace('_', '-')} must not be negative")


def main() -> int:
    parser = argparse.ArgumentParser(description="Complex test suite for NarratorDB.")
    parser.add_argument("--locomo-limit", type=int, default=None, help="Optional cap on LoCoMo questions.")
    parser.add_argument("--skip-locomo", action="store_true", help="Skip the LoCoMo retrieval section.")
    parser.add_argument("--noise-messages", type=int, default=5000, help="Noise messages for scale suite.")
    parser.add_argument("--queries-per-pass", type=int, default=25, help="Repeated searches per scale query.")
    parser.add_argument("--output", type=str, default="", help="Optional JSON output path.")
    parser.add_argument("--min-custom-pass-rate", type=float)
    parser.add_argument("--require-time-filter", action="store_true")
    parser.add_argument("--min-locomo-recall", type=float)
    parser.add_argument("--min-locomo-answers", type=int)
    parser.add_argument("--max-locomo-p95-ms", type=float)
    parser.add_argument("--min-scale-stored-messages", type=int)
    parser.add_argument("--min-scale-hit-rate", type=float)
    parser.add_argument("--max-scale-p95-ms", type=float)
    args = parser.parse_args()
    _validate_gate_arguments(parser, args)

    random.seed(12345)

    started_at = time.time()
    custom = run_custom_complex_suite()
    locomo = None if args.skip_locomo else run_locomo_retrieval(args.locomo_limit)
    scale = run_scale_suite(args.noise_messages, args.queries_per_pass)

    summary = {
        "engine": "NarratorDB",
        "engine_path": str(ROOT / "narratordb" / "engine.py"),
        "started_at": started_at,
        "completed_at": time.time(),
        "custom_complex": custom,
        "locomo_retrieval": locomo,
        "scale": scale,
    }
    failures = evaluate_gates(
        summary,
        min_custom_pass_rate=args.min_custom_pass_rate,
        require_time_filter=args.require_time_filter,
        min_locomo_recall=args.min_locomo_recall,
        min_locomo_answers=args.min_locomo_answers,
        max_locomo_p95_ms=args.max_locomo_p95_ms,
        min_scale_stored_messages=args.min_scale_stored_messages,
        min_scale_hit_rate=args.min_scale_hit_rate,
        max_scale_p95_ms=args.max_scale_p95_ms,
    )
    summary["gates"] = {
        "passed": not failures,
        "configured": {
            "min_custom_pass_rate": args.min_custom_pass_rate,
            "require_time_filter": args.require_time_filter,
            "min_locomo_recall": args.min_locomo_recall,
            "min_locomo_answers": args.min_locomo_answers,
            "max_locomo_p95_ms": args.max_locomo_p95_ms,
            "min_scale_stored_messages": args.min_scale_stored_messages,
            "min_scale_hit_rate": args.min_scale_hit_rate,
            "max_scale_p95_ms": args.max_scale_p95_ms,
        },
        "failures": failures,
    }

    if args.output:
        output_path = Path(args.output)
    else:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        output_path = TMP_DIR / f"narratordb_complex_test_{stamp}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2))

    print(json.dumps(summary, indent=2))
    print(f"\nSaved report to {output_path}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
