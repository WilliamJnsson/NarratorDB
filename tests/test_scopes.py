"""Project-scope identity and fallback diagnostics."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from narratordb.scopes import (
    path_fallback_writes_allowed,
    project_workspace,
    resolve_project_scope,
)


class ProjectScopeTests(unittest.TestCase):
    def test_explicit_scope_reports_its_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ, {"NARRATORDB_WORKSPACE_ID": "team/portable-project"}
            ):
                scope = resolve_project_scope(directory)
                workspace = project_workspace(directory)

        self.assertEqual(scope.workspace_id, "team/portable-project")
        self.assertEqual(scope.origin, "explicit")
        self.assertIsNone(scope.warning)
        self.assertFalse(scope.write_confirmation_required)
        self.assertEqual(workspace, "team/portable-project")

    def test_git_remote_scope_is_portable_and_has_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "narratordb.scopes._run_git",
                    side_effect=[str(root), "git@github.com:owner/repository.git"],
                ),
            ):
                scope = resolve_project_scope(root)

        self.assertEqual(scope.workspace_id, "project/owner/repository")
        self.assertEqual(scope.origin, "git_remote")
        self.assertTrue(scope.in_git_repo)
        self.assertIsNone(scope.warning)

    def test_non_git_project_warns_but_remains_writable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("narratordb.scopes._run_git", return_value=""),
            ):
                scope = resolve_project_scope(root)

        self.assertEqual(scope.origin, "path_fallback")
        self.assertIn("No Git repository", scope.warning or "")
        self.assertFalse(scope.write_confirmation_required)
        self.assertEqual(scope.project_root, str(root))

    def test_git_repository_without_remote_uses_warned_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            nested = root / "src"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "narratordb.scopes._run_git",
                    side_effect=[str(root), ""],
                ),
            ):
                scope = resolve_project_scope(nested)

        self.assertEqual(scope.origin, "path_fallback")
        self.assertTrue(scope.in_git_repo)
        self.assertIn("remote.origin.url", scope.warning or "")
        self.assertEqual(scope.suggested_cwd, str(root))
        self.assertFalse(scope.write_confirmation_required)

    def test_home_fallback_requires_write_confirmation(self) -> None:
        home = Path.home().expanduser().resolve()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("narratordb.scopes._run_git", return_value=""),
        ):
            scope = resolve_project_scope(home)

        self.assertEqual(scope.origin, "path_fallback")
        self.assertTrue(scope.write_confirmation_required)
        self.assertIn("home directory", scope.warning or "")
        self.assertIn("Project writes are blocked", scope.warning or "")

    def test_workspace_argument_wins_over_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ, {"NARRATORDB_WORKSPACE_ID": "environment/scope"}
            ):
                scope = resolve_project_scope(
                    directory, workspace_id="command/line-scope"
                )

        self.assertEqual(scope.workspace_id, "command/line-scope")
        self.assertEqual(scope.origin, "explicit")

    def test_path_fallback_write_confirmation_accepts_env_or_flag(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(path_fallback_writes_allowed())
            self.assertTrue(path_fallback_writes_allowed(confirmed=True))

        with patch.dict(
            os.environ, {"NARRATORDB_ALLOW_PATH_FALLBACK_WRITES": "yes"}, clear=True
        ):
            self.assertTrue(path_fallback_writes_allowed())


if __name__ == "__main__":
    unittest.main()
