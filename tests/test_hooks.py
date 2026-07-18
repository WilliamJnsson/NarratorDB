"""Local lifecycle capture and retrieval safety."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from narratordb import CompilerConfig, NarratorDB
from narratordb.config import ProjectConfigStore
from narratordb.hooks import (
    MAX_CAPTURE_CHARS,
    MAX_CAPTURE_MESSAGES,
    extract_conversation,
    extract_turn,
    redact_secrets,
    run_hook,
)
from narratordb.scopes import ProjectScope


def _codex_message(role: str, text: str, *, phase: str | None = None) -> str:
    payload = {
        "type": "message",
        "role": role,
        "content": [
            {
                "type": "input_text" if role == "user" else "output_text",
                "text": text,
            }
        ],
    }
    if phase is not None:
        payload["phase"] = phase
    return json.dumps({"type": "response_item", "payload": payload})


def _project_scope() -> ProjectScope:
    return ProjectScope(
        workspace_id="project/test",
        origin="explicit",
        project_root="/test/project",
        in_git_repo=True,
    )


class HookTests(unittest.TestCase):
    def test_extracts_only_user_and_final_answer_and_redacts_keys(self) -> None:
        user, assistant = extract_turn(
            [
                _codex_message("user", "Please deploy with api_key=super-secret-value"),
                _codex_message("assistant", "hidden progress", phase="commentary"),
                json.dumps(
                    {
                        "type": "function_call_output",
                        "payload": {"output": "password=tool-secret"},
                    }
                ),
                _codex_message(
                    "assistant",
                    "Deployment passed; bearer abcdefghijklmnopqrstuvwxyz",
                    phase="final_answer",
                ),
            ]
        )
        self.assertIn("[REDACTED]", user)
        self.assertNotIn("super-secret", user)
        self.assertIn("[REDACTED]", assistant)
        self.assertNotIn("hidden progress", assistant)
        self.assertNotIn("tool-secret", assistant)

    def test_conversation_window_keeps_recent_final_turns_only(self) -> None:
        lines = []
        for index in range(40):
            lines.extend(
                [
                    _codex_message(
                        "user",
                        f"User request number {index} has durable project context.",
                    ),
                    _codex_message(
                        "assistant",
                        f"Commentary for request {index} must stay out of memory.",
                        phase="commentary",
                    ),
                    json.dumps(
                        {
                            "type": "function_call_output",
                            "payload": {"output": f"tool output {index}"},
                        }
                    ),
                    _codex_message(
                        "assistant",
                        f"Final answer number {index} records the durable outcome clearly.",
                        phase="final_answer",
                    ),
                ]
            )

        messages = extract_conversation(lines)

        self.assertEqual(len(messages), MAX_CAPTURE_MESSAGES)
        self.assertLessEqual(
            sum(len(message["content"]) for message in messages), MAX_CAPTURE_CHARS
        )
        self.assertIn("request number 24", messages[0]["content"])
        self.assertIn("Final answer number 39", messages[-1]["content"])
        rendered = "\n".join(message["content"] for message in messages)
        self.assertNotIn("Commentary", rendered)
        self.assertNotIn("tool output", rendered)

    def test_claude_transcript_shape_is_supported(self) -> None:
        user, assistant = extract_turn(
            [
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [{"type": "text", "text": "Remember Tokyo."}]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": "Tokyo is recorded."}]
                        },
                    }
                ),
            ]
        )
        self.assertEqual(user, "Remember Tokyo.")
        self.assertEqual(assistant, "Tokyo is recorded.")

        conversation = extract_conversation(
            [
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {"type": "text", "text": "Inspect the deployment."}
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "I will inspect it now."},
                                {"type": "tool_use", "name": "shell"},
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "content": "private tool output",
                                }
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "The deployment inspection passed.",
                                }
                            ]
                        },
                    }
                ),
            ]
        )
        self.assertEqual(
            conversation,
            [
                {"role": "user", "content": "Inspect the deployment."},
                {
                    "role": "assistant",
                    "content": "The deployment inspection passed.",
                },
            ],
        )

    def test_stop_capture_and_session_start_retrieval_are_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "memory.db"
            transcript = root / "transcript.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        _codex_message(
                            "user",
                            "We decided that the release codename is cobalt-seven.",
                        ),
                        _codex_message(
                            "assistant",
                            "The release codename is cobalt-seven and the decision is complete.",
                            phase="final_answer",
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            event = {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "cwd": str(root),
                "transcript_path": str(transcript),
            }
            with NarratorDB(
                db_path=str(path),
                user_id="user-zero",
                mode="private",
                capture_policy="sessions",
            ):
                pass
            with patch(
                "narratordb.hooks.resolve_project_scope", return_value=_project_scope()
            ):
                run_hook("Stop", event, path=str(path), user_id="user-zero")
                run_hook(
                    "PreCompact",
                    {**event, "turn_id": "turn-2"},
                    path=str(path),
                    user_id="user-zero",
                )

            with NarratorDB(db_path=str(path), user_id="user-zero") as memory:
                result = memory.recall(
                    "What is the release codename?", workspace_id="project/test"
                )
                self.assertIn("cobalt-seven", result.text)
                self.assertEqual(
                    memory.stats(workspace_id="project/test")["message_count"], 2
                )

            with sqlite3.connect(path) as connection:
                sessions = connection.execute(
                    "SELECT external_id FROM memory_sessions"
                ).fetchall()
                jobs = connection.execute(
                    "SELECT status FROM memory_compiler_jobs"
                ).fetchall()
            self.assertEqual(sessions, [("agent/session-1",)])
            self.assertEqual(jobs, [])

            with NarratorDB(db_path=str(path), user_id="user-zero") as memory:
                self.assertIn(
                    "cobalt-seven",
                    memory.recall("release codename", workspace_id="project/test").text,
                )

    def test_home_fallback_blocks_project_capture_but_allows_preference_save(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "memory.db"
            transcript = root / "transcript.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        _codex_message(
                            "user", "Remember this durable home fallback decision."
                        ),
                        _codex_message(
                            "assistant",
                            "The durable home fallback decision has been recorded.",
                            phase="final_answer",
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            event = {
                "session_id": "home-fallback",
                "cwd": str(Path.home()),
                "transcript_path": str(transcript),
            }
            fallback = ProjectScope(
                workspace_id="project/home-fallback",
                origin="path_fallback",
                project_root=str(Path.home()),
                in_git_repo=False,
                warning="PROJECT SCOPE WARNING: home fallback",
                write_confirmation_required=True,
            )
            with (
                patch("narratordb.hooks.resolve_project_scope", return_value=fallback),
                patch.dict(os.environ, {}, clear=True),
            ):
                run_hook("Stop", event, path=str(path), user_id="user-zero")
            self.assertFalse(path.exists())

            with NarratorDB(
                db_path=str(path),
                user_id="user-zero",
                mode="private",
                capture_policy="sessions",
            ):
                pass

            with (
                patch("narratordb.hooks.resolve_project_scope", return_value=fallback),
                patch.dict(
                    os.environ,
                    {"NARRATORDB_ALLOW_PATH_FALLBACK_WRITES": "true"},
                    clear=True,
                ),
            ):
                run_hook("Stop", event, path=str(path), user_id="user-zero")

            with NarratorDB(db_path=str(path), user_id="user-zero") as memory:
                self.assertEqual(
                    memory.stats(workspace_id="project/home-fallback")["message_count"],
                    2,
                )
                memory.remember(
                    "The user drinks coffee in the park at four on Fridays.",
                    workspace_id=None,
                )

            with (
                patch("narratordb.hooks.resolve_project_scope", return_value=fallback),
                patch.dict(os.environ, {}, clear=True),
            ):
                run_hook(
                    "UserPromptSubmit",
                    {
                        "cwd": str(Path.home()),
                        "prompt": "Fridays I like to drink tea at 5 am in the park",
                    },
                    path=str(path),
                    user_id="user-zero",
                )
            with NarratorDB(db_path=str(path), user_id="user-zero") as memory:
                self.assertIn(
                    "tea at 5 a.m.",
                    memory.recall("tea on Fridays", workspace_id=None).text,
                )

    def test_intelligence_capture_debounces_and_supersedes_one_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "memory.db"
            transcript = root / "transcript.jsonl"
            compiler = CompilerConfig.local(
                model="hook-test-model",
                endpoint="http://127.0.0.1:11434/v1",
            )
            with NarratorDB(
                db_path=str(path),
                user_id="user-zero",
                mode="intelligence",
                compiler=compiler,
                capture_policy="sessions",
            ):
                pass

            first_window = [
                _codex_message(
                    "user",
                    "The first durable decision is to use the cobalt release train.",
                ),
                _codex_message(
                    "assistant",
                    "The cobalt release-train decision is recorded as the current plan.",
                    phase="final_answer",
                ),
                _codex_message(
                    "user",
                    "The second durable decision is to deploy from the Tokyo region.",
                ),
                _codex_message(
                    "assistant",
                    "The Tokyo deployment region is recorded as the current target.",
                    phase="final_answer",
                ),
            ]
            transcript.write_text("\n".join(first_window), encoding="utf-8")
            event = {
                "session_id": "stable-session",
                "turn_id": "turn-one",
                "cwd": str(root),
                "transcript_path": str(transcript),
            }
            with patch(
                "narratordb.hooks.resolve_project_scope", return_value=_project_scope()
            ):
                run_hook("PreCompact", event, path=str(path), user_id="user-zero")
                run_hook(
                    "Stop",
                    {**event, "turn_id": "turn-two"},
                    path=str(path),
                    user_id="user-zero",
                )

            with sqlite3.connect(path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM memory_sessions"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 4
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM memory_compiler_jobs ORDER BY id"
                    ).fetchall(),
                    [("pending",)],
                )

            expanded_window = [
                *first_window,
                _codex_message(
                    "user",
                    "The third durable decision is to require a green health check.",
                ),
                _codex_message(
                    "assistant",
                    "The green health-check requirement is recorded for every deployment.",
                    phase="final_answer",
                ),
            ]
            transcript.write_text("\n".join(expanded_window), encoding="utf-8")
            with patch(
                "narratordb.hooks.resolve_project_scope", return_value=_project_scope()
            ):
                run_hook("Stop", event, path=str(path), user_id="user-zero")
                run_hook("PreCompact", event, path=str(path), user_id="user-zero")

            with sqlite3.connect(path) as connection:
                session = connection.execute(
                    "SELECT id, external_id, metadata_json FROM memory_sessions"
                ).fetchone()
                membership = connection.execute(
                    "SELECT COUNT(*) FROM memory_session_messages WHERE session_id = ?",
                    (session[0],),
                ).fetchone()[0]
                jobs = connection.execute(
                    "SELECT status FROM memory_compiler_jobs ORDER BY id"
                ).fetchall()
                message_count = connection.execute(
                    "SELECT COUNT(*) FROM messages"
                ).fetchone()[0]

            self.assertEqual(session[1], "agent/stable-session")
            self.assertEqual(json.loads(session[2])["source_event"], "turn-boundary")
            self.assertEqual(membership, 6)
            self.assertEqual(message_count, 6)
            self.assertEqual(jobs, [("obsolete",), ("pending",)])

            with NarratorDB(db_path=str(path), user_id="user-zero") as memory:
                self.assertIn(
                    "green health-check",
                    memory.recall("health-check", workspace_id="project/test").text,
                )

    def test_selective_personal_preference_saves_globally_at_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            fallback = ProjectScope(
                workspace_id="project/home-fallback",
                origin="path_fallback",
                project_root=str(Path.home()),
                in_git_repo=False,
                warning="PROJECT SCOPE WARNING: home fallback",
                write_confirmation_required=True,
            )
            with NarratorDB(
                db_path=str(path),
                user_id="user-zero",
                mode="private",
                capture_policy="preferences",
            ):
                pass

            with (
                patch("narratordb.hooks.resolve_project_scope", return_value=fallback),
                patch.dict(os.environ, {}, clear=True),
                redirect_stdout(StringIO()) as output,
            ):
                for _ in range(2):
                    run_hook(
                        "UserPromptSubmit",
                        {
                            "cwd": str(Path.home()),
                            "prompt": (
                                "i like porsche its truly my favorite car and dream car"
                            ),
                        },
                        path=str(path),
                        user_id="user-zero",
                    )
            self.assertEqual(output.getvalue(), "")

            with NarratorDB(db_path=str(path), user_id="user-zero") as memory:
                recalled = memory.recall(
                    "What is the user's favorite and dream car?",
                    workspace_id=None,
                )
                self.assertIn("favorite and dream car is Porsche", recalled.text)
                self.assertEqual(memory.message_counts()["global"], 1)
                self.assertEqual(
                    memory.message_counts(workspace_id="project/home-fallback")[
                        "selected_scope"
                    ],
                    0,
                )

    def test_manual_and_emergency_off_disable_selective_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for policy, environment in (
                ("manual", {}),
                ("preferences", {"NARRATORDB_AUTO_CAPTURE": "false"}),
            ):
                with self.subTest(policy=policy, environment=environment):
                    path = root / f"{policy}-{bool(environment)}.db"
                    with NarratorDB(
                        db_path=str(path),
                        user_id="user-zero",
                        mode="private",
                        capture_policy=policy,
                    ):
                        pass
                    with (
                        patch(
                            "narratordb.hooks.resolve_project_scope",
                            return_value=_project_scope(),
                        ),
                        patch.dict(os.environ, environment, clear=True),
                    ):
                        run_hook(
                            "UserPromptSubmit",
                            {
                                "cwd": str(root),
                                "prompt": "I really like Porsche 911",
                            },
                            path=str(path),
                            user_id="user-zero",
                        )
                    with NarratorDB(db_path=str(path), user_id="user-zero") as memory:
                        self.assertEqual(memory.message_counts()["global"], 0)

    def test_selective_favorite_update_replaces_only_automatic_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with NarratorDB(
                db_path=str(path),
                user_id="user-zero",
                mode="private",
                capture_policy="preferences",
            ) as memory:
                explicit = memory.remember(
                    "The user explicitly saved a separate Porsche track-day goal.",
                    workspace_id=None,
                )
                self.assertIsNotNone(explicit.message_id)

            with patch(
                "narratordb.hooks.resolve_project_scope", return_value=_project_scope()
            ):
                for prompt in (
                    "My favorite car is Porsche",
                    "My favorite car is Ferrari",
                ):
                    run_hook(
                        "UserPromptSubmit",
                        {"cwd": directory, "prompt": prompt},
                        path=str(path),
                        user_id="user-zero",
                    )

            with NarratorDB(db_path=str(path), user_id="user-zero") as memory:
                result = memory.recall("What is the user's favorite car?")
                self.assertIn("favorite car is Ferrari", result.text)
                self.assertNotIn("favorite car is Porsche", result.text)
                self.assertIn(
                    "Porsche track-day goal",
                    memory.recall("track-day goal").text,
                )
                self.assertEqual(memory.message_counts()["global"], 2)

    def test_explicit_exact_memory_wins_over_automatic_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with NarratorDB(
                db_path=str(path),
                user_id="user-zero",
                mode="private",
                capture_policy="preferences",
            ) as memory:
                explicit = memory.remember(
                    "The user's favorite car is Ferrari.",
                    workspace_id=None,
                )
                self.assertIsNotNone(explicit.message_id)

            with patch(
                "narratordb.hooks.resolve_project_scope", return_value=_project_scope()
            ):
                for prompt in (
                    "My favorite car is Porsche",
                    "My favorite car is Ferrari",
                ):
                    run_hook(
                        "UserPromptSubmit",
                        {"cwd": directory, "prompt": prompt},
                        path=str(path),
                        user_id="user-zero",
                    )

            with NarratorDB(db_path=str(path), user_id="user-zero") as memory:
                self.assertEqual(memory.message_counts()["global"], 1)
                result = memory.recall("What is the user's favorite car?")
                self.assertIn("favorite car is Ferrari", result.text)
                self.assertNotIn("favorite car is Porsche", result.text)

    def test_secret_changed_prompt_is_not_partially_saved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with NarratorDB(
                db_path=str(path),
                user_id="user-zero",
                mode="private",
                capture_policy="preferences",
            ):
                pass
            with patch(
                "narratordb.hooks.resolve_project_scope", return_value=_project_scope()
            ):
                run_hook(
                    "UserPromptSubmit",
                    {
                        "cwd": directory,
                        "prompt": (
                            "I really like Porsche 911 and my api_key="
                            "sk-proj-abcdefghijklmnop"
                        ),
                    },
                    path=str(path),
                    user_id="user-zero",
                )
            with NarratorDB(db_path=str(path), user_id="user-zero") as memory:
                self.assertEqual(memory.message_counts()["global"], 0)

    def test_user_prompt_hook_skips_short_prompts_and_does_not_store_query(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with patch(
                "narratordb.hooks.resolve_project_scope", return_value=_project_scope()
            ):
                run_hook(
                    "UserPromptSubmit",
                    {"cwd": directory, "prompt": "status?"},
                    path=str(path),
                    user_id="user-zero",
                )
            self.assertFalse(path.exists())

    def test_unconfigured_hook_does_not_choose_mode_or_inject_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            output = StringIO()
            with (
                patch(
                    "narratordb.hooks.resolve_project_scope",
                    return_value=_project_scope(),
                ),
                redirect_stdout(output),
            ):
                for event_name, event in (
                    ("SessionStart", {"cwd": directory}),
                    (
                        "UserPromptSubmit",
                        {
                            "cwd": directory,
                            "prompt": "Recall a durable decision from this project.",
                        },
                    ),
                    ("PreCompact", {"cwd": directory}),
                    ("Stop", {"cwd": directory}),
                ):
                    run_hook(
                        event_name,
                        event,
                        path=str(path),
                        user_id="user-zero",
                    )
            self.assertEqual(output.getvalue(), "")
            self.assertFalse(path.exists())

    def test_hooks_preserve_configured_private_and_intelligence_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                ("private", None),
                (
                    "intelligence",
                    CompilerConfig.local(
                        model="mode-preservation-test",
                        endpoint="http://127.0.0.1:11434/v1",
                    ),
                ),
            )
            for mode, compiler in cases:
                with self.subTest(mode=mode):
                    path = root / f"{mode}.db"
                    with NarratorDB(
                        db_path=str(path),
                        user_id="user-zero",
                        mode=mode,
                        compiler=compiler,
                    ):
                        pass
                    before = ProjectConfigStore(str(path)).load()

                    output = StringIO()
                    with (
                        patch(
                            "narratordb.hooks.resolve_project_scope",
                            return_value=_project_scope(),
                        ),
                        redirect_stdout(output),
                    ):
                        run_hook(
                            "SessionStart",
                            {"cwd": str(root)},
                            path=str(path),
                            user_id="user-zero",
                        )

                    after = ProjectConfigStore(str(path)).load()
                    self.assertEqual(after, before)
                    self.assertEqual(after.mode.value, mode)
                    self.assertEqual(output.getvalue(), "")

    def test_secret_redaction_patterns(self) -> None:
        redacted = redact_secrets(
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz and password=hunter2"
        )
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 2)

        # Assemble credential-shaped fixtures at runtime so repository secret
        # scanners never have to distinguish test data from live credentials.
        tokens = tuple(
            prefix + suffix
            for prefix, suffix in (
                ("sk-ant-api03-", "abcdefghijklmnopqrstuvwxyz012345"),
                ("sk_" + "live_", "abcdefghijklmnopqrstuvwxyz012345"),
                ("rk_" + "test_", "abcdefghijklmnopqrstuvwxyz012345"),
                ("AI" + "za", "abcdefghijklmnopqrstuvwxyz0123456789"),
                ("xo" + "xb-", "123456789012-abcdefghijklmnopqrstuvwxyz"),
                ("gl" + "pat-", "abcdefghijklmnopqrstuvwxyz012345"),
                ("npm" + "_", "abcdefghijklmnopqrstuvwxyz012345"),
            )
        )
        for token in tokens:
            with self.subTest(token_type=token.split("-", 1)[0]):
                cleaned = redact_secrets(f"before {token} after")
                self.assertNotIn(token, cleaned)
                self.assertIn("[REDACTED]", cleaned)

        assigned = redact_secrets("auth_token=short-but-sensitive")
        self.assertNotIn("short-but-sensitive", assigned)
        self.assertIn("[REDACTED]", assigned)


if __name__ == "__main__":
    unittest.main()
