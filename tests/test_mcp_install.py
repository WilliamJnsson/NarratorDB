"""Tests for native Codex and Claude Code MCP registration."""

from __future__ import annotations

from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from narratordb import CompilerConfig, ConfigurationError, NarratorDB
from narratordb.cli import build_parser, main
from narratordb.config import FeatureUnavailableError, ProjectConfigStore
from narratordb.mcp_install import install_mcp_client, uninstall_mcp_client


def _completed(
    returncode: int = 0, *, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _not_registered() -> subprocess.CompletedProcess[str]:
    return _completed(1, stderr="No MCP server named 'narratordb' found.")


class MCPInstallTests(unittest.TestCase):
    def test_codex_private_install_initializes_database_and_uses_native_cli(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with (
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    side_effect=[_not_registered(), _completed()],
                ) as run,
            ):
                result = install_mcp_client(
                    "codex", path=path, user_id="william", mode="private"
                )

            self.assertEqual(result["status"], "installed")
            self.assertTrue(result["changed"])
            self.assertTrue(path.is_file())
            self.assertEqual(ProjectConfigStore(str(path)).load().mode.value, "private")

            server_command = result["server"]["command"]
            self.assertEqual(server_command[0], os.path.abspath(sys.executable))
            self.assertEqual(
                server_command[1:],
                [
                    "-m",
                    "narratordb.mcp_server",
                    "--path",
                    str(path.resolve()),
                    "--user-id",
                    "william",
                    "--init-mode",
                    "private",
                    "--client",
                    "codex",
                ],
            )
            self.assertEqual(
                result["commands"]["add"],
                ["codex", "mcp", "add", "narratordb", "--", *server_command],
            )
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assertIsInstance(call.args[0], list)
                self.assertNotIn("shell", call.kwargs)

    def test_claude_install_uses_user_scope_and_argument_array(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with (
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch(
                    "narratordb.mcp_install.shutil.which", return_value="/bin/claude"
                ),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    side_effect=[_not_registered(), _completed()],
                ),
            ):
                result = install_mcp_client(
                    "claude", path=path, user_id="william", mode="private"
                )

            self.assertEqual(
                result["commands"]["add"],
                [
                    "claude",
                    "mcp",
                    "add",
                    "--scope",
                    "user",
                    "narratordb",
                    "--",
                    *result["server"]["command"],
                ],
            )
            self.assertIn("Claude Code", result["restart"])

    def test_duplicate_requires_force_without_creating_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with (
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    return_value=_completed(stdout='{"name":"narratordb"}'),
                ) as run,
            ):
                with self.assertRaisesRegex(ConfigurationError, "--force"):
                    install_mcp_client("codex", path=path, mode="private")

            self.assertFalse(path.exists())
            self.assertEqual(run.call_count, 1)

    def test_force_removes_then_replaces_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with (
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch(
                    "narratordb.mcp_install.shutil.which", return_value="/bin/claude"
                ),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    side_effect=[_completed(), _completed(), _completed()],
                ) as run,
            ):
                result = install_mcp_client(
                    "claude", path=path, mode="private", force=True
                )

            self.assertEqual(result["status"], "replaced")
            self.assertEqual(
                run.call_args_list[1].args[0],
                [
                    "claude",
                    "mcp",
                    "remove",
                    "--scope",
                    "user",
                    "narratordb",
                ],
            )
            self.assertEqual(run.call_args_list[2].args[0], result["commands"]["add"])

    def test_dry_run_is_json_safe_and_does_not_create_database_or_register(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with (
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    return_value=_not_registered(),
                ) as run,
            ):
                result = install_mcp_client(
                    "codex", path=path, mode="private", dry_run=True
                )

            self.assertEqual(result["status"], "would_install")
            self.assertFalse(result["changed"])
            self.assertTrue(result["database"]["would_initialize"])
            self.assertIsNone(result["database"]["compiler"])
            self.assertFalse(path.exists())
            json.dumps(result)
            self.assertEqual(run.call_count, 1)

    def test_intelligence_dry_run_discloses_credential_free_compiler_plan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            compiler = CompilerConfig.openai(model="gpt-5.4-mini")
            with (
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    return_value=_not_registered(),
                ),
            ):
                result = install_mcp_client(
                    "codex",
                    path=path,
                    mode="intelligence",
                    compiler=compiler,
                    dry_run=True,
                )

            self.assertEqual(result["database"]["compiler"], compiler.to_dict())
            self.assertFalse(path.exists())

    def test_programmatic_new_install_requires_explicit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with (
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    return_value=_not_registered(),
                ) as run,
            ):
                with self.assertRaisesRegex(ConfigurationError, "mode is required"):
                    install_mcp_client("codex", path=path, dry_run=True)

            self.assertEqual(run.call_count, 1)
            self.assertFalse(path.exists())

    def test_install_can_persist_explicit_home_fallback_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with (
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    return_value=_not_registered(),
                ),
            ):
                result = install_mcp_client(
                    "codex",
                    path=path,
                    mode="private",
                    dry_run=True,
                    allow_path_fallback_writes=True,
                )

        self.assertEqual(
            result["server"]["command"][-1], "--allow-path-fallback-writes"
        )

    def test_intelligence_install_reuses_persisted_compiler_without_serializing_key(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with NarratorDB(
                db_path=str(path),
                mode="intelligence",
                compiler=CompilerConfig.openai(),
            ) as memory:
                memory.project_status()

            secret = "test-api-key-that-must-not-be-serialized"
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=False),
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    side_effect=[_not_registered(), _completed()],
                ),
            ):
                result = install_mcp_client("codex", path=path)

            serialized = json.dumps(result)
            self.assertNotIn(secret, serialized)
            self.assertNotIn("OPENAI_API_KEY", serialized)
            self.assertEqual(result["database"]["mode"], "intelligence")
            self.assertEqual(result["server"]["command"][-3], "intelligence")
            self.assertEqual(result["server"]["command"][-1], "codex")

    def test_new_intelligence_install_initializes_local_compiler_in_one_step(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            compiler = CompilerConfig.local(
                model="local-test",
                endpoint="http://127.0.0.1:11434/v1",
            )
            with (
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    side_effect=[_not_registered(), _completed()],
                ),
            ):
                result = install_mcp_client(
                    "codex",
                    path=path,
                    mode="intelligence",
                    compiler=compiler,
                )

            persisted = ProjectConfigStore(str(path)).load()
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.mode.value, "intelligence")
            self.assertEqual(persisted.compiler, compiler)
            self.assertEqual(result["database"]["mode"], "intelligence")
            self.assertEqual(result["database"]["compiler"]["kind"], "local")
            self.assertTrue(result["database"]["created_for_install"])

    def test_new_hosted_intelligence_install_does_not_serialize_runtime_key(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            secret = "test-openai-key-that-must-remain-in-the-environment"
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=False),
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    side_effect=[_not_registered(), _completed()],
                ),
                patch("sys.stdout", new_callable=StringIO) as stdout,
            ):
                code = main(
                    [
                        "--path",
                        str(path),
                        "mcp",
                        "install",
                        "codex",
                        "--mode",
                        "intelligence",
                        "--compiler",
                        "openai",
                        "--model",
                        "gpt-5.4-mini",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            serialized = stdout.getvalue()
            result = json.loads(serialized)
            self.assertNotIn(secret, serialized)
            self.assertNotIn("OPENAI_API_KEY", serialized)
            self.assertEqual(result["database"]["mode"], "intelligence")
            self.assertEqual(result["database"]["compiler"]["kind"], "openai")

    def test_install_rejects_compiler_change_for_configured_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with NarratorDB(
                db_path=str(path),
                mode="intelligence",
                compiler=CompilerConfig.openai(model="gpt-5.4-mini"),
            ):
                pass
            with (
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    return_value=_not_registered(),
                ),
            ):
                with self.assertRaisesRegex(
                    ConfigurationError, "different compiler configuration"
                ):
                    install_mcp_client(
                        "codex",
                        path=path,
                        mode="intelligence",
                        compiler=CompilerConfig.openai(model="gpt-5.6-luna"),
                    )

    def test_new_intelligence_database_requires_explicit_compiler_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with (
                patch("narratordb.mcp_install._validate_mcp_extra"),
                patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
                patch(
                    "narratordb.mcp_install.subprocess.run",
                    return_value=_not_registered(),
                ) as run,
            ):
                with self.assertRaisesRegex(
                    ConfigurationError, "needs a compiler first"
                ):
                    install_mcp_client("codex", path=path, mode="intelligence")

            self.assertFalse(path.exists())
            self.assertEqual(run.call_count, 1)

    def test_uninstall_is_idempotent_and_preserves_data(self) -> None:
        with (
            patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
            patch(
                "narratordb.mcp_install.subprocess.run",
                return_value=_not_registered(),
            ) as run,
        ):
            absent = uninstall_mcp_client("codex")
        self.assertEqual(absent["status"], "not_installed")
        self.assertTrue(absent["data_preserved"])
        self.assertEqual(run.call_count, 1)

        with (
            patch("narratordb.mcp_install.shutil.which", return_value="/bin/codex"),
            patch(
                "narratordb.mcp_install.subprocess.run",
                side_effect=[_completed(), _completed()],
            ) as run,
        ):
            removed = uninstall_mcp_client("codex")
        self.assertEqual(removed["status"], "uninstalled")
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["codex", "mcp", "remove", "narratordb"],
        )

    def test_missing_mcp_extra_or_client_fails_before_registration(self) -> None:
        with patch(
            "narratordb.mcp_install.importlib.import_module",
            side_effect=ModuleNotFoundError("mcp"),
        ):
            with self.assertRaisesRegex(
                FeatureUnavailableError, r"narratordb-memory\[mcp\]"
            ):
                install_mcp_client("codex", dry_run=True)

        with (
            patch("narratordb.mcp_install._validate_mcp_extra"),
            patch("narratordb.mcp_install.shutil.which", return_value=None),
        ):
            with self.assertRaisesRegex(ConfigurationError, "not available on PATH"):
                install_mcp_client("codex", dry_run=True)

    def test_cli_parser_and_dry_run_json_handler(self) -> None:
        args = build_parser().parse_args(
            ["mcp", "install", "codex", "--mode", "private", "--dry-run", "--json"]
        )
        self.assertEqual(args.client, "codex")
        self.assertEqual(args.mode, "private")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.json)

        unselected = build_parser().parse_args(["mcp", "install", "codex"])
        self.assertIsNone(unselected.mode)

        confirmed = build_parser().parse_args(
            ["mcp", "install", "codex", "--allow-path-fallback-writes"]
        )
        self.assertTrue(confirmed.allow_path_fallback_writes)

        expected = {"status": "would_install", "dry_run": True}
        with (
            patch(
                "narratordb.cli.install_mcp_client", return_value=expected
            ) as install,
            patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            code = main(
                [
                    "--path",
                    "/tmp/narratordb-test.db",
                    "mcp",
                    "install",
                    "codex",
                    "--mode",
                    "private",
                    "--dry-run",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), expected)
        install.assert_called_once()

    def test_cli_requires_first_run_mode_when_noninteractive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "memory.db")
            with (
                patch("sys.stdin.isatty", return_value=False),
                patch("narratordb.cli.install_mcp_client") as install,
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                code = main(
                    [
                        "--path",
                        path,
                        "mcp",
                        "install",
                        "codex",
                        "--dry-run",
                    ]
                )

        self.assertEqual(code, 2)
        self.assertIn("--mode is required", stderr.getvalue())
        install.assert_not_called()

    def test_cli_can_configure_intelligence_install_in_one_step(self) -> None:
        expected = {"status": "would_install", "dry_run": True}
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "memory.db")
            with (
                patch(
                    "narratordb.cli.install_mcp_client", return_value=expected
                ) as install,
                patch("sys.stdout", new_callable=StringIO),
            ):
                code = main(
                    [
                        "--path",
                        path,
                        "mcp",
                        "install",
                        "codex",
                        "--mode",
                        "intelligence",
                        "--compiler",
                        "local",
                        "--model",
                        "local-test",
                        "--endpoint",
                        "http://127.0.0.1:11434/v1",
                        "--dry-run",
                    ]
                )

        self.assertEqual(code, 0)
        compiler = install.call_args.kwargs["compiler"]
        self.assertEqual(compiler.kind.value, "local")
        self.assertEqual(compiler.model, "local-test")
        self.assertEqual(
            compiler.endpoint,
            "http://127.0.0.1:11434/v1",
        )
        self.assertEqual(install.call_args.kwargs["mode"].value, "intelligence")

    def test_cli_interactive_first_run_completes_local_compiler_setup(self) -> None:
        expected = {"status": "would_install", "dry_run": True}
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "memory.db")
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch(
                    "builtins.input",
                    side_effect=["2", "1", "local-test", ""],
                ),
                patch(
                    "narratordb.cli.install_mcp_client", return_value=expected
                ) as install,
                patch("sys.stdout", new_callable=StringIO),
            ):
                code = main(
                    [
                        "--path",
                        path,
                        "mcp",
                        "install",
                        "codex",
                        "--dry-run",
                    ]
                )

        self.assertEqual(code, 0)
        compiler = install.call_args.kwargs["compiler"]
        self.assertEqual(install.call_args.kwargs["mode"].value, "intelligence")
        self.assertEqual(compiler.kind.value, "local")
        self.assertEqual(compiler.model, "local-test")
        self.assertEqual(compiler.endpoint, "http://127.0.0.1:11434/v1")

    def test_cli_reuses_existing_intelligence_choice_without_prompting(self) -> None:
        expected = {"status": "would_install", "dry_run": True}
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "memory.db")
            with NarratorDB(
                db_path=path,
                mode="intelligence",
                compiler=CompilerConfig.openai(model="gpt-5.4-mini"),
            ):
                pass
            with (
                patch("sys.stdin.isatty", return_value=False),
                patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
                patch(
                    "narratordb.cli.install_mcp_client", return_value=expected
                ) as install,
                patch("sys.stdout", new_callable=StringIO),
            ):
                code = main(
                    [
                        "--path",
                        path,
                        "mcp",
                        "install",
                        "codex",
                        "--dry-run",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(install.call_args.kwargs["mode"].value, "intelligence")
        self.assertIsNone(install.call_args.kwargs["compiler"])


if __name__ == "__main__":
    unittest.main()
