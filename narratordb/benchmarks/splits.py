#!/usr/bin/env python3
"""Generate and verify the frozen LongMemEval development/holdout split."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
QUESTION_TYPES = (
    "knowledge-update",
    "multi-session",
    "single-session-assistant",
    "single-session-preference",
    "single-session-user",
    "temporal-reasoning",
)
DEFAULT_SEED = 42
DEFAULT_SAMPLE_SIZES = (5, 1, 2)
DEVELOPMENT_FILENAME = "longmemeval_s_dev42_question_ids.json"
HOLDOUT_FILENAME = "longmemeval_s_holdout458_question_ids.json"
MANIFEST_FILENAME = "longmemeval_s_split_manifest.json"
SPLIT_FILENAMES = (
    DEVELOPMENT_FILENAME,
    HOLDOUT_FILENAME,
    MANIFEST_FILENAME,
)
REPOSITORY_OUTPUT_DIR = ROOT / "benchmark_records" / "splits"
PACKAGED_OUTPUT_DIR = Path(__file__).resolve().parent / "data" / "splits"


def _contains_frozen_split(directory: Path) -> bool:
    return all((directory / filename).is_file() for filename in SPLIT_FILENAMES)


# Source checkouts keep the auditable records at repository top level. Installed
# wheels do not contain that repository directory, so they use the identical
# package-data copy shipped beside this module.
DEFAULT_OUTPUT_DIR = (
    REPOSITORY_OUTPUT_DIR
    if _contains_frozen_split(REPOSITORY_OUTPUT_DIR)
    else PACKAGED_OUTPUT_DIR
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def render_question_ids(question_ids: list[str]) -> str:
    """Return the canonical, hash-stable question-ID file representation."""

    return json.dumps(sorted(question_ids), indent=2) + "\n"


def _validated_rows(dataset: Path) -> list[dict[str, Any]]:
    rows = json.loads(dataset.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("LongMemEval dataset must be a non-empty JSON array")

    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"dataset row {index} is not a JSON object")
        question_id = str(row.get("question_id") or "").strip()
        question_type = str(row.get("question_type") or "").strip()
        if not question_id:
            raise ValueError(f"dataset row {index} has no question_id")
        if question_id in seen:
            raise ValueError(f"dataset contains duplicate question_id: {question_id}")
        if question_type not in QUESTION_TYPES:
            raise ValueError(
                f"dataset row {question_id} has unsupported question_type: {question_type}"
            )
        seen.add(question_id)
        validated.append(row)
    return validated


def stratified_sample_ids(
    rows: list[dict[str, Any]],
    *,
    per_type: int,
    seed: int,
) -> set[str]:
    """Match the benchmark adapter's deterministic per-type sampling."""

    if per_type <= 0:
        raise ValueError("per_type must be positive")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row["question_type"])].append(row)

    rng = random.Random(seed)
    selected: set[str] = set()
    for question_type in QUESTION_TYPES:
        group = sorted(groups[question_type], key=lambda row: str(row["question_id"]))
        if len(group) < per_type:
            raise ValueError(
                f"question type {question_type} has {len(group)} rows; need {per_type}"
            )
        selected.update(
            str(row["question_id"])
            for row in rng.sample(group, per_type)
        )
    return selected


def derive_split(
    rows: list[dict[str, Any]],
    *,
    seed: int = DEFAULT_SEED,
    sample_sizes: tuple[int, ...] = DEFAULT_SAMPLE_SIZES,
) -> tuple[list[str], list[str]]:
    """Derive dev IDs from inspected samples and holdout IDs from the complement."""

    development: set[str] = set()
    for per_type in sample_sizes:
        development.update(stratified_sample_ids(rows, per_type=per_type, seed=seed))
    dataset_ids = {str(row["question_id"]) for row in rows}
    return sorted(development), sorted(dataset_ids - development)


