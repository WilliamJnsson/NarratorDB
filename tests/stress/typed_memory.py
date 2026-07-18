#!/usr/bin/env python3
"""Advanced typed-memory validation for NarratorDB."""

from __future__ import annotations

import argparse
import json
import random
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


CODE_FILES = [
    {
        "path": "src/main/services/provider-service.ts",
        "content": """
export function buildContextEnvelope(memoryBlocks: string[]) {
  return memoryBlocks.filter(Boolean).slice(0, 12).join("\\n\\n");
}

export async function processBatchJob(providerAccounts: string[], prompts: string[]) {
  return providerAccounts.map((account, index) => ({ account, prompt: prompts[index] ?? null }));
}
""".strip(),
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "backend",
            "run_id": "run-memory-02",
            "workspace_id": "narratordb",
            "response_id": "code-claude-501",
            "metadata": {"file": "provider-service.ts"},
        },
    },
    {
        "path": "src/renderer/PreviewApp.tsx",
        "content": """
export function renderComparePopout(answer: string) {
  return `<section class="compare-popout">${answer}</section>`;
}

export function renderSummaryCard(summary: string) {
  return `<article class="summary-card">${summary}</article>`;
}
""".strip(),
        "provenance": {
            "provider": "qwen",
            "model_id": "qwen-2.5-coder-32b",
            "agent_id": "reviewer",
            "run_id": "run-memory-02",
            "workspace_id": "narratordb",
            "response_id": "code-qwen-502",
            "metadata": {"file": "PreviewApp.tsx"},
        },
    },
]


def noise_messages(count: int):
    speakers = ["Claude Noise", "GPT Noise", "Qwen Noise"]
    modules = ["auth", "memory", "compare", "batch", "deploy", "search", "agents", "editor"]
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
                    "agent_id": f"noise-agent-{index % 11}",
                    "run_id": f"noise-run-{index % 13}",
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
                "summary": "synthetic artifact for typed-memory benchmark noise",
                "content": f"Noise artifact payload {index} with unrelated benchmark content.",
                "tags": ["noise"],
                "provenance": {
                    "provider": random.choice(["anthropic", "openai", "qwen"]),
                    "model_id": f"noise-model-{index % 5}",
                    "agent_id": f"noise-artifact-{index % 6}",
                    "run_id": f"noise-run-{index % 17}",
            "workspace_id": "narratordb",
                },
            }
        )
    return rows


