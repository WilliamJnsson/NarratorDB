import concurrent.futures
import http.client
import http.server
import json
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

from narratordb.benchmarks.openrouter_proxy import (
    UsageLedger,
    make_handler,
    prepare_openrouter_payload,
)


class OpenRouterBenchmarkTransportTests(unittest.TestCase):
    def _proxy_completion(self, content: object) -> dict[str, object]:
        private_detail = "private upstream completion detail"

        class UpstreamHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.dumps(
                    {
                        "model": "deepseek/pinned",
                        "provider": "DeepInfra",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": content},
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 2,
                            "completion_tokens": 3,
                            "cost": 0.01,
                        },
                        "private_detail": private_detail,
                    }
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return None

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                usage_path = Path(directory) / "usage.jsonl"
                ledger = UsageLedger(
                    usage_path,
                    max_cost_usd=1.0,
                    request_reservation_usd=0.05,
                    safety_reserve_usd=0.01,
                )
                handler = make_handler(
                    api_key="runtime-secret",
                    upstream=f"http://127.0.0.1:{upstream.server_port}/upstream",
                    provider_only="DeepInfra",
                    reasoning_effort=None,
                    ledger=ledger,
                    timeout=2.0,
                    max_request_bytes=4096,
                    max_response_bytes=4096,
                    allow_insecure_test_upstream=True,
                )
                proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
                proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
                proxy_thread.start()
                try:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", proxy.server_port, timeout=3.0
                    )
                    connection.request(
                        "POST",
                        "/v1/chat/completions",
                        body=json.dumps({"model": "deepseek/pinned", "messages": []}),
                        headers={"Content-Type": "application/json"},
                    )
                    response = connection.getresponse()
                    client_body = response.read().decode("utf-8")
                    status = response.status
                    connection.close()
                finally:
                    proxy.shutdown()
                    proxy.server_close()
                    proxy_thread.join(timeout=2.0)

                for _ in range(100):
                    summary = ledger.summary()
                    if summary["reserved_cost_usd"] == 0.0:
                        break
                    time.sleep(0.01)
                encoded_ledger = usage_path.read_text(encoding="utf-8")
                records = [
                    json.loads(line)
                    for line in encoded_ledger.splitlines()
                    if line.strip()
                ]
                return {
                    "status": status,
                    "client_body": client_body,
                    "ledger": encoded_ledger,
                    "records": records,
                    "summary": ledger.summary(),
                    "private_detail": private_detail,
                }
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2.0)

    def test_payload_pins_provider_and_reasoning_without_mutating_input(self):
        original = {
            "model": "deepseek/example",
            "messages": [{"role": "user", "content": "x"}],
        }
        prepared = prepare_openrouter_payload(
            original,
            provider_only="DeepInfra",
            reasoning_effort="high",
        )

        self.assertNotIn("provider", original)
        self.assertNotIn("reasoning", original)
        self.assertEqual(prepared["provider"]["only"], ["DeepInfra"])
        self.assertEqual(prepared["provider"]["order"], ["DeepInfra"])
        self.assertFalse(prepared["provider"]["allow_fallbacks"])
        self.assertTrue(prepared["provider"]["require_parameters"])
        self.assertEqual(prepared["provider"]["data_collection"], "deny")
        self.assertTrue(prepared["provider"]["zdr"])
        self.assertEqual(prepared["reasoning"], {"effort": "high"})

    def test_payload_allows_a_declared_provider_fallback_set(self):
        prepared = prepare_openrouter_payload(
            {"model": "deepseek/example"},
            provider_allow=("DeepInfra", "StreamLake", "Baidu"),
            reasoning_effort="high",
        )

        self.assertEqual(
            prepared["provider"],
            {
                "only": ["DeepInfra", "StreamLake", "Baidu"],
                "order": ["DeepInfra", "StreamLake", "Baidu"],
                "allow_fallbacks": True,
                "require_parameters": True,
                "data_collection": "deny",
                "zdr": True,
            },
        )

    def test_public_benchmark_policy_is_explicit_and_overrides_incoming_privacy(self):
        prepared = prepare_openrouter_payload(
            {
                "model": "z-ai/glm-5.2",
                "provider": {"data_collection": "deny", "zdr": True},
            },
            provider_allow=("StreamLake", "DeepInfra", "AtlasCloud"),
            reasoning_effort="high",
            public_benchmark=True,
        )

        self.assertEqual(
            prepared["provider"],
            {
                "only": ["StreamLake", "DeepInfra", "AtlasCloud"],
                "order": ["StreamLake", "DeepInfra", "AtlasCloud"],
                "allow_fallbacks": True,
                "require_parameters": True,
                "data_collection": "allow",
                "zdr": False,
            },
        )
        self.assertEqual(prepared["reasoning"], {"effort": "high"})

    def test_reasoning_override_none_removes_field_and_levels_preserve_siblings(self):
        original = {
            "model": "openai/gpt-5.4-mini",
            "reasoning": {
                "effort": "xhigh",
                "exclude": True,
                "max_tokens": 512,
            },
        }

        disabled = prepare_openrouter_payload(
            original,
            provider_only="Azure",
            reasoning_effort="none",
        )
        overridden = prepare_openrouter_payload(
            original,
            provider_only="Azure",
            reasoning_effort="medium",
        )

        self.assertEqual(original["reasoning"]["effort"], "xhigh")
        self.assertNotIn("reasoning", disabled)
        self.assertEqual(
            overridden["reasoning"],
            {"effort": "medium", "exclude": True, "max_tokens": 512},
        )
        with self.assertRaisesRegex(ValueError, "none, low, medium"):
            prepare_openrouter_payload(
                original,
                provider_only="Azure",
                reasoning_effort="minimal",
            )

    def test_output_token_parameter_renames_only_the_alternate_exact_integer(self):
        original = {
            "model": "z-ai/glm-5.2",
            "messages": [],
            "max_tokens": 4096,
        }
        prepared = prepare_openrouter_payload(
            original,
            provider_only="StreamLake",
            output_token_parameter="max_completion_tokens",
        )

        self.assertEqual(original["max_tokens"], 4096)
        self.assertNotIn("max_completion_tokens", original)
        self.assertNotIn("max_tokens", prepared)
        self.assertEqual(prepared["max_completion_tokens"], 4096)

        already_desired = prepare_openrouter_payload(
            {
                "model": "z-ai/glm-5.2",
                "max_completion_tokens": 2048,
            },
            provider_only="StreamLake",
            output_token_parameter="max_completion_tokens",
        )
        self.assertEqual(already_desired["max_completion_tokens"], 2048)
        self.assertNotIn("max_tokens", already_desired)

        reverse = prepare_openrouter_payload(
            {
                "model": "deepseek/deepseek-v4-flash",
                "max_completion_tokens": 1024,
            },
            provider_only="DeepInfra",
            output_token_parameter="max_tokens",
        )
        self.assertEqual(reverse["max_tokens"], 1024)
        self.assertNotIn("max_completion_tokens", reverse)

        absent = prepare_openrouter_payload(
            {"model": "z-ai/glm-5.2"},
            provider_only="StreamLake",
            output_token_parameter="max_completion_tokens",
        )
        self.assertNotIn("max_tokens", absent)
        self.assertNotIn("max_completion_tokens", absent)

    def test_output_token_parameter_rejects_both_fields_and_nonexact_integers(self):
        with self.assertRaisesRegex(ValueError, "must not contain both"):
            prepare_openrouter_payload(
                {
                    "model": "z-ai/glm-5.2",
                    "max_tokens": 1,
                    "max_completion_tokens": 1,
                },
                provider_only="StreamLake",
                output_token_parameter="max_completion_tokens",
            )

        invalid_values = (True, False, 0, -1, 1.0, "1", (1 << 63))
        for field in ("max_tokens", "max_completion_tokens"):
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, "positive integer"):
                        prepare_openrouter_payload(
                            {"model": "z-ai/glm-5.2", field: value},
                            provider_only="StreamLake",
                            output_token_parameter="max_completion_tokens",
                        )

        with self.assertRaisesRegex(ValueError, "must be max_tokens"):
            prepare_openrouter_payload(
                {"model": "z-ai/glm-5.2", "max_tokens": 1},
                provider_only="StreamLake",
                output_token_parameter="max_output_tokens",
            )

    def test_temperature_omission_removes_only_that_field_from_copied_payload(self):
        original = {
            "model": "openai/gpt-5.4-mini",
            "temperature": 0,
            "top_p": 0.9,
            "messages": [],
        }
        prepared = prepare_openrouter_payload(
            original,
            provider_only="Azure",
            omit_temperature=True,
        )

        self.assertEqual(original["temperature"], 0)
        self.assertNotIn("temperature", prepared)
        self.assertEqual(prepared["top_p"], 0.9)
        self.assertEqual(prepared["messages"], [])

        preserved = prepare_openrouter_payload(
            original,
            provider_only="Azure",
        )
        self.assertEqual(preserved["temperature"], 0)

    def test_temperature_omission_safely_discards_nonnumeric_values(self):
        for value in (None, True, "zero", {"private": "value"}, [0]):
            with self.subTest(value=value):
                prepared = prepare_openrouter_payload(
                    {
                        "model": "openai/gpt-5.4-mini",
                        "temperature": value,
                        "messages": [],
                    },
                    provider_only="Azure",
                    omit_temperature=True,
                )

                self.assertNotIn("temperature", prepared)
                self.assertEqual(prepared["messages"], [])

    def test_payload_replaces_incoming_routing_policy_and_rejects_collisions(self):
        prepared = prepare_openrouter_payload(
            {
                "model": "deepseek/example",
                "provider": {
                    "only": ["attacker"],
                    "order": ["attacker"],
                    "allow_fallbacks": True,
                    "require_parameters": False,
                    "data_collection": "allow",
                    "zdr": False,
                },
            },
            provider_only="DeepInfra",
        )

        self.assertEqual(prepared["provider"]["only"], ["DeepInfra"])
        self.assertEqual(prepared["provider"]["order"], ["DeepInfra"])
        self.assertFalse(prepared["provider"]["allow_fallbacks"])
        self.assertTrue(prepared["provider"]["require_parameters"])
        self.assertEqual(prepared["provider"]["data_collection"], "deny")
        self.assertTrue(prepared["provider"]["zdr"])
        with self.assertRaisesRegex(ValueError, "distinct provider families"):
            prepare_openrouter_payload(
                {"model": "deepseek/example"},
                provider_allow=("wafer/fp4", "wafer/fast"),
            )

    def test_provider_modes_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            prepare_openrouter_payload(
                {},
                provider_only="DeepInfra",
                provider_allow=("StreamLake",),
            )

    def test_model_routes_override_exact_models_and_keep_global_fallback(self):
        captured: list[dict[str, object]] = []

        class UpstreamHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(length))
                captured.append(request)
                body = json.dumps(
                    {
                        "model": request["model"],
                        "provider": request["provider"]["only"][0],
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": "ok"},
                            }
                        ],
                        "usage": {"cost": 0.01},
                    }
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return None

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                ledger = UsageLedger(Path(directory) / "usage.jsonl", max_cost_usd=1.0)
                handler = make_handler(
                    api_key="runtime-secret",
                    upstream=f"http://127.0.0.1:{upstream.server_port}/upstream",
                    provider_only="Azure",
                    model_routes={
                        "z-ai/glm-5.2": ("StreamLake", "DeepInfra"),
                    },
                    model_output_token_parameters={
                        "z-ai/glm-5.2": "max_completion_tokens",
                    },
                    model_omit_temperature=("z-ai/glm-5.2",),
                    model_reasoning_efforts={"z-ai/glm-5.2": "none"},
                    reasoning_effort="high",
                    public_benchmark=True,
                    ledger=ledger,
                    timeout=2.0,
                    max_request_bytes=4096,
                    max_response_bytes=4096,
                    allow_insecure_test_upstream=True,
                )
                proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
                proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
                proxy_thread.start()
                try:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", proxy.server_port, timeout=3.0
                    )
                    connection.request("GET", "/v1/health")
                    health_response = connection.getresponse()
                    health = json.loads(health_response.read())
                    connection.close()

                    statuses = []
                    for model in ("z-ai/glm-5.2", "z-ai/glm-5.2-preview"):
                        request_payload = {"model": model, "messages": []}
                        if model == "z-ai/glm-5.2":
                            request_payload["max_tokens"] = 4096
                            request_payload["temperature"] = 0
                            request_payload["reasoning"] = {
                                "effort": "xhigh",
                                "exclude": True,
                            }
                        else:
                            # The compatibility map is exact-model only. An
                            # unmatched request is forwarded byte-for-value
                            # even when both token fields would be invalid for
                            # a mapped request.
                            request_payload["max_tokens"] = 5
                            request_payload["max_completion_tokens"] = "unchanged"
                            request_payload["temperature"] = 0.25
                            request_payload["reasoning"] = {"exclude": True}
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", proxy.server_port, timeout=3.0
                        )
                        connection.request(
                            "POST",
                            "/v1/chat/completions",
                            body=json.dumps(request_payload),
                            headers={"Content-Type": "application/json"},
                        )
                        response = connection.getresponse()
                        statuses.append(response.status)
                        response.read()
                        connection.close()

                    connection = http.client.HTTPConnection(
                        "127.0.0.1", proxy.server_port, timeout=3.0
                    )
                    connection.request(
                        "POST",
                        "/v1/chat/completions",
                        body=json.dumps({"model": " z-ai/glm-5.2", "messages": []}),
                        headers={"Content-Type": "application/json"},
                    )
                    unsanitized_response = connection.getresponse()
                    unsanitized_response.read()
                    connection.close()
                finally:
                    proxy.shutdown()
                    proxy.server_close()
                    proxy_thread.join(timeout=2.0)

                self.assertEqual(health_response.status, HTTPStatus.OK)
                self.assertEqual(
                    health["model_routes"],
                    {"z-ai/glm-5.2": ["StreamLake", "DeepInfra"]},
                )
                self.assertEqual(
                    health["model_output_token_parameters"],
                    {"z-ai/glm-5.2": "max_completion_tokens"},
                )
                self.assertEqual(health["model_omit_temperature"], ["z-ai/glm-5.2"])
                self.assertEqual(
                    health["model_reasoning_efforts"],
                    {"z-ai/glm-5.2": "none"},
                )
                self.assertEqual(health["reasoning_effort"], "high")
                self.assertEqual(statuses, [HTTPStatus.OK, HTTPStatus.OK])
                self.assertEqual(unsanitized_response.status, HTTPStatus.BAD_REQUEST)
                self.assertEqual(len(captured), 2)
                self.assertEqual(
                    captured[0]["provider"],
                    {
                        "only": ["StreamLake", "DeepInfra"],
                        "order": ["StreamLake", "DeepInfra"],
                        "allow_fallbacks": True,
                        "require_parameters": True,
                        "data_collection": "allow",
                        "zdr": False,
                    },
                )
                self.assertEqual(captured[1]["provider"]["only"], ["Azure"])
                self.assertFalse(captured[1]["provider"]["allow_fallbacks"])
                self.assertNotIn("max_tokens", captured[0])
                self.assertEqual(captured[0]["max_completion_tokens"], 4096)
                self.assertNotIn("temperature", captured[0])
                self.assertNotIn("reasoning", captured[0])
                self.assertEqual(captured[1]["max_tokens"], 5)
                self.assertEqual(captured[1]["max_completion_tokens"], "unchanged")
                self.assertEqual(captured[1]["temperature"], 0.25)
                self.assertEqual(
                    captured[1]["reasoning"],
                    {"exclude": True, "effort": "high"},
                )
                self.assertEqual(
                    [
                        json.loads(line)["provider"]
                        for line in ledger.path.read_text(encoding="utf-8").splitlines()
                    ],
                    ["StreamLake", "Azure"],
                )
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2.0)

    def test_model_route_attestation_uses_selected_request_route(self):
        class UpstreamHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(length))
                body = json.dumps(
                    {
                        "model": request["model"],
                        "provider": "Azure",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": "must not be forwarded"},
                            }
                        ],
                        "usage": {"cost": 0.01},
                    }
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return None

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                usage_path = Path(directory) / "usage.jsonl"
                handler = make_handler(
                    api_key="runtime-secret",
                    upstream=f"http://127.0.0.1:{upstream.server_port}/upstream",
                    provider_only="Azure",
                    model_routes={"z-ai/glm-5.2": ("StreamLake",)},
                    reasoning_effort=None,
                    ledger=UsageLedger(usage_path, max_cost_usd=1.0),
                    timeout=2.0,
                    max_request_bytes=4096,
                    allow_insecure_test_upstream=True,
                )
                proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
                proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
                proxy_thread.start()
                try:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", proxy.server_port, timeout=3.0
                    )
                    connection.request(
                        "POST",
                        "/v1/chat/completions",
                        body=json.dumps({"model": "z-ai/glm-5.2", "messages": []}),
                        headers={"Content-Type": "application/json"},
                    )
                    response = connection.getresponse()
                    body = response.read().decode("utf-8")
                    connection.close()
                finally:
                    proxy.shutdown()
                    proxy.server_close()
                    proxy_thread.join(timeout=2.0)

                self.assertEqual(response.status, HTTPStatus.BAD_GATEWAY)
                self.assertNotIn("must not be forwarded", body)
                self.assertEqual(
                    json.loads(usage_path.read_text(encoding="utf-8"))["provider"],
                    "route_mismatch",
                )
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2.0)

    def test_model_route_validation_rejects_unsafe_or_ambiguous_routes(self):
        cases = (
            ({" z-ai/glm-5.2": ("StreamLake",)}, "exact sanitized"),
            ({"z-ai/glm-5.2": ()}, "must contain providers"),
            (
                {"z-ai/glm-5.2": ("wafer/fp4", "wafer/fast")},
                "distinct provider families",
            ),
        )
        for model_routes, message in cases:
            with self.subTest(model_routes=model_routes):
                with self.assertRaisesRegex(ValueError, message):
                    make_handler(
                        api_key="runtime-secret",
                        upstream="https://openrouter.ai/api/v1/chat/completions",
                        provider_only="Azure",
                        model_routes=model_routes,
                        reasoning_effort=None,
                        ledger=UsageLedger(None, max_cost_usd=1.0),
                        timeout=2.0,
                        max_request_bytes=4096,
                    )

    def test_model_output_token_parameter_mapping_validation_is_strict(self):
        cases = (
            ({" z-ai/glm-5.2": "max_tokens"}, "exact sanitized"),
            ({"z-ai/glm-5.2": "max_output_tokens"}, "must be max_tokens"),
            ([("z-ai/glm-5.2", "max_tokens")], "model-to-parameter mapping"),
        )
        for parameters, message in cases:
            with self.subTest(parameters=parameters):
                with self.assertRaisesRegex(ValueError, message):
                    make_handler(
                        api_key="runtime-secret",
                        upstream="https://openrouter.ai/api/v1/chat/completions",
                        provider_only="Azure",
                        model_output_token_parameters=parameters,
                        reasoning_effort=None,
                        ledger=UsageLedger(None, max_cost_usd=1.0),
                        timeout=2.0,
                        max_request_bytes=4096,
                    )

    def test_model_temperature_omission_validation_is_strict(self):
        cases = (
            ((" openai/gpt-5.4-mini",), "exact sanitized"),
            (
                ("openai/gpt-5.4-mini", "openai/gpt-5.4-mini"),
                "must be unique",
            ),
            ("openai/gpt-5.4-mini", "must be an array"),
        )
        for models, message in cases:
            with self.subTest(models=models):
                with self.assertRaisesRegex(ValueError, message):
                    make_handler(
                        api_key="runtime-secret",
                        upstream="https://openrouter.ai/api/v1/chat/completions",
                        provider_only="Azure",
                        model_omit_temperature=models,
                        reasoning_effort=None,
                        ledger=UsageLedger(None, max_cost_usd=1.0),
                        timeout=2.0,
                        max_request_bytes=4096,
                    )

    def test_model_reasoning_effort_mapping_validation_is_strict(self):
        cases = (
            ({" openai/gpt-5.4-mini": "none"}, "exact sanitized"),
            ({"openai/gpt-5.4-mini": "minimal"}, "none, low, medium"),
            ({"openai/gpt-5.4-mini": "HIGH"}, "none, low, medium"),
            ({"openai/gpt-5.4-mini": None}, "none, low, medium"),
            (
                [("openai/gpt-5.4-mini", "none")],
                "model-to-effort mapping",
            ),
        )
        for efforts, message in cases:
            with self.subTest(efforts=efforts):
                with self.assertRaisesRegex(ValueError, message):
                    make_handler(
                        api_key="runtime-secret",
                        upstream="https://openrouter.ai/api/v1/chat/completions",
                        provider_only="Azure",
                        model_reasoning_efforts=efforts,
                        reasoning_effort=None,
                        ledger=UsageLedger(None, max_cost_usd=1.0),
                        timeout=2.0,
                        max_request_bytes=4096,
                    )

    def test_cli_rejects_duplicate_exact_model_routes_before_startup(self):
        repository_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                "-B",
                "-m",
                "narratordb.benchmarks.openrouter_proxy",
                "--provider-only",
                "Azure",
                "--model-route",
                "z-ai/glm-5.2=StreamLake",
                "--model-route",
                "z-ai/glm-5.2=DeepInfra",
            ],
            cwd=repository_root,
            env={
                "LANG": "C",
                "LC_ALL": "C",
                "OPENROUTER_API_KEY": "runtime-secret",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(repository_root),
            },
            capture_output=True,
            text=True,
            timeout=5.0,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("repeats exact model", completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_cli_strictly_parses_and_rejects_duplicate_output_token_mappings(self):
        repository_root = Path(__file__).resolve().parents[1]
        base_command = [
            sys.executable,
            "-S",
            "-B",
            "-m",
            "narratordb.benchmarks.openrouter_proxy",
            "--provider-only",
            "Azure",
        ]
        environment = {
            "LANG": "C",
            "LC_ALL": "C",
            "OPENROUTER_API_KEY": "runtime-secret",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(repository_root),
        }
        cases = (
            (
                [
                    "--model-output-token-parameter",
                    "z-ai/glm-5.2=max_tokens",
                    "--model-output-token-parameter",
                    "z-ai/glm-5.2=max_completion_tokens",
                ],
                "repeats exact model",
            ),
            (
                [
                    "--model-output-token-parameter",
                    " z-ai/glm-5.2=max_tokens",
                ],
                "exact sanitized model identifier",
            ),
            (
                [
                    "--model-output-token-parameter",
                    "z-ai/glm-5.2=max_output_tokens",
                ],
                "must be max_tokens or max_completion_tokens",
            ),
        )
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    base_command + arguments,
                    cwd=repository_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )

                self.assertEqual(completed.returncode, 2)
                self.assertIn(message, completed.stderr)
                self.assertEqual(completed.stdout, "")

    def test_cli_strictly_parses_and_rejects_duplicate_temperature_omissions(self):
        repository_root = Path(__file__).resolve().parents[1]
        base_command = [
            sys.executable,
            "-S",
            "-B",
            "-m",
            "narratordb.benchmarks.openrouter_proxy",
            "--provider-only",
            "Azure",
        ]
        environment = {
            "LANG": "C",
            "LC_ALL": "C",
            "OPENROUTER_API_KEY": "runtime-secret",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(repository_root),
        }
        cases = (
            (
                [
                    "--model-omit-temperature",
                    "openai/gpt-5.4-mini",
                    "--model-omit-temperature",
                    "openai/gpt-5.4-mini",
                ],
                "repeats an exact model",
            ),
            (
                [
                    "--model-omit-temperature",
                    " openai/gpt-5.4-mini",
                ],
                "exact sanitized model identifier",
            ),
        )
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    base_command + arguments,
                    cwd=repository_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )

                self.assertEqual(completed.returncode, 2)
                self.assertIn(message, completed.stderr)
                self.assertEqual(completed.stdout, "")

    def test_cli_strictly_parses_model_reasoning_effort_overrides(self):
        repository_root = Path(__file__).resolve().parents[1]
        base_command = [
            sys.executable,
            "-S",
            "-B",
            "-m",
            "narratordb.benchmarks.openrouter_proxy",
            "--provider-only",
            "Azure",
        ]
        environment = {
            "LANG": "C",
            "LC_ALL": "C",
            "OPENROUTER_API_KEY": "runtime-secret",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(repository_root),
        }
        cases = (
            (
                [
                    "--model-reasoning-effort",
                    "openai/gpt-5.4-mini=none",
                    "--model-reasoning-effort",
                    "openai/gpt-5.4-mini=high",
                ],
                "repeats exact model",
            ),
            (
                ["--model-reasoning-effort", " openai/gpt-5.4-mini=none"],
                "exact sanitized model identifier",
            ),
            (
                ["--model-reasoning-effort", "openai/gpt-5.4-mini=HIGH"],
                "none, low, medium, high, or xhigh",
            ),
            (
                ["--model-reasoning-effort", "openai/gpt-5.4-mini"],
                "MODEL=none|low|medium|high|xhigh",
            ),
        )
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    base_command + arguments,
                    cwd=repository_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )

                self.assertEqual(completed.returncode, 2)
                self.assertIn(message, completed.stderr)
                self.assertEqual(completed.stdout, "")

    def test_usage_ledger_records_only_usage_metadata_and_enforces_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            ledger = UsageLedger(path, max_cost_usd=0.01)
            response = {
                "model": "deepseek/deepseek-v4-flash",
                "provider": "DeepInfra",
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "secret answer"}}
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "cost": 0.01,
                    "prompt_tokens_details": {"cached_tokens": 50},
                    "completion_tokens_details": {"reasoning_tokens": 7},
                },
            }

            record = ledger.record(
                response,
                "deepseek/deepseek-v4-flash-20260423",
                provider_routes=("DeepInfra",),
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(record, saved)
            self.assertNotIn("secret answer", path.read_text(encoding="utf-8"))
            self.assertEqual(saved["provider"], "DeepInfra")
            self.assertTrue(saved["response_complete"])
            self.assertEqual(saved["cached_tokens"], 50)
            self.assertEqual(saved["reasoning_tokens"], 7)
            self.assertFalse(ledger.can_start_request())
            self.assertEqual(ledger.summary()["calls"], 1)
            self.assertEqual(ledger.summary()["errors"], 0)
            self.assertEqual(ledger.summary()["malformed_responses"], 0)
            self.assertEqual(ledger.summary()["cached_tokens"], 50)

            resumed = UsageLedger(path, max_cost_usd=0.01)
            self.assertEqual(resumed.summary()["calls"], 1)
            self.assertEqual(resumed.summary()["errors"], 0)
            self.assertEqual(resumed.summary()["malformed_responses"], 0)
            self.assertEqual(resumed.summary()["cached_tokens"], 50)
            self.assertAlmostEqual(resumed.summary()["cost_usd"], 0.01)
            self.assertFalse(resumed.can_start_request())

    def test_usage_ledger_counts_content_free_http_200_as_malformed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            ledger = UsageLedger(path, max_cost_usd=1.0)

            saved = ledger.record(
                {
                    "model": "deepseek/deepseek-v4-flash",
                    "provider": "GMICloud",
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": None}}
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 0,
                        "cost": 0.001,
                    },
                },
                "deepseek/deepseek-v4-flash-20260423",
                provider_routes=("GMICloud",),
            )

            self.assertFalse(saved["response_complete"])
            self.assertEqual(ledger.summary()["malformed_responses"], 1)
            resumed = UsageLedger(path, max_cost_usd=1.0)
            self.assertEqual(resumed.summary()["malformed_responses"], 1)

    def test_proxy_turns_null_and_blank_completions_into_retryable_502(self):
        for content in (None, " \t\n"):
            with self.subTest(content=content):
                result = self._proxy_completion(content)
                response = json.loads(result["client_body"])
                records = result["records"]
                summary = result["summary"]

                self.assertEqual(result["status"], HTTPStatus.BAD_GATEWAY)
                self.assertEqual(
                    response,
                    {
                        "error": "OpenRouter returned an empty completion",
                        "retryable": True,
                    },
                )
                self.assertNotIn(result["private_detail"], result["client_body"])
                self.assertEqual(len(records), 1)
                self.assertFalse(records[0]["response_complete"])
                self.assertFalse(records[0]["unknown_cost"])
                self.assertNotIn('"content"', result["ledger"])
                self.assertNotIn(result["private_detail"], result["ledger"])
                self.assertEqual(summary["calls"], 1)
                self.assertEqual(summary["errors"], 0)
                self.assertEqual(summary["malformed_responses"], 1)
                self.assertAlmostEqual(summary["cost_usd"], 0.01)
                self.assertEqual(summary["reserved_cost_usd"], 0.0)

    def test_proxy_forwards_valid_completion_and_releases_reservation(self):
        content = "valid assistant answer"
        result = self._proxy_completion(content)
        response = json.loads(result["client_body"])
        records = result["records"]
        summary = result["summary"]

        self.assertEqual(result["status"], HTTPStatus.OK)
        self.assertEqual(response["choices"][0]["message"]["content"], content)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["response_complete"])
        self.assertFalse(records[0]["unknown_cost"])
        self.assertNotIn(content, result["ledger"])
        self.assertNotIn(result["private_detail"], result["ledger"])
        self.assertEqual(summary["calls"], 1)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["malformed_responses"], 0)
        self.assertAlmostEqual(summary["cost_usd"], 0.01)
        self.assertEqual(summary["reserved_cost_usd"], 0.0)

    def test_usage_ledger_records_upstream_errors_without_response_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            ledger = UsageLedger(path, max_cost_usd=1.0)
            body = json.dumps(
                {
                    "error": {
                        "message": "Provider temporarily rate-limited",
                        "code": 429,
                        "metadata": {
                            "provider_name": "DeepInfra",
                            "raw": "private upstream response",
                        },
                    }
                }
            ).encode()

            record = ledger.record_error(
                429,
                "deepseek/pinned",
                body,
                provider_routes=("DeepInfra",),
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(record, saved)
            self.assertEqual(saved["event"], "upstream_error")
            self.assertEqual(saved["status"], 429)
            self.assertEqual(saved["provider"], "DeepInfra")
            self.assertEqual(saved["error_code"], "429")
            self.assertEqual(saved["error_type"], "unknown")
            self.assertNotIn("error_message", saved)
            self.assertNotIn(
                "Provider temporarily rate-limited", path.read_text(encoding="utf-8")
            )
            self.assertNotIn(
                "private upstream response", path.read_text(encoding="utf-8")
            )
            self.assertEqual(ledger.summary()["calls"], 0)
            self.assertEqual(ledger.summary()["errors"], 1)

            resumed = UsageLedger(path, max_cost_usd=1.0)
            self.assertEqual(resumed.summary()["calls"], 0)
            self.assertEqual(resumed.summary()["errors"], 1)

    def test_proxy_sanitizes_http_error_body_and_preserves_status_and_cost(self):
        private_detail = "sk-or-v1-" + "private-upstream-fragment" * 2

        class UpstreamHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.dumps(
                    {
                        "error": {
                            "message": private_detail,
                            "code": "rate_limited",
                            "type": "rate_limit_error",
                            "metadata": {
                                "provider_name": "DeepInfra",
                                "raw": private_detail,
                            },
                        },
                        "usage": {
                            "prompt_tokens": 3,
                            "completion_tokens": 2,
                            "prompt_tokens_details": {"cached_tokens": 1},
                            "completion_tokens_details": {"reasoning_tokens": 1},
                            "cost": 0.25,
                        },
                    }
                ).encode("utf-8")
                self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return None

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                usage_path = Path(directory) / "usage.jsonl"
                ledger = UsageLedger(
                    usage_path,
                    max_cost_usd=1.0,
                    request_reservation_usd=0.05,
                    safety_reserve_usd=0.01,
                )
                handler = make_handler(
                    api_key="runtime-secret",
                    upstream=f"http://127.0.0.1:{upstream.server_port}/upstream",
                    provider_only="DeepInfra",
                    reasoning_effort=None,
                    ledger=ledger,
                    timeout=2.0,
                    max_request_bytes=4096,
                    max_response_bytes=4096,
                    allow_insecure_test_upstream=True,
                )
                proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
                proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
                proxy_thread.start()
                try:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", proxy.server_port, timeout=3.0
                    )
                    connection.request(
                        "POST",
                        "/v1/chat/completions",
                        body=json.dumps({"model": "deepseek/pinned", "messages": []}),
                        headers={"Content-Type": "application/json"},
                    )
                    response = connection.getresponse()
                    client_body = response.read().decode("utf-8")
                    status = response.status
                    connection.close()
                finally:
                    proxy.shutdown()
                    proxy.server_close()
                    proxy_thread.join(timeout=2.0)

                saved = json.loads(usage_path.read_text(encoding="utf-8"))
                self.assertEqual(status, HTTPStatus.TOO_MANY_REQUESTS)
                self.assertEqual(
                    json.loads(client_body), {"error": "OpenRouter request failed"}
                )
                self.assertNotIn(private_detail, client_body)
                self.assertNotIn(private_detail, usage_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["event"], "upstream_error")
                self.assertEqual(saved["status"], HTTPStatus.TOO_MANY_REQUESTS)
                self.assertEqual(saved["provider"], "DeepInfra")
                self.assertEqual(saved["error_type"], "rate_limit_error")
                self.assertEqual(saved["cost_usd"], 0.25)
                self.assertEqual(saved["prompt_tokens"], 3)
                self.assertEqual(saved["cached_tokens"], 1)
                self.assertEqual(saved["completion_tokens"], 2)
                self.assertEqual(saved["reasoning_tokens"], 1)
                self.assertAlmostEqual(ledger.summary()["cost_usd"], 0.25)
                self.assertEqual(ledger.summary()["reserved_cost_usd"], 0.0)
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2.0)

    def test_proxy_signals_emit_exact_stop_evidence_and_release_port(self):
        repository_root = Path(__file__).resolve().parents[1]
        for stop_signal in (signal.SIGINT, signal.SIGTERM):
            with (
                self.subTest(stop_signal=stop_signal),
                tempfile.TemporaryDirectory() as directory,
            ):
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]

                usage_path = Path(directory) / "usage.jsonl"
                environment = {
                    "LANG": "C",
                    "LC_ALL": "C",
                    "OPENROUTER_API_KEY": "runtime-secret",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": str(repository_root),
                }
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-S",
                        "-B",
                        "-m",
                        "narratordb.benchmarks.openrouter_proxy",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--provider-only",
                        "DeepInfra",
                        "--model-route",
                        "z-ai/glm-5.2=StreamLake,DeepInfra",
                        "--model-route",
                        "deepseek/deepseek-v4-flash=AtlasCloud",
                        "--model-output-token-parameter",
                        "z-ai/glm-5.2=max_completion_tokens",
                        "--model-output-token-parameter",
                        "deepseek/deepseek-v4-flash=max_tokens",
                        "--model-omit-temperature",
                        "z-ai/glm-5.2",
                        "--model-omit-temperature",
                        "openai/gpt-5.4-mini",
                        "--model-reasoning-effort",
                        "z-ai/glm-5.2=none",
                        "--model-reasoning-effort",
                        "deepseek/deepseek-v4-flash=medium",
                        "--usage-log",
                        str(usage_path),
                        "--max-cost-usd",
                        "1.0",
                    ],
                    cwd=repository_root,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 5.0
                    while True:
                        if process.poll() is not None:
                            stdout, stderr = process.communicate()
                            self.fail(
                                "proxy exited before listening: "
                                f"stdout={stdout!r}, stderr={stderr!r}"
                            )
                        try:
                            with socket.create_connection(
                                ("127.0.0.1", port), timeout=0.05
                            ):
                                break
                        except OSError:
                            if time.monotonic() >= deadline:
                                self.fail("proxy did not start listening")
                            time.sleep(0.02)

                    process.send_signal(stop_signal)
                    stdout, stderr = process.communicate(timeout=5.0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5.0)

                self.assertEqual(process.returncode, 0)
                self.assertEqual(stderr, "")
                lines = stdout.splitlines()
                self.assertEqual(len(lines), 2)
                started, stopped = (json.loads(line) for line in lines)
                self.assertTrue(started["ok"])
                self.assertEqual(started["url"], f"http://127.0.0.1:{port}/v1")
                self.assertEqual(
                    started["model_routes"],
                    {
                        "z-ai/glm-5.2": ["StreamLake", "DeepInfra"],
                        "deepseek/deepseek-v4-flash": ["AtlasCloud"],
                    },
                )
                self.assertEqual(
                    started["model_output_token_parameters"],
                    {
                        "z-ai/glm-5.2": "max_completion_tokens",
                        "deepseek/deepseek-v4-flash": "max_tokens",
                    },
                )
                self.assertEqual(
                    started["model_omit_temperature"],
                    ["openai/gpt-5.4-mini", "z-ai/glm-5.2"],
                )
                self.assertEqual(
                    started["model_reasoning_efforts"],
                    {
                        "deepseek/deepseek-v4-flash": "medium",
                        "z-ai/glm-5.2": "none",
                    },
                )
                self.assertEqual(
                    stopped,
                    {
                        "stopped": True,
                        "usage": {
                            "calls": 0,
                            "errors": 0,
                            "malformed_responses": 0,
                            "cost_usd": 0.0,
                            "prompt_tokens": 0,
                            "cached_tokens": 0,
                            "completion_tokens": 0,
                            "reasoning_tokens": 0,
                            "unknown_cost_attempts": 0,
                            "max_cost_usd": 1.0,
                            "request_reservation_usd": 0.05,
                            "safety_reserve_usd": 0.01,
                            "reserved_cost_usd": 0.0,
                            "scope": "process",
                            "enforcement": "soft_fuse",
                        },
                    },
                )
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", port), timeout=0.1)

    def test_error_usage_is_bounded_recorded_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            ledger = UsageLedger(path, max_cost_usd=1.0)
            body = json.dumps(
                {
                    "error": {
                        "code": "rate_limited",
                        "type": "rate_limit_error",
                        "metadata": {"provider_name": "DeepInfra"},
                    },
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "prompt_tokens_details": {"cached_tokens": 1},
                        "completion_tokens_details": {"reasoning_tokens": 1},
                        "cost": 0.25,
                    },
                }
            ).encode()

            saved = ledger.record_error(
                429,
                "deepseek/pinned",
                body,
                provider_routes=("DeepInfra",),
            )

            self.assertEqual(saved["cost_usd"], 0.25)
            self.assertEqual(saved["prompt_tokens"], 3)
            self.assertEqual(saved["cached_tokens"], 1)
            self.assertEqual(saved["completion_tokens"], 2)
            self.assertEqual(saved["reasoning_tokens"], 1)
            self.assertEqual(saved["error_type"], "rate_limit_error")
            resumed = UsageLedger(path, max_cost_usd=1.0)
            self.assertEqual(resumed.summary()["cost_usd"], 0.25)
            self.assertEqual(resumed.summary()["prompt_tokens"], 3)
            self.assertEqual(resumed.summary()["errors"], 1)

    def test_malicious_identity_and_nonfinite_usage_never_enter_ledger(self):
        secret = "sk-or-v1-" + "x" * 32
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            ledger = UsageLedger(path, max_cost_usd=1.0)
            response = {
                "model": secret,
                "provider": secret,
                "choices": [
                    {
                        "finish_reason": secret,
                        "message": {"content": "private answer"},
                    }
                ],
                "usage": {
                    "prompt_tokens": float("inf"),
                    "completion_tokens": 10**100,
                    "cost": float("nan"),
                },
            }

            saved = ledger.record(
                response,
                "deepseek/pinned",
                provider_routes=("DeepInfra",),
            )
            encoded = path.read_text(encoding="utf-8")

            self.assertNotIn(secret, encoded)
            self.assertNotIn("private answer", encoded)
            self.assertEqual(saved["response_model"], "route_mismatch")
            self.assertEqual(saved["provider"], "route_mismatch")
            self.assertEqual(saved["finish_reason"], "unknown")
            self.assertEqual(saved["prompt_tokens"], 0)
            self.assertEqual(saved["completion_tokens"], 0)
            self.assertEqual(saved["cost_usd"], 0.01)
            self.assertTrue(saved["unknown_cost"])

    def test_one_hundred_unknown_cost_attempts_exhaust_a_one_dollar_cap(self):
        ledger = UsageLedger(None, max_cost_usd=1.0)

        for attempt in range(1, 101):
            self.assertTrue(ledger.reserve_request(), attempt)
            ledger.record_error(
                HTTPStatus.BAD_GATEWAY,
                "deepseek/pinned",
                b"",
                provider_routes=("DeepInfra",),
            )
            ledger.release_request()

        summary = ledger.summary()
        self.assertEqual(summary["unknown_cost_attempts"], 100)
        self.assertAlmostEqual(summary["cost_usd"], 1.0)
        self.assertFalse(ledger.can_start_request())
        self.assertFalse(ledger.reserve_request())

    def test_proxy_fails_closed_when_success_route_identity_is_unverified(self):
        private_body = "private answer must never be forwarded"

        class UpstreamHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.dumps(
                    {
                        "model": "deepseek/wrong-model",
                        "provider": "Azure",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": private_body},
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 2,
                            "completion_tokens": 3,
                            "cost": 0.01,
                        },
                    }
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return None

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        with tempfile.TemporaryDirectory() as directory:
            usage_path = Path(directory) / "usage.jsonl"
            ledger = UsageLedger(usage_path, max_cost_usd=1.0)
            handler = make_handler(
                api_key="runtime-secret",
                upstream=f"http://127.0.0.1:{upstream.server_port}/upstream",
                provider_only="Azure",
                reasoning_effort=None,
                ledger=ledger,
                timeout=2.0,
                max_request_bytes=4096,
                max_response_bytes=4096,
                allow_insecure_test_upstream=True,
            )
            proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            proxy_thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", proxy.server_port, timeout=3.0
                )
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    body=json.dumps({"model": "deepseek/pinned", "messages": []}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                forwarded = response.read().decode("utf-8")
                connection.close()
            finally:
                proxy.shutdown()
                proxy.server_close()
                proxy_thread.join(timeout=2.0)

            saved = json.loads(usage_path.read_text(encoding="utf-8"))
            self.assertEqual(response.status, HTTPStatus.BAD_GATEWAY)
            self.assertNotIn(private_body, forwarded)
            self.assertEqual(saved["response_model"], "route_mismatch")
            self.assertEqual(saved["provider"], "Azure")
            self.assertFalse(saved["unknown_cost"])
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2.0)

    def test_proxy_ledgers_http_protocol_failures_without_exception_content(self):
        secret = "private-http-protocol-fragment"
        with tempfile.TemporaryDirectory() as directory:
            usage_path = Path(directory) / "usage.jsonl"
            ledger = UsageLedger(usage_path, max_cost_usd=1.0)
            handler = make_handler(
                api_key="runtime-secret",
                upstream="http://127.0.0.1:1/upstream",
                provider_only="Azure",
                reasoning_effort=None,
                ledger=ledger,
                timeout=2.0,
                max_request_bytes=4096,
                max_response_bytes=4096,
                allow_insecure_test_upstream=True,
            )
            proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            proxy_thread.start()
            try:
                failures = (
                    http.client.IncompleteRead(secret.encode("utf-8")),
                    http.client.BadStatusLine(secret),
                )
                for failure in failures:
                    with self.subTest(failure=type(failure).__name__):

                        class BrokenOpener:
                            def open(self, *_args: object, **_kwargs: object) -> None:
                                raise failure

                        with patch(
                            "narratordb.benchmarks.openrouter_proxy.build_opener",
                            return_value=BrokenOpener(),
                        ):
                            connection = http.client.HTTPConnection(
                                "127.0.0.1", proxy.server_port, timeout=3.0
                            )
                            connection.request(
                                "POST",
                                "/v1/chat/completions",
                                body=json.dumps(
                                    {"model": "deepseek/pinned", "messages": []}
                                ),
                                headers={"Content-Type": "application/json"},
                            )
                            response = connection.getresponse()
                            response.read()
                            connection.close()
                        self.assertEqual(response.status, HTTPStatus.BAD_GATEWAY)
            finally:
                proxy.shutdown()
                proxy.server_close()
                proxy_thread.join(timeout=2.0)

            encoded = usage_path.read_text(encoding="utf-8")
            self.assertNotIn(secret, encoded)
            events = [json.loads(line) for line in encoded.splitlines()]
            self.assertEqual(len(events), 2)
            self.assertTrue(all(event["unknown_cost"] for event in events))
            self.assertEqual(ledger.summary()["unknown_cost_attempts"], 2)

    def test_request_reservation_is_atomic_and_labeled_single_process(self):
        ledger = UsageLedger(
            None,
            max_cost_usd=0.11,
            request_reservation_usd=0.05,
            safety_reserve_usd=0.01,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            admitted = list(
                executor.map(lambda _index: ledger.reserve_request(), range(32))
            )

        self.assertEqual(sum(admitted), 2)
        self.assertFalse(ledger.can_start_request())
        self.assertEqual(ledger.summary()["scope"], "process")
        self.assertEqual(ledger.summary()["enforcement"], "soft_fuse")
        self.assertAlmostEqual(ledger.summary()["reserved_cost_usd"], 0.10)

    def test_two_fifty_cap_handles_ten_reservations_after_settled_cost(self):
        settled_cost = 0.93
        reservation = 0.05
        safety_reserve = 0.01
        concurrent_requests = 10
        projected = settled_cost + reservation * concurrent_requests + safety_reserve

        # The former $1.25 fuse cannot admit this production concurrency level.
        self.assertGreater(projected, 1.25)
        self.assertLessEqual(projected, 2.50)

        ledger = UsageLedger(
            None,
            max_cost_usd=2.50,
            request_reservation_usd=reservation,
            safety_reserve_usd=safety_reserve,
        )
        ledger.record(
            {
                "model": "deepseek/pinned",
                "provider": "DeepInfra",
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "settled"}}
                ],
                "usage": {"cost": settled_cost},
            },
            "deepseek/pinned",
            provider_routes=("DeepInfra",),
        )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrent_requests
        ) as executor:
            admitted = list(
                executor.map(
                    lambda _index: ledger.reserve_request(),
                    range(concurrent_requests),
                )
            )

        self.assertEqual(sum(admitted), concurrent_requests)
        self.assertAlmostEqual(ledger.summary()["cost_usd"], settled_cost)
        self.assertAlmostEqual(
            ledger.summary()["reserved_cost_usd"],
            reservation * concurrent_requests,
        )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrent_requests
        ) as executor:
            list(
                executor.map(
                    lambda _index: ledger.release_request(),
                    range(concurrent_requests),
                )
            )

        self.assertEqual(ledger.summary()["reserved_cost_usd"], 0.0)
        self.assertTrue(ledger.can_start_request())

    def test_resume_rejects_incomplete_nonfinite_and_unknown_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            invalid_payloads = (
                '{"event":"completion"}',
                '{"event":"completion","cost_usd":NaN}\n',
                '{"event":"private_content","cost_usd":0}\n',
            )
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        UsageLedger(path, max_cost_usd=1.0)

    def test_proxy_refuses_redirects_without_forwarding_authorization(self):
        attacker_authorizations: list[str | None] = []

        class AttackerHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                attacker_authorizations.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

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
        ledger = UsageLedger(None, max_cost_usd=1.0)
        proxy_handler = make_handler(
            api_key="runtime-secret",
            upstream=f"http://127.0.0.1:{redirector.server_port}/upstream",
            provider_only="Azure",
            reasoning_effort=None,
            ledger=ledger,
            timeout=2.0,
            max_request_bytes=4096,
            max_response_bytes=4096,
            allow_insecure_test_upstream=True,
        )
        proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), proxy_handler)
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()
        try:
            for redirect_status in (301, 302, 303):
                with self.subTest(redirect_status=redirect_status):
                    RedirectHandler.redirect_status = redirect_status
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", proxy.server_port, timeout=3.0
                    )
                    connection.request(
                        "POST",
                        "/v1/chat/completions",
                        body=json.dumps({"model": "deepseek/pinned", "messages": []}),
                        headers={"Content-Type": "application/json"},
                    )
                    response = connection.getresponse()
                    response.read()
                    connection.close()
                    self.assertEqual(response.status, redirect_status)
                    self.assertEqual(attacker_authorizations, [])
        finally:
            proxy.shutdown()
            redirector.shutdown()
            attacker.shutdown()
            proxy.server_close()
            redirector.server_close()
            attacker.server_close()
            proxy_thread.join(timeout=2.0)
            redirector_thread.join(timeout=2.0)
            attacker_thread.join(timeout=2.0)

        self.assertEqual(attacker_authorizations, [])
        self.assertEqual(ledger.summary()["errors"], 3)

    def test_proxy_upstream_is_pinned_outside_explicit_test_mode(self):
        with self.assertRaisesRegex(ValueError, "exact OpenRouter"):
            make_handler(
                api_key="runtime-secret",
                upstream="https://attacker.example/v1/chat/completions",
                provider_only="Azure",
                reasoning_effort=None,
                ledger=UsageLedger(None, max_cost_usd=1.0),
                timeout=2.0,
                max_request_bytes=4096,
            )


if __name__ == "__main__":
    unittest.main()
