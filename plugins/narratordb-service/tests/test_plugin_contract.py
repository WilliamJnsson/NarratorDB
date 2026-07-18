from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = PLUGIN_ROOT / "scripts" / "run-hook.sh"


class ServicePluginContractTests(unittest.TestCase):
    def test_plugin_has_boundary_hooks_but_no_submit_hook(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text()
        )
        self.assertEqual(manifest["name"], "narratordb-service")
        payload = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
        self.assertEqual(set(payload["hooks"]), {"PreCompact", "Stop"})
        self.assertNotIn("UserPromptSubmit", payload["hooks"])

    def test_wrapper_uses_private_pointer_and_strips_provider_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_python = temp / "service-python"
            fake_python.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" > \"$TMPDIR/args\"\n"
                "printf '%s' \"${OPENAI_API_KEY-unset}\" > \"$TMPDIR/secret\"\n"
                "cat > \"$TMPDIR/input\"\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            credentials = temp / "credentials.env"
            credentials.write_text("not-read-by-launcher\n", encoding="utf-8")
            credentials.chmod(0o600)
            config = temp / "service-hook.json"
            config.write_text(
                json.dumps(
                    {
                        "credentials_file": str(credentials),
                        "python": str(fake_python),
                    }
                ),
                encoding="utf-8",
            )
            config.chmod(0o600)
            env = os.environ.copy()
            env.update(
                {
                    "NARRATORDB_SERVICE_HOOK_CONFIG": str(config),
                    "OPENAI_API_KEY": "must-not-be-inherited",
                    "TMPDIR": str(temp),
                }
            )
            completed = subprocess.run(
                ["/bin/sh", str(WRAPPER), "Stop"],
                input='{"transcript_path":"example"}',
                text=True,
                capture_output=True,
                env=env,
                timeout=12,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "")
            self.assertEqual((temp / "secret").read_text(), "unset")
            self.assertEqual(
                (temp / "args").read_text().splitlines(),
                [
                    "-m",
                    "narratordb.service_hook",
                    "Stop",
                    "--credentials-file",
                    str(credentials),
                ],
            )
            self.assertEqual(
                (temp / "input").read_text(),
                '{"transcript_path":"example"}',
            )

    def test_wrapper_rejects_submit_and_has_fixed_bounds(self) -> None:
        submit = subprocess.run(
            ["/bin/sh", str(WRAPPER), "UserPromptSubmit"],
            input="ignored",
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
        self.assertEqual(submit.returncode, 0)
        self.assertEqual(submit.stdout, "")
        contents = WRAPPER.read_text()
        self.assertIn("MAX_INPUT_BYTES = 1024 * 1024", contents)
        self.assertIn("HOOK_TIMEOUT_SECONDS = 8.0", contents)
        self.assertIn("start_new_session=True", contents)


if __name__ == "__main__":
    unittest.main()
