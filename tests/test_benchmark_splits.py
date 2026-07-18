from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from narratordb.benchmarks.splits import (
    DEFAULT_OUTPUT_DIR,
    DEVELOPMENT_FILENAME,
    HOLDOUT_FILENAME,
    MANIFEST_FILENAME,
    PACKAGED_OUTPUT_DIR,
    QUESTION_TYPES,
    REPOSITORY_OUTPUT_DIR,
    verify_split,
    write_split,
)


class BenchmarkSplitTests(unittest.TestCase):
    def test_source_checkout_defaults_to_repository_records(self) -> None:
        self.assertEqual(DEFAULT_OUTPUT_DIR, REPOSITORY_OUTPUT_DIR)

    def test_tracked_longmemeval_split_matches_published_hashes(self) -> None:
        report = verify_split(DEFAULT_OUTPUT_DIR)

        self.assertTrue(report["complete"])
        self.assertFalse(report["dataset_verified"])
        self.assertEqual(report["development_questions"], 42)
        self.assertEqual(report["holdout_questions"], 458)
        self.assertEqual(
            report["development_sha256"],
            "60c150dc1e7421b15ca4bc56f9bad63ea2265b4d52386ae63fd83568c3e0e3ff",
        )
        self.assertEqual(
            report["holdout_sha256"],
            "b2ebe3d023e65369cba071afd622384ce5cc4217e67ed492cd66c7b6a62c6207",
        )

    def test_packaged_split_is_identical_and_verifiable(self) -> None:
        report = verify_split(PACKAGED_OUTPUT_DIR)

        self.assertTrue(report["complete"])
        self.assertEqual(report["development_questions"], 42)
        self.assertEqual(report["holdout_questions"], 458)
        for filename in (
            DEVELOPMENT_FILENAME,
            HOLDOUT_FILENAME,
            MANIFEST_FILENAME,
        ):
            self.assertEqual(
                (PACKAGED_OUTPUT_DIR / filename).read_bytes(),
                (REPOSITORY_OUTPUT_DIR / filename).read_bytes(),
            )

    def test_generated_split_can_be_rederived_from_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "synthetic.json"
            rows = [
                {
                    "question_id": f"{question_type}-{index}",
                    "question_type": question_type,
                }
                for question_type in QUESTION_TYPES
                for index in range(8)
            ]
            dataset.write_text(json.dumps(rows), encoding="utf-8")
            output = root / "split"

            manifest = write_split(dataset, output)
            report = verify_split(output, dataset=dataset)

            self.assertTrue(report["dataset_verified"])
            self.assertEqual(
                report["development_questions"] + report["holdout_questions"],
                len(rows),
            )
            self.assertEqual(
                manifest["development"]["sha256"],
                report["development_sha256"],
            )

    def test_hash_verification_rejects_a_modified_id_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "synthetic.json"
            rows = [
                {
                    "question_id": f"{question_type}-{index}",
                    "question_type": question_type,
                }
                for question_type in QUESTION_TYPES
                for index in range(8)
            ]
            dataset.write_text(json.dumps(rows), encoding="utf-8")
            output = root / "split"
            write_split(dataset, output)

            development_path = output / DEVELOPMENT_FILENAME
            development = json.loads(development_path.read_text(encoding="utf-8"))
            holdout_path = output / HOLDOUT_FILENAME
            holdout = json.loads(holdout_path.read_text(encoding="utf-8"))
            development[0], holdout[0] = holdout[0], development[0]
            development_path.write_text(
                json.dumps(sorted(development), indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                verify_split(output)


if __name__ == "__main__":
    unittest.main()
