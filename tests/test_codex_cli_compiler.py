import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from narratordb.compiler import (
    CODEX_CLI_POLICY_VERSION,
    CodexCliCompiler,
    CodexProcessResult,
    CompileSessionInput,
    CompilerConfigurationError,
    CompilerError,
    SourceMessage,
    compiled_memory_json_schema,
    compiler_from_project_config,
)
from narratordb.config import CompilerConfig, CompilerKind
from narratordb.compiler_cache import compiler_usage_from_dict, compiler_usage_to_dict


CLI_VERSION = "codex-cli 0.144.4"


def session_input() -> CompileSessionInput:
    return CompileSessionInput(
        session_id="session-1",
        document_time="2025-01-02T00:00:00Z",
        messages=(
            SourceMessage(
                message_id="message-1",
                role="user",
                content="I moved to Kyoto on 2025-01-02.",
                occurred_at="2025-01-02T00:00:00Z",
            ),
        ),
    )


def compiled_payload() -> dict:
    quote = "I moved to Kyoto on 2025-01-02."
    evidence = {
        "message_id": "message-1",
        "quote": quote,
        "start": None,
        "end": None,
    }
    return {
        "summary": {"text": "The user moved to Kyoto.", "evidence": [evidence]},
        "claims": [
            {
                "claim_id": "claim-1",
                "kind": "event",
                "text": "The user moved to Kyoto on 2025-01-02.",
                "subject": "the user",
                "predicate": "moved to",
                "object_text": "Kyoto",
                "memory_key": "user.residence.current_city",
                "confidence": 1.0,
                "status": "active",
                "document_time": "2025-01-02T00:00:00Z",
                "event_start": "2025-01-02T00:00:00Z",
                "event_end": None,
                "valid_from": "2025-01-02T00:00:00Z",
                "valid_to": None,
                "entity_ids": [],
                "evidence": [evidence],
            }
        ],
        "entities": [],
        "relations": [],
    }


class FakeCodexRunner:
    def __init__(
        self,
        turns=(),
        *,
        version: str = CLI_VERSION,
        login_status: str = "Logged in using ChatGPT",
    ) -> None:
        self.turns = list(turns)
        self.version = version
        self.login_status = login_status
        self.calls = []
        self.schemas = []

    def __call__(self, argv, stdin_text, environment, cwd, timeout_seconds):
        call = {
            "argv": tuple(argv),
            "stdin": stdin_text,
            "environment": dict(environment),
            "cwd": cwd,
            "cwd_exists": Path(cwd).is_dir(),
            "timeout": timeout_seconds,
        }
        self.calls.append(call)
        if tuple(argv[-1:]) == ("--version",):
            return CodexProcessResult(0, self.version + "\n", "")
        if "login" in argv and "status" in argv:
            return CodexProcessResult(0, self.login_status + "\n", "")

        turn = self.turns.pop(0) if self.turns else {"output": compiled_payload()}
        if "raise" in turn:
            raise turn["raise"]
        returncode = int(turn.get("returncode", 0))
        if returncode:
            return CodexProcessResult(
                returncode,
                str(turn.get("stdout", "")),
                str(turn.get("stderr", "")),
            )

        schema_path = Path(argv[argv.index("--output-schema") + 1])
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        self.schemas.append(json.loads(schema_path.read_text(encoding="utf-8")))
        output = turn.get("output", compiled_payload())
        output_text = output if isinstance(output, str) else json.dumps(output)
        file_output = turn.get("file_output", output_text)
        if not isinstance(file_output, str):
            file_output = json.dumps(file_output)
        output_path.write_text(file_output, encoding="utf-8")

        item_type = str(turn.get("item_type", "agent_message"))
        item = {"id": "item-1", "type": item_type, "text": output_text}
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": item},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 20,
                    "output_tokens": 40,
                },
            },
        ]
        return CodexProcessResult(
            0,
            "\n".join(json.dumps(event) for event in events) + "\n",
            str(turn.get("stderr", "")),
        )


