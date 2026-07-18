from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narratordb.benchmarks.intelligence_canary import (
    IntelligenceCanaryConfig,
    build_argument_parser,
    main,
    run_intelligence_canary,
)
from narratordb.compiler import (
    CODEX_CLI_PROVIDER,
    CompileResult,
    CompileSessionInput,
    CompiledClaim,
    CompiledMemory,
    CompiledSummary,
    CompilerBudgetExceededError,
    CompilerResponseError,
    CompilerUsage,
    EvidenceSpan,
    SourceMessage,
    compiler_from_project_config,
)
from narratordb.config import CompilerKind


class _DeterministicCanaryCompiler:
    fingerprint = "deterministic-canary:v1"
    include_router_attempts = True
    router_attempt_provider = None

    def __init__(self, config, usage_ledger) -> None:
        self.config = config
        self.usage_ledger = usage_ledger
        self.calls = []
        self.usage_events = []

    @staticmethod
    def _claim(
        source,
        *,
        claim_id: str,
        text: str,
        memory_key: str,
        document_time: str | None,
        kind: str = "fact",
    ) -> CompiledClaim:
        return CompiledClaim(
            claim_id=claim_id,
            kind=kind,
            text=text,
            confidence=1.0,
            status="active",
            document_time=document_time,
            event_start=None,
            event_end=None,
            valid_from=document_time,
            valid_to=None,
            entity_ids=(),
            evidence=(
                EvidenceSpan(
                    message_id=source.message_id,
                    quote=source.content,
                    start=0,
                    end=len(source.content),
                ),
            ),
            memory_key=memory_key,
        )

    def compile_session(self, session) -> CompileResult:
        if not self.usage_ledger.can_start_request():
            raise CompilerBudgetExceededError()
        self.calls.append(session)
        if session.session_id == "canary-foundation":
            claims = (
                self._claim(
                    session.messages[0],
                    claim_id="control-panel-accent-apricot",
                    text=(
                        "The fictional rover has a stored control-panel accent "
                        "preference."
                    ),
                    memory_key="fictional_rover.preference.control_panel_accent",
                    document_time=session.document_time,
                    kind="preference",
                ),
                self._claim(
                    session.messages[1],
                    claim_id="fictional-field-note",
                    text="The assistant recommended the fictional LanternBreath field note.",
                    memory_key="assistant.resource.fictional_pressure_breathing",
                    document_time=session.document_time,
                ),
            )
        elif session.session_id == "canary-numbered-list":
            claims = (
                self._claim(
                    session.messages[1],
                    claim_id="travel-checklist",
                    text="The assistant supplied a fictional field-kit checklist.",
                    memory_key="assistant.list.fictional_field_kit",
                    document_time=session.document_time,
                ),
            )
        else:
            claims = (
                self._claim(
                    session.messages[0],
                    claim_id="control-panel-accent-deep-violet",
                    text=(
                        "The fictional rover control-panel accent preference was "
                        "updated."
                    ),
                    memory_key="fictional_rover.preference.control_panel_accent",
                    document_time=session.document_time,
                    kind="preference",
                ),
            )

        codex_cli = self.config.kind is CompilerKind.CODEX_CLI
        include_router_attempts = (
            not codex_cli
            and self.config.capture_router_metadata
            and self.include_router_attempts
        )
        route_provider = self.router_attempt_provider or str(
            self.config.provider or "Parasail"
        )
        provider = (
            CODEX_CLI_PROVIDER
            if codex_cli
            else str(
                self.config.provider
                or self.config.provider_allowlist[0].split("/", 1)[0]
            )
        )
        usage = CompilerUsage(
            request_model=str(self.config.model),
            response_model=str(self.config.model),
            provider=provider,
            attempt=1,
            prompt_tokens=100,
            cached_tokens=0,
            completion_tokens=40,
            reasoning_tokens=5,
            cost_usd=0.0 if codex_cli else 0.01,
            cost_source="subscription" if codex_cli else "estimated",
            finish_reason="stop",
            router_attempt=1 if include_router_attempts else None,
            attempted_providers=((route_provider,) if include_router_attempts else ()),
            attempt_statuses=(200,) if include_router_attempts else (),
        )
        self.usage_events.append(usage)
        self.usage_ledger.record(usage)
        return CompileResult(
            memory=CompiledMemory(
                session_id=session.session_id,
                summary=CompiledSummary(text=""),
                claims=claims,
                entities=(),
                relations=(),
            ),
            usage=(usage,),
        )


class IntelligenceCanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = IntelligenceCanaryConfig(
            model="test/model",
            provider="test-provider",
            reasoning="none",
            max_output_tokens=512,
            max_cost_usd=1.0,
            min_request_interval_seconds=0.0,
        )

    def test_fake_compiler_passes_without_network_and_report_is_content_safe(
        self,
    ) -> None:
        holder = {}

        def factory(config, usage_ledger):
            compiler = _DeterministicCanaryCompiler(config, usage_ledger)
            holder["compiler"] = compiler
            return compiler

        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "canary-secret"}):
            report = run_intelligence_canary(
                self.config,
                compiler_factory=factory,
            )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["pipeline"]["finalized_sessions"], 3)
        self.assertEqual(report["pipeline"]["finalization_attempts"], 3)
        self.assertEqual(report["pipeline"]["complete_jobs"], 3)
        self.assertEqual(report["pipeline"]["recall_upstream_requests"], 0)
        self.assertGreater(report["pipeline"]["update_reference_claims"], 0)
        self.assertTrue(report["pipeline"]["update_matching_reference_present"])
        self.assertEqual(report["usage"]["events"], 3)
        self.assertAlmostEqual(report["usage"]["cost_usd"], 0.03)
        self.assertEqual(
            report["compiler"]["output_token_parameter"],
            "max_completion_tokens",
        )
        self.assertTrue(all(check["ok"] for check in report["checks"]))

        compiler = holder["compiler"]
        self.assertEqual(len(compiler.calls), 3)
        update = compiler.calls[-1]
        self.assertTrue(
            any(
                claim.memory_key == "fictional_rover.preference.control_panel_accent"
                for claim in update.reference_claims
            )
        )

        encoded = json.dumps(report, sort_keys=True).casefold()
        for forbidden in (
            "canary-secret",
            "lanternbreath.example",
            "deep violet",
            "clockwork compass",
            "which entry was 19th",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_codex_cli_canary_uses_subscription_route_and_invocation_fuse(
        self,
    ) -> None:
        config = IntelligenceCanaryConfig(
            compiler=CompilerKind.CODEX_CLI,
            codex_cli_version="codex-cli-test",
        )
        holder = {}

        def factory(project_config, usage_ledger):
            compiler = _DeterministicCanaryCompiler(project_config, usage_ledger)
            holder["compiler"] = compiler
            return compiler

        report = run_intelligence_canary(config, compiler_factory=factory)
        project_config = config.project_compiler_config()

        self.assertEqual(project_config.kind, CompilerKind.CODEX_CLI)
        self.assertEqual(project_config.model, "gpt-5.4-mini")
        self.assertEqual(project_config.reasoning, "low")
        self.assertEqual(project_config.codex_cli_version, "codex-cli-test")
        self.assertEqual(project_config.codex_timeout_seconds, 300.0)
        self.assertEqual(project_config.codex_max_invocations, 6)
        self.assertEqual(project_config.codex_max_concurrency, 1)
        self.assertEqual(project_config.semantic_max_attempts, 2)

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["compiler"]["kind"], "codex-cli")
        self.assertFalse(report["cost_fuse"]["applicable"])
        self.assertIsNone(report["cost_fuse"]["max_cost"])
        self.assertTrue(report["invocation_fuse"]["applicable"])
        self.assertEqual(report["invocation_fuse"]["max_invocations"], 6)
        self.assertEqual(report["invocation_fuse"]["observed_invocations"], 3)
        self.assertEqual(report["usage"]["attempts"], 3)
        self.assertEqual(report["usage"]["cost_usd"], 0.0)
        self.assertEqual(
            report["usage"]["accounting_basis"],
            "chatgpt_subscription_invocations",
        )
        self.assertFalse(report["usage"]["provider_api_cost_required"])
        self.assertFalse(report["usage"]["provider_billing_reconciled"])
        self.assertEqual(report["route_observations"]["attempts"], [])
        self.assertEqual(
            report["route_observations"]["providers"],
            [CODEX_CLI_PROVIDER],
        )
        checks = {check["name"]: check["ok"] for check in report["checks"]}
        self.assertTrue(checks["subscription_invocations_within_fuse"])
        self.assertTrue(checks["codex_cli_subscription_route_observed"])
        self.assertTrue(checks["subscription_route_has_no_router_attempts"])
        self.assertTrue(all(check["ok"] for check in report["checks"]))
        self.assertEqual(len(holder["compiler"].calls), 3)
        self.assertTrue(
            all(
                usage.cost_source == "subscription"
                for usage in holder["compiler"].usage_events
            )
        )

        encoded = json.dumps(report, sort_keys=True).casefold()
        for forbidden in (
            "lanternbreath.example",
            "deep violet",
            "clockwork compass",
            "which entry was 19th",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_codex_cli_invocation_fuse_stops_before_third_call(
        self,
    ) -> None:
        config = IntelligenceCanaryConfig(
            compiler=CompilerKind.CODEX_CLI,
            codex_max_invocations=2,
        )
        holder = {}

        def factory(project_config, usage_ledger):
            compiler = _DeterministicCanaryCompiler(project_config, usage_ledger)
            holder["compiler"] = compiler
            return compiler

        report = run_intelligence_canary(config, compiler_factory=factory)

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["pipeline"]["complete_jobs"], 2)
        self.assertEqual(report["pipeline"]["failed_jobs"], 1)
        self.assertEqual(report["pipeline"]["finalization_attempts"], 3)
        self.assertEqual(
            report["pipeline"]["enrichment_error_codes"],
            ["invocation_fuse_exhausted"],
        )
        self.assertEqual(report["invocation_fuse"]["max_invocations"], 2)
        self.assertEqual(report["invocation_fuse"]["observed_invocations"], 2)
        self.assertEqual(len(holder["compiler"].calls), 2)

    def test_cost_fuse_stops_a_later_compilation_and_returns_failure(self) -> None:
        config = IntelligenceCanaryConfig(
            model="test/model",
            provider="test-provider",
            reasoning="none",
            max_output_tokens=512,
            max_cost_usd=0.07,
            min_request_interval_seconds=0.0,
        )
        holder = {}

        def factory(project_config, usage_ledger):
            compiler = _DeterministicCanaryCompiler(project_config, usage_ledger)
            holder["compiler"] = compiler
            return compiler

        report = run_intelligence_canary(config, compiler_factory=factory)

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["pipeline"]["complete_jobs"], 2)
        self.assertEqual(report["pipeline"]["finalized_sessions"], 2)
        self.assertEqual(report["pipeline"]["finalization_attempts"], 3)
        self.assertEqual(report["pipeline"]["failed_jobs"], 1)
        self.assertEqual(len(holder["compiler"].calls), 2)
        self.assertLessEqual(report["usage"]["cost_usd"], config.max_cost_usd)

    def test_frozen_canary_malformed_response_makes_one_wire_call(self) -> None:
        calls = []

        def malformed_transport(endpoint, headers, payload, timeout, max_bytes):
            calls.append((endpoint, headers, payload, timeout, max_bytes))
            return {
                "model": "test/model",
                "provider": "test-provider",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "not-json"},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        compiler = compiler_from_project_config(
            self.config.project_compiler_config(),
            api_key="runtime-only-test-key",
            transport=malformed_transport,
            sleep=lambda _seconds: None,
        )
        session = CompileSessionInput(
            session_id="one-wire-call",
            messages=(
                SourceMessage(message_id="1", role="user", content="Synthetic."),
            ),
        )

        with self.assertRaises(CompilerResponseError):
            compiler.compile_session(session)

        self.assertEqual(len(calls), 1)
        self.assertEqual(self.config.transport_max_attempts, 1)
        self.assertEqual(self.config.semantic_max_attempts, 1)

    def test_ordered_allowlist_report_and_session_pacing(self) -> None:
        config = IntelligenceCanaryConfig(
            model="test/model",
            provider="",
            provider_allowlist=("parasail/fp4", "wafer/fp4"),
            allow_fallbacks=True,
            reasoning="none",
            max_output_tokens=512,
            max_cost_usd=1.0,
            min_request_interval_seconds=5.0,
            capture_router_metadata=True,
        )
        clock = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        report = run_intelligence_canary(
            config,
            compiler_factory=_DeterministicCanaryCompiler,
            sleep=sleep,
            monotonic=lambda: clock[0],
        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(
            report["compiler"]["provider_allowlist"],
            ["parasail/fp4", "wafer/fp4"],
        )
        self.assertTrue(report["compiler"]["no_client_retry"])
        self.assertEqual(sleeps, [5.0, 5.0])
        self.assertEqual(
            report["route_observations"]["attempts"][0],
            {"provider": "Parasail", "status": 200},
        )
        self.assertTrue(report["route_observations"]["attempt_metadata_available"])

    def test_missing_optional_router_attempt_metadata_is_non_gating(self) -> None:
        class NoAttemptMetadataCompiler(_DeterministicCanaryCompiler):
            include_router_attempts = False

        config = IntelligenceCanaryConfig(
            model="test/model",
            provider="",
            provider_allowlist=("parasail/fp4", "wafer/fp4"),
            allow_fallbacks=True,
            reasoning="none",
            max_output_tokens=512,
            max_cost_usd=1.0,
            min_request_interval_seconds=0.0,
            capture_router_metadata=True,
        )

        report = run_intelligence_canary(
            config,
            compiler_factory=NoAttemptMetadataCompiler,
        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["route_observations"]["attempts"], [])
        self.assertFalse(report["route_observations"]["attempt_metadata_available"])
        route_check = next(
            check
            for check in report["checks"]
            if check["name"] == "observed_router_attempts_within_declared_policy"
        )
        self.assertTrue(route_check["ok"])

    def test_observed_out_of_policy_router_attempt_fails(self) -> None:
        class OutOfPolicyAttemptCompiler(_DeterministicCanaryCompiler):
            router_attempt_provider = "outside/fp4"

        config = IntelligenceCanaryConfig(
            model="test/model",
            provider="",
            provider_allowlist=("parasail/fp4", "wafer/fp4"),
            allow_fallbacks=True,
            reasoning="none",
            max_output_tokens=512,
            max_cost_usd=1.0,
            min_request_interval_seconds=0.0,
            capture_router_metadata=True,
        )

        report = run_intelligence_canary(
            config,
            compiler_factory=OutOfPolicyAttemptCompiler,
        )

        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["route_observations"]["attempt_metadata_available"])
        route_check = next(
            check
            for check in report["checks"]
            if check["name"] == "observed_router_attempts_within_declared_policy"
        )
        self.assertFalse(route_check["ok"])

    def test_cli_requires_disclosed_cost_and_has_no_credential_argument(self) -> None:
        parser = build_argument_parser()
        option_strings = {
            option for action in parser._actions for option in action.option_strings
        }
        self.assertNotIn("--api-key", option_strings)
        self.assertNotIn("--openrouter-api-key", option_strings)
        self.assertIn("--compiler", option_strings)
        parsed = parser.parse_args(
            [
                "--model",
                "test/model",
                "--provider",
                "test-provider",
                "--reasoning",
                "none",
                "--max-output-tokens",
                "512",
                "--output-token-parameter",
                "max_tokens",
                "--max-cost-usd",
                "1",
            ]
        )
        self.assertEqual(parsed.output_token_parameter, "max_tokens")
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--model",
                    "test/model",
                    "--provider",
                    "test-provider",
                    "--reasoning",
                    "none",
                    "--max-output-tokens",
                    "512",
                ]
            )
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--model",
                    "test/model",
                    "--provider",
                    "test-provider",
                    "--reasoning",
                    "none",
                    "--max-output-tokens",
                    "512",
                    "--max-cost-usd",
                    "0",
                ]
            )

    def test_codex_cli_cli_options_need_no_provider_or_api_cost(self) -> None:
        parser = build_argument_parser()
        parsed = parser.parse_args(
            [
                "--compiler",
                "codex-cli",
                "--model",
                "gpt-5.4-mini",
                "--reasoning",
                "high",
                "--semantic-max-attempts",
                "2",
                "--retry-delay-seconds",
                "0.5",
                "--min-request-interval-seconds",
                "1",
                "--codex-cli-version",
                "codex-cli-test",
                "--codex-timeout-seconds",
                "45",
                "--codex-max-invocations",
                "4",
                "--codex-max-concurrency",
                "1",
            ]
        )
        self.assertEqual(parsed.compiler, "codex-cli")
        self.assertIsNone(parsed.provider)
        self.assertIsNone(parsed.max_cost_usd)
        self.assertEqual(parsed.codex_max_invocations, 4)

        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--compiler",
                    "codex-cli",
                    "--provider",
                    "OpenAI",
                ]
            )
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--compiler",
                    "codex-cli",
                    "--max-cost-usd",
                    "1",
                ]
            )
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--compiler",
                    "codex-cli",
                    "--transport-max-attempts",
                    "1",
                ]
            )

        captured = {}

        def fake_run(config):
            captured["config"] = config
            return {"schema_version": "test", "status": "passed"}

        stdout = io.StringIO()
        with (
            patch(
                "narratordb.benchmarks.intelligence_canary.run_intelligence_canary",
                side_effect=fake_run,
            ),
            patch("sys.stdout", stdout),
        ):
            self.assertEqual(
                main(
                    [
                        "--compiler",
                        "codex-cli",
                        "--codex-cli-version",
                        "codex-cli-test",
                    ]
                ),
                0,
            )
        config = captured["config"]
        self.assertEqual(config.compiler, CompilerKind.CODEX_CLI)
        self.assertEqual(config.model, "gpt-5.4-mini")
        self.assertEqual(config.reasoning, "low")
        self.assertIsNone(config.max_cost_usd)
        self.assertEqual(config.codex_invocation_limit, 6)

    def test_main_writes_safe_report_and_exits_nonzero_on_failed_canary(self) -> None:
        failed = {
            "schema_version": "test",
            "status": "failed",
            "failure": {
                "type": "CompilerConfigurationError",
                "code": "missing_api_key",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safe-report.json"
            stdout = io.StringIO()
            with (
                patch(
                    "narratordb.benchmarks.intelligence_canary.run_intelligence_canary",
                    return_value=failed,
                ),
                patch("sys.stdout", stdout),
            ):
                exit_code = main(
                    [
                        "--model",
                        "test/model",
                        "--provider",
                        "test-provider",
                        "--reasoning",
                        "none",
                        "--max-output-tokens",
                        "512",
                        "--max-cost-usd",
                        "1",
                        "--report",
                        str(path),
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), failed)
            self.assertEqual(json.loads(stdout.getvalue()), failed)


if __name__ == "__main__":
    unittest.main()
