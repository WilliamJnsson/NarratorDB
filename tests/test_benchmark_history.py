from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from narratordb.benchmarks.history import (
    archive_run,
    scan_for_secrets,
    verify_index,
    verify_manifest,
)


class BenchmarkHistoryTests(unittest.TestCase):
    def test_archive_is_append_only_and_manifest_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "reports" / "run-1"
            run_dir.mkdir(parents=True)
            (run_dir / "results.json").write_text('{"score": 1}\n', encoding="utf-8")
            record = root / "benchmark_records" / "run-1.json"
            record.parent.mkdir(parents=True)
            record.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "recorded_at": "2026-07-14",
                        "status": "development",
                        "benchmark": {"name": "synthetic"},
                    }
                ),
                encoding="utf-8",
            )
            index = record.parent / "index.json"

            entry = archive_run(run_dir, record, index)
            self.assertEqual(entry["id"], "run-1")
            self.assertTrue(verify_manifest(run_dir)["ok"])
            self.assertTrue(verify_index(index)["ok"])
            with self.assertRaisesRegex(ValueError, "already contains"):
                archive_run(run_dir, record, index)

            (run_dir / "results.json").write_text('{"score": 0}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                verify_manifest(run_dir)

    def test_index_detects_a_mutated_tracked_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "reports" / "run-1"
            run_dir.mkdir(parents=True)
            (run_dir / "result.json").write_text("{}\n", encoding="utf-8")
            record = root / "benchmark_records" / "run-1.json"
            record.parent.mkdir(parents=True)
            record.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "recorded_at": "2026-07-14",
                        "status": "development",
                        "benchmark": {"name": "synthetic"},
                    }
                ),
                encoding="utf-8",
            )
            index = record.parent / "index.json"
            archive_run(run_dir, record, index)

            record.write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "record checksum mismatch"):
                verify_index(index)

    def test_records_only_verification_does_not_require_local_raw_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "reports" / "run-1"
            run_dir.mkdir(parents=True)
            (run_dir / "result.json").write_text("{}\n", encoding="utf-8")
            record = root / "benchmark_records" / "run-1.json"
            record.parent.mkdir(parents=True)
            record.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "recorded_at": "2026-07-17",
                        "status": "development",
                        "benchmark": {"name": "synthetic"},
                    }
                ),
                encoding="utf-8",
            )
            index = record.parent / "index.json"
            archive_run(run_dir, record, index)

            for path in run_dir.iterdir():
                path.unlink()
            run_dir.rmdir()

            result = verify_index(index, records_only=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["verification_scope"], "records-only")
            with self.assertRaises(FileNotFoundError):
                verify_index(index)

    def test_legacy_calibration_status_is_normalized_without_rewriting_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "reports" / "legacy"
            run_dir.mkdir(parents=True)
            (run_dir / "result.json").write_text("{}\n", encoding="utf-8")
            record = root / "benchmark_records" / "legacy.json"
            record.parent.mkdir(parents=True)
            record.write_text(
                json.dumps(
                    {
                        "run_id": "legacy",
                        "recorded_at": "2026-07-14",
                        "status": "development_calibration_not_competitor_headline",
                        "benchmark": {"name": "LongMemEval_S"},
                    }
                ),
                encoding="utf-8",
            )

            entry = archive_run(run_dir, record, record.parent / "index.json")

            self.assertEqual(entry["status"], "development")
            self.assertEqual(
                entry["declared_status"],
                "development_calibration_not_competitor_headline",
            )

    def test_secret_scan_rejects_key_shaped_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = b"sk-" + b"or-v1-" + (b"x" * 32)
            (root / "unsafe.log").write_bytes(b"token=" + secret)
            with self.assertRaisesRegex(ValueError, "OpenRouter API key"):
                scan_for_secrets(root)

    def test_archive_rejects_a_secret_in_the_tracked_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "reports" / "unsafe"
            run_dir.mkdir(parents=True)
            (run_dir / "result.json").write_text("{}\n", encoding="utf-8")
            record = root / "benchmark_records" / "unsafe.json"
            record.parent.mkdir(parents=True)
            secret = "sk-" + "or-v1-" + ("x" * 32)
            record.write_text(
                json.dumps(
                    {
                        "run_id": "unsafe",
                        "status": "development",
                        "accidental_key": secret,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "OpenRouter API key"):
                archive_run(run_dir, record, record.parent / "index.json")


if __name__ == "__main__":
    unittest.main()
