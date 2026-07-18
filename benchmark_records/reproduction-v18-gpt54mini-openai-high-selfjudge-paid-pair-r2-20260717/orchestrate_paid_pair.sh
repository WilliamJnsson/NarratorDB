#!/bin/bash -p
set -euo pipefail

[[ $# -eq 0 ]] || exit 2
SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
[[ $(pwd -P) == "$ROOT" ]] || exit 1
PUBLISHED=${NARRATORDB_V18_R2_GPT_SELFJUDGE_PRECOMMIT_SHA256:-}
[[ $PUBLISHED =~ ^[0-9a-f]{64}$ ]] || exit 1
PRECOMMIT="$ROOT/benchmark_records/precommits/longmemeval_dev42_v18_gpt54mini_openai_high_selfjudge_paid_pair_r2_precommit_20260717.json"
[[ $(/usr/bin/shasum -a 256 "$PRECOMMIT" | /usr/bin/awk '{print $1}') == "$PUBLISHED" ]] || exit 1
(cd "$SCRIPT_DIR" && /usr/bin/shasum -a 256 -c SEALED_FILES_SHA256SUMS)
(cd "$ROOT" && /usr/bin/shasum -a 256 -c "$SCRIPT_DIR/BOUND_INPUTS_SHA256SUMS")

PYTHON="$ROOT/vendor/memory-benchmarks/.venv/bin/python"
REPORT="$ROOT/reports/longmemeval-intelligence-dev42-v18-gpt54mini-openai-high-selfjudge-paid-pair-r2-20260717"
R1="$ROOT/reports/longmemeval-intelligence-dev42-v18-gpt54mini-openai-high-selfjudge-paid-pair-r1-20260717/ABORTED_AFTER_PRIMARY_AUDIT.json"
IDS="$ROOT/reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/dev42_question_ids.json"
SOURCE_ROOT="$ROOT/reports/longmemeval-intelligence-dev42-v18-replay-v7gpt54mini-repeat2-attempt2-20260717"
PROJECT=narratordb-intelligence-dev42-v18-replay-v7gpt54mini-repeat2-attempt2
FROZEN="$SOURCE_ROOT/official-harness/predicted_$PROJECT"
ADMISSION="$REPORT/preflight/dynamic-admission.json"
CANARY_RESULT="$REPORT/canary/evaluation/canary-result.json"
CANARY_USAGE="$REPORT/canary/evaluation/openrouter-usage.jsonl"
TELEMETRY_PRE="$REPORT/preflight/provider-telemetry-pre.json"
TELEMETRY_BETWEEN="$REPORT/between/provider-telemetry-between.json"
TELEMETRY_POST="$REPORT/postrun/provider-telemetry-post.json"
FINAL_AUDIT="$REPORT/PAIRED_SCORE_AUDIT.json"
FINAL_MANIFEST="$REPORT/PAIRED_SCORE_SHA256SUMS"
TERMINAL_STATUS="$REPORT/TERMINAL_EXECUTION_STATUS.json"

[[ -x $PYTHON ]] || exit 1
[[ -f $R1 && $(/usr/bin/shasum -a 256 "$R1" | /usr/bin/awk '{print $1}') == 4bdfe140a4f232b79a1e2b6121fa4a496b01ecd0d924a61a7c4e2468b0481eba ]] || exit 1
for P in "$ADMISSION" "$CANARY_RESULT" "$CANARY_USAGE" "$TELEMETRY_PRE" "$TELEMETRY_BETWEEN" "$TELEMETRY_POST" "$FINAL_AUDIT" "$FINAL_MANIFEST" "$TERMINAL_STATUS"; do
  [[ ! -e $P && ! -L $P ]] || exit 1
done
for ARM in primary replication; do
  for P in \
    "$REPORT/$ARM/evaluation/openrouter-usage.jsonl" \
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
            "schema_version": "narratordb.v18-gpt-selfjudge-r2-terminal-execution.v1",
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

# These tests and source-policy assertions are deliberately before any ECB,
# credential, proxy, provider, answerer, or judge process can start.
"$PYTHON" -I -S -B "$SCRIPT_DIR/test_transport_arm_audit.py"
"$PYTHON" -I -S -B "$SCRIPT_DIR/test_orchestration_policy.py"
"$PYTHON" -I -S -B "$SCRIPT_DIR/test_openrouter_proxy_r2.py"
"$PYTHON" -I -S -B - \
  "$SCRIPT_DIR/openrouter_proxy_r2.py" \
  "$SCRIPT_DIR/run_openrouter_proxy_guarded.py" \
  "$SCRIPT_DIR/launch_with_openrouter_key.sh" <<'PY'
import sys
from pathlib import Path

proxy = Path(sys.argv[1]).read_text(encoding="utf-8")
guard = Path(sys.argv[2]).read_text(encoding="utf-8")
launcher = Path(sys.argv[3]).read_text(encoding="utf-8")
proxy_required = (
    "_MAX_DISCARDED_TRANSIENTS = 4",
    "ProxyHandler({})",
    '"upstream_timeout_seconds"',
    '"direct_upstream_networking"',
    '"inbound_retry_count_policy"',
    '"local_caller_auth_required"',
    "x-stainless-retry-count",
    'self.headers.get("Authorization") != "Bearer local-transport"',
    'self.headers.get("x-stainless-retry-count") not in {None, "0"}',
)
launcher_required = (
    "UPSTREAM_TIMEOUT=105",
    "MAX_REQUEST_BYTES=20971520",
    "MAX_RESPONSE_BYTES=4194304",
    "OPENAI_API_KEY=local-transport",
)
if any(fragment not in proxy for fragment in proxy_required):
    raise SystemExit("sealed r2 proxy lacks a required policy binding")
if "openrouter_proxy_r2.py" not in guard:
    raise SystemExit("proxy guard does not load the sibling r2 proxy")
if any(fragment not in launcher for fragment in launcher_required):
    raise SystemExit("launcher lacks a required timeout/size/auth binding")
PY

"$PYTHON" -I -S -B "$SCRIPT_DIR/verify_staged_copy.py" \
  --manifest "$REPORT/primary/evaluation/official-harness/frozen-copy-manifest.json" \
  --expected-manifest-sha256 846e8fa41bc5abd7de6f73b5237bb413e7db5a35330e5dd805ba4e57ac160959
"$PYTHON" -I -S -B "$SCRIPT_DIR/verify_staged_copy.py" \
  --manifest "$REPORT/replication/evaluation/official-harness/frozen-copy-manifest.json" \
  --expected-manifest-sha256 98866ed97c337718d32e2e18e50f22d8d0e3de58ba95b0ae0c887e4aceda1b71

PHASE=ecb-fx
"$PYTHON" -I -S -B "$SCRIPT_DIR/capture_ecb_fx.py" \
  --repository-root "$ROOT" \
  --raw-output "$REPORT/preflight/ecb-eurofxref-daily.xml" \
  --metadata-output "$REPORT/preflight/ecb-usd-eur.json" \
  --parser "$SCRIPT_DIR/verify_campaign_budget.py" \
  --timeout 20
PHASE=telemetry-pre
"$SCRIPT_DIR/launch_with_openrouter_key.sh" telemetry-pre
PHASE=dynamic-admission
"$PYTHON" -I -S -B "$SCRIPT_DIR/verify_campaign_budget.py" \
  --telemetry "$TELEMETRY_PRE" \
  --fx "$REPORT/preflight/ecb-usd-eur.json" \
  --prior-r1-terminal "$R1" \
  --output "$ADMISSION"
PHASE=strict-canary
"$SCRIPT_DIR/launch_with_openrouter_key.sh" canary
PHASE=primary
"$SCRIPT_DIR/launch_with_openrouter_key.sh" primary

PHASE=primary-score-blind-transport-audit
"$PYTHON" -I -S -B "$SCRIPT_DIR/transport_arm_audit.py" \
  --evaluated-directory "$REPORT/primary/evaluation/official-harness/predicted_$PROJECT" \
  --frozen-directory "$FROZEN" \
  --usage-log "$REPORT/primary/evaluation/openrouter-usage.jsonl" \
  --evaluator-log "$REPORT/primary/evaluation/evaluate.log" \
  --proxy-log "$REPORT/primary/evaluation/proxy.log" \
  --question-id-file "$IDS" \
  --copy-manifest "$REPORT/primary/evaluation/official-harness/frozen-copy-manifest.json" \
  --evaluation-auditor "$ROOT/narratordb/benchmarks/evaluation_audit.py" \
  --proxy-source "$SCRIPT_DIR/openrouter_proxy_r2.py" \
  --harness-client-source "$REPORT/primary/evaluation/harness-runtime/benchmarks/common/llm_client.py" \
  --raw-audit-output "$REPORT/primary/evaluation-audit.json" \
  --transport-audit-output "$REPORT/primary/transport-arm-audit.json"

# The transport audit intentionally contains no score values. Once it
# authorizes the complete primary, replication proceeds with no score branch.
PHASE=telemetry-between
"$SCRIPT_DIR/launch_with_openrouter_key.sh" telemetry-between
PHASE=unconditional-replication
"$SCRIPT_DIR/launch_with_openrouter_key.sh" replication
PHASE=replication-score-blind-transport-audit
"$PYTHON" -I -S -B "$SCRIPT_DIR/transport_arm_audit.py" \
  --evaluated-directory "$REPORT/replication/evaluation/official-harness/predicted_$PROJECT" \
  --frozen-directory "$FROZEN" \
  --usage-log "$REPORT/replication/evaluation/openrouter-usage.jsonl" \
  --evaluator-log "$REPORT/replication/evaluation/evaluate.log" \
  --proxy-log "$REPORT/replication/evaluation/proxy.log" \
  --question-id-file "$IDS" \
  --copy-manifest "$REPORT/replication/evaluation/official-harness/frozen-copy-manifest.json" \
  --evaluation-auditor "$ROOT/narratordb/benchmarks/evaluation_audit.py" \
  --proxy-source "$SCRIPT_DIR/openrouter_proxy_r2.py" \
  --harness-client-source "$REPORT/replication/evaluation/harness-runtime/benchmarks/common/llm_client.py" \
  --raw-audit-output "$REPORT/replication/evaluation-audit.json" \
  --transport-audit-output "$REPORT/replication/transport-arm-audit.json"
PHASE=telemetry-post
"$SCRIPT_DIR/launch_with_openrouter_key.sh" telemetry-post

PHASE=score-agnostic-finalization
"$PYTHON" -I -S -B "$SCRIPT_DIR/finalize_pair.py" \
  --repository-root "$ROOT" \
  --report-root "$REPORT" \
  --precommit "$PRECOMMIT" \
  --expected-precommit-sha256 "$PUBLISHED" \
  --r1-terminal "$R1" \
  --admission "$ADMISSION" \
  --canary-result "$CANARY_RESULT" \
  --canary-usage "$CANARY_USAGE" \
  --telemetry-pre "$TELEMETRY_PRE" \
  --telemetry-between "$TELEMETRY_BETWEEN" \
  --telemetry-post "$TELEMETRY_POST" \
  --primary-raw-audit "$REPORT/primary/evaluation-audit.json" \
  --primary-transport-audit "$REPORT/primary/transport-arm-audit.json" \
  --replication-raw-audit "$REPORT/replication/evaluation-audit.json" \
  --replication-transport-audit "$REPORT/replication/transport-arm-audit.json" \
  --question-id-file "$IDS" \
  --output "$FINAL_AUDIT" \
  --manifest-output "$FINAL_MANIFEST"
(cd "$ROOT" && /usr/bin/shasum -a 256 -c "$FINAL_MANIFEST")
SUCCESS=1
