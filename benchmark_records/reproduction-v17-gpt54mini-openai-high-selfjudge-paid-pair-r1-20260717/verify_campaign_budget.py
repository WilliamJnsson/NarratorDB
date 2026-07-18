#!/usr/bin/env python3
"""Admit the GPT-self-judged pair within the original five-dollar envelope."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

BAIDU = Decimal("0.015344432")
DEEPSEEK = Decimal("0.005171")
PRIOR = BAIDU + DEEPSEEK
CANARY = Decimal("0.079484568")
ARMS = Decimal("4.90")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", type=Path, required=True)
    parser.add_argument("--fx", type=Path, required=True)
    parser.add_argument("--baidu-audit", type=Path, required=True)
    parser.add_argument("--deepseek-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    telemetry = json.loads(args.telemetry.read_text())
    fx = json.loads(args.fx.read_text())
    baidu = json.loads(args.baidu_audit.read_text())
    deepseek = json.loads(args.deepseek_audit.read_text())
    if (
        baidu.get("status") != "abandoned-before-benchmark-scoring"
        or baidu.get("budget_audit", {}).get(
            "conservative_total_canary_cost_or_reservation_usd"
        )
        != str(BAIDU)
        or baidu.get("benchmark_model_calls_made") is not False
    ):
        raise RuntimeError("Baidu conservative exposure audit mismatch")
    if (
        deepseek.get("status") != "abandoned-before-benchmark-scoring"
        or deepseek.get("budget_audit", {}).get(
            "deepseek_route_conservative_exposure_usd"
        )
        != str(DEEPSEEK)
        or deepseek.get("benchmark_model_call_count") != 0
    ):
        raise RuntimeError("DeepSeek conservative exposure audit mismatch")
    now = datetime.now(timezone.utc)
    observed = datetime.fromisoformat(telemetry["observed_at_utc"].replace("Z", "+00:00"))
    fx_time = datetime.fromisoformat(fx["retrieved_at_utc"].replace("Z", "+00:00"))
    limit = Decimal(telemetry["provider_limit_usd"])
    remaining = Decimal(telemetry["provider_remaining_usd"])
    rate = Decimal(fx["usd_per_eur"])
    new = CANARY + ARMS
    combined = PRIOR + new
    checks = {
        "telemetry_fresh_15m": -120 <= (now - observed).total_seconds() <= 900,
        "fx_fresh_24h": -120 <= (now - fx_time).total_seconds() <= 86400,
        "provider_cap_at_most_250_usd": limit <= Decimal("250"),
        "remaining_covers_new_allocation": remaining >= new,
        "both_prior_attempts_are_no_score": True,
        "prior_plus_new_equals_5_usd": combined == Decimal("5"),
        "provider_cap_below_300_eur": limit / rate <= Decimal("300"),
        "campaign_below_300_eur": combined / rate <= Decimal("300"),
    }
    if not all(checks.values()):
        raise RuntimeError(f"campaign budget admission failed: {checks}")
    document = {
        "schema_version": "narratordb.v17-gpt-selfjudge-campaign-admission.v1",
        "created_at_utc": now.replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "admitted": True,
        "checks": checks,
        "prior_baidu_conservative_exposure_usd": str(BAIDU),
        "prior_deepseek_conservative_exposure_usd": str(DEEPSEEK),
        "cumulative_prior_conservative_exposure_usd": str(PRIOR),
        "new_canary_fuse_usd": str(CANARY),
        "new_arm_fuses_usd": str(ARMS),
        "new_allocation_usd": str(new),
        "combined_campaign_ceiling_usd": str(combined),
        "provider_remaining_usd": str(remaining),
        "usd_per_eur": str(rate),
        "combined_campaign_eur": format(combined / rate, ".8f"),
        "telemetry_sha256": hashlib.sha256(args.telemetry.read_bytes()).hexdigest(),
        "fx_sha256": hashlib.sha256(args.fx.read_bytes()).hexdigest(),
        "baidu_audit_sha256": hashlib.sha256(args.baidu_audit.read_bytes()).hexdigest(),
        "deepseek_audit_sha256": hashlib.sha256(
            args.deepseek_audit.read_bytes()
        ).hexdigest(),
    }
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    if args.output.exists() or args.output.is_symlink():
        raise RuntimeError("admission output must start absent")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    print(json.dumps({"ok": True, "sha256": hashlib.sha256(payload).hexdigest()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
