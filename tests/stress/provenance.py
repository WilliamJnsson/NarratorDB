#!/usr/bin/env python3
"""Multi-model provenance retention test for NarratorDB."""

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



RECORDS = [
    {
        "speaker": "Claude Strategist",
        "text": "Claude proposed auth hardening by enforcing stricter session rotation and input validation on admin routes.",
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "architect",
            "run_id": "run-auth-01",
            "workspace_id": "narratordb",
            "response_id": "resp-claude-001",
            "metadata": {"role": "planner", "topic": "auth-hardening"},
        },
    },
    {
        "speaker": "Qwen Reviewer",
        "text": "Qwen signed off the final approval handoff after confirming deployment safety and rollback coverage.",
        "provenance": {
            "provider": "qwen",
            "model_id": "qwen-2.5-coder-32b",
            "agent_id": "reviewer",
            "run_id": "run-auth-01",
            "workspace_id": "narratordb",
            "response_id": "resp-qwen-002",
            "metadata": {"role": "reviewer", "stage": "final-approval"},
        },
    },
    {
        "speaker": "GPT Operator",
        "text": "GPT updated Doppler secret references after the staging API key rotation and documented the rollback path.",
        "provenance": {
            "provider": "openai",
            "model_id": "gpt-5",
            "agent_id": "ops",
            "run_id": "run-auth-01",
            "workspace_id": "narratordb",
            "response_id": "resp-gpt-003",
            "metadata": {"role": "operations", "system": "doppler"},
        },
    },
    {
        "speaker": "Claude Backend",
        "text": "Claude patched buildContextEnvelope in provider-service.ts so memory context is clipped before compare fanout.",
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "backend",
            "run_id": "run-memory-02",
            "workspace_id": "narratordb",
            "response_id": "resp-claude-004",
            "metadata": {"role": "coder", "file": "provider-service.ts"},
        },
    },
    {
        "speaker": "Qwen Reviewer",
        "text": "Qwen flagged the missing Content-Security-Policy header in the compare popout response path.",
        "provenance": {
            "provider": "qwen",
            "model_id": "qwen-2.5-coder-32b",
            "agent_id": "reviewer",
            "run_id": "run-memory-02",
            "workspace_id": "narratordb",
            "response_id": "resp-qwen-005",
            "metadata": {"role": "reviewer", "issue": "csp"},
        },
    },
    {
        "speaker": "GPT Planner",
        "text": "GPT routed the batch job fanout through processBatchJob to spread prompts across provider accounts.",
        "provenance": {
            "provider": "openai",
            "model_id": "gpt-5",
            "agent_id": "planner",
            "run_id": "run-batch-03",
            "workspace_id": "narratordb",
            "response_id": "resp-gpt-006",
            "metadata": {"role": "planner", "feature": "batch"},
        },
    },
]


TESTS = [
    {
        "query": "who signed off the final approval handoff",
        "must_text": ["final approval", "deployment safety"],
        "must_provenance": {"provider": "qwen", "model_id": "qwen-2.5-coder-32b", "agent_id": "reviewer"},
    },
    {
        "query": "what did claude propose for auth hardening",
        "must_text": ["auth hardening", "session rotation"],
        "must_provenance": {"provider": "anthropic", "model_id": "claude-sonnet-4", "agent_id": "architect"},
    },
    {
        "query": "who updated doppler secret references",
        "must_text": ["Doppler", "rollback path"],
        "must_provenance": {"provider": "openai", "model_id": "gpt-5", "agent_id": "ops"},
    },
    {
        "query": "which model patched buildContextEnvelope",
        "must_text": ["buildContextEnvelope", "provider-service.ts"],
        "must_provenance": {"provider": "anthropic", "agent_id": "backend"},
    },
    {
        "query": "who flagged the missing content security policy header",
        "must_text": ["Content-Security-Policy", "compare popout"],
        "must_provenance": {"provider": "qwen", "model_id": "qwen-2.5-coder-32b"},
    },
    {
        "query": "which model routed the batch job fanout",
        "must_text": ["processBatchJob", "provider accounts"],
        "must_provenance": {"provider": "openai", "agent_id": "planner"},
    },
]


def noise_rows(count: int):
    modules = ["auth", "ops", "swarm", "compare", "memory", "batch"]
    verbs = ["logged", "observed", "noted", "captured", "stored", "retried"]
    nouns = ["events", "metrics", "outputs", "snapshots", "warnings", "summaries"]
    providers = ["anthropic", "openai", "qwen"]
    rows = []
    for index in range(count):
        provider = random.choice(providers)
        rows.append(
            {
                "speaker": f"{provider.title()} Noise",
                "text": f"Noise {random.choice(modules)} module {random.choice(verbs)} background {random.choice(nouns)} row {index}.",
                "provenance": {
                    "provider": provider,
                    "model_id": f"{provider}-noise-{index % 5}",
                    "agent_id": f"noise-{index % 7}",
                    "run_id": f"noise-run-{index % 11}",
            "workspace_id": "narratordb",
                },
            }
        )
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--noise", type=int, default=3000)
    parser.add_argument("--passes", type=int, default=20)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "memory.db")
        engine = Engine(db_path=db_path, user_id="provenance-bench", semantic_dedup=True)

        timestamp = time.time() - 250_000
        batch = []
        for row in RECORDS + noise_rows(args.noise):
            timestamp += 1
            batch.append(
                {
                    "speaker": row["speaker"],
                    "text": row["text"],
                    "timestamp": timestamp,
                    "provenance": row["provenance"],
                }
            )
        engine.store_batch(batch)

        latencies = {test["query"]: [] for test in TESTS}
        failures = []
        top_examples = {}

        for _ in range(args.passes):
            for test in TESTS:
                started = time.perf_counter()
                result = engine.search(test["query"], limit=8, max_context=16, full_context_threshold=0)
                elapsed_ms = (time.perf_counter() - started) * 1000
                latencies[test["query"]].append(elapsed_ms)
                if not result.messages:
                    failures.append({"query": test["query"], "reason": "no messages"})
                    continue

                top = result.messages[0]
                top_examples[test["query"]] = {
                    "text": top.text,
                    "provenance": top.provenance,
                    "query_ms": round(result.query_ms, 3),
                }

                if not all(fragment.lower() in top.text.lower() for fragment in test["must_text"]):
                    failures.append({"query": test["query"], "reason": "wrong text", "top": top.text})
                    continue

                for key, expected in test["must_provenance"].items():
                    actual = top.provenance.get(key)
                    if actual != expected:
                        failures.append(
                            {
                                "query": test["query"],
                                "reason": f"bad provenance for {key}",
                                "expected": expected,
                                "actual": actual,
                                "top": top.text,
                            }
                        )
                        break

        summary = []
        for test in TESTS:
            values = sorted(latencies[test["query"]])
            summary.append(
                {
                    "query": test["query"],
                    "avg_ms": round(statistics.mean(values), 3),
                    "p95_ms": round(values[max(0, int(len(values) * 0.95) - 1)], 3),
                    "top": top_examples.get(test["query"], {}).get("text"),
                    "provenance": top_examples.get(test["query"], {}).get("provenance"),
                }
            )

        report = {
            "passed": not failures,
            "total_queries": len(TESTS),
            "failures": failures,
            "summary": summary,
            "stats": engine.stats(),
            "noise_count": args.noise,
            "passes": args.passes,
        }

        print(json.dumps(report, indent=2))
        engine.close()
        raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
