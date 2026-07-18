#!/usr/bin/env python3
"""Advanced mixed-system validation for NarratorDB."""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from narratordb.engine import Engine

ROOT = Path(__file__).resolve().parents[2]

BRIDGE_PATH = ROOT / "narratordb" / "stdio.py"


MESSAGE_ROWS = [
    {
        "speaker": "Claude Strategist",
        "text": "Claude mapped the backend hardening plan in task-compiler-service.ts and prioritized auth rotation first.",
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "architect",
            "run_id": "run-auth-01",
            "workspace_id": "narratordb",
            "response_id": "resp-claude-101",
            "metadata": {"role": "planner", "topic": "backend-hardening"},
        },
    },
    {
        "speaker": "GPT Operator",
        "text": "GPT moved staging secrets into Doppler and documented the rollback runbook in deploy-worker.ts.",
        "provenance": {
            "provider": "openai",
            "model_id": "gpt-5",
            "agent_id": "ops",
            "run_id": "run-auth-01",
            "workspace_id": "narratordb",
            "response_id": "resp-gpt-201",
            "metadata": {"role": "operations", "system": "doppler"},
        },
    },
    {
        "speaker": "Qwen Reviewer",
        "text": "Qwen confirmed final approval after verifying rollback coverage and deployment safety checks.",
        "provenance": {
            "provider": "qwen",
            "model_id": "qwen-2.5-coder-32b",
            "agent_id": "reviewer",
            "run_id": "run-auth-01",
            "workspace_id": "narratordb",
            "response_id": "resp-qwen-301",
            "metadata": {"role": "reviewer", "stage": "approval"},
        },
    },
    {
        "speaker": "Claude Backend",
        "text": "Claude patched buildContextEnvelope in provider-service.ts so compare fanout receives clipped memory payloads.",
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "backend",
            "run_id": "run-memory-02",
            "workspace_id": "narratordb",
            "response_id": "resp-claude-102",
            "metadata": {"role": "coder", "file": "provider-service.ts"},
        },
    },
    {
        "speaker": "GPT Planner",
        "text": "GPT routed the prompt fanout through processBatchJob for provider account balancing.",
        "provenance": {
            "provider": "openai",
            "model_id": "gpt-5",
            "agent_id": "planner",
            "run_id": "run-batch-03",
            "workspace_id": "narratordb",
            "response_id": "resp-gpt-202",
            "metadata": {"role": "planner", "feature": "batch"},
        },
    },
    {
        "speaker": "Qwen Reviewer",
        "text": "Qwen flagged the missing Content-Security-Policy header in renderComparePopout before release.",
        "provenance": {
            "provider": "qwen",
            "model_id": "qwen-2.5-coder-32b",
            "agent_id": "reviewer",
            "run_id": "run-memory-02",
            "workspace_id": "narratordb",
            "response_id": "resp-qwen-302",
            "metadata": {"role": "reviewer", "issue": "csp"},
        },
    },
    {
        "speaker": "Claude Pricing",
        "text": "NarratorDB pricing was 79 dollars per month in January 2026.",
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "pricing",
            "run_id": "run-pricing-00",
            "workspace_id": "narratordb",
            "response_id": "resp-claude-104",
        },
    },
    {
        "speaker": "Claude Pricing",
        "text": "NarratorDB pricing changed to 99 dollars per month on 10 March 2026.",
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "pricing",
            "run_id": "run-pricing-01",
            "workspace_id": "narratordb",
            "response_id": "resp-claude-103",
        },
    },
]


ARTIFACT_ROWS = [
    {
        "kind": "diff",
        "title": "provider-service memory clipping patch",
        "summary": "Patch to clip memory context before compare fanout.",
        "content": "diff --git a/src/main/services/provider-service.ts b/src/main/services/provider-service.ts\n+ buildContextEnvelope now trims memory blocks before compare fanout",
        "tags": ["code", "diff", "provider-service.ts"],
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "backend",
            "run_id": "run-memory-02",
            "workspace_id": "narratordb",
            "response_id": "artifact-claude-401",
            "metadata": {"file": "provider-service.ts"},
        },
    },
    {
        "kind": "review_verdict",
        "title": "compare popout CSP verdict",
        "summary": "Reviewer found missing CSP header in compare popout path.",
        "content": "Verdict: fail until renderComparePopout response path adds Content-Security-Policy.",
        "tags": ["review", "security", "compare"],
        "provenance": {
            "provider": "qwen",
            "model_id": "qwen-2.5-coder-32b",
            "agent_id": "reviewer",
            "run_id": "run-memory-02",
            "workspace_id": "narratordb",
            "response_id": "artifact-qwen-402",
        },
    },
    {
        "kind": "web_source",
        "title": "Anthropic web search tool doc",
        "summary": "Source for Claude web search support.",
        "content": "Anthropic documents web search tool use for Claude in agent workflows.",
        "tags": ["source", "web-search", "anthropic"],
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "researcher",
            "run_id": "run-research-04",
            "workspace_id": "narratordb",
            "tool_used": "web_search",
            "response_id": "artifact-claude-403",
        },
    },
    {
        "kind": "plan",
        "title": "batch routing rollout",
        "summary": "Plan to spread prompts across provider accounts with processBatchJob.",
        "content": "Use processBatchJob in batch-job-service.ts to distribute requests across provider accounts safely.",
        "tags": ["plan", "batch", "providers"],
        "provenance": {
            "provider": "openai",
            "model_id": "gpt-5",
            "agent_id": "planner",
            "run_id": "run-batch-03",
            "workspace_id": "narratordb",
            "response_id": "artifact-gpt-404",
        },
    },
]


