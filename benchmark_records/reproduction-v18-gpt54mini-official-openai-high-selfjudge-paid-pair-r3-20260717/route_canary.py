#!/usr/bin/env python3
"""Probe the pinned official OpenAI snapshot once in each evaluation role.

The response is validated without retaining prompt or completion content.  Cost
is derived from official response token usage; the official API does not return
a provider-computed ``usage.cost`` field.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


MODEL = "gpt-5.4-mini-2026-03-17"
INPUT_PER_MILLION = Decimal("0.75")
CACHED_INPUT_PER_MILLION = Decimal("0.075")
OUTPUT_PER_MILLION = Decimal("4.50")
MILLION = Decimal(1_000_000)
CASES = (
    (
        "answerer",
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "A shop has 17 boxes with 19 items each. "
                        "Reply with only the total."
                    ),
                }
            ],
            "max_completion_tokens": 128,
        },
    ),
    (
        "judge",
        {
            "messages": [{"role": "user", "content": "Reply with exactly YES."}],
            "max_completion_tokens": 128,
        },
    ),
)
EXPECTED_OUTPUTS = {"answerer": "323", "judge": "YES"}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_response(raw: bytes) -> dict:
    parsed = json.loads(
        raw,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON number: {value}")
        ),
        object_pairs_hook=_unique_object,
    )
    if not isinstance(parsed, dict):
        raise ValueError("canary response is not an object")
    return parsed


def _token(value: object, *, label: str, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < (1 if positive else 0):
        raise ValueError(f"{label} is out of range")
    return value


def _usage_cost(usage: object) -> Decimal:
    if not isinstance(usage, dict):
        raise ValueError("usage is missing")
    prompt = _token(usage.get("prompt_tokens"), label="prompt_tokens", positive=True)
    completion = _token(
        usage.get("completion_tokens"), label="completion_tokens", positive=True
    )
    total = _token(usage.get("total_tokens"), label="total_tokens", positive=True)
    if total != prompt + completion:
        raise ValueError("total_tokens does not equal prompt plus completion")
    prompt_details = usage.get("prompt_tokens_details")
    if prompt_details is None:
        prompt_details = {}
    if not isinstance(prompt_details, dict):
        raise ValueError("prompt_tokens_details is invalid")
    completion_details = usage.get("completion_tokens_details")
    if completion_details is None:
        completion_details = {}
    if not isinstance(completion_details, dict):
        raise ValueError("completion_tokens_details is invalid")
    cached = _token(
        prompt_details.get("cached_tokens", 0), label="cached_tokens"
    )
    reasoning = _token(
        completion_details.get("reasoning_tokens", 0), label="reasoning_tokens"
    )
    if cached > prompt or reasoning > completion:
        raise ValueError("usage detail tokens exceed their billed parent total")
    for name in ("audio_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens"):
        value = completion_details.get(name, 0)
        if value is not None and _token(value, label=name) != 0:
            raise ValueError(f"unexpected {name}")
    prompt_audio = prompt_details.get("audio_tokens", 0)
    if prompt_audio is not None and _token(
        prompt_audio, label="prompt audio_tokens"
    ) != 0:
        raise ValueError("unexpected prompt audio_tokens")
    # Reasoning is already included in completion_tokens and is not billed twice.
    return (
        Decimal(prompt - cached) * INPUT_PER_MILLION
        + Decimal(cached) * CACHED_INPUT_PER_MILLION
        + Decimal(completion) * OUTPUT_PER_MILLION
    ) / MILLION


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    opener = build_opener(ProxyHandler({}))
    calls = []
    for label, extra in CASES:
        payload = {"model": MODEL, "stream": False, **extra}
        if "temperature" in payload:
            raise RuntimeError("temperature must be omitted in both roles")
        request = Request(
            args.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer local-transport",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with opener.open(request, timeout=180) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            status = response.status
        decoded = _strict_response(raw)
        choices = decoded.get("choices") if isinstance(decoded, dict) else None
        usage = decoded.get("usage")
        if (
            status != 200
            or len(raw) > 4 * 1024 * 1024
            or not isinstance(choices, list)
            or len(choices) != 1
            or decoded.get("object") != "chat.completion"
            or decoded.get("model") != MODEL
            or decoded.get("service_tier") != "default"
        ):
            raise RuntimeError(f"{label} canary route failed")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        content = (
            str(message.get("content") or "").strip()
            if isinstance(message, dict)
            else ""
        )
        cost = _usage_cost(usage)
        if (
            not isinstance(message, dict)
            or choice.get("index") != 0
            or message.get("role") != "assistant"
            or content != EXPECTED_OUTPUTS[label]
            or bool(message.get("tool_calls") or message.get("function_call"))
            or bool(message.get("refusal"))
            or choice.get("finish_reason") != "stop"
            or cost <= 0
        ):
            raise RuntimeError(f"{label} canary incomplete")
        calls.append(
            {
                "label": label,
                "request_model": MODEL,
                "http_status": status,
                "response_model": decoded.get("model"),
                "endpoint_provider_identity": "OpenAI",
                "finish_reason": choice.get("finish_reason"),
                "output_exact": True,
                "usage_validated_and_cost_computed": True,
                "computed_cost_usd": format(cost, "f"),
                "max_completion_tokens": 128,
                "reasoning_effort": "high",
                "service_tier": "default",
                "store": False,
                "temperature_omitted": True,
                "content_retained": False,
            }
        )
    document = {
        "schema_version": "narratordb.route-canary.v1",
        "observed_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "complete": True,
        "same_model_self_judge": True,
        "calls": calls,
        "prompt_or_completion_content_retained": False,
    }
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    if args.output.exists() or args.output.is_symlink():
        raise RuntimeError("canary output must start absent")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    print(
        json.dumps(
            {
                "ok": True,
                "calls": len(calls),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
