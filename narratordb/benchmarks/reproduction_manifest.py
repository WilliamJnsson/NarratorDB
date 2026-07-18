#!/usr/bin/env python3
"""Create and verify reproducible, secret-safe benchmark run manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Mapping, Sequence

from .history import SECRET_PATTERNS


PREFLIGHT_SCHEMA = "narratordb.benchmark-reproduction-preflight.v1"
SEALED_SCHEMA = "narratordb.benchmark-reproduction-sealed.v1"
DOCUMENT_HASH_FIELD = "document_sha256"
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_STEP_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_FRESH_ROLE = re.compile(r"[a-z][a-z0-9_-]*")
_SENSITIVE_CONFIG_KEY = re.compile(
    r"(?:^|_)(?:api_key|access_token|bearer_token|authorization|password|secret|credential)(?:$|_)"
)
_SENSITIVE_COMMAND_FLAG = re.compile(
    r"^--?(?:(?:[a-z0-9]+[-_])*(?:api[-_]?key|access[-_]?token|bearer[-_]?token|auth[-_]?token|authorization|password|secret|credential)|token)(?:[-_]|=|$)",
    re.IGNORECASE,
)
_SENSITIVE_ENV_ASSIGNMENT = re.compile(
    r"(?:^|\s)(?:[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|BEARER_TOKEN|PASSWORD|SECRET|CREDENTIAL|AUTHORIZATION))=",
    re.IGNORECASE,
)
_ARCHIVE_SUFFIXES = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".zip",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_timestamp(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{label} must be a UTC ISO-8601 timestamp ending in Z")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ValueError(f"invalid {label}: {value}") from error


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as error:
        raise ValueError(f"JSON file is not UTF-8: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON file {path}: {error}") from error


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _document_hash(document: Mapping[str, Any]) -> str:
    payload = dict(document)
    payload.pop(DOCUMENT_HASH_FIELD, None)
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _attach_document_hash(document: dict[str, Any]) -> dict[str, Any]:
    if DOCUMENT_HASH_FIELD in document:
        raise ValueError(f"reserved document field: {DOCUMENT_HASH_FIELD}")
    document[DOCUMENT_HASH_FIELD] = _document_hash(document)
    return document


def _verify_document_hash(document: Mapping[str, Any]) -> None:
    declared = document.get(DOCUMENT_HASH_FIELD)
    if not isinstance(declared, str) or not _HEX_SHA256.fullmatch(declared):
        raise ValueError("manifest has no valid document_sha256")
    actual = _document_hash(document)
    if declared != actual:
        raise ValueError(
            f"manifest document checksum mismatch: expected {declared}, got {actual}"
        )


def _write_new_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    file_descriptor: int | None = None
    created = False
    try:
        file_descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        created = True
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as target:
            file_descriptor = None
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if created:
            path.unlink(missing_ok=True)
        raise


def _repository_path(
    value: Path | str,
    repository_root: Path,
    *,
    label: str,
    must_exist: bool | None = None,
) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repository_root / path
    path = path.resolve(strict=False)
    try:
        path.relative_to(repository_root)
    except ValueError as error:
        raise ValueError(f"{label} is outside the repository: {value}") from error
    if must_exist is True and not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    if must_exist is False and path.exists():
        raise FileExistsError(f"{label} must be fresh but already exists: {path}")
    return path


def _relative_path(path: Path, repository_root: Path) -> str:
    return path.relative_to(repository_root).as_posix()


def _manifest_path(value: object, repository_root: Path, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"manifest {label} path must be a non-empty string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe manifest {label} path: {value}")
    return _repository_path(path, repository_root, label=label)


def _regular_file(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{label} may not be a symlink: {path}")
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except FileNotFoundError as error:
        raise FileNotFoundError(f"{label} not found: {path}") from error
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a regular file: {path}")


def _scan_stream_for_secrets(source: BinaryIO, display_name: str) -> None:
    carry = b""
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        searchable = carry + chunk
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(searchable):
                raise ValueError(
                    f"{label} detected in benchmark material: {display_name}"
                )
        carry = searchable[-256:]


def _hash_and_scan_file(path: Path, display_name: str) -> tuple[str, int]:
    _regular_file(path, display_name)
    before = path.stat(follow_symlinks=False)
    digest = hashlib.sha256()
    size = 0
    carry = b""
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            searchable = carry + chunk
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(searchable):
                    raise ValueError(
                        f"{label} detected in benchmark material: {display_name}"
                    )
            carry = searchable[-256:]
    after = path.stat(follow_symlinks=False)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or size != after.st_size:
        raise RuntimeError(f"file changed while being hashed: {display_name}")
    return digest.hexdigest(), size


def _safe_archive_member(name: str, archive: Path) -> None:
    member = PurePosixPath(name)
    if not name or member.is_absolute() or ".." in member.parts:
        raise ValueError(f"unsafe member path in source archive {archive.name}: {name}")


def _scan_tar(path: Path) -> None:
    try:
        with tarfile.open(path, mode="r:*") as archive:
            for member in archive:
                _safe_archive_member(member.name, path)
                if member.issym() or member.islnk():
                    raise ValueError(
                        f"archive may not contain links: {path.name}:{member.name}"
                    )
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError(
                        "archive may contain only regular files and directories: "
                        f"{path.name}:{member.name}"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(
                        f"could not read archive member: {path.name}:{member.name}"
                    )
                with source:
                    _scan_stream_for_secrets(source, f"{path.name}:{member.name}")
    except tarfile.TarError as error:
        raise ValueError(f"invalid tar source archive: {path}") from error


def _zip_member_is_link(member: zipfile.ZipInfo) -> bool:
    unix_mode = member.external_attr >> 16
    return bool(unix_mode) and stat.S_ISLNK(unix_mode)


def _scan_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                _safe_archive_member(member.filename, path)
                if _zip_member_is_link(member):
                    raise ValueError(
                        f"archive may not contain links: {path.name}:{member.filename}"
                    )
                if member.is_dir():
                    continue
                with archive.open(member) as source:
                    _scan_stream_for_secrets(source, f"{path.name}:{member.filename}")
    except zipfile.BadZipFile as error:
        raise ValueError(f"invalid zip source archive: {path}") from error


def _scan_recognized_archive(path: Path, *, required: bool) -> str | None:
    if tarfile.is_tarfile(path):
        _scan_tar(path)
        return "tar"
    if zipfile.is_zipfile(path):
        _scan_zip(path)
        return "zip"
    if required:
        raise ValueError(f"source archive is not a readable tar or zip archive: {path}")
    return None


def _file_record(
    path: Path,
    repository_root: Path,
    *,
    label: str,
    scan_archive: bool = False,
    require_archive: bool = False,
) -> dict[str, Any]:
    digest, size = _hash_and_scan_file(path, label)
    archive_format = None
    if require_archive or (
        scan_archive and path.name.lower().endswith(_ARCHIVE_SUFFIXES)
    ):
        archive_format = _scan_recognized_archive(path, required=require_archive)
    record: dict[str, Any] = {
        "path": _relative_path(path, repository_root),
        "sha256": digest,
        "size_bytes": size,
    }
    if archive_format is not None:
        record["archive_format"] = archive_format
        record["archive_members_secret_scanned"] = True
    return record


def _check_expected_hash(actual: str, expected: str | None, label: str) -> None:
    if expected is None:
        return
    normalized = expected.strip().lower()
    if not _HEX_SHA256.fullmatch(normalized):
        raise ValueError(
            f"expected {label} SHA-256 must contain 64 lowercase hex digits"
        )
    if actual != normalized:
        raise ValueError(
            f"{label} SHA-256 mismatch: expected {normalized}, got {actual}"
        )


def _run_git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as error:
        raise RuntimeError("git is required for harness provenance checks") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "git command failed").strip()
        raise ValueError(
            f"could not inspect harness git repository: {detail}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "timed out while inspecting harness git repository"
        ) from error
    return result.stdout.strip()


def _harness_record(harness_root: Path, repository_root: Path) -> dict[str, Any]:
    if harness_root.is_symlink() or not harness_root.is_dir():
        raise ValueError(f"harness root must be a real directory: {harness_root}")
    git_root = Path(_run_git(harness_root, "rev-parse", "--show-toplevel")).resolve(
        strict=True
    )
    try:
        git_root.relative_to(repository_root)
    except ValueError as error:
        raise ValueError(
            f"harness git repository is outside repository: {git_root}"
        ) from error
    commit = _run_git(harness_root, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError(f"invalid harness git commit: {commit}")
    status_output = _run_git(
        harness_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    clean = not bool(status_output)
    if not clean:
        raise ValueError("harness git working tree is not clean")
    return {
        "path": _relative_path(harness_root, repository_root),
        "git_root": _relative_path(git_root, repository_root),
        "commit": commit,
        "clean": True,
    }


def _dataset_record(
    path: Path, repository_root: Path
) -> tuple[dict[str, Any], set[str]]:
    _regular_file(path, "dataset")
    rows = _load_json(path)
    if not isinstance(rows, list) or not rows:
        raise ValueError("dataset must be a non-empty JSON array")
    question_ids: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"dataset row {index} is not a JSON object")
        question_id = row.get("question_id")
        if not isinstance(question_id, str) or not question_id.strip():
            raise ValueError(f"dataset row {index} has no valid question_id")
        if question_id in question_ids:
            raise ValueError(f"dataset contains duplicate question_id: {question_id}")
        question_ids.add(question_id)
    digest, size = _hash_and_scan_file(path, "dataset")
    return (
        {
            "path": _relative_path(path, repository_root),
            "sha256": digest,
            "size_bytes": size,
            "questions": len(rows),
        },
        question_ids,
    )


def _question_scope_record(
    path: Path,
    repository_root: Path,
    dataset_ids: set[str],
) -> dict[str, Any]:
    _regular_file(path, "question-ID file")
    values = _load_json(path)
    if not isinstance(values, list) or not values:
        raise ValueError("question-ID file must be a non-empty JSON array")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError("question-ID file must contain only non-empty strings")
    if values != sorted(values):
        raise ValueError("question-ID file must be sorted")
    if len(set(values)) != len(values):
        raise ValueError("question-ID file contains duplicates")
    unknown = sorted(set(values) - dataset_ids)
    if unknown:
        raise ValueError(
            f"question-ID file contains IDs absent from dataset: {unknown[:10]}"
        )
    digest, size = _hash_and_scan_file(path, "question-ID file")
    membership_hash = hashlib.sha256(_canonical_json_bytes(values)).hexdigest()
    return {
        "path": _relative_path(path, repository_root),
        "sha256": digest,
        "membership_sha256": membership_hash,
        "size_bytes": size,
        "questions": len(values),
    }


def _validate_non_secret_structure(value: Any, *, location: str = "config") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{location} has a non-string key")
            normalized = key.lower().replace("-", "_")
            if _SENSITIVE_CONFIG_KEY.search(normalized) and not normalized.endswith(
                "_env"
            ):
                raise ValueError(
                    f"{location}.{key} is a credential-bearing field and may not be recorded"
                )
            _validate_non_secret_structure(child, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_non_secret_structure(child, location=f"{location}[{index}]")
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError(f"{location} has an unsupported value type")
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(encoded):
                raise ValueError(f"{label} detected in {location}")


def _validate_no_secret_values(value: Any, *, location: str) -> None:
    """Scan serialized values without treating audit-policy field names as secrets."""

    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{location} has a non-string key")
            _validate_no_secret_values(child, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_no_secret_values(child, location=f"{location}[{index}]")
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(encoded):
                raise ValueError(f"{label} detected in {location}")
    elif not isinstance(value, (int, float, bool, type(None))):
        raise ValueError(f"{location} has an unsupported value type")


def _required_string(mapping: Mapping[str, Any], field: str, location: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}.{field} must be a non-empty string")
    return value


def _validate_actor_config(config: Mapping[str, Any], role: str) -> None:
    value = config.get(role)
    if not isinstance(value, dict):
        raise ValueError(f"config.{role} must be a JSON object")
    for field in ("model", "provider", "reasoning"):
        _required_string(value, field, f"config.{role}")


def _validate_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("config file must contain a JSON object")
    _validate_non_secret_structure(config)
    for field in ("benchmark", "run_id", "project_name", "mode"):
        _required_string(config, field, "config")
    for role in ("compiler", "answerer", "judge"):
        _validate_actor_config(config, role)
    retrieval = config.get("retrieval")
    if not isinstance(retrieval, dict):
        raise ValueError("config.retrieval must be a JSON object")
    top_k = retrieval.get("top_k")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise ValueError("config.retrieval.top_k must be a positive integer")
    cutoffs = retrieval.get("cutoffs")
    if not isinstance(cutoffs, list) or not cutoffs:
        raise ValueError("config.retrieval.cutoffs must be a non-empty integer array")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in cutoffs
    ):
        raise ValueError("config.retrieval.cutoffs must contain positive integers")
    if cutoffs != sorted(set(cutoffs)):
        raise ValueError("config.retrieval.cutoffs must be sorted and unique")
    if cutoffs[-1] > top_k:
        raise ValueError("config.retrieval cutoffs may not exceed top_k")
    return config


def _validate_commands(commands: Any) -> list[dict[str, Any]]:
    if not isinstance(commands, list) or not commands:
        raise ValueError("commands file must contain a non-empty ordered JSON array")
    validated: list[dict[str, Any]] = []
    seen_steps: set[str] = set()
    for index, command in enumerate(commands):
        if not isinstance(command, dict) or set(command) != {"step", "argv"}:
            raise ValueError(
                f"command entry {index} must contain exactly step and argv"
            )
        step = command.get("step")
        argv = command.get("argv")
        if not isinstance(step, str) or not _STEP_NAME.fullmatch(step):
            raise ValueError(f"invalid command step name: {step}")
        if step in seen_steps:
            raise ValueError(f"duplicate command step name: {step}")
        seen_steps.add(step)
        if not isinstance(argv, list) or not argv:
            raise ValueError(f"command {step} must be a non-empty argv array")
        if not all(isinstance(token, str) and token for token in argv):
            raise ValueError(f"command {step} argv must contain only non-empty strings")
        for token in argv:
            encoded = token.encode("utf-8")
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(encoded):
                    raise ValueError(f"{label} detected in command {step}")
            if _SENSITIVE_COMMAND_FLAG.match(token):
                raise ValueError(
                    f"credential-bearing flag may not be recorded in command {step}: {token}"
                )
            if _SENSITIVE_ENV_ASSIGNMENT.search(token):
                raise ValueError(
                    f"credential environment assignment may not be recorded in command {step}"
                )
        validated.append({"step": step, "argv": list(argv)})
    return validated


def _execution_record(
    config_path: Path,
    commands_path: Path,
    repository_root: Path,
) -> dict[str, Any]:
    _regular_file(config_path, "benchmark config")
    _regular_file(commands_path, "command declaration")
    config = _validate_config(_load_json(config_path))
    commands = _validate_commands(_load_json(commands_path))
    config_hash, config_size = _hash_and_scan_file(config_path, "benchmark config")
    commands_hash, commands_size = _hash_and_scan_file(
        commands_path, "command declaration"
    )
    return {
        "config": config,
        "config_file": {
            "path": _relative_path(config_path, repository_root),
            "sha256": config_hash,
            "size_bytes": config_size,
        },
        "commands": commands,
        "commands_file": {
            "path": _relative_path(commands_path, repository_root),
            "sha256": commands_hash,
            "size_bytes": commands_size,
        },
        "credential_policy": {
            "credentials_recorded": False,
            "runtime_credentials": "environment-only",
        },
    }


def _fresh_paths(
    repository_root: Path,
    database: Path | str,
    compiler_cache: Path | str,
    additional: Mapping[str, Path | str],
) -> list[dict[str, str]]:
    declared: list[tuple[str, Path | str]] = [
        ("database", database),
        ("database-wal", f"{database}-wal"),
        ("database-shm", f"{database}-shm"),
        ("database-journal", f"{database}-journal"),
        ("compiler-cache", compiler_cache),
        ("compiler-cache-wal", f"{compiler_cache}-wal"),
        ("compiler-cache-shm", f"{compiler_cache}-shm"),
        ("compiler-cache-journal", f"{compiler_cache}-journal"),
    ]
    for role, path in sorted(additional.items()):
        if not _FRESH_ROLE.fullmatch(role):
            raise ValueError(f"invalid fresh-path role: {role}")
        if role in {existing_role for existing_role, _ in declared}:
            raise ValueError(f"duplicate fresh-path role: {role}")
        declared.append((role, path))
    records: list[dict[str, str]] = []
    seen_paths: set[Path] = set()
    for role, value in declared:
        path = _repository_path(
            value,
            repository_root,
            label=f"fresh path {role}",
            must_exist=False,
        )
        if path in seen_paths:
            raise ValueError(f"fresh path is declared more than once: {path}")
        seen_paths.add(path)
        records.append({"role": role, "path": _relative_path(path, repository_root)})
    return records


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_run_state_under_artifact_root(
    preflight: Mapping[str, Any],
    artifact_root: Path,
    repository_root: Path,
) -> None:
    fresh_state = preflight.get("fresh_state")
    if not isinstance(fresh_state, dict) or not isinstance(
        fresh_state.get("paths"), list
    ):
        raise ValueError("preflight manifest has no valid fresh-state paths")
    for record in fresh_state["paths"]:
        if not isinstance(record, dict):
            raise ValueError("preflight manifest has an invalid fresh-state path")
        role = str(record.get("role") or "unknown")
        path = _manifest_path(
            record.get("path"), repository_root, f"fresh-state {role}"
        )
        if not _is_within(path, artifact_root):
            raise ValueError(
                f"declared run-state path is outside the artifact root: {role}={path}"
            )


def create_preflight(
    *,
    repository_root: Path | str,
    output: Path | str,
    source_archive: Path | str,
    harness_root: Path | str,
    dataset: Path | str,
    question_ids: Path | str,
    config_file: Path | str,
    commands_file: Path | str,
    database: Path | str,
    compiler_cache: Path | str,
    additional_fresh_paths: Mapping[str, Path | str] | None = None,
    expected_source_sha256: str | None = None,
    expected_harness_commit: str | None = None,
    expected_dataset_sha256: str | None = None,
    expected_question_ids_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate a future run and write an exclusive preflight declaration."""

    root = Path(repository_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"repository root must be a directory: {root}")
    output_path = _repository_path(output, root, label="preflight output")
    if output_path.exists():
        raise FileExistsError(
            f"refusing to overwrite preflight manifest: {output_path}"
        )
    source_path = _repository_path(
        source_archive, root, label="source archive", must_exist=True
    )
    harness_path = _repository_path(
        harness_root, root, label="harness root", must_exist=True
    )
    dataset_path = _repository_path(dataset, root, label="dataset", must_exist=True)
    ids_path = _repository_path(
        question_ids, root, label="question-ID file", must_exist=True
    )
    config_path = _repository_path(
        config_file, root, label="benchmark config", must_exist=True
    )
    commands_path = _repository_path(
        commands_file, root, label="command declaration", must_exist=True
    )

    source_record = _file_record(
        source_path,
        root,
        label="source archive",
        require_archive=True,
    )
    _check_expected_hash(
        str(source_record["sha256"]), expected_source_sha256, "source archive"
    )
    harness_record = _harness_record(harness_path, root)
    if expected_harness_commit is not None:
        expected_commit = expected_harness_commit.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40,64}", expected_commit):
            raise ValueError("expected harness commit is not a full git object ID")
        if harness_record["commit"] != expected_commit:
            raise ValueError(
                "harness commit mismatch: "
                f"expected {expected_commit}, got {harness_record['commit']}"
            )
    dataset_record, dataset_ids = _dataset_record(dataset_path, root)
    _check_expected_hash(
        str(dataset_record["sha256"]), expected_dataset_sha256, "dataset"
    )
    scope_record = _question_scope_record(ids_path, root, dataset_ids)
    _check_expected_hash(
        str(scope_record["sha256"]),
        expected_question_ids_sha256,
        "question-ID file",
    )
    execution = _execution_record(config_path, commands_path, root)
    fresh_records = _fresh_paths(
        root,
        database,
        compiler_cache,
        additional_fresh_paths or {},
    )
    for record in fresh_records:
        fresh_path = _manifest_path(record["path"], root, "fresh-state")
        if _is_within(output_path, fresh_path):
            raise ValueError(
                "preflight output may not be placed inside an asserted-fresh path: "
                f"{fresh_path}"
            )

    document = _attach_document_hash(
        {
            "schema_version": PREFLIGHT_SCHEMA,
            "created_at_utc": _utc_now(),
            "repository": {
                "path_semantics": "POSIX paths relative to the repository root",
            },
            "source_archive": source_record,
            "harness": harness_record,
            "dataset": dataset_record,
            "question_scope": scope_record,
            "execution": execution,
            "fresh_state": {
                "asserted_fresh": True,
                "checked_at_utc": _utc_now(),
                "paths": fresh_records,
            },
            "secret_scan": {
                "passed": True,
                "pattern_labels": [label for label, _ in SECRET_PATTERNS],
                "credentials_recorded": False,
            },
        }
    )
    _validate_no_secret_values(document, location="preflight")
    _write_new_json(output_path, document)
    return document


