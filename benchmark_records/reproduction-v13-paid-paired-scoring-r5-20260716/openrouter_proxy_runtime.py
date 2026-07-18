#!/usr/bin/env python3
"""Cost-capped OpenRouter transport for reproducible benchmark runs.

The official benchmark harness remains responsible for prompts, sampling,
answer generation, judging, and aggregation.  This transport only pins
OpenRouter routing/model options and records response usage without retaining
prompt or completion content.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..config import (
    normalize_openrouter_provider_allowlist,
    normalize_openrouter_provider_route,
    openrouter_provider_family_identity,
)


DEFAULT_UPSTREAM = "https://openrouter.ai/api/v1/chat/completions"
_ALLOWED_UPSTREAMS = {
    "https://openrouter.ai/api/v1/chat/completions",
    "https://api.openrouter.ai/api/v1/chat/completions",
}
_MODEL_ID = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
)
_PROVIDER_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._/-]{0,31}"
    r"(?: [A-Za-z0-9][A-Za-z0-9._/-]{0,31}){0,4}"
)
_FINISH_REASONS = {
    "stop",
    "length",
    "content_filter",
    "tool_calls",
    "function_call",
    "error",
}
_ERROR_CODES = {"rate_limited", "overloaded", "timeout", "unavailable"}
_ERROR_TYPES = {
    "rate_limit",
    "rate_limit_error",
    "rate_limit_exceeded",
    "provider_error",
    "upstream_error",
    "timeout",
    "overloaded",
    "server_error",
    "invalid_request",
}
_MAX_INTEGER = (1 << 63) - 1
_MAX_COST_USD = 1_000_000_000.0


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


def _provider_routes(
    provider_only: str | None,
    provider_allow: tuple[str, ...],
) -> tuple[str, ...]:
    if provider_only and provider_allow:
        raise ValueError("provider_only and provider_allow are mutually exclusive")
    if provider_only:
        return (normalize_openrouter_provider_route(provider_only),)
    routes = normalize_openrouter_provider_allowlist(provider_allow)
    if not routes:
        raise ValueError("an explicit OpenRouter provider route is required")
    return routes


def _validate_upstream(upstream: str, *, allow_insecure_test_upstream: bool) -> str:
    if allow_insecure_test_upstream:
        parsed = urlsplit(upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("test upstream must be an absolute HTTP(S) URL")
        return upstream
    if upstream not in _ALLOWED_UPSTREAMS:
        raise ValueError("upstream must be the exact OpenRouter chat-completions URL")
    return upstream


def _safe_model(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip()
    return (
        normalized
        if len(normalized) <= 192 and _MODEL_ID.fullmatch(normalized)
        else "unknown"
    )


def _canonical_response_model(value: Any, request_model: str) -> str:
    response_model = _safe_model(value)
    if response_model == request_model:
        return response_model
    if re.fullmatch(re.escape(response_model) + r"-[0-9]{8}", request_model):
        return response_model
    return "route_mismatch"


def _canonical_provider(value: Any, configured_routes: tuple[str, ...]) -> str:
    if value is None:
        return "unknown"
    if not isinstance(value, str):
        return "route_mismatch"
    response_provider = value.strip()
    if not response_provider:
        return "unknown"
    if "/" in response_provider:
        for configured in configured_routes:
            if configured.casefold() == response_provider.casefold():
                return configured
        return "route_mismatch"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}", response_provider):
        return "route_mismatch"
    identity = "".join(
        character for character in response_provider.casefold() if character.isalnum()
    )
    matches = [
        route
        for route in configured_routes
        if openrouter_provider_family_identity(route) == identity
    ]
    return matches[0] if len(matches) == 1 else "route_mismatch"


def _safe_finish_reason(value: Any) -> str:
    return value if isinstance(value, str) and value in _FINISH_REASONS else "unknown"


def _safe_error_code(value: Any) -> str:
    if isinstance(value, bool):
        return "unknown"
    normalized = str(value).strip().casefold()
    if normalized in _ERROR_CODES or (normalized.isdigit() and len(normalized) <= 6):
        return normalized
    return "unknown"


def _safe_error_type(value: Any) -> str:
    normalized = str(value).strip().casefold() if value is not None else ""
    return normalized if normalized in _ERROR_TYPES else "unknown"


def prepare_openrouter_payload(
    payload: dict[str, Any],
    *,
    provider_only: str | None = None,
    provider_allow: tuple[str, ...] = (),
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Return a copy with reproducibility controls added to the request."""
    prepared = dict(payload)
    routes = _provider_routes(provider_only, provider_allow)
    prepared["provider"] = {
        "only": list(routes),
        "order": list(routes),
        "allow_fallbacks": len(routes) > 1,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
    }
    if reasoning_effort:
        existing_reasoning = prepared.get("reasoning")
        reasoning = (
            dict(existing_reasoning) if isinstance(existing_reasoning, dict) else {}
        )
        reasoning["effort"] = reasoning_effort
        prepared["reasoning"] = reasoning
    return prepared


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if 0 <= parsed <= _MAX_INTEGER else 0


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) and 0 <= parsed <= _MAX_COST_USD else 0.0


