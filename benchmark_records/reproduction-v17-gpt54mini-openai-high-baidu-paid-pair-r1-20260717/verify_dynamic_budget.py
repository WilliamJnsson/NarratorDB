#!/usr/bin/env python3
"""Verify fresh provider/ECB evidence against the fixed V17 budget envelope."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_new(path: Path, value: dict) -> str:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite admission evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", type=Path, required=True)
    parser.add_argument("--fx", type=Path, required=True)
    parser.add_argument("--required-remaining-usd", type=Decimal, required=True)
    parser.add_argument("--allocated-new-work-usd", type=Decimal, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    telemetry = _read(args.telemetry)
    fx = _read(args.fx)
    now = datetime.now(timezone.utc)
    observed = datetime.fromisoformat(str(telemetry["observed_at_utc"]).replace("Z", "+00:00"))
    retrieved = datetime.fromisoformat(str(fx["retrieved_at_utc"]).replace("Z", "+00:00"))
    telemetry_age = Decimal(str((now - observed).total_seconds()))
    fx_age = Decimal(str((now - retrieved).total_seconds()))
    limit = Decimal(str(telemetry["provider_limit_usd"]))
    remaining = Decimal(str(telemetry["provider_remaining_usd"]))
    usd_per_eur = Decimal(str(fx["usd_per_eur"]))
    checks = {
        "telemetry_at_most_15_minutes_old": Decimal("-120") <= telemetry_age <= Decimal("900"),
        "fx_capture_at_most_24_hours_old": Decimal("-120") <= fx_age <= Decimal("86400"),
        "provider_account_cap_at_most_250_usd": limit <= Decimal("250"),
        "provider_remaining_covers_fixed_allocation": remaining >= args.required_remaining_usd,
        "provider_cap_below_300_eur_governance": limit / usd_per_eur <= Decimal("300"),
        "new_work_below_300_eur_governance": args.allocated_new_work_usd / usd_per_eur <= Decimal("300"),
        "exact_global_allocation_at_most_5_usd": args.allocated_new_work_usd <= Decimal("5"),
    }
    if not all(checks.values()):
        raise RuntimeError(f"dynamic budget admission failed: {checks}")
    document = {
        "schema_version": "narratordb.v17-dynamic-budget-admission.v1",
        "created_at_utc": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "admitted": True,
        "checks": checks,
        "provider_limit_usd": format(limit, "f"),
        "provider_remaining_usd": format(remaining, "f"),
        "required_remaining_usd": format(args.required_remaining_usd, "f"),
        "allocated_new_work_usd": format(args.allocated_new_work_usd, "f"),
        "usd_per_eur": format(usd_per_eur, "f"),
        "allocated_new_work_eur": format(args.allocated_new_work_usd / usd_per_eur, ".8f"),
        "telemetry_sha256": hashlib.sha256(args.telemetry.read_bytes()).hexdigest(),
        "fx_metadata_sha256": hashlib.sha256(args.fx.read_bytes()).hexdigest(),
    }
    digest = _write_new(args.output, document)
    print(json.dumps({"ok": True, "admitted": True, "sha256": digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