def noise_messages(count: int):
    speakers = ["Claude Noise", "GPT Noise", "Qwen Noise"]
    modules = ["auth", "memory", "compare", "batch", "deploy", "search"]
    actions = ["logged", "captured", "stored", "retried", "observed", "queued"]
    details = ["metrics", "events", "snapshots", "warnings", "notes", "buffers"]
    providers = ["anthropic", "openai", "qwen"]
    rows = []
    for index in range(count):
        provider = random.choice(providers)
        rows.append(
            {
                "speaker": random.choice(speakers),
                "text": f"Noise {random.choice(modules)} worker {random.choice(actions)} background {random.choice(details)} row {index}.",
                "provenance": {
                    "provider": provider,
                    "model_id": f"{provider}-noise-{index % 7}",
                    "agent_id": f"noise-agent-{index % 9}",
                    "run_id": f"noise-run-{index % 11}",
            "workspace_id": "narratordb",
                },
            }
        )
    return rows


def noise_artifacts(count: int):
    kinds = ["note", "trace", "snapshot", "report"]
    rows = []
    for index in range(count):
        rows.append(
            {
                "kind": random.choice(kinds),
                "title": f"noise artifact {index}",
                "summary": "synthetic artifact for benchmark noise",
                "content": f"Noise artifact payload {index} with unrelated benchmark content.",
                "tags": ["noise"],
                "provenance": {
                    "provider": random.choice(["anthropic", "openai", "qwen"]),
                    "model_id": f"noise-model-{index % 5}",
                    "agent_id": f"noise-artifact-{index % 6}",
                    "run_id": f"noise-run-{index % 13}",
            "workspace_id": "narratordb",
                },
            }
        )
    return rows


