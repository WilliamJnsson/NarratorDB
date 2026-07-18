#!/usr/bin/env python3
"""Admit the frozen R3 pair without querying provider account telemetry.

OpenAI's documented organization usage and cost endpoints require an admin key,
so this campaign uses the user's $30 balance attestation only as an admission
input.  Enforcement remains local, content-free, and independently auditable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


PRIOR_TRACKED_MAXIMUM = Decimal("2.878283432")
CANARY_FUSE = Decimal("0.611152")
ARM_FUSES = Decimal("4.90")
NEW_ALLOCATION = CANARY_FUSE + ARM_FUSES
TRACKED_CUMULATIVE = PRIOR_TRACKED_MAXIMUM + NEW_ALLOCATION
TRACKED_CEILING = Decimal("30.00")
ATTESTED_AVAILABLE_BALANCE = Decimal("30.00")
GOVERNANCE_CEILING_EUR = Decimal("300")
EXPECTED_R1_TERMINAL_SHA256 = (
    "4bdfe140a4f232b79a1e2b6121fa4a496b01ecd0d924a61a7c4e2468b0481eba"
)
EXPECTED_R2_TERMINAL_SHA256 = (
    "808d84f547fbd42587a2c1ac17e8b3fd8bf3853ec34221344bc58e94c0a14b9d"
)
EXPECTED_PRICING_SHA256 = (
    "41e6f74aab48e82f3854fff2c6a6425a4b7c13879dc3006674526d9190a41870"
)
EXPECTED_R2_DISCLOSURE_SHA256 = (
    "715698e49cbf421046063f5537642be955d3da6091f67a8d0674fe6631ce080c"
)
MODEL = "gpt-5.4-mini-2026-03-17"
FORMULA = (
    "((prompt_tokens - cached_tokens) * 0.75 + cached_tokens * 0.075 + "
    "completion_tokens * 4.50) / 1000000"
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
    parser.add_argument("--fx", type=Path, required=True)
    parser.add_argument("--pricing-evidence", type=Path, required=True)
    parser.add_argument("--prior-r1-terminal", type=Path, required=True)
    parser.add_argument("--prior-r2-terminal", type=Path, required=True)
    parser.add_argument("--r2-disclosure", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    fx = _load(args.fx)
    pricing = _load(args.pricing_evidence)
    r1 = _load(args.prior_r1_terminal)
    r2 = _load(args.prior_r2_terminal)
    r2_disclosure = _load(args.r2_disclosure)
    now = datetime.now(timezone.utc)
    fx_time = _timestamp(fx.get("retrieved_at_utc"), label="FX time")
    rate = _decimal(fx.get("usd_per_eur"), label="USD per EUR")
    r1_exposure = _decimal(
        r1.get("cost_and_budget", {}).get("cumulative_conservative_exposure_usd"),
        label="r1 cumulative conservative exposure",
    )
    r1_primary = r1.get("primary_score", {})
    r1_metrics = r1_primary.get("metrics", {})
    r1_execution = r1.get("execution_state", {})
    r1_replication = r1_execution.get("replication", {})
    r1_transport = r1.get("primary_transport", {})

    checks = {
        "pricing_evidence_hash_exact": (
            _sha256(args.pricing_evidence) == EXPECTED_PRICING_SHA256
        ),
        "pricing_evidence_exact": (
            pricing.get("schema_version")
            == "narratordb.openai-model-pricing-evidence.v1"
            and pricing.get("model_snapshot") == MODEL
            and pricing.get("input_usd_per_million_tokens") == "0.75"
            and pricing.get("cached_input_usd_per_million_tokens") == "0.075"
            and pricing.get("output_usd_per_million_tokens") == "4.50"
            and pricing.get("cost_formula") == FORMULA
            and pricing.get("completion_reasoning_accounting")
            == (
                "reasoning_tokens are a subset of completion_tokens and are not "
                "charged a second time"
            )
        ),
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
        "fx_parser_exact": fx.get("parser_sha256") == _sha256(Path(__file__)),
        "fx_fresh_24h": -120 <= (now - fx_time).total_seconds() <= 86400,
        "r1_terminal_hash_exact": (
            _sha256(args.prior_r1_terminal) == EXPECTED_R1_TERMINAL_SHA256
        ),
        "r1_terminal_status_exact": (
            r1.get("schema_version")
            == "narratordb.v18-gpt-selfjudge-paid-pair-aborted-after-primary-audit.v1"
            and r1.get("status") == "ABORTED_AFTER_PRIMARY_AUDIT"
            and r1.get("terminal_reason", {}).get(
                "score_values_used_to_decide_abort"
            )
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
        "r1_cumulative_exposure_exact": r1_exposure == Decimal("1.914605682"),
        "r2_terminal_hash_exact": (
            _sha256(args.prior_r2_terminal) == EXPECTED_R2_TERMINAL_SHA256
        ),
        "r2_terminal_status_exact": (
            r2.get("schema_version")
            == "narratordb.v18-gpt-selfjudge-r2-terminal-execution.v1"
            and r2.get("status") == "TERMINAL_INCOMPLETE"
            and r2.get("failed_phase") == "unconditional-replication"
            and r2.get("score_driven_branching") is False
            and r2.get("failure_decision_used_score_values") is False
            and r2.get("score_values_embedded_in_terminal_record") is False
        ),
        "r2_disclosure_hash_exact": (
            _sha256(args.r2_disclosure) == EXPECTED_R2_DISCLOSURE_SHA256
        ),
        "r2_disclosure_exact": (
            r2_disclosure.get("schema_version")
            == "narratordb.v18-r2-terminal-disclosure.v1"
            and r2_disclosure.get("status") == "TERMINAL_INCOMPLETE"
            and r2_disclosure.get("terminal_execution_status_sha256")
            == EXPECTED_R2_TERMINAL_SHA256
            and r2_disclosure.get("classification")
            == "score-exposed post-hoc consumed-development history; not a paired score"
            and r2_disclosure.get("provider_billing_reconciled") is False
            and r2_disclosure.get(
                "cumulative_campaign_conservative_exposure_usd_after_r2"
            )
            == str(PRIOR_TRACKED_MAXIMUM)
            and r2_disclosure.get("primary", {}).get("score_complete") is True
            and r2_disclosure.get("replication", {}).get("transport_score_complete")
            is False
            and r2_disclosure.get("replication", {}).get(
                "failure_decision_used_score_values"
            )
            is False
        ),
        "prior_tracked_maximum_exact": (
            PRIOR_TRACKED_MAXIMUM == Decimal("2.878283432")
        ),
        "new_allocation_within_attested_balance": (
            NEW_ALLOCATION <= ATTESTED_AVAILABLE_BALANCE
        ),
        "tracked_campaign_within_local_ceiling": (
            TRACKED_CUMULATIVE <= TRACKED_CEILING
        ),
        "new_allocation_below_300_eur": (
            NEW_ALLOCATION / rate <= GOVERNANCE_CEILING_EUR
        ),
        "tracked_campaign_below_300_eur": (
            TRACKED_CUMULATIVE / rate <= GOVERNANCE_CEILING_EUR
        ),
        "provider_balance_not_api_verified": True,
        "provider_admin_key_not_required": True,
    }
    if not all(checks.values()):
        raise RuntimeError(f"V18 R3 campaign admission failed: {checks}")

    document = {
        "schema_version": "narratordb.v18-gpt-selfjudge-campaign-admission.r3.v1",
        "created_at_utc": now.replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "admitted": True,
        "checks": checks,
        "model_snapshot": MODEL,
        "official_openai_endpoint": "https://api.openai.com/v1/chat/completions",
        "pricing_evidence_sha256": _sha256(args.pricing_evidence),
        "cost_formula": FORMULA,
        "reasoning_tokens_billed_twice": False,
        "prior_tracked_cumulative_maximum_usd": str(PRIOR_TRACKED_MAXIMUM),
        "new_canary_process_fuse_usd": str(CANARY_FUSE),
        "new_arm_process_fuses_usd": str(ARM_FUSES),
        "new_allocation_usd": str(NEW_ALLOCATION),
        "tracked_cumulative_maximum_usd": str(TRACKED_CUMULATIVE),
        "tracked_campaign_ceiling_usd": str(TRACKED_CEILING),
        "balance_attestation": {
            "available_usd": str(ATTESTED_AVAILABLE_BALANCE),
            "source": "user-provided",
            "verification": "not_api_verified",
            "provider_balance_endpoint_called": False,
            "organization_admin_key_required_or_used": False,
        },
        "usd_per_eur": str(rate),
        "new_allocation_eur": format(NEW_ALLOCATION / rate, ".8f"),
        "tracked_cumulative_maximum_eur": format(
            TRACKED_CUMULATIVE / rate, ".8f"
        ),
        "fx_sha256": _sha256(args.fx),
        "prior_r1_terminal_sha256": _sha256(args.prior_r1_terminal),
        "prior_r2_terminal_sha256": _sha256(args.prior_r2_terminal),
        "r2_terminal_disclosure_sha256": _sha256(args.r2_disclosure),
        "credential_recorded": False,
        "model_content_recorded": False,
        "provider_telemetry_performed": False,
    }
    digest = _write_new(args.output, document)
    print(json.dumps({"ok": True, "sha256": digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
