"""Authenticated Streamable HTTP service for the NarratorDB internal alpha.

The service control plane owns account, project, and API-key identity.  Memory
tools never derive authorization from the process working directory or accept a
caller-selected workspace identifier.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .config import (
    CapturePolicy,
    CompilerConfig,
    ConfigurationError,
    MemoryMode,
    ProjectConfigStore,
)
from .database import NarratorDB
from .mcp_contract import create_server
from .mcp_server import MCPRuntime, MCPServerConfig


CONTROL_SCHEMA_VERSION = 1
DEFAULT_SERVICE_URL = "http://127.0.0.1:8787"
DEFAULT_LOCAL_SERVICE_DIR = Path.home() / ".narratordb" / "service"
DEFAULT_SCOPES = (
    "memory:read",
    "memory:write",
    "memory:delete",
)
ADMIN_SCOPES = (*DEFAULT_SCOPES, "project:admin")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}")
_TOKEN_RE = re.compile(r"ndb_[A-Za-z0-9_-]{32,128}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _name(value: str, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not _NAME_RE.fullmatch(normalized):
        raise ConfigurationError(
            f"{field} must be 1-100 letters, numbers, spaces, dots, underscores, "
            "or hyphens"
        )
    return normalized


def _data_dir(value: str | os.PathLike[str]) -> Path:
    requested = Path(value).expanduser()
    if requested.is_symlink():
        raise ConfigurationError("service data directory cannot be a symbolic link")
    path = requested.resolve()
    if path == Path.home().resolve():
        raise ConfigurationError("service data directory cannot be the home directory")
    return path


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return "ndb_" + secrets.token_urlsafe(32)


def _normalize_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    allowed = frozenset(ADMIN_SCOPES)
    normalized = tuple(dict.fromkeys(str(scope).strip() for scope in scopes))
    if not normalized or any(scope not in allowed for scope in normalized):
        raise ConfigurationError(
            "service key scopes must be selected from: " + ", ".join(ADMIN_SCOPES)
        )
    return normalized


@dataclass(frozen=True)
class ServicePrincipal:
    account_id: str
    project_id: str
    scopes: tuple[str, ...]
    key_id: str


class ServiceControlPlane:
    """Small credential-free control plane stored separately from memory data."""

    def __init__(self, data_dir: str | os.PathLike[str]):
        self.data_dir = _data_dir(data_dir)
        self.control_path = self.data_dir / "service.db"
        self.accounts_dir = self.data_dir / "accounts"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.control_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        if self.control_path.exists():
            raise ConfigurationError(
                f"service is already initialized at {self.data_dir}"
            )
        self.data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.accounts_dir.is_symlink():
            raise ConfigurationError("service accounts directory cannot be a symlink")
        self.accounts_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        os.chmod(self.accounts_dir, 0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE service_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE projects (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, name)
                );
                CREATE TABLE api_keys (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    token_prefix TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE INDEX idx_api_keys_project ON api_keys(project_id);
                """
            )
            connection.execute(
                "INSERT INTO service_metadata(key, value) VALUES(?, ?)",
                ("schema_version", str(CONTROL_SCHEMA_VERSION)),
            )
        os.chmod(self.control_path, 0o600)

    def rollback_initialization(self, account_id: str | None = None) -> None:
        """Remove only artifacts created by a failed first-time initialization."""

        database_paths = [self.control_path]
        if account_id is not None:
            database_paths.append(self.account_db_path(account_id))
        for database_path in database_paths:
            for suffix in ("", "-wal", "-shm", "-journal"):
                artifact = Path(f"{database_path}{suffix}")
                if artifact.is_file() or artifact.is_symlink():
                    artifact.unlink()
        try:
            self.accounts_dir.rmdir()
        except OSError:
            pass

    def require_initialized(self) -> None:
        if not self.control_path.is_file():
            raise ConfigurationError(
                "service is not initialized; run `narratordb service init` first"
            )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM service_metadata WHERE key = 'schema_version'"
            ).fetchone()
        version = int(row["value"]) if row is not None else 0
        if version != CONTROL_SCHEMA_VERSION:
            raise ConfigurationError(
                f"unsupported service schema version {version}; expected "
                f"{CONTROL_SCHEMA_VERSION}"
            )

    def account_db_path(self, account_id: str) -> Path:
        try:
            normalized = uuid.UUID(account_id).hex
        except (ValueError, AttributeError) as error:
            raise ConfigurationError("invalid service account identity") from error
        return self.accounts_dir / f"{normalized}.db"

    def create_account(self, name: str) -> str:
        account_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO accounts(id, name, created_at) VALUES(?, ?, ?)",
                (account_id, _name(name, field="account name"), _utc_now()),
            )
        return account_id

    def sole_account_id(self) -> str:
        self.require_initialized()
        with self._connect() as connection:
            rows = connection.execute("SELECT id FROM accounts ORDER BY id").fetchall()
        if len(rows) != 1:
            raise ConfigurationError(
                "the internal alpha requires exactly one initialized account"
            )
        return str(rows[0]["id"])

    def create_project(self, account_id: str, name: str) -> str:
        project_id = str(uuid.uuid4())
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO projects(id, account_id, name, created_at)
                    VALUES(?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        account_id,
                        _name(name, field="project name"),
                        _utc_now(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ConfigurationError("project name already exists") from error
        return project_id

    def resolve_project(self, value: str) -> tuple[str, str]:
        normalized = str(value or "").strip()
        if not normalized:
            raise ConfigurationError("project is required")
        account_id = self.sole_account_id()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name FROM projects
                WHERE account_id = ? AND (id = ? OR name = ?)
                """,
                (account_id, normalized, normalized),
            ).fetchall()
        if len(rows) != 1:
            raise ConfigurationError("project was not found or is ambiguous")
        return account_id, str(rows[0]["id"])

    def issue_key(
        self,
        *,
        account_id: str,
        project_id: str,
        scopes: Iterable[str] = DEFAULT_SCOPES,
    ) -> tuple[str, str, tuple[str, ...]]:
        normalized_scopes = _normalize_scopes(scopes)
        token = _new_token()
        key_id = str(uuid.uuid4())
        with self._connect() as connection:
            project = connection.execute(
                "SELECT 1 FROM projects WHERE id = ? AND account_id = ?",
                (project_id, account_id),
            ).fetchone()
            if project is None:
                raise ConfigurationError("project does not belong to the account")
            connection.execute(
                """
                INSERT INTO api_keys(
                    id, account_id, project_id, token_hash, token_prefix,
                    scopes_json, created_at, revoked_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    key_id,
                    account_id,
                    project_id,
                    _token_hash(token),
                    token[:12],
                    json.dumps(normalized_scopes),
                    _utc_now(),
                ),
            )
        return token, key_id, normalized_scopes

    def resolve_token(self, token: str) -> ServicePrincipal | None:
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            return None
        self.require_initialized()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, account_id, project_id, scopes_json
                FROM api_keys
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (_token_hash(token),),
            ).fetchone()
        if row is None:
            return None
        try:
            scopes = _normalize_scopes(json.loads(row["scopes_json"]))
        except (TypeError, ValueError, json.JSONDecodeError, ConfigurationError):
            return None
        return ServicePrincipal(
            account_id=str(row["account_id"]),
            project_id=str(row["project_id"]),
            scopes=scopes,
            key_id=str(row["id"]),
        )

    def revoke_token(self, token: str) -> bool:
        if not _TOKEN_RE.fullmatch(str(token or "")):
            raise ConfigurationError("service token is malformed")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE api_keys SET revoked_at = ?
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (_utc_now(), _token_hash(token)),
            )
        return cursor.rowcount == 1

    def readiness(self, principal: ServicePrincipal) -> dict[str, Any]:
        db_path = self.account_db_path(principal.account_id)
        config = ProjectConfigStore(str(db_path)).load()
        return {
            "ready": config is not None and db_path.is_file(),
            "mode": config.mode.value if config is not None else None,
            "project_id": principal.project_id,
        }

    def runtime_identities(self) -> tuple[tuple[str, str], ...]:
        self.require_initialized()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT account_id, id FROM projects ORDER BY account_id, id"
            ).fetchall()
        return tuple((str(row["account_id"]), str(row["id"])) for row in rows)


