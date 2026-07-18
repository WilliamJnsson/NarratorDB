"""Install NarratorDB's stdio MCP server in supported coding clients.

The installer deliberately delegates configuration writes to each client's
native CLI.  It never edits client configuration files and never places API
keys or other credentials in registration arguments.
"""

from __future__ import annotations

import getpass
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Sequence

from .config import (
    CompilerConfig,
    ConfigurationError,
    FeatureUnavailableError,
    MemoryMode,
    ProjectConfigStore,
    default_db_path,
    default_user_id,
    normalize_mode,
)
from .database import NarratorDB


MCP_SERVER_NAME = "narratordb"
SUPPORTED_CLIENTS = ("codex", "claude")
SERVICE_PLUGIN_ID = "narratordb-service@narratordb-plugins"
LOCAL_PLUGIN_ID = "narratordb@narratordb-plugins"
NARRATORDB_MARKETPLACE = "narratordb-plugins"
NARRATORDB_MARKETPLACE_SOURCE = "WilliamJnsson/NarratorDB"

_NOT_FOUND_MARKER = "no mcp server named"


def _validate_mcp_extra() -> None:
    """Fail before client registration when the optional MCP runtime is absent."""

    try:
        importlib.import_module("mcp.server.fastmcp")
    except (ImportError, ModuleNotFoundError) as error:
        raise FeatureUnavailableError(
            "MCP support is not installed; install it with "
            "`python -m pip install 'narratordb-memory[mcp]'`"
        ) from error


def _normalize_client(client: str) -> str:
    normalized = str(client).strip().lower()
    if normalized not in SUPPORTED_CLIENTS:
        choices = ", ".join(SUPPORTED_CLIENTS)
        raise ConfigurationError(f"MCP client must be one of: {choices}")
    return normalized


def _normalize_path(path: str | os.PathLike[str] | None) -> str:
    value = os.fspath(path or default_db_path())
    if value == ":memory:":
        raise ConfigurationError("MCP installation requires a persistent database path")
    return str(Path(value).expanduser().resolve())


def _normalize_user_id(user_id: str | None) -> str:
    value = user_id if user_id is not None else default_user_id(getpass.getuser())
    normalized = str(value).strip()
    if not normalized:
        raise ConfigurationError("MCP installation requires a non-empty user ID")
    return normalized


