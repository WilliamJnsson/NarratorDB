import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from narratordb.benchmark_server import (
    _compiler_config_from_args,
    build_argument_parser,
)
from narratordb.compiler import (
    OPENAI_CHAT_COMPLETIONS_URL,
    CompileSessionInput,
    CompilerConfigurationError,
    CompilerResponseError,
    ContentFreeUsageLedger,
    OpenAICompiler,
    OpenAICompilerConfig,
    SourceMessage,
    compiler_from_project_config,
)
from narratordb.config import CompilerConfig, ConfigurationError


SOURCE_TEXT = "I moved to Kyoto."


def _session() -> CompileSessionInput:
    return CompileSessionInput(
        session_id="session-1",
        messages=(
            SourceMessage(
                message_id="message-1",
                role="user",
                content=SOURCE_TEXT,
            ),
        ),
    )


def _compiled_payload() -> dict:
    evidence = {
        "message_id": "message-1",
        "quote": SOURCE_TEXT,
        "start": None,
        "end": None,
    }
    return {
        "summary": {"text": SOURCE_TEXT, "evidence": [deepcopy(evidence)]},
        "entities": [],
        "claims": [
            {
                "claim_id": "c1",
                "kind": "event",
                "text": SOURCE_TEXT,
                "subject": "the user",
                "predicate": "moved to",
                "object_text": "Kyoto",
                "memory_key": "user.residence.current_city",
                "confidence": 1.0,
                "status": "active",
                "document_time": None,
                "event_start": None,
                "event_end": None,
                "valid_from": None,
                "valid_to": None,
                "entity_ids": [],
                "evidence": [deepcopy(evidence)],
            }
        ],
        "relations": [],
    }


def _response(
    *,
    model: str = "gpt-5.4-mini-2026-03-17",
    prompt_tokens: int = 100,
    cached_tokens: int = 20,
    completion_tokens: int = 10,
) -> dict:
    return {
        "model": model,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": json.dumps(_compiled_payload())},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
            "completion_tokens_details": {"reasoning_tokens": 4},
        },
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
            }
        )
        return self.responses.pop(0)


