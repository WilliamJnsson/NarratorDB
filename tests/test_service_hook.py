"""Authenticated service lifecycle capture tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from narratordb import ConfigurationError
from narratordb.service_hook import (
    read_service_hook_config,
    run_service_hook,
    write_service_hook_config,
)


class ServiceHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.credentials = self.root / "credentials.env"
        self.credentials.write_text(
            "NARRATORDB_SERVICE_URL=http://127.0.0.1:8787/mcp\n"
            f"NARRATORDB_SERVICE_TOKEN=ndb_{'A' * 43}\n"
            "NARRATORDB_PROJECT_ID=00000000-0000-0000-0000-000000000001\n",
            encoding="utf-8",
        )
        self.credentials.chmod(0o600)
        self.transcript = self.root / "transcript.jsonl"
        entries = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "The deployment decision is to retain detailed rollback notes.",
                        }
                    ],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Recorded the deployment decision and its rollback constraints for the next session.",
                        }
                    ],
                },
            },
        ]
        self.transcript.write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n",
            encoding="utf-8",
        )
        self.event = {
            "cwd": str(self.root),
            "session_id": "service-hook-test",
            "transcript_path": str(self.transcript),
        }

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_hook_config_is_private_strict_and_round_trips(self) -> None:
        target = self.root / "service-hook.json"
        written = write_service_hook_config(
            self.credentials,
            target=target,
            python=sys.executable,
        )
        self.assertEqual(written, target.resolve())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        values = read_service_hook_config(target)
        self.assertEqual(
            Path(values["credentials_file"]),
            self.credentials.resolve(),
        )
        self.assertEqual(values["python"], os.path.abspath(sys.executable))

        target.chmod(0o644)
        with self.assertRaisesRegex(ConfigurationError, "mode 0600"):
            read_service_hook_config(target)

    def test_stop_captures_sessions_into_the_authenticated_project(self) -> None:
        runtime = MagicMock()
        runtime.status.return_value = {"capture_policy": "sessions"}
        with patch(
            "narratordb.service_hook.ServiceBridgeRuntime",
            return_value=runtime,
        ):
            run_service_hook(
                "Stop",
                self.event,
                credentials_file=self.credentials,
            )

        runtime.status.assert_called_once_with(scope="project", full_check=False)
        runtime.remember_session.assert_called_once()
        messages = runtime.remember_session.call_args.args[0]
        self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
        self.assertIn("rollback notes", messages[0]["content"])
        self.assertEqual(
            runtime.remember_session.call_args.kwargs["scope"],
            "project",
        )

    def test_hook_config_rejects_symlinked_credentials_and_parent(self) -> None:
        linked_credentials = self.root / "linked.env"
        linked_parent = self.root / "linked-config"
        try:
            linked_credentials.symlink_to(self.credentials)
            real_parent = self.root / "real-config"
            real_parent.mkdir()
            linked_parent.symlink_to(real_parent, target_is_directory=True)
        except OSError as error:  # pragma: no cover - unsupported platform
            self.skipTest(f"symbolic links unavailable: {error}")

        with self.assertRaisesRegex(ConfigurationError, "symbolic link"):
            write_service_hook_config(
                linked_credentials,
                target=self.root / "service-hook.json",
            )
        with self.assertRaisesRegex(ConfigurationError, "symlink"):
            write_service_hook_config(
                self.credentials,
                target=linked_parent / "service-hook.json",
            )

    def test_manual_policy_and_submit_event_do_not_capture(self) -> None:
        manual = MagicMock()
        manual.status.return_value = {"capture_policy": "manual"}
        with patch(
            "narratordb.service_hook.ServiceBridgeRuntime",
            return_value=manual,
        ):
            run_service_hook(
                "PreCompact",
                self.event,
                credentials_file=self.credentials,
            )
        manual.remember_session.assert_not_called()

        submit = MagicMock()
        with patch(
            "narratordb.service_hook.ServiceBridgeRuntime",
            return_value=submit,
        ):
            run_service_hook(
                "UserPromptSubmit",
                self.event,
                credentials_file=self.credentials,
            )
        submit.status.assert_not_called()
        submit.remember_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
