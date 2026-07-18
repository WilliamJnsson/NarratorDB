#!/usr/bin/env python3
"""Broad production-style validation for NarratorDB."""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import narratordb.engine as engine_module

ROOT = Path(__file__).resolve().parents[2]

BRIDGE_PATH = ROOT / "narratordb" / "stdio.py"
TYPED_FIXTURES_PATH = ROOT / "tests" / "stress" / "typed_memory.py"


def load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def iso_ts(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


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


EXTRA_MESSAGES = [
    {
        "speaker": "Noor Hale",
        "text": "Noor Hale handled deployment safety for the Bastion rollout in January 2026.",
        "timestamp": iso_ts("2026-01-12T09:00:00"),
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "release-steward",
            "run_id": "run-deploy-00",
            "workspace_id": "narratordb",
            "response_id": "resp-claude-deploy-001",
        },
    },
    {
        "speaker": "Qwen Sentinel",
        "text": "Qwen Sentinel took over final approval and deployment safety on 3 March 2026.",
        "timestamp": iso_ts("2026-03-03T10:00:00"),
        "provenance": {
            "provider": "qwen",
            "model_id": "qwen-2.5-coder-32b",
            "agent_id": "reviewer",
            "run_id": "run-deploy-01",
            "workspace_id": "narratordb",
            "response_id": "resp-qwen-deploy-002",
        },
    },
    {
        "speaker": "Claude Memory",
        "text": "Project Bastion was merged into Sentinel Gate after the March hardening review.",
        "timestamp": iso_ts("2026-03-11T14:00:00"),
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "historian",
            "run_id": "run-memory-merge-01",
            "workspace_id": "narratordb",
            "response_id": "resp-claude-merge-003",
        },
    },
    {
        "speaker": "Claude Memory",
        "text": "After the staging API key rotation, the secrets moved into Doppler.",
        "timestamp": iso_ts("2026-03-05T13:30:00"),
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "ops",
            "run_id": "run-ops-rotate-01",
            "workspace_id": "narratordb",
            "response_id": "resp-claude-ops-004",
        },
    },
    {
        "speaker": "Coach Memory",
        "text": "William trains kickboxing twice a week for conditioning.",
        "timestamp": iso_ts("2026-02-21T18:00:00"),
        "provenance": {
            "provider": "openai",
            "model_id": "gpt-5",
            "agent_id": "coach",
            "run_id": "run-personal-01",
            "workspace_id": "narratordb",
            "response_id": "resp-gpt-personal-005",
        },
    },
    {
        "speaker": "Claude Compare",
        "text": "Shared verdict text for provider comparison.",
        "timestamp": iso_ts("2026-03-20T09:15:00"),
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "judge",
            "run_id": "run-compare-01",
            "workspace_id": "narratordb",
            "response_id": "resp-claude-compare-006",
        },
    },
    {
        "speaker": "GPT Compare",
        "text": "Shared verdict text for provider comparison.",
        "timestamp": iso_ts("2026-03-20T09:16:00"),
        "provenance": {
            "provider": "openai",
            "model_id": "gpt-5",
            "agent_id": "judge",
            "run_id": "run-compare-01",
            "workspace_id": "narratordb",
            "response_id": "resp-gpt-compare-007",
        },
    },
]


EXTRA_ARTIFACTS = [
    {
        "kind": "web_source",
        "title": "Vendor OAuth rollout guidance",
        "summary": "Vendor docs covering OAuth rollout caveats and callback validation.",
        "content": "Source: https://vendor.example/oauth-rollout\nRemember to validate callback origins and rotate client secrets before rollout.",
        "tags": ["web", "oauth", "source"],
        "timestamp": iso_ts("2026-03-07T11:00:00"),
        "provenance": {
            "provider": "openai",
            "model_id": "gpt-5",
            "agent_id": "researcher",
            "run_id": "run-web-01",
            "workspace_id": "narratordb",
            "tool_used": "web_search",
            "response_id": "artifact-gpt-web-001",
        },
    },
    {
        "kind": "test_result",
        "title": "Auth header hardening test run",
        "summary": "Security tests passed after header and cookie fixes.",
        "content": "Result: pass. CSP, secure cookies, and CSRF protections verified in auth-service.ts.",
        "tags": ["test", "security", "auth"],
        "timestamp": iso_ts("2026-03-14T12:00:00"),
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "tester",
            "run_id": "run-auth-verify-01",
            "workspace_id": "narratordb",
            "response_id": "artifact-claude-test-002",
        },
    },
    {
        "kind": "decision",
        "title": "Release rollback approval",
        "summary": "Rollback plan approved for release train Orion.",
        "content": "Decision: approve rollback plan for release train Orion before deploy.",
        "tags": ["decision", "release", "rollback"],
        "timestamp": iso_ts("2026-03-18T15:00:00"),
        "provenance": {
            "provider": "qwen",
            "model_id": "qwen-2.5-coder-32b",
            "agent_id": "reviewer",
            "run_id": "run-release-approve-01",
            "workspace_id": "narratordb",
            "response_id": "artifact-qwen-decision-003",
        },
    },
]