class OfficialOpenAICompilerTests(unittest.TestCase):
    def test_first_party_endpoint_key_schema_and_usage_ledger(self) -> None:
        transport = CapturingTransport([_response()])
        with tempfile.TemporaryDirectory() as directory:
            ledger = ContentFreeUsageLedger(Path(directory) / "usage.jsonl")
            compiler = OpenAICompiler(
                api_key="runtime-openai-key",
                transport=transport,
                usage_sink=ledger,
            )

            result = compiler.compile_session(_session())

            request = transport.calls[0]
            self.assertEqual(request["endpoint"], OPENAI_CHAT_COMPLETIONS_URL)
            self.assertEqual(
                request["headers"]["Authorization"], "Bearer runtime-openai-key"
            )
            self.assertEqual(request["payload"]["model"], "gpt-5.4-mini")
            self.assertEqual(request["payload"]["reasoning_effort"], "low")
            self.assertEqual(
                request["payload"]["response_format"]["type"], "json_schema"
            )
            self.assertTrue(
                request["payload"]["response_format"]["json_schema"]["strict"]
            )
            self.assertNotIn("provider", request["payload"])
            self.assertNotIn("seed", request["payload"])
            usage = result.usage[0]
            self.assertEqual(usage.provider, "openai")
            self.assertEqual(usage.response_model, "gpt-5.4-mini-2026-03-17")
            self.assertEqual(usage.cached_tokens, 20)
            self.assertEqual(usage.reasoning_tokens, 4)
            self.assertEqual(usage.cost_source, "estimated")
            self.assertAlmostEqual(usage.cost_usd, 0.0001065)
            ledger_text = ledger.path.read_text(encoding="utf-8")
            self.assertNotIn(SOURCE_TEXT, ledger_text)
            self.assertNotIn("runtime-openai-key", ledger_text)

    def test_luna_standard_and_long_context_pricing(self) -> None:
        transport = CapturingTransport(
            [
                _response(
                    model="gpt-5.6-luna-2026-07-01",
                    prompt_tokens=272_000,
                    cached_tokens=72_000,
                    completion_tokens=1_000,
                ),
                _response(
                    model="gpt-5.6-luna-2026-07-01",
                    prompt_tokens=272_001,
                    cached_tokens=72_000,
                    completion_tokens=1_000,
                ),
            ]
        )
        compiler = OpenAICompiler(
            OpenAICompilerConfig(model="gpt-5.6-luna"),
            api_key="runtime-openai-key",
            transport=transport,
        )

        standard = compiler.compile_session(_session()).usage[0]
        long_context = compiler.compile_session(_session()).usage[0]

        self.assertEqual(transport.calls[0]["payload"]["model"], "gpt-5.6-luna")
        self.assertEqual(standard.response_model, "gpt-5.6-luna-2026-07-01")
        self.assertEqual(standard.cost_source, "estimated")
        self.assertAlmostEqual(standard.cost_usd, 0.2132)
        self.assertEqual(long_context.cost_source, "estimated")
        self.assertAlmostEqual(long_context.cost_usd, 0.423402)

    def test_factory_uses_openai_key_and_rejects_model_mismatch(self) -> None:
        config = CompilerConfig.openai(
            model="gpt-5",
            reasoning="high",
            semantic_max_attempts=1,
            transport_max_attempts=1,
        )
        transport = CapturingTransport([_response(model="gpt-5-mini")])
        compiler = compiler_from_project_config(
            config,
            api_key="runtime-openai-key",
            transport=transport,
        )

        with self.assertRaises(CompilerResponseError) as raised:
            compiler.compile_session(_session())

        self.assertEqual(raised.exception.code, "model_route_mismatch")
        self.assertEqual(raised.exception.usage[0].response_model, "route_mismatch")
        self.assertEqual(transport.calls[0]["payload"]["reasoning_effort"], "high")

    def test_key_and_endpoint_are_closed_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "endpoint is fixed"):
            OpenAICompilerConfig(endpoint="https://example.com/v1/chat/completions")
        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            OpenAICompilerConfig(api_key_env="OPENROUTER_API_KEY")
        with self.assertRaises(ConfigurationError):
            CompilerConfig(
                kind="openai",
                model="gpt-5",
                provider="OpenAI",
                reasoning="low",
                seed=None,
                zero_data_retention=False,
                data_collection="allow",
            )
        with patch.dict("os.environ", {}, clear=True):
            compiler = OpenAICompiler(transport=CapturingTransport([_response()]))
            with self.assertRaises(CompilerConfigurationError) as raised:
                compiler.compile_session(_session())
        self.assertEqual(raised.exception.code, "missing_api_key")

    def test_benchmark_cli_accepts_official_openai_and_rejects_router_flags(self) -> None:
        parser = build_argument_parser()
        args = parser.parse_args(
            [
                "--mode",
                "intelligence",
                "--compiler",
                "openai",
                "--model",
                "gpt-5",
                "--reasoning",
                "high",
                "--compiler-max-cost-usd",
                "30",
            ]
        )
        config = _compiler_config_from_args(args, parser)
        self.assertEqual(config.kind.value, "openai")
        self.assertEqual(config.model, "gpt-5")
        self.assertEqual(config.reasoning, "high")
        self.assertFalse(config.zero_data_retention)
        self.assertEqual(config.data_collection, "allow")

        for extra in (
            ["--provider", "OpenAI"],
            ["--provider-allow", "OpenAI"],
            ["--compiler-capture-router-metadata"],
            ["--endpoint", "https://api.openai.com/v1/chat/completions"],
        ):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                invalid = parser.parse_args(
                    [
                        "--mode",
                        "intelligence",
                        "--compiler",
                        "openai",
                        "--compiler-max-cost-usd",
                        "30",
                        *extra,
                    ]
                )
                _compiler_config_from_args(invalid, parser)

        no_cap = parser.parse_args(
            ["--mode", "intelligence", "--compiler", "openai"]
        )
        with self.assertRaises(SystemExit):
            _compiler_config_from_args(no_cap, parser)


if __name__ == "__main__":
    unittest.main()