def _credentials_target(path: str | os.PathLike[str]) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise ConfigurationError("credentials file cannot be a symbolic link")
    target = requested.resolve()
    if target.exists():
        raise ConfigurationError(f"credentials file already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _write_credentials(
    target: Path,
    *,
    token: str,
    project_id: str,
    service_url: str = DEFAULT_SERVICE_URL,
) -> Path:
    from .service_bridge import write_service_credentials

    return write_service_credentials(
        target,
        service_url=f"{service_url.rstrip('/')}/mcp",
        token=token,
        project_id=project_id,
    )


def initialize_service(
    *,
    data_dir: str | os.PathLike[str],
    project_name: str,
    credentials_file: str | os.PathLike[str],
    mode: MemoryMode,
    compiler: CompilerConfig | None,
    capture_policy: CapturePolicy | str | None,
    service_url: str = DEFAULT_SERVICE_URL,
) -> dict[str, Any]:
    credentials = _credentials_target(credentials_file)
    control = ServiceControlPlane(data_dir)
    account_id: str | None = None
    initialized = False
    try:
        control.initialize()
        initialized = True
        account_id = control.create_account("internal-alpha")
        project_id = control.create_project(account_id, project_name)
        db_path = control.account_db_path(account_id)
        with NarratorDB(
            data_dir=str(control.data_dir),
            db_path=str(db_path),
            user_id=account_id,
            mode=mode,
            compiler=compiler,
            capture_policy=capture_policy,
        ):
            pass
        os.chmod(db_path, 0o600)
        token, key_id, scopes = control.issue_key(
            account_id=account_id,
            project_id=project_id,
            scopes=ADMIN_SCOPES,
        )
        written = _write_credentials(
            credentials,
            token=token,
            project_id=project_id,
            service_url=service_url,
        )
    except BaseException:
        if initialized:
            control.rollback_initialization(account_id)
        raise
    return {
        "initialized": True,
        "data_dir": str(control.data_dir),
        "project_id": project_id,
        "key_id": key_id,
        "key_prefix": token[:12],
        "scopes": list(scopes),
        "credentials_file": str(written),
        "mode": mode.value,
    }


