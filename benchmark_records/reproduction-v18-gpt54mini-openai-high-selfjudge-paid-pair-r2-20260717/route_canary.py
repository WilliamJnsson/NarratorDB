#!/usr/bin/env python3
"""Probe GPT-5.4-mini/OpenAI once in each sealed evaluation role."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


MODEL = "openai/gpt-5.4-mini"
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
            "max_tokens": 128,
        },
    ),
    (
        "judge",
        {
            "messages": [{"role": "user", "content": "Reply with exactly YES."}],
            "max_tokens": 128,
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


def _positive_cost(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        cost = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return cost.is_finite() and cost > 0


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
        usage = decoded.get("usage") if isinstance(decoded.get("usage"), dict) else {}
        if (
            status != 200
            or len(raw) > 4 * 1024 * 1024
            or not isinstance(choices, list)
            or len(choices) != 1
            or decoded.get("model") != MODEL
            or decoded.get("provider") != "OpenAI"
        ):
            raise RuntimeError(f"{label} canary route failed")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        content = (
            str(message.get("content") or "").strip()
            if isinstance(message, dict)
            else ""
        )
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
        cached_tokens = prompt_details.get("cached_tokens", 0)
        reasoning_tokens = completion_details.get("reasoning_tokens", 0)
        if (
            not isinstance(message, dict)
            or content != EXPECTED_OUTPUTS[label]
            or bool(message.get("tool_calls") or message.get("function_call"))
            or choice.get("finish_reason") != "stop"
            or isinstance(usage.get("prompt_tokens"), bool)
            or not isinstance(usage.get("prompt_tokens"), int)
            or usage.get("prompt_tokens") <= 0
            or isinstance(usage.get("completion_tokens"), bool)
            or not isinstance(usage.get("completion_tokens"), int)
            or usage.get("completion_tokens") <= 0
            or isinstance(cached_tokens, bool)
            or not isinstance(cached_tokens, int)
            or cached_tokens < 0
            or isinstance(reasoning_tokens, bool)
            or not isinstance(reasoning_tokens, int)
            or reasoning_tokens < 0
            or not _positive_cost(usage.get("cost"))
        ):
            raise RuntimeError(f"{label} canary incomplete")
        calls.append(
            {
                "label": label,
                "request_model": MODEL,
                "http_status": status,
                "response_model": decoded.get("model"),
                "provider": decoded.get("provider"),
                "finish_reason": choice.get("finish_reason"),
                "output_exact": True,
                "known_positive_usage_and_cost": True,
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
