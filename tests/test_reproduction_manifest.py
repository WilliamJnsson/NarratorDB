from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from narratordb.benchmarks.reproduction_manifest import (
    PREFLIGHT_SCHEMA,
    SEALED_SCHEMA,
    create_preflight,
    seal_run,
    verify_preflight,
    verify_seal,
)


class ReproductionManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "inputs" / "narratordb-source.tar.gz"
        self.source.parent.mkdir(parents=True)
        self._write_source_archive(self.source, b"print('frozen source')\n")

        self.harness = self.root / "vendor" / "harness"
        self.harness.mkdir(parents=True)
        (self.harness / "runner.py").write_text("print('harness')\n", encoding="utf-8")
        self._git("init", "-q")
        self._git("add", "runner.py")
        self._git(
            "-c",
            "user.name=NarratorDB Test",
            "-c",
            "user.email=test@narratordb.example",
            "commit",
            "--no-gpg-sign",
            "-q",
            "-m",
            "fixture",
        )
        self.harness_commit = self._git("rev-parse", "HEAD").stdout.strip()

        self.dataset = self.root / "inputs" / "dataset.json"
        self.dataset.write_text(
            json.dumps(
                [
                    {"question_id": "q-1", "question": "First?"},
                    {"question_id": "q-2", "question": "Second?"},
                ],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.question_ids = self.root / "inputs" / "question_ids.json"
        self.question_ids.write_text('[\n  "q-1",\n  "q-2"\n]\n', encoding="utf-8")
        self.config = self.root / "inputs" / "run-config.json"
        self.config.write_text(
            json.dumps(
                {
                    "benchmark": "LongMemEval_S",
                    "run_id": "official-fixture-v1",
                    "project_name": "narratordb-official-fixture-v1",
                    "mode": "intelligence",
                    "compiler": {
                        "model": "openai/gpt-5.4-mini",
                        "provider": "OpenAI",
                        "reasoning": "medium",
                        "max_output_tokens": 8192,
                        "max_cost_usd": 180,
                    },
                    "retrieval": {"top_k": 200, "cutoffs": [20, 50, 200]},
                    "answerer": {
                        "model": "z-ai/glm-5.2",
                        "provider": "OpenRouter",
                        "reasoning": "none",
                    },
                    "judge": {
                        "model": "deepseek/deepseek-v3.2",
                        "provider": "OpenRouter",
                        "reasoning": "none",
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.commands = self.root / "inputs" / "commands.json"
        self.commands.write_text(
            json.dumps(
                [
                    {
                        "step": "backend",
                        "argv": [
                            "python3",
                            "-m",
                            "narratordb.benchmark_server",
                            "--mode",
                            "intelligence",
                        ],
                    },
                    {
                        "step": "predict",
                        "argv": [
                            "python3",
                            "run_experiments.py",
                            "--method",
                            "narratordb",
                        ],
                    },
                    {
                        "step": "evaluate",
                        "argv": [
                            "python3",
                            "evaluate_qa.py",
                            "--top-k",
                            "200",
                        ],
                    },
                ],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.database = self.root / "run" / "intelligence.db"
        self.cache = self.root / "run" / "compiler-cache.sqlite3"
        self.preflight = self.root / "records" / "preflight.json"
        self.sealed = self.root / "records" / "sealed.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_source_archive(path: Path, content: bytes) -> None:
        info = tarfile.TarInfo("narratordb/source.py")
        info.size = len(content)
        info.mode = 0o644
        with tarfile.open(path, "w:gz") as archive:
            archive.addfile(info, io.BytesIO(content))

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.harness), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _create(self, output: Path | None = None) -> dict:
        return create_preflight(
            repository_root=self.root,
            output=output or self.preflight,
            source_archive=self.source,
            harness_root=self.harness,
            dataset=self.dataset,
            question_ids=self.question_ids,
            config_file=self.config,
            commands_file=self.commands,
            database=self.database,
            compiler_cache=self.cache,
            additional_fresh_paths={
                "prediction-directory": self.root / "run" / "predictions",
                "usage-ledger": self.root / "run" / "usage.jsonl",
            },
            expected_source_sha256=self._sha256(self.source),
            expected_harness_commit=self.harness_commit,
            expected_dataset_sha256=self._sha256(self.dataset),
            expected_question_ids_sha256=self._sha256(self.question_ids),
        )

    def test_preflight_records_and_recomputes_all_reproduction_inputs(self) -> None:
        manifest = self._create()

        self.assertEqual(manifest["schema_version"], PREFLIGHT_SCHEMA)
        self.assertEqual(
            manifest["source_archive"]["sha256"], self._sha256(self.source)
        )
        self.assertTrue(manifest["source_archive"]["archive_members_secret_scanned"])
        self.assertEqual(manifest["harness"]["commit"], self.harness_commit)
        self.assertTrue(manifest["harness"]["clean"])
        self.assertEqual(manifest["dataset"]["questions"], 2)
        self.assertEqual(manifest["question_scope"]["questions"], 2)
        self.assertEqual(manifest["execution"]["config"]["retrieval"]["top_k"], 200)
        self.assertEqual(
            manifest["execution"]["commands"][0]["argv"][2],
            "narratordb.benchmark_server",
        )
        self.assertFalse(
            manifest["execution"]["credential_policy"]["credentials_recorded"]
        )
        report = verify_preflight(self.preflight, repository_root=self.root)
        self.assertTrue(report["ok"])
        self.assertTrue(report["fresh_state_verified"])
        with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
            self._create()

    def test_dirty_harness_and_existing_state_are_rejected(self) -> None:
        (self.harness / "runner.py").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not clean"):
            self._create()

        self._git("checkout", "--", "runner.py")
        self.database.parent.mkdir(parents=True)
        self.database.write_bytes(b"")
        with self.assertRaisesRegex(FileExistsError, "must be fresh"):
            self._create()

    def test_preflight_verification_detects_execution_config_mutation(self) -> None:
        self._create()
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["retrieval"]["top_k"] = 300
        self.config.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "execution config or commands"):
            verify_preflight(self.preflight, repository_root=self.root)

    def test_scope_and_credentials_are_rejected_before_manifest_write(self) -> None:
        self.question_ids.write_text('["not-in-dataset"]\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "absent from dataset"):
            self._create()
        self.assertFalse(self.preflight.exists())

        self.question_ids.write_text('["q-1"]\n', encoding="utf-8")
        secret = "sk-" + "or-v1-" + ("x" * 32)
        self.commands.write_text(
            json.dumps(
                [
                    {
                        "step": "backend",
                        "argv": ["python3", "server.py", secret],
                    }
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "OpenRouter API key"):
            self._create()
        self.assertFalse(self.preflight.exists())

    def test_source_archive_members_are_secret_scanned(self) -> None:
        secret = b"sk-" + b"or-v1-" + (b"y" * 32)
        self._write_source_archive(self.source, b"token = " + secret)

        with self.assertRaisesRegex(ValueError, "OpenRouter API key"):
            self._create()
        self.assertFalse(self.preflight.exists())

    def test_seal_hashes_artifacts_and_detects_later_mutation(self) -> None:
        self._create()
        self.database.parent.mkdir(parents=True)
        self.database.write_bytes(b"sqlite fixture")
        self.cache.write_bytes(b"cache fixture")
        predictions = self.root / "run" / "predictions"
        predictions.mkdir()
        result = predictions / "q-1.json"
        result.write_text('{"score": 1}\n', encoding="utf-8")

        with self.assertRaises(FileExistsError):
            verify_preflight(self.preflight, repository_root=self.root)

        manifest = seal_run(
            repository_root=self.root,
            preflight_manifest=self.preflight,
            artifact_root=self.root / "run",
            output=self.sealed,
        )
        self.assertEqual(manifest["schema_version"], SEALED_SCHEMA)
        self.assertEqual(manifest["artifacts"]["file_count"], 3)
        self.assertEqual(
            manifest["preflight"]["harness"]["commit"], self.harness_commit
        )
        self.assertTrue(verify_seal(self.sealed, repository_root=self.root)["ok"])

        result.write_text('{"score": 0}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "artifact set"):
            verify_seal(self.sealed, repository_root=self.root)

    def test_seal_rejects_secret_artifacts_and_output_inside_artifacts(self) -> None:
        self._create()
        self.database.parent.mkdir(parents=True)
        secret = "sk-" + ("z" * 32)
        (self.database.parent / "unsafe.log").write_text(secret, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "OpenAI-style API key"):
            seal_run(
                repository_root=self.root,
                preflight_manifest=self.preflight,
                artifact_root=self.root / "run",
                output=self.sealed,
            )
        with self.assertRaisesRegex(ValueError, "outside the artifact root"):
            seal_run(
                repository_root=self.root,
                preflight_manifest=self.preflight,
                artifact_root=self.root / "run",
                output=self.root / "run" / "sealed.json",
            )

    def test_seal_requires_declared_run_state_inside_artifact_root(self) -> None:
        self._create()
        incomplete_artifacts = self.root / "published-artifacts"
        incomplete_artifacts.mkdir()
        (incomplete_artifacts / "result.json").write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "run-state path is outside"):
            seal_run(
                repository_root=self.root,
                preflight_manifest=self.preflight,
                artifact_root=incomplete_artifacts,
                output=self.sealed,
            )


if __name__ == "__main__":
    unittest.main()
