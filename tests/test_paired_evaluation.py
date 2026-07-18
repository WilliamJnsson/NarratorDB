from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from narratordb.benchmarks.paired_evaluation import (
    prepare_evaluation_copy,
    verify_frozen_copy_manifest,
)


class PairedEvaluationCopyTests(unittest.TestCase):
    def test_fresh_copy_is_byte_identical_and_frozen_source_is_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ids = root / "ids.json"
            ids.write_text('["q1", "q2"]\n', encoding="utf-8")
            ids_hash = hashlib.sha256(ids.read_bytes()).hexdigest()
            frozen = root / "predicted_pair-a"
            frozen.mkdir()
            (frozen / "q1.json").write_bytes(b'{"opaque":1}\n')
            (frozen / "q2.json").write_bytes(b'{"opaque":2}\n')
            (frozen / "_ingestion_q1.json").write_bytes(b'{"done":true}\n')

            manifest = prepare_evaluation_copy(
                frozen,
                root / "evaluation",
                project_name="pair-a",
                question_id_file=ids,
                expected_questions=2,
                expected_question_ids_sha256=ids_hash,
            )

            self.assertEqual(manifest["prediction_file_count"], 2)
            self.assertFalse(manifest["checks"]["prediction_payloads_parsed"])
            copied = root / "evaluation" / "predicted_pair-a"
            self.assertEqual((copied / "q1.json").read_bytes(), (frozen / "q1.json").read_bytes())
            verified = verify_frozen_copy_manifest(
                root / "evaluation" / "frozen-copy-manifest.json"
            )
            self.assertTrue(verified["ok"])

    def test_refuses_nonfresh_output_and_scope_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ids = root / "ids.json"
            ids.write_text('["q1"]\n', encoding="utf-8")
            frozen = root / "predicted_pair-a"
            frozen.mkdir()
            (frozen / "q2.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "filename scope mismatch"):
                prepare_evaluation_copy(
                    frozen,
                    root / "evaluation",
                    project_name="pair-a",
                    question_id_file=ids,
                    expected_questions=1,
                )

            (frozen / "q2.json").unlink()
            (frozen / "q1.json").write_text("{}", encoding="utf-8")
            (root / "evaluation").mkdir()
            with self.assertRaisesRegex(FileExistsError, "must be absent"):
                prepare_evaluation_copy(
                    frozen,
                    root / "evaluation",
                    project_name="pair-a",
                    question_id_file=ids,
                    expected_questions=1,
                )

    def test_detects_post_copy_frozen_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ids = root / "ids.json"
            ids.write_text('["q1"]\n', encoding="utf-8")
            frozen = root / "predicted_pair-a"
            frozen.mkdir()
            prediction = frozen / "q1.json"
            prediction.write_text("{}", encoding="utf-8")
            prepare_evaluation_copy(
                frozen,
                root / "evaluation",
                project_name="pair-a",
                question_id_file=ids,
                expected_questions=1,
            )

            prediction.write_text('{"changed":true}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frozen prediction tree changed"):
                verify_frozen_copy_manifest(
                    root / "evaluation" / "frozen-copy-manifest.json"
                )


if __name__ == "__main__":
    unittest.main()
