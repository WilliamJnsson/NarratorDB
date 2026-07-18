"""Stable local identity and project scoping for agent integrations."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse


_SAFE_SCOPE_RE = re.compile(r"[^A-Za-z0-9._/-]+")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

ScopeOrigin = Literal["explicit", "git_remote", "path_fallback"]


@dataclass(frozen=True)
class ProjectScope:
    """Resolved project identity plus diagnostics for user-facing integrations."""

    workspace_id: str
    origin: ScopeOrigin
    project_root: str
    in_git_repo: bool
    warning: str | None = None
    suggested_cwd: str | None = None
    write_confirmation_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "origin": self.origin,
            "project_root": self.project_root,
            "in_git_repo": self.in_git_repo,
            "warning": self.warning,
            "suggested_cwd": self.suggested_cwd,
            "write_confirmation_required": self.write_confirmation_required,
        }


def _run_git(cwd: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _remote_slug(remote: str) -> str:
    value = remote.strip()
    if not value:
        return ""
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        value = f"ssh://git@{host}/{path}"
    parsed = urlparse(value)
    path = parsed.path if parsed.scheme else value
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return ""
    return "/".join(parts[-2:])


def resolve_project_scope(
    cwd: str | os.PathLike[str] | None = None,
    *,
    workspace_id: str | None = None,
) -> ProjectScope:
    """Resolve project scope and explain how the identity was selected.

    ``workspace_id`` and ``NARRATORDB_WORKSPACE_ID`` are explicit identities.
    Otherwise a Git remote slug is preferred. A path-derived identity remains
    available for legitimate local/non-Git projects, but a home-directory
    fallback is marked as requiring explicit write confirmation.
    """

    explicit = (
        str(workspace_id).strip()
        if workspace_id is not None
        else os.getenv("NARRATORDB_WORKSPACE_ID", "").strip()
    )
    base = Path(cwd or os.getcwd()).expanduser().resolve()
    if explicit:
        return ProjectScope(
            workspace_id=sanitize_scope(explicit),
            origin="explicit",
            project_root=str(base),
            in_git_repo=bool(_run_git(base, "rev-parse", "--show-toplevel")),
        )

    root_text = _run_git(base, "rev-parse", "--show-toplevel")
    root = Path(root_text).resolve() if root_text else base
    remote = _run_git(root, "config", "--get", "remote.origin.url")
    slug = _remote_slug(remote)
    if slug:
        return ProjectScope(
            workspace_id=sanitize_scope(f"project/{slug}"),
            origin="git_remote",
            project_root=str(root),
            in_git_repo=True,
        )

    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:10]
    name = root.name or "workspace"
    workspace = sanitize_scope(f"project/{name}-{digest}")
    if root_text:
        warning = (
            "PROJECT SCOPE WARNING: Git remote.origin.url was not detected, so "
            "this workspace uses a machine-local path identity. Set "
            "NARRATORDB_WORKSPACE_ID to share the same scope across machines."
        )
        suggested_cwd = str(root) if base != root else None
        confirmation_required = False
    else:
        is_home = root == Path.home().expanduser().resolve()
        if is_home:
            warning = (
                "PROJECT SCOPE WARNING: NarratorDB was started from your home "
                "directory outside a Git repository. Project writes are blocked "
                "until this fallback is explicitly confirmed. Start the client "
                "from the intended project directory or set "
                "NARRATORDB_WORKSPACE_ID."
            )
        else:
            warning = (
                "PROJECT SCOPE WARNING: No Git repository or explicit workspace "
                "ID was detected, so this workspace uses a machine-local path "
                "identity. Start the client from the intended project directory "
                "or set NARRATORDB_WORKSPACE_ID for a portable scope."
            )
        suggested_cwd = None
        confirmation_required = is_home
    return ProjectScope(
        workspace_id=workspace,
        origin="path_fallback",
        project_root=str(root),
        in_git_repo=bool(root_text),
        warning=warning,
        suggested_cwd=suggested_cwd,
        write_confirmation_required=confirmation_required,
    )


def project_workspace(cwd: str | os.PathLike[str] | None = None) -> str:
    """Return a stable project identity without embedding its absolute path."""

    return resolve_project_scope(cwd).workspace_id


def path_fallback_writes_allowed(*, confirmed: bool = False) -> bool:
    """Return whether a user explicitly accepted path-fallback project writes."""

    return bool(
        confirmed
        or os.getenv("NARRATORDB_ALLOW_PATH_FALLBACK_WRITES", "").strip().lower()
        in _TRUE_VALUES
    )


def project_branch(cwd: str | os.PathLike[str] | None = None) -> str:
    base = Path(cwd or os.getcwd()).expanduser().resolve()
    branch = _run_git(base, "branch", "--show-current")
    return branch[:200] if branch else ""


def sanitize_scope(value: str) -> str:
    normalized = _SAFE_SCOPE_RE.sub("-", value.strip()).strip("-./")
    if not normalized:
        raise ValueError("workspace scope cannot be empty")
    if len(normalized) > 240:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
        normalized = f"{normalized[:227]}-{digest}"
    return normalized
