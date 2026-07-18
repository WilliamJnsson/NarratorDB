#!/usr/bin/env python3
"""Capture exact requested endpoint metadata with a dated-slug alias guard."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

REQUESTED = "deepseek/deepseek-v4-flash-20260423"
CANONICAL = re.sub(r"-[0-9]{8}$", "", REQUESTED)
PROVIDER = "DeepSeek"
URL = f"https://openrouter.ai/api/v1/models/{REQUESTED}/endpoints"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _write(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("endpoint output must start absent")
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
    returned = data.get("id") if isinstance(data, dict) else None
    if returned != CANONICAL or not isinstance(endpoints, list):
        raise RuntimeError("dated-slug canonical alias or endpoint schema mismatch")
    selected = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or endpoint.get("provider_name") != PROVIDER:
            continue
        if endpoint.get("model_id") != CANONICAL:
            raise RuntimeError("first-party endpoint model mismatch")
        parameters = endpoint.get("supported_parameters")
        if not isinstance(parameters, list) or not {"max_tokens", "temperature"}.issubset(set(parameters)):
            raise RuntimeError("first-party endpoint lacks judge parameters")
        selected.append({key: endpoint.get(key) for key in ("provider_name", "tag", "status", "model_id", "quantization", "context_length", "max_completion_tokens", "supported_parameters", "uptime_last_5m", "uptime_last_30m")})
    if not selected:
        raise RuntimeError("exact first-party DeepSeek provider is absent")
    metadata = {
        "schema_version": "narratordb.openrouter-model-endpoints.v2",
        "observed_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_url": URL,
        "http_status": status,
        "requested_model_id": REQUESTED,
        "returned_canonical_model_id": returned,
        "accepted_alias_rule": "remove exactly one terminal hyphen plus eight decimal date digits",
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