def add_project(
    *,
    data_dir: str | os.PathLike[str],
    project_name: str,
    credentials_file: str | os.PathLike[str],
    admin: bool = False,
) -> dict[str, Any]:
    credentials = _credentials_target(credentials_file)
    control = ServiceControlPlane(data_dir)
    account_id = control.sole_account_id()
    project_id = control.create_project(account_id, project_name)
    token, key_id, scopes = control.issue_key(
        account_id=account_id,
        project_id=project_id,
        scopes=ADMIN_SCOPES if admin else DEFAULT_SCOPES,
    )
    try:
        written = _write_credentials(
            credentials,
            token=token,
            project_id=project_id,
        )
    except BaseException:
        control.revoke_token(token)
        raise
    return {
        "created": True,
        "project_id": project_id,
        "key_id": key_id,
        "key_prefix": token[:12],
        "scopes": list(scopes),
        "credentials_file": str(written),
    }


def issue_project_key(
    *,
    data_dir: str | os.PathLike[str],
    project: str,
    credentials_file: str | os.PathLike[str],
    admin: bool = False,
) -> dict[str, Any]:
    credentials = _credentials_target(credentials_file)
    control = ServiceControlPlane(data_dir)
    account_id, project_id = control.resolve_project(project)
    token, key_id, scopes = control.issue_key(
        account_id=account_id,
        project_id=project_id,
        scopes=ADMIN_SCOPES if admin else DEFAULT_SCOPES,
    )
    try:
        written = _write_credentials(
            credentials,
            token=token,
            project_id=project_id,
        )
    except BaseException:
        control.revoke_token(token)
        raise
    return {
        "created": True,
        "project_id": project_id,
        "key_id": key_id,
        "key_prefix": token[:12],
        "scopes": list(scopes),
        "credentials_file": str(written),
    }


def revoke_project_key(
    *, data_dir: str | os.PathLike[str], token_environment: str
) -> dict[str, Any]:
    environment_name = str(token_environment or "").strip()
    token = os.getenv(environment_name, "")
    if not token:
        raise ConfigurationError(
            f"service token environment variable is not set: {environment_name}"
        )
    revoked = ServiceControlPlane(data_dir).revoke_token(token)
    return {"revoked": revoked, "token_environment": environment_name}


