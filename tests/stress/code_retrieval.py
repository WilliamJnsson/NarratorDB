#!/usr/bin/env python3
"""Coding-heavy direct-query test for NarratorDB."""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import tempfile
import time
from pathlib import Path

from narratordb.engine import Engine

ROOT = Path(__file__).resolve().parents[2]



CANONICAL_RECORDS = [
    ("code", "The provider context envelope is built in src/main/services/provider-service.ts inside buildContextEnvelope()."),
    ("code", "NARRATORDB_PATH is read in narratordb/config.py before Engine starts."),
    ("code", "The /v2/memory/context route lives in python/server.py and calls recall_context()."),
    ("code", "handleRunSwarm in src/renderer/src/PreviewApp.tsx starts the swarm runtime flow."),
    ("code", "Rate limiting for login is configured by AUTH_RATE_LIMIT_PER_MINUTE in .env.production."),
    ("code", "The compare popout renderer lives in src/renderer/src/PreviewApp.tsx under renderComparePopout()."),
    ("code", "Swarm state persists through src/main/services/swarm-service.ts using saveSwarmSnapshot()."),
    ("code", "The NarratorDB JSONL interface is narratordb/stdio.py and the HTTP adapter is narratordb/benchmark_server.py."),
    ("code", "The claude provider adapter keeps web search capability flags in src/main/services/provider-capabilities.ts."),
    ("code", "The image attachment pipeline stores source metadata in src/main/services/attachment-service.ts using persistAttachmentSource()."),
    ("code", "The compileTask request path enters through src/main/services/task-compiler-service.ts and returns a structured task envelope."),
    ("code", "processBatchJob in src/main/services/batch-job-service.ts fans prompts across provider accounts."),
    ("code", "The agent reviewer verdict schema is declared in src/shared/contracts.ts as ReviewVerdict."),
    ("code", "The system prompt audit journal writes to src/main/services/memory-journal-service.ts in appendJournalEvent()."),
]


TARGETS = [
    {
        "query": "where is provider-service.ts",
        "must_include": ["provider-service.ts", "buildContextEnvelope"],
        "label": "path lookup",
    },
    {
        "query": "which file reads NARRATORDB_PATH",
        "must_include": ["narratordb/config.py"],
        "label": "env var lookup",
    },
    {
        "query": "where is /v2/memory/context implemented",
        "must_include": ["python/server.py", "recall_context"],
        "label": "route lookup",
    },
    {
        "query": "what function starts the swarm runtime flow",
        "must_include": ["handleRunSwarm", "PreviewApp.tsx"],
        "label": "function lookup",
    },
    {
        "query": "where is AUTH_RATE_LIMIT_PER_MINUTE configured",
        "must_include": ["AUTH_RATE_LIMIT_PER_MINUTE", ".env.production"],
        "label": "config lookup",
    },
    {
        "query": "which component renders compare popout",
        "must_include": ["renderComparePopout", "PreviewApp.tsx"],
        "label": "ui function lookup",
    },
    {
        "query": "where is swarm state saved",
        "must_include": ["swarm-service.ts", "saveSwarmSnapshot"],
        "label": "persistence lookup",
    },
    {
        "query": "where is the narrator database bridge",
        "must_include": ["narratordb/stdio.py"],
        "label": "bridge lookup",
    },
    {
        "query": "which file declares ReviewVerdict",
        "must_include": ["src/shared/contracts.ts", "ReviewVerdict"],
        "label": "type lookup",
    },
    {
        "query": "which service writes the system prompt audit journal",
        "must_include": ["memory-journal-service.ts", "appendJournalEvent"],
        "label": "journal lookup",
    },
    {
        "query": "where are image attachment sources persisted",
        "must_include": ["attachment-service.ts", "persistAttachmentSource"],
        "label": "attachment lookup",
    },
    {
        "query": "which service fans prompts across provider accounts",
        "must_include": ["batch-job-service.ts", "processBatchJob"],
        "label": "batch lookup",
    },
    {
        "query": "what module handles task envelope compilation",
        "must_include": ["task-compiler-service.ts", "structured task envelope"],
        "label": "compiler lookup",
    },
    {
        "query": "where are claude web search capabilities kept",
        "must_include": ["provider-capabilities.ts", "web search capability flags"],
        "label": "provider capability lookup",
    },
    {
        "query": "which function builds provider context envelope",
        "must_include": ["buildContextEnvelope", "provider-service.ts"],
        "label": "camelCase symbol lookup",
    },
    {
        "query": "which component handles compare pop out rendering",
        "must_include": ["renderComparePopout", "PreviewApp.tsx"],
        "label": "semantic ui phrasing",
    },
]


