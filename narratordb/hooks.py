#!/usr/bin/env python3
"""Fail-open lifecycle hooks for silent automatic memory capture.

Hooks never call a hosted model. They persist high-confidence current-prompt
preferences and a bounded recent window of human requests and final assistant
responses. Tool output, reasoning, system messages, and developer instructions
are deliberately excluded. MCP instructions are static; stored data is exposed
only by explicit recall and resume calls.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from .autocapture import AutoCaptureCandidate, classify_prompt
from .config import (
    CapturePolicy,
    ProjectConfigStore,
    default_db_path,
    default_user_id,
)
from .database import NarratorDB
from .scopes import (
    path_fallback_writes_allowed,
    project_branch,
    resolve_project_scope,
)


MAX_EVENT_BYTES = 1_000_000
MAX_TRANSCRIPT_BYTES = 8_000_000
MAX_TRANSCRIPT_LINES = 3_000
MAX_USER_CHARS = 4_000
MAX_ASSISTANT_CHARS = 12_000
MAX_CAPTURE_MESSAGES = 32
MAX_CAPTURE_CHARS = 64_000

_EVENT_ALIASES = {
    "sessionstart": "SessionStart",
    "session-start": "SessionStart",
    "userpromptsubmit": "UserPromptSubmit",
    "user-prompt": "UserPromptSubmit",
    "precompact": "PreCompact",
    "pre-compact": "PreCompact",
    "stop": "Stop",
}

_SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bxox[A-Za-z0-9]*-[A-Za-z0-9-]{10,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----.*?"
        r"-----END [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----",
        re.DOTALL,
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)\b(api[_ -]?key|access[_ -]?token|auth[_ -]?token|"
        r"auth(?:orization)?|password|"
        r"passwd|secret)\b\s*[:=]\s*[^\s,;]+"
    ),
)

_SYSTEM_BLOCK_RE = re.compile(
    r"<(?:system-reminder|environment_context|developer|private|"
    r"system_instruction)\b[^>]*>.*?</(?:system-reminder|environment_context|"
    r"developer|private|system_instruction)>",
    re.DOTALL | re.IGNORECASE,
)


def normalize_event(value: str) -> str:
    normalized = str(value or "").strip()
    canonical = _EVENT_ALIASES.get(normalized.lower())
    if canonical is None:
        raise ValueError(
            "event must be SessionStart, UserPromptSubmit, PreCompact, or Stop"
        )
    return canonical


def redact_secrets(text: str) -> str:
    redacted = _SYSTEM_BLOCK_RE.sub("[SYSTEM CONTEXT OMITTED]", str(text or ""))
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted.strip()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "input_text", "output_text"}:
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts).strip()


def _tail_lines(path: Path) -> list[str]:
    try:
        if not path.is_file() or path.is_symlink():
            return []
        size = path.stat().st_size
        if size <= 0:
            return []
        with path.open("rb") as handle:
            offset = max(0, size - MAX_TRANSCRIPT_BYTES)
            handle.seek(offset)
            data = handle.read(MAX_TRANSCRIPT_BYTES)
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if offset and lines:
            lines = lines[1:]
        return lines[-MAX_TRANSCRIPT_LINES:]
    except OSError:
        return []


def extract_conversation(lines: Iterable[str]) -> list[dict[str, str]]:
    """Extract a bounded recent window of user and final-assistant messages."""

    candidates: list[tuple[str, str, str]] = []
    has_codex_final = False
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue

        # Current Codex JSONL: {type: response_item, payload: {type: message}}.
        if entry.get("type") == "response_item":
            payload = entry.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue
            role = str(payload.get("role") or "")
            text = _content_text(payload.get("content"))
            if role == "user" and text:
                candidates.append((role, text, "user"))
            elif role == "assistant" and text:
                phase = str(payload.get("phase") or "")
                if phase == "final_answer":
                    has_codex_final = True
                    candidates.append((role, text, "codex-final"))
                elif not phase:
                    # Older Codex transcripts did not label final answers. Use
                    # these only when the transcript has no explicit finals.
                    candidates.append((role, text, "codex-fallback"))
            continue

        # Claude-compatible JSONL: {type: user|assistant, message: {content: ...}}.
        role = str(entry.get("type") or "")
        if role not in {"user", "assistant"} or entry.get("isSidechain"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if (
            role == "assistant"
            and isinstance(content, list)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_use"
                for block in content
            )
        ):
            continue
        text = _content_text(content)
        if role == "user" and text:
            candidates.append((role, text, "user"))
        elif role == "assistant" and text:
            candidates.append((role, text, "claude-final"))

    extracted: list[dict[str, str]] = []
    for role, raw_text, kind in candidates:
        if kind == "codex-fallback" and has_codex_final:
            continue
        if role == "user" and raw_text.lstrip().startswith("<") and "</" in raw_text:
            continue
        text = redact_secrets(raw_text)
        if not text or text == "[SYSTEM CONTEXT OMITTED]":
            continue
        limit = MAX_USER_CHARS if role == "user" else MAX_ASSISTANT_CHARS
        message = {"role": role, "content": text[:limit]}
        # In unphased/Claude transcripts, progress text and the final response
        # can be separate assistant records around non-text tool records. Keep
        # only the final assistant record before the next real user message.
        if role == "assistant" and extracted and extracted[-1]["role"] == "assistant":
            extracted[-1] = message
        else:
            extracted.append(message)

    bounded_reversed: list[dict[str, str]] = []
    captured_chars = 0
    for message in reversed(extracted):
        if len(bounded_reversed) >= MAX_CAPTURE_MESSAGES:
            break
        content_length = len(message["content"])
        if captured_chars + content_length > MAX_CAPTURE_CHARS:
            break
        bounded_reversed.append(message)
        captured_chars += content_length
    return list(reversed(bounded_reversed))


def extract_turn(lines: Iterable[str]) -> tuple[str, str]:
    """Extract only the last user message and final assistant answer."""

    messages = extract_conversation(lines)
    user = next(
        (
            message["content"]
            for message in reversed(messages)
            if message["role"] == "user"
        ),
        "",
    )
    assistant = next(
        (
            message["content"]
            for message in reversed(messages)
            if message["role"] == "assistant"
        ),
        "",
    )
    return user, assistant


def _read_event() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
    if len(raw) > MAX_EVENT_BYTES:
        raise ValueError("hook event exceeds the 1 MB limit")
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("hook event must be a JSON object")
    return value


def _open_memory(path: str, user_id: str) -> NarratorDB | None:
    """Open configured memory without making a first-run mode decision."""

    store = ProjectConfigStore(path)
    if store.load() is None:
        return None
    return NarratorDB(db_path=path, user_id=user_id)


def _capture_session_id(event: dict[str, Any], transcript: Path) -> str:
    supplied = str(event.get("session_id") or "").strip()
    if supplied:
        normalized = re.sub(r"[^A-Za-z0-9._:-]+", "-", supplied).strip("-")
        if normalized:
            return f"agent/{normalized[:200]}"
    identity = "\0".join(
        (
            str(event.get("cwd") or ""),
            str(transcript.expanduser().absolute()),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"agent/transcript-{digest}"


def _capture_turn_index(role: str, content: str) -> int:
    """Return a stable provenance ordinal as a window slides forward."""

    digest = hashlib.sha256(f"{role}\0{content}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def _automatic_capture_enabled() -> bool:
    return os.getenv("NARRATORDB_AUTO_CAPTURE", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _candidate_provenance(candidate: AutoCaptureCandidate) -> dict[str, Any]:
    """Return stable, content-free provenance for a typed personal memory."""

    return {
        "workspace_id": "global",
        "tool_used": "narratordb",
        "metadata": {
            "surface": "agent-hook",
            "capture_kind": "selective_personal_memory",
            "memory_key": candidate.key,
            "rule_id": candidate.rule_id,
            "source_event": "UserPromptSubmit",
        },
    }


def _capture_personal_prompt(memory: NarratorDB, raw_prompt: str) -> int:
    """Store only typed durable personal clauses from the current prompt."""

    if not _automatic_capture_enabled():
        return 0
    original = str(raw_prompt or "").strip()
    redacted = redact_secrets(original)
    candidates = classify_prompt(
        redacted,
        redaction_changed=redacted != original,
    )
    stored = 0
    for candidate in candidates:
        result = memory.remember_automatic(
            candidate.canonical_text,
            memory_key=candidate.key,
            memory_value=candidate.value,
            rule_id=candidate.rule_id,
            source="user",
            workspace_id=None,
            provenance=_candidate_provenance(candidate),
        )
        stored += int(not result.duplicate)
    return stored


def _capture(
    memory: NarratorDB,
    event: dict[str, Any],
    *,
    workspace_id: str,
) -> None:
    if not _automatic_capture_enabled():
        return
    messages, session_id = prepare_session_capture(event)
    if not messages or session_id is None:
        return
    branch = project_branch(str(event.get("cwd") or os.getcwd()))
    memory.ingest_session(
        messages,
        session_id=session_id,
        workspace_id=workspace_id,
        metadata={
            "surface": "agent-hook",
            # Keep provenance stable across duplicate Stop/PreCompact triggers.
            # The hook invocation is a transport detail, not source content.
            "source_event": "turn-boundary",
            "branch": branch,
        },
        wait_for_enrichment=False,
    )


def prepare_session_capture(
    event: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    """Return a bounded, redacted transcript window for lifecycle capture."""

    if event.get("agent_id"):
        return [], None
    transcript = str(event.get("transcript_path") or "").strip()
    if not transcript:
        return [], None
    transcript_path = Path(transcript).expanduser()
    conversation = extract_conversation(_tail_lines(transcript_path))
    messages: list[dict[str, Any]] = []
    skip_acknowledgement = False
    for message in conversation:
        if message["role"] == "user" and classify_prompt(message["content"]):
            # A selective preference is stored globally at prompt time. Do not
            # duplicate it, or its acknowledgement, into project history.
            skip_acknowledgement = True
            continue
        if message["role"] == "assistant" and skip_acknowledgement:
            skip_acknowledgement = False
            continue
        skip_acknowledgement = False
        minimum = 20 if message["role"] == "user" else 40
        if len(message["content"]) < minimum:
            continue
        messages.append(
            {
                **message,
                "provenance": {
                    "metadata": {
                        "turn_index": _capture_turn_index(
                            message["role"], message["content"]
                        )
                    }
                },
            }
        )
    return messages, _capture_session_id(event, transcript_path)


def run_hook(
    event_name: str,
    event: dict[str, Any],
    *,
    path: str,
    user_id: str,
) -> None:
    canonical = normalize_event(event_name)
    cwd = str(event.get("cwd") or os.getcwd())
    project_scope = resolve_project_scope(cwd)
    workspace_id = project_scope.workspace_id
    project_blocked = bool(
        project_scope.write_confirmation_required and not path_fallback_writes_allowed()
    )
    memory = _open_memory(path, user_id)
    if memory is None:
        return
    with memory:
        if canonical == "SessionStart":
            return
        if canonical == "UserPromptSubmit":
            raw_prompt = str(event.get("prompt") or "")[:20_000]
            if memory.capture_policy in {
                CapturePolicy.PREFERENCES,
                CapturePolicy.SESSIONS,
            }:
                _capture_personal_prompt(memory, raw_prompt)
            return
        if project_blocked:
            return
        if memory.capture_policy is not CapturePolicy.SESSIONS:
            return
        _capture(
            memory,
            event,
            workspace_id=workspace_id,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="narratordb-hook",
        description="Run one fail-open NarratorDB agent lifecycle hook",
    )
    parser.add_argument("event")
    parser.add_argument("--path", default=default_db_path())
    parser.add_argument("--user-id", default=default_user_id(getpass.getuser()))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        event = _read_event()
        run_hook(
            args.event,
            event,
            path=str(Path(args.path).expanduser()),
            user_id=str(args.user_id).strip(),
        )
    except Exception as error:  # Hooks must never block an agent turn.
        if os.getenv("NARRATORDB_DEBUG"):
            print(f"narratordb-hook: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
