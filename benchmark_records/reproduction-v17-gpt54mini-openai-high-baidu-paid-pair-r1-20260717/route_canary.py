#!/usr/bin/env python3
"""Exercise the exact answerer and judge routes without retaining content."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener


CASES = (
    (
        "answerer",
        "openai/gpt-5.4-mini",
        {"messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 64},
    ),
    (
        "judge",
        "deepseek/deepseek-v4-flash-20260423",
        {
            "messages": [{"role": "user", "content": "Reply with YES."}],
            "max_tokens": 16,
            "temperature": 0,
        },
    ),
)


def _write_new(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite canary evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    opener = build_opener(ProxyHandler({}))
    observations: list[dict[str, Any]] = []
    for label, model, extra in CASES:
        request_payload = {"model": model, "stream": False, **extra}
        request = Request(
            args.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(request_payload).encode("utf-8"),
            headers={"Authorization": "Bearer local-transport", "Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(request, timeout=args.timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            status = response.status
        if status != 200 or len(raw) > 4 * 1024 * 1024:
            raise RuntimeError(f"{label} canary transport failed")
        decoded = json.loads(raw)
        if not isinstance(decoded, dict) or not isinstance(decoded.get("choices"), list):
            raise RuntimeError(f"{label} canary returned malformed JSON")
        choices = decoded["choices"]
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise RuntimeError(f"{label} canary returned an invalid choice count")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not str(message.get("content") or "").strip():
            raise RuntimeError(f"{label} canary returned empty content")
        observations.append(
            {
                "label": label,
                "request_model": model,
                "http_status": status,
                "response_model": str(decoded.get("model") or ""),
                "finish_reason": str(choices[0].get("finish_reason") or ""),
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
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_new(args.output, payload)
    print(json.dumps({"ok": True, "calls": len(observations), "sha256": hashlib.sha256(payload).hexdigest()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
