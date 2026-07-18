#!/usr/bin/env python3
"""Capture exact-model OpenRouter endpoint metadata without retaining a credential."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MODEL = "deepseek/deepseek-v4-flash-20260423"
PROVIDER = "DeepSeek"
URL = f"https://openrouter.ai/api/v1/models/{MODEL}/endpoints"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _write(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite endpoint evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-output", type=Path, required=True)
    parser.add_argument("--metadata-output", type=Path, required=True)
    args = parser.parse_args()
    key = os.environ.pop("OPENROUTER_API_KEY", "")
    if not key.startswith("sk-or-"):
        raise RuntimeError("OpenRouter credential is missing")
    request = Request(URL, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    key = ""
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=20) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
        status = response.status
    if status != 200 or len(raw) > 4 * 1024 * 1024:
        raise RuntimeError("endpoint metadata request failed")
    decoded = json.loads(raw)
    data = decoded.get("data") if isinstance(decoded, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("id") != MODEL or not isinstance(endpoints, list):
        raise RuntimeError("endpoint metadata model/schema mismatch")
    selected = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or str(endpoint.get("provider_name") or "").casefold() != PROVIDER.casefold():
            continue
        parameters = endpoint.get("supported_parameters")
        if not isinstance(parameters, list) or not all(isinstance(item, str) for item in parameters):
            raise RuntimeError("DeepSeek endpoint parameter metadata is malformed")
        selected.append(
            {
                "provider_name": endpoint.get("provider_name"),
                "tag": endpoint.get("tag"),
                "status": endpoint.get("status"),
                "quantization": endpoint.get("quantization"),
                "context_length": endpoint.get("context_length"),
                "max_completion_tokens": endpoint.get("max_completion_tokens"),
                "supported_parameters": parameters,
            }
        )
    if not selected:
        raise RuntimeError("exact first-party DeepSeek endpoint is absent")
    if not any({"max_tokens", "temperature"}.issubset(set(item["supported_parameters"])) for item in selected):
        raise RuntimeError("first-party DeepSeek endpoint lacks judge wire parameters")
    metadata = {
        "schema_version": "narratordb.openrouter-model-endpoints.v1",
        "observed_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_url": URL,
        "http_status": status,
        "model_id": MODEL,
        "required_provider": PROVIDER,
        "matching_endpoints": selected,
        "exact_provider_present": True,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "credential_recorded": False,
        "model_content_recorded": False,
    }
    payload = (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode()
    _write(args.raw_output, raw)
    _write(args.metadata_output, payload)
    print(json.dumps({"ok": True, "matching_endpoints": len(selected), "metadata_sha256": hashlib.sha256(payload).hexdigest()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
