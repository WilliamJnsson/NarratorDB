#!/usr/bin/env python3
"""NarratorDB JSONL stdio interface.

Talks directly to the canonical SQLite/FTS5 engine and exposes a small JSONL
RPC surface over stdin/stdout.
"""

from __future__ import annotations

import getpass
import io
import json
import os
import sys
from contextlib import redirect_stdout
from dataclasses import asdict
from typing import Any

if __package__:
    from .config import default_db_path, default_user_id
    from .engine import ENGINE_NAME, Engine
else:  # Allow ``python narratordb/stdio.py`` for desktop subprocess bridges.
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from narratordb.config import default_db_path, default_user_id
    from narratordb.engine import ENGINE_NAME, Engine

DB_PATH = default_db_path()
USER_ID = default_user_id(getpass.getuser())


def format_signature(message) -> str:
    provenance = getattr(message, "provenance", None) or {}
    parts = []
    for key in ("provider", "model_id", "agent_id", "run_id", "workspace_id", "tool_used", "response_id"):
        value = provenance.get(key)
        if value:
            parts.append(str(value))
    return f" [{' / '.join(parts)}]" if parts else ""


def build_context(
    engine: Engine,
    query: str,
    limit: int = 20,
    max_context: int = 80,
    filters: dict[str, Any] | None = None,
    artifact_limit: int = 5,
    profile: str = "default",
) -> dict[str, Any]:
    result = engine.search(query, limit=limit, max_context=max_context, full_context_threshold=0, filters=filters, profile=profile)
    artifact_result = engine.search_artifacts(query, limit=artifact_limit, filters=filters)
    code_result = engine.search_code(query, limit=min(5, limit), filters=filters, profile="code" if profile == "default" else profile)
    lines = ["<memory>", f"## {ENGINE_NAME}"]
    lines.append(f"Search query: {query}")
    lines.append("")
    lines.append("## Direct Hits")
    for msg in result.direct_hits:
        lines.append(f"- {msg.speaker}{format_signature(msg)}: {msg.text}")
    if result.context_messages:
        lines.append("")
        lines.append("## Supporting Context")
        for msg in result.context_messages:
            lines.append(f"- {msg.speaker}{format_signature(msg)}: {msg.text}")
    if artifact_result.artifacts:
        lines.append("")
        lines.append("## Related Artifacts")
        for artifact in artifact_result.artifacts:
            parts = [artifact.kind, artifact.title]
            if artifact.provenance:
                signature = " / ".join(
                    str(artifact.provenance[key])
                    for key in ("provider", "model_id", "agent_id", "run_id")
                    if artifact.provenance.get(key)
                )
                if signature:
                    parts.append(signature)
            lines.append(f"- {' | '.join(parts)}")
            if artifact.summary:
                lines.append(f"  summary: {artifact.summary}")
    if code_result.chunks:
        lines.append("")
        lines.append("## Related Code")
        for chunk in code_result.chunks:
            label = " | ".join(part for part in [chunk.path, chunk.symbol or chunk.kind, chunk.language] if part)
            lines.append(f"- {label}")
            if chunk.summary:
                lines.append(f"  summary: {chunk.summary}")
    lines.append("</memory>")
    return {
        "context": "\n".join(lines),
        "formatted_context": "\n".join(lines),
        "query_ms": result.query_ms,
        "total_matches": result.total_matches,
        "messages": [asdict(msg) for msg in result.messages],
        "direct_hits": [asdict(msg) for msg in result.direct_hits],
        "context_messages": [asdict(msg) for msg in result.context_messages],
        "artifacts": [asdict(artifact) for artifact in artifact_result.artifacts],
        "code_chunks": [asdict(chunk) for chunk in code_result.chunks],
        "sources": {
            "sqlite_fts5": result.total_matches,
            "semantic": 1 if engine._sbert else 0,
        },
    }


