"""Checksummed, bounded portability for NarratorDB service projects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from types import MappingProxyType
from typing import Any, BinaryIO, Iterator, Mapping

from .config import ConfigurationError
from .database import NarratorDB
from .mcp_contract import ALLOWED_SOURCES
from .service import ServiceControlPlane


EXPORT_FORMAT = "narratordb-export-v1"
MANIFEST_FILE = "manifest.json"
MESSAGE_FILE = "messages.jsonl"

MAX_MANIFEST_BYTES = 1_000_000
MAX_MESSAGE_FILE_BYTES = 1_073_741_824
MAX_RECORD_BYTES = 2_000_000
MAX_RECORDS = 1_000_000
MAX_TEXT_CHARS = 1_000_000
MAX_PROVENANCE_BYTES = 100_000

_RECORD_KEYS = frozenset(
    {
        "type",
        "source_message_id",
        "scope",
        "speaker",
        "text",
        "source_timestamp",
        "position",
        "created_at",
        "provenance",
    }
)
_PROVENANCE_KEYS = frozenset(
    {
        "provider",
        "model_id",
        "agent_id",
        "run_id",
        "workspace_id",
        "tool_used",
        "response_id",
        "metadata",
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path, *, maximum: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with _open_regular(path, maximum=maximum) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), total


def _open_regular(path: Path, *, maximum: int) -> BinaryIO:
    if path.is_symlink():
        raise ConfigurationError(f"portable export file cannot be a symbolic link: {path.name}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ConfigurationError(f"portable export file is unavailable: {path.name}") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ConfigurationError(
                f"portable export entry must be a regular file: {path.name}"
            )
        if details.st_size > maximum:
            raise ConfigurationError(
                f"portable export file exceeds its size limit: {path.name}"
            )
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _exclusive_text(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")


def _safe_output_directory(value: str | os.PathLike[str]) -> Path:
    requested = Path(value).expanduser()
    if requested.is_symlink():
        raise ConfigurationError("portable export directory cannot be a symbolic link")
    target = requested.resolve()
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise ConfigurationError("portable export directory must be new or empty")
    else:
        target.mkdir(parents=True, mode=0o700)
    os.chmod(target, 0o700)
    return target


def _bounded_string(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ConfigurationError(f"portable export {field} is invalid")
    return value


def _bounded_integer(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"portable export {field} is invalid")
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"portable export {field} is outside its limit")
    return value


def _validate_provenance(value: Any, *, line_number: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _PROVENANCE_KEYS:
        raise ConfigurationError(
            f"portable export provenance is invalid on line {line_number}"
        )
    if len(_canonical_json(value).encode("utf-8")) > MAX_PROVENANCE_BYTES:
        raise ConfigurationError(
            f"portable export provenance exceeds its limit on line {line_number}"
        )
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "metadata":
            if not isinstance(item, dict) or len(item) > 100:
                raise ConfigurationError(
                    f"portable export metadata is invalid on line {line_number}"
                )
            metadata: dict[str, str] = {}
            for metadata_key, metadata_value in item.items():
                if not isinstance(metadata_key, str) or not 1 <= len(metadata_key) <= 200:
                    raise ConfigurationError(
                        f"portable export metadata key is invalid on line {line_number}"
                    )
                if isinstance(metadata_value, (dict, list)) or metadata_value is None:
                    raise ConfigurationError(
                        f"portable export metadata value is invalid on line {line_number}"
                    )
                rendered = str(metadata_value)
                if len(rendered) > 10_000:
                    raise ConfigurationError(
                        f"portable export metadata value is too long on line {line_number}"
                    )
                metadata[metadata_key] = rendered
            if metadata:
                result[key] = metadata
            continue
        result[key] = _bounded_string(
            item,
            field=f"provenance.{key} on line {line_number}",
            maximum=10_000,
        )
    return result


def _validate_record(value: Any, *, line_number: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _RECORD_KEYS:
        raise ConfigurationError(f"portable export record is invalid on line {line_number}")
    if value.get("type") != "message" or value.get("scope") not in {"global", "project"}:
        raise ConfigurationError(f"portable export scope is invalid on line {line_number}")
    if value.get("speaker") not in ALLOWED_SOURCES:
        raise ConfigurationError(f"portable export speaker is invalid on line {line_number}")
    timestamp = value.get("source_timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise ConfigurationError(
            f"portable export timestamp is invalid on line {line_number}"
        )
    timestamp = float(timestamp)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ConfigurationError(
            f"portable export timestamp is invalid on line {line_number}"
        )
    return {
        "type": "message",
        "source_message_id": _bounded_integer(
            value.get("source_message_id"),
            field=f"source_message_id on line {line_number}",
            minimum=1,
            maximum=2**63 - 1,
        ),
        "scope": str(value["scope"]),
        "speaker": str(value["speaker"]),
        "text": _bounded_string(
            value.get("text"),
            field=f"text on line {line_number}",
            maximum=MAX_TEXT_CHARS,
        ),
        "source_timestamp": timestamp,
        "position": _bounded_integer(
            value.get("position"),
            field=f"position on line {line_number}",
            minimum=0,
            maximum=2**63 - 1,
        ),
        "created_at": _bounded_string(
            value.get("created_at"),
            field=f"created_at on line {line_number}",
            maximum=100,
        ),
        "provenance": _validate_provenance(
            value.get("provenance"), line_number=line_number
        ),
    }


def _iter_records(path: Path) -> Iterator[dict[str, Any]]:
    with _open_regular(path, maximum=MAX_MESSAGE_FILE_BYTES) as handle:
        line_number = 0
        while True:
            raw = handle.readline(MAX_RECORD_BYTES + 1)
            if not raw:
                break
            line_number += 1
            if len(raw) > MAX_RECORD_BYTES or not raw.endswith(b"\n"):
                raise ConfigurationError(
                    f"portable export line {line_number} exceeds its limit or is truncated"
                )
            if line_number > MAX_RECORDS:
                raise ConfigurationError("portable export record count exceeds its limit")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ConfigurationError(
                    f"portable export JSON is invalid on line {line_number}"
                ) from error
            yield _validate_record(value, line_number=line_number)


def _immutable(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _immutable(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_immutable(item) for item in value)
    return value


@dataclass(frozen=True)
class PortableExport:
    """A fully verified export descriptor whose records can be streamed again."""

    root: Path
    manifest: Mapping[str, Any]
    record_count: int

    def iter_records(self) -> Iterator[dict[str, Any]]:
        yield from _iter_records(self.root / MESSAGE_FILE)


def export_service_project(
    *,
    data_dir: str | os.PathLike[str],
    project: str,
    output_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    """Export one service project plus account-global memories from an online snapshot."""

    control = ServiceControlPlane(data_dir)
    account_id, project_id = control.resolve_project(project)
    source_path = control.account_db_path(account_id)
    if source_path.is_symlink() or not source_path.is_file():
        raise ConfigurationError("service account database is unavailable or unsafe")
    target = _safe_output_directory(output_dir)
    message_path = target / MESSAGE_FILE
    counts = {"global": 0, "project": 0, "total": 0}
    written_bytes = 0

    with tempfile.TemporaryDirectory(prefix="narratordb-export-") as temporary:
        snapshot = Path(temporary) / "snapshot.db"
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
        destination = sqlite3.connect(snapshot)
        try:
            source.backup(destination)
        except sqlite3.DatabaseError as error:
            raise ConfigurationError("service account database could not be snapshotted") from error
        finally:
            destination.close()
            source.close()
        database_hash, _ = _sha256_file(snapshot, maximum=MAX_MESSAGE_FILE_BYTES)
        connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        workspace_key = f"{account_id}::workspace::project/{project_id}"
        try:
            rows = connection.execute(
                "SELECT m.id, m.user_id, m.speaker, m.text, m.timestamp, "
                "m.position, m.created_at, p.provider, p.model_id, p.agent_id, "
                "p.run_id, p.workspace_id, p.tool_used, p.response_id, "
                "p.metadata_json FROM messages AS m "
                "LEFT JOIN message_provenance AS p ON p.message_id = m.id "
                "WHERE m.user_id IN (?, ?) ORDER BY m.user_id, m.position, m.id",
                (account_id, workspace_key),
            )
            with _exclusive_text(message_path) as handle:
                for row in rows:
                    if counts["total"] >= MAX_RECORDS:
                        raise ConfigurationError("portable export record count exceeds its limit")
                    scope = "global" if str(row["user_id"]) == account_id else "project"
                    provenance = {
                        key: row[key]
                        for key in (
                            "provider",
                            "model_id",
                            "agent_id",
                            "run_id",
                            "workspace_id",
                            "tool_used",
                            "response_id",
                        )
                        if row[key] is not None
                    }
                    if row["metadata_json"]:
                        try:
                            metadata = json.loads(row["metadata_json"])
                        except json.JSONDecodeError as error:
                            raise ConfigurationError(
                                f"message {row['id']} contains invalid provenance JSON"
                            ) from error
                        if not isinstance(metadata, dict):
                            raise ConfigurationError(
                                f"message {row['id']} contains invalid provenance metadata"
                            )
                        provenance["metadata"] = metadata
                    record = _validate_record(
                        {
                            "type": "message",
                            "source_message_id": int(row["id"]),
                            "scope": scope,
                            "speaker": str(row["speaker"]),
                            "text": str(row["text"]),
                            "source_timestamp": float(row["timestamp"]),
                            "position": int(row["position"]),
                            "created_at": str(row["created_at"]),
                            "provenance": provenance,
                        },
                        line_number=counts["total"] + 1,
                    )
                    rendered = (_canonical_json(record) + "\n").encode("utf-8")
                    if len(rendered) > MAX_RECORD_BYTES:
                        raise ConfigurationError("portable export record exceeds its limit")
                    written_bytes += len(rendered)
                    if written_bytes > MAX_MESSAGE_FILE_BYTES:
                        raise ConfigurationError("portable export file exceeds its size limit")
                    handle.write(rendered.decode("utf-8"))
                    counts[scope] += 1
                    counts["total"] += 1
        except sqlite3.DatabaseError as error:
            raise ConfigurationError("service account database is unreadable") from error
        finally:
            connection.close()

    message_hash, message_bytes = _sha256_file(
        message_path, maximum=MAX_MESSAGE_FILE_BYTES
    )
    manifest = {
        "format": EXPORT_FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {"account_id": account_id, "project_id": project_id, "database_sha256": database_hash},
        "files": {
            MESSAGE_FILE: {
                "sha256": message_hash,
                "bytes": message_bytes,
                "records": counts["total"],
            }
        },
        "counts": counts,
    }
    manifest_path = target / MANIFEST_FILE
    rendered_manifest = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(rendered_manifest) > MAX_MANIFEST_BYTES:
        raise ConfigurationError("portable export manifest exceeds its size limit")
    with _exclusive_text(manifest_path) as handle:
        handle.write(rendered_manifest.decode("utf-8"))
    return {
        "format": EXPORT_FORMAT,
        "output_dir": str(target),
        "manifest": str(manifest_path),
        "counts": counts,
    }


def load_export(value: str | os.PathLike[str]) -> PortableExport:
    """Verify checksums, structure, bounds, record count, and every JSONL record."""

    requested = Path(value).expanduser()
    if requested.is_symlink():
        raise ConfigurationError("portable export directory cannot be a symbolic link")
    root = requested.resolve()
    if not root.is_dir():
        raise ConfigurationError("portable export directory was not found")
    manifest_path = root / MANIFEST_FILE
    message_path = root / MESSAGE_FILE
    with _open_regular(manifest_path, maximum=MAX_MANIFEST_BYTES) as handle:
        raw_manifest = handle.read(MAX_MANIFEST_BYTES + 1)
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("portable export manifest is unreadable") from error
    try:
        if not isinstance(manifest, dict) or manifest.get("format") != EXPORT_FORMAT:
            raise ConfigurationError("unsupported portable export format")
        files = manifest["files"]
        if not isinstance(files, dict) or set(files) != {MESSAGE_FILE}:
            raise ConfigurationError("portable export manifest files are invalid")
        descriptor = files[MESSAGE_FILE]
        if not isinstance(descriptor, dict) or set(descriptor) != {"sha256", "bytes", "records"}:
            raise ConfigurationError("portable export message descriptor is invalid")
        expected_hash = descriptor["sha256"]
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
        ):
            raise ConfigurationError("portable export checksum is invalid")
        expected_bytes = _bounded_integer(
            descriptor["bytes"], field="message bytes", minimum=0, maximum=MAX_MESSAGE_FILE_BYTES
        )
        expected_records = _bounded_integer(
            descriptor["records"], field="record count", minimum=0, maximum=MAX_RECORDS
        )
    except (KeyError, TypeError) as error:
        raise ConfigurationError("portable export manifest is malformed") from error
    actual_hash, actual_bytes = _sha256_file(
        message_path, maximum=MAX_MESSAGE_FILE_BYTES
    )
    if actual_bytes != expected_bytes or not hmac.compare_digest(actual_hash, expected_hash):
        raise ConfigurationError("portable export message checksum does not match the manifest")
    record_count = sum(1 for _ in _iter_records(message_path))
    if record_count != expected_records:
        raise ConfigurationError("portable export record count does not match the manifest")
    return PortableExport(root=root, manifest=_immutable(manifest), record_count=record_count)


def import_service_project(
    *,
    data_dir: str | os.PathLike[str],
    project: str,
    input_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    """Prevalidate and idempotently import an export into one local service project."""

    portable = load_export(input_dir)
    control = ServiceControlPlane(data_dir)
    account_id, project_id = control.resolve_project(project)
    database_path = control.account_db_path(account_id)
    imported = 0
    duplicates = 0
    with NarratorDB(
        data_dir=str(control.data_dir),
        db_path=str(database_path),
        user_id=account_id,
    ) as memory:
        for record in portable.iter_records():
            workspace_id = (
                f"project/{project_id}" if record["scope"] == "project" else None
            )
            provenance = dict(record["provenance"])
            metadata = dict(provenance.get("metadata") or {})
            original_workspace = provenance.get("workspace_id")
            if original_workspace:
                metadata["migration_source_workspace_id"] = str(original_workspace)
            source = portable.manifest.get("source")
            if not isinstance(source, Mapping):
                raise ConfigurationError("portable export source metadata is invalid")
            metadata.update(
                {
                    "migration_format": EXPORT_FORMAT,
                    "migration_source_account_id": str(source.get("account_id") or ""),
                    "migration_source_project_id": str(source.get("project_id") or ""),
                    "migration_source_message_id": str(record["source_message_id"]),
                }
            )
            provenance["metadata"] = metadata
            provenance["workspace_id"] = workspace_id or "global"
            engine = memory._get_engine(workspace_id=workspace_id)
            message_id = engine.remember(
                text=record["text"],
                speaker=record["speaker"],
                timestamp=record["source_timestamp"],
                provenance=provenance,
                semantic_dedup=False,
            )
            if message_id is None:
                duplicates += 1
            else:
                imported += 1
    return {
        "format": EXPORT_FORMAT,
        "source_records": portable.record_count,
        "imported": imported,
        "duplicates": duplicates,
        "verified": True,
        "project_id": project_id,
    }


__all__ = [
    "EXPORT_FORMAT",
    "MAX_MANIFEST_BYTES",
    "MAX_MESSAGE_FILE_BYTES",
    "MAX_RECORD_BYTES",
    "MAX_RECORDS",
    "MAX_TEXT_CHARS",
    "MESSAGE_FILE",
    "MANIFEST_FILE",
    "PortableExport",
    "export_service_project",
    "import_service_project",
    "load_export",
]
