#!/usr/bin/env python3
"""Offline-only finalization recovery for the terminal sealed R5 paid pair.

The worker deliberately writes nothing to stdout or stderr.  Stage A reconstructs
only score-blind finalization evidence at its preserved review timestamp.  Stage B
is inaccessible until two immutable independent reviews and their aggregate GO
bind the sealed recovery candidate and the exact Stage-A verification payload.
"""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence


PROTOCOL_SCHEMA = "narratordb.v13-paid-r5-offline-finalization-recovery-protocol.v1"
TERMINAL_STATUS_SCHEMA = "narratordb.v13-paid-r5-recovery-terminal-status.v1"
STAGE_A_ENVELOPE_SCHEMA = "narratordb.v13-paid-r5-historical-finalization-envelope.v1"
REVIEW_SCHEMA = "narratordb.v13-paid-r5-recovery-go-review.v1"
GO_SCHEMA = "narratordb.v13-paid-r5-recovery-go.v1"
RESULT_SCHEMA = "narratordb.v13-paid-r5-recovered-paired-result.v1"
COMPLETE_SCHEMA = "narratordb.v13-paid-r5-recovery-release-complete.v1"
EXPECTED_ENVIRONMENT_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "PYTHONDONTWRITEBYTECODE",
    "TMPDIR",
}
HEX = frozenset("0123456789abcdef")
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_INPUT_BYTES = 64 * 1024 * 1024
NETWORK_AUDIT_PREFIXES = (
    "socket.",
    "urllib.",
    "http.client.",
)
FORBIDDEN_SCORE_FIELDS = {
    "accuracy",
    "answer",
    "answers",
    "by_question_type",
    "correct",
    "delta_correct",
    "judge",
    "judges",
    "metric",
    "metrics",
    "numerator",
    "score",
    "scores",
    "score_release_authorized",
    "verdict",
    "verdicts",
}
RENAME_EXCL = 0x00000004
AT_FDCWD = -2
_RELEASE_COMMITTED = False