def _load_manifest(path: Path, expected_schema: str) -> dict[str, Any]:
    _regular_file(path, "reproduction manifest")
    document = _load_json(path)
    if not isinstance(document, dict):
        raise ValueError("reproduction manifest must be a JSON object")
    if document.get("schema_version") != expected_schema:
        raise ValueError(
            f"unsupported reproduction manifest schema: {document.get('schema_version')}"
        )
    _verify_document_hash(document)
    _validate_no_secret_values(document, location="manifest")
    return document


def _verify_preflight_document(
    document: Mapping[str, Any],
    repository_root: Path,
    *,
    require_fresh: bool,
) -> None:
    _verify_document_hash(document)
    if document.get("schema_version") != PREFLIGHT_SCHEMA:
        raise ValueError("embedded preflight has an unsupported schema")
    expected_fields = {
        "schema_version",
        "created_at_utc",
        "repository",
        "source_archive",
        "harness",
        "dataset",
        "question_scope",
        "execution",
        "fresh_state",
        "secret_scan",
        DOCUMENT_HASH_FIELD,
    }
    if set(document) != expected_fields:
        raise ValueError("preflight manifest has unexpected or missing fields")
    if document.get("repository") != {
        "path_semantics": "POSIX paths relative to the repository root"
    }:
        raise ValueError("preflight manifest has unsupported path semantics")
    if document.get("secret_scan") != {
        "passed": True,
        "pattern_labels": [label for label, _ in SECRET_PATTERNS],
        "credentials_recorded": False,
    }:
        raise ValueError("preflight manifest has an invalid secret-scan assertion")
    _validate_timestamp(document.get("created_at_utc"), "created_at_utc")

    source = document.get("source_archive")
    harness = document.get("harness")
    dataset = document.get("dataset")
    scope = document.get("question_scope")
    execution = document.get("execution")
    fresh_state = document.get("fresh_state")
    if not all(
        isinstance(value, dict)
        for value in (source, harness, dataset, scope, execution, fresh_state)
    ):
        raise ValueError("preflight manifest has an invalid object structure")

    source_path = _manifest_path(source.get("path"), repository_root, "source archive")
    actual_source = _file_record(
        source_path,
        repository_root,
        label="source archive",
        require_archive=True,
    )
    if source != actual_source:
        raise ValueError(
            "source archive provenance does not match the preflight manifest"
        )

    harness_path = _manifest_path(harness.get("path"), repository_root, "harness")
    actual_harness = _harness_record(harness_path, repository_root)
    if harness != actual_harness:
        raise ValueError("harness provenance does not match the preflight manifest")

    dataset_path = _manifest_path(dataset.get("path"), repository_root, "dataset")
    actual_dataset, dataset_ids = _dataset_record(dataset_path, repository_root)
    if dataset != actual_dataset:
        raise ValueError("dataset provenance does not match the preflight manifest")

    scope_path = _manifest_path(scope.get("path"), repository_root, "question scope")
    actual_scope = _question_scope_record(scope_path, repository_root, dataset_ids)
    if scope != actual_scope:
        raise ValueError("question scope does not match the preflight manifest")

    config_file = execution.get("config_file")
    commands_file = execution.get("commands_file")
    if not isinstance(config_file, dict) or not isinstance(commands_file, dict):
        raise ValueError("execution file provenance is invalid")
    config_path = _manifest_path(
        config_file.get("path"), repository_root, "benchmark config"
    )
    commands_path = _manifest_path(
        commands_file.get("path"), repository_root, "command declaration"
    )
    actual_execution = _execution_record(config_path, commands_path, repository_root)
    if execution != actual_execution:
        raise ValueError(
            "execution config or commands do not match the preflight manifest"
        )

    if fresh_state.get("asserted_fresh") is not True:
        raise ValueError("preflight manifest does not assert fresh state")
    _validate_timestamp(fresh_state.get("checked_at_utc"), "fresh_state.checked_at_utc")
    paths = fresh_state.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("preflight manifest has no fresh-state paths")
    seen_roles: set[str] = set()
    seen_paths: set[Path] = set()
    required_roles = {
        "database",
        "database-wal",
        "database-shm",
        "database-journal",
        "compiler-cache",
        "compiler-cache-wal",
        "compiler-cache-shm",
        "compiler-cache-journal",
    }
    paths_by_role: dict[str, str] = {}
    for record in paths:
        if not isinstance(record, dict) or set(record) != {"role", "path"}:
            raise ValueError("invalid fresh-state path record")
        role = record.get("role")
        if not isinstance(role, str) or not _FRESH_ROLE.fullmatch(role):
            raise ValueError(f"invalid fresh-state role: {role}")
        if role in seen_roles:
            raise ValueError(f"duplicate fresh-state role: {role}")
        seen_roles.add(role)
        paths_by_role[role] = str(record.get("path") or "")
        path = _manifest_path(
            record.get("path"), repository_root, f"fresh-state {role}"
        )
        if path in seen_paths:
            raise ValueError(f"duplicate fresh-state path: {path}")
        seen_paths.add(path)
        if require_fresh and path.exists():
            raise FileExistsError(f"fresh-state path now exists: {path}")
    if not required_roles <= seen_roles:
        raise ValueError("fresh-state assertion is missing database or cache sidecars")
    for base_role in ("database", "compiler-cache"):
        base_path = paths_by_role[base_role]
        for suffix in ("wal", "shm", "journal"):
            if paths_by_role[f"{base_role}-{suffix}"] != f"{base_path}-{suffix}":
                raise ValueError(
                    f"fresh-state {base_role}-{suffix} is not the expected sidecar"
                )