def prepare_quickstart(
    *,
    data_dir: str | os.PathLike[str] = DEFAULT_LOCAL_SERVICE_DIR,
    credentials_file: str | os.PathLike[str] | None = None,
    project_name: str = "default",
    mode: MemoryMode = MemoryMode.PRIVATE,
    compiler: CompilerConfig | None = None,
    capture_policy: CapturePolicy | str | None = "sessions",
    public_url: str = DEFAULT_SERVICE_URL,
    register_codex: bool = True,
    replace_codex: bool = False,
) -> dict[str, Any]:
    """Initialize once and register a credential-file bridge for Codex."""

    control = ServiceControlPlane(data_dir)
    credentials = (
        Path(credentials_file or (control.data_dir / "credentials.env"))
        .expanduser()
        .resolve()
    )
    if control.control_path.exists():
        control.require_initialized()
        if not credentials.is_file():
            raise ConfigurationError(
                "service is initialized but its credentials file is missing; "
                "issue a replacement key explicitly"
            )
        initialized = False
        mode_value = None
        project_id = None
    else:
        result = initialize_service(
            data_dir=control.data_dir,
            project_name=project_name,
            credentials_file=credentials,
            mode=mode,
            compiler=compiler,
            capture_policy=capture_policy,
            service_url=public_url,
        )
        initialized = True
        mode_value = result["mode"]
        project_id = result["project_id"]

    from .service_bridge import read_service_credentials

    credential_values = read_service_credentials(credentials)
    expected_url = f"{public_url.rstrip('/')}/mcp"
    if credential_values["NARRATORDB_SERVICE_URL"] != expected_url:
        raise ConfigurationError(
            "service credentials URL does not match --public-url; use the "
            "original URL or issue a replacement credentials file"
        )

    registration = None
    if register_codex:
        from .mcp_install import install_service_bridge

        registration = install_service_bridge(
            credentials,
            force=replace_codex,
        )
    return {
        "ready_to_start": True,
        "initialized": initialized,
        "data_dir": str(control.data_dir),
        "credentials_file": str(credentials),
        "project_id": project_id,
        "mode": mode_value,
        "codex": registration,
    }


class ServiceTokenVerifier:
    def __init__(self, control: ServiceControlPlane):
        self.control = control

    async def verify_token(self, token: str):
        try:
            from mcp.server.auth.provider import AccessToken
        except ImportError as error:  # pragma: no cover - clean wheel behavior
            raise ConfigurationError(
                "NarratorDB service dependencies are missing; install "
                "`narratordb-memory[mcp]`"
            ) from error
        principal = self.control.resolve_token(token)
        if principal is None:
            return None
        return AccessToken(
            token="verified",
            client_id=principal.key_id,
            scopes=list(principal.scopes),
            subject=principal.account_id,
            claims={
                "account_id": principal.account_id,
                "project_id": principal.project_id,
                "key_id": principal.key_id,
            },
        )


def _without_paths(value: Any) -> Any:
    hidden = {"db_path", "project_root", "suggested_cwd", "suggested_command"}
    if isinstance(value, dict):
        return {
            key: _without_paths(item)
            for key, item in value.items()
            if key not in hidden
        }
    if isinstance(value, list):
        return [_without_paths(item) for item in value]
    return value


class ServiceRuntimeProxy:
    """Resolve a fixed MCP runtime from the authenticated request context."""

    def __init__(self, control: ServiceControlPlane):
        self.control = control
        self._lock = threading.RLock()
        self._runtimes: dict[tuple[str, str], MCPRuntime] = {}

    @staticmethod
    def _principal():
        from mcp.server.auth.middleware.auth_context import get_access_token

        access = get_access_token()
        claims = access.claims if access is not None else None
        if access is None or not isinstance(claims, dict):
            raise PermissionError("authenticated service context is required")
        account_id = str(claims.get("account_id") or "")
        project_id = str(claims.get("project_id") or "")
        if not account_id or not project_id:
            raise PermissionError("service token is missing scope identity")
        return access, account_id, project_id

    def _runtime_for(self, account_id: str, project_id: str) -> MCPRuntime:
        key = (account_id, project_id)
        with self._lock:
            runtime = self._runtimes.get(key)
            if runtime is None:
                runtime = MCPRuntime(
                    MCPServerConfig(
                        db_path=str(self.control.account_db_path(account_id)),
                        user_id=account_id,
                        workspace_id=f"project/{project_id}",
                        data_dir=str(self.control.data_dir),
                        client="service-mcp",
                        scope_origin="explicit",
                    )
                )
                runtime.status()
                runtime.start_background_enrichment()
                self._runtimes[key] = runtime
            return runtime

    def _runtime(self, required_scope: str) -> MCPRuntime:
        access, account_id, project_id = self._principal()
        if required_scope not in access.scopes:
            raise PermissionError(f"service token lacks {required_scope}")
        return self._runtime_for(account_id, project_id)

    def prewarm(self) -> None:
        """Open databases and load retrieval dependencies before accepting traffic."""

        for account_id, project_id in self.control.runtime_identities():
            runtime = self._runtime_for(account_id, project_id)
            runtime.recall(
                "NarratorDB service readiness probe",
                include_global=False,
                token_budget=128,
            )

    def close(self) -> None:
        with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            runtime.close()

    def configure(self, **kwargs) -> dict[str, Any]:
        runtime = self._runtime("project:admin")
        result = runtime.configure(**kwargs)
        self.close()
        return _without_paths(result)

    def remember(self, *args, **kwargs) -> dict[str, Any]:
        return self._runtime("memory:write").remember(*args, **kwargs)

    def remember_session(self, *args, **kwargs) -> dict[str, Any]:
        return self._runtime("memory:write").remember_session(*args, **kwargs)

    def recall(self, *args, **kwargs) -> dict[str, Any]:
        return _without_paths(self._runtime("memory:read").recall(*args, **kwargs))

    def resume(self, *args, **kwargs) -> dict[str, Any]:
        return _without_paths(self._runtime("memory:read").resume(*args, **kwargs))

    def forget(self, *args, **kwargs) -> dict[str, Any]:
        return self._runtime("memory:delete").forget(*args, **kwargs)

    def status(self, *args, **kwargs) -> dict[str, Any]:
        return _without_paths(self._runtime("memory:read").status(*args, **kwargs))


