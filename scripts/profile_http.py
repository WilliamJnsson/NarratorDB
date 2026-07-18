"""Profile the official HTTP search path with per-stage timings retained.

Dev-only tool (not shipped in the wheel). Replays every dataset question
against a running or self-started benchmark_server and keeps the
query_debug payload the official client discards, so the end-to-end
latency can be decomposed into engine stages vs HTTP/queue overhead.

Usage:
    python3 scripts/profile_http.py \
        --database /path/to/benchmark.db \
        --dataset datasets/longmemeval_s_cleaned.json \
        --workers 10 --limit 200 --output reports/profile_http.json

Only question text and user scopes are read from the dataset; answers and
evidence labels are never loaded.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[index]


def summarize(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "p95": round(percentile(values, 95), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def load_questions(dataset_path: str) -> dict[str, str]:
    """question_id -> question text. Never reads answers or evidence labels."""
    with open(dataset_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return {str(item["question_id"]): str(item["question"]) for item in data}


def scope_map(database: str) -> dict[str, str]:
    """question_id -> stored user scope (longmemeval_{qid}_{run_id})."""
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        scopes = [row[0] for row in conn.execute("SELECT DISTINCT user_id FROM messages")]
    finally:
        conn.close()
    mapping = {}
    for scope in scopes:
        if scope.startswith("longmemeval_"):
            remainder = scope[len("longmemeval_"):]
            question_id = remainder.rsplit("_", 1)[0]
            mapping[question_id] = scope
    return mapping


def wait_for_health(base_url: str, timeout_secs: float = 60.0) -> None:
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2):
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"benchmark_server at {base_url} did not become healthy")


def run_search(base_url: str, user_id: str, query: str, limit: int) -> dict:
    payload = json.dumps({"user_id": user_id, "query": query, "limit": limit}).encode()
    request = urllib.request.Request(
        f"{base_url}/search", data=payload, headers={"Content-Type": "application/json"}
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.load(response)
    client_ms = (time.perf_counter() - started) * 1000
    debug = body.get("query_debug") or {}
    timings = debug.get("timings_ms") or {}
    return {
        "client_ms": client_ms,
        "backend_ms": float(debug.get("backend_ms") or 0.0),
        "result_count": len(body.get("results") or []),
        "stages": {key: float(value) for key, value in timings.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--external-server", action="store_true",
        help="Assume a benchmark_server is already running; do not spawn one.",
    )
    args = parser.parse_args()

    questions = load_questions(args.dataset)
    scopes = scope_map(args.database)
    pairs = [(qid, scopes[qid], questions[qid]) for qid in questions if qid in scopes]
    missing = len(questions) - len(pairs)
    print(f"Replaying {len(pairs)} questions ({missing} without an ingested scope)")
    if not pairs:
        print("No matching scopes found in database — was it built by the official harness?")
        return 1

    base_url = f"http://{args.host}:{args.port}"
    server = None
    if not args.external_server:
        server = subprocess.Popen(
            [sys.executable, "-m", "narratordb.benchmark_server",
             "--host", args.host, "--port", str(args.port), "--database", args.database],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    try:
        wait_for_health(base_url)
        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [
                pool.submit(run_search, base_url, scope, question, args.limit)
                for _, scope, question in pairs
            ]
            for future in futures:
                results.append(future.result())
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=10)

    stage_names = sorted({name for entry in results for name in entry["stages"]})
    report = {
        "questions": len(results),
        "workers": args.workers,
        "limit": args.limit,
        "client_ms": summarize([entry["client_ms"] for entry in results]),
        "backend_ms": summarize([entry["backend_ms"] for entry in results]),
        "http_queue_overhead_ms": summarize(
            [entry["client_ms"] - entry["backend_ms"] for entry in results]
        ),
        "stages_ms": {
            name: summarize([e["stages"][name] for e in results if name in e["stages"]])
            for name in stage_names
        },
        "result_count": summarize([float(entry["result_count"]) for entry in results]),
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
