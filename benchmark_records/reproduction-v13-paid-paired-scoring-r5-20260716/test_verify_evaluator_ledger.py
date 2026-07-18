#!/usr/bin/env python3
"""Self-contained tests for the sealed evaluator-ledger identity verifier."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("verify_evaluator_ledger.py")
SPEC = importlib.util.spec_from_file_location("v13_paid_ledger_verifier", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


def _completion(request_model: str, response_model: str, provider: str = "DeepInfra"):
    return {
        "cached_tokens": 0,
        "completion_tokens": 1,
        "cost_usd": 0.01,
        "event": "completion",
        "finish_reason": "stop",
        "prompt_tokens": 1,
        "provider": provider,
        "reasoning_tokens": 0,
        "request_model": request_model,
        "response_complete": True,
        "response_model": response_model,
        "status": 200,
        "timestamp": "2026-07-16T00:00:00+00:00",
        "unknown_cost": False,
    }


def _error(request_model: str):
    return {
        "cached_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.05,
        "error_code": "timeout",
        "error_type": "timeout",
        "event": "upstream_error",
        "prompt_tokens": 0,
        "provider": "unknown",
        "reasoning_tokens": 0,
        "request_model": request_model,
        "status": 504,
        "timestamp": "2026-07-16T00:00:00+00:00",
        "unknown_cost": True,
    }


ROOT = Path(__file__).resolve().parents[2]


class LedgerVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ledger = Path(self.temporary.name) / "usage.jsonl"

    def write(self, events: list[dict]) -> None:
        self.ledger.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )

    def write_raw(self, payload: bytes) -> None:
        self.ledger.write_bytes(payload)

    def valid_events(self) -> list[dict]:
        return [
            _completion("z-ai/glm-5.2", "z-ai/glm-5.2"),
            _completion(
                "deepseek/deepseek-v4-flash-20260423",
                "deepseek/deepseek-v4-flash",
                "AtlasCloud",
            ),
            _completion(
                "deepseek/deepseek-v4-flash-20260423",
                "deepseek/deepseek-v4-flash-20260423",
                "StreamLake",
            ),
        ]

    def assert_rejected(self, events: list[dict], message: str) -> None:
        self.write(events)
        with self.assertRaisesRegex(ValueError, message):
            VERIFIER.verify_ledger(self.ledger)

    def assert_mutation_rejected(
        self,
        event_index: int,
        field: str,
        value,
        message: str,
        *,
        include_error: bool = False,
    ) -> None:
        events = self.valid_events()
        if include_error:
            events.append(_error("z-ai/glm-5.2"))
        events[event_index][field] = value
        self.assert_rejected(events, message)

    def test_accepts_exact_closed_identity_mapping(self) -> None:
        self.write(self.valid_events() + [_error("z-ai/glm-5.2")])

        report = VERIFIER.verify_ledger(self.ledger)

        self.assertTrue(report["complete"])
        self.assertEqual(report["events"], 4)
        self.assertEqual(
            report["completion_counts_by_request"],
            {
                "deepseek/deepseek-v4-flash-20260423": 2,
                "z-ai/glm-5.2": 1,
            },
        )
        self.assertEqual(report["unknown_cost_attempts"], 1)
        self.assertEqual(report["cost_usd"], "0.08")

    def test_accepts_every_frozen_completion_finish_reason(self) -> None:
        for finish_reason in sorted(VERIFIER.FINISH_REASONS):
            with self.subTest(finish_reason=finish_reason):
                events = self.valid_events()
                events[0]["finish_reason"] = finish_reason
                self.write(events)
                self.assertTrue(VERIFIER.verify_ledger(self.ledger)["complete"])

    def test_accepts_frozen_error_semantics(self) -> None:
        for provider in sorted(VERIFIER.ERROR_PROVIDERS):
            with self.subTest(provider=provider):
                event = _error("z-ai/glm-5.2")
                event["provider"] = provider
                self.write(self.valid_events() + [event])
                self.assertTrue(VERIFIER.verify_ledger(self.ledger)["complete"])
        for error_code in sorted(VERIFIER.ERROR_CODES | {"123456"}):
            with self.subTest(error_code=error_code):
                event = _error("z-ai/glm-5.2")
                event["error_code"] = error_code
                self.write(self.valid_events() + [event])
                self.assertTrue(VERIFIER.verify_ledger(self.ledger)["complete"])
        for error_type in sorted(VERIFIER.ERROR_TYPES):
            with self.subTest(error_type=error_type):
                event = _error("z-ai/glm-5.2")
                event["error_type"] = error_type
                self.write(self.valid_events() + [event])
                self.assertTrue(VERIFIER.verify_ledger(self.ledger)["complete"])

    def test_rejects_empty_ledger(self) -> None:
        self.assert_rejected([], "no events")

    def test_rejects_missing_request_model_completion(self) -> None:
        self.assert_rejected(
            [_completion("z-ai/glm-5.2", "z-ai/glm-5.2")],
            "lacks a completion",
        )

    def test_rejects_cross_model_response(self) -> None:
        self.assert_rejected(
            [
                _completion("z-ai/glm-5.2", "deepseek/deepseek-v4-flash"),
                _completion(
                    "deepseek/deepseek-v4-flash-20260423",
                    "deepseek/deepseek-v4-flash",
                ),
            ],
            "request-to-response",
        )

    def test_rejects_undeclared_completion_provider(self) -> None:
        events = self.valid_events()
        events[0]["provider"] = "UnknownCloud"
        self.assert_rejected(events, "undeclared completion provider")

    def test_rejects_undeclared_event_type(self) -> None:
        events = self.valid_events()
        events[0]["event"] = "retry"
        self.assert_rejected(events, "undeclared event type")

    def test_rejects_missing_frozen_fields(self) -> None:
        for event_index, field, include_error in (
            (0, "unknown_cost", False),
            (3, "unknown_cost", True),
            (3, "cached_tokens", True),
            (3, "error_type", True),
        ):
            with self.subTest(event_index=event_index, field=field):
                events = self.valid_events()
                if include_error:
                    events.append(_error("z-ai/glm-5.2"))
                del events[event_index][field]
                self.assert_rejected(events, "invalid event fields")

    def test_rejects_timestamp_mutations(self) -> None:
        for value in (None, True, 1, "2026-07-16T00:00:00", "not-a-time"):
            with self.subTest(value=value):
                self.assert_mutation_rejected(0, "timestamp", value, "timestamp")

    def test_rejects_completion_status_mutations(self) -> None:
        for value in (True, 200.0, -1, 0, 199, 201, 1000):
            with self.subTest(value=value):
                self.assert_mutation_rejected(
                    0, "status", value, "completion status"
                )

    def test_rejects_error_status_mutations(self) -> None:
        for value in (True, 500.0, -1, 1000):
            with self.subTest(value=value):
                self.assert_mutation_rejected(
                    3,
                    "status",
                    value,
                    "upstream-error status",
                    include_error=True,
                )

    def test_rejects_token_type_and_range_mutations(self) -> None:
        for event_index, include_error in ((0, False), (3, True)):
            for field in VERIFIER.TOKEN_FIELDS:
                for value in (True, 1.5, -1, 1 << 63):
                    with self.subTest(
                        event_index=event_index, field=field, value=value
                    ):
                        self.assert_mutation_rejected(
                            event_index,
                            field,
                            value,
                            field,
                            include_error=include_error,
                        )

    def test_rejects_completion_semantic_types(self) -> None:
        mutations = (
            ("event", [], "undeclared event type"),
            ("request_model", {}, "undeclared request model"),
            ("response_model", [], "request-to-response"),
            ("provider", {}, "undeclared completion provider"),
            ("finish_reason", [], "undeclared finish_reason"),
            ("finish_reason", "other", "undeclared finish_reason"),
            ("response_complete", 1, "response_complete"),
            ("unknown_cost", 1, "unknown_cost"),
        )
        for field, value, message in mutations:
            with self.subTest(field=field, value=value):
                self.assert_mutation_rejected(0, field, value, message)

    def test_rejects_error_semantic_mutations(self) -> None:
        mutations = (
            ("provider", [], "upstream-error provider"),
            ("provider", "OtherCloud", "upstream-error provider"),
            ("error_code", [], "error_code"),
            ("error_code", "not_allowed", "error_code"),
            ("error_code", "1234567", "error_code"),
            ("error_type", [], "error_type"),
            ("error_type", "not_allowed", "error_type"),
            ("unknown_cost", 1, "unknown_cost"),
        )
        for field, value, message in mutations:
            with self.subTest(field=field, value=value):
                self.assert_mutation_rejected(
                    3, field, value, message, include_error=True
                )

    def test_rejects_cost_type_range_and_unknown_reservation_mutations(self) -> None:
        for value in (True, "0.01", [], -1, 1_000_000_001):
            with self.subTest(value=value):
                self.assert_mutation_rejected(0, "cost_usd", value, "cost_usd")
        self.assert_mutation_rejected(0, "unknown_cost", True, "unknown cost")
        self.assert_mutation_rejected(
            3,
            "cost_usd",
            0.04,
            "unknown cost",
            include_error=True,
        )

    def test_rejects_nonfinite_and_huge_json_numbers(self) -> None:
        events = self.valid_events()
        events[0]["cost_usd"] = float("nan")
        self.assert_rejected(events, "non-finite")
        events = self.valid_events()
        events[0]["prompt_tokens"] = 10**1000
        self.write(events)
        with self.assertRaises(ValueError):
            VERIFIER.verify_ledger(self.ledger)

    def test_rejects_standard_and_unicode_escaped_credentials(self) -> None:
        fake_credential = "s" + "k" + "-" + "or-v1-" + ("A" * 40)
        events = self.valid_events()
        events[0]["timestamp"] = fake_credential
        self.assert_rejected(events, "credential-like")

        escaped = (
            b'{"cached_tokens":0,"completion_tokens":1,"cost_usd":0.01,'
            b'"event":"completion","finish_reason":"stop","prompt_tokens":1,'
            b'"provider":"DeepInfra","reasoning_tokens":0,'
            b'"request_model":"z-ai/glm-5.2","response_complete":true,'
            b'"response_model":"z-ai/glm-5.2","status":200,'
            b'"timestamp":"\\u0073\\u006b\\u002dor-v1-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",'
            b'"unknown_cost":false}\n'
        )
        self.write_raw(escaped)
        with self.assertRaisesRegex(ValueError, "after JSON decoding"):
            VERIFIER.verify_ledger(self.ledger)

    def test_rejects_standard_and_unicode_escaped_content_fields(self) -> None:
        events = self.valid_events()
        events[0]["content"] = "forbidden"
        self.assert_rejected(events, "model-content fields")

        raw = json.dumps(self.valid_events()[0], sort_keys=True)[:-1]
        raw += ',"\\u0063ontent":"forbidden"}\n'
        self.write_raw(raw.encode("utf-8"))
        with self.assertRaisesRegex(ValueError, "content fields are forbidden"):
            VERIFIER.verify_ledger(self.ledger)

    def test_rejects_incomplete_final_line(self) -> None:
        self.write(self.valid_events())
        self.write_raw(self.ledger.read_bytes().rstrip(b"\n"))
        with self.assertRaisesRegex(ValueError, "incomplete final line"):
            VERIFIER.verify_ledger(self.ledger)

    def test_rejects_symlink(self) -> None:
        target = Path(self.temporary.name) / "target.jsonl"
        target.write_text("\n", encoding="utf-8")
        self.ledger.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            VERIFIER.verify_ledger(self.ledger)

    def test_rejects_duplicate_json_keys(self) -> None:
        self.ledger.write_text(
            '{"event":"completion","event":"upstream_error"}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            VERIFIER.verify_ledger(self.ledger)

    def test_regression_accepts_both_sealed_v11_pair_ledgers(self) -> None:
        pair_root = (
            ROOT
            / "reports"
            / "longmemeval-intelligence-dev42-v11-replay-v7gpt54mini-20260716"
            / "paired-evaluation"
        )
        expected = {"v7-reference": 183, "v11": 181}
        for variant, events in expected.items():
            with self.subTest(variant=variant):
                report = VERIFIER.verify_ledger(
                    pair_root / variant / "openrouter-usage.jsonl"
                )
                self.assertTrue(report["complete"])
                self.assertEqual(report["events"], events)
                self.assertEqual(report["unknown_cost_attempts"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
