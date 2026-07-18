#!/usr/bin/env python3
"""Admit the frozen V18 r2 paid pair under the cumulative campaign caps."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


PRIOR_CUMULATIVE = Decimal("1.914605682")
CANARY_FUSE = Decimal("0.079484568")
ARM_FUSES = Decimal("4.90")
NEW_ALLOCATION = CANARY_FUSE + ARM_FUSES
TRACKED_CUMULATIVE = PRIOR_CUMULATIVE + NEW_ALLOCATION
TRACKED_CEILING = Decimal("10.00")
PROVIDER_CAP = Decimal("250")
GOVERNANCE_CEILING_EUR = Decimal("300")
TOLERANCE = Decimal("0.000000001")
EXPECTED_R1_TERMINAL_SHA256 = (
    "4bdfe140a4f232b79a1e2b6121fa4a496b01ecd0d924a61a7c4e2468b0481eba"
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite number is forbidden: {value}")


def _load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"input is missing or a symbolic link: {path}")
    parsed = json.loads(
        path.read_text(encoding="utf-8"),
        parse_float=Decimal,
        parse_constant=_reject_constant,
        object_pairs_hook=_unique_object,
    )
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object: {path}")
    return parsed


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


def _timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} lacks a timezone")
    return parsed


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_new(path: Path, document: dict[str, Any]) -> str:
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    if path.exists() or path.is_symlink():
        raise ValueError("admission output must start absent")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o444,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.fchmod(descriptor, 0o444)
    finally:
        os.close(descriptor)
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", type=Path, required=True)
    parser.add_argument("--fx", type=Path, required=True)
    parser.add_argument("--prior-r1-terminal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    telemetry = _load(args.telemetry)
    fx = _load(args.fx)
    prior = _load(args.prior_r1_terminal)
    now = datetime.now(timezone.utc)
    observed = _timestamp(telemetry.get("observed_at_utc"), label="telemetry time")
    fx_time = _timestamp(fx.get("retrieved_at_utc"), label="FX time")

    limit = _decimal(telemetry.get("provider_limit_usd"), label="provider limit")
    usage = _decimal(telemetry.get("provider_usage_usd"), label="provider usage")
    remaining = _decimal(
        telemetry.get("provider_remaining_usd"), label="provider remaining"
    )
    rate = _decimal(fx.get("usd_per_eur"), label="USD per EUR")
    prior_exposure = _decimal(
        prior.get("cost_and_budget", {}).get(
            "cumulative_conservative_exposure_usd"
        ),
        label="terminal r1 cumulative exposure",
    )

    telemetry_capture = Path(__file__).with_name("capture_provider_telemetry.py")
    expected_capture_hash = _sha256(telemetry_capture)
    this_parser_hash = _sha256(Path(__file__))
    r1_primary = prior.get("primary_score", {})
    r1_metrics = r1_primary.get("metrics", {})
    r1_execution = prior.get("execution_state", {})
    r1_replication = r1_execution.get("replication", {})
    r1_transport = prior.get("primary_transport", {})

    checks = {
        "telemetry_schema_exact": (
            telemetry.get("schema_version")
            == "narratordb.provider-key-telemetry.v2"
        ),
        "telemetry_content_free": (
            telemetry.get("credential_recorded") is False
            and telemetry.get("key_label_recorded") is False
            and telemetry.get("account_identifier_recorded") is False
            and telemetry.get("model_content_recorded") is False
        ),
        "telemetry_capture_tool_exact": (
            telemetry.get("capture_tool_sha256") == expected_capture_hash
        ),
        "telemetry_fresh_15m": -120 <= (now - observed).total_seconds() <= 900,
        "telemetry_arithmetic_exact": abs(limit - usage - remaining) <= TOLERANCE,
        "fx_schema_exact": (
            fx.get("schema_version") == "narratordb.ecb-usd-eur-observation.v1"
        ),
        "fx_source_exact": (
            fx.get("publisher") == "European Central Bank"
            and fx.get("base_currency") == "EUR"
            and fx.get("quote_currency") == "USD"
            and fx.get("credential_recorded") is False
            and fx.get("model_content_recorded") is False
        ),
        "fx_parser_exact": fx.get("parser_sha256") == this_parser_hash,
        "fx_fresh_24h": -120 <= (now - fx_time).total_seconds() <= 86400,
        "r1_terminal_hash_exact": (
            _sha256(args.prior_r1_terminal) == EXPECTED_R1_TERMINAL_SHA256
        ),
        "r1_terminal_status_exact": (
            prior.get("schema_version")
            == "narratordb.v18-gpt-selfjudge-paid-pair-aborted-after-primary-audit.v1"
            and prior.get("status") == "ABORTED_AFTER_PRIMARY_AUDIT"
            and prior.get("terminal_reason", {}).get("score_values_used_to_decide_abort")
            is False
        ),
        "r1_score_exposure_disclosed": (
            r1_primary.get("official_harness_score_complete") is True
            and r1_metrics.get("top_20", {}).get("correct") == 40
            and r1_metrics.get("top_20", {}).get("total") == 42
            and r1_metrics.get("top_50", {}).get("correct") == 41
            and r1_metrics.get("top_50", {}).get("total") == 42
        ),
        "r1_transport_terminal_exact": (
            r1_transport.get("invalid_completion_identities") == 1
            and r1_transport.get("unknown_cost_attempts") == 1
            and r1_transport.get("publication_ready") is False
            and r1_replication.get("execution_started") is False
            and r1_replication.get("provider_role_calls") == 0
        ),
        "r1_cumulative_exposure_exact": prior_exposure == PRIOR_CUMULATIVE,
        "tracked_selfjudge_campaign_below_10_usd": (
            TRACKED_CUMULATIVE <= TRACKED_CEILING
        ),
        "new_pair_allocation_below_5_usd": NEW_ALLOCATION < Decimal("5.00"),
        "provider_cap_at_most_250_usd": limit <= PROVIDER_CAP,
        "provider_remaining_covers_new_allocation": remaining >= NEW_ALLOCATION,
        "provider_usage_plus_allocation_within_cap": (
            usage + NEW_ALLOCATION <= limit + TOLERANCE
        ),
        "provider_cap_below_300_eur": (
            limit / rate <= GOVERNANCE_CEILING_EUR
        ),
        "tracked_campaign_below_300_eur": (
            TRACKED_CUMULATIVE / rate <= GOVERNANCE_CEILING_EUR
        ),
    }
    if not all(checks.values()):
        raise RuntimeError(f"V18 r2 campaign admission failed: {checks}")

    document = {
        "schema_version": "narratordb.v18-gpt-selfjudge-campaign-admission.r2.v1",
        "created_at_utc": now.replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "admitted": True,
        "checks": checks,
        "prior_r1_cumulative_conservative_exposure_usd": str(PRIOR_CUMULATIVE),
        "new_canary_process_fuse_usd": str(CANARY_FUSE),
        "new_arm_process_fuses_usd": str(ARM_FUSES),
        "new_allocation_usd": str(NEW_ALLOCATION),
        "tracked_cumulative_maximum_usd": str(TRACKED_CUMULATIVE),
        "tracked_selfjudge_campaign_ceiling_usd": str(TRACKED_CEILING),
        "provider_limit_usd": str(limit),
        "provider_usage_usd": str(usage),
        "provider_remaining_usd": str(remaining),
        "usd_per_eur": str(rate),
        "provider_cap_eur": format(limit / rate, ".8f"),
        "tracked_cumulative_maximum_eur": format(
            TRACKED_CUMULATIVE / rate, ".8f"
        ),
        "telemetry_sha256": _sha256(args.telemetry),
        "fx_sha256": _sha256(args.fx),
        "prior_r1_terminal_sha256": _sha256(args.prior_r1_terminal),
        "credential_recorded": False,
        "model_content_recorded": False,
    }
    digest = _write_new(args.output, document)
    print(json.dumps({"ok": True, "sha256": digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