def create_service_server(
    control: ServiceControlPlane,
    *,
    host: str,
    port: int,
    public_url: str,
):
    try:
        from mcp.server.auth.settings import AuthSettings
        from starlette.requests import Request
        from starlette.responses import JSONResponse
    except ImportError as error:  # pragma: no cover - clean wheel behavior
        raise ConfigurationError(
            "NarratorDB service dependencies are missing; install "
            "`narratordb-memory[mcp]`"
        ) from error

    control.require_initialized()
    base_url = str(public_url or "").strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigurationError("public URL must use http:// or https://")
    public_host = (urlparse(base_url).hostname or "").casefold()
    public_is_loopback = public_host in {"127.0.0.1", "localhost", "::1"}
    if (
        host not in {"127.0.0.1", "localhost", "::1"}
        and not public_is_loopback
        and not base_url.startswith("https://")
    ):
        raise ConfigurationError(
            "non-loopback service binding requires an https public URL"
        )
    if isinstance(port, bool) or not 1 <= int(port) <= 65535:
        raise ConfigurationError("service port must be between 1 and 65535")

    verifier = ServiceTokenVerifier(control)
    proxy = ServiceRuntimeProxy(control)
    proxy.prewarm()
    auth = AuthSettings(
        issuer_url=base_url,
        resource_server_url=f"{base_url}/mcp",
        required_scopes=[],
    )
    server = create_server(
        proxy,
        server_options={
            "host": host,
            "port": int(port),
            "streamable_http_path": "/mcp",
            "stateless_http": True,
            "json_response": True,
            "auth": auth,
            "token_verifier": verifier,
        },
    )

    @server.custom_route("/healthz", methods=["GET"])
    async def health(_: Request):
        return JSONResponse({"status": "ok", "service": "NarratorDB"})

    @server.custom_route("/readyz", methods=["GET"])
    async def ready(request: Request):
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not token:
            return JSONResponse({"ready": False}, status_code=401)
        access = await verifier.verify_token(token)
        if access is None or not isinstance(access.claims, dict):
            return JSONResponse({"ready": False}, status_code=401)
        principal = ServicePrincipal(
            account_id=str(access.claims["account_id"]),
            project_id=str(access.claims["project_id"]),
            scopes=tuple(access.scopes),
            key_id=str(access.claims["key_id"]),
        )
        result = control.readiness(principal)
        return JSONResponse(result, status_code=200 if result["ready"] else 503)

    return server, proxy


def serve(
    *,
    data_dir: str | os.PathLike[str],
    host: str = "127.0.0.1",
    port: int = 8787,
    public_url: str = DEFAULT_SERVICE_URL,
) -> dict[str, Any]:
    control = ServiceControlPlane(data_dir)
    proxy: ServiceRuntimeProxy | None = None
    try:
        try:
            server, proxy = create_service_server(
                control,
                host=host,
                port=port,
                public_url=public_url,
            )
            atexit.register(proxy.close)
            server.run(transport="streamable-http")
        except KeyboardInterrupt:
            pass
    finally:
        if proxy is not None:
            proxy.close()
    return {"stopped": True}


__all__ = [
    "ADMIN_SCOPES",
    "DEFAULT_LOCAL_SERVICE_DIR",
    "DEFAULT_SCOPES",
    "DEFAULT_SERVICE_URL",
    "ServiceControlPlane",
    "ServicePrincipal",
    "ServiceRuntimeProxy",
    "ServiceTokenVerifier",
    "add_project",
    "create_service_server",
    "initialize_service",
    "issue_project_key",
    "prepare_quickstart",
    "revoke_project_key",
    "serve",
]