EXTRA_CODE_FILES = [
    {
        "path": "src/main/services/auth-service.ts",
        "content": """
export function rotateStagingSecrets(secretStore: string) {
  return `rotated:${secretStore}`;
}

export function installSecurityHeaders() {
  return ['Content-Security-Policy', 'Strict-Transport-Security'];
}

export function validateSessionCookie(cookie: string) {
  return cookie.includes('Secure') && cookie.includes('HttpOnly');
}
""".strip(),
        "provenance": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4",
            "agent_id": "backend",
            "run_id": "run-auth-verify-01",
            "workspace_id": "narratordb",
            "response_id": "code-claude-auth-601",
            "metadata": {"file": "auth-service.ts"},
        },
    },
    {
        "path": "src/main/services/deploy-review.ts",
        "content": """
export function approveRollbackPlan(releaseTrain: string) {
  return `approved:${releaseTrain}`;
}

export function validateReleaseReadiness(checks: string[]) {
  return checks.every(Boolean);
}
""".strip(),
        "provenance": {
            "provider": "qwen",
            "model_id": "qwen-2.5-coder-32b",
            "agent_id": "reviewer",
            "run_id": "run-release-approve-01",
            "workspace_id": "narratordb",
            "response_id": "code-qwen-release-602",
            "metadata": {"file": "deploy-review.ts"},
        },
    },
]


FIXTURE_MESSAGE_TIMESTAMPS = {
    "Claude mapped the backend hardening plan in task-compiler-service.ts and prioritized auth rotation first.": iso_ts("2026-03-01T09:00:00"),
    "Claude patched buildContextEnvelope in provider-service.ts so compare fanout receives clipped memory payloads.": iso_ts("2026-03-09T14:00:00"),
    "GPT routed the prompt fanout through processBatchJob for provider account balancing.": iso_ts("2026-03-08T11:00:00"),
    "Qwen flagged the missing Content-Security-Policy header in renderComparePopout before release.": iso_ts("2026-03-13T16:00:00"),
    "NarratorDB pricing was 79 dollars per month in January 2026.": iso_ts("2026-01-20T12:00:00"),
    "NarratorDB pricing changed to 99 dollars per month on 10 March 2026.": iso_ts("2026-03-10T12:00:00"),
}


def summarize_message(result):
    return {
        "top_text": result.messages[0].text if result.messages else "",
        "query_ms": round(result.query_ms, 3),
        "direct_hits": len(result.direct_hits),
    }


def summarize_artifact(result):
    return {
        "top_title": result.artifacts[0].title if result.artifacts else "",
        "top_kind": result.artifacts[0].kind if result.artifacts else "",
        "query_ms": round(result.query_ms, 3),
    }


def summarize_code(result):
    return {
        "top_path": result.chunks[0].path if result.chunks else "",
        "top_symbol": result.chunks[0].symbol if result.chunks else "",
        "query_ms": round(result.query_ms, 3),
    }