def noise_code_chunks(count: int):
    providers = ["anthropic", "openai", "qwen"]
    modules = ["cache", "router", "viewport", "agents", "search", "telemetry", "render", "queue"]
    verbs = ["record", "hydrate", "wire", "measure", "capture", "layout", "fanout", "marshal"]
    for index in range(count):
        provider = random.choice(providers)
        module = random.choice(modules)
        verb = random.choice(verbs)
        symbol = f"{verb}{module.title()}{index}"
        path = f"src/noise/{module}/module_{index % 137}.ts"
        content = f"export function {symbol}(payload: string) {{ return payload + '-{module}-{index}'; }}"
        yield {
            "path": path,
            "content": content,
            "symbol": symbol,
            "language": "typescript",
            "kind": "function",
            "summary": f"Noise function {symbol} in {path}",
            "tags": ["noise", module],
            "provenance": {
                "provider": provider,
                "model_id": f"{provider}-noise-code-{index % 7}",
                "agent_id": f"noise-code-{index % 9}",
                "run_id": f"noise-run-{index % 19}",
            "workspace_id": "narratordb",
            },
        }


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
    parser.add_argument("--noise-messages", type=int, default=6000)
    parser.add_argument("--noise-artifacts", type=int, default=1500)
    parser.add_argument("--noise-code", type=int, default=1800)
    parser.add_argument("--passes", type=int, default=15)
    args = parser.parse_args()

    random.seed(7)

    with tempfile.TemporaryDirectory(prefix="narratordb_typed_") as td:
        db_path = Path(td) / "typed-memory.db"
        engine = Engine(db_path=str(db_path), user_id="typed-test", context_window=5)

        message_ids = []
        for row in MESSAGE_ROWS:
            message_ids.append(engine.store(row["speaker"], row["text"], provenance=row.get("provenance")))

        artifact_ids = []
        for artifact in ARTIFACT_ROWS:
            artifact_ids.append(
                engine.store_artifact(
                    kind=artifact["kind"],
                    title=artifact["title"],
                    content=artifact["content"],
                    summary=artifact["summary"],
                    tags=artifact["tags"],
                    provenance=artifact["provenance"],
                )
            )

        for row in noise_messages(args.noise_messages):
            engine.store(row["speaker"], row["text"], provenance=row.get("provenance"))
        for artifact in noise_artifacts(args.noise_artifacts):
            engine.store_artifact(
                kind=artifact["kind"],
                title=artifact["title"],
                content=artifact["content"],
                summary=artifact["summary"],
                tags=artifact["tags"],
                provenance=artifact["provenance"],
            )

        for file_def in CODE_FILES:
            engine.ingest_code_file(
                path=file_def["path"],
                content=file_def["content"],
                provenance=file_def["provenance"],
            )
        for chunk in noise_code_chunks(args.noise_code):
            engine.store_code_chunk(
                path=chunk["path"],
                content=chunk["content"],
                symbol=chunk["symbol"],
                language=chunk["language"],
                kind=chunk["kind"],
                summary=chunk["summary"],
                tags=chunk["tags"],
                provenance=chunk["provenance"],
            )

        build_chunk = engine.search_code("build context envelope compare fanout", limit=3).chunks[0]
        render_chunk = engine.search_code("render compare popout", limit=3).chunks[0]
        batch_chunk = engine.search_code("process batch job provider account balancing", limit=3).chunks[0]

        diff_artifact_id = artifact_ids[0]
        verdict_artifact_id = artifact_ids[1]
        plan_artifact_id = artifact_ids[2]

        engine.link_records("artifact", diff_artifact_id, "code_chunk", build_chunk.id, "implements", metadata={"reason": "memory clipping patch"})
        engine.link_records("artifact", verdict_artifact_id, "artifact", diff_artifact_id, "reviews", metadata={"verdict": "fail"})
        engine.link_records("artifact", plan_artifact_id, "code_chunk", batch_chunk.id, "targets", metadata={"reason": "batch rollout"})

        checks = [
            {
                "name": "code_symbol_lookup",
                "run": lambda: engine.search_code("where is render compare popout implemented", limit=5),
                "assert": lambda result: result.chunks and result.chunks[0].symbol == "renderComparePopout",
                "sample": lambda result: {
                    "top_path": result.chunks[0].path,
                    "top_symbol": result.chunks[0].symbol,
                    "query_ms": round(result.query_ms, 3),
                },
            },
            {
                "name": "code_path_lookup",
                "run": lambda: engine.search_code("which file clips memory before compare fanout", limit=5),
                "assert": lambda result: result.chunks and result.chunks[0].path.endswith("provider-service.ts"),
                "sample": lambda result: {
                    "top_path": result.chunks[0].path,
                    "top_symbol": result.chunks[0].symbol,
                    "query_ms": round(result.query_ms, 3),
                },
            },
            {
                "name": "code_profile_lookup",
                "run": lambda: engine.search_code("which module patched compare fanout memory clipping", limit=5, profile="code"),
                "assert": lambda result: result.chunks and result.chunks[0].path.endswith("provider-service.ts"),
                "sample": lambda result: {
                    "top_path": result.chunks[0].path,
                    "top_symbol": result.chunks[0].symbol,
                    "query_ms": round(result.query_ms, 3),
                },
            },
            {
                "name": "timeline_profile_lookup",
                "run": lambda: engine.search("what is the current narratordb price", limit=8, max_context=10, full_context_threshold=0, profile="timeline"),
                "assert": lambda result: result.messages and "99 dollars" in result.messages[0].text.lower(),
                "sample": lambda result: {
                    "top_text": result.messages[0].text,
                    "query_ms": round(result.query_ms, 3),
                },
            },
            {
                "name": "audit_profile_lookup",
                "run": lambda: engine.search("which review failed the compare popout release", limit=8, max_context=10, full_context_threshold=0, profile="audit"),
                "assert": lambda result: result.messages and "rendercomparepopout" in result.messages[0].text.lower(),
                "sample": lambda result: {
                    "top_text": result.messages[0].text,
                    "query_ms": round(result.query_ms, 3),
                },
            },
            {
                "name": "relation_lookup",
                "run": lambda: engine.related_records("artifact", diff_artifact_id, limit=10),
                "assert": lambda result: {relation.relation_type for relation in result} >= {"implements", "reviews"},
                "sample": lambda result: {
                    "relation_types": sorted({relation.relation_type for relation in result}),
                    "count": len(result),
                },
            },
            {
                "name": "code_filter_lookup",
                "run": lambda: engine.search_code("provider account balancing", limit=5, filters={"path": "src/main/services/provider-service.ts"}),
                "assert": lambda result: result.chunks and result.chunks[0].symbol == "processBatchJob",
                "sample": lambda result: {
                    "top_symbol": result.chunks[0].symbol,
                    "query_ms": round(result.query_ms, 3),
                },
            },
        ]

        metrics = {check["name"]: [] for check in checks}
        failures = []
        samples = {}

        for _ in range(args.passes):
            for check in checks:
                started = time.perf_counter()
                result = check["run"]()
                duration_ms = (time.perf_counter() - started) * 1000
                metrics[check["name"]].append(duration_ms)
                if not check["assert"](result):
                    failures.append({"name": check["name"], "result": repr(result)})
                elif check["name"] not in samples:
                    samples[check["name"]] = check["sample"](result)

        bridge_env = {
            "NARRATORDB_PATH": str(db_path),
            "NARRATORDB_USER_ID": "typed-test",
        }
        proc = subprocess.Popen(
            [sys.executable, str(BRIDGE_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=bridge_env,
        )
        try:
            bridge_context = bridge_request(
                proc,
                "context",
                {
                    "query": "compare popout memory clipping review",
                    "limit": 10,
                    "max_context": 12,
                    "artifact_limit": 3,
                    "profile": "code",
                },
            )
        finally:
            proc.terminate()
            proc.communicate(timeout=5)

        bridge_assertions = [
            "## Related Code" in bridge_context["context"],
            "renderComparePopout" in bridge_context["context"],
            "provider-service.ts" in bridge_context["context"],
            "## Related Artifacts" in bridge_context["context"],
        ]

        if not all(bridge_assertions):
            failures.append({"name": "bridge_context", "result": bridge_context["context"]})

        stats = engine.stats()
        summary = []
        for check in checks:
            name = check["name"]
            values = metrics[name]
            values_sorted = sorted(values)
            p95_index = max(0, min(len(values_sorted) - 1, int(len(values_sorted) * 0.95) - 1))
            summary.append(
                {
                    "name": name,
                    "avg_ms": round(sum(values) / len(values), 3),
                    "p95_ms": round(values_sorted[p95_index], 3),
                    "sample": samples.get(name),
                }
            )

        payload = {
            "passed": not failures,
            "failures": failures,
            "summary": summary,
            "bridge_context_preview": bridge_context["context"].splitlines()[:16],
            "stats": stats,
            "noise_messages": args.noise_messages,
            "noise_artifacts": args.noise_artifacts,
            "noise_code": args.noise_code,
            "passes": args.passes,
            "code_chunk_ids": {
                "buildContextEnvelope": build_chunk.id,
                "renderComparePopout": render_chunk.id,
                "processBatchJob": batch_chunk.id,
            },
        }

        report_dir = Path(tempfile.gettempdir()) / "narratordb_typed"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"narratordb_typed_memory_{int(time.time())}.json"
        report_path.write_text(json.dumps(payload, indent=2))
        print(json.dumps({**payload, "report_path": str(report_path)}, indent=2))

        if failures:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
