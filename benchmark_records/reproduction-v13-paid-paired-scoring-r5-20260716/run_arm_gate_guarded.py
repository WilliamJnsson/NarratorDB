#!/usr/bin/env python3
"""Run the sealed score-blind R5 per-arm gate in synthetic packages."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import types
from pathlib import Path


ARM_GATE_SHA256 = "2f71497c480ed25851b18d0b87beb408c39e012e65d3000df3bbaf54b95e2971"
EVALUATION_AUDIT_SHA256 = (
    "011708f614bee9cfc15209986bc68969f3c70191a06f540d3a86a2d7f74aeefc"
)


def _sealed_file(path: Path, expected_sha256: str) -> Path:
    if path.is_symlink():
        raise RuntimeError(f"sealed arm-gate input is a symlink: {path.name}")
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.parent != path.parent.resolve(strict=True):
        raise RuntimeError(f"sealed arm-gate input is unsafe: {path.name}")
    if hashlib.sha256(resolved.read_bytes()).hexdigest() != expected_sha256:
        raise RuntimeError(f"sealed arm-gate input checksum mismatch: {path.name}")
    return resolved


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to construct sealed module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    if Path(module.__file__).resolve(strict=True) != path:
        raise RuntimeError(f"sealed module provenance changed: {name}")
    return module


def main() -> int:
    if not (
        sys.flags.isolated
        and sys.flags.no_site
        and sys.flags.safe_path
        and sys.flags.dont_write_bytecode
        and "site" not in sys.modules
    ):
        raise RuntimeError("R5 arm-gate guard requires Python -I -S -B")

    root = Path(__file__).resolve(strict=True).parent
    evaluator_path = _sealed_file(
        root / "evaluation_audit_runtime.py", EVALUATION_AUDIT_SHA256
    )
    arm_gate_path = _sealed_file(root / "arm_gate_runtime.py", ARM_GATE_SHA256)
    for name in (
        "narratordb",
        "narratordb.benchmarks",
        "narratordb.benchmarks.evaluation_audit",
        "narratordb.benchmarks.arm_gate",
    ):
        if name in sys.modules:
            raise RuntimeError(f"sealed module was imported before guard: {name}")

    package = types.ModuleType("narratordb")
    package.__path__ = [str(root)]
    package.__package__ = "narratordb"
    benchmarks = types.ModuleType("narratordb.benchmarks")
    benchmarks.__path__ = [str(root)]
    benchmarks.__package__ = "narratordb.benchmarks"
    sys.modules["narratordb"] = package
    sys.modules["narratordb.benchmarks"] = benchmarks

    _load("narratordb.benchmarks.evaluation_audit", evaluator_path)
    arm_gate = _load("narratordb.benchmarks.arm_gate", arm_gate_path)
    entrypoint = getattr(arm_gate, "main", None)
    if not callable(entrypoint):
        raise RuntimeError("sealed arm-gate entrypoint is missing")
    result = entrypoint()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
