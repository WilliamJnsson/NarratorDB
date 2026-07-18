"""Production MCP runtime, schemas, and scope isolation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from mcp.types import CallToolResult

from narratordb.hooks import run_hook
from narratordb.intelligence import estimate_tokens
from narratordb.mcp_server import (
    MCPRuntime,
    MCPServerConfig,
    SERVER_INSTRUCTIONS,
    build_parser,
    create_server,
)
from narratordb.mcp_contract import (
    MCPRuntimeProtocol,
    MCP_TOOL_INPUT_SCHEMAS,
    MCP_TOOL_NAMES,
    bounded_int,
    bounded_text,
    create_server as create_contract_server,
)
from narratordb import CompilerConfig, ConfigurationRequiredError, NarratorDB
from narratordb.scopes import ProjectScope
from tests.test_enrichment import _SuccessfulCompiler


class MCPServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "memory.db"
        self.runtime = MCPRuntime(
            MCPServerConfig(
                db_path=str(self.path),
                user_id="user-zero",
                workspace_id="project/narratordb",
                init_mode="private",
                client="test",
            )
        )

    def tearDown(self) -> None:
        self.runtime.close()
        self.directory.cleanup()

    def test_public_extension_contract_preserves_server_factory_call_shape(self) -> None:
        self.assertIsInstance(self.runtime, MCPRuntimeProtocol)
        self.assertEqual(tuple(MCP_TOOL_INPUT_SCHEMAS), MCP_TOOL_NAMES)
        self.assertEqual(bounded_text(" value ", field="value", maximum=10), "value")
        self.assertEqual(bounded_int(2, field="value", minimum=1, maximum=3), 2)
        server = create_contract_server(self.runtime, include_bootstrap=False)
        self.assertEqual(server.instructions, SERVER_INSTRUCTIONS)

    def test_private_first_run_store_recall_status_and_forget(self) -> None:
        stored = self.runtime.remember(
            "The launch checklist requires the durability suite.",
            source="user",
        )
        self.assertTrue(stored["stored"])
        self.assertEqual(stored["mode"], "private")

        duplicate = self.runtime.remember(
            "The launch checklist requires the durability suite.",
            source="user",
        )
        self.assertTrue(duplicate["duplicate"])

        recalled = self.runtime.recall("What does the launch checklist require?")
        self.assertIn("durability suite", recalled["context"])
        self.assertEqual(recalled["workspace_id"], "project/narratordb")

        status = self.runtime.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["stats"]["message_count"], 1)
        self.assertTrue(status["health"]["ok"])

        with self.assertRaisesRegex(ValueError, "confirm=true"):
            self.runtime.forget(stored["message_id"])
        deleted = self.runtime.forget(stored["message_id"], confirm=True)
        self.assertTrue(deleted["deleted"])
        self.assertFalse(
            self.runtime.forget(stored["message_id"], confirm=True)["deleted"]
        )

    def test_server_bootstrap_preloads_private_context_without_hook_output(
        self,
    ) -> None:
        self.runtime.remember(
            "The current project decision is to use a cobalt release.",
            scope="project",
        )
        self.runtime.remember(
            "The user's favorite and dream car is Porsche.",
            scope="global",
        )

        server = create_server(self.runtime)

        self.assertIn("[Private local memory context]", server.instructions)
        self.assertIn("cobalt release", server.instructions)
        self.assertIn("favorite and dream car is Porsche", server.instructions)
        self.assertIn(
            "do not call recall or announce a memory check", server.instructions
        )
        self.assertNotIn("message:", server.instructions)
        self.assertNotIn("<memory>", server.instructions)

    def test_existing_intelligence_mode_keeps_the_same_write_and_receipt_contract(
        self,
    ) -> None:
        intelligence_path = Path(self.directory.name) / "intelligence.db"
        with NarratorDB(
            db_path=str(intelligence_path),
            user_id="user-zero",
            mode="intelligence",
            compiler=CompilerConfig.local(
                model="first-run-test",
                endpoint="http://127.0.0.1:11434/v1",
            ),
        ):
            pass
        runtime = MCPRuntime(
            MCPServerConfig(
                db_path=str(intelligence_path),
                user_id="user-zero",
                workspace_id="project/narratordb",
                # A creation default must not overwrite the persisted choice.
                init_mode="private",
                client="test",
            )
        )
        try:
            server = create_server(runtime)
            receipt = asyncio.run(
                server.call_tool(
                    "remember",
                    {
                        "content": "The Intelligence project uses a cobalt release.",
                        "scope": "project",
                        "source": "user",
                    },
                )
            )
            status = asyncio.run(server.call_tool("status", {}))

            self.assertEqual(
                receipt.content[0].text,
                "✓ Saved to NarratorDB project memory.",
            )
            self.assertTrue(receipt.structuredContent["stored"])
            self.assertEqual(receipt.structuredContent["mode"], "intelligence")
            self.assertIn("Intelligence mode", status.content[0].text)
            self.assertEqual(status.structuredContent["mode"], "intelligence")
            self.assertIn(
                "cobalt release",
                runtime.recall("What release does the project use?")["context"],
            )
        finally:
            runtime.close()

    def test_status_distinguishes_current_workspace_from_database_total(self) -> None:
        with NarratorDB(
            db_path=str(self.path), user_id="user-zero", mode="private"
        ) as memory:
            memory.remember(
                "A memory that belongs to a different project.",
                workspace_id="project/another",
            )
        with NarratorDB(db_path=str(self.path), user_id="another-user") as memory:
            memory.remember("A memory owned by another logical user.")

        status = self.runtime.status()

        self.assertEqual(status["memory_counts"]["current_workspace"], 0)
        self.assertEqual(status["memory_counts"]["current_user_total"], 1)
        self.assertEqual(status["memory_counts"]["database_total"], 2)
        self.assertEqual(
            status["memory_summary"],
            "Current workspace: 0 memories. Your total: 1 memory.",
        )
        self.assertNotIn("database empty", status["memory_summary"].lower())
        self.assertEqual(status["scope_origin"], "explicit")

    def test_home_fallback_blocks_only_unconfirmed_project_writes(self) -> None:
        guarded_path = Path(self.directory.name) / "guarded.db"
        guarded = MCPRuntime(
            MCPServerConfig(
                db_path=str(guarded_path),
                user_id="user-zero",
                workspace_id="project/home-fallback",
                scope_origin="path_fallback",
                scope_warning="PROJECT SCOPE WARNING: home fallback",
                project_root=str(Path.home()),
                init_mode="private",
                write_confirmation_required=True,
            )
        )
        try:
            with self.assertRaisesRegex(ValueError, "project write blocked"):
                guarded.remember("This must not be written to the home scope.")
            with self.assertRaisesRegex(ValueError, "project write blocked"):
                guarded.remember_session(
                    [{"role": "user", "content": "This is a durable user fact."}],
                    session_id="guarded-session",
                )
            with self.assertRaisesRegex(ValueError, "project write blocked"):
                guarded.forget(1, confirm=True)
            self.assertFalse(guarded_path.exists())

            visible = asyncio.run(
                create_server(guarded).call_tool(
                    "remember", {"content": "Do not write this either."}
                )
            )
            self.assertTrue(visible.isError)
            self.assertEqual(
                visible.structuredContent["error"], "project_scope_unconfirmed"
            )
            self.assertIn("intended project directory", visible.content[0].text)
            self.assertTrue(guarded_path.exists())
            self.assertEqual(guarded.status()["memory_counts"]["database_total"], 0)

            global_result = guarded.remember(
                "A cross-project preference remains allowed.", scope="global"
            )
            self.assertTrue(global_result["stored"])
            status = guarded.status()
            self.assertTrue(status["scope_diagnostics"]["project_writes_blocked"])
            self.assertIn("home fallback", status["scope_warning"])
            self.assertIn(
                "cd /path/to/your/project",
                status["scope_diagnostics"]["suggested_command"],
            )
        finally:
            guarded.close()

    def test_explicit_confirmation_allows_home_fallback_project_write(self) -> None:
        confirmed = MCPRuntime(
            MCPServerConfig(
                db_path=str(Path(self.directory.name) / "confirmed.db"),
                user_id="user-zero",
                workspace_id="project/home-fallback",
                init_mode="private",
                scope_origin="path_fallback",
                write_confirmation_required=True,
                allow_path_fallback_writes=True,
            )
        )
        try:
            self.assertTrue(confirmed.remember("Confirmed local project.")["stored"])
            self.assertFalse(
                confirmed.status()["scope_diagnostics"]["project_writes_blocked"]
            )
        finally:
            confirmed.close()

    def test_write_confirmation_flag_is_explicit(self) -> None:
        default = build_parser().parse_args([])
        confirmed = build_parser().parse_args(["--allow-path-fallback-writes"])

        self.assertIsNone(default.init_mode)
        self.assertFalse(default.allow_path_fallback_writes)
        self.assertTrue(confirmed.allow_path_fallback_writes)

    def test_new_database_requires_explicit_startup_mode(self) -> None:
        path = Path(self.directory.name) / "unselected.db"
        unselected = MCPRuntime(
            MCPServerConfig(
                db_path=str(path),
                user_id="user-zero",
                workspace_id="project/narratordb",
            )
        )
        try:
            with self.assertRaisesRegex(
                ConfigurationRequiredError, "choose --init-mode private"
            ):
                unselected.status()
            self.assertFalse(path.exists())
        finally:
            unselected.close()

    def test_project_and_global_scopes_are_isolated_then_combined(self) -> None:
        self.runtime.remember("Project uses SQLite WAL mode.", scope="project")
        self.runtime.remember(
            "User prefers concise answers across projects.", scope="global"
        )

        project_only = self.runtime.recall("What do I prefer?", include_global=False)
        self.assertNotIn("concise answers", project_only["context"])

        combined = self.runtime.recall("What do I prefer?")
        self.assertIn("concise answers", combined["context"])
        self.assertEqual(combined["scope"], "project+global")
        self.assertTrue(combined["global_included"])

        global_only = self.runtime.recall("SQLite", scope="global")
        self.assertNotIn("SQLite WAL", global_only["context"])

    def test_session_ingest_is_bounded_and_idempotent(self) -> None:
        first = self.runtime.remember_session(
            [
                {"role": "user", "content": "We selected acid chartreuse."},
                {"role": "assistant", "content": "I recorded that design decision."},
            ],
            session_id="design-1",
        )
        self.assertEqual(first["messages_stored"], 2)
        repeated = self.runtime.remember_session(
            [
                {"role": "user", "content": "We selected acid chartreuse."},
                {"role": "assistant", "content": "I recorded that design decision."},
            ],
            session_id="design-1",
        )
        self.assertEqual(repeated["messages_stored"], 0)

        with self.assertRaisesRegex(ValueError, "non-empty array"):
            self.runtime.remember_session([], session_id="empty")

    def test_configure_exposes_capture_and_dual_mode_without_credentials(self) -> None:
        capture = self.runtime.configure(capture_policy="manual")
        self.assertEqual(capture["capture_policy"], "manual")
        self.assertEqual(capture["mode"], "private")

        intelligence = self.runtime.configure(
            mode="intelligence",
            compiler="local",
            model="mcp-config-test",
            endpoint="http://127.0.0.1:11434/v1",
            capture_policy="sessions",
        )
        self.assertEqual(intelligence["mode"], "intelligence")
        self.assertEqual(intelligence["capture_policy"], "sessions")
        self.assertTrue(intelligence["background_enrichment"]["running"])
        self.assertNotIn("credential", str(intelligence).lower())

        private = self.runtime.configure(mode="private", derived_data="retain")
        self.assertEqual(private["mode"], "private")
        self.assertEqual(private["capture_policy"], "sessions")
        self.assertFalse(private["background_enrichment"]["running"])

    def test_hook_queue_returns_before_background_intelligence_compilation(
        self,
    ) -> None:
        intelligence_path = Path(self.directory.name) / "background.db"
        compiler = CompilerConfig.local(
            model="background-test",
            endpoint="http://127.0.0.1:11434/v1",
        )
        with NarratorDB(
            db_path=str(intelligence_path),
            user_id="user-zero",
            mode="intelligence",
            compiler=compiler,
            capture_policy="sessions",
        ):
            pass
        runtime = MCPRuntime(
            MCPServerConfig(
                db_path=str(intelligence_path),
                user_id="user-zero",
                workspace_id="project/narratordb",
                client="test",
            )
        )
        try:
            memory = runtime._open()
            memory._compiler_runtime = _SuccessfulCompiler()
            self.assertTrue(runtime.start_background_enrichment())

            transcript = Path(self.directory.name) / "background.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "The project codename is firefly.",
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            scope = ProjectScope(
                workspace_id="project/narratordb",
                origin="explicit",
                project_root=self.directory.name,
                in_git_repo=True,
            )
            started = time.monotonic()
            with patch("narratordb.hooks.resolve_project_scope", return_value=scope):
                run_hook(
                    "Stop",
                    {
                        "session_id": "background-session",
                        "cwd": self.directory.name,
                        "transcript_path": str(transcript),
                    },
                    path=str(intelligence_path),
                    user_id="user-zero",
                )
            self.assertLess(time.monotonic() - started, 0.5)

            deadline = time.monotonic() + 5.0
            jobs = {}
            while time.monotonic() < deadline:
                jobs = runtime.status()["project"]["enrichment"]["jobs"]
                if jobs.get("complete", 0) + jobs.get("partial", 0) == 1:
                    break
                time.sleep(0.05)
            self.assertEqual(jobs.get("complete", 0) + jobs.get("partial", 0), 1)
            self.assertEqual(jobs.get("pending", 0), 0)
        finally:
            runtime.close()

    def test_server_has_small_safe_annotated_tool_surface(self) -> None:
        server = create_server(self.runtime)
        tools = asyncio.run(server.list_tools())
        by_name = {tool.name: tool for tool in tools}
        self.assertEqual(
            set(by_name),
            {
                "configure",
                "remember",
                "remember_session",
                "recall",
                "resume",
                "forget",
                "status",
            },
        )
        self.assertFalse(by_name["configure"].annotations.readOnlyHint)
        self.assertTrue(by_name["recall"].annotations.readOnlyHint)
        self.assertTrue(by_name["status"].annotations.readOnlyHint)
        self.assertTrue(by_name["forget"].annotations.destructiveHint)
        self.assertFalse(by_name["remember"].annotations.openWorldHint)
        self.assertEqual(by_name["remember"].title, "Save to NarratorDB")
        self.assertEqual(
            by_name["remember"].inputSchema["properties"]["scope"]["enum"],
            ["project", "global"],
        )
        self.assertEqual(
            by_name["remember"].inputSchema["properties"]["source"]["enum"],
            ["user", "assistant", "system", "memory"],
        )
        self.assertIn("Store concise facts", SERVER_INSTRUCTIONS)

    def test_visible_write_receipt_is_clean_and_keeps_structured_metadata(self) -> None:
        server = create_server(self.runtime)
        receipt = asyncio.run(
            server.call_tool(
                "remember",
                {"content": "Use two-space indentation.", "scope": "project"},
            )
        )

        self.assertIsInstance(receipt, CallToolResult)
        self.assertEqual(
            receipt.content[0].text,
            "✓ Saved to NarratorDB project memory.",
        )
        self.assertTrue(receipt.structuredContent["stored"])
        self.assertEqual(
            receipt.structuredContent["workspace_id"], "project/narratordb"
        )

    def test_visible_read_receipts_avoid_duplicated_json(self) -> None:
        self.runtime.remember("Friday coffee happens in the park at four.")
        server = create_server(self.runtime)

        recalled = asyncio.run(
            server.call_tool("recall", {"query": "Friday coffee at four"})
        )
        status = asyncio.run(server.call_tool("status", {}))

        self.assertIsInstance(recalled, CallToolResult)
        self.assertTrue(recalled.content[0].text.startswith("✓ Recalled"))
        self.assertIn("Friday coffee", recalled.content[0].text)
        self.assertNotIn('"structuredContent"', recalled.content[0].text)
        self.assertIn("Friday coffee", recalled.structuredContent["context"])
        self.assertIsInstance(status, CallToolResult)
        self.assertTrue(status.content[0].text.startswith("✓ NarratorDB ready"))
        self.assertNotIn('"project":', status.content[0].text)
        self.assertTrue(status.structuredContent["ready"])

    def test_invalid_scope_source_and_bounds_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "scope"):
            self.runtime.remember("content", scope="another-user")
        with self.assertRaisesRegex(ValueError, "source"):
            self.runtime.remember("content", source="tool")
        with self.assertRaisesRegex(ValueError, "token_budget"):
            self.runtime.recall("query", token_budget=1)

    def test_empty_recall_returns_no_prompt_scaffolding(self) -> None:
        recalled = self.runtime.recall("There is no matching memory yet.")
        self.assertEqual(recalled["context"], "")
        self.assertEqual(recalled["token_count"], 0)
        self.assertEqual(recalled["project"]["blocks"], [])
        self.assertEqual(recalled["global"]["blocks"], [])

    def test_combined_scope_never_exceeds_the_declared_token_budget(self) -> None:
        repeated = " ".join(["cobalt launch protocol"] * 80)
        self.runtime.remember(repeated + " project detail", scope="project")
        self.runtime.remember(repeated + " global preference", scope="global")

        recalled = self.runtime.recall(
            "cobalt launch protocol", token_budget=128, include_global=True
        )

        self.assertLessEqual(recalled["token_count"], 128)
        self.assertEqual(recalled["token_count"], estimate_tokens(recalled["context"]))
        self.assertLessEqual(
            recalled["project"]["token_count"] + recalled["global"]["token_count"],
            128,
        )


if __name__ == "__main__":
    unittest.main()
