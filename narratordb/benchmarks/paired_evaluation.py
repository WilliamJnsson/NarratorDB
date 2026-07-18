#!/usr/bin/env python3
"""Prepare and verify isolated copies for paired LongMemEval evaluation.

The official evaluate-only harness mutates each prediction JSON by adding its
answer/judge results.  This helper makes a byte-identical copy in a fresh
output root and records a content-free SHA-256 manifest.  It deliberately does
not parse prediction payloads; scope validation is based on the predeclared
question-ID file and prediction filenames.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "narratordb.paired-evaluation-copy.v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_question_ids(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [line.strip() for line in raw.splitlines() if line.strip()]
    if not isinstance(parsed, list) or not all(
        isinstance(value, str) and value.strip() for value in parsed
    ):
        raise ValueError("question-ID file must contain a JSON string array or one ID per line")
    normalized = [value.strip() for value in parsed]
    if len(set(normalized)) != len(normalized):
        raise ValueError("question-ID file contains duplicate IDs")
    if not normalized:
        raise ValueError("question-ID file must not be empty")
    return normalized


def _snapshot(directory: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"prediction tree contains a symbolic link: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        entries.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return entries


def _question_file_ids(directory: Path) -> set[str]:
    return {
        path.stem
        for path in directory.glob("*.json")
        if path.is_file() and not path.name.startswith("_")
    }


def prepare_evaluation_copy(
    frozen_directory: Path,
    evaluation_output_root: Path,
    *,
    project_name: str,
    question_id_file: Path,
    expected_questions: int,
    expected_question_ids_sha256: str | None = None,
) -> dict[str, Any]:
    """Copy a frozen prediction tree into a fresh official-harness output root."""

    frozen_directory = frozen_directory.expanduser().resolve()
    evaluation_output_root = evaluation_output_root.expanduser().resolve()
    question_id_file = question_id_file.expanduser().resolve()
    project_name = project_name.strip()

    if not frozen_directory.is_dir():
        raise FileNotFoundError(f"frozen prediction directory does not exist: {frozen_directory}")
    if not question_id_file.is_file():
        raise FileNotFoundError(f"question-ID file does not exist: {question_id_file}")
    if not project_name or "/" in project_name or "\\" in project_name:
        raise ValueError("project_name must be a non-empty path-safe name")
    if expected_questions <= 0:
        raise ValueError("expected_questions must be positive")
    if evaluation_output_root.exists():
        raise FileExistsError(
            f"evaluation output root must be absent (fresh-copy invariant): {evaluation_output_root}"
        )

    expected_folder = f"predicted_{project_name}"
    if frozen_directory.name != expected_folder:
        raise ValueError(
            "frozen prediction directory basename does not match project name: "
            f"{frozen_directory.name!r} != {expected_folder!r}"
        )
    try:
        evaluation_output_root.relative_to(frozen_directory)
    except ValueError:
        pass
    else:
        raise ValueError("evaluation output root may not be inside the frozen prediction tree")

    question_ids = _load_question_ids(question_id_file)
    if len(question_ids) != expected_questions:
        raise ValueError(
            "declared question count does not match question-ID file: "
            f"{expected_questions} != {len(question_ids)}"
        )
    question_ids_sha256 = _sha256_file(question_id_file)
    if (
        expected_question_ids_sha256 is not None
        and question_ids_sha256 != expected_question_ids_sha256.lower()
    ):
        raise ValueError(
            "question-ID checksum mismatch: "
            f"expected {expected_question_ids_sha256.lower()}, got {question_ids_sha256}"
        )

    actual_ids = _question_file_ids(frozen_directory)
    expected_ids = set(question_ids)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        unexpected = sorted(actual_ids - expected_ids)
        raise ValueError(
            "frozen prediction filename scope mismatch: "
            f"missing={missing[:10]} ({len(missing)} total), "
            f"unexpected={unexpected[:10]} ({len(unexpected)} total)"
        )

    source_before = _snapshot(frozen_directory)
    evaluation_output_root.mkdir(parents=True)
    evaluated_directory = evaluation_output_root / expected_folder
    try:
        shutil.copytree(frozen_directory, evaluated_directory, copy_function=shutil.copy2)
        source_after = _snapshot(frozen_directory)
        copied = _snapshot(evaluated_directory)
        if source_before != source_after:
            raise RuntimeError("frozen prediction tree changed while it was being copied")
        if source_after != copied:
            raise RuntimeError("evaluation copy is not byte-identical to frozen predictions")

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "project_name": project_name,
            "expected_questions": expected_questions,
            "question_id_file": str(question_id_file),
            "question_ids_sha256": question_ids_sha256,
            "frozen_directory": str(frozen_directory),
            "evaluated_directory": str(evaluated_directory),
            "file_count": len(source_after),
            "prediction_file_count": len(actual_ids),
            "files": source_after,
            "checks": {
                "fresh_output_root": True,
                "question_scope_exact": True,
                "source_stable_during_copy": True,
                "copy_byte_identical": True,
                "prediction_payloads_parsed": False,
            },
        }
        manifest_path = evaluation_output_root / "frozen-copy-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return manifest
    except Exception:
        shutil.rmtree(evaluation_output_root, ignore_errors=True)
        raise


def verify_frozen_copy_manifest(manifest_path: Path) -> dict[str, Any]:
    """Verify that the immutable source tree still matches a copy manifest."""

    manifest_path = manifest_path.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported paired-evaluation copy manifest")
    frozen_directory = Path(str(manifest.get("frozen_directory") or "")).resolve()
    question_id_file = Path(str(manifest.get("question_id_file") or "")).resolve()
    if not frozen_directory.is_dir() or not question_id_file.is_file():
        raise FileNotFoundError("manifest source prediction tree or question-ID file is missing")

    current_question_ids_sha256 = _sha256_file(question_id_file)
    expected_question_ids_sha256 = str(manifest.get("question_ids_sha256") or "")
    current_files = _snapshot(frozen_directory)
    expected_files = manifest.get("files")
    if current_question_ids_sha256 != expected_question_ids_sha256:
        raise ValueError("question-ID file changed after evaluation copy preparation")
    if current_files != expected_files:
        raise ValueError("frozen prediction tree changed after evaluation copy preparation")
    return {
        "ok": True,
        "manifest": str(manifest_path),
        "frozen_directory": str(frozen_directory),
        "file_count": len(current_files),
        "prediction_file_count": int(manifest.get("prediction_file_count") or 0),
        "question_ids_sha256": current_question_ids_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="make a fresh byte-identical copy")
    prepare.add_argument("--frozen-directory", type=Path, required=True)
    prepare.add_argument("--evaluation-output-root", type=Path, required=True)
    prepare.add_argument("--project-name", required=True)
    prepare.add_argument("--question-id-file", type=Path, required=True)
    prepare.add_argument("--expected-questions", type=int, required=True)
    prepare.add_argument("--expected-question-ids-sha256")

    verify = subparsers.add_parser(
        "verify-frozen", help="recheck the source tree after paid evaluation"
    )
    verify.add_argument("--manifest", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "prepare":
            report = prepare_evaluation_copy(
                args.frozen_directory,
                args.evaluation_output_root,
                project_name=args.project_name,
                question_id_file=args.question_id_file,
                expected_questions=args.expected_questions,
                expected_question_ids_sha256=args.expected_question_ids_sha256,
            )
            output = {
                key: report[key]
                for key in (
                    "project_name",
                    "expected_questions",
                    "question_ids_sha256",
                    "frozen_directory",
                    "evaluated_directory",
                    "file_count",
                    "prediction_file_count",
                    "checks",
                )
            }
        else:
            output = verify_frozen_copy_manifest(args.manifest)
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
