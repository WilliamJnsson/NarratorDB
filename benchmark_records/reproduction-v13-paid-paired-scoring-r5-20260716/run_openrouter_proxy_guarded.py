#!/usr/bin/env python3
"""Run the two-file sealed R5 OpenRouter transport in synthetic packages."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import types
from pathlib import Path


CONFIG_SHA256 = "5b4dd9b39d221489ceca98a53ad89d565b993047c1c3a2abef2801f62b49a9c3"
PROXY_SHA256 = "cd0909f22ae2b3771a7f13c5eccf08aa980c198470015863f9d80a5b80aad44a"


def _sealed_file(path: Path, expected_sha256: str) -> Path:
    if path.is_symlink():
        raise RuntimeError(f"sealed runtime input is a symlink: {path.name}")
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.parent != path.parent.resolve(strict=True):
        raise RuntimeError(f"sealed runtime input is unsafe: {path.name}")
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise RuntimeError(f"sealed runtime input checksum mismatch: {path.name}")
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
        raise RuntimeError("R5 proxy guard requires Python -I -S -B")

    root = Path(__file__).resolve(strict=True).parent
    config_path = _sealed_file(root / "config_runtime.py", CONFIG_SHA256)
    proxy_path = _sealed_file(root / "openrouter_proxy_runtime.py", PROXY_SHA256)

    for name in (
        "narratordb",
        "narratordb.config",
        "narratordb.benchmarks",
        "narratordb.benchmarks.openrouter_proxy",
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

    _load("narratordb.config", config_path)
    proxy = _load("narratordb.benchmarks.openrouter_proxy", proxy_path)
    entrypoint = getattr(proxy, "main", None)
    if not callable(entrypoint):
        raise RuntimeError("sealed proxy entrypoint is missing")
    result = entrypoint()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
