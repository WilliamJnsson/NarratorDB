#!/usr/bin/env python3
"""Run the sealed LongMemEval harness without site startup or dotenv routing."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import runpy
import sys
from pathlib import Path


EXPECTED_BASE_URL = "http://127.0.0.1:8890/v1"
EXPECTED_API_KEY = "local-transport"
FORBIDDEN_ENVIRONMENT = frozenset(
    {
        "ALL_PROXY",
        "ANTHROPIC_API_KEY",
        "CURL_CA_BUNDLE",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "OPENAI_LOG",
        "OPENROUTER_API_KEY",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "REQUESTS_CA_BUNDLE",
        "SSLKEYLOGFILE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "all_proxy",
        "https_proxy",
        "http_proxy",
    }
)


def _required_directory(name: str) -> Path:
    value = os.environ.get(name)
    if not value or "\n" in value or "\r" in value:
        raise RuntimeError(f"missing guarded harness path: {name}")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError(f"unsafe guarded harness path: {name}")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise RuntimeError(f"guarded harness path is not a directory: {name}")
    return resolved


def _assert_route() -> None:
    if os.environ.get("OPENAI_BASE_URL") != EXPECTED_BASE_URL:
        raise RuntimeError("harness route is not the fixed localhost proxy")
    if os.environ.get("OPENAI_API_KEY") != EXPECTED_API_KEY:
        raise RuntimeError("harness transport credential is not the fixed local sentinel")
    inherited = sorted(name for name in FORBIDDEN_ENVIRONMENT if name in os.environ)
    if inherited:
        raise RuntimeError(f"forbidden harness environment variables: {inherited}")
    if os.environ.get("NO_PROXY") != "127.0.0.1,localhost":
        raise RuntimeError("harness NO_PROXY policy changed")
    if os.environ.get("no_proxy") != "127.0.0.1,localhost":
        raise RuntimeError("harness no_proxy policy changed")


def _install_paths(source: Path, site_packages: Path) -> None:
    source_text = str(source)
    site_text = str(site_packages)
    if source_text in sys.path or site_text in sys.path:
        raise RuntimeError("guarded paths were present before explicit installation")
    sys.path.insert(0, source_text)
    sys.path.append(site_text)


def _disable_dotenv() -> None:
    dotenv = importlib.import_module("dotenv")
    dotenv_main = importlib.import_module("dotenv.main")

    def disabled_load_dotenv(*_args: object, **_kwargs: object) -> bool:
        _assert_route()
        return False

    dotenv.load_dotenv = disabled_load_dotenv
    dotenv_main.load_dotenv = disabled_load_dotenv


def _module_path(name: str, expected: Path) -> None:
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        raise RuntimeError(f"guarded module is missing: {name}")
    if Path(spec.origin).resolve(strict=True) != expected.resolve(strict=True):
        raise RuntimeError(f"guarded module provenance changed: {name}")


def _preflight(source: Path) -> None:
    llm_module = importlib.import_module("benchmarks.common.llm_client")
    evaluator_module = importlib.import_module("benchmarks.longmemeval.run")
    _assert_route()
    if Path(llm_module.__file__).resolve(strict=True) != (
        source / "benchmarks/common/llm_client.py"
    ).resolve(strict=True):
        raise RuntimeError("LLM client provenance changed")
    if Path(evaluator_module.__file__).resolve(strict=True) != (
        source / "benchmarks/longmemeval/run.py"
    ).resolve(strict=True):
        raise RuntimeError("evaluator provenance changed")
    client = llm_module.LLMClient
    constructor = inspect.signature(client.__init__).parameters
    generate = inspect.signature(client.generate).parameters
    structured = inspect.signature(client.generate_structured).parameters
    if not (
        constructor["max_retries"].default == 5
        and constructor["timeout"].default == 120.0
        and generate["temperature"].default == 0
        and generate["max_tokens"].default == 4096
        and structured["temperature"].default == 0
        and structured["max_tokens"].default == 4096
    ):
        raise RuntimeError("sealed harness signature defaults changed")
    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "dotenv_disabled": True,
                "harness_source": str(source),
                "route": EXPECTED_BASE_URL,
                "site_startup_disabled": True,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main() -> int:
    if not (
        sys.flags.isolated
        and sys.flags.no_site
        and sys.flags.safe_path
        and sys.flags.dont_write_bytecode
        and "site" not in sys.modules
    ):
        raise RuntimeError("harness guard requires Python -I -S -B")
    source = _required_directory("NARRATORDB_EXPECTED_HARNESS_SOURCE")
    site_packages = _required_directory("NARRATORDB_HARNESS_SITE_PACKAGES")
    _install_paths(source, site_packages)
    _assert_route()
    _disable_dotenv()
    evaluator = (source / "benchmarks/longmemeval/run.py").resolve(strict=True)
    _module_path("benchmarks.longmemeval.run", evaluator)
    if sys.argv[1:] == ["--narratordb-preflight"]:
        _preflight(source)
        return 0
    runpy.run_module("benchmarks.longmemeval.run", run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
