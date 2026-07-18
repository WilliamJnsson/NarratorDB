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
R5_RELATIVE = Path(
    "benchmark_records/reproduction-v13-paid-paired-scoring-r5-20260716"
)
R5 = REPOSITORY / R5_RELATIVE
R2 = (
    REPOSITORY
    / "benchmark_records/reproduction-v13-paid-paired-scoring-r2-20260716"
)
LAUNCHER = R5 / "launch_with_openrouter_key.sh"
WRAPPER = R5 / "run_paid_variant_hardened.sh"
COMMANDS = R5 / "commands-r5.json"
REQUIREMENTS = R5 / "execution-authorization-requirements.json"
DUMMY_KEY = "narratordb-credential-transport-dummy-value"
SEALED_VENDOR_PYTHON = Path(
    "/Users/william/.local/share/uv/python/"
    "cpython-3.12-macos-aarch64-none/bin/python3.12"
)
SEALED_VENDOR_PYTHON_SHA256 = (
    "7b05d803bbc1bbfc81644af4faf2b88f0a37b8de96b9f42c1e08033e2cd0848a"
)
NONCREDENTIAL_PREFIX = (
    "unset OPENROUTER_API_KEY ANTHROPIC_API_KEY GOOGLE_API_KEY "
    "GEMINI_API_KEY OPENAI_API_KEY;"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sealed_vendor_runtime_available() -> bool:
    try:
        return (
            SEALED_VENDOR_PYTHON.is_file()
            and _sha256(SEALED_VENDOR_PYTHON) == SEALED_VENDOR_PYTHON_SHA256
        )
    except OSError:
        return False


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


class CredentialTransportR5Tests(unittest.TestCase):
    maxDiff = None

    def test_launcher_and_wrapper_copy_unset_before_external_child(self) -> None:
        launcher = LAUNCHER.read_text(encoding="utf-8")
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertTrue(launcher.startswith("#!/bin/bash -p\n"))
        self.assertTrue(wrapper.startswith("#!/bin/bash -p\n"))
        self.assertTrue(
            _copy_unset_precedes_external(
                launcher,
                holder="R5_RUNTIME_OPENROUTER_KEY",
                external="R5_SCRIPT_DIR=$(",
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
        self.assertIn(
            'exec "$ROOT/vendor/memory-benchmarks/.venv/bin/python"', wrapper
        )
        self.assertIn("verify_completed_arm_before_telemetry v7-control", launcher)
        self.assertIn("verify_completed_arm_before_telemetry v13-first", launcher)
        self.assertLess(
            launcher.index("verify_completed_arm_before_telemetry v7-control"),
            launcher.index("OPENROUTER_API_KEY=$R5_RUNTIME_OPENROUTER_KEY", 1000),
        )

    def test_commands_close_exact_credential_and_noncredential_sets(self) -> None:
        document = json.loads(COMMANDS.read_text(encoding="utf-8"))
        operator = document["runtime_environment"]["operator_pty_injection_protocol"]
        self.assertEqual(
            operator["schema_version"],
            "narratordb.r5-operator-pty-credential-injection.v1",
        )
        self.assertEqual(
            operator["first_write_exact_line"],
            "set +x +v; set +o history; "
            "unset HISTFILE PROMPT_COMMAND BASH_XTRACEFD; "
            "trap - DEBUG RETURN; unset OPENROUTER_API_KEY; "
            "unset R5_OPERATOR_KEY; "
            "IFS= read -r -s -p 'R5_OPENROUTER_KEY> ' R5_OPERATOR_KEY",
        )
        self.assertEqual(
            operator["fresh_shell_exact_argv"],
            [
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME=/tmp",
                "TMPDIR=/tmp",
                "LANG=C",
                "LC_ALL=C",
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-p",
            ],
        )
        self.assertTrue(operator["fresh_shell_tty_required"])
        self.assertTrue(operator["reusing_an_existing_shell_forbidden"])
        self.assertFalse(operator["parent_export_survives_clean_shell"])
        self.assertEqual(
            operator["published_seal_initialization_placeholder"],
            "<EXTERNALLY_PUBLISHED_R5_SEAL>",
        )
        self.assertIn(
            "export NARRATORDB_PAID_PRECOMMIT_SHA256",
            operator["published_seal_initialization_template_line"],
        )
        self.assertIn(
            "shasum -a 256",
            operator["published_seal_initialization_template_line"],
        )
        self.assertIn("secret value and one newline", operator["second_write_rule"])
        self.assertEqual(
            set(operator["third_write_exact_lines_by_action"]),
            {
                "telemetry-before-v7",
                "evaluate-v7",
                "telemetry-before-v13",
                "evaluate-v13",
                "telemetry-after-pair",
            },
        )
        for action, command in operator["third_write_exact_lines_by_action"].items():
            self.assertIn(f"launch_with_openrouter_key.sh {action}", command)
            self.assertIn('OPENROUTER_API_KEY="$R5_OPERATOR_KEY"', command)
            self.assertNotIn(DUMMY_KEY, command)
            self.assertIn("unset OPENROUTER_API_KEY R5_OPERATOR_KEY", command)
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
                    f"{R5_RELATIVE.as_posix()}/launch_with_openrouter_key.sh "
                    "telemetry-before-v7"
                ),
                "execute-v7-once": (
                    f"{R5_RELATIVE.as_posix()}/launch_with_openrouter_key.sh "
                    "evaluate-v7"
                ),
                "provider-telemetry-before-v13": (
                    f"{R5_RELATIVE.as_posix()}/launch_with_openrouter_key.sh "
                    "telemetry-before-v13"
                ),
                "execute-v13-once": (
                    f"{R5_RELATIVE.as_posix()}/launch_with_openrouter_key.sh "
                    "evaluate-v13"
                ),
                "final-provider-telemetry": (
                    f"{R5_RELATIVE.as_posix()}/launch_with_openrouter_key.sh "
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
            f"{R5_RELATIVE.as_posix()}/launch_with_openrouter_key.sh",
        )
        self.assertEqual(revision["credential_launcher_sha256"], _sha256(LAUNCHER))
        verifier = (R5 / "verify_dynamic_admission.py").read_text(encoding="utf-8")
        self.assertIn('"credential_launcher_path"', verifier)
        self.assertIn('"credential_launcher_sha256"', verifier)

    def test_r2_regressions_are_detected_by_r5_static_guards(self) -> None:
        r2_wrapper = (R2 / "run_paid_variant_hardened.sh").read_text(
            encoding="utf-8"
        )
        r5_wrapper = WRAPPER.read_text(encoding="utf-8")
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
                r5_wrapper,
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
                    "reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1/v7-control",
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

    def test_launcher_missing_key_exits_before_any_external_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_dir = root / "bin"
            child_marker = root / "external-child-ran"
            _write_executable(
                fake_dir / "dirname",
                "#!/bin/bash -p\n"
                f"printf ran > {shlex.quote(str(child_marker))}\n"
                "exit 97\n",
            )
            result = subprocess.run(
                [str(LAUNCHER), "telemetry-before-v7"],
                cwd=REPOSITORY,
                env={
                    "PATH": f"{fake_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "NARRATORDB_PAID_PRECOMMIT_SHA256": "0" * 64,
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"runtime OpenRouter environment is missing", result.stderr)
            self.assertFalse(child_marker.exists())

    @unittest.skipUnless(
        _sealed_vendor_runtime_available(),
        "historical R5 launcher is sealed to its original macOS Python runtime",
    )
    def test_later_telemetry_preaction_runs_before_key_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bundle = root / R5_RELATIVE
            bundle.mkdir(parents=True)
            launcher = bundle / LAUNCHER.name
            shutil.copy2(LAUNCHER, launcher)
            launcher.chmod(0o755)
            _write_executable(
                bundle / "run_paid_variant_hardened.sh",
                "#!/bin/bash -p\nexit 99\n",
            )
            observation = root / "preaction-observation.json"
            target_marker = root / "credential-target-ran"
            (bundle / "verify_dynamic_admission.py").write_text(
                "import json, os\n"
                f"path = {str(observation)!r}\n"
                "document = {"
                "'key_present': 'OPENROUTER_API_KEY' in os.environ, "
                "'holder_present': 'R5_RUNTIME_OPENROUTER_KEY' in os.environ}\n"
                "open(path, 'w', encoding='utf-8').write(json.dumps(document))\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            (bundle / "execution-authorization-requirements.json").write_text(
                "{}\n", encoding="utf-8"
            )
            (bundle / "capture_provider_telemetry.py").write_text(
                f"open({str(target_marker)!r}, 'w').write('ran')\n",
                encoding="utf-8",
            )
            vendor_python = root / "vendor/memory-benchmarks/.venv/bin/python"
            vendor_python.parent.mkdir(parents=True)
            vendor_python.symlink_to(
                "/Users/william/.local/share/uv/python/"
                "cpython-3.12-macos-aarch64-none/bin/python3.12"
            )
            bound = root / "bound-input.txt"
            bound.write_text("sealed test input\n", encoding="utf-8")
            (bundle / "BOUND_INPUTS_SHA256SUMS").write_text(
                f"{_sha256(bound)}  bound-input.txt\n", encoding="utf-8"
            )
            members = sorted(
                path
                for path in bundle.iterdir()
                if path.name != "SHA256SUMS"
            )
            seal = bundle / "SHA256SUMS"
            seal.write_text(
                "".join(f"{_sha256(path)}  {path.name}\n" for path in members),
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(launcher), "telemetry-before-v13"],
                cwd=root,
                env={
                    "OPENROUTER_API_KEY": DUMMY_KEY,
                    "NARRATORDB_PAID_PRECOMMIT_SHA256": _sha256(seal),
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                json.loads(observation.read_text(encoding="utf-8")),
                {"holder_present": False, "key_present": False},
            )
            self.assertFalse(target_marker.exists())
            self.assertNotIn(DUMMY_KEY.encode(), result.stdout + result.stderr)

    @unittest.skipUnless((R5 / "SHA256SUMS").is_file(), "R5 is intentionally unsealed")
    def test_operator_protocol_suppresses_hostile_trace_verbose_and_history(self) -> None:
        document = json.loads(COMMANDS.read_text(encoding="utf-8"))
        operator = document["runtime_environment"]["operator_pty_injection_protocol"]
        dummy_secret = "sk-or-neutral-transport-test-value-1234567890"
        with tempfile.TemporaryDirectory() as temporary:
            history = Path(temporary) / "hostile-history"
            seal_initialization = operator[
                "published_seal_initialization_template_line"
            ].replace(
                operator["published_seal_initialization_placeholder"],
                _sha256(R5 / "SHA256SUMS"),
            )
            setup = [
                seal_initialization,
                f"export HISTFILE={shlex.quote(str(history))}",
                "set -x -v",
                "set -o history",
            ]
            transcript_input = "\n".join(
                [
                    *setup,
                    operator["first_write_exact_line"],
                    dummy_secret,
                    operator["third_write_exact_lines_by_action"][
                        "telemetry-before-v7"
                    ],
                ]
            ) + "\n"
            result = subprocess.run(
                operator["fresh_shell_exact_argv"],
                cwd=REPOSITORY,
                input=transcript_input.encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15,
            )
            self.assertNotEqual(result.returncode, 0)
            transcript = result.stdout + result.stderr
            self.assertNotIn(dummy_secret.encode(), transcript)
            if history.exists():
                self.assertNotIn(dummy_secret, history.read_text(encoding="utf-8"))

    @unittest.skipUnless((R5 / "SHA256SUMS").is_file(), "R5 is intentionally unsealed")
    def test_clean_shell_seal_initialization_accepts_only_exact_physical_hash(self) -> None:
        document = json.loads(COMMANDS.read_text(encoding="utf-8"))
        operator = document["runtime_environment"]["operator_pty_injection_protocol"]
        template = operator["published_seal_initialization_template_line"]
        placeholder = operator["published_seal_initialization_placeholder"]
        physical = _sha256(R5 / "SHA256SUMS")
        cases = [
            (physical, 0),
            ("0" * 63, 95),
            ("g" + "0" * 63, 95),
        ]
        for value, expected in cases:
            with self.subTest(value_shape=(len(value), value[:1])):
                result = subprocess.run(
                    ["/bin/bash", "--noprofile", "--norc", "-p", "-c", template.replace(placeholder, value)],
                    cwd=REPOSITORY,
                    env={
                        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                        "HOME": "/tmp",
                        "TMPDIR": "/tmp",
                        "LANG": "C",
                        "LC_ALL": "C",
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, expected)

    @unittest.skipUnless(
        _sealed_vendor_runtime_available(),
        "historical R5 launcher is sealed to its original macOS Python runtime",
    )
    def test_temp_sealed_launcher_handoff_keeps_key_out_of_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bundle = root / R5_RELATIVE
            bundle.mkdir(parents=True)
            launcher = bundle / LAUNCHER.name
            shutil.copy2(LAUNCHER, launcher)
            launcher.chmod(0o755)

            observation = root / "target-observation.json"
            startup_marker = root / "bash-env-ran"
            vendor_python = root / "vendor/memory-benchmarks/.venv/bin/python"
            vendor_python.parent.mkdir(parents=True)
            vendor_python.symlink_to(
                "/Users/william/.local/share/uv/python/"
                "cpython-3.12-macos-aarch64-none/bin/python3.12"
            )
            (bundle / "capture_provider_telemetry.py").write_text(
                "import json, os, sys, time\n"
                f"path = {str(observation)!r}\n"
                "key = os.environ.get('OPENROUTER_API_KEY')\n"
                "document = {"
                "'argv_contains_key': bool(key and key in sys.argv), "
                "'holder_present': 'R5_RUNTIME_OPENROUTER_KEY' in os.environ, "
                "'key_present': key is not None}\n"
                "open(path, 'w', encoding='utf-8').write(json.dumps(document))\n"
                "time.sleep(5)\n",
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
                "R5_RUNTIME_OPENROUTER_KEY": "hostile-exported-holder",
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
