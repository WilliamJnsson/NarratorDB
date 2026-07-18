#!/usr/bin/env python3
"""Retry only the pre-score route probe with safe output-token ceilings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


CASES = (
    (
        "answerer",
        "openai/gpt-5.4-mini",
        {
            "messages": [
                {
                    "role": "user",
                    "content": "A shop has 17 boxes with 19 items each. Reply with only the total.",
                }
            ],
            "max_tokens": 128,
        },
    ),
    (
        "judge",
        "deepseek/deepseek-v4-flash-20260423",
        {
            "messages": [{"role": "user", "content": "Reply with exactly YES."}],
            "max_tokens": 1024,
            "temperature": 0,
        },
    ),
)


def _write_new(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite canary evidence: {path}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    opener = build_opener(ProxyHandler({}))
    observations = []
    for label, model, extra in CASES:
        request = Request(
            args.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps({"model": model, "stream": False, **extra}).encode(),
            headers={"Authorization": "Bearer local-transport", "Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(request, timeout=180) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            status = response.status
        decoded = json.loads(raw)
        choices = decoded.get("choices") if isinstance(decoded, dict) else None
        if status != 200 or len(raw) > 4 * 1024 * 1024 or not isinstance(choices, list) or len(choices) != 1:
            raise RuntimeError(f"{label} route canary failed")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict) or not str(message.get("content") or "").strip():
            raise RuntimeError(f"{label} route canary returned empty content")
        observations.append(
            {
                "label": label,
                "request_model": model,
                "http_status": status,
                "response_model": str(decoded.get("model") or ""),
                "finish_reason": str(choice.get("finish_reason") or ""),
                "content_retained": False,
            }
        )
    document = {
        "schema_version": "narratordb.route-canary.v1",
        "observed_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "complete": True,
        "calls": observations,
        "prompt_or_completion_content_retained": False,
    }
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    _write_new(args.output, payload)
    print(json.dumps({"ok": True, "calls": len(observations), "sha256": hashlib.sha256(payload).hexdigest()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
