#!/usr/bin/env python3
"""Verify a sealed paired-evaluation staging copy without reading its content."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
from pathlib import Path
from typing import Any


SCHEMA = "narratordb.paired-evaluation-copy.v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot(directory: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symbolic link is forbidden: {path}")
        if path.is_file():
            entries.append(
                {
                    "path": path.relative_to(directory).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--require-read-only", action="store_true")
    args = parser.parse_args()

    manifest_path = args.manifest.expanduser().resolve()
    actual_manifest_hash = sha256_file(manifest_path)
    if actual_manifest_hash != args.expected_manifest_sha256.lower():
        raise ValueError(
            f"manifest checksum mismatch: {actual_manifest_hash} != "
            f"{args.expected_manifest_sha256.lower()}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA:
        raise ValueError("unsupported copy-manifest schema")
    expected = manifest.get("files")
    if not isinstance(expected, list):
        raise ValueError("copy manifest requires a file list")

    frozen = Path(str(manifest.get("frozen_directory") or "")).resolve()
    staged = Path(str(manifest.get("evaluated_directory") or "")).resolve()
    frozen_snapshot = snapshot(frozen)
    staged_snapshot = snapshot(staged)
    if frozen_snapshot != expected:
        raise ValueError("original frozen prediction directory changed")
    if staged_snapshot != expected:
        raise ValueError("staged prediction directory is not byte-identical")

    writable: list[str] = []
    if args.require_read_only:
        for entry in expected:
            path = staged / entry["path"]
            if path.stat().st_mode & stat.S_IWUSR:
                writable.append(entry["path"])
        if manifest_path.stat().st_mode & stat.S_IWUSR:
            writable.append(manifest_path.name)
        if writable:
            raise ValueError(f"staged files are owner-writable: {writable[:10]}")

    print(
        json.dumps(
            {
                "ok": True,
                "manifest": str(manifest_path),
                "manifest_sha256": actual_manifest_hash,
                "files": len(expected),
                "prediction_files": int(manifest.get("prediction_file_count") or 0),
                "original_matches": True,
                "staged_matches": True,
                "read_only": bool(args.require_read_only),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
