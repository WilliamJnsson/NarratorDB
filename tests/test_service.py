"""Authenticated service control-plane and tenant-isolation tests."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from narratordb import CompilerConfig, ConfigurationError, MemoryMode, NarratorDB
from narratordb.config import CapturePolicy, ProjectConfigStore
from narratordb.service import (
    ADMIN_SCOPES,
    DEFAULT_SCOPES,
    ServiceControlPlane,
    ServiceRuntimeProxy,
    ServiceTokenVerifier,
    add_project,
    create_service_server,
    initialize_service,
    issue_project_key,
    prepare_quickstart,
    revoke_project_key,
    serve,
)


def _read_credentials(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        values[key] = value
    return values


class ServiceAlphaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data_dir = self.root / "service-data"
        self.credentials = self.root / "alpha.env"
        self.initialized = initialize_service(
            data_dir=self.data_dir,
            project_name="alpha-project",
            credentials_file=self.credentials,
            mode=MemoryMode.PRIVATE,
            compiler=None,
            capture_policy="manual",
        )
        self.values = _read_credentials(self.credentials)
        self.control = ServiceControlPlane(self.data_dir)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _principal_patch(self, token: str):
        principal = self.control.resolve_token(token)
        self.assertIsNotNone(principal)
        assert principal is not None
        access = SimpleNamespace(scopes=list(principal.scopes))
        return patch.object(
            ServiceRuntimeProxy,
            "_principal",
            return_value=(access, principal.account_id, principal.project_id),
        )

    def test_initialization_writes_one_time_credentials_and_hashes_token(self) -> None:
        token = self.values["NARRATORDB_SERVICE_TOKEN"]
        self.assertTrue(token.startswith("ndb_"))
        self.assertEqual(
            stat.S_IMODE(self.credentials.stat().st_mode),
            0o600,
        )
        self.assertEqual(stat.S_IMODE(self.data_dir.stat().st_mode), 0o700)
        control_bytes = (self.data_dir / "service.db").read_bytes()
        self.assertNotIn(token.encode("utf-8"), control_bytes)
        principal = self.control.resolve_token(token)
        self.assertIsNotNone(principal)
        assert principal is not None
        self.assertEqual(principal.scopes, ADMIN_SCOPES)
        self.assertEqual(
            principal.project_id,
            self.initialized["project_id"],
        )
        self.assertNotIn(token, repr(self.initialized))

        with self.assertRaisesRegex(ConfigurationError, "already initialized"):
            initialize_service(
                data_dir=self.data_dir,
                project_name="duplicate",
                credentials_file=self.root / "other.env",
                mode=MemoryMode.PRIVATE,
                compiler=None,
                capture_policy=None,
            )

    def test_service_data_directory_rejects_a_symbolic_link(self) -> None:
        link = self.root / "linked-service-data"
        try:
            link.symlink_to(self.data_dir, target_is_directory=True)
        except OSError as error:  # pragma: no cover - unsupported test platform
            self.skipTest(f"symbolic links unavailable: {error}")
        with self.assertRaisesRegex(ConfigurationError, "symbolic link"):
            ServiceControlPlane(link)

    def test_credentials_collision_fails_before_control_plane_mutation(self) -> None:
        def counts() -> tuple[int, int]:
            with sqlite3.connect(self.data_dir / "service.db") as connection:
                projects = connection.execute("SELECT COUNT(*) FROM projects").fetchone()
                keys = connection.execute("SELECT COUNT(*) FROM api_keys").fetchone()
            assert projects is not None and keys is not None
            return int(projects[0]), int(keys[0])

        before = counts()
        with self.assertRaisesRegex(ConfigurationError, "already exists"):
            add_project(
                data_dir=self.data_dir,
                project_name="must-not-exist",
                credentials_file=self.credentials,
            )
        self.assertEqual(counts(), before)

        link = self.root / "linked-credentials.env"
        try:
            link.symlink_to(self.root / "missing-target.env")
        except OSError as error:  # pragma: no cover - unsupported test platform
            self.skipTest(f"symbolic links unavailable: {error}")
        with self.assertRaisesRegex(ConfigurationError, "symbolic link"):
            issue_project_key(
                data_dir=self.data_dir,
                project="alpha-project",
                credentials_file=link,
            )
        self.assertEqual(counts(), before)

    def test_project_keys_isolate_memory_and_survive_runtime_restart(self) -> None:
        second_credentials = self.root / "second.env"
        second = add_project(
            data_dir=self.data_dir,
            project_name="second-project",
            credentials_file=second_credentials,
        )
        first_token = self.values["NARRATORDB_SERVICE_TOKEN"]
        second_token = _read_credentials(second_credentials)[
            "NARRATORDB_SERVICE_TOKEN"
        ]
        proxy = ServiceRuntimeProxy(self.control)
        try:
            with self._principal_patch(first_token):
                proxy.remember("The alpha project release color is cobalt.")
                first = proxy.recall("What is the release color?")
                self.assertIn("cobalt", first["context"])
            with self._principal_patch(second_token):
                second_result = proxy.recall("What is the release color?")
                self.assertNotIn("cobalt", second_result["context"])
                proxy.remember("The second project release color is amber.")
            proxy.close()

            restarted = ServiceRuntimeProxy(self.control)
            try:
                with self._principal_patch(first_token):
                    first = restarted.recall("What is the release color?")
                    self.assertIn("cobalt", first["context"])
                    self.assertNotIn("amber", first["context"])
                with self._principal_patch(second_token):
                    second_result = restarted.recall("What is the release color?")
                    self.assertIn("amber", second_result["context"])
                    self.assertNotIn("cobalt", second_result["context"])
            finally:
                restarted.close()
        finally:
            proxy.close()
        self.assertNotEqual(second["project_id"], self.initialized["project_id"])

    def test_account_and_project_identity_are_both_isolated(self) -> None:
        second_account = self.control.create_account("second-account")
        second_project = self.control.create_project(second_account, "shared-name")
        second_db = self.control.account_db_path(second_account)
        with NarratorDB(
            db_path=str(second_db),
            user_id=second_account,
            mode=MemoryMode.PRIVATE,
            capture_policy="manual",
        ):
            pass
        second_token, _, _ = self.control.issue_key(
            account_id=second_account,
            project_id=second_project,
        )
        first_token = self.values["NARRATORDB_SERVICE_TOKEN"]

        proxy = ServiceRuntimeProxy(self.control)
        try:
            with self._principal_patch(first_token):
                proxy.remember("The account boundary marker is northern-light.")
            with self._principal_patch(second_token):
                result = proxy.recall("What is the account boundary marker?")
                self.assertNotIn("northern-light", result["context"])
        finally:
            proxy.close()

    def test_scopes_revoke_and_path_redaction_fail_closed(self) -> None:
        restricted_file = self.root / "restricted.env"
        issued = issue_project_key(
            data_dir=self.data_dir,
            project="alpha-project",
            credentials_file=restricted_file,
            admin=False,
        )
        token = _read_credentials(restricted_file)["NARRATORDB_SERVICE_TOKEN"]
        self.assertEqual(tuple(issued["scopes"]), DEFAULT_SCOPES)
        proxy = ServiceRuntimeProxy(self.control)
        try:
            with self._principal_patch(token):
                status = proxy.status()
                self.assertNotIn("db_path", repr(status))
                self.assertNotIn(str(self.data_dir), repr(status))
                with self.assertRaisesRegex(PermissionError, "project:admin"):
                    proxy.configure(capture_policy="preferences")
        finally:
            proxy.close()

        with patch.dict(os.environ, {"TEST_NARRATORDB_TOKEN": token}):
            result = revoke_project_key(
                data_dir=self.data_dir,
                token_environment="TEST_NARRATORDB_TOKEN",
            )
        self.assertTrue(result["revoked"])
        self.assertIsNone(self.control.resolve_token(token))

    def test_intelligence_configuration_and_http_auth_contract(self) -> None:
        intelligence_data = self.root / "intelligence-data"
        intelligence_credentials = self.root / "intelligence.env"
        compiler = CompilerConfig.local(
            model="alpha-local",
            endpoint="http://127.0.0.1:11434/v1",
        )
        result = initialize_service(
            data_dir=intelligence_data,
            project_name="intelligence-project",
            credentials_file=intelligence_credentials,
            mode=MemoryMode.INTELLIGENCE,
            compiler=compiler,
            capture_policy="manual",
        )
        self.assertEqual(result["mode"], "intelligence")

        server, proxy = create_service_server(
            self.control,
            host="127.0.0.1",
            port=8787,
            public_url="http://127.0.0.1:8787",
        )
        try:
            self.assertEqual(server.settings.streamable_http_path, "/mcp")
            self.assertTrue(server.settings.stateless_http)
            self.assertIsNotNone(server.settings.auth)
            paths = {route.path for route in server.streamable_http_app().routes}
            self.assertIn("/mcp", paths)
            self.assertIn("/healthz", paths)
            self.assertIn("/readyz", paths)
        finally:
            proxy.close()

        with self.assertRaisesRegex(ConfigurationError, "https public URL"):
            create_service_server(
                self.control,
                host="0.0.0.0",
                port=8787,
                public_url="http://service.example",
            )

    def test_token_verifier_returns_only_bounded_identity_claims(self) -> None:
        import asyncio

        token = self.values["NARRATORDB_SERVICE_TOKEN"]
        verifier = ServiceTokenVerifier(self.control)
        access = asyncio.run(verifier.verify_token(token))
        self.assertIsNotNone(access)
        assert access is not None
        self.assertEqual(access.token, "verified")
        self.assertNotIn(token, repr(access))
        self.assertIsNone(asyncio.run(verifier.verify_token("ndb_invalid")))

    def test_quickstart_initializes_once_without_environment_setup(self) -> None:
        data_dir = self.root / "quickstart-data"
        credentials = data_dir / "credentials.env"
        first = prepare_quickstart(
            data_dir=data_dir,
            credentials_file=credentials,
            project_name="quickstart-project",
            register_codex=False,
        )
        self.assertTrue(first["initialized"])
        self.assertTrue(credentials.is_file())
        self.assertNotIn("NARRATORDB_SERVICE_TOKEN", repr(first))
        values = _read_credentials(credentials)
        control = ServiceControlPlane(data_dir)
        principal = control.resolve_token(values["NARRATORDB_SERVICE_TOKEN"])
        self.assertIsNotNone(principal)
        assert principal is not None
        config = ProjectConfigStore(
            str(control.account_db_path(principal.account_id))
        ).load()
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.capture_policy, CapturePolicy.SESSIONS)

        second = prepare_quickstart(
            data_dir=data_dir,
            credentials_file=credentials,
            project_name="ignored-on-restart",
            register_codex=False,
        )
        self.assertFalse(second["initialized"])
        self.assertEqual(first["data_dir"], second["data_dir"])

    def test_service_never_falls_back_to_the_user_data_directory(self) -> None:
        data_dir = self.root / "explicit-service-data"
        credentials = self.root / "explicit-service.env"
        with patch(
            "narratordb.database.default_data_dir",
            side_effect=AssertionError("default data directory was accessed"),
        ):
            initialize_service(
                data_dir=data_dir,
                project_name="explicit-project",
                credentials_file=credentials,
                mode=MemoryMode.PRIVATE,
                compiler=None,
                capture_policy="manual",
            )
            values = _read_credentials(credentials)
            control = ServiceControlPlane(data_dir)
            principal = control.resolve_token(
                values["NARRATORDB_SERVICE_TOKEN"]
            )
            self.assertIsNotNone(principal)
            assert principal is not None
            access = SimpleNamespace(scopes=list(principal.scopes))
            proxy = ServiceRuntimeProxy(control)
            try:
                with patch.object(
                    ServiceRuntimeProxy,
                    "_principal",
                    return_value=(
                        access,
                        principal.account_id,
                        principal.project_id,
                    ),
                ):
                    self.assertEqual(proxy.status()["mode"], "private")
            finally:
                proxy.close()

    def test_failed_initialization_rolls_back_and_can_be_retried(self) -> None:
        data_dir = self.root / "failed-service-data"
        credentials = data_dir / "credentials.env"
        with patch(
            "narratordb.service._write_credentials",
            side_effect=OSError("simulated credentials failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated credentials failure"):
                initialize_service(
                    data_dir=data_dir,
                    project_name="retry-project",
                    credentials_file=credentials,
                    mode=MemoryMode.PRIVATE,
                    compiler=None,
                    capture_policy="manual",
                )

        self.assertFalse((data_dir / "service.db").exists())
        self.assertFalse((data_dir / "accounts").exists())
        self.assertFalse(credentials.exists())

        retried = initialize_service(
            data_dir=data_dir,
            project_name="retry-project",
            credentials_file=credentials,
            mode=MemoryMode.PRIVATE,
            compiler=None,
            capture_policy="manual",
        )
        self.assertTrue(retried["initialized"])
        self.assertTrue(credentials.is_file())

    def test_failed_initialization_preserves_a_raced_credentials_file(self) -> None:
        data_dir = self.root / "raced-service-data"
        credentials = data_dir / "credentials.env"

        def raced_write(target: Path, **_: object) -> Path:
            target.write_text("created-by-another-process\n", encoding="utf-8")
            raise FileExistsError("simulated credentials race")

        with patch("narratordb.service._write_credentials", side_effect=raced_write):
            with self.assertRaisesRegex(FileExistsError, "credentials race"):
                initialize_service(
                    data_dir=data_dir,
                    project_name="raced-project",
                    credentials_file=credentials,
                    mode=MemoryMode.PRIVATE,
                    compiler=None,
                    capture_policy="manual",
                )

        self.assertEqual(
            credentials.read_text(encoding="utf-8"),
            "created-by-another-process\n",
        )
        self.assertFalse((data_dir / "service.db").exists())

    def test_interrupt_during_prewarm_stops_without_traceback(self) -> None:
        with patch(
            "narratordb.service.create_service_server",
            side_effect=KeyboardInterrupt,
        ):
            result = serve(data_dir=self.data_dir)
        self.assertTrue(result["stopped"])


if __name__ == "__main__":
    unittest.main()
