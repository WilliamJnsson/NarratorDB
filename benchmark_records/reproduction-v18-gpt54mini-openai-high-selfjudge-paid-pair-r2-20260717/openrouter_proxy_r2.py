#!/usr/bin/env python3
"""Cost-capped OpenRouter transport for reproducible benchmark runs.

The official benchmark harness remains responsible for prompts, sampling,
answer generation, judging, and aggregation.  This transport only pins
OpenRouter routing/model options and records response usage without retaining
prompt or completion content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from http import HTTPStatus
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

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
_OUTPUT_TOKEN_PARAMETERS = frozenset({"max_tokens", "max_completion_tokens"})
_MODEL_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh"})
_MAX_LOGICAL_ATTEMPTS = 5
_MAX_DISCARDED_TRANSIENTS = 4
_PINNED_UPSTREAM_TIMEOUT_SECONDS = 105.0


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


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_loads(payload: bytes) -> Any:
    return json.loads(
        payload,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON number is not allowed: {value}")
        ),
        object_pairs_hook=_unique_json_object,
    )


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


def _normalize_model_routes(
    model_routes: Mapping[str, tuple[str, ...]] | None,
) -> dict[str, tuple[str, ...]]:
    """Validate exact model-to-provider overrides without normalizing models."""

    if model_routes is None:
        return {}
    if not isinstance(model_routes, Mapping):
        raise ValueError("model_routes must be a model-to-provider mapping")
    normalized: dict[str, tuple[str, ...]] = {}
    for model, routes in model_routes.items():
        if not isinstance(model, str) or _safe_model(model) != model:
            raise ValueError(
                "model route keys must be exact sanitized model identifiers"
            )
        if not isinstance(routes, (tuple, list)) or not routes:
            raise ValueError(f"model route for {model!r} must contain providers")
        normalized[model] = normalize_openrouter_provider_allowlist(routes)
    return normalized


def _parse_model_route(value: str) -> tuple[str, tuple[str, ...]]:
    """Parse one ``MODEL=PROVIDER[,PROVIDER...]`` command-line override."""

    model, separator, provider_text = value.partition("=")
    if not separator or not model or not provider_text:
        raise argparse.ArgumentTypeError("must use MODEL=PROVIDER[,PROVIDER...]")
    if _safe_model(model) != model:
        raise argparse.ArgumentTypeError(
            "MODEL must be an exact sanitized model identifier"
        )
    raw_routes = provider_text.split(",")
    if any(not route.strip() for route in raw_routes):
        raise argparse.ArgumentTypeError("provider list must not contain empty entries")
    try:
        routes = normalize_openrouter_provider_allowlist(
            tuple(route.strip() for route in raw_routes)
        )
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return model, routes


def _normalize_model_output_token_parameters(
    parameters: Mapping[str, str] | None,
) -> dict[str, str]:
    """Validate exact model-to-output-token-field compatibility settings."""

    if parameters is None:
        return {}
    if not isinstance(parameters, Mapping):
        raise ValueError(
            "model_output_token_parameters must be a model-to-parameter mapping"
        )
    normalized: dict[str, str] = {}
    for model, parameter in parameters.items():
        if not isinstance(model, str) or _safe_model(model) != model:
            raise ValueError(
                "output-token model keys must be exact sanitized model identifiers"
            )
        if parameter not in _OUTPUT_TOKEN_PARAMETERS:
            raise ValueError(
                f"output-token parameter for {model!r} must be max_tokens "
                "or max_completion_tokens"
            )
        normalized[model] = parameter
    return normalized


def _parse_model_output_token_parameter(value: str) -> tuple[str, str]:
    """Parse one ``MODEL=max_tokens|max_completion_tokens`` CLI setting."""

    model, separator, parameter = value.partition("=")
    if not separator or not model or not parameter:
        raise argparse.ArgumentTypeError(
            "must use MODEL=max_tokens|max_completion_tokens"
        )
    if _safe_model(model) != model:
        raise argparse.ArgumentTypeError(
            "MODEL must be an exact sanitized model identifier"
        )
    if parameter not in _OUTPUT_TOKEN_PARAMETERS:
        raise argparse.ArgumentTypeError(
            "parameter must be max_tokens or max_completion_tokens"
        )
    return model, parameter


def _apply_output_token_parameter(
    payload: dict[str, Any], desired_parameter: str | None
) -> None:
    """Apply a validated output-token field rename to a copied request."""

    if desired_parameter is None:
        return
    if desired_parameter not in _OUTPUT_TOKEN_PARAMETERS:
        raise ValueError(
            "output-token parameter must be max_tokens or max_completion_tokens"
        )
    alternate_parameter = next(
        parameter
        for parameter in _OUTPUT_TOKEN_PARAMETERS
        if parameter != desired_parameter
    )
    desired_present = desired_parameter in payload
    alternate_present = alternate_parameter in payload
    if desired_present and alternate_present:
        raise ValueError(
            "request must not contain both max_tokens and max_completion_tokens"
        )
    present_parameter = (
        desired_parameter
        if desired_present
        else alternate_parameter
        if alternate_present
        else None
    )
    if present_parameter is None:
        return
    value = payload[present_parameter]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_INTEGER
    ):
        raise ValueError(
            f"{present_parameter} must be a positive integer no greater than "
            f"{_MAX_INTEGER}"
        )
    if alternate_present:
        payload[desired_parameter] = payload.pop(alternate_parameter)


def _normalize_model_omit_temperature(
    models: tuple[str, ...] | list[str] | None,
) -> frozenset[str]:
    """Validate models whose unsupported temperature field must be omitted."""

    if models is None:
        return frozenset()
    if not isinstance(models, (tuple, list)):
        raise ValueError("model_omit_temperature must be an array of exact models")
    normalized: list[str] = []
    for model in models:
        if not isinstance(model, str) or _safe_model(model) != model:
            raise ValueError(
                "temperature-omit models must be exact sanitized model identifiers"
            )
        normalized.append(model)
    if len(set(normalized)) != len(normalized):
        raise ValueError("model_omit_temperature entries must be unique")
    return frozenset(normalized)


def _parse_model_omit_temperature(value: str) -> str:
    """Parse one exact model slug for ``--model-omit-temperature``."""

    if _safe_model(value) != value:
        raise argparse.ArgumentTypeError(
            "MODEL must be an exact sanitized model identifier"
        )
    return value


def _normalize_model_reasoning_efforts(
    efforts: Mapping[str, str] | None,
) -> dict[str, str]:
    """Validate exact model-to-reasoning-effort compatibility settings."""

    if efforts is None:
        return {}
    if not isinstance(efforts, Mapping):
        raise ValueError("model_reasoning_efforts must be a model-to-effort mapping")
    normalized: dict[str, str] = {}
    for model, effort in efforts.items():
        if not isinstance(model, str) or _safe_model(model) != model:
            raise ValueError(
                "reasoning-effort model keys must be exact sanitized model identifiers"
            )
        if not isinstance(effort, str) or effort not in _MODEL_REASONING_EFFORTS:
            raise ValueError(
                f"reasoning effort for {model!r} must be none, low, medium, "
                "high, or xhigh"
            )
        normalized[model] = effort
    return normalized


def _parse_model_reasoning_effort(value: str) -> tuple[str, str]:
    """Parse one ``MODEL=none|low|medium|high|xhigh`` CLI setting."""

    model, separator, effort = value.partition("=")
    if not separator or not model or not effort:
        raise argparse.ArgumentTypeError("must use MODEL=none|low|medium|high|xhigh")
    if _safe_model(model) != model:
        raise argparse.ArgumentTypeError(
            "MODEL must be an exact sanitized model identifier"
        )
    if effort not in _MODEL_REASONING_EFFORTS:
        raise argparse.ArgumentTypeError(
            "effort must be none, low, medium, high, or xhigh"
        )
    return model, effort


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
    public_benchmark: bool = False,
    output_token_parameter: str | None = None,
    omit_temperature: bool = False,
) -> dict[str, Any]:
    """Return a copy with reproducibility controls added to the request."""
    prepared = dict(payload)
    _apply_output_token_parameter(prepared, output_token_parameter)
    if omit_temperature:
        prepared.pop("temperature", None)
    routes = _provider_routes(provider_only, provider_allow)
    prepared["provider"] = {
        "only": list(routes),
        "order": list(routes),
        "allow_fallbacks": len(routes) > 1,
        "require_parameters": True,
        "data_collection": "allow" if public_benchmark else "deny",
        "zdr": not public_benchmark,
    }
    if reasoning_effort == "none":
        # Exact-model ``none`` means no reasoning request at all, including
        # one supplied by the benchmark client.
        prepared.pop("reasoning", None)
    elif reasoning_effort is not None:
        if (
            not isinstance(reasoning_effort, str)
            or reasoning_effort not in _MODEL_REASONING_EFFORTS
        ):
            raise ValueError(
                "reasoning_effort must be none, low, medium, high, or xhigh"
            )
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
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
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


def _sanitized_error_usage(body: bytes) -> dict[str, Any]:
    """Extract only bounded accounting fields from a discarded error body."""

    try:
        parsed = _strict_json_loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    error = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
    metadata = (
        error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
    )
    usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}
    if not usage and isinstance(error.get("usage"), dict):
        usage = error["usage"]
    if not usage and isinstance(metadata.get("usage"), dict):
        usage = metadata["usage"]
    if not isinstance(usage, dict):
        return {}
    prompt_details = (
        usage.get("prompt_tokens_details")
        if isinstance(usage.get("prompt_tokens_details"), dict)
        else {}
    )
    completion_details = (
        usage.get("completion_tokens_details")
        if isinstance(usage.get("completion_tokens_details"), dict)
        else {}
    )
    return {
        "cost_usd": _optional_number(usage.get("cost")),
        "prompt_tokens": _integer(usage.get("prompt_tokens")),
        "cached_tokens": _integer(prompt_details.get("cached_tokens")),
        "completion_tokens": _integer(usage.get("completion_tokens")),
        "reasoning_tokens": _integer(completion_details.get("reasoning_tokens")),
    }


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
        self._discarded_transients = 0
        self._transport_failed = False
        self._logical_ordinals: dict[str, int] = {}
        self._pending_attempts: dict[str, list[tuple[str, int]]] = {}
        self._active_attempts: set[str] = set()
        self._active_payloads: set[str] = set()
        self._hidden_sdk_retry_rejections = 0
        if self.path and self.path.exists():
            raise ValueError("r2 transport usage ledger must start absent")

    def start_attempt(self, request_payload_sha256: str) -> dict[str, Any] | None:
        """Allocate content-free logical/physical retry evidence."""

        if not re.fullmatch(r"[0-9a-f]{64}", request_payload_sha256):
            raise ValueError("request payload checksum must be lowercase SHA-256")
        with self._lock:
            if self._transport_failed:
                return None
            if request_payload_sha256 in self._active_payloads:
                self._transport_failed = True
                return None
            pending = self._pending_attempts.get(request_payload_sha256, [])
            if pending:
                logical_call_id, attempt_number = pending.pop(0)
                if not pending:
                    self._pending_attempts.pop(request_payload_sha256, None)
            else:
                ordinal = self._logical_ordinals.get(request_payload_sha256, 0) + 1
                self._logical_ordinals[request_payload_sha256] = ordinal
                logical_call_id = hashlib.sha256(
                    f"{request_payload_sha256}:{ordinal}".encode("ascii")
                ).hexdigest()
                attempt_number = 1
            if attempt_number > _MAX_LOGICAL_ATTEMPTS:
                self._transport_failed = True
                return None
            if logical_call_id in self._active_attempts:
                raise RuntimeError("duplicate active logical-call attempt")
            self._active_attempts.add(logical_call_id)
            self._active_payloads.add(request_payload_sha256)
            return {
                "request_payload_sha256": request_payload_sha256,
                "logical_call_id": logical_call_id,
                "attempt_number": attempt_number,
            }

    def reject_hidden_sdk_retry(self) -> None:
        """Hard-fuse an SDK retry before its body or provider route is used."""

        with self._lock:
            self._hidden_sdk_retry_rejections += 1
            self._transport_failed = True

    def _finish_attempt_locked(
        self,
        attempt: Mapping[str, Any],
        *,
        response_forwarded: bool,
        retryable: bool,
    ) -> None:
        logical_call_id = str(attempt["logical_call_id"])
        request_payload_sha256 = str(attempt["request_payload_sha256"])
        attempt_number = int(attempt["attempt_number"])
        if logical_call_id not in self._active_attempts:
            raise RuntimeError("logical-call attempt was not active")
        self._active_attempts.remove(logical_call_id)
        if request_payload_sha256 not in self._active_payloads:
            raise RuntimeError("logical-call payload was not active")
        self._active_payloads.remove(request_payload_sha256)
        if response_forwarded:
            return
        if (
            retryable
            and attempt_number < _MAX_LOGICAL_ATTEMPTS
            and not self._transport_failed
        ):
            self._pending_attempts.setdefault(request_payload_sha256, []).append(
                (logical_call_id, attempt_number + 1)
            )
        else:
            self._transport_failed = True

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
        attempt: Mapping[str, Any],
        upstream_status: int,
        provider_routes: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        safe_request_model = _safe_model(request_model)
        raw_usage = response.get("usage")
        usage_is_dict = isinstance(raw_usage, dict)
        usage = raw_usage if usage_is_dict else {}
        raw_completion_details = usage.get("completion_tokens_details")
        completion_details_valid = (
            "completion_tokens_details" not in usage
            or raw_completion_details is None
            or isinstance(raw_completion_details, dict)
        )
        completion_details = (
            raw_completion_details if isinstance(raw_completion_details, dict) else {}
        )
        raw_prompt_details = usage.get("prompt_tokens_details")
        prompt_details_valid = (
            "prompt_tokens_details" not in usage
            or raw_prompt_details is None
            or isinstance(raw_prompt_details, dict)
        )
        prompt_details = (
            raw_prompt_details if isinstance(raw_prompt_details, dict) else {}
        )
        choices = response.get("choices")
        choices_absent = "choices" not in response
        choices_are_list = isinstance(choices, list)
        zero_choices = choices_absent or (choices_are_list and len(choices) == 0)
        exact_choice = (
            choices_are_list
            and len(choices) == 1
            and isinstance(choices[0], dict)
        )
        first_choice = choices[0] if exact_choice else {}
        raw_message = first_choice.get("message")
        message_is_dict = isinstance(raw_message, dict)
        message = raw_message if message_is_dict else {}
        content_missing = "content" not in message
        content = message.get("content")
        response_complete = isinstance(content, str) and bool(content.strip())
        content_is_empty = content_missing or content is None or (
            isinstance(content, str) and not content.strip()
        )
        invalid_content_type = (
            not content_missing and content is not None and not isinstance(content, str)
        )
        role_present = "role" in message
        role = message.get("role")
        invalid_message_role = (role_present and role != "assistant") or (
            response_complete and role != "assistant"
        )
        raw_tool_calls = message.get("tool_calls")
        tool_calls_valid = (
            "tool_calls" not in message
            or raw_tool_calls is None
            or (isinstance(raw_tool_calls, list) and not raw_tool_calls)
        )
        function_call_valid = (
            "function_call" not in message or message.get("function_call") is None
        )
        raw_refusal = message.get("refusal")
        refusal_valid = (
            "refusal" not in message
            or raw_refusal is None
            or (isinstance(raw_refusal, str) and raw_refusal == "")
        )
        provider_cost = _optional_number(usage.get("cost"))
        raw_prompt_tokens = usage.get("prompt_tokens")
        observed_cached_tokens = prompt_details.get("cached_tokens")
        raw_cached_tokens = (
            0 if observed_cached_tokens is None else observed_cached_tokens
        )
        raw_completion_tokens = usage.get("completion_tokens")
        observed_reasoning_tokens = completion_details.get("reasoning_tokens")
        raw_reasoning_tokens = (
            0 if observed_reasoning_tokens is None else observed_reasoning_tokens
        )
        accounting_valid = (
            usage_is_dict
            and prompt_details_valid
            and completion_details_valid
            and not isinstance(raw_prompt_tokens, bool)
            and isinstance(raw_prompt_tokens, int)
            and raw_prompt_tokens > 0
            and not isinstance(raw_cached_tokens, bool)
            and isinstance(raw_cached_tokens, int)
            and raw_cached_tokens >= 0
            and not isinstance(raw_completion_tokens, bool)
            and isinstance(raw_completion_tokens, int)
            and raw_completion_tokens > 0
            and not isinstance(raw_reasoning_tokens, bool)
            and isinstance(raw_reasoning_tokens, int)
            and raw_reasoning_tokens >= 0
            and provider_cost is not None
            and provider_cost > 0
        )
        response_model = _canonical_response_model(
            response.get("model"), safe_request_model
        )
        provider = _canonical_provider(response.get("provider"), provider_routes)
        raw_finish_reason = first_choice.get("finish_reason")
        finish_reason = _safe_finish_reason(raw_finish_reason)
        discarded_reason: str | None = None
        retryable = False
        if upstream_status != HTTPStatus.OK:
            discarded_reason = "unexpected_http_success_status"
        elif zero_choices:
            discarded_reason = "empty_completion"
            retryable = True
        elif not choices_are_list:
            discarded_reason = "choice_schema_mismatch"
        elif not exact_choice:
            discarded_reason = "choice_count_mismatch"
        elif not message_is_dict:
            discarded_reason = "message_schema_mismatch"
        elif invalid_content_type:
            discarded_reason = "invalid_content_type"
        elif invalid_message_role:
            discarded_reason = "invalid_message_role"
        elif not tool_calls_valid or not function_call_valid:
            discarded_reason = "unexpected_tool_call"
        elif not refusal_valid:
            discarded_reason = "unexpected_refusal"
        elif raw_finish_reason is not None and raw_finish_reason != "stop":
            discarded_reason = (
                "non_stop_finish"
                if isinstance(raw_finish_reason, str)
                else "invalid_finish_reason"
            )
        elif content_is_empty:
            discarded_reason = "empty_completion"
            retryable = True
        elif response_model in {"unknown", "route_mismatch"} or provider in {
            "unknown",
            "route_mismatch",
        }:
            discarded_reason = "route_identity_unverified"
        elif finish_reason != "stop":
            discarded_reason = "non_stop_finish"
        elif not accounting_valid:
            discarded_reason = "invalid_accounting"
        response_forwarded = discarded_reason is None
        common = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": _integer(upstream_status),
            "request_model": safe_request_model,
            "request_payload_sha256": str(attempt["request_payload_sha256"]),
            "logical_call_id": str(attempt["logical_call_id"]),
            "attempt_number": int(attempt["attempt_number"]),
            "response_forwarded": response_forwarded,
            "discarded_reason": discarded_reason,
            "retryable": retryable,
        }
        if response_forwarded:
            record = {
                **common,
                "event": "completion",
                "response_model": response_model,
                "provider": provider,
                "finish_reason": finish_reason,
                "response_complete": True,
                "prompt_tokens": raw_prompt_tokens,
                "cached_tokens": raw_cached_tokens,
                "completion_tokens": raw_completion_tokens,
                "reasoning_tokens": raw_reasoning_tokens,
                "cost_usd": provider_cost,
                "unknown_cost": False,
            }
        else:
            discarded_cost = (
                self.request_reservation_usd
                if provider_cost is None
                else max(self.request_reservation_usd, provider_cost)
            )
            record = {
                **common,
                "event": (
                    "discarded_transient" if retryable else "terminal_rejection"
                ),
                "response_model": response_model,
                "provider": provider,
                "finish_reason": "unknown",
                "response_complete": False,
                "prompt_tokens": _integer(usage.get("prompt_tokens")),
                "cached_tokens": _integer(prompt_details.get("cached_tokens")),
                "completion_tokens": _integer(usage.get("completion_tokens")),
                "reasoning_tokens": _integer(
                    completion_details.get("reasoning_tokens")
                ),
                "cost_usd": discarded_cost,
                "unknown_cost": provider_cost is None,
            }
        with self._lock:
            self._calls += int(response_forwarded)
            self._malformed_responses += int(retryable)
            self._discarded_transients += int(retryable)
            if not response_forwarded and not retryable:
                self._transport_failed = True
            if self._discarded_transients > _MAX_DISCARDED_TRANSIENTS:
                self._transport_failed = True
            self._cost += record["cost_usd"]
            self._prompt_tokens += record["prompt_tokens"]
            self._cached_tokens += record["cached_tokens"]
            self._completion_tokens += record["completion_tokens"]
            self._reasoning_tokens += record["reasoning_tokens"]
            self._unknown_cost_attempts += int(record["unknown_cost"])
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(record, sort_keys=True) + "\n")
            self._finish_attempt_locked(
                attempt,
                response_forwarded=response_forwarded,
                retryable=retryable,
            )
        return record

    def record_error(
        self,
        status: int,
        request_model: str,
        *,
        attempt: Mapping[str, Any],
        discarded_reason: str,
        observed_usage: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if discarded_reason not in {
            "oversized_response",
            "upstream_http_error",
            "upstream_protocol_error",
            "upstream_timeout_or_network",
        }:
            raise ValueError("unsupported rejection reason")
        status_code = _integer(status)
        retryable = (
            discarded_reason == "upstream_timeout_or_network"
            or (
                discarded_reason == "upstream_http_error"
                and status_code in {408, 409, 429, 500, 502, 503, 504}
            )
        )
        observed_usage = observed_usage or {}
        observed_cost = _optional_number(observed_usage.get("cost_usd"))
        booked_cost = (
            self.request_reservation_usd
            if observed_cost is None
            else max(self.request_reservation_usd, observed_cost)
        )
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": (
                "discarded_transient" if retryable else "terminal_rejection"
            ),
            "status": status_code,
            "request_model": _safe_model(request_model),
            "response_model": "route_mismatch",
            "provider": "unknown",
            "finish_reason": "unknown",
            "response_complete": False,
            "prompt_tokens": _integer(observed_usage.get("prompt_tokens")),
            "cached_tokens": _integer(observed_usage.get("cached_tokens")),
            "completion_tokens": _integer(observed_usage.get("completion_tokens")),
            "reasoning_tokens": _integer(observed_usage.get("reasoning_tokens")),
            "cost_usd": booked_cost,
            "unknown_cost": observed_cost is None,
            "request_payload_sha256": str(attempt["request_payload_sha256"]),
            "logical_call_id": str(attempt["logical_call_id"]),
            "attempt_number": int(attempt["attempt_number"]),
            "response_forwarded": False,
            "discarded_reason": discarded_reason,
            "retryable": retryable,
        }
        with self._lock:
            self._errors += 1
            self._discarded_transients += int(retryable)
            if not retryable:
                self._transport_failed = True
            if self._discarded_transients > _MAX_DISCARDED_TRANSIENTS:
                self._transport_failed = True
            self._cost += record["cost_usd"]
            self._prompt_tokens += record["prompt_tokens"]
            self._cached_tokens += record["cached_tokens"]
            self._completion_tokens += record["completion_tokens"]
            self._reasoning_tokens += record["reasoning_tokens"]
            self._unknown_cost_attempts += int(record["unknown_cost"])
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(record, sort_keys=True) + "\n")
            self._finish_attempt_locked(
                attempt, response_forwarded=False, retryable=retryable
            )
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
                "discarded_transients": self._discarded_transients,
                "max_discarded_transients": _MAX_DISCARDED_TRANSIENTS,
                "max_logical_attempts": _MAX_LOGICAL_ATTEMPTS,
                "transport_failed": self._transport_failed,
                "pending_logical_calls": sum(
                    len(values) for values in self._pending_attempts.values()
                ),
                "active_logical_calls": len(self._active_attempts),
                "hidden_sdk_retry_rejections": self._hidden_sdk_retry_rejections,
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
    model_routes: Mapping[str, tuple[str, ...]] | None = None,
    model_output_token_parameters: Mapping[str, str] | None = None,
    model_omit_temperature: tuple[str, ...] | list[str] | None = None,
    model_reasoning_efforts: Mapping[str, str] | None = None,
    reasoning_effort: str | None,
    public_benchmark: bool = False,
    ledger: UsageLedger,
    timeout: float,
    max_request_bytes: int,
    max_response_bytes: int = 4 * 1024 * 1024,
    allow_insecure_test_upstream: bool = False,
):
    configured_routes = _provider_routes(provider_only, provider_allow)
    configured_model_routes = _normalize_model_routes(model_routes)
    model_route_metadata = {
        model: list(routes) for model, routes in configured_model_routes.items()
    }
    configured_output_token_parameters = _normalize_model_output_token_parameters(
        model_output_token_parameters
    )
    configured_temperature_omit_models = _normalize_model_omit_temperature(
        model_omit_temperature
    )
    temperature_omit_metadata = sorted(configured_temperature_omit_models)
    configured_model_reasoning_efforts = _normalize_model_reasoning_efforts(
        model_reasoning_efforts
    )
    model_reasoning_effort_metadata = {
        model: configured_model_reasoning_efforts[model]
        for model in sorted(configured_model_reasoning_efforts)
    }
    upstream = _validate_upstream(
        upstream, allow_insecure_test_upstream=allow_insecure_test_upstream
    )
    if timeout != _PINNED_UPSTREAM_TIMEOUT_SECONDS:
        raise ValueError("upstream timeout must be exactly 105 seconds")
    if max_request_bytes <= 0 or max_response_bytes <= 0:
        raise ValueError("timeout and request/response size limits must be positive")

    class Handler(BaseHTTPRequestHandler):
        server_version = "NarratorDBOpenRouterTransport/1.0"

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            self.send_body(
                status, json.dumps(payload).encode("utf-8"), "application/json"
            )

        def send_error_json(self, status: int, payload: dict[str, Any]) -> None:
            self.send_body(
                status,
                json.dumps(payload).encode("utf-8"),
                "application/json",
                should_retry=False,
            )

        def send_body(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            should_retry: bool | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if should_retry is not None:
                self.send_header(
                    "x-should-retry", "true" if should_retry else "false"
                )
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
                        "model_routes": model_route_metadata,
                        "model_output_token_parameters": (
                            configured_output_token_parameters
                        ),
                        "model_omit_temperature": temperature_omit_metadata,
                        "model_reasoning_efforts": model_reasoning_effort_metadata,
                        "reasoning_effort": reasoning_effort,
                        "public_benchmark": public_benchmark,
                        "upstream_timeout_seconds": timeout,
                        "max_request_bytes": max_request_bytes,
                        "max_response_bytes": max_response_bytes,
                        "direct_upstream_networking": True,
                        "inbound_retry_count_policy": "absent-or-zero-only",
                        "local_caller_auth_required": True,
                        "usage": ledger.summary(),
                    },
                )
                return
            self.send_error_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            request_model = "unknown"
            request_routes = configured_routes
            reserved = False
            attempt: Mapping[str, Any] | None = None
            if self.headers.get("Authorization") != "Bearer local-transport":
                self.send_error_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "NarratorDB rejected local transport authentication"},
                )
                return
            if self.headers.get("x-stainless-retry-count") not in {None, "0"}:
                ledger.reject_hidden_sdk_retry()
                self.send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "NarratorDB rejected an internal SDK retry"},
                )
                return
            if self.path.split("?", 1)[0].rstrip("/") not in {
                "/chat/completions",
                "/v1/chat/completions",
            }:
                self.send_error_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > max_request_bytes:
                    raise ValueError("invalid or oversized request body")
                payload = _strict_json_loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
                if payload.get("stream"):
                    raise ValueError(
                        "streaming is not supported by the benchmark transport"
                    )
                raw_request_model = payload.get("model")
                request_model = _safe_model(raw_request_model)
                if request_model == "unknown" or request_model != raw_request_model:
                    raise ValueError(
                        "request model must be a sanitized model identifier"
                    )
                request_routes = configured_model_routes.get(
                    request_model, configured_routes
                )
                prepared = prepare_openrouter_payload(
                    payload,
                    provider_only=(
                        request_routes[0] if len(request_routes) == 1 else None
                    ),
                    provider_allow=(request_routes if len(request_routes) > 1 else ()),
                    reasoning_effort=configured_model_reasoning_efforts.get(
                        request_model, reasoning_effort
                    ),
                    public_benchmark=public_benchmark,
                    output_token_parameter=configured_output_token_parameters.get(
                        request_model
                    ),
                    omit_temperature=(
                        request_model in configured_temperature_omit_models
                    ),
                )
                request_payload_sha256 = hashlib.sha256(
                    json.dumps(
                        prepared,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
                if not ledger.reserve_request():
                    self.send_error_json(
                        HTTPStatus.PAYMENT_REQUIRED,
                        {
                            "error": "NarratorDB benchmark cost cap reached",
                            "usage": ledger.summary(),
                        },
                    )
                    return
                reserved = True
                attempt = ledger.start_attempt(request_payload_sha256)
                if attempt is None:
                    self.send_error_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "NarratorDB r2 transport invariant failed"},
                    )
                    return
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
                with build_opener(ProxyHandler({}), _NoRedirectHandler()).open(
                    request, timeout=timeout
                ) as response:
                    body = response.read(max_response_bytes + 1)
                    status = response.status
                if len(body) > max_response_bytes:
                    assert attempt is not None
                    ledger.record_error(
                        HTTPStatus.BAD_GATEWAY,
                        request_model,
                        attempt=attempt,
                        discarded_reason="oversized_response",
                    )
                    self.send_error_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": "OpenRouter response exceeded the size limit"},
                    )
                    return
                try:
                    decoded = _strict_json_loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    assert attempt is not None
                    ledger.record_error(
                        HTTPStatus.BAD_GATEWAY,
                        request_model,
                        attempt=attempt,
                        discarded_reason="upstream_protocol_error",
                    )
                    self.send_error_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": "OpenRouter response failed strict JSON validation"},
                    )
                    return
                assert attempt is not None
                if not isinstance(decoded, dict):
                    ledger.record_error(
                        HTTPStatus.BAD_GATEWAY,
                        request_model,
                        attempt=attempt,
                        discarded_reason="upstream_protocol_error",
                    )
                    self.send_error_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": "OpenRouter response failed schema validation"},
                    )
                    return
                usage_record = ledger.record(
                    decoded,
                    request_model,
                    attempt=attempt,
                    upstream_status=status,
                    provider_routes=request_routes,
                )
                if not usage_record["response_forwarded"]:
                    self.send_error_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": "OpenRouter response rejected by r2 transport"},
                    )
                    return
                self.send_body(status, body, "application/json")
            except (json.JSONDecodeError, TypeError, ValueError):
                self.send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "NarratorDB rejected an invalid local request"},
                )
            except HTTPError as error:
                body, protocol_failed = _read_http_error_body(
                    error, max_response_bytes=max_response_bytes
                )
                observed_usage = _sanitized_error_usage(body)
                if attempt is None:
                    self.send_error_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": "OpenRouter request failed before accounting"},
                    )
                    return
                if protocol_failed:
                    ledger.record_error(
                        HTTPStatus.BAD_GATEWAY,
                        request_model,
                        attempt=attempt,
                        discarded_reason="upstream_protocol_error",
                    )
                    self.send_error_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": "OpenRouter request failed"},
                    )
                    return
                ledger.record_error(
                    error.code,
                    request_model,
                    attempt=attempt,
                    discarded_reason="upstream_http_error",
                    observed_usage=observed_usage,
                )
                # The upstream body is private accounting input only.  Provider
                # errors may contain request fragments, account metadata, or
                # credential-shaped text, so never forward them into the
                # harness exception stream and its immutable evaluator log.
                self.send_error_json(
                    error.code, {"error": "OpenRouter request failed"}
                )
            except HTTPException:
                if attempt is None:
                    self.send_error_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": "OpenRouter request failed before accounting"},
                    )
                    return
                ledger.record_error(
                    HTTPStatus.BAD_GATEWAY,
                    request_model,
                    attempt=attempt,
                    discarded_reason="upstream_protocol_error",
                )
                self.send_error_json(
                    HTTPStatus.BAD_GATEWAY, {"error": "OpenRouter request failed"}
                )
            except (TimeoutError, URLError, OSError):
                if attempt is None:
                    self.send_error_json(
                        HTTPStatus.GATEWAY_TIMEOUT,
                        {"error": "OpenRouter request failed before accounting"},
                    )
                    return
                ledger.record_error(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    request_model,
                    attempt=attempt,
                    discarded_reason="upstream_timeout_or_network",
                )
                self.send_error_json(
                    HTTPStatus.GATEWAY_TIMEOUT,
                    {"error": "OpenRouter request failed"},
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
    parser.add_argument(
        "--model-route",
        action="append",
        type=_parse_model_route,
        default=[],
        metavar="MODEL=PROVIDER[,PROVIDER...]",
        help=(
            "exact model-specific provider override; repeat for additional models "
            "(the required global provider route remains the fallback)"
        ),
    )
    parser.add_argument(
        "--model-output-token-parameter",
        action="append",
        type=_parse_model_output_token_parameter,
        default=[],
        metavar="MODEL=max_tokens|max_completion_tokens",
        help=(
            "exact model-specific output-token field compatibility setting; "
            "repeat for additional models"
        ),
    )
    parser.add_argument(
        "--model-omit-temperature",
        action="append",
        type=_parse_model_omit_temperature,
        default=[],
        metavar="MODEL",
        help=(
            "omit the temperature field only for this exact model; repeat for "
            "additional models"
        ),
    )
    parser.add_argument(
        "--model-reasoning-effort",
        action="append",
        type=_parse_model_reasoning_effort,
        default=[],
        metavar="MODEL=none|low|medium|high|xhigh",
        help=(
            "exact model-specific reasoning override; none removes the incoming "
            "reasoning field, while other values set reasoning.effort; repeat "
            "for additional models (the global reasoning effort remains the fallback)"
        ),
    )
    parser.add_argument("--reasoning-effort", choices=("high", "xhigh"))
    parser.add_argument(
        "--public-benchmark",
        action="store_true",
        help=(
            "relax ZDR/data-collection routing only for a disclosed public "
            "benchmark; private routing remains the default"
        ),
    )
    parser.add_argument("--usage-log", type=Path)
    parser.add_argument("--max-cost-usd", type=float, default=1.0)
    parser.add_argument("--request-reservation-usd", type=float, default=0.05)
    parser.add_argument("--budget-safety-reserve-usd", type=float, default=0.01)
    parser.add_argument(
        "--timeout", type=float, default=_PINNED_UPSTREAM_TIMEOUT_SECONDS
    )
    parser.add_argument("--max-request-bytes", type=int, default=20 * 1024 * 1024)
    parser.add_argument("--max-response-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    args = parser.parse_args()

    api_key = os.environ.pop(args.api_key_env, "").strip()
    if not api_key:
        parser.error(f"{args.api_key_env} is required")
    raw_provider_allow = (args.provider_allow or "").split(",")
    if args.provider_allow and any(not item.strip() for item in raw_provider_allow):
        parser.error("--provider-allow must not contain empty entries")
    provider_allow = tuple(item.strip() for item in raw_provider_allow if item.strip())
    model_routes: dict[str, tuple[str, ...]] = {}
    for model, routes in args.model_route:
        if model in model_routes:
            parser.error(f"--model-route repeats exact model {model!r}")
        model_routes[model] = routes
    model_output_token_parameters: dict[str, str] = {}
    for model, parameter in args.model_output_token_parameter:
        if model in model_output_token_parameters:
            parser.error(
                f"--model-output-token-parameter repeats exact model {model!r}"
            )
        model_output_token_parameters[model] = parameter
    if len(set(args.model_omit_temperature)) != len(args.model_omit_temperature):
        parser.error("--model-omit-temperature repeats an exact model")
    model_omit_temperature = tuple(args.model_omit_temperature)
    model_reasoning_efforts: dict[str, str] = {}
    for model, effort in args.model_reasoning_effort:
        if model in model_reasoning_efforts:
            parser.error(f"--model-reasoning-effort repeats exact model {model!r}")
        model_reasoning_efforts[model] = effort

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
            model_routes=model_routes,
            model_output_token_parameters=model_output_token_parameters,
            model_omit_temperature=model_omit_temperature,
            model_reasoning_efforts=model_reasoning_efforts,
            reasoning_effort=args.reasoning_effort,
            public_benchmark=args.public_benchmark,
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
                    "model_routes": {
                        model: list(routes) for model, routes in model_routes.items()
                    },
                    "model_output_token_parameters": (model_output_token_parameters),
                    "model_omit_temperature": sorted(model_omit_temperature),
                    "model_reasoning_efforts": {
                        model: model_reasoning_efforts[model]
                        for model in sorted(model_reasoning_efforts)
                    },
                    "reasoning_effort": args.reasoning_effort,
                    "public_benchmark": args.public_benchmark,
                    "usage_log": str(ledger.path) if ledger.path else None,
                    "max_cost_usd": args.max_cost_usd,
                    "request_reservation_usd": args.request_reservation_usd,
                    "budget_safety_reserve_usd": args.budget_safety_reserve_usd,
                    "upstream_timeout_seconds": args.timeout,
                    "max_request_bytes": args.max_request_bytes,
                    "max_response_bytes": args.max_response_bytes,
                    "direct_upstream_networking": True,
                    "inbound_retry_count_policy": "absent-or-zero-only",
                    "local_caller_auth_required": True,
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
