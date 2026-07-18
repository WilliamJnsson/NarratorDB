#!/usr/bin/env python3
"""Run only the checksum-pinned sibling R3 official OpenAI proxy."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path


PROXY_SHA256 = "90a342bb7f97162a7af448d26ed191a78c5618a56a8106b9d11868a6a128c253"


def _sealed_file(path: Path) -> Path:
    if path.is_symlink():
        raise RuntimeError("sealed proxy source is a symbolic link")
    resolved = path.resolve(strict=True)
    if not re.fullmatch(r"[0-9a-f]{64}", PROXY_SHA256):
        raise RuntimeError("R3 proxy guard has not been checksum-pinned")
    actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if actual != PROXY_SHA256:
        raise RuntimeError("sealed R3 proxy checksum mismatch")
    return resolved


def main() -> int:
    if not (
        sys.flags.isolated
        and sys.flags.no_site
        and sys.flags.safe_path
        and sys.flags.dont_write_bytecode
    ):
        raise RuntimeError("proxy guard requires Python -I -S -B")
    source = _sealed_file(Path(__file__).with_name("openai_proxy_r3.py"))
    spec = importlib.util.spec_from_file_location(
        "narratordb_r3_official_openai_proxy", source
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to construct sealed R3 proxy module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    result = module.main()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
