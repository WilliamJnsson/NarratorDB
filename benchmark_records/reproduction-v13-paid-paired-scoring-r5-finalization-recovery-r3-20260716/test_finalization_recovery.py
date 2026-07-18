#!/usr/bin/env python3
"""Offline tests for the unsealed R5 finalization recovery candidate."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORKER = HERE / "verify_finalization_recovery.py"
PROTOCOL_PATH = HERE / "recovery-protocol-r3.json"
spec = importlib.util.spec_from_file_location("r5_recovery_under_test", WORKER)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot import recovery worker")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
PROTOCOL = json.loads(PROTOCOL_PATH.read_bytes())


class RecoveryTests(unittest.TestCase):
    maxDiff = None

    def _candidate_artifact_paths(self) -> list[Path]:
        return [
            ROOT / PROTOCOL["output"]["output_root"],
            ROOT / PROTOCOL["go_policy"]["aggregate_path"],
            *(
                ROOT / relative
                for relative in PROTOCOL["go_policy"]["review_paths"]
            ),
        ]

    def _assert_candidate_artifacts_absent(self) -> None:
        for path in self._candidate_artifact_paths():
            self.assertFalse(path.exists() or path.is_symlink(), str(path))

    @staticmethod
    def _recovery_scratch_names() -> set[str]:
        temporary_root = Path("/private/tmp")
        return {
            item.name
            for item in temporary_root.iterdir()
            if item.name.startswith("narratordb-r5-")
            and "recovery" in item.name
        }

    def _assert_sealed_bundle(self, inventory: dict[str, object]) -> None:
        allowed = set(inventory["allowed_bundle_files_before_seal"])
        seal_path = HERE / str(inventory["allowed_file_created_at_seal"])
        seal_payload = recovery._require_immutable(
            seal_path, maximum=recovery.MAX_JSON_BYTES, exact_mode=0o444
        )
        seal_metadata = seal_path.lstat()
        self.assertTrue(stat.S_ISREG(seal_metadata.st_mode))
        self.assertEqual(seal_metadata.st_nlink, 1)
        self.assertEqual(stat.S_IMODE(seal_metadata.st_mode), 0o444)
        entries = recovery._parse_manifest(seal_payload, basename_only=True)
        self.assertEqual(len(entries), PROTOCOL["recovery_precommit"]["preseal_file_count"])
        self.assertEqual(set(entries), allowed)
        self.assertEqual(
            {path.name for path in HERE.iterdir()},
            allowed | {seal_path.name},
        )
        self.assertEqual(
            len(list(HERE.iterdir())),
            PROTOCOL["recovery_precommit"]["sealed_physical_file_count"],
        )
        for name, expected in entries.items():
            member = HERE / name
            metadata = member.lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode), name)
            self.assertEqual(metadata.st_nlink, 1, name)
            self.assertEqual(
                stat.S_IMODE(metadata.st_mode),
                0o555 if name == "run_offline_recovery.sh" else 0o444,
                name,
            )
            self.assertEqual(recovery._sha256(recovery._stable_bytes(member)), expected)

    def test_protocol_is_canonical_and_preseal_or_postseal_inventory_is_exact(self) -> None:
        document, payload = recovery._load_json(PROTOCOL_PATH)
        self.assertEqual(payload, recovery._canonical_json(document))
        inventory = json.loads((HERE / "closed-world-inventory.json").read_bytes())
        allowed = set(inventory["allowed_bundle_files_before_seal"])
        self.assertEqual(len(allowed), 11)
        self.assertEqual(inventory["allowed_subdirectories"], [])
        self.assertEqual(inventory["allowed_file_created_at_seal"], "SHA256SUMS")
        self.assertEqual(PROTOCOL["recovery_precommit"]["preseal_file_count"], 11)
        self.assertEqual(PROTOCOL["recovery_precommit"]["sealed_physical_file_count"], 12)
        self.assertEqual(
            PROTOCOL["recovery_precommit"]["bundle_root"],
            inventory["bundle_root"],
        )
        self.assertFalse(any(path.is_dir() for path in HERE.iterdir()))
        if (HERE / "SHA256SUMS").exists():
            self._assert_sealed_bundle(inventory)
        else:
            self.assertEqual({path.name for path in HERE.iterdir()}, allowed)
            self.assertEqual(
                len(list(HERE.iterdir())),
                PROTOCOL["recovery_precommit"]["preseal_file_count"],
            )

    def test_stage_b_canary_bundle_snapshot_accepts_preseal_and_exact_seal_only(self) -> None:
        allowed = [
            "BOUND_INPUTS_SHA256SUMS",
            "README.md",
            "closed-world-inventory.json",
            "commands-r3.json",
            "launcher-executables-r3.json",
            "offline-recovery.sb",
            "preflight-r3.json",
            "recovery-protocol-r3.json",
            "run_offline_recovery.sh",
            "test_finalization_recovery.py",
            "verify_finalization_recovery.py",
        ]
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            bundle = root / "bundle"
            bundle.mkdir(mode=0o700)
            inventory = {
                "allowed_bundle_files_before_seal": allowed,
                "allowed_file_created_at_seal": "SHA256SUMS",
                "allowed_subdirectories": [],
                "bundle_root": "bundle",
                "candidate_status": "UNSEALED_CANDIDATE",
                "schema_version": "narratordb.v13-paid-r5-recovery-closed-world-inventory.v1",
            }
            for index, member_name in enumerate(allowed):
                member = bundle / member_name
                payload = (
                    recovery._canonical_json(inventory)
                    if member_name == "closed-world-inventory.json"
                    else f"synthetic member {index}: {member_name}\n".encode("ascii")
                )
                member.write_bytes(payload)
                member.chmod(0o755 if member_name == "run_offline_recovery.sh" else 0o644)
            protocol = {
                "recovery_precommit": {
                    "bundle_inventory_path": "bundle/closed-world-inventory.json",
                    "bundle_root": "bundle",
                    "preseal_file_count": 11,
                    "seal_manifest_path": "bundle/SHA256SUMS",
                    "sealed_physical_file_count": 12,
                }
            }

            preseal = recovery._stage_b_canary_bundle_snapshot(root, protocol)
            self.assertEqual(preseal["@state"], "preseal")
            self.assertEqual(set(preseal), set(allowed) | {"@state"})
            for member_name in allowed:
                member = bundle / member_name
                member.chmod(
                    0o555 if member_name == "run_offline_recovery.sh" else 0o444
                )
            seal = bundle / "SHA256SUMS"
            seal.write_bytes(
                "".join(
                    f"{preseal[member_name]}  {member_name}\n"
                    for member_name in sorted(allowed)
                ).encode("ascii")
            )
            seal.chmod(0o444)
            sealed = recovery._stage_b_canary_bundle_snapshot(root, protocol)
            seal_sha = recovery._sha256(seal.read_bytes())
            self.assertEqual(sealed["@state"], "sealed")
            self.assertEqual(set(sealed), set(allowed) | {"@state", "SHA256SUMS"})
            self.assertEqual(
                {member_name: sealed[member_name] for member_name in allowed},
                {member_name: preseal[member_name] for member_name in allowed},
            )
            self.assertEqual(sealed["SHA256SUMS"], seal_sha)
            recovery._validate_bundle_seal(root, protocol, seal_sha)

            launcher = bundle / "run_offline_recovery.sh"
            launcher.chmod(0o444)
            with self.assertRaises(recovery.RecoveryError):
                recovery._stage_b_canary_bundle_snapshot(root, protocol)
            with self.assertRaises(recovery.RecoveryError):
                recovery._validate_bundle_seal(root, protocol, seal_sha)
            launcher.chmod(0o555)

            ordinary = bundle / "README.md"
            ordinary.chmod(0o555)
            with self.assertRaises(recovery.RecoveryError):
                recovery._stage_b_canary_bundle_snapshot(root, protocol)
            with self.assertRaises(recovery.RecoveryError):
                recovery._validate_bundle_seal(root, protocol, seal_sha)
            ordinary.chmod(0o444)

            seal.chmod(0o400)
            with self.assertRaises(recovery.RecoveryError):
                recovery._stage_b_canary_bundle_snapshot(root, protocol)
            with self.assertRaises(recovery.RecoveryError):
                recovery._validate_bundle_seal(root, protocol, seal_sha)
            seal.chmod(0o444)

            external_link = root / "README-hardlink"
            os.link(ordinary, external_link)
            try:
                with self.assertRaises(recovery.RecoveryError):
                    recovery._stage_b_canary_bundle_snapshot(root, protocol)
                with self.assertRaises(recovery.RecoveryError):
                    recovery._validate_bundle_seal(root, protocol, seal_sha)
            finally:
                external_link.unlink()
            self.assertEqual(
                recovery._stage_b_canary_bundle_snapshot(root, protocol), sealed
            )

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

    def test_prior_r2_terminal_record_and_all_evidence_are_recursively_bound(self) -> None:
        prior = PROTOCOL["prior_recovery_terminal"]
        self.assertEqual(
            set(prior),
            {
                "checksum_manifest_path",
                "checksum_manifest_sha256",
                "r1_terminal_record_sha256",
                "r2_aggregate_go_path",
                "r2_aggregate_go_sha256",
                "r2_bundle_root",
                "r2_output_root",
                "r2_review_paths",
                "r2_review_sha256",
                "r2_seal_manifest_path",
                "r2_seal_manifest_sha256",
                "r2_stage_a_envelope_path",
                "r2_stage_a_envelope_sha256",
                "r2_terminal_status_path",
                "r2_terminal_status_sha256",
                "record_path",
                "record_sha256",
            },
        )
        record_path = ROOT / prior["record_path"]
        checksum_path = ROOT / prior["checksum_manifest_path"]
        seal_path = ROOT / prior["r2_seal_manifest_path"]
        record_payload = recovery._require_immutable(
            record_path, maximum=recovery.MAX_JSON_BYTES, exact_mode=0o444
        )
        checksum_payload = recovery._require_immutable(
            checksum_path, maximum=recovery.MAX_JSON_BYTES, exact_mode=0o444
        )
        seal_payload = recovery._require_immutable(
            seal_path, maximum=recovery.MAX_JSON_BYTES, exact_mode=0o444
        )
        self.assertEqual(recovery._sha256(record_payload), prior["record_sha256"])
        self.assertEqual(
            recovery._sha256(checksum_payload), prior["checksum_manifest_sha256"]
        )
        self.assertEqual(
            recovery._sha256(seal_payload), prior["r2_seal_manifest_sha256"]
        )
        self.assertEqual(
            recovery._parse_manifest(checksum_payload, basename_only=True),
            {record_path.name: prior["record_sha256"]},
        )

        terminal = json.loads(record_payload)
        self.assertEqual(record_payload, recovery._canonical_json(terminal))
        self.assertEqual(
            set(terminal),
            {
                "execution_observation",
                "failure",
                "output_evidence",
                "private_stage_b_progress",
                "publication_state",
                "r2_precommit",
                "recorded_at_utc",
                "recovery_policy",
                "schema_version",
                "source_attempt_preservation",
                "stage_a_and_go_evidence",
                "status",
                "zero_activity",
            },
        )
        self.assertEqual(
            terminal["schema_version"],
            "narratordb.v13-paid-r5-finalization-recovery-r2-terminal.v1",
        )
        self.assertEqual(
            terminal["status"],
            "terminal-deterministic-v7-retry-loader-policy-mismatch-no-release-no-retry",
        )
        execution = terminal["execution_observation"]
        self.assertTrue(execution["exact_authorized_stage_b_launched"])
        self.assertEqual(execution["exit_status"], 1)
        self.assertEqual(execution["stage"], "stage-b")
        self.assertFalse(execution["stage_a_reexecuted"])
        self.assertTrue(execution["stage_b_worker_and_sandbox_started"])
        self.assertEqual(execution["observed_stdout_bytes"], 0)
        self.assertEqual(execution["observed_stderr_bytes"], 0)
        self.assertEqual(
            execution["published_recovery_precommit_sha256"],
            prior["r2_seal_manifest_sha256"],
        )

        failure = terminal["failure"]
        self.assertEqual(
            failure["classification"], "deterministic-validation-policy-mismatch"
        )
        self.assertTrue(failure["deterministic"])
        self.assertFalse(failure["terminal_harness_failure_present"])
        self.assertFalse(failure["stderr_payload_recorded"])
        semantics = failure["generated_v7_audit_semantics"]
        self.assertEqual(
            recovery._validate_retry_count_map(
                semantics["failed_attempt_counts"],
                label="terminal R2 V7 failed-attempt counts",
            ),
            {"1": 1},
        )
        self.assertEqual(
            recovery._validate_retry_count_map(
                semantics["timed_out_attempt_counts"],
                label="terminal R2 V7 timed-out-attempt counts",
            ),
            {},
        )
        self.assertEqual(semantics["attempt_five_failures"], 0)
        self.assertTrue(semantics["complete"])
        self.assertTrue(semantics["official_harness_score_complete"])
        self.assertTrue(semantics["usage_publication_ready"])
        self.assertTrue(semantics["validation_lists_empty"])
        self.assertEqual(failure["loader_rejection_label"], "terminal harness failures")
        self.assertEqual(
            failure["sealed_loader_policy"],
            {
                "attempt_five_failures_required": 0,
                "failed_attempt_counts_required": {},
                "timed_out_attempt_counts_required": {},
            },
        )

        progress = terminal["private_stage_b_progress"]
        self.assertTrue(progress["aggregate_go_validated_before_private_computation"])
        self.assertEqual(
            progress["evaluation_audit_generator_invocations"],
            {"v13-first": 0, "v7-control": 2},
        )
        self.assertEqual(
            progress["v7_generation_passes"],
            ["private-generation", "private-byte-recomputation"],
        )
        self.assertTrue(progress["v7_payload_byte_identity_checked"])
        self.assertFalse(progress["v13_computation_started"])
        self.assertFalse(progress["result_document_computed"])
        self.assertFalse(progress["release_completion_computed"])
        self.assertTrue(progress["score_bearing_private_scratch_destroyed"])
        self.assertFalse(
            progress["score_bearing_values_read_by_operator_or_terminalizer"]
        )

        publication = terminal["publication_state"]
        self.assertTrue(publication["stage_a_envelope_published"])
        self.assertTrue(publication["terminal_status_published"])
        self.assertTrue(publication["private_scratch_absent"])
        self.assertTrue(publication["release_absent"])
        self.assertFalse(publication["recovered_result_published"])
        self.assertFalse(publication["score_bearing_audit_published"])
        self.assertFalse(
            publication["score_bearing_content_read_by_operator_or_terminalizer"]
        )
        release_path = ROOT / publication["release_path"]
        self.assertFalse(release_path.exists() or release_path.is_symlink())

        r2_entries = recovery._parse_manifest(seal_payload, basename_only=True)
        r2_bundle = ROOT / prior["r2_bundle_root"]
        self.assertEqual(len(r2_entries), 11)
        self.assertEqual(
            {item.name for item in r2_bundle.iterdir()},
            set(r2_entries) | {"SHA256SUMS"},
        )
        self.assertEqual(len(list(r2_bundle.iterdir())), 12)
        r2_precommit = terminal["r2_precommit"]
        self.assertEqual(r2_precommit["closed_world_member_count"], 11)
        self.assertEqual(r2_precommit["physical_file_count_including_seal"], 12)
        self.assertEqual(
            r2_precommit["seal_manifest_sha256"],
            prior["r2_seal_manifest_sha256"],
        )
        self.assertEqual(
            r2_precommit["prior_r1_terminal_record_sha256"],
            prior["r1_terminal_record_sha256"],
        )
        self.assertTrue(r2_precommit["all_sealed_member_nlinks_one"])
        for name, expected in r2_entries.items():
            member = r2_bundle / name
            metadata = member.lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode), name)
            self.assertEqual(metadata.st_nlink, 1, name)
            self.assertEqual(
                stat.S_IMODE(metadata.st_mode),
                0o555 if name == "run_offline_recovery.sh" else 0o444,
                name,
            )
            self.assertEqual(recovery._sha256(recovery._stable_bytes(member)), expected)

        # Follow the sealed R2 protocol all the way through the external R1
        # terminal checksum and every member of the sealed R1 bundle.  Binding
        # only the R1 record digest would not prove the recursive closed world.
        r2_protocol_path = r2_bundle / "recovery-protocol-r2.json"
        r2_protocol_payload = recovery._require_immutable(
            r2_protocol_path, maximum=recovery.MAX_JSON_BYTES, exact_mode=0o444
        )
        self.assertEqual(
            recovery._sha256(r2_protocol_payload), r2_entries[r2_protocol_path.name]
        )
        r2_protocol = json.loads(r2_protocol_payload)
        self.assertEqual(r2_protocol_payload, recovery._canonical_json(r2_protocol))
        r1_prior = r2_protocol["prior_recovery_terminal"]
        self.assertEqual(
            set(r1_prior),
            {
                "checksum_manifest_path",
                "checksum_manifest_sha256",
                "r1_bundle_root",
                "r1_seal_manifest_path",
                "r1_seal_manifest_sha256",
                "record_path",
                "record_sha256",
            },
        )
        self.assertEqual(r1_prior["record_sha256"], prior["r1_terminal_record_sha256"])

        r1_record_path = ROOT / r1_prior["record_path"]
        r1_checksum_path = ROOT / r1_prior["checksum_manifest_path"]
        r1_seal_path = ROOT / r1_prior["r1_seal_manifest_path"]
        r1_record_payload = recovery._require_immutable(
            r1_record_path, maximum=recovery.MAX_JSON_BYTES, exact_mode=0o444
        )
        r1_checksum_payload = recovery._require_immutable(
            r1_checksum_path, maximum=recovery.MAX_JSON_BYTES, exact_mode=0o444
        )
        r1_seal_payload = recovery._require_immutable(
            r1_seal_path, maximum=recovery.MAX_JSON_BYTES, exact_mode=0o444
        )
        self.assertEqual(recovery._sha256(r1_record_payload), r1_prior["record_sha256"])
        self.assertEqual(
            recovery._sha256(r1_checksum_payload),
            r1_prior["checksum_manifest_sha256"],
        )
        self.assertEqual(
            recovery._sha256(r1_seal_payload), r1_prior["r1_seal_manifest_sha256"]
        )
        self.assertEqual(
            recovery._parse_manifest(r1_checksum_payload, basename_only=True),
            {r1_record_path.name: r1_prior["record_sha256"]},
        )

        r1_terminal = json.loads(r1_record_payload)
        self.assertEqual(r1_record_payload, recovery._canonical_json(r1_terminal))
        self.assertEqual(
            r1_terminal["schema_version"],
            "narratordb.v13-paid-r5-finalization-recovery-r1-terminal.v1",
        )
        self.assertEqual(
            r1_terminal["status"],
            "terminal-launcher-preflight-failure-no-worker-no-output",
        )
        self.assertEqual(r1_terminal["failure"]["exit_status"], 67)
        self.assertEqual(r1_terminal["failure"]["failed_launcher_line"], 39)
        self.assertEqual(r1_terminal["failure"]["missing_path"], "/usr/bin/realpath")
        self.assertTrue(
            r1_terminal["failure"]["failed_before_candidate_seal_reverification"]
        )
        self.assertFalse(r1_terminal["zero_activity"]["worker_process_started"])
        self.assertFalse(r1_terminal["zero_activity"]["sandbox_process_started"])
        self.assertTrue(r1_terminal["recovery_policy"]["r1_is_terminal"])
        self.assertFalse(
            r1_terminal["recovery_policy"]["r1_overwrite_delete_resume_or_retry_allowed"]
        )
        r1_precommit = r1_terminal["r1_precommit"]
        self.assertEqual(r1_precommit["closed_world_member_count"], 10)
        self.assertEqual(r1_precommit["physical_file_count_including_seal"], 11)
        self.assertEqual(
            r1_precommit["seal_manifest_sha256"],
            r1_prior["r1_seal_manifest_sha256"],
        )
        self.assertEqual(
            r1_terminal["source_attempt_preservation"][
                "r5_attempt_tree_fingerprint_sha256"
            ],
            PROTOCOL["attempt_preservation"]["tree_fingerprint_sha256"],
        )

        r1_entries = recovery._parse_manifest(r1_seal_payload, basename_only=True)
        r1_bundle = ROOT / r1_prior["r1_bundle_root"]
        self.assertEqual(len(r1_entries), 10)
        self.assertFalse(any(item.is_symlink() for item in r1_bundle.iterdir()))
        self.assertEqual(
            {item.name for item in r1_bundle.iterdir()},
            set(r1_entries) | {r1_seal_path.name},
        )
        self.assertEqual(len(list(r1_bundle.iterdir())), 11)
        seal_metadata = r1_seal_path.lstat()
        self.assertTrue(stat.S_ISREG(seal_metadata.st_mode))
        self.assertEqual(seal_metadata.st_nlink, 1)
        self.assertEqual(stat.S_IMODE(seal_metadata.st_mode), 0o444)
        for name, expected in r1_entries.items():
            member = r1_bundle / name
            metadata = member.lstat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode), name)
            self.assertEqual(metadata.st_nlink, 1, name)
            self.assertEqual(
                stat.S_IMODE(metadata.st_mode),
                0o555 if name == "run_offline_recovery.sh" else 0o444,
                name,
            )
            self.assertEqual(recovery._sha256(recovery._stable_bytes(member)), expected)
        bound_source = inspect.getsource(recovery._validate_bound_inputs)
        r1_validator_source = inspect.getsource(recovery._validate_r1_terminal_chain)
        self.assertEqual(bound_source.count("_validate_r1_terminal_chain("), 1)
        for exact_runtime_check in (
            "_parse_manifest(checksum_payload, basename_only=True)",
            "{record.name: expected_record_sha256}",
            'len(entries) != 10',
            'len(list(bundle.iterdir())) != 11',
            '0o555 if name == "run_offline_recovery.sh" else 0o444',
        ):
            self.assertIn(exact_runtime_check, r1_validator_source)

        bound_evidence = [
            (
                prior["r2_stage_a_envelope_path"],
                prior["r2_stage_a_envelope_sha256"],
            ),
            (
                prior["r2_terminal_status_path"],
                prior["r2_terminal_status_sha256"],
            ),
            *zip(prior["r2_review_paths"], prior["r2_review_sha256"], strict=True),
            (prior["r2_aggregate_go_path"], prior["r2_aggregate_go_sha256"]),
        ]
        self.assertEqual(len(prior["r2_review_paths"]), 2)
        self.assertEqual(len(prior["r2_review_sha256"]), 2)
        for relative, expected in bound_evidence:
            payload = recovery._require_immutable(
                ROOT / relative, maximum=recovery.MAX_JSON_BYTES, exact_mode=0o444
            )
            self.assertEqual(recovery._sha256(payload), expected, relative)

        output = ROOT / prior["r2_output_root"]
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o555)
        self.assertEqual(
            {item.name for item in output.iterdir()},
            {
                Path(prior["r2_stage_a_envelope_path"]).name,
                Path(prior["r2_terminal_status_path"]).name,
            },
        )
        recorded = terminal["stage_a_and_go_evidence"]
        expected_records = {
            "aggregate_go": (
                prior["r2_aggregate_go_path"],
                prior["r2_aggregate_go_sha256"],
            ),
            "review_1": (
                prior["r2_review_paths"][0],
                prior["r2_review_sha256"][0],
            ),
            "review_2": (
                prior["r2_review_paths"][1],
                prior["r2_review_sha256"][1],
            ),
            "stage_a_envelope": (
                prior["r2_stage_a_envelope_path"],
                prior["r2_stage_a_envelope_sha256"],
            ),
        }
        for label, (relative, expected) in expected_records.items():
            self.assertEqual(recorded[label]["path"], relative)
            self.assertEqual(recorded[label]["sha256"], expected)
            self.assertEqual(recorded[label]["mode"], "0444")
            self.assertEqual(recorded[label]["nlink"], 1)

        self.assertEqual(
            terminal["source_attempt_preservation"][
                "r5_attempt_tree_fingerprint_sha256"
            ],
            PROTOCOL["attempt_preservation"]["tree_fingerprint_sha256"],
        )
        self.assertEqual(terminal["zero_activity"], PROTOCOL["zero_new_activity"])
        policy = terminal["recovery_policy"]
        self.assertTrue(policy["r2_is_terminal"])
        self.assertFalse(policy["r2_overwrite_delete_resume_or_retry_allowed"])
        self.assertTrue(policy["r3_requires_distinct_bundle_output_review_go_and_seal_paths"])
        self.assertTrue(policy["r3_requires_fresh_independent_preseal_review"])
        self.assertTrue(policy["r3_requires_separately_sealed_policy_fix"])
        self.assertEqual(
            policy["r3_namespace"],
            {
                "bundle_root": PROTOCOL["recovery_precommit"]["bundle_root"],
                "go_path": PROTOCOL["go_policy"]["aggregate_path"],
                "output_root": PROTOCOL["output"]["output_root"],
                "review_1_path": PROTOCOL["go_policy"]["review_paths"][0],
                "review_2_path": PROTOCOL["go_policy"]["review_paths"][1],
                "seal_manifest_path": PROTOCOL["recovery_precommit"][
                    "seal_manifest_path"
                ],
            },
        )

        direct = recovery._parse_manifest(
            (HERE / "BOUND_INPUTS_SHA256SUMS").read_bytes(), basename_only=False
        )
        recursively_bound = [
            (prior["record_path"], prior["record_sha256"]),
            (prior["checksum_manifest_path"], prior["checksum_manifest_sha256"]),
            (prior["r2_seal_manifest_path"], prior["r2_seal_manifest_sha256"]),
            *bound_evidence,
        ]
        for relative, expected in recursively_bound:
            self.assertEqual(direct[relative], expected)

        r2_paths = {
            prior["r2_bundle_root"],
            prior["r2_output_root"],
            prior["r2_aggregate_go_path"],
            *prior["r2_review_paths"],
        }
        r3_paths = {
            PROTOCOL["recovery_precommit"]["bundle_root"],
            PROTOCOL["output"]["output_root"],
            PROTOCOL["go_policy"]["aggregate_path"],
            *PROTOCOL["go_policy"]["review_paths"],
        }
        self.assertTrue(r2_paths.isdisjoint(r3_paths))
        self._assert_candidate_artifacts_absent()

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
                "prior_recovery_terminal": {
                    "record_sha256": "c" * 64,
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

    def test_failure_phase_enum_and_terminal_status_are_exact_and_score_free(self) -> None:
        expected_phases = {
            "launcher-or-worker-preflight",
            "stage-a-validation",
            "stage-a-publication",
            "stage-b-pre-go-validation",
            "stage-b-v7-audit",
            "stage-b-v13-audit",
            "stage-b-result-and-completion",
            "stage-b-post-computation-recheck",
            "stage-b-atomic-publication",
            "stage-b-committed-reentry",
        }
        self.assertEqual(recovery.FAILURE_PHASES, frozenset(expected_phases))
        original_phase = recovery._FAILURE_PHASE
        recovery._set_failure_phase("launcher-or-worker-preflight")
        for invalid in (
            "",
            "preflight",
            "stage-a",
            "stage-b",
            "stage-b-score",
            "stage-b-complete",
            True,
            None,
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(recovery.RecoveryError):
                    recovery._set_failure_phase(invalid)
                self.assertEqual(
                    recovery._FAILURE_PHASE, "launcher-or-worker-preflight"
                )

        try:
            with tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve()
                for index, phase in enumerate(sorted(expected_phases)):
                    stage = (
                        "stage-a"
                        if phase.startswith("stage-a-")
                        else "stage-b"
                        if phase.startswith("stage-b-")
                        else "preflight"
                    )
                    relative_output = f"terminal-{index}"
                    protocol = {
                        "attempt_preservation": {
                            "tree_fingerprint_sha256": "a" * 64,
                        },
                        "output": {
                            "failed_status_path": (
                                f"{relative_output}/recovery-terminal-status.json"
                            ),
                            "output_root": relative_output,
                        },
                        "prior_recovery_terminal": {
                            "record_sha256": "b" * 64,
                        },
                        "terminal_failure_record": {
                            "record_sha256": "c" * 64,
                        },
                        "zero_new_activity": PROTOCOL["zero_new_activity"],
                    }
                    recovery._set_failure_phase(phase)
                    recovery._terminalize(
                        root,
                        protocol,
                        stage=stage,
                        published_seal="d" * 64,
                    )
                    output = root / relative_output
                    status_path = output / "recovery-terminal-status.json"
                    payload = status_path.read_bytes()
                    status_document = json.loads(payload)
                    self.assertEqual(payload, recovery._canonical_json(status_document))
                    self.assertEqual(
                        set(status_document),
                        {
                            "credential_recorded",
                            "failure_phase",
                            "model_content_recorded",
                            "prior_recovery_terminal_record_sha256",
                            "published_recovery_precommit_sha256",
                            "recovery_attempt",
                            "schema_version",
                            "source_attempt_tree_fingerprint_sha256",
                            "stage",
                            "status",
                            "terminal_failure_record_sha256",
                            "zero_new_activity",
                        },
                    )
                    recovery._reject_score_fields(status_document)
                    self.assertEqual(status_document["failure_phase"], phase)
                    self.assertEqual(status_document["stage"], stage)
                    self.assertEqual(
                        status_document["schema_version"],
                        recovery.TERMINAL_STATUS_SCHEMA,
                    )
                    self.assertEqual(status_document["recovery_attempt"], "r3")
                    self.assertFalse(status_document["credential_recorded"])
                    self.assertFalse(status_document["model_content_recorded"])
                    self.assertEqual(
                        status_document["zero_new_activity"],
                        PROTOCOL["zero_new_activity"],
                    )
                    self.assertEqual(stat.S_IMODE(status_path.stat().st_mode), 0o444)
                    self.assertEqual(status_path.stat().st_nlink, 1)
                    self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o555)
                    os.chmod(output, 0o700)
        finally:
            recovery._set_failure_phase(original_phase)

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

    def test_recovered_retry_count_maps_are_structurally_exact(self) -> None:
        self.assertEqual(
            recovery._validate_retry_count_map({}, label="empty retry map"),
            {},
        )
        self.assertEqual(
            recovery._validate_retry_count_map(
                {"1": 1}, label="observed V7 failed retry map"
            ),
            {"1": 1},
        )
        self.assertEqual(
            recovery._validate_retry_count_map(
                {"1": 4, "2": 3, "3": 2, "4": 1},
                label="all recoverable retry ordinals",
            ),
            {"1": 4, "2": 3, "3": 2, "4": 1},
        )

        malformed = (
            None,
            [],
            {1: 1},
            {"0": 1},
            {"5": 1},
            {"01": 1},
            {"1": 0},
            {"1": -1},
            {"1": True},
            {"1": False},
            {"1": 1.0},
            {"1": "1"},
            {"1": None},
            {"1": []},
            {"1": 1, "5": 1},
        )
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaises(recovery.RecoveryError):
                    recovery._validate_retry_count_map(
                        value, label="malformed retry map"
                    )

    @staticmethod
    def _synthetic_recovered_retry_audit() -> dict[str, object]:
        empty_metric = {"accuracy": 0, "correct": 0, "total": 42}
        return {
            "by_question_type": {
                "synthetic": {
                    "top_20": dict(empty_metric),
                    "top_50": dict(empty_metric),
                }
            },
            "complete": True,
            "cutoffs": ["top_20", "top_50"],
            "evaluated_questions": 42,
            "expected_questions": 42,
            "frozen_questions": 42,
            "harness_log": {
                "attempt_five_failures": 0,
                "failed_attempt_counts": {"1": 1},
                "returned_none_responses": 0,
                "timed_out_attempt_counts": {},
            },
            "metrics": {
                "top_20": dict(empty_metric),
                "top_50": dict(empty_metric),
            },
            "official_harness_score_complete": True,
            "schema_version": 1,
            "scoped_question_subset": True,
            "usage": {
                "error_provider_counts": {},
                "error_status_counts": {},
                "invalid_completion_identities": 0,
                "publication_ready": True,
                "unknown_cost_attempts": 0,
                "upstream_errors": 0,
            },
            "validation": {
                "empty_answers": [],
                "empty_judges": [],
                "extra_evaluated_ids": [],
                "frozen_payload_mismatches": [],
                "inconsistent_verdicts": [],
                "invalid_scores": [],
                "missing_cutoffs": [],
                "missing_evaluated_ids": [],
                "missing_frozen_ids": [],
            },
        }

    def test_recovered_retry_audit_requires_complete_nonterminal_exact_structure(self) -> None:
        module, _, _ = recovery._load_sealed_verifier(ROOT, PROTOCOL)
        valid = self._synthetic_recovered_retry_audit()
        with tempfile.TemporaryDirectory() as name:
            temporary = Path(name).resolve()

            def write_candidate(index: int, document: dict[str, object]) -> Path:
                path = temporary / f"audit-{index}.json"
                path.write_bytes(recovery._canonical_json(document))
                path.chmod(0o444)
                return path

            path = write_candidate(0, valid)
            payload = path.read_bytes()
            document, observed_payload, metrics = (
                recovery._load_evaluation_audit_with_recovered_retries(
                    module,
                    path,
                    temporary,
                    label="synthetic recovered V7 audit",
                    expected_internal_sha256=recovery._sha256(payload),
                )
            )
            self.assertEqual(observed_payload, payload)
            self.assertEqual(document, valid)
            self.assertEqual(
                document["harness_log"]["failed_attempt_counts"], {"1": 1}
            )
            self.assertEqual(
                metrics,
                {
                    "top_20": {"accuracy": "0", "correct": 0, "total": 42},
                    "top_50": {"accuracy": "0", "correct": 0, "total": 42},
                },
            )
            self.assertEqual(
                {item.name for item in temporary.iterdir()}, {path.name}
            )

            invalid_mutations = (
                (
                    "attempt five terminal failure",
                    lambda item: item["harness_log"].__setitem__(
                        "attempt_five_failures", 1
                    ),
                ),
                (
                    "attempt five bool",
                    lambda item: item["harness_log"].__setitem__(
                        "attempt_five_failures", False
                    ),
                ),
                (
                    "returned none terminal failure",
                    lambda item: item["harness_log"].__setitem__(
                        "returned_none_responses", 1
                    ),
                ),
                (
                    "returned none bool",
                    lambda item: item["harness_log"].__setitem__(
                        "returned_none_responses", False
                    ),
                ),
                (
                    "fifth-attempt map key",
                    lambda item: item["harness_log"].__setitem__(
                        "failed_attempt_counts", {"5": 1}
                    ),
                ),
                (
                    "failed-attempt list instead of map",
                    lambda item: item["harness_log"].__setitem__(
                        "failed_attempt_counts", []
                    ),
                ),
                (
                    "malformed timed-out map",
                    lambda item: item["harness_log"].__setitem__(
                        "timed_out_attempt_counts", {"2": 0}
                    ),
                ),
                (
                    "bool timed-out count",
                    lambda item: item["harness_log"].__setitem__(
                        "timed_out_attempt_counts", {"2": True}
                    ),
                ),
                (
                    "incomplete",
                    lambda item: item.__setitem__("complete", False),
                ),
                (
                    "official harness incomplete",
                    lambda item: item.__setitem__(
                        "official_harness_score_complete", False
                    ),
                ),
                (
                    "validation failure",
                    lambda item: item["validation"].__setitem__(
                        "missing_cutoffs", ["top_20"]
                    ),
                ),
                (
                    "malformed validation",
                    lambda item: item.__setitem__("validation", {}),
                ),
                (
                    "publication not ready",
                    lambda item: item["usage"].__setitem__(
                        "publication_ready", False
                    ),
                ),
                (
                    "unknown cost attempt",
                    lambda item: item["usage"].__setitem__(
                        "unknown_cost_attempts", 1
                    ),
                ),
                (
                    "invalid completion identity",
                    lambda item: item["usage"].__setitem__(
                        "invalid_completion_identities", 1
                    ),
                ),
                (
                    "upstream error",
                    lambda item: item["usage"].__setitem__(
                        "upstream_errors", 1
                    ),
                ),
                (
                    "provider error",
                    lambda item: item["usage"].__setitem__(
                        "error_provider_counts", {"synthetic": 1}
                    ),
                ),
                (
                    "status error",
                    lambda item: item["usage"].__setitem__(
                        "error_status_counts", {"500": 1}
                    ),
                ),
                (
                    "extra harness field",
                    lambda item: item["harness_log"].__setitem__(
                        "terminal", False
                    ),
                ),
                (
                    "missing returned-none field",
                    lambda item: item["harness_log"].pop(
                        "returned_none_responses"
                    ),
                ),
            )
            for index, (label, mutate) in enumerate(invalid_mutations, start=1):
                candidate = json.loads(json.dumps(valid))
                mutate(candidate)
                candidate_path = write_candidate(index, candidate)
                with self.subTest(label=label):
                    with self.assertRaises(
                        (recovery.RecoveryError, module.AdmissionError)
                    ):
                        recovery._load_evaluation_audit_with_recovered_retries(
                            module,
                            candidate_path,
                            temporary,
                            label=f"synthetic invalid audit: {label}",
                        )

    def test_regenerated_audit_bytes_are_bound_to_sealed_arm_gate_hash(self) -> None:
        module, _, _ = recovery._load_sealed_verifier(ROOT, PROTOCOL)
        document = self._synthetic_recovered_retry_audit()
        with tempfile.TemporaryDirectory() as name:
            temporary = Path(name).resolve()
            path = temporary / "audit.json"
            payload = recovery._canonical_json(document)
            path.write_bytes(payload)
            path.chmod(0o444)
            with self.assertRaises(recovery.RecoveryError):
                recovery._load_evaluation_audit_with_recovered_retries(
                    module,
                    path,
                    temporary,
                    label="synthetic arm-gate-mismatched audit",
                    expected_internal_sha256="0" * 64,
                )
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual({item.name for item in temporary.iterdir()}, {path.name})

            original_generator = recovery._evaluation_audit_payload
            original_gate_hash = recovery._arm_gate_internal_audit_sha256
            recovery._evaluation_audit_payload = (
                lambda _root, _requirements, _module, _variant: payload
            )
            recovery._arm_gate_internal_audit_sha256 = (
                lambda _root, _module, _variant, *, label: "0" * 64
            )
            try:
                with self.assertRaises(recovery.RecoveryError):
                    recovery._private_evaluation_audit(
                        temporary,
                        {},
                        object(),
                        {"label": "v7-control"},
                        temporary,
                        label="synthetic regenerated audit",
                    )
            finally:
                recovery._evaluation_audit_payload = original_generator
                recovery._arm_gate_internal_audit_sha256 = original_gate_hash
            self.assertEqual({item.name for item in temporary.iterdir()}, {path.name})

    def test_evaluation_auditor_argv_is_exact_and_shared(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            paths = {
                "auditor": root / "sealed-runtime/narratordb/benchmarks/evaluation_audit.py",
                "evaluated": root / "private/v7/evaluated",
                "frozen": root / "private/v7/frozen",
                "ledger": root / "private/v7/usage.jsonl",
                "evaluator_log": root / "private/v7/evaluate.log",
                "question_scope": root / "private/v7/question-ids.json",
            }
            expected = (
                recovery.sys.executable,
                "-I",
                "-S",
                "-B",
                str(paths["auditor"]),
                "--evaluated-directory",
                str(paths["evaluated"]),
                "--frozen-directory",
                str(paths["frozen"]),
                "--usage-log",
                str(paths["ledger"]),
                "--evaluator-log",
                str(paths["evaluator_log"]),
                "--expected-questions",
                "42",
                "--cutoffs",
                "20,50",
                "--question-id-file",
                str(paths["question_scope"]),
                "--require-complete",
                "--require-official-score-complete",
            )
            self.assertEqual(recovery._evaluation_audit_command(**paths), expected)

            calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
            original_run = recovery.subprocess.run

            def exact_runner(
                command: tuple[str, ...], **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                calls.append((command, kwargs))
                return subprocess.CompletedProcess(
                    command, 0, stdout=b"synthetic-audit\n", stderr=b""
                )

            recovery.subprocess.run = exact_runner
            try:
                payload, observed_command = recovery._run_evaluation_auditor(
                    root, **paths
                )
            finally:
                recovery.subprocess.run = original_run
            self.assertEqual(payload, b"synthetic-audit\n")
            self.assertEqual(observed_command, expected)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], expected)
            self.assertEqual(
                calls[0][1],
                {
                    "check": False,
                    "cwd": root,
                    "env": {"LANG": "C", "LC_ALL": "C"},
                    "stderr": subprocess.PIPE,
                    "stdout": subprocess.PIPE,
                    "timeout": 60,
                },
            )
            production_source = inspect.getsource(recovery._evaluation_audit_payload)
            canary_source = inspect.getsource(recovery._run_stage_b_canary)
            self.assertEqual(production_source.count("_run_evaluation_auditor("), 1)
            self.assertEqual(canary_source.count("_run_evaluation_auditor("), 2)
            self.assertNotIn("subprocess.run(", production_source)
            self.assertNotIn("subprocess.run(", canary_source)

    def test_generated_and_committed_audit_paths_share_retry_aware_recomputation(self) -> None:
        private_source = inspect.getsource(recovery._private_evaluation_audit)
        committed_source = inspect.getsource(recovery._validate_committed_release)
        core_source = inspect.getsource(recovery._validate_committed_release_core)
        reentry_source = inspect.getsource(recovery._reenter_committed_release)
        stage_b_source = inspect.getsource(recovery._run_stage_b)
        canary_source = inspect.getsource(recovery._run_stage_b_canary)
        helper_source = inspect.getsource(
            recovery._recompute_evaluation_audit_with_recovered_retries
        )
        helper_call = "_recompute_evaluation_audit_with_recovered_retries("
        self.assertEqual(private_source.count(helper_call), 1)
        self.assertEqual(core_source.count(helper_call), 2)
        self.assertNotIn("module._recompute_evaluation_audit(", private_source)
        self.assertNotIn("module._recompute_evaluation_audit(", core_source)
        self.assertEqual(committed_source.count("_validate_committed_release_core("), 1)
        self.assertEqual(reentry_source.count("_validate_committed_release("), 1)
        self.assertEqual(stage_b_source.count("_reenter_committed_release("), 1)
        self.assertEqual(canary_source.count("_reenter_committed_release("), 1)
        for exact_hook in (
            "preserved_state_validator(\"before\")",
            "_load_stage_a(",
            "_validate_release_go_copies(",
            "_result_document(",
            "_completion_document(",
            "preserved_state_validator(\"after\")",
        ):
            self.assertIn(exact_hook, core_source)
        self.assertIn('trace.append("production-reentry-branch")', reentry_source)
        self.assertIn(
            "expected_internal_sha256 = _arm_gate_internal_audit_sha256(",
            helper_source,
        )
        self.assertIn("sealed_loader=original_loader", helper_source)
        self.assertIn("module._load_evaluation_audit = original_loader", helper_source)

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

    def test_launcher_executable_manifest_is_canonical_and_matches_host(self) -> None:
        manifest_path = HERE / "launcher-executables-r3.json"
        manifest_payload = recovery._stable_bytes(
            manifest_path, maximum=recovery.MAX_JSON_BYTES
        )
        manifest = json.loads(manifest_payload)
        self.assertEqual(manifest_payload, recovery._canonical_json(manifest))
        self.assertEqual(
            recovery._sha256(manifest_payload),
            PROTOCOL["execution_environment"]["launcher_executable_manifest_sha256"],
        )
        self.assertEqual(
            str(manifest_path.relative_to(ROOT)),
            PROTOCOL["execution_environment"]["launcher_executable_manifest_path"],
        )
        self.assertEqual(
            manifest["host"],
            {
                "architecture": PROTOCOL["execution_environment"]["architecture"],
                "macos_build": PROTOCOL["execution_environment"]["macos_build"],
                "macos_product_version": PROTOCOL["execution_environment"][
                    "macos_product_version"
                ],
            },
        )

        expected_paths = {
            "/bin/bash",
            "/bin/chmod",
            "/bin/mkdir",
            "/bin/realpath",
            "/bin/rm",
            "/usr/bin/codesign",
            "/usr/bin/env",
            "/usr/bin/mktemp",
            "/usr/bin/perl",
            "/usr/bin/sandbox-exec",
            "/usr/bin/shasum",
            "/usr/bin/stat",
            "/usr/bin/sw_vers",
            "/usr/bin/uname",
            PROTOCOL["execution_environment"]["python_real_path"],
        }
        records = manifest["executables"]
        self.assertEqual(len(records), len(expected_paths))
        self.assertEqual({record["path"] for record in records}, expected_paths)
        self.assertEqual(
            len({record["path"] for record in records}), len(records)
        )
        for record in records:
            path = Path(record["path"])
            metadata = path.lstat()
            self.assertTrue(path.is_absolute(), record["path"])
            self.assertTrue(stat.S_ISREG(metadata.st_mode), record["path"])
            self.assertEqual(metadata.st_nlink, record["nlink"], record["path"])
            self.assertEqual(metadata.st_size, record["size_bytes"], record["path"])
            self.assertEqual(
                f"{stat.S_IMODE(metadata.st_mode):04o}",
                record["mode"],
                record["path"],
            )
            self.assertNotEqual(stat.S_IMODE(metadata.st_mode) & 0o111, 0, record["path"])
            self.assertEqual(
                recovery._sha256(recovery._stable_bytes(path)),
                record["sha256"],
                record["path"],
            )
            if record["apple_codesign_required"]:
                result = subprocess.run(
                    ["/usr/bin/codesign", "-v", record["path"]],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, record["path"])
            else:
                self.assertEqual(record["path"], "/usr/bin/shasum")
                self.assertEqual(record["interpreter_path"], "/usr/bin/perl")
                self.assertTrue(path.read_bytes().startswith(b"#!/usr/bin/perl\n"))

        realpath_record = next(
            record for record in records if record["path"] == "/bin/realpath"
        )
        self.assertEqual(
            realpath_record["sha256"],
            PROTOCOL["execution_environment"]["realpath_sha256"],
        )
        self.assertEqual(
            realpath_record["apple_codesign_identifier"],
            PROTOCOL["execution_environment"]["realpath_codesign_identifier"],
        )
        identity = subprocess.run(
            ["/usr/bin/codesign", "-d", "--verbose=4", "/bin/realpath"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(identity.returncode, 0)
        self.assertIn(
            b"Identifier=com.apple.realpath", identity.stdout + identity.stderr
        )
        resolved = subprocess.run(
            ["/bin/realpath", str(ROOT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(resolved.returncode, 0)
        self.assertEqual(resolved.stdout, f"{ROOT}\n".encode())
        self.assertEqual(resolved.stderr, b"")

    def test_launcher_absolute_executable_references_are_manifest_closed(self) -> None:
        launcher = (HERE / "run_offline_recovery.sh").read_text()
        manifest = json.loads((HERE / "launcher-executables-r3.json").read_bytes())
        declared = {record["path"] for record in manifest["executables"]}
        referenced = set(
            re.findall(
                r"(?<![A-Za-z0-9_.-])((?:/bin|/usr/bin)/[A-Za-z0-9_.+-]+)",
                launcher,
            )
        )
        python_real = PROTOCOL["execution_environment"]["python_real_path"]
        python_root = str(Path(python_real).parents[1])
        self.assertIn(f'python_root="{python_root}"', launcher)
        self.assertIn('python_real="$python_root/bin/python3.12"', launcher)
        self.assertIn('require_tool "$python_real"', launcher)
        referenced.add(python_real)
        self.assertEqual(referenced, declared)
        self.assertNotIn("/usr/bin/realpath", launcher)
        self.assertNotIn("/usr/bin/awk", launcher)
        self.assertIn("/bin/realpath", launcher)
        self.assertEqual(
            PROTOCOL["execution_environment"]["realpath_path"], "/bin/realpath"
        )
        self.assertTrue(launcher.startswith("#!/bin/bash -p\n"))
        self.assertNotRegex(
            launcher,
            r"(?m)^[ \t]*(?:awk|bash|chmod|codesign|env|mkdir|mktemp|perl|"
            r"realpath|rm|sandbox-exec|shasum|stat|sw_vers|uname)(?:[ \t]|$)",
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

    def test_actual_launcher_preflight_smoke_is_end_to_end_and_leaves_no_artifact(self) -> None:
        self._assert_candidate_artifacts_absent()
        scratch_before = self._recovery_scratch_names()
        bytecode_before = {
            path.relative_to(HERE) for path in HERE.rglob("*.pyc")
        } | {
            path.relative_to(HERE) for path in HERE.rglob("__pycache__")
        }
        bundle_names_before = {path.name for path in HERE.iterdir()}
        result = subprocess.run(
            [
                "/bin/bash",
                "-p",
                str(HERE / "run_offline_recovery.sh"),
                "preflight-smoke",
            ],
            cwd=ROOT,
            env={},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        self._assert_candidate_artifacts_absent()
        self.assertEqual(self._recovery_scratch_names(), scratch_before)
        self.assertEqual({path.name for path in HERE.iterdir()}, bundle_names_before)
        bytecode_after = {
            path.relative_to(HERE) for path in HERE.rglob("*.pyc")
        } | {
            path.relative_to(HERE) for path in HERE.rglob("__pycache__")
        }
        self.assertEqual(bytecode_after, bytecode_before)

    def test_actual_launcher_stage_b_canary_covers_private_pipeline_without_artifact(self) -> None:
        launcher = (HERE / "run_offline_recovery.sh").read_text()
        worker = WORKER.read_text()
        self.assertEqual(
            recovery.STAGE_B_CANARY_STEPS,
            (
                "synthetic-private-evidence",
                "exact-auditor-v7",
                "exact-auditor-v13",
                "attempt-five-rejected",
                "generated-audit-parser",
                "atomic-eight-file-rename",
                "production-reentry-branch",
                "stage-a-reconstruction",
                "go-review-validation",
                "retry-aware-evidence-recomputation",
                "result-completion-validation",
                "postscore-bundle",
                "postscore-bound-inputs",
                "postscore-attempt",
                "exact-subprocess-invocations",
                "private-cleanup",
            ),
        )
        self.assertIn("stage-b-canary", launcher)
        self.assertIn('"$worker" stage-b-canary', launcher)
        self.assertIn("stage-b-canary", worker)
        canary_block = launcher.split(
            'if [[ "$stage" == stage-b-canary ]]; then', 1
        )[1].split("\nseal_manifest=", 1)[0]
        for exact_fragment in (
            "/usr/bin/env -i",
            'HOME="$scratch/home"',
            'TMPDIR="$scratch/tmp"',
            "LANG=C",
            "LC_ALL=C",
            "PYTHONDONTWRITEBYTECODE=1",
            "/usr/bin/sandbox-exec",
            '-D "ROOT=$root"',
            '-D "PYROOT=$python_root"',
            '-D "PYTHON_REAL=$python_real"',
            '-D "SCRATCH=$scratch"',
            '-D "OUT=$canary_output"',
            '-f "$profile"',
            '"$python_real" -I -S -B "$worker" stage-b-canary',
            '--repository-root "$root"',
            '--protocol "$protocol"',
        ):
            self.assertIn(exact_fragment, canary_block)
        self.assertNotIn("published-recovery-seal", canary_block)
        self.assertNotIn(
            PROTOCOL["recovery_precommit"][
                "published_seal_environment_variable"
            ],
            canary_block,
        )

        self._assert_candidate_artifacts_absent()
        scratch_before = self._recovery_scratch_names()
        bytecode_before = {
            path.relative_to(HERE) for path in HERE.rglob("*.pyc")
        } | {
            path.relative_to(HERE) for path in HERE.rglob("__pycache__")
        }
        bundle_names_before = {path.name for path in HERE.iterdir()}
        result = subprocess.run(
            [
                "/bin/bash",
                "-p",
                str(HERE / "run_offline_recovery.sh"),
                "stage-b-canary",
            ],
            cwd=ROOT,
            env={},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        self._assert_candidate_artifacts_absent()
        self.assertEqual(self._recovery_scratch_names(), scratch_before)
        self.assertEqual({path.name for path in HERE.iterdir()}, bundle_names_before)
        bytecode_after = {
            path.relative_to(HERE) for path in HERE.rglob("*.pyc")
        } | {
            path.relative_to(HERE) for path in HERE.rglob("__pycache__")
        }
        self.assertEqual(bytecode_after, bytecode_before)

    def test_actual_launcher_zero_seal_stage_a_canary_exits_71_without_artifact(self) -> None:
        self._assert_candidate_artifacts_absent()
        scratch_before = self._recovery_scratch_names()
        bytecode_before = {
            path.relative_to(HERE) for path in HERE.rglob("*.pyc")
        } | {
            path.relative_to(HERE) for path in HERE.rglob("__pycache__")
        }
        bundle_names_before = {path.name for path in HERE.iterdir()}
        result = subprocess.run(
            [
                "/bin/bash",
                "-p",
                str(HERE / "run_offline_recovery.sh"),
                "stage-a",
            ],
            cwd=ROOT,
            env={
                PROTOCOL["recovery_precommit"][
                    "published_seal_environment_variable"
                ]: "0" * 64,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 71)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        self._assert_candidate_artifacts_absent()
        self.assertEqual(self._recovery_scratch_names(), scratch_before)
        self.assertEqual({path.name for path in HERE.iterdir()}, bundle_names_before)
        bytecode_after = {
            path.relative_to(HERE) for path in HERE.rglob("*.pyc")
        } | {
            path.relative_to(HERE) for path in HERE.rglob("__pycache__")
        }
        self.assertEqual(bytecode_after, bytecode_before)

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

    def test_candidate_has_no_output_go_review_or_bytecode(self) -> None:
        self._assert_candidate_artifacts_absent()
        inventory = json.loads((HERE / "closed-world-inventory.json").read_bytes())
        if (HERE / "SHA256SUMS").exists():
            self._assert_sealed_bundle(inventory)
        else:
            self.assertEqual(
                {path.name for path in HERE.iterdir()},
                set(inventory["allowed_bundle_files_before_seal"]),
            )
        self.assertEqual(list(HERE.rglob("*.pyc")), [])
        self.assertEqual(list(HERE.rglob("__pycache__")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
