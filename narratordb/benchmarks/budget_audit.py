#!/usr/bin/env python3
"""Audit aggregate hosted-model campaign spend without model content."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import stat
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from .history import SECRET_PATTERNS


LEGACY_SCHEMA = "narratordb.campaign-budget-declaration.v1"
SCHEMA = "narratordb.campaign-budget-declaration.v2"
REPORT_SCHEMA = "narratordb.campaign-budget-audit.v1"
MAX_DECLARATION_BYTES = 1024 * 1024
MAX_LEDGER_LINE_BYTES = 64 * 1024

_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SENSITIVE_FIELD = re.compile(
    r"(?:^|_)(?:api_key|access_token|bearer_token|authorization|password|secret|credential)(?:$|_)",
    re.IGNORECASE,
)
_EXTRA_SECRET_PATTERNS = (
    ("private key", re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("bearer credential", re.compile(rb"\bBearer[ \t]+[A-Za-z0-9._~+/-]{20,}", re.I)),
)
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
_COMMON_LEDGER_FIELDS = {
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
    "error_code",
}
_COMPILER_USAGE_LEDGER_FIELDS = _COMMON_LEDGER_FIELDS | {
    "attempt",
    "cost_source",
    "router_attempt",
    "attempted_providers",
    "attempt_statuses",
}
_COMPILER_USAGE_REQUIRED_FIELDS = {
    "timestamp",
    "event",
    "request_model",
    "response_model",
    "provider",
    "attempt",
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cost_usd",
    "cost_source",
    "finish_reason",
}
_COMPILER_USAGE_OPTIONAL_FIELDS = {
    "unknown_cost",
    "router_attempt",
    "attempted_providers",
    "attempt_statuses",
}
_EVALUATOR_COMPLETION_REQUIRED_FIELDS = {
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
}
_EVALUATOR_ERROR_REQUIRED_FIELDS = {
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
}
_EVALUATOR_COMPLETION_OPTIONAL_FIELDS = {"unknown_cost"}
_EVALUATOR_ERROR_OPTIONAL_FIELDS = {"cached_tokens", "error_type", "unknown_cost"}
_COMPILER_ERROR_REQUIRED_FIELDS = {
    "timestamp",
    "event",
    "request_model",
    "attempt",
    "code",
    "status",
    "retryable",
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cost_usd",
}
_COMPILER_ERROR_OPTIONAL_FIELDS = {
    "unknown_cost",
    "retry_after_seconds",
    "rate_limit_reset_at",
    "rate_limit_limit",
    "rate_limit_remaining",
    "error_type",
    "provider",
    "provider_code",
    "router_attempt",
    "attempted_providers",
    "attempt_statuses",
}
_COMPILER_ERROR_LEDGER_FIELDS = (
    _COMPILER_ERROR_REQUIRED_FIELDS | _COMPILER_ERROR_OPTIONAL_FIELDS
)
_COMPILER_ERROR_INTEGER_FIELDS = {
    "attempt",
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "rate_limit_limit",
    "rate_limit_remaining",
    "router_attempt",
}
_COMPILER_ERROR_CODE_FIELDS = {
    "request_model": 160,
    "code": 80,
    "error_type": 64,
    "provider_code": 40,
}
_CODE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
_MODEL_ID = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
)
_PROVIDER_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._/-]{0,31}"
    r"(?: [A-Za-z0-9][A-Za-z0-9._/-]{0,31}){0,4}"
)
_INTEGER_LEDGER_FIELDS = {
    "status",
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "attempt",
}
_STRING_LEDGER_FIELDS = {
    "timestamp",
    "event",
    "request_model",
    "response_model",
    "provider",
    "finish_reason",
    "cost_source",
    "error_code",
}
_IDENTITY_SENTINELS = {"route_mismatch", "unknown"}
_COMPILER_FINISH_REASONS = {
    "stop",
    "length",
    "content_filter",
    "tool_calls",
    "function_call",
    "unknown",
}
_COMPILER_COST_SOURCES = {"provider", "estimated", "subscription", "unavailable"}
_COMPILER_ERROR_TYPES = {
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
_COMPILER_PROVIDER_CODES = {
    "rate_limited",
    "overloaded",
    "timeout",
    "unavailable",
    "unknown",
}
_COMPILER_ERROR_CODES = {
    "compiler_error",
    "content_filtered",
    "cost_limit_reached",
    "http_error",
    "incomplete_completion",
    "incomplete_openrouter_route",
    "invalid_compiled_memory",
    "invalid_json",
    "invalid_response_envelope",
    "missing_api_key",
    "missing_completion",
    "missing_local_endpoint",
    "missing_local_model",
    "model_refusal",
    "model_route_mismatch",
    "network_error",
    "http_protocol_error",
    "provider_route_mismatch",
    "response_too_large",
    "unsafe_hosted_privacy",
    "unsupported_local_endpoint",
    "upstream_error",
}
_EVALUATOR_FINISH_REASONS = _COMPILER_FINISH_REASONS | {"error"}
_EVALUATOR_ERROR_CODES = _COMPILER_PROVIDER_CODES


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_json_bytes(payload: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            payload,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} is not UTF-8") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON in {label}: {error}") from error


def _scan_for_secrets(payload: bytes, *, label: str) -> None:
    for secret_label, pattern in (*SECRET_PATTERNS, *_EXTRA_SECRET_PATTERNS):
        if pattern.search(payload):
            raise ValueError(f"{secret_label} detected in {label}")


def _reject_sensitive_keys(value: Any, *, label: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SENSITIVE_FIELD.search(str(key)):
                raise ValueError(f"sensitive field is not allowed in {label}: {key}")
            _reject_sensitive_keys(child, label=label)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_keys(child, label=label)


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    expected: set[str],
    label: str,
) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise ValueError(f"invalid {label} fields: missing={missing}, extra={extra}")


def _money(value: Any, *, label: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{label} must be a decimal amount")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label} must be a decimal amount") from error
    if not amount.is_finite():
        raise ValueError(f"{label} must be finite")
    if amount < 0:
        raise ValueError(f"{label} cannot be negative")
    if positive and amount <= 0:
        raise ValueError(f"{label} must be positive")
    if amount.adjusted() > 1000 or amount.adjusted() < -1000:
        raise ValueError(f"{label} is outside the supported decimal range")
    return amount


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _source_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SOURCE_ID.fullmatch(value):
        raise ValueError(
            f"{label} must match {_SOURCE_ID.pattern!r} and be at most 128 characters"
        )
    return value


def _declared_path(value: Any, *, declaration_directory: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = declaration_directory / path
    if path.is_symlink():
        raise ValueError(f"{label} may not be a symlink: {value}")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"{label} not found: {value}") from error
    mode = resolved.stat(follow_symlinks=False).st_mode
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a regular file: {value}")
    return resolved


def _file_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def _hash_and_scan_immutable(path: Path, *, label: str) -> tuple[str, int]:
    before = path.stat(follow_symlinks=False)
    digest = hashlib.sha256()
    size = 0
    carry = b""
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            searchable = carry + chunk
            _scan_for_secrets(searchable, label=label)
            carry = searchable[-256:]
    after = path.stat(follow_symlinks=False)
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity or size != after.st_size:
        raise RuntimeError(f"immutable source changed while being audited: {label}")
    return digest.hexdigest(), size


def _nonnegative_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{label} must be a non-negative integer")
    if isinstance(value, Decimal) and (
        not value.is_finite() or value != value.to_integral()
    ):
        raise ValueError(f"{label} must be a non-negative integer")
    rendered = int(value)
    if rendered < 0:
        raise ValueError(f"{label} cannot be negative")
    if rendered > (1 << 63) - 1:
        raise ValueError(f"{label} exceeds the supported integer range")
    return rendered


def _validate_timestamp(value: Any, *, label: str) -> None:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"{label} timestamp must be an ISO-8601 string")
    try:
        parsed_timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} timestamp must be an ISO-8601 string") from error
    if parsed_timestamp.tzinfo is None:
        raise ValueError(f"{label} timestamp must include a timezone")


def _validate_unknown_cost_marker(event: Mapping[str, Any], *, label: str) -> None:
    if "unknown_cost" in event and not isinstance(event["unknown_cost"], bool):
        raise ValueError(f"{label} unknown_cost must be boolean")


def _validate_compiler_model(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or len(value) > 192:
        raise ValueError(f"{label} must be a sanitized model identifier")
    if value != "unknown" and not _MODEL_ID.fullmatch(value):
        raise ValueError(f"{label} must be a sanitized model identifier")
    return value


def _validate_event_model(
    value: Any,
    *,
    label: str,
    allowed_models: frozenset[str] | None,
) -> str:
    model = _validate_compiler_model(value, label=label)
    if (
        model != "unknown"
        and allowed_models is not None
        and model not in allowed_models
    ):
        raise ValueError(f"{label} is not declared by the ledger identity policy")
    return model


def _validate_compiler_provider(
    value: Any,
    *,
    label: str,
    allowed_providers: frozenset[str] | None,
) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError(f"{label} must be a canonical provider identity")
    if value not in _IDENTITY_SENTINELS and (
        not _PROVIDER_NAME.fullmatch(value)
        or value.casefold().startswith(("sk-", "sk_", "bearer"))
    ):
        raise ValueError(f"{label} must be a canonical provider identity")
    if (
        value not in _IDENTITY_SENTINELS
        and allowed_providers is not None
        and value not in allowed_providers
    ):
        raise ValueError(f"{label} is not declared by the ledger identity policy")
    return value


def _parse_identity_policy(
    value: Any,
    *,
    kind: str,
    label: str,
) -> dict[str, frozenset[str]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    expected = {"request_models", "providers"}
    if kind == "evaluator":
        expected.add("response_models")
    _require_exact_keys(value, expected=expected, label=label)
    parsed: dict[str, frozenset[str]] = {}
    for key in sorted(expected):
        entries = value[key]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
            raise ValueError(f"{label}.{key} must contain 1 to 32 identities")
        if any(not isinstance(item, str) for item in entries):
            raise ValueError(f"{label}.{key} identities must be strings")
        if len({item.casefold() for item in entries}) != len(entries):
            raise ValueError(f"{label}.{key} identities must be unique")
        if key == "providers":
            for index, provider in enumerate(entries):
                if provider in _IDENTITY_SENTINELS:
                    raise ValueError(f"{label}.{key}[{index}] is a reserved sentinel")
                _validate_compiler_provider(
                    provider,
                    label=f"{label}.{key}[{index}]",
                    allowed_providers=None,
                )
        else:
            for index, model in enumerate(entries):
                if model == "unknown":
                    raise ValueError(f"{label}.{key}[{index}] is a reserved sentinel")
                _validate_compiler_model(model, label=f"{label}.{key}[{index}]")
        parsed[key] = frozenset(entries)
    return parsed


def _validate_compiler_error_code(value: Any, *, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical compiler error code")
    if value in _COMPILER_ERROR_CODES or re.fullmatch(
        r"(?:http|upstream)_[0-9]{3}", value
    ):
        return
    raise ValueError(f"{label} must be a canonical compiler error code")


def _validate_compiler_usage_event(
    event: Mapping[str, Any],
    *,
    label: str,
    identity_policy: Mapping[str, frozenset[str]] | None,
) -> Decimal:
    keys = set(event)
    allowed = _COMPILER_USAGE_REQUIRED_FIELDS | _COMPILER_USAGE_OPTIONAL_FIELDS
    missing = sorted(_COMPILER_USAGE_REQUIRED_FIELDS - keys)
    unexpected = sorted(keys - allowed)
    if missing or unexpected:
        raise ValueError(
            f"invalid {label} fields: missing={missing}, extra={unexpected}"
        )
    _validate_timestamp(event["timestamp"], label=label)
    _validate_unknown_cost_marker(event, label=label)
    request_model = _validate_event_model(
        event["request_model"],
        label=f"{label} request_model",
        allowed_models=(
            identity_policy["request_models"] if identity_policy is not None else None
        ),
    )
    response_model = event["response_model"]
    if response_model not in _IDENTITY_SENTINELS and response_model != request_model:
        raise ValueError(
            f"{label} response_model must equal request_model or a route sentinel"
        )
    _validate_compiler_provider(
        event["provider"],
        label=f"{label} provider",
        allowed_providers=(
            identity_policy["providers"] if identity_policy is not None else None
        ),
    )
    if event["finish_reason"] not in _COMPILER_FINISH_REASONS:
        raise ValueError(f"{label} finish_reason is not canonical")
    if event["cost_source"] not in _COMPILER_COST_SOURCES:
        raise ValueError(f"{label} cost_source is not canonical")
    for key in (
        "attempt",
        "prompt_tokens",
        "cached_tokens",
        "completion_tokens",
        "reasoning_tokens",
    ):
        number = _nonnegative_integer(event[key], label=f"{label} {key}")
        if key == "attempt" and number == 0:
            raise ValueError(f"{label} attempt must be positive")
    _validate_router_attempt_metadata(
        event,
        label=label,
        allowed_providers=(
            identity_policy["providers"] if identity_policy is not None else None
        ),
    )
    cost = event["cost_usd"]
    if isinstance(cost, bool) or not isinstance(cost, (int, Decimal)):
        raise ValueError(f"{label} cost_usd must be a non-negative number")
    return _money(cost, label=f"{label} cost_usd")


def _validate_compiler_error_event(
    event: Mapping[str, Any],
    *,
    label: str,
    identity_policy: Mapping[str, frozenset[str]] | None,
) -> Decimal:
    keys = set(event)
    missing = sorted(_COMPILER_ERROR_REQUIRED_FIELDS - keys)
    unexpected = sorted(keys - _COMPILER_ERROR_LEDGER_FIELDS)
    if missing or unexpected:
        raise ValueError(
            f"invalid {label} fields: missing={missing}, extra={unexpected}"
        )

    _validate_timestamp(event["timestamp"], label=label)
    _validate_unknown_cost_marker(event, label=label)

    for key, max_length in _COMPILER_ERROR_CODE_FIELDS.items():
        if key not in event:
            continue
        value = event[key]
        if (
            not isinstance(value, str)
            or len(value) > max_length
            or not _CODE_TOKEN.fullmatch(value)
        ):
            raise ValueError(f"{label} {key} must be a sanitized code token")

    _validate_event_model(
        event["request_model"],
        label=f"{label} request_model",
        allowed_models=(
            identity_policy["request_models"] if identity_policy is not None else None
        ),
    )
    _validate_compiler_error_code(event["code"], label=f"{label} code")
    if "error_type" in event and event["error_type"] not in _COMPILER_ERROR_TYPES:
        raise ValueError(f"{label} error_type is not canonical")
    if "provider_code" in event:
        provider_code = event["provider_code"]
        if provider_code not in _COMPILER_PROVIDER_CODES and not (
            isinstance(provider_code, str)
            and provider_code.isdigit()
            and len(provider_code) <= 6
        ):
            raise ValueError(f"{label} provider_code is not canonical")

    if "provider" in event:
        _validate_compiler_provider(
            event["provider"],
            label=f"{label} provider",
            allowed_providers=(
                identity_policy["providers"] if identity_policy is not None else None
            ),
        )

    for key in sorted(keys & _COMPILER_ERROR_INTEGER_FIELDS):
        value = _nonnegative_integer(event[key], label=f"{label} {key}")
        if key == "attempt" and value == 0:
            raise ValueError(f"{label} attempt must be positive")

    status = event["status"]
    if status is not None:
        parsed_status = _nonnegative_integer(status, label=f"{label} status")
        if parsed_status > 999:
            raise ValueError(f"{label} status exceeds the supported limit")
    if not isinstance(event["retryable"], bool):
        raise ValueError(f"{label} retryable must be boolean")
    _validate_router_attempt_metadata(
        event,
        label=label,
        allowed_providers=(
            identity_policy["providers"] if identity_policy is not None else None
        ),
    )

    for key in ("retry_after_seconds", "rate_limit_reset_at"):
        if key not in event:
            continue
        value = event[key]
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise ValueError(f"{label} {key} must be a non-negative number")
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError(f"{label} {key} must be a non-negative number")
        if key == "retry_after_seconds" and number > 86400:
            raise ValueError(f"{label} retry_after_seconds exceeds the supported limit")

    cost = event["cost_usd"]
    if isinstance(cost, bool) or not isinstance(cost, (int, Decimal)):
        raise ValueError(f"{label} cost_usd must be a non-negative number")
    return _money(cost, label=f"{label} cost_usd")


def _validate_router_attempt_metadata(
    event: Mapping[str, Any],
    *,
    label: str,
    allowed_providers: frozenset[str] | None,
) -> None:
    router_attempt = event.get("router_attempt")
    if router_attempt is not None:
        parsed_router_attempt = _nonnegative_integer(
            router_attempt, label=f"{label} router_attempt"
        )
        if parsed_router_attempt == 0:
            raise ValueError(f"{label} router_attempt must be positive")
    has_providers = "attempted_providers" in event
    has_statuses = "attempt_statuses" in event
    if has_providers != has_statuses:
        raise ValueError(f"{label} route-attempt arrays must be paired")
    if not has_providers:
        return
    providers = event["attempted_providers"]
    statuses = event["attempt_statuses"]
    if not isinstance(providers, list) or not isinstance(statuses, list):
        raise ValueError(f"{label} route-attempt metadata must use arrays")
    if len(providers) != len(statuses) or len(providers) > 16:
        raise ValueError(
            f"{label} route-attempt arrays must align and contain at most 16 entries"
        )
    for index, (provider, status) in enumerate(zip(providers, statuses)):
        _validate_compiler_provider(
            provider,
            label=f"{label} attempted_providers[{index}]",
            allowed_providers=allowed_providers,
        )
        parsed_status = _nonnegative_integer(
            status,
            label=f"{label} attempt_statuses[{index}]",
        )
        if parsed_status > 999:
            raise ValueError(
                f"{label} attempt_statuses[{index}] exceeds the supported limit"
            )


def _validate_evaluator_event(
    event: Mapping[str, Any],
    *,
    label: str,
    identity_policy: Mapping[str, frozenset[str]] | None,
) -> Decimal:
    event_type = event["event"]
    if event_type == "completion":
        required = _EVALUATOR_COMPLETION_REQUIRED_FIELDS
        allowed = required | _EVALUATOR_COMPLETION_OPTIONAL_FIELDS
    else:
        required = _EVALUATOR_ERROR_REQUIRED_FIELDS
        allowed = required | _EVALUATOR_ERROR_OPTIONAL_FIELDS
    keys = set(event)
    missing = sorted(required - keys)
    unexpected = sorted(keys - allowed)
    if missing or unexpected:
        raise ValueError(
            f"invalid {label} fields: missing={missing}, extra={unexpected}"
        )
    _validate_timestamp(event["timestamp"], label=label)
    _validate_unknown_cost_marker(event, label=label)
    request_model = _validate_event_model(
        event["request_model"],
        label=f"{label} request_model",
        allowed_models=(
            identity_policy["request_models"] if identity_policy is not None else None
        ),
    )
    _validate_compiler_provider(
        event["provider"],
        label=f"{label} provider",
        allowed_providers=(
            identity_policy["providers"] if identity_policy is not None else None
        ),
    )
    status = _nonnegative_integer(event["status"], label=f"{label} status")
    if status > 999:
        raise ValueError(f"{label} status exceeds the supported limit")
    token_fields = (
        "prompt_tokens",
        "cached_tokens",
        "completion_tokens",
        "reasoning_tokens",
    )
    for key in token_fields:
        if key in event:
            _nonnegative_integer(event[key], label=f"{label} {key}")
    if event_type == "completion":
        response_model = event["response_model"]
        if response_model not in _IDENTITY_SENTINELS:
            response_model = _validate_event_model(
                response_model,
                label=f"{label} response_model",
                allowed_models=(
                    identity_policy["response_models"]
                    if identity_policy is not None
                    else None
                ),
            )
            if response_model != request_model and not re.fullmatch(
                re.escape(response_model) + r"-[0-9]{8}", request_model
            ):
                raise ValueError(
                    f"{label} response_model is not the request model or its dated alias"
                )
        if event["finish_reason"] not in _EVALUATOR_FINISH_REASONS:
            raise ValueError(f"{label} finish_reason is not canonical")
        if not isinstance(event["response_complete"], bool):
            raise ValueError(f"{label} response_complete must be boolean")
    else:
        error_code = event["error_code"]
        if not isinstance(error_code, str) or not (
            error_code in _EVALUATOR_ERROR_CODES
            or error_code == "unknown"
            or (error_code.isdigit() and len(error_code) <= 6)
        ):
            raise ValueError(f"{label} error_code is not canonical")
        if "error_type" in event and event["error_type"] not in _COMPILER_ERROR_TYPES:
            raise ValueError(f"{label} error_type is not canonical")
    cost = event["cost_usd"]
    if isinstance(cost, bool) or not isinstance(cost, (int, Decimal)):
        raise ValueError(f"{label} cost_usd must be a non-negative number")
    return _money(cost, label=f"{label} cost_usd")


def _validate_ledger_event(
    event: Any,
    *,
    kind: str,
    line_number: int,
    identity_policy: Mapping[str, frozenset[str]] | None,
) -> Decimal:
    label = f"{kind} usage ledger line {line_number}"
    if not isinstance(event, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    keys = set(event)
    forbidden = sorted(keys & _FORBIDDEN_CONTENT_FIELDS)
    if forbidden:
        raise ValueError(f"model content fields are forbidden in {label}: {forbidden}")
    if "event" not in event or "cost_usd" not in event:
        raise ValueError(f"{label} requires event and cost_usd")
    expected_events = (
        {"compiler_usage", "compiler_error"}
        if kind == "compiler"
        else {"completion", "upstream_error"}
    )
    event_type = event["event"]
    if not isinstance(event_type, str) or event_type not in expected_events:
        raise ValueError(f"unexpected event type in {label}")
    if kind == "compiler" and event_type == "compiler_error":
        return _validate_compiler_error_event(
            event, label=label, identity_policy=identity_policy
        )

    if kind == "compiler":
        return _validate_compiler_usage_event(
            event, label=label, identity_policy=identity_policy
        )
    return _validate_evaluator_event(
        event, label=label, identity_policy=identity_policy
    )


def _audit_usage_ledger(
    path: Path,
    *,
    kind: str,
    label: str,
    identity_policy: Mapping[str, frozenset[str]] | None,
) -> dict[str, Any]:
    before = path.stat(follow_symlinks=False)
    initial_size = before.st_size
    remaining = initial_size
    digest = hashlib.sha256()
    events = 0
    cost = Decimal("0")
    unknown_cost_attempts = 0
    invalid_completion_identities = 0
    with path.open("rb") as source:
        while remaining:
            raw_line = source.readline(min(remaining, MAX_LEDGER_LINE_BYTES + 1))
            if not raw_line:
                raise RuntimeError(
                    f"usage ledger was truncated while being audited: {label}"
                )
            remaining -= len(raw_line)
            digest.update(raw_line)
            _scan_for_secrets(raw_line, label=label)
            if len(raw_line) > MAX_LEDGER_LINE_BYTES:
                raise ValueError(f"usage ledger line exceeds metadata limit: {label}")
            if not raw_line.endswith(b"\n"):
                if remaining:
                    raise ValueError(
                        f"usage ledger line exceeds metadata limit: {label}"
                    )
                raise ValueError(f"usage ledger has an incomplete final line: {label}")
            if not raw_line.strip():
                continue
            events += 1
            event = _load_json_bytes(raw_line, label=f"{label} line {events}")
            cost += _validate_ledger_event(
                event,
                kind=kind,
                line_number=events,
                identity_policy=identity_policy,
            )
            unknown_cost_attempts += int(event.get("unknown_cost") is True)
            if event.get("event") in {"compiler_usage", "completion"} and (
                event.get("response_model") in _IDENTITY_SENTINELS
                or event.get("provider") in _IDENTITY_SENTINELS
            ):
                invalid_completion_identities += 1
    after = path.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RuntimeError(f"usage ledger was replaced while being audited: {label}")
    if after.st_size < initial_size:
        raise RuntimeError(f"usage ledger was truncated while being audited: {label}")
    if after.st_size == initial_size and after.st_mtime_ns != before.st_mtime_ns:
        raise RuntimeError(f"usage ledger was modified while being audited: {label}")
    return {
        "sha256_prefix": digest.hexdigest(),
        "snapshot_bytes": initial_size,
        "appended_during_audit": after.st_size > initial_size,
        "events": events,
        "unknown_cost_attempts": unknown_cost_attempts,
        "invalid_completion_identities": invalid_completion_identities,
        "cost": cost,
    }


def _read_declaration(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink():
        raise ValueError(f"declaration may not be a symlink: {path}")
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"declaration not found: {path}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"declaration must be a regular file: {path}")
    if metadata.st_size > MAX_DECLARATION_BYTES:
        raise ValueError("campaign budget declaration exceeds the 1 MiB limit")
    payload = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    before_identity = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if len(payload) != metadata.st_size or before_identity != after_identity:
        raise RuntimeError("campaign budget declaration changed while being read")
    _scan_for_secrets(payload, label="campaign budget declaration")
    declaration = _load_json_bytes(payload, label="campaign budget declaration")
    if not isinstance(declaration, dict):
        raise ValueError("campaign budget declaration must be a JSON object")
    _reject_sensitive_keys(declaration, label="campaign budget declaration")
    return declaration, payload


def audit_campaign_budget(
    declaration_path: Path,
    *,
    provider_cap_usd: str | int | float | Decimal | None = None,
) -> dict[str, Any]:
    """Return a read-only aggregate spend audit for one declared campaign."""

    declaration_path = declaration_path.expanduser()
    if not declaration_path.is_absolute():
        declaration_path = Path.cwd() / declaration_path
    declaration, declaration_payload = _read_declaration(declaration_path)
    declaration_path = declaration_path.resolve(strict=True)
    _require_exact_keys(
        declaration,
        expected={
            "schema",
            "campaign_id",
            "provider_cap_usd",
            "governance_ceiling_eur",
            "prior_immutable_costs",
            "active_usage_ledgers",
        },
        label="campaign budget declaration",
    )
    declaration_schema = declaration["schema"]
    if declaration_schema not in {SCHEMA, LEGACY_SCHEMA}:
        raise ValueError(
            f"unsupported campaign budget declaration schema: {declaration['schema']}"
        )
    strict_identity_policy = declaration_schema == SCHEMA
    campaign_id = _source_id(declaration["campaign_id"], label="campaign_id")
    declared_cap = _money(
        declaration["provider_cap_usd"],
        label="provider_cap_usd",
        positive=True,
    )
    enforced_cap = (
        declared_cap
        if provider_cap_usd is None
        else _money(provider_cap_usd, label="provider cap override", positive=True)
    )
    governance_ceiling = _money(
        declaration["governance_ceiling_eur"],
        label="governance_ceiling_eur",
        positive=True,
    )
    prior = declaration["prior_immutable_costs"]
    ledgers = declaration["active_usage_ledgers"]
    if not isinstance(prior, list) or not isinstance(ledgers, list):
        raise ValueError(
            "prior_immutable_costs and active_usage_ledgers must be arrays"
        )
    if not prior and not ledgers:
        raise ValueError(
            "campaign budget declaration must contain at least one spend source"
        )

    seen_ids: set[str] = set()
    seen_paths: dict[Path, str] = {}
    seen_identities: dict[tuple[int, int], str] = {}
    seen_hashes: dict[str, str] = {}
    sources: list[dict[str, Any]] = []
    prior_cost = Decimal("0")
    ledger_cost = Decimal("0")
    unknown_cost_attempts = 0
    invalid_completion_identities = 0

    def check_source_identity(source_id: str, path: Path) -> None:
        if source_id in seen_ids:
            raise ValueError(f"duplicate spend source id: {source_id}")
        if path in seen_paths:
            raise ValueError(
                f"duplicate spend source path: {source_id} duplicates {seen_paths[path]}"
            )
        identity = _file_identity(path)
        if identity in seen_identities:
            raise ValueError(
                f"duplicate spend source file: {source_id} duplicates {seen_identities[identity]}"
            )

    def register_source(source_id: str, path: Path, digest: str) -> None:
        check_source_identity(source_id, path)
        seen_ids.add(source_id)
        seen_paths[path] = source_id
        identity = _file_identity(path)
        seen_identities[identity] = source_id
        if digest in seen_hashes:
            raise ValueError(
                f"duplicate spend source content: {source_id} duplicates {seen_hashes[digest]}"
            )
        seen_hashes[digest] = source_id

    declaration_directory = declaration_path.parent
    for index, item in enumerate(prior):
        label = f"prior_immutable_costs[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{label} must be a JSON object")
        _require_exact_keys(
            item,
            expected={"source_id", "record_path", "record_sha256", "cost_usd"},
            label=label,
        )
        source_id = _source_id(item["source_id"], label=f"{label}.source_id")
        expected_hash = item["record_sha256"]
        if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            raise ValueError(f"{label}.record_sha256 must be a lowercase SHA-256")
        path = _declared_path(
            item["record_path"],
            declaration_directory=declaration_directory,
            label=f"{label}.record_path",
        )
        check_source_identity(source_id, path)
        digest, size = _hash_and_scan_immutable(path, label=source_id)
        if digest != expected_hash:
            raise ValueError(
                f"immutable record checksum mismatch for {source_id}: expected {expected_hash}, got {digest}"
            )
        register_source(source_id, path, digest)
        cost = _money(item["cost_usd"], label=f"{label}.cost_usd")
        prior_cost += cost
        sources.append(
            {
                "source_id": source_id,
                "source_type": "prior_immutable_record",
                "path": str(item["record_path"]),
                "sha256": digest,
                "snapshot_bytes": size,
                "cost_usd": _decimal_text(cost),
            }
        )

    for index, item in enumerate(ledgers):
        label = f"active_usage_ledgers[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{label} must be a JSON object")
        _require_exact_keys(
            item,
            expected={"source_id", "kind", "path", "identity_policy"}
            if strict_identity_policy
            else {"source_id", "kind", "path"},
            label=label,
        )
        source_id = _source_id(item["source_id"], label=f"{label}.source_id")
        kind = item["kind"]
        if not isinstance(kind, str) or kind not in {"compiler", "evaluator"}:
            raise ValueError(f"{label}.kind must be compiler or evaluator")
        identity_policy = (
            _parse_identity_policy(
                item["identity_policy"],
                kind=kind,
                label=f"{label}.identity_policy",
            )
            if strict_identity_policy
            else None
        )
        path = _declared_path(
            item["path"],
            declaration_directory=declaration_directory,
            label=f"{label}.path",
        )
        check_source_identity(source_id, path)
        ledger = _audit_usage_ledger(
            path,
            kind=kind,
            label=source_id,
            identity_policy=identity_policy,
        )
        digest = ledger["sha256_prefix"]
        register_source(source_id, path, digest)
        cost = ledger.pop("cost")
        ledger_cost += cost
        unknown_cost_attempts += int(ledger["unknown_cost_attempts"])
        invalid_completion_identities += int(ledger["invalid_completion_identities"])
        sources.append(
            {
                "source_id": source_id,
                "source_type": f"active_{kind}_usage_ledger",
                "path": str(item["path"]),
                "identity_policy": (
                    "declared_closed_world"
                    if strict_identity_policy
                    else "legacy_syntax_only"
                ),
                **ledger,
                "cost_usd": _decimal_text(cost),
            }
        )

    observed = prior_cost + ledger_cost
    within_cap = observed <= enforced_cap
    publication_ready = (
        within_cap and unknown_cost_attempts == 0 and invalid_completion_identities == 0
    )
    remaining = max(Decimal("0"), enforced_cap - observed)
    overage = max(Decimal("0"), observed - enforced_cap)
    return {
        "schema": REPORT_SCHEMA,
        "campaign_id": campaign_id,
        "complete": publication_ready,
        "unknown_cost_attempts": unknown_cost_attempts,
        "invalid_completion_identities": invalid_completion_identities,
        "publication_readiness": {
            "ready": publication_ready,
            "unknown_costs_reconciled": unknown_cost_attempts == 0,
            "successful_completion_identities_verified": (
                invalid_completion_identities == 0
            ),
            "unknown_cost_policy": (
                "conservative request reservations remain incomplete until "
                "reconciled to provider billing"
            ),
        },
        "declaration_sha256": hashlib.sha256(declaration_payload).hexdigest(),
        "aggregation_policy": {
            "basis": "declared immutable costs plus allowlisted usage metadata",
            "prompt_or_response_content_permitted": False,
            "currency_conversion_performed": False,
            "compiler_and_evaluator_identity_policy": (
                "declared_closed_world"
                if strict_identity_policy
                else "legacy_v1_syntax_only"
            ),
        },
        "observed_spend_usd": _decimal_text(observed),
        "spend_breakdown_usd": {
            "prior_immutable_records": _decimal_text(prior_cost),
            "active_usage_ledgers": _decimal_text(ledger_cost),
        },
        "provider_limit": {
            "currency": "USD",
            "declared_cap_usd": _decimal_text(declared_cap),
            "enforced_cap_usd": _decimal_text(enforced_cap),
            "cap_source": "declaration" if provider_cap_usd is None else "cli_override",
            "remaining_headroom_usd": _decimal_text(remaining),
            "overage_usd": _decimal_text(overage),
            "within_cap": within_cap,
        },
        "governance_limit": {
            "currency": "EUR",
            "ceiling_eur": _decimal_text(governance_ceiling),
            "observed_eur": None,
            "remaining_headroom_eur": None,
            "status": "recorded_not_cross_currency_evaluated",
            "reason": "no FX conversion was declared or performed",
        },
        "sources": sources,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument(
        "--provider-cap-usd",
        help="Explicit USD cap override; the declaration's cap remains reported",
    )
    args = parser.parse_args(argv)
    try:
        report = audit_campaign_budget(
            args.declaration,
            provider_cap_usd=args.provider_cap_usd,
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2, sort_keys=True) + "\n", end="")
    return 0 if report["provider_limit"]["within_cap"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