def run_single_shot_checks(Engine):
    checks = []

    with tempfile.TemporaryDirectory(prefix="narratordb_small_") as td:
        db_path = Path(td) / "small.db"
        engine = Engine(db_path=str(db_path), user_id="small-scope", semantic_dedup=False)
        engine.store("user", "Small scope hello", timestamp=1.0)
        engine.store("assistant", "Small scope answer", timestamp=2.0)
        engine.store("assistant", "Small scope follow up", timestamp=3.0)
        result = engine.search("small scope", limit=10, full_context_threshold=10)
        checks.append(
            {
                "name": "small_scope_full_context",
                "passed": len(result.messages) == 3 and result.messages[0].text == "Small scope hello",
                "sample": {"count": len(result.messages), "top_text": result.messages[0].text if result.messages else ""},
            }
        )
        engine.close()

    with tempfile.TemporaryDirectory(prefix="narratordb_delete_") as td:
        db_path = Path(td) / "delete.db"
        engine = Engine(db_path=str(db_path), user_id="delete-scope", semantic_dedup=False)
        message_id = engine.remember(
            text="ephemeral deletion marker",
            speaker="system",
            provenance={"provider": "system", "model_id": "codex-gpt-5", "workspace_id": "narratordb"},
        )
        deleted = engine.delete(message_id)
        result = engine.search("ephemeral deletion marker", limit=5, full_context_threshold=0)
        checks.append(
            {
                "name": "delete_roundtrip",
                "passed": bool(deleted) and not result.messages,
                "sample": {"deleted": bool(deleted), "remaining": len(result.messages)},
            }
        )
        engine.close()

    with tempfile.TemporaryDirectory(prefix="narratordb_cleanup_") as td:
        db_path = Path(td) / "cleanup.db"
        engine = Engine(db_path=str(db_path), user_id="cleanup-scope", semantic_dedup=False)
        for index in range(5):
            engine.store("cleanup", f"cleanup message {index}", timestamp=float(index + 1))
        deleted = engine.cleanup(max_messages=2)
        stats = engine.stats()
        checks.append(
            {
                "name": "cleanup_max_messages",
                "passed": deleted == 3 and stats["message_count"] == 2,
                "sample": {"deleted": deleted, "message_count": stats["message_count"]},
            }
        )
        engine.close()

    with tempfile.TemporaryDirectory(prefix="narratordb_clear_") as td:
        db_path = Path(td) / "clear.db"
        engine = Engine(db_path=str(db_path), user_id="clear-scope", semantic_dedup=False)
        message_id = engine.store("system", "clear scope marker")
        artifact_id = engine.store_artifact(kind="note", title="clear artifact", content="artifact body")
        chunk_id = engine.store_code_chunk(path="src/tmp.ts", content="export const marker = true;", symbol="marker")
        engine.link_records("artifact", artifact_id, "code_chunk", chunk_id, "references")
        deleted = engine.clear_scope()
        stats = engine.stats()
        checks.append(
            {
                "name": "clear_scope_roundtrip",
                "passed": deleted == 1 and stats["message_count"] == 0 and stats["artifact_count"] == 0 and stats["code_chunk_count"] == 0 and stats["relation_count"] == 0,
                "sample": {
                    "deleted": deleted,
                    "message_count": stats["message_count"],
                    "artifact_count": stats["artifact_count"],
                    "code_chunk_count": stats["code_chunk_count"],
                    "relation_count": stats["relation_count"],
                    "message_id": message_id,
                },
            }
        )
        engine.close()

    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--noise-messages", type=int, default=8000)
    parser.add_argument("--noise-artifacts", type=int, default=2000)
    parser.add_argument("--noise-code", type=int, default=2200)
    parser.add_argument("--passes", type=int, default=12)
    args = parser.parse_args()

    fixtures = load_module(TYPED_FIXTURES_PATH, "narratordb_typed_fixtures")
    Engine = engine_module.Engine

    random.seed(17)

    with tempfile.TemporaryDirectory(prefix="narratordb_every_angle_") as td:
        db_path = Path(td) / "every-angle.db"
        engine = Engine(db_path=str(db_path), user_id="every-angle", context_window=5)

        core_messages = []
        for row in fixtures.MESSAGE_ROWS:
            seeded = dict(row)
            if seeded["text"] in FIXTURE_MESSAGE_TIMESTAMPS:
                seeded["timestamp"] = FIXTURE_MESSAGE_TIMESTAMPS[seeded["text"]]
            core_messages.append(seeded)
        core_messages.extend(EXTRA_MESSAGES)
        engine.store_batch(core_messages)
        engine.store_batch(fixtures.noise_messages(args.noise_messages))

        artifact_ids = []
        for artifact in fixtures.ARTIFACT_ROWS + EXTRA_ARTIFACTS:
            artifact_ids.append(
                engine.store_artifact(
                    kind=artifact["kind"],
                    title=artifact["title"],
                    content=artifact["content"],
                    summary=artifact["summary"],
                    tags=artifact["tags"],
                    timestamp=artifact.get("timestamp"),
                    provenance=artifact["provenance"],
                )
            )
        for artifact in fixtures.noise_artifacts(args.noise_artifacts):
            engine.store_artifact(
                kind=artifact["kind"],
                title=artifact["title"],
                content=artifact["content"],
                summary=artifact["summary"],
                tags=artifact["tags"],
                provenance=artifact["provenance"],
            )

        for file_def in fixtures.CODE_FILES + EXTRA_CODE_FILES:
            engine.ingest_code_file(
                path=file_def["path"],
                content=file_def["content"],
                provenance=file_def["provenance"],
            )
        for chunk in fixtures.noise_code_chunks(args.noise_code):
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
        auth_chunk = engine.search_code("rotate staging secrets", limit=3).chunks[0]
        rollback_chunk = engine.search_code("approve rollback plan", limit=3).chunks[0]

        diff_artifact_id = artifact_ids[0]
        verdict_artifact_id = artifact_ids[1]
        plan_artifact_id = artifact_ids[2]
        web_artifact_id = artifact_ids[3]
        test_artifact_id = artifact_ids[4]
        decision_artifact_id = artifact_ids[5]

        engine.link_records("artifact", diff_artifact_id, "code_chunk", build_chunk.id, "implements", metadata={"reason": "memory clipping patch"})
        engine.link_records("artifact", verdict_artifact_id, "artifact", diff_artifact_id, "reviews", metadata={"verdict": "fail"})
        engine.link_records("artifact", plan_artifact_id, "code_chunk", batch_chunk.id, "targets", metadata={"reason": "batch rollout"})
        engine.link_records("artifact", web_artifact_id, "artifact", plan_artifact_id, "supports", metadata={"source": "vendor docs"})
        engine.link_records("artifact", test_artifact_id, "code_chunk", auth_chunk.id, "verifies", metadata={"scope": "auth hardening"})
        engine.link_records("artifact", decision_artifact_id, "code_chunk", rollback_chunk.id, "approves", metadata={"release_train": "Orion"})

        ts_mar_1 = iso_ts("2026-03-01T00:00:00")
        ts_mar_6 = iso_ts("2026-03-06T00:00:00")

        checks = [
            {
                "name": "timeline_current_price",
                "run": lambda: engine.search("what is the current narratordb price", limit=8, max_context=8, full_context_threshold=0, profile="timeline"),
                "assert": lambda result: result.messages and "99 dollars" in result.messages[0].text.lower(),
                "sample": summarize_message,
            },
            {
                "name": "timeline_previous_owner",
                "run": lambda: engine.search("who handled deployment safety before qwen sentinel", limit=8, max_context=8, full_context_threshold=0, profile="timeline"),
                "assert": lambda result: result.messages and "noor hale" in result.messages[0].text.lower(),
                "sample": summarize_message,
            },
            {
                "name": "semantic_combat_sport",
                "run": lambda: engine.recall("what combat sport does william practice", limit=8, max_context=8, full_context_threshold=0, profile="default"),
                "assert": lambda result: result.messages and "kickboxing" in result.messages[0].text.lower(),
                "sample": summarize_message,
            },
            {
                "name": "fact_secret_destination",
                "run": lambda: engine.search("where were the secrets moved after the staging api key rotation", limit=8, max_context=8, full_context_threshold=0, profile="fact"),
                "assert": lambda result: result.messages and "doppler" in result.messages[0].text.lower(),
                "sample": summarize_message,
            },
            {
                "name": "fact_project_merge",
                "run": lambda: engine.search("what did project bastion get merged into", limit=8, max_context=8, full_context_threshold=0, profile="fact"),
                "assert": lambda result: result.messages and "sentinel gate" in result.messages[0].text.lower(),
                "sample": summarize_message,
            },
            {
                "name": "provider_filter_qwen",
                "run": lambda: engine.search("compare popout release", limit=8, max_context=8, full_context_threshold=0, filters={"provider": "qwen"}, profile="audit"),
                "assert": lambda result: result.messages and all((msg.provenance or {}).get("provider") == "qwen" for msg in result.messages),
                "sample": summarize_message,
            },
            {
                "name": "model_filter_claude",
                "run": lambda: engine.search("narratordb price", limit=8, max_context=8, full_context_threshold=0, filters={"model_id": "claude-sonnet-4"}, profile="timeline"),
                "assert": lambda result: result.messages and all((msg.provenance or {}).get("model_id") == "claude-sonnet-4" for msg in result.messages),
                "sample": summarize_message,
            },
            {
                "name": "agent_filter_reviewer",
                "run": lambda: engine.search("compare popout release", limit=8, max_context=8, full_context_threshold=0, filters={"agent_id": "reviewer"}, profile="audit"),
                "assert": lambda result: result.messages and all((msg.provenance or {}).get("agent_id") == "reviewer" for msg in result.messages),
                "sample": summarize_message,
            },
            {
                "name": "run_filter_memory",
                "run": lambda: engine.search("buildcontextenvelope clipped memory payloads compare fanout", limit=8, max_context=8, full_context_threshold=0, filters={"run_id": "run-memory-02"}, profile="fact"),
                "assert": lambda result: result.direct_hits and result.direct_hits[0].speaker == "Claude Backend" and all((msg.provenance or {}).get("run_id") == "run-memory-02" for msg in result.direct_hits),
                "sample": summarize_message,
            },
            {
                "name": "time_range_price",
                "run": lambda: engine.search("narratordb price", limit=8, max_context=8, full_context_threshold=0, after=ts_mar_1, profile="timeline"),
                "assert": lambda result: result.messages and "99 dollars" in result.messages[0].text.lower(),
                "sample": summarize_message,
            },
            {
                "name": "duplicate_provider_anthropic",
                "run": lambda: engine.search("shared verdict text for provider comparison", limit=8, max_context=8, full_context_threshold=0, filters={"provider": "anthropic"}, profile="fact"),
                "assert": lambda result: result.messages and result.messages[0].speaker == "Claude Compare",
                "sample": summarize_message,
            },
            {
                "name": "duplicate_provider_openai",
                "run": lambda: engine.search("shared verdict text for provider comparison", limit=8, max_context=8, full_context_threshold=0, filters={"provider": "openai"}, profile="fact"),
                "assert": lambda result: result.messages and result.messages[0].speaker == "GPT Compare",
                "sample": summarize_message,
            },
            {
                "name": "artifact_review_lookup",
                "run": lambda: engine.search_artifacts("compare popout csp verdict", limit=5),
                "assert": lambda result: result.artifacts and result.artifacts[0].title == "compare popout CSP verdict",
                "sample": summarize_artifact,
            },
            {
                "name": "artifact_provider_filter",
                "run": lambda: engine.search_artifacts("batch routing rollout", limit=5, filters={"provider": "openai"}),
                "assert": lambda result: result.artifacts and all((artifact.provenance or {}).get("provider") == "openai" for artifact in result.artifacts),
                "sample": summarize_artifact,
            },
            {
                "name": "artifact_kind_filter",
                "run": lambda: engine.search_artifacts("oauth rollout guidance", limit=5, filters={"kind": "web_source"}),
                "assert": lambda result: result.artifacts and result.artifacts[0].kind == "web_source",
                "sample": summarize_artifact,
            },
            {
                "name": "artifact_tag_filter",
                "run": lambda: engine.search_artifacts("security", limit=5, filters={"tag": "security"}),
                "assert": lambda result: result.artifacts and "security" in result.artifacts[0].tags,
                "sample": summarize_artifact,
            },
            {
                "name": "code_symbol_lookup",
                "run": lambda: engine.search_code("where is render compare popout implemented", limit=5),
                "assert": lambda result: result.chunks and result.chunks[0].symbol == "renderComparePopout",
                "sample": summarize_code,
            },
            {
                "name": "code_semantic_lookup",
                "run": lambda: engine.search_code("which function rotates staging secrets", limit=5, profile="code"),
                "assert": lambda result: result.chunks and result.chunks[0].symbol == "rotateStagingSecrets",
                "sample": summarize_code,
            },
            {
                "name": "code_filter_lookup",
                "run": lambda: engine.search_code("provider account balancing", limit=5, filters={"path": "src/main/services/provider-service.ts"}),
                "assert": lambda result: result.chunks and result.chunks[0].symbol == "processBatchJob",
                "sample": summarize_code,
            },
            {
                "name": "code_after_filter",
                "run": lambda: engine.search_code("approve rollback plan", limit=5, after=ts_mar_6, profile="code"),
                "assert": lambda result: result.chunks and result.chunks[0].symbol == "approveRollbackPlan",
                "sample": summarize_code,
            },
            {
                "name": "relation_forward",
                "run": lambda: engine.related_records("artifact", diff_artifact_id, limit=10),
                "assert": lambda result: {relation.relation_type for relation in result} >= {"implements", "reviews"},
                "sample": lambda result: {"relation_types": sorted({relation.relation_type for relation in result}), "count": len(result)},
            },
            {
                "name": "relation_reverse_code",
                "run": lambda: engine.related_records("code_chunk", auth_chunk.id, limit=10),
                "assert": lambda result: any(relation.relation_type == "verifies" for relation in result),
                "sample": lambda result: {"relation_types": sorted({relation.relation_type for relation in result}), "count": len(result)},
            },
            {
                "name": "stats_counts",
                "run": engine.stats,
                "assert": lambda result: result["message_count"] >= len(core_messages) and result["artifact_count"] >= len(fixtures.ARTIFACT_ROWS) + len(EXTRA_ARTIFACTS) and result["code_chunk_count"] >= 1 and result["relation_count"] >= 6,
                "sample": lambda result: {
                    "message_count": result["message_count"],
                    "artifact_count": result["artifact_count"],
                    "code_chunk_count": result["code_chunk_count"],
                    "relation_count": result["relation_count"],
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
            "NARRATORDB_USER_ID": "every-angle",
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
                    "artifact_limit": 4,
                    "profile": "code",
                },
            )
            bridge_filtered = bridge_request(
                proc,
                "context",
                {
                    "query": "shared verdict text for provider comparison",
                    "limit": 6,
                    "max_context": 6,
                    "filters": {"provider": "openai"},
                    "profile": "fact",
                },
            )
        finally:
            proc.terminate()
            proc.communicate(timeout=5)

        bridge_checks = [
            {
                "name": "bridge_context_sections",
                "passed": all(
                    marker in bridge_context["context"]
                    for marker in ("## Direct Hits", "## Related Artifacts", "## Related Code", "renderComparePopout", "provider-service.ts")
                ),
                "sample": {
                    "query_ms": bridge_context["query_ms"],
                    "preview": bridge_context["context"].splitlines()[:10],
                },
            },
            {
                "name": "bridge_filtered_context",
                "passed": "GPT Compare" in bridge_filtered["context"] and "Claude Compare" not in bridge_filtered["context"],
                "sample": {
                    "query_ms": bridge_filtered["query_ms"],
                    "preview": bridge_filtered["context"].splitlines()[:8],
                },
            },
        ]

        maintenance_checks = run_single_shot_checks(Engine)

        for check in bridge_checks + maintenance_checks:
            if not check["passed"]:
                failures.append({"name": check["name"], "result": check["sample"]})

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
            "bridge_checks": bridge_checks,
            "maintenance_checks": maintenance_checks,
            "stats": engine.stats(),
            "noise_messages": args.noise_messages,
            "noise_artifacts": args.noise_artifacts,
            "noise_code": args.noise_code,
            "passes": args.passes,
            "record_ids": {
                "diff_artifact_id": diff_artifact_id,
                "verdict_artifact_id": verdict_artifact_id,
                "plan_artifact_id": plan_artifact_id,
                "web_artifact_id": web_artifact_id,
                "test_artifact_id": test_artifact_id,
                "decision_artifact_id": decision_artifact_id,
                "build_chunk_id": build_chunk.id,
                "render_chunk_id": render_chunk.id,
                "batch_chunk_id": batch_chunk.id,
                "auth_chunk_id": auth_chunk.id,
                "rollback_chunk_id": rollback_chunk.id,
            },
        }

        report_dir = Path("/tmp/narratordb_every_angle")
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"narratordb_every_angle_{int(time.time())}.json"
        payload["report_path"] = str(report_path)
        report_path.write_text(json.dumps(payload, indent=2))
        print(json.dumps(payload, indent=2))

        engine.close()

        raise SystemExit(0 if payload["passed"] else 1)


if __name__ == "__main__":
    main()
