#!/usr/bin/env python3
"""Closed-world, content-free admission verifier for the V13 paid pair.

The verifier never contacts a provider.  It validates already captured,
sanitized evidence and refuses any missing, stale, extra, inconsistent, or
mutated field.  ``build-authorization`` and ``build-audit`` create immutable
derived records. ``build-campaign-audit`` is the only supported campaign-audit
producer, and ``build-paired-result`` is the only supported score-publication
producer. ``verify`` is the only mode used by the paid wrapper.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


REQUIREMENTS_SCHEMA = "narratordb.v13-paid-dynamic-admission-requirements.v4"
PROVIDER_SCHEMA = "narratordb.provider-key-telemetry.v2"
FX_SCHEMA = "narratordb.ecb-usd-eur-observation.v1"
AUTHORIZATION_SCHEMA = "narratordb.v13-paid-execution-authorization.v2"
AUDIT_SCHEMA = "narratordb.v13-paid-execution-authorization-audit.v1"
FINALIZATION_SCHEMA = "narratordb.v13-paid-final-spend-authorization.v1"
FINALIZATION_AUDIT_SCHEMA = "narratordb.v13-paid-final-spend-authorization-audit.v1"
PAIRED_RESULT_SCHEMA = "narratordb.v13-paid-paired-result.v2"
CAMPAIGN_AUDIT_SCHEMA = "narratordb.campaign-budget-audit.v1"
COPY_MANIFEST_SCHEMA = "narratordb.paired-evaluation-copy.v1"
LEDGER_AUDIT_SCHEMA = "narratordb.v13-paid-evaluator-ledger-identity-audit.v1"
ARM_GATE_SCHEMA = "narratordb.arm-evaluation-gate.v1"

SHA256_RE = re.compile(r"[0-9a-f]{64}")
DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
UTC_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
FORBIDDEN_KEYS = frozenset(
    {
        "account",
        "account_id",
        "answer",
        "api_key",
        "authorization",
        "bearer",
        "choices",
        "completion",
        "content",
        "credential",
        "input",
        "key",
        "messages",
        "output",
        "password",
        "prompt",
        "question",
        "raw_response",
        "request",
        "response",
        "secret",
        "text",
        "token",
        "user",
        "user_id",
    }
)
SECRET_PATTERNS = (
    re.compile(rb"\bsk-(?:or-v1-)?[A-Za-z0-9_-]{20,}"),
    re.compile(rb"\bBearer[ \t]+[A-Za-z0-9._~+/-]{20,}", re.I),
    re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
)
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_XML_BYTES = 1024 * 1024
CENT = Decimal("0.01")
ARM_GATE_VALIDATION_FIELDS = frozenset(
    {
        "empty_answers",
        "empty_judges",
        "frozen_payload_mismatches",
        "missing_evaluated_ids",
        "missing_frozen_ids",
        "extra_evaluated_ids",
        "missing_cutoffs",
        "invalid_scores",
        "inconsistent_verdicts",
    }
)
ARM_GATE_USAGE_FIELDS = frozenset(
    {
        "events",
        "completion_calls",
        "upstream_errors",
        "malformed_http_200_responses",
        "invalid_completion_identities",
        "unknown_cost_attempts",
        "publication_ready",
        "provider_counts",
        "completion_provider_counts",
        "error_provider_counts",
        "request_model_counts",
        "error_status_counts",
        "finish_reason_counts",
        "cost_usd",
        "prompt_tokens",
        "cached_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "first_timestamp",
        "last_timestamp",
    }
)
ARM_GATE_FORBIDDEN_CONTENT_KEYS = frozenset(
    {
        "metrics",
        "by_question_type",
        "correct",
        "accuracy",
        "score",
        "verdict",
        "judgment",
        "generated_answer",
        "judge_raw",
    }
)


class AdmissionError(ValueError):
    """Raised whenever a paid admission invariant is not proven."""


def _reject_float(value: str) -> None:
    raise AdmissionError(f"JSON decimal numbers must be encoded as strings: {value}")


def _reject_constant(value: str) -> None:
    raise AdmissionError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdmissionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_keys(
    value: Mapping[str, Any], *, expected: set[str], label: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise AdmissionError(
            f"{label} fields are not closed-world: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def _scan_for_secrets(value: Any, *, label: str) -> None:
    if isinstance(value, Mapping):
        forbidden = sorted(str(key) for key in value if str(key).casefold() in FORBIDDEN_KEYS)
        if forbidden:
            raise AdmissionError(f"{label} contains forbidden fields: {forbidden}")
        for child in value.values():
            _scan_for_secrets(child, label=label)
    elif isinstance(value, list):
        for child in value:
            _scan_for_secrets(child, label=label)


def _scan_arm_gate_for_score_content(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = sorted(
            str(key)
            for key in value
            if str(key).casefold() in ARM_GATE_FORBIDDEN_CONTENT_KEYS
        )
        if forbidden:
            raise AdmissionError(
                f"arm gate contains score-bearing fields: {forbidden}"
            )
        for child in value.values():
            _scan_arm_gate_for_score_content(child)
    elif isinstance(value, list):
        for child in value:
            _scan_arm_gate_for_score_content(child)


def _stable_bytes(path: Path, *, maximum: int, label: str) -> bytes:
    if path.is_symlink():
        raise AdmissionError(f"{label} may not be a symlink")
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise AdmissionError(f"{label} is missing: {path}") from error
    if not stat.S_ISREG(before.st_mode):
        raise AdmissionError(f"{label} must be a regular file")
    if before.st_size > maximum:
        raise AdmissionError(f"{label} exceeds its byte limit")
    payload = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if len(payload) != before.st_size or identity_before != identity_after:
        raise AdmissionError(f"{label} changed while being read")
    return payload


def _load_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    payload = _stable_bytes(path, maximum=MAX_JSON_BYTES, label=label)

    return _parse_json_payload(payload, label=label), payload


def _parse_json_payload(payload: bytes, *, label: str) -> dict[str, Any]:
    if len(payload) > MAX_JSON_BYTES:
        raise AdmissionError(f"{label} exceeds its byte limit")
    for pattern in SECRET_PATTERNS:
        if pattern.search(payload):
            raise AdmissionError(f"{label} contains credential-like material")
    try:
        parsed = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, AdmissionError) as error:
        raise AdmissionError(f"invalid {label}: {error}") from error
    if not isinstance(parsed, dict):
        raise AdmissionError(f"{label} must be a JSON object")
    _scan_for_secrets(parsed, label=label)
    return parsed


def _parse_arm_gate_payload(payload: bytes, *, label: str) -> dict[str, Any]:
    """Parse the gate's numeric telemetry without weakening other JSON inputs."""

    if len(payload) > MAX_JSON_BYTES:
        raise AdmissionError(f"{label} exceeds its byte limit")
    for pattern in SECRET_PATTERNS:
        if pattern.search(payload):
            raise AdmissionError(f"{label} contains credential-like material")
    try:
        parsed = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, AdmissionError) as error:
        raise AdmissionError(f"invalid {label}: {error}") from error
    if not isinstance(parsed, dict):
        raise AdmissionError(f"{label} must be a JSON object")
    _scan_for_secrets(parsed, label=label)
    return parsed


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path, *, label: str = "artifact") -> str:
    return _sha256(_stable_bytes(path, maximum=64 * 1024 * 1024, label=label))


def _sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise AdmissionError(f"{label} must be a lowercase SHA-256")
    return value


