#!/usr/bin/env python3
"""Offline tests for the unsealed R5 finalization recovery candidate."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORKER = HERE / "verify_finalization_recovery.py"
PROTOCOL_PATH = HERE / "recovery-protocol-r1.json"
spec = importlib.util.spec_from_file_location("r5_recovery_under_test", WORKER)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot import recovery worker")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
PROTOCOL = json.loads(PROTOCOL_PATH.read_bytes())


class RecoveryTests(unittest.TestCase):
    maxDiff = None

    def test_protocol_is_canonical_and_unsealed_inventory_is_exact(self) -> None:
        document, payload = recovery._load_json(PROTOCOL_PATH)
        self.assertEqual(payload, recovery._canonical_json(document))
        inventory = json.loads((HERE / "closed-world-inventory.json").read_bytes())
        self.assertEqual(len(inventory["allowed_bundle_files_before_seal"]), 10)
        self.assertEqual(PROTOCOL["recovery_precommit"]["preseal_file_count"], 10)
        self.assertEqual(PROTOCOL["recovery_precommit"]["sealed_physical_file_count"], 11)
        self.assertEqual(
            {path.name for path in HERE.iterdir()},
            set(inventory["allowed_bundle_files_before_seal"]),
        )
        self.assertFalse((HERE / "SHA256SUMS").exists())

    def test_direct_and_nested_bound_manifests_match(self) -> None:
        direct = recovery._parse_manifest(
            (HERE / "BOUND_INPUTS_SHA256SUMS").read_bytes(), basename_only=False
        )
        self.assertGreaterEqual(len(direct), 40)
        for relative, expected in direct.items():
            self.assertEqual(
                recovery._sha256(recovery._stable_bytes(ROOT / relative)), expected
            )
        r5 = ROOT / "benchmark_records/reproduction-v13-paid-paired-scoring-r5-20260716"
        sealed = recovery._parse_manifest(
            (r5 / "SHA256SUMS").read_bytes(), basename_only=True
        )
        self.assertEqual(len(sealed), 22)
        for name, expected in sealed.items():
            self.assertEqual(recovery._sha256(recovery._stable_bytes(r5 / name)), expected)
        nested = recovery._parse_manifest(
            (r5 / "BOUND_INPUTS_SHA256SUMS").read_bytes(), basename_only=False
        )
        self.assertEqual(len(nested), 66)
        for relative, expected in nested.items():
            self.assertEqual(
                recovery._sha256(
                    recovery._r5_nested_input_payload(ROOT, PROTOCOL, relative)
                ),
                expected,
            )

    def test_only_exact_sealed_python_symlink_is_allowed(self) -> None:
        real_python = Path(PROTOCOL["execution_environment"]["python_real_path"])
        relative = PROTOCOL["execution_environment"]["python_entrypoint_path"]
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            candidate = root / relative
            candidate.parent.mkdir(parents=True)
            candidate.symlink_to(
                PROTOCOL["execution_environment"]["python_entrypoint_symlink_target"]
            )
            payload = recovery._r5_nested_input_payload(root, PROTOCOL, relative)
            self.assertEqual(recovery._sha256(payload), recovery._sha256(real_python.read_bytes()))
            candidate.unlink()
            candidate.symlink_to("/usr/bin/false")
            with self.assertRaises(recovery.RecoveryError):
                recovery._r5_nested_input_payload(root, PROTOCOL, relative)
            other = root / "other-link"
            other.symlink_to(real_python)
            with self.assertRaises(recovery.RecoveryError):
                recovery._r5_nested_input_payload(root, PROTOCOL, "other-link")

    def test_original_attempt_fingerprint_matches_exact_stat_stream(self) -> None:
        observed = recovery._attempt_inventory(ROOT, PROTOCOL)
        self.assertEqual(
            observed["tree_fingerprint_sha256"],
            "db0f54b46b3c24c6fac212e59a472365451dd88638f2b8274bf94538b804f804",
        )
        self.assertEqual(
            observed,
            {
                "directories": 404,
                "files": 5185,
                "hardlinked_regular_files": 0,
                "symlinks": 0,
                "tree_fingerprint_sha256": "db0f54b46b3c24c6fac212e59a472365451dd88638f2b8274bf94538b804f804",
            },
        )

    def test_terminal_record_and_empty_failure_are_immutable(self) -> None:
        terminal = PROTOCOL["terminal_failure_record"]
        record = ROOT / terminal["record_path"]
        checksum = ROOT / terminal["checksum_manifest_path"]
        self.assertEqual(recovery._sha256(recovery._require_immutable(record)), terminal["record_sha256"])
        self.assertEqual(
            recovery._sha256(recovery._require_immutable(checksum)),
            terminal["checksum_manifest_sha256"],
        )
        failed = ROOT / (
            "reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-"
            "20260716/attempt1/postrun/score-release-verification.json"
        )
        self.assertEqual(recovery._require_immutable(failed), b"")
        self.assertEqual(stat.S_IMODE(failed.stat().st_mode), 0o444)

    def test_umask_regression_and_fixed_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            old = os.umask(0o222)
            try:
                broken = Path(tempfile.mkdtemp(dir=parent))
            finally:
                os.umask(old)
            self.assertEqual(stat.S_IMODE(broken.stat().st_mode), 0o500)
            with self.assertRaises(PermissionError):
                (broken / "child").mkdir()
            broken.rmdir()
            old = os.umask(0o077)
            try:
                fixed = Path(tempfile.mkdtemp(dir=parent))
            finally:
                os.umask(old)
            self.assertEqual(stat.S_IMODE(fixed.stat().st_mode), 0o700)
            (fixed / "child").mkdir()

    def test_o_excl_publish_is_immutable_and_refuses_collision(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            output = Path(name)
            os.chmod(output, 0o700)
            target = output / "artifact.json"
            recovery._write_new_bytes(target, b"{}\n", output_root=output)
            self.assertEqual(target.read_bytes(), b"{}\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o444)
            with self.assertRaises(recovery.RecoveryError):
                recovery._write_new_bytes(target, b"changed\n", output_root=output)

    def test_exact_0444_rejects_other_nonwritable_modes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / "artifact.json"
            target.write_bytes(b"{}\n")
            target.chmod(0o444)
            self.assertEqual(
                recovery._require_immutable(target, exact_mode=0o444), b"{}\n"
            )
            for wrong_mode in (0o400, 0o440, 0o555):
                target.chmod(wrong_mode)
                with self.subTest(mode=oct(wrong_mode)):
                    with self.assertRaises(recovery.RecoveryError):
                        recovery._require_immutable(target, exact_mode=0o444)

    def test_private_score_file_is_0600(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / "private.json"
            recovery._write_private_file(target, b"private\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def _release_payloads(self) -> dict[str, bytes]:
        return {
            "RECOVERED_PAIRED_RESULT_SHA256SUMS": b"0" * 64
            + b"  recovered-paired-result.json\n",
            "recovered-paired-result.json": b'{"private":true}\n',
            "v7-evaluation-audit.json": b'{"private":true}\n',
            "v13-evaluation-audit.json": b'{"private":true}\n',
            "recovery-review-1.json": b"{}\n",
            "recovery-review-2.json": b"{}\n",
            "recovery-go.json": b"{}\n",
            "release-complete.json": b"{}\n",
        }

    def test_atomic_release_namespace_appears_complete(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / "out"
            output.mkdir(mode=0o700)
            release = output / "release"
            recovery._RELEASE_COMMITTED = False
            recovery._publish_release(output, release=release, payloads=self._release_payloads())
            self.assertTrue(recovery._RELEASE_COMMITTED)
            self.assertEqual({item.name for item in release.iterdir()}, set(self._release_payloads()))
            self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o555)
            self.assertTrue(all(stat.S_IMODE(item.stat().st_mode) == 0o444 for item in release.iterdir()))
            os.chmod(output, 0o700)

    def test_interruption_before_atomic_commit_exposes_no_release_and_terminalizes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            output = root / "out"
            output.mkdir(mode=0o700)
            release = output / "release"
            original = recovery._write_new_bytes
            calls = 0

            def injected(path: Path, payload: bytes, *, output_root: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise recovery.RecoveryError("injected interruption")
                original(path, payload, output_root=output_root)

            recovery._RELEASE_COMMITTED = False
            recovery._write_new_bytes = injected
            try:
                with self.assertRaises(recovery.RecoveryError):
                    recovery._publish_release(
                        output, release=release, payloads=self._release_payloads()
                    )
            finally:
                recovery._write_new_bytes = original
            self.assertFalse(release.exists())
            self.assertEqual(list(output.iterdir()), [])
            protocol = {
                "attempt_preservation": {
                    "tree_fingerprint_sha256": "d" * 64,
                },
                "output": {
                    "failed_status_path": "out/recovery-terminal-status.json",
                    "output_root": "out",
                },
                "terminal_failure_record": {"record_sha256": "e" * 64},
                "zero_new_activity": PROTOCOL["zero_new_activity"],
            }
            recovery._terminalize(root, protocol, stage="stage-b", published_seal="f" * 64)
            status = output / "recovery-terminal-status.json"
            self.assertTrue(status.exists())
            self.assertEqual(stat.S_IMODE(status.stat().st_mode), 0o444)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o555)
            os.chmod(output, 0o700)

    def test_postcommit_rerun_accepts_valid_release_and_never_terminalizes_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            output = root / "out"
            release = output / "release"
            release.mkdir(parents=True, mode=0o555)
            os.chmod(output, 0o555)
            protocol = {
                "output": {
                    "failed_status_path": "out/recovery-terminal-status.json",
                    "output_root": "out",
                    "stage_a": {"envelope_path": "out/stage-a.json"},
                    "stage_b": {
                        "completion_path": "out/release/release-complete.json",
                        "go_copy_path": "out/release/recovery-go.json",
                        "release_directory_path": "out/release",
                        "result_checksum_path": "out/release/RECOVERED_PAIRED_RESULT_SHA256SUMS",
                        "result_path": "out/release/recovered-paired-result.json",
                        "review_1_copy_path": "out/release/recovery-review-1.json",
                        "review_2_copy_path": "out/release/recovery-review-2.json",
                        "v13_evaluation_audit_path": "out/release/v13-evaluation-audit.json",
                        "v7_evaluation_audit_path": "out/release/v7-evaluation-audit.json",
                    },
                }
            }
            original = recovery._validate_committed_release
            calls = 0

            def valid(*_args: object, **_kwargs: object) -> None:
                nonlocal calls
                calls += 1

            recovery._validate_committed_release = valid
            recovery._RELEASE_COMMITTED = False
            try:
                recovery._run_stage_b(
                    root,
                    protocol,
                    object(),
                    root / "requirements.json",
                    recovery_seal="a" * 64,
                    now=datetime.now(timezone.utc),
                )
            finally:
                recovery._validate_committed_release = original
            self.assertEqual(calls, 1)
            self.assertTrue(recovery._RELEASE_COMMITTED)
            self.assertFalse((output / "recovery-terminal-status.json").exists())

            def malformed(*_args: object, **_kwargs: object) -> None:
                raise recovery.RecoveryError("malformed committed release")

            recovery._validate_committed_release = malformed
            recovery._RELEASE_COMMITTED = False
            try:
                with self.assertRaises(recovery.RecoveryError):
                    recovery._run_stage_b(
                        root,
                        protocol,
                        object(),
                        root / "requirements.json",
                        recovery_seal="a" * 64,
                        now=datetime.now(timezone.utc),
                    )
            finally:
                recovery._validate_committed_release = original
            self.assertFalse((output / "recovery-terminal-status.json").exists())
            os.chmod(output, 0o700)

    def test_exact_predeclared_two_review_go(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=30)
            stage_path = root / "stage-a.json"
            stage_document = {"executed_at_utc": recovery._timestamp_text(base)}
            stage_payload = recovery._canonical_json(stage_document)
            stage_path.write_bytes(stage_payload)
            os.chmod(stage_path, 0o444)
            stage_ns = int(base.timestamp() * 1_000_000_000)
            os.utime(stage_path, ns=(stage_ns, stage_ns))
            reviewers = PROTOCOL["go_policy"]["reviewers"]
            policy = {
                **PROTOCOL["go_policy"],
                "aggregate_path": "go.json",
                "review_paths": ["review-1.json", "review-2.json"],
                "reviewers": [
                    {**reviewers[0], "path": "review-1.json"},
                    {**reviewers[1], "path": "review-2.json"},
                ],
            }
            protocol = {
                "attempt_preservation": {
                    "tree_fingerprint_sha256": "a" * 64,
                },
                "go_policy": policy,
                "terminal_failure_record": {"record_sha256": "b" * 64},
            }
            seal = "c" * 64
            reviews = []
            review_records = []
            for index, reviewer in enumerate(policy["reviewers"], start=1):
                created = base + timedelta(seconds=5 * index)
                document = {
                    "created_at_utc": recovery._timestamp_text(created),
                    "credential_recorded": False,
                    "decision": "GO",
                    "model_content_recorded": False,
                    "no_score_read": True,
                    "recovery_precommit_sha256": seal,
                    "review_authority": reviewer["authority"],
                    "reviewer_codename": reviewer["codename"],
                    "reviewer_id": reviewer["reviewer_id"],
                    "schema_version": recovery.REVIEW_SCHEMA,
                    "score_blind": True,
                    "source_attempt_tree_fingerprint_sha256": "a" * 64,
                    "stage_a_envelope_sha256": recovery._sha256(stage_payload),
                    "terminal_failure_record_sha256": "b" * 64,
                }
                path = root / f"review-{index}.json"
                payload = recovery._canonical_json(document)
                path.write_bytes(payload)
                os.chmod(path, 0o444)
                timestamp_ns = int(created.timestamp() * 1_000_000_000)
                os.utime(path, ns=(timestamp_ns, timestamp_ns))
                reviews.append(payload)
                review_records.append(
                    {
                        "path": f"review-{index}.json",
                        "reviewer_id": reviewer["reviewer_id"],
                        "sha256": recovery._sha256(payload),
                    }
                )
            go_created = base + timedelta(seconds=15)
            go = {
                "created_at_utc": recovery._timestamp_text(go_created),
                "credential_recorded": False,
                "go": True,
                "model_content_recorded": False,
                "no_score_read": True,
                "recovery_precommit_sha256": seal,
                "reviews": review_records,
                "schema_version": recovery.GO_SCHEMA,
                "score_blind": True,
                "source_attempt_tree_fingerprint_sha256": "a" * 64,
                "stage_a_envelope_sha256": recovery._sha256(stage_payload),
                "terminal_failure_record_sha256": "b" * 64,
            }
            go_path = root / "go.json"
            go_payload = recovery._canonical_json(go)
            go_path.write_bytes(go_payload)
            os.chmod(go_path, 0o444)
            go_ns = int(go_created.timestamp() * 1_000_000_000)
            os.utime(go_path, ns=(go_ns, go_ns))
            result = recovery._validate_go(
                root,
                protocol,
                recovery_seal=seal,
                stage_a_payload=stage_payload,
                stage_a_document=stage_document,
                stage_a_path=stage_path,
                now=base + timedelta(seconds=20),
            )
            self.assertEqual(result[0], recovery._sha256(go_payload))
            self.assertEqual(result[2], reviews)
            self.assertEqual(result[3], go_payload)
            with self.assertRaises(recovery.RecoveryError):
                recovery._validate_go(
                    root,
                    protocol,
                    recovery_seal="0" * 64,
                    stage_a_payload=stage_payload,
                    stage_a_document=stage_document,
                    stage_a_path=stage_path,
                    now=base + timedelta(seconds=20),
                )
            review_path = root / "review-1.json"
            os.chmod(review_path, 0o600)
            review_path.write_text(json.dumps(json.loads(reviews[0])) + "\n")
            os.chmod(review_path, 0o444)
            os.utime(review_path, ns=(int((base + timedelta(seconds=5)).timestamp() * 1_000_000_000),) * 2)
            with self.assertRaises(recovery.RecoveryError):
                recovery._validate_go(
                    root,
                    protocol,
                    recovery_seal=seal,
                    stage_a_payload=stage_payload,
                    stage_a_document=stage_document,
                    stage_a_path=stage_path,
                    now=base + timedelta(seconds=20),
                )
            review_path.chmod(0o600)
            review_path.write_bytes(reviews[0])
            review_path.chmod(0o444)
            os.utime(review_path, ns=(int((base + timedelta(seconds=5)).timestamp() * 1_000_000_000),) * 2)
            go_path.chmod(0o600)
            go_path.write_text(json.dumps(go) + "\n")
            go_path.chmod(0o444)
            os.utime(go_path, ns=(go_ns, go_ns))
            with self.assertRaises(recovery.RecoveryError):
                recovery._validate_go(
                    root,
                    protocol,
                    recovery_seal=seal,
                    stage_a_payload=stage_payload,
                    stage_a_document=stage_document,
                    stage_a_path=stage_path,
                    now=base + timedelta(seconds=20),
                )

    def test_recursive_score_field_scan(self) -> None:
        recovery._reject_score_fields({"score_blind": True, "nested": [{"safe": 1}]})
        with self.assertRaises(recovery.RecoveryError):
            recovery._reject_score_fields({"nested": [{"metrics": {}}]})
        with self.assertRaises(recovery.RecoveryError):
            recovery._reject_score_fields({"answer": "hidden"})

    def test_stage_a_envelope_is_hash_only_and_historical(self) -> None:
        document = recovery._stage_a_envelope(
            PROTOCOL,
            executed_at=datetime.now(timezone.utc).replace(microsecond=0),
            authorization_payload=b"authorization",
            audit_payload=b"audit",
            verification_payload=b"verification",
        )
        recovery._reject_score_fields(document)
        self.assertEqual(document["benchmark_scope"], "consumed-development")
        self.assertFalse(document["present_time_freshness_claimed"])
        self.assertFalse(document["cross_attempt_combination"])
        self.assertEqual(document["zero_new_activity"]["additional_spend_usd"], "0")

    def test_result_and_completion_embed_historical_not_current_semantics(self) -> None:
        metrics = {
            "top_20": {"accuracy": "0", "correct": 0, "total": 42},
            "top_50": {"accuracy": "0", "correct": 0, "total": 42},
        }
        evidence = {
            "attempt_status_sha256": "a" * 64,
            "evaluation_auditor_sha256": "b" * 64,
            "evaluator_log_sha256": "c" * 64,
            "scored_tree_sha256": "d" * 64,
            "usage_ledger_sha256": "e" * 64,
        }
        result = recovery._result_document(
            PROTOCOL,
            b"verification",
            {
                "authorization_sha256": "f" * 64,
                "revision_precommit_sha256": "1" * 64,
            },
            recovery_seal="2" * 64,
            go_sha="3" * 64,
            review_shas=["4" * 64, "5" * 64],
            v7_payload=b"v7",
            v13_payload=b"v13",
            v7_metrics=metrics,
            v13_metrics=metrics,
            v7_evidence=evidence,
            v13_evidence=evidence,
        )
        completion = recovery._completion_document(
            PROTOCOL,
            result_sha=recovery._sha256(recovery._canonical_json(result)),
            go_sha="3" * 64,
            review_shas=["4" * 64, "5" * 64],
            recovery_seal="2" * 64,
            v7_payload=b"v7",
            v13_payload=b"v13",
        )
        for document in (result, completion):
            self.assertEqual(
                document["historical_replay_at_utc"],
                PROTOCOL["historical_finalization"]["verification_now_utc"],
            )
            self.assertFalse(document["present_time_freshness_claimed"])

    def test_exact_historical_recomputation_and_present_expiry(self) -> None:
        module, _, requirements = recovery._load_sealed_verifier(ROOT, PROTOCOL)
        old = os.umask(0o077)
        try:
            authorization, audit, verification, result = recovery._historical_recomputation(
                ROOT, PROTOCOL, module, requirements
            )
        finally:
            os.umask(old)
        self.assertEqual(recovery._sha256(authorization), PROTOCOL["historical_finalization"]["authorization_sha256"])
        self.assertEqual(recovery._sha256(audit), PROTOCOL["historical_finalization"]["independent_audit_sha256"])
        self.assertEqual(recovery._sha256(verification), recovery._sha256(recovery._canonical_json(result)))
        original = json.loads(authorization)
        with self.assertRaises(module.AdmissionError):
            module._validate_authorization_freshness(
                original,
                now=datetime(2026, 7, 16, 14, 48, 39, tzinfo=timezone.utc),
                maximum_age_seconds=900,
            )

    def test_launcher_and_profile_are_closed_world(self) -> None:
        launcher = (HERE / "run_offline_recovery.sh").read_text()
        profile = (HERE / "offline-recovery.sb").read_text()
        self.assertTrue(launcher.startswith("#!/bin/bash -p\n"))
        self.assertIn("/usr/bin/env -i", launcher)
        self.assertIn("--noprofile --norc -p", launcher)
        self.assertIn("unset BASH_ENV ENV", launcher)
        self.assertIn("$python_real\" -I -S -B", launcher)
        self.assertIn(">/dev/null 2>/dev/null", launcher)
        self.assertIn("(deny default)", profile)
        self.assertIn("(deny network*)", profile)
        self.assertIn("(allow sysctl-read)", profile)
        self.assertNotIn("(allow network", profile)
        source = WORKER.read_text()
        for forbidden in ("https://", "http://", "OPENROUTER_API_KEY", "capture_ecb_fx", "capture_provider_telemetry"):
            self.assertNotIn(forbidden, source)

    def test_exact_clean_sandbox_invalid_worker_smoke(self) -> None:
        python_real = PROTOCOL["execution_environment"]["python_real_path"]
        python_root = str(Path(python_real).parents[1])
        with tempfile.TemporaryDirectory(
            prefix="narratordb-r5-sbpl-smoke.", dir="/private/tmp"
        ) as name:
            scratch = Path(name).resolve()
            home = scratch / "home"
            temporary = scratch / "tmp"
            output = scratch / "out"
            for path in (home, temporary, output):
                path.mkdir(mode=0o700)
            command = [
                "/usr/bin/sandbox-exec",
                "-D",
                f"ROOT={ROOT}",
                "-D",
                f"PYROOT={python_root}",
                "-D",
                f"PYTHON_REAL={python_real}",
                "-D",
                f"SCRATCH={scratch}",
                "-D",
                f"OUT={output}",
                "-f",
                str(HERE / "offline-recovery.sb"),
                python_real,
                "-I",
                "-S",
                "-B",
                str(WORKER),
            ]
            result = subprocess.run(
                command,
                env={
                    "HOME": str(home),
                    "TMPDIR": str(temporary),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=20,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b"")
            self.assertEqual(result.stderr, b"")
            self.assertEqual(list(output.iterdir()), [])

    def test_privileged_launcher_ignores_bash_env_hook(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            marker = directory / "marker"
            hook = directory / "hook.sh"
            hook.write_text(f"touch {marker}\n")
            environment = {
                "BASH_ENV": str(hook),
                "ENV": str(hook),
                "SHELLOPTS": "braceexpand:hashall:interactive-comments:xtrace",
            }
            result = subprocess.run(
                ["/bin/bash", "-p", str(HERE / "run_offline_recovery.sh"), "invalid"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 64)
            self.assertEqual(result.stdout, b"")
            self.assertEqual(result.stderr, b"")
            self.assertFalse(marker.exists())

    def test_main_invalid_invocation_emits_zero_bytes(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = recovery.main([])
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_candidate_has_no_output_go_seal_or_bytecode(self) -> None:
        output = ROOT / PROTOCOL["output"]["output_root"]
        self.assertFalse(output.exists())
        self.assertFalse((ROOT / PROTOCOL["go_policy"]["aggregate_path"]).exists())
        for relative in PROTOCOL["go_policy"]["review_paths"]:
            self.assertFalse((ROOT / relative).exists())
        self.assertFalse((HERE / "SHA256SUMS").exists())
        self.assertEqual(list(HERE.rglob("*.pyc")), [])
        self.assertEqual(list(HERE.rglob("__pycache__")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