def write_split(
    dataset: Path,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    seed: int = DEFAULT_SEED,
    sample_sizes: tuple[int, ...] = DEFAULT_SAMPLE_SIZES,
) -> dict[str, Any]:
    """Generate canonical ID files and their provenance manifest."""

    rows = _validated_rows(dataset)
    development, holdout = derive_split(rows, seed=seed, sample_sizes=sample_sizes)
    development_payload = render_question_ids(development)
    holdout_payload = render_question_ids(holdout)

    manifest = {
        "schema_version": 1,
        "benchmark": "LongMemEval_S",
        "dataset": {
            "filename": dataset.name,
            "questions": len(rows),
            "sha256": sha256_file(dataset),
        },
        "derivation": {
            "description": (
                "Development is the union of deterministic, stratified samples; "
                "holdout is the exact dataset complement."
            ),
            "question_types": list(QUESTION_TYPES),
            "sample_per_type": list(sample_sizes),
            "seed": seed,
        },
        "development": {
            "filename": DEVELOPMENT_FILENAME,
            "questions": len(development),
            "sha256": sha256_bytes(development_payload.encode("utf-8")),
        },
        "holdout": {
            "filename": HOLDOUT_FILENAME,
            "questions": len(holdout),
            "sha256": sha256_bytes(holdout_payload.encode("utf-8")),
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / DEVELOPMENT_FILENAME).write_text(development_payload, encoding="utf-8")
    (output_dir / HOLDOUT_FILENAME).write_text(holdout_payload, encoding="utf-8")
    (output_dir / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _load_id_file(path: Path) -> list[str]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value.strip() for value in values
    ):
        raise ValueError(f"question-ID file must be a JSON string array: {path}")
    if values != sorted(values):
        raise ValueError(f"question-ID file is not sorted: {path}")
    if len(set(values)) != len(values):
        raise ValueError(f"question-ID file contains duplicates: {path}")
    return values


def verify_split(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    dataset: Path | None = None,
) -> dict[str, Any]:
    """Verify file hashes, complement integrity, and optionally re-derive the split."""

    manifest_path = output_dir / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("split manifest must be a JSON object")

    development_path = output_dir / str(manifest["development"]["filename"])
    holdout_path = output_dir / str(manifest["holdout"]["filename"])
    development = _load_id_file(development_path)
    holdout = _load_id_file(holdout_path)

    for label, path, values in (
        ("development", development_path, development),
        ("holdout", holdout_path, holdout),
    ):
        expected_count = int(manifest[label]["questions"])
        if len(values) != expected_count:
            raise ValueError(
                f"{label} count mismatch: {len(values)} != {expected_count}"
            )
        expected_hash = str(manifest[label]["sha256"])
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            raise ValueError(
                f"{label} SHA-256 mismatch: {actual_hash} != {expected_hash}"
            )

    overlap = set(development) & set(holdout)
    if overlap:
        raise ValueError(f"development and holdout overlap: {sorted(overlap)[:10]}")
    declared_total = int(manifest["dataset"]["questions"])
    if len(development) + len(holdout) != declared_total:
        raise ValueError(
            "split does not cover the declared dataset denominator: "
            f"{len(development) + len(holdout)} != {declared_total}"
        )

    dataset_verified = False
    if dataset is not None:
        rows = _validated_rows(dataset)
        actual_dataset_hash = sha256_file(dataset)
        expected_dataset_hash = str(manifest["dataset"]["sha256"])
        if actual_dataset_hash != expected_dataset_hash:
            raise ValueError(
                "dataset SHA-256 mismatch: "
                f"{actual_dataset_hash} != {expected_dataset_hash}"
            )
        derived_development, derived_holdout = derive_split(
            rows,
            seed=int(manifest["derivation"]["seed"]),
            sample_sizes=tuple(int(value) for value in manifest["derivation"]["sample_per_type"]),
        )
        if development != derived_development or holdout != derived_holdout:
            raise ValueError("tracked question-ID files do not match the derived split")
        dataset_verified = True

    return {
        "complete": True,
        "dataset_verified": dataset_verified,
        "development_questions": len(development),
        "holdout_questions": len(holdout),
        "development_sha256": sha256_file(development_path),
        "holdout_sha256": sha256_file(holdout_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--write", action="store_true", help="Generate the frozen split")
    operation.add_argument("--verify", action="store_true", help="Verify tracked split files")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    output_dir = args.output_dir.expanduser().resolve()
    if args.write:
        if args.dataset is None:
            parser.error("--dataset is required with --write")
        report = write_split(args.dataset.expanduser().resolve(), output_dir)
    else:
        report = verify_split(
            output_dir,
            dataset=args.dataset.expanduser().resolve() if args.dataset else None,
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
