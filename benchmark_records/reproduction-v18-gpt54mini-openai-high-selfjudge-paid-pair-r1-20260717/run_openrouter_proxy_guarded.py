#!/usr/bin/env python3
"""Run the checksum-pinned OpenRouter transport in synthetic packages."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import types
from pathlib import Path

CONFIG_SHA256 = "5b4dd9b39d221489ceca98a53ad89d565b993047c1c3a2abef2801f62b49a9c3"
PROXY_SHA256 = "7ce24f80cb48843c8956e24eba62c215fabac3d97e673dd355ab7051cbc64948"


def _file(path: Path, digest: str) -> Path:
    if path.is_symlink():
        raise RuntimeError("sealed input is a symlink")
    resolved = path.resolve(strict=True)
    if hashlib.sha256(resolved.read_bytes()).hexdigest() != digest:
        raise RuntimeError("sealed input checksum mismatch")
    return resolved


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to construct sealed module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if not (
        sys.flags.isolated
        and sys.flags.no_site
        and sys.flags.safe_path
        and sys.flags.dont_write_bytecode
    ):
        raise RuntimeError("proxy guard requires Python -I -S -B")
    root = Path(__file__).resolve(strict=True).parents[2]
    package = types.ModuleType("narratordb")
    package.__path__ = [str(root / "narratordb")]
    package.__package__ = "narratordb"
    benchmarks = types.ModuleType("narratordb.benchmarks")
    benchmarks.__path__ = [str(root / "narratordb/benchmarks")]
    benchmarks.__package__ = "narratordb.benchmarks"
    sys.modules["narratordb"] = package
    sys.modules["narratordb.benchmarks"] = benchmarks
    _load("narratordb.config", _file(root / "narratordb/config.py", CONFIG_SHA256))
    proxy = _load(
        "narratordb.benchmarks.openrouter_proxy",
        _file(root / "narratordb/benchmarks/openrouter_proxy.py", PROXY_SHA256),
    )
    result = proxy.main()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
