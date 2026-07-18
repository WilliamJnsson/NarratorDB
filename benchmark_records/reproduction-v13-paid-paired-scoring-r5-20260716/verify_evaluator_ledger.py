#!/usr/bin/env python3
"""Fail-closed V13 paid-pair evaluator-ledger identity verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "narratordb.v13-paid-evaluator-ledger-identity-audit.v1"
ANSWERER_MODEL = "z-ai/glm-5.2"
JUDGE_MODEL = "deepseek/deepseek-v4-flash-20260423"
REQUEST_MODELS = frozenset({ANSWERER_MODEL, JUDGE_MODEL})
RESPONSE_MODELS_BY_REQUEST = {
    ANSWERER_MODEL: frozenset({ANSWERER_MODEL}),
    JUDGE_MODEL: frozenset(
        {
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-flash-20260423",
        }
    ),
}
PROVIDERS = frozenset(
    {"AtlasCloud", "Baidu", "DeepInfra", "GMICloud", "StreamLake"}
)
EVENT_TYPES = frozenset({"completion", "upstream_error"})
MAX_LEDGER_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_INTEGER = (1 << 63) - 1
MAX_COST_USD = Decimal("1000000000")
UNKNOWN_COST_RESERVATION_USD = Decimal("0.05")
FINISH_REASONS = frozenset(
    {
        "stop",
        "length",
        "content_filter",
        "tool_calls",
        "function_call",
        "error",
        "unknown",
    }
)
ERROR_CODES = frozenset(
    {"rate_limited", "overloaded", "timeout", "unavailable", "unknown"}
)
ERROR_TYPES = frozenset(
    {
        "rate_limit",
        "rate_limit_error",
        "rate_limit_exceeded",
        "provider_error",
        "upstream_error",
        "timeout",
        "overloaded",
        "server_error",
        "invalid_request",
        "unknown",
    }
)
ERROR_PROVIDERS = PROVIDERS | frozenset({"unknown", "route_mismatch"})
TOKEN_FIELDS = (
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "reasoning_tokens",
)

_COMPLETION_REQUIRED = {
    "timestamp",
    "event",
    "status",
    "request_model",
    "response_model",
    "provider",
    "finish_reason",
    "response_complete",
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cost_usd",
    "unknown_cost",
}
_COMPLETION_ALLOWED = _COMPLETION_REQUIRED
_ERROR_REQUIRED = {
    "timestamp",
    "event",
    "status",
    "request_model",
    "provider",
    "error_code",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cost_usd",
    "cached_tokens",
    "error_type",
    "unknown_cost",
}
_ERROR_ALLOWED = _ERROR_REQUIRED
_FORBIDDEN_CONTENT_FIELDS = {
    "answer",
    "body",
    "choices",
    "completion",
    "content",
    "input",
    "messages",
    "output",
    "prompt",
    "question",
    "raw",
    "request",
    "response",
    "text",
}
_SECRET_PATTERNS = (
    re.compile(rb"\bsk-(?:or-v1-)?[A-Za-z0-9_-]{20,}"),
    re.compile(rb"\bBearer[ \t]+[A-Za-z0-9._~+/-]{20,}", re.IGNORECASE),
    re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"\bsk-(?:or-v1-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/-]{20,}", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
)
_RAW_CONTENT_FIELD = re.compile(
    rb'"(?:answer|body|choices|completion|content|input|messages|output|prompt|question|raw|request|response|text)"\s*:',
    re.IGNORECASE,
)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_exact_fields(
    event: Mapping[str, Any], *, required: set[str], allowed: set[str], line: int
) -> None:
    fields = set(event)
    missing = sorted(required - fields)
    extra = sorted(fields - allowed)
    if missing or extra:
        raise ValueError(
            f"line {line}: invalid event fields: missing={missing}, extra={extra}"
        )


def _reject_content(value: Any, *, line: int) -> None:
    if isinstance(value, Mapping):
        forbidden = sorted(set(value) & _FORBIDDEN_CONTENT_FIELDS)
        if forbidden:
            raise ValueError(f"line {line}: content fields are forbidden: {forbidden}")
        for key, child in value.items():
            _scan_decoded_string(key, line=line)
            _reject_content(child, line=line)
    elif isinstance(value, list):
        for child in value:
            _reject_content(child, line=line)
    elif isinstance(value, str):
        _scan_decoded_string(value, line=line)


def _scan_decoded_string(value: str, *, line: int) -> None:
    for pattern in _SECRET_TEXT_PATTERNS:
        if pattern.search(value):
            raise ValueError(
                f"line {line}: credential-like material is forbidden after JSON decoding"
            )


def _timestamp(value: Any, *, line: int) -> None:
    if not isinstance(value, str):
        raise ValueError(f"line {line}: timestamp must be a timezone-aware string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"line {line}: timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"line {line}: timestamp must be timezone-aware")


def _integer(value: Any, *, label: str, line: int, maximum: int = MAX_INTEGER) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"line {line}: {label} must be a non-boolean integer")
    if not 0 <= value <= maximum:
        raise ValueError(f"line {line}: {label} is outside 0..{maximum}")
    return value


def _cost(value: Any, *, unknown_cost: bool, line: int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"line {line}: cost_usd must be an exact JSON number")
    try:
        parsed = Decimal(value)
    except (ArithmeticError, ValueError) as error:
        raise ValueError(f"line {line}: cost_usd is invalid") from error
    if not parsed.is_finite() or not Decimal("0") <= parsed <= MAX_COST_USD:
        raise ValueError(
            f"line {line}: cost_usd must be finite and within 0..{MAX_COST_USD}"
        )
    if unknown_cost and parsed != UNKNOWN_COST_RESERVATION_USD:
        raise ValueError(
            f"line {line}: unknown cost must equal the frozen "
            f"USD {UNKNOWN_COST_RESERVATION_USD} reservation"
        )
    return parsed


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def verify_ledger(path: Path) -> dict[str, Any]:
    """Verify one sealed evaluator ledger without reading model content."""

    if path.is_symlink():
        raise ValueError("ledger may not be a symlink")
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise ValueError(f"ledger does not exist: {path}") from error
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("ledger must be a regular file")
    if before.st_size > MAX_LEDGER_BYTES:
        raise ValueError("ledger exceeds the 32 MiB verification limit")

    payload = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if len(payload) != before.st_size or before_identity != after_identity:
        raise RuntimeError("ledger changed while being read")
    if payload and not payload.endswith(b"\n"):
        raise ValueError("ledger has an incomplete final line")
    for pattern in _SECRET_PATTERNS:
        if pattern.search(payload):
            raise ValueError("credential-like material is forbidden in the ledger")
    if _RAW_CONTENT_FIELD.search(payload):
        raise ValueError("model-content fields are forbidden in the raw ledger")

    events = 0
    unknown_cost_attempts = 0
    total_cost = Decimal("0")
    completions = {model: 0 for model in sorted(REQUEST_MODELS)}
    errors = {model: 0 for model in sorted(REQUEST_MODELS)}
    response_counts: dict[str, int] = {}
    provider_counts: dict[str, int] = {}

    for physical_line, raw_line in enumerate(payload.splitlines(keepends=True), 1):
        if len(raw_line) > MAX_LINE_BYTES:
            raise ValueError(f"line {physical_line}: exceeds 64 KiB")
        if not raw_line.strip():
            continue
        events += 1
        try:
            event = json.loads(
                raw_line,
                parse_float=Decimal,
                parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError(f"line {physical_line}: invalid JSON: {error}") from error
        if not isinstance(event, Mapping):
            raise ValueError(f"line {physical_line}: event must be an object")
        _reject_content(event, line=physical_line)

        event_type = event.get("event")
        if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
            raise ValueError(f"line {physical_line}: undeclared event type")
        request_model = event.get("request_model")
        if not isinstance(request_model, str) or request_model not in REQUEST_MODELS:
            raise ValueError(f"line {physical_line}: undeclared request model")

        if event_type == "completion":
            _require_exact_fields(
                event,
                required=_COMPLETION_REQUIRED,
                allowed=_COMPLETION_ALLOWED,
                line=physical_line,
            )
            response_model = event["response_model"]
            if not isinstance(response_model, str) or response_model not in (
                RESPONSE_MODELS_BY_REQUEST[request_model]
            ):
                raise ValueError(
                    f"line {physical_line}: response model is not in the exact "
                    "request-to-response identity mapping"
                )
            provider = event["provider"]
            if not isinstance(provider, str) or provider not in PROVIDERS:
                raise ValueError(f"line {physical_line}: undeclared completion provider")
            _timestamp(event["timestamp"], line=physical_line)
            if _integer(
                event["status"], label="completion status", line=physical_line, maximum=999
            ) != 200:
                raise ValueError(f"line {physical_line}: completion status must equal 200")
            for field in TOKEN_FIELDS:
                _integer(event[field], label=field, line=physical_line)
            if not isinstance(event["finish_reason"], str) or event[
                "finish_reason"
            ] not in FINISH_REASONS:
                raise ValueError(f"line {physical_line}: undeclared finish_reason")
            if not isinstance(event["response_complete"], bool):
                raise ValueError(f"line {physical_line}: response_complete must be boolean")
            completions[request_model] += 1
            response_counts[response_model] = response_counts.get(response_model, 0) + 1
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
        else:
            _require_exact_fields(
                event,
                required=_ERROR_REQUIRED,
                allowed=_ERROR_ALLOWED,
                line=physical_line,
            )
            _timestamp(event["timestamp"], line=physical_line)
            _integer(
                event["status"], label="upstream-error status", line=physical_line, maximum=999
            )
            for field in TOKEN_FIELDS:
                _integer(event[field], label=field, line=physical_line)
            if not isinstance(event["provider"], str) or event[
                "provider"
            ] not in ERROR_PROVIDERS:
                raise ValueError(f"line {physical_line}: undeclared upstream-error provider")
            error_code = event["error_code"]
            if not isinstance(error_code, str) or not (
                error_code in ERROR_CODES
                or (error_code.isdigit() and len(error_code) <= 6)
            ):
                raise ValueError(f"line {physical_line}: undeclared error_code")
            if not isinstance(event["error_type"], str) or event[
                "error_type"
            ] not in ERROR_TYPES:
                raise ValueError(f"line {physical_line}: undeclared error_type")
            errors[request_model] += 1

        unknown_cost = event["unknown_cost"]
        if not isinstance(unknown_cost, bool):
            raise ValueError(f"line {physical_line}: unknown_cost must be boolean")
        total_cost += _cost(
            event["cost_usd"], unknown_cost=unknown_cost, line=physical_line
        )
        unknown_cost_attempts += int(unknown_cost)

    if events == 0:
        raise ValueError("ledger contains no events")
    missing_completion_models = sorted(
        model for model, count in completions.items() if count < 1
    )
    if missing_completion_models:
        raise ValueError(
            "ledger lacks a completion for request model(s): "
            + ", ".join(missing_completion_models)
        )

    return {
        "schema": SCHEMA,
        "complete": True,
        "ledger_path": str(path),
        "ledger_sha256": hashlib.sha256(payload).hexdigest(),
        "snapshot_bytes": len(payload),
        "events": events,
        "event_types": sorted(EVENT_TYPES),
        "request_models": sorted(REQUEST_MODELS),
        "accepted_responses_by_request": {
            model: sorted(RESPONSE_MODELS_BY_REQUEST[model])
            for model in sorted(RESPONSE_MODELS_BY_REQUEST)
        },
        "completion_providers": sorted(PROVIDERS),
        "completion_counts_by_request": completions,
        "upstream_error_counts_by_request": errors,
        "response_counts": dict(sorted(response_counts.items())),
        "provider_counts": dict(sorted(provider_counts.items())),
        "unknown_cost_attempts": unknown_cost_attempts,
        "cost_usd": _decimal_text(total_cost),
        "credential_material_recorded": False,
        "model_content_recorded": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = verify_ledger(args.ledger)
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            if args.output.exists() or args.output.is_symlink():
                raise ValueError(f"refusing to overwrite output: {args.output}")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