def _ensure_client_available(client: str) -> None:
    if shutil.which(client) is None:
        raise ConfigurationError(
            f"{client} is not installed or is not available on PATH"
        )


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one native client command without a shell or ambient string parsing."""

    try:
        return subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ConfigurationError(
            f"could not execute {command[0]!r} while configuring MCP"
        ) from error


def _check_command(client: str) -> list[str]:
    if client == "codex":
        return ["codex", "mcp", "get", MCP_SERVER_NAME, "--json"]
    return ["claude", "mcp", "get", MCP_SERVER_NAME]


def _remove_command(client: str) -> list[str]:
    if client == "codex":
        return ["codex", "mcp", "remove", MCP_SERVER_NAME]
    return [
        "claude",
        "mcp",
        "remove",
        "--scope",
        "user",
        MCP_SERVER_NAME,
    ]


def _server_command(
    *,
    path: str,
    user_id: str,
    mode: MemoryMode,
    client: str,
    allow_path_fallback_writes: bool = False,
) -> list[str]:
    # Preserve a virtual-environment interpreter symlink: resolving it to the
    # base interpreter can lose the environment that contains NarratorDB/MCP.
    python = os.path.abspath(sys.executable)
    command = [
        python,
        "-m",
        "narratordb.mcp_server",
        "--path",
        path,
        "--user-id",
        user_id,
        "--init-mode",
        mode.value,
        "--client",
        client,
    ]
    if allow_path_fallback_writes:
        command.append("--allow-path-fallback-writes")
    return command


def _add_command(client: str, server_command: Sequence[str]) -> list[str]:
    if client == "codex":
        return [
            "codex",
            "mcp",
            "add",
            MCP_SERVER_NAME,
            "--",
            *server_command,
        ]
    return [
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        MCP_SERVER_NAME,
        "--",
        *server_command,
    ]


def _restart_instruction(client: str) -> str:
    if client == "codex":
        return (
            "Restart Codex or open a new session, then use /mcp to verify "
            "NarratorDB tools."
        )
    return (
        "Restart Claude Code or open a new session, then use /mcp to verify "
        "NarratorDB tools."
    )


def _is_registered(client: str) -> tuple[bool, list[str]]:
    command = _check_command(client)
    completed = _run(command)
    if completed.returncode == 0:
        return True, command
    message = f"{completed.stdout}\n{completed.stderr}".casefold()
    if _NOT_FOUND_MARKER in message:
        return False, command
    raise ConfigurationError(
        f"could not inspect {client} MCP configuration "
        f"(exit status {completed.returncode})"
    )


def _codex_plugin_inventory() -> tuple[set[str], set[str]]:
    completed = _run(["codex", "plugin", "list", "--available", "--json"])
    if completed.returncode != 0:
        raise ConfigurationError(
            "could not inspect Codex plugins while configuring service capture"
        )
    try:
        payload = json.loads(completed.stdout)
        installed = {
            str(item["pluginId"])
            for item in payload.get("installed", [])
            if isinstance(item, dict) and item.get("installed") is True
        }
        available = {
            str(item["pluginId"])
            for item in payload.get("available", [])
            if isinstance(item, dict)
        }
    except (json.JSONDecodeError, TypeError, KeyError) as error:
        raise ConfigurationError("Codex returned malformed plugin inventory") from error
    return installed, available


def _preflight_codex_service_plugin(*, force: bool) -> None:
    installed, _ = _codex_plugin_inventory()
    if LOCAL_PLUGIN_ID in installed and not force:
        raise ConfigurationError(
            f"{LOCAL_PLUGIN_ID!r} is installed and writes to a different local "
            "database; use --replace-codex to replace it with the service plugin"
        )


def _ensure_codex_service_plugin(*, force: bool) -> dict[str, Any]:
    """Install the service-only hooks and remove the conflicting local plugin."""

    installed, available = _codex_plugin_inventory()
    local_conflict = LOCAL_PLUGIN_ID in installed
    if local_conflict and not force:
        raise ConfigurationError(
            f"{LOCAL_PLUGIN_ID!r} is installed and writes to a different local "
            "database; use --replace-codex to replace it with the service plugin"
        )

    marketplace_changed = False
    if SERVICE_PLUGIN_ID not in installed | available:
        listed = _run(["codex", "plugin", "marketplace", "list"])
        if listed.returncode != 0:
            raise ConfigurationError("could not inspect Codex plugin marketplaces")
        marketplace_names = {
            line.split(None, 1)[0]
            for line in listed.stdout.splitlines()[1:]
            if line.strip()
        }
        if NARRATORDB_MARKETPLACE in marketplace_names:
            marketplace_command = [
                "codex",
                "plugin",
                "marketplace",
                "upgrade",
                NARRATORDB_MARKETPLACE,
            ]
        else:
            marketplace_command = [
                "codex",
                "plugin",
                "marketplace",
                "add",
                NARRATORDB_MARKETPLACE_SOURCE,
            ]
        configured = _run(marketplace_command)
        if configured.returncode != 0:
            raise ConfigurationError(
                "could not configure the NarratorDB Codex plugin marketplace"
            )
        marketplace_changed = True
        installed, available = _codex_plugin_inventory()

    plugin_added = False
    if SERVICE_PLUGIN_ID not in installed:
        if SERVICE_PLUGIN_ID not in available:
            raise ConfigurationError(
                "the configured NarratorDB marketplace does not contain the "
                "service plugin; upgrade NarratorDB and retry"
            )
        added = _run(["codex", "plugin", "add", SERVICE_PLUGIN_ID])
        if added.returncode != 0:
            raise ConfigurationError("could not install the NarratorDB service plugin")
        plugin_added = True

    local_removed = False
    if local_conflict:
        removed = _run(["codex", "plugin", "remove", LOCAL_PLUGIN_ID])
        if removed.returncode != 0:
            if plugin_added:
                _run(["codex", "plugin", "remove", SERVICE_PLUGIN_ID])
            raise ConfigurationError(
                "could not remove the conflicting local NarratorDB plugin"
            )
        local_removed = True

    return {
        "plugin": SERVICE_PLUGIN_ID,
        "installed": True,
        "changed": plugin_added or local_removed or marketplace_changed,
        "local_plugin_removed": local_removed,
    }


def _preflight_database(
    path: str,
    mode: MemoryMode,
    compiler: CompilerConfig | None,
) -> tuple[bool, str | None, CompilerConfig | None]:
    """Validate a requested mode without creating or modifying the database."""

    store = ProjectConfigStore(path)
    config = store.load()
    if config is None:
        if mode is MemoryMode.PRIVATE and compiler is not None:
            raise ConfigurationError("private mode cannot configure a memory compiler")
        if mode is MemoryMode.INTELLIGENCE and compiler is None:
            raise ConfigurationError(
                "a new intelligence-mode MCP database needs a compiler first; run "
                "this install with --mode intelligence --compiler ..."
            )
        return True, None, compiler
    if config.mode is not mode:
        raise ConfigurationError(
            f"database is configured for {config.mode.value!r}, not {mode.value!r}; "
            "change it explicitly with `narratordb mode` before MCP installation"
        )
    if compiler is not None and compiler != config.compiler:
        raise ConfigurationError(
            "database already has a different compiler configuration; change it "
            "explicitly with `narratordb mode` before MCP installation"
        )
    return False, config.mode.value, config.compiler


def _initialize_database(
    path: str,
    user_id: str,
    mode: MemoryMode,
    compiler: CompilerConfig | None,
) -> dict[str, Any]:
    with NarratorDB(
        db_path=path,
        user_id=user_id,
        mode=mode,
        compiler=compiler,
    ) as memory:
        status = memory.project_status()
    return {
        "path": path,
        "user_id": user_id,
        "mode": str(status["mode"]),
        "compiler": status.get("compiler"),
        "initialized": True,
    }


def install_mcp_client(
    client: str,
    *,
    path: str | os.PathLike[str] | None = None,
    user_id: str | None = None,
    mode: str | MemoryMode | None = None,
    compiler: CompilerConfig | None = None,
    force: bool = False,
    dry_run: bool = False,
    allow_path_fallback_writes: bool = False,
) -> dict[str, Any]:
    """Register NarratorDB with Codex or Claude Code using its native CLI."""

    normalized_client = _normalize_client(client)
    normalized_path = _normalize_path(path)
    normalized_user_id = _normalize_user_id(user_id)

    _validate_mcp_extra()
    _ensure_client_available(normalized_client)

    registered, check_command = _is_registered(normalized_client)
    if registered and not force:
        raise ConfigurationError(
            f"{MCP_SERVER_NAME!r} is already registered with {normalized_client}; "
            "use --force to replace it"
        )

    if mode is not None:
        normalized_mode = normalize_mode(mode)
    else:
        store = ProjectConfigStore(normalized_path)
        current = store.load()
        if current is not None:
            normalized_mode = current.mode
        elif store.is_legacy_database():
            normalized_mode = MemoryMode.PRIVATE
        else:
            raise ConfigurationError(
                "mode is required for a new MCP database; choose 'private' or "
                "'intelligence' explicitly"
            )

    would_initialize, configured_mode, configured_compiler = _preflight_database(
        normalized_path, normalized_mode, compiler
    )
    server_command = _server_command(
        path=normalized_path,
        user_id=normalized_user_id,
        mode=normalized_mode,
        client=normalized_client,
        allow_path_fallback_writes=bool(allow_path_fallback_writes),
    )
    add_command = _add_command(normalized_client, server_command)
    remove_command = _remove_command(normalized_client) if registered else None

    commands: dict[str, list[str] | None] = {
        "check": check_command,
        "remove": remove_command,
        "add": add_command,
    }
    if dry_run:
        return {
            "action": "install",
            "client": normalized_client,
            "name": MCP_SERVER_NAME,
            "status": "would_replace" if registered else "would_install",
            "changed": False,
            "dry_run": True,
            "database": {
                "path": normalized_path,
                "user_id": normalized_user_id,
                "mode": normalized_mode.value,
                "compiler": (
                    configured_compiler.to_dict() if configured_compiler else None
                ),
                "initialized": not would_initialize,
                "would_initialize": would_initialize,
                "configured_mode": configured_mode,
            },
            "server": {"transport": "stdio", "command": server_command},
            "commands": commands,
            "restart": _restart_instruction(normalized_client),
        }

    database = _initialize_database(
        normalized_path, normalized_user_id, normalized_mode, compiler
    )
    database["created_for_install"] = would_initialize

    if remove_command is not None:
        removed = _run(remove_command)
        if removed.returncode != 0:
            raise ConfigurationError(
                f"could not replace {MCP_SERVER_NAME!r}: {normalized_client} "
                f"remove failed with exit status {removed.returncode}"
            )

    added = _run(add_command)
    if added.returncode != 0:
        raise ConfigurationError(
            f"{normalized_client} MCP registration failed with exit status "
            f"{added.returncode}"
        )

    return {
        "action": "install",
        "client": normalized_client,
        "name": MCP_SERVER_NAME,
        "status": "replaced" if registered else "installed",
        "changed": True,
        "dry_run": False,
        "database": database,
        "server": {"transport": "stdio", "command": server_command},
        "commands": commands,
        "restart": _restart_instruction(normalized_client),
    }


def install_service_bridge(
    credentials_file: str | os.PathLike[str],
    *,
    client: str = "codex",
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Register the credential-file service bridge without exposing its token."""

    normalized_client = _normalize_client(client)
    _validate_mcp_extra()
    _ensure_client_available(normalized_client)
    credentials = str(Path(credentials_file).expanduser().resolve())
    if not Path(credentials).is_file():
        raise ConfigurationError("service credentials file was not found")
    if normalized_client == "codex":
        _preflight_codex_service_plugin(force=force)

    python = os.path.abspath(sys.executable)
    server_command = [
        python,
        "-m",
        "narratordb.service_bridge",
        "--credentials-file",
        credentials,
    ]
    registered, check_command = _is_registered(normalized_client)
    already_current = False
    if registered and normalized_client == "codex":
        inspected = _run(check_command)
        try:
            current = json.loads(inspected.stdout)
            transport = current["transport"]
            already_current = (
                transport.get("type") == "stdio"
                and transport.get("command") == server_command[0]
                and transport.get("args") == server_command[1:]
            )
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            already_current = False
        if not already_current and not force:
            raise ConfigurationError(
                f"{MCP_SERVER_NAME!r} is already registered with "
                f"{normalized_client}; use the explicit replacement option"
            )
    elif registered and not force:
        raise ConfigurationError(
            f"{MCP_SERVER_NAME!r} is already registered with {normalized_client}; "
            "use the explicit replacement option"
        )

    add_command = _add_command(normalized_client, server_command)
    remove_command = (
        _remove_command(normalized_client) if registered and not already_current else None
    )
    commands = {
        "check": check_command,
        "remove": remove_command,
        "add": add_command,
    }
    if dry_run:
        return {
            "action": "install-service-bridge",
            "client": normalized_client,
            "name": MCP_SERVER_NAME,
            "status": (
                "already_installed"
                if already_current
                else "would_replace" if registered else "would_install"
            ),
            "changed": False,
            "dry_run": True,
            "server": {"transport": "stdio", "command": server_command},
            "commands": commands,
            "restart": _restart_instruction(normalized_client),
        }

    from .service_hook import write_service_hook_config

    if already_current:
        hook_config = write_service_hook_config(credentials)
        plugin = _ensure_codex_service_plugin(force=force)
        return {
            "action": "install-service-bridge",
            "client": normalized_client,
            "name": MCP_SERVER_NAME,
            "status": "already_installed",
            "changed": False,
            "dry_run": False,
            "server": {"transport": "stdio", "command": server_command},
            "service_hook_config": str(hook_config),
            "lifecycle_plugin": plugin,
            "commands": commands,
            "restart": None,
        }

    if remove_command is not None:
        removed = _run(remove_command)
        if removed.returncode != 0:
            raise ConfigurationError(
                f"could not replace {MCP_SERVER_NAME!r}: {normalized_client} remove failed "
                f"with exit status {removed.returncode}"
            )
    added = _run(add_command)
    if added.returncode != 0:
        raise ConfigurationError(
            f"{normalized_client} MCP registration failed with exit status "
            f"{added.returncode}"
        )
    hook_config = (
        write_service_hook_config(credentials)
        if normalized_client == "codex"
        else None
    )
    plugin = (
        _ensure_codex_service_plugin(force=force)
        if normalized_client == "codex"
        else None
    )
    return {
        "action": "install-service-bridge",
        "client": normalized_client,
        "name": MCP_SERVER_NAME,
        "status": "replaced" if registered else "installed",
        "changed": True,
        "dry_run": False,
        "server": {"transport": "stdio", "command": server_command},
        "service_hook_config": str(hook_config) if hook_config is not None else None,
        "lifecycle_plugin": plugin,
        "automatic_capture": normalized_client == "codex",
        "commands": commands,
        "restart": _restart_instruction(normalized_client),
    }


