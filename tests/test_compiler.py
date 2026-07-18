import hashlib
import http.client
import http.server
import json
import os
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from narratordb.compiler import (
    DEFAULT_GPT_54_MINI_COMPILER_CONFIG,
    DEFAULT_LUNA_PRO_EXPERIMENT_CONFIG,
    MAX_COMPILED_CLAIMS,
    MAX_COMPILED_ENTITIES,
    MAX_COMPILED_RELATIONS,
    MAX_EVIDENCE_SPANS,
    CompileSessionInput,
    CompilerBudgetExceededError,
    CompilerConfigurationError,
    CompilerResponseError,
    CompilerTransportError,
    ContentFreeUsageLedger,
    LocalOpenAICompiler,
    LocalOpenAICompilerConfig,
    MemoryCompiler,
    OpenRouterCompiler,
    OpenRouterCompilerConfig,
    ReferenceClaim,
    SourceMessage,
    compiled_memory_json_schema,
    compiler_from_project_config,
    _stdlib_json_transport,
    validate_loopback_url,
)
from narratordb.config import CompilerConfig


SOURCE_TEXT = "I moved to Kyoto on 2025-01-02. Kyoto is home now."


def session_input() -> CompileSessionInput:
    return CompileSessionInput(
        session_id="session-1",
        document_time="2025-01-03T12:00:00Z",
        messages=(
            SourceMessage(
                message_id="message-1",
                role="user",
                content=SOURCE_TEXT,
                occurred_at="2025-01-03T12:00:00Z",
            ),
        ),
    )


def compiled_payload(*, quote: str = "I moved to Kyoto on 2025-01-02.") -> dict:
    evidence = {
        "message_id": "message-1",
        "quote": quote,
        "start": None,
        "end": None,
    }
    return {
        "summary": {
            "text": "The user moved to Kyoto and considers it home.",
            "evidence": [deepcopy(evidence)],
        },
        "entities": [
            {
                "entity_id": "e1",
                "name": "Kyoto",
                "entity_type": "place",
                "aliases": [],
                "evidence": [deepcopy(evidence)],
            }
        ],
        "claims": [
            {
                "claim_id": "c1",
                "kind": "event",
                "text": "The user moved to Kyoto on 2025-01-02.",
                "subject": "the user",
                "predicate": "moved to",
                "object_text": "Kyoto",
                "memory_key": "user.residence.current_city",
                "confidence": 0.99,
                "status": "active",
                "document_time": "2025-01-03T12:00:00Z",
                "event_start": "2025-01-02",
                "event_end": None,
                "valid_from": "2025-01-02",
                "valid_to": None,
                "entity_ids": ["e1"],
                "evidence": [deepcopy(evidence)],
            }
        ],
        "relations": [],
    }


def completion_response(
    payload: dict | None = None,
    *,
    model: str = "openai/gpt-5.4-mini",
    provider: str = "Azure",
    cost: float | None = 0.002,
) -> dict:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 5},
        "completion_tokens_details": {"reasoning_tokens": 7},
    }
    if cost is not None:
        usage["cost"] = cost
    return {
        "model": model,
        "provider": provider,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": json.dumps(payload or compiled_payload())},
            }
        ],
        "usage": usage,
    }


class CapturingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, endpoint, headers, payload, timeout, max_response_bytes):
        self.calls.append(
            {
                "endpoint": endpoint,
                "headers": dict(headers),
                "payload": deepcopy(payload),
                "timeout": timeout,
                "max_response_bytes": max_response_bytes,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CompilerTests(unittest.TestCase):
    def test_stdlib_transport_never_forwards_authorization_on_redirect(self):
        attacker_authorizations: list[str | None] = []

        class AttackerHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                attacker_authorizations.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = do_GET

            def log_message(self, _format: str, *args: object) -> None:
                return None

        attacker = http.server.ThreadingHTTPServer(("127.0.0.1", 0), AttackerHandler)
        attacker_thread = threading.Thread(target=attacker.serve_forever, daemon=True)
        attacker_thread.start()
        attacker_url = f"http://127.0.0.1:{attacker.server_port}/capture"

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            redirect_status = 302

            def do_POST(self) -> None:
                self.send_response(self.redirect_status)
                self.send_header("Location", attacker_url)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _format: str, *args: object) -> None:
                return None

        redirector = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirector_thread = threading.Thread(
            target=redirector.serve_forever, daemon=True
        )
        redirector_thread.start()
        try:
            for redirect_status in (301, 302, 303):
                with self.subTest(redirect_status=redirect_status):
                    RedirectHandler.redirect_status = redirect_status
                    with self.assertRaises(CompilerTransportError) as raised:
                        _stdlib_json_transport(
                            f"http://127.0.0.1:{redirector.server_port}/start",
                            {"Authorization": "Bearer runtime-secret"},
                            {"model": "test"},
                            2.0,
                            1024,
                        )
                    self.assertEqual(raised.exception.code, f"http_{redirect_status}")
                    self.assertFalse(raised.exception.retryable)
                    self.assertEqual(attacker_authorizations, [])
        finally:
            redirector.shutdown()
            attacker.shutdown()
            redirector.server_close()
            attacker.server_close()
            redirector_thread.join(timeout=2.0)
            attacker_thread.join(timeout=2.0)

        self.assertEqual(attacker_authorizations, [])

    def test_wire_schema_avoids_unsupported_unique_items_keyword(self):
        schema = compiled_memory_json_schema()
        encoded = json.dumps(schema, sort_keys=True)

        self.assertNotIn('"uniqueItems"', encoded)
        self.assertEqual(
            schema["properties"]["claims"]["maxItems"],
            MAX_COMPILED_CLAIMS,
        )
        self.assertEqual(
            schema["properties"]["entities"]["maxItems"],
            MAX_COMPILED_ENTITIES,
        )
        self.assertEqual(
            schema["properties"]["relations"]["maxItems"],
            MAX_COMPILED_RELATIONS,
        )
        self.assertEqual(
            schema["properties"]["summary"]["properties"]["evidence"]["maxItems"],
            MAX_EVIDENCE_SPANS,
        )

    def test_local_compiler_urls_must_be_explicit_loopback(self):
        self.assertEqual(
            validate_loopback_url("http://localhost:11434/v1/"),
            "http://localhost:11434/v1",
        )
        self.assertEqual(
            validate_loopback_url("http://127.0.0.2:8000"), "http://127.0.0.2:8000"
        )
        self.assertEqual(
            validate_loopback_url("http://[::1]:8080/v1"), "http://[::1]:8080/v1"
        )

        for unsafe in (
            "https://example.com/v1",
            "http://0.0.0.0:8000",
            "http://127.0.0.1.example.com",
            "http://user:pass@localhost:8000",
        ):
            with self.subTest(url=unsafe), self.assertRaises(ValueError):
                validate_loopback_url(unsafe)

    def test_local_adapter_uses_strict_schema_without_auth_by_default(self):
        transport = CapturingTransport(
            [completion_response(model="local-memory-model", provider="", cost=None)]
        )
        compiler = LocalOpenAICompiler(
            LocalOpenAICompilerConfig(
                base_url="http://localhost:11434/v1",
                model="local-memory-model",
                max_attempts=1,
            ),
            transport=transport,
        )

        result = compiler.compile_session(session_input())
        request = transport.calls[0]

        self.assertIsInstance(compiler, MemoryCompiler)
        self.assertEqual(
            request["endpoint"], "http://localhost:11434/v1/chat/completions"
        )
        self.assertNotIn("Authorization", request["headers"])
        self.assertTrue(request["payload"]["response_format"]["json_schema"]["strict"])
        schema = request["payload"]["response_format"]["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(request["payload"]["max_completion_tokens"], 8192)
        self.assertNotIn("max_tokens", request["payload"])
        self.assertIn(
            "Return at most 16 claims, 16 entities, and 8 relations",
            request["payload"]["messages"][0]["content"],
        )
        claim_schema = schema["properties"]["claims"]["items"]
        for field in ("subject", "predicate", "object_text", "memory_key"):
            self.assertIn(field, claim_schema["required"])
        self.assertEqual(result.memory.claims[0].subject, "the user")
        self.assertEqual(result.memory.claims[0].predicate, "moved to")
        self.assertEqual(result.memory.claims[0].object_text, "Kyoto")
        self.assertEqual(
            result.memory.claims[0].memory_key,
            "user.residence.current_city",
        )
        self.assertEqual(result.memory.claims[0].evidence[0].start, 0)
        self.assertEqual(
            result.memory.claims[0].evidence[0].end,
            len("I moved to Kyoto on 2025-01-02."),
        )
        self.assertEqual(result.usage[0].cost_source, "unavailable")

    def test_prompt_preserves_salient_assistant_memory_and_separates_references(self):
        reference = ReferenceClaim(
            claim_id="prior-42",
            memory_key="user.residence.current_city",
            text="The user previously lived in Tokyo.",
            document_time="2024-10-01T00:00:00Z",
        )
        transport = CapturingTransport(
            [completion_response(model="local-memory-model", provider="", cost=None)]
        )
        compiler = LocalOpenAICompiler(
            LocalOpenAICompilerConfig(
                base_url="http://localhost:11434/v1",
                model="local-memory-model",
                max_attempts=1,
            ),
            transport=transport,
        )

        compiler.compile_session(
            CompileSessionInput(
                session_id="session-1",
                messages=session_input().messages,
                document_time=session_input().document_time,
                reference_claims=(reference,),
            )
        )

        request = transport.calls[0]["payload"]
        prompt = request["messages"][0]["content"]
        supplied = json.loads(request["messages"][1]["content"])
        self.assertIn("salient assistant recommendations", prompt)
        self.assertIn("assistant-authored information is durable", prompt)
        self.assertIn("Source messages are the only evidence", prompt)
        self.assertIn("never cite them", prompt)
        self.assertEqual(
            supplied["reference_claims"],
            [
                {
                    "claim_id": "prior-42",
                    "memory_key": "user.residence.current_city",
                    "text": "The user previously lived in Tokyo.",
                    "document_time": "2024-10-01T00:00:00Z",
                    "event_start": None,
                    "event_end": None,
                    "valid_from": None,
                    "valid_to": None,
                }
            ],
        )

    def test_reference_claim_cannot_be_used_as_compiled_evidence(self):
        reference = ReferenceClaim(
            claim_id="prior-42",
            memory_key="user.residence.current_city",
            text="The user lives in Tokyo.",
        )
        payload = compiled_payload(quote=reference.text)
        payload["summary"]["evidence"][0]["message_id"] = reference.claim_id
        payload["entities"][0]["evidence"][0]["message_id"] = reference.claim_id
        payload["claims"][0]["evidence"][0]["message_id"] = reference.claim_id
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=CapturingTransport([completion_response(payload)]),
        )
        current = session_input()

        result = compiler.compile_session(
            CompileSessionInput(
                session_id=current.session_id,
                messages=current.messages,
                document_time=current.document_time,
                reference_claims=(reference,),
            )
        )

        self.assertEqual(result.memory.summary.text, "")
        self.assertEqual(result.memory.claims, ())
        self.assertEqual(result.memory.entities, ())

    def test_openrouter_adapter_pins_privacy_provider_model_and_no_fallbacks(self):
        transport = CapturingTransport([completion_response(cost=None)])
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=transport,
            sleep=lambda _seconds: None,
        )

        result = compiler.compile_session(session_input())
        request = transport.calls[0]
        provider = request["payload"]["provider"]

        self.assertEqual(
            DEFAULT_LUNA_PRO_EXPERIMENT_CONFIG.model, "openai/gpt-5.6-luna-pro"
        )
        self.assertEqual(DEFAULT_LUNA_PRO_EXPERIMENT_CONFIG.provider, "Azure")
        self.assertEqual(DEFAULT_LUNA_PRO_EXPERIMENT_CONFIG.reasoning_effort, "low")
        self.assertEqual(
            DEFAULT_GPT_54_MINI_COMPILER_CONFIG.model, "openai/gpt-5.4-mini"
        )
        self.assertEqual(DEFAULT_GPT_54_MINI_COMPILER_CONFIG.provider, "Azure")
        self.assertEqual(provider["only"], ["Azure"])
        self.assertFalse(provider["allow_fallbacks"])
        self.assertTrue(provider["require_parameters"])
        self.assertTrue(provider["zdr"])
        self.assertEqual(provider["data_collection"], "deny")
        self.assertEqual(request["payload"]["reasoning"], {"effort": "minimal"})
        self.assertEqual(request["payload"]["seed"], 0)
        self.assertEqual(request["payload"]["max_completion_tokens"], 8192)
        self.assertNotIn("max_tokens", request["payload"])
        self.assertEqual(request["headers"]["Authorization"], "Bearer runtime-test-key")
        self.assertNotIn("runtime-test-key", compiler.fingerprint)
        self.assertEqual(result.usage[0].cost_source, "estimated")
        self.assertAlmostEqual(result.total_cost_usd, 0.000165)

    def test_openrouter_ordered_allowlist_contains_fallbacks_and_is_fingerprinted(self):
        providers = ("parasail/fp4", "wafer/fp4", "deepinfra/fp4")
        configured = CompilerConfig.openrouter(
            model="z-ai/glm-5.2",
            provider_allowlist=providers,
            output_token_parameter="max_tokens",
            transport_max_attempts=1,
            retry_delay_seconds=60.0,
            capture_router_metadata=True,
        )
        self.assertIsNone(configured.provider)
        self.assertTrue(configured.allow_fallbacks)
        self.assertEqual(CompilerConfig.from_dict(configured.to_dict()), configured)

        response = completion_response(
            model="z-ai/glm-5.2",
            provider="Wafer",
            cost=0.001,
        )
        response["openrouter_metadata"] = {
            "attempt": 2,
            "attempts": [
                {"provider": "Parasail", "status": 429},
                {"provider": "Wafer", "status": 200},
            ],
        }
        transport = CapturingTransport([response])
        compiler = compiler_from_project_config(
            configured,
            api_key="runtime-test-key",
            transport=transport,
        )

        result = compiler.compile_session(session_input())

        request = transport.calls[0]
        self.assertEqual(request["payload"]["provider"]["only"], list(providers))
        self.assertEqual(request["payload"]["provider"]["order"], list(providers))
        self.assertTrue(request["payload"]["provider"]["allow_fallbacks"])
        self.assertTrue(request["payload"]["provider"]["zdr"])
        self.assertEqual(request["payload"]["provider"]["data_collection"], "deny")
        self.assertEqual(request["payload"]["max_tokens"], 8192)
        self.assertEqual(request["headers"]["X-OpenRouter-Metadata"], "enabled")
        self.assertEqual(result.usage[0].router_attempt, 2)
        self.assertEqual(
            result.usage[0].attempted_providers,
            ("parasail/fp4", "wafer/fp4"),
        )
        self.assertEqual(result.usage[0].attempt_statuses, (429, 200))

    def test_provider_routes_reject_malformed_ambiguous_and_typed_allowlists(self):
        invalid_allowlists = (
            ("/",),
            ("wafer/fp4", "wafer/fast"),
            (["Wafer"],),
            ("wafer/fp4/extra",),
        )
        for provider_allowlist in invalid_allowlists:
            with self.subTest(provider_allowlist=provider_allowlist):
                with self.assertRaises(ValueError):
                    CompilerConfig.openrouter(
                        provider_allowlist=provider_allowlist,  # type: ignore[arg-type]
                    )
                with self.assertRaises(ValueError):
                    OpenRouterCompilerConfig(
                        model="openai/gpt-5.4-mini",
                        provider="",
                        provider_allowlist=provider_allowlist,  # type: ignore[arg-type]
                    )

    def test_provider_attestation_distinguishes_full_routes_and_rejects_types(self):
        config = OpenRouterCompilerConfig(
            model="openai/gpt-5.4-mini",
            provider="",
            provider_allowlist=("wafer/fp4",),
            max_attempts=1,
            transport_max_attempts=1,
        )
        accepted = ("Wafer", "wafer/fp4", "WAFER/FP4")
        for response_provider in accepted:
            with self.subTest(accepted=response_provider):
                result = OpenRouterCompiler(
                    config,
                    api_key="runtime-test-key",
                    transport=CapturingTransport(
                        [completion_response(provider=response_provider)]
                    ),
                ).compile_session(session_input())
                self.assertEqual(result.usage[0].provider, "wafer/fp4")

        rejected = (None, "", "wafer/fast", ["Wafer"])
        for response_provider in rejected:
            with self.subTest(rejected=response_provider):
                response = completion_response()
                if response_provider is None:
                    response.pop("provider")
                else:
                    response["provider"] = response_provider
                compiler = OpenRouterCompiler(
                    config,
                    api_key="runtime-test-key",
                    transport=CapturingTransport([response]),
                )
                with self.assertRaises(CompilerResponseError) as raised:
                    compiler.compile_session(session_input())
                self.assertEqual(raised.exception.code, "provider_route_mismatch")

    def test_transport_retry_honors_sanitized_retry_after_and_records_error(self):
        sleeps = []
        transport_error = CompilerTransportError(
            "safe transport failure",
            code="http_429",
            retryable=True,
            status=429,
            retry_after_seconds=12.5,
            error_type="rate_limit_exceeded",
            provider_name="DeepInfra",
            provider_code="429",
            attempted_providers=("Parasail", "DeepInfra"),
            attempt_statuses=(429, 429),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            ledger = ContentFreeUsageLedger(path, max_cost_usd=1.0)
            compiler = OpenRouterCompiler(
                OpenRouterCompilerConfig(
                    model="openai/gpt-5.4-mini",
                    provider="Azure",
                    transport_max_attempts=2,
                ),
                api_key="runtime-test-key",
                transport=CapturingTransport(
                    [transport_error, completion_response(cost=0.001)]
                ),
                usage_sink=ledger,
                sleep=sleeps.append,
            )

            result = compiler.compile_session(session_input())

            self.assertEqual(sleeps, [12.5])
            self.assertEqual(result.usage[0].attempt, 2)
            self.assertEqual(ledger.summary()["events"], 1)
            self.assertEqual(ledger.summary()["error_events"], 1)
            saved = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(saved[0]["retry_after_seconds"], 12.5)
            self.assertEqual(saved[0]["error_type"], "rate_limit_exceeded")
            self.assertEqual(
                saved[0]["attempted_providers"],
                ["route_mismatch", "route_mismatch"],
            )
            self.assertEqual(saved[0]["attempt_statuses"], [429, 429])

    def test_shared_pacer_spaces_semantic_repair_wire_attempts(self):
        malformed = completion_response()
        malformed["choices"][0]["message"]["content"] = "not-json"
        transport = CapturingTransport([malformed, completion_response()])
        sleeps = []
        compiler = OpenRouterCompiler(
            OpenRouterCompilerConfig(
                model="openai/gpt-5.4-mini",
                provider="Azure",
                max_attempts=2,
                transport_max_attempts=1,
                retry_delay_seconds=0.0,
                min_request_interval_seconds=10.0,
            ),
            api_key="runtime-only-test-key",
            transport=transport,
            sleep=sleeps.append,
            monotonic=lambda: 0.0,
        )

        result = compiler.compile_session(session_input())

        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(transport.calls), 2)
        self.assertIn(10.0, sleeps)
        unpaced = OpenRouterCompiler(
            OpenRouterCompilerConfig(
                model="openai/gpt-5.4-mini",
                provider="Azure",
                max_attempts=2,
                transport_max_attempts=1,
            ),
            api_key="runtime-only-test-key",
            transport=CapturingTransport([completion_response()]),
        )
        self.assertNotEqual(compiler.fingerprint, unpaced.fingerprint)

    def test_invalid_wire_envelope_is_recorded_and_semantically_repaired(self):
        transport = CapturingTransport(["not-an-object", completion_response()])
        with tempfile.TemporaryDirectory() as directory:
            ledger = ContentFreeUsageLedger(
                Path(directory) / "usage.jsonl",
                max_cost_usd=1.0,
            )
            compiler = OpenRouterCompiler(
                OpenRouterCompilerConfig(
                    model="openai/gpt-5.4-mini",
                    provider="Azure",
                    max_attempts=2,
                    transport_max_attempts=1,
                    retry_delay_seconds=0.0,
                ),
                api_key="runtime-test-key",
                transport=transport,
                usage_sink=ledger,
                sleep=lambda _seconds: None,
            )

            result = compiler.compile_session(session_input())

            self.assertEqual(len(transport.calls), 2)
            self.assertEqual(result.usage[0].attempt, 2)
            self.assertEqual(ledger.summary()["error_events"], 1)
            self.assertEqual(ledger.summary()["events"], 1)
            saved = [
                json.loads(line)
                for line in ledger.path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(saved[0]["code"], "invalid_response_envelope")

    def test_error_ledger_rejects_free_form_metadata_and_raw_content(self):
        secret = "I moved to Kyoto and this must never enter diagnostics"
        response = {
            "error": {
                "code": 429,
                "message": secret,
                "metadata": {
                    "error_type": f"rate limit {secret}",
                    "provider_name": f"DeepInfra leaked private words from {secret}",
                    "provider_code": f"429 {secret}",
                    "raw": secret,
                },
            },
            "openrouter_metadata": {
                "attempt": 2,
                "attempts": [
                    {"provider": "Parasail", "status": 429, "summary": secret},
                    {"provider": "DeepInfra", "status": 429, "pipeline": secret},
                ],
            },
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            ledger = ContentFreeUsageLedger(path, max_cost_usd=1.0)
            compiler = OpenRouterCompiler(
                OpenRouterCompilerConfig(
                    model="openai/gpt-5.4-mini",
                    provider="Azure",
                    transport_max_attempts=1,
                ),
                api_key="runtime-test-key",
                transport=CapturingTransport([response]),
                usage_sink=ledger,
            )

            with self.assertRaises(CompilerTransportError):
                compiler.compile_session(session_input())

            saved_text = path.read_text(encoding="utf-8")
            saved = json.loads(saved_text)
            self.assertNotIn(secret, saved_text)
            self.assertNotIn(SOURCE_TEXT, saved_text)
            self.assertEqual(saved["error_type"], "unknown")
            self.assertNotIn("provider", saved)
            self.assertEqual(saved["provider_code"], "unknown")
            self.assertEqual(
                saved["attempted_providers"],
                ["route_mismatch", "route_mismatch"],
            )
            self.assertEqual(saved["attempt_statuses"], [429, 429])

    def test_upstream_identity_metadata_is_closed_world_before_ledger_write(self):
        secret = "sk-or-v1-abc123def456"
        response = completion_response()
        response["model"] = secret
        response["provider"] = secret
        response["choices"][0]["finish_reason"] = secret
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            ledger = ContentFreeUsageLedger(path, max_cost_usd=1.0)
            compiler = OpenRouterCompiler(
                OpenRouterCompilerConfig(
                    model="openai/gpt-5.4-mini",
                    provider="Azure",
                    transport_max_attempts=1,
                ),
                api_key="runtime-test-key",
                transport=CapturingTransport([response]),
                usage_sink=ledger,
            )

            with self.assertRaises(CompilerResponseError) as raised:
                compiler.compile_session(session_input())

            usage = raised.exception.usage[0]
            self.assertEqual(usage.response_model, "route_mismatch")
            self.assertEqual(usage.provider, "route_mismatch")
            self.assertEqual(usage.finish_reason, "unknown")
            saved_text = path.read_text(encoding="utf-8")
            self.assertNotIn(secret, saved_text)
            saved = json.loads(saved_text)
            self.assertEqual(saved["response_model"], "route_mismatch")
            self.assertEqual(saved["provider"], "route_mismatch")
            self.assertEqual(saved["finish_reason"], "unknown")

    def test_retry_topology_bumps_the_legacy_runtime_fingerprint(self):
        compiler = OpenRouterCompiler(api_key="runtime-test-key")

        self.assertNotEqual(
            compiler.fingerprint,
            "openai/gpt-5.4-mini:4ff0c033bb294d163400",
        )

    def test_usage_ledger_atomically_reserves_request_and_safety_headroom(self):
        ledger = ContentFreeUsageLedger(
            max_cost_usd=1.10,
            request_reservation_usd=0.05,
            safety_reserve_usd=1.0,
        )

        self.assertTrue(ledger.reserve_request())
        self.assertTrue(ledger.reserve_request())
        self.assertFalse(ledger.reserve_request())
        self.assertAlmostEqual(ledger.summary()["reserved_cost_usd"], 0.10)
        ledger.release_request()
        self.assertTrue(ledger.reserve_request())

    def test_http_protocol_failures_are_retryable_content_free_attempts(self):
        secret = "private-http-protocol-fragment"
        failures = (
            http.client.IncompleteRead(secret.encode("utf-8")),
            http.client.BadStatusLine(secret),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "usage.jsonl"
                    ledger = ContentFreeUsageLedger(
                        path,
                        max_cost_usd=0.10,
                        request_reservation_usd=0.01,
                    )
                    compiler = OpenRouterCompiler(
                        OpenRouterCompilerConfig(
                            model="openai/gpt-5.4-mini",
                            provider="Azure",
                            max_attempts=1,
                            transport_max_attempts=1,
                        ),
                        api_key="runtime-test-key",
                        transport=CapturingTransport([failure]),
                        usage_sink=ledger,
                    )

                    with self.assertRaises(CompilerTransportError) as raised:
                        compiler.compile_session(session_input())

                    self.assertEqual(raised.exception.code, "http_protocol_error")
                    self.assertTrue(raised.exception.retryable)
                    encoded = path.read_text(encoding="utf-8")
                    self.assertNotIn(secret, encoded)
                    saved = json.loads(encoded)
                    self.assertEqual(saved["code"], "http_protocol_error")
                    self.assertTrue(saved["unknown_cost"])
                    self.assertEqual(saved["cost_usd"], 0.01)
                    self.assertEqual(ledger.summary()["unknown_cost_attempts"], 1)

    def test_one_hundred_unknown_compiler_attempts_exhaust_cap(self):
        ledger = ContentFreeUsageLedger(max_cost_usd=1.0)
        error = CompilerTransportError(
            "transport failed",
            code="http_protocol_error",
            retryable=True,
        )

        for attempt in range(1, 101):
            self.assertTrue(ledger.reserve_request(), attempt)
            ledger.record_error(
                error,
                request_model="z-ai/glm-5.2",
                attempt=attempt,
            )
            ledger.release_request()

        summary = ledger.summary()
        self.assertEqual(summary["unknown_cost_attempts"], 100)
        self.assertAlmostEqual(summary["cost_usd"], 1.0)
        self.assertFalse(ledger.can_start_request())
        self.assertFalse(ledger.reserve_request())

    def test_output_token_parameter_is_persisted_fingerprinted_and_sent_exactly(self):
        configured = CompilerConfig.openrouter(output_token_parameter="max_tokens")
        serialized = configured.to_dict()
        self.assertEqual(serialized["output_token_parameter"], "max_tokens")
        self.assertEqual(CompilerConfig.from_dict(serialized), configured)

        legacy = dict(serialized)
        legacy.pop("output_token_parameter")
        restored_legacy = CompilerConfig.from_dict(legacy)
        self.assertEqual(
            restored_legacy.output_token_parameter, "max_completion_tokens"
        )
        old_payload = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
        old_digest = hashlib.sha256(old_payload.encode("utf-8")).hexdigest()
        self.assertEqual(
            restored_legacy.fingerprint,
            f"openrouter:{old_digest[:20]}",
        )
        self.assertNotEqual(configured.fingerprint, restored_legacy.fingerprint)

        transport = CapturingTransport([completion_response(cost=None)])
        compiler = compiler_from_project_config(
            configured,
            api_key="runtime-test-key",
            transport=transport,
        )
        default_compiler = compiler_from_project_config(
            restored_legacy,
            api_key="runtime-test-key",
            transport=CapturingTransport([completion_response(cost=None)]),
        )
        self.assertNotEqual(compiler.fingerprint, default_compiler.fingerprint)
        compiler.compile_session(session_input())
        request = transport.calls[0]["payload"]
        self.assertEqual(request["max_tokens"], 8192)
        self.assertNotIn("max_completion_tokens", request)

    def test_output_token_parameter_has_a_strict_allowlist(self):
        for value in ("max_new_tokens", "MAX_TOKENS", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CompilerConfig.openrouter(output_token_parameter=value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                LocalOpenAICompilerConfig(
                    base_url="http://localhost:11434/v1",
                    model="local-memory-model",
                    output_token_parameter=value,
                )
            with self.subTest(value=value), self.assertRaises(ValueError):
                OpenRouterCompilerConfig(
                    model="openai/gpt-5.4-mini",
                    provider="Azure",
                    output_token_parameter=value,
                )

    def test_schema_failure_is_retryable_and_second_attempt_is_repaired(self):
        invalid = compiled_payload()
        del invalid["claims"][0]["predicate"]
        transport = CapturingTransport(
            [completion_response(invalid), completion_response(compiled_payload())]
        )
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=transport,
            sleep=lambda _seconds: None,
        )

        result = compiler.compile_session(session_input())

        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(transport.calls), 2)
        repair_prompt = transport.calls[1]["payload"]["messages"][0]["content"]
        self.assertIn("invalid_compiled_memory", repair_prompt)
        self.assertNotIn("I moved to Osaka.", repair_prompt)
        self.assertEqual(
            result.memory.claims[0].evidence[0].quote, "I moved to Kyoto on 2025-01-02."
        )

    def test_incorrect_model_offsets_are_resolved_from_the_exact_quote(self):
        payload = compiled_payload()
        payload["summary"]["evidence"][0].update({"start": 99, "end": 100})
        payload["entities"][0]["evidence"][0].update({"start": 99, "end": 100})
        payload["claims"][0]["evidence"][0].update({"start": 99, "end": 100})
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=CapturingTransport([completion_response(payload)]),
        )

        result = compiler.compile_session(session_input())

        expected_end = len("I moved to Kyoto on 2025-01-02.")
        self.assertEqual(result.memory.summary.evidence[0].start, 0)
        self.assertEqual(result.memory.summary.evidence[0].end, expected_end)
        self.assertEqual(result.memory.claims[0].evidence[0].start, 0)
        self.assertEqual(result.memory.claims[0].evidence[0].end, expected_end)

    def test_paraphrased_summary_evidence_rebinds_to_grounded_claim(self):
        payload = compiled_payload()
        payload["summary"]["evidence"][0]["quote"] = "The user now lives in Kyoto."
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=CapturingTransport([completion_response(payload)]),
        )

        result = compiler.compile_session(session_input())

        self.assertEqual(result.attempts, 1)
        self.assertEqual(
            result.memory.summary.evidence,
            result.memory.claims[0].evidence,
        )
        self.assertEqual(
            result.memory.summary.evidence[0].quote,
            "I moved to Kyoto on 2025-01-02.",
        )

    def test_one_ungrounded_claim_is_dropped_without_losing_grounded_output(self):
        payload = compiled_payload()
        unsupported = deepcopy(payload["claims"][0])
        unsupported["claim_id"] = "c2"
        unsupported["memory_key"] = ""
        unsupported["evidence"][0]["quote"] = "The user moved to Osaka."
        payload["claims"].append(unsupported)
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=CapturingTransport([completion_response(payload)]),
        )

        result = compiler.compile_session(session_input())

        self.assertEqual(result.attempts, 1)
        self.assertEqual([claim.claim_id for claim in result.memory.claims], ["c1"])
        self.assertTrue(result.memory.claims[0].evidence)

    def test_duplicate_memory_key_forms_an_intra_session_update_timeline(self):
        payload = compiled_payload()
        replacement = deepcopy(payload["claims"][0])
        replacement["claim_id"] = "c2"
        replacement["text"] = "The user considers Kyoto home now."
        replacement["predicate"] = "considers home"
        replacement["evidence"][0]["quote"] = "Kyoto is home now."
        replacement["document_time"] = "2025-02-03T12:00:00Z"
        replacement["event_start"] = "2025-02-02"
        replacement["valid_from"] = "2025-02-02"
        payload["claims"].append(replacement)
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=CapturingTransport([completion_response(payload)]),
        )

        result = compiler.compile_session(session_input())

        self.assertEqual(
            [claim.status for claim in result.memory.claims],
            ["superseded", "active"],
        )
        self.assertEqual(result.memory.claims[0].valid_to, "2025-02-02")
        self.assertIsNone(result.memory.claims[1].valid_to)

    def test_exact_duplicate_claim_keeps_the_last_model_record(self):
        payload = compiled_payload()
        duplicate = deepcopy(payload["claims"][0])
        duplicate["claim_id"] = "c2"
        payload["claims"].append(duplicate)
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=CapturingTransport([completion_response(payload)]),
        )

        result = compiler.compile_session(session_input())

        self.assertEqual([claim.claim_id for claim in result.memory.claims], ["c2"])
        self.assertEqual(result.memory.claims[0].status, "active")

    def test_schema_subset_duplicates_and_invalid_optional_time_are_salvaged(self):
        payload = compiled_payload()
        payload["entities"][0]["aliases"] = ["Kyoto", "Kyoto"]
        payload["claims"][0]["entity_ids"] = ["e1", "e1"]
        payload["claims"][0]["event_end"] = "not-a-date"
        duplicate_entity = deepcopy(payload["entities"][0])
        payload["entities"].append(duplicate_entity)
        duplicate_claim = deepcopy(payload["claims"][0])
        payload["claims"].append(duplicate_claim)
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=CapturingTransport([completion_response(payload)]),
        )

        result = compiler.compile_session(session_input())

        self.assertEqual(len(result.memory.entities), 1)
        self.assertEqual(result.memory.entities[0].aliases, ("Kyoto",))
        self.assertEqual(len(result.memory.claims), 1)
        self.assertEqual(result.memory.claims[0].entity_ids, ("e1",))
        self.assertIsNone(result.memory.claims[0].event_end)

    def test_fully_ungrounded_derived_output_degrades_to_raw_only(self):
        payload = compiled_payload(quote="The user moved to Osaka.")
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=CapturingTransport([completion_response(payload)]),
        )

        result = compiler.compile_session(session_input())

        self.assertEqual(result.memory.summary.text, "")
        self.assertEqual(result.memory.summary.evidence, ())
        self.assertEqual(result.memory.claims, ())
        self.assertEqual(result.memory.entities, ())

    def test_exhausted_schema_failure_exposes_retry_metadata_and_usage(self):
        invalid = compiled_payload()
        del invalid["claims"][0]["predicate"]
        transport = CapturingTransport(
            [completion_response(invalid), completion_response(invalid)]
        )
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=transport,
            sleep=lambda _seconds: None,
        )

        with self.assertRaises(CompilerResponseError) as raised:
            compiler.compile_session(session_input())

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.code, "invalid_compiled_memory")
        self.assertEqual(len(raised.exception.usage), 2)

    def test_route_mismatch_is_non_retryable(self):
        transport = CapturingTransport(
            [completion_response(provider="AnotherProvider"), completion_response()]
        )
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=transport,
            sleep=lambda _seconds: None,
        )

        with self.assertRaises(CompilerResponseError) as raised:
            compiler.compile_session(session_input())

        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.code, "provider_route_mismatch")

    def test_provider_content_filter_is_nonretryable_and_keeps_usage(self):
        response = completion_response()
        response["choices"][0]["finish_reason"] = "content_filter"
        transport = CapturingTransport([response, completion_response()])
        compiler = OpenRouterCompiler(
            api_key="runtime-test-key",
            transport=transport,
            sleep=lambda _seconds: None,
        )

        with self.assertRaises(CompilerResponseError) as raised:
            compiler.compile_session(session_input())

        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.code, "content_filtered")
        self.assertEqual(len(raised.exception.usage), 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(transport.calls), 1)

    def test_usage_ledger_is_content_free_resumable_and_guards_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compiler-usage.jsonl"
            ledger = ContentFreeUsageLedger(path, max_cost_usd=0.002)
            transport = CapturingTransport([completion_response(cost=0.002)])
            compiler = OpenRouterCompiler(
                api_key="runtime-test-key",
                transport=transport,
                usage_sink=ledger,
                sleep=lambda _seconds: None,
            )

            compiler.compile_session(session_input())
            saved = path.read_text(encoding="utf-8")

            self.assertNotIn(SOURCE_TEXT, saved)
            self.assertNotIn("runtime-test-key", saved)
            self.assertNotIn("session-1", saved)
            self.assertEqual(ledger.summary()["events"], 1)
            self.assertFalse(ledger.can_start_request())
            resumed = ContentFreeUsageLedger(path, max_cost_usd=0.002)
            self.assertAlmostEqual(resumed.summary()["cost_usd"], 0.002)
            with self.assertRaises(CompilerBudgetExceededError):
                OpenRouterCompiler(
                    api_key="runtime-test-key",
                    transport=CapturingTransport([completion_response()]),
                    usage_sink=resumed,
                ).compile_session(session_input())

    def test_usage_ledger_rejects_nonfinite_cost_fuses(self):
        for invalid in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    ContentFreeUsageLedger(max_cost_usd=invalid)

    def test_nonfinite_and_huge_usage_metadata_is_safely_ledgered(self):
        for malformed in (float("inf"), float("nan"), 10**100):
            with self.subTest(malformed=malformed):
                response = completion_response(cost=None)
                response["usage"]["prompt_tokens"] = malformed
                response["usage"]["completion_tokens"] = malformed
                response["usage"]["cost"] = malformed
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "usage.jsonl"
                    ledger = ContentFreeUsageLedger(path, max_cost_usd=1.0)
                    result = OpenRouterCompiler(
                        api_key="runtime-test-key",
                        transport=CapturingTransport([response]),
                        usage_sink=ledger,
                    ).compile_session(session_input())

                    self.assertEqual(result.usage[0].prompt_tokens, 0)
                    self.assertEqual(result.usage[0].completion_tokens, 0)
                    self.assertEqual(result.usage[0].cost_usd, 0.01)
                    self.assertTrue(result.usage[0].unknown_cost)
                    self.assertEqual(ledger.summary()["attempts"], 1)
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(saved["prompt_tokens"], 0)
                    self.assertEqual(saved["completion_tokens"], 0)
                    self.assertEqual(saved["cost_usd"], 0.01)
                    self.assertTrue(saved["unknown_cost"])

    def test_hosted_key_is_runtime_only_and_required(self):
        transport = CapturingTransport([completion_response()])
        with patch.dict(os.environ, {}, clear=True):
            compiler = OpenRouterCompiler(transport=transport)
            with self.assertRaises(CompilerConfigurationError) as raised:
                compiler.compile_session(session_input())

        self.assertEqual(raised.exception.code, "missing_api_key")
        self.assertEqual(transport.calls, [])

    def test_project_config_factory_dispatches_and_enforces_safe_hosted_settings(self):
        local_transport = CapturingTransport(
            [completion_response(model="memory-model", provider="", cost=None)]
        )
        local = compiler_from_project_config(
            CompilerConfig.local(
                model="memory-model", endpoint="http://localhost:8080/v1"
            ),
            transport=local_transport,
        )
        self.assertIsInstance(local, LocalOpenAICompiler)
        local.compile_session(session_input())

        hosted = compiler_from_project_config(
            CompilerConfig.openrouter(),
            api_key="runtime-test-key",
            transport=CapturingTransport([completion_response()]),
        )
        self.assertIsInstance(hosted, OpenRouterCompiler)
        luna = CompilerConfig.openrouter(model="openai/gpt-5.6-luna-pro")
        self.assertEqual(luna.provider, "Azure")
        self.assertEqual(luna.reasoning, "low")

        mini = compiler_from_project_config(
            CompilerConfig.openrouter(model="openai/gpt-5.4-mini"),
            api_key="runtime-test-key",
            transport=CapturingTransport(
                [
                    completion_response(
                        model="openai/gpt-5.4-mini",
                        provider="Azure",
                        cost=None,
                    )
                ]
            ),
        )
        mini_result = mini.compile_session(session_input())
        self.assertEqual(mini_result.usage[0].cost_source, "estimated")
        self.assertAlmostEqual(mini_result.total_cost_usd, 0.000165)

        with self.assertRaisesRegex(CompilerConfigurationError, "requires a loopback"):
            compiler_from_project_config(CompilerConfig.local(model="memory-model"))
        with self.assertRaisesRegex(CompilerConfigurationError, "zero-data-retention"):
            compiler_from_project_config(
                CompilerConfig(
                    kind="openrouter",
                    model="openai/gpt-5.6-luna-pro",
                    provider="Azure",
                    zero_data_retention=False,
                )
            )

    def test_project_config_factory_does_not_store_or_require_key_during_construction(
        self,
    ):
        with patch.dict(os.environ, {}, clear=True):
            compiler = compiler_from_project_config(
                CompilerConfig.openrouter(),
                transport=CapturingTransport([completion_response()]),
            )
        self.assertIsInstance(compiler, OpenRouterCompiler)

    def test_openrouter_endpoint_cannot_be_redirected_to_another_host(self):
        with self.assertRaises(ValueError):
            OpenRouterCompilerConfig(
                model="openai/gpt-5.6-luna-pro",
                provider="Azure",
                endpoint="https://example.com/v1/chat/completions",
            )


if __name__ == "__main__":
    unittest.main()
