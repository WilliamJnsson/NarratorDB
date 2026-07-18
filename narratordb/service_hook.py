#!/usr/bin/env python3
"""Fail-open lifecycle capture for the authenticated NarratorDB service."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any, Sequence

from .config import ConfigurationError
from .hooks import (
    MAX_EVENT_BYTES,
    _automatic_capture_enabled,
    normalize_event,
    prepare_session_capture,
)
from .service_bridge import ServiceBridgeRuntime, read_service_credentials


DEFAULT_SERVICE_HOOK_CONFIG = Path.home() / ".narratordb" / "service-hook.json"
_CONFIG_KEYS = frozenset({"credentials_file", "python"})


def write_service_hook_config(
    credentials_file: str | os.PathLike[str],
    *,
    target: str | os.PathLike[str] = DEFAULT_SERVICE_HOOK_CONFIG,
    python: str | os.PathLike[str] = sys.executable,
) -> Path:
    """Atomically write a secret-free pointer used by the service hook plugin."""

    requested_credentials = Path(credentials_file).expanduser()
    read_service_credentials(requested_credentials)
    credentials = requested_credentials.resolve()
    # Preserve a virtual-environment interpreter symlink. Resolving it to the
    # base interpreter can drop the environment containing NarratorDB/MCP.
    executable = Path(os.path.abspath(Path(python).expanduser()))
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ConfigurationError("service hook Python executable is unavailable")

    destination = Path(target).expanduser()
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ConfigurationError("service hook configuration cannot be a symlink")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    payload = json.dumps(
        {
            "credentials_file": str(credentials),
            "python": str(executable),
        },
        sort_keys=True,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if destination.is_symlink():
            raise ConfigurationError("service hook configuration cannot be a symlink")
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return destination


def read_service_hook_config(
    path: str | os.PathLike[str] = DEFAULT_SERVICE_HOOK_CONFIG,
) -> dict[str, str]:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise ConfigurationError("service hook configuration cannot be a symlink")
    target = requested.resolve()
    if not target.is_file():
        raise ConfigurationError("service hook configuration was not found")
    if os.name != "nt" and stat.S_IMODE(target.stat().st_mode) & 0o077:
        raise ConfigurationError("service hook configuration must have mode 0600")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError("service hook configuration is malformed") from error
    if not isinstance(payload, dict) or payload.keys() != _CONFIG_KEYS:
        raise ConfigurationError("service hook configuration is malformed")
    values = {key: str(payload[key] or "").strip() for key in _CONFIG_KEYS}
    if not all(values.values()):
        raise ConfigurationError("service hook configuration is incomplete")
    executable = Path(values["python"])
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise ConfigurationError("service hook Python executable is unavailable")
    read_service_credentials(values["credentials_file"])
    return values


def run_service_hook(
    event_name: str,
    event: dict[str, Any],
    *,
    credentials_file: str | os.PathLike[str],
) -> None:
    canonical = normalize_event(event_name)
    if canonical not in {"PreCompact", "Stop"}:
        return
    if not _automatic_capture_enabled():
        return
    messages, session_id = prepare_session_capture(event)
    if not messages or session_id is None:
        return
    runtime = ServiceBridgeRuntime(credentials_file)
    status = runtime.status(scope="project", full_check=False)
    if status.get("capture_policy") != "sessions":
        return
    runtime.remember_session(
        messages,
        session_id=session_id,
        scope="project",
        wait_for_enrichment=False,
    )


def _read_event() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
    if len(raw) > MAX_EVENT_BYTES:
        raise ValueError("hook event exceeds the 1 MB limit")
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("hook event must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="narratordb-service-hook",
        description="capture one lifecycle event into an authenticated service",
    )
    parser.add_argument("event")
    parser.add_argument("--credentials-file", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_service_hook(
            args.event,
            _read_event(),
            credentials_file=args.credentials_file,
        )
    except Exception as error:  # Hooks must never block an agent turn.
        if os.getenv("NARRATORDB_DEBUG"):
            print(f"narratordb-service-hook: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