def verify_preflight(
    manifest: Path | str,
    *,
    repository_root: Path | str,
    require_fresh: bool = True,
) -> dict[str, Any]:
    """Recompute a preflight declaration from local files and git state."""

    root = Path(repository_root).expanduser().resolve(strict=True)
    manifest_path = _repository_path(
        manifest, root, label="preflight manifest", must_exist=True
    )
    document = _load_manifest(manifest_path, PREFLIGHT_SCHEMA)
    _verify_preflight_document(document, root, require_fresh=require_fresh)
    return {
        "ok": True,
        "schema_version": PREFLIGHT_SCHEMA,
        "manifest": _relative_path(manifest_path, root),
        "document_sha256": document[DOCUMENT_HASH_FIELD],
        "fresh_state_verified": require_fresh,
    }


def _iter_artifacts(root: Path) -> Iterable[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"artifact root must be a real directory: {root}")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"artifact tree may not contain symlinks: {path}")
        if path.is_dir():
            continue
        try:
            mode = path.stat(follow_symlinks=False).st_mode
        except FileNotFoundError as error:
            raise RuntimeError(
                f"artifact disappeared during sealing: {path}"
            ) from error
        if not stat.S_ISREG(mode):
            raise ValueError(f"artifact tree contains a non-regular file: {path}")
        yield path


