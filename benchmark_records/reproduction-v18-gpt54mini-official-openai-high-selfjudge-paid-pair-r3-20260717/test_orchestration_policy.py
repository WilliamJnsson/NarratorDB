#!/usr/bin/env python3
"""Offline invariants for R3 admission, canary, watchdog, and orchestration."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ADMISSION = _load("v18_r3_admission", "admit_openai_campaign.py")
CANARY = _load("v18_r3_canary", "route_canary.py")


class AdmissionPolicyTests(unittest.TestCase):
    def test_allocation_includes_both_worst_case_canary_reservations(self) -> None:
        self.assertEqual(ADMISSION.PRIOR_TRACKED_MAXIMUM, Decimal("2.878283432"))
        self.assertEqual(ADMISSION.CANARY_FUSE, Decimal("0.611152"))
        self.assertEqual(ADMISSION.ARM_FUSES, Decimal("4.90"))
        self.assertEqual(ADMISSION.NEW_ALLOCATION, Decimal("5.511152"))
        self.assertEqual(ADMISSION.TRACKED_CUMULATIVE, Decimal("8.389435432"))
        self.assertLessEqual(
            ADMISSION.NEW_ALLOCATION, ADMISSION.ATTESTED_AVAILABLE_BALANCE
        )

    def test_history_and_official_pricing_hashes_are_exact(self) -> None:
        r1 = ROOT / (
            "reports/longmemeval-intelligence-dev42-v18-gpt54mini-openai-high-"
            "selfjudge-paid-pair-r1-20260717/ABORTED_AFTER_PRIMARY_AUDIT.json"
        )
        r2 = ROOT / (
            "reports/longmemeval-intelligence-dev42-v18-gpt54mini-openai-high-"
            "selfjudge-paid-pair-r2-20260717/TERMINAL_EXECUTION_STATUS.json"
        )
        self.assertEqual(ADMISSION._sha256(r1), ADMISSION.EXPECTED_R1_TERMINAL_SHA256)
        self.assertEqual(ADMISSION._sha256(r2), ADMISSION.EXPECTED_R2_TERMINAL_SHA256)
        self.assertEqual(
            ADMISSION._sha256(HERE / "R2_TERMINAL_DISCLOSURE.json"),
            ADMISSION.EXPECTED_R2_DISCLOSURE_SHA256,
        )
        self.assertEqual(
            ADMISSION._sha256(HERE / "OFFICIAL_OPENAI_MODEL_AND_PRICING.json"),
            ADMISSION.EXPECTED_PRICING_SHA256,
        )

    def test_synthetic_local_admission_performs_no_provider_telemetry(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        r1 = ROOT / (
            "reports/longmemeval-intelligence-dev42-v18-gpt54mini-openai-high-"
            "selfjudge-paid-pair-r1-20260717/ABORTED_AFTER_PRIMARY_AUDIT.json"
        )
        r2 = ROOT / (
            "reports/longmemeval-intelligence-dev42-v18-gpt54mini-openai-high-"
            "selfjudge-paid-pair-r2-20260717/TERMINAL_EXECUTION_STATUS.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            fx = temporary / "fx.json"
            output = temporary / "admission.json"
            fx.write_text(
                json.dumps(
                    {
                        "schema_version": "narratordb.ecb-usd-eur-observation.v1",
                        "publisher": "European Central Bank",
                        "base_currency": "EUR",
                        "quote_currency": "USD",
                        "usd_per_eur": "1.2",
                        "retrieved_at_utc": now,
                        "parser_sha256": ADMISSION._sha256(
                            HERE / "admit_openai_campaign.py"
                        ),
                        "credential_recorded": False,
                        "model_content_recorded": False,
                    }
                ),
                encoding="utf-8",
            )
            argv = [
                "admit_openai_campaign.py",
                "--fx",
                str(fx),
                "--pricing-evidence",
                str(HERE / "OFFICIAL_OPENAI_MODEL_AND_PRICING.json"),
                "--prior-r1-terminal",
                str(r1),
                "--prior-r2-terminal",
                str(r2),
                "--r2-disclosure",
                str(HERE / "R2_TERMINAL_DISCLOSURE.json"),
                "--output",
                str(output),
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                self.assertEqual(ADMISSION.main(), 0)
            admitted = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(admitted["admitted"])
            self.assertEqual(admitted["new_allocation_usd"], "5.511152")
            self.assertEqual(
                admitted["tracked_cumulative_maximum_usd"], "8.389435432"
            )
            self.assertEqual(
                admitted["balance_attestation"]["verification"],
                "not_api_verified",
            )
            self.assertFalse(admitted["provider_telemetry_performed"])


class CanaryPolicyTests(unittest.TestCase):
    def test_canary_uses_official_completion_limit(self) -> None:
        for label, payload in CANARY.CASES:
            with self.subTest(label=label):
                self.assertEqual(payload["max_completion_tokens"], 128)
                self.assertNotIn("max_tokens", payload)
                self.assertNotIn("temperature", payload)

    def test_exact_cost_does_not_double_charge_reasoning(self) -> None:
        usage = {
            "prompt_tokens": 741_933,
            "completion_tokens": 68_284,
            "total_tokens": 810_217,
            "prompt_tokens_details": {"cached_tokens": 219_904},
            "completion_tokens_details": {"reasoning_tokens": 54_396},
        }
        self.assertEqual(CANARY._usage_cost(usage), Decimal("0.715292550"))
        usage["completion_tokens_details"]["reasoning_tokens"] = 0
        self.assertEqual(CANARY._usage_cost(usage), Decimal("0.715292550"))


class _HealthState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.failures_remaining = 0
        self.transport_failed = False
        self.active = 0
        self.reserved = "0.000000000"


class _HealthHandler(BaseHTTPRequestHandler):
    state: _HealthState

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:  # noqa: N802
        with self.state.lock:
            if self.state.failures_remaining:
                self.state.failures_remaining -= 1
                self.send_response(503)
                self.end_headers()
                return
            document = {
                "ok": True,
                "usage": {
                    "transport_failed": self.state.transport_failed,
                    "fatal_reason_code": (
                        "terminal_response" if self.state.transport_failed else None
                    ),
                    "active_logical_calls": self.state.active,
                    "reserved_cost_usd": self.state.reserved,
                },
            }
        raw = json.dumps(document).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class WatchdogPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = (HERE / "launch_with_openai_key.sh").read_text(encoding="utf-8")
        start = source.index("<<'PY'", source.index("run_with_watchdog()")) + len(
            "<<'PY'"
        )
        end = source.index("\nPY\n}", start)
        cls.watchdog_source = source[start:end].lstrip("\n")

    def _server(self, state: _HealthState):
        handler = type("HealthHandler", (_HealthHandler,), {"state": state})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _watchdog(self, port: int, child_seconds: float) -> subprocess.Popen[str]:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as output:
            output.write(self.watchdog_source)
            script = Path(output.name)
        self.addCleanup(script.unlink, missing_ok=True)
        return subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(script),
                str(port),
                "-",
                sys.executable,
                "-c",
                f"import time; time.sleep({child_seconds})",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_fatal_waits_for_two_active_requests_to_drain(self) -> None:
        state = _HealthState()
        state.active = 2
        state.reserved = "0.636864000"
        server, thread = self._server(state)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        process = self._watchdog(server.server_port, 20)
        time.sleep(0.3)
        with state.lock:
            state.transport_failed = True

        def drain() -> None:
            time.sleep(0.7)
            with state.lock:
                state.active = 0
                state.reserved = "0.000000000"

        drainer = threading.Thread(target=drain)
        drainer.start()
        started = time.monotonic()
        _, stderr = process.communicate(timeout=10)
        elapsed = time.monotonic() - started
        drainer.join()
        server.shutdown()
        thread.join()
        self.assertEqual(process.returncode, 97)
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertIn('"accounting_drained": true', stderr)

    def test_two_health_read_failures_do_not_kill_child(self) -> None:
        state = _HealthState()
        state.failures_remaining = 2
        server, thread = self._server(state)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        process = self._watchdog(server.server_port, 1.2)
        _, stderr = process.communicate(timeout=10)
        server.shutdown()
        thread.join()
        self.assertEqual(process.returncode, 0, stderr)

    def test_drain_timeout_has_distinct_accounting_exit(self) -> None:
        source = self.watchdog_source
        self.assertIn("deadline = time.monotonic() + 112", source)
        self.assertIn("raise SystemExit(97 if drained else 98)", source)


class OrchestrationSourceTests(unittest.TestCase):
    def test_no_score_or_telemetry_branch_between_arms(self) -> None:
        source = (HERE / "orchestrate_paid_pair.sh").read_text(encoding="utf-8")
        primary = source.index('launch_with_openai_key.sh" primary')
        replication = source.index('launch_with_openai_key.sh" replication')
        between = source[primary:replication]
        self.assertIn("transport_arm_audit.py", between)
        self.assertNotIn("accuracy", between.lower())
        self.assertNotIn("target_passed", between)
        self.assertNotIn("telemetry-between", between)
        self.assertNotIn("provider-telemetry", source)

    def test_staging_and_history_bindings_are_exact(self) -> None:
        source = (HERE / "orchestrate_paid_pair.sh").read_text(encoding="utf-8")
        self.assertIn(
            "6b4a949a15a842b1c2dfc9b101ffb1ba908c48c4667f559d077ec6294b403161",
            source,
        )
        self.assertIn(
            "28b3cadccad086e6dfab20c4a89fcf70b4292d8d3ecdf1b161be93c9e550ecac",
            source,
        )
        self.assertIn(ADMISSION.EXPECTED_R1_TERMINAL_SHA256, source)
        self.assertIn(ADMISSION.EXPECTED_R2_TERMINAL_SHA256, source)
        self.assertIn(ADMISSION.EXPECTED_R2_DISCLOSURE_SHA256, source)
        self.assertIn(ADMISSION.EXPECTED_PRICING_SHA256, source)

    def test_launcher_is_official_key_isolated_and_watchdog_drained(self) -> None:
        source = (HERE / "launch_with_openai_key.sh").read_text(encoding="utf-8")
        self.assertIn("/Users/william/.narratordb/openai.env", source)
        self.assertIn("OPENAI_API_KEY=local-transport", source)
        self.assertIn("PYTHON_DOTENV_DISABLED=1", source)
        self.assertIn("gpt-5.4-mini-2026-03-17", source)
        self.assertIn("RESERVE=0.318432", source)
        self.assertIn("MAX=0.611152", source)
        self.assertIn("active_logical_calls", source)
        self.assertIn("reserved_cost_usd", source)
        self.assertNotIn("sk-or-", source)
        self.assertNotIn("OPENROUTER", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
