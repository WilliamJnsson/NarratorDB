#!/usr/bin/env python3
"""Archive and verify immutable NarratorDB benchmark runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable


MANIFEST_NAME = "SHA256SUMS"
SECRET_PATTERNS = (
    ("OpenRouter API key", re.compile(rb"sk-or-v1-[A-Za-z0-9_-]{16,}")),
    ("OpenAI-style API key", re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}")),
    ("Mem0 API key", re.compile(rb"\bm0-[A-Za-z0-9_-]{20,}")),
)
VALID_STATUSES = {
    "development",
    "final-frozen",
    "final-unbiased",
    "published-reference",
    "aborted",
}
STATUS_ALIASES = {
    # Retain compatibility with the first immutable calibration record. Its
    # deliberately explicit legacy label predates the normalized history
    # status vocabulary and must not require rewriting the signed summary.
    "development_calibration_not_competitor_headline": "development",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_artifact_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"benchmark archives may not contain symlinks: {path}")
        if path.is_file() and path.name != MANIFEST_NAME:
            yield path


def scan_file_for_secrets(path: Path, display_name: str | None = None) -> None:
    carry = b""
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            searchable = carry + chunk
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(searchable):
                    raise ValueError(
                        f"{label} detected in benchmark artifact: {display_name or path.name}"
                    )
            carry = searchable[-256:]


def scan_for_secrets(root: Path) -> None:
    for path in iter_artifact_files(root):
        scan_file_for_secrets(path, path.relative_to(root).as_posix())


def manifest_lines(root: Path) -> list[str]:
    return [
        f"{sha256_file(path)}  ./{path.relative_to(root).as_posix()}"
        for path in iter_artifact_files(root)
    ]


def create_manifest(root: Path) -> Path:
    manifest = root / MANIFEST_NAME
    if manifest.exists():
        raise FileExistsError(f"refusing to overwrite existing manifest: {manifest}")
    lines = manifest_lines(root)
    manifest.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return manifest


def parse_manifest(manifest: Path) -> list[tuple[str, str]]:
    entries = []
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            digest, relative = line.split("  ", 1)
        except ValueError as error:
            raise ValueError(f"invalid manifest line {line_number}") from error
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid SHA-256 on manifest line {line_number}")
        relative = relative.removeprefix("./")
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError(f"unsafe manifest path on line {line_number}: {relative}")
        entries.append((digest, relative))
    return entries


def verify_manifest(root: Path, manifest: Path | None = None) -> dict:
    manifest = manifest or root / MANIFEST_NAME
    if not manifest.is_file():
        raise FileNotFoundError(f"manifest not found: {manifest}")
    entries = parse_manifest(manifest)
    expected_paths = {relative for _, relative in entries}
    actual_paths = {path.relative_to(root).as_posix() for path in iter_artifact_files(root)}
    if expected_paths != actual_paths:
        missing = sorted(expected_paths - actual_paths)
        untracked = sorted(actual_paths - expected_paths)
        raise ValueError(f"manifest file set mismatch: missing={missing}, untracked={untracked}")
    for expected, relative in entries:
        actual = sha256_file(root / relative)
        if actual != expected:
            raise ValueError(f"checksum mismatch for {relative}: expected {expected}, got {actual}")
    return {
        "ok": True,
        "files": len(entries),
        "manifest": str(manifest),
        "manifest_sha256": sha256_file(manifest),
    }


def _record_identity(record: dict, record_path: Path) -> str:
    for candidate in (
        record.get("run_id"),
        (record.get("narratordb_baseline") or {}).get("evaluation_run_id"),
        record_path.stem,
    ):
        if candidate:
            return str(candidate)
    raise ValueError("benchmark record needs a stable identity")


def _load_index(index_path: Path) -> dict:
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
    else:
        index = {"schema_version": 1, "runs": []}
    if index.get("schema_version") != 1 or not isinstance(index.get("runs"), list):
        raise ValueError("unsupported benchmark history index")
    return index


def _repository_relative(path: Path, repository_root: Path) -> str:
    try:
        return path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError as error:
        raise ValueError(f"benchmark history path is outside repository: {path}") from error


def _indexed_path(repository_root: Path, value: object, label: str) -> Path:
    relative = Path(str(value or ""))
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe {label} path in benchmark history: {value}")
    return repository_root / relative


def verify_index(index_path: Path, *, records_only: bool = False) -> dict:
    """Verify tracked summaries and, unless omitted, every local raw archive."""
    index_path = index_path.expanduser().resolve()
    index = _load_index(index_path)
    repository_root = index_path.parent.parent
    seen_ids: set[str] = set()
    verified = []
    for entry in index["runs"]:
        if not isinstance(entry, dict):
            raise ValueError("benchmark history run entries must be JSON objects")
        entry_id = str(entry.get("id") or "")
        if not entry_id or entry_id in seen_ids:
            raise ValueError(f"duplicate or empty benchmark history id: {entry_id}")
        seen_ids.add(entry_id)

        record_path = _indexed_path(repository_root, entry.get("record"), "record")
        manifest_path = _indexed_path(repository_root, entry.get("manifest"), "manifest")
        artifacts_path = _indexed_path(repository_root, entry.get("artifacts"), "artifacts")
        if not record_path.is_file():
            raise FileNotFoundError(f"indexed benchmark record not found: {record_path}")
        if record_path.is_symlink():
            raise ValueError(f"indexed benchmark record may not be a symlink: {record_path}")
        scan_file_for_secrets(record_path, entry.get("record"))
        if sha256_file(record_path) != entry.get("record_sha256"):
            raise ValueError(f"indexed benchmark record checksum mismatch: {entry_id}")
        if records_only:
            verified.append(entry_id)
            continue
        if not manifest_path.is_file():
            raise FileNotFoundError(f"indexed benchmark manifest not found: {manifest_path}")
        if sha256_file(manifest_path) != entry.get("manifest_sha256"):
            raise ValueError(f"indexed benchmark manifest checksum mismatch: {entry_id}")
        scan_for_secrets(artifacts_path)
        result = verify_manifest(artifacts_path, manifest_path)
        if result["files"] != entry.get("artifact_files"):
            raise ValueError(f"indexed artifact count mismatch: {entry_id}")
        verified.append(entry_id)
    return {
        "ok": True,
        "runs": len(verified),
        "verified_ids": verified,
        "verification_scope": "records-only" if records_only else "records-and-artifacts",
    }


def archive_run(run_dir: Path, record_path: Path, index_path: Path) -> dict:
    run_dir = run_dir.expanduser().resolve()
    record_path = record_path.expanduser().resolve()
    index_path = index_path.expanduser().resolve()
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run directory not found: {run_dir}")
    if not record_path.is_file():
        raise FileNotFoundError(f"record not found: {record_path}")
    if record_path.is_symlink():
        raise ValueError(f"benchmark record may not be a symlink: {record_path}")
    scan_file_for_secrets(record_path, record_path.name)

    record = json.loads(record_path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("benchmark record must be a JSON object")
    declared_status = str(record.get("status") or "").strip()
    status = STATUS_ALIASES.get(declared_status, declared_status)
    if status not in VALID_STATUSES:
        raise ValueError(f"record status must be one of {sorted(VALID_STATUSES)}")

    index = _load_index(index_path)
    entry_id = _record_identity(record, record_path)
    if any(str(entry.get("id")) == entry_id for entry in index["runs"]):
        raise ValueError(f"benchmark history already contains run id {entry_id}")

    repository_root = index_path.parent.parent
    record_relative = _repository_relative(record_path, repository_root)
    artifacts_relative = _repository_relative(run_dir, repository_root)

    scan_for_secrets(run_dir)
    manifest = run_dir / MANIFEST_NAME
    if manifest.exists():
        verification = verify_manifest(run_dir, manifest)
    else:
        manifest = create_manifest(run_dir)
        verification = verify_manifest(run_dir, manifest)
    manifest_relative = _repository_relative(manifest, repository_root)

    benchmark = record.get("benchmark") or {}
    if isinstance(benchmark, dict):
        benchmark_name = str(benchmark.get("name") or "")
    else:
        benchmark_name = str(benchmark)
    entry = {
        "id": entry_id,
        "recorded_at": str(record.get("recorded_at") or ""),
        "status": status,
        "declared_status": declared_status,
        "system": str(record.get("system") or "NarratorDB"),
        "benchmark": benchmark_name,
        "record": record_relative,
        "record_sha256": sha256_file(record_path),
        "artifacts": artifacts_relative,
        "manifest": manifest_relative,
        "manifest_sha256": verification["manifest_sha256"],
        "artifact_files": verification["files"],
    }
    index["runs"].append(entry)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = index_path.with_suffix(index_path.suffix + ".tmp")
    temporary.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(index_path)
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    archive_parser = subparsers.add_parser("archive", help="verify and append a run to history")
    archive_parser.add_argument("--run-dir", type=Path, required=True)
    archive_parser.add_argument("--record", type=Path, required=True)
    archive_parser.add_argument(
        "--index",
        type=Path,
        default=Path("benchmark_records/index.json"),
    )

    verify_parser = subparsers.add_parser("verify", help="verify checksums and secret safety")
    verify_parser.add_argument("--run-dir", type=Path, required=True)

    verify_index_parser = subparsers.add_parser(
        "verify-index", help="verify every summary and raw archive in the history ledger"
    )
    verify_index_parser.add_argument(
        "--index",
        type=Path,
        default=Path("benchmark_records/index.json"),
    )
    verify_index_parser.add_argument(
        "--records-only",
        action="store_true",
        help="verify public summary records without requiring separately retained raw archives",
    )

    args = parser.parse_args()
    if args.command == "archive":
        output = archive_run(args.run_dir, args.record, args.index)
    elif args.command == "verify":
        root = args.run_dir.expanduser().resolve()
        scan_for_secrets(root)
        output = verify_manifest(root)
    else:
        output = verify_index(args.index, records_only=args.records_only)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
