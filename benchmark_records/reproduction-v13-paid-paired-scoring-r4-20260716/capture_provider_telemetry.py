#!/usr/bin/env python3
"""Capture only sanitized OpenRouter key-limit telemetry for paid admission.

This is one of exactly two processes allowed to inherit ``OPENROUTER_API_KEY``
during the campaign.  It never makes a model request and never emits the key,
key label, account identity, user identity, prompts, or completions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


SCHEMA = "narratordb.provider-key-telemetry.v2"
ENDPOINT = "https://openrouter.ai/api/v1/key"
MAX_RESPONSE_BYTES = 256 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite number is forbidden: {value}")


def _decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
        raise ValueError(f"{label} is missing or nonnumeric")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label} is invalid") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return parsed


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _write_new(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite telemetry: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def capture(*, api_key: str, timeout: float = 20.0) -> dict[str, Any]:
    if not api_key or not api_key.startswith("sk-or-") or len(api_key) < 30:
        raise ValueError("OPENROUTER_API_KEY is missing or malformed")
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
        raise ValueError("timeout must be in (0,60]")
    request = Request(
        ENDPOINT,
        method="GET",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    # Direct-route evidence must not inherit shell or OS proxy discovery.  A
    # proxy would receive the bearer credential even though this request is
    # content-free.
    with build_opener(ProxyHandler({}), _NoRedirect()).open(
        request, timeout=timeout
    ) as response:
        if response.status != 200:
            raise ValueError(f"provider telemetry returned HTTP {response.status}")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        status = response.status
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("provider telemetry response exceeded the byte limit")
    try:
        decoded = json.loads(
            raw,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"provider telemetry response is invalid JSON: {error}") from error
    if not isinstance(decoded, Mapping) or not isinstance(decoded.get("data"), Mapping):
        raise ValueError("provider telemetry response lacks a data object")
    data = decoded["data"]
    limit = _decimal(data.get("limit"), label="provider limit")
    usage = _decimal(data.get("usage"), label="provider usage")
    remaining = _decimal(data.get("limit_remaining"), label="provider remaining")
    if abs(limit - usage - remaining) > Decimal("0.000000001"):
        raise ValueError("provider limit/usage/remaining arithmetic is inconsistent")
    return {
        "schema_version": SCHEMA,
        "observed_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_endpoint": ENDPOINT,
        "request_class": "authenticated content-free account telemetry",
        "http_status": status,
        "currency": "USD",
        "provider_limit_usd": _decimal_text(limit),
        "provider_usage_usd": _decimal_text(usage),
        "provider_remaining_usd": _decimal_text(remaining),
        "capture_tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "credential_recorded": False,
        "key_label_recorded": False,
        "account_identifier_recorded": False,
        "model_content_recorded": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    api_key = os.environ.pop("OPENROUTER_API_KEY", "")
    try:
        document = capture(api_key=api_key, timeout=args.timeout)
        payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _write_new(args.output, payload)
    except (HTTPError, HTTPException, OSError, URLError, ValueError) as error:
        parser.error(str(error))
    finally:
        api_key = ""
        os.environ.pop("OPENROUTER_API_KEY", None)
    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "credential_recorded": False,
                "model_content_recorded": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
