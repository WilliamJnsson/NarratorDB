"""Portable Community export/import contract tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from narratordb import ConfigurationError, NarratorDB
from narratordb.config import MemoryMode
from narratordb.portability import (
    MESSAGE_FILE,
    MANIFEST_FILE,
    export_service_project,
    import_service_project,
    load_export,
)
from narratordb.service import ServiceControlPlane, initialize_service


class PortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source_dir = self.root / "source"
        initialize_service(
            data_dir=self.source_dir,
            project_name="source-project",
            credentials_file=self.root / "source.env",
            mode=MemoryMode.PRIVATE,
            compiler=None,
            capture_policy="manual",
        )
        control = ServiceControlPlane(self.source_dir)
        account_id, project_id = control.resolve_project("source-project")
        with NarratorDB(
            data_dir=str(control.data_dir),
            db_path=str(control.account_db_path(account_id)),
            user_id=account_id,
        ) as memory:
            memory.remember(
                "The portable project uses a cobalt release.",
                workspace_id=f"project/{project_id}",
                provenance={"workspace_id": f"project/{project_id}"},
            )
            memory.remember(
                "The user prefers concise release notes.",
                workspace_id=None,
                provenance={"workspace_id": "global"},
            )
        self.export_dir = self.root / "export"
        export_service_project(
            data_dir=self.source_dir,
            project="source-project",
            output_dir=self.export_dir,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _rewrite_manifest_checksum(self) -> None:
        message_path = self.export_dir / MESSAGE_FILE
        payload = message_path.read_bytes()
        manifest_path = self.export_dir / MANIFEST_FILE
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["files"][MESSAGE_FILE]["sha256"] = hashlib.sha256(payload).hexdigest()
        manifest["files"][MESSAGE_FILE]["bytes"] = len(payload)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_verified_export_import_is_idempotent_and_preserves_scopes(self) -> None:
        loaded = load_export(self.export_dir)
        self.assertEqual(loaded.record_count, 2)
        self.assertEqual({record["scope"] for record in loaded.iter_records()}, {"project", "global"})

        destination = self.root / "destination"
        initialize_service(
            data_dir=destination,
            project_name="target-project",
            credentials_file=self.root / "destination.env",
            mode=MemoryMode.PRIVATE,
            compiler=None,
            capture_policy="manual",
        )
        first = import_service_project(
            data_dir=destination,
            project="target-project",
            input_dir=self.export_dir,
        )
        second = import_service_project(
            data_dir=destination,
            project="target-project",
            input_dir=self.export_dir,
        )
        self.assertEqual(first["imported"], 2)
        self.assertEqual(second["duplicates"], 2)

        control = ServiceControlPlane(destination)
        account_id, project_id = control.resolve_project("target-project")
        with NarratorDB(
            data_dir=str(control.data_dir),
            db_path=str(control.account_db_path(account_id)),
            user_id=account_id,
        ) as memory:
            project = memory.recall_context(
                "What release does the portable project use?",
                workspace_id=f"project/{project_id}",
            )
            global_result = memory.recall_context(
                "How does the user prefer release notes?", workspace_id=None
            )
        self.assertIn("cobalt release", project.text)
        self.assertIn("concise release notes", global_result.text)

    def test_checksum_tampering_and_unknown_fields_fail_closed(self) -> None:
        message_path = self.export_dir / MESSAGE_FILE
        message_path.write_bytes(message_path.read_bytes() + b"{}\n")
        with self.assertRaisesRegex(ConfigurationError, "checksum"):
            load_export(self.export_dir)

        records = message_path.read_text("utf-8").splitlines()[:-1]
        record = json.loads(records[0])
        record["unexpected"] = True
        records[0] = json.dumps(record)
        message_path.write_text("\n".join(records) + "\n", encoding="utf-8")
        self._rewrite_manifest_checksum()
        with self.assertRaisesRegex(ConfigurationError, "record is invalid"):
            load_export(self.export_dir)

    def test_symlinked_export_file_is_rejected(self) -> None:
        message_path = self.export_dir / MESSAGE_FILE
        replacement = self.root / "messages-copy.jsonl"
        replacement.write_bytes(message_path.read_bytes())
        message_path.unlink()
        try:
            message_path.symlink_to(replacement)
        except OSError as error:
            self.skipTest(f"symbolic links unavailable: {error}")
        with self.assertRaisesRegex(ConfigurationError, "symbolic link"):
            load_export(self.export_dir)


if __name__ == "__main__":
    unittest.main()
