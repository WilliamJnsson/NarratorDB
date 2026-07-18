"""Production-shaped, content-safe canary for Intelligence compilation.

The canary uses synthetic conversations plus a fresh database and compiler
cache. OpenRouter runs use an explicit USD fuse; ChatGPT-authenticated Codex
CLI runs use an explicit invocation fuse instead. This module has no credential
argument and never places credentials, prompts, claims, recall text, or CLI
output in its report.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..compiler import (
    CODEX_CLI_PROVIDER,
    COMPILER_PROMPT_VERSION,
    CompilerError,
    ContentFreeUsageLedger,
    MemoryCompiler,
    compiler_from_project_config,
)
from ..compiler_cache import CachedMemoryCompiler, CompiledSessionCache
from ..config import (
    DEFAULT_CODEX_CLI_MODEL,
    DEFAULT_OUTPUT_TOKEN_PARAMETER,
    SUPPORTED_OUTPUT_TOKEN_PARAMETERS,
    CompilerConfig,
    CompilerKind,
    normalize_compiler_kind,
    normalize_output_token_parameter,
)
from ..engine import Engine
from ..enrichment import EnrichmentRunner, build_compile_input


REPORT_SCHEMA_VERSION = "narratordb.intelligence-canary.v5"
CANARY_USER_ID = "narratordb-intelligence-canary"
CANARY_REQUEST_RESERVATION_USD = 0.05
CANARY_SAFETY_RESERVE_USD = 0.01
CANARY_SEMANTIC_CALLS = 3

CompilerFactory = Callable[[CompilerConfig, ContentFreeUsageLedger], MemoryCompiler]
Sleep = Callable[[float], None]
Monotonic = Callable[[], float]


@dataclass(frozen=True, slots=True)
class IntelligenceCanaryConfig:
    """Credential-free, fully disclosed canary configuration."""

    model: str | None = None
    provider: str = ""
    reasoning: str | None = None
    max_output_tokens: int | None = None
    max_cost_usd: float | None = None
    compiler: CompilerKind = CompilerKind.OPENROUTER
    output_token_parameter: str = DEFAULT_OUTPUT_TOKEN_PARAMETER
    provider_allowlist: tuple[str, ...] = ()
    allow_fallbacks: bool = False
    transport_max_attempts: int = 1
    semantic_max_attempts: int | None = None
    retry_delay_seconds: float | None = None
    min_request_interval_seconds: float | None = None
    capture_router_metadata: bool = False
    codex_cli_version: str | None = None
    codex_timeout_seconds: float = 300.0
    codex_max_invocations: int | None = None
    codex_max_concurrency: int = 1

    def __post_init__(self) -> None:
        compiler = normalize_compiler_kind(self.compiler)
        object.__setattr__(self, "compiler", compiler)
        model = self.model.strip() if isinstance(self.model, str) else ""
        reasoning = self.reasoning.strip() if isinstance(self.reasoning, str) else ""
        if compiler is CompilerKind.CODEX_CLI:
            model = model or DEFAULT_CODEX_CLI_MODEL
            reasoning = reasoning or "low"
        if not model:
            raise ValueError("model must be a non-empty string")
        if not reasoning:
            raise ValueError("reasoning must be a non-empty string")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "reasoning", reasoning)

        provider = self.provider.strip() if isinstance(self.provider, str) else ""
        provider_allowlist = tuple(
            item.strip()
            for item in self.provider_allowlist
            if isinstance(item, str) and item.strip()
        )
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "provider_allowlist", provider_allowlist)
        if len({item.casefold() for item in provider_allowlist}) != len(
            provider_allowlist
        ):
            raise ValueError("provider_allowlist entries must be unique")

        max_output_tokens = self.max_output_tokens or 8192
        object.__setattr__(self, "max_output_tokens", max_output_tokens)
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if self.transport_max_attempts != 1:
            raise ValueError("the frozen canary requires transport_max_attempts=1")

        semantic_max_attempts = self.semantic_max_attempts
        if semantic_max_attempts is None:
            semantic_max_attempts = 2 if compiler is CompilerKind.CODEX_CLI else 1
        if semantic_max_attempts < 1:
            raise ValueError("semantic_max_attempts must be positive")
        if compiler is CompilerKind.OPENROUTER and semantic_max_attempts != 1:
            raise ValueError(
                "the frozen OpenRouter canary requires semantic_max_attempts=1"
            )
        object.__setattr__(self, "semantic_max_attempts", semantic_max_attempts)

        retry_delay_seconds = self.retry_delay_seconds
        if retry_delay_seconds is None and compiler is CompilerKind.CODEX_CLI:
            retry_delay_seconds = 0.25
        object.__setattr__(self, "retry_delay_seconds", retry_delay_seconds)
        if self.retry_delay_seconds is not None and (
            not math.isfinite(self.retry_delay_seconds) or self.retry_delay_seconds < 0
        ):
            raise ValueError("retry_delay_seconds must be non-negative and finite")

        min_request_interval_seconds = self.min_request_interval_seconds
        if min_request_interval_seconds is None:
            min_request_interval_seconds = (
                0.0 if compiler is CompilerKind.CODEX_CLI else 35.0
            )
        object.__setattr__(
            self, "min_request_interval_seconds", min_request_interval_seconds
        )
        if (
            not math.isfinite(self.min_request_interval_seconds)
            or self.min_request_interval_seconds < 0
        ):
            raise ValueError(
                "min_request_interval_seconds must be non-negative and finite"
            )
        object.__setattr__(
            self,
            "output_token_parameter",
            normalize_output_token_parameter(self.output_token_parameter),
        )

        if compiler is CompilerKind.OPENROUTER:
            if provider and provider_allowlist:
                raise ValueError("configure either a provider or provider allowlist")
            if not provider and not provider_allowlist:
                raise ValueError("provider or provider_allowlist is required")
            if self.allow_fallbacks and not provider_allowlist:
                raise ValueError("fallbacks require a provider allowlist")
            if self.max_cost_usd is None or (
                not math.isfinite(self.max_cost_usd) or self.max_cost_usd <= 0
            ):
                raise ValueError("max_cost_usd must be a positive finite number")
            if self.max_cost_usd < (
                CANARY_REQUEST_RESERVATION_USD + CANARY_SAFETY_RESERVE_USD
            ):
                raise ValueError(
                    "max_cost_usd is below the canary request reservation and safety reserve"
                )
            if any(
                (
                    self.codex_cli_version is not None,
                    self.codex_timeout_seconds != 300.0,
                    self.codex_max_invocations is not None,
                    self.codex_max_concurrency != 1,
                )
            ):
                raise ValueError("Codex CLI options require compiler='codex-cli'")
            return

        if compiler is not CompilerKind.CODEX_CLI:
            raise ValueError("the canary supports only openrouter or codex-cli")
        if provider or provider_allowlist:
            raise ValueError("Codex CLI does not accept provider routes")
        if self.allow_fallbacks or self.capture_router_metadata:
            raise ValueError("OpenRouter route options are invalid for Codex CLI")
        if self.max_cost_usd is not None:
            raise ValueError("Codex CLI subscription runs do not accept max_cost_usd")
        if self.output_token_parameter != DEFAULT_OUTPUT_TOKEN_PARAMETER:
            raise ValueError("Codex CLI does not accept output token parameters")
        if max_output_tokens != 8192:
            raise ValueError("Codex CLI requires max_output_tokens=8192")
        if (
            not math.isfinite(self.codex_timeout_seconds)
            or self.codex_timeout_seconds <= 0
        ):
            raise ValueError("codex_timeout_seconds must be positive and finite")
        for name, value in (
            ("codex_max_invocations", self.codex_max_invocations),
            ("codex_max_concurrency", self.codex_max_concurrency),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"{name} must be positive")

        # Delegate version/reasoning validation to the durable public config API.
        self.project_compiler_config()

    @property
    def codex_invocation_limit(self) -> int | None:
        if self.compiler is not CompilerKind.CODEX_CLI:
            return None
        return self.codex_max_invocations or (
            CANARY_SEMANTIC_CALLS * int(self.semantic_max_attempts)
        )

    def project_compiler_config(self) -> CompilerConfig:
        if self.compiler is CompilerKind.CODEX_CLI:
            return CompilerConfig.codex_cli(
                model=str(self.model),
                reasoning=str(self.reasoning),
                cli_version=self.codex_cli_version,
                timeout_seconds=self.codex_timeout_seconds,
                max_invocations=self.codex_invocation_limit,
                max_concurrency=self.codex_max_concurrency,
                semantic_max_attempts=int(self.semantic_max_attempts),
                retry_delay_seconds=float(self.retry_delay_seconds or 0.0),
                min_request_interval_seconds=float(self.min_request_interval_seconds),
            )
        return CompilerConfig.openrouter(
            model=str(self.model),
            provider=self.provider or None,
            provider_allowlist=self.provider_allowlist,
            allow_fallbacks=self.allow_fallbacks,
            reasoning=str(self.reasoning),
            max_output_tokens=int(self.max_output_tokens),
            output_token_parameter=self.output_token_parameter,
            transport_max_attempts=self.transport_max_attempts,
            semantic_max_attempts=self.semantic_max_attempts,
            retry_delay_seconds=self.retry_delay_seconds,
            min_request_interval_seconds=self.min_request_interval_seconds,
            capture_router_metadata=self.capture_router_metadata,
        )


def _synthetic_sessions() -> tuple[dict[str, Any], ...]:
    numbered_items = [
        (
            "19. Clockwork compass with teal dial"
            if number == 19
            else f"{number}. Fictional expedition field-kit part {number}"
        )
        for number in range(1, 34)
    ]
    return (
        {
            "session_id": "canary-foundation",
            "occurred_at": 1_735_689_600.0,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "For our fictional rover simulator, my preferred control-panel "
                        "accent is apricot. Please also recommend a fictional field "
                        "note about pressure-breathing drills."
                    ),
                },
                {
                    "role": "assistant",
                    "content": (
                        "Use the LanternBreath field note: "
                        "https://lanternbreath.example/guides/first-signal"
                    ),
                },
            ],
        },
        {
            "session_id": "canary-numbered-list",
            "occurred_at": 1_735_776_000.0,
            "messages": [
                {
                    "role": "user",
                    "content": "Save this fictional expedition field-kit checklist.",
                },
                {
                    "role": "assistant",
                    "content": "Fictional expedition field-kit checklist:\n"
                    + "\n".join(numbered_items),
                },
            ],
        },
        {
            "session_id": "canary-preference-update",
            "occurred_at": 1_735_862_400.0,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "For the fictional rover simulator, I changed my preferred "
                        "control-panel accent from apricot to deep violet."
                    ),
                },
                {
                    "role": "assistant",
                    "content": (
                        "Understood; the current fictional control-panel accent is "
                        "deep violet."
                    ),
                },
            ],
        },
    )


def _default_compiler_factory(
    config: CompilerConfig,
    usage_ledger: ContentFreeUsageLedger,
) -> MemoryCompiler:
    return compiler_from_project_config(config, usage_sink=usage_ledger)


def _round_ms(value: float) -> float:
    return round(max(0.0, float(value)), 3)


def _provider_identity(value: str) -> str:
    base = str(value).split("/", 1)[0]
    return "".join(character for character in base.casefold() if character.isalnum())


def _usage_event_count(usage_ledger: ContentFreeUsageLedger) -> int:
    return int(usage_ledger.summary().get("attempts") or 0)


def _safe_failure(error: BaseException) -> dict[str, str]:
    """Return stable metadata without serializing exception or source content."""

    return {
        "type": type(error).__name__,
        "code": (
            str(error.code)
            if isinstance(error, CompilerError)
            else "canary_runtime_error"
        ),
    }


def _base_report(config: IntelligenceCanaryConfig) -> dict[str, Any]:
    codex_cli = config.compiler is CompilerKind.CODEX_CLI
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "narratordb_version": __version__,
        "status": "failed",
        "compiler": {
            "kind": config.compiler.value,
            "model": config.model,
            "provider": config.provider or None,
            "provider_allowlist": list(config.provider_allowlist),
            "allow_fallbacks": config.allow_fallbacks,
            "reasoning": config.reasoning,
            "max_output_tokens": config.max_output_tokens,
            "output_token_parameter": config.output_token_parameter,
            "fingerprint": None,
            "prompt_version": COMPILER_PROMPT_VERSION,
            "transport_max_attempts": config.transport_max_attempts,
            "semantic_max_attempts": config.semantic_max_attempts,
            "no_client_retry": True,
            "retry_delay_seconds": config.retry_delay_seconds,
            "min_request_interval_seconds": config.min_request_interval_seconds,
            "capture_router_metadata": config.capture_router_metadata,
            "codex_cli_version": config.codex_cli_version if codex_cli else None,
            "codex_timeout_seconds": (
                config.codex_timeout_seconds if codex_cli else None
            ),
            "codex_max_invocations": (
                config.codex_invocation_limit if codex_cli else None
            ),
            "codex_max_concurrency": (
                config.codex_max_concurrency if codex_cli else None
            ),
        },
        "cost_fuse": {
            "applicable": not codex_cli,
            "currency": None if codex_cli else "USD",
            "max_cost": None if codex_cli else config.max_cost_usd,
            "request_reservation": (
                0.0 if codex_cli else CANARY_REQUEST_RESERVATION_USD
            ),
            "safety_reserve": 0.0 if codex_cli else CANARY_SAFETY_RESERVE_USD,
            "scope": (
                "not_applicable_chatgpt_subscription" if codex_cli else "single_process"
            ),
        },
        "invocation_fuse": {
            "applicable": codex_cli,
            "expected_semantic_calls": CANARY_SEMANTIC_CALLS,
            "max_invocations": config.codex_invocation_limit,
            "observed_invocations": 0,
            "scope": "single_process" if codex_cli else "not_applicable",
        },
        "subscription_route": {
            "applicable": codex_cli,
            "transport": "codex-cli-chatgpt" if codex_cli else None,
            "authentication": "chatgpt-subscription" if codex_cli else None,
            "provider_identity": CODEX_CLI_PROVIDER if codex_cli else None,
            "provider_api_cost_required": False if codex_cli else None,
            "provider_billing_reconciled": False if codex_cli else None,
            "subscription_use_is_not_claimed_as_free": codex_cli,
        },
        "state": {
            "fresh_database": True,
            "fresh_compiler_cache": True,
            "synthetic_sessions": 3,
        },
        "pipeline": {
            "query_free_finalization": True,
            "finalization_attempts": 0,
            "finalized_sessions": 0,
            "complete_jobs": 0,
            "failed_jobs": 0,
            "enrichment_error_codes": [],
            "update_reference_claims": 0,
            "update_matching_reference_present": False,
            "recall_upstream_requests": None,
            "transport_failure": None,
        },
        "timings_ms": {
            "ingestion": 0.0,
            "enrichment": 0.0,
            "local_recall": 0.0,
            "total": 0.0,
        },
        "usage": {},
        "route_observations": {
            "models": [],
            "providers": [],
            "attempts": [],
            "attempt_metadata_available": False,
        },
        "checks": [],
    }


def _run_local_checks(
    engine: Engine,
    *,
    usage_ledger: ContentFreeUsageLedger,
    update_reference_claims: int,
    update_matching_reference_present: bool,
    job_checks_ok: bool,
) -> tuple[list[dict[str, Any]], float, int]:
    before_events = _usage_event_count(usage_ledger)
    started = time.perf_counter()

    resource_rows = engine._conn.execute(
        """
        SELECT c.text, c.status, cs.quote, m.speaker
        FROM memory_claims c
        JOIN memory_sessions s ON s.id = c.session_id
        JOIN memory_claim_sources cs ON cs.claim_id = c.id
        JOIN messages m ON m.id = cs.message_id
        WHERE c.user_id = ? AND s.external_id = 'canary-foundation'
        """,
        (engine.user_id,),
    ).fetchall()
    resource_rows = [
        row
        for row in resource_rows
        if str(row[1]) == "active"
        and "lanternbreath" in str(row[0]).casefold()
        and "field note" in str(row[0]).casefold()
    ]
    assistant_resource_retained = bool(resource_rows)
    assistant_resource_grounded = any(
        str(row[3]).casefold() == "assistant"
        and "https://lanternbreath.example/guides/first-signal"
        in str(row[2]).casefold()
        for row in resource_rows
    )
    resource_recall = engine.search_memory_blocks(
        "What was the LanternBreath website link?",
        limit=12,
        include_derived=True,
        max_chars=220,
    )
    assistant_resource_url_recalled = any(
        "https://lanternbreath.example/guides/first-signal" in block.text.casefold()
        for block in resource_recall.blocks
    )

    numbered = engine.search_memory_blocks(
        "Which entry was 19th on the fictional expedition field-kit checklist?",
        limit=12,
        include_derived=True,
        max_chars=220,
    )
    numbered_item_recalled = any(
        block.kind == "raw_message"
        and "19. clockwork compass with teal dial" in block.text.casefold()
        for block in numbered.blocks
    )

    preference_rows = engine._conn.execute(
        """
        SELECT c.status, c.memory_key, s.external_id,
               MAX(CASE WHEN LOWER(cs.quote) LIKE '%apricot%' THEN 1 ELSE 0 END),
               MAX(CASE WHEN LOWER(cs.quote) LIKE '%deep violet%' THEN 1 ELSE 0 END)
        FROM memory_claims c
        JOIN memory_sessions s ON s.id = c.session_id
        JOIN memory_claim_sources cs ON cs.claim_id = c.id
        WHERE c.user_id = ?
          AND s.external_id IN ('canary-foundation', 'canary-preference-update')
        GROUP BY c.id
        """,
        (engine.user_id,),
    ).fetchall()
    old_preferences = [
        row
        for row in preference_rows
        if str(row[2]) == "canary-foundation"
        and int(row[3]) == 1
        and str(row[1]).strip()
    ]
    new_preferences = [
        row
        for row in preference_rows
        if str(row[2]) == "canary-preference-update"
        and int(row[4]) == 1
        and str(row[1]).strip()
    ]
    stable_key_update = any(
        str(old[1]) == str(new[1])
        and str(old[0]) == "superseded"
        and str(new[0]) == "active"
        for old in old_preferences
        for new in new_preferences
    )

    ungrounded_claims = int(
        engine._conn.execute(
            """
            SELECT COUNT(*)
            FROM memory_claims c
            WHERE c.user_id = ? AND NOT EXISTS (
                SELECT 1 FROM memory_claim_sources cs WHERE cs.claim_id = c.id
            )
            """,
            (engine.user_id,),
        ).fetchone()[0]
    )
    health = engine.health_check()
    local_recall_ms = (time.perf_counter() - started) * 1000
    after_events = _usage_event_count(usage_ledger)
    recall_upstream_requests = after_events - before_events

    checks = [
        {"name": "all_enrichment_jobs_complete", "ok": job_checks_ok},
        {
            "name": "assistant_resource_claim_retained",
            "ok": assistant_resource_retained,
        },
        {
            "name": "assistant_resource_claim_grounded",
            "ok": assistant_resource_grounded,
        },
        {
            "name": "assistant_resource_url_locally_recalled",
            "ok": assistant_resource_url_recalled,
        },
        {"name": "numbered_item_raw_excerpt_recalled", "ok": numbered_item_recalled},
        {
            "name": "update_received_reference_context",
            "ok": update_reference_claims > 0,
        },
        {
            "name": "update_received_matching_prior_slot",
            "ok": update_matching_reference_present,
        },
        {"name": "stable_memory_key_superseded_prior_value", "ok": stable_key_update},
        {"name": "all_claims_have_source_evidence", "ok": ungrounded_claims == 0},
        {"name": "database_and_indexes_healthy", "ok": bool(health.get("ok"))},
        {
            "name": "recall_made_no_upstream_requests",
            "ok": recall_upstream_requests == 0,
        },
    ]
    return checks, local_recall_ms, recall_upstream_requests


def run_intelligence_canary(
    config: IntelligenceCanaryConfig,
    *,
    compiler_factory: CompilerFactory | None = None,
    sleep: Sleep = time.sleep,
    monotonic: Monotonic = time.monotonic,
) -> dict[str, Any]:
    """Run the isolated canary and return a content-safe JSON-compatible report."""

    if not isinstance(config, IntelligenceCanaryConfig):
        raise TypeError("config must be an IntelligenceCanaryConfig")
    report = _base_report(config)
    total_started = time.perf_counter()
    ingestion_ms = 0.0
    enrichment_ms = 0.0
    factory = compiler_factory or _default_compiler_factory
    usage_ledger: ContentFreeUsageLedger | None = None

    try:
        with tempfile.TemporaryDirectory(
            prefix="narratordb-intelligence-canary-"
        ) as root:
            root_path = Path(root)
            database_path = root_path / "canary.sqlite3"
            cache_path = root_path / "compiler-cache.sqlite3"
            ledger_path = root_path / "compiler-usage.jsonl"
            if database_path.exists() or cache_path.exists() or ledger_path.exists():
                raise RuntimeError("canary state was not fresh")

            codex_cli = config.compiler is CompilerKind.CODEX_CLI
            usage_ledger = ContentFreeUsageLedger(
                ledger_path,
                max_cost_usd=None if codex_cli else config.max_cost_usd,
                request_reservation_usd=(
                    0.0 if codex_cli else CANARY_REQUEST_RESERVATION_USD
                ),
                safety_reserve_usd=(0.0 if codex_cli else CANARY_SAFETY_RESERVE_USD),
            )
            project_config = config.project_compiler_config()
            base_compiler = factory(project_config, usage_ledger)
            report["compiler"]["fingerprint"] = base_compiler.fingerprint

            engine = Engine(
                str(database_path),
                user_id=CANARY_USER_ID,
                context_window=0,
                semantic_dedup=False,
                semantic_search_mode="hybrid",
                local_only=True,
            )
            cache = CompiledSessionCache(cache_path)
            compiler = CachedMemoryCompiler(base_compiler, cache)
            runner = EnrichmentRunner(engine, compiler)
            finalization_attempts = 0
            complete_jobs = 0
            failed_jobs = 0
            enrichment_error_codes: set[str] = set()
            update_reference_claims = 0
            update_matching_reference_present = False
            observed_models: set[str] = set()
            observed_providers: set[str] = set()
            observed_route_attempts: list[dict[str, int | str]] = []
            last_request_started: float | None = None
            try:
                for session in _synthetic_sessions():
                    ingestion_started = time.perf_counter()
                    stored = engine.store_session(
                        session["messages"],
                        session_id=str(session["session_id"]),
                        occurred_at=float(session["occurred_at"]),
                        metadata={"source": "synthetic-intelligence-canary"},
                    )
                    job_id = engine.enqueue_compilation(
                        int(stored["session_pk"]),
                        str(stored["source_hash"]),
                        compiler.fingerprint,
                    )
                    ingestion_ms += (time.perf_counter() - ingestion_started) * 1000

                    preview = build_compile_input(engine, int(stored["session_pk"]))
                    if session["session_id"] == "canary-preference-update":
                        update_reference_claims = len(preview.reference_claims)
                        matching_prior_ids = {
                            str(row[0])
                            for row in engine._conn.execute(
                                """
                                SELECT DISTINCT c.id
                                FROM memory_claims c
                                JOIN memory_sessions s ON s.id = c.session_id
                                JOIN memory_claim_sources cs ON cs.claim_id = c.id
                                WHERE c.user_id = ?
                                  AND s.external_id = 'canary-foundation'
                                  AND LOWER(cs.quote) LIKE '%apricot%'
                                  AND LOWER(
                                      c.text || ' ' || c.subject || ' ' ||
                                      c.predicate || ' ' || c.object_text
                                  ) LIKE '%accent%'
                                """,
                                (engine.user_id,),
                            ).fetchall()
                        }
                        update_matching_reference_present = any(
                            reference.claim_id in matching_prior_ids
                            for reference in preview.reference_claims
                        )

                    job = next(
                        (
                            candidate
                            for candidate in engine.pending_compilations(limit=100)
                            if int(candidate["id"]) == int(job_id)
                        ),
                        None,
                    )
                    if job is None:
                        raise RuntimeError("queued canary job was not runnable")

                    if codex_cli and _usage_event_count(usage_ledger) >= int(
                        config.codex_invocation_limit or 0
                    ):
                        finalization_attempts += 1
                        failed_jobs += 1
                        enrichment_error_codes.add("invocation_fuse_exhausted")
                        report["pipeline"].update(
                            {
                                "finalization_attempts": finalization_attempts,
                                "finalized_sessions": complete_jobs,
                                "complete_jobs": complete_jobs,
                                "failed_jobs": failed_jobs,
                                "enrichment_error_codes": sorted(
                                    enrichment_error_codes
                                ),
                                "update_reference_claims": update_reference_claims,
                                "update_matching_reference_present": (
                                    update_matching_reference_present
                                ),
                            }
                        )
                        break

                    if last_request_started is not None:
                        elapsed = monotonic() - last_request_started
                        remaining = config.min_request_interval_seconds - elapsed
                        if remaining > 0:
                            sleep(remaining)
                    last_request_started = monotonic()
                    enrichment_started = time.perf_counter()
                    outcome = runner.run_job(job)
                    enrichment_ms += (time.perf_counter() - enrichment_started) * 1000
                    finalization_attempts += 1
                    usage = outcome.get("usage") or {}
                    if usage.get("model"):
                        observed_models.add(str(usage["model"]))
                    if usage.get("provider"):
                        observed_providers.add(str(usage["provider"]))
                    raw_route_attempts = usage.get("router_attempts")
                    if isinstance(raw_route_attempts, list):
                        observed_route_attempts.extend(raw_route_attempts)
                        observed_route_attempts = observed_route_attempts[:16]
                    if outcome.get("ok") and outcome.get("status") == "complete":
                        complete_jobs += 1
                    else:
                        failed_jobs += 1
                        enrichment_error_codes.add(
                            str(outcome.get("code") or "compiler_failed")
                        )
                        failed_route_attempts = outcome.get("route_attempts")
                        if isinstance(failed_route_attempts, list):
                            observed_route_attempts.extend(failed_route_attempts)
                            observed_route_attempts = observed_route_attempts[:16]
                        safe_transport_failure = {
                            key: outcome[key]
                            for key in (
                                "upstream_status",
                                "retry_after_seconds",
                                "error_type",
                                "provider",
                                "provider_code",
                                "router_attempt",
                                "route_attempts",
                            )
                            if outcome.get(key) is not None
                        }
                        if safe_transport_failure:
                            report["pipeline"]["transport_failure"] = (
                                safe_transport_failure
                            )
                    report["pipeline"].update(
                        {
                            "finalization_attempts": finalization_attempts,
                            "finalized_sessions": complete_jobs,
                            "complete_jobs": complete_jobs,
                            "failed_jobs": failed_jobs,
                            "enrichment_error_codes": sorted(enrichment_error_codes),
                            "update_reference_claims": update_reference_claims,
                            "update_matching_reference_present": (
                                update_matching_reference_present
                            ),
                        }
                    )
                    report["route_observations"] = {
                        "models": sorted(observed_models),
                        "providers": sorted(observed_providers),
                        "attempts": observed_route_attempts,
                        "attempt_metadata_available": bool(observed_route_attempts),
                    }
                    if codex_cli:
                        report["invocation_fuse"]["observed_invocations"] = (
                            _usage_event_count(usage_ledger)
                        )
                    if failed_jobs:
                        break

                job_checks_ok = complete_jobs == len(_synthetic_sessions())
                if job_checks_ok:
                    checks, local_recall_ms, recall_upstream_requests = (
                        _run_local_checks(
                            engine,
                            usage_ledger=usage_ledger,
                            update_reference_claims=update_reference_claims,
                            update_matching_reference_present=(
                                update_matching_reference_present
                            ),
                            job_checks_ok=True,
                        )
                    )
                else:
                    local_recall_ms = 0.0
                    recall_upstream_requests = 0
                    checks = [
                        {"name": "all_enrichment_jobs_complete", "ok": False},
                    ]

                if codex_cli:
                    invocation_count = _usage_event_count(usage_ledger)
                    report["invocation_fuse"]["observed_invocations"] = invocation_count
                    checks.extend(
                        [
                            {
                                "name": "subscription_invocations_within_fuse",
                                "ok": invocation_count
                                <= int(config.codex_invocation_limit or 0),
                            },
                            {
                                "name": "provider_api_cost_not_required",
                                "ok": (
                                    usage_ledger.max_cost_usd is None
                                    and usage_ledger.request_reservation_usd == 0.0
                                    and usage_ledger.safety_reserve_usd == 0.0
                                ),
                            },
                        ]
                    )
                else:
                    cost_within_fuse = float(
                        usage_ledger.summary().get("cost_usd") or 0.0
                    ) <= float(config.max_cost_usd)
                    checks.append(
                        {
                            "name": "reported_cost_within_fuse",
                            "ok": cost_within_fuse,
                        }
                    )
                cache_stats = cache.stats()
                checks.append(
                    {
                        "name": "fresh_cache_compiled_each_session_once",
                        "ok": (
                            cache_stats.hits == 0
                            and cache_stats.misses == len(_synthetic_sessions())
                            and cache_stats.writes == len(_synthetic_sessions())
                            and cache_stats.entries == len(_synthetic_sessions())
                        ),
                    }
                )
                checks.append(
                    {
                        "name": "exact_model_route_observed",
                        "ok": observed_models == {config.model},
                    }
                )
                if codex_cli:
                    checks.extend(
                        [
                            {
                                "name": "codex_cli_subscription_route_observed",
                                "ok": (
                                    project_config.kind is CompilerKind.CODEX_CLI
                                    and project_config.endpoint is None
                                    and project_config.provider is None
                                    and not project_config.provider_allowlist
                                    and observed_providers == {CODEX_CLI_PROVIDER}
                                ),
                            },
                            {
                                "name": "subscription_route_has_no_router_attempts",
                                "ok": not observed_route_attempts,
                            },
                        ]
                    )
                else:
                    expected_provider_identities = {
                        _provider_identity(provider)
                        for provider in (
                            config.provider_allowlist
                            if config.provider_allowlist
                            else (config.provider,)
                        )
                    }
                    checks.extend(
                        [
                            {
                                "name": "provider_route_within_declared_policy",
                                "ok": bool(observed_providers)
                                and all(
                                    _provider_identity(provider)
                                    in expected_provider_identities
                                    for provider in observed_providers
                                ),
                            },
                            {
                                "name": (
                                    "observed_router_attempts_within_declared_policy"
                                ),
                                "ok": all(
                                    _provider_identity(str(attempt["provider"]))
                                    in expected_provider_identities
                                    for attempt in observed_route_attempts
                                ),
                            },
                        ]
                    )

                report["state"].update(
                    {
                        "derived_claims": int(engine.memory_status()["claim_count"]),
                        "cache": {
                            "hits": cache_stats.hits,
                            "misses": cache_stats.misses,
                            "writes": cache_stats.writes,
                            "entries": cache_stats.entries,
                        },
                    }
                )
                report["pipeline"].update(
                    {
                        "finalization_attempts": finalization_attempts,
                        "finalized_sessions": complete_jobs,
                        "complete_jobs": complete_jobs,
                        "failed_jobs": failed_jobs,
                        "enrichment_error_codes": sorted(enrichment_error_codes),
                        "update_reference_claims": update_reference_claims,
                        "update_matching_reference_present": (
                            update_matching_reference_present
                        ),
                        "recall_upstream_requests": recall_upstream_requests,
                    }
                )
                report["route_observations"] = {
                    "models": sorted(observed_models),
                    "providers": sorted(observed_providers),
                    "attempts": observed_route_attempts,
                    "attempt_metadata_available": bool(observed_route_attempts),
                }
                report["checks"] = checks
                report["usage"] = usage_ledger.summary()
                if codex_cli:
                    report["usage"].update(
                        {
                            "accounting_basis": "chatgpt_subscription_invocations",
                            "provider_api_cost_required": False,
                            "provider_billing_reconciled": False,
                            "subscription_use_is_not_claimed_as_free": True,
                        }
                    )
                report["timings_ms"].update(
                    {
                        "ingestion": _round_ms(ingestion_ms),
                        "enrichment": _round_ms(enrichment_ms),
                        "local_recall": _round_ms(local_recall_ms),
                    }
                )
                report["status"] = (
                    "passed"
                    if checks and all(check["ok"] for check in checks)
                    else "failed"
                )
            finally:
                engine.close()
                cache.close()
    except Exception as error:
        report["failure"] = _safe_failure(error)

    if usage_ledger is not None:
        report["usage"] = usage_ledger.summary()
        if config.compiler is CompilerKind.CODEX_CLI:
            report["invocation_fuse"]["observed_invocations"] = _usage_event_count(
                usage_ledger
            )
            report["usage"].update(
                {
                    "accounting_basis": "chatgpt_subscription_invocations",
                    "provider_api_cost_required": False,
                    "provider_billing_reconciled": False,
                    "subscription_use_is_not_claimed_as_free": True,
                }
            )
    report["timings_ms"].update(
        {
            "ingestion": _round_ms(ingestion_ms),
            "enrichment": _round_ms(enrichment_ms),
        }
    )

    report["timings_ms"]["total"] = _round_ms(
        (time.perf_counter() - total_started) * 1000
    )
    return report


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative finite number")
    return parsed


def _provider_allowlist(value: str) -> tuple[str, ...]:
    providers = tuple(item.strip() for item in value.split(",") if item.strip())
    if not providers:
        raise argparse.ArgumentTypeError("must contain at least one provider")
    if len({provider.casefold() for provider in providers}) != len(providers):
        raise argparse.ArgumentTypeError("providers must be unique")
    return providers


class _CanaryArgumentParser(argparse.ArgumentParser):
    """Argument parser with backend-specific, credential-free validation."""

    def parse_args(
        self,
        args: Sequence[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        parsed = super().parse_args(args, namespace)
        compiler = normalize_compiler_kind(parsed.compiler)
        if compiler is CompilerKind.OPENROUTER:
            missing = [
                option
                for option, value in (
                    ("--model", parsed.model),
                    ("--reasoning", parsed.reasoning),
                    ("--max-output-tokens", parsed.max_output_tokens),
                    ("--max-cost-usd", parsed.max_cost_usd),
                )
                if value is None
            ]
            if missing:
                self.error("the OpenRouter compiler requires " + ", ".join(missing))
            if not parsed.provider and not parsed.provider_allow:
                self.error(
                    "the OpenRouter compiler requires --provider or --provider-allow"
                )
            if any(
                value is not None
                for value in (
                    parsed.codex_cli_version,
                    parsed.codex_timeout_seconds,
                    parsed.codex_max_invocations,
                    parsed.codex_max_concurrency,
                )
            ):
                self.error("Codex CLI options require --compiler codex-cli")
            return parsed

        if parsed.provider or parsed.provider_allow:
            self.error("provider route options are invalid with --compiler codex-cli")
        if parsed.max_cost_usd is not None:
            self.error("--max-cost-usd is invalid with --compiler codex-cli")
        if parsed.max_output_tokens is not None:
            self.error("--max-output-tokens is invalid with --compiler codex-cli")
        if parsed.output_token_parameter != DEFAULT_OUTPUT_TOKEN_PARAMETER:
            self.error("--output-token-parameter is invalid with --compiler codex-cli")
        if parsed.capture_router_metadata:
            self.error("--capture-router-metadata is invalid with --compiler codex-cli")
        if parsed.transport_max_attempts is not None:
            self.error("--transport-max-attempts is invalid with --compiler codex-cli")
        return parsed


def build_argument_parser() -> argparse.ArgumentParser:
    parser = _CanaryArgumentParser(
        prog="narratordb-intelligence-canary",
        description=(
            "Run an isolated Intelligence compiler canary through OpenRouter "
            "or the ChatGPT-authenticated Codex CLI"
        ),
    )
    parser.add_argument(
        "--compiler",
        choices=(CompilerKind.OPENROUTER.value, CompilerKind.CODEX_CLI.value),
        default=CompilerKind.OPENROUTER.value,
        help="compiler backend (default: openrouter)",
    )
    parser.add_argument("--model", help="exact compiler model slug")
    provider_group = parser.add_mutually_exclusive_group()
    provider_group.add_argument("--provider", help="exact provider route pin")
    provider_group.add_argument(
        "--provider-allow",
        type=_provider_allowlist,
        help="ordered comma-separated provider allowlist with contained fallbacks",
    )
    parser.add_argument("--reasoning", help="model-supported reasoning effort")
    parser.add_argument("--max-output-tokens", type=_positive_int)
    parser.add_argument(
        "--output-token-parameter",
        choices=sorted(SUPPORTED_OUTPUT_TOKEN_PARAMETERS),
        default=DEFAULT_OUTPUT_TOKEN_PARAMETER,
        help="output limit field advertised by the pinned provider endpoint",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=_positive_float,
        help="mandatory OpenRouter cost fuse; invalid for Codex CLI subscription use",
    )
    parser.add_argument(
        "--transport-max-attempts",
        type=_positive_int,
        choices=(1,),
        default=None,
        help="frozen at one: the canary performs no client transport retry",
    )
    parser.add_argument(
        "--semantic-max-attempts",
        type=_positive_int,
        help="semantic parse/validation attempts (OpenRouter is frozen at one)",
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=_nonnegative_float,
        help="base delay when a retryable response provides no Retry-After value",
    )
    parser.add_argument(
        "--min-request-interval-seconds",
        type=_nonnegative_float,
        help="minimum interval between compiler requests",
    )
    parser.add_argument(
        "--capture-router-metadata",
        action="store_true",
        help="request content-free OpenRouter route-attempt metadata",
    )
    parser.add_argument(
        "--codex-cli-version",
        help="optional expected Codex CLI version identity",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=_positive_float,
        help="Codex CLI invocation timeout (default: 300s)",
    )
    parser.add_argument(
        "--codex-max-invocations",
        type=_positive_int,
        help=(
            "Codex CLI process fuse (default: three sessions multiplied by "
            "semantic-max-attempts)"
        ),
    )
    parser.add_argument(
        "--codex-max-concurrency",
        type=_positive_int,
        help="Codex CLI maximum concurrency (default: 1)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="optional path for the same content-safe JSON emitted to stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    config = IntelligenceCanaryConfig(
        compiler=CompilerKind(args.compiler),
        model=args.model,
        provider=args.provider or "",
        provider_allowlist=args.provider_allow or (),
        allow_fallbacks=bool(args.provider_allow),
        reasoning=args.reasoning,
        max_output_tokens=args.max_output_tokens,
        max_cost_usd=args.max_cost_usd,
        output_token_parameter=args.output_token_parameter,
        transport_max_attempts=args.transport_max_attempts or 1,
        semantic_max_attempts=args.semantic_max_attempts,
        retry_delay_seconds=args.retry_delay_seconds,
        min_request_interval_seconds=args.min_request_interval_seconds,
        capture_router_metadata=args.capture_router_metadata,
        codex_cli_version=args.codex_cli_version,
        codex_timeout_seconds=args.codex_timeout_seconds or 300.0,
        codex_max_invocations=args.codex_max_invocations,
        codex_max_concurrency=args.codex_max_concurrency or 1,
    )
    report = run_intelligence_canary(config)
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.report is not None:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(encoded + "\n", encoding="utf-8")
        except OSError:
            print(
                "narratordb-intelligence-canary: could not write report",
                file=sys.stderr,
            )
            return 2
    print(encoded)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
