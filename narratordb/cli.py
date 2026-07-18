#!/usr/bin/env python3
"""Project setup and dual-mode lifecycle commands."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Sequence

from .config import (
    CapturePolicy,
    DEFAULT_CODEX_CLI_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_OUTPUT_TOKEN_PARAMETER,
    SUPPORTED_OUTPUT_TOKEN_PARAMETERS,
    CompilerConfig,
    CompilerKind,
    ConfigurationError,
    FeatureUnavailableError,
    MemoryMode,
    ProjectConfigStore,
    default_db_path,
)
from .database import NarratorDB
from .mcp_install import (
    SUPPORTED_CLIENTS,
    install_mcp_client,
    install_remote_service,
    install_service_bridge,
    uninstall_mcp_client,
)
from .portability import export_service_project, import_service_project
from .service import (
    DEFAULT_LOCAL_SERVICE_DIR,
    add_project as add_service_project,
    initialize_service,
    issue_project_key,
    prepare_quickstart,
    revoke_project_key,
    serve as serve_service,
)


def _provider_allowlist(value: str) -> tuple[str, ...]:
    providers = tuple(item.strip() for item in value.split(",") if item.strip())
    if not providers:
        raise argparse.ArgumentTypeError("must contain at least one provider")
    if len({provider.casefold() for provider in providers}) != len(providers):
        raise argparse.ArgumentTypeError("providers must be unique")
    return providers


def _add_compiler_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--compiler",
        choices=[kind.value for kind in CompilerKind],
        help="intelligence compiler; credentials always come from the environment",
    )
    parser.add_argument(
        "--model", help="local, OpenAI, OpenRouter, or Codex CLI model identifier"
    )
    parser.add_argument(
        "--endpoint", help="local loopback OpenAI-compatible HTTP endpoint"
    )
    provider_group = parser.add_mutually_exclusive_group()
    provider_group.add_argument(
        "--provider", help="pinned OpenRouter provider (model-specific default: Azure)"
    )
    provider_group.add_argument(
        "--provider-allow",
        type=_provider_allowlist,
        help="ordered OpenRouter endpoint-slug allowlist with contained fallbacks",
    )
    parser.add_argument(
        "--reasoning",
        help=(
            "hosted or Codex CLI reasoning effort "
            "(official OpenAI/Codex GPT-5.4 Mini: low; OpenRouter: minimal)"
        ),
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        help="maximum compiler response tokens (default: 8192)",
    )
    parser.add_argument(
        "--output-token-parameter",
        choices=sorted(SUPPORTED_OUTPUT_TOKEN_PARAMETERS),
        help=(
            "OpenAI-compatible output limit field accepted by the selected route "
            f"(default: {DEFAULT_OUTPUT_TOKEN_PARAMETER})"
        ),
    )
    parser.add_argument("--semantic-max-attempts", type=int)
    parser.add_argument("--transport-max-attempts", type=int)
    parser.add_argument("--retry-delay-seconds", type=float)
    parser.add_argument("--min-request-interval-seconds", type=float)
    parser.add_argument(
        "--capture-router-metadata",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="request content-free route-attempt metadata (hosted default: on)",
    )
    parser.add_argument(
        "--codex-cli-version",
        help="required Codex CLI version identity to persist and verify",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=float,
        help="hard timeout for each Codex CLI invocation (default: 300)",
    )
    parser.add_argument(
        "--codex-max-invocations",
        type=int,
        help="optional aggregate invocation fuse for the Codex compiler",
    )
    parser.add_argument(
        "--codex-max-concurrency",
        type=int,
        help="maximum concurrent Codex CLI processes (default: 1)",
    )


def _select_mode_interactively() -> MemoryMode:
    if not sys.stdin.isatty():
        raise ConfigurationError("--mode is required when stdin is not interactive")
    print("Choose NarratorDB mode:")
    print("  1. private      zero-egress local storage and retrieval")
    print("  2. intelligence local or hosted write-time memory compiler")
    answer = input("Mode [1/2]: ").strip().lower()
    if answer in {"1", "private", "p"}:
        return MemoryMode.PRIVATE
    if answer in {"2", "intelligence", "i"}:
        return MemoryMode.INTELLIGENCE
    raise ConfigurationError("mode selection must be 1/private or 2/intelligence")


def _select_compiler_interactively() -> CompilerKind:
    if not sys.stdin.isatty():
        raise ConfigurationError("--compiler is required for intelligence mode")
    print("Choose intelligence compiler:")
    print("  1. local       zero-egress loopback HTTP model")
    print("  2. openai      sends session content to the official OpenAI API")
    print("  3. openrouter  sends session content to the pinned hosted provider")
    print("  4. codex-cli   uses an isolated Codex CLI ChatGPT subscription call")
    answer = input("Compiler [1/2/3/4]: ").strip().lower()
    if answer in {"1", "local", "l"}:
        return CompilerKind.LOCAL
    if answer in {"2", "openai", "a"}:
        return CompilerKind.OPENAI
    if answer in {"3", "openrouter", "o"}:
        return CompilerKind.OPENROUTER
    if answer in {"4", "codex-cli", "codex", "c"}:
        return CompilerKind.CODEX_CLI
    raise ConfigurationError(
        "compiler selection must be 1/local, 2/openai, 3/openrouter, or 4/codex-cli"
    )


_COMPILER_ARGUMENT_NAMES = (
    "compiler",
    "model",
    "endpoint",
    "provider",
    "provider_allow",
    "reasoning",
    "max_output_tokens",
    "output_token_parameter",
    "semantic_max_attempts",
    "transport_max_attempts",
    "retry_delay_seconds",
    "min_request_interval_seconds",
    "capture_router_metadata",
    "codex_cli_version",
    "codex_timeout_seconds",
    "codex_max_invocations",
    "codex_max_concurrency",
)


def _compiler_options_requested(args: argparse.Namespace) -> bool:
    """Return whether a command supplied any compiler-specific option."""

    return any(
        getattr(args, name, None) is not None for name in _COMPILER_ARGUMENT_NAMES
    )


def _compiler_from_args(
    args: argparse.Namespace, mode: MemoryMode
) -> CompilerConfig | None:
    if mode is MemoryMode.PRIVATE:
        if (
            args.compiler
            or args.model
            or args.endpoint
            or args.provider
            or args.provider_allow
            or args.reasoning
            or args.max_output_tokens is not None
            or args.output_token_parameter is not None
            or args.semantic_max_attempts is not None
            or args.transport_max_attempts is not None
            or args.retry_delay_seconds is not None
            or args.min_request_interval_seconds is not None
            or args.capture_router_metadata is not None
            or args.codex_cli_version is not None
            or args.codex_timeout_seconds is not None
            or args.codex_max_invocations is not None
            or args.codex_max_concurrency is not None
        ):
            raise ConfigurationError("private mode does not accept compiler options")
        return None

    compiler_selected_interactively = not args.compiler
    kind = (
        CompilerKind(args.compiler)
        if args.compiler
        else _select_compiler_interactively()
    )
    if kind is CompilerKind.LOCAL:
        model = args.model
        endpoint = args.endpoint
        if (
            compiler_selected_interactively
            and (not model or not endpoint)
            and sys.stdin.isatty()
        ):
            if not model:
                model = input("Local model identifier: ").strip()
            if not endpoint:
                endpoint = (
                    input("Loopback endpoint [http://127.0.0.1:11434/v1]: ").strip()
                    or "http://127.0.0.1:11434/v1"
                )
        if not model or not endpoint:
            raise ConfigurationError(
                "a local compiler requires both --model and --endpoint"
            )
        if args.provider or args.provider_allow or args.reasoning:
            raise ConfigurationError(
                "OpenRouter route options are invalid for a local compiler"
            )
        if args.capture_router_metadata is not None:
            raise ConfigurationError(
                "--capture-router-metadata is valid only for OpenRouter"
            )
        if any(
            value is not None
            for value in (
                args.codex_cli_version,
                args.codex_timeout_seconds,
                args.codex_max_invocations,
                args.codex_max_concurrency,
            )
        ):
            raise ConfigurationError("Codex CLI options require --compiler codex-cli")
        return CompilerConfig.local(
            model=model,
            endpoint=endpoint,
            max_output_tokens=(
                args.max_output_tokens if args.max_output_tokens is not None else 8192
            ),
            output_token_parameter=(
                args.output_token_parameter or DEFAULT_OUTPUT_TOKEN_PARAMETER
            ),
            semantic_max_attempts=args.semantic_max_attempts,
            transport_max_attempts=args.transport_max_attempts,
            retry_delay_seconds=args.retry_delay_seconds,
            min_request_interval_seconds=(args.min_request_interval_seconds or 0.0),
        )
    if kind is CompilerKind.CODEX_CLI:
        if args.endpoint:
            raise ConfigurationError("--endpoint is valid only for a local compiler")
        if (
            args.provider
            or args.provider_allow
            or args.capture_router_metadata is not None
        ):
            raise ConfigurationError(
                "OpenRouter provider options are invalid for a Codex CLI compiler"
            )
        if args.transport_max_attempts is not None:
            raise ConfigurationError(
                "--transport-max-attempts is invalid for a Codex CLI compiler"
            )
        if args.max_output_tokens is not None:
            raise ConfigurationError(
                "--max-output-tokens is invalid for a Codex CLI compiler"
            )
        if args.output_token_parameter is not None:
            raise ConfigurationError(
                "--output-token-parameter is invalid for a Codex CLI compiler"
            )
        return CompilerConfig.codex_cli(
            model=args.model or DEFAULT_CODEX_CLI_MODEL,
            reasoning=args.reasoning or "low",
            cli_version=args.codex_cli_version,
            timeout_seconds=(
                args.codex_timeout_seconds
                if args.codex_timeout_seconds is not None
                else 300.0
            ),
            max_invocations=args.codex_max_invocations,
            max_concurrency=(
                args.codex_max_concurrency
                if args.codex_max_concurrency is not None
                else 1
            ),
            semantic_max_attempts=(
                args.semantic_max_attempts
                if args.semantic_max_attempts is not None
                else 2
            ),
            retry_delay_seconds=(
                args.retry_delay_seconds
                if args.retry_delay_seconds is not None
                else 0.25
            ),
            min_request_interval_seconds=(
                args.min_request_interval_seconds
                if args.min_request_interval_seconds is not None
                else 0.0
            ),
        )
    if kind is CompilerKind.OPENAI:
        if args.endpoint:
            raise ConfigurationError("--endpoint is valid only for a local compiler")
        if (
            args.provider
            or args.provider_allow
            or args.capture_router_metadata is not None
        ):
            raise ConfigurationError(
                "OpenRouter provider options are invalid for an OpenAI compiler"
            )
        if any(
            value is not None
            for value in (
                args.codex_cli_version,
                args.codex_timeout_seconds,
                args.codex_max_invocations,
                args.codex_max_concurrency,
            )
        ):
            raise ConfigurationError("Codex CLI options require --compiler codex-cli")
        return CompilerConfig.openai(
            model=args.model or DEFAULT_OPENAI_MODEL,
            reasoning=args.reasoning or "low",
            max_output_tokens=(
                args.max_output_tokens if args.max_output_tokens is not None else 8192
            ),
            output_token_parameter=(
                args.output_token_parameter or DEFAULT_OUTPUT_TOKEN_PARAMETER
            ),
            semantic_max_attempts=(
                args.semantic_max_attempts
                if args.semantic_max_attempts is not None
                else 2
            ),
            transport_max_attempts=(
                args.transport_max_attempts
                if args.transport_max_attempts is not None
                else 2
            ),
            retry_delay_seconds=(
                args.retry_delay_seconds
                if args.retry_delay_seconds is not None
                else 0.25
            ),
            min_request_interval_seconds=(
                args.min_request_interval_seconds
                if args.min_request_interval_seconds is not None
                else 0.0
            ),
        )
    if args.endpoint:
        raise ConfigurationError("--endpoint is valid only for a local compiler")
    if any(
        value is not None
        for value in (
            args.codex_cli_version,
            args.codex_timeout_seconds,
            args.codex_max_invocations,
            args.codex_max_concurrency,
        )
    ):
        raise ConfigurationError("Codex CLI options require --compiler codex-cli")
    return CompilerConfig.openrouter(
        model=args.model or DEFAULT_OPENROUTER_MODEL,
        provider=args.provider,
        provider_allowlist=args.provider_allow or (),
        allow_fallbacks=bool(args.provider_allow),
        reasoning=args.reasoning,
        max_output_tokens=(
            args.max_output_tokens if args.max_output_tokens is not None else 8192
        ),
        output_token_parameter=(
            args.output_token_parameter or DEFAULT_OUTPUT_TOKEN_PARAMETER
        ),
        semantic_max_attempts=(
            args.semantic_max_attempts if args.semantic_max_attempts is not None else 2
        ),
        transport_max_attempts=(
            args.transport_max_attempts
            if args.transport_max_attempts is not None
            else 1
        ),
        retry_delay_seconds=(
            args.retry_delay_seconds if args.retry_delay_seconds is not None else 0.25
        ),
        min_request_interval_seconds=(
            args.min_request_interval_seconds
            if args.min_request_interval_seconds is not None
            else 10.0
        ),
        capture_router_metadata=(
            True
            if args.capture_router_metadata is None
            else args.capture_router_metadata
        ),
    )


def _emit(value: object, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                rendered = json.dumps(item, sort_keys=True)
            elif item is None:
                rendered = "-"
            else:
                rendered = str(item)
            print(f"{key}: {rendered}")
        return
    print(value)


def _memory(args: argparse.Namespace, **kwargs) -> NarratorDB:
    return NarratorDB(db_path=args.path, user_id=args.user_id, **kwargs)


def _init(args: argparse.Namespace) -> dict:
    mode = MemoryMode(args.mode) if args.mode else _select_mode_interactively()
    compiler = _compiler_from_args(args, mode)
    with _memory(
        args,
        mode=mode,
        compiler=compiler,
        capture_policy=args.capture_policy,
    ) as memory:
        return memory.project_status()


def _mode(args: argparse.Namespace) -> dict:
    with _memory(args) as memory:
        if args.value is None:
            return memory.project_status()
        mode = MemoryMode(args.value)
        compiler = _compiler_from_args(args, mode)
        derived_data = None
        if args.retain_derived:
            derived_data = "retain"
        elif args.purge_derived:
            derived_data = "purge"
        memory.set_mode(mode, compiler=compiler, derived_data=derived_data)
        return memory.project_status()


def _status(args: argparse.Namespace) -> dict:
    with _memory(args) as memory:
        return memory.project_status()


def _capture_policy(args: argparse.Namespace) -> dict:
    with _memory(args) as memory:
        if args.value is not None:
            memory.set_capture_policy(args.value)
        return memory.project_status()


def _backfill(args: argparse.Namespace) -> dict:
    with _memory(args) as memory:
        return memory.backfill(process=not args.queue_only, limit=args.limit)


def _purge(args: argparse.Namespace) -> dict:
    if not args.yes:
        raise ConfigurationError(
            "purge requires --yes; canonical raw messages are preserved"
        )
    with _memory(args) as memory:
        return memory.purge_derived()


def _mcp_install(args: argparse.Namespace) -> dict:
    store = ProjectConfigStore(str(args.path))
    current = store.load()
    if args.mode:
        mode = MemoryMode(args.mode)
    elif current is not None:
        # A persisted choice is authoritative and does not need to be made again.
        mode = current.mode
    elif store.is_legacy_database():
        # Existing 1.x databases have a defined, non-interactive migration path.
        mode = MemoryMode.PRIVATE
    else:
        mode = _select_mode_interactively()

    compiler: CompilerConfig | None
    if (
        mode is MemoryMode.PRIVATE
        or current is None
        or _compiler_options_requested(args)
    ):
        compiler = _compiler_from_args(args, mode)
    else:
        # Reuse the credential-free compiler already persisted in this database.
        compiler = None
    return install_mcp_client(
        args.client,
        path=args.path,
        user_id=args.user_id,
        mode=mode,
        compiler=compiler,
        force=args.force,
        dry_run=args.dry_run,
        allow_path_fallback_writes=args.allow_path_fallback_writes,
    )


def _mcp_uninstall(args: argparse.Namespace) -> dict:
    return uninstall_mcp_client(args.client, dry_run=args.dry_run)


def _service_init(args: argparse.Namespace) -> dict:
    mode = MemoryMode(args.mode)
    compiler = _compiler_from_args(args, mode)
    return initialize_service(
        data_dir=args.data_dir,
        project_name=args.project,
        credentials_file=args.credentials_file,
        mode=mode,
        compiler=compiler,
        capture_policy=args.capture_policy,
    )


def _service_add_project(args: argparse.Namespace) -> dict:
    return add_service_project(
        data_dir=args.data_dir,
        project_name=args.project,
        credentials_file=args.credentials_file,
        admin=args.admin,
    )


def _service_issue_key(args: argparse.Namespace) -> dict:
    return issue_project_key(
        data_dir=args.data_dir,
        project=args.project,
        credentials_file=args.credentials_file,
        admin=args.admin,
    )


def _service_revoke_key(args: argparse.Namespace) -> dict:
    return revoke_project_key(
        data_dir=args.data_dir,
        token_environment=args.token_env,
    )


def _service_export(args: argparse.Namespace) -> dict:
    return export_service_project(
        data_dir=args.data_dir,
        project=args.project,
        output_dir=args.output,
    )


def _service_import(args: argparse.Namespace) -> dict:
    return import_service_project(
        data_dir=args.data_dir,
        project=args.project,
        input_dir=args.input,
    )


def _service_serve(args: argparse.Namespace) -> dict:
    return serve_service(
        data_dir=args.data_dir,
        host=args.host,
        port=args.port,
        public_url=args.public_url,
    )


def _service_quickstart(args: argparse.Namespace) -> dict:
    mode = MemoryMode(args.mode)
    compiler = _compiler_from_args(args, mode)
    prepared = prepare_quickstart(
        data_dir=args.data_dir,
        credentials_file=args.credentials_file,
        project_name=args.project,
        mode=mode,
        compiler=compiler,
        capture_policy=args.capture_policy,
        public_url=args.public_url,
        register_codex=not args.no_register_codex,
        replace_codex=args.replace_codex,
    )
    _emit(prepared, as_json=args.json)
    sys.stdout.flush()
    return serve_service(
        data_dir=args.data_dir,
        host=args.host,
        port=args.port,
        public_url=args.public_url,
    )


def _service_install_codex(args: argparse.Namespace) -> dict:
    from .service_bridge import read_service_credentials

    read_service_credentials(args.credentials_file)
    return install_service_bridge(
        args.credentials_file,
        force=args.replace_codex,
        dry_run=args.dry_run,
    )


def _remote_install(args: argparse.Namespace) -> dict:
    token = getpass.getpass("NarratorDB service token: ")
    return install_remote_service(
        args.client,
        endpoint=args.endpoint,
        project_id=args.project_id,
        credentials_file=args.credentials_file,
        token=token,
        force=args.force,
    )


def _add_leaf_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit machine-readable JSON",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="narratordb",
        description="Configure and inspect a NarratorDB project",
    )
    parser.add_argument(
        "--path", default=default_db_path(), help="SQLite database path"
    )
    parser.add_argument("--user-id", help="logical user/scope for lifecycle operations")
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    subparsers = parser.add_subparsers(dest="command")

    init = subparsers.add_parser("init", help="select a mode for a new database")
    init.add_argument("--mode", choices=[mode.value for mode in MemoryMode])
    init.add_argument(
        "--capture-policy",
        choices=[policy.value for policy in CapturePolicy],
        help="automatic capture policy (default: preferences)",
    )
    _add_compiler_arguments(init)
    init.set_defaults(handler=_init)

    mode = subparsers.add_parser("mode", help="show or explicitly change memory mode")
    mode.add_argument("value", nargs="?", choices=[item.value for item in MemoryMode])
    _add_compiler_arguments(mode)
    derived = mode.add_mutually_exclusive_group()
    derived.add_argument("--retain-derived", action="store_true")
    derived.add_argument("--purge-derived", action="store_true")
    mode.set_defaults(handler=_mode)

    status = subparsers.add_parser("status", help="show mode and enrichment status")
    status.set_defaults(handler=_status)

    capture_policy = subparsers.add_parser(
        "capture-policy", help="show or explicitly change automatic capture"
    )
    capture_policy.add_argument(
        "value", nargs="?", choices=[policy.value for policy in CapturePolicy]
    )
    capture_policy.set_defaults(handler=_capture_policy)

    backfill = subparsers.add_parser(
        "backfill", help="start or resume intelligence backfill"
    )
    backfill.add_argument(
        "--limit", type=int, default=100, help="maximum jobs to process"
    )
    backfill.add_argument(
        "--queue-only",
        action="store_true",
        help="enqueue sessions without making compiler requests",
    )
    backfill.set_defaults(handler=_backfill)

    purge = subparsers.add_parser(
        "purge", help="delete derived records, preserving raw messages"
    )
    purge.add_argument(
        "--yes", action="store_true", help="confirm derived-data deletion"
    )
    purge.set_defaults(handler=_purge)

    mcp = subparsers.add_parser(
        "mcp", help="install or uninstall the NarratorDB MCP server"
    )
    mcp_actions = mcp.add_subparsers(dest="mcp_action", required=True)

    mcp_install = mcp_actions.add_parser(
        "install", help="register NarratorDB with an MCP client"
    )
    mcp_install.add_argument("client", choices=SUPPORTED_CLIENTS)
    mcp_install.add_argument(
        "--mode",
        choices=[item.value for item in MemoryMode],
        help=(
            "database mode to initialize or validate; a new database prompts on a "
            "terminal and requires --mode in non-interactive use"
        ),
    )
    _add_compiler_arguments(mcp_install)
    mcp_install.add_argument(
        "--force",
        action="store_true",
        help="replace an existing NarratorDB MCP registration",
    )
    mcp_install.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect and print the plan without changing the database or client",
    )
    mcp_install.add_argument(
        "--allow-path-fallback-writes",
        action="store_true",
        help=(
            "confirm project writes if the MCP server starts from the home-directory "
            "path fallback"
        ),
    )
    _add_leaf_json_argument(mcp_install)
    mcp_install.set_defaults(handler=_mcp_install)

    mcp_uninstall = mcp_actions.add_parser(
        "uninstall", help="remove NarratorDB from an MCP client"
    )
    mcp_uninstall.add_argument("client", choices=SUPPORTED_CLIENTS)
    mcp_uninstall.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect and print the removal plan without changing the client",
    )
    _add_leaf_json_argument(mcp_uninstall)
    mcp_uninstall.set_defaults(handler=_mcp_uninstall)

    remote = subparsers.add_parser(
        "remote", help="connect a supported MCP client to a remote NarratorDB service"
    )
    remote_actions = remote.add_subparsers(dest="remote_action", required=True)
    remote_install = remote_actions.add_parser(
        "install", help="prompt for a token, verify status, and install securely"
    )
    remote_install.add_argument("client", choices=SUPPORTED_CLIENTS)
    remote_install.add_argument(
        "--endpoint", required=True, help="HTTPS NarratorDB MCP URL ending in /mcp"
    )
    remote_install.add_argument("--project-id", required=True)
    remote_install.add_argument("--credentials-file", required=True)
    remote_install.add_argument(
        "--force", action="store_true", help="replace an existing MCP registration"
    )
    _add_leaf_json_argument(remote_install)
    remote_install.set_defaults(handler=_remote_install)

    service = subparsers.add_parser(
        "service", help="initialize and run the authenticated HTTP service alpha"
    )
    service_actions = service.add_subparsers(dest="service_action", required=True)

    service_quickstart = service_actions.add_parser(
        "quickstart",
        help="initialize, register Codex securely, and start the service",
    )
    service_quickstart.add_argument(
        "--data-dir", default=str(DEFAULT_LOCAL_SERVICE_DIR)
    )
    service_quickstart.add_argument("--credentials-file")
    service_quickstart.add_argument("--project", default="default")
    service_quickstart.add_argument(
        "--mode",
        choices=[item.value for item in MemoryMode],
        default=MemoryMode.PRIVATE.value,
    )
    service_quickstart.add_argument(
        "--capture-policy",
        choices=[policy.value for policy in CapturePolicy],
        default=CapturePolicy.SESSIONS.value,
    )
    _add_compiler_arguments(service_quickstart)
    service_quickstart.add_argument("--host", default="127.0.0.1")
    service_quickstart.add_argument("--port", type=int, default=8787)
    service_quickstart.add_argument(
        "--public-url", default="http://127.0.0.1:8787"
    )
    service_quickstart.add_argument(
        "--no-register-codex",
        action="store_true",
        help="start without changing Codex MCP registration",
    )
    service_quickstart.add_argument(
        "--replace-codex",
        action="store_true",
        help="replace an existing NarratorDB MCP registration",
    )
    _add_leaf_json_argument(service_quickstart)
    service_quickstart.set_defaults(handler=_service_quickstart)

    service_install_codex = service_actions.add_parser(
        "install-codex",
        help="register the secure credential-file bridge with Codex",
    )
    service_install_codex.add_argument("--credentials-file", required=True)
    service_install_codex.add_argument("--replace-codex", action="store_true")
    service_install_codex.add_argument("--dry-run", action="store_true")
    _add_leaf_json_argument(service_install_codex)
    service_install_codex.set_defaults(handler=_service_install_codex)

    service_init = service_actions.add_parser(
        "init", help="initialize an explicit service data directory and first project"
    )
    service_init.add_argument("--data-dir", required=True)
    service_init.add_argument("--project", required=True)
    service_init.add_argument("--credentials-file", required=True)
    service_init.add_argument(
        "--mode",
        choices=[item.value for item in MemoryMode],
        default=MemoryMode.PRIVATE.value,
    )
    service_init.add_argument(
        "--capture-policy",
        choices=[policy.value for policy in CapturePolicy],
    )
    _add_compiler_arguments(service_init)
    _add_leaf_json_argument(service_init)
    service_init.set_defaults(handler=_service_init)

    service_add_project = service_actions.add_parser(
        "add-project", help="create an isolated project and scoped service key"
    )
    service_add_project.add_argument("--data-dir", required=True)
    service_add_project.add_argument("--project", required=True)
    service_add_project.add_argument("--credentials-file", required=True)
    service_add_project.add_argument(
        "--admin", action="store_true", help="also grant project configuration access"
    )
    _add_leaf_json_argument(service_add_project)
    service_add_project.set_defaults(handler=_service_add_project)

    service_issue_key = service_actions.add_parser(
        "issue-key", help="issue another key for an existing project"
    )
    service_issue_key.add_argument("--data-dir", required=True)
    service_issue_key.add_argument("--project", required=True)
    service_issue_key.add_argument("--credentials-file", required=True)
    service_issue_key.add_argument(
        "--admin", action="store_true", help="also grant project configuration access"
    )
    _add_leaf_json_argument(service_issue_key)
    service_issue_key.set_defaults(handler=_service_issue_key)

    service_revoke_key = service_actions.add_parser(
        "revoke-key", help="revoke the token held in an environment variable"
    )
    service_revoke_key.add_argument("--data-dir", required=True)
    service_revoke_key.add_argument(
        "--token-env", default="NARRATORDB_SERVICE_TOKEN"
    )
    _add_leaf_json_argument(service_revoke_key)
    service_revoke_key.set_defaults(handler=_service_revoke_key)

    service_export = service_actions.add_parser(
        "export", help="create a checksummed portable project export"
    )
    service_export.add_argument("--data-dir", required=True)
    service_export.add_argument("--project", required=True)
    service_export.add_argument("--output", required=True)
    _add_leaf_json_argument(service_export)
    service_export.set_defaults(handler=_service_export)

    service_import = service_actions.add_parser(
        "import", help="verify and idempotently import a portable project export"
    )
    service_import.add_argument("--data-dir", required=True)
    service_import.add_argument("--project", required=True)
    service_import.add_argument("--input", required=True)
    _add_leaf_json_argument(service_import)
    service_import.set_defaults(handler=_service_import)

    service_serve = service_actions.add_parser(
        "serve", help="run the authenticated Streamable HTTP MCP service"
    )
    service_serve.add_argument("--data-dir", required=True)
    service_serve.add_argument("--host", default="127.0.0.1")
    service_serve.add_argument("--port", type=int, default=8787)
    service_serve.add_argument(
        "--public-url", default="http://127.0.0.1:8787"
    )
    service_serve.set_defaults(handler=_service_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        result = args.handler(args)
    except (ConfigurationError, FeatureUnavailableError) as error:
        print(f"narratordb: {error}", file=sys.stderr)
        return 2
    _emit(result, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
