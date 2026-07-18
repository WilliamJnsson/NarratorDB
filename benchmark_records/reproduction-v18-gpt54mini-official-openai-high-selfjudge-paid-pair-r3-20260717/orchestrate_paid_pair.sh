#!/bin/bash -p
set -euo pipefail

[[ $# -eq 0 ]] || exit 2
SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
[[ $(pwd -P) == "$ROOT" ]] || exit 1
PUBLISHED=${NARRATORDB_V18_R3_GPT_SELFJUDGE_PRECOMMIT_SHA256:-}
[[ $PUBLISHED =~ ^[0-9a-f]{64}$ ]] || exit 1
PRECOMMIT="$ROOT/benchmark_records/precommits/longmemeval_dev42_v18_gpt54mini_official_openai_high_selfjudge_paid_pair_r3_precommit_20260717.json"
[[ $(/usr/bin/shasum -a 256 "$PRECOMMIT" | /usr/bin/awk '{print $1}') == "$PUBLISHED" ]] || exit 1
(cd "$SCRIPT_DIR" && /usr/bin/shasum -a 256 -c SEALED_FILES_SHA256SUMS)
(cd "$ROOT" && /usr/bin/shasum -a 256 -c "$SCRIPT_DIR/BOUND_INPUTS_SHA256SUMS")

PYTHON="$ROOT/vendor/memory-benchmarks/.venv/bin/python"
REPORT="$ROOT/reports/longmemeval-intelligence-dev42-v18-gpt54mini-official-openai-high-selfjudge-paid-pair-r3-20260717"
R1="$ROOT/reports/longmemeval-intelligence-dev42-v18-gpt54mini-openai-high-selfjudge-paid-pair-r1-20260717/ABORTED_AFTER_PRIMARY_AUDIT.json"
R2="$ROOT/reports/longmemeval-intelligence-dev42-v18-gpt54mini-openai-high-selfjudge-paid-pair-r2-20260717/TERMINAL_EXECUTION_STATUS.json"
R2_DISCLOSURE="$SCRIPT_DIR/R2_TERMINAL_DISCLOSURE.json"
PRICING="$SCRIPT_DIR/OFFICIAL_OPENAI_MODEL_AND_PRICING.json"
IDS="$ROOT/reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/dev42_question_ids.json"
SOURCE_ROOT="$ROOT/reports/longmemeval-intelligence-dev42-v18-replay-v7gpt54mini-repeat2-attempt2-20260717"
PROJECT=narratordb-intelligence-dev42-v18-replay-v7gpt54mini-repeat2-attempt2
FROZEN="$SOURCE_ROOT/official-harness/predicted_$PROJECT"
ADMISSION="$REPORT/preflight/local-admission.json"
CANARY_RESULT="$REPORT/canary/evaluation/canary-result.json"
CANARY_USAGE="$REPORT/canary/evaluation/openai-usage.jsonl"
FINAL_AUDIT="$REPORT/PAIRED_SCORE_AUDIT.json"
FINAL_MANIFEST="$REPORT/PAIRED_SCORE_SHA256SUMS"
TERMINAL_STATUS="$REPORT/TERMINAL_EXECUTION_STATUS.json"

[[ -x $PYTHON ]] || exit 1
[[ -f $R1 && $(/usr/bin/shasum -a 256 "$R1" | /usr/bin/awk '{print $1}') == 4bdfe140a4f232b79a1e2b6121fa4a496b01ecd0d924a61a7c4e2468b0481eba ]] || exit 1
[[ -f $R2 && $(/usr/bin/shasum -a 256 "$R2" | /usr/bin/awk '{print $1}') == 808d84f547fbd42587a2c1ac17e8b3fd8bf3853ec34221344bc58e94c0a14b9d ]] || exit 1
[[ -f $R2_DISCLOSURE && $(/usr/bin/shasum -a 256 "$R2_DISCLOSURE" | /usr/bin/awk '{print $1}') == 715698e49cbf421046063f5537642be955d3da6091f67a8d0674fe6631ce080c ]] || exit 1
[[ -f $PRICING && $(/usr/bin/shasum -a 256 "$PRICING" | /usr/bin/awk '{print $1}') == 41e6f74aab48e82f3854fff2c6a6425a4b7c13879dc3006674526d9190a41870 ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$SCRIPT_DIR/openai_proxy_r3.py" | /usr/bin/awk '{print $1}') == 90a342bb7f97162a7af448d26ed191a78c5618a56a8106b9d11868a6a128c253 ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$SCRIPT_DIR/run_openai_proxy_guarded.py" | /usr/bin/awk '{print $1}') == b3e67b69b9757b79f862ef6ad005ab4b3279eb7d72ff58fa6333ef5ae9de500a ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$SCRIPT_DIR/test_openai_proxy_r3.py" | /usr/bin/awk '{print $1}') == 473693b74e4e90d9fed80ace43eac9d94bbe3592387ba1bd7c0f8e929910b8c6 ]] || exit 1
for P in \
  "$REPORT/preflight/ecb-eurofxref-daily.xml" \
  "$REPORT/preflight/ecb-usd-eur.json" \
  "$ADMISSION" "$CANARY_RESULT" "$CANARY_USAGE" \
  "$FINAL_AUDIT" "$FINAL_MANIFEST" "$TERMINAL_STATUS"; do
  [[ ! -e $P && ! -L $P ]] || exit 1
done
for ARM in primary replication; do
  for P in \
    "$REPORT/$ARM/evaluation/openai-usage.jsonl" \
    "$REPORT/$ARM/evaluation/proxy-health-before.json" \
    "$REPORT/$ARM/evaluation/proxy-health-after.json" \
    "$REPORT/$ARM/evaluation/proxy.log" \
    "$REPORT/$ARM/evaluation/evaluate.log" \
    "$REPORT/$ARM/evaluation/harness-runtime" \
    "$REPORT/$ARM/evaluation-audit.json" \
    "$REPORT/$ARM/transport-arm-audit.json"; do
    [[ ! -e $P && ! -L $P ]] || exit 1
  done
done

PHASE=static-preflight
SUCCESS=0
preserve_terminal_status() {
  STATUS=$?
  trap - EXIT
  if [[ $SUCCESS -ne 1 && ! -e $TERMINAL_STATUS && ! -L $TERMINAL_STATUS ]]; then
    "$PYTHON" -I -S -B - "$TERMINAL_STATUS" "$PHASE" "$STATUS" <<'PY' || :
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = (
    json.dumps(
        {
            "schema_version": "narratordb.v18-gpt-selfjudge-r3-terminal-execution.v1",
            "recorded_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "TERMINAL_INCOMPLETE",
            "failed_phase": sys.argv[2],
            "exit_status": int(sys.argv[3]),
            "score_values_embedded_in_terminal_record": False,
            "score_values_may_exist_in_completed_phase_artifacts": True,
            "failure_decision_used_score_values": False,
            "score_driven_branching": False,
            "provider_telemetry_performed": False,
            "accounting_drain_complete": int(sys.argv[3]) != 98,
            "accounting_incomplete_reason": (
                "fatal watchdog drain exceeded 112 seconds or health stayed unavailable"
                if int(sys.argv[3]) == 98
                else None
            ),
            "restart_policy": "new fresh pair root; no selective question retry",
        },
        indent=2,
        sort_keys=True,
    )
    + "\n"
).encode()
fd = os.open(
    path,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o444,
)
with os.fdopen(fd, "wb") as output:
    output.write(payload)
    output.flush()
    os.fsync(output.fileno())
PY
  fi
  exit "$STATUS"
}
trap preserve_terminal_status EXIT

# All offline tests and static policy checks run before ECB, credentials, the
# proxy, or any answerer/judge request.
"$PYTHON" -I -S -B "$SCRIPT_DIR/test_transport_arm_audit.py"
"$PYTHON" -I -S -B "$SCRIPT_DIR/test_orchestration_policy.py"
"$PYTHON" -I -S -B "$SCRIPT_DIR/test_openai_proxy_r3.py"
"$PYTHON" -I -S -B - \
  "$SCRIPT_DIR/openai_proxy_r3.py" \
  "$SCRIPT_DIR/run_openai_proxy_guarded.py" \
  "$SCRIPT_DIR/launch_with_openai_key.sh" \
  "$SCRIPT_DIR/admit_openai_campaign.py" <<'PY'
import sys
from pathlib import Path

proxy = Path(sys.argv[1]).read_text(encoding="utf-8")
guard = Path(sys.argv[2]).read_text(encoding="utf-8")
launcher = Path(sys.argv[3]).read_text(encoding="utf-8")
admission = Path(sys.argv[4]).read_text(encoding="utf-8")
proxy_required = (
    'OFFICIAL_UPSTREAM = "https://api.openai.com/v1/chat/completions"',
    'PINNED_MODEL = "gpt-5.4-mini-2026-03-17"',
    "MAX_DISCARDED_TRANSIENTS = 4",
    "MAX_LOGICAL_ATTEMPTS = 5",
    "ProxyHandler({})",
    'self.send_header("x-narratordb-transport-fatal", "true")',
    'self.headers.get("x-stainless-retry-count") not in {None, "0"}',
    'self.headers.get("Authorization") != "Bearer local-transport"',
    '"reasoning_effort", "high"',
    '"service_tier", "default"',
    '"store", False',
)
launcher_required = (
    "UPSTREAM_TIMEOUT=105",
    "MAX_REQUEST_BYTES=20971520",
    "MAX_RESPONSE_BYTES=4194304",
    "RESERVE=0.318432",
    "MAX=0.611152",
    "RESERVE=0.300576",
    "transport_failed",
    "start_new_session=True",
    "OPENAI_API_KEY=local-transport",
    "PYTHON_DOTENV_DISABLED=1",
    "--answerer-model \"$MODEL\"",
    "--judge-model \"$MODEL\"",
)
admission_required = (
    'ATTESTED_AVAILABLE_BALANCE = Decimal("30.00")',
    '"verification": "not_api_verified"',
    '"provider_telemetry_performed": False',
    'CANARY_FUSE = Decimal("0.611152")',
)
if any(fragment not in proxy for fragment in proxy_required):
    raise SystemExit("sealed R3 proxy lacks a required official policy binding")
if "openai_proxy_r3.py" not in guard:
    raise SystemExit("proxy guard does not load the sibling R3 proxy")
if any(fragment not in launcher for fragment in launcher_required):
    raise SystemExit("R3 launcher lacks a required watchdog/transport binding")
if any(fragment not in admission for fragment in admission_required):
    raise SystemExit("R3 local admission lacks a required limitation/budget binding")
PY

"$PYTHON" -I -S -B "$SCRIPT_DIR/verify_staged_copy.py" \
  --manifest "$REPORT/primary/evaluation/official-harness/frozen-copy-manifest.json" \
  --expected-manifest-sha256 6b4a949a15a842b1c2dfc9b101ffb1ba908c48c4667f559d077ec6294b403161
"$PYTHON" -I -S -B "$SCRIPT_DIR/verify_staged_copy.py" \
  --manifest "$REPORT/replication/evaluation/official-harness/frozen-copy-manifest.json" \
  --expected-manifest-sha256 28b3cadccad086e6dfab20c4a89fcf70b4292d8d3ecdf1b161be93c9e550ecac

PHASE=ecb-fx
"$PYTHON" -I -S -B "$SCRIPT_DIR/capture_ecb_fx.py" \
  --repository-root "$ROOT" \
  --raw-output "$REPORT/preflight/ecb-eurofxref-daily.xml" \
  --metadata-output "$REPORT/preflight/ecb-usd-eur.json" \
  --parser "$SCRIPT_DIR/admit_openai_campaign.py" \
  --timeout 20
PHASE=local-admission
"$PYTHON" -I -S -B "$SCRIPT_DIR/admit_openai_campaign.py" \
  --fx "$REPORT/preflight/ecb-usd-eur.json" \
  --pricing-evidence "$PRICING" \
  --prior-r1-terminal "$R1" \
  --prior-r2-terminal "$R2" \
  --r2-disclosure "$R2_DISCLOSURE" \
  --output "$ADMISSION"
PHASE=strict-canary
"$SCRIPT_DIR/launch_with_openai_key.sh" canary
PHASE=primary
"$SCRIPT_DIR/launch_with_openai_key.sh" primary

PHASE=primary-score-blind-transport-audit
"$PYTHON" -I -S -B "$SCRIPT_DIR/transport_arm_audit.py" \
  --evaluated-directory "$REPORT/primary/evaluation/official-harness/predicted_$PROJECT" \
  --frozen-directory "$FROZEN" \
  --usage-log "$REPORT/primary/evaluation/openai-usage.jsonl" \
  --evaluator-log "$REPORT/primary/evaluation/evaluate.log" \
  --proxy-log "$REPORT/primary/evaluation/proxy.log" \
  --question-id-file "$IDS" \
  --copy-manifest "$REPORT/primary/evaluation/official-harness/frozen-copy-manifest.json" \
  --evaluation-auditor "$ROOT/narratordb/benchmarks/evaluation_audit.py" \
  --proxy-source "$SCRIPT_DIR/openai_proxy_r3.py" \
  --harness-client-source "$REPORT/primary/evaluation/harness-runtime/benchmarks/common/llm_client.py" \
  --raw-audit-output "$REPORT/primary/evaluation-audit.json" \
  --transport-audit-output "$REPORT/primary/transport-arm-audit.json"

# The audit contains no score values.  Once it authorizes the complete primary,
# replication starts unconditionally with no score branch or provider telemetry.
PHASE=unconditional-replication
"$SCRIPT_DIR/launch_with_openai_key.sh" replication
PHASE=replication-score-blind-transport-audit
"$PYTHON" -I -S -B "$SCRIPT_DIR/transport_arm_audit.py" \
  --evaluated-directory "$REPORT/replication/evaluation/official-harness/predicted_$PROJECT" \
  --frozen-directory "$FROZEN" \
  --usage-log "$REPORT/replication/evaluation/openai-usage.jsonl" \
  --evaluator-log "$REPORT/replication/evaluation/evaluate.log" \
  --proxy-log "$REPORT/replication/evaluation/proxy.log" \
  --question-id-file "$IDS" \
  --copy-manifest "$REPORT/replication/evaluation/official-harness/frozen-copy-manifest.json" \
  --evaluation-auditor "$ROOT/narratordb/benchmarks/evaluation_audit.py" \
  --proxy-source "$SCRIPT_DIR/openai_proxy_r3.py" \
  --harness-client-source "$REPORT/replication/evaluation/harness-runtime/benchmarks/common/llm_client.py" \
  --raw-audit-output "$REPORT/replication/evaluation-audit.json" \
  --transport-audit-output "$REPORT/replication/transport-arm-audit.json"

PHASE=score-agnostic-finalization
"$PYTHON" -I -S -B "$SCRIPT_DIR/finalize_pair.py" \
  --repository-root "$ROOT" \
  --report-root "$REPORT" \
  --precommit "$PRECOMMIT" \
  --expected-precommit-sha256 "$PUBLISHED" \
  --r1-terminal "$R1" \
  --r2-terminal "$R2" \
  --r2-disclosure "$R2_DISCLOSURE" \
  --pricing-evidence "$PRICING" \
  --admission "$ADMISSION" \
  --canary-result "$CANARY_RESULT" \
  --canary-usage "$CANARY_USAGE" \
  --primary-raw-audit "$REPORT/primary/evaluation-audit.json" \
  --primary-transport-audit "$REPORT/primary/transport-arm-audit.json" \
  --replication-raw-audit "$REPORT/replication/evaluation-audit.json" \
  --replication-transport-audit "$REPORT/replication/transport-arm-audit.json" \
  --question-id-file "$IDS" \
  --output "$FINAL_AUDIT" \
  --manifest-output "$FINAL_MANIFEST"
(cd "$ROOT" && /usr/bin/shasum -a 256 -c "$FINAL_MANIFEST")
SUCCESS=1
