#!/usr/bin/env python3
"""Offline handler/state-machine tests for the sealed r2 transport proxy."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MODEL = "openai/gpt-5.4-mini"


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType("narratordb")
package.__path__ = [str(ROOT / "narratordb")]
benchmarks = types.ModuleType("narratordb.benchmarks")
benchmarks.__path__ = [str(ROOT / "narratordb/benchmarks")]
sys.modules["narratordb"] = package
sys.modules["narratordb.benchmarks"] = benchmarks
_load("narratordb.config", ROOT / "narratordb/config.py")
PROXY = _load("narratordb.benchmarks.openrouter_proxy", HERE / "openrouter_proxy_r2.py")
CANARY = _load("v18_r2_route_canary", HERE / "route_canary.py")


def good_response(content: str = "OK", *, cost: float | None = 0.002) -> dict:
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 6,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 2},
    }
    if cost is not None:
        usage["cost"] = cost
    return {
        "model": MODEL,
        "provider": "OpenAI",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def empty_response(*, finish_reason: str | None = None) -> dict:
    choice = {
        "message": {"role": "assistant", "content": ""},
    }
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {
        "model": MODEL,
        "provider": "OpenAI",
        "choices": [choice],
    }


class UpstreamState:
    def __init__(self) -> None:
        self.responses: list[tuple[int, bytes]] = []
        self.calls: list[bytes] = []
        self.lock = threading.Lock()
        self.first_call_entered = threading.Event()
        self.first_call_release: threading.Event | None = None

    def push(self, status: int, value: dict | bytes) -> None:
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.responses.append((status, body))


def upstream_handler(state: UpstreamState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            with state.lock:
                state.calls.append(body)
                call_number = len(state.calls)
                status, response = state.responses.pop(0)
            if call_number == 1 and state.first_call_release is not None:
                state.first_call_entered.set()
                state.first_call_release.wait(timeout=5)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    return Handler


class ProxyHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.usage_path = Path(self.temporary.name) / "usage.jsonl"
        self.state = UpstreamState()
        self.upstream = ThreadingHTTPServer(
            ("127.0.0.1", 0), upstream_handler(self.state)
        )
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()
        self.addCleanup(self._stop_upstream)
        self.ledger = PROXY.UsageLedger(
            self.usage_path,
            2.45,
            request_reservation_usd=0.05,
            safety_reserve_usd=0.01,
        )
        handler = PROXY.make_handler(
            api_key="synthetic-upstream-key",
            upstream=(
                f"http://127.0.0.1:{self.upstream.server_port}/chat/completions"
            ),
            provider_only="OpenAI",
            model_routes={MODEL: ("OpenAI",)},
            model_output_token_parameters={},
            model_omit_temperature=(MODEL,),
            model_reasoning_efforts={MODEL: "high"},
            reasoning_effort=None,
            public_benchmark=True,
            ledger=self.ledger,
            timeout=105.0,
            max_request_bytes=20 * 1024 * 1024,
            max_response_bytes=4 * 1024 * 1024,
            allow_insecure_test_upstream=True,
        )
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        self.addCleanup(self._stop_proxy)
        self.base = f"http://127.0.0.1:{self.proxy.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def _stop_proxy(self) -> None:
        self.proxy.shutdown()
        self.proxy.server_close()
        self.proxy_thread.join(timeout=5)

    def _stop_upstream(self) -> None:
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=5)

    @staticmethod
    def payload() -> bytes:
        return json.dumps(
            {
                "model": MODEL,
                "stream": False,
                "messages": [{"role": "user", "content": "synthetic prompt"}],
                "max_tokens": 32,
            }
        ).encode()

    def request(
        self,
        *,
        body: bytes | None = None,
        authorization: str = "Bearer local-transport",
        retry_count: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json",
        }
        if retry_count is not None:
            headers["x-stainless-retry-count"] = retry_count
        request = Request(
            self.base + "/v1/chat/completions",
            data=self.payload() if body is None else body,
            headers=headers,
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def health(self) -> dict:
        with self.opener.open(self.base + "/health", timeout=5) as response:
            return json.loads(response.read())

    def events(self) -> list[dict]:
        if not self.usage_path.exists():
            return []
        return [json.loads(line) for line in self.usage_path.read_text().splitlines()]

    def test_good_response_is_forwarded_unchanged_with_exact_metadata(self) -> None:
        self.state.push(200, good_response())
        status, headers, raw = self.request()
        self.assertEqual(status, 200)
        self.assertNotIn("x-should-retry", {key.lower() for key in headers})
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "OK")
        event = self.events()[0]
        self.assertEqual(event["event"], "completion")
        self.assertTrue(event["response_forwarded"])
        self.assertEqual(event["attempt_number"], 1)
        health = self.health()
        self.assertEqual(health["upstream_timeout_seconds"], 105.0)
        self.assertTrue(health["direct_upstream_networking"])
        self.assertTrue(health["local_caller_auth_required"])
        self.assertEqual(health["inbound_retry_count_policy"], "absent-or-zero-only")

    def test_http200_json_non_object_is_terminal_protocol_error(self) -> None:
        self.state.push(200, b"[]")
        self.state.push(200, good_response())
        status, headers, _ = self.request()
        self.assertEqual(status, 502)
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(normalized_headers.get("x-should-retry"), "false")
        event = self.events()[0]
        self.assertEqual(event["event"], "terminal_rejection")
        self.assertEqual(event["discarded_reason"], "upstream_protocol_error")
        self.assertFalse(event["retryable"])
        self.assertTrue(self.ledger.summary()["transport_failed"])

        second_status, _, _ = self.request()
        self.assertEqual(second_status, 503)
        self.assertEqual(len(self.state.calls), 1)

    def test_empty_then_success_uses_same_logical_chain_attempt_two(self) -> None:
        self.state.push(
            200, {"model": MODEL, "provider": "OpenAI", "choices": []}
        )
        self.state.push(200, good_response())
        status, headers, _ = self.request(retry_count="0")
        self.assertEqual(status, 502)
        self.assertEqual(headers.get("x-should-retry"), "false")
        self.assertEqual(self.request(retry_count="0")[0], 200)
        first, second = self.events()
        self.assertEqual(first["event"], "discarded_transient")
        self.assertEqual([first["attempt_number"], second["attempt_number"]], [1, 2])
        self.assertEqual(first["logical_call_id"], second["logical_call_id"])
        self.assertEqual(
            first["request_payload_sha256"], second["request_payload_sha256"]
        )

    def test_four_discards_then_fifth_success_and_fifth_discard_fuse(self) -> None:
        for _ in range(4):
            self.state.push(200, empty_response())
        self.state.push(200, good_response())
        for expected in (502, 502, 502, 502, 200):
            self.assertEqual(self.request()[0], expected)
        self.assertEqual([event["attempt_number"] for event in self.events()], [1, 2, 3, 4, 5])
        self.assertFalse(self.health()["usage"]["transport_failed"])

    def test_fifth_discard_hard_fuses_every_future_provider_call(self) -> None:
        for _ in range(5):
            self.state.push(200, empty_response())
        for _ in range(5):
            self.assertEqual(self.request()[0], 502)
        self.assertEqual(len(self.state.calls), 5)
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(len(self.state.calls), 5)
        health = self.health()["usage"]
        self.assertTrue(health["transport_failed"])
        self.assertEqual(health["discarded_transients"], 5)

    def test_completed_same_payload_starts_a_new_ordinal_chain(self) -> None:
        self.state.push(200, good_response())
        self.state.push(200, good_response())
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request()[0], 200)
        first, second = self.events()
        self.assertEqual(first["request_payload_sha256"], second["request_payload_sha256"])
        self.assertNotEqual(first["logical_call_id"], second["logical_call_id"])
        payload = first["request_payload_sha256"]
        self.assertEqual(
            [first["logical_call_id"], second["logical_call_id"]],
            [
                hashlib.sha256(f"{payload}:1".encode()).hexdigest(),
                hashlib.sha256(f"{payload}:2".encode()).hexdigest(),
            ],
        )

    def test_concurrent_identical_payload_fuses_before_second_upstream(self) -> None:
        self.state.push(200, good_response())
        self.state.first_call_release = threading.Event()
        first_result: list[tuple[int, dict[str, str], bytes]] = []
        first = threading.Thread(target=lambda: first_result.append(self.request()))
        first.start()
        self.assertTrue(self.state.first_call_entered.wait(timeout=3))
        second = self.request()
        self.assertEqual(second[0], 503)
        self.assertEqual(len(self.state.calls), 1)
        self.state.first_call_release.set()
        first.join(timeout=5)
        self.assertEqual(first_result[0][0], 200)
        self.assertTrue(self.health()["usage"]["transport_failed"])

    def test_hidden_sdk_retry_header_is_audited_and_persistently_fused(self) -> None:
        status, headers, _ = self.request(retry_count="1")
        self.assertEqual(status, 400)
        self.assertEqual(headers.get("x-should-retry"), "false")
        self.assertEqual(len(self.state.calls), 0)
        health = self.health()["usage"]
        self.assertTrue(health["transport_failed"])
        self.assertEqual(health["hidden_sdk_retry_rejections"], 1)
        self.assertEqual(self.request(retry_count="0")[0], 503)
        self.assertEqual(len(self.state.calls), 0)

    def test_auth_duplicate_and_nan_reject_before_provider_without_echo(self) -> None:
        cases = (
            {"authorization": "Bearer wrong"},
            {"body": b'{"model":"x","model":"y"}'},
            {"body": b'{"model":NaN}'},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                status, headers, raw = self.request(**kwargs)
                self.assertIn(status, {400, 401})
                self.assertEqual(headers.get("x-should-retry"), "false")
                self.assertNotIn(b"synthetic prompt", raw)
        self.assertEqual(len(self.state.calls), 0)

    def test_retryable_upstream_429_books_known_cost_and_outer_retry(self) -> None:
        self.state.push(429, {"error": {"code": 429}, "usage": {"cost": 0.07}})
        self.state.push(200, good_response())
        status, headers, _ = self.request()
        self.assertEqual(status, 429)
        self.assertEqual(headers.get("x-should-retry"), "false")
        self.assertEqual(self.request()[0], 200)
        first = self.events()[0]
        self.assertEqual(first["event"], "discarded_transient")
        self.assertFalse(first["unknown_cost"])
        self.assertEqual(first["cost_usd"], 0.07)

    def test_prompt_and_secret_shaped_text_never_enter_ledger_or_error(self) -> None:
        secret = "sk-or-synthetic-never-retain-123456789"
        body = json.dumps(
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": secret}],
                "max_tokens": 32,
            }
        ).encode()
        self.state.push(200, empty_response())
        _, _, raw = self.request(body=body)
        ledger = self.usage_path.read_bytes()
        self.assertNotIn(secret.encode(), ledger)
        self.assertNotIn(secret.encode(), raw)

    def test_route_canary_requires_exact_323_yes_and_clean_attempts(self) -> None:
        self.state.push(200, good_response("323"))
        self.state.push(200, good_response("YES"))
        output = Path(self.temporary.name) / "canary.json"
        with patch.object(
            sys,
            "argv",
            [
                "route_canary.py",
                "--base-url",
                self.base + "/v1",
                "--output",
                str(output),
            ],
        ):
            self.assertEqual(CANARY.main(), 0)
        document = json.loads(output.read_text())
        self.assertTrue(document["complete"])
        self.assertTrue(all(call["output_exact"] for call in document["calls"]))
        events = self.events()
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event["event"] == "completion" for event in events))
        self.assertTrue(all(event["attempt_number"] == 1 for event in events))

    def test_route_canary_rejects_wrong_exact_output(self) -> None:
        self.state.push(200, good_response("324"))
        output = Path(self.temporary.name) / "wrong-canary.json"
        with patch.object(
            sys,
            "argv",
            [
                "route_canary.py",
                "--base-url",
                self.base + "/v1",
                "--output",
                str(output),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "answerer canary incomplete"):
                CANARY.main()
        self.assertFalse(output.exists())


class LedgerTerminalTests(unittest.TestCase):
    def ledger_record(self, response: dict, *, status: int = 200) -> tuple[dict, dict]:
        ledger = PROXY.UsageLedger(None, 2.45, request_reservation_usd=0.05)
        payload = "a" * 64
        attempt = ledger.start_attempt(payload)
        assert attempt is not None
        event = ledger.record(
            response,
            MODEL,
            attempt=attempt,
            upstream_status=status,
            provider_routes=("OpenAI",),
        )
        return event, ledger.summary()

    def test_multichoice_201_unknown_cost_tool_and_nonstop_are_terminal(self) -> None:
        cases: list[tuple[dict, int, str]] = []
        multichoice = good_response()
        multichoice["choices"].append(multichoice["choices"][0])
        cases.append((multichoice, 200, "choice_count_mismatch"))
        invalid_choices = good_response()
        invalid_choices["choices"] = {}
        cases.append((invalid_choices, 200, "choice_schema_mismatch"))
        cases.append((good_response(), 201, "unexpected_http_success_status"))
        cases.append((good_response(cost=None), 200, "invalid_accounting"))
        tool = good_response()
        tool["choices"][0]["message"]["tool_calls"] = [{"id": "synthetic"}]
        cases.append((tool, 200, "unexpected_tool_call"))
        invalid_content = good_response()
        invalid_content["choices"][0]["message"]["content"] = 42
        cases.append((invalid_content, 200, "invalid_content_type"))
        nonstop = empty_response(finish_reason="length")
        cases.append((nonstop, 200, "non_stop_finish"))
        unknown_nonstop = empty_response(finish_reason="banana")
        cases.append((unknown_nonstop, 200, "non_stop_finish"))
        cases.append((empty_response(finish_reason=""), 200, "non_stop_finish"))
        cases.append((empty_response(finish_reason="  "), 200, "non_stop_finish"))
        for response, status, reason in cases:
            with self.subTest(reason=reason):
                event, summary = self.ledger_record(response, status=status)
                self.assertEqual(event["event"], "terminal_rejection")
                self.assertFalse(event["response_forwarded"])
                self.assertEqual(event["discarded_reason"], reason)
                self.assertTrue(summary["transport_failed"])

    def test_nested_response_shape_policy_is_table_driven_and_terminal(self) -> None:
        cases: list[tuple[str, dict, str]] = []

        message_missing = good_response()
        del message_missing["choices"][0]["message"]
        cases.append(("message_missing", message_missing, "message_schema_mismatch"))

        message_null = good_response()
        message_null["choices"][0]["message"] = None
        cases.append(("message_null", message_null, "message_schema_mismatch"))

        role_missing = good_response()
        del role_missing["choices"][0]["message"]["role"]
        cases.append(("candidate_role_missing", role_missing, "invalid_message_role"))

        role_wrong = good_response()
        role_wrong["choices"][0]["message"]["role"] = "user"
        cases.append(("candidate_role_wrong", role_wrong, "invalid_message_role"))

        empty_role_null = empty_response(finish_reason="stop")
        empty_role_null["choices"][0]["message"]["role"] = None
        cases.append(("empty_role_null", empty_role_null, "invalid_message_role"))

        invalid_content = good_response()
        invalid_content["choices"][0]["message"]["content"] = ["not", "text"]
        cases.append(("content_array", invalid_content, "invalid_content_type"))

        for value_name, value in (
            ("object", {}),
            ("empty_string", ""),
            ("false", False),
        ):
            tool = good_response()
            tool["choices"][0]["message"]["tool_calls"] = value
            cases.append((f"tool_calls_{value_name}", tool, "unexpected_tool_call"))

            function = good_response()
            function["choices"][0]["message"]["function_call"] = value
            cases.append(
                (f"function_call_{value_name}", function, "unexpected_tool_call")
            )

        finish_nonstring = empty_response()
        finish_nonstring["choices"][0]["finish_reason"] = 0
        cases.append(("finish_nonstring", finish_nonstring, "invalid_finish_reason"))

        for value_name, value in (
            ("text", "declined"),
            ("whitespace", " "),
            ("object", {}),
        ):
            refusal = good_response()
            refusal["choices"][0]["message"]["refusal"] = value
            cases.append((f"refusal_{value_name}", refusal, "unexpected_refusal"))

        prompt_details_string = good_response()
        prompt_details_string["usage"]["prompt_tokens_details"] = "invalid"
        cases.append(
            ("prompt_details_string", prompt_details_string, "invalid_accounting")
        )

        completion_details_array = good_response()
        completion_details_array["usage"]["completion_tokens_details"] = []
        cases.append(
            (
                "completion_details_array",
                completion_details_array,
                "invalid_accounting",
            )
        )

        cached_tokens_string = good_response()
        cached_tokens_string["usage"]["prompt_tokens_details"][
            "cached_tokens"
        ] = "0"
        cases.append(
            ("cached_tokens_string", cached_tokens_string, "invalid_accounting")
        )

        reasoning_tokens_bool = good_response()
        reasoning_tokens_bool["usage"]["completion_tokens_details"][
            "reasoning_tokens"
        ] = False
        cases.append(
            ("reasoning_tokens_bool", reasoning_tokens_bool, "invalid_accounting")
        )

        cost_string = good_response()
        cost_string["usage"]["cost"] = "0.002"
        cases.append(("cost_string", cost_string, "invalid_accounting"))

        for name, response, reason in cases:
            with self.subTest(name=name):
                event, summary = self.ledger_record(response)
                self.assertEqual(event["event"], "terminal_rejection")
                self.assertEqual(event["discarded_reason"], reason)
                self.assertFalse(event["retryable"])
                self.assertTrue(summary["transport_failed"])

    def test_exact_empty_and_no_call_shapes_are_the_only_allowed_shapes(self) -> None:
        empty_cases: list[tuple[str, dict]] = []

        content_missing = empty_response(finish_reason="stop")
        del content_missing["choices"][0]["message"]["content"]
        empty_cases.append(("content_missing", content_missing))

        content_null = empty_response(finish_reason="stop")
        content_null["choices"][0]["message"]["content"] = None
        empty_cases.append(("content_null", content_null))

        empty_role_missing = empty_response(finish_reason="stop")
        empty_role_missing["choices"][0]["message"].pop("role", None)
        empty_cases.append(("empty_role_missing", empty_role_missing))

        for name, response in empty_cases:
            with self.subTest(name=name):
                event, summary = self.ledger_record(response)
                self.assertEqual(event["event"], "discarded_transient")
                self.assertEqual(event["discarded_reason"], "empty_completion")
                self.assertTrue(event["retryable"])
                self.assertFalse(summary["transport_failed"])

        for field, value in (
            ("tool_calls", None),
            ("tool_calls", []),
            ("function_call", None),
            ("refusal", None),
            ("refusal", ""),
        ):
            with self.subTest(field=field, value=value):
                response = good_response()
                response["choices"][0]["message"][field] = value
                event, summary = self.ledger_record(response)
                self.assertEqual(event["event"], "completion")
                self.assertTrue(event["response_forwarded"])
                self.assertFalse(summary["transport_failed"])

        for field in ("prompt_tokens_details", "completion_tokens_details"):
            with self.subTest(nullable_usage_detail=field):
                response = good_response()
                response["usage"][field] = None
                event, summary = self.ledger_record(response)
                self.assertEqual(event["event"], "completion")
                self.assertTrue(event["response_forwarded"])
                self.assertFalse(summary["transport_failed"])

        for wrapper, field in (
            ("prompt_tokens_details", "cached_tokens"),
            ("completion_tokens_details", "reasoning_tokens"),
        ):
            with self.subTest(nullable_usage_counter=field):
                response = good_response()
                response["usage"][wrapper][field] = None
                event, summary = self.ledger_record(response)
                self.assertEqual(event["event"], "completion")
                self.assertEqual(event[field], 0)
                self.assertTrue(event["response_forwarded"])
                self.assertFalse(summary["transport_failed"])

    def test_zero_choice_strict_json_is_contentless_retryable(self) -> None:
        event, summary = self.ledger_record(
            {"model": MODEL, "provider": "OpenAI", "choices": []}
        )
        self.assertEqual(event["event"], "discarded_transient")
        self.assertEqual(event["discarded_reason"], "empty_completion")
        self.assertTrue(event["retryable"])
        self.assertFalse(summary["transport_failed"])
        self.assertEqual(summary["pending_logical_calls"], 1)

        event, summary = self.ledger_record(
            {"model": MODEL, "provider": "OpenAI"}
        )
        self.assertEqual(event["event"], "discarded_transient")
        self.assertEqual(event["discarded_reason"], "empty_completion")
        self.assertFalse(summary["transport_failed"])

    def test_concurrent_same_payload_attempt_fails_closed(self) -> None:
        ledger = PROXY.UsageLedger(None, 2.45, request_reservation_usd=0.05)
        payload = "b" * 64
        self.assertIsNotNone(ledger.start_attempt(payload))
        self.assertIsNone(ledger.start_attempt(payload))
        summary = ledger.summary()
        self.assertTrue(summary["transport_failed"])
        self.assertEqual(summary["active_logical_calls"], 1)


class SourceInvariantTests(unittest.TestCase):
    def test_direct_network_timeout_key_pop_and_retry_header_are_source_pinned(self) -> None:
        source = (HERE / "openrouter_proxy_r2.py").read_text(encoding="utf-8")
        for required in (
            "build_opener(ProxyHandler({}), _NoRedirectHandler())",
            "os.environ.pop(args.api_key_env",
            "_PINNED_UPSTREAM_TIMEOUT_SECONDS = 105.0",
            'self.headers.get("x-stainless-retry-count")',
            'self.headers.get("Authorization") != "Bearer local-transport"',
            '"x-should-retry", "true" if should_retry else "false"',
        ):
            self.assertIn(required, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
