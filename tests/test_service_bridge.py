"""Secure service bridge and Codex registration tests."""

from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from narratordb import ConfigurationError
from narratordb.mcp_install import (
    _ensure_codex_service_plugin,
    install_remote_service,
    install_service_bridge,
)
from narratordb.service_bridge import (
    BOOTSTRAP_TIMEOUT_SECONDS,
    BOOTSTRAP_TOKEN_BUDGET,
    MAX_BOOTSTRAP_CONTEXT_BYTES,
    SERVICE_CALL_TIMEOUT_SECONDS,
    ServiceBridgeRuntime,
    main as service_bridge_main,
    read_service_credentials,
    write_service_credentials,
)


def _completed(
    returncode: int = 0, *, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class ServiceBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.credentials = self.root / "credentials.env"
        self.token = "ndb_" + "A" * 43
        self.credentials.write_text(
            "NARRATORDB_SERVICE_URL=http://127.0.0.1:8787/mcp\n"
            f"NARRATORDB_SERVICE_TOKEN={self.token}\n"
            "NARRATORDB_PROJECT_ID=00000000-0000-0000-0000-000000000001\n",
            encoding="utf-8",
        )
        self.credentials.chmod(0o600)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_credentials_are_strict_private_and_loopback_safe(self) -> None:
        values = read_service_credentials(self.credentials)
        self.assertEqual(values["NARRATORDB_SERVICE_TOKEN"], self.token)

        self.credentials.chmod(0o644)
        if os.name != "nt":
            with self.assertRaisesRegex(ConfigurationError, "mode 0600"):
                read_service_credentials(self.credentials)
        self.credentials.chmod(0o600)
        self.credentials.write_text(
            self.credentials.read_text(encoding="utf-8").replace(
                "http://127.0.0.1:8787", "http://service.example"
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigurationError, "HTTPS"):
            read_service_credentials(self.credentials)

    def test_remote_credentials_reject_unsafe_url_and_are_written_privately(
        self,
    ) -> None:
        target = self.root / "remote.env"
        with self.assertRaisesRegex(ConfigurationError, "without embedded credentials"):
            write_service_credentials(
                target,
                service_url="https://user@example.com/mcp",
                token=self.token,
                project_id="00000000-0000-0000-0000-000000000001",
            )
        written = write_service_credentials(
            target,
            service_url="https://memory.example/mcp",
            token=self.token,
            project_id="00000000-0000-0000-0000-000000000001",
        )
        self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o600)
        self.assertEqual(
            read_service_credentials(written)["NARRATORDB_SERVICE_URL"],
            "https://memory.example/mcp",
        )

    def test_remote_installer_verifies_project_before_registration(self) -> None:
        target = self.root / "hosted.env"
        project_id = "00000000-0000-0000-0000-000000000001"
        with (
            patch.object(
                ServiceBridgeRuntime,
                "status",
                return_value={"ready": True, "workspace_id": f"project/{project_id}"},
            ),
            patch(
                "narratordb.mcp_install.install_service_bridge",
                return_value={"status": "installed", "client": "codex"},
            ) as install,
        ):
            result = install_remote_service(
                "codex",
                endpoint="https://memory.example/mcp",
                project_id=project_id,
                credentials_file=target,
                token=self.token,
            )
        self.assertTrue(result["verified"])
        self.assertNotIn(self.token, repr(result))
        self.assertNotIn(self.token, repr(install.call_args))

        mismatched = self.root / "mismatched.env"
        with patch.object(
            ServiceBridgeRuntime,
            "status",
            return_value={
                "ready": True,
                "workspace_id": "project/00000000-0000-0000-0000-000000000002",
            },
        ):
            with self.assertRaisesRegex(ConfigurationError, "different project"):
                install_remote_service(
                    "codex",
                    endpoint="https://memory.example/mcp",
                    project_id=project_id,
                    credentials_file=mismatched,
                    token=self.token,
                )
        self.assertFalse(mismatched.exists())

    def test_bridge_maps_tools_without_putting_token_in_arguments(self) -> None:
        runtime = ServiceBridgeRuntime(self.credentials)
        with patch.object(runtime, "_call", return_value={"stored": True}) as call:
            result = runtime.remember("keep this", scope="project")
        self.assertTrue(result["stored"])
        call.assert_called_once_with(
            "remember", {"content": "keep this", "scope": "project"}
        )
        self.assertNotIn(self.token, repr(call.call_args))

    def test_bootstrap_preloads_one_project_resume(self) -> None:
        runtime = ServiceBridgeRuntime(self.credentials)
        with patch.object(
            runtime,
            "_call",
            return_value={"context": "Use the agreed deployment sequence."},
        ) as call:
            first = runtime.bootstrap_context()
            second = runtime.bootstrap_context()

        self.assertEqual(first, "Use the agreed deployment sequence.")
        self.assertEqual(second, first)
        call.assert_called_once_with(
            "resume",
            {
                "topic": "",
                "include_global": False,
                "token_budget": BOOTSTRAP_TOKEN_BUDGET,
            },
            timeout_seconds=BOOTSTRAP_TIMEOUT_SECONDS,
            hard_timeout=True,
        )

    def test_bootstrap_context_enforces_its_own_response_bound(self) -> None:
        runtime = ServiceBridgeRuntime(self.credentials)
        oversized = "\N{SNOWMAN}" * (MAX_BOOTSTRAP_CONTEXT_BYTES + 1)
        with patch.object(
            runtime,
            "_call",
            return_value={"context": oversized},
        ):
            context = runtime.bootstrap_context()

        self.assertTrue(context)
        self.assertLessEqual(len(context.encode("utf-8")), MAX_BOOTSTRAP_CONTEXT_BYTES)

    def test_bootstrap_empty_and_non_structured_results_fail_open(self) -> None:
        for result in ({}, {"context": ""}, {"context": 123}, [], "not-json", None):
            with self.subTest(result=repr(result)):
                runtime = ServiceBridgeRuntime(self.credentials)
                with patch.object(runtime, "_call", return_value=result):
                    self.assertEqual(runtime.bootstrap_context(), "")

    def test_bootstrap_errors_and_timeouts_are_silent_and_not_retried(self) -> None:
        failures = (
            RuntimeError(f"remote failure included {self.token}"),
            TimeoutError(f"cold start exposed {self.token}"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                runtime = ServiceBridgeRuntime(self.credentials)
                output = io.StringIO()
                errors = io.StringIO()
                with (
                    patch.object(runtime, "_call", side_effect=failure) as call,
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    self.assertEqual(runtime.bootstrap_context(), "")
                    self.assertEqual(runtime.bootstrap_context(), "")

                call.assert_called_once()
                emitted = output.getvalue() + errors.getvalue()
                self.assertEqual(emitted, "")
                self.assertNotIn(self.token, emitted)

    def test_bootstrap_enforces_a_hard_wall_clock_timeout(self) -> None:
        runtime = ServiceBridgeRuntime(self.credentials)

        async def delayed_call(*args, **kwargs):
            await asyncio.sleep(0.05)
            return {"context": "too late"}

        with (
            patch.object(runtime, "_call_async", side_effect=delayed_call),
            patch(
                "narratordb.service_bridge.BOOTSTRAP_TIMEOUT_SECONDS",
                0.001,
            ),
        ):
            self.assertEqual(runtime.bootstrap_context(), "")

    def test_ordinary_tool_calls_keep_the_sixty_second_timeout(self) -> None:
        runtime = ServiceBridgeRuntime(self.credentials)
        remote = AsyncMock(return_value={"ready": True})
        with patch.object(runtime, "_call_async", new=remote):
            result = runtime._call("status", {"scope": "project"})

        self.assertTrue(result["ready"])
        remote.assert_awaited_once_with(
            "status",
            {"scope": "project"},
            timeout_seconds=SERVICE_CALL_TIMEOUT_SECONDS,
        )

    def test_bridge_main_enables_startup_bootstrap(self) -> None:
        server = MagicMock()
        with patch(
            "narratordb.service_bridge.create_server", return_value=server
        ) as create:
            result = service_bridge_main(["--credentials-file", str(self.credentials)])

        self.assertEqual(result, 0)
        runtime = create.call_args.args[0]
        self.assertIsInstance(runtime, ServiceBridgeRuntime)
        self.assertTrue(create.call_args.kwargs["include_bootstrap"])
        server.run.assert_called_once_with(transport="stdio")

    def test_bridge_runs_remote_coroutine_from_an_active_event_loop(self) -> None:
        runtime = ServiceBridgeRuntime(self.credentials)
        with patch.object(
            runtime,
            "_call_async",
            new=AsyncMock(return_value={"ok": True}),
        ):

            async def invoke() -> dict:
                return runtime._call("status", {"scope": "project"})

            result = asyncio.run(invoke())
        self.assertTrue(result["ok"])

    def test_service_plugin_requires_explicit_replacement_of_local_plugin(self) -> None:
        inventory = _completed(
            stdout=json.dumps(
                {
                    "installed": [
                        {
                            "pluginId": "narratordb@narratordb-plugins",
                            "installed": True,
                        }
                    ],
                    "available": [
                        {"pluginId": "narratordb-service@narratordb-plugins"}
                    ],
                }
            )
        )
        with patch("narratordb.mcp_install._run", return_value=inventory) as run:
            with self.assertRaisesRegex(ConfigurationError, "different local database"):
                _ensure_codex_service_plugin(force=False)
        run.assert_called_once()

    def test_service_plugin_replaces_conflicting_local_plugin(self) -> None:
        inventory = _completed(
            stdout=json.dumps(
                {
                    "installed": [
                        {
                            "pluginId": "narratordb@narratordb-plugins",
                            "installed": True,
                        }
                    ],
                    "available": [
                        {"pluginId": "narratordb-service@narratordb-plugins"}
                    ],
                }
            )
        )
        with patch(
            "narratordb.mcp_install._run",
            side_effect=[inventory, _completed(), _completed()],
        ) as run:
            result = _ensure_codex_service_plugin(force=True)
        self.assertTrue(result["installed"])
        self.assertTrue(result["local_plugin_removed"])
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                "codex",
                "plugin",
                "add",
                "narratordb-service@narratordb-plugins",
            ],
        )
        self.assertEqual(
            run.call_args_list[2].args[0],
            [
                "codex",
                "plugin",
                "remove",
                "narratordb@narratordb-plugins",
            ],
        )

    def test_codex_registration_uses_only_the_credentials_path(self) -> None:
        not_found = _completed(
            1, stderr="Error: No MCP server named 'narratordb' found."
        )
        added = _completed(0)
        with (
            patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
            patch("narratordb.mcp_install._preflight_codex_service_plugin"),
            patch(
                "narratordb.mcp_install._ensure_codex_service_plugin",
                return_value={"installed": True},
            ),
            patch(
                "narratordb.service_hook.write_service_hook_config",
                return_value=self.root / "service-hook.json",
            ),
            patch(
                "narratordb.mcp_install.subprocess.run",
                side_effect=[not_found, added],
            ) as run,
        ):
            result = install_service_bridge(self.credentials)
        self.assertEqual(result["status"], "installed")
        add_command = run.call_args_list[-1].args[0]
        self.assertIn(str(self.credentials.resolve()), add_command)
        self.assertNotIn(self.token, repr(add_command))
        self.assertEqual(stat.S_IMODE(self.credentials.stat().st_mode), 0o600)

    def test_current_bridge_registration_is_idempotent(self) -> None:
        runtime = ServiceBridgeRuntime(self.credentials)
        server_command = [
            os.path.abspath(os.sys.executable),
            "-m",
            "narratordb.service_bridge",
            "--credentials-file",
            runtime.credentials_file,
        ]
        current = _completed(
            0,
            stdout=json.dumps(
                {
                    "transport": {
                        "type": "stdio",
                        "command": server_command[0],
                        "args": server_command[1:],
                    }
                }
            ),
        )
        with (
            patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
            patch("narratordb.mcp_install._preflight_codex_service_plugin"),
            patch(
                "narratordb.mcp_install._ensure_codex_service_plugin",
                return_value={"installed": True},
            ),
            patch(
                "narratordb.service_hook.write_service_hook_config",
                return_value=self.root / "service-hook.json",
            ),
            patch(
                "narratordb.mcp_install.subprocess.run",
                side_effect=[current, current],
            ) as run,
        ):
            result = install_service_bridge(self.credentials)
        self.assertEqual(result["status"], "already_installed")
        self.assertFalse(result["changed"])
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
