from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRECOMMIT_DIR = ROOT / "benchmark_records" / "precommits"
BEAM_MANIFEST = PRECOMMIT_DIR / "beam_1m_scope_precommit_20260716.json"
BEAM_SUMS = PRECOMMIT_DIR / "beam_1m_scope_precommit_20260716.SHA256SUMS"
LME_V2_MANIFEST = PRECOMMIT_DIR / "longmemeval_v2_scope_precommit_20260716.json"
LME_V2_SUMS = PRECOMMIT_DIR / "longmemeval_v2_scope_precommit_20260716.SHA256SUMS"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"manifest is not an object: {path}")
    return value


def assert_checksum_file(test: unittest.TestCase, manifest: Path, sums: Path) -> None:
    fields = sums.read_text(encoding="utf-8").strip().split()
    test.assertEqual(len(fields), 2)
    expected_sha256, filename = fields
    test.assertEqual(filename, manifest.name)
    test.assertEqual(expected_sha256, sha256_file(manifest))


class BenchmarkScopePrecommitTests(unittest.TestCase):
    def test_artifact_checksums(self) -> None:
        assert_checksum_file(self, BEAM_MANIFEST, BEAM_SUMS)
        assert_checksum_file(self, LME_V2_MANIFEST, LME_V2_SUMS)

    def test_beam_1m_scope_is_exact_and_not_overclaimed(self) -> None:
        manifest = load_manifest(BEAM_MANIFEST)
        scope = manifest["dataset_scope"]
        self.assertEqual(scope["tier"], "1M")
        self.assertEqual(scope["conversations"], 35)
        self.assertEqual(scope["sampling"], "none")
        self.assertEqual(
            scope["source_file"]["sha256"],
            "41b5acbbb55a586b1305514ef9d9fb03365d9b3331b598a1c2dd7603d93ef533",
        )
        self.assertFalse(manifest["state"]["run_authorized"])
        self.assertFalse(
            manifest["verified_official_evaluator_behavior"][
                "single_headline_scalar_verified"
            ]
        )
        self.assertFalse(
            manifest["verified_official_evaluator_behavior"][
                "success_threshold_verified"
            ]
        )
        self.assertIn(
            "not globally hidden",
            manifest["claim_semantics"]["allowed_label"],
        )
        self.assertEqual(
            len(manifest["prior_local_use_audit"]["preexisting_local_vendor_artifacts"]),
            2,
        )
        self.assertTrue(all(value is None for value in manifest["unresolved_protocol"].values()))

    def test_longmemeval_v2_scope_is_exact_and_blind_label_is_qualified(self) -> None:
        manifest = load_manifest(LME_V2_MANIFEST)
        scope = manifest["dataset_scope"]
        self.assertEqual(scope["questions"], 451)
        self.assertEqual(scope["question_selection"], "all questions")
        self.assertEqual(scope["sampling"], "none")
        self.assertEqual(scope["variants"], ["small", "medium"])
        expected_hashes = {
            "questions.jsonl": "0a3ae5ebea938c24d7800e1e0b0828e08ae1646f939a53853b2b8cdc08e292b7",
            "trajectories.jsonl": "363cec9a8e87aa8d9101ce4e600aadbf7031d674056ebe4f969e8424abc5f3c6",
            "haystacks/lme_v2_small.json": "9b5301defb23a088a5f06e45ff8d5f35e569d78305a66d492046a9fff9b46593",
            "haystacks/lme_v2_medium.json": "4756d5126347f0d18f045bb6c47b08cb3b23e9db24386cc48a9b2879e7969b59",
        }
        actual_hashes = {
            item["path"]: item["sha256"] for item in scope["core_files"]
        }
        self.assertEqual(
            {path: actual_hashes[path] for path in expected_hashes},
            expected_hashes,
        )
        self.assertEqual(
            manifest["official_sources"]["dataset"]["revision"],
            "f152293e235517d504809563c833d7190b8c713b",
        )
        self.assertFalse(manifest["state"]["run_authorized"])
        self.assertFalse(
            manifest["official_evaluation_status"][
                "official_submission_is_server_side_hidden_evaluation"
            ]
        )
        self.assertIn(
            "not globally secret",
            manifest["claim_semantics"]["allowed_label"],
        )
        self.assertTrue(all(value is None for value in manifest["unresolved_protocol"].values()))

    def test_answer_bearing_core_dataset_files_are_not_in_repository(self) -> None:
        forbidden_basenames = {
            "1M-00000-of-00001.parquet",
            "lme_v2_small.json",
            "lme_v2_medium.json",
            "trajectories.jsonl",
        }
        found = [
            str(path.relative_to(ROOT))
            for path in ROOT.rglob("*")
            if path.is_file() and path.name in forbidden_basenames
        ]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
