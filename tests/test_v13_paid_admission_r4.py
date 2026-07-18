from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
VERIFIER_PATH = (
    REPOSITORY
    / "benchmark_records/reproduction-v13-paid-paired-scoring-r4-20260716/verify_dynamic_admission.py"
)
SPEC = importlib.util.spec_from_file_location("v13_paid_admission", VERIFIER_PATH)
assert SPEC and SPEC.loader
admission = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admission)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _inventory_digest(root: Path) -> tuple[int, int, int, str]:
    files: list[Path] = []
    directories = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise AssertionError(f"unexpected symlink: {path}")
        if path.is_dir():
            directories += 1
        elif path.is_file():
            files.append(path)
        else:
            raise AssertionError(f"unexpected special entry: {path}")
    records: list[bytes] = [
        f"D {relative}\n".encode()
        for relative in sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_dir()
        )
    ]
    total = 0
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        payload = path.read_bytes()
        total += len(payload)
        relative = path.relative_to(root).as_posix()
        records.append(
            f"F {hashlib.sha256(payload).hexdigest()} {len(payload)} {relative}\n".encode()
        )
    return len(files), directories, total, hashlib.sha256(b"".join(records)).hexdigest()


_INVENTORY_CACHE: dict[Path, tuple[int, int, int, str]] = {}


def _cached_inventory(root: Path) -> tuple[int, int, int, str]:
    resolved = root.resolve(strict=True)
    if resolved not in _INVENTORY_CACHE:
        _INVENTORY_CACHE[resolved] = _inventory_digest(resolved)
    return _INVENTORY_CACHE[resolved]


