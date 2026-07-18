from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = PLUGIN_ROOT / "scripts" / "run-hook.sh"
SOURCE = (
    "narratordb-memory[mcp] @ "
    "git+https://github.com/WilliamJnsson/NarratorDB.git@5cda4adbc5c72bec06fa5a63a81bae42369007ec"
)


class PluginContractTests(unittest.TestCase):
    def test_mcp_command_is_private_local_stdio(self) -> None:
        self.assertRegex(SOURCE, r"@[0-9a-f]{40}$")
        self.assertNotIn("@v", SOURCE)
        payload = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())
        server = payload["mcpServers"]["narratordb"]
        self.assertEqual(server["command"], "uvx")
        self.assertEqual(
            server["args"],
            [
                "--from",
                SOURCE,
                "narratordb-mcp",
                "--init-mode",
                "private",
                "--client",
                "codex-plugin",
            ],
        )
        self.assertEqual(server["env"]["NARRATORDB_LOCAL_ONLY"], "1")
        self.assertEqual(server["env"]["NARRATORDB_TELEMETRY"], "0")

    def test_hooks_cover_the_codex_lifecycle_contract(self) -> None:
        payload = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
        self.assertEqual(
            set(payload["hooks"]),
            {"UserPromptSubmit", "PreCompact", "Stop"},
        )
        for event, registrations in payload["hooks"].items():
            hook = registrations[0]["hooks"][0]
            self.assertIn("scripts/run-hook.sh", hook["command"])
            self.assertTrue(hook["command"].startswith("/bin/sh "))
            self.assertTrue(hook["command"].endswith(event))
            self.assertLessEqual(hook["timeout"], 10)
            self.assertIn("NarratorDB", hook["statusMessage"])

    def test_wrapper_forwards_valid_event_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_uvx = temp / "uvx"
            fake_uvx.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" > \"$TMPDIR/args\"\n"
                "printf '%s' \"${OPENAI_API_KEY-unset}\" > \"$TMPDIR/secret\"\n"
                "printf '%s' \"${UV_OFFLINE-unset}\" > \"$TMPDIR/offline\"\n"
                "printf '%s' \"${NARRATORDB_AUTO_CAPTURE-unset}\" > \"$TMPDIR/auto_capture\"\n"
                "printf '%s' \"${NARRATORDB_ALLOW_PATH_FALLBACK_WRITES-unset}\" > \"$TMPDIR/fallback_writes\"\n"
                "cat > \"$TMPDIR/input\"\n"
                "printf 'local context'\n"
            )
            fake_uvx.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{temp}{os.pathsep}{env.get('PATH', '')}",
                    "TMPDIR": str(temp),
                    "OPENAI_API_KEY": "must-not-be-inherited",
                    "NARRATORDB_AUTO_CAPTURE": "false",
                    "NARRATORDB_ALLOW_PATH_FALLBACK_WRITES": "true",
                }
            )
            completed = subprocess.run(
                ["sh", str(WRAPPER), "SessionStart"],
                input="hook payload",
                text=True,
                capture_output=True,
                env=env,
                timeout=12,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "local context")
            self.assertEqual((temp / "secret").read_text(), "unset")
            self.assertEqual((temp / "offline").read_text(), "1")
            self.assertEqual((temp / "auto_capture").read_text(), "false")
            self.assertEqual((temp / "fallback_writes").read_text(), "true")
            self.assertEqual((temp / "input").read_text(), "hook payload")
            self.assertEqual(
                (temp / "args").read_text().splitlines(),
                ["--from", SOURCE, "narratordb-hook", "SessionStart"],
            )

    def test_wrapper_finds_uvx_with_gui_style_minimal_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = temp / "home"
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            fake_uvx = bin_dir / "uvx"
            fake_uvx.write_text(
                "#!/bin/sh\n"
                "printf '%s' \"${UV_OFFLINE-unset}\" > \"$TMPDIR/offline\"\n"
                "cat >/dev/null\n"
                "printf 'gui context'\n"
            )
            fake_uvx.chmod(0o755)
            env = {
                "HOME": str(home),
                "PATH": "/usr/bin:/bin",
                "TMPDIR": str(temp),
            }
            completed = subprocess.run(
                ["/bin/sh", str(WRAPPER), "UserPromptSubmit"],
                input="hook payload",
                text=True,
                capture_output=True,
                env=env,
                timeout=12,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "gui context")
            self.assertEqual((temp / "offline").read_text(), "1")

    def test_wrapper_rejects_unknown_event_and_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_uvx = temp / "uvx"
            fake_uvx.write_text("#!/bin/sh\nexit 9\n")
            fake_uvx.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env.get('PATH', '')}"
            env["TMPDIR"] = str(temp)

            invalid = subprocess.run(
                ["sh", str(WRAPPER), "UnknownEvent"],
                text=True,
                capture_output=True,
                env=env,
                timeout=2,
                check=False,
            )
            failed = subprocess.run(
                ["sh", str(WRAPPER), "Stop"],
                text=True,
                capture_output=True,
                env=env,
                timeout=2,
                check=False,
            )
            self.assertEqual(invalid.returncode, 0)
            self.assertEqual(failed.returncode, 0)
            self.assertEqual(failed.stdout, "")

    def test_wrapper_has_fixed_resource_bounds(self) -> None:
        contents = WRAPPER.read_text()
        self.assertIn("HOOK_TIMEOUT_SECONDS = 8.0", contents)
        self.assertIn("MAX_INPUT_BYTES = 1024 * 1024", contents)
        self.assertIn("MAX_OUTPUT_BYTES = 64 * 1024", contents)
        self.assertIn("start_new_session=True", contents)

    def test_remember_skill_pins_valid_source_and_clean_result(self) -> None:
        contents = (PLUGIN_ROOT / "skills" / "remember" / "SKILL.md").read_text()
        self.assertIn("Call `remember` exactly once", contents)
        self.assertIn("`source`: `user`", contents)
        self.assertIn(
            "Its only valid values are `user`, `assistant`,\n`system`, and `memory`",
            contents,
        )
        self.assertIn("Do not print or restate raw JSON", contents)
        self.assertIn("Remembered for this project:", contents)

    def test_onboard_skill_uses_one_read_only_status_call(self) -> None:
        contents = (PLUGIN_ROOT / "skills" / "onboard" / "SKILL.md").read_text()
        self.assertIn("Call `status` exactly once", contents)
        self.assertIn('`scope="project"` and `full_check=false`', contents)
        self.assertIn("Do not echo raw JSON", contents)
        self.assertIn("Do not call `resume`, `recall`, or `remember`", contents)
        self.assertIn("Codex controls native status/spinner animation", contents)

    def test_health_skill_separates_database_health_from_scope_safety(self) -> None:
        contents = (PLUGIN_ROOT / "skills" / "health" / "SKILL.md").read_text()
        self.assertIn("Call `status` exactly once", contents)
        self.assertIn('`scope="project"` and `full_check=false`', contents)
        self.assertIn("Never echo its raw JSON", contents)
        self.assertIn("`scope_diagnostics.project_writes_blocked`", contents)
        self.assertIn("`scope_diagnostics.warning`", contents)
        self.assertIn("`memory_counts.current_workspace`", contents)
        self.assertIn("`memory_counts.current_user_total`", contents)
        self.assertIn("database is healthy, but project scope is unsafe", contents)
        self.assertIn("Restart Codex from the intended project folder", contents)
        self.assertIn(
            "Never call project memory healthy or ready while that block",
            contents,
        )
        self.assertIn(
            "Do not call `recall`, `resume`, `remember`, `remember_session`, or `forget`",
            contents,
        )
        self.assertIn("Codex controls native status/spinner animation", contents)


if __name__ == "__main__":
    unittest.main()