class CodexCliCompilerTests(unittest.TestCase):
    def make_compiler(self, runner, **overrides) -> CodexCliCompiler:
        config = CompilerConfig.codex_cli(
            cli_version=CLI_VERSION,
            semantic_max_attempts=overrides.pop("semantic_max_attempts", 1),
            **overrides,
        )
        compiler = compiler_from_project_config(
            config,
            codex_executable="/mock/codex",
            codex_process_runner=runner,
        )
        self.assertIsInstance(compiler, CodexCliCompiler)
        return compiler

    def test_config_round_trip_is_credential_free_and_pins_runtime_policy(self):
        configured = CompilerConfig.codex_cli(
            cli_version=CLI_VERSION,
            max_invocations=100,
            max_concurrency=2,
            timeout_seconds=45,
        )
        restored = CompilerConfig.from_dict(configured.to_dict())

        self.assertEqual(configured, restored)
        self.assertIs(configured.kind, CompilerKind.CODEX_CLI)
        self.assertEqual(configured.model, "gpt-5.4-mini")
        self.assertEqual(configured.reasoning, "low")
        self.assertIsNone(configured.seed)
        serialized = json.dumps(configured.to_dict()).casefold()
        for forbidden in ("api_key", "authorization", "password", "secret"):
            self.assertNotIn(forbidden, serialized)

    def test_success_uses_isolated_argv_stdin_schema_and_subscription_usage(self):
        runner = FakeCodexRunner([{"output": compiled_payload()}])
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "must-not-reach-child",
                "OPENROUTER_API_KEY": "must-not-reach-child",
                "ANOTHER_API_KEY": "must-not-reach-child",
                "CODEX_ACCESS_TOKEN": "must-not-reach-child",
            },
        ):
            compiler = self.make_compiler(runner)
            result = compiler.compile_session(session_input())

        invocation = runner.calls[2]
        argv = invocation["argv"]
        self.assertEqual(argv[0], "/mock/codex")
        self.assertEqual(argv[1:4], ("--ask-for-approval", "never", "exec"))
        self.assertIn("exec", argv)
        self.assertIn("--ephemeral", argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)
        self.assertIn("--output-schema", argv)
        self.assertIn("--output-last-message", argv)
        self.assertIn("read-only", argv)
        self.assertEqual(argv[-1], "-")
        reasoning_override = argv[argv.index("-c") + 1]
        self.assertEqual(reasoning_override, 'model_reasoning_effort="low"')
        self.assertFalse(any("I moved to Kyoto" in value for value in argv))
        self.assertIn("I moved to Kyoto", invocation["stdin"])
        self.assertTrue(invocation["cwd_exists"])
        for hardening_flag in (
            "--strict-config",
            "--sandbox",
            "--skip-git-repo-check",
            "--disable",
        ):
            self.assertIn(hardening_flag, argv)
        for disabled_feature in (
            "memories",
            "multi_agent",
            "shell_tool",
            "unified_exec",
            "apps",
            "browser_use",
            "computer_use",
            "image_generation",
        ):
            self.assertIn(disabled_feature, argv)
        self.assertEqual(runner.schemas, [compiled_memory_json_schema()])
        for forbidden in (
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "ANOTHER_API_KEY",
            "CODEX_ACCESS_TOKEN",
        ):
            self.assertNotIn(forbidden, invocation["environment"])

        self.assertEqual(result.memory.claims[0].object_text, "Kyoto")
        self.assertEqual(result.memory.claims[0].evidence[0].start, 0)
        self.assertEqual(result.usage[0].provider, "openai-chatgpt")
        self.assertEqual(result.usage[0].cost_source, "subscription")
        self.assertEqual(result.usage[0].cost_usd, 0.0)
        self.assertFalse(result.usage[0].unknown_cost)
        self.assertEqual(result.usage[0].prompt_tokens, 120)
        self.assertEqual(result.usage[0].cached_tokens, 20)
        self.assertEqual(result.usage[0].completion_tokens, 40)
        self.assertEqual(
            compiler_usage_from_dict(compiler_usage_to_dict(result.usage[0])),
            result.usage[0],
        )
        self.assertEqual(compiler.invocation_count, 1)
        self.assertIn(CODEX_CLI_POLICY_VERSION, "isolated-structured-exec.v1")
        self.assertNotIn("must-not-reach-child", compiler.fingerprint)

    def test_preflight_requires_chatgpt_login_and_exact_version(self):
        api_login = FakeCodexRunner(login_status="Logged in using an API key")
        with self.assertRaises(CompilerConfigurationError) as missing_login:
            self.make_compiler(api_login)
        self.assertEqual(missing_login.exception.code, "missing_chatgpt_login")

        wrong_version = FakeCodexRunner(version="codex-cli 0.145.0")
        with self.assertRaises(CompilerConfigurationError) as mismatch:
            self.make_compiler(wrong_version)
        self.assertEqual(mismatch.exception.code, "codex_version_mismatch")

    def test_forbidden_tool_event_is_rejected_without_retry(self):
        runner = FakeCodexRunner(
            [{"output": compiled_payload(), "item_type": "command_execution"}]
        )
        compiler = self.make_compiler(runner, semantic_max_attempts=2)

        with self.assertRaises(CompilerError) as raised:
            compiler.compile_session(session_input())

        self.assertEqual(raised.exception.code, "codex_forbidden_event")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(compiler.invocation_count, 1)

    def test_output_mismatch_fails_closed(self):
        mismatch_runner = FakeCodexRunner(
            [
                {
                    "output": compiled_payload(),
                    "file_output": {
                        "summary": {"text": "", "evidence": []},
                        "claims": [],
                        "entities": [],
                        "relations": [],
                    },
                }
            ]
        )
        compiler = self.make_compiler(mismatch_runner)
        with self.assertRaises(CompilerError) as mismatch:
            compiler.compile_session(session_input())
        self.assertEqual(mismatch.exception.code, "codex_output_mismatch")

    def test_timeout_invocation_fuse_and_subscription_circuit(self):
        timeout_runner = FakeCodexRunner(
            [{"raise": subprocess.TimeoutExpired(cmd="codex", timeout=1)}]
        )
        timeout_compiler = self.make_compiler(timeout_runner)
        with self.assertRaises(CompilerError) as timeout:
            timeout_compiler.compile_session(session_input())
        self.assertEqual(timeout.exception.code, "codex_timeout")

        fuse_runner = FakeCodexRunner([{"output": compiled_payload()}])
        fuse_compiler = self.make_compiler(fuse_runner, max_invocations=1)
        fuse_compiler.compile_session(session_input())
        with self.assertRaises(CompilerError) as exhausted:
            fuse_compiler.compile_session(session_input())
        self.assertEqual(exhausted.exception.code, "invocation_limit_reached")
        self.assertEqual(fuse_compiler.invocation_count, 1)

        quota_runner = FakeCodexRunner(
            [{"returncode": 1, "stderr": "subscription usage limit reached"}]
        )
        quota_compiler = self.make_compiler(quota_runner)
        with self.assertRaises(CompilerError) as quota:
            quota_compiler.compile_session(session_input())
        self.assertEqual(quota.exception.code, "codex_subscription_limited")
        with self.assertRaises(CompilerError) as circuit:
            quota_compiler.compile_session(session_input())
        self.assertEqual(circuit.exception.code, "codex_subscription_limited")
        self.assertEqual(quota_compiler.invocation_count, 1)


if __name__ == "__main__":
    unittest.main()