def install_remote_service(
    client: str,
    *,
    endpoint: str,
    project_id: str,
    credentials_file: str | os.PathLike[str],
    token: str,
    force: bool = False,
) -> dict[str, Any]:
    """Verify a hosted service and register its credential-file bridge."""

    from .service_bridge import (
        ServiceBridgeRuntime,
        read_service_credentials,
        write_service_credentials,
    )

    normalized_client = _normalize_client(client)
    written = write_service_credentials(
        credentials_file,
        service_url=endpoint,
        token=token,
        project_id=project_id,
    )
    try:
        credentials = read_service_credentials(written)
        status = ServiceBridgeRuntime(written).status(scope="project", full_check=False)
        expected_workspace = f"project/{credentials['NARRATORDB_PROJECT_ID']}"
        actual_workspace = status.get("workspace_id")
        actual_project = status.get("project_id")
        if status.get("ready") is not True:
            raise ConfigurationError("remote NarratorDB status is not ready")
        if actual_workspace != expected_workspace and actual_project != credentials[
            "NARRATORDB_PROJECT_ID"
        ]:
            raise ConfigurationError(
                "remote NarratorDB credential resolved to a different project"
            )
    except (ConfigurationError, KeyboardInterrupt):
        written.unlink(missing_ok=True)
        raise
    except Exception as error:
        written.unlink(missing_ok=True)
        raise ConfigurationError("remote NarratorDB status verification failed") from error
    installed = install_service_bridge(
        written,
        client=normalized_client,
        force=force,
    )
    return {
        **installed,
        "action": "install-remote",
        "endpoint": credentials["NARRATORDB_SERVICE_URL"],
        "project_id": credentials["NARRATORDB_PROJECT_ID"],
        "credentials_file": str(written),
        "verified": True,
    }