class RecoveryError(RuntimeError):
    """Fail-closed recovery error whose details are never emitted."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError("duplicate JSON field")
        result[key] = value
    return result


def _stable_bytes(path: Path, *, maximum: int = MAX_INPUT_BYTES) -> bytes:
    if path.is_symlink():
        raise RecoveryError("symlink input")
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        raise RecoveryError("invalid input file")
    payload = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    if (
        len(payload) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise RecoveryError("input changed while read")
    return payload


def _load_json(path: Path) -> tuple[dict[str, Any], bytes]:
    payload = _stable_bytes(path, maximum=MAX_JSON_BYTES)
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_unique_pairs,
            parse_float=Decimal,
            parse_constant=lambda _: (_ for _ in ()).throw(RecoveryError("constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecoveryError) as error:
        raise RecoveryError("invalid JSON") from error
    if not isinstance(document, dict):
        raise RecoveryError("JSON root is not an object")
    return document, payload


def _require_exact_keys(
    document: Mapping[str, Any], expected: set[str], *, label: str
) -> None:
    if set(document) != expected:
        raise RecoveryError(f"{label} fields")


def _reject_score_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in FORBIDDEN_SCORE_FIELDS:
                raise RecoveryError("score-bearing field in score-blind document")
            _reject_score_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_score_fields(child)


def _zero_activity(protocol: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "additional_spend_usd": "0",
        "credential_calls": 0,
        "fx_calls": 0,
        "judge_calls": 0,
        "model_calls": 0,
        "network_calls": 0,
        "provider_calls": 0,
    }
    if protocol.get("zero_new_activity") != expected:
        raise RecoveryError("zero-activity policy changed")
    return expected


def _require_sha(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in HEX for character in value)
    ):
        raise RecoveryError("invalid SHA-256")
    return value


def _require_immutable(
    path: Path, *, maximum: int = MAX_INPUT_BYTES, exact_mode: int | None = None
) -> bytes:
    payload = _stable_bytes(path, maximum=maximum)
    metadata = path.stat(follow_symlinks=False)
    if metadata.st_mode & 0o222 or metadata.st_nlink != 1:
        raise RecoveryError("artifact is not immutable and singly linked")
    if exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode:
        raise RecoveryError("artifact mode is not exact")
    return payload


def _repository_path(
    root: Path, relative: Any, *, must_exist: bool = True
) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise RecoveryError("unsafe repository path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise RecoveryError("unsafe repository path")
    candidate = root.joinpath(*pure.parts)
    if must_exist:
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise RecoveryError("repository path escape") from error
        current = root
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise RecoveryError("repository path traverses a symlink")
        return resolved
    parent = candidate.parent
    while parent != root and not parent.exists():
        parent = parent.parent
    if parent.exists():
        resolved_parent = parent.resolve(strict=True)
        try:
            resolved_parent.relative_to(root)
        except ValueError as error:
            raise RecoveryError("output parent escapes repository") from error
        if resolved_parent != parent.absolute():
            raise RecoveryError("output parent traverses a symlink")
    return candidate.absolute()


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RecoveryError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise RecoveryError("invalid timestamp") from error
    if parsed.tzinfo is None or parsed.microsecond:
        raise RecoveryError("timestamp must be whole-second UTC")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_manifest(payload: bytes, *, basename_only: bool) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise RecoveryError("manifest encoding") from error
    if not lines or payload != ("\n".join(lines) + "\n").encode("utf-8"):
        raise RecoveryError("manifest is not canonical line text")
    for line in lines:
        if len(line) < 67 or line[64:66] != "  ":
            raise RecoveryError("manifest line")
        digest = _require_sha(line[:64])
        relative = line[66:]
        if not relative or relative in result:
            raise RecoveryError("duplicate manifest path")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
            raise RecoveryError("unsafe manifest path")
        if basename_only and len(pure.parts) != 1:
            raise RecoveryError("bundle manifest path must be a basename")
        result[relative] = digest
    return result


def _validate_clean_environment() -> None:
    if set(os.environ) != EXPECTED_ENVIRONMENT_KEYS:
        raise RecoveryError("environment is not closed-world")
    if (
        os.environ.get("LANG") != "C"
        or os.environ.get("LC_ALL") != "C"
        or os.environ.get("PYTHONDONTWRITEBYTECODE") != "1"
    ):
        raise RecoveryError("environment values changed")
    for name in ("HOME", "TMPDIR"):
        path = Path(os.environ[name]).resolve(strict=True)
        metadata = path.stat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise RecoveryError("private runtime directory mode changed")
    for value in os.listdir("/dev/fd"):
        if not value.isdigit() or int(value) <= 2:
            continue
        try:
            fcntl.fcntl(int(value), fcntl.F_GETFD)
        except OSError:
            continue
        raise RecoveryError("unexpected inherited file descriptor")


def _network_audit(event: str, _arguments: tuple[Any, ...]) -> None:
    if event.startswith(NETWORK_AUDIT_PREFIXES):
        raise RecoveryError("network operation denied")


def _validate_python_command(command: Any, expected_python: Path) -> list[str]:
    if not isinstance(command, (list, tuple)) or not all(
        isinstance(item, str) for item in command
    ):
        raise RecoveryError("non-list subprocess command")
    normalized = list(command)
    if len(normalized) < 5:
        raise RecoveryError("short subprocess command")
    executable = Path(normalized[0]).resolve(strict=True)
    if executable != expected_python or normalized[1:4] != ["-I", "-S", "-B"]:
        raise RecoveryError("subprocess is not the exact isolated Python")
    if "-m" in normalized[4:]:
        raise RecoveryError("module subprocess is forbidden")
    return normalized


def _install_subprocess_guard(expected_python: Path) -> None:
    original = subprocess.run

    def guarded(command: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        normalized = _validate_python_command(command, expected_python)
        if kwargs.get("shell"):
            raise RecoveryError("shell subprocess forbidden")
        return original(normalized, *args, **kwargs)

    subprocess.run = guarded  # type: ignore[assignment]


def _validate_bundle_seal(
    root: Path, protocol: Mapping[str, Any], published_seal: str
) -> None:
    published_seal = _require_sha(published_seal)
    bundle = _repository_path(root, protocol["recovery_precommit"]["bundle_root"])
    inventory_path = _repository_path(
        root, protocol["recovery_precommit"]["bundle_inventory_path"]
    )
    inventory, _ = _load_json(inventory_path)
    _require_exact_keys(
        inventory,
        {
            "allowed_bundle_files_before_seal",
            "allowed_file_created_at_seal",
            "allowed_subdirectories",
            "bundle_root",
            "candidate_status",
            "schema_version",
        },
        label="bundle inventory",
    )
    allowed = inventory["allowed_bundle_files_before_seal"]
    if (
        not isinstance(allowed, list)
        or not allowed
        or any(not isinstance(item, str) for item in allowed)
        or len(set(allowed)) != len(allowed)
        or inventory["allowed_subdirectories"] != []
        or inventory["allowed_file_created_at_seal"] != "SHA256SUMS"
        or len(allowed) != protocol["recovery_precommit"]["preseal_file_count"]
        or protocol["recovery_precommit"]["sealed_physical_file_count"] != 11
    ):
        raise RecoveryError("invalid bundle inventory")
    physical = list(bundle.iterdir())
    if any(item.is_dir() or item.is_symlink() for item in physical):
        raise RecoveryError("bundle has a directory or symlink")
    if {item.name for item in physical} != set(allowed) | {"SHA256SUMS"}:
        raise RecoveryError("bundle inventory changed")
    if len(physical) != protocol["recovery_precommit"]["sealed_physical_file_count"]:
        raise RecoveryError("sealed bundle physical file count changed")
    manifest = _repository_path(root, protocol["recovery_precommit"]["seal_manifest_path"])
    manifest_payload = _require_immutable(
        manifest, maximum=MAX_JSON_BYTES, exact_mode=0o444
    )
    if _sha256(manifest_payload) != published_seal:
        raise RecoveryError("published recovery seal mismatch")
    entries = _parse_manifest(manifest_payload, basename_only=True)
    if set(entries) != set(allowed):
        raise RecoveryError("sealed bundle entries changed")
    for name, expected in entries.items():
        payload = _require_immutable(bundle / name, maximum=MAX_INPUT_BYTES)
        if _sha256(payload) != expected:
            raise RecoveryError("sealed bundle member changed")


def _r5_nested_input_payload(
    root: Path, protocol: Mapping[str, Any], relative: str
) -> bytes:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise RecoveryError("unsafe R5 nested path")
    candidate = root.joinpath(*pure.parts)
    current = root
    for part in pure.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise RecoveryError("R5 nested path has an intermediate symlink")
    if not candidate.is_symlink():
        return _stable_bytes(
            _repository_path(root, relative), maximum=MAX_INPUT_BYTES
        )
    environment = protocol["execution_environment"]
    if relative != environment["python_entrypoint_path"]:
        raise RecoveryError("unexpected R5 nested symlink")
    if os.readlink(candidate) != environment["python_entrypoint_symlink_target"]:
        raise RecoveryError("R5 Python symlink text changed")
    resolved = candidate.resolve(strict=True)
    expected_python = Path(environment["python_real_path"]).resolve(strict=True)
    metadata = resolved.stat(follow_symlinks=False)
    if (
        resolved != expected_python
        or not stat.S_ISREG(metadata.st_mode)
        or not os.access(resolved, os.X_OK)
    ):
        raise RecoveryError("R5 Python symlink target changed")
    payload = _stable_bytes(resolved, maximum=MAX_INPUT_BYTES)
    if _sha256(payload) != environment["python_real_sha256"]:
        raise RecoveryError("R5 Python target bytes changed")
    return payload


def _validate_bound_inputs(root: Path, protocol: Mapping[str, Any]) -> None:
    manifest = _repository_path(root, protocol["input_manifest_path"])
    entries = _parse_manifest(
        _require_immutable(manifest, maximum=MAX_JSON_BYTES, exact_mode=0o444),
        basename_only=False,
    )
    if len(entries) < 30:
        raise RecoveryError("bound input manifest is incomplete")
    for relative, expected in entries.items():
        path = _repository_path(root, relative)
        if _sha256(_stable_bytes(path, maximum=MAX_INPUT_BYTES)) != expected:
            raise RecoveryError("bound input changed")
    terminal = protocol["terminal_failure_record"]
    record = _repository_path(root, terminal["record_path"])
    checksum = _repository_path(root, terminal["checksum_manifest_path"])
    if (
        _sha256(
            _require_immutable(record, maximum=MAX_JSON_BYTES, exact_mode=0o444)
        )
        != terminal["record_sha256"]
        or _sha256(
            _require_immutable(checksum, maximum=MAX_JSON_BYTES, exact_mode=0o444)
        )
        != terminal["checksum_manifest_sha256"]
    ):
        raise RecoveryError("terminal failure record binding changed")
    source = protocol["r5_source"]
    r5_manifest = _repository_path(root, source["seal_manifest_path"])
    r5_payload = _stable_bytes(r5_manifest, maximum=MAX_JSON_BYTES)
    if _sha256(r5_payload) != source["seal_manifest_sha256"]:
        raise RecoveryError("R5 seal changed")
    r5_entries = _parse_manifest(r5_payload, basename_only=True)
    r5_bundle = r5_manifest.parent
    if {item.name for item in r5_bundle.iterdir()} != set(r5_entries) | {"SHA256SUMS"}:
        raise RecoveryError("R5 closed-world bundle changed")
    for name, expected in r5_entries.items():
        member = r5_bundle / name
        if member.is_symlink() or not member.is_file():
            raise RecoveryError("R5 bundle member type changed")
        if _sha256(_stable_bytes(member, maximum=MAX_INPUT_BYTES)) != expected:
            raise RecoveryError("R5 bundle member changed")
    r5_bound = r5_bundle / "BOUND_INPUTS_SHA256SUMS"
    nested = _parse_manifest(
        _stable_bytes(r5_bound, maximum=MAX_JSON_BYTES), basename_only=False
    )
    if len(nested) != 66:
        raise RecoveryError("R5 nested input count changed")
    for relative, expected in nested.items():
        payload = _r5_nested_input_payload(root, protocol, relative)
        if _sha256(payload) != expected:
            raise RecoveryError("R5 nested bound input changed")


def _mac_type(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "Directory"
    if stat.S_ISREG(mode):
        return "Regular File"
    if stat.S_ISLNK(mode):
        return "Symbolic Link"
    if stat.S_ISFIFO(mode):
        return "Fifo"
    if stat.S_ISSOCK(mode):
        return "Socket"
    if stat.S_ISCHR(mode):
        return "Character Device"
    if stat.S_ISBLK(mode):
        return "Block Device"
    raise RecoveryError("unsupported inventory entry type")


def _attempt_inventory(root: Path, protocol: Mapping[str, Any]) -> dict[str, Any]:
    policy = protocol["attempt_preservation"]
    attempt = _repository_path(root, policy["root"])
    entries: list[Path] = []
    pending = [attempt]
    while pending:
        current = pending.pop()
        entries.append(current)
        metadata = current.lstat()
        if stat.S_ISDIR(metadata.st_mode) and not current.is_symlink():
            with os.scandir(current) as iterator:
                pending.extend(Path(item.path) for item in iterator)
    entries.sort(key=lambda item: os.fsencode(item.relative_to(root).as_posix()))
    digest = hashlib.sha256()
    files = directories = symlinks = hardlinked = 0
    for entry in entries:
        metadata = entry.lstat()
        relative = entry.relative_to(root).as_posix()
        entry_type = _mac_type(metadata.st_mode)
        digest.update(
            f"{relative}|{entry_type}|{metadata.st_size}|{stat.filemode(metadata.st_mode)}|{metadata.st_nlink}\n".encode(
                "utf-8"
            )
        )
        if stat.S_ISREG(metadata.st_mode) and not entry.is_symlink():
            files += 1
            if metadata.st_nlink != 1:
                hardlinked += 1
            payload = _stable_bytes(entry, maximum=MAX_INPUT_BYTES)
            digest.update(f"{_sha256(payload)}  {relative}\n".encode("utf-8"))
        elif stat.S_ISDIR(metadata.st_mode):
            directories += 1
        elif stat.S_ISLNK(metadata.st_mode):
            symlinks += 1
    result = {
        "directories": directories,
        "files": files,
        "hardlinked_regular_files": hardlinked,
        "symlinks": symlinks,
        "tree_fingerprint_sha256": digest.hexdigest(),
    }
    expected = {
        "directories": policy["directories"],
        "files": policy["files"],
        "hardlinked_regular_files": policy["hardlinked_regular_files"],
        "symlinks": policy["symlinks"],
        "tree_fingerprint_sha256": policy["tree_fingerprint_sha256"],
    }
    if result != expected:
        raise RecoveryError("terminal R5 attempt changed")
    return result


def _assert_original_publication_absent(root: Path) -> None:
    record_path = root / "benchmark_records/reproduction-v13-paid-paired-scoring-r5-3453-failed-finalization-20260716.json"
    record, _ = _load_json(record_path)
    for relative in record["publication_state"]["absent_paths"]:
        candidate = _repository_path(root, relative, must_exist=False)
        if candidate.exists() or candidate.is_symlink():
            raise RecoveryError("terminal R5 publication state changed")


def _load_protocol(root: Path, requested: Path) -> tuple[dict[str, Any], Path]:
    path = requested if requested.is_absolute() else root / requested
    path = path.resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise RecoveryError("protocol escapes repository") from error
    protocol, payload = _load_json(path)
    if protocol.get("schema_version") != PROTOCOL_SCHEMA:
        raise RecoveryError("unsupported recovery protocol")
    if _canonical_json(protocol) != payload:
        raise RecoveryError("recovery protocol is not canonical JSON")
    return protocol, path


def _load_sealed_verifier(
    root: Path, protocol: Mapping[str, Any]
) -> tuple[Any, Path, Path]:
    source = protocol["r5_source"]
    verifier_path = _repository_path(root, source["verifier_path"])
    requirements_path = _repository_path(root, source["requirements_path"])
    if (
        _sha256(_stable_bytes(verifier_path)) != source["verifier_sha256"]
        or _sha256(_stable_bytes(requirements_path)) != source["requirements_sha256"]
    ):
        raise RecoveryError("sealed R5 verifier binding changed")
    spec = importlib.util.spec_from_file_location(
        "narratordb_r5_finalization_recovery_exact_verifier", verifier_path
    )
    if spec is None or spec.loader is None:
        raise RecoveryError("cannot import sealed verifier")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for name in (
        "_canonical_json",
        "_finalization_document",
        "_finalization_audit_document",
        "_finalization_config",
        "_load_evaluation_audit",
        "_recompute_evaluation_audit",
        "_repository_path",
        "_requirements",
        "_scored_tree_evidence",
        "_question_scope_path",
        "_variant_map",
        "_verify_copy_manifest",
        "verify_finalization",
    ):
        if not callable(getattr(module, name, None)):
            raise RecoveryError("sealed verifier API changed")
    return module, verifier_path, requirements_path


def _historical_recomputation(
    root: Path,
    protocol: Mapping[str, Any],
    module: Any,
    requirements_path: Path,
) -> tuple[bytes, bytes, bytes, dict[str, Any]]:
    source_seal = protocol["r5_source"]["seal_manifest_sha256"]
    historical = protocol["historical_finalization"]
    created = _parse_timestamp(historical["authorization_created_at_utc"])
    reviewed = _parse_timestamp(historical["verification_now_utc"])
    expires = _parse_timestamp(historical["authorization_expires_at_utc"])
    if not created <= reviewed <= expires:
        raise RecoveryError("historical verification time is invalid")
    authorization_path = _repository_path(root, historical["authorization_path"])
    audit_path = _repository_path(root, historical["independent_audit_path"])
    authorization = module._finalization_document(
        root,
        requirements_path,
        published_precommit_sha256=source_seal,
        created_at=created,
    )
    authorization_payload = module._canonical_json(authorization)
    if (
        authorization_payload != _stable_bytes(authorization_path, maximum=MAX_JSON_BYTES)
        or _sha256(authorization_payload) != historical["authorization_sha256"]
    ):
        raise RecoveryError("historical finalization reconstruction changed")
    audit = module._finalization_audit_document(
        root,
        requirements_path,
        authorization_path,
        published_precommit_sha256=source_seal,
        reviewed_at=reviewed,
    )
    audit_payload = module._canonical_json(audit)
    if (
        audit_payload != _stable_bytes(audit_path, maximum=MAX_JSON_BYTES)
        or _sha256(audit_payload) != historical["independent_audit_sha256"]
    ):
        raise RecoveryError("historical audit reconstruction changed")
    verification = module.verify_finalization(
        root,
        requirements_path,
        published_precommit_sha256=source_seal,
        now=reviewed,
    )
    _require_exact_keys(
        verification,
        {
            "authorization_sha256",
            "campaign_observed_usd",
            "credential_recorded",
            "independent_audit_sha256",
            "model_content_recorded",
            "ok",
            "projected_eur_ceil_cent",
            "provider_usage_usd",
            "revision_precommit_sha256",
            "score_release_authorized",
        },
        label="historical verification",
    )
    if (
        verification["ok"] is not True
        or verification["score_release_authorized"] is not True
        or verification["credential_recorded"] is not False
        or verification["model_content_recorded"] is not False
        or verification["authorization_sha256"] != historical["authorization_sha256"]
        or verification["independent_audit_sha256"]
        != historical["independent_audit_sha256"]
    ):
        raise RecoveryError("historical verification did not authorize recovery")
    return (
        authorization_payload,
        audit_payload,
        _canonical_json(verification),
        verification,
    )


def _assert_output_root(
    root: Path, protocol: Mapping[str, Any], *, writable: bool = True
) -> Path:
    output = _repository_path(root, protocol["output"]["output_root"])
    metadata = output.stat(follow_symlinks=False)
    if (
        output.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode)
        not in ({0o700} if writable else {0o700, 0o555})
        or metadata.st_uid != os.getuid()
    ):
        raise RecoveryError("recovery output root is not private")
    return output


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_bytes(path: Path, payload: bytes, *, output_root: Path) -> None:
    if path.parent != output_root or path.exists() or path.is_symlink():
        raise RecoveryError("refusing recovery output overwrite")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RecoveryError("short recovery output write")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(output_root)


def _write_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RecoveryError("short private write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RecoveryError("private file mode changed")


def _terminalize(
    root: Path,
    protocol: Mapping[str, Any] | None,
    *,
    stage: str,
    published_seal: str,
) -> None:
    if protocol is None:
        return
    try:
        output_relative = protocol["output"]["output_root"]
        output = _repository_path(root, output_relative, must_exist=False)
        if not output.exists():
            output.mkdir(mode=0o700, parents=True, exist_ok=False)
        if output.is_symlink() or not output.is_dir():
            return
        os.chmod(output, 0o700)
        status_path = _repository_path(
            root, protocol["output"]["failed_status_path"], must_exist=False
        )
        if not status_path.exists() and not status_path.is_symlink():
            document = {
                "credential_recorded": False,
                "model_content_recorded": False,
                "published_recovery_precommit_sha256": (
                    published_seal if len(published_seal) == 64 else None
                ),
                "recovery_attempt": "r1",
                "schema_version": TERMINAL_STATUS_SCHEMA,
                "source_attempt_tree_fingerprint_sha256": protocol[
                    "attempt_preservation"
                ]["tree_fingerprint_sha256"],
                "stage": stage,
                "status": "terminal-failure-preserve-preexisting-and-published-evidence-destroy-private-scratch-no-retry",
                "terminal_failure_record_sha256": protocol["terminal_failure_record"][
                    "record_sha256"
                ],
                "zero_new_activity": _zero_activity(protocol),
            }
            _reject_score_fields(document)
            _write_new_bytes(status_path, _canonical_json(document), output_root=output)
        os.chmod(output, 0o555)
        _fsync_directory(output.parent)
    except Exception:
        return


def _stage_a_path(root: Path, protocol: Mapping[str, Any]) -> Path:
    return _repository_path(
        root, protocol["output"]["stage_a"]["envelope_path"], must_exist=False
    )


def _stage_a_envelope(
    protocol: Mapping[str, Any],
    *,
    executed_at: datetime,
    authorization_payload: bytes,
    audit_payload: bytes,
    verification_payload: bytes,
) -> dict[str, Any]:
    expires = _parse_timestamp(
        protocol["historical_finalization"]["authorization_expires_at_utc"]
    )
    document = {
        "benchmark_scope": "consumed-development",
        "credential_recorded": False,
        "cross_attempt_combination": False,
        "executed_at_utc": _timestamp_text(executed_at),
        "external_result_or_score_input": False,
        "historical_authorization_reconstruction_sha256": _sha256(
            authorization_payload
        ),
        "historical_independent_audit_reconstruction_sha256": _sha256(audit_payload),
        "historical_raw_verification_payload_sha256": _sha256(verification_payload),
        "historical_replay_at_utc": protocol["historical_finalization"][
            "verification_now_utc"
        ],
        "model_content_recorded": False,
        "present_time_freshness_assessed": True,
        "present_time_freshness_claimed": False,
        "present_time_freshness_valid": executed_at <= expires,
        "r5_status": "terminal-finalization-failure",
        "recovery_status": "historical-finalization-recomputed-score-blind",
        "schema_version": STAGE_A_ENVELOPE_SCHEMA,
        "score_bearing_fields_published": False,
        "sole_source_attempt_root": protocol["sole_source_policy"][
            "sole_source_attempt_root"
        ],
        "source_attempt_tree_fingerprint_sha256": protocol["attempt_preservation"][
            "tree_fingerprint_sha256"
        ],
        "source_r5_precommit_sha256": protocol["r5_source"][
            "seal_manifest_sha256"
        ],
        "zero_new_activity": _zero_activity(protocol),
    }
    _reject_score_fields(document)
    return document


def _run_stage_a(
    root: Path,
    protocol: Mapping[str, Any],
    module: Any,
    requirements_path: Path,
    *,
    recovery_seal: str,
    now: datetime,
) -> None:
    output = _assert_output_root(root, protocol)
    if any(output.iterdir()):
        raise RecoveryError("Stage-A output root is not fresh")
    before = _attempt_inventory(root, protocol)
    _assert_original_publication_absent(root)
    authorization, audit, verification, _ = _historical_recomputation(
        root, protocol, module, requirements_path
    )
    _validate_bundle_seal(root, protocol, recovery_seal)
    _validate_bound_inputs(root, protocol)
    after = _attempt_inventory(root, protocol)
    if after != before:
        raise RecoveryError("R5 attempt changed during Stage A")
    envelope = _stage_a_envelope(
        protocol,
        executed_at=now,
        authorization_payload=authorization,
        audit_payload=audit,
        verification_payload=verification,
    )
    _write_new_bytes(
        _stage_a_path(root, protocol), _canonical_json(envelope), output_root=output
    )


def _load_stage_a(
    root: Path,
    protocol: Mapping[str, Any],
    module: Any,
    requirements_path: Path,
) -> tuple[bytes, dict[str, Any], Path, bytes, dict[str, Any]]:
    expected_authorization, expected_audit, expected_verification, verification = (
        _historical_recomputation(root, protocol, module, requirements_path)
    )
    path = _stage_a_path(root, protocol)
    envelope, payload = _load_json(path)
    _require_immutable(path, maximum=MAX_JSON_BYTES, exact_mode=0o444)
    _require_exact_keys(
        envelope,
        {
            "benchmark_scope",
            "credential_recorded",
            "cross_attempt_combination",
            "executed_at_utc",
            "external_result_or_score_input",
            "historical_authorization_reconstruction_sha256",
            "historical_independent_audit_reconstruction_sha256",
            "historical_raw_verification_payload_sha256",
            "historical_replay_at_utc",
            "model_content_recorded",
            "present_time_freshness_assessed",
            "present_time_freshness_claimed",
            "present_time_freshness_valid",
            "r5_status",
            "recovery_status",
            "schema_version",
            "score_bearing_fields_published",
            "sole_source_attempt_root",
            "source_attempt_tree_fingerprint_sha256",
            "source_r5_precommit_sha256",
            "zero_new_activity",
        },
        label="Stage-A envelope",
    )
    _reject_score_fields(envelope)
    fixed = dict(envelope)
    executed_at = _parse_timestamp(fixed.pop("executed_at_utc"))
    expected = _stage_a_envelope(
        protocol,
        executed_at=executed_at,
        authorization_payload=expected_authorization,
        audit_payload=expected_audit,
        verification_payload=expected_verification,
    )
    if envelope != expected or _canonical_json(envelope) != payload:
        raise RecoveryError("Stage-A envelope changed")
    return payload, envelope, path, expected_verification, verification


def _validate_review(
    document: Mapping[str, Any],
    *,
    path: Path,
    recovery_seal: str,
    stage_a_sha: str,
    stage_a_created: datetime,
    stage_a_mtime_ns: int,
    terminal_sha: str,
    expected_reviewer: Mapping[str, str],
    source_fingerprint: str,
    now: datetime,
    freshness_seconds: int,
) -> tuple[str, datetime]:
    _, raw_payload = _load_json(path)
    if _canonical_json(document) != raw_payload:
        raise RecoveryError("GO review is not canonical JSON")
    _require_exact_keys(
        document,
        {
            "created_at_utc",
            "credential_recorded",
            "decision",
            "model_content_recorded",
            "no_score_read",
            "recovery_precommit_sha256",
            "review_authority",
            "reviewer_codename",
            "reviewer_id",
            "schema_version",
            "score_blind",
            "source_attempt_tree_fingerprint_sha256",
            "stage_a_envelope_sha256",
            "terminal_failure_record_sha256",
        },
        label="GO review",
    )
    created = _parse_timestamp(document["created_at_utc"])
    _reject_score_fields(document)
    metadata = path.stat(follow_symlinks=False)
    if (
        document["schema_version"] != REVIEW_SCHEMA
        or document["decision"] != "GO"
        or document["score_blind"] is not True
        or document["no_score_read"] is not True
        or document["credential_recorded"] is not False
        or document["model_content_recorded"] is not False
        or document["recovery_precommit_sha256"] != recovery_seal
        or document["stage_a_envelope_sha256"] != stage_a_sha
        or document["terminal_failure_record_sha256"] != terminal_sha
        or document["source_attempt_tree_fingerprint_sha256"] != source_fingerprint
        or document["reviewer_id"] != expected_reviewer["reviewer_id"]
        or document["reviewer_codename"] != expected_reviewer["codename"]
        or document["review_authority"] != expected_reviewer["authority"]
        or created <= stage_a_created
        or created > now
        or (now - created).total_seconds() > freshness_seconds
        or metadata.st_mtime_ns <= stage_a_mtime_ns
    ):
        raise RecoveryError("independent GO review failed")
    _require_immutable(path, maximum=MAX_JSON_BYTES, exact_mode=0o444)
    return document["reviewer_id"], created


def _validate_go(
    root: Path,
    protocol: Mapping[str, Any],
    *,
    recovery_seal: str,
    stage_a_payload: bytes,
    stage_a_document: Mapping[str, Any],
    stage_a_path: Path,
    now: datetime,
) -> tuple[str, dict[str, Any], list[bytes], bytes]:
    policy = protocol["go_policy"]
    freshness = policy["freshness_seconds"]
    if not isinstance(freshness, int) or freshness <= 0:
        raise RecoveryError("invalid GO freshness")
    terminal_sha = protocol["terminal_failure_record"]["record_sha256"]
    stage_a_sha = _sha256(stage_a_payload)
    stage_a_created = _parse_timestamp(stage_a_document["executed_at_utc"])
    stage_a_mtime_ns = stage_a_path.stat(follow_symlinks=False).st_mtime_ns
    source_fingerprint = protocol["attempt_preservation"]["tree_fingerprint_sha256"]
    review_records: list[dict[str, str]] = []
    review_payloads: list[bytes] = []
    reviewer_ids: list[str] = []
    review_times: list[datetime] = []
    review_mtimes: list[int] = []
    reviewers = policy["reviewers"]
    if (
        not isinstance(reviewers, list)
        or len(reviewers) != 2
        or [item.get("path") for item in reviewers] != policy["review_paths"]
    ):
        raise RecoveryError("predeclared reviewers changed")
    for expected_reviewer in reviewers:
        relative = expected_reviewer["path"]
        path = _repository_path(root, relative)
        document, payload = _load_json(path)
        reviewer, created = _validate_review(
            document,
            path=path,
            recovery_seal=recovery_seal,
            stage_a_sha=stage_a_sha,
            stage_a_created=stage_a_created,
            stage_a_mtime_ns=stage_a_mtime_ns,
            terminal_sha=terminal_sha,
            expected_reviewer=expected_reviewer,
            source_fingerprint=source_fingerprint,
            now=now,
            freshness_seconds=freshness,
        )
        reviewer_ids.append(reviewer)
        review_times.append(created)
        review_mtimes.append(path.stat(follow_symlinks=False).st_mtime_ns)
        review_payloads.append(payload)
        review_records.append(
            {
                "path": relative,
                "reviewer_id": reviewer,
                "sha256": _sha256(payload),
            }
        )
    if len(reviewer_ids) != 2 or len(set(reviewer_ids)) != 2:
        raise RecoveryError("GO reviews are not independent")
    go_path = _repository_path(root, policy["aggregate_path"])
    go, go_payload = _load_json(go_path)
    if _canonical_json(go) != go_payload:
        raise RecoveryError("aggregate GO is not canonical JSON")
    _require_exact_keys(
        go,
        {
            "created_at_utc",
            "credential_recorded",
            "go",
            "model_content_recorded",
            "no_score_read",
            "recovery_precommit_sha256",
            "reviews",
            "schema_version",
            "score_blind",
            "source_attempt_tree_fingerprint_sha256",
            "stage_a_envelope_sha256",
            "terminal_failure_record_sha256",
        },
        label="aggregate GO",
    )
    created = _parse_timestamp(go["created_at_utc"])
    _reject_score_fields(go)
    metadata = go_path.stat(follow_symlinks=False)
    if (
        go["schema_version"] != GO_SCHEMA
        or go["go"] is not True
        or go["score_blind"] is not True
        or go["no_score_read"] is not True
        or go["credential_recorded"] is not False
        or go["model_content_recorded"] is not False
        or go["recovery_precommit_sha256"] != recovery_seal
        or go["stage_a_envelope_sha256"] != stage_a_sha
        or go["terminal_failure_record_sha256"] != terminal_sha
        or go["source_attempt_tree_fingerprint_sha256"] != source_fingerprint
        or go["reviews"] != review_records
        or created <= stage_a_created
        or created < max(review_times)
        or created > now
        or (now - created).total_seconds() > freshness
        or metadata.st_mtime_ns < max(review_mtimes)
        or metadata.st_mtime_ns <= stage_a_mtime_ns
    ):
        raise RecoveryError("aggregate GO failed")
    _require_immutable(go_path, maximum=MAX_JSON_BYTES, exact_mode=0o444)
    return _sha256(go_payload), go, review_payloads, go_payload


def _evaluation_audit_payload(
    root: Path,
    requirements: Mapping[str, Any],
    module: Any,
    variant: Mapping[str, Any],
) -> bytes:
    _, _, working = module._verify_copy_manifest(
        root, variant, verify_evaluated_files=False
    )
    evaluated, _ = module._scored_tree_evidence(root, variant, working)
    frozen = module._repository_path(
        root, variant["staged_prediction_directory"], label="frozen prediction directory"
    )
    question_scope = module._question_scope_path(root, variant, working)
    ledger = module._repository_path(
        root, variant["ledger_path"], label="usage ledger"
    )
    evaluator_log = module._repository_path(
        root,
        f"{variant['run_root']}/evaluation/evaluate.log",
        label="evaluator log",
    )
    runtime_policy = requirements["runtime_sources"]["v11-source"]
    runtime_root = module._repository_path(
        root, runtime_policy["extracted_root"], label="verified V11 runtime source"
    )
    auditor = runtime_root / "narratordb/benchmarks/evaluation_audit.py"
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
    if result.returncode != 0 or not result.stdout:
        raise RecoveryError("evaluation audit generation failed")
    return result.stdout


def _private_evaluation_audit(
    root: Path,
    requirements: Mapping[str, Any],
    module: Any,
    variant: Mapping[str, Any],
    temporary: Path,
    *,
    label: str,
) -> tuple[bytes, dict[str, dict[str, Any]], dict[str, str]]:
    payload = _evaluation_audit_payload(root, requirements, module, variant)
    path = temporary / f"{variant['label']}-evaluation-audit.json"
    _write_private_file(path, payload)
    os.chmod(path, 0o444)
    document, recomputed_payload, metrics, evidence = module._recompute_evaluation_audit(
        root, requirements, variant, path, label=label
    )
    if recomputed_payload != payload or module._canonical_json(document) != payload:
        raise RecoveryError("evaluation audit is not byte-identical")
    return payload, metrics, evidence


def _result_document(
    protocol: Mapping[str, Any],
    verification_payload: bytes,
    verification: Mapping[str, Any],
    *,
    recovery_seal: str,
    go_sha: str,
    review_shas: Sequence[str],
    v7_payload: bytes,
    v13_payload: bytes,
    v7_metrics: Mapping[str, Mapping[str, Any]],
    v13_metrics: Mapping[str, Mapping[str, Any]],
    v7_evidence: Mapping[str, str],
    v13_evidence: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "benchmark_scope": "consumed-development",
        "classification": protocol["release_classification"],
        "credential_recorded": False,
        "cross_attempt_combination": False,
        "cutoffs": [20, 50],
        "delta_correct": {
            cutoff: v13_metrics[cutoff]["correct"] - v7_metrics[cutoff]["correct"]
            for cutoff in ("top_20", "top_50")
        },
        "denominator": 42,
        "evaluation_audit_sha256": {
            "v13_first": _sha256(v13_payload),
            "v7_control": _sha256(v7_payload),
        },
        "evaluation_evidence_sha256": {
            "v13_first": dict(v13_evidence),
            "v7_control": dict(v7_evidence),
        },
        "final_spend_authorization_sha256": verification["authorization_sha256"],
        "external_result_or_score_input": False,
        "historical_final_verification_sha256": _sha256(verification_payload),
        "historical_replay_at_utc": protocol["historical_finalization"][
            "verification_now_utc"
        ],
        "model_content_recorded": False,
        "original_r5_status": "terminal-finalization-failure",
        "present_time_freshness_claimed": False,
        "recovery_go_sha256": go_sha,
        "recovery_precommit_sha256": recovery_seal,
        "recovery_review_sha256": list(review_shas),
        "recovery_status": "offline-recovered-from-sole-terminal-r5-source",
        "revision_precommit_sha256": verification["revision_precommit_sha256"],
        "r5_protocol_status": "terminal-finalization-failure",
        "schema_version": RESULT_SCHEMA,
        "score_release_authorized_by_offline_recovery": True,
        "sole_source_attempt_root": protocol["sole_source_policy"][
            "sole_source_attempt_root"
        ],
        "source_attempt_tree_fingerprint_sha256": protocol["attempt_preservation"][
            "tree_fingerprint_sha256"
        ],
        "terminal_failure_record_sha256": protocol["terminal_failure_record"][
            "record_sha256"
        ],
        "v13_first": dict(v13_metrics),
        "v7_control": dict(v7_metrics),
        "zero_new_activity": _zero_activity(protocol),
    }


def _release_directory_candidate(root: Path, protocol: Mapping[str, Any]) -> Path:
    return _repository_path(
        root,
        protocol["output"]["stage_b"]["release_directory_path"],
        must_exist=False,
    )


def _stage_b_layout(
    root: Path, protocol: Mapping[str, Any]
) -> tuple[Path, dict[str, Path]]:
    paths = protocol["output"]["stage_b"]
    release = _release_directory_candidate(root, protocol)
    layout = {
        "RECOVERED_PAIRED_RESULT_SHA256SUMS": _repository_path(
            root, paths["result_checksum_path"], must_exist=False
        ),
        "recovered-paired-result.json": _repository_path(
            root, paths["result_path"], must_exist=False
        ),
        "v7-evaluation-audit.json": _repository_path(
            root, paths["v7_evaluation_audit_path"], must_exist=False
        ),
        "v13-evaluation-audit.json": _repository_path(
            root, paths["v13_evaluation_audit_path"], must_exist=False
        ),
        "recovery-review-1.json": _repository_path(
            root, paths["review_1_copy_path"], must_exist=False
        ),
        "recovery-review-2.json": _repository_path(
            root, paths["review_2_copy_path"], must_exist=False
        ),
        "recovery-go.json": _repository_path(
            root, paths["go_copy_path"], must_exist=False
        ),
        "release-complete.json": _repository_path(
            root, paths["completion_path"], must_exist=False
        ),
    }
    if any(path.parent != release or path.name != name for name, path in layout.items()):
        raise RecoveryError("atomic release layout changed")
    return release, layout


def _destroy_private_staging(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    for current, directories, files in os.walk(path, topdown=False):
        current_path = Path(current)
        os.chmod(current_path, 0o700)
        for name in files:
            candidate = current_path / name
            if candidate.is_symlink():
                candidate.unlink()
            else:
                os.chmod(candidate, 0o600)
                candidate.unlink()
        for name in directories:
            candidate = current_path / name
            if candidate.is_symlink():
                candidate.unlink()
            else:
                os.chmod(candidate, 0o700)
                candidate.rmdir()
    path.rmdir()


def _rename_exclusive(source: Path, destination: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameatx_np", None)
    if rename is None:
        raise RecoveryError("exclusive atomic rename is unavailable")
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    ctypes.set_errno(0)
    if (
        rename(
            AT_FDCWD,
            os.fsencode(source),
            AT_FDCWD,
            os.fsencode(destination),
            RENAME_EXCL,
        )
        != 0
    ):
        raise RecoveryError("exclusive atomic release commit failed")


def _publish_release(
    output: Path,
    *,
    release: Path,
    payloads: Mapping[str, bytes],
) -> None:
    global _RELEASE_COMMITTED

    expected_order = [
        "RECOVERED_PAIRED_RESULT_SHA256SUMS",
        "recovered-paired-result.json",
        "v7-evaluation-audit.json",
        "v13-evaluation-audit.json",
        "recovery-review-1.json",
        "recovery-review-2.json",
        "recovery-go.json",
        "release-complete.json",
    ]
    if list(payloads) != expected_order or release.parent != output:
        raise RecoveryError("atomic release payload inventory changed")
    if release.exists() or release.is_symlink():
        raise RecoveryError("atomic release destination already exists")
    staging = Path(tempfile.mkdtemp(prefix=".release-private-", dir=output))
    committed = False
    try:
        metadata = staging.stat(follow_symlinks=False)
        if (
            stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_dev != output.stat(follow_symlinks=False).st_dev
        ):
            raise RecoveryError("private release staging is not same-filesystem 0700")
        for name, payload in payloads.items():
            _write_new_bytes(staging / name, payload, output_root=staging)
        os.chmod(staging, 0o555)
        _fsync_directory(staging)
        _fsync_directory(output)
        blocked = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
        try:
            _rename_exclusive(staging, release)
            committed = True
            _RELEASE_COMMITTED = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        # The exclusive rename is the irrevocable release commit.  Durability and
        # output-root hardening below are best-effort and can never contradict it.
        try:
            _fsync_directory(output)
            os.chmod(output, 0o555)
            _fsync_directory(output.parent)
        except OSError:
            pass
    finally:
        if not committed and staging.exists():
            _destroy_private_staging(staging)


def _completion_document(
    protocol: Mapping[str, Any],
    *,
    result_sha: str,
    go_sha: str,
    review_shas: Sequence[str],
    recovery_seal: str,
    v7_payload: bytes,
    v13_payload: bytes,
) -> dict[str, Any]:
    return {
        "benchmark_scope": "consumed-development",
        "credential_recorded": False,
        "evaluation_audit_sha256": {
            "v13_first": _sha256(v13_payload),
            "v7_control": _sha256(v7_payload),
        },
        "historical_replay_at_utc": protocol["historical_finalization"][
            "verification_now_utc"
        ],
        "model_content_recorded": False,
        "original_r5_status": "terminal-finalization-failure",
        "present_time_freshness_claimed": False,
        "recovered_paired_result_sha256": result_sha,
        "recovery_go_sha256": go_sha,
        "recovery_precommit_sha256": recovery_seal,
        "recovery_review_sha256": list(review_shas),
        "schema_version": COMPLETE_SCHEMA,
        "status": "offline-recovery-release-complete",
        "stdout_bytes": 0,
        "source_attempt_tree_fingerprint_sha256": protocol["attempt_preservation"][
            "tree_fingerprint_sha256"
        ],
        "zero_new_activity": _zero_activity(protocol),
    }


def _validate_release_go_copies(
    protocol: Mapping[str, Any],
    layout: Mapping[str, Path],
    *,
    recovery_seal: str,
    stage_a_payload: bytes,
    stage_a_document: Mapping[str, Any],
) -> tuple[str, list[str]]:
    stage_a_sha = _sha256(stage_a_payload)
    stage_a_created = _parse_timestamp(stage_a_document["executed_at_utc"])
    terminal_sha = protocol["terminal_failure_record"]["record_sha256"]
    fingerprint = protocol["attempt_preservation"]["tree_fingerprint_sha256"]
    review_records: list[dict[str, str]] = []
    review_shas: list[str] = []
    review_times: list[datetime] = []
    for index, expected in enumerate(protocol["go_policy"]["reviewers"], start=1):
        path = layout[f"recovery-review-{index}.json"]
        document, payload = _load_json(path)
        if _canonical_json(document) != payload:
            raise RecoveryError("committed review copy is not canonical")
        _require_exact_keys(
            document,
            {
                "created_at_utc",
                "credential_recorded",
                "decision",
                "model_content_recorded",
                "no_score_read",
                "recovery_precommit_sha256",
                "review_authority",
                "reviewer_codename",
                "reviewer_id",
                "schema_version",
                "score_blind",
                "source_attempt_tree_fingerprint_sha256",
                "stage_a_envelope_sha256",
                "terminal_failure_record_sha256",
            },
            label="committed review copy",
        )
        _reject_score_fields(document)
        created = _parse_timestamp(document["created_at_utc"])
        if (
            document["schema_version"] != REVIEW_SCHEMA
            or document["decision"] != "GO"
            or document["score_blind"] is not True
            or document["no_score_read"] is not True
            or document["credential_recorded"] is not False
            or document["model_content_recorded"] is not False
            or document["recovery_precommit_sha256"] != recovery_seal
            or document["stage_a_envelope_sha256"] != stage_a_sha
            or document["terminal_failure_record_sha256"] != terminal_sha
            or document["source_attempt_tree_fingerprint_sha256"] != fingerprint
            or document["reviewer_id"] != expected["reviewer_id"]
            or document["reviewer_codename"] != expected["codename"]
            or document["review_authority"] != expected["authority"]
            or created <= stage_a_created
        ):
            raise RecoveryError("committed review copy changed")
        digest = _sha256(payload)
        review_times.append(created)
        review_shas.append(digest)
        review_records.append(
            {
                "path": expected["path"],
                "reviewer_id": expected["reviewer_id"],
                "sha256": digest,
            }
        )
    go, go_payload = _load_json(layout["recovery-go.json"])
    if _canonical_json(go) != go_payload:
        raise RecoveryError("committed GO copy is not canonical")
    _require_exact_keys(
        go,
        {
            "created_at_utc",
            "credential_recorded",
            "go",
            "model_content_recorded",
            "no_score_read",
            "recovery_precommit_sha256",
            "reviews",
            "schema_version",
            "score_blind",
            "source_attempt_tree_fingerprint_sha256",
            "stage_a_envelope_sha256",
            "terminal_failure_record_sha256",
        },
        label="committed GO copy",
    )
    _reject_score_fields(go)
    go_created = _parse_timestamp(go["created_at_utc"])
    if (
        go["schema_version"] != GO_SCHEMA
        or go["go"] is not True
        or go["score_blind"] is not True
        or go["no_score_read"] is not True
        or go["credential_recorded"] is not False
        or go["model_content_recorded"] is not False
        or go["recovery_precommit_sha256"] != recovery_seal
        or go["stage_a_envelope_sha256"] != stage_a_sha
        or go["terminal_failure_record_sha256"] != terminal_sha
        or go["source_attempt_tree_fingerprint_sha256"] != fingerprint
        or go["reviews"] != review_records
        or go_created < max(review_times)
    ):
        raise RecoveryError("committed GO copy changed")
    return _sha256(go_payload), review_shas


def _validate_committed_release(
    root: Path,
    protocol: Mapping[str, Any],
    module: Any,
    requirements_path: Path,
    *,
    recovery_seal: str,
) -> None:
    _validate_bundle_seal(root, protocol, recovery_seal)
    _validate_bound_inputs(root, protocol)
    _attempt_inventory(root, protocol)
    _assert_original_publication_absent(root)
    output = _assert_output_root(root, protocol, writable=False)
    release, layout = _stage_b_layout(root, protocol)
    metadata = release.stat(follow_symlinks=False)
    if (
        release.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o555
        or metadata.st_dev != output.stat(follow_symlinks=False).st_dev
        or {path.name for path in release.iterdir()} != set(layout)
        or {path.name for path in output.iterdir()}
        != {_stage_a_path(root, protocol).name, release.name}
    ):
        raise RecoveryError("committed release namespace is not closed-world")
    payloads = {
        name: _require_immutable(
            path, maximum=MAX_INPUT_BYTES, exact_mode=0o444
        )
        for name, path in layout.items()
    }
    stage_a_payload, stage_a_document, _, verification_payload, verification = (
        _load_stage_a(root, protocol, module, requirements_path)
    )
    go_sha, review_shas = _validate_release_go_copies(
        protocol,
        layout,
        recovery_seal=recovery_seal,
        stage_a_payload=stage_a_payload,
        stage_a_document=stage_a_document,
    )
    requirements, _ = module._requirements(root, requirements_path)
    variants = module._variant_map(root, requirements)
    _, v7_payload, v7_metrics, v7_evidence = module._recompute_evaluation_audit(
        root,
        requirements,
        variants["v7-control"],
        layout["v7-evaluation-audit.json"],
        label="committed V7 recovery evaluation audit",
    )
    _, v13_payload, v13_metrics, v13_evidence = module._recompute_evaluation_audit(
        root,
        requirements,
        variants["v13-first"],
        layout["v13-evaluation-audit.json"],
        label="committed V13 recovery evaluation audit",
    )
    expected_result = _result_document(
        protocol,
        verification_payload,
        verification,
        recovery_seal=recovery_seal,
        go_sha=go_sha,
        review_shas=review_shas,
        v7_payload=v7_payload,
        v13_payload=v13_payload,
        v7_metrics=v7_metrics,
        v13_metrics=v13_metrics,
        v7_evidence=v7_evidence,
        v13_evidence=v13_evidence,
    )
    result_payload = _canonical_json(expected_result)
    if payloads["recovered-paired-result.json"] != result_payload:
        raise RecoveryError("committed recovered result changed")
    result_sha = _sha256(result_payload)
    if payloads["RECOVERED_PAIRED_RESULT_SHA256SUMS"] != (
        f"{result_sha}  recovered-paired-result.json\n".encode("ascii")
    ):
        raise RecoveryError("committed result checksum changed")
    expected_completion = _completion_document(
        protocol,
        result_sha=result_sha,
        go_sha=go_sha,
        review_shas=review_shas,
        recovery_seal=recovery_seal,
        v7_payload=v7_payload,
        v13_payload=v13_payload,
    )
    if payloads["release-complete.json"] != _canonical_json(expected_completion):
        raise RecoveryError("committed completion record changed")


def _remove_orphan_release_staging(output: Path) -> bool:
    found = False
    for path in output.iterdir():
        if not path.name.startswith(".release-private-"):
            continue
        if path.is_symlink() or not path.is_dir():
            raise RecoveryError("invalid orphan release staging")
        _destroy_private_staging(path)
        found = True
    return found


def _run_stage_b(
    root: Path,
    protocol: Mapping[str, Any],
    module: Any,
    requirements_path: Path,
    *,
    recovery_seal: str,
    now: datetime,
) -> None:
    global _RELEASE_COMMITTED

    output = _assert_output_root(root, protocol, writable=False)
    release_candidate = _release_directory_candidate(root, protocol)
    if release_candidate.exists() or release_candidate.is_symlink():
        _validate_committed_release(
            root,
            protocol,
            module,
            requirements_path,
            recovery_seal=recovery_seal,
        )
        _RELEASE_COMMITTED = True
        return
    release, release_layout = _stage_b_layout(root, protocol)
    if stat.S_IMODE(output.stat(follow_symlinks=False).st_mode) != 0o700:
        raise RecoveryError("uncommitted Stage-B output root is not writable")
    if _remove_orphan_release_staging(output):
        raise RecoveryError("precommit crash staging destroyed; recovery-r1 is terminal")
    terminal_path = _repository_path(
        root, protocol["output"]["failed_status_path"], must_exist=False
    )
    if terminal_path.exists() or terminal_path.is_symlink():
        raise RecoveryError("recovery-r1 is already terminal")
    stage_a_expected = _stage_a_path(root, protocol).name
    if {path.name for path in output.iterdir()} != {stage_a_expected}:
        raise RecoveryError("pre-Stage-B output inventory changed")
    _validate_bundle_seal(root, protocol, recovery_seal)
    _validate_bound_inputs(root, protocol)
    before = _attempt_inventory(root, protocol)
    _assert_original_publication_absent(root)
    (
        stage_a_payload,
        stage_a_document,
        stage_a_path,
        verification_payload,
        verification,
    ) = _load_stage_a(
        root, protocol, module, requirements_path
    )
    go_sha, _, review_payloads, go_payload = _validate_go(
        root,
        protocol,
        recovery_seal=recovery_seal,
        stage_a_payload=stage_a_payload,
        stage_a_document=stage_a_document,
        stage_a_path=stage_a_path,
        now=now,
    )
    review_shas = [_sha256(payload) for payload in review_payloads]
    requirements, _ = module._requirements(root, requirements_path)
    variants = module._variant_map(root, requirements)
    scratch = Path(os.environ["TMPDIR"]).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="narratordb-r5-release-", dir=scratch) as name:
        temporary = Path(name)
        if stat.S_IMODE(temporary.stat().st_mode) != 0o700:
            raise RecoveryError("score-bearing temporary directory is not private")
        v7_payload, v7_metrics, v7_evidence = _private_evaluation_audit(
            root,
            requirements,
            module,
            variants["v7-control"],
            temporary,
            label="V7 recovery evaluation audit",
        )
        v13_payload, v13_metrics, v13_evidence = _private_evaluation_audit(
            root,
            requirements,
            module,
            variants["v13-first"],
            temporary,
            label="V13 recovery evaluation audit",
        )
        result = _result_document(
            protocol,
            verification_payload,
            verification,
            recovery_seal=recovery_seal,
            go_sha=go_sha,
            review_shas=review_shas,
            v7_payload=v7_payload,
            v13_payload=v13_payload,
            v7_metrics=v7_metrics,
            v13_metrics=v13_metrics,
            v7_evidence=v7_evidence,
            v13_evidence=v13_evidence,
        )
        result_payload = _canonical_json(result)
        result_sha = _sha256(result_payload)
        checksum_payload = f"{result_sha}  recovered-paired-result.json\n".encode("ascii")
        completion = _completion_document(
            protocol,
            result_sha=result_sha,
            go_sha=go_sha,
            review_shas=review_shas,
            recovery_seal=recovery_seal,
            v7_payload=v7_payload,
            v13_payload=v13_payload,
        )
        completion_payload = _canonical_json(completion)
        _validate_bundle_seal(root, protocol, recovery_seal)
        _validate_bound_inputs(root, protocol)
        after = _attempt_inventory(root, protocol)
        if after != before:
            raise RecoveryError("R5 attempt changed during Stage B")
    payloads = {
        "RECOVERED_PAIRED_RESULT_SHA256SUMS": checksum_payload,
        "recovered-paired-result.json": result_payload,
        "v7-evaluation-audit.json": v7_payload,
        "v13-evaluation-audit.json": v13_payload,
        "recovery-review-1.json": review_payloads[0],
        "recovery-review-2.json": review_payloads[1],
        "recovery-go.json": go_payload,
        "release-complete.json": completion_payload,
    }
    if {name: release / name for name in payloads} != release_layout:
        raise RecoveryError("atomic release protocol paths changed")
    _publish_release(output, release=release, payloads=payloads)


def _arguments(argv: Sequence[str]) -> tuple[str, Path, Path, str]:
    if len(argv) != 7:
        raise RecoveryError("invalid arguments")
    stage = argv[0]
    if stage not in {"stage-a", "stage-b"}:
        raise RecoveryError("invalid stage")
    expected_flags = [
        "--repository-root",
        "--protocol",
        "--published-recovery-seal-sha256",
    ]
    if [argv[1], argv[3], argv[5]] != expected_flags:
        raise RecoveryError("invalid arguments")
    return stage, Path(argv[2]), Path(argv[4]), argv[6]


def _interrupt(_signum: int, _frame: Any) -> None:
    raise RecoveryError("interrupted recovery")


def main(argv: Sequence[str] | None = None) -> int:
    global _RELEASE_COMMITTED

    _RELEASE_COMMITTED = False
    arguments = list(sys.argv[1:] if argv is None else argv)
    root = Path("/Users/william/Desktop/narratorDB")
    protocol: dict[str, Any] | None = None
    module: Any | None = None
    requirements_path: Path | None = None
    stage = "preflight"
    published_seal = ""
    old_umask = os.umask(0o077)
    try:
        stage, requested_root, requested_protocol, published_seal = _arguments(arguments)
        root = requested_root.resolve(strict=True)
        if root != Path("/Users/william/Desktop/narratorDB"):
            raise RecoveryError("repository root changed")
        _validate_clean_environment()
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, _interrupt)
        sys.addaudithook(_network_audit)
        protocol, _ = _load_protocol(root, requested_protocol)
        _validate_bundle_seal(root, protocol, published_seal)
        _validate_bound_inputs(root, protocol)
        python_real = _repository_path(
            Path("/"), protocol["execution_environment"]["python_real_path"].lstrip("/")
        )
        if _sha256(_stable_bytes(python_real)) != protocol["execution_environment"][
            "python_real_sha256"
        ]:
            raise RecoveryError("Python runtime changed")
        _install_subprocess_guard(python_real)
        module, _, requirements_path = _load_sealed_verifier(root, protocol)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        if stage == "stage-a":
            _run_stage_a(
                root,
                protocol,
                module,
                requirements_path,
                recovery_seal=published_seal,
                now=now,
            )
        else:
            _run_stage_b(
                root,
                protocol,
                module,
                requirements_path,
                recovery_seal=published_seal,
                now=now,
            )
        return 0
    except BaseException:
        if _RELEASE_COMMITTED:
            return 0
        if stage == "stage-b" and protocol is not None:
            try:
                release = _release_directory_candidate(root, protocol)
                release_present = release.exists() or release.is_symlink()
            except BaseException:
                release_present = False
            if release_present:
                if module is None or requirements_path is None:
                    return 1
                try:
                    _validate_committed_release(
                        root,
                        protocol,
                        module,
                        requirements_path,
                        recovery_seal=published_seal,
                    )
                    _RELEASE_COMMITTED = True
                    return 0
                except BaseException:
                    return 1
        _terminalize(
            root,
            protocol,
            stage=stage,
            published_seal=published_seal,
        )
        return 1
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    raise SystemExit(main())
