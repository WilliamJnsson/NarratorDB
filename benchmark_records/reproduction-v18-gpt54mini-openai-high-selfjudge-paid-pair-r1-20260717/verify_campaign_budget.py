#!/usr/bin/env python3
"""Admit the frozen V18 GPT-self-judged pair within fixed campaign ceilings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


PRIOR_V17 = Decimal("1.157744332")
CANARY_FUSE = Decimal("0.079484568")
ARM_FUSES = Decimal("4.90")
NEW_ALLOCATION = CANARY_FUSE + ARM_FUSES
TRACKED_CUMULATIVE = PRIOR_V17 + NEW_ALLOCATION
TRACKED_CEILING = Decimal("10.00")
PROVIDER_CAP = Decimal("250")
GOVERNANCE_CEILING_EUR = Decimal("300")
TOLERANCE = Decimal("0.000000001")


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
    parser.add_argument("--prior-pair-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    telemetry = _load(args.telemetry)
    fx = _load(args.fx)
    prior = _load(args.prior_pair_audit)
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
        prior.get("budget", {}).get("cumulative_conservative_campaign_exposure_usd"),
        label="prior V17 campaign exposure",
    )

    telemetry_capture = Path(__file__).with_name("capture_provider_telemetry.py")
    expected_capture_hash = _sha256(telemetry_capture)
    this_parser_hash = _sha256(Path(__file__))
    configuration = prior.get("configuration", {})
    fairness = prior.get("fairness", {})
    prior_arms = prior.get("arms", {})

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
        "telemetry_fresh_15m": (
            -120 <= (now - observed).total_seconds() <= 900
        ),
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
        "prior_v17_audit_complete": (
            prior.get("schema_version")
            == "narratordb.v17-gpt-selfjudge-paired-score.v1"
            and prior.get("status") == "complete-below-threshold"
            and prior.get("same_model_self_judge") is True
            and prior.get("target_passed") is False
        ),
        "prior_v17_configuration_exact": (
            configuration.get("answerer_model") == "openai/gpt-5.4-mini"
            and configuration.get("judge_model") == "openai/gpt-5.4-mini"
            and configuration.get("answerer_provider") == "OpenAI"
            and configuration.get("judge_provider") == "OpenAI"
            and configuration.get("answerer_reasoning_effort") == "high"
            and configuration.get("judge_reasoning_effort") == "high"
            and configuration.get("fallbacks_allowed") is False
            and configuration.get("replication_unconditional") is True
        ),
        "prior_v17_both_arms_complete": (
            set(prior_arms) == {"primary", "replication"}
            and all(
                arm.get("evaluation_audit_complete") is True
                and arm.get("official_score_complete") is True
                for arm in prior_arms.values()
            )
            and fairness.get("both_full_42_question_arms_completed") is True
        ),
        "prior_v17_exposure_exact": prior_exposure == PRIOR_V17,
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
        raise RuntimeError(f"V18 campaign admission failed: {checks}")

    document = {
        "schema_version": "narratordb.v18-gpt-selfjudge-campaign-admission.v1",
        "created_at_utc": now.replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "admitted": True,
        "checks": checks,
        "prior_v17_conservative_exposure_usd": str(PRIOR_V17),
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
        "prior_pair_audit_sha256": _sha256(args.prior_pair_audit),
        "credential_recorded": False,
        "model_content_recorded": False,
    }
    digest = _write_new(args.output, document)
    print(json.dumps({"ok": True, "sha256": digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