def uninstall_mcp_client(
    client: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove NarratorDB's MCP registration without deleting memory data."""

    normalized_client = _normalize_client(client)
    _ensure_client_available(normalized_client)
    registered, check_command = _is_registered(normalized_client)
    remove_command = _remove_command(normalized_client)

    if not registered:
        return {
            "action": "uninstall",
            "client": normalized_client,
            "name": MCP_SERVER_NAME,
            "status": "not_installed",
            "changed": False,
            "dry_run": dry_run,
            "commands": {"check": check_command, "remove": None},
            "data_preserved": True,
            "restart": None,
        }

    if not dry_run:
        removed = _run(remove_command)
        if removed.returncode != 0:
            raise ConfigurationError(
                f"{normalized_client} MCP removal failed with exit status "
                f"{removed.returncode}"
            )

    return {
        "action": "uninstall",
        "client": normalized_client,
        "name": MCP_SERVER_NAME,
        "status": "would_uninstall" if dry_run else "uninstalled",
        "changed": not dry_run,
        "dry_run": dry_run,
        "commands": {"check": check_command, "remove": remove_command},
        "data_preserved": True,
        "restart": _restart_instruction(normalized_client),
    }


__all__ = [
    "MCP_SERVER_NAME",
    "SUPPORTED_CLIENTS",
    "install_mcp_client",
    "install_remote_service",
    "install_service_bridge",
    "uninstall_mcp_client",
]
