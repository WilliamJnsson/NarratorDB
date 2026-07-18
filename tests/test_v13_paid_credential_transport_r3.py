from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
R3_RELATIVE = Path(
    "benchmark_records/reproduction-v13-paid-paired-scoring-r3-20260716"
)
R3 = REPOSITORY / R3_RELATIVE
R2 = (
    REPOSITORY
    / "benchmark_records/reproduction-v13-paid-paired-scoring-r2-20260716"
)
LAUNCHER = R3 / "launch_with_openrouter_key.sh"
WRAPPER = R3 / "run_paid_variant_hardened.sh"
COMMANDS = R3 / "commands-r3.json"
REQUIREMENTS = R3 / "execution-authorization-requirements.json"
DUMMY_KEY = "narratordb-credential-transport-dummy-value"
NONCREDENTIAL_PREFIX = (
    "unset OPENROUTER_API_KEY ANTHROPIC_API_KEY GOOGLE_API_KEY "
    "GEMINI_API_KEY OPENAI_API_KEY;"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _unsafe_external_env_assignment(command: str) -> bool:
    return bool(
        re.search(
            r"(?:^|[;&|()]\s*|\bexec\s+)env\s+[^;\n]*"
            r"\bOPENROUTER_API_KEY=",
            command,
        )
    )


def _copy_unset_precedes_external(source: str, *, holder: str, external: str) -> bool:
    assignment = f"{holder}=$OPENROUTER_API_KEY"
    unexport = f"export -n {holder}"
    unset = "unset OPENROUTER_API_KEY"
    try:
        assignment_index = source.index(assignment)
        unexport_index = source.index(unexport, assignment_index)
        unset_index = source.index(unset, unexport_index)
        external_index = source.index(external, unset_index)
    except ValueError:
        return False
    return assignment_index < unexport_index < unset_index < external_index


class CredentialTransportR3Tests(unittest.TestCase):
    maxDiff = None

    def test_launcher_and_wrapper_copy_unset_before_external_child(self) -> None:
        launcher = LAUNCHER.read_text(encoding="utf-8")
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertTrue(launcher.startswith("#!/bin/bash -p\n"))
        self.assertTrue(wrapper.startswith("#!/bin/bash -p\n"))
        self.assertTrue(
            _copy_unset_precedes_external(
                launcher,
                holder="R3_RUNTIME_OPENROUTER_KEY",
                external="R3_SCRIPT_DIR=$(",
            )
        )
        self.assertTrue(
            _copy_unset_precedes_external(
                wrapper,
                holder="RUNTIME_OPENROUTER_KEY",
                external="SCRIPT_DIR=$(",
            )
        )
        self.assertNotIn("exec env -i", launcher)
        self.assertNotIn("exec env -i", wrapper)
        self.assertIn('exec "$@"', launcher)
        self.assertIn('exec "$ROOT/.venv/bin/python"', wrapper)

    def test_commands_close_exact_credential_and_noncredential_sets(self) -> None:
        document = json.loads(COMMANDS.read_text(encoding="utf-8"))
        entries = [
            value
            for value in _walk_objects(document)
            if "id" in value and "command" in value
        ]
        self.assertEqual(len(entries), 35)
        credential = {
            value["id"]: value["command"]
            for value in entries
            if value.get("credential_process") is True
        }
        self.assertEqual(
            credential,
            {
                "provider-telemetry-before-v7": (
                    f"{R3_RELATIVE.as_posix()}/launch_with_openrouter_key.sh "
                    "telemetry-before-v7"
                ),
                "execute-v7-once": (
                    f"{R3_RELATIVE.as_posix()}/launch_with_openrouter_key.sh "
                    "evaluate-v7"
                ),
                "provider-telemetry-before-v13": (
                    f"{R3_RELATIVE.as_posix()}/launch_with_openrouter_key.sh "
                    "telemetry-before-v13"
                ),
                "execute-v13-once": (
                    f"{R3_RELATIVE.as_posix()}/launch_with_openrouter_key.sh "
                    "evaluate-v13"
                ),
                "final-provider-telemetry": (
                    f"{R3_RELATIVE.as_posix()}/launch_with_openrouter_key.sh "
                    "telemetry-after-pair"
                ),
            },
        )
        for value in entries:
            command = value["command"]
            self.assertFalse(
                _unsafe_external_env_assignment(command), msg=value["id"]
            )
            if value.get("credential_process") is not True:
                self.assertTrue(
                    command.startswith(NONCREDENTIAL_PREFIX), msg=value["id"]
                )

    def test_launcher_is_hash_bound_by_dynamic_admission(self) -> None:
        requirements = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))
        revision = requirements["revision"]
        self.assertEqual(
            revision["credential_launcher_path"],
            f"{R3_RELATIVE.as_posix()}/launch_with_openrouter_key.sh",
        )
        self.assertEqual(revision["credential_launcher_sha256"], _sha256(LAUNCHER))
        verifier = (R3 / "verify_dynamic_admission.py").read_text(encoding="utf-8")
        self.assertIn('"credential_launcher_path"', verifier)
        self.assertIn('"credential_launcher_sha256"', verifier)

    def test_r2_regressions_are_detected_by_r3_static_guards(self) -> None:
        r2_wrapper = (R2 / "run_paid_variant_hardened.sh").read_text(
            encoding="utf-8"
        )
        r3_wrapper = WRAPPER.read_text(encoding="utf-8")
        r2_commands = json.loads((R2 / "commands-r2.json").read_text(encoding="utf-8"))
        self.assertFalse(
            _copy_unset_precedes_external(
                r2_wrapper,
                holder="RUNTIME_OPENROUTER_KEY",
                external="SCRIPT_DIR=$(",
            )
        )
        self.assertTrue(
            _copy_unset_precedes_external(
                r3_wrapper,
                holder="RUNTIME_OPENROUTER_KEY",
                external="SCRIPT_DIR=$(",
            )
        )
        self.assertTrue(
            any(
                _unsafe_external_env_assignment(value["command"])
                for value in _walk_objects(r2_commands)
                if "command" in value
            )
        )

    def test_wrapper_first_external_child_has_no_credential(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observation = root / "first-child.json"
            startup_marker = root / "bash-env-ran"
            fake_dir = root / "bin"
            _write_executable(
                fake_dir / "dirname",
                "#!/bin/bash -p\n"
                "set -eu\n"
                f"OUT={shlex.quote(str(observation))}\n"
                "openrouter=false\n"
                "holder=false\n"
                "[[ -z ${OPENROUTER_API_KEY+x} ]] || openrouter=true\n"
                "[[ -z ${RUNTIME_OPENROUTER_KEY+x} ]] || holder=true\n"
                "printf '{\"openrouter_present\":%s,\"holder_present\":%s}\\n' "
                '"$openrouter" "$holder" >"$OUT"\n'
                "exit 97\n",
            )
            bash_env = root / "hostile-bash-env"
            bash_env.write_text(
                f"printf ran > {shlex.quote(str(startup_marker))}\n",
                encoding="utf-8",
            )
            environment = {
                "PATH": f"{fake_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
                "OPENROUTER_API_KEY": DUMMY_KEY,
                "RUNTIME_OPENROUTER_KEY": "hostile-exported-holder",
                "NARRATORDB_PAID_PRECOMMIT_SHA256": "0" * 64,
                "BASH_ENV": str(bash_env),
            }
            result = subprocess.run(
                [
                    str(WRAPPER),
                    "reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r3-20260716/attempt1/v7-control",
                    "narratordb-intelligence-dev42-v7-gpt54mini",
                    "reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/longmemeval_s_dev42.json",
                ],
                cwd=REPOSITORY,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(DUMMY_KEY.encode(), result.stdout + result.stderr)
            self.assertFalse(startup_marker.exists())
            self.assertEqual(
                json.loads(observation.read_text(encoding="utf-8")),
                {"holder_present": False, "openrouter_present": False},
            )

    def test_launcher_rejects_arbitrary_target_without_echoing_key(self) -> None:
        environment = {
            "OPENROUTER_API_KEY": DUMMY_KEY,
            "NARRATORDB_PAID_PRECOMMIT_SHA256": "0" * 64,
            "BASH_ENV": "/definitely/not/a/startup/file",
        }
        result = subprocess.run(
            [str(LAUNCHER), "not-a-precommitted-action"],
            cwd=REPOSITORY,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"not one exact precommitted tuple", result.stderr)
        self.assertNotIn(DUMMY_KEY.encode(), result.stdout + result.stderr)

    def test_temp_sealed_launcher_handoff_keeps_key_out_of_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bundle = root / R3_RELATIVE
            bundle.mkdir(parents=True)
            launcher = bundle / LAUNCHER.name
            shutil.copy2(LAUNCHER, launcher)
            launcher.chmod(0o755)

            observation = root / "target-observation.json"
            startup_marker = root / "bash-env-ran"
            fake_python = root / ".venv/bin/python"
            _write_executable(
                fake_python,
                "#!/bin/bash -p\n"
                "set -eu\n"
                f"OUT={shlex.quote(str(observation))}\n"
                "key_present=false\n"
                "argv_contains_key=false\n"
                "holder_present=false\n"
                "[[ -z ${OPENROUTER_API_KEY+x} ]] || key_present=true\n"
                "[[ -z ${R3_RUNTIME_OPENROUTER_KEY+x} ]] || holder_present=true\n"
                "case \" $* \" in *\"${OPENROUTER_API_KEY:-missing}\"*) "
                "argv_contains_key=true ;; esac\n"
                "printf '{\"argv_contains_key\":%s,\"holder_present\":%s,"
                "\"key_present\":%s}\\n' \"$argv_contains_key\" \"$holder_present\" "
                '"$key_present" >"$OUT"\n'
                "exec /bin/sleep 5\n",
            )
            (bundle / "capture_provider_telemetry.py").write_text(
                "# exact fake target argument; never imported by the fake interpreter\n",
                encoding="utf-8",
            )
            _write_executable(
                bundle / "run_paid_variant_hardened.sh",
                "#!/bin/bash -p\nexit 99\n",
            )
            bound = root / "bound-input.txt"
            bound.write_text("sealed test input\n", encoding="utf-8")
            bound_manifest = bundle / "BOUND_INPUTS_SHA256SUMS"
            bound_manifest.write_text(
                f"{_sha256(bound)}  bound-input.txt\n", encoding="utf-8"
            )
            members = [
                bundle / "BOUND_INPUTS_SHA256SUMS",
                bundle / "capture_provider_telemetry.py",
                bundle / "launch_with_openrouter_key.sh",
                bundle / "run_paid_variant_hardened.sh",
            ]
            seal = bundle / "SHA256SUMS"
            seal.write_text(
                "".join(
                    f"{_sha256(path)}  {path.name}\n" for path in sorted(members)
                ),
                encoding="utf-8",
            )
            precommit = _sha256(seal)
            bash_env = root / "hostile-bash-env"
            bash_env.write_text(
                f"printf ran > {shlex.quote(str(startup_marker))}\n",
                encoding="utf-8",
            )
            environment = {
                "OPENROUTER_API_KEY": DUMMY_KEY,
                "R3_RUNTIME_OPENROUTER_KEY": "hostile-exported-holder",
                "NARRATORDB_PAID_PRECOMMIT_SHA256": precommit,
                "BASH_ENV": str(bash_env),
            }
            process = subprocess.Popen(
                [str(launcher), "telemetry-before-v7"],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                for _ in range(100):
                    if observation.exists() or process.poll() is not None:
                        break
                    time.sleep(0.02)
                self.assertTrue(
                    observation.exists(),
                    msg=(process.communicate(timeout=2) if process.poll() is not None else None),
                )
                ps = subprocess.run(
                    ["/bin/ps", "-p", str(process.pid), "-o", "command="],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=5,
                )
                self.assertNotIn(DUMMY_KEY.encode(), ps.stdout)
                self.assertEqual(
                    json.loads(observation.read_text(encoding="utf-8")),
                    {
                        "argv_contains_key": False,
                        "holder_present": False,
                        "key_present": True,
                    },
                )
                self.assertFalse(startup_marker.exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)


def _walk_objects(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_objects(child)


if __name__ == "__main__":
    unittest.main()
