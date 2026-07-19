#!/usr/bin/env python3
"""Credential-file stdio bridge for the authenticated NarratorDB service."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import stat
from typing import Any, Sequence
from urllib.parse import urlparse
import uuid

from .config import ConfigurationError
from .mcp_contract import create_server


REQUIRED_CREDENTIALS = frozenset(
    {
        "NARRATORDB_SERVICE_URL",
        "NARRATORDB_SERVICE_TOKEN",
        "NARRATORDB_PROJECT_ID",
    }
)
_TOKEN_RE = re.compile(r"ndb_[A-Za-z0-9_-]{32,128}")
MAX_CREDENTIAL_BYTES = 16_384
SERVICE_CALL_TIMEOUT_SECONDS = 60.0


def _normalize_service_values(
    *, service_url: str, token: str, project_id: str
) -> dict[str, str]:
    url = str(service_url or "").strip().rstrip("/")
    parsed = urlparse(url)
    loopback = (parsed.hostname or "").casefold() in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
    ):
        raise ConfigurationError(
            "service credentials require an HTTPS URL without embedded credentials, "
            "query, or fragment; HTTP is allowed only for loopback"
        )
    if not parsed.path.endswith("/mcp"):
        raise ConfigurationError("service credentials URL must end in /mcp")
    normalized_token = str(token or "")
    if not _TOKEN_RE.fullmatch(normalized_token):
        raise ConfigurationError("service token is malformed")
    try:
        normalized_project = str(uuid.UUID(str(project_id or "").strip()))
    except (ValueError, AttributeError) as error:
        raise ConfigurationError("service project identity is malformed") from error
    return {
        "NARRATORDB_SERVICE_URL": url,
        "NARRATORDB_SERVICE_TOKEN": normalized_token,
        "NARRATORDB_PROJECT_ID": normalized_project,
    }


def write_service_credentials(
    path: str | os.PathLike[str],
    *,
    service_url: str,
    token: str,
    project_id: str,
) -> Path:
    """Create one private credential file without exposing the token in argv."""

    values = _normalize_service_values(
        service_url=service_url, token=token, project_id=project_id
    )
    requested = Path(path).expanduser()
    if requested.is_symlink() or requested.parent.is_symlink():
        raise ConfigurationError("service credentials cannot be a symbolic link")
    target = requested.resolve()
    if target.exists():
        raise ConfigurationError(f"service credentials file already exists: {target}")
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for key in sorted(REQUIRED_CREDENTIALS):
                handle.write(f"{key}={values[key]}\n")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def read_service_credentials(path: str | os.PathLike[str]) -> dict[str, str]:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise ConfigurationError("service credentials cannot be a symbolic link")
    target = requested.resolve()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags)
    except OSError as error:
        raise ConfigurationError("service credentials file was not found") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_CREDENTIAL_BYTES:
            raise ConfigurationError("service credentials must be a small regular file")
        if os.name != "nt" and stat.S_IMODE(details.st_mode) & 0o077:
            raise ConfigurationError("service credentials must have mode 0600")
        handle = os.fdopen(descriptor, "r", encoding="utf-8")
        descriptor = -1
        try:
            with handle:
                content = handle.read(MAX_CREDENTIAL_BYTES + 1)
        except (OSError, UnicodeError) as error:
            raise ConfigurationError(
                "service credentials file is unreadable"
            ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    values: dict[str, str] = {}
    for line in content.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in REQUIRED_CREDENTIALS or not value:
            raise ConfigurationError("service credentials file is malformed")
        if key in values:
            raise ConfigurationError("service credentials file has duplicate keys")
        values[key] = value
    if values.keys() != REQUIRED_CREDENTIALS:
        raise ConfigurationError("service credentials file is incomplete")

    return _normalize_service_values(
        service_url=values["NARRATORDB_SERVICE_URL"],
        token=values["NARRATORDB_SERVICE_TOKEN"],
        project_id=values["NARRATORDB_PROJECT_ID"],
    )


class ServiceBridgeRuntime:
    """Forward the fixed NarratorDB tool surface without exposing its token."""

    def __init__(self, credentials_file: str | os.PathLike[str]):
        self.credentials_file = str(Path(credentials_file).expanduser().resolve())

    async def _call_async(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float = SERVICE_CALL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        try:
            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as error:  # pragma: no cover - clean wheel behavior
            raise ConfigurationError(
                "NarratorDB service dependencies are missing; install "
                "`narratordb-memory[mcp]`"
            ) from error

        credentials = read_service_credentials(self.credentials_file)
        headers = {
            "Authorization": (f"Bearer {credentials['NARRATORDB_SERVICE_TOKEN']}")
        }
        async with httpx.AsyncClient(
            headers=headers, timeout=timeout_seconds
        ) as client:
            async with streamable_http_client(
                credentials["NARRATORDB_SERVICE_URL"],
                http_client=client,
            ) as streams:
                read_stream, write_stream, _ = streams
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
        if result.isError:
            raise RuntimeError(f"remote NarratorDB tool failed: {name}")
        if not isinstance(result.structuredContent, dict):
            raise RuntimeError(
                f"remote NarratorDB tool returned no structured result: {name}"
            )
        return result.structuredContent

    def _call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float = SERVICE_CALL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        def execute() -> dict[str, Any]:
            async def invoke() -> dict[str, Any]:
                return await self._call_async(
                    name,
                    arguments,
                    timeout_seconds=timeout_seconds,
                )

            return asyncio.run(invoke())

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return execute()
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(execute).result()

    def configure(self, **kwargs) -> dict[str, Any]:
        return self._call("configure", kwargs)

    def remember(self, *args, **kwargs) -> dict[str, Any]:
        content = args[0] if args else kwargs.pop("content")
        return self._call("remember", {"content": content, **kwargs})

    def remember_session(self, *args, **kwargs) -> dict[str, Any]:
        messages = args[0] if args else kwargs.pop("messages")
        return self._call("remember_session", {"messages": messages, **kwargs})

    def recall(self, *args, **kwargs) -> dict[str, Any]:
        query = args[0] if args else kwargs.pop("query")
        return self._call("recall", {"query": query, **kwargs})

    def resume(self, *args, **kwargs) -> dict[str, Any]:
        return self._call("resume", kwargs)

    def forget(self, *args, **kwargs) -> dict[str, Any]:
        message_id = args[0] if args else kwargs.pop("message_id")
        return self._call("forget", {"message_id": message_id, **kwargs})

    def status(self, *args, **kwargs) -> dict[str, Any]:
        return self._call("status", kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="narratordb service bridge",
        description="bridge local stdio MCP to an authenticated NarratorDB service",
    )
    parser.add_argument("--credentials-file", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runtime = ServiceBridgeRuntime(args.credentials_file)
        read_service_credentials(args.credentials_file)
        server = create_server(runtime)
        server.run(transport="stdio")
    except ConfigurationError as error:
        print(f"narratordb: {error}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