class DynamicAdmissionMutationTests(unittest.TestCase):
    NOW = datetime(2026, 7, 16, 6, 46, 0, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.revision = self.root / "revision"
        self.revision.mkdir()

        self.predecessor_manifest = self.root / "predecessor/SHA256SUMS"
        self.predecessor_manifest.parent.mkdir()
        self.predecessor_manifest.write_text("predecessor seal\n", encoding="utf-8")
        self.revision_manifest = self.revision / "SHA256SUMS"
        self.revision_manifest.write_text("replacement seal\n", encoding="utf-8")
        self.protocol = self.revision / "protocol.json"
        self.protocol.write_text("{}\n", encoding="utf-8")
        self.ledger_verifier = self.revision / "verify_evaluator_ledger.py"
        shutil.copy2(
            REPOSITORY
            / "benchmark_records/reproduction-v13-paid-paired-scoring-r4-20260716/verify_evaluator_ledger.py",
            self.ledger_verifier,
        )
        self.telemetry_capture = self.revision / "capture_provider_telemetry.py"
        shutil.copy2(
            REPOSITORY
            / "benchmark_records/reproduction-v13-paid-paired-scoring-r4-20260716/capture_provider_telemetry.py",
            self.telemetry_capture,
        )
        self.fx_capture = self.revision / "capture_ecb_fx.py"
        shutil.copy2(
            REPOSITORY
            / "benchmark_records/reproduction-v13-paid-paired-scoring-r4-20260716/capture_ecb_fx.py",
            self.fx_capture,
        )
        self.credential_launcher = self.revision / "launch_with_openrouter_key.sh"
        shutil.copy2(
            REPOSITORY
            / "benchmark_records/reproduction-v13-paid-paired-scoring-r4-20260716/launch_with_openrouter_key.sh",
            self.credential_launcher,
        )
        self.paid_wrapper = self.revision / "run_paid_variant_hardened.sh"
        shutil.copy2(
            REPOSITORY
            / "benchmark_records/reproduction-v13-paid-paired-scoring-r4-20260716/run_paid_variant_hardened.sh",
            self.paid_wrapper,
        )
        self.harness_guard = self.revision / "run_harness_guarded.py"
        shutil.copy2(
            REPOSITORY
            / "benchmark_records/reproduction-v13-paid-paired-scoring-r4-20260716/run_harness_guarded.py",
            self.harness_guard,
        )

        self.runtime = self.root / "runtime-source"
        self.budget_auditor = self.runtime / "narratordb/benchmarks/budget_audit.py"
        self.budget_auditor_history = self.runtime / "narratordb/benchmarks/history.py"
        self.budget_auditor.parent.mkdir(parents=True)
        shutil.copy2(
            REPOSITORY / "narratordb/benchmarks/budget_audit.py",
            self.budget_auditor,
        )
        shutil.copy2(
            REPOSITORY / "narratordb/benchmarks/history.py",
            self.budget_auditor_history,
        )

        self.runtime_sources: dict[str, dict] = {}
        runtime_definitions = {
            "v11-source": (
                "tar.gz",
                {
                    "narratordb/__init__.py": b"",
                    "narratordb/benchmarks/openrouter_proxy.py": b"# sealed proxy\n",
                    "narratordb/benchmarks/evaluation_audit.py": (
                        REPOSITORY / "narratordb/benchmarks/evaluation_audit.py"
                    ).read_bytes(),
                },
            ),
            "harness-source": (
                "tar",
                {
                    "benchmarks/__init__.py": b"",
                    "benchmarks/common/llm_client.py": b"# sealed client\n",
                    "benchmarks/longmemeval/run.py": b"# sealed evaluator\n",
                },
            ),
        }
        for label, (archive_format, members) in runtime_definitions.items():
            extracted = self.root / f"attempt1/tools/{label}"
            for relative, payload in members.items():
                destination = extracted / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
            suffix = ".tar.gz" if archive_format == "tar.gz" else ".tar"
            archive = self.root / f"archives/{label}{suffix}"
            archive.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive, "w:gz" if archive_format == "tar.gz" else "w") as output:
                for path in sorted(extracted.rglob("*")):
                    output.add(
                        path,
                        arcname=path.relative_to(extracted).as_posix(),
                        recursive=False,
                    )
            for path in sorted(extracted.rglob("*"), reverse=True):
                path.chmod(0o555 if path.is_dir() else 0o444)
            extracted.chmod(0o555)
            self.runtime_sources[label] = {
                "archive_path": str(archive.relative_to(self.root)),
                "archive_sha256": _sha(archive),
                "archive_format": archive_format,
                "extracted_root": str(extracted.relative_to(self.root)),
                "read_only_before_execution": True,
            }

        self.vendor_python = self.root / "vendor/harness/.venv/bin/python"
        self.vendor_python.parent.mkdir(parents=True)
        self.vendor_python.symlink_to(Path(os.sys.executable).resolve())
        self.vendor_pyvenv = self.vendor_python.parent.parent / "pyvenv.cfg"
        self.vendor_pyvenv.write_text(
            f"home = {Path(os.sys.executable).resolve().parent}\n"
            "include-system-site-packages = false\n"
            f"version = {os.sys.version_info.major}.{os.sys.version_info.minor}\n",
            encoding="utf-8",
        )
        probe = subprocess.run(
            [
                str(self.vendor_python),
                "-I",
                "-S",
                "-B",
                "-c",
                (
                    "import json,sys,sysconfig;"
                    "print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,"
                    "'base_prefix':sys.base_prefix,'stdlib':sysconfig.get_path('stdlib')}))"
                ),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        runtime = json.loads(probe.stdout)
        stdlib = Path(runtime["stdlib"]).resolve(strict=True)
        stdlib_files, stdlib_directories, stdlib_bytes, stdlib_sha = _cached_inventory(
            stdlib
        )
        self.vendor_site = self.root / "vendor/harness/.venv/lib/python3.12/site-packages"
        (self.vendor_site / "example_package").mkdir(parents=True)
        (self.vendor_site / "example_package/__init__.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        (self.vendor_site / "example-1.0.dist-info").mkdir()
        (self.vendor_site / "example-1.0.dist-info/METADATA").write_text(
            "Name: example\nVersion: 1.0\n", encoding="utf-8"
        )
        vendor_files, vendor_directories, vendor_bytes, vendor_sha = _inventory_digest(
            self.vendor_site
        )
        self.vendor_execution_site = self.root / "attempt1/tools/harness-site-packages"
        shutil.copytree(self.vendor_site, self.vendor_execution_site)
        for path in sorted(self.vendor_execution_site.rglob("*"), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        self.vendor_execution_site.chmod(0o555)
        self.vendor_environment = {
            "python_executable_path": str(self.vendor_python.relative_to(self.root)),
            "python_executable_symlink_target": os.readlink(self.vendor_python),
            "python_executable_resolved_path": str(self.vendor_python.resolve(strict=True)),
            "python_executable_sha256": _sha(self.vendor_python.resolve(strict=True)),
            "python_version": ".".join(map(str, os.sys.version_info[:3])),
            "python_cache_tag": os.sys.implementation.cache_tag,
            "python_prefix": runtime["prefix"],
            "python_base_prefix": runtime["base_prefix"],
            "pyvenv_config_path": str(self.vendor_pyvenv.relative_to(self.root)),
            "pyvenv_config_sha256": _sha(self.vendor_pyvenv),
            "stdlib_path": str(stdlib),
            "stdlib_file_count": stdlib_files,
            "stdlib_directory_count": stdlib_directories,
            "stdlib_total_bytes": stdlib_bytes,
            "stdlib_inventory_sha256": stdlib_sha,
            "source_site_packages_path": str(self.vendor_site.relative_to(self.root)),
            "execution_site_packages_path": str(
                self.vendor_execution_site.relative_to(self.root)
            ),
            "site_packages_file_count": vendor_files,
            "site_packages_directory_count": vendor_directories,
            "site_packages_total_bytes": vendor_bytes,
            "site_packages_inventory_sha256": vendor_sha,
            "read_only_before_execution": True,
        }

        self.dataset = self.root / "dataset.json"
        self.dataset.write_text("[]\n", encoding="utf-8")
        self.question_ids = self.root / "question-ids.json"
        question_ids = [f"q{number:02d}" for number in range(42)]
        self.question_ids.write_text(json.dumps(question_ids) + "\n", encoding="utf-8")

        self.variants: dict[str, dict] = {}
        for label, project, initial in (
            ("v7-control", "project-v7", b"\n"),
            ("v13-first", "project-v13", b"\n\n"),
        ):
            staged = self.root / f"staged/{label}/predicted_{project}"
            evaluated = self.root / f"attempt1/{label}/evaluation/official-harness/predicted_{project}"
            staged.mkdir(parents=True)
            evaluated.mkdir(parents=True)
            entries = []
            for question_id in question_ids:
                payload = (
                    json.dumps(
                        {"id": question_id, "question_id": question_id, "question_type": "all"},
                        sort_keys=True,
                    )
                    + "\n"
                ).encode()
                staged_file = staged / f"{question_id}.json"
                evaluated_file = evaluated / f"{question_id}.json"
                staged_file.write_bytes(payload)
                evaluated_file.write_bytes(payload)
                entries.append(
                    {
                        "path": f"{question_id}.json",
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            checks = {
                "fresh_output_root": True,
                "question_scope_exact": True,
                "source_stable_during_copy": True,
                "copy_byte_identical": True,
                "prediction_payloads_parsed": False,
            }
            staged_manifest = staged.parent / "frozen-copy-manifest.json"
            _write_json(
                staged_manifest,
                {
                    "schema_version": "narratordb.paired-evaluation-copy.v1",
                    "project_name": project,
                    "expected_questions": 42,
                    "question_id_file": "/sealed/source/question-ids.json",
                    "question_ids_sha256": _sha(self.question_ids),
                    "frozen_directory": f"/sealed/upstream/predicted_{project}",
                    "evaluated_directory": (
                        "/sealed/precommit/"
                        + staged.relative_to(self.root).as_posix()
                    ),
                    "file_count": len(entries),
                    "prediction_file_count": 42,
                    "files": entries,
                    "checks": checks,
                },
            )
            manifest = evaluated.parent / "frozen-copy-manifest.json"
            _write_json(
                manifest,
                {
                    "schema_version": "narratordb.paired-evaluation-copy.v1",
                    "project_name": project,
                    "expected_questions": 42,
                    "question_id_file": str(self.question_ids),
                    "question_ids_sha256": _sha(self.question_ids),
                    "frozen_directory": str(staged),
                    "evaluated_directory": str(evaluated),
                    "file_count": len(entries),
                    "prediction_file_count": 42,
                    "files": entries,
                    "checks": checks,
                },
            )
            ledger = self.root / f"attempt1/{label}/evaluation/openrouter-usage.jsonl"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_bytes(initial)
            self.variants[label] = {
                "label": label,
                "run_root": f"attempt1/{label}",
                "project_name": project,
                "dataset_path": "dataset.json",
                "dataset_sha256": _sha(self.dataset),
                "working_copy_manifest_path": str(manifest.relative_to(self.root)),
                "staged_copy_manifest_path": str(
                    staged_manifest.relative_to(self.root)
                ),
                "staged_copy_manifest_sha256": _sha(staged_manifest),
                "staged_prediction_directory": str(staged.relative_to(self.root)),
                "question_ids_sha256": _sha(self.question_ids),
                "ledger_path": str(ledger.relative_to(self.root)),
                "initial_ledger_sha256": hashlib.sha256(initial).hexdigest(),
                "initial_ledger_bytes_hex": initial.hex(),
                "soft_fuse_usd": "1.25",
            }

        self.baseline_record = self.root / "budgets/baseline-audit.json"
        _write_json(self.baseline_record, {"schema": "immutable-baseline"})
        self.declaration = self.root / "budgets/campaign.json"
        evaluator_policy = {
            "request_models": [
                "deepseek/deepseek-v4-flash-20260423",
                "z-ai/glm-5.2",
            ],
            "response_models": [
                "deepseek/deepseek-v4-flash",
                "deepseek/deepseek-v4-flash-20260423",
                "z-ai/glm-5.2",
            ],
            "providers": ["AtlasCloud", "Baidu", "DeepInfra", "GMICloud", "StreamLake"],
        }
        _write_json(
            self.declaration,
            {
                "schema": "narratordb.campaign-budget-declaration.v2",
                "campaign_id": "mutation-test",
                "provider_cap_usd": "250",
                "governance_ceiling_eur": "300",
                "prior_immutable_costs": [
                    {
                        "source_id": "baseline",
                        "record_path": "baseline-audit.json",
                        "record_sha256": _sha(self.baseline_record),
                        "cost_usd": "108.31010877191",
                    }
                ],
                "active_usage_ledgers": [
                    {
                        "source_id": f"{label}-ledger",
                        "kind": "evaluator",
                        "path": f"../{variant['ledger_path']}",
                        "identity_policy": evaluator_policy,
                    }
                    for label, variant in self.variants.items()
                ],
            },
        )

        self.provider_path = self.root / "attempt1/precall/provider.json"
        self._write_provider()
        self.fx_raw = self.root / "attempt1/precall/ecb.xml"
        self.fx_raw.parent.mkdir(parents=True, exist_ok=True)
        self.fx_raw.write_bytes(
            b"<?xml version='1.0' encoding='UTF-8'?><Envelope><Cube><Cube time='2026-07-15'><Cube currency='USD' rate='1.1406'/></Cube></Cube></Envelope>"
        )
        self.fx_metadata = self.root / "attempt1/precall/ecb.json"
        self._write_fx_metadata()

        self.campaign_audit = self.root / "attempt1/precall/campaign-audit.json"
        self.authorization_path = self.root / "attempt1/precall/authorization.json"
        self.audit_path = self.root / "attempt1/precall/authorization.audit.json"
        self.requirements_path = self.revision / "requirements.json"
        self._write_requirements()
        self.published = _sha(self.revision_manifest)
        requirements, _ = admission._requirements(self.root, self.requirements_path)
        _, campaign_payload = admission._current_campaign_audit(self.root, requirements)
        self.campaign_audit.parent.mkdir(parents=True, exist_ok=True)
        self.campaign_audit.write_bytes(campaign_payload)
        authorization = admission._authorization_document(
            self.root,
            self.requirements_path,
            phase_name="before-v7",
            run_root=self.variants["v7-control"]["run_root"],
            project_name=self.variants["v7-control"]["project_name"],
            dataset_path="dataset.json",
            published_precommit_sha256=self.published,
            created_at=self.NOW,
        )
        _write_json(self.authorization_path, authorization)
        audit = admission._build_audit_document(
            self.root,
            self.requirements_path,
            self.authorization_path,
            phase_name="before-v7",
            run_root=self.variants["v7-control"]["run_root"],
            project_name=self.variants["v7-control"]["project_name"],
            dataset_path="dataset.json",
            published_precommit_sha256=self.published,
            reviewed_at=self.NOW,
        )
        _write_json(self.audit_path, audit)

    def tearDown(self) -> None:
        for path in [self.root, *self.root.rglob("*")]:
            if not path.is_symlink():
                try:
                    path.chmod(path.stat().st_mode | stat.S_IWUSR)
                except FileNotFoundError:
                    pass
        self.temporary.cleanup()

    def _write_provider(
        self,
        *,
        observed: str = "2026-07-16T06:45:00Z",
        usage: str = "121.362526048",
        remaining: str = "128.637473952",
        extra: dict | None = None,
    ) -> None:
        document = {
            "schema_version": "narratordb.provider-key-telemetry.v2",
            "observed_at_utc": observed,
            "source_endpoint": "https://openrouter.ai/api/v1/key",
            "request_class": "authenticated content-free account telemetry",
            "http_status": 200,
            "currency": "USD",
            "provider_limit_usd": "250",
            "provider_usage_usd": usage,
            "provider_remaining_usd": remaining,
            "capture_tool_sha256": _sha(self.telemetry_capture),
            "credential_recorded": False,
            "key_label_recorded": False,
            "account_identifier_recorded": False,
            "model_content_recorded": False,
        }
        document.update(extra or {})
        _write_json(self.provider_path, document)

    def _write_fx_metadata(self, **changes: object) -> None:
        document = {
            "schema_version": "narratordb.ecb-usd-eur-observation.v1",
            "publisher": "European Central Bank",
            "source_url": "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
            "http_status": 200,
            "retrieved_at_utc": "2026-07-16T06:45:00Z",
            "raw_xml_path": str(self.fx_raw.relative_to(self.root)),
            "raw_xml_bytes": len(self.fx_raw.read_bytes()),
            "raw_xml_sha256": _sha(self.fx_raw),
            "reference_date": "2026-07-15",
            "base_currency": "EUR",
            "quote_currency": "USD",
            "usd_per_eur": "1.1406",
            "parser_sha256": _sha(VERIFIER_PATH),
            "credential_recorded": False,
            "model_content_recorded": False,
        }
        document.update(changes)
        _write_json(self.fx_metadata, document)

    def _write_requirements(self) -> None:
        document = {
            "schema_version": "narratordb.v13-paid-dynamic-admission-requirements.v3",
            "predecessor": {
                "manifest_path": str(self.predecessor_manifest.relative_to(self.root)),
                "manifest_sha256": _sha(self.predecessor_manifest),
            },
            "revision": {
                "manifest_path": str(self.revision_manifest.relative_to(self.root)),
                "protocol_path": str(self.protocol.relative_to(self.root)),
                "protocol_sha256": _sha(self.protocol),
                "ledger_verifier_path": str(self.ledger_verifier.relative_to(self.root)),
                "ledger_verifier_sha256": _sha(self.ledger_verifier),
                "telemetry_capture_path": str(self.telemetry_capture.relative_to(self.root)),
                "telemetry_capture_sha256": _sha(self.telemetry_capture),
                "fx_capture_path": str(self.fx_capture.relative_to(self.root)),
                "fx_capture_sha256": _sha(self.fx_capture),
                "credential_launcher_path": str(
                    self.credential_launcher.relative_to(self.root)
                ),
                "credential_launcher_sha256": _sha(self.credential_launcher),
                "paid_wrapper_path": str(self.paid_wrapper.relative_to(self.root)),
                "paid_wrapper_sha256": _sha(self.paid_wrapper),
                "harness_guard_path": str(self.harness_guard.relative_to(self.root)),
                "harness_guard_sha256": _sha(self.harness_guard),
            },
            "campaign": {
                "declaration_path": str(self.declaration.relative_to(self.root)),
                "declaration_sha256": _sha(self.declaration),
                "frozen_runtime_source": str(self.runtime.relative_to(self.root)),
                "budget_auditor_path": str(self.budget_auditor.relative_to(self.root)),
                "budget_auditor_sha256": _sha(self.budget_auditor),
                "budget_auditor_history_path": str(
                    self.budget_auditor_history.relative_to(self.root)
                ),
                "budget_auditor_history_sha256": _sha(self.budget_auditor_history),
                "baseline_observed_usd": "108.31010877191",
                "provider_cap_usd": "250",
                "governance_ceiling_eur": "300",
            },
            "provider": {
                "endpoint": "https://openrouter.ai/api/v1/key",
                "currency": "USD",
                "provider_limit_usd": "250",
                "historical_usage_floor_usd": "121.362526048",
                "maximum_age_seconds": 900,
                "arithmetic_tolerance_usd": "0.000000001",
            },
            "fx": {
                "source_url": "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
                "publisher": "European Central Bank",
                "maximum_capture_age_seconds": 900,
                "maximum_reference_age_days": 7,
                "buffer_basis_points": 1000,
                "governance_ceiling_eur": "300",
                "rounding": "ROUND_CEILING_TO_0.01_EUR",
            },
            "runtime_sources": self.runtime_sources,
            "vendor_environment": self.vendor_environment,
            "variants": list(self.variants.values()),
            "phases": {
                "before-v7": {
                    "variant": "v7-control",
                    "unspent_fuses_usd": "2.50",
                    "campaign_audit_path": str(self.campaign_audit.relative_to(self.root)),
                    "provider_telemetry_path": str(self.provider_path.relative_to(self.root)),
                    "fx_metadata_path": str(self.fx_metadata.relative_to(self.root)),
                    "fx_raw_xml_path": str(self.fx_raw.relative_to(self.root)),
                    "authorization_path": str(self.authorization_path.relative_to(self.root)),
                    "independent_audit_path": str(self.audit_path.relative_to(self.root)),
                    "prior_ledger_identity_audit_path": None,
                    "prior_provider_telemetry_path": None,
                },
                "before-v13": {
                    "variant": "v13-first",
                    "unspent_fuses_usd": "1.25",
                    "campaign_audit_path": "attempt1/between/campaign.json",
                    "provider_telemetry_path": "attempt1/between/provider.json",
                    "fx_metadata_path": "attempt1/between/ecb.json",
                    "fx_raw_xml_path": "attempt1/between/ecb.xml",
                    "authorization_path": "attempt1/between/authorization.json",
                    "independent_audit_path": "attempt1/between/authorization.audit.json",
                    "prior_ledger_identity_audit_path": "attempt1/v7-control/evaluation/ledger-audit.json",
                    "prior_provider_telemetry_path": str(self.provider_path.relative_to(self.root)),
                },
            },
            "finalization": {
                "campaign_audit_path": "attempt1/postrun/campaign.json",
                "provider_telemetry_path": "attempt1/postrun/provider.json",
                "fx_metadata_path": "attempt1/postrun/ecb.json",
                "fx_raw_xml_path": "attempt1/postrun/ecb.xml",
                "before_v7_provider_telemetry_path": str(self.provider_path.relative_to(self.root)),
                "v7_ledger_identity_audit_path": "attempt1/v7-control/evaluation/ledger-audit.json",
                "v13_ledger_identity_audit_path": "attempt1/v13-first/evaluation/ledger-audit.json",
                "before_v7_authorization_path": str(self.authorization_path.relative_to(self.root)),
                "before_v7_authorization_audit_path": str(self.audit_path.relative_to(self.root)),
                "before_v13_authorization_path": "attempt1/between/authorization.json",
                "before_v13_authorization_audit_path": "attempt1/between/authorization.audit.json",
                "authorization_path": "attempt1/postrun/final.json",
                "independent_audit_path": "attempt1/postrun/final.audit.json",
                "v7_evaluation_audit_path": "attempt1/v7-control/evaluation-audit.json",
                "v13_evaluation_audit_path": "attempt1/v13-first/evaluation-audit.json",
                "paired_result_path": "attempt1/paired-result.json",
            },
        }
        _write_json(self.requirements_path, document)

    def _verify(self, **overrides: str) -> dict:
        arguments = {
            "phase_name": "before-v7",
            "run_root": self.variants["v7-control"]["run_root"],
            "project_name": self.variants["v7-control"]["project_name"],
            "dataset_path": "dataset.json",
            "published_precommit_sha256": self.published,
            "now": self.NOW,
        }
        arguments.update(overrides)
        return admission.verify_admission(self.root, self.requirements_path, **arguments)

    def _rewrite_authorizations(self) -> None:
        self.authorization_path.unlink()
        self.audit_path.unlink()
        authorization = admission._authorization_document(
            self.root,
            self.requirements_path,
            phase_name="before-v7",
            run_root=self.variants["v7-control"]["run_root"],
            project_name=self.variants["v7-control"]["project_name"],
            dataset_path="dataset.json",
            published_precommit_sha256=self.published,
            created_at=self.NOW,
        )
        _write_json(self.authorization_path, authorization)
        audit = admission._build_audit_document(
            self.root,
            self.requirements_path,
            self.authorization_path,
            phase_name="before-v7",
            run_root=self.variants["v7-control"]["run_root"],
            project_name=self.variants["v7-control"]["project_name"],
            dataset_path="dataset.json",
            published_precommit_sha256=self.published,
            reviewed_at=self.NOW,
        )
        _write_json(self.audit_path, audit)

    def _prepare_finalization(self) -> tuple[Path, Path, Path]:
        def completion(request_model: str, response_model: str, cost: float) -> dict:
            return {
                "timestamp": "2026-07-16T06:48:00Z",
                "event": "completion",
                "status": 200,
                "request_model": request_model,
                "response_model": response_model,
                "provider": "DeepInfra",
                "finish_reason": "stop",
                "response_complete": True,
                "prompt_tokens": 10,
                "cached_tokens": 0,
                "completion_tokens": 2,
                "reasoning_tokens": 0,
                "cost_usd": cost,
                "unknown_cost": False,
            }

        costs = {
            "v7-control": (0.3, 0.1),
            "v13-first": (0.35, 0.15),
        }
        for label, (answerer_cost, judge_cost) in costs.items():
            ledger = self.root / self.variants[label]["ledger_path"]
            with ledger.open("a", encoding="utf-8") as output:
                output.write(
                    json.dumps(completion("z-ai/glm-5.2", "z-ai/glm-5.2", answerer_cost), sort_keys=True)
                    + "\n"
                )
                output.write(
                    json.dumps(
                        completion(
                            "deepseek/deepseek-v4-flash-20260423",
                            "deepseek/deepseek-v4-flash",
                            judge_cost,
                        ),
                        sort_keys=True,
                    )
                    + "\n"
                )
            identity_path = self.root / f"attempt1/{label}/evaluation/ledger-audit.json"
            result = subprocess.run(
                [
                    os.sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    str(self.ledger_verifier),
                    "--ledger",
                    str(ledger),
                ],
                cwd=self.root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            identity_path.write_bytes(result.stdout)
            if label == "v7-control":
                between = self.root / "attempt1/between"
                between.mkdir(parents=True, exist_ok=True)
                requirements, _ = admission._requirements(
                    self.root, self.requirements_path
                )
                _, campaign_payload = admission._current_campaign_audit(
                    self.root, requirements
                )
                (between / "campaign.json").write_bytes(campaign_payload)
                _write_json(
                    between / "provider.json",
                    {
                        "schema_version": "narratordb.provider-key-telemetry.v2",
                        "observed_at_utc": "2026-07-16T06:48:00Z",
                        "source_endpoint": "https://openrouter.ai/api/v1/key",
                        "request_class": "authenticated content-free account telemetry",
                        "http_status": 200,
                        "currency": "USD",
                        "provider_limit_usd": "250",
                        "provider_usage_usd": "121.762526048",
                        "provider_remaining_usd": "128.237473952",
                        "capture_tool_sha256": _sha(self.telemetry_capture),
                        "credential_recorded": False,
                        "key_label_recorded": False,
                        "account_identifier_recorded": False,
                        "model_content_recorded": False,
                    },
                )
                between_fx_raw = between / "ecb.xml"
                between_fx_raw.write_bytes(self.fx_raw.read_bytes())
                _write_json(
                    between / "ecb.json",
                    {
                        "schema_version": "narratordb.ecb-usd-eur-observation.v1",
                        "publisher": "European Central Bank",
                        "source_url": "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
                        "http_status": 200,
                        "retrieved_at_utc": "2026-07-16T06:48:00Z",
                        "raw_xml_path": str(between_fx_raw.relative_to(self.root)),
                        "raw_xml_bytes": len(between_fx_raw.read_bytes()),
                        "raw_xml_sha256": _sha(between_fx_raw),
                        "reference_date": "2026-07-15",
                        "base_currency": "EUR",
                        "quote_currency": "USD",
                        "usd_per_eur": "1.1406",
                        "parser_sha256": _sha(VERIFIER_PATH),
                        "credential_recorded": False,
                        "model_content_recorded": False,
                    },
                )
                between_now = datetime(2026, 7, 16, 6, 49, 0, tzinfo=timezone.utc)
                between_auth = between / "authorization.json"
                between_document = admission._authorization_document(
                    self.root,
                    self.requirements_path,
                    phase_name="before-v13",
                    run_root=self.variants["v13-first"]["run_root"],
                    project_name=self.variants["v13-first"]["project_name"],
                    dataset_path="dataset.json",
                    published_precommit_sha256=self.published,
                    created_at=between_now,
                )
                _write_json(between_auth, between_document)
                between_audit = admission._build_audit_document(
                    self.root,
                    self.requirements_path,
                    between_auth,
                    phase_name="before-v13",
                    run_root=self.variants["v13-first"]["run_root"],
                    project_name=self.variants["v13-first"]["project_name"],
                    dataset_path="dataset.json",
                    published_precommit_sha256=self.published,
                    reviewed_at=between_now,
                )
                _write_json(between / "authorization.audit.json", between_audit)

        final_campaign = self.root / "attempt1/postrun/campaign.json"
        final_campaign.parent.mkdir(parents=True, exist_ok=True)
        requirements, _ = admission._requirements(self.root, self.requirements_path)
        _, campaign_payload = admission._current_campaign_audit(self.root, requirements)
        final_campaign.write_bytes(campaign_payload)

        final_provider = self.root / "attempt1/postrun/provider.json"
        _write_json(
            final_provider,
            {
                "schema_version": "narratordb.provider-key-telemetry.v2",
                "observed_at_utc": "2026-07-16T06:50:00Z",
                "source_endpoint": "https://openrouter.ai/api/v1/key",
                "request_class": "authenticated content-free account telemetry",
                "http_status": 200,
                "currency": "USD",
                "provider_limit_usd": "250",
                "provider_usage_usd": "122.262526048",
                "provider_remaining_usd": "127.737473952",
                "capture_tool_sha256": _sha(self.telemetry_capture),
                "credential_recorded": False,
                "key_label_recorded": False,
                "account_identifier_recorded": False,
                "model_content_recorded": False,
            },
        )
        final_fx_raw = self.root / "attempt1/postrun/ecb.xml"
        final_fx_raw.write_bytes(self.fx_raw.read_bytes())
        final_fx = self.root / "attempt1/postrun/ecb.json"
        _write_json(
            final_fx,
            {
                "schema_version": "narratordb.ecb-usd-eur-observation.v1",
                "publisher": "European Central Bank",
                "source_url": "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
                "http_status": 200,
                "retrieved_at_utc": "2026-07-16T06:50:00Z",
                "raw_xml_path": str(final_fx_raw.relative_to(self.root)),
                "raw_xml_bytes": len(final_fx_raw.read_bytes()),
                "raw_xml_sha256": _sha(final_fx_raw),
                "reference_date": "2026-07-15",
                "base_currency": "EUR",
                "quote_currency": "USD",
                "usd_per_eur": "1.1406",
                "parser_sha256": _sha(VERIFIER_PATH),
                "credential_recorded": False,
                "model_content_recorded": False,
            },
        )

        final_auth = self.root / "attempt1/postrun/final.json"
        final_audit = self.root / "attempt1/postrun/final.audit.json"
        final_now = datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc)
        document = admission._finalization_document(
            self.root,
            self.requirements_path,
            published_precommit_sha256=self.published,
            created_at=final_now,
        )
        _write_json(final_auth, document)
        audit = admission._finalization_audit_document(
            self.root,
            self.requirements_path,
            final_auth,
            published_precommit_sha256=self.published,
            reviewed_at=final_now,
        )
        _write_json(final_audit, audit)

        for label, scores in {
            "v7-control": (36, 38),
            "v13-first": (37, 39),
        }.items():
            variant = self.variants[label]
            evaluated = (
                self.root
                / variant["run_root"]
                / f"evaluation/official-harness/predicted_{variant['project_name']}"
            )
            for index, prediction in enumerate(sorted(evaluated.glob("q*.json"))):
                document = json.loads(prediction.read_text(encoding="utf-8"))
                document["cutoff_results"] = {}
                for cutoff, correct in zip(("top_20", "top_50"), scores, strict=True):
                    passed = index < correct
                    document["cutoff_results"][cutoff] = {
                        "generated_answer": "synthetic test answer",
                        "judge_raw": "synthetic test judgment",
                        "judgment": "PASS" if passed else "FAIL",
                        "score": 1.0 if passed else 0.0,
                    }
                _write_json(prediction, document)
                prediction.chmod(0o444)
            evaluated.chmod(0o555)
            evaluator_log = self.root / f"attempt1/{label}/evaluation/evaluate.log"
            evaluator_log.write_text("evaluation complete\n", encoding="utf-8")
            evaluator_log.chmod(0o444)
            status = self.root / f"attempt1/{label}/evaluation/attempt-status.json"
            _write_json(
                status,
                {
                    "evaluator_status": "0",
                    "exit_status": 0,
                    "final_status": "completed",
                    "phase": "before-v7" if label == "v7-control" else "before-v13",
                    "project": variant["project_name"],
                    "schema_version": "narratordb.v13-paid-variant-attempt-status.v2",
                },
            )
            status.chmod(0o444)
            ledger = self.root / variant["ledger_path"]
            ledger.chmod(0o444)
            evaluation_path = self.root / f"attempt1/{label}/evaluation-audit.json"
            auditor = (
                self.root
                / self.runtime_sources["v11-source"]["extracted_root"]
                / "narratordb/benchmarks/evaluation_audit.py"
            )
            result = subprocess.run(
                [
                    os.sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    str(auditor),
                    "--evaluated-directory",
                    str(evaluated),
                    "--frozen-directory",
                    str(self.root / variant["staged_prediction_directory"]),
                    "--usage-log",
                    str(ledger),
                    "--evaluator-log",
                    str(evaluator_log),
                    "--expected-questions",
                    "42",
                    "--cutoffs",
                    "20,50",
                    "--question-id-file",
                    str(self.question_ids),
                    "--require-complete",
                    "--require-official-score-complete",
                ],
                cwd=self.root,
                env={"LANG": "C", "LC_ALL": "C"},
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            evaluation_path.write_bytes(result.stdout)
            evaluation_path.chmod(0o444)
        return final_auth, final_audit, final_provider

    def test_valid_closed_world_bundle_passes(self) -> None:
        report = self._verify()
        self.assertTrue(report["ok"])
        self.assertEqual(report["phase"], "before-v7")
        self.assertEqual(report["campaign_projected_usd"], "110.81010877191")
        self.assertEqual(report["projected_eur_ceil_cent"], "119.46")

    def test_campaign_audit_builder_is_isolated_and_immutable(self) -> None:
        self.campaign_audit.unlink()
        hostile = self.root / "hostile-pythonpath"
        hostile.mkdir()
        marker = self.root / "startup-hook-loaded"
        hook = f"open({str(marker)!r}, 'w').write('loaded')\nraise SystemExit('hook')\n"
        (hostile / "sitecustomize.py").write_text(hook, encoding="utf-8")
        (hostile / "usercustomize.py").write_text(hook, encoding="utf-8")
        startup = hostile / "startup.py"
        startup.write_text(hook, encoding="utf-8")
        hostile_history = hostile / "narratordb/benchmarks/history.py"
        hostile_history.parent.mkdir(parents=True)
        hostile_history.write_text(hook, encoding="utf-8")
        before_caches = {
            path.relative_to(self.runtime)
            for path in self.runtime.rglob("__pycache__")
        }
        command = [
            os.sys.executable,
            "-I",
            "-S",
            "-B",
            str(VERIFIER_PATH),
            "build-campaign-audit",
            "--repository-root",
            str(self.root),
            "--requirements",
            str(self.requirements_path),
            "--output",
            str(self.campaign_audit.relative_to(self.root)),
        ]
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(hostile),
            "PYTHONSTARTUP": str(startup),
        }
        result = subprocess.run(
            command,
            cwd=self.root,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(marker.exists())
        self.assertEqual(
            before_caches,
            {
                path.relative_to(self.runtime)
                for path in self.runtime.rglob("__pycache__")
            },
        )
        report = json.loads(result.stdout)
        self.assertEqual(report["sha256"], _sha(self.campaign_audit))
        requirements, _ = admission._requirements(self.root, self.requirements_path)
        _, expected = admission._current_campaign_audit(self.root, requirements)
        self.assertEqual(self.campaign_audit.read_bytes(), expected)

        repeated = subprocess.run(
            command,
            cwd=self.root,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn(b"refusing to overwrite immutable artifact", repeated.stderr)

    def test_campaign_audit_builder_rejects_undeclared_output(self) -> None:
        attacker = self.root / "attempt1/attacker-controlled-audit.json"
        result = subprocess.run(
            [
                os.sys.executable,
                "-I",
                "-S",
                "-B",
                str(VERIFIER_PATH),
                "build-campaign-audit",
                "--repository-root",
                str(self.root),
                "--requirements",
                str(self.requirements_path),
                "--output",
                str(attacker.relative_to(self.root)),
            ],
            cwd=self.root,
            env={"PATH": os.environ.get("PATH", "")},
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"not one of the three declared paths", result.stderr)
        self.assertFalse(attacker.exists())

    def test_wrong_variant_tuple_fails_before_admission(self) -> None:
        with self.assertRaisesRegex(admission.AdmissionError, "argument tuple mismatch"):
            self._verify(project_name="attacker-project")

    def test_provider_extra_field_fails_closed(self) -> None:
        self._write_provider(extra={"label": "must-not-survive-sanitization"})
        with self.assertRaises(admission.AdmissionError):
            self._verify()

    def test_provider_arithmetic_mutation_fails_closed(self) -> None:
        self._write_provider(remaining="128.637473950")
        with self.assertRaisesRegex(admission.AdmissionError, "arithmetic"):
            self._verify()

    def test_provider_usage_regression_below_historical_floor_fails(self) -> None:
        self._write_provider(usage="100", remaining="150")
        with self.assertRaisesRegex(admission.AdmissionError, "historical floor"):
            self._verify()

    def test_stale_and_future_provider_timestamps_fail(self) -> None:
        for observed in ("2026-07-16T06:00:00Z", "2026-07-16T06:47:00Z"):
            with self.subTest(observed=observed):
                self._write_provider(observed=observed)
                with self.assertRaisesRegex(admission.AdmissionError, "stale or future"):
                    self._verify()

    def test_fx_raw_xml_mutation_fails_hash_binding(self) -> None:
        self.fx_raw.write_bytes(self.fx_raw.read_bytes() + b"\n")
        with self.assertRaisesRegex(admission.AdmissionError, "byte/hash"):
            self._verify()

    def test_fx_multiple_dated_cubes_fail_closed(self) -> None:
        self.fx_raw.write_bytes(
            b"<Envelope><Cube><Cube time='2026-07-15'><Cube currency='USD' rate='1.1406'/></Cube><Cube time='2026-07-14'><Cube currency='USD' rate='1.13'/></Cube></Cube></Envelope>"
        )
        self._write_fx_metadata(raw_xml_bytes=len(self.fx_raw.read_bytes()), raw_xml_sha256=_sha(self.fx_raw))
        with self.assertRaisesRegex(admission.AdmissionError, "exactly one"):
            self._verify()

    def test_fx_inverted_currency_metadata_fails_closed(self) -> None:
        self._write_fx_metadata(base_currency="USD", quote_currency="EUR")
        with self.assertRaisesRegex(admission.AdmissionError, "provenance"):
            self._verify()

    def test_authorization_extra_field_fails_recomputation(self) -> None:
        document = json.loads(self.authorization_path.read_text(encoding="utf-8"))
        document["bypass"] = True
        _write_json(self.authorization_path, document)
        with self.assertRaisesRegex(admission.AdmissionError, "recomputation"):
            self._verify()

    def test_independent_audit_hash_mutation_fails(self) -> None:
        document = json.loads(self.audit_path.read_text(encoding="utf-8"))
        document["authorization_sha256"] = "0" * 64
        _write_json(self.audit_path, document)
        with self.assertRaisesRegex(admission.AdmissionError, "audit failed hash"):
            self._verify()

    def test_initial_ledger_mutation_fails(self) -> None:
        ledger = self.root / self.variants["v7-control"]["ledger_path"]
        ledger.write_bytes(b"\n\n")
        with self.assertRaisesRegex(admission.AdmissionError, "initial blank state"):
            self._verify()

    def test_working_prediction_mutation_fails(self) -> None:
        prediction = (
            self.root
            / self.variants["v7-control"]["run_root"]
            / "evaluation/official-harness/predicted_project-v7/q00.json"
        )
        prediction.write_text('{"mutated":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(admission.AdmissionError, "working prediction tree file changed"):
            self._verify()

    def test_staged_manifest_hash_mutation_fails(self) -> None:
        variant = self.variants["v7-control"]
        manifest = self.root / variant["staged_copy_manifest_path"]
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["unsealed"] = True
        manifest.chmod(0o644)
        _write_json(manifest, document)
        with self.assertRaisesRegex(admission.AdmissionError, "sealed hash"):
            self._verify()

    def test_staged_source_byte_mutation_fails(self) -> None:
        staged = self.root / self.variants["v7-control"]["staged_prediction_directory"]
        (staged / "q00.json").write_text('{"mutated":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(admission.AdmissionError, "staged prediction tree file changed"):
            self._verify()

    def test_staged_source_extra_file_and_symlink_fail(self) -> None:
        staged = self.root / self.variants["v7-control"]["staged_prediction_directory"]
        extra = staged / "extra.json"
        extra.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(admission.AdmissionError, "missing or extra entries"):
            self._verify()
        extra.unlink()
        (staged / "escape.json").symlink_to(staged / "q00.json")
        with self.assertRaisesRegex(admission.AdmissionError, "symbolic link"):
            self._verify()

    def test_working_manifest_cannot_rebase_mutated_copy(self) -> None:
        variant = self.variants["v7-control"]
        manifest = self.root / variant["working_copy_manifest_path"]
        document = json.loads(manifest.read_text(encoding="utf-8"))
        working = (
            self.root
            / variant["run_root"]
            / "evaluation/official-harness/predicted_project-v7/q00.json"
        )
        payload = b'{"rebased":true}\n'
        working.write_bytes(payload)
        entry = next(item for item in document["files"] if item["path"] == "q00.json")
        entry["bytes"] = len(payload)
        entry["sha256"] = hashlib.sha256(payload).hexdigest()
        _write_json(manifest, document)
        with self.assertRaisesRegex(admission.AdmissionError, "sealed staged manifest"):
            self._verify()

    def test_extracted_runtime_mutation_and_extra_entry_fail(self) -> None:
        source = self.root / self.runtime_sources["v11-source"]["extracted_root"]
        proxy = source / "narratordb/benchmarks/openrouter_proxy.py"
        proxy.chmod(0o644)
        proxy.write_text("# mutated\n", encoding="utf-8")
        proxy.chmod(0o444)
        with self.assertRaisesRegex(admission.AdmissionError, "differs from its archive"):
            self._verify()

    def test_unsafe_archive_member_fails_closed(self) -> None:
        archive = self.root / self.runtime_sources["harness-source"]["archive_path"]
        with tarfile.open(archive, "w") as output:
            member = tarfile.TarInfo("../escape.py")
            payload = b"escape\n"
            member.size = len(payload)
            output.addfile(member, io.BytesIO(payload))
        requirements = json.loads(self.requirements_path.read_text(encoding="utf-8"))
        requirements["runtime_sources"]["harness-source"]["archive_sha256"] = _sha(archive)
        _write_json(self.requirements_path, requirements)
        with self.assertRaisesRegex(admission.AdmissionError, "member path is unsafe"):
            self._verify()

    def test_vendor_environment_mutation_fails(self) -> None:
        package = self.vendor_site / "example_package/__init__.py"
        package.chmod(0o644)
        package.write_text("VALUE = 2\n", encoding="utf-8")
        package.chmod(0o444)
        with self.assertRaisesRegex(admission.AdmissionError, "sealed inventory"):
            self._verify()

    def test_vendor_python_executable_substitution_fails(self) -> None:
        self.vendor_python.unlink()
        self.vendor_python.write_text(
            "#!/bin/sh\nprintf '%s\\n' "
            "'{\"cache_tag\":\"cpython-312\",\"version\":\"3.12.13\"}'\n",
            encoding="utf-8",
        )
        self.vendor_python.chmod(0o755)
        with self.assertRaisesRegex(admission.AdmissionError, "exact sealed symlink"):
            self._verify()

    def test_runtime_tree_must_be_read_only(self) -> None:
        source = self.root / self.runtime_sources["v11-source"]["extracted_root"]
        proxy = source / "narratordb/benchmarks/openrouter_proxy.py"
        proxy.chmod(0o644)
        with self.assertRaisesRegex(admission.AdmissionError, "read-only"):
            self._verify()

    def test_budget_auditor_support_module_mutation_fails(self) -> None:
        self.budget_auditor_history.write_text("raise SystemExit('injected')\n", encoding="utf-8")
        with self.assertRaisesRegex(admission.AdmissionError, "history support changed"):
            self._verify()

    def test_wrong_published_precommit_hash_fails(self) -> None:
        with self.assertRaisesRegex(admission.AdmissionError, "published replacement"):
            self._verify(published_precommit_sha256="0" * 64)

    def test_valid_post_pair_reconciliation_authorizes_score_release(self) -> None:
        self._prepare_finalization()
        report = admission.verify_finalization(
            self.root,
            self.requirements_path,
            published_precommit_sha256=self.published,
            now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(report["score_release_authorized"])
        self.assertEqual(report["campaign_observed_usd"], "109.21010877191")
        self.assertEqual(report["provider_usage_usd"], "122.262526048")

    def test_ledger_identity_recomputation_uses_isolated_clean_environment(self) -> None:
        self._prepare_finalization()
        variant = self.variants["v7-control"]
        state = admission._ledger_state(self.root, variant, require_initial=False)
        requirements, _ = admission._requirements(self.root, self.requirements_path)
        hostile = self.root / "hostile-ledger-pythonpath"
        hostile.mkdir()
        marker = self.root / "ledger-startup-loaded"
        hook = f"open({str(marker)!r}, 'w').write('loaded')\n"
        (hostile / "sitecustomize.py").write_text(hook, encoding="utf-8")
        (hostile / "usercustomize.py").write_text(hook, encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {
                "PYTHONPATH": str(hostile),
                "PYTHONSTARTUP": str(hostile / "sitecustomize.py"),
                "OPENROUTER_API_KEY": "must-not-reach-ledger-verifier",
                "HTTP_PROXY": "http://attacker.invalid:8080",
            },
        ):
            _, cost = admission._verify_ledger_identity_audit(
                self.root,
                requirements,
                state,
                "attempt1/v7-control/evaluation/ledger-audit.json",
                label="V7",
            )
        self.assertEqual(str(cost), "0.4")
        self.assertFalse(marker.exists())

    def test_post_pair_provider_delta_mutation_blocks_score_release(self) -> None:
        _, _, provider = self._prepare_finalization()
        document = json.loads(provider.read_text(encoding="utf-8"))
        document["provider_usage_usd"] = "122.272526048"
        document["provider_remaining_usd"] = "127.727473952"
        _write_json(provider, document)
        with self.assertRaisesRegex(admission.AdmissionError, "reconcile both"):
            admission.verify_finalization(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )

    def test_pre_v7_provider_baseline_mutation_cannot_be_rebased(self) -> None:
        _, _, final_provider = self._prepare_finalization()
        before = json.loads(self.provider_path.read_text(encoding="utf-8"))
        before["provider_usage_usd"] = "121.462526048"
        before["provider_remaining_usd"] = "128.537473952"
        _write_json(self.provider_path, before)
        after = json.loads(final_provider.read_text(encoding="utf-8"))
        after["provider_usage_usd"] = "122.362526048"
        after["provider_remaining_usd"] = "127.637473952"
        _write_json(final_provider, after)
        with self.assertRaisesRegex(admission.AdmissionError, "exact phase reconstruction"):
            admission.verify_finalization(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )

    def test_post_pair_independent_audit_mutation_blocks_score_release(self) -> None:
        _, audit_path, _ = self._prepare_finalization()
        document = json.loads(audit_path.read_text(encoding="utf-8"))
        document["authorization_sha256"] = "0" * 64
        _write_json(audit_path, document)
        with self.assertRaisesRegex(admission.AdmissionError, "independent audit"):
            admission.verify_finalization(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )

    def test_preserved_phase_evidence_mutation_blocks_score_release(self) -> None:
        self._prepare_finalization()
        between_provider = self.root / "attempt1/between/provider.json"
        document = json.loads(between_provider.read_text(encoding="utf-8"))
        document["provider_usage_usd"] = "121.762526049"
        document["provider_remaining_usd"] = "128.237473951"
        _write_json(between_provider, document)
        with self.assertRaisesRegex(admission.AdmissionError, "exact phase reconstruction"):
            admission.verify_finalization(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )

    def test_forged_phase_campaign_gate_and_rebased_audit_are_rejected(self) -> None:
        self._prepare_finalization()
        authorization = self.root / "attempt1/between/authorization.json"
        document = json.loads(authorization.read_text(encoding="utf-8"))
        document["campaign_gate"] = {"forged": True, "within_cap": False}
        document["forged_extra_field"] = True
        _write_json(authorization, document)
        audit_path = self.root / "attempt1/between/authorization.audit.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        audit["authorization_sha256"] = _sha(authorization)
        _write_json(audit_path, audit)
        with self.assertRaisesRegex(admission.AdmissionError, "exact phase reconstruction"):
            admission.verify_finalization(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )

    def test_preserved_working_manifest_deletion_blocks_score_release(self) -> None:
        self._prepare_finalization()
        manifest = self.root / self.variants["v7-control"]["working_copy_manifest_path"]
        manifest.unlink()
        with self.assertRaisesRegex(admission.AdmissionError, "working-copy manifest is missing"):
            admission.verify_finalization(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )

    def test_paired_result_is_built_only_after_every_gate(self) -> None:
        self._prepare_finalization()
        document = admission._paired_result_document(
            self.root,
            self.requirements_path,
            published_precommit_sha256=self.published,
            now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(document["score_release_authorized"])
        self.assertEqual(document["delta_correct"], {"top_20": 1, "top_50": 1})
        self.assertEqual(document["revision_precommit_sha256"], self.published)
        self.assertEqual(
            set(document["evaluation_audit_sha256"]), {"v7_control", "v13_first"}
        )

    def test_paired_result_rejects_skipped_final_gate(self) -> None:
        _, final_audit, _ = self._prepare_finalization()
        final_audit.unlink()
        with self.assertRaisesRegex(admission.AdmissionError, "independent audit is missing"):
            admission._paired_result_document(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )

    def test_paired_result_rejects_mutated_evaluation_audit(self) -> None:
        self._prepare_finalization()
        evaluation = self.root / "attempt1/v13-first/evaluation-audit.json"
        evaluation.chmod(0o644)
        document = json.loads(evaluation.read_text(encoding="utf-8"))
        document["usage"]["publication_ready"] = False
        _write_json(evaluation, document)
        evaluation.chmod(0o444)
        with self.assertRaisesRegex(admission.AdmissionError, "not byte-identical"):
            admission._paired_result_document(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )

    def test_paired_result_rejects_fabricated_self_consistent_100_percent(self) -> None:
        self._prepare_finalization()
        evaluation = self.root / "attempt1/v13-first/evaluation-audit.json"
        evaluation.chmod(0o644)
        document = json.loads(evaluation.read_text(encoding="utf-8"))
        perfect = {"accuracy": 1.0, "correct": 42, "total": 42}
        document["metrics"] = {"top_20": perfect, "top_50": perfect}
        document["by_question_type"] = {
            "fabricated": {"top_20": perfect, "top_50": perfect}
        }
        _write_json(evaluation, document)
        evaluation.chmod(0o444)
        with self.assertRaisesRegex(admission.AdmissionError, "not byte-identical"):
            admission._paired_result_document(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )

    def test_paired_result_rejects_swapped_variant_audits(self) -> None:
        self._prepare_finalization()
        v7 = self.root / "attempt1/v7-control/evaluation-audit.json"
        v13 = self.root / "attempt1/v13-first/evaluation-audit.json"
        v7.chmod(0o644)
        v13.chmod(0o644)
        v7_payload, v13_payload = v7.read_bytes(), v13.read_bytes()
        v7.write_bytes(v13_payload)
        v13.write_bytes(v7_payload)
        v7.chmod(0o444)
        v13.chmod(0o444)
        with self.assertRaisesRegex(admission.AdmissionError, "not byte-identical"):
            admission._paired_result_document(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )

    def test_paired_result_rejects_evaluation_tool_and_scored_output_mutation(self) -> None:
        self._prepare_finalization()
        auditor = (
            self.root
            / self.runtime_sources["v11-source"]["extracted_root"]
            / "narratordb/benchmarks/evaluation_audit.py"
        )
        auditor_payload = auditor.read_bytes()
        auditor.chmod(0o644)
        auditor.write_text("raise SystemExit('forged auditor')\n", encoding="utf-8")
        auditor.chmod(0o444)
        with self.assertRaisesRegex(admission.AdmissionError, "differs from its archive"):
            admission._paired_result_document(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )
        auditor.chmod(0o644)
        auditor.write_bytes(auditor_payload)
        auditor.chmod(0o444)
        variant = self.variants["v13-first"]
        scored = (
            self.root
            / variant["run_root"]
            / f"evaluation/official-harness/predicted_{variant['project_name']}/q00.json"
        )
        scored.chmod(0o644)
        document = json.loads(scored.read_text(encoding="utf-8"))
        document["cutoff_results"]["top_20"]["score"] = 0.0
        document["cutoff_results"]["top_20"]["judgment"] = "FAIL"
        _write_json(scored, document)
        scored.chmod(0o444)
        with self.assertRaisesRegex(admission.AdmissionError, "not byte-identical"):
            admission._paired_result_document(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )

    def test_paired_result_rejects_boolean_metric_accuracy(self) -> None:
        self._prepare_finalization()
        evaluation = self.root / "attempt1/v13-first/evaluation-audit.json"
        evaluation.chmod(0o644)
        document = json.loads(evaluation.read_text(encoding="utf-8"))
        document["metrics"]["top_20"]["accuracy"] = True
        _write_json(evaluation, document)
        evaluation.chmod(0o444)
        with self.assertRaisesRegex(admission.AdmissionError, "not byte-identical"):
            admission._paired_result_document(
                self.root,
                self.requirements_path,
                published_precommit_sha256=self.published,
                now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
            )

    def test_paired_result_output_is_exact_new_and_read_only(self) -> None:
        self._prepare_finalization()
        requirements, _ = admission._requirements(self.root, self.requirements_path)
        output = admission._paired_result_output(
            self.root, requirements, Path("attempt1/paired-result.json")
        )
        document = admission._paired_result_document(
            self.root,
            self.requirements_path,
            published_precommit_sha256=self.published,
            now=datetime(2026, 7, 16, 6, 51, 0, tzinfo=timezone.utc),
        )
        admission._write_new(output, document)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o444)
        with self.assertRaisesRegex(admission.AdmissionError, "refusing to overwrite"):
            admission._write_new(output, document)
        with self.assertRaisesRegex(admission.AdmissionError, "exact declared path"):
            admission._paired_result_output(
                self.root, requirements, Path("attempt1/attacker-result.json")
            )


class GuardedHarnessTests(unittest.TestCase):
    def test_malicious_ancestor_dotenv_cannot_override_local_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "harness-source"
            module = source / "benchmarks/longmemeval/run.py"
            module.parent.mkdir(parents=True)
            (source / "benchmarks/__init__.py").write_text("", encoding="utf-8")
            (source / "benchmarks/longmemeval/__init__.py").write_text("", encoding="utf-8")
            module.write_text(
                "from dotenv import load_dotenv\n"
                "load_dotenv(override=True)\n"
                "if __name__ == '__main__':\n"
                " import json, os, pathlib, sys\n"
                " output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
                " output.write_text(json.dumps({'base': os.getenv('OPENAI_BASE_URL'), "
                "'key': os.getenv('OPENAI_API_KEY')}))\n",
                encoding="utf-8",
            )
            (root / ".env").write_text(
                "OPENAI_BASE_URL=https://attacker.invalid/v1\n"
                "OPENAI_API_KEY=attacker-secret\n",
                encoding="utf-8",
            )
            site_packages = root / "site-packages"
            dotenv_package = site_packages / "dotenv"
            dotenv_package.mkdir(parents=True)
            (dotenv_package / "__init__.py").write_text(
                "from .main import load_dotenv\n", encoding="utf-8"
            )
            (dotenv_package / "main.py").write_text(
                "def load_dotenv(*args, **kwargs):\n"
                "    raise AssertionError('guard must disable dotenv before use')\n",
                encoding="utf-8",
            )
            output = root / "route.json"
            guard = (
                REPOSITORY
                / "benchmark_records/reproduction-v13-paid-paired-scoring-r4-20260716/run_harness_guarded.py"
            )
            result = subprocess.run(
                [
                    os.sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    str(guard),
                    "--output",
                    str(output),
                ],
                cwd=root,
                env={
                    "LANG": "C",
                    "LC_ALL": "C",
                    "NARRATORDB_EXPECTED_HARNESS_SOURCE": str(source),
                    "NARRATORDB_HARNESS_SITE_PACKAGES": str(site_packages),
                    "OPENAI_API_KEY": "local-transport",
                    "OPENAI_BASE_URL": "http://127.0.0.1:8890/v1",
                    "NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost",
                },
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"base": "http://127.0.0.1:8890/v1", "key": "local-transport"},
            )


class HardenedWrapperMutationTests(unittest.TestCase):
    def test_static_closed_tree_and_guarded_runtime_controls_are_present(self) -> None:
        wrapper = (
            REPOSITORY
            / "benchmark_records/reproduction-v13-paid-paired-scoring-r4-20260716/run_paid_variant_hardened.sh"
        ).read_text(encoding="utf-8")
        self.assertTrue(wrapper.startswith("#!/bin/bash -p\n"))
        self.assertLess(
            wrapper.index("unset OPENROUTER_API_KEY"),
            wrapper.index("SCRIPT_DIR=$("),
        )
        self.assertNotIn("#!/usr/bin/env bash", wrapper)
        self.assertNotIn("exec env -i", wrapper)
        self.assertIn("OPENROUTER_API_KEY=$RUNTIME_OPENROUTER_KEY", wrapper)
        self.assertIn("export PYTHONPATH OPENROUTER_API_KEY", wrapper)
        self.assertIn("find . -mindepth 1 ! -type f", wrapper)
        self.assertIn("find . -type f -links +1", wrapper)
        self.assertIn("find . -type f -name .DS_Store", wrapper)
        self.assertIn('chmod -R a-w "$V11_SOURCE" "$HARNESS_SOURCE"', wrapper)
        self.assertIn('"$HARNESS_GUARD" --narratordb-preflight', wrapper)
        self.assertIn('-I -S -B "$HARNESS_GUARD"', wrapper)
        self.assertNotIn("-m benchmarks.longmemeval.run", wrapper)
        self.assertNotIn("PYTHON_DOTENV_DISABLED", wrapper)
        self.assertGreaterEqual(wrapper.count("env -i"), 4)
        self.assertIn("curl --noproxy '*'", wrapper)

    def test_arbitrary_variant_tuple_is_rejected_after_safe_key_isolation(self) -> None:
        wrapper = (
            REPOSITORY
            / "benchmark_records/reproduction-v13-paid-paired-scoring-r4-20260716/run_paid_variant_hardened.sh"
        )
        result = subprocess.run(
            [str(wrapper), "reports/attacker", "attacker-project", "dataset.json"],
            cwd=REPOSITORY,
            env={
                "PATH": os.environ.get("PATH", ""),
                "OPENROUTER_API_KEY": "neutral-test-credential",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"not one exact precommitted tuple", result.stderr)


if __name__ == "__main__":
    unittest.main()
