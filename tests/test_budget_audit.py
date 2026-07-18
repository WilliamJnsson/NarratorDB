from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from narratordb.benchmarks.budget_audit import (
    LEGACY_SCHEMA,
    SCHEMA,
    audit_campaign_budget,
    main,
)
from narratordb.compiler import (
    CompilerTransportError,
    CompilerUsage,
    ContentFreeUsageLedger,
)


class CampaignBudgetAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.record = self.root / "prior-record.json"
        self.record.write_text('{"status":"immutable"}\n', encoding="utf-8")
        self.compiler = self.root / "compiler.jsonl"
        self.compiler.write_text(
            json.dumps(
                {
                    "timestamp": "2026-07-16T00:00:00Z",
                    "event": "compiler_usage",
                    "request_model": "openai/gpt-5.4-mini",
                    "response_model": "openai/gpt-5.4-mini",
                    "provider": "Azure",
                    "finish_reason": "stop",
                    "prompt_tokens": 10,
                    "cached_tokens": 0,
                    "completion_tokens": 4,
                    "reasoning_tokens": 1,
                    "cost_usd": 1.25,
                    "attempt": 1,
                    "cost_source": "provider",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.evaluator = self.root / "evaluator.jsonl"
        self.evaluator.write_text(
            json.dumps(
                {
                    "timestamp": "2026-07-16T00:00:01Z",
                    "event": "completion",
                    "status": 200,
                    "request_model": "model/answerer",
                    "response_model": "model/answerer",
                    "provider": "Provider",
                    "finish_reason": "stop",
                    "response_complete": True,
                    "prompt_tokens": 20,
                    "cached_tokens": 2,
                    "completion_tokens": 5,
                    "reasoning_tokens": 1,
                    "cost_usd": 0.5,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "timestamp": "2026-07-16T00:00:02Z",
                    "event": "upstream_error",
                    "status": 429,
                    "request_model": "model/answerer",
                    "provider": "Provider",
                    "error_code": "rate_limited",
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "reasoning_tokens": 0,
                    "cost_usd": 0,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.declaration = self.root / "campaign.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _document(self) -> dict:
        return {
            "schema": SCHEMA,
            "campaign_id": "narratordb-v8",
            "provider_cap_usd": "250",
            "governance_ceiling_eur": "300",
            "prior_immutable_costs": [
                {
                    "source_id": "v7-frozen",
                    "record_path": self.record.name,
                    "record_sha256": hashlib.sha256(
                        self.record.read_bytes()
                    ).hexdigest(),
                    "cost_usd": "24.90064606428",
                }
            ],
            "active_usage_ledgers": [
                {
                    "source_id": "v8-compiler",
                    "kind": "compiler",
                    "path": self.compiler.name,
                    "identity_policy": {
                        "request_models": [
                            "openai/gpt-5.4-mini",
                            "z-ai/glm-5.2",
                        ],
                        "providers": [
                            "Azure",
                            "deepinfra/fp4",
                            "parasail/fp4",
                            "wafer/fp4",
                        ],
                    },
                },
                {
                    "source_id": "v8-evaluator",
                    "kind": "evaluator",
                    "path": self.evaluator.name,
                    "identity_policy": {
                        "request_models": ["model/answerer"],
                        "response_models": ["model/answerer"],
                        "providers": ["Provider"],
                    },
                },
            ],
        }

    def _write(self, document: dict | None = None) -> None:
        self.declaration.write_text(
            json.dumps(document or self._document(), indent=2) + "\n",
            encoding="utf-8",
        )

    def test_aggregates_disjoint_sources_and_records_eur_without_fx(self) -> None:
        self._write()
        source_state = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (self.record, self.compiler, self.evaluator)
        }

        report = audit_campaign_budget(self.declaration)

        self.assertTrue(report["complete"])
        self.assertEqual(report["observed_spend_usd"], "26.65064606428")
        self.assertEqual(
            report["spend_breakdown_usd"],
            {
                "prior_immutable_records": "24.90064606428",
                "active_usage_ledgers": "1.75",
            },
        )
        self.assertEqual(report["provider_limit"]["enforced_cap_usd"], "250")
        self.assertEqual(
            report["provider_limit"]["remaining_headroom_usd"],
            "223.34935393572",
        )
        self.assertEqual(report["governance_limit"]["ceiling_eur"], "300")
        self.assertIsNone(report["governance_limit"]["observed_eur"])
        self.assertIsNone(report["governance_limit"]["remaining_headroom_eur"])
        self.assertFalse(report["aggregation_policy"]["currency_conversion_performed"])
        self.assertEqual([source["events"] for source in report["sources"][1:]], [1, 2])
        for path, state in source_state.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), state)

    def test_accepts_content_free_compiler_error_events(self) -> None:
        ledger = ContentFreeUsageLedger(self.compiler)
        ledger.record_error(
            CompilerTransportError(
                "upstream request failed",
                code="http_error",
                retryable=True,
                status=429,
                retry_after_seconds=12.5,
                rate_limit_reset_at=1_783_200_000.25,
                rate_limit_limit=100,
                rate_limit_remaining=0,
                error_type="rate_limit_error",
                provider_name="deepinfra/fp4",
                provider_code="rate_limited",
                router_attempt=2,
                attempted_providers=("parasail/fp4", "deepinfra/fp4"),
                attempt_statuses=(429, 429),
                response_usage={
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "completion_tokens_details": {"reasoning_tokens": 1},
                    "cost": 0.25,
                },
            ),
            request_model="z-ai/glm-5.2",
            attempt=2,
        )
        self._write()

        report = audit_campaign_budget(self.declaration)

        self.assertEqual(report["observed_spend_usd"], "26.90064606428")
        compiler_source = report["sources"][1]
        self.assertEqual(compiler_source["events"], 2)
        self.assertEqual(compiler_source["cost_usd"], "1.5")

    def test_unknown_cost_reservation_is_counted_and_blocks_publication(self) -> None:
        ledger = ContentFreeUsageLedger(
            self.compiler,
            max_cost_usd=10.0,
            request_reservation_usd=0.05,
        )
        self.assertTrue(ledger.reserve_request())
        ledger.record_error(
            CompilerTransportError(
                "HTTP protocol failed",
                code="http_protocol_error",
                retryable=True,
            ),
            request_model="z-ai/glm-5.2",
            attempt=1,
        )
        ledger.release_request()
        self._write()

        report = audit_campaign_budget(self.declaration)

        self.assertFalse(report["complete"])
        self.assertEqual(report["unknown_cost_attempts"], 1)
        self.assertEqual(report["observed_spend_usd"], "26.70064606428")
        self.assertFalse(report["publication_readiness"]["ready"])
        self.assertFalse(report["publication_readiness"]["unknown_costs_reconciled"])
        compiler_source = report["sources"][1]
        self.assertEqual(compiler_source["unknown_cost_attempts"], 1)

    def test_success_identity_sentinels_block_budget_publication(self) -> None:
        event = json.loads(self.evaluator.read_text(encoding="utf-8").splitlines()[0])
        event["response_model"] = "route_mismatch"
        self.evaluator.write_text(json.dumps(event) + "\n", encoding="utf-8")
        self._write()

        report = audit_campaign_budget(self.declaration)

        self.assertFalse(report["complete"])
        self.assertEqual(report["invalid_completion_identities"], 1)
        self.assertFalse(
            report["publication_readiness"]["successful_completion_identities_verified"]
        )

    def test_accepts_paired_router_attempt_metadata_from_real_ledger(self) -> None:
        ledger = ContentFreeUsageLedger(self.compiler)
        ledger.record(
            CompilerUsage(
                request_model="z-ai/glm-5.2",
                response_model="z-ai/glm-5.2",
                provider="wafer/fp4",
                attempt=1,
                prompt_tokens=3,
                cached_tokens=0,
                completion_tokens=2,
                reasoning_tokens=1,
                cost_usd=0.25,
                cost_source="provider",
                finish_reason="stop",
                router_attempt=2,
                attempted_providers=("parasail/fp4", "wafer/fp4"),
                attempt_statuses=(429, 200),
            )
        )
        self._write()

        report = audit_campaign_budget(self.declaration)

        self.assertEqual(report["observed_spend_usd"], "26.90064606428")
        self.assertEqual(report["sources"][1]["events"], 2)

        malformed = json.loads(self.compiler.read_text().splitlines()[-1])
        malformed["attempt_statuses"] = [429]
        self.compiler.write_text(json.dumps(malformed) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "arrays must align"):
            audit_campaign_budget(self.declaration)

    def test_cli_override_is_explicit_and_enforced(self) -> None:
        document = self._document()
        document["provider_cap_usd"] = "180"
        document["prior_immutable_costs"][0]["cost_usd"] = "249"
        self._write(document)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            status = main(
                [
                    "--declaration",
                    str(self.declaration),
                    "--provider-cap-usd",
                    "250",
                ]
            )

        report = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["observed_spend_usd"], "250.75")
        self.assertEqual(report["provider_limit"]["declared_cap_usd"], "180")
        self.assertEqual(report["provider_limit"]["enforced_cap_usd"], "250")
        self.assertEqual(report["provider_limit"]["cap_source"], "cli_override")
        self.assertEqual(report["provider_limit"]["remaining_headroom_usd"], "0")
        self.assertEqual(report["provider_limit"]["overage_usd"], "0.75")

    def test_duplicate_ids_paths_file_identities_and_content_are_rejected(self) -> None:
        cases: list[tuple[str, str]] = []
        duplicate_id = self._document()
        duplicate_id["active_usage_ledgers"][1]["source_id"] = "v8-compiler"
        cases.append((json.dumps(duplicate_id), "duplicate spend source id"))

        duplicate_path = self._document()
        duplicate_path["active_usage_ledgers"][1]["path"] = self.compiler.name
        cases.append((json.dumps(duplicate_path), "duplicate spend source path"))

        copied = self.root / "compiler-copy.jsonl"
        copied.write_bytes(self.compiler.read_bytes())
        duplicate_content = self._document()
        duplicate_content["active_usage_ledgers"][1] = {
            "source_id": "v8-compiler-copy",
            "kind": "compiler",
            "path": copied.name,
            "identity_policy": duplicate_content["active_usage_ledgers"][0][
                "identity_policy"
            ],
        }
        cases.append((json.dumps(duplicate_content), "duplicate spend source content"))

        for index, (payload, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                path = self.root / f"duplicate-{index}.json"
                path.write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected):
                    audit_campaign_budget(path)

    def test_nonfinite_negative_and_duplicate_json_values_are_rejected(self) -> None:
        cases = [
            ('"provider_cap_usd": "250"', '"provider_cap_usd": NaN', "non-finite"),
            (
                '"governance_ceiling_eur": "300"',
                '"governance_ceiling_eur": -1',
                "negative",
            ),
            ('"cost_usd": 1.25', '"cost_usd": -0.01', "negative"),
        ]
        base = json.dumps(self._document())
        for index, (old, new, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                payload = base.replace(old, new, 1)
                path = self.root / f"invalid-{index}.json"
                path.write_text(payload, encoding="utf-8")
                if index == 2:
                    original = self.compiler.read_text(encoding="utf-8")
                    self.compiler.write_text(
                        original.replace('"cost_usd": 1.25', '"cost_usd": -0.01'),
                        encoding="utf-8",
                    )
                try:
                    with self.assertRaisesRegex(ValueError, expected):
                        audit_campaign_budget(path)
                finally:
                    if index == 2:
                        self.compiler.write_text(original, encoding="utf-8")

        duplicate_key = json.dumps(self._document()).replace(
            '"campaign_id": "narratordb-v8"',
            '"campaign_id": "narratordb-v8", "campaign_id": "again"',
        )
        self.declaration.write_text(duplicate_key, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
            audit_campaign_budget(self.declaration)

    def test_secrets_and_model_content_fields_are_rejected_without_echoing_values(
        self,
    ) -> None:
        self._write()
        secret = "sk-or-v1-" + "x" * 32
        self.compiler.write_text(
            self.compiler.read_text(encoding="utf-8").replace("Azure", secret),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError) as raised:
            audit_campaign_budget(self.declaration)
        self.assertNotIn(secret, str(raised.exception))

        self.compiler.write_text(
            json.dumps(
                {
                    "event": "compiler_usage",
                    "cost_usd": 0.1,
                    "prompt": "private model input that must not enter the report",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ValueError, "model content fields are forbidden"
        ) as raised:
            audit_campaign_budget(self.declaration)
        self.assertNotIn("private model input", str(raised.exception))

    def test_record_checksum_and_incomplete_ledger_are_rejected(self) -> None:
        document = self._document()
        document["prior_immutable_costs"][0]["record_sha256"] = "0" * 64
        self._write(document)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            audit_campaign_budget(self.declaration)

        self._write()
        self.compiler.write_text(
            '{"event":"compiler_usage","cost_usd":1}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "incomplete final line"):
            audit_campaign_budget(self.declaration)

    def test_malformed_event_and_negative_metadata_are_rejected_cleanly(self) -> None:
        self._write()
        for event, expected in (
            ({"event": [], "cost_usd": 0}, "unexpected event type"),
            (
                {"event": "compiler_usage", "cost_usd": 0, "provider": -1},
                "missing",
            ),
        ):
            with self.subTest(expected=expected):
                self.compiler.write_text(json.dumps(event) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected):
                    audit_campaign_budget(self.declaration)

    def test_compiler_error_schema_rejects_non_sanitized_metadata(self) -> None:
        self._write()
        valid = {
            "timestamp": "2026-07-16T00:00:03+00:00",
            "event": "compiler_error",
            "request_model": "z-ai/glm-5.2",
            "attempt": 1,
            "code": "http_error",
            "status": 429,
            "retryable": True,
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": 0.0,
        }
        cases = (
            ({**valid, "response_model": "unexpected"}, "invalid .* fields"),
            (
                {key: value for key, value in valid.items() if key != "timestamp"},
                "missing",
            ),
            ({**valid, "retryable": 1}, "retryable must be boolean"),
            ({**valid, "status": "429"}, "status must be a non-negative integer"),
            (
                {**valid, "provider": "provider metadata? with free-form punctuation"},
                "canonical provider identity",
            ),
            ({**valid, "error_type": "rate limit error"}, "sanitized code token"),
            ({**valid, "retry_after_seconds": 86_401}, "exceeds the supported limit"),
            (
                {**valid, "prompt": "private request content"},
                "content fields are forbidden",
            ),
        )
        for event, expected in cases:
            with self.subTest(expected=expected):
                self.compiler.write_text(json.dumps(event) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected):
                    audit_campaign_budget(self.declaration)

    def test_v2_identity_policy_rejects_token_shaped_metadata_and_minimal_events(
        self,
    ) -> None:
        self._write()
        minimal = {"event": "compiler_usage", "cost_usd": 0}
        self.compiler.write_text(json.dumps(minimal) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing"):
            audit_campaign_budget(self.declaration)

        event = {
            "timestamp": "2026-07-16T00:00:00Z",
            "event": "compiler_usage",
            "request_model": "openai/gpt-5.4-mini",
            "response_model": "openai/gpt-5.4-mini",
            "provider": "KyotoIsHome",
            "finish_reason": "stop",
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": 0,
            "attempt": 1,
            "cost_source": "provider",
        }
        self.compiler.write_text(json.dumps(event) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not declared"):
            audit_campaign_budget(self.declaration)

        event["provider"] = "Azure"
        event["finish_reason"] = "KyotoIsHome"
        self.compiler.write_text(json.dumps(event) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "finish_reason is not canonical"):
            audit_campaign_budget(self.declaration)

    def test_v2_evaluator_policy_accepts_dated_alias_and_rejects_undeclared_identity(
        self,
    ) -> None:
        document = self._document()
        evaluator_policy = document["active_usage_ledgers"][1]["identity_policy"]
        evaluator_policy["request_models"] = ["deepseek/deepseek-v4-flash-20260423"]
        evaluator_policy["response_models"] = ["deepseek/deepseek-v4-flash"]
        evaluator_policy["providers"] = ["DeepInfra"]
        event = json.loads(self.evaluator.read_text(encoding="utf-8").splitlines()[0])
        event["request_model"] = "deepseek/deepseek-v4-flash-20260423"
        event["response_model"] = "deepseek/deepseek-v4-flash"
        event["provider"] = "DeepInfra"
        self.evaluator.write_text(json.dumps(event) + "\n", encoding="utf-8")
        self._write(document)

        report = audit_campaign_budget(self.declaration)
        self.assertEqual(
            report["aggregation_policy"]["compiler_and_evaluator_identity_policy"],
            "declared_closed_world",
        )

        event["response_model"] = "deepseek/another-model"
        self.evaluator.write_text(json.dumps(event) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not declared"):
            audit_campaign_budget(self.declaration)

    def test_legacy_v1_declaration_is_readable_and_explicitly_labeled_weaker(
        self,
    ) -> None:
        document = self._document()
        document["schema"] = LEGACY_SCHEMA
        for ledger in document["active_usage_ledgers"]:
            ledger.pop("identity_policy")
        self._write(document)

        report = audit_campaign_budget(self.declaration)

        self.assertEqual(
            report["aggregation_policy"]["compiler_and_evaluator_identity_policy"],
            "legacy_v1_syntax_only",
        )
        self.assertTrue(
            all(
                source.get("identity_policy") == "legacy_syntax_only"
                for source in report["sources"]
                if source["source_type"].startswith("active_")
            )
        )


if __name__ == "__main__":
    unittest.main()
