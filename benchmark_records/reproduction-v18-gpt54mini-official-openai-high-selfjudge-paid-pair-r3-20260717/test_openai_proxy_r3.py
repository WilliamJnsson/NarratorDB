#!/usr/bin/env python3
"""Comprehensive offline tests for the prospective R3 official OpenAI proxy."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


HERE = Path(__file__).resolve().parent


def _load_proxy():
    path = HERE / "openai_proxy_r3.py"
    spec = importlib.util.spec_from_file_location("r3_official_openai_proxy_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROXY = _load_proxy()
MODEL = PROXY.PINNED_MODEL
TEST_KEY = "synthetic-official-key-never-write-this"
LOCAL_AUTH = "Bearer local-transport"


def request_payload(*, limit: int = 4096, marker: str = "question") -> dict:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": marker}],
        "max_completion_tokens": limit,
    }


def good_response(
    content: str | None = "YES",
    *,
    finish_reason: object = "stop",
    prompt_tokens: object = 100,
    cached_tokens: object = 20,
    completion_tokens: object = 10,
    reasoning_tokens: object = 6,
) -> dict:
    return {
        "id": "chatcmpl-synthetic",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "service_tier": "default",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": (
                prompt_tokens + completion_tokens
                if isinstance(prompt_tokens, int)
                and not isinstance(prompt_tokens, bool)
                and isinstance(completion_tokens, int)
                and not isinstance(completion_tokens, bool)
                else 0
            ),
            "prompt_tokens_details": {
                "cached_tokens": cached_tokens,
                "audio_tokens": 0,
            },
            "completion_tokens_details": {
                "reasoning_tokens": reasoning_tokens,
                "audio_tokens": 0,
                "accepted_prediction_tokens": 0,
                "rejected_prediction_tokens": 0,
            },
        },
    }


class UpstreamState:
    def __init__(self) -> None:
        self.responses: list[tuple[int, bytes, dict[str, str]]] = []
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def push(
        self,
        status: int,
        body: dict | bytes,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
        defaults = {"x-request-id": f"req-test-{len(self.responses) + 1}"}
        defaults.update(headers or {})
        self.responses.append((status, encoded, defaults))


def upstream_handler(state: UpstreamState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            with state.lock:
                state.calls.append(
                    {
                        "body": raw,
                        "authorization": self.headers.get("Authorization"),
                        "client_request_id": self.headers.get("X-Client-Request-Id"),
                    }
                )
                status, response, headers = state.responses.pop(0)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    return Handler


class RunningProxy:
    def __init__(
        self,
        temporary: Path,
        responses: list[tuple[int, dict | bytes, dict[str, str] | None]],
        *,
        max_cost: str = "2.45",
        reservation: str = "0.318432",
        safety: str = "0.01",
        limit: int = 4096,
    ) -> None:
        self.state = UpstreamState()
        for status, body, headers in responses:
            self.state.push(status, body, headers=headers)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), upstream_handler(self.state))
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()
        self.ledger_path = temporary / "usage.jsonl"
        self.ledger = PROXY.UsageLedger(
            self.ledger_path,
            max_cost,
            request_reservation_usd=reservation,
            safety_reserve_usd=safety,
        )
        upstream_url = (
            f"http://127.0.0.1:{self.upstream.server_address[1]}"
            "/v1/chat/completions"
        )
        handler = PROXY.make_handler(
            api_key=TEST_KEY,
            ledger=self.ledger,
            upstream=upstream_url,
            allow_insecure_test_upstream=True,
            max_completion_tokens=limit,
        )
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        self.base_url = f"http://127.0.0.1:{self.proxy.server_address[1]}"

    def close(self) -> None:
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.proxy_thread.join(timeout=2)
        self.upstream_thread.join(timeout=2)

    def post(
        self,
        payload: dict,
        *,
        authorization: str = LOCAL_AUTH,
        retry_count: str | None = "0",
    ) -> tuple[int, dict, dict[str, str]]:
        headers = {"Authorization": authorization, "Content-Type": "application/json"}
        if retry_count is not None:
            headers["x-stainless-retry-count"] = retry_count
        request = Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                return (
                    response.status,
                    json.loads(response.read()),
                    {key.casefold(): value for key, value in response.headers.items()},
                )
        except HTTPError as error:
            return (
                error.code,
                json.loads(error.read()),
                {key.casefold(): value for key, value in error.headers.items()},
            )

    def health(self) -> dict:
        with build_opener(ProxyHandler({})).open(
            f"{self.base_url}/health", timeout=3
        ) as response:
            return json.loads(response.read())

    def events(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.ledger_path.read_text(encoding="utf-8").splitlines()
            if line
        ]


class OfficialProxyUnitTests(unittest.TestCase):
    def test_pricing_evidence_and_golden_cost(self) -> None:
        evidence = PROXY.verify_pricing_evidence()
        self.assertEqual(evidence["model_snapshot"], MODEL)
        cost = PROXY.exact_official_cost_usd(
            prompt_tokens=741933,
            cached_tokens=219904,
            completion_tokens=68284,
        )
        self.assertEqual(PROXY._money(cost), "0.715292550")

    def test_reasoning_is_not_double_counted(self) -> None:
        response = good_response(
            prompt_tokens=100,
            cached_tokens=20,
            completion_tokens=10,
            reasoning_tokens=10,
        )
        fields, cost = PROXY._usage_fields(response)
        self.assertEqual(fields["reasoning_tokens"], 10)
        expected = ((80 * 0.75) + (20 * 0.075) + (10 * 4.50)) / 1_000_000
        self.assertEqual(float(cost), expected)

    def test_prepare_applies_exact_controls_without_mutating_messages(self) -> None:
        payload = request_payload(marker="byte-semantic marker \u2603")
        original = json.loads(json.dumps(payload))
        prepared = PROXY.prepare_official_payload(payload, max_completion_tokens=4096)
        self.assertEqual(payload, original)
        self.assertEqual(prepared["messages"], payload["messages"])
        self.assertEqual(prepared["reasoning_effort"], "high")
        self.assertEqual(prepared["service_tier"], "default")
        self.assertIs(prepared["store"], False)
        self.assertEqual(prepared["n"], 1)
        self.assertEqual(prepared["max_completion_tokens"], 4096)

    def test_prepare_rejects_alias_legacy_and_conflicting_controls(self) -> None:
        mutations = (
            {"model": "gpt-5.4-mini"},
            {"model": "openai/gpt-5.4-mini-2026-03-17"},
            {"max_tokens": 4096},
            {"temperature": 0},
            {"provider": {"only": ["OpenAI"]}},
            {"reasoning": {"effort": "high"}},
            {"reasoning_effort": "low"},
            {"service_tier": "flex"},
            {"store": True},
            {"n": 2},
            {"stream": True},
            {"max_completion_tokens": 128},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                payload = request_payload()
                payload.update(mutation)
                with self.assertRaises(ValueError):
                    PROXY.prepare_official_payload(payload, max_completion_tokens=4096)

    def test_production_endpoint_is_exact_and_test_route_is_loopback_only(self) -> None:
        self.assertEqual(PROXY._validate_upstream(PROXY.OFFICIAL_UPSTREAM), PROXY.OFFICIAL_UPSTREAM)
        for invalid in (
            "http://api.openai.com/v1/chat/completions",
            "https://api.openai.com/v1/chat/completions?x=1",
            "https://example.com/v1/chat/completions",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                PROXY._validate_upstream(invalid)
        self.assertEqual(
            PROXY._validate_upstream(
                "http://127.0.0.1:1234/v1/chat/completions",
                allow_insecure_test_upstream=True,
            ),
            "http://127.0.0.1:1234/v1/chat/completions",
        )

    def test_proxy_source_and_guard_bind_direct_networking_and_checksum(self) -> None:
        source_path = HERE / "openai_proxy_r3.py"
        source = source_path.read_text(encoding="utf-8")
        guard = (HERE / "run_openai_proxy_guarded.py").read_text(encoding="utf-8")
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        self.assertIn("ProxyHandler({})", source)
        self.assertIn("_NoRedirectHandler()", source)
        self.assertNotIn("getproxies", source)
        self.assertIn(digest, guard)

    def test_quota_429_is_terminal_but_rate_limit_429_is_retryable(self) -> None:
        quota = json.dumps(
            {"error": {"type": "insufficient_quota", "code": "insufficient_quota"}}
        ).encode()
        rate = json.dumps(
            {"error": {"type": "rate_limit_error", "code": "rate_limit_exceeded"}}
        ).encode()
        self.assertFalse(PROXY._http_error_is_retryable(429, quota))
        self.assertTrue(PROXY._http_error_is_retryable(429, rate))
        self.assertTrue(PROXY._http_error_is_retryable(503, b""))
        self.assertFalse(PROXY._http_error_is_retryable(401, b""))

    def test_usage_validation_is_strict(self) -> None:
        cases = []
        wrong_total = good_response()
        wrong_total["usage"]["total_tokens"] += 1
        cases.append(wrong_total)
        cached_too_large = good_response()
        cached_too_large["usage"]["prompt_tokens_details"]["cached_tokens"] = 101
        cases.append(cached_too_large)
        reasoning_too_large = good_response()
        reasoning_too_large["usage"]["completion_tokens_details"]["reasoning_tokens"] = 11
        cases.append(reasoning_too_large)
        bool_token = good_response(prompt_tokens=True)
        cases.append(bool_token)
        prediction = good_response()
        prediction["usage"]["completion_tokens_details"]["accepted_prediction_tokens"] = 1
        cases.append(prediction)
        for response in cases:
            with self.subTest(response=response):
                self.assertIsNone(PROXY._usage_fields(response))


class OfficialProxyIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temporary = Path(self.temporary_directory.name)
        self.running: RunningProxy | None = None

    def tearDown(self) -> None:
        if self.running is not None:
            self.running.close()
        self.temporary_directory.cleanup()

    def run_proxy(
        self,
        responses: list[tuple[int, dict | bytes, dict[str, str] | None]],
        **kwargs,
    ) -> RunningProxy:
        self.running = RunningProxy(self.temporary, responses, **kwargs)
        return self.running

    def test_success_forwards_unchanged_body_and_exact_controls(self) -> None:
        upstream_body = good_response("synthetic-visible-answer")
        running = self.run_proxy([(200, upstream_body, None)])
        status, body, headers = running.post(request_payload(marker="secret-prompt-marker"))
        self.assertEqual(status, 200)
        self.assertEqual(body, upstream_body)
        self.assertNotIn("x-narratordb-transport-fatal", headers)
        self.assertEqual(len(running.state.calls), 1)
        upstream_call = running.state.calls[0]
        prepared = json.loads(upstream_call["body"])
        self.assertEqual(prepared["model"], MODEL)
        self.assertEqual(prepared["max_completion_tokens"], 4096)
        self.assertEqual(prepared["reasoning_effort"], "high")
        self.assertEqual(prepared["service_tier"], "default")
        self.assertIs(prepared["store"], False)
        self.assertEqual(upstream_call["authorization"], f"Bearer {TEST_KEY}")
        self.assertRegex(upstream_call["client_request_id"], r"^narratordb-r3-[0-9a-f]{32}$")
        event = running.events()[0]
        self.assertEqual(event["event"], "completion")
        self.assertEqual(event["cost_usd"], "0.000106500")
        self.assertEqual(event["observed_finish_class"], "stop")
        self.assertEqual(event["visible_content_state"], "nonempty")
        ledger_text = running.ledger_path.read_text(encoding="utf-8")
        self.assertNotIn("secret-prompt-marker", ledger_text)
        self.assertNotIn("synthetic-visible-answer", ledger_text)
        self.assertNotIn(TEST_KEY, ledger_text)

    def test_r2_anomaly_shape_retries_same_payload_then_succeeds(self) -> None:
        anomaly = good_response(
            None,
            finish_reason="error",
            prompt_tokens=10613,
            cached_tokens=0,
            completion_tokens=315,
            reasoning_tokens=315,
        )
        recovered = good_response("Premier Silver.")
        running = self.run_proxy([(200, anomaly, None), (200, recovered, None)])
        payload = request_payload(marker="50635ada-top50-synthetic")
        status1, _, headers1 = running.post(payload)
        status2, body2, headers2 = running.post(payload)
        self.assertEqual(status1, 502)
        self.assertEqual(status2, 200)
        self.assertEqual(body2["choices"][0]["message"]["content"], "Premier Silver.")
        self.assertNotIn("x-narratordb-transport-fatal", headers1)
        self.assertNotIn("x-narratordb-transport-fatal", headers2)
        events = running.events()
        self.assertEqual([event["event"] for event in events], ["discarded_transient", "completion"])
        self.assertEqual(events[0]["discarded_reason"], "contentless_provider_error")
        self.assertEqual(events[0]["visible_content_state"], "null")
        self.assertEqual(events[0]["observed_finish_class"], "error")
        self.assertEqual(events[0]["logical_call_id"], events[1]["logical_call_id"])
        self.assertEqual(events[0]["request_payload_sha256"], events[1]["request_payload_sha256"])
        self.assertEqual([event["attempt_number"] for event in events], [1, 2])
        self.assertNotEqual(events[0]["client_request_id"], events[1]["client_request_id"])
        self.assertFalse(running.health()["usage"]["transport_failed"])

    def test_reasoning_only_length_is_retryable(self) -> None:
        anomaly = good_response(
            "",
            finish_reason="length",
            completion_tokens=315,
            reasoning_tokens=315,
        )
        running = self.run_proxy([(200, anomaly, None)])
        status, _, headers = running.post(request_payload())
        self.assertEqual(status, 502)
        self.assertNotIn("x-narratordb-transport-fatal", headers)
        self.assertEqual(
            running.events()[0]["discarded_reason"],
            "contentless_reasoning_exhausted",
        )
        self.assertFalse(running.health()["usage"]["transport_failed"])

    def test_empty_stop_is_retryable(self) -> None:
        running = self.run_proxy([(200, good_response("", finish_reason="stop"), None)])
        status, _, headers = running.post(request_payload())
        self.assertEqual(status, 502)
        self.assertEqual(headers["x-should-retry"], "false")
        self.assertNotIn("x-narratordb-transport-fatal", headers)
        self.assertEqual(running.events()[0]["discarded_reason"], "empty_completion")

    def test_terminal_response_shapes_set_sticky_fuse_and_fatal_header(self) -> None:
        cases: list[tuple[str, dict]] = []
        partial = good_response("partial", finish_reason="length")
        cases.append(("partial", partial))
        content_filter = good_response(None, finish_reason="content_filter")
        cases.append(("content_filter", content_filter))
        refusal = good_response(None)
        refusal["choices"][0]["message"]["refusal"] = "blocked"
        cases.append(("refusal", refusal))
        tool = good_response(None, finish_reason="tool_calls")
        tool["choices"][0]["message"]["tool_calls"] = [{"id": "tool"}]
        cases.append(("tool", tool))
        unknown = good_response(None, finish_reason="banana")
        cases.append(("unknown", unknown))
        wrong_model = good_response()
        wrong_model["model"] = "gpt-5.4-mini"
        cases.append(("wrong_model", wrong_model))
        wrong_tier = good_response()
        wrong_tier["service_tier"] = "flex"
        cases.append(("wrong_tier", wrong_tier))
        bad_usage = good_response()
        bad_usage["usage"]["total_tokens"] += 1
        cases.append(("bad_usage", bad_usage))
        for name, response in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                running = RunningProxy(Path(directory), [(200, response, None)])
                try:
                    status, _, headers = running.post(request_payload())
                    self.assertEqual(status, 502)
                    self.assertEqual(headers["x-should-retry"], "false")
                    self.assertEqual(headers["x-narratordb-transport-fatal"], "true")
                    self.assertTrue(running.health()["usage"]["transport_failed"])
                    self.assertEqual(running.health()["usage"]["fatal_reason_code"], "terminal_response")
                finally:
                    running.close()

    def test_missing_upstream_request_id_is_terminal(self) -> None:
        running = self.run_proxy(
            [(200, good_response(), {"x-request-id": ""})]
        )
        status, _, headers = running.post(request_payload())
        self.assertEqual(status, 502)
        self.assertEqual(headers["x-narratordb-transport-fatal"], "true")
        self.assertEqual(running.events()[0]["discarded_reason"], "response_identity_unverified")

    def test_hidden_sdk_retry_fuses_before_upstream(self) -> None:
        running = self.run_proxy([(200, good_response(), None)])
        status, _, headers = running.post(request_payload(), retry_count="1")
        self.assertEqual(status, 400)
        self.assertEqual(headers["x-should-retry"], "false")
        self.assertEqual(headers["x-narratordb-transport-fatal"], "true")
        self.assertEqual(running.state.calls, [])
        usage = running.health()["usage"]
        self.assertTrue(usage["transport_failed"])
        self.assertEqual(usage["fatal_reason_code"], "hidden_sdk_retry")
        self.assertEqual(usage["hidden_sdk_retry_rejections"], 1)

    def test_fatal_fuse_prevents_every_later_upstream_call(self) -> None:
        running = self.run_proxy([(200, good_response("partial", finish_reason="length"), None)])
        first_status, _, first_headers = running.post(request_payload())
        second_status, _, second_headers = running.post(request_payload(marker="later"))
        self.assertEqual(first_status, 502)
        self.assertEqual(second_status, 503)
        self.assertEqual(first_headers["x-narratordb-transport-fatal"], "true")
        self.assertEqual(second_headers["x-narratordb-transport-fatal"], "true")
        self.assertEqual(len(running.state.calls), 1)

    def test_four_transients_then_success_is_the_maximum_valid_chain(self) -> None:
        empty = good_response("", finish_reason="stop")
        responses = [(200, empty, None) for _ in range(4)]
        responses.append((200, good_response("OK"), None))
        running = self.run_proxy(responses)
        payload = request_payload()
        statuses = [running.post(payload)[0] for _ in range(5)]
        self.assertEqual(statuses, [502, 502, 502, 502, 200])
        events = running.events()
        self.assertEqual([event["attempt_number"] for event in events], [1, 2, 3, 4, 5])
        self.assertEqual(len({event["logical_call_id"] for event in events}), 1)
        self.assertEqual(len({event["request_payload_sha256"] for event in events}), 1)
        self.assertEqual(len({event["client_request_id"] for event in events}), 5)
        self.assertFalse(running.health()["usage"]["transport_failed"])

    def test_fifth_transient_trips_retry_limit_and_sixth_never_reaches_upstream(self) -> None:
        empty = good_response("", finish_reason="stop")
        running = self.run_proxy([(200, empty, None) for _ in range(5)])
        payload = request_payload()
        attempts = [running.post(payload) for _ in range(5)]
        statuses = [attempt[0] for attempt in attempts]
        sixth_status, _, sixth_headers = running.post(request_payload(marker="sixth"))
        self.assertEqual(statuses, [502, 502, 502, 502, 502])
        self.assertNotIn("x-narratordb-transport-fatal", attempts[3][2])
        self.assertEqual(
            attempts[4][2]["x-narratordb-transport-fatal"], "true"
        )
        self.assertEqual(sixth_status, 503)
        self.assertEqual(sixth_headers["x-narratordb-transport-fatal"], "true")
        self.assertEqual(len(running.state.calls), 5)
        usage = running.health()["usage"]
        self.assertTrue(usage["transport_failed"])
        self.assertEqual(usage["fatal_reason_code"], "retry_limit")
        self.assertEqual(usage["discarded_transients"], 4)
        self.assertEqual(usage["terminal_rejections"], 1)
        self.assertEqual(running.events()[-1]["event"], "terminal_rejection")

    def test_retryable_http_and_terminal_quota_429_headers(self) -> None:
        rate_body = {
            "error": {"type": "rate_limit_error", "code": "rate_limit_exceeded"}
        }
        running = self.run_proxy([(429, rate_body, None)])
        status, _, headers = running.post(request_payload())
        self.assertEqual(status, 429)
        self.assertNotIn("x-narratordb-transport-fatal", headers)
        self.assertEqual(running.events()[0]["event"], "discarded_transient")
        running.close()
        self.running = None

        with tempfile.TemporaryDirectory() as directory:
            quota_body = {
                "error": {"type": "insufficient_quota", "code": "insufficient_quota"}
            }
            quota = RunningProxy(Path(directory), [(429, quota_body, None)])
            try:
                status, _, headers = quota.post(request_payload())
                self.assertEqual(status, 429)
                self.assertEqual(headers["x-narratordb-transport-fatal"], "true")
                self.assertTrue(quota.health()["usage"]["transport_failed"])
            finally:
                quota.close()

    def test_unknown_transport_cost_books_full_reservation(self) -> None:
        running = self.run_proxy([(503, {"error": {"type": "server_error"}}, None)])
        running.post(request_payload())
        event = running.events()[0]
        self.assertEqual(event["cost_usd"], "0.318432000")
        self.assertIs(event["unknown_cost"], True)
        self.assertEqual(running.health()["usage"]["cost_usd"], "0.318432000")

    def test_budget_fuse_is_fatal_before_upstream(self) -> None:
        running = self.run_proxy(
            [(200, good_response(), None)],
            max_cost="0.32",
            reservation="0.318432",
            safety="0.01",
        )
        status, _, headers = running.post(request_payload())
        self.assertEqual(status, 503)
        self.assertEqual(headers["x-narratordb-transport-fatal"], "true")
        self.assertEqual(running.state.calls, [])
        usage = running.health()["usage"]
        self.assertTrue(usage["transport_failed"])
        self.assertEqual(usage["fatal_reason_code"], "budget_fuse")

    def test_computed_cost_above_reservation_cannot_overrun_fuse(self) -> None:
        expensive = good_response(
            prompt_tokens=400000,
            cached_tokens=0,
            completion_tokens=4096,
            reasoning_tokens=4096,
        )
        running = self.run_proxy(
            [(200, expensive, None)],
            max_cost="0.32",
            reservation="0.10",
            safety="0.01",
        )
        status, _, headers = running.post(request_payload())
        self.assertEqual(status, 502)
        self.assertEqual(headers["x-narratordb-transport-fatal"], "true")
        event = running.events()[0]
        self.assertEqual(event["event"], "terminal_rejection")
        self.assertEqual(event["discarded_reason"], "budget_cost_overrun")
        usage = running.health()["usage"]
        self.assertTrue(usage["transport_failed"])
        self.assertEqual(usage["fatal_reason_code"], "budget_fuse")

    def test_wrong_local_auth_never_exposes_or_uses_official_key(self) -> None:
        running = self.run_proxy([(200, good_response(), None)])
        status, body, headers = running.post(
            request_payload(), authorization=f"Bearer {TEST_KEY}"
        )
        self.assertEqual(status, 401)
        self.assertEqual(headers["x-should-retry"], "false")
        self.assertEqual(headers["x-narratordb-transport-fatal"], "true")
        self.assertNotIn(TEST_KEY, json.dumps(body))
        self.assertEqual(running.state.calls, [])
        self.assertEqual(running.events(), [])
        self.assertEqual(
            running.health()["usage"]["fatal_reason_code"],
            "invalid_local_request",
        )

    def test_invalid_local_model_and_controls_fuse_without_upstream(self) -> None:
        mutations = (
            {"model": "gpt-5.4-mini"},
            {"max_tokens": 4096},
            {"temperature": 0},
            {"reasoning_effort": "low"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                running = RunningProxy(Path(directory), [(200, good_response(), None)])
                try:
                    payload = request_payload()
                    payload.update(mutation)
                    status, _, headers = running.post(payload)
                    self.assertEqual(status, 400)
                    self.assertEqual(headers["x-should-retry"], "false")
                    self.assertEqual(headers["x-narratordb-transport-fatal"], "true")
                    self.assertEqual(running.state.calls, [])
                    self.assertEqual(
                        running.health()["usage"]["fatal_reason_code"],
                        "invalid_local_request",
                    )
                finally:
                    running.close()

    def test_health_is_content_free_and_binds_immutable_configuration(self) -> None:
        running = self.run_proxy([(200, good_response(), None)])
        health = running.health()
        self.assertIs(health["ok"], True)
        self.assertEqual(health["upstream"], PROXY.OFFICIAL_UPSTREAM)
        self.assertEqual(health["endpoint_identity"], PROXY.ENDPOINT_IDENTITY)
        self.assertEqual(health["provider_identity"], "OpenAI")
        self.assertEqual(health["model"], MODEL)
        self.assertEqual(health["max_completion_tokens"], 4096)
        self.assertEqual(health["reasoning_effort"], "high")
        self.assertEqual(health["service_tier"], "default")
        self.assertIs(health["store"], False)
        self.assertEqual(health["n"], 1)
        self.assertIs(health["direct_upstream_networking"], True)
        self.assertIs(health["environment_proxy_inheritance"], False)
        self.assertIs(health["prompt_or_completion_content_retained"], False)
        self.assertFalse(health["usage"]["transport_failed"])

    def test_canary_limit_is_exactly_128(self) -> None:
        running = self.run_proxy(
            [(200, good_response(), None)],
            limit=128,
            max_cost="0.310576",
            reservation="0.300576",
            safety="0.01",
        )
        status, _, _ = running.post(request_payload(limit=128))
        self.assertEqual(status, 200)
        prepared = json.loads(running.state.calls[0]["body"])
        self.assertEqual(prepared["max_completion_tokens"], 128)
        self.assertEqual(running.health()["max_completion_tokens"], 128)


if __name__ == "__main__":
    unittest.main(verbosity=2)