def bridge_request(proc, method, params):
    proc.stdin.write(json.dumps({"id": method, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError(f"no response for {method}")
    payload = json.loads(line)
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or f"{method} failed")
    return payload["result"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--noise-messages", type=int, default=5000)
    parser.add_argument("--noise-artifacts", type=int, default=1200)
    parser.add_argument("--passes", type=int, default=20)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "memory.db")
        engine = Engine(db_path=db_path, user_id="advanced-bench", semantic_dedup=True)

        ts = time.time() - 500_000
        message_batch = []
        pricing_now_ts = None
        for row in MESSAGE_ROWS + noise_messages(args.noise_messages):
            ts += 1
            if row["text"].startswith("NarratorDB pricing changed to 99 dollars"):
                pricing_now_ts = ts
            message_batch.append(
                {
                    "speaker": row["speaker"],
                    "text": row["text"],
                    "timestamp": ts,
                    "provenance": row["provenance"],
                }
            )
        engine.store_batch(message_batch)

        for artifact in ARTIFACT_ROWS + noise_artifacts(args.noise_artifacts):
            ts += 1
            engine.store_artifact(
                kind=artifact["kind"],
                title=artifact["title"],
                content=artifact["content"],
                summary=artifact.get("summary", ""),
                tags=artifact.get("tags"),
                timestamp=ts,
                provenance=artifact.get("provenance"),
            )

        pricing_cutoff = pricing_now_ts - 0.5 if pricing_now_ts is not None else None

        checks = [
            {
                "name": "exact_message_lookup",
                "run": lambda: engine.search("who confirmed final approval", limit=8, max_context=12, full_context_threshold=0),
                "assert": lambda result: "final approval" in result.messages[0].text.lower() and result.messages[0].provenance.get("provider") == "qwen",
            },
            {
                "name": "semantic_coding_lookup",
                "run": lambda: engine.search("which module deals with pop out compare rendering", limit=8, max_context=12, full_context_threshold=0),
                "assert": lambda result: "rendercomparepopout" in result.messages[0].text.lower(),
            },
            {
                "name": "provider_filter",
                "run": lambda: engine.search(
                    "who proposed auth hardening",
                    limit=8,
                    max_context=12,
                    full_context_threshold=0,
                    filters={"provider": "anthropic", "agent_id": "architect"},
                ),
                "assert": lambda result: result.messages[0].provenance.get("provider") == "anthropic" and result.messages[0].provenance.get("agent_id") == "architect",
            },
            {
                "name": "run_filter",
                "run": lambda: engine.search(
                    "which model routed the prompt fanout",
                    limit=8,
                    max_context=12,
                    full_context_threshold=0,
                    filters={"run_id": "run-batch-03"},
                ),
                "assert": lambda result: result.messages[0].provenance.get("run_id") == "run-batch-03",
            },
            {
                "name": "time_range",
                "run": lambda: engine.search(
                    "narratordb pricing",
                    limit=8,
                    max_context=10,
                    full_context_threshold=0,
                    after=pricing_cutoff,
                ),
                "assert": lambda result: (
                    result.direct_hits
                    and "99 dollars" in result.direct_hits[0].text.lower()
                    and all("79 dollars" not in message.text.lower() for message in result.messages)
                ),
            },
            {
                "name": "artifact_diff_lookup",
                "run": lambda: engine.search_artifacts("memory clipping patch", limit=5, filters={"kind": "diff"}),
                "assert": lambda result: result.artifacts and result.artifacts[0].kind == "diff" and "provider-service.ts" in result.artifacts[0].content,
            },
            {
                "name": "artifact_provider_filter",
                "run": lambda: engine.search_artifacts(
                    "content security policy header",
                    limit=5,
                    filters={"provider": "qwen", "kind": "review_verdict"},
                ),
                "assert": lambda result: result.artifacts and result.artifacts[0].provenance.get("provider") == "qwen",
            },
        ]

        failures = []
        latencies = {check["name"]: [] for check in checks}
        samples = {}
        for _ in range(args.passes):
            for check in checks:
                started = time.perf_counter()
                result = check["run"]()
                elapsed_ms = (time.perf_counter() - started) * 1000
                latencies[check["name"]].append(elapsed_ms)
                ok = check["assert"](result)
                if not ok:
                    failures.append({"name": check["name"], "result": str(result)})
                if check["name"] not in samples:
                    if hasattr(result, "messages"):
                        samples[check["name"]] = {
                            "top_text": result.messages[0].text if result.messages else None,
                            "provenance": result.messages[0].provenance if result.messages else {},
                            "query_ms": round(result.query_ms, 3),
                        }
                    else:
                        samples[check["name"]] = {
                            "top_title": result.artifacts[0].title if result.artifacts else None,
                            "kind": result.artifacts[0].kind if result.artifacts else None,
                            "provenance": result.artifacts[0].provenance if result.artifacts else {},
                            "query_ms": round(result.query_ms, 3),
                        }

        env = os.environ.copy()
        env["NARRATORDB_PATH"] = db_path
        env["NARRATORDB_USER_ID"] = "advanced-bench"
        bridge = subprocess.Popen(
            [sys.executable, str(BRIDGE_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            bridge_context = bridge_request(
                bridge,
                "context",
                {
                    "query": "compare popout review and memory clipping patch",
                    "limit": 10,
                    "max_context": 14,
                    "artifact_limit": 3,
                    "filters": {"workspace_id": "narratordb"},
                },
            )
        finally:
            bridge.stdin.close()
            bridge.terminate()
            bridge.wait(timeout=5)

        bridge_ok = (
            "## Related Artifacts" in bridge_context["context"]
            and "Claude Backend [anthropic / claude-sonnet-4 / backend / run-memory-02" in bridge_context["context"]
            and bridge_context.get("artifacts")
        )
        if not bridge_ok:
            failures.append({"name": "bridge_context", "result": bridge_context["context"]})

        summary = []
        for name, values in latencies.items():
            ordered = sorted(values)
            summary.append(
                {
                    "name": name,
                    "avg_ms": round(statistics.mean(values), 3),
                    "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3),
                    "sample": samples.get(name),
                }
            )

        report = {
            "passed": not failures,
            "failures": failures,
            "summary": summary,
            "bridge_context_ok": bridge_ok,
            "bridge_context_preview": bridge_context["context"].splitlines()[:12],
            "stats": engine.stats(),
            "noise_messages": args.noise_messages,
            "noise_artifacts": args.noise_artifacts,
            "passes": args.passes,
        }

        print(json.dumps(report, indent=2))
        engine.close()
        raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