def _decimal(value: Any, *, label: str, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not DECIMAL_RE.fullmatch(value):
        raise AdmissionError(f"{label} must be a canonical nonnegative decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise AdmissionError(f"{label} is not a decimal") from error
    if not parsed.is_finite() or parsed < 0 or (positive and parsed <= 0):
        raise AdmissionError(f"{label} is outside the allowed range")
    return parsed


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not UTC_RE.fullmatch(value):
        raise AdmissionError(f"{label} must be second-precision RFC3339 UTC")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise AdmissionError(f"{label} is invalid") from error
    return parsed


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _repository_path(
    root: Path, relative: Any, *, label: str, must_exist: bool = True
) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise AdmissionError(f"{label} must be a nonempty POSIX repository path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise AdmissionError(f"{label} escapes the repository")
    candidate = root.joinpath(*pure.parts)
    if must_exist:
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as error:
            raise AdmissionError(f"{label} is missing: {relative}") from error
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise AdmissionError(f"{label} resolves outside the repository") from error
        current = root
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise AdmissionError(f"{label} traverses a symlink")
        return resolved
    parent = candidate.parent
    while parent != root and not parent.exists():
        parent = parent.parent
    if parent.exists() and parent.resolve(strict=True) != parent.absolute():
        raise AdmissionError(f"{label} parent traverses a symlink")
    return candidate.absolute()


def _canonical_json(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_new(path: Path, document: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise AdmissionError(f"refusing to overwrite immutable artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(document)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _requirements(root: Path, path: Path) -> tuple[dict[str, Any], str]:
    document, payload = _load_json(path, label="dynamic admission requirements")
    _require_keys(
        document,
        expected={
            "schema_version",
            "predecessor",
            "revision",
            "campaign",
            "provider",
            "fx",
            "runtime_sources",
            "vendor_environment",
            "arm_gate_policy",
            "variants",
            "phases",
            "finalization",
        },
        label="dynamic admission requirements",
    )
    if document["schema_version"] != REQUIREMENTS_SCHEMA:
        raise AdmissionError("unsupported dynamic admission requirements schema")
    predecessor = document["predecessor"]
    if not isinstance(predecessor, Mapping):
        raise AdmissionError("predecessor must be an object")
    _require_keys(
        predecessor,
        expected={"manifest_path", "manifest_sha256"},
        label="predecessor",
    )
    predecessor_path = _repository_path(
        root, predecessor["manifest_path"], label="predecessor manifest"
    )
    if _sha256_file(predecessor_path, label="predecessor manifest") != _sha(
        predecessor["manifest_sha256"], label="predecessor manifest_sha256"
    ):
        raise AdmissionError("superseded predecessor seal changed")
    revision = document["revision"]
    if not isinstance(revision, Mapping):
        raise AdmissionError("revision must be an object")
    _require_keys(
        revision,
        expected={
            "manifest_path",
            "protocol_path",
            "protocol_sha256",
            "ledger_verifier_path",
            "ledger_verifier_sha256",
            "telemetry_capture_path",
            "telemetry_capture_sha256",
            "fx_capture_path",
            "fx_capture_sha256",
            "credential_launcher_path",
            "credential_launcher_sha256",
            "paid_wrapper_path",
            "paid_wrapper_sha256",
            "harness_guard_path",
            "harness_guard_sha256",
            "proxy_config_path",
            "proxy_config_sha256",
            "proxy_runtime_path",
            "proxy_runtime_sha256",
            "proxy_guard_path",
            "proxy_guard_sha256",
            "arm_gate_path",
            "arm_gate_sha256",
            "arm_gate_evaluation_auditor_path",
            "arm_gate_evaluation_auditor_sha256",
            "arm_gate_guard_path",
            "arm_gate_guard_sha256",
        },
        label="revision",
    )
    for prefix in (
        "protocol",
        "ledger_verifier",
        "telemetry_capture",
        "fx_capture",
        "credential_launcher",
        "paid_wrapper",
        "harness_guard",
        "proxy_config",
        "proxy_runtime",
        "proxy_guard",
        "arm_gate",
        "arm_gate_evaluation_auditor",
        "arm_gate_guard",
    ):
        artifact = _repository_path(root, revision[f"{prefix}_path"], label=prefix)
        if _sha256_file(artifact, label=prefix) != _sha(
            revision[f"{prefix}_sha256"], label=f"{prefix}_sha256"
        ):
            raise AdmissionError(f"sealed {prefix} changed")
    arm_policy = document["arm_gate_policy"]
    if not isinstance(arm_policy, Mapping):
        raise AdmissionError("arm gate policy must be an object")
    _require_keys(
        arm_policy,
        expected={
            "schema_version",
            "expected_questions",
            "cutoffs",
            "allowed_request_models",
            "allowed_providers",
            "max_cost_usd",
        },
        label="arm gate policy",
    )
    if (
        arm_policy["schema_version"] != "narratordb.arm-evaluation-gate.v1"
        or arm_policy["expected_questions"] != 42
        or arm_policy["cutoffs"] != "20,50"
        or arm_policy["allowed_request_models"]
        != ["deepseek/deepseek-v4-flash-20260423", "z-ai/glm-5.2"]
        or arm_policy["allowed_providers"]
        != ["DeepInfra", "StreamLake", "GMICloud", "Baidu", "AtlasCloud"]
        or arm_policy["max_cost_usd"] != "2.50"
    ):
        raise AdmissionError("arm gate policy changed")
    return document, _sha256(payload)


def _variant_map(
    root: Path, requirements: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    variants = requirements["variants"]
    if not isinstance(variants, list) or len(variants) != 2:
        raise AdmissionError("exactly two variants are required")
    result: dict[str, Mapping[str, Any]] = {}
    expected_fields = {
        "label",
        "run_root",
        "project_name",
        "dataset_path",
        "dataset_sha256",
        "working_copy_manifest_path",
        "staged_copy_manifest_path",
        "staged_copy_manifest_sha256",
        "staged_prediction_directory",
        "question_ids_sha256",
        "question_ids_path",
        "ledger_path",
        "initial_ledger_sha256",
        "initial_ledger_bytes_hex",
        "soft_fuse_usd",
    }
    for index, variant in enumerate(variants):
        if not isinstance(variant, Mapping):
            raise AdmissionError("variant entries must be objects")
        _require_keys(variant, expected=expected_fields, label=f"variant {index}")
        label = variant["label"]
        if label not in {"v7-control", "v13-first"} or label in result:
            raise AdmissionError("variant labels must be the exact declared pair")
        _decimal(variant["soft_fuse_usd"], label=f"{label} fuse", positive=True)
        _sha(variant["dataset_sha256"], label=f"{label} dataset_sha256")
        _sha(variant["question_ids_sha256"], label=f"{label} question_ids_sha256")
        question_ids_path = _repository_path(
            root,
            variant["question_ids_path"],
            label=f"{label} question IDs",
        )
        if _sha256_file(question_ids_path, label=f"{label} question IDs") != variant[
            "question_ids_sha256"
        ]:
            raise AdmissionError(f"{label} question IDs changed")
        _sha(
            variant["staged_copy_manifest_sha256"],
            label=f"{label} staged copy manifest",
        )
        _sha(variant["initial_ledger_sha256"], label=f"{label} initial ledger")
        staged_directory = variant["staged_prediction_directory"]
        staged_manifest = variant["staged_copy_manifest_path"]
        if not isinstance(staged_directory, str) or not isinstance(staged_manifest, str):
            raise AdmissionError("staged copy paths must be strings")
        expected_staged_manifest = (
            PurePosixPath(staged_directory).parent / "frozen-copy-manifest.json"
        ).as_posix()
        if staged_manifest != expected_staged_manifest:
            raise AdmissionError("staged copy manifest is not beside its exact source tree")
        if not isinstance(variant["initial_ledger_bytes_hex"], str):
            raise AdmissionError("initial ledger bytes must be hex")
        try:
            initial = bytes.fromhex(variant["initial_ledger_bytes_hex"])
        except ValueError as error:
            raise AdmissionError("invalid initial ledger bytes") from error
        if _sha256(initial) != variant["initial_ledger_sha256"]:
            raise AdmissionError("initial ledger byte/hash binding is inconsistent")
        result[label] = variant
    return result


def _phase_config(
    requirements: Mapping[str, Any], phase: str
) -> Mapping[str, Any]:
    phases = requirements["phases"]
    if not isinstance(phases, Mapping) or set(phases) != {"before-v7", "before-v13"}:
        raise AdmissionError("phases must be the exact closed pair")
    config = phases.get(phase)
    if not isinstance(config, Mapping):
        raise AdmissionError("unknown admission phase")
    _require_keys(
        config,
        expected={
            "variant",
            "unspent_fuses_usd",
            "campaign_audit_path",
            "provider_telemetry_path",
            "fx_metadata_path",
            "fx_raw_xml_path",
            "authorization_path",
            "independent_audit_path",
            "prior_ledger_identity_audit_path",
            "prior_provider_telemetry_path",
            "prior_arm_gate_path",
        },
        label=f"phase {phase}",
    )
    expected_variant = "v7-control" if phase == "before-v7" else "v13-first"
    if config["variant"] != expected_variant:
        raise AdmissionError("phase-to-variant mapping changed")
    return config


def _verify_exact_argument_tuple(
    root: Path,
    variant: Mapping[str, Any],
    *,
    run_root: str,
    project_name: str,
    dataset_path: str,
) -> dict[str, Any]:
    expected = {
        "run_root": variant["run_root"],
        "project_name": variant["project_name"],
        "dataset_path": variant["dataset_path"],
    }
    actual = {
        "run_root": run_root,
        "project_name": project_name,
        "dataset_path": dataset_path,
    }
    if actual != expected:
        raise AdmissionError(f"paid variant argument tuple mismatch: {actual!r}")
    dataset = _repository_path(root, dataset_path, label="dataset")
    if _sha256_file(dataset, label="dataset") != variant["dataset_sha256"]:
        raise AdmissionError("dataset hash changed")
    return expected


def _copy_manifest_files(
    document: Mapping[str, Any], *, label: str
) -> list[Mapping[str, Any]]:
    _require_keys(
        document,
        expected={
            "schema_version",
            "project_name",
            "expected_questions",
            "question_id_file",
            "question_ids_sha256",
            "frozen_directory",
            "evaluated_directory",
            "file_count",
            "prediction_file_count",
            "files",
            "checks",
        },
        label=label,
    )
    if document["schema_version"] != COPY_MANIFEST_SCHEMA:
        raise AdmissionError(f"unsupported {label} schema")
    if not isinstance(document["question_id_file"], str):
        raise AdmissionError(f"{label} question-ID path must be a string")
    checks = document["checks"]
    if not isinstance(checks, Mapping):
        raise AdmissionError(f"{label} checks must be an object")
    _require_keys(
        checks,
        expected={
            "fresh_output_root",
            "question_scope_exact",
            "source_stable_during_copy",
            "copy_byte_identical",
            "prediction_payloads_parsed",
        },
        label=f"{label} checks",
    )
    if checks != {
        "fresh_output_root": True,
        "question_scope_exact": True,
        "source_stable_during_copy": True,
        "copy_byte_identical": True,
        "prediction_payloads_parsed": False,
    }:
        raise AdmissionError(f"{label} preparation checks failed")
    files = document["files"]
    file_count = document["file_count"]
    if (
        not isinstance(files, list)
        or isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or file_count != len(files)
    ):
        raise AdmissionError(f"{label} file manifest is inconsistent")
    declared_paths: list[str] = []
    for index, entry in enumerate(files):
        if not isinstance(entry, Mapping):
            raise AdmissionError(f"{label} file entry must be an object")
        _require_keys(
            entry,
            expected={"path", "bytes", "sha256"},
            label=f"{label} file {index}",
        )
        relative = entry["path"]
        if not isinstance(relative, str) or "\\" in relative:
            raise AdmissionError(f"invalid {label} relative file path")
        pure = PurePosixPath(relative)
        if (
            not relative
            or pure.is_absolute()
            or ".." in pure.parts
            or "." in pure.parts
            or pure.as_posix() != relative
            or relative in declared_paths
        ):
            raise AdmissionError(f"invalid {label} relative file path")
        byte_count = entry["bytes"]
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
        ):
            raise AdmissionError(f"{label} file byte count is invalid")
        _sha(entry["sha256"], label=f"{label} file sha256")
        declared_paths.append(relative)
    if declared_paths != sorted(declared_paths):
        raise AdmissionError(f"{label} file entries are not canonically ordered")
    return files


def _tree_inventory(base: Path, *, label: str) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()

    def visit(current: Path, prefix: PurePosixPath | None) -> None:
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as error:
            raise AdmissionError(f"cannot inspect {label}") from error
        for entry in entries:
            relative = (
                PurePosixPath(entry.name)
                if prefix is None
                else prefix / entry.name
            )
            relative_text = relative.as_posix()
            if entry.is_symlink():
                raise AdmissionError(f"{label} contains a symbolic link")
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise AdmissionError(f"cannot inspect an entry in {label}") from error
            if stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise AdmissionError(f"{label} contains a hard-linked file")
                files.add(relative_text)
            elif stat.S_ISDIR(metadata.st_mode):
                directories.add(relative_text)
                visit(Path(entry.path), relative)
            else:
                raise AdmissionError(f"{label} contains a nonregular entry")

    visit(base, None)
    return files, directories


def _verify_manifest_tree(
    base: Path,
    files: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> None:
    declared_files = {str(entry["path"]) for entry in files}
    declared_directories: set[str] = set()
    for relative in declared_files:
        parts = PurePosixPath(relative).parts[:-1]
        for length in range(1, len(parts) + 1):
            declared_directories.add(PurePosixPath(*parts[:length]).as_posix())
    before_files, before_directories = _tree_inventory(base, label=label)
    if before_files != declared_files or before_directories != declared_directories:
        raise AdmissionError(f"{label} has missing or extra entries")
    for entry in files:
        relative = PurePosixPath(str(entry["path"]))
        current = base.joinpath(*relative.parts)
        payload = _stable_bytes(
            current, maximum=16 * 1024 * 1024, label=f"{label} file"
        )
        if len(payload) != entry["bytes"] or _sha256(payload) != entry["sha256"]:
            raise AdmissionError(f"{label} file changed")
    after_files, after_directories = _tree_inventory(base, label=label)
    if (after_files, after_directories) != (before_files, before_directories):
        raise AdmissionError(f"{label} changed while being verified")


def _require_read_only_tree(
    base: Path, files: set[str], directories: set[str], *, label: str
) -> None:
    for relative in [None, *sorted(directories), *sorted(files)]:
        current = base if relative is None else base.joinpath(*PurePosixPath(relative).parts)
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError as error:
            raise AdmissionError(f"cannot inspect permissions for {label}") from error
        if metadata.st_mode & 0o222:
            raise AdmissionError(f"{label} must be read-only before paid execution")


def _verify_runtime_sources(
    root: Path, requirements: Mapping[str, Any]
) -> dict[str, str]:
    sources = requirements["runtime_sources"]
    if not isinstance(sources, Mapping) or set(sources) != {
        "v11-source",
        "harness-source",
    }:
        raise AdmissionError("runtime sources must be the exact closed pair")
    result: dict[str, str] = {}
    formats = {"v11-source": "tar.gz", "harness-source": "tar"}
    for label in ("v11-source", "harness-source"):
        source = sources[label]
        if not isinstance(source, Mapping):
            raise AdmissionError(f"{label} requirements must be an object")
        _require_keys(
            source,
            expected={
                "archive_path",
                "archive_sha256",
                "archive_format",
                "extracted_root",
                "read_only_before_execution",
            },
            label=f"{label} requirements",
        )
        if (
            source["archive_format"] != formats[label]
            or source["read_only_before_execution"] is not True
        ):
            raise AdmissionError(f"{label} extraction policy changed")
        archive = _repository_path(root, source["archive_path"], label=f"{label} archive")
        archive_payload = _stable_bytes(
            archive, maximum=64 * 1024 * 1024, label=f"{label} archive"
        )
        archive_sha = _sha256(archive_payload)
        if archive_sha != _sha(source["archive_sha256"], label=f"{label} archive sha256"):
            raise AdmissionError(f"{label} archive changed")
        mode = "r:gz" if source["archive_format"] == "tar.gz" else "r:"
        archive_files: dict[str, tuple[int, str]] = {}
        archive_directories: set[str] = set()
        names: set[str] = set()
        total_bytes = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(archive_payload), mode=mode) as opened:
                members = opened.getmembers()
                if len(members) > 1000:
                    raise AdmissionError(f"{label} archive has too many members")
                for member in members:
                    name = member.name
                    if not isinstance(name, str) or not name or "\\" in name:
                        raise AdmissionError(f"{label} archive member path is invalid")
                    pure = PurePosixPath(name)
                    if (
                        pure.is_absolute()
                        or ".." in pure.parts
                        or "." in pure.parts
                        or pure.as_posix() != name
                        or name in names
                    ):
                        raise AdmissionError(f"{label} archive member path is unsafe")
                    names.add(name)
                    if member.isdir():
                        archive_directories.add(name)
                        continue
                    if not member.isfile() or member.issparse():
                        raise AdmissionError(f"{label} archive contains a link or special entry")
                    if member.size < 0 or member.size > 16 * 1024 * 1024:
                        raise AdmissionError(f"{label} archive member exceeds its size limit")
                    total_bytes += member.size
                    if total_bytes > 64 * 1024 * 1024:
                        raise AdmissionError(f"{label} archive expands beyond its byte limit")
                    extracted = opened.extractfile(member)
                    if extracted is None:
                        raise AdmissionError(f"cannot read {label} archive member")
                    payload = extracted.read(member.size + 1)
                    if len(payload) != member.size:
                        raise AdmissionError(f"{label} archive member size is inconsistent")
                    archive_files[name] = (member.size, _sha256(payload))
        except (tarfile.TarError, OSError) as error:
            raise AdmissionError(f"cannot safely parse {label} archive") from error
        for name in archive_files:
            parts = PurePosixPath(name).parts[:-1]
            for length in range(1, len(parts) + 1):
                archive_directories.add(PurePosixPath(*parts[:length]).as_posix())

        extracted_root = _repository_path(
            root, source["extracted_root"], label=f"{label} extracted root"
        )
        extracted_files, extracted_directories = _tree_inventory(
            extracted_root, label=f"{label} extracted tree"
        )
        if extracted_files != set(archive_files) or extracted_directories != archive_directories:
            raise AdmissionError(f"{label} extracted tree differs from its archive inventory")
        _require_read_only_tree(
            extracted_root,
            extracted_files,
            extracted_directories,
            label=f"{label} extracted tree",
        )
        for relative, (expected_bytes, expected_sha) in archive_files.items():
            payload = _stable_bytes(
                extracted_root.joinpath(*PurePosixPath(relative).parts),
                maximum=16 * 1024 * 1024,
                label=f"{label} extracted file",
            )
            if len(payload) != expected_bytes or _sha256(payload) != expected_sha:
                raise AdmissionError(f"{label} extracted file differs from its archive")
        final_files, final_directories = _tree_inventory(
            extracted_root, label=f"{label} extracted tree"
        )
        if (final_files, final_directories) != (extracted_files, extracted_directories):
            raise AdmissionError(f"{label} extracted tree changed during verification")
        result[label] = archive_sha
    return result


def _verify_vendor_environment(
    root: Path, requirements: Mapping[str, Any]
) -> dict[str, Any]:
    policy = requirements["vendor_environment"]
    if not isinstance(policy, Mapping):
        raise AdmissionError("vendor environment requirements must be an object")
    _require_keys(
        policy,
        expected={
            "python_executable_path",
            "python_executable_symlink_target",
            "python_executable_resolved_path",
            "python_executable_sha256",
            "python_version",
            "python_cache_tag",
            "python_prefix",
            "python_base_prefix",
            "pyvenv_config_path",
            "pyvenv_config_sha256",
            "stdlib_path",
            "stdlib_file_count",
            "stdlib_directory_count",
            "stdlib_total_bytes",
            "stdlib_inventory_sha256",
            "source_site_packages_path",
            "execution_site_packages_path",
            "site_packages_file_count",
            "site_packages_directory_count",
            "site_packages_total_bytes",
            "site_packages_inventory_sha256",
            "read_only_before_execution",
        },
        label="vendor environment requirements",
    )
    executable_relative = policy["python_executable_path"]
    if not isinstance(executable_relative, str) or not executable_relative:
        raise AdmissionError("vendor Python path is invalid")
    pure_executable = PurePosixPath(executable_relative)
    if (
        "\\" in executable_relative
        or pure_executable.is_absolute()
        or ".." in pure_executable.parts
        or "." in pure_executable.parts
    ):
        raise AdmissionError("vendor Python path escapes the repository")
    executable = root.joinpath(*pure_executable.parts)
    if not executable.is_symlink():
        raise AdmissionError("vendor Python executable must be the exact sealed symlink")
    try:
        symlink_target = os.readlink(executable)
    except OSError as error:
        raise AdmissionError("vendor Python symlink cannot be inspected") from error
    if symlink_target != policy["python_executable_symlink_target"]:
        raise AdmissionError("vendor Python symlink target changed")
    try:
        resolved_executable = executable.resolve(strict=True)
        metadata = resolved_executable.stat()
    except OSError as error:
        raise AdmissionError("vendor Python executable is missing") from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved_executable, os.X_OK):
        raise AdmissionError("vendor Python executable is not executable")
    if str(resolved_executable) != policy["python_executable_resolved_path"]:
        raise AdmissionError("vendor Python resolved executable path changed")
    if _sha256_file(
        resolved_executable, label="vendor Python executable"
    ) != _sha(policy["python_executable_sha256"], label="vendor Python executable SHA"):
        raise AdmissionError("vendor Python executable bytes changed")

    pyvenv = _repository_path(
        root, policy["pyvenv_config_path"], label="vendor pyvenv configuration"
    )
    if _sha256_file(pyvenv, label="vendor pyvenv configuration") != _sha(
        policy["pyvenv_config_sha256"], label="vendor pyvenv configuration SHA"
    ):
        raise AdmissionError("vendor pyvenv configuration changed")
    probe = subprocess.run(
        [
            str(executable),
            "-I",
            "-S",
            "-B",
            "-c",
            (
                "import json,sys,sysconfig;"
                "print(json.dumps({'version': '.'.join(map(str, sys.version_info[:3])), "
                "'cache_tag': sys.implementation.cache_tag, "
                "'executable': sys.executable, 'prefix': sys.prefix, "
                "'base_prefix': sys.base_prefix, "
                "'stdlib': sysconfig.get_path('stdlib')}, sort_keys=True))"
            ),
        ],
        cwd=root,
        env={"LANG": "C", "LC_ALL": "C"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=20,
    )
    if probe.returncode != 0:
        raise AdmissionError("vendor Python isolated version probe failed")
    version_document = _parse_json_payload(probe.stdout, label="vendor Python version")
    if version_document != {
        "version": policy["python_version"],
        "cache_tag": policy["python_cache_tag"],
        "executable": str(executable),
        "prefix": policy["python_prefix"],
        "base_prefix": policy["python_base_prefix"],
        "stdlib": policy["stdlib_path"],
    }:
        raise AdmissionError("vendor Python runtime provenance changed")

    if policy["read_only_before_execution"] is not True:
        raise AdmissionError("vendor site-packages read-only policy changed")

    def inventory(
        relative_path: Any,
        *,
        label: str,
        require_read_only: bool,
        allow_absolute: bool = False,
    ) -> tuple[int, int, int, str]:
        if allow_absolute:
            if not isinstance(relative_path, str) or not Path(relative_path).is_absolute():
                raise AdmissionError(f"{label} path must be absolute")
            try:
                base = Path(relative_path).resolve(strict=True)
            except OSError as error:
                raise AdmissionError(f"{label} is missing") from error
        else:
            base = _repository_path(root, relative_path, label=label)
        files, directories = _tree_inventory(base, label=label)
        if require_read_only:
            _require_read_only_tree(base, files, directories, label=label)
        records: list[bytes] = [
            f"D {relative}\n".encode("utf-8") for relative in sorted(directories)
        ]
        total_bytes = 0
        for relative in sorted(files):
            payload = _stable_bytes(
                base.joinpath(*PurePosixPath(relative).parts),
                maximum=64 * 1024 * 1024,
                label=f"{label} file",
            )
            total_bytes += len(payload)
            records.append(
                f"F {_sha256(payload)} {len(payload)} {relative}\n".encode("utf-8")
            )
        digest = _sha256(b"".join(records))
        final_files, final_directories = _tree_inventory(base, label=label)
        if (final_files, final_directories) != (files, directories):
            raise AdmissionError(f"{label} changed during verification")
        return len(files), len(directories), total_bytes, digest

    expected_inventory = (
        policy["site_packages_file_count"],
        policy["site_packages_directory_count"],
        policy["site_packages_total_bytes"],
        policy["site_packages_inventory_sha256"],
    )
    source_inventory = inventory(
        policy["source_site_packages_path"],
        label="vendor source site-packages",
        require_read_only=False,
    )
    execution_inventory = inventory(
        policy["execution_site_packages_path"],
        label="attempt-local harness site-packages",
        require_read_only=True,
    )
    if source_inventory != expected_inventory or execution_inventory != expected_inventory:
        raise AdmissionError("vendor site-packages differs from its sealed inventory")
    stdlib_path = policy["stdlib_path"]
    if not isinstance(stdlib_path, str) or not Path(stdlib_path).is_absolute():
        raise AdmissionError("vendor Python stdlib path must be absolute")
    try:
        stdlib = Path(stdlib_path).resolve(strict=True)
    except OSError as error:
        raise AdmissionError("vendor Python stdlib is missing") from error
    if str(stdlib) != stdlib_path:
        raise AdmissionError("vendor Python stdlib path is not canonical")
    stdlib_inventory = inventory(
        str(stdlib),
        label="vendor Python stdlib",
        require_read_only=False,
        allow_absolute=True,
    )
    expected_stdlib_inventory = (
        policy["stdlib_file_count"],
        policy["stdlib_directory_count"],
        policy["stdlib_total_bytes"],
        policy["stdlib_inventory_sha256"],
    )
    if stdlib_inventory != expected_stdlib_inventory:
        raise AdmissionError("vendor Python stdlib differs from its sealed inventory")
    inventory_sha = execution_inventory[3]
    return {
        "python_version": policy["python_version"],
        "python_cache_tag": policy["python_cache_tag"],
        "python_executable_sha256": policy["python_executable_sha256"],
        "pyvenv_config_sha256": policy["pyvenv_config_sha256"],
        "stdlib_inventory_sha256": policy["stdlib_inventory_sha256"],
        "site_packages_inventory_sha256": inventory_sha,
    }


def _recorded_path_ends_with(value: Any, relative: str, *, label: str) -> None:
    if not isinstance(value, str) or "\\" in value:
        raise AdmissionError(f"{label} is invalid")
    recorded = PurePosixPath(value)
    expected = PurePosixPath(relative)
    if (
        not recorded.is_absolute()
        or ".." in recorded.parts
        or tuple(recorded.parts[-len(expected.parts) :]) != expected.parts
    ):
        raise AdmissionError(f"{label} does not identify the exact staged source")


def _verify_copy_manifest(
    root: Path,
    variant: Mapping[str, Any],
    *,
    verify_evaluated_files: bool,
) -> tuple[str, str, dict[str, Any]]:
    staged_path = _repository_path(
        root, variant["staged_copy_manifest_path"], label="staged copy manifest"
    )
    staged, staged_payload = _load_json(staged_path, label="staged copy manifest")
    staged_sha = _sha256(staged_payload)
    if staged_sha != variant["staged_copy_manifest_sha256"]:
        raise AdmissionError("staged copy manifest changed from its sealed hash")
    staged_files = _copy_manifest_files(staged, label="staged copy manifest")
    if (
        staged["project_name"] != variant["project_name"]
        or staged["expected_questions"] != 42
        or staged["prediction_file_count"] != 42
        or staged["question_ids_sha256"] != variant["question_ids_sha256"]
    ):
        raise AdmissionError("staged copy manifest scope or project changed")
    _recorded_path_ends_with(
        staged["evaluated_directory"],
        variant["staged_prediction_directory"],
        label="staged manifest evaluated directory",
    )
    staged_base = _repository_path(
        root,
        variant["staged_prediction_directory"],
        label="sealed staged prediction directory",
    )
    _verify_manifest_tree(staged_base, staged_files, label="staged prediction tree")

    working_path = _repository_path(
        root, variant["working_copy_manifest_path"], label="working-copy manifest"
    )
    working, working_payload = _load_json(working_path, label="working-copy manifest")
    working_files = _copy_manifest_files(working, label="working-copy manifest")
    expected_evaluated = str(
        _repository_path(
            root,
            f"{variant['run_root']}/evaluation/official-harness/"
            f"predicted_{variant['project_name']}",
            label="working prediction directory",
        )
    )
    if (
        working["project_name"] != variant["project_name"]
        or working["expected_questions"] != 42
        or working["prediction_file_count"] != 42
        or working["question_ids_sha256"] != variant["question_ids_sha256"]
    ):
        raise AdmissionError("working-copy manifest scope or project changed")
    if working["frozen_directory"] != str(staged_base):
        raise AdmissionError("working-copy frozen source path mismatch")
    if working["evaluated_directory"] != expected_evaluated:
        raise AdmissionError("working-copy evaluated path mismatch")
    if working_files != staged_files:
        raise AdmissionError("working-copy files do not exactly match the sealed staged manifest")
    if verify_evaluated_files:
        _verify_manifest_tree(
            Path(expected_evaluated), working_files, label="working prediction tree"
        )
    return _sha256(working_payload), staged_sha, working


def _current_campaign_audit(
    root: Path, requirements: Mapping[str, Any]
) -> tuple[dict[str, Any], bytes]:
    """Return the current frozen-auditor report through an isolated bootstrap."""

    campaign = requirements["campaign"]
    if not isinstance(campaign, Mapping):
        raise AdmissionError("campaign requirements must be an object")
    _require_keys(
        campaign,
        expected={
            "declaration_path",
            "declaration_sha256",
            "frozen_runtime_source",
            "budget_auditor_path",
            "budget_auditor_sha256",
            "budget_auditor_history_path",
            "budget_auditor_history_sha256",
            "baseline_observed_usd",
            "provider_cap_usd",
            "governance_ceiling_eur",
        },
        label="campaign requirements",
    )
    declaration = _repository_path(
        root, campaign["declaration_path"], label="campaign declaration"
    )
    if _sha256_file(declaration, label="campaign declaration") != _sha(
        campaign["declaration_sha256"], label="campaign declaration_sha256"
    ):
        raise AdmissionError("campaign declaration changed")
    auditor = _repository_path(root, campaign["budget_auditor_path"], label="budget auditor")
    if _sha256_file(auditor, label="budget auditor") != _sha(
        campaign["budget_auditor_sha256"], label="budget auditor_sha256"
    ):
        raise AdmissionError("frozen budget auditor changed")
    history = _repository_path(
        root,
        campaign["budget_auditor_history_path"],
        label="budget auditor history support",
    )
    if _sha256_file(history, label="budget auditor history support") != _sha(
        campaign["budget_auditor_history_sha256"],
        label="budget auditor history sha256",
    ):
        raise AdmissionError("frozen budget auditor history support changed")
    runtime = _repository_path(
        root, campaign["frozen_runtime_source"], label="frozen runtime source"
    )
    try:
        auditor_relative = auditor.relative_to(runtime).as_posix()
        history_relative = history.relative_to(runtime).as_posix()
    except ValueError as error:
        raise AdmissionError("frozen auditor files escape the frozen runtime source") from error
    if auditor_relative != "narratordb/benchmarks/budget_audit.py":
        raise AdmissionError("budget auditor is not at its exact frozen runtime location")
    if history_relative != "narratordb/benchmarks/history.py":
        raise AdmissionError("budget auditor history is not at its exact frozen runtime location")
    environment = {
        "LANG": "C",
        "LC_ALL": "C",
    }
    bootstrap = """
import importlib.util
import sys
import types

auditor, history, declaration, cap = sys.argv[1:]
narratordb = types.ModuleType("narratordb")
narratordb.__package__ = "narratordb"
narratordb.__path__ = []
benchmarks = types.ModuleType("narratordb.benchmarks")
benchmarks.__package__ = "narratordb.benchmarks"
benchmarks.__path__ = []
sys.modules["narratordb"] = narratordb
sys.modules["narratordb.benchmarks"] = benchmarks
history_spec = importlib.util.spec_from_file_location(
    "narratordb.benchmarks.history", history
)
history_module = importlib.util.module_from_spec(history_spec)
sys.modules[history_spec.name] = history_module
history_spec.loader.exec_module(history_module)
spec = importlib.util.spec_from_file_location(
    "narratordb.benchmarks.budget_audit", auditor
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
raise SystemExit(module.main([
    "--declaration", declaration, "--provider-cap-usd", cap
]))
"""
    command = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        "-c",
        bootstrap,
        str(auditor),
        str(history),
        str(declaration),
        str(campaign["provider_cap_usd"]),
    ]
    result = subprocess.run(
        command,
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[-500:]
        raise AdmissionError(f"fresh campaign audit failed: {stderr}")
    document = _parse_json_payload(result.stdout, label="fresh campaign audit")
    if _canonical_json(document) != result.stdout:
        raise AdmissionError("fresh campaign audit is not canonical JSON")
    if document.get("schema") != CAMPAIGN_AUDIT_SCHEMA:
        raise AdmissionError("unsupported campaign audit schema")
    if document.get("complete") is not True:
        raise AdmissionError("campaign audit is incomplete")
    if document.get("unknown_cost_attempts") != 0:
        raise AdmissionError("campaign audit contains unknown costs")
    if document.get("invalid_completion_identities") != 0:
        raise AdmissionError("campaign audit contains invalid identities")
    provider_limit = document.get("provider_limit")
    if not isinstance(provider_limit, Mapping):
        raise AdmissionError("campaign provider limit is missing")
    if (
        provider_limit.get("enforced_cap_usd") != campaign["provider_cap_usd"]
        or provider_limit.get("within_cap") is not True
    ):
        raise AdmissionError("campaign USD cap is not proven")
    governance = document.get("governance_limit")
    if not isinstance(governance, Mapping) or governance.get("ceiling_eur") != campaign[
        "governance_ceiling_eur"
    ]:
        raise AdmissionError("campaign EUR ceiling changed")
    return document, result.stdout


def _recompute_campaign_audit(
    root: Path, requirements: Mapping[str, Any], audit_path: Path
) -> tuple[dict[str, Any], str]:
    document, current = _current_campaign_audit(root, requirements)
    stored = _stable_bytes(audit_path, maximum=MAX_JSON_BYTES, label="campaign audit")
    if stored != current:
        raise AdmissionError("stored campaign audit is not the current byte-identical audit")
    return document, _sha256(stored)


def _recompute_historical_campaign_audit(
    root: Path,
    requirements: Mapping[str, Any],
    audit_path: Path,
    ledger_payloads: Mapping[str, bytes],
) -> tuple[dict[str, Any], str]:
    """Re-run the frozen auditor against exact phase-time ledger snapshots."""

    campaign = requirements["campaign"]
    declaration_path = _repository_path(
        root, campaign["declaration_path"], label="campaign declaration"
    )
    declaration, declaration_payload = _load_json(
        declaration_path, label="campaign declaration"
    )
    active = declaration.get("active_usage_ledgers")
    prior = declaration.get("prior_immutable_costs")
    if not isinstance(active, list) or not isinstance(prior, list):
        raise AdmissionError("campaign declaration source lists are invalid")

    expected_ledgers = {
        _repository_path(root, relative, label="historical campaign ledger"): payload
        for relative, payload in ledger_payloads.items()
    }
    declared_ledgers: set[Path] = set()
    for index, source in enumerate(active):
        if not isinstance(source, Mapping) or not isinstance(source.get("path"), str):
            raise AdmissionError(f"campaign active ledger {index} is invalid")
        try:
            resolved = (declaration_path.parent / source["path"]).resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise AdmissionError("campaign active ledger escapes the repository") from error
        declared_ledgers.add(resolved)
    if declared_ledgers != set(expected_ledgers):
        raise AdmissionError("historical ledger snapshots do not match the campaign declaration")

    with tempfile.TemporaryDirectory(prefix="narratordb-historical-audit-") as temporary:
        mirror = Path(temporary).resolve()

        def copy_repository_file(relative: Any, *, label: str) -> None:
            source = _repository_path(root, relative, label=label)
            target = mirror.joinpath(*PurePosixPath(str(relative)).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(
                _stable_bytes(source, maximum=64 * 1024 * 1024, label=label)
            )

        copy_repository_file(campaign["budget_auditor_path"], label="budget auditor")
        copy_repository_file(
            campaign["budget_auditor_history_path"], label="budget auditor history"
        )
        declaration_target = mirror.joinpath(
            *PurePosixPath(campaign["declaration_path"]).parts
        )
        declaration_target.parent.mkdir(parents=True, exist_ok=True)
        declaration_target.write_bytes(declaration_payload)

        for index, source in enumerate(prior):
            if not isinstance(source, Mapping) or not isinstance(
                source.get("record_path"), str
            ):
                raise AdmissionError(f"campaign prior source {index} is invalid")
            original = (declaration_path.parent / source["record_path"]).resolve(
                strict=True
            )
            try:
                relative = original.relative_to(root)
            except ValueError as error:
                raise AdmissionError("campaign prior source escapes the repository") from error
            target = mirror / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(
                _stable_bytes(
                    original,
                    maximum=64 * 1024 * 1024,
                    label="campaign prior source",
                )
            )

        for original, payload in expected_ledgers.items():
            relative = original.relative_to(root)
            target = mirror / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

        document, recomputed = _current_campaign_audit(mirror, requirements)

    stored = _stable_bytes(
        audit_path, maximum=MAX_JSON_BYTES, label="historical campaign audit"
    )
    if stored != recomputed:
        raise AdmissionError(
            "stored historical campaign audit is not byte-identical to phase reconstruction"
        )
    return document, _sha256(stored)


def _declared_campaign_audit_output(
    root: Path, requirements: Mapping[str, Any], requested: Path
) -> Path:
    """Resolve only one of the three precommitted campaign-audit destinations."""

    relative_paths = [
        _phase_config(requirements, "before-v7")["campaign_audit_path"],
        _phase_config(requirements, "before-v13")["campaign_audit_path"],
        _finalization_config(requirements)["campaign_audit_path"],
    ]
    if any(not isinstance(relative, str) for relative in relative_paths):
        raise AdmissionError("declared campaign audit path must be a string")
    declared = {
        _repository_path(
            root,
            relative,
            label="declared campaign audit output",
            must_exist=False,
        )
        for relative in relative_paths
    }
    if len(declared) != 3:
        raise AdmissionError("campaign audit outputs must be three distinct paths")

    if requested.is_absolute():
        try:
            relative_requested = requested.relative_to(root)
        except ValueError as error:
            raise AdmissionError("campaign audit output escapes the repository") from error
    else:
        relative_requested = requested
    relative_text = relative_requested.as_posix()
    pure = PurePosixPath(relative_text)
    if (
        not relative_text
        or "\\" in relative_text
        or pure.is_absolute()
        or ".." in pure.parts
        or "." in pure.parts
    ):
        raise AdmissionError("campaign audit output must be a safe repository path")
    candidate = _repository_path(
        root,
        relative_text,
        label="campaign audit output",
        must_exist=False,
    )
    if candidate not in declared:
        raise AdmissionError("campaign audit output is not one of the three declared paths")
    return candidate


def _ledger_state(
    root: Path,
    variant: Mapping[str, Any],
    *,
    require_initial: bool,
) -> dict[str, Any]:
    path = _repository_path(root, variant["ledger_path"], label="campaign ledger")
    payload = _stable_bytes(path, maximum=32 * 1024 * 1024, label="campaign ledger")
    return _ledger_state_from_payload(variant, payload, require_initial=require_initial)


def _ledger_state_from_payload(
    variant: Mapping[str, Any], payload: bytes, *, require_initial: bool
) -> dict[str, Any]:
    """Describe a current or preserved historical ledger byte snapshot."""

    if payload and not payload.endswith(b"\n"):
        raise AdmissionError("campaign ledger has an incomplete final line")
    events = sum(1 for line in payload.splitlines() if line.strip())
    digest = _sha256(payload)
    if require_initial and (
        digest != variant["initial_ledger_sha256"] or events != 0
    ):
        raise AdmissionError("campaign ledger is not in its exact initial blank state")
    return {
        "path": variant["ledger_path"],
        "sha256": digest,
        "bytes": len(payload),
        "events": events,
    }


def _verify_ledger_identity_audit(
    root: Path,
    requirements: Mapping[str, Any],
    state: Mapping[str, Any],
    audit_relative: str,
    *,
    label: str,
) -> tuple[str, Decimal]:
    audit_path = _repository_path(root, audit_relative, label=f"{label} identity audit")
    payload = _require_immutable_file(
        audit_path, label=f"{label} identity audit", maximum=MAX_JSON_BYTES
    )
    document = _parse_json_payload(payload, label=f"{label} identity audit")
    if document.get("schema") != LEDGER_AUDIT_SCHEMA or document.get("complete") is not True:
        raise AdmissionError(f"{label} identity audit did not pass")
    if document.get("ledger_sha256") != state["sha256"]:
        raise AdmissionError(f"{label} identity audit hash mismatch")
    revision = requirements["revision"]
    verifier = _repository_path(
        root, revision["ledger_verifier_path"], label="ledger identity verifier"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(verifier),
            "--ledger",
            str(_repository_path(root, state["path"], label=f"{label} ledger")),
        ],
        cwd=root,
        env={"LANG": "C", "LC_ALL": "C"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=20,
    )
    if result.returncode != 0 or result.stdout != payload:
        raise AdmissionError(f"{label} identity audit is not byte-identical to recomputation")
    cost = Decimal("0")
    ledger_payload = _stable_bytes(
        _repository_path(root, state["path"], label=f"{label} ledger"),
        maximum=32 * 1024 * 1024,
        label="V7 ledger",
    )
    for raw_line in ledger_payload.splitlines():
        if not raw_line.strip():
            continue
        event = json.loads(raw_line, parse_float=Decimal, object_pairs_hook=_unique_object)
        value = event.get("cost_usd")
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise AdmissionError("ledger cost is not an exact finite number")
        parsed = Decimal(value)
        if not parsed.is_finite() or parsed < 0:
            raise AdmissionError("ledger cost is outside the allowed range")
        if event.get("unknown_cost") is True:
            raise AdmissionError(f"{label} ledger contains an unknown cost")
        cost += parsed
    return _sha256(payload), cost


def _verify_prior_ledger_identity(
    root: Path,
    requirements: Mapping[str, Any],
    phase: Mapping[str, Any],
    v7_state: Mapping[str, Any],
) -> tuple[str | None, Decimal]:
    audit_relative = phase["prior_ledger_identity_audit_path"]
    if audit_relative is None:
        if v7_state["events"] != 0:
            raise AdmissionError("V7 ledger must be blank before the first variant")
        return None, Decimal("0")
    if not isinstance(audit_relative, str):
        raise AdmissionError("prior ledger identity audit path must be a string")
    return _verify_ledger_identity_audit(
        root, requirements, v7_state, audit_relative, label="V7"
    )


def _verify_completed_arm_status(
    root: Path, variant: Mapping[str, Any], *, display_label: str
) -> str:
    status_path = _repository_path(
        root,
        f"{variant['run_root']}/evaluation/attempt-status.json",
        label=f"{display_label} attempt status",
    )
    payload = _require_immutable_file(
        status_path, label=f"{display_label} attempt status", maximum=MAX_JSON_BYTES
    )
    status = _parse_json_payload(payload, label=f"{display_label} attempt status")
    expected = {
        "evaluator_status": "0",
        "exit_status": 0,
        "final_status": "completed",
        "phase": "before-v7" if variant["label"] == "v7-control" else "before-v13",
        "project": variant["project_name"],
        "schema_version": "narratordb.v13-paid-variant-attempt-status.v2",
    }
    if status != expected:
        raise AdmissionError(f"{display_label} attempt is not exactly completed")
    return _sha256(payload)


def _verify_proxy_stop_evidence(
    root: Path,
    requirements: Mapping[str, Any],
    variant: Mapping[str, Any],
    *,
    display_label: str,
    gate_cost: Decimal,
    gate_usage: Mapping[str, Any],
) -> tuple[str, str]:
    """Bind the stopped proxy process and its initial content-free health state."""

    proxy_path = _repository_path(
        root,
        f"{variant['run_root']}/evaluation/proxy.log",
        label=f"{display_label} proxy log",
    )
    proxy_payload = _require_immutable_file(
        proxy_path, label=f"{display_label} proxy log", maximum=MAX_JSON_BYTES
    )
    lines = proxy_payload.splitlines()
    if len(lines) != 2 or not all(lines):
        raise AdmissionError(f"{display_label} proxy log is not exactly start/stop evidence")
    startup = _parse_arm_gate_payload(lines[0], label=f"{display_label} proxy start")
    stopped = _parse_arm_gate_payload(lines[1], label=f"{display_label} proxy stop")
    _require_keys(
        startup,
        expected={
            "ok",
            "url",
            "provider_only",
            "provider_allow",
            "reasoning_effort",
            "usage_log",
            "max_cost_usd",
            "request_reservation_usd",
            "budget_safety_reserve_usd",
            "max_response_bytes",
        },
        label=f"{display_label} proxy start",
    )
    _require_keys(
        stopped,
        expected={"stopped", "usage"},
        label=f"{display_label} proxy stop",
    )

    expected_providers = requirements["arm_gate_policy"]["allowed_providers"]
    usage_log = str(
        _repository_path(
            root, variant["ledger_path"], label=f"{display_label} ledger"
        )
    )
    startup_numbers = (
        startup["max_cost_usd"],
        startup["request_reservation_usd"],
        startup["budget_safety_reserve_usd"],
    )
    if (
        any(
            isinstance(value, bool) or not isinstance(value, (int, Decimal))
            for value in startup_numbers
        )
        or
        startup["ok"] is not True
        or startup["url"] != "http://127.0.0.1:8890/v1"
        or startup["provider_only"] is not None
        or startup["provider_allow"] != expected_providers
        or startup["reasoning_effort"] != "high"
        or startup["usage_log"] != usage_log
        or isinstance(startup["max_response_bytes"], bool)
        or startup["max_response_bytes"] != 4 * 1024 * 1024
        or Decimal(startup["max_cost_usd"]) != Decimal("2.5")
        or Decimal(startup["request_reservation_usd"]) != Decimal("0.05")
        or Decimal(startup["budget_safety_reserve_usd"]) != Decimal("0.01")
        or stopped["stopped"] is not True
        or not isinstance(stopped["usage"], Mapping)
    ):
        raise AdmissionError(f"{display_label} proxy start/stop evidence is invalid")

    summary = stopped["usage"]
    _require_keys(
        summary,
        expected={
            "calls",
            "errors",
            "malformed_responses",
            "cost_usd",
            "prompt_tokens",
            "cached_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "unknown_cost_attempts",
            "max_cost_usd",
            "request_reservation_usd",
            "safety_reserve_usd",
            "reserved_cost_usd",
            "scope",
            "enforcement",
        },
        label=f"{display_label} proxy stop usage",
    )
    numeric_summary = {
        key: summary[key]
        for key in (
            "cost_usd",
            "max_cost_usd",
            "request_reservation_usd",
            "safety_reserve_usd",
            "reserved_cost_usd",
        )
    }
    if any(isinstance(value, bool) or not isinstance(value, (int, Decimal)) for value in numeric_summary.values()):
        raise AdmissionError(f"{display_label} proxy stop numeric evidence is invalid")
    counter_pairs = {
        "calls": "completion_calls",
        "errors": "upstream_errors",
        "malformed_responses": "malformed_http_200_responses",
        "prompt_tokens": "prompt_tokens",
        "cached_tokens": "cached_tokens",
        "completion_tokens": "completion_tokens",
        "reasoning_tokens": "reasoning_tokens",
        "unknown_cost_attempts": "unknown_cost_attempts",
    }
    if any(
        isinstance(summary[source], bool)
        or not isinstance(summary[source], int)
        or isinstance(gate_usage[target], bool)
        or not isinstance(gate_usage[target], int)
        or summary[source] != gate_usage[target]
        for source, target in counter_pairs.items()
    ) or gate_usage.get("events") != summary["calls"] + summary["errors"]:
        raise AdmissionError(
            f"{display_label} proxy stop counters do not match the sealed ledger gate"
        )
    if (
        Decimal(summary["cost_usd"]) != gate_cost
        or Decimal(summary["max_cost_usd"]) != Decimal("2.5")
        or Decimal(summary["request_reservation_usd"]) != Decimal("0.05")
        or Decimal(summary["safety_reserve_usd"]) != Decimal("0.01")
        or Decimal(summary["reserved_cost_usd"]) != 0
        or summary["unknown_cost_attempts"] != 0
        or summary["scope"] != "process"
        or summary["enforcement"] != "soft_fuse"
    ):
        raise AdmissionError(f"{display_label} proxy did not stop in a safe budget state")

    health_path = _repository_path(
        root,
        f"{variant['run_root']}/evaluation/proxy-health-before.json",
        label=f"{display_label} proxy health",
    )
    health_payload = _require_immutable_file(
        health_path, label=f"{display_label} proxy health", maximum=MAX_JSON_BYTES
    )
    health = _parse_arm_gate_payload(
        health_payload, label=f"{display_label} proxy health"
    )
    _require_keys(
        health,
        expected={"ok", "provider_only", "provider_allow", "reasoning_effort", "usage"},
        label=f"{display_label} proxy health",
    )
    initial = health.get("usage")
    if not isinstance(initial, Mapping):
        raise AdmissionError(f"{display_label} proxy health usage is invalid")
    _require_keys(
        initial,
        expected=set(summary),
        label=f"{display_label} proxy health usage",
    )
    initial_cost = initial.get("cost_usd")
    initial_reserved = initial.get("reserved_cost_usd")
    if (
        isinstance(initial_cost, bool)
        or not isinstance(initial_cost, (int, Decimal))
        or isinstance(initial_reserved, bool)
        or not isinstance(initial_reserved, (int, Decimal))
        or
        health["ok"] is not True
        or health["provider_only"] is not None
        or health["provider_allow"] != expected_providers
        or health["reasoning_effort"] != "high"
        or initial["calls"] != 0
        or initial["errors"] != 0
        or Decimal(initial_cost) != 0
        or initial["unknown_cost_attempts"] != 0
        or Decimal(initial_reserved) != 0
    ):
        raise AdmissionError(f"{display_label} initial proxy health is invalid")
    return _sha256(proxy_payload), _sha256(health_payload)


def _verify_arm_gate_artifact(
    root: Path,
    requirements: Mapping[str, Any],
    variants: Mapping[str, Mapping[str, Any]],
    *,
    variant_label: str,
    gate_relative: str,
) -> tuple[str, str, str, str]:
    """Recompute one immutable score-blind arm gate from its exact evidence."""

    if variant_label not in {"v7-control", "v13-first"}:
        raise AdmissionError("arm gate variant is invalid")
    if not isinstance(gate_relative, str) or not gate_relative:
        raise AdmissionError("arm gate path must be a nonempty string")
    display_label = "V7" if variant_label == "v7-control" else "V13"
    variant = variants[variant_label]
    expected_gate_relative = f"{variant['run_root']}/evaluation/arm-gate.json"
    if gate_relative != expected_gate_relative:
        raise AdmissionError(
            f"{display_label} arm gate path is not the exact derived variant path"
        )
    gate_path = _repository_path(root, gate_relative, label=f"{display_label} arm gate")
    payload = _require_immutable_file(
        gate_path, label=f"{display_label} arm gate", maximum=MAX_JSON_BYTES
    )
    document = _parse_arm_gate_payload(payload, label=f"{display_label} arm gate")
    _scan_arm_gate_for_score_content(document)
    _require_keys(
        document,
        expected={
            "schema_version",
            "authorized",
            "complete",
            "official_harness_score_complete",
            "expected_questions",
            "evaluated_questions",
            "frozen_questions",
            "scoped_question_subset",
            "cutoffs",
            "denominators",
            "validation",
            "validation_counts",
            "usage",
            "harness_log",
            "failures",
            "internal_evaluation_audit_sha256",
        },
        label=f"{display_label} arm gate",
    )

    policy = requirements["arm_gate_policy"]
    expected_cutoffs = [
        f"top_{value.strip()}" for value in policy["cutoffs"].split(",")
    ]
    expected_denominators = {
        cutoff: policy["expected_questions"] for cutoff in expected_cutoffs
    }
    validation_counts = document.get("validation_counts")
    validation = document.get("validation")
    usage = document.get("usage")
    harness_log = document.get("harness_log")
    if isinstance(validation_counts, Mapping):
        _require_keys(
            validation_counts,
            expected=set(ARM_GATE_VALIDATION_FIELDS),
            label=f"{display_label} arm gate validation counts",
        )
    if isinstance(validation, Mapping):
        _require_keys(
            validation,
            expected=set(ARM_GATE_VALIDATION_FIELDS),
            label=f"{display_label} arm gate validation",
        )
    if isinstance(usage, Mapping):
        _require_keys(
            usage,
            expected=set(ARM_GATE_USAGE_FIELDS),
            label=f"{display_label} arm gate usage",
        )
    if isinstance(harness_log, Mapping):
        _require_keys(
            harness_log,
            expected={
                "failed_attempt_counts",
                "timed_out_attempt_counts",
                "returned_none_responses",
                "attempt_five_failures",
            },
            label=f"{display_label} arm gate harness log",
        )
    raw_gate_cost = usage.get("cost_usd") if isinstance(usage, Mapping) else None
    if isinstance(raw_gate_cost, bool) or not isinstance(
        raw_gate_cost, (int, Decimal)
    ):
        gate_cost = None
    else:
        gate_cost = Decimal(raw_gate_cost)
    maximum_gate_cost = _decimal(
        policy["max_cost_usd"], label="arm gate maximum cost", positive=True
    )
    if (
        document.get("schema_version") != policy["schema_version"]
        or document.get("authorized") is not True
        or document.get("complete") is not True
        or document.get("official_harness_score_complete") is not True
        or document.get("expected_questions") != policy["expected_questions"]
        or document.get("evaluated_questions") != policy["expected_questions"]
        or document.get("frozen_questions") != policy["expected_questions"]
        or document.get("scoped_question_subset") is not True
        or document.get("cutoffs") != expected_cutoffs
        or document.get("denominators") != expected_denominators
        or not isinstance(document.get("internal_evaluation_audit_sha256"), str)
        or not SHA256_RE.fullmatch(document["internal_evaluation_audit_sha256"])
        or not isinstance(validation, Mapping)
        or any(value != [] for value in validation.values())
        or not isinstance(validation_counts, Mapping)
        or not validation_counts
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value != 0
            for value in validation_counts.values()
        )
        or not isinstance(usage, Mapping)
        or usage.get("publication_ready") is not True
        or usage.get("unknown_cost_attempts") != 0
        or usage.get("invalid_completion_identities") != 0
        or gate_cost is None
        or not gate_cost.is_finite()
        or gate_cost < 0
        or gate_cost > maximum_gate_cost
        or document.get("failures") != []
    ):
        raise AdmissionError(f"{display_label} score-blind arm gate did not authorize")

    request_model_counts = usage.get("request_model_counts")
    provider_maps = [
        usage.get("provider_counts"),
        usage.get("completion_provider_counts"),
        usage.get("error_provider_counts"),
    ]
    if (
        not isinstance(request_model_counts, Mapping)
        or not set(request_model_counts) <= set(policy["allowed_request_models"])
        or any(not isinstance(item, Mapping) for item in provider_maps)
        or any(
            not set(item) <= set(policy["allowed_providers"])
            for item in provider_maps
            if isinstance(item, Mapping)
        )
        or not isinstance(harness_log, Mapping)
    ):
        raise AdmissionError(f"{display_label} arm gate route evidence is invalid")

    _, _, working = _verify_copy_manifest(
        root, variant, verify_evaluated_files=False
    )
    _scored_tree_evidence(root, variant, working)
    _require_immutable_file(
        _repository_path(
            root, variant["ledger_path"], label=f"{display_label} ledger"
        ),
        label=f"{display_label} ledger",
        maximum=32 * 1024 * 1024,
    )
    _require_immutable_file(
        _repository_path(
            root,
            f"{variant['run_root']}/evaluation/evaluate.log",
            label=f"{display_label} evaluator log",
        ),
        label=f"{display_label} evaluator log",
        maximum=32 * 1024 * 1024,
    )
    status_sha = _verify_completed_arm_status(
        root, variant, display_label=display_label
    )
    proxy_sha, health_sha = _verify_proxy_stop_evidence(
        root,
        requirements,
        variant,
        display_label=display_label,
        gate_cost=gate_cost,
        gate_usage=usage,
    )

    revision = requirements["revision"]
    runner = _repository_path(
        root, revision["arm_gate_guard_path"], label="arm gate guard"
    )
    command = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        str(runner),
        "--evaluated-directory",
        str(
            _repository_path(
                root,
                f"{variant['run_root']}/evaluation/official-harness/"
                f"predicted_{variant['project_name']}",
                label=f"{display_label} evaluated directory",
            )
        ),
        "--frozen-directory",
        str(
            _repository_path(
                root,
                variant["staged_prediction_directory"],
                label=f"{display_label} frozen directory",
            )
        ),
        "--usage-log",
        str(
            _repository_path(
                root, variant["ledger_path"], label=f"{display_label} ledger"
            )
        ),
        "--evaluator-log",
        str(
            _repository_path(
                root,
                f"{variant['run_root']}/evaluation/evaluate.log",
                label=f"{display_label} evaluator log",
            )
        ),
        "--expected-questions",
        str(policy["expected_questions"]),
        "--cutoffs",
        policy["cutoffs"],
        "--question-id-file",
        str(
            _repository_path(
                root,
                variant["question_ids_path"],
                label=f"{display_label} question IDs",
            )
        ),
    ]
    for model in policy["allowed_request_models"]:
        command.extend(("--allowed-request-model", model))
    for provider in policy["allowed_providers"]:
        command.extend(("--allowed-provider", provider))
    command.extend(("--max-cost-usd", policy["max_cost_usd"]))
    result = subprocess.run(
        command,
        cwd=root,
        env={"LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    if result.returncode != 0 or result.stdout != payload:
        raise AdmissionError(
            f"{display_label} arm gate is not byte-identical to sealed recomputation"
        )
    return _sha256(payload), status_sha, proxy_sha, health_sha


def _verify_prior_arm_gate(
    root: Path,
    requirements: Mapping[str, Any],
    phase: Mapping[str, Any],
    variants: Mapping[str, Mapping[str, Any]],
) -> tuple[str | None, str | None, str | None, str | None]:
    gate_relative = phase["prior_arm_gate_path"]
    if gate_relative is None:
        if phase["variant"] != "v7-control":
            raise AdmissionError("only before-V7 may omit the prior arm gate")
        return None, None, None, None
    if phase["variant"] != "v13-first" or not isinstance(gate_relative, str):
        raise AdmissionError("before-V13 must declare the V7 arm gate")
    return _verify_arm_gate_artifact(
        root,
        requirements,
        variants,
        variant_label="v7-control",
        gate_relative=gate_relative,
    )


def _provider_telemetry(
    root: Path,
    requirements: Mapping[str, Any],
    path: Path,
    *,
    at: datetime,
    unspent: Decimal,
) -> tuple[dict[str, Any], str]:
    policy = requirements["provider"]
    if not isinstance(policy, Mapping):
        raise AdmissionError("provider policy must be an object")
    _require_keys(
        policy,
        expected={
            "endpoint",
            "currency",
            "provider_limit_usd",
            "historical_usage_floor_usd",
            "maximum_age_seconds",
            "arithmetic_tolerance_usd",
        },
        label="provider policy",
    )
    document, payload = _load_json(path, label="provider telemetry")
    _require_keys(
        document,
        expected={
            "schema_version",
            "observed_at_utc",
            "source_endpoint",
            "request_class",
            "http_status",
            "currency",
            "provider_limit_usd",
            "provider_usage_usd",
            "provider_remaining_usd",
            "capture_tool_sha256",
            "credential_recorded",
            "key_label_recorded",
            "account_identifier_recorded",
            "model_content_recorded",
        },
        label="provider telemetry",
    )
    if document["schema_version"] != PROVIDER_SCHEMA:
        raise AdmissionError("unsupported provider telemetry schema")
    if (
        document["source_endpoint"] != policy["endpoint"]
        or document["request_class"] != "authenticated content-free account telemetry"
        or document["http_status"] != 200
        or document["currency"] != policy["currency"]
    ):
        raise AdmissionError("provider telemetry provenance changed")
    for key in (
        "credential_recorded",
        "key_label_recorded",
        "account_identifier_recorded",
        "model_content_recorded",
    ):
        if document[key] is not False:
            raise AdmissionError("provider telemetry retained forbidden data")
    capture = _repository_path(
        root, requirements["revision"]["telemetry_capture_path"], label="telemetry capture tool"
    )
    if document["capture_tool_sha256"] != _sha256_file(
        capture, label="telemetry capture tool"
    ):
        raise AdmissionError("provider telemetry capture-tool binding mismatch")
    observed = _timestamp(document["observed_at_utc"], label="provider observed_at_utc")
    age = Decimal(str((at - observed).total_seconds()))
    maximum_age = Decimal(str(policy["maximum_age_seconds"]))
    if age < 0 or age > maximum_age:
        raise AdmissionError("provider telemetry is stale or future-dated")
    limit = _decimal(document["provider_limit_usd"], label="provider limit", positive=True)
    usage = _decimal(document["provider_usage_usd"], label="provider usage")
    remaining = _decimal(document["provider_remaining_usd"], label="provider remaining")
    expected_limit = _decimal(policy["provider_limit_usd"], label="required provider limit", positive=True)
    tolerance = _decimal(policy["arithmetic_tolerance_usd"], label="provider tolerance")
    if limit != expected_limit:
        raise AdmissionError("provider key limit is not the frozen USD 250 cap")
    if abs(limit - usage - remaining) > tolerance:
        raise AdmissionError("provider limit/usage/remaining arithmetic is inconsistent")
    projected = usage + unspent
    if remaining < unspent or projected > limit:
        raise AdmissionError("provider telemetry does not cover every unspent fuse")
    return {
        "observed_at_utc": document["observed_at_utc"],
        "age_seconds_at_authorization": _decimal_text(age),
        "limit_usd": _decimal_text(limit),
        "usage_usd": _decimal_text(usage),
        "remaining_usd": _decimal_text(remaining),
        "unspent_fuses_usd": _decimal_text(unspent),
        "projected_usage_usd": _decimal_text(projected),
        "within_cap": True,
    }, _sha256(payload)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _fx_observation(
    root: Path,
    requirements: Mapping[str, Any],
    metadata_path: Path,
    raw_path: Path,
    *,
    at: datetime,
) -> tuple[dict[str, Any], str, str, Decimal]:
    policy = requirements["fx"]
    if not isinstance(policy, Mapping):
        raise AdmissionError("FX policy must be an object")
    _require_keys(
        policy,
        expected={
            "source_url",
            "publisher",
            "maximum_capture_age_seconds",
            "maximum_reference_age_days",
            "buffer_basis_points",
            "governance_ceiling_eur",
            "rounding",
        },
        label="FX policy",
    )
    document, metadata_payload = _load_json(metadata_path, label="ECB FX metadata")
    _require_keys(
        document,
        expected={
            "schema_version",
            "publisher",
            "source_url",
            "http_status",
            "retrieved_at_utc",
            "raw_xml_path",
            "raw_xml_bytes",
            "raw_xml_sha256",
            "reference_date",
            "base_currency",
            "quote_currency",
            "usd_per_eur",
            "parser_sha256",
            "credential_recorded",
            "model_content_recorded",
        },
        label="ECB FX metadata",
    )
    if document["schema_version"] != FX_SCHEMA:
        raise AdmissionError("unsupported ECB FX metadata schema")
    if (
        document["publisher"] != policy["publisher"]
        or document["source_url"] != policy["source_url"]
        or document["http_status"] != 200
        or document["base_currency"] != "EUR"
        or document["quote_currency"] != "USD"
        or document["credential_recorded"] is not False
        or document["model_content_recorded"] is not False
    ):
        raise AdmissionError("ECB FX provenance or content policy changed")
    if document["raw_xml_path"] != str(raw_path.relative_to(root)):
        raise AdmissionError("ECB raw XML path binding mismatch")
    raw = _stable_bytes(raw_path, maximum=MAX_XML_BYTES, label="ECB daily XML")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise AdmissionError("ECB XML declarations/entities are forbidden")
    raw_sha = _sha256(raw)
    if document["raw_xml_bytes"] != len(raw) or document["raw_xml_sha256"] != raw_sha:
        raise AdmissionError("ECB raw XML byte/hash binding mismatch")
    if document["parser_sha256"] != _sha256_file(Path(__file__), label="admission verifier"):
        raise AdmissionError("ECB parser hash does not bind this verifier")
    retrieved = _timestamp(document["retrieved_at_utc"], label="ECB retrieved_at_utc")
    capture_age = Decimal(str((at - retrieved).total_seconds()))
    if capture_age < 0 or capture_age > Decimal(str(policy["maximum_capture_age_seconds"])):
        raise AdmissionError("ECB evidence is stale or future-dated")
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError as error:
        raise AdmissionError(f"ECB XML is malformed: {error}") from error
    dated = [
        element
        for element in tree.iter()
        if _local_name(element.tag) == "Cube" and "time" in element.attrib
    ]
    if len(dated) != 1 or set(dated[0].attrib) != {"time"}:
        raise AdmissionError("ECB XML must contain exactly one unambiguous dated Cube")
    usd_quotes = [
        child
        for child in list(dated[0])
        if _local_name(child.tag) == "Cube" and child.attrib.get("currency") == "USD"
    ]
    if len(usd_quotes) != 1 or set(usd_quotes[0].attrib) != {"currency", "rate"}:
        raise AdmissionError("ECB dated Cube must contain exactly one USD quote")
    reference_text = dated[0].attrib["time"]
    try:
        reference = date.fromisoformat(reference_text)
    except ValueError as error:
        raise AdmissionError("ECB reference date is invalid") from error
    if reference.weekday() >= 5:
        raise AdmissionError("ECB reference date must be Monday through Friday")
    reference_age = (retrieved.date() - reference).days
    if reference_age < 0 or reference_age > int(policy["maximum_reference_age_days"]):
        raise AdmissionError("ECB reference date is future or too old")
    rate_text = usd_quotes[0].attrib["rate"]
    rate = _decimal(rate_text, label="ECB USD per EUR", positive=True)
    if document["reference_date"] != reference_text or document["usd_per_eur"] != rate_text:
        raise AdmissionError("ECB parsed date/rate metadata disagrees with raw XML")
    return {
        "retrieved_at_utc": document["retrieved_at_utc"],
        "capture_age_seconds_at_authorization": _decimal_text(capture_age),
        "reference_date": reference_text,
        "reference_age_days": reference_age,
        "usd_per_eur": _decimal_text(rate),
        "buffer_basis_points": policy["buffer_basis_points"],
        "rounding": policy["rounding"],
    }, _sha256(metadata_payload), raw_sha, rate


def _authorization_document(
    root: Path,
    requirements_path: Path,
    *,
    phase_name: str,
    run_root: str,
    project_name: str,
    dataset_path: str,
    published_precommit_sha256: str,
    created_at: datetime,
    historical_ledger_payloads: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    requirements, requirements_sha = _requirements(root, requirements_path)
    variants = _variant_map(root, requirements)
    phase = _phase_config(requirements, phase_name)
    variant = variants[phase["variant"]]
    argument_tuple = _verify_exact_argument_tuple(
        root,
        variant,
        run_root=run_root,
        project_name=project_name,
        dataset_path=dataset_path,
    )
    revision_manifest = _repository_path(
        root, requirements["revision"]["manifest_path"], label="revision manifest"
    )
    revision_sha = _sha256_file(revision_manifest, label="revision manifest")
    if revision_sha != _sha(published_precommit_sha256, label="published precommit SHA"):
        raise AdmissionError("published replacement precommit SHA does not match the sealed manifest")

    runtime_source_hashes = _verify_runtime_sources(root, requirements)
    vendor_environment_gate = _verify_vendor_environment(root, requirements)
    working_hashes: dict[str, str] = {}
    staged_hashes: dict[str, str] = {}
    for label, current_variant in variants.items():
        verify_files = historical_ledger_payloads is None and (
            phase_name == "before-v7" or label == "v13-first"
        )
        working_hashes[label], staged_hashes[label], _ = _verify_copy_manifest(
            root, current_variant, verify_evaluated_files=verify_files
        )

    if historical_ledger_payloads is None:
        v7_state = _ledger_state(
            root, variants["v7-control"], require_initial=phase_name == "before-v7"
        )
        v13_state = _ledger_state(root, variants["v13-first"], require_initial=True)
    else:
        if set(historical_ledger_payloads) != {"v7-control", "v13-first"}:
            raise AdmissionError("historical ledger reconstruction requires the exact pair")
        v7_state = _ledger_state_from_payload(
            variants["v7-control"],
            historical_ledger_payloads["v7-control"],
            require_initial=phase_name == "before-v7",
        )
        v13_state = _ledger_state_from_payload(
            variants["v13-first"],
            historical_ledger_payloads["v13-first"],
            require_initial=True,
        )
    prior_audit_sha, v7_cost = _verify_prior_ledger_identity(
        root, requirements, phase, v7_state
    )
    if phase_name == "before-v13" and v7_cost > _decimal(
        requirements["arm_gate_policy"]["max_cost_usd"],
        label="V7 exact ledger fuse",
        positive=True,
    ):
        raise AdmissionError("V7 exact ledger cost exceeds the arm fuse")
    (
        prior_arm_gate_sha,
        prior_attempt_status_sha,
        prior_proxy_log_sha,
        prior_proxy_health_sha,
    ) = _verify_prior_arm_gate(root, requirements, phase, variants)
    campaign_audit_path = _repository_path(
        root, phase["campaign_audit_path"], label="campaign audit"
    )
    if historical_ledger_payloads is None:
        campaign_audit, campaign_audit_sha = _recompute_campaign_audit(
            root, requirements, campaign_audit_path
        )
    else:
        campaign_audit, campaign_audit_sha = _recompute_historical_campaign_audit(
            root,
            requirements,
            campaign_audit_path,
            {
                variants[label]["ledger_path"]: historical_ledger_payloads[label]
                for label in ("v7-control", "v13-first")
            },
        )
    observed = _decimal(campaign_audit["observed_spend_usd"], label="campaign observed")
    baseline = _decimal(
        requirements["campaign"]["baseline_observed_usd"], label="campaign baseline"
    )
    if observed != baseline + v7_cost:
        raise AdmissionError("campaign observed spend does not equal baseline plus admitted ledgers")
    unspent = _decimal(phase["unspent_fuses_usd"], label="unspent fuses", positive=True)
    expected_unspent = sum(
        (_decimal(item["soft_fuse_usd"], label="variant fuse") for label, item in variants.items()
         if phase_name == "before-v7" or label == "v13-first"),
        Decimal("0"),
    )
    if unspent != expected_unspent:
        raise AdmissionError("phase unspent fuse total is inconsistent")
    campaign_cap = _decimal(
        requirements["campaign"]["provider_cap_usd"], label="campaign cap", positive=True
    )
    campaign_projected = observed + unspent
    if campaign_projected > campaign_cap:
        raise AdmissionError("campaign projection exceeds the USD cap")

    provider_path = _repository_path(
        root, phase["provider_telemetry_path"], label="provider telemetry"
    )
    provider_gate, provider_sha = _provider_telemetry(
        root, requirements, provider_path, at=created_at, unspent=unspent
    )
    provider_usage = _decimal(provider_gate["usage_usd"], label="current provider usage")
    historical_floor = _decimal(
        requirements["provider"]["historical_usage_floor_usd"],
        label="historical provider usage floor",
    )
    prior_provider_sha: str | None = None
    provider_delta: Decimal | None = None
    if phase_name == "before-v7":
        if phase["prior_provider_telemetry_path"] is not None:
            raise AdmissionError("before-V7 phase may not declare prior provider telemetry")
        if provider_usage < historical_floor:
            raise AdmissionError("provider usage regressed below the frozen historical floor")
    else:
        prior_relative = phase["prior_provider_telemetry_path"]
        if not isinstance(prior_relative, str):
            raise AdmissionError("before-V13 phase must bind before-V7 provider telemetry")
        prior_path = _repository_path(
            root, prior_relative, label="before-V7 provider telemetry"
        )
        prior_document, _ = _load_json(prior_path, label="before-V7 provider telemetry")
        prior_observed = _timestamp(
            prior_document.get("observed_at_utc"), label="before-V7 provider observed_at"
        )
        prior_gate, prior_provider_sha = _provider_telemetry(
            root,
            requirements,
            prior_path,
            at=prior_observed,
            unspent=Decimal("5.00"),
        )
        prior_usage = _decimal(prior_gate["usage_usd"], label="before-V7 provider usage")
        current_observed = _timestamp(
            provider_gate["observed_at_utc"], label="current provider observed_at"
        )
        if current_observed < prior_observed or provider_usage < prior_usage:
            raise AdmissionError("provider telemetry time/usage is not monotonic")
        provider_delta = provider_usage - prior_usage
        tolerance = _decimal(
            requirements["provider"]["arithmetic_tolerance_usd"],
            label="provider reconciliation tolerance",
        )
        if abs(provider_delta - v7_cost) > tolerance:
            raise AdmissionError("provider usage delta does not reconcile the V7 ledger")
    fx_metadata_path = _repository_path(
        root, phase["fx_metadata_path"], label="ECB FX metadata"
    )
    fx_raw_path = _repository_path(root, phase["fx_raw_xml_path"], label="ECB daily XML")
    fx_gate, fx_metadata_sha, fx_raw_sha, usd_per_eur = _fx_observation(
        root, requirements, fx_metadata_path, fx_raw_path, at=created_at
    )
    provider_projected = _decimal(
        provider_gate["projected_usage_usd"], label="provider projected usage"
    )
    governance_usd = max(campaign_projected, provider_projected)
    buffer_bps = Decimal(str(requirements["fx"]["buffer_basis_points"]))
    with localcontext() as context:
        context.prec = 50
        unbuffered_eur = governance_usd / usd_per_eur
        buffered_eur = unbuffered_eur * (Decimal("1") + buffer_bps / Decimal("10000"))
        projected_eur = buffered_eur.quantize(CENT, rounding=ROUND_CEILING)
    eur_ceiling = _decimal(
        requirements["fx"]["governance_ceiling_eur"], label="EUR ceiling", positive=True
    )
    if projected_eur > eur_ceiling:
        raise AdmissionError("buffered upward-rounded EUR projection exceeds the ceiling")

    predecessor = requirements["predecessor"]
    verifier_sha = _sha256_file(Path(__file__), label="admission verifier")
    provider_expiry = _timestamp(
        provider_gate["observed_at_utc"], label="provider observed_at_utc"
    ) + timedelta(seconds=int(requirements["provider"]["maximum_age_seconds"]))
    fx_expiry = _timestamp(
        fx_gate["retrieved_at_utc"], label="ECB retrieved_at_utc"
    ) + timedelta(seconds=int(requirements["fx"]["maximum_capture_age_seconds"]))
    expires = min(
        created_at
        + timedelta(
            seconds=min(
                int(requirements["provider"]["maximum_age_seconds"]),
                int(requirements["fx"]["maximum_capture_age_seconds"]),
            )
        ),
        provider_expiry,
        fx_expiry,
    )
    return {
        "schema_version": AUTHORIZATION_SCHEMA,
        "phase": phase_name,
        "variant_label": phase["variant"],
        "created_at_utc": _timestamp_text(created_at),
        "expires_at_utc": _timestamp_text(expires),
        "predecessor_precommit_sha256": predecessor["manifest_sha256"],
        "revision_precommit_sha256": revision_sha,
        "requirements_sha256": requirements_sha,
        "verifier_sha256": verifier_sha,
        "fixed_argument_tuple": argument_tuple,
        "artifact_hashes": {
            "campaign_declaration_sha256": requirements["campaign"]["declaration_sha256"],
            "runtime_source_archive_sha256": dict(sorted(runtime_source_hashes.items())),
            "vendor_site_packages_inventory_sha256": vendor_environment_gate[
                "site_packages_inventory_sha256"
            ],
            "campaign_audit_sha256": campaign_audit_sha,
            "provider_telemetry_sha256": provider_sha,
            "fx_metadata_sha256": fx_metadata_sha,
            "fx_raw_xml_sha256": fx_raw_sha,
            "working_copy_manifest_sha256": dict(sorted(working_hashes.items())),
            "staged_copy_manifest_sha256": dict(sorted(staged_hashes.items())),
            "v7_ledger_sha256": v7_state["sha256"],
            "v13_ledger_sha256": v13_state["sha256"],
            "prior_ledger_identity_audit_sha256": prior_audit_sha,
            "prior_arm_gate_sha256": prior_arm_gate_sha,
            "prior_attempt_status_sha256": prior_attempt_status_sha,
            "prior_proxy_log_sha256": prior_proxy_log_sha,
            "prior_proxy_health_sha256": prior_proxy_health_sha,
            "prior_provider_telemetry_sha256": prior_provider_sha,
        },
        "campaign_gate": {
            "baseline_observed_usd": _decimal_text(baseline),
            "newly_observed_ledger_spend_usd": _decimal_text(v7_cost),
            "fresh_observed_usd": _decimal_text(observed),
            "unspent_fuses_usd": _decimal_text(unspent),
            "projected_usd": _decimal_text(campaign_projected),
            "cap_usd": _decimal_text(campaign_cap),
            "complete": True,
            "unknown_cost_attempts": 0,
            "invalid_completion_identities": 0,
            "within_cap": True,
        },
        "provider_gate": {
            **provider_gate,
            "historical_usage_floor_usd": _decimal_text(historical_floor),
            "prior_provider_telemetry_sha256": prior_provider_sha,
            "delta_since_prior_usd": (
                None if provider_delta is None else _decimal_text(provider_delta)
            ),
            "delta_reconciles_prior_ledger": phase_name == "before-v13",
        },
        "eur_gate": {
            **fx_gate,
            "governance_basis_usd": _decimal_text(governance_usd),
            "unbuffered_eur": _decimal_text(unbuffered_eur),
            "buffer_multiplier": _decimal_text(
                Decimal("1") + buffer_bps / Decimal("10000")
            ),
            "buffered_eur_before_rounding": _decimal_text(buffered_eur),
            "projected_eur_ceil_cent": format(projected_eur, ".2f"),
            "ceiling_eur": format(eur_ceiling, ".2f"),
            "within_ceiling": True,
        },
        "ledger_gate": {
            "v7": v7_state,
            "v13": v13_state,
            "prior_identity_audit_required": phase_name == "before-v13",
            "prior_identity_audit_passed": phase_name == "before-v13",
            "prior_score_blind_arm_gate_required": phase_name == "before-v13",
            "prior_score_blind_arm_gate_passed": phase_name == "before-v13",
        },
        "working_copy_gate": {
            "both_manifests_bound": True,
            "both_staged_manifests_hash_bound": True,
            "staged_and_working_file_lists_exact": True,
            "both_staged_source_trees_pristine": True,
            "current_variant_pristine": True,
            "all_42_question_scopes_exact": True,
        },
        "runtime_source_gate": {
            "both_archives_hash_bound": True,
            "both_archives_safely_parsed": True,
            "both_extracted_trees_byte_identical_and_read_only": True,
            **vendor_environment_gate,
            "vendor_site_packages_read_only": True,
            "python_startup_isolated": True,
        },
        "authorized": True,
        "credential_recorded": False,
        "model_content_recorded": False,
    }


def _validate_authorization_freshness(
    authorization: Mapping[str, Any], *, now: datetime, maximum_age_seconds: int
) -> None:
    created = _timestamp(authorization.get("created_at_utc"), label="authorization created_at")
    expires = _timestamp(authorization.get("expires_at_utc"), label="authorization expires_at")
    if created > now or expires < now:
        raise AdmissionError("execution authorization is future-dated or expired")
    if (now - created).total_seconds() > maximum_age_seconds:
        raise AdmissionError("execution authorization is stale")


def _build_audit_document(
    root: Path,
    requirements_path: Path,
    authorization_path: Path,
    *,
    phase_name: str,
    run_root: str,
    project_name: str,
    dataset_path: str,
    published_precommit_sha256: str,
    reviewed_at: datetime,
) -> dict[str, Any]:
    authorization, payload = _load_json(authorization_path, label="execution authorization")
    created = _timestamp(authorization.get("created_at_utc"), label="authorization created_at")
    expected = _authorization_document(
        root,
        requirements_path,
        phase_name=phase_name,
        run_root=run_root,
        project_name=project_name,
        dataset_path=dataset_path,
        published_precommit_sha256=published_precommit_sha256,
        created_at=created,
    )
    if authorization != expected:
        raise AdmissionError("execution authorization differs from closed-world recomputation")
    if reviewed_at < created:
        raise AdmissionError("independent review predates authorization")
    expires = _timestamp(authorization["expires_at_utc"], label="authorization expires_at")
    if reviewed_at > expires:
        raise AdmissionError("independent review occurred after authorization expiry")
    return _audit_document_for_authorization(
        root,
        authorization_path,
        authorization,
        payload,
        phase_name=phase_name,
        reviewed_at=reviewed_at,
    )


def _audit_document_for_authorization(
    root: Path,
    authorization_path: Path,
    authorization: Mapping[str, Any],
    payload: bytes,
    *,
    phase_name: str,
    reviewed_at: datetime,
) -> dict[str, Any]:
    """Build the exact closed audit envelope around a recomputed authorization."""

    return {
        "schema_version": AUDIT_SCHEMA,
        "phase": phase_name,
        "reviewed_at_utc": _timestamp_text(reviewed_at),
        "authorization_path": str(authorization_path.relative_to(root)),
        "authorization_sha256": _sha256(payload),
        "requirements_sha256": authorization["requirements_sha256"],
        "revision_precommit_sha256": authorization["revision_precommit_sha256"],
        "verifier_sha256": authorization["verifier_sha256"],
        "artifact_hashes": authorization["artifact_hashes"],
        "checks": {
            "authorization_byte_hash_bound": True,
            "authorization_closed_world_recomputed": True,
            "campaign_gate_recomputed": True,
            "provider_gate_recomputed": True,
            "eur_gate_recomputed": True,
            "working_copy_gate_recomputed": True,
            "runtime_source_gate_recomputed": True,
            "ledger_gate_recomputed": True,
            "exact_variant_arguments_recomputed": True,
        },
        "independent_audit_passed": True,
        "credential_recorded": False,
        "model_content_recorded": False,
    }


def _finalization_config(requirements: Mapping[str, Any]) -> Mapping[str, Any]:
    config = requirements["finalization"]
    if not isinstance(config, Mapping):
        raise AdmissionError("finalization requirements must be an object")
    _require_keys(
        config,
        expected={
            "campaign_audit_path",
            "provider_telemetry_path",
            "fx_metadata_path",
            "fx_raw_xml_path",
            "before_v7_provider_telemetry_path",
            "v7_ledger_identity_audit_path",
            "v13_ledger_identity_audit_path",
            "v7_arm_gate_path",
            "v13_arm_gate_path",
            "before_v7_authorization_path",
            "before_v7_authorization_audit_path",
            "before_v13_authorization_path",
            "before_v13_authorization_audit_path",
            "authorization_path",
            "independent_audit_path",
            "v7_evaluation_audit_path",
            "v13_evaluation_audit_path",
            "paired_result_path",
        },
        label="finalization requirements",
    )
    return config


def _phase_authorization_binding(
    root: Path,
    requirements_path: Path,
    requirements: Mapping[str, Any],
    *,
    authorization_relative: str,
    audit_relative: str,
    expected_phase: str,
    revision_sha: str,
) -> dict[str, str]:
    """Exactly reconstruct an immutable phase authorization and its audit."""

    variants = _variant_map(root, requirements)
    variant = variants["v7-control" if expected_phase == "before-v7" else "v13-first"]
    authorization_path = _repository_path(
        root, authorization_relative, label=f"{expected_phase} authorization"
    )
    audit_path = _repository_path(
        root, audit_relative, label=f"{expected_phase} authorization audit"
    )
    authorization, authorization_payload = _load_json(
        authorization_path, label=f"{expected_phase} authorization"
    )
    audit, audit_payload = _load_json(
        audit_path, label=f"{expected_phase} authorization audit"
    )
    initial = {
        label: bytes.fromhex(item["initial_ledger_bytes_hex"])
        for label, item in variants.items()
    }
    historical_ledgers = dict(initial)
    if expected_phase == "before-v13":
        v7_path = _repository_path(
            root, variants["v7-control"]["ledger_path"], label="preserved V7 ledger"
        )
        historical_ledgers["v7-control"] = _stable_bytes(
            v7_path, maximum=32 * 1024 * 1024, label="preserved V7 ledger"
        )
    created = _timestamp(
        authorization.get("created_at_utc"), label=f"{expected_phase} created_at"
    )
    expected_authorization = _authorization_document(
        root,
        requirements_path,
        phase_name=expected_phase,
        run_root=variant["run_root"],
        project_name=variant["project_name"],
        dataset_path=variant["dataset_path"],
        published_precommit_sha256=revision_sha,
        created_at=created,
        historical_ledger_payloads=historical_ledgers,
    )
    if authorization != expected_authorization:
        raise AdmissionError(
            f"{expected_phase} authorization differs from exact phase reconstruction"
        )

    reviewed = _timestamp(
        audit.get("reviewed_at_utc"), label=f"{expected_phase} reviewed_at"
    )
    expires = _timestamp(
        authorization["expires_at_utc"], label=f"{expected_phase} expires_at"
    )
    if reviewed < created or reviewed > expires:
        raise AdmissionError(f"{expected_phase} audit is outside authorization lifetime")
    expected_audit = _audit_document_for_authorization(
        root,
        authorization_path,
        authorization,
        authorization_payload,
        phase_name=expected_phase,
        reviewed_at=reviewed,
    )
    if audit != expected_audit:
        raise AdmissionError(f"{expected_phase} independent audit differs from reconstruction")

    authorization_artifacts = authorization["artifact_hashes"]
    return {
        "authorization_sha256": _sha256(authorization_payload),
        "independent_audit_sha256": _sha256(audit_payload),
        "provider_telemetry_sha256": authorization_artifacts.get(
            "provider_telemetry_sha256"
        ),
        "prior_provider_telemetry_sha256": authorization_artifacts.get(
            "prior_provider_telemetry_sha256"
        ),
        "prior_arm_gate_sha256": authorization_artifacts.get(
            "prior_arm_gate_sha256"
        ),
        "prior_attempt_status_sha256": authorization_artifacts.get(
            "prior_attempt_status_sha256"
        ),
        "prior_proxy_log_sha256": authorization_artifacts.get(
            "prior_proxy_log_sha256"
        ),
        "prior_proxy_health_sha256": authorization_artifacts.get(
            "prior_proxy_health_sha256"
        ),
    }


def _finalization_document(
    root: Path,
    requirements_path: Path,
    *,
    published_precommit_sha256: str,
    created_at: datetime,
) -> dict[str, Any]:
    requirements, requirements_sha = _requirements(root, requirements_path)
    variants = _variant_map(root, requirements)
    config = _finalization_config(requirements)
    revision_manifest = _repository_path(
        root, requirements["revision"]["manifest_path"], label="revision manifest"
    )
    revision_sha = _sha256_file(revision_manifest, label="revision manifest")
    if revision_sha != _sha(published_precommit_sha256, label="published precommit SHA"):
        raise AdmissionError("published replacement precommit SHA does not match the sealed manifest")

    _verify_runtime_sources(root, requirements)
    _verify_vendor_environment(root, requirements)
    for variant in variants.values():
        _verify_copy_manifest(root, variant, verify_evaluated_files=False)

    v7_state = _ledger_state(root, variants["v7-control"], require_initial=False)
    v13_state = _ledger_state(root, variants["v13-first"], require_initial=False)
    if v7_state["events"] == 0 or v13_state["events"] == 0:
        raise AdmissionError("both evaluator ledgers must contain preserved events before finalization")
    v7_audit_sha, v7_cost = _verify_ledger_identity_audit(
        root,
        requirements,
        v7_state,
        config["v7_ledger_identity_audit_path"],
        label="V7",
    )
    v13_audit_sha, v13_cost = _verify_ledger_identity_audit(
        root,
        requirements,
        v13_state,
        config["v13_ledger_identity_audit_path"],
        label="V13",
    )
    arm_fuse = _decimal(
        requirements["arm_gate_policy"]["max_cost_usd"],
        label="arm gate exact ledger fuse",
        positive=True,
    )
    if v7_cost > arm_fuse or v13_cost > arm_fuse:
        raise AdmissionError("an exact evaluator ledger cost exceeds its arm fuse")
    (
        v7_arm_gate_sha,
        v7_attempt_status_sha,
        v7_proxy_log_sha,
        v7_proxy_health_sha,
    ) = _verify_arm_gate_artifact(
        root,
        requirements,
        variants,
        variant_label="v7-control",
        gate_relative=config["v7_arm_gate_path"],
    )
    (
        v13_arm_gate_sha,
        v13_attempt_status_sha,
        v13_proxy_log_sha,
        v13_proxy_health_sha,
    ) = _verify_arm_gate_artifact(
        root,
        requirements,
        variants,
        variant_label="v13-first",
        gate_relative=config["v13_arm_gate_path"],
    )
    pair_cost = v7_cost + v13_cost

    campaign_audit_path = _repository_path(
        root, config["campaign_audit_path"], label="final campaign audit"
    )
    campaign_audit, campaign_sha = _recompute_campaign_audit(
        root, requirements, campaign_audit_path
    )
    campaign_observed = _decimal(
        campaign_audit["observed_spend_usd"], label="final campaign observed"
    )
    baseline = _decimal(
        requirements["campaign"]["baseline_observed_usd"], label="campaign baseline"
    )
    if campaign_observed != baseline + pair_cost:
        raise AdmissionError("final campaign spend does not equal baseline plus both ledgers")
    campaign_cap = _decimal(
        requirements["campaign"]["provider_cap_usd"], label="campaign cap", positive=True
    )
    if campaign_observed > campaign_cap:
        raise AdmissionError("final campaign spend exceeds the USD cap")

    provider_path = _repository_path(
        root, config["provider_telemetry_path"], label="post-pair provider telemetry"
    )
    provider_gate, provider_sha = _provider_telemetry(
        root, requirements, provider_path, at=created_at, unspent=Decimal("0")
    )
    provider_usage = _decimal(provider_gate["usage_usd"], label="post-pair provider usage")
    before_path = _repository_path(
        root,
        config["before_v7_provider_telemetry_path"],
        label="before-V7 provider telemetry",
    )
    before_document, _ = _load_json(before_path, label="before-V7 provider telemetry")
    before_observed = _timestamp(
        before_document.get("observed_at_utc"), label="before-V7 provider observed_at"
    )
    before_gate, before_provider_sha = _provider_telemetry(
        root,
        requirements,
        before_path,
        at=before_observed,
        unspent=Decimal("5.00"),
    )
    before_usage = _decimal(before_gate["usage_usd"], label="before-V7 provider usage")
    after_observed = _timestamp(
        provider_gate["observed_at_utc"], label="post-pair provider observed_at"
    )
    if after_observed < before_observed or provider_usage < before_usage:
        raise AdmissionError("post-pair provider time/usage is not monotonic")
    provider_delta = provider_usage - before_usage
    tolerance = _decimal(
        requirements["provider"]["arithmetic_tolerance_usd"],
        label="provider reconciliation tolerance",
    )
    if abs(provider_delta - pair_cost) > tolerance:
        raise AdmissionError("post-pair provider delta does not reconcile both evaluator ledgers")

    fx_metadata_path = _repository_path(
        root, config["fx_metadata_path"], label="post-pair ECB FX metadata"
    )
    fx_raw_path = _repository_path(
        root, config["fx_raw_xml_path"], label="post-pair ECB daily XML"
    )
    fx_gate, fx_metadata_sha, fx_raw_sha, usd_per_eur = _fx_observation(
        root, requirements, fx_metadata_path, fx_raw_path, at=created_at
    )
    governance_usd = max(campaign_observed, provider_usage)
    buffer_bps = Decimal(str(requirements["fx"]["buffer_basis_points"]))
    with localcontext() as context:
        context.prec = 50
        unbuffered_eur = governance_usd / usd_per_eur
        buffered_eur = unbuffered_eur * (Decimal("1") + buffer_bps / Decimal("10000"))
        projected_eur = buffered_eur.quantize(CENT, rounding=ROUND_CEILING)
    eur_ceiling = _decimal(
        requirements["fx"]["governance_ceiling_eur"], label="EUR ceiling", positive=True
    )
    if projected_eur > eur_ceiling:
        raise AdmissionError("final buffered upward-rounded EUR spend exceeds the ceiling")

    phase_bindings = {
        "before-v7": _phase_authorization_binding(
            root,
            requirements_path,
            requirements,
            authorization_relative=config["before_v7_authorization_path"],
            audit_relative=config["before_v7_authorization_audit_path"],
            expected_phase="before-v7",
            revision_sha=revision_sha,
        ),
        "before-v13": _phase_authorization_binding(
            root,
            requirements_path,
            requirements,
            authorization_relative=config["before_v13_authorization_path"],
            audit_relative=config["before_v13_authorization_audit_path"],
            expected_phase="before-v13",
            revision_sha=revision_sha,
        ),
    }
    if (
        phase_bindings["before-v7"]["provider_telemetry_sha256"]
        != before_provider_sha
        or phase_bindings["before-v13"]["prior_provider_telemetry_sha256"]
        != before_provider_sha
    ):
        raise AdmissionError(
            "before-V7 provider telemetry no longer matches both phase authorizations"
        )
    if (
        any(
            phase_bindings["before-v7"][field] is not None
            for field in (
                "prior_arm_gate_sha256",
                "prior_attempt_status_sha256",
                "prior_proxy_log_sha256",
                "prior_proxy_health_sha256",
            )
        )
        or phase_bindings["before-v13"]["prior_arm_gate_sha256"]
        != v7_arm_gate_sha
        or phase_bindings["before-v13"]["prior_attempt_status_sha256"]
        != v7_attempt_status_sha
        or phase_bindings["before-v13"]["prior_proxy_log_sha256"]
        != v7_proxy_log_sha
        or phase_bindings["before-v13"]["prior_proxy_health_sha256"]
        != v7_proxy_health_sha
    ):
        raise AdmissionError(
            "V7 arm gate no longer matches the before-V13 authorization"
        )
    provider_expiry = after_observed + timedelta(
        seconds=int(requirements["provider"]["maximum_age_seconds"])
    )
    fx_expiry = _timestamp(
        fx_gate["retrieved_at_utc"], label="post-pair ECB retrieved_at"
    ) + timedelta(seconds=int(requirements["fx"]["maximum_capture_age_seconds"]))
    expires = min(
        created_at
        + timedelta(
            seconds=min(
                int(requirements["provider"]["maximum_age_seconds"]),
                int(requirements["fx"]["maximum_capture_age_seconds"]),
            )
        ),
        provider_expiry,
        fx_expiry,
    )
    return {
        "schema_version": FINALIZATION_SCHEMA,
        "created_at_utc": _timestamp_text(created_at),
        "expires_at_utc": _timestamp_text(expires),
        "revision_precommit_sha256": revision_sha,
        "requirements_sha256": requirements_sha,
        "verifier_sha256": _sha256_file(Path(__file__), label="admission verifier"),
        "phase_authorization_hashes": phase_bindings,
        "artifact_hashes": {
            "campaign_audit_sha256": campaign_sha,
            "post_pair_provider_telemetry_sha256": provider_sha,
            "before_v7_provider_telemetry_sha256": before_provider_sha,
            "fx_metadata_sha256": fx_metadata_sha,
            "fx_raw_xml_sha256": fx_raw_sha,
            "v7_ledger_sha256": v7_state["sha256"],
            "v13_ledger_sha256": v13_state["sha256"],
            "v7_ledger_identity_audit_sha256": v7_audit_sha,
            "v13_ledger_identity_audit_sha256": v13_audit_sha,
            "v7_arm_gate_sha256": v7_arm_gate_sha,
            "v13_arm_gate_sha256": v13_arm_gate_sha,
            "v7_attempt_status_sha256": v7_attempt_status_sha,
            "v13_attempt_status_sha256": v13_attempt_status_sha,
            "v7_proxy_log_sha256": v7_proxy_log_sha,
            "v13_proxy_log_sha256": v13_proxy_log_sha,
            "v7_proxy_health_sha256": v7_proxy_health_sha,
            "v13_proxy_health_sha256": v13_proxy_health_sha,
        },
        "campaign_gate": {
            "baseline_usd": _decimal_text(baseline),
            "v7_ledger_cost_usd": _decimal_text(v7_cost),
            "v13_ledger_cost_usd": _decimal_text(v13_cost),
            "pair_cost_usd": _decimal_text(pair_cost),
            "final_observed_usd": _decimal_text(campaign_observed),
            "cap_usd": _decimal_text(campaign_cap),
            "complete": True,
            "unknown_cost_attempts": 0,
            "invalid_completion_identities": 0,
            "within_cap": True,
        },
        "provider_gate": {
            **provider_gate,
            "before_v7_usage_usd": _decimal_text(before_usage),
            "post_pair_delta_usd": _decimal_text(provider_delta),
            "pair_ledger_cost_usd": _decimal_text(pair_cost),
            "delta_reconciles_both_ledgers": True,
        },
        "eur_gate": {
            **fx_gate,
            "governance_basis_usd": _decimal_text(governance_usd),
            "unbuffered_eur": _decimal_text(unbuffered_eur),
            "buffer_multiplier": _decimal_text(
                Decimal("1") + buffer_bps / Decimal("10000")
            ),
            "buffered_eur_before_rounding": _decimal_text(buffered_eur),
            "projected_eur_ceil_cent": format(projected_eur, ".2f"),
            "ceiling_eur": format(eur_ceiling, ".2f"),
            "within_ceiling": True,
        },
        "score_release_authorized": True,
        "credential_recorded": False,
        "model_content_recorded": False,
    }


def _finalization_audit_document(
    root: Path,
    requirements_path: Path,
    authorization_path: Path,
    *,
    published_precommit_sha256: str,
    reviewed_at: datetime,
) -> dict[str, Any]:
    authorization, payload = _load_json(
        authorization_path, label="final-spend authorization"
    )
    created = _timestamp(
        authorization.get("created_at_utc"), label="final-spend created_at"
    )
    expected = _finalization_document(
        root,
        requirements_path,
        published_precommit_sha256=published_precommit_sha256,
        created_at=created,
    )
    if authorization != expected:
        raise AdmissionError("final-spend authorization differs from recomputation")
    expires = _timestamp(
        authorization["expires_at_utc"], label="final-spend expires_at"
    )
    if reviewed_at < created or reviewed_at > expires:
        raise AdmissionError("final-spend independent review is outside authorization lifetime")
    return {
        "schema_version": FINALIZATION_AUDIT_SCHEMA,
        "reviewed_at_utc": _timestamp_text(reviewed_at),
        "authorization_path": str(authorization_path.relative_to(root)),
        "authorization_sha256": _sha256(payload),
        "requirements_sha256": authorization["requirements_sha256"],
        "revision_precommit_sha256": authorization["revision_precommit_sha256"],
        "verifier_sha256": authorization["verifier_sha256"],
        "artifact_hashes": authorization["artifact_hashes"],
        "phase_authorization_hashes": authorization["phase_authorization_hashes"],
        "checks": {
            "both_phase_authorizations_hash_bound": True,
            "all_phase_immutable_evidence_rehashed": True,
            "both_ledgers_identity_audited": True,
            "both_score_blind_arm_gates_recomputed": True,
            "campaign_audit_recomputed": True,
            "provider_delta_reconciled_to_both_ledgers": True,
            "provider_cap_recomputed": True,
            "buffered_eur_ceiling_recomputed": True,
            "runtime_sources_reverified": True,
            "score_release_gate_recomputed": True,
        },
        "independent_audit_passed": True,
        "credential_recorded": False,
        "model_content_recorded": False,
    }


def verify_finalization(
    repository_root: Path,
    requirements_path: Path,
    *,
    published_precommit_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify final provider/campaign reconciliation before score release."""

    root = repository_root.resolve(strict=True)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    requirements, _ = _requirements(root, requirements_path)
    config = _finalization_config(requirements)
    authorization_path = _repository_path(
        root, config["authorization_path"], label="final-spend authorization"
    )
    authorization, authorization_payload = _load_json(
        authorization_path, label="final-spend authorization"
    )
    _validate_authorization_freshness(
        authorization,
        now=now,
        maximum_age_seconds=min(
            int(requirements["provider"]["maximum_age_seconds"]),
            int(requirements["fx"]["maximum_capture_age_seconds"]),
        ),
    )
    created = _timestamp(authorization["created_at_utc"], label="final-spend created_at")
    expected = _finalization_document(
        root,
        requirements_path,
        published_precommit_sha256=published_precommit_sha256,
        created_at=created,
    )
    if authorization != expected:
        raise AdmissionError("final-spend authorization failed closed-world recomputation")
    audit_path = _repository_path(
        root, config["independent_audit_path"], label="final-spend independent audit"
    )
    audit, audit_payload = _load_json(audit_path, label="final-spend independent audit")
    reviewed = _timestamp(audit.get("reviewed_at_utc"), label="final-spend reviewed_at")
    expected_audit = _finalization_audit_document(
        root,
        requirements_path,
        authorization_path,
        published_precommit_sha256=published_precommit_sha256,
        reviewed_at=reviewed,
    )
    if audit != expected_audit:
        raise AdmissionError("final-spend independent audit failed hash recomputation")
    return {
        "ok": True,
        "score_release_authorized": True,
        "authorization_sha256": _sha256(authorization_payload),
        "independent_audit_sha256": _sha256(audit_payload),
        "revision_precommit_sha256": authorization["revision_precommit_sha256"],
        "campaign_observed_usd": authorization["campaign_gate"]["final_observed_usd"],
        "provider_usage_usd": authorization["provider_gate"]["usage_usd"],
        "projected_eur_ceil_cent": authorization["eur_gate"]["projected_eur_ceil_cent"],
        "credential_recorded": False,
        "model_content_recorded": False,
    }


def verify_admission(
    repository_root: Path,
    requirements_path: Path,
    *,
    phase_name: str,
    run_root: str,
    project_name: str,
    dataset_path: str,
    published_precommit_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify the complete dynamic gate without writing or contacting a provider."""

    root = repository_root.resolve(strict=True)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    requirements, _ = _requirements(root, requirements_path)
    phase = _phase_config(requirements, phase_name)
    authorization_path = _repository_path(
        root, phase["authorization_path"], label="execution authorization"
    )
    authorization, authorization_payload = _load_json(
        authorization_path, label="execution authorization"
    )
    _validate_authorization_freshness(
        authorization,
        now=now,
        maximum_age_seconds=min(
            int(requirements["provider"]["maximum_age_seconds"]),
            int(requirements["fx"]["maximum_capture_age_seconds"]),
        ),
    )
    created = _timestamp(authorization["created_at_utc"], label="authorization created_at")
    expected_authorization = _authorization_document(
        root,
        requirements_path,
        phase_name=phase_name,
        run_root=run_root,
        project_name=project_name,
        dataset_path=dataset_path,
        published_precommit_sha256=published_precommit_sha256,
        created_at=created,
    )
    if authorization != expected_authorization:
        raise AdmissionError("execution authorization failed closed-world recomputation")
    audit_path = _repository_path(
        root, phase["independent_audit_path"], label="independent authorization audit"
    )
    audit, audit_payload = _load_json(audit_path, label="independent authorization audit")
    reviewed = _timestamp(audit.get("reviewed_at_utc"), label="independent reviewed_at")
    if reviewed > now or (now - reviewed).total_seconds() > int(
        requirements["provider"]["maximum_age_seconds"]
    ):
        raise AdmissionError("independent authorization audit is stale or future-dated")
    expected_audit = _build_audit_document(
        root,
        requirements_path,
        authorization_path,
        phase_name=phase_name,
        run_root=run_root,
        project_name=project_name,
        dataset_path=dataset_path,
        published_precommit_sha256=published_precommit_sha256,
        reviewed_at=reviewed,
    )
    if audit != expected_audit:
        raise AdmissionError("independent authorization audit failed hash recomputation")
    return {
        "ok": True,
        "phase": phase_name,
        "variant_label": authorization["variant_label"],
        "authorization_sha256": _sha256(authorization_payload),
        "independent_audit_sha256": _sha256(audit_payload),
        "revision_precommit_sha256": authorization["revision_precommit_sha256"],
        "campaign_projected_usd": authorization["campaign_gate"]["projected_usd"],
        "provider_projected_usd": authorization["provider_gate"]["projected_usage_usd"],
        "projected_eur_ceil_cent": authorization["eur_gate"]["projected_eur_ceil_cent"],
        "credential_recorded": False,
        "model_content_recorded": False,
    }


def verify_arm_gate(
    repository_root: Path,
    requirements_path: Path,
    *,
    variant_label: str,
) -> dict[str, Any]:
    """Recompute a completed arm gate before any later credential action."""

    root = repository_root.resolve(strict=True)
    requirements, _ = _requirements(root, requirements_path)
    variants = _variant_map(root, requirements)
    if variant_label not in variants:
        raise AdmissionError("arm gate variant is not declared")
    config = _finalization_config(requirements)
    prefix = "v7" if variant_label == "v7-control" else "v13"
    display_label = "V7" if variant_label == "v7-control" else "V13"
    variant = variants[variant_label]
    state = _ledger_state(root, variant, require_initial=False)
    if state["events"] == 0:
        raise AdmissionError(f"{display_label} ledger has no preserved events")
    ledger_audit_sha, exact_cost = _verify_ledger_identity_audit(
        root,
        requirements,
        state,
        config[f"{prefix}_ledger_identity_audit_path"],
        label=display_label,
    )
    fuse = _decimal(
        requirements["arm_gate_policy"]["max_cost_usd"],
        label=f"{display_label} exact ledger fuse",
        positive=True,
    )
    if exact_cost > fuse:
        raise AdmissionError(f"{display_label} exact ledger cost exceeds the arm fuse")
    gate_sha, status_sha, proxy_sha, health_sha = _verify_arm_gate_artifact(
        root,
        requirements,
        variants,
        variant_label=variant_label,
        gate_relative=config[f"{prefix}_arm_gate_path"],
    )
    return {
        "ok": True,
        "schema_version": "narratordb.arm-evaluation-preaction-verification.v1",
        "variant_label": variant_label,
        "arm_gate_sha256": gate_sha,
        "attempt_status_sha256": status_sha,
        "proxy_log_sha256": proxy_sha,
        "proxy_health_sha256": health_sha,
        "ledger_identity_audit_sha256": ledger_audit_sha,
        "exact_ledger_cost_usd": _decimal_text(exact_cost),
        "within_arm_fuse": True,
        "score_blind": True,
        "credential_recorded": False,
        "model_content_recorded": False,
    }


def _require_immutable_file(path: Path, *, label: str, maximum: int) -> bytes:
    payload = _stable_bytes(path, maximum=maximum, label=label)
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise AdmissionError(f"cannot inspect immutable {label}") from error
    if metadata.st_mode & 0o222:
        raise AdmissionError(f"{label} must be immutable before score publication")
    if metadata.st_nlink != 1:
        raise AdmissionError(f"{label} must not be hard-linked")
    return payload


def _scored_tree_evidence(
    root: Path, variant: Mapping[str, Any], working: Mapping[str, Any]
) -> tuple[Path, str]:
    evaluated = Path(working["evaluated_directory"])
    try:
        evaluated = evaluated.resolve(strict=True)
        evaluated.relative_to(root)
    except (OSError, ValueError) as error:
        raise AdmissionError("scored prediction directory escapes the repository") from error
    expected_files = {
        item["path"] for item in _copy_manifest_files(working, label="working-copy manifest")
    }
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    files, directories = _tree_inventory(evaluated, label="scored prediction tree")
    if files != expected_files or directories != expected_directories:
        raise AdmissionError("scored prediction tree inventory changed")
    _require_read_only_tree(
        evaluated, files, directories, label="scored prediction tree"
    )
    records: list[bytes] = []
    for relative in sorted(files):
        payload = _stable_bytes(
            evaluated.joinpath(*PurePosixPath(relative).parts),
            maximum=16 * 1024 * 1024,
            label="scored prediction file",
        )
        records.append(
            f"{_sha256(payload)} {len(payload)} {relative}\n".encode("utf-8")
        )
    return evaluated, _sha256(b"".join(records))


def _question_scope_path(
    root: Path,
    variant: Mapping[str, Any],
    working: Mapping[str, Any],
) -> Path:
    recorded = working.get("question_id_file")
    if not isinstance(recorded, str) or not recorded or "\\" in recorded:
        raise AdmissionError("working-copy question scope path is invalid")
    candidate = Path(recorded)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate = candidate.resolve(strict=True)
        relative = candidate.relative_to(root).as_posix()
    except (OSError, ValueError) as error:
        raise AdmissionError("working-copy question scope escapes the repository") from error
    if _sha256_file(candidate, label="question scope") != variant["question_ids_sha256"]:
        raise AdmissionError("question scope hash changed")
    staged_path = _repository_path(
        root, variant["staged_copy_manifest_path"], label="staged copy manifest"
    )
    staged, _ = _load_json(staged_path, label="staged copy manifest")
    _recorded_path_ends_with(
        staged.get("question_id_file"), relative, label="staged question scope"
    )
    return candidate


def _recompute_evaluation_audit(
    root: Path,
    requirements: Mapping[str, Any],
    variant: Mapping[str, Any],
    audit_path: Path,
    *,
    label: str,
) -> tuple[dict[str, Any], bytes, dict[str, dict[str, Any]], dict[str, str]]:
    """Byte-recompute one score audit from immutable, exact variant evidence."""

    _, _, working = _verify_copy_manifest(
        root, variant, verify_evaluated_files=False
    )
    evaluated, scored_tree_sha = _scored_tree_evidence(root, variant, working)
    frozen = _repository_path(
        root,
        variant["staged_prediction_directory"],
        label=f"{label} frozen prediction directory",
    )
    question_scope = _question_scope_path(root, variant, working)
    ledger = _repository_path(root, variant["ledger_path"], label=f"{label} usage ledger")
    ledger_payload = _require_immutable_file(
        ledger, label=f"{label} usage ledger", maximum=32 * 1024 * 1024
    )
    evaluator_log = _repository_path(
        root,
        f"{variant['run_root']}/evaluation/evaluate.log",
        label=f"{label} evaluator log",
    )
    evaluator_log_payload = _require_immutable_file(
        evaluator_log, label=f"{label} evaluator log", maximum=32 * 1024 * 1024
    )
    status_path = _repository_path(
        root,
        f"{variant['run_root']}/evaluation/attempt-status.json",
        label=f"{label} attempt status",
    )
    status, status_payload = _load_json(status_path, label=f"{label} attempt status")
    _require_immutable_file(
        status_path, label=f"{label} attempt status", maximum=MAX_JSON_BYTES
    )
    expected_phase = "before-v7" if variant["label"] == "v7-control" else "before-v13"
    expected_status = {
        "evaluator_status": "0",
        "exit_status": 0,
        "final_status": "completed",
        "phase": expected_phase,
        "project": variant["project_name"],
        "schema_version": "narratordb.v13-paid-variant-attempt-status.v2",
    }
    if status != expected_status:
        raise AdmissionError(f"{label} attempt did not finish in the exact completed state")

    runtime_policy = requirements["runtime_sources"].get("v11-source")
    if not isinstance(runtime_policy, Mapping):
        raise AdmissionError("verified V11 runtime source is missing")
    runtime_root = _repository_path(
        root, runtime_policy["extracted_root"], label="verified V11 runtime source"
    )
    auditor = runtime_root / "narratordb/benchmarks/evaluation_audit.py"
    try:
        auditor.resolve(strict=True).relative_to(runtime_root)
    except (OSError, ValueError) as error:
        raise AdmissionError("evaluation auditor is missing from the verified runtime") from error
    _require_immutable_file(
        auditor, label="verified evaluation auditor", maximum=2 * 1024 * 1024
    )
    command = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        str(auditor),
        "--evaluated-directory",
        str(evaluated),
        "--frozen-directory",
        str(frozen),
        "--usage-log",
        str(ledger),
        "--evaluator-log",
        str(evaluator_log),
        "--expected-questions",
        "42",
        "--cutoffs",
        "20,50",
        "--question-id-file",
        str(question_scope),
        "--require-complete",
        "--require-official-score-complete",
    ]
    result = subprocess.run(
        command,
        cwd=root,
        env={"LANG": "C", "LC_ALL": "C"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace")[-500:]
        raise AdmissionError(f"{label} evaluation-audit recomputation failed: {error}")
    stored = _require_immutable_file(
        audit_path, label=label, maximum=MAX_JSON_BYTES
    )
    if result.stdout != stored:
        raise AdmissionError(f"{label} is not byte-identical to evidence recomputation")
    document, payload, metrics = _load_evaluation_audit(audit_path, label=label)
    return document, payload, metrics, {
        "scored_tree_sha256": scored_tree_sha,
        "usage_ledger_sha256": _sha256(ledger_payload),
        "evaluator_log_sha256": _sha256(evaluator_log_payload),
        "attempt_status_sha256": _sha256(status_payload),
        "evaluation_auditor_sha256": _sha256_file(
            auditor, label="verified evaluation auditor"
        ),
    }


def _load_evaluation_audit(
    path: Path, *, label: str
) -> tuple[dict[str, Any], bytes, dict[str, dict[str, Any]]]:
    payload = _stable_bytes(path, maximum=MAX_JSON_BYTES, label=label)
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise AdmissionError(f"cannot inspect immutable {label}") from error
    if metadata.st_mode & 0o222:
        raise AdmissionError(f"{label} must be immutable before result publication")
    for pattern in SECRET_PATTERNS:
        if pattern.search(payload):
            raise AdmissionError(f"{label} contains credential-like material")
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, AdmissionError) as error:
        raise AdmissionError(f"invalid {label}: {error}") from error
    if not isinstance(document, dict):
        raise AdmissionError(f"{label} must be a JSON object")
    _scan_for_secrets(document, label=label)
    _require_keys(
        document,
        expected={
            "schema_version",
            "complete",
            "cutoffs",
            "evaluated_questions",
            "expected_questions",
            "frozen_questions",
            "scoped_question_subset",
            "official_harness_score_complete",
            "metrics",
            "by_question_type",
            "harness_log",
            "usage",
            "validation",
        },
        label=label,
    )
    if (
        document["schema_version"] != 1
        or document["complete"] is not True
        or document["scoped_question_subset"] is not True
        or document["official_harness_score_complete"] is not True
        or document["cutoffs"] != ["top_20", "top_50"]
        or document["evaluated_questions"] != 42
        or document["expected_questions"] != 42
        or document["frozen_questions"] != 42
    ):
        raise AdmissionError(f"{label} is not a complete exact 42-question audit")
    validation = document["validation"]
    expected_validation = {
        "empty_answers",
        "empty_judges",
        "extra_evaluated_ids",
        "frozen_payload_mismatches",
        "inconsistent_verdicts",
        "invalid_scores",
        "missing_cutoffs",
        "missing_evaluated_ids",
        "missing_frozen_ids",
    }
    if not isinstance(validation, Mapping):
        raise AdmissionError(f"{label} validation is missing")
    _require_keys(validation, expected=expected_validation, label=f"{label} validation")
    if any(value != [] for value in validation.values()):
        raise AdmissionError(f"{label} contains validation failures")
    harness_log = document["harness_log"]
    if not isinstance(harness_log, Mapping) or (
        harness_log.get("attempt_five_failures") != 0
        or harness_log.get("failed_attempt_counts") != {}
        or harness_log.get("timed_out_attempt_counts") != {}
    ):
        raise AdmissionError(f"{label} contains terminal harness failures")
    usage = document["usage"]
    if not isinstance(usage, Mapping) or (
        usage.get("publication_ready") is not True
        or usage.get("unknown_cost_attempts") != 0
        or usage.get("invalid_completion_identities") != 0
        or usage.get("upstream_errors") != 0
        or usage.get("error_provider_counts") != {}
        or usage.get("error_status_counts") != {}
    ):
        raise AdmissionError(f"{label} usage is not publication-ready")

    def metric(value: Any, *, metric_label: str, expected_total: int) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise AdmissionError(f"{metric_label} is missing")
        _require_keys(
            value,
            expected={"accuracy", "correct", "total"},
            label=metric_label,
        )
        correct = value["correct"]
        total = value["total"]
        accuracy = value["accuracy"]
        if (
            isinstance(correct, bool)
            or not isinstance(correct, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total != expected_total
            or total <= 0
            or not 0 <= correct <= total
            or isinstance(accuracy, bool)
            or not isinstance(accuracy, (int, Decimal))
        ):
            raise AdmissionError(f"{metric_label} has invalid counts")
        accuracy_decimal = Decimal(accuracy)
        if not accuracy_decimal.is_finite() or abs(
            accuracy_decimal - Decimal(correct) / Decimal(total)
        ) > Decimal("0.0000005"):
            raise AdmissionError(f"{metric_label} accuracy disagrees with its counts")
        return {
            "accuracy": _decimal_text(accuracy_decimal),
            "correct": correct,
            "total": total,
        }

    metrics = document["metrics"]
    if not isinstance(metrics, Mapping):
        raise AdmissionError(f"{label} metrics are missing")
    _require_keys(metrics, expected={"top_20", "top_50"}, label=f"{label} metrics")
    normalized = {
        cutoff: metric(metrics[cutoff], metric_label=f"{label} {cutoff}", expected_total=42)
        for cutoff in ("top_20", "top_50")
    }
    by_type = document["by_question_type"]
    if not isinstance(by_type, Mapping) or not by_type:
        raise AdmissionError(f"{label} by-question-type metrics are missing")
    totals = {"top_20": 0, "top_50": 0}
    corrects = {"top_20": 0, "top_50": 0}
    for question_type, type_metrics in by_type.items():
        if not isinstance(question_type, str) or not isinstance(type_metrics, Mapping):
            raise AdmissionError(f"{label} question-type metrics are invalid")
        _require_keys(
            type_metrics,
            expected={"top_20", "top_50"},
            label=f"{label} {question_type}",
        )
        for cutoff in ("top_20", "top_50"):
            value = type_metrics[cutoff]
            if not isinstance(value, Mapping) or not isinstance(value.get("total"), int):
                raise AdmissionError(f"{label} question-type denominator is invalid")
            normalized_type = metric(
                value,
                metric_label=f"{label} {question_type} {cutoff}",
                expected_total=value["total"],
            )
            totals[cutoff] += normalized_type["total"]
            corrects[cutoff] += normalized_type["correct"]
    for cutoff in ("top_20", "top_50"):
        if totals[cutoff] != 42 or corrects[cutoff] != normalized[cutoff]["correct"]:
            raise AdmissionError(f"{label} question-type aggregation is inconsistent")
    return document, payload, normalized


def _paired_result_document(
    root: Path,
    requirements_path: Path,
    *,
    published_precommit_sha256: str,
    now: datetime,
) -> dict[str, Any]:
    requirements, _ = _requirements(root, requirements_path)
    config = _finalization_config(requirements)
    variants = _variant_map(root, requirements)
    verification = verify_finalization(
        root,
        requirements_path,
        published_precommit_sha256=published_precommit_sha256,
        now=now,
    )
    if verification.get("score_release_authorized") is not True:
        raise AdmissionError("final spend verification did not authorize score release")
    v7_path = _repository_path(
        root, config["v7_evaluation_audit_path"], label="V7 evaluation audit"
    )
    v13_path = _repository_path(
        root, config["v13_evaluation_audit_path"], label="V13 evaluation audit"
    )
    _verify_runtime_sources(root, requirements)
    _, v7_payload, v7_metrics, v7_evidence = _recompute_evaluation_audit(
        root,
        requirements,
        variants["v7-control"],
        v7_path,
        label="V7 evaluation audit",
    )
    _, v13_payload, v13_metrics, v13_evidence = _recompute_evaluation_audit(
        root,
        requirements,
        variants["v13-first"],
        v13_path,
        label="V13 evaluation audit",
    )
    return {
        "schema_version": PAIRED_RESULT_SCHEMA,
        "classification": "consumed-development paired V7-control versus V13-local-replay",
        "denominator": 42,
        "cutoffs": [20, 50],
        "v7_control": v7_metrics,
        "v13_first": v13_metrics,
        "delta_correct": {
            cutoff: v13_metrics[cutoff]["correct"] - v7_metrics[cutoff]["correct"]
            for cutoff in ("top_20", "top_50")
        },
        "evaluation_audit_sha256": {
            "v7_control": _sha256(v7_payload),
            "v13_first": _sha256(v13_payload),
        },
        "evaluation_evidence_sha256": {
            "v7_control": v7_evidence,
            "v13_first": v13_evidence,
        },
        "final_verification_sha256": _sha256(_canonical_json(verification)),
        "final_spend_authorization_sha256": verification["authorization_sha256"],
        "revision_precommit_sha256": verification["revision_precommit_sha256"],
        "score_release_authorized": True,
        "credential_recorded": False,
        "model_content_recorded": False,
    }


def _paired_result_output(
    root: Path, requirements: Mapping[str, Any], requested: Path
) -> Path:
    declared_relative = _finalization_config(requirements)["paired_result_path"]
    declared = _repository_path(
        root, declared_relative, label="declared paired result", must_exist=False
    )
    if requested.is_absolute():
        try:
            relative = requested.relative_to(root)
        except ValueError as error:
            raise AdmissionError("paired result output escapes the repository") from error
    else:
        relative = requested
    pure = PurePosixPath(relative.as_posix())
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative.as_posix():
        raise AdmissionError("paired result output path is unsafe")
    candidate = _repository_path(
        root, pure.as_posix(), label="paired result output", must_exist=False
    )
    if candidate != declared:
        raise AdmissionError("paired result output is not the exact declared path")
    return candidate


def _arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--phase", choices=("before-v7", "before-v13"), required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--dataset-path", required=True)
    parser.add_argument("--published-precommit-sha256", required=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    campaign_build = subparsers.add_parser("build-campaign-audit")
    campaign_build.add_argument("--repository-root", type=Path, required=True)
    campaign_build.add_argument("--requirements", type=Path, required=True)
    campaign_build.add_argument("--output", type=Path, required=True)
    paired_result = subparsers.add_parser("build-paired-result")
    paired_result.add_argument("--repository-root", type=Path, required=True)
    paired_result.add_argument("--requirements", type=Path, required=True)
    paired_result.add_argument("--published-precommit-sha256", required=True)
    paired_result.add_argument("--output", type=Path, required=True)
    build = subparsers.add_parser("build-authorization")
    _arguments(build)
    build.add_argument("--output", type=Path, required=True)
    audit = subparsers.add_parser("build-audit")
    _arguments(audit)
    audit.add_argument("--authorization", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    _arguments(verify)
    arm_verify = subparsers.add_parser("verify-arm-gate")
    arm_verify.add_argument("--repository-root", type=Path, required=True)
    arm_verify.add_argument("--requirements", type=Path, required=True)
    arm_verify.add_argument(
        "--variant", choices=("v7-control", "v13-first"), required=True
    )
    final_build = subparsers.add_parser("build-finalization")
    final_build.add_argument("--repository-root", type=Path, required=True)
    final_build.add_argument("--requirements", type=Path, required=True)
    final_build.add_argument("--published-precommit-sha256", required=True)
    final_build.add_argument("--output", type=Path, required=True)
    final_audit = subparsers.add_parser("build-finalization-audit")
    final_audit.add_argument("--repository-root", type=Path, required=True)
    final_audit.add_argument("--requirements", type=Path, required=True)
    final_audit.add_argument("--published-precommit-sha256", required=True)
    final_audit.add_argument("--authorization", type=Path, required=True)
    final_audit.add_argument("--output", type=Path, required=True)
    final_verify = subparsers.add_parser("verify-finalization")
    final_verify.add_argument("--repository-root", type=Path, required=True)
    final_verify.add_argument("--requirements", type=Path, required=True)
    final_verify.add_argument("--published-precommit-sha256", required=True)

    args = parser.parse_args(argv)
    try:
        root = args.repository_root.resolve(strict=True)
        requirements_path = args.requirements.resolve(strict=True)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        if args.command == "build-campaign-audit":
            requirements, _ = _requirements(root, requirements_path)
            output = _declared_campaign_audit_output(root, requirements, args.output)
            document, payload = _current_campaign_audit(root, requirements)
            if _canonical_json(document) != payload:
                raise AdmissionError("campaign audit builder output changed during validation")
            _write_new(output, document)
            result = {
                "ok": True,
                "output": str(output),
                "sha256": _sha256(payload),
            }
        elif args.command == "build-paired-result":
            requirements, _ = _requirements(root, requirements_path)
            output = _paired_result_output(root, requirements, args.output)
            document = _paired_result_document(
                root,
                requirements_path,
                published_precommit_sha256=args.published_precommit_sha256,
                now=now,
            )
            _write_new(output, document)
            result = {
                "ok": True,
                "output": str(output),
                "sha256": _sha256(_canonical_json(document)),
            }
        elif args.command in {"build-authorization", "build-audit", "verify"}:
            common = {
                "phase_name": args.phase,
                "run_root": args.run_root,
                "project_name": args.project_name,
                "dataset_path": args.dataset_path,
                "published_precommit_sha256": args.published_precommit_sha256,
            }
            if args.command == "build-authorization":
                document = _authorization_document(
                    root, requirements_path, created_at=now, **common
                )
                output = args.output if args.output.is_absolute() else root / args.output
                _write_new(output, document)
                result = {
                    "ok": True,
                    "output": str(output),
                    "sha256": _sha256(_canonical_json(document)),
                }
            elif args.command == "build-audit":
                authorization = (
                    args.authorization
                    if args.authorization.is_absolute()
                    else root / args.authorization
                )
                document = _build_audit_document(
                    root,
                    requirements_path,
                    authorization,
                    reviewed_at=now,
                    **common,
                )
                output = args.output if args.output.is_absolute() else root / args.output
                _write_new(output, document)
                result = {
                    "ok": True,
                    "output": str(output),
                    "sha256": _sha256(_canonical_json(document)),
                }
            else:
                result = verify_admission(
                    root, requirements_path, now=now, **common
                )
        elif args.command == "verify-arm-gate":
            result = verify_arm_gate(
                root, requirements_path, variant_label=args.variant
            )
        elif args.command == "build-finalization":
            document = _finalization_document(
                root,
                requirements_path,
                published_precommit_sha256=args.published_precommit_sha256,
                created_at=now,
            )
            output = args.output if args.output.is_absolute() else root / args.output
            _write_new(output, document)
            result = {
                "ok": True,
                "output": str(output),
                "sha256": _sha256(_canonical_json(document)),
            }
        elif args.command == "build-finalization-audit":
            authorization = (
                args.authorization
                if args.authorization.is_absolute()
                else root / args.authorization
            )
            document = _finalization_audit_document(
                root,
                requirements_path,
                authorization,
                published_precommit_sha256=args.published_precommit_sha256,
                reviewed_at=now,
            )
            output = args.output if args.output.is_absolute() else root / args.output
            _write_new(output, document)
            result = {
                "ok": True,
                "output": str(output),
                "sha256": _sha256(_canonical_json(document)),
            }
        else:
            result = verify_finalization(
                root,
                requirements_path,
                published_precommit_sha256=args.published_precommit_sha256,
                now=now,
            )
    except (AdmissionError, FileNotFoundError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