def _optional_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and 0 <= parsed <= _MAX_COST_USD else None


def _read_http_error_body(
    error: HTTPError, *, max_response_bytes: int
) -> tuple[bytes, bool]:
    """Return a bounded error body and whether the HTTP exchange itself failed."""

    try:
        body = error.read(max_response_bytes + 1)
    except (HTTPException, OSError):
        return b"", True
    return (b"" if len(body) > max_response_bytes else body), False


class UsageLedger:
    """Thread-safe, content-free usage log and soft budget guard."""

    def __init__(
        self,
        path: Path | None,
        max_cost_usd: float | None,
        *,
        request_reservation_usd: float | None = None,
        safety_reserve_usd: float = 0.0,
    ):
        if max_cost_usd is not None and (
            not math.isfinite(max_cost_usd) or max_cost_usd <= 0
        ):
            raise ValueError("max_cost_usd must be a positive finite number")
        effective_reservation = (
            min(0.01, max_cost_usd)
            if max_cost_usd is not None and request_reservation_usd in {None, 0.0}
            else (0.0 if request_reservation_usd is None else request_reservation_usd)
        )
        for label, value in (
            ("request_reservation_usd", effective_reservation),
            ("safety_reserve_usd", safety_reserve_usd),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{label} must be a non-negative finite number")
        self.path = path
        self.max_cost_usd = max_cost_usd
        self.request_reservation_usd = float(effective_reservation)
        self.safety_reserve_usd = float(safety_reserve_usd)
        self._lock = threading.Lock()
        self._calls = 0
        self._errors = 0
        self._malformed_responses = 0
        self._cost = 0.0
        self._prompt_tokens = 0
        self._cached_tokens = 0
        self._completion_tokens = 0
        self._reasoning_tokens = 0
        self._unknown_cost_attempts = 0
        self._reserved_cost_usd = 0.0
        if self.path and self.path.exists():
            self._load_existing()

    def _load_existing(self) -> None:
        """Restore counters so a resumed proxy cannot reset the cost guard."""
        assert self.path is not None
        payload = self.path.read_bytes()
        if payload and not payload.endswith(b"\n"):
            raise ValueError(
                f"existing usage ledger has an incomplete final line: {self.path}"
            )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(
                f"existing usage ledger is not UTF-8: {self.path}"
            ) from error
        for line_number, line in enumerate(
            text.splitlines(),
            1,
        ):
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > 64 * 1024:
                raise ValueError(
                    f"existing usage event exceeds metadata limit on line {line_number}"
                )
            try:
                record = json.loads(
                    line,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"non-finite JSON number is not allowed: {value}")
                    ),
                )
            except (json.JSONDecodeError, ValueError) as error:
                raise ValueError(
                    f"invalid existing usage event on line {line_number}: {self.path}"
                ) from error
            if not isinstance(record, dict):
                raise ValueError(
                    f"existing usage event on line {line_number} is not an object"
                )
            event = record.get("event")
            if event not in {"completion", "upstream_error"}:
                raise ValueError(
                    f"unsupported existing usage event on line {line_number}"
                )
            required = (
                {
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
                if event == "completion"
                else {
                    "timestamp",
                    "event",
                    "status",
                    "request_model",
                    "provider",
                    "error_code",
                    "cost_usd",
                    "prompt_tokens",
                    "completion_tokens",
                    "reasoning_tokens",
                }
            )
            allowed = (
                required
                | {"unknown_cost"}
                | (
                    {"cached_tokens", "error_type"}
                    if event == "upstream_error"
                    else set()
                )
            )
            if set(record) - allowed or required - set(record):
                raise ValueError(f"invalid existing usage fields on line {line_number}")
            try:
                timestamp = datetime.fromisoformat(
                    str(record["timestamp"]).replace("Z", "+00:00")
                )
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"invalid existing timestamp on line {line_number}"
                ) from error
            if timestamp.tzinfo is None:
                raise ValueError(f"invalid existing timestamp on line {line_number}")
            request_model = record["request_model"]
            if (
                request_model != "unknown"
                and _safe_model(request_model) != request_model
            ):
                raise ValueError(
                    f"invalid existing model identity on line {line_number}"
                )
            provider = record["provider"]
            if not isinstance(provider, str) or (
                provider not in {"unknown", "route_mismatch"}
                and (
                    len(provider) > 80
                    or not _PROVIDER_NAME.fullmatch(provider)
                    or provider.casefold().startswith(("sk-", "sk_", "bearer"))
                )
            ):
                raise ValueError(
                    f"invalid existing provider identity on line {line_number}"
                )
            for key in (
                "status",
                "prompt_tokens",
                "cached_tokens",
                "completion_tokens",
                "reasoning_tokens",
            ):
                if key in record and (
                    isinstance(record[key], bool)
                    or not isinstance(record[key], int)
                    or not 0 <= record[key] <= _MAX_INTEGER
                ):
                    raise ValueError(
                        f"invalid existing numeric metadata on line {line_number}"
                    )
            if not 0 <= record["status"] <= 999:
                raise ValueError(f"invalid existing status on line {line_number}")
            cost = record.get("cost_usd")
            if isinstance(cost, bool) or not isinstance(cost, (int, float)):
                raise ValueError(
                    f"invalid existing cost metadata on line {line_number}"
                )
            parsed_cost = float(cost)
            if not math.isfinite(parsed_cost) or not 0 <= parsed_cost <= _MAX_COST_USD:
                raise ValueError(
                    f"invalid existing cost metadata on line {line_number}"
                )
            unknown_cost = record.get("unknown_cost", False)
            if not isinstance(unknown_cost, bool):
                raise ValueError(
                    f"invalid existing unknown-cost marker on line {line_number}"
                )
            self._unknown_cost_attempts += int(unknown_cost)
            if event == "completion":
                response_model = record["response_model"]
                if response_model not in {"unknown", "route_mismatch"} and (
                    _safe_model(response_model) != response_model
                ):
                    raise ValueError(
                        f"invalid existing response model on line {line_number}"
                    )
                if record["finish_reason"] not in _FINISH_REASONS | {"unknown"}:
                    raise ValueError(
                        f"invalid existing finish reason on line {line_number}"
                    )
                if not isinstance(record["response_complete"], bool):
                    raise ValueError(
                        f"invalid existing completion flag on line {line_number}"
                    )
                self._calls += 1
                if record.get("response_complete") is False or (
                    "response_complete" not in record
                    and not record.get("provider")
                    and not record.get("response_model")
                    and not record.get("finish_reason")
                ):
                    self._malformed_responses += 1
                self._cost += parsed_cost
                self._prompt_tokens += _integer(record.get("prompt_tokens"))
                self._cached_tokens += _integer(record.get("cached_tokens"))
                self._completion_tokens += _integer(record.get("completion_tokens"))
                self._reasoning_tokens += _integer(record.get("reasoning_tokens"))
            else:
                error_code = record["error_code"]
                if not isinstance(error_code, str) or not (
                    error_code in _ERROR_CODES | {"unknown"}
                    or (error_code.isdigit() and len(error_code) <= 6)
                ):
                    raise ValueError(
                        f"invalid existing error code on line {line_number}"
                    )
                if "error_type" in record and record[
                    "error_type"
                ] not in _ERROR_TYPES | {"unknown"}:
                    raise ValueError(
                        f"invalid existing error type on line {line_number}"
                    )
                self._errors += 1
                self._cost += parsed_cost
                self._prompt_tokens += _integer(record.get("prompt_tokens"))
                self._cached_tokens += _integer(record.get("cached_tokens"))
                self._completion_tokens += _integer(record.get("completion_tokens"))
                self._reasoning_tokens += _integer(record.get("reasoning_tokens"))

    def can_start_request(self) -> bool:
        with self._lock:
            return self._can_reserve_locked(self.request_reservation_usd)

    def _can_reserve_locked(self, amount: float) -> bool:
        if self.max_cost_usd is None:
            return True
        projected = (
            self._cost + self._reserved_cost_usd + amount + self.safety_reserve_usd
        )
        return (
            (
                projected <= self.max_cost_usd
                or math.isclose(
                    projected,
                    self.max_cost_usd,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
            )
            if amount > 0
            else projected < self.max_cost_usd
        )

    def reserve_request(self) -> bool:
        with self._lock:
            if not self._can_reserve_locked(self.request_reservation_usd):
                return False
            self._reserved_cost_usd += self.request_reservation_usd
            return True

    def release_request(self) -> None:
        with self._lock:
            self._reserved_cost_usd = max(
                0.0, self._reserved_cost_usd - self.request_reservation_usd
            )

    def record(
        self,
        response: dict[str, Any],
        request_model: str,
        *,
        provider_routes: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        safe_request_model = _safe_model(request_model)
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        completion_details = usage.get("completion_tokens_details")
        if not isinstance(completion_details, dict):
            completion_details = {}
        prompt_details = usage.get("prompt_tokens_details")
        if not isinstance(prompt_details, dict):
            prompt_details = {}
        choices = response.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        if not isinstance(first_choice, dict):
            first_choice = {}
        message = first_choice.get("message")
        if not isinstance(message, dict):
            message = {}
        content = message.get("content")
        response_complete = isinstance(content, str) and bool(content.strip())
        provider_cost = _optional_number(usage.get("cost"))
        unknown_cost = provider_cost is None

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "completion",
            "status": HTTPStatus.OK,
            "request_model": safe_request_model,
            "response_model": _canonical_response_model(
                response.get("model"), safe_request_model
            ),
            "provider": _canonical_provider(response.get("provider"), provider_routes),
            "finish_reason": _safe_finish_reason(first_choice.get("finish_reason")),
            "response_complete": response_complete,
            "prompt_tokens": _integer(usage.get("prompt_tokens")),
            "cached_tokens": _integer(prompt_details.get("cached_tokens")),
            "completion_tokens": _integer(usage.get("completion_tokens")),
            "reasoning_tokens": _integer(completion_details.get("reasoning_tokens")),
            "cost_usd": (
                self.request_reservation_usd if unknown_cost else provider_cost
            ),
            "unknown_cost": unknown_cost,
        }
        with self._lock:
            self._calls += 1
            self._malformed_responses += int(not response_complete)
            self._cost += record["cost_usd"]
            self._prompt_tokens += record["prompt_tokens"]
            self._cached_tokens += record["cached_tokens"]
            self._completion_tokens += record["completion_tokens"]
            self._reasoning_tokens += record["reasoning_tokens"]
            self._unknown_cost_attempts += int(unknown_cost)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def record_error(
        self,
        status: int,
        request_model: str,
        body: bytes,
        *,
        provider_routes: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        try:
            response = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = {}
        error = response.get("error") if isinstance(response, dict) else {}
        if not isinstance(error, dict):
            error = {}
        metadata = error.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        usage = response.get("usage") if isinstance(response, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        if not usage and isinstance(error.get("usage"), dict):
            usage = error["usage"]
        if not usage and isinstance(metadata.get("usage"), dict):
            usage = metadata["usage"]
        prompt_details = usage.get("prompt_tokens_details")
        if not isinstance(prompt_details, dict):
            prompt_details = {}
        completion_details = usage.get("completion_tokens_details")
        if not isinstance(completion_details, dict):
            completion_details = {}
        provider_cost = _optional_number(usage.get("cost"))
        unknown_cost = provider_cost is None
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "upstream_error",
            "status": _integer(status),
            "request_model": _safe_model(request_model),
            "provider": _canonical_provider(
                metadata.get("provider_name"), provider_routes
            ),
            "error_code": _safe_error_code(error.get("code")),
            "error_type": _safe_error_type(error.get("type")),
            "cost_usd": (
                self.request_reservation_usd if unknown_cost else provider_cost
            ),
            "unknown_cost": unknown_cost,
            "prompt_tokens": _integer(usage.get("prompt_tokens")),
            "cached_tokens": _integer(prompt_details.get("cached_tokens")),
            "completion_tokens": _integer(usage.get("completion_tokens")),
            "reasoning_tokens": _integer(completion_details.get("reasoning_tokens")),
        }
        with self._lock:
            self._errors += 1
            self._cost += record["cost_usd"]
            self._prompt_tokens += record["prompt_tokens"]
            self._cached_tokens += record["cached_tokens"]
            self._completion_tokens += record["completion_tokens"]
            self._reasoning_tokens += record["reasoning_tokens"]
            self._unknown_cost_attempts += int(unknown_cost)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": self._calls,
                "errors": self._errors,
                "malformed_responses": self._malformed_responses,
                "cost_usd": round(self._cost, 12),
                "prompt_tokens": self._prompt_tokens,
                "cached_tokens": self._cached_tokens,
                "completion_tokens": self._completion_tokens,
                "reasoning_tokens": self._reasoning_tokens,
                "unknown_cost_attempts": self._unknown_cost_attempts,
                "max_cost_usd": self.max_cost_usd,
                "request_reservation_usd": self.request_reservation_usd,
                "safety_reserve_usd": self.safety_reserve_usd,
                "reserved_cost_usd": round(self._reserved_cost_usd, 12),
                "scope": "process",
                "enforcement": "soft_fuse",
            }


def make_handler(
    *,
    api_key: str,
    upstream: str,
    provider_only: str | None,
    provider_allow: tuple[str, ...] = (),
    reasoning_effort: str | None,
    ledger: UsageLedger,
    timeout: float,
    max_request_bytes: int,
    max_response_bytes: int = 4 * 1024 * 1024,
    allow_insecure_test_upstream: bool = False,
):
    configured_routes = _provider_routes(provider_only, provider_allow)
    upstream = _validate_upstream(
        upstream, allow_insecure_test_upstream=allow_insecure_test_upstream
    )
    if timeout <= 0 or max_request_bytes <= 0 or max_response_bytes <= 0:
        raise ValueError("timeout and request/response size limits must be positive")

    class Handler(BaseHTTPRequestHandler):
        server_version = "NarratorDBOpenRouterTransport/1.0"

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            self.send_body(
                status, json.dumps(payload).encode("utf-8"), "application/json"
            )

        def send_body(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The official client owns its timeout and retry policy. A slow
                # upstream response can finish after that client has moved on;
                # its usage is still recorded, but the transport should not
                # emit an unhandled server traceback for the closed socket.
                return

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") in {"/health", "/v1/health"}:
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "provider_only": provider_only,
                        "provider_allow": list(provider_allow),
                        "reasoning_effort": reasoning_effort,
                        "usage": ledger.summary(),
                    },
                )
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            request_model = "unknown"
            reserved = False
            if self.path.split("?", 1)[0].rstrip("/") not in {
                "/chat/completions",
                "/v1/chat/completions",
            }:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > max_request_bytes:
                    raise ValueError("invalid or oversized request body")
                payload = json.loads(
                    self.rfile.read(length),
                    parse_constant=lambda _value: (_ for _ in ()).throw(
                        ValueError("non-finite JSON numbers are not allowed")
                    ),
                )
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
                if payload.get("stream"):
                    raise ValueError(
                        "streaming is not supported by the benchmark transport"
                    )
                request_model = _safe_model(payload.get("model"))
                if request_model == "unknown":
                    raise ValueError(
                        "request model must be a sanitized model identifier"
                    )
                prepared = prepare_openrouter_payload(
                    payload,
                    provider_only=provider_only,
                    provider_allow=provider_allow,
                    reasoning_effort=reasoning_effort,
                )
                if not ledger.reserve_request():
                    self.send_json(
                        HTTPStatus.PAYMENT_REQUIRED,
                        {
                            "error": "NarratorDB benchmark cost cap reached",
                            "usage": ledger.summary(),
                        },
                    )
                    return
                reserved = True
                request = Request(
                    upstream,
                    data=json.dumps(prepared).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://github.com/NarratorDB/NarratorDB",
                        "X-Title": "NarratorDB benchmark",
                    },
                    method="POST",
                )
                with build_opener(_NoRedirectHandler()).open(
                    request, timeout=timeout
                ) as response:
                    body = response.read(max_response_bytes + 1)
                    status = response.status
                    content_type = response.headers.get(
                        "Content-Type", "application/json"
                    )
                if len(body) > max_response_bytes:
                    ledger.record_error(
                        HTTPStatus.BAD_GATEWAY,
                        request_model,
                        b"",
                        provider_routes=configured_routes,
                    )
                    self.send_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": "OpenRouter response exceeded the size limit"},
                    )
                    return
                try:
                    decoded = json.loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    decoded = {}
                usage_record = ledger.record(
                    decoded if isinstance(decoded, dict) else {},
                    request_model,
                    provider_routes=configured_routes,
                )
                if not usage_record["response_complete"]:
                    self.send_json(
                        HTTPStatus.BAD_GATEWAY,
                        {
                            "error": "OpenRouter returned an empty completion",
                            "retryable": True,
                        },
                    )
                    return
                if usage_record["response_model"] in {
                    "unknown",
                    "route_mismatch",
                } or usage_record["provider"] in {"unknown", "route_mismatch"}:
                    self.send_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": "OpenRouter returned an unverified route"},
                    )
                    return
                self.send_body(status, body, content_type)
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except HTTPError as error:
                body, protocol_failed = _read_http_error_body(
                    error, max_response_bytes=max_response_bytes
                )
                if protocol_failed:
                    ledger.record_error(
                        HTTPStatus.BAD_GATEWAY,
                        request_model,
                        b"",
                        provider_routes=configured_routes,
                    )
                    self.send_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": "OpenRouter request failed"},
                    )
                    return
                ledger.record_error(
                    error.code,
                    request_model,
                    body,
                    provider_routes=configured_routes,
                )
                # The upstream body is private accounting input only.  Provider
                # errors may contain request fragments, account metadata, or
                # credential-shaped text, so never forward them into the
                # harness exception stream and its immutable evaluator log.
                self.send_json(error.code, {"error": "OpenRouter request failed"})
            except HTTPException:
                ledger.record_error(
                    HTTPStatus.BAD_GATEWAY,
                    request_model,
                    b"",
                    provider_routes=configured_routes,
                )
                self.send_json(
                    HTTPStatus.BAD_GATEWAY, {"error": "OpenRouter request failed"}
                )
            except (TimeoutError, URLError, OSError):
                ledger.record_error(
                    HTTPStatus.BAD_GATEWAY,
                    request_model,
                    b"",
                    provider_routes=configured_routes,
                )
                self.send_json(
                    HTTPStatus.BAD_GATEWAY, {"error": "OpenRouter request failed"}
                )
            finally:
                if reserved:
                    ledger.release_request()

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    provider_group = parser.add_mutually_exclusive_group(required=True)
    provider_group.add_argument("--provider-only")
    provider_group.add_argument(
        "--provider-allow",
        help="Comma-separated provider allowlist with automatic fallback",
    )
    parser.add_argument("--reasoning-effort", choices=("high", "xhigh"))
    parser.add_argument("--usage-log", type=Path)
    parser.add_argument("--max-cost-usd", type=float, default=1.0)
    parser.add_argument("--request-reservation-usd", type=float, default=0.05)
    parser.add_argument("--budget-safety-reserve-usd", type=float, default=0.01)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--max-request-bytes", type=int, default=20 * 1024 * 1024)
    parser.add_argument("--max-response-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    args = parser.parse_args()

    api_key = os.getenv(args.api_key_env, "").strip()
    if not api_key:
        parser.error(f"{args.api_key_env} is required")
    raw_provider_allow = (args.provider_allow or "").split(",")
    if args.provider_allow and any(not item.strip() for item in raw_provider_allow):
        parser.error("--provider-allow must not contain empty entries")
    provider_allow = tuple(item.strip() for item in raw_provider_allow if item.strip())

    try:
        ledger = UsageLedger(
            args.usage_log.expanduser().resolve() if args.usage_log else None,
            args.max_cost_usd,
            request_reservation_usd=args.request_reservation_usd,
            safety_reserve_usd=args.budget_safety_reserve_usd,
        )
        handler = make_handler(
            api_key=api_key,
            upstream=args.upstream,
            provider_only=args.provider_only,
            provider_allow=provider_allow,
            reasoning_effort=args.reasoning_effort,
            ledger=ledger,
            timeout=args.timeout,
            max_request_bytes=args.max_request_bytes,
            max_response_bytes=args.max_response_bytes,
        )
    except ValueError as error:
        parser.error(str(error))
    server = ThreadingHTTPServer((args.host, args.port), handler)

    def stop_on_signal(_signum: int, _frame: Any) -> None:
        # Background shells may start children with SIGINT ignored.  Install
        # explicit handlers for both wrapper shutdown signals so the process
        # always reaches the evidence-emitting finally block.
        raise KeyboardInterrupt

    previous_handlers = {
        stop_signal: signal.signal(stop_signal, stop_on_signal)
        for stop_signal in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        print(
            json.dumps(
                {
                    "ok": True,
                    "url": f"http://{args.host}:{args.port}/v1",
                    "provider_only": args.provider_only,
                    "provider_allow": list(provider_allow),
                    "reasoning_effort": args.reasoning_effort,
                    "usage_log": str(ledger.path) if ledger.path else None,
                    "max_cost_usd": args.max_cost_usd,
                    "request_reservation_usd": args.request_reservation_usd,
                    "budget_safety_reserve_usd": args.budget_safety_reserve_usd,
                    "max_response_bytes": args.max_response_bytes,
                }
            ),
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print(json.dumps({"stopped": True, "usage": ledger.summary()}), flush=True)
        for stop_signal, previous_handler in previous_handlers.items():
            signal.signal(stop_signal, previous_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