def handle(engine: Engine, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "health":
        stats = engine.stats()
        health = engine.health_check(full=bool(params.get("full", False)))
        return {
            "ok": health["ok"],
            "engine_name": ENGINE_NAME,
            "engine_path": os.path.abspath(os.path.join(os.path.dirname(__file__), "engine.py")),
            "db_path": DB_PATH,
            "user_id": engine.user_id,
            "stats": stats,
            "health": health,
        }

    if method == "ingest":
        text = str(params.get("text") or "").strip()
        speaker = str(params.get("speaker") or "memory")
        provenance = params.get("provenance")
        if not text:
            return {"stored": False, "reason": "empty"}
        msg_id = engine.remember(text=text, speaker=speaker, provenance=provenance if isinstance(provenance, dict) else None)
        return {
            "stored": msg_id is not None,
            "message_id": msg_id,
        }

    if method == "remember":
        text = str(params.get("text") or "").strip()
        speaker = str(params.get("speaker") or "memory")
        provenance = params.get("provenance")
        if not text:
            return {"stored": False, "reason": "empty"}
        msg_id = engine.remember(text=text, speaker=speaker, provenance=provenance if isinstance(provenance, dict) else None)
        return {
            "stored": msg_id is not None,
            "message_id": msg_id,
        }

    if method == "store_batch":
        messages = params.get("messages") or []
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        stored = engine.store_batch(messages)
        return {"stored": stored}

    if method == "search":
        query = str(params.get("query") or "").strip()
        limit = int(params.get("limit") or 20)
        max_context = int(params.get("max_context") or 80)
        filters = params.get("filters") if isinstance(params.get("filters"), dict) else None
        profile = str(params.get("profile") or "default")
        result = engine.search(query, limit=limit, max_context=max_context, full_context_threshold=0, filters=filters, profile=profile)
        return {
            "query_ms": result.query_ms,
            "total_matches": result.total_matches,
            "messages": [asdict(msg) for msg in result.messages],
            "direct_hits": [asdict(msg) for msg in result.direct_hits],
            "context_messages": [asdict(msg) for msg in result.context_messages],
        }

    if method == "recall":
        query = str(params.get("query") or "").strip()
        limit = int(params.get("limit") or 20)
        max_context = int(params.get("max_context") or 80)
        filters = params.get("filters") if isinstance(params.get("filters"), dict) else None
        profile = str(params.get("profile") or "default")
        result = engine.recall(query, limit=limit, max_context=max_context, full_context_threshold=0, filters=filters, profile=profile)
        return {
            "query_ms": result.query_ms,
            "total_matches": result.total_matches,
            "messages": [asdict(msg) for msg in result.messages],
            "direct_hits": [asdict(msg) for msg in result.direct_hits],
            "context_messages": [asdict(msg) for msg in result.context_messages],
        }

    if method == "context":
        query = str(params.get("query") or "").strip()
        limit = int(params.get("limit") or 20)
        max_context = int(params.get("max_context") or 80)
        artifact_limit = int(params.get("artifact_limit") or 5)
        filters = params.get("filters") if isinstance(params.get("filters"), dict) else None
        profile = str(params.get("profile") or "default")
        return build_context(engine, query, limit=limit, max_context=max_context, filters=filters, artifact_limit=artifact_limit, profile=profile)

    if method == "store_artifact":
        kind = str(params.get("kind") or "").strip()
        title = str(params.get("title") or "").strip()
        content = str(params.get("content") or "").strip()
        summary = str(params.get("summary") or "").strip()
        tags = params.get("tags")
        provenance = params.get("provenance")
        if not kind or not title or not content:
            raise ValueError("store_artifact requires kind, title, and content")
        artifact_id = engine.store_artifact(
            kind=kind,
            title=title,
            content=content,
            summary=summary,
            tags=tags if isinstance(tags, list) else None,
            provenance=provenance if isinstance(provenance, dict) else None,
        )
        return {"stored": artifact_id is not None, "artifact_id": artifact_id}

    if method == "search_artifacts":
        query = str(params.get("query") or "").strip()
        limit = int(params.get("limit") or 10)
        filters = params.get("filters") if isinstance(params.get("filters"), dict) else None
        result = engine.search_artifacts(query=query, limit=limit, filters=filters)
        return {
            "query_ms": result.query_ms,
            "total_matches": result.total_matches,
            "artifacts": [asdict(artifact) for artifact in result.artifacts],
        }

    if method == "store_code_chunk":
        path = str(params.get("path") or "").strip()
        content = str(params.get("content") or "")
        if not path or not content.strip():
            raise ValueError("store_code_chunk requires path and content")
        chunk_id = engine.store_code_chunk(
            path=path,
            content=content,
            symbol=str(params.get("symbol") or "").strip(),
            language=str(params.get("language") or "").strip(),
            kind=str(params.get("kind") or "chunk").strip(),
            summary=str(params.get("summary") or "").strip(),
            start_line=params.get("start_line"),
            end_line=params.get("end_line"),
            tags=params.get("tags") if isinstance(params.get("tags"), list) else None,
            provenance=params.get("provenance") if isinstance(params.get("provenance"), dict) else None,
        )
        return {"stored": chunk_id is not None, "chunk_id": chunk_id}

    if method == "ingest_code_file":
        path = str(params.get("path") or "").strip()
        content = str(params.get("content") or "")
        if not path or not content.strip():
            raise ValueError("ingest_code_file requires path and content")
        stored = engine.ingest_code_file(
            path=path,
            content=content,
            language=str(params.get("language") or "").strip(),
            provenance=params.get("provenance") if isinstance(params.get("provenance"), dict) else None,
            chunk_lines=int(params.get("chunk_lines") or 120),
        )
        return {"stored": stored}

    if method == "search_code":
        query = str(params.get("query") or "").strip()
        limit = int(params.get("limit") or 10)
        filters = params.get("filters") if isinstance(params.get("filters"), dict) else None
        profile = str(params.get("profile") or "code")
        result = engine.search_code(query=query, limit=limit, filters=filters, profile=profile)
        return {
            "query_ms": result.query_ms,
            "total_matches": result.total_matches,
            "chunks": [asdict(chunk) for chunk in result.chunks],
        }

    if method == "link_records":
        relation_id = engine.link_records(
            source_type=str(params.get("source_type") or "").strip(),
            source_id=int(params.get("source_id")),
            target_type=str(params.get("target_type") or "").strip(),
            target_id=int(params.get("target_id")),
            relation_type=str(params.get("relation_type") or "").strip(),
            weight=float(params.get("weight") or 1.0),
            metadata=params.get("metadata") if isinstance(params.get("metadata"), dict) else None,
        )
        return {"stored": relation_id is not None, "relation_id": relation_id}

    if method == "related_records":
        relations = engine.related_records(
            record_type=str(params.get("record_type") or "").strip(),
            record_id=int(params.get("record_id")),
            relation_type=str(params.get("relation_type")).strip() if params.get("relation_type") is not None else None,
            limit=int(params.get("limit") or 20),
        )
        return {"relations": [asdict(relation) for relation in relations]}

    if method == "delete":
        message_id = int(params.get("message_id"))
        deleted = engine.delete(message_id)
        return {"deleted": deleted}

    if method == "forget":
        message_id = params.get("message_id")
        if message_id is None:
            raise ValueError("forget requires message_id; use clear_scope explicitly for full deletion")
        deleted = engine.forget(int(message_id))
        return {"deleted": 1 if deleted else 0}

    if method == "clear_scope":
        deleted = engine.clear_scope()
        return {"deleted": deleted}

    if method == "stats":
        return engine.stats()

    raise ValueError(f"Unknown method: {method}")


def main() -> int:
    with redirect_stdout(io.StringIO()):
        engine = Engine(db_path=DB_PATH, user_id=USER_ID, context_window=5)
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            request_id = None
            try:
                payload = json.loads(line)
                request_id = payload.get("id")
                method = str(payload.get("method") or "")
                params = payload.get("params") or {}
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                result = handle(engine, method, params)
                response = {"id": request_id, "ok": True, "result": result}
            except Exception as exc:  # noqa: BLE001
                response = {"id": request_id, "ok": False, "error": str(exc)}

            sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
            sys.stdout.flush()
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