def _artifact_record(root: Path, repository_root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    paths = sorted(
        _iter_artifacts(root), key=lambda path: path.relative_to(root).as_posix()
    )
    for path in paths:
        relative = path.relative_to(root).as_posix()
        digest, size = _hash_and_scan_file(path, f"artifact:{relative}")
        archive_format = None
        if path.name.lower().endswith(_ARCHIVE_SUFFIXES):
            archive_format = _scan_recognized_archive(path, required=True)
        entry: dict[str, Any] = {
            "path": relative,
            "sha256": digest,
            "size_bytes": size,
        }
        if archive_format is not None:
            entry["archive_format"] = archive_format
            entry["archive_members_secret_scanned"] = True
        files.append(entry)
    if not files:
        raise ValueError("artifact root must contain at least one file")
    return {
        "root": _relative_path(root, repository_root),
        "files": files,
        "file_count": len(files),
        "total_size_bytes": sum(int(entry["size_bytes"]) for entry in files),
        "artifact_set_sha256": hashlib.sha256(_canonical_json_bytes(files)).hexdigest(),
        "secret_scan": {
            "passed": True,
            "archives_expanded": True,
            "pattern_labels": [label for label, _ in SECRET_PATTERNS],
        },
    }


def seal_run(
    *,
    repository_root: Path | str,
    preflight_manifest: Path | str,
    artifact_root: Path | str,
    output: Path | str,
) -> dict[str, Any]:
    """Seal completed artifacts against a previously verified preflight."""

    root = Path(repository_root).expanduser().resolve(strict=True)
    preflight_path = _repository_path(
        preflight_manifest, root, label="preflight manifest", must_exist=True
    )
    artifacts_path = _repository_path(
        artifact_root, root, label="artifact root", must_exist=True
    )
    output_path = _repository_path(output, root, label="sealed manifest output")
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite sealed manifest: {output_path}")
    if _is_within(output_path, artifacts_path):
        raise ValueError("sealed manifest output must be outside the artifact root")

    preflight = _load_manifest(preflight_path, PREFLIGHT_SCHEMA)
    _verify_preflight_document(preflight, root, require_fresh=False)
    _require_run_state_under_artifact_root(preflight, artifacts_path, root)
    preflight_file_hash, preflight_file_size = _hash_and_scan_file(
        preflight_path, "preflight manifest"
    )
    artifacts = _artifact_record(artifacts_path, root)
    document = _attach_document_hash(
        {
            "schema_version": SEALED_SCHEMA,
            "sealed_at_utc": _utc_now(),
            "repository": {
                "path_semantics": "POSIX paths relative to the repository root",
            },
            "preflight": preflight,
            "preflight_file": {
                "path": _relative_path(preflight_path, root),
                "sha256": preflight_file_hash,
                "size_bytes": preflight_file_size,
            },
            "artifacts": artifacts,
            "secret_scan": {
                "passed": True,
                "pattern_labels": [label for label, _ in SECRET_PATTERNS],
                "credentials_recorded": False,
            },
        }
    )
    _validate_no_secret_values(document, location="sealed_manifest")
    _write_new_json(output_path, document)
    return document


def verify_seal(
    manifest: Path | str,
    *,
    repository_root: Path | str,
) -> dict[str, Any]:
    """Verify a sealed run, all immutable inputs, and every artifact hash."""

    root = Path(repository_root).expanduser().resolve(strict=True)
    manifest_path = _repository_path(
        manifest, root, label="sealed manifest", must_exist=True
    )
    document = _load_manifest(manifest_path, SEALED_SCHEMA)
    expected_fields = {
        "schema_version",
        "sealed_at_utc",
        "repository",
        "preflight",
        "preflight_file",
        "artifacts",
        "secret_scan",
        DOCUMENT_HASH_FIELD,
    }
    if set(document) != expected_fields:
        raise ValueError("sealed manifest has unexpected or missing fields")
    if document.get("repository") != {
        "path_semantics": "POSIX paths relative to the repository root"
    }:
        raise ValueError("sealed manifest has unsupported path semantics")
    if document.get("secret_scan") != {
        "passed": True,
        "pattern_labels": [label for label, _ in SECRET_PATTERNS],
        "credentials_recorded": False,
    }:
        raise ValueError("sealed manifest has an invalid secret-scan assertion")
    _validate_timestamp(document.get("sealed_at_utc"), "sealed_at_utc")
    preflight = document.get("preflight")
    preflight_file = document.get("preflight_file")
    artifacts = document.get("artifacts")
    if not isinstance(preflight, dict):
        raise ValueError("sealed manifest has no embedded preflight")
    if not isinstance(preflight_file, dict) or not isinstance(artifacts, dict):
        raise ValueError("sealed manifest has invalid provenance records")
    _verify_preflight_document(preflight, root, require_fresh=False)

    preflight_path = _manifest_path(
        preflight_file.get("path"), root, "preflight manifest"
    )
    actual_preflight_file = _file_record(
        preflight_path,
        root,
        label="preflight manifest",
    )
    if preflight_file != actual_preflight_file:
        raise ValueError("preflight file does not match the sealed manifest")
    on_disk_preflight = _load_manifest(preflight_path, PREFLIGHT_SCHEMA)
    if preflight != on_disk_preflight:
        raise ValueError("embedded preflight does not match the preflight file")

    artifact_root = _manifest_path(artifacts.get("root"), root, "artifact root")
    _require_run_state_under_artifact_root(preflight, artifact_root, root)
    actual_artifacts = _artifact_record(artifact_root, root)
    if artifacts != actual_artifacts:
        raise ValueError("artifact set does not match the sealed manifest")
    return {
        "ok": True,
        "schema_version": SEALED_SCHEMA,
        "manifest": _relative_path(manifest_path, root),
        "document_sha256": document[DOCUMENT_HASH_FIELD],
        "artifact_set_sha256": artifacts["artifact_set_sha256"],
        "artifact_files": artifacts["file_count"],
    }


def _parse_fresh_paths(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        try:
            role, raw_path = value.split("=", 1)
        except ValueError as error:
            raise ValueError(
                f"additional fresh path must use ROLE=PATH syntax: {value}"
            ) from error
        if not role or not raw_path:
            raise ValueError(f"invalid additional fresh path: {value}")
        if role in result:
            raise ValueError(f"duplicate additional fresh path role: {role}")
        result[role] = Path(raw_path)
    return result


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser(
        "preflight", help="validate and declare a future benchmark run"
    )
    preflight.add_argument("--repository-root", type=Path, default=Path.cwd())
    preflight.add_argument("--source-archive", type=Path, required=True)
    preflight.add_argument("--harness-root", type=Path, required=True)
    preflight.add_argument("--dataset", type=Path, required=True)
    preflight.add_argument("--question-ids", type=Path, required=True)
    preflight.add_argument("--config-file", type=Path, required=True)
    preflight.add_argument("--commands-file", type=Path, required=True)
    preflight.add_argument("--database", type=Path, required=True)
    preflight.add_argument("--compiler-cache", type=Path, required=True)
    preflight.add_argument(
        "--fresh-path",
        action="append",
        default=[],
        metavar="ROLE=PATH",
        help="assert an additional output, ledger, cache, or directory is absent",
    )
    preflight.add_argument("--expected-source-sha256")
    preflight.add_argument("--expected-harness-commit")
    preflight.add_argument("--expected-dataset-sha256")
    preflight.add_argument("--expected-question-ids-sha256")
    preflight.add_argument("--output", type=Path, required=True)

    verify_before = subparsers.add_parser(
        "verify-preflight", help="recheck immutable inputs and fresh run state"
    )
    verify_before.add_argument("--repository-root", type=Path, default=Path.cwd())
    verify_before.add_argument("--manifest", type=Path, required=True)

    seal = subparsers.add_parser(
        "seal", help="hash completed artifacts against a verified preflight"
    )
    seal.add_argument("--repository-root", type=Path, default=Path.cwd())
    seal.add_argument("--preflight", type=Path, required=True)
    seal.add_argument("--artifact-root", type=Path, required=True)
    seal.add_argument("--output", type=Path, required=True)

    verify_after = subparsers.add_parser(
        "verify-seal", help="recheck a sealed run and all artifact hashes"
    )
    verify_after.add_argument("--repository-root", type=Path, default=Path.cwd())
    verify_after.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if args.command == "preflight":
        document = create_preflight(
            repository_root=args.repository_root,
            output=args.output,
            source_archive=args.source_archive,
            harness_root=args.harness_root,
            dataset=args.dataset,
            question_ids=args.question_ids,
            config_file=args.config_file,
            commands_file=args.commands_file,
            database=args.database,
            compiler_cache=args.compiler_cache,
            additional_fresh_paths=_parse_fresh_paths(args.fresh_path),
            expected_source_sha256=args.expected_source_sha256,
            expected_harness_commit=args.expected_harness_commit,
            expected_dataset_sha256=args.expected_dataset_sha256,
            expected_question_ids_sha256=args.expected_question_ids_sha256,
        )
        output = {
            "ok": True,
            "schema_version": PREFLIGHT_SCHEMA,
            "manifest": str(args.output),
            "document_sha256": document[DOCUMENT_HASH_FIELD],
        }
    elif args.command == "verify-preflight":
        output = verify_preflight(
            args.manifest,
            repository_root=args.repository_root,
            require_fresh=True,
        )
    elif args.command == "seal":
        document = seal_run(
            repository_root=args.repository_root,
            preflight_manifest=args.preflight,
            artifact_root=args.artifact_root,
            output=args.output,
        )
        output = {
            "ok": True,
            "schema_version": SEALED_SCHEMA,
            "manifest": str(args.output),
            "document_sha256": document[DOCUMENT_HASH_FIELD],
            "artifact_set_sha256": document["artifacts"]["artifact_set_sha256"],
            "artifact_files": document["artifacts"]["file_count"],
        }
    else:
        output = verify_seal(args.manifest, repository_root=args.repository_root)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
