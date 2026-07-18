#!/usr/bin/env python3
"""Fail-closed official OpenAI proxy for the prospective V18 R3 pair.

This process is deliberately a transport, not an evaluator.  It pins one
official endpoint and model snapshot, applies fixed request controls, owns
content-free accounting, and never stores prompt or completion content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


OFFICIAL_UPSTREAM = "https://api.openai.com/v1/chat/completions"
PINNED_MODEL = "gpt-5.4-mini-2026-03-17"
PROVIDER_IDENTITY = "OpenAI"
ENDPOINT_IDENTITY = "api.openai.com/v1/chat/completions"
PRICING_EVIDENCE_SHA256 = (
    "41e6f74aab48e82f3854fff2c6a6425a4b7c13879dc3006674526d9190a41870"
)
PINNED_TIMEOUT_SECONDS = 105.0
MAX_LOGICAL_ATTEMPTS = 5
MAX_DISCARDED_TRANSIENTS = 4
DEFAULT_MAX_REQUEST_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

INPUT_USD_PER_MTOK = Decimal("0.75")
CACHED_INPUT_USD_PER_MTOK = Decimal("0.075")
OUTPUT_USD_PER_MTOK = Decimal("4.50")
ONE_MILLION = Decimal("1000000")
MONEY_QUANTUM = Decimal("0.000000001")

_RETRYABLE_HTTP_STATUSES = {408, 409, 500, 502, 503, 504}
_QUOTA_CODES = {
    "billing_hard_limit_reached",
    "billing_not_active",
    "insufficient_quota",
    "quota_exceeded",
}
_QUOTA_TYPES = {
    "billing_error",
    "insufficient_quota",
    "quota_exceeded",
}
_KNOWN_FINISH_REASONS = {
    "stop",
    "length",
    "content_filter",
    "tool_calls",
    "function_call",
    "error",
}
_FATAL_REASON_CODES = {
    "attempt_limit",
    "budget_fuse",
    "concurrent_payload",
    "hidden_sdk_retry",
    "internal_invariant",
    "invalid_local_request",
    "retry_limit",
    "terminal_http_error",
    "terminal_response",
}
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_MAX_INTEGER = (1 << 63) - 1


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


class _ThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_loads(payload: bytes | str) -> Any:
    return json.loads(
        payload,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON number is not allowed: {value}")
        ),
        object_pairs_hook=_unique_json_object,
    )


def _decimal(value: str | int | float | Decimal, *, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{label} must be a finite decimal") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be a non-negative finite decimal")
    return parsed


def _money(value: Decimal) -> str:
    return format(value.quantize(MONEY_QUANTUM), "f")


def verify_pricing_evidence(path: Path | None = None) -> dict[str, Any]:
    """Bind the proxy constants to the frozen official model/pricing evidence."""

    evidence_path = path or Path(__file__).with_name(
        "OFFICIAL_OPENAI_MODEL_AND_PRICING.json"
    )
    if evidence_path.is_symlink() or not evidence_path.is_file():
        raise ValueError("official model/pricing evidence is missing or a symlink")
    payload = evidence_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != PRICING_EVIDENCE_SHA256:
        raise ValueError("official model/pricing evidence checksum mismatch")
    parsed = _strict_json_loads(payload)
    expected = {
        "model_snapshot": PINNED_MODEL,
        "input_usd_per_million_tokens": str(INPUT_USD_PER_MTOK),
        "cached_input_usd_per_million_tokens": str(CACHED_INPUT_USD_PER_MTOK),
        "output_usd_per_million_tokens": str(OUTPUT_USD_PER_MTOK),
        "pricing_currency": "USD",
    }
    if not isinstance(parsed, dict) or any(
        parsed.get(field) != value for field, value in expected.items()
    ):
        raise ValueError("official model/pricing evidence fields mismatch")
    return parsed


def exact_official_cost_usd(
    *, prompt_tokens: int, cached_tokens: int, completion_tokens: int
) -> Decimal:
    """Return exact official cost; reasoning is already in completion tokens."""

    for name, value in (
        ("prompt_tokens", prompt_tokens),
        ("cached_tokens", cached_tokens),
        ("completion_tokens", completion_tokens),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative exact integer")
    if cached_tokens > prompt_tokens:
        raise ValueError("cached_tokens must not exceed prompt_tokens")
    uncached = prompt_tokens - cached_tokens
    return (
        Decimal(uncached) * INPUT_USD_PER_MTOK
        + Decimal(cached_tokens) * CACHED_INPUT_USD_PER_MTOK
        + Decimal(completion_tokens) * OUTPUT_USD_PER_MTOK
    ) / ONE_MILLION


def _validate_upstream(
    upstream: str, *, allow_insecure_test_upstream: bool = False
) -> str:
    if not allow_insecure_test_upstream:
        if upstream != OFFICIAL_UPSTREAM:
            raise ValueError("production upstream must be the exact official endpoint")
        return upstream
    parsed = urlsplit(upstream)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1/chat/completions"
    ):
        raise ValueError("test upstream must be an explicit loopback chat endpoint")
    return upstream


def prepare_official_payload(
    payload: Mapping[str, Any], *, max_completion_tokens: int
) -> dict[str, Any]:
    """Validate the caller request and apply immutable official controls."""

    if not isinstance(payload, Mapping):
        raise ValueError("request body must be a JSON object")
    prepared = dict(payload)
    if prepared.get("model") != PINNED_MODEL:
        raise ValueError("request model must be the exact pinned snapshot")
    if "max_tokens" in prepared:
        raise ValueError("legacy max_tokens is forbidden on the official route")
    supplied_limit = prepared.get("max_completion_tokens")
    if (
        isinstance(supplied_limit, bool)
        or not isinstance(supplied_limit, int)
        or supplied_limit != max_completion_tokens
    ):
        raise ValueError("max_completion_tokens must equal the frozen phase limit")
    if "temperature" in prepared:
        raise ValueError("temperature is forbidden for the pinned reasoning model")
    if "provider" in prepared or "reasoning" in prepared:
        raise ValueError("provider-router controls are forbidden on the official route")
    if prepared.get("stream") not in {None, False}:
        raise ValueError("streaming is not supported")
    prepared.pop("stream", None)
    for field, expected in (
        ("reasoning_effort", "high"),
        ("service_tier", "default"),
        ("store", False),
        ("n", 1),
    ):
        if field in prepared and prepared[field] != expected:
            raise ValueError(f"{field} conflicts with the frozen control")
        prepared[field] = expected
    messages = prepared.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    return prepared


def canonical_payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _exact_nonnegative_int(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 <= value <= _MAX_INTEGER
    )


def _usage_fields(response: Mapping[str, Any]) -> tuple[dict[str, int], Decimal] | None:
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    if (
        not _exact_nonnegative_int(prompt)
        or prompt <= 0
        or not _exact_nonnegative_int(completion)
        or completion <= 0
        or not _exact_nonnegative_int(total)
        or total != prompt + completion
    ):
        return None
    prompt_details = usage.get("prompt_tokens_details")
    if prompt_details is None:
        prompt_details = {}
    if not isinstance(prompt_details, Mapping):
        return None
    completion_details = usage.get("completion_tokens_details")
    if completion_details is None:
        completion_details = {}
    if not isinstance(completion_details, Mapping):
        return None
    cached = prompt_details.get("cached_tokens", 0)
    reasoning = completion_details.get("reasoning_tokens", 0)
    if (
        not _exact_nonnegative_int(cached)
        or cached > prompt
        or not _exact_nonnegative_int(reasoning)
        or reasoning > completion
    ):
        return None
    for mapping, fields in (
        (prompt_details, ("audio_tokens",)),
        (
            completion_details,
            (
                "audio_tokens",
                "accepted_prediction_tokens",
                "rejected_prediction_tokens",
            ),
        ),
    ):
        for field in fields:
            if field in mapping and mapping[field] not in {None, 0}:
                return None
            if field in mapping and mapping[field] is not None and not _exact_nonnegative_int(
                mapping[field]
            ):
                return None
    fields = {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
    }
    return fields, exact_official_cost_usd(
        prompt_tokens=prompt,
        cached_tokens=cached,
        completion_tokens=completion,
    )


def _visible_content_state(message: Mapping[str, Any]) -> str:
    if "content" not in message:
        return "missing"
    content = message.get("content")
    if content is None:
        return "null"
    if not isinstance(content, str):
        return "invalid"
    return "nonempty" if content.strip() else "blank"


def _observed_finish_class(choice: Mapping[str, Any]) -> str:
    if "finish_reason" not in choice:
        return "missing"
    raw = choice.get("finish_reason")
    if raw is None:
        return "null"
    if not isinstance(raw, str):
        return "invalid_type"
    return raw if raw in _KNOWN_FINISH_REASONS else "unknown_string"


def _safe_request_id(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip()
    return normalized if _SAFE_REQUEST_ID.fullmatch(normalized) else "unknown"


def _http_error_is_retryable(status: int, body: bytes) -> bool:
    if status in _RETRYABLE_HTTP_STATUSES:
        return True
    if status != HTTPStatus.TOO_MANY_REQUESTS:
        return False
    try:
        parsed = _strict_json_loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return True
    error = parsed.get("error") if isinstance(parsed, Mapping) else None
    if not isinstance(error, Mapping):
        return True
    code = str(error.get("code") or "").strip().casefold()
    kind = str(error.get("type") or "").strip().casefold()
    return code not in _QUOTA_CODES and kind not in _QUOTA_TYPES


class UsageLedger:
    """Thread-safe exact accounting and retry-chain ledger."""

    def __init__(
        self,
        path: Path | None,
        max_cost_usd: Decimal | str,
        *,
        request_reservation_usd: Decimal | str,
        safety_reserve_usd: Decimal | str,
    ):
        self.path = path
        self.max_cost_usd = _decimal(max_cost_usd, label="max_cost_usd")
        self.request_reservation_usd = _decimal(
            request_reservation_usd, label="request_reservation_usd"
        )
        self.safety_reserve_usd = _decimal(
            safety_reserve_usd, label="safety_reserve_usd"
        )
        if self.max_cost_usd <= 0 or self.request_reservation_usd <= 0:
            raise ValueError("cost fuse and reservation must be positive")
        if self.path and (self.path.exists() or self.path.is_symlink()):
            raise ValueError("usage ledger must start absent")
        self._lock = threading.Lock()
        self._calls = 0
        self._errors = 0
        self._malformed_responses = 0
        self._terminal_rejections = 0
        self._discarded_transients = 0
        self._hidden_sdk_retry_rejections = 0
        self._cost = Decimal("0")
        self._reserved = Decimal("0")
        self._prompt_tokens = 0
        self._cached_tokens = 0
        self._completion_tokens = 0
        self._reasoning_tokens = 0
        self._unknown_cost_attempts = 0
        self._transport_failed = False
        self._fatal_reason_code: str | None = None
        self._logical_ordinals: dict[str, int] = {}
        self._pending_attempts: dict[str, list[tuple[str, int]]] = {}
        self._active_attempts: set[str] = set()
        self._active_payloads: set[str] = set()
        self._client_request_ids: set[str] = set()

    def _trip_fatal_locked(self, reason_code: str) -> None:
        if reason_code not in _FATAL_REASON_CODES:
            reason_code = "internal_invariant"
        self._transport_failed = True
        if self._fatal_reason_code is None:
            self._fatal_reason_code = reason_code

    def trip_fatal(self, reason_code: str) -> None:
        with self._lock:
            self._trip_fatal_locked(reason_code)

    def reserve_request(self) -> bool:
        with self._lock:
            if self._transport_failed:
                return False
            projected = (
                self._cost
                + self._reserved
                + self.request_reservation_usd
                + self.safety_reserve_usd
            )
            if projected > self.max_cost_usd:
                self._trip_fatal_locked("budget_fuse")
                return False
            self._reserved += self.request_reservation_usd
            return True

    def release_request(self) -> None:
        with self._lock:
            self._reserved = max(
                Decimal("0"), self._reserved - self.request_reservation_usd
            )

    def start_attempt(self, payload_sha256: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
            raise ValueError("payload hash must be lowercase SHA-256")
        with self._lock:
            if self._transport_failed:
                return None
            if payload_sha256 in self._active_payloads:
                self._trip_fatal_locked("concurrent_payload")
                return None
            pending = self._pending_attempts.get(payload_sha256, [])
            if pending:
                logical_id, attempt_number = pending.pop(0)
                if not pending:
                    self._pending_attempts.pop(payload_sha256, None)
            else:
                ordinal = self._logical_ordinals.get(payload_sha256, 0) + 1
                self._logical_ordinals[payload_sha256] = ordinal
                logical_id = hashlib.sha256(
                    f"{payload_sha256}:{ordinal}".encode("ascii")
                ).hexdigest()
                attempt_number = 1
            if attempt_number > MAX_LOGICAL_ATTEMPTS:
                self._trip_fatal_locked("attempt_limit")
                return None
            client_request_id = f"narratordb-r3-{uuid.uuid4().hex}"
            if client_request_id in self._client_request_ids:
                self._trip_fatal_locked("internal_invariant")
                return None
            self._client_request_ids.add(client_request_id)
            self._active_attempts.add(logical_id)
            self._active_payloads.add(payload_sha256)
            return {
                "request_payload_sha256": payload_sha256,
                "logical_call_id": logical_id,
                "attempt_number": attempt_number,
                "client_request_id": client_request_id,
            }

    def reject_hidden_sdk_retry(self) -> None:
        with self._lock:
            self._hidden_sdk_retry_rejections += 1
            self._trip_fatal_locked("hidden_sdk_retry")

    def _finish_attempt_locked(
        self, attempt: Mapping[str, Any], *, forwarded: bool, retryable: bool
    ) -> None:
        logical_id = str(attempt["logical_call_id"])
        payload_sha = str(attempt["request_payload_sha256"])
        attempt_number = int(attempt["attempt_number"])
        if logical_id not in self._active_attempts or payload_sha not in self._active_payloads:
            self._trip_fatal_locked("internal_invariant")
            return
        self._active_attempts.remove(logical_id)
        self._active_payloads.remove(payload_sha)
        if forwarded:
            return
        if (
            retryable
            and attempt_number < MAX_LOGICAL_ATTEMPTS
            and self._discarded_transients <= MAX_DISCARDED_TRANSIENTS
            and not self._transport_failed
        ):
            self._pending_attempts.setdefault(payload_sha, []).append(
                (logical_id, attempt_number + 1)
            )
        else:
            self._trip_fatal_locked(
                "retry_limit" if retryable else "terminal_response"
            )

    def _append_locked(self, record: Mapping[str, Any]) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(dict(record), sort_keys=True) + "\n")
            output.flush()

    def record_response(
        self,
        response: Mapping[str, Any],
        *,
        attempt: Mapping[str, Any],
        upstream_status: int,
        upstream_request_id: str,
    ) -> dict[str, Any]:
        usage_result = _usage_fields(response)
        accounting_valid = usage_result is not None
        usage, computed_cost = (
            usage_result
            if usage_result is not None
            else (
                {
                    "prompt_tokens": 0,
                    "cached_tokens": 0,
                    "completion_tokens": 0,
                    "reasoning_tokens": 0,
                },
                self.request_reservation_usd,
            )
        )
        choices = response.get("choices")
        exact_choice = (
            isinstance(choices, list)
            and len(choices) == 1
            and isinstance(choices[0], Mapping)
            and choices[0].get("index") == 0
        )
        choice = choices[0] if exact_choice else {}
        message = choice.get("message") if isinstance(choice, Mapping) else None
        message_valid = isinstance(message, Mapping) and message.get("role") == "assistant"
        safe_message = message if isinstance(message, Mapping) else {}
        visible_state = _visible_content_state(safe_message)
        finish_class = _observed_finish_class(choice)
        tool_calls = safe_message.get("tool_calls")
        has_tools = not (
            "tool_calls" not in safe_message
            or tool_calls is None
            or (isinstance(tool_calls, list) and not tool_calls)
        ) or not (
            "function_call" not in safe_message
            or safe_message.get("function_call") is None
        )
        refusal = safe_message.get("refusal")
        has_refusal = not (
            "refusal" not in safe_message
            or refusal is None
            or (isinstance(refusal, str) and not refusal)
        )
        identity_valid = (
            upstream_status == HTTPStatus.OK
            and response.get("object") == "chat.completion"
            and response.get("model") == PINNED_MODEL
            and response.get("service_tier") == "default"
            and _safe_request_id(upstream_request_id) != "unknown"
        )

        discarded_reason: str | None = None
        retryable = False
        if not identity_valid:
            discarded_reason = "response_identity_unverified"
        elif not accounting_valid:
            discarded_reason = "invalid_accounting"
        elif not exact_choice:
            discarded_reason = "choice_schema_mismatch"
        elif not message_valid:
            discarded_reason = "message_schema_mismatch"
        elif visible_state == "invalid":
            discarded_reason = "invalid_content_type"
        elif has_tools:
            discarded_reason = "unexpected_tool_call"
        elif has_refusal:
            discarded_reason = "unexpected_refusal"
        elif visible_state == "nonempty" and finish_class == "stop":
            discarded_reason = None
        elif visible_state in {"missing", "null", "blank"} and finish_class == "stop":
            discarded_reason = "empty_completion"
            retryable = True
        elif visible_state in {"missing", "null", "blank"} and finish_class == "error":
            discarded_reason = "contentless_provider_error"
            retryable = True
        elif (
            visible_state in {"missing", "null", "blank"}
            and finish_class == "length"
            and usage["completion_tokens"] > 0
            and usage["completion_tokens"] == usage["reasoning_tokens"]
        ):
            discarded_reason = "contentless_reasoning_exhausted"
            retryable = True
        elif finish_class in {"missing", "null", "invalid_type", "unknown_string"}:
            discarded_reason = "invalid_finish_reason"
        elif finish_class == "content_filter":
            discarded_reason = "content_filtered"
        elif finish_class in {"tool_calls", "function_call"}:
            discarded_reason = "unexpected_tool_call"
        else:
            discarded_reason = "non_stop_or_partial_completion"

        forwarded = discarded_reason is None
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": (
                "completion"
                if forwarded
                else "discarded_transient"
                if retryable
                else "terminal_rejection"
            ),
            "status": int(upstream_status),
            "endpoint_identity": ENDPOINT_IDENTITY,
            "provider": PROVIDER_IDENTITY,
            "request_model": PINNED_MODEL,
            "response_model": (
                PINNED_MODEL if response.get("model") == PINNED_MODEL else "mismatch"
            ),
            "service_tier": (
                "default" if response.get("service_tier") == "default" else "mismatch"
            ),
            "observed_finish_class": finish_class,
            "visible_content_state": visible_state,
            "response_complete": forwarded,
            "response_forwarded": forwarded,
            "discarded_reason": discarded_reason,
            "retryable": retryable,
            **usage,
            "cost_usd": _money(computed_cost),
            "unknown_cost": not accounting_valid,
            "request_payload_sha256": str(attempt["request_payload_sha256"]),
            "logical_call_id": str(attempt["logical_call_id"]),
            "attempt_number": int(attempt["attempt_number"]),
            "client_request_id": str(attempt["client_request_id"]),
            "upstream_request_id": _safe_request_id(upstream_request_id),
        }
        with self._lock:
            if retryable and self._discarded_transients >= MAX_DISCARDED_TRANSIENTS:
                retryable = False
                discarded_reason = "retry_limit_exceeded"
                record["event"] = "terminal_rejection"
                record["discarded_reason"] = discarded_reason
                record["retryable"] = False
                self._trip_fatal_locked("retry_limit")
            projected_actual = (
                self._cost
                + max(
                    Decimal("0"), self._reserved - self.request_reservation_usd
                )
                + computed_cost
                + self.safety_reserve_usd
            )
            if projected_actual > self.max_cost_usd:
                forwarded = False
                retryable = False
                discarded_reason = "budget_cost_overrun"
                record["event"] = "terminal_rejection"
                record["response_complete"] = False
                record["response_forwarded"] = False
                record["discarded_reason"] = discarded_reason
                record["retryable"] = False
                self._trip_fatal_locked("budget_fuse")
            self._calls += int(forwarded)
            self._malformed_responses += int(not forwarded)
            self._discarded_transients += int(retryable)
            self._terminal_rejections += int(not forwarded and not retryable)
            if self._discarded_transients > MAX_DISCARDED_TRANSIENTS:
                self._trip_fatal_locked("retry_limit")
            if not forwarded and not retryable:
                self._trip_fatal_locked("terminal_response")
            self._cost += computed_cost
            self._prompt_tokens += usage["prompt_tokens"]
            self._cached_tokens += usage["cached_tokens"]
            self._completion_tokens += usage["completion_tokens"]
            self._reasoning_tokens += usage["reasoning_tokens"]
            self._unknown_cost_attempts += int(not accounting_valid)
            self._append_locked(record)
            self._finish_attempt_locked(attempt, forwarded=forwarded, retryable=retryable)
        return record

    def record_transport_error(
        self,
        status: int,
        *,
        attempt: Mapping[str, Any],
        discarded_reason: str,
        retryable: bool,
        upstream_request_id: str = "unknown",
    ) -> dict[str, Any]:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "discarded_transient" if retryable else "terminal_rejection",
            "status": int(status),
            "endpoint_identity": ENDPOINT_IDENTITY,
            "provider": PROVIDER_IDENTITY,
            "request_model": PINNED_MODEL,
            "response_model": "unknown",
            "service_tier": "unknown",
            "observed_finish_class": "unknown",
            "visible_content_state": "unavailable",
            "response_complete": False,
            "response_forwarded": False,
            "discarded_reason": discarded_reason,
            "retryable": retryable,
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": _money(self.request_reservation_usd),
            "unknown_cost": True,
            "request_payload_sha256": str(attempt["request_payload_sha256"]),
            "logical_call_id": str(attempt["logical_call_id"]),
            "attempt_number": int(attempt["attempt_number"]),
            "client_request_id": str(attempt["client_request_id"]),
            "upstream_request_id": _safe_request_id(upstream_request_id),
        }
        with self._lock:
            if retryable and self._discarded_transients >= MAX_DISCARDED_TRANSIENTS:
                retryable = False
                record["event"] = "terminal_rejection"
                record["discarded_reason"] = "retry_limit_exceeded"
                record["retryable"] = False
                self._trip_fatal_locked("retry_limit")
            self._errors += 1
            self._discarded_transients += int(retryable)
            self._terminal_rejections += int(not retryable)
            self._cost += self.request_reservation_usd
            self._unknown_cost_attempts += 1
            if self._discarded_transients > MAX_DISCARDED_TRANSIENTS:
                self._trip_fatal_locked("retry_limit")
            if not retryable:
                self._trip_fatal_locked("terminal_http_error")
            self._append_locked(record)
            self._finish_attempt_locked(attempt, forwarded=False, retryable=retryable)
        return record

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": self._calls,
                "errors": self._errors,
                "malformed_responses": self._malformed_responses,
                "terminal_rejections": self._terminal_rejections,
                "discarded_transients": self._discarded_transients,
                "hidden_sdk_retry_rejections": self._hidden_sdk_retry_rejections,
                "cost_usd": _money(self._cost),
                "prompt_tokens": self._prompt_tokens,
                "cached_tokens": self._cached_tokens,
                "completion_tokens": self._completion_tokens,
                "reasoning_tokens": self._reasoning_tokens,
                "unknown_cost_attempts": self._unknown_cost_attempts,
                "max_cost_usd": _money(self.max_cost_usd),
                "request_reservation_usd": _money(self.request_reservation_usd),
                "safety_reserve_usd": _money(self.safety_reserve_usd),
                "reserved_cost_usd": _money(self._reserved),
                "max_discarded_transients": MAX_DISCARDED_TRANSIENTS,
                "max_logical_attempts": MAX_LOGICAL_ATTEMPTS,
                "transport_failed": self._transport_failed,
                "fatal_reason_code": self._fatal_reason_code,
                "pending_logical_calls": sum(
                    len(items) for items in self._pending_attempts.values()
                ),
                "active_logical_calls": len(self._active_attempts),
                "scope": "process",
                "enforcement": "hard_fuse",
            }


def make_handler(
    *,
    api_key: str,
    ledger: UsageLedger,
    upstream: str = OFFICIAL_UPSTREAM,
    allow_insecure_test_upstream: bool = False,
    max_completion_tokens: int = 4096,
    timeout: float = PINNED_TIMEOUT_SECONDS,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> type[BaseHTTPRequestHandler]:
    upstream = _validate_upstream(
        upstream, allow_insecure_test_upstream=allow_insecure_test_upstream
    )
    if timeout != PINNED_TIMEOUT_SECONDS:
        raise ValueError("upstream timeout must be exactly 105 seconds")
    if max_completion_tokens not in {128, 4096}:
        raise ValueError("phase token limit must be exactly 128 or 4096")
    if max_request_bytes <= 0 or max_response_bytes <= 0:
        raise ValueError("request and response size limits must be positive")

    class Handler(BaseHTTPRequestHandler):
        server_version = "NarratorDBOfficialOpenAITransport/3.0"

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def send_body(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            fatal: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if status >= 400:
                self.send_header("x-should-retry", "false")
            if fatal:
                self.send_header("x-narratordb-transport-fatal", "true")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def send_error_json(
            self, status: int, message: str, *, fatal: bool = False
        ) -> None:
            self.send_body(
                status,
                json.dumps({"error": message}).encode("utf-8"),
                "application/json",
                fatal=fatal,
            )

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") not in {"/health", "/v1/health"}:
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return
            self.send_body(
                HTTPStatus.OK,
                json.dumps(
                    {
                        "ok": True,
                        "upstream": OFFICIAL_UPSTREAM,
                        "endpoint_identity": ENDPOINT_IDENTITY,
                        "provider_identity": PROVIDER_IDENTITY,
                        "model": PINNED_MODEL,
                        "max_completion_tokens": max_completion_tokens,
                        "reasoning_effort": "high",
                        "service_tier": "default",
                        "store": False,
                        "n": 1,
                        "upstream_timeout_seconds": timeout,
                        "max_request_bytes": max_request_bytes,
                        "max_response_bytes": max_response_bytes,
                        "direct_upstream_networking": True,
                        "environment_proxy_inheritance": False,
                        "inbound_retry_count_policy": "absent-or-zero-only",
                        "local_caller_auth_required": True,
                        "prompt_or_completion_content_retained": False,
                        "usage": ledger.summary(),
                    },
                    sort_keys=True,
                ).encode("utf-8"),
                "application/json",
            )

        def do_POST(self) -> None:  # noqa: N802
            reserved = False
            attempt: Mapping[str, Any] | None = None
            if self.headers.get("Authorization") != "Bearer local-transport":
                ledger.trip_fatal("invalid_local_request")
                self.send_error_json(
                    HTTPStatus.UNAUTHORIZED, "local auth rejected", fatal=True
                )
                return
            if self.headers.get("x-stainless-retry-count") not in {None, "0"}:
                ledger.reject_hidden_sdk_retry()
                self.send_error_json(
                    HTTPStatus.BAD_REQUEST, "hidden SDK retry rejected", fatal=True
                )
                return
            if self.path.split("?", 1)[0].rstrip("/") not in {
                "/chat/completions",
                "/v1/chat/completions",
            }:
                ledger.trip_fatal("invalid_local_request")
                self.send_error_json(HTTPStatus.NOT_FOUND, "not found", fatal=True)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > max_request_bytes:
                    raise ValueError("invalid request length")
                payload = _strict_json_loads(self.rfile.read(length))
                prepared = prepare_official_payload(
                    payload, max_completion_tokens=max_completion_tokens
                )
                payload_sha = canonical_payload_sha256(prepared)
                if not ledger.reserve_request():
                    self.send_error_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "transport fuse is closed",
                        fatal=True,
                    )
                    return
                reserved = True
                attempt = ledger.start_attempt(payload_sha)
                if attempt is None:
                    self.send_error_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "transport fuse is closed",
                        fatal=True,
                    )
                    return
                request = Request(
                    upstream,
                    data=json.dumps(prepared, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "X-Client-Request-Id": str(attempt["client_request_id"]),
                    },
                    method="POST",
                )
                with build_opener(ProxyHandler({}), _NoRedirectHandler()).open(
                    request, timeout=timeout
                ) as response:
                    body = response.read(max_response_bytes + 1)
                    status = int(response.status)
                    upstream_request_id = response.headers.get("x-request-id", "")
                if len(body) > max_response_bytes:
                    ledger.record_transport_error(
                        HTTPStatus.BAD_GATEWAY,
                        attempt=attempt,
                        discarded_reason="oversized_response",
                        retryable=False,
                    )
                    self.send_error_json(
                        HTTPStatus.BAD_GATEWAY, "official response rejected", fatal=True
                    )
                    return
                try:
                    decoded = _strict_json_loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    ledger.record_transport_error(
                        HTTPStatus.BAD_GATEWAY,
                        attempt=attempt,
                        discarded_reason="invalid_json_response",
                        retryable=False,
                    )
                    self.send_error_json(
                        HTTPStatus.BAD_GATEWAY, "official response rejected", fatal=True
                    )
                    return
                if not isinstance(decoded, Mapping):
                    ledger.record_transport_error(
                        HTTPStatus.BAD_GATEWAY,
                        attempt=attempt,
                        discarded_reason="invalid_response_schema",
                        retryable=False,
                    )
                    self.send_error_json(
                        HTTPStatus.BAD_GATEWAY, "official response rejected", fatal=True
                    )
                    return
                event = ledger.record_response(
                    decoded,
                    attempt=attempt,
                    upstream_status=status,
                    upstream_request_id=upstream_request_id,
                )
                if not event["response_forwarded"]:
                    fatal = not bool(event["retryable"])
                    self.send_error_json(
                        HTTPStatus.BAD_GATEWAY,
                        "official response rejected",
                        fatal=fatal,
                    )
                    return
                self.send_body(status, body, "application/json")
            except (json.JSONDecodeError, TypeError, ValueError):
                ledger.trip_fatal("invalid_local_request")
                self.send_error_json(
                    HTTPStatus.BAD_REQUEST, "invalid local request", fatal=True
                )
            except HTTPError as error:
                if attempt is None:
                    ledger.trip_fatal("terminal_http_error")
                    self.send_error_json(
                        HTTPStatus.BAD_GATEWAY, "official request failed", fatal=True
                    )
                    return
                try:
                    body = error.read(max_response_bytes + 1)
                except (HTTPException, OSError):
                    body = b""
                retryable = _http_error_is_retryable(error.code, body)
                event = ledger.record_transport_error(
                    error.code,
                    attempt=attempt,
                    discarded_reason=(
                        "upstream_http_error" if retryable else "terminal_http_error"
                    ),
                    retryable=retryable,
                    upstream_request_id=error.headers.get("x-request-id", ""),
                )
                self.send_error_json(
                    error.code,
                    "official request failed",
                    fatal=not bool(event["retryable"]),
                )
            except (HTTPException, TimeoutError, URLError, OSError):
                if attempt is None:
                    ledger.trip_fatal("terminal_http_error")
                    self.send_error_json(
                        HTTPStatus.GATEWAY_TIMEOUT,
                        "official request failed",
                        fatal=True,
                    )
                    return
                event = ledger.record_transport_error(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    attempt=attempt,
                    discarded_reason="upstream_timeout_or_network",
                    retryable=True,
                )
                self.send_error_json(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    "official request failed",
                    fatal=not bool(event["retryable"]),
                )
            finally:
                if reserved:
                    ledger.release_request()

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8893)
    parser.add_argument("--usage-log", type=Path, required=True)
    parser.add_argument("--max-cost-usd", default="2.45")
    parser.add_argument("--request-reservation-usd", default="0.318432")
    parser.add_argument("--budget-safety-reserve-usd", default="0.01")
    parser.add_argument("--max-completion-tokens", type=int, choices=(128, 4096), default=4096)
    parser.add_argument("--timeout", type=float, default=PINNED_TIMEOUT_SECONDS)
    parser.add_argument("--max-request-bytes", type=int, default=DEFAULT_MAX_REQUEST_BYTES)
    parser.add_argument("--max-response-bytes", type=int, default=DEFAULT_MAX_RESPONSE_BYTES)
    parser.add_argument("--test-upstream")
    parser.add_argument("--allow-insecure-test-upstream", action="store_true")
    args = parser.parse_args()
    if bool(args.test_upstream) != bool(args.allow_insecure_test_upstream):
        parser.error("test upstream and insecure-test flag must be supplied together")
    verify_pricing_evidence()
    api_key = os.environ.pop("OPENAI_API_KEY", "")
    if (
        not api_key
        or any(ord(character) < 33 or ord(character) > 126 for character in api_key)
        or "\n" in api_key
        or "\r" in api_key
    ):
        parser.error("OPENAI_API_KEY must be one non-empty printable value")
    ledger = UsageLedger(
        args.usage_log,
        args.max_cost_usd,
        request_reservation_usd=args.request_reservation_usd,
        safety_reserve_usd=args.budget_safety_reserve_usd,
    )
    upstream = args.test_upstream or OFFICIAL_UPSTREAM
    handler = make_handler(
        api_key=api_key,
        ledger=ledger,
        upstream=upstream,
        allow_insecure_test_upstream=args.allow_insecure_test_upstream,
        max_completion_tokens=args.max_completion_tokens,
        timeout=args.timeout,
        max_request_bytes=args.max_request_bytes,
        max_response_bytes=args.max_response_bytes,
    )
    api_key = ""
    server = _ThreadingHTTPServer((args.host, args.port), handler)

    def stop_server(signum: int, frame: Any) -> None:
        del signum, frame
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop_server)
    signal.signal(signal.SIGTERM, stop_server)
    print(
        json.dumps(
            {
                "ok": True,
                "upstream": OFFICIAL_UPSTREAM,
                "endpoint_identity": ENDPOINT_IDENTITY,
                "provider_identity": PROVIDER_IDENTITY,
                "model": PINNED_MODEL,
                "max_completion_tokens": args.max_completion_tokens,
                "reasoning_effort": "high",
                "service_tier": "default",
                "store": False,
                "n": 1,
                "usage_log": str(args.usage_log.resolve()),
                "max_cost_usd": _money(ledger.max_cost_usd),
                "request_reservation_usd": _money(ledger.request_reservation_usd),
                "safety_reserve_usd": _money(ledger.safety_reserve_usd),
                "upstream_timeout_seconds": args.timeout,
                "direct_upstream_networking": True,
                "environment_proxy_inheritance": False,
                "prompt_or_completion_content_retained": False,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        print(json.dumps({"stopped": True, "usage": ledger.summary()}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