def generate_noise(count: int):
    modules = [
        "auth-service.ts",
        "deploy-worker.ts",
        "session-cache.ts",
        "api-router.ts",
        "metrics-reporter.ts",
        "prompt-router.ts",
    ]
    verbs = [
        "initializes",
        "records",
        "collects",
        "retries",
        "forwards",
        "hydrates",
        "caches",
    ]
    nouns = [
        "telemetry",
        "session state",
        "provider usage",
        "runtime events",
        "review notes",
        "memory spans",
    ]
    noise = []
    for index in range(count):
        module = random.choice(modules)
        verb = random.choice(verbs)
        noun = random.choice(nouns)
        noise.append(("noise", f"Noise module {module} {verb} {noun} for synthetic benchmark row {index}."))
    return noise


def run(noise_count: int, passes: int):
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "memory.db")
        engine = Engine(db_path=db_path, user_id="code_complex", semantic_dedup=True)
        timestamp = time.time() - 500_000
        batch = []
        for speaker, text in CANONICAL_RECORDS + generate_noise(noise_count):
            timestamp += 1
            batch.append({"speaker": speaker, "text": text, "timestamp": timestamp})
        engine.store_batch(batch)

        latencies: dict[str, list[float]] = {target["query"]: [] for target in TARGETS}
        examples = {}
        failures = []

        for _ in range(passes):
            for target in TARGETS:
                started = time.perf_counter()
                result = engine.search(target["query"], limit=8, max_context=18, full_context_threshold=0)
                elapsed_ms = (time.perf_counter() - started) * 1000
                latencies[target["query"]].append(elapsed_ms)
                top_text = result.messages[0].text if result.messages else ""
                examples[target["query"]] = {
                    "top": top_text,
                    "query_ms": round(result.query_ms, 3),
                    "direct_hit_count": len(result.direct_hits),
                    "context_count": len(result.context_messages),
                }
                if not all(needle.lower() in top_text.lower() for needle in target["must_include"]):
                    failures.append(
                        {
                            "query": target["query"],
                            "label": target["label"],
                            "expected": target["must_include"],
                            "top": top_text,
                        }
                    )

        semantic_probe = engine.search(
            "which module deals with pop out compare rendering",
            limit=8,
            max_context=18,
            full_context_threshold=0,
        )

        summary = []
        for target in TARGETS:
            values = latencies[target["query"]]
            ordered = sorted(values)
            summary.append(
                {
                    "query": target["query"],
                    "avg_ms": round(statistics.mean(values), 3),
                    "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3),
                    "min_ms": round(min(values), 3),
                    "max_ms": round(max(values), 3),
                    "top": examples[target["query"]]["top"],
                }
            )

        overall = {
            "passed": len(failures) == 0,
            "total_queries": len(TARGETS),
            "failures": failures,
            "summary": summary,
            "semantic_probe": {
                "query": "which module deals with pop out compare rendering",
                "top": semantic_probe.messages[0].text if semantic_probe.messages else "",
                "query_ms": round(semantic_probe.query_ms, 3),
            },
            "engine_stats": engine.stats(),
            "noise_count": noise_count,
            "passes": passes,
        }
        engine.close()
        return overall


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--noise", type=int, default=4000)
    parser.add_argument("--passes", type=int, default=20)
    args = parser.parse_args()
    result = run(args.noise, args.passes)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
