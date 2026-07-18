#!/usr/bin/env python3
"""Content-free integrity controls for the frozen V8 existing-derived replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SCHEMA = "narratordb.v8-existing-derived-replay-control.v1"
CORE_TABLES = (
    "messages",
    "memory_sessions",
    "memory_compiler_jobs",
    "memory_claims",
    "memory_entities",
    "memory_claim_relations",
    "memory_usage_ledger",
)
CHECKPOINT_KEYS = {
    "chunk_size",
    "completed_at",
    "question_id",
    "run_id",
    "total_pairs_failed",
    "total_pairs_processed",
    "user_id",
}
SHA256 = re.compile(r"[0-9a-f]{64}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _question_ids(path: Path) -> list[str]:
    values = _load_json(path)
    if not isinstance(values, list) or not values:
        raise ValueError("question-ID file must be a non-empty JSON array")
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("question-ID file must contain non-empty strings")
    if values != sorted(values) or len(values) != len(set(values)):
        raise ValueError("question-ID file must be sorted and unique")
    return values


def _expected_sha256(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not SHA256.fullmatch(normalized):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal digits")
    return normalized


def _write_new(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())


def validate_checkpoints(args: argparse.Namespace) -> dict[str, Any]:
    directory = args.directory.resolve(strict=True)
    ids = _question_ids(args.question_id_file.resolve(strict=True))
    if len(ids) != args.expected_questions:
        raise ValueError("question-ID count does not match --expected-questions")
    expected_files = {f"_ingestion_{question_id}.json" for question_id in ids}
    actual_files = {path.name for path in directory.glob("_ingestion_*.json")}
    if actual_files != expected_files:
        raise ValueError("checkpoint filename scope is not exact")
    prediction_files = [
        path for path in directory.glob("*.json") if not path.name.startswith("_")
    ]
    if args.require_zero_predictions and prediction_files:
        raise ValueError("normal prediction files exist before replay")

    processed = 0
    manifests: list[dict[str, Any]] = []
    for question_id in ids:
        path = directory / f"_ingestion_{question_id}.json"
        record = _load_json(path)
        if not isinstance(record, dict) or set(record) != CHECKPOINT_KEYS:
            raise ValueError(f"checkpoint schema mismatch: {path.name}")
        expected_user = f"longmemeval_{question_id}_{args.run_id}"
        if record.get("question_id") != question_id:
            raise ValueError(f"checkpoint question mismatch: {path.name}")
        if record.get("run_id") != args.run_id:
            raise ValueError(f"checkpoint run ID mismatch: {path.name}")
        if record.get("user_id") != expected_user:
            raise ValueError(f"checkpoint user scope mismatch: {path.name}")
        if record.get("chunk_size") != args.expected_chunk_size:
            raise ValueError(f"checkpoint chunk size mismatch: {path.name}")
        if record.get("total_pairs_failed") != 0:
            raise ValueError(f"checkpoint contains failed pairs: {path.name}")
        pair_count = record.get("total_pairs_processed")
        if (
            isinstance(pair_count, bool)
            or not isinstance(pair_count, int)
            or pair_count <= 0
        ):
            raise ValueError(f"checkpoint has invalid processed count: {path.name}")
        if (
            not isinstance(record.get("completed_at"), str)
            or not record["completed_at"]
        ):
            raise ValueError(f"checkpoint has no completion timestamp: {path.name}")
        processed += pair_count
        manifests.append(
            {
                "file": path.name,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return {
        "schema_version": SCHEMA,
        "operation": "validate-checkpoints",
        "checks": {
            "checkpoint_scope_exact": True,
            "checkpoint_schema_exact": True,
            "run_id_exact": True,
            "user_scope_exact": True,
            "chunk_size_exact": True,
            "failed_pairs_zero": True,
            "normal_predictions_zero": not prediction_files,
        },
        "checkpoints": len(manifests),
        "pairs_processed": processed,
        "pairs_failed": 0,
        "run_id": args.run_id,
        "expected_chunk_size": args.expected_chunk_size,
        "question_ids_sha256": _sha256(args.question_id_file.resolve(strict=True)),
        "files": manifests,
    }


def verify_file(args: argparse.Namespace) -> dict[str, Any]:
    path = args.path.resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise ValueError("verified path must be a regular non-symlink file")
    expected = _expected_sha256(args.expected_sha256, "expected SHA-256")
    actual = _sha256(path)
    if actual != expected:
        raise ValueError(f"file SHA-256 mismatch: expected {expected}, got {actual}")
    return {
        "schema_version": SCHEMA,
        "operation": "verify-file",
        "file": str(path),
        "bytes": path.stat().st_size,
        "sha256": actual,
        "verified": True,
    }


def verify_manifest(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.resolve(strict=True)
    manifest = args.manifest.resolve(strict=True)
    expected_manifest = _expected_sha256(
        args.expected_manifest_sha256, "expected manifest SHA-256"
    )
    actual_manifest = _sha256(manifest)
    if actual_manifest != expected_manifest:
        raise ValueError(
            "manifest SHA-256 mismatch: "
            f"expected {expected_manifest}, got {actual_manifest}"
        )
    verified: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise ValueError(f"invalid SHA-256 manifest line {number}")
        expected, relative = match.groups()
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"unsafe SHA-256 manifest path on line {number}")
        path = (root / relative_path).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ValueError(f"manifest path escapes root on line {number}") from error
        if path in seen:
            raise ValueError(f"duplicate SHA-256 manifest path on line {number}")
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"manifest path is not a regular file on line {number}")
        seen.add(path)
        actual = _sha256(path)
        if actual != expected:
            raise ValueError(f"SHA-256 mismatch on manifest line {number}")
        verified.append(
            {
                "file": relative_path.as_posix(),
                "bytes": path.stat().st_size,
                "sha256": actual,
            }
        )
    if not verified:
        raise ValueError("SHA-256 manifest is empty")
    return {
        "schema_version": SCHEMA,
        "operation": "verify-manifest",
        "root": str(root),
        "manifest": str(manifest),
        "manifest_sha256": actual_manifest,
        "verified_files": len(verified),
        "files": verified,
        "verified": True,
    }


def copy_checkpoints(args: argparse.Namespace) -> dict[str, Any]:
    source = args.source.resolve(strict=True)
    destination = args.destination.resolve(strict=True)
    ids = _question_ids(args.question_id_file.resolve(strict=True))
    if source == destination:
        raise ValueError("checkpoint source and destination must differ")
    if any(destination.iterdir()):
        raise ValueError("checkpoint destination must be empty")
    expected_source_files = {
        source / f"_ingestion_{question_id}.json" for question_id in ids
    }
    missing = sorted(path.name for path in expected_source_files if not path.is_file())
    if missing:
        raise ValueError(f"checkpoint source is incomplete: {missing[:10]}")
    source_before = {path.name: _sha256(path) for path in expected_source_files}
    for path in expected_source_files:
        shutil.copy2(path, destination / path.name)
    source_after = {path.name: _sha256(path) for path in expected_source_files}
    copied = {
        path.name: _sha256(path)
        for path in destination.glob("_ingestion_*.json")
        if path.is_file()
    }
    if source_before != source_after:
        raise RuntimeError("checkpoint source changed during copy")
    if source_after != copied:
        raise RuntimeError("checkpoint copy is not byte-identical")
    return {
        "schema_version": SCHEMA,
        "operation": "copy-checkpoints",
        "checkpoints": len(copied),
        "source_stable": True,
        "copy_byte_identical": True,
        "normal_predictions_copied": 0,
        "files": [{"file": name, "sha256": copied[name]} for name in sorted(copied)],
    }


def capture_counts(args: argparse.Namespace) -> dict[str, Any]:
    database = args.database.resolve(strict=True)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = sorted(set(CORE_TABLES) - tables)
        if missing:
            raise ValueError(f"database is missing core tables: {missing}")
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in CORE_TABLES
        }
        statuses = {
            str(status): int(count)
            for status, count in connection.execute(
                "SELECT status, COUNT(*) FROM memory_compiler_jobs GROUP BY status ORDER BY status"
            )
        }
        fingerprints = {
            str(fingerprint): int(count)
            for fingerprint, count in connection.execute(
                "SELECT compiler_fingerprint, COUNT(*) FROM memory_compiler_jobs "
                "GROUP BY compiler_fingerprint ORDER BY compiler_fingerprint"
            )
        }
    finally:
        connection.close()
    if quick_check != "ok":
        raise ValueError(f"database quick_check failed: {quick_check}")
    return {
        "schema_version": SCHEMA,
        "operation": "capture-db-counts",
        "database_sha256": _sha256(database),
        "sqlite_quick_check": quick_check,
        "core_table_counts": counts,
        "compiler_statuses": statuses,
        "compiler_fingerprints": fingerprints,
    }


def probe_diagnostics(args: argparse.Namespace) -> dict[str, Any]:
    directory = args.directory.resolve(strict=True)
    ids = _question_ids(args.question_id_file.resolve(strict=True))
    if len(ids) != args.expected_questions:
        raise ValueError("question-ID count does not match --expected-questions")
    totals = {
        "registered_sessions": 0,
        "complete_sessions": 0,
        "partial_sessions": 0,
        "nonterminal_sessions": 0,
    }
    ready = 0
    for question_id in ids:
        checkpoint = _load_json(directory / f"_ingestion_{question_id}.json")
        user_id = checkpoint.get("user_id") if isinstance(checkpoint, dict) else None
        if not isinstance(user_id, str) or not user_id:
            raise ValueError(f"checkpoint has no valid user ID: {question_id}")
        query = urllib.parse.urlencode({"user_id": user_id})
        url = f"{args.base_url.rstrip('/')}/replay/diagnostics?{query}"
        with urllib.request.urlopen(url, timeout=args.timeout) as response:
            document = json.loads(response.read())
        if document.get("compiler_fingerprint") != args.fingerprint:
            raise ValueError(f"diagnostic fingerprint mismatch: {question_id}")
        if document.get("ready") is not True:
            raise ValueError(f"scope is not replay-ready: {question_id}")
        if document.get("compiler_constructed") is not False:
            raise ValueError(f"diagnostics report a compiler: {question_id}")
        if document.get("compiler_cache_constructed") is not False:
            raise ValueError(f"diagnostics report a compiler cache: {question_id}")
        ready += 1
        for key in totals:
            value = document.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"invalid diagnostic count {key}: {question_id}")
            totals[key] += value
    if totals["nonterminal_sessions"] != 0:
        raise ValueError("diagnostics contain nonterminal sessions")
    return {
        "schema_version": SCHEMA,
        "operation": "probe-diagnostics",
        "ready_scopes": ready,
        "expected_scopes": args.expected_questions,
        "compiler_fingerprint": args.fingerprint,
        "compiler_constructed": False,
        "compiler_cache_constructed": False,
        "aggregate_lifecycle_counts": totals,
    }


def compare_counts(args: argparse.Namespace) -> dict[str, Any]:
    before = _load_json(args.before.resolve(strict=True))
    after = _load_json(args.after.resolve(strict=True))
    fields = ("core_table_counts", "compiler_statuses", "compiler_fingerprints")
    changes = {field: before.get(field) != after.get(field) for field in fields}
    if any(changes.values()):
        raise ValueError(f"database content-free counts changed: {changes}")
    return {
        "schema_version": SCHEMA,
        "operation": "compare-db-counts",
        "unchanged": True,
        "fields": list(fields),
        "before_database_sha256": before.get("database_sha256"),
        "after_database_sha256": after.get("database_sha256"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    file_verification = subparsers.add_parser("verify-file")
    file_verification.add_argument("--path", type=Path, required=True)
    file_verification.add_argument("--expected-sha256", required=True)
    file_verification.add_argument("--output", type=Path, required=True)

    manifest_verification = subparsers.add_parser("verify-manifest")
    manifest_verification.add_argument("--root", type=Path, required=True)
    manifest_verification.add_argument("--manifest", type=Path, required=True)
    manifest_verification.add_argument("--expected-manifest-sha256", required=True)
    manifest_verification.add_argument("--output", type=Path, required=True)

    checkpoint_copy = subparsers.add_parser("copy-checkpoints")
    checkpoint_copy.add_argument("--source", type=Path, required=True)
    checkpoint_copy.add_argument("--destination", type=Path, required=True)
    checkpoint_copy.add_argument("--question-id-file", type=Path, required=True)
    checkpoint_copy.add_argument("--output", type=Path, required=True)

    checkpoints = subparsers.add_parser("validate-checkpoints")
    checkpoints.add_argument("--directory", type=Path, required=True)
    checkpoints.add_argument("--question-id-file", type=Path, required=True)
    checkpoints.add_argument("--run-id", required=True)
    checkpoints.add_argument("--expected-questions", type=int, required=True)
    checkpoints.add_argument("--expected-chunk-size", type=int, required=True)
    checkpoints.add_argument("--require-zero-predictions", action="store_true")
    checkpoints.add_argument("--output", type=Path, required=True)

    counts = subparsers.add_parser("capture-db-counts")
    counts.add_argument("--database", type=Path, required=True)
    counts.add_argument("--output", type=Path, required=True)

    diagnostics = subparsers.add_parser("probe-diagnostics")
    diagnostics.add_argument("--base-url", required=True)
    diagnostics.add_argument("--directory", type=Path, required=True)
    diagnostics.add_argument("--question-id-file", type=Path, required=True)
    diagnostics.add_argument("--expected-questions", type=int, required=True)
    diagnostics.add_argument("--fingerprint", required=True)
    diagnostics.add_argument("--timeout", type=float, default=30.0)
    diagnostics.add_argument("--output", type=Path, required=True)

    compare = subparsers.add_parser("compare-db-counts")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "verify-file":
            report = verify_file(args)
        elif args.command == "verify-manifest":
            report = verify_manifest(args)
        elif args.command == "copy-checkpoints":
            report = copy_checkpoints(args)
        elif args.command == "validate-checkpoints":
            report = validate_checkpoints(args)
        elif args.command == "capture-db-counts":
            report = capture_counts(args)
        elif args.command == "probe-diagnostics":
            report = probe_diagnostics(args)
        else:
            report = compare_counts(args)
        _write_new(args.output, report)
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        parser.error(str(error))
    print(json.dumps({"ok": True, "operation": report["operation"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
