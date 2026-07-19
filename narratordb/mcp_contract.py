"""Stable extension contract for NarratorDB MCP runtimes.

Hosted and third-party runtimes can implement :class:`MCPRuntimeProtocol` and
reuse NarratorDB's fixed seven-tool FastMCP surface without importing private
implementation helpers.  This module is intentionally dependency-light; MCP
itself is loaded only when :func:`create_server` is called.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

from .intelligence import estimate_tokens


MAX_MEMORY_CHARS = 100_000
MAX_SESSION_CHARS = 500_000
MAX_SESSION_MESSAGES = 100
ALLOWED_SOURCES = frozenset({"user", "assistant", "memory"})

MemoryScope = Literal["project", "global"]
MemorySource = Literal["user", "assistant", "memory"]
MemoryModeValue = Literal["private", "intelligence"]
CapturePolicyValue = Literal["manual", "preferences", "sessions"]
CompilerValue = Literal["local", "openai", "openrouter", "codex-cli"]
DerivedDataPolicy = Literal["retain", "purge"]

MCP_TOOL_NAMES = (
    "configure",
    "remember",
    "remember_session",
    "recall",
    "resume",
    "forget",
    "status",
)


def _schema(
    *, required: tuple[str, ...], properties: Mapping[str, str]
) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "type": "object",
            "required": required,
            "properties": MappingProxyType(dict(properties)),
        }
    )


# A compact, immutable description of the stable tool inputs. FastMCP remains
# the canonical JSON Schema generator; this map lets extension runtimes check
# compatibility without importing FastMCP or reaching into decorated closures.
MCP_TOOL_INPUT_SCHEMAS: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "configure": _schema(
            required=(),
            properties={
                "mode": "private|intelligence|null",
                "capture_policy": "manual|preferences|sessions|null",
                "compiler": "local|openai|openrouter|codex-cli|null",
                "model": "string|null",
                "endpoint": "string|null",
                "provider": "string|null",
                "reasoning": "string|null",
                "derived_data": "retain|purge",
            },
        ),
        "remember": _schema(
            required=("content",),
            properties={
                "content": "string",
                "scope": "project|global",
                "source": "user|assistant|memory",
            },
        ),
        "remember_session": _schema(
            required=("messages", "session_id"),
            properties={
                "messages": "array<object>",
                "session_id": "string",
                "scope": "project|global",
                "wait_for_enrichment": "boolean",
            },
        ),
        "recall": _schema(
            required=("query",),
            properties={
                "query": "string",
                "scope": "project|global",
                "include_global": "boolean",
                "token_budget": "integer",
                "explain": "boolean",
            },
        ),
        "resume": _schema(
            required=(),
            properties={
                "topic": "string",
                "include_global": "boolean",
                "token_budget": "integer",
            },
        ),
        "forget": _schema(
            required=("message_id",),
            properties={
                "message_id": "integer",
                "scope": "project|global",
                "confirm": "boolean",
            },
        ),
        "status": _schema(
            required=(),
            properties={"scope": "project|global", "full_check": "boolean"},
        ),
    }
)


def bounded_text(value: str, *, field: str, maximum: int) -> str:
    """Normalize a required string and enforce a caller-supplied hard limit."""

    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} cannot be empty")
    if len(text) > maximum:
        raise ValueError(f"{field} exceeds the {maximum:,}-character limit")
    return text


def bounded_int(value: int, *, field: str, minimum: int, maximum: int) -> int:
    """Validate an integer without accepting booleans as integers."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


@runtime_checkable
class MCPRuntimeProtocol(Protocol):
    """Behavior required by NarratorDB's fixed MCP tool surface."""

    def configure(
        self,
        *,
        mode: str | None = None,
        capture_policy: str | None = None,
        compiler: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        provider: str | None = None,
        reasoning: str | None = None,
        derived_data: str = "retain",
    ) -> dict[str, Any]: ...

    def remember(
        self, content: str, *, scope: str = "project", source: str = "user"
    ) -> dict[str, Any]: ...

    def remember_session(
        self,
        messages: list[dict[str, Any]],
        *,
        session_id: str,
        scope: str = "project",
        wait_for_enrichment: bool = False,
    ) -> dict[str, Any]: ...

    def recall(
        self,
        query: str,
        *,
        scope: str = "project",
        include_global: bool = True,
        token_budget: int = 1600,
        explain: bool = False,
    ) -> dict[str, Any]: ...

    def resume(
        self,
        *,
        topic: str = "",
        include_global: bool = True,
        token_budget: int = 2000,
    ) -> dict[str, Any]: ...

    def forget(
        self, message_id: int, *, scope: str = "project", confirm: bool = False
    ) -> dict[str, Any]: ...

    def status(
        self, *, scope: str = "project", full_check: bool = False
    ) -> dict[str, Any]: ...


def create_server(
    runtime: MCPRuntimeProtocol,
    *,
    server_options: dict[str, Any] | None = None,
    include_bootstrap: bool = True,
):
    """Create the standard server while preserving positional runtime callers.

    ``include_bootstrap`` is retained as a compatibility no-op. NarratorDB
    server instructions are permanently static and never include stored data.
    """

    # Lazy import avoids a module cycle while keeping existing
    # ``narratordb.mcp_server.create_server`` callers fully compatible.
    from .mcp_server import create_server as _create_server

    return _create_server(
        runtime,
        server_options=server_options,
        include_bootstrap=include_bootstrap,
    )


__all__ = [
    "ALLOWED_SOURCES",
    "CapturePolicyValue",
    "CompilerValue",
    "DerivedDataPolicy",
    "MAX_MEMORY_CHARS",
    "MAX_SESSION_CHARS",
    "MAX_SESSION_MESSAGES",
    "MCPRuntimeProtocol",
    "MCP_TOOL_INPUT_SCHEMAS",
    "MCP_TOOL_NAMES",
    "MemoryModeValue",
    "MemoryScope",
    "MemorySource",
    "bounded_int",
    "bounded_text",
    "create_server",
    "estimate_tokens",
]
