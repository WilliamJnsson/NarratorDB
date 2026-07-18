#!/bin/bash -p
set -euo pipefail

[[ $# -eq 1 ]] || exit 2
ACTION=$1
case $ACTION in
  canary|primary|replication) ;;
  *) exit 2 ;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
[[ $(pwd -P) == "$ROOT" ]] || exit 1
PRECOMMIT="$ROOT/benchmark_records/precommits/longmemeval_dev42_v18_gpt54mini_official_openai_high_selfjudge_paid_pair_r3_precommit_20260717.json"
PUBLISHED=${NARRATORDB_V18_R3_GPT_SELFJUDGE_PRECOMMIT_SHA256:-}
[[ $PUBLISHED =~ ^[0-9a-f]{64}$ ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$PRECOMMIT" | /usr/bin/awk '{print $1}') == "$PUBLISHED" ]] || exit 1
(cd "$SCRIPT_DIR" && /usr/bin/shasum -a 256 -c SEALED_FILES_SHA256SUMS)
(cd "$ROOT" && /usr/bin/shasum -a 256 -c "$SCRIPT_DIR/BOUND_INPUTS_SHA256SUMS")
[[ $(/usr/bin/shasum -a 256 "$SCRIPT_DIR/OFFICIAL_OPENAI_MODEL_AND_PRICING.json" | /usr/bin/awk '{print $1}') == 41e6f74aab48e82f3854fff2c6a6425a4b7c13879dc3006674526d9190a41870 ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$SCRIPT_DIR/openai_proxy_r3.py" | /usr/bin/awk '{print $1}') == 90a342bb7f97162a7af448d26ed191a78c5618a56a8106b9d11868a6a128c253 ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$SCRIPT_DIR/run_openai_proxy_guarded.py" | /usr/bin/awk '{print $1}') == b3e67b69b9757b79f862ef6ad005ab4b3279eb7d72ff58fa6333ef5ae9de500a ]] || exit 1
[[ ! -e "$ROOT/.env" && ! -L "$ROOT/.env" ]] || exit 1

PYTHON="$ROOT/vendor/memory-benchmarks/.venv/bin/python"
[[ -L $PYTHON ]] || exit 1
[[ $(/usr/bin/readlink "$PYTHON") == "/Users/william/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12" ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$PYTHON" | /usr/bin/awk '{print $1}') == 7b05d803bbc1bbfc81644af4faf2b88f0a37b8de96b9f42c1e08033e2cd0848a ]] || exit 1

REPORT="$ROOT/reports/longmemeval-intelligence-dev42-v18-gpt54mini-official-openai-high-selfjudge-paid-pair-r3-20260717"
PROJECT=narratordb-intelligence-dev42-v18-replay-v7gpt54mini-repeat2-attempt2
DATASET="$ROOT/reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/longmemeval_s_dev42.json"
SOURCE_ROOT="$ROOT/reports/longmemeval-intelligence-dev42-v18-replay-v7gpt54mini-repeat2-attempt2-20260717"
HARNESS_TAR="$SOURCE_ROOT/harness-source.tar"
PROXY_SCRIPT="$SCRIPT_DIR/run_openai_proxy_guarded.py"
ADMISSION="$REPORT/preflight/local-admission.json"
PORT=8893
UPSTREAM_TIMEOUT=105
MAX_REQUEST_BYTES=20971520
MAX_RESPONSE_BYTES=4194304
MODEL=gpt-5.4-mini-2026-03-17

case $ACTION in
  canary) RUN="$REPORT/canary" ;;
  primary) RUN="$REPORT/primary" ;;
  replication) RUN="$REPORT/replication" ;;
esac

verify_admission() {
  "$PYTHON" -I -S -B - "$ADMISSION" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
checks = data.get("checks")
attestation = data.get("balance_attestation")
if (
    data.get("schema_version")
    != "narratordb.v18-gpt-selfjudge-campaign-admission.r3.v1"
    or data.get("admitted") is not True
    or not isinstance(checks, dict)
    or not checks
    or not all(value is True for value in checks.values())
    or data.get("credential_recorded") is not False
    or data.get("model_content_recorded") is not False
    or data.get("provider_telemetry_performed") is not False
    or data.get("model_snapshot") != "gpt-5.4-mini-2026-03-17"
    or data.get("official_openai_endpoint")
    != "https://api.openai.com/v1/chat/completions"
    or data.get("pricing_evidence_sha256")
    != "41e6f74aab48e82f3854fff2c6a6425a4b7c13879dc3006674526d9190a41870"
    or data.get("reasoning_tokens_billed_twice") is not False
    or data.get("prior_tracked_cumulative_maximum_usd") != "2.878283432"
    or data.get("new_canary_process_fuse_usd") != "0.611152"
    or data.get("new_arm_process_fuses_usd") != "4.90"
    or data.get("new_allocation_usd") != "5.511152"
    or data.get("tracked_cumulative_maximum_usd") != "8.389435432"
    or not isinstance(attestation, dict)
    or attestation.get("available_usd") != "30.00"
    or attestation.get("source") != "user-provided"
    or attestation.get("verification") != "not_api_verified"
    or attestation.get("provider_balance_endpoint_called") is not False
    or attestation.get("organization_admin_key_required_or_used") is not False
):
    raise SystemExit("V18 R3 local admission is missing or invalid")
PY
}

verify_canary() {
  "$PYTHON" -I -S -B - \
    "$REPORT/canary/evaluation/canary-result.json" \
    "$REPORT/canary/evaluation/openai-usage.jsonl" \
    "$REPORT/canary/evaluation/proxy-health-after.json" <<'PY'
import json
import sys
from decimal import Decimal
from pathlib import Path

model = "gpt-5.4-mini-2026-03-17"
result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
events = [
    json.loads(line)
    for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
    if line.strip()
]
health = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
calls = result.get("calls")
usage = health.get("usage")

def cost(event):
    prompt = event.get("prompt_tokens")
    cached = event.get("cached_tokens")
    completion = event.get("completion_tokens")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (prompt, cached, completion)):
        raise SystemExit("canary usage is not integral")
    if prompt <= 0 or completion <= 0 or not 0 <= cached <= prompt:
        raise SystemExit("canary usage is out of range")
    return (
        Decimal(prompt - cached) * Decimal("0.75")
        + Decimal(cached) * Decimal("0.075")
        + Decimal(completion) * Decimal("4.50")
    ) / Decimal(1_000_000)

if (
    result.get("schema_version") != "narratordb.route-canary.v1"
    or result.get("complete") is not True
    or result.get("same_model_self_judge") is not True
    or result.get("prompt_or_completion_content_retained") is not False
    or not isinstance(calls, list)
    or [call.get("label") for call in calls] != ["answerer", "judge"]
    or any(
        call.get("request_model") != model
        or call.get("response_model") != model
        or call.get("endpoint_provider_identity") != "OpenAI"
        or call.get("finish_reason") != "stop"
        or call.get("usage_validated_and_cost_computed") is not True
        or call.get("max_completion_tokens") != 128
        or call.get("reasoning_effort") != "high"
        or call.get("service_tier") != "default"
        or call.get("store") is not False
        or call.get("temperature_omitted") is not True
        or call.get("content_retained") is not False
        for call in calls
    )
    or len(events) != 2
    or any(
        event.get("event") != "completion"
        or event.get("endpoint_identity") != "api.openai.com/v1/chat/completions"
        or event.get("provider") != "OpenAI"
        or event.get("request_model") != model
        or event.get("response_model") != model
        or event.get("service_tier") != "default"
        or event.get("observed_finish_class") != "stop"
        or event.get("visible_content_state") != "nonempty"
        or event.get("response_complete") is not True
        or event.get("response_forwarded") is not True
        or event.get("attempt_number") != 1
        or event.get("retryable") is not False
        or event.get("discarded_reason") is not None
        or event.get("unknown_cost") is not False
        or event.get("upstream_request_id") in {None, "", "unknown"}
        or event.get("client_request_id") in {None, ""}
        for event in events
    )
    or any(Decimal(str(event.get("cost_usd"))) != cost(event) for event in events)
    or sum(Decimal(str(event.get("cost_usd"))) for event in events)
    > Decimal("0.611152")
    or not isinstance(usage, dict)
    or usage.get("calls") != 2
    or usage.get("errors") != 0
    or usage.get("malformed_responses") != 0
    or usage.get("terminal_rejections") != 0
    or usage.get("discarded_transients") != 0
    or usage.get("unknown_cost_attempts") != 0
    or usage.get("transport_failed") is not False
    or usage.get("fatal_reason_code") is not None
    or usage.get("pending_logical_calls") != 0
    or usage.get("active_logical_calls") != 0
    or Decimal(str(usage.get("reserved_cost_usd"))) != 0
):
    raise SystemExit("V18 R3 strict route canary is incomplete or mismatched")
PY
}

verify_transport_gate() {
  "$PYTHON" -I -S -B - "$1" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
policy = data.get("transport_policy")
usage = data.get("usage")
transients = usage.get("discarded_transients") if isinstance(usage, dict) else None
if (
    data.get("schema_version")
    != "narratordb.v18-gpt-selfjudge-transport-arm-audit.r3.v1"
    or data.get("authorized") is not True
    or data.get("score_values_present") is not False
    or data.get("score_driven_branching") is not False
    or data.get("official_harness_score_complete") is not True
    or data.get("expected_questions") != 42
    or data.get("cutoffs") != ["top_20", "top_50"]
    or data.get("failures") != []
    or not isinstance(policy, dict)
    or policy.get("successful_calls_required") != 168
    or policy.get("discarded_transients_maximum") != 4
    or policy.get("operator_selective_retries") is not False
    or policy.get("fatal_health_watchdog") is not True
    or not isinstance(usage, dict)
    or usage.get("successful_forwarded_official_openai_stop_calls") != 168
    or isinstance(transients, bool)
    or not isinstance(transients, int)
    or not 0 <= transients <= 4
):
    raise SystemExit("R3 score-blind transport gate is incomplete")
PY
}

verify_health() {
  "$PYTHON" -I -S -B - \
    "$1" "$2" "$MAX" "$RESERVE" "$SAFETY" "$TOKEN_LIMIT" \
    "$UPSTREAM_TIMEOUT" "$MAX_REQUEST_BYTES" "$MAX_RESPONSE_BYTES" <<'PY'
import json
import sys
from decimal import Decimal
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
phase = sys.argv[2]
usage = data.get("usage")
if (
    data.get("ok") is not True
    or data.get("upstream") != "https://api.openai.com/v1/chat/completions"
    or data.get("endpoint_identity") != "api.openai.com/v1/chat/completions"
    or data.get("provider_identity") != "OpenAI"
    or data.get("model") != "gpt-5.4-mini-2026-03-17"
    or data.get("max_completion_tokens") != int(sys.argv[6])
    or data.get("reasoning_effort") != "high"
    or data.get("service_tier") != "default"
    or data.get("store") is not False
    or data.get("n") != 1
    or Decimal(str(data.get("upstream_timeout_seconds"))) != Decimal(sys.argv[7])
    or data.get("max_request_bytes") != int(sys.argv[8])
    or data.get("max_response_bytes") != int(sys.argv[9])
    or data.get("direct_upstream_networking") is not True
    or data.get("environment_proxy_inheritance") is not False
    or data.get("inbound_retry_count_policy") != "absent-or-zero-only"
    or data.get("local_caller_auth_required") is not True
    or data.get("prompt_or_completion_content_retained") is not False
    or not isinstance(usage, dict)
    or Decimal(str(usage.get("max_cost_usd"))) != Decimal(sys.argv[3])
    or Decimal(str(usage.get("request_reservation_usd"))) != Decimal(sys.argv[4])
    or Decimal(str(usage.get("safety_reserve_usd"))) != Decimal(sys.argv[5])
    or usage.get("max_discarded_transients") != 4
    or usage.get("max_logical_attempts") != 5
    or usage.get("hidden_sdk_retry_rejections") != 0
    or usage.get("transport_failed") is not False
    or usage.get("fatal_reason_code") is not None
    or usage.get("terminal_rejections") != 0
    or usage.get("pending_logical_calls") != 0
    or usage.get("active_logical_calls") != 0
    or usage.get("scope") != "process"
    or usage.get("enforcement") != "hard_fuse"
    or Decimal(str(usage.get("reserved_cost_usd"))) != 0
):
    raise SystemExit("R3 official proxy health/configuration mismatch")
if phase == "before":
    if any(usage.get(field) != 0 for field in (
        "calls", "errors", "malformed_responses", "discarded_transients",
        "unknown_cost_attempts", "prompt_tokens", "cached_tokens",
        "completion_tokens", "reasoning_tokens",
    )) or Decimal(str(usage.get("cost_usd"))) != 0:
        raise SystemExit("R3 proxy was not fresh")
elif phase == "canary-after":
    if usage.get("calls") != 2 or usage.get("errors") != 0 or usage.get("discarded_transients") != 0:
        raise SystemExit("R3 canary proxy did not finish cleanly")
elif phase == "arm-after":
    discarded = usage.get("discarded_transients")
    if usage.get("calls") != 168 or isinstance(discarded, bool) or not isinstance(discarded, int) or not 0 <= discarded <= 4:
        raise SystemExit("R3 arm proxy did not finish cleanly")
else:
    raise SystemExit("unknown proxy health phase")
PY
}

# The supervisor is score-blind.  It reads only local proxy health and kills the
# whole child process group if the proxy dies, health becomes malformed, or the
# sticky transport-fatal sentinel trips.  It never reads evaluator artifacts.
run_with_watchdog() {
  local LOG_PATH=$1
  shift
  "$PYTHON" -I -S -B - "$PORT" "$LOG_PATH" "$@" <<'PY'
import json
import os
import signal
import subprocess
import sys
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

port = int(sys.argv[1])
log_path = sys.argv[2]
command = sys.argv[3:]
if not command:
    raise SystemExit("watchdog command is missing")
opener = build_opener(ProxyHandler({}))
log = None
if log_path != "-":
    path = Path(log_path)
    if path.exists() or path.is_symlink():
        raise SystemExit("watchdog log must start absent")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o444,
    )
    log = os.fdopen(descriptor, "wb")

process = subprocess.Popen(
    command,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    env=os.environ.copy(),
    start_new_session=True,
)

def copy_output():
    assert process.stdout is not None
    for block in iter(process.stdout.readline, b""):
        sys.stdout.buffer.write(block)
        sys.stdout.buffer.flush()
        if log is not None:
            log.write(block)
            log.flush()

pump = threading.Thread(target=copy_output, daemon=True)
pump.start()
fatal = None
health_failures = 0

def read_health():
    with opener.open(f"http://127.0.0.1:{port}/health", timeout=1) as response:
        raw = response.read(1024 * 1024 + 1)
    health = json.loads(raw)
    usage = health.get("usage") if isinstance(health, dict) else None
    if (
        len(raw) > 1024 * 1024
        or not isinstance(usage, dict)
        or health.get("ok") is not True
    ):
        raise ValueError("invalid health payload")
    return usage

while process.poll() is None:
    try:
        usage = read_health()
        health_failures = 0
        if usage.get("transport_failed") is True:
            code = usage.get("fatal_reason_code")
            fatal = code if isinstance(code, str) and code else "transport_failed"
        elif usage.get("transport_failed") is not False:
            fatal = "invalid_health"
    except Exception:
        health_failures += 1
        if health_failures >= 3:
            fatal = "health_unavailable"
    if fatal is not None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        break
    time.sleep(0.25)

returncode = process.wait()
pump.join(timeout=5)
if log is not None:
    log.flush()
    os.fsync(log.fileno())
    log.close()
if fatal is not None:
    # The proxy may still have another request in flight.  Keep it alive long
    # enough for its 105-second upstream timeout and ledger finally block, then
    # let the shell stop it.  This preserves conservative cost evidence.
    deadline = time.monotonic() + 112
    drained = False
    while time.monotonic() < deadline:
        try:
            usage = read_health()
            active = usage.get("active_logical_calls")
            reserved = Decimal(str(usage.get("reserved_cost_usd")))
            if active == 0 and reserved == 0:
                drained = True
                break
        except (Exception, InvalidOperation):
            pass
        time.sleep(0.25)
    print(
        json.dumps(
            {
                "watchdog": "fatal",
                "reason_code": fatal,
                "accounting_drained": drained,
            }
        ),
        file=sys.stderr,
    )
    raise SystemExit(97 if drained else 98)
raise SystemExit(returncode)
PY
}

verify_admission
USAGE="$RUN/evaluation/openai-usage.jsonl"
PROXY_LOG="$RUN/evaluation/proxy.log"
HEALTH_BEFORE="$RUN/evaluation/proxy-health-before.json"
HEALTH_AFTER="$RUN/evaluation/proxy-health-after.json"
if [[ $ACTION == canary ]]; then
  RESULT="$RUN/evaluation/canary-result.json"
  /bin/mkdir -p "$RUN/evaluation"
  for P in "$USAGE" "$PROXY_LOG" "$HEALTH_BEFORE" "$HEALTH_AFTER" "$RESULT"; do
    [[ ! -e $P && ! -L $P ]] || exit 1
  done
  MAX=0.611152
  RESERVE=0.300576
  SAFETY=0.01
  TOKEN_LIMIT=128
else
  EVALUATOR_LOG="$RUN/evaluation/evaluate.log"
  OFFICIAL="$RUN/evaluation/official-harness"
  HARNESS_RUNTIME="$RUN/evaluation/harness-runtime"
  COPY_MANIFEST="$OFFICIAL/frozen-copy-manifest.json"
  RAW_AUDIT="$RUN/evaluation-audit.json"
  TRANSPORT_AUDIT="$RUN/transport-arm-audit.json"
  if [[ $ACTION == primary ]]; then
    EXPECTED_COPY_SHA=6b4a949a15a842b1c2dfc9b101ffb1ba908c48c4667f559d077ec6294b403161
  else
    EXPECTED_COPY_SHA=28b3cadccad086e6dfab20c4a89fcf70b4292d8d3ecdf1b161be93c9e550ecac
    verify_transport_gate "$REPORT/primary/transport-arm-audit.json"
  fi
  verify_canary
  "$PYTHON" -I -S -B "$SCRIPT_DIR/verify_staged_copy.py" \
    --manifest "$COPY_MANIFEST" \
    --expected-manifest-sha256 "$EXPECTED_COPY_SHA"
  [[ -d "$OFFICIAL/predicted_$PROJECT" ]] || exit 1
  for P in "$USAGE" "$PROXY_LOG" "$HEALTH_BEFORE" "$HEALTH_AFTER" "$EVALUATOR_LOG" "$HARNESS_RUNTIME" "$RAW_AUDIT" "$TRANSPORT_AUDIT"; do
    [[ ! -e $P && ! -L $P ]] || exit 1
  done
  /bin/mkdir -p "$HARNESS_RUNTIME"
  /usr/bin/tar -xf "$HARNESS_TAR" -C "$HARNESS_RUNTIME"
  [[ ! -e "$HARNESS_RUNTIME/.env" && ! -L "$HARNESS_RUNTIME/.env" ]] || exit 1
  [[ -z $(/usr/bin/find "$HARNESS_RUNTIME" -name .env -print -quit) ]] || exit 1
  MAX=2.45
  RESERVE=0.318432
  SAFETY=0.01
  TOKEN_LIMIT=4096
fi
if /usr/sbin/lsof -nP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null | /usr/bin/grep -q .; then
  exit 1
fi

ENV_FILE=/Users/william/.narratordb/openai.env
[[ -f $ENV_FILE && ! -L $ENV_FILE ]] || exit 1
[[ $(/usr/bin/stat -f '%Lp:%u' "$ENV_FILE") == "600:$(/usr/bin/id -u)" ]] || exit 1
SECRET=""
LINES=0
while IFS= read -r LINE || [[ -n $LINE ]]; do
  [[ -n $LINE ]] || exit 1
  LINES=$((LINES + 1))
  case $LINE in
    OPENAI_API_KEY=*) VALUE=${LINE#OPENAI_API_KEY=} ;;
    *) exit 1 ;;
  esac
  [[ $VALUE == \"*\" && $VALUE == *\" ]] && VALUE=${VALUE:1:${#VALUE}-2}
  [[ $VALUE == \'*\' && $VALUE == *\' ]] && VALUE=${VALUE:1:${#VALUE}-2}
  SECRET=$VALUE
done < "$ENV_FILE"
LC_ALL=C
[[ $LINES -eq 1 && $SECRET =~ ^[[:graph:]]+$ ]] || exit 1
export -n SECRET

while IFS= read -r NAME; do unset "$NAME" 2>/dev/null || :; done < <(builtin compgen -e)
unset BASH_ENV ENV CDPATH GLOBIGNORE
PATH=/usr/bin:/bin:/usr/sbin:/sbin
HOME=/tmp
TMPDIR=/tmp
LANG=C
LC_ALL=C
NO_PROXY='*'
no_proxy='*'
PYTHONDONTWRITEBYTECODE=1
PYTHONNOUSERSITE=1
PYTHON_DOTENV_DISABLED=1
NARRATORDB_V18_R3_GPT_SELFJUDGE_PRECOMMIT_SHA256=$PUBLISHED
export PATH HOME TMPDIR LANG LC_ALL NO_PROXY no_proxy PYTHONDONTWRITEBYTECODE PYTHONNOUSERSITE PYTHON_DOTENV_DISABLED NARRATORDB_V18_R3_GPT_SELFJUDGE_PRECOMMIT_SHA256

PID=""
stop_proxy() {
  if [[ -n ${PID:-} ]] && /bin/kill -0 "$PID" 2>/dev/null; then
    /bin/kill -INT "$PID" 2>/dev/null || :
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      /bin/kill -0 "$PID" 2>/dev/null || break
      /bin/sleep .1
    done
    /bin/kill -0 "$PID" 2>/dev/null && /bin/kill -TERM "$PID" 2>/dev/null || :
    wait "$PID" 2>/dev/null || :
  fi
  PID=""
}
cleanup() {
  stop_proxy
}
trap cleanup EXIT INT TERM

OPENAI_API_KEY=$SECRET "$PYTHON" -I -S -B "$PROXY_SCRIPT" \
  --host 127.0.0.1 --port "$PORT" \
  --usage-log "$USAGE" --max-cost-usd "$MAX" \
  --request-reservation-usd "$RESERVE" \
  --budget-safety-reserve-usd "$SAFETY" \
  --max-completion-tokens "$TOKEN_LIMIT" \
  --timeout "$UPSTREAM_TIMEOUT" \
  --max-request-bytes "$MAX_REQUEST_BYTES" \
  --max-response-bytes "$MAX_RESPONSE_BYTES" \
  >"$PROXY_LOG" 2>&1 &
PID=$!
unset SECRET LINE VALUE NAME

READY=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45 46 47 48 49 50; do
  /bin/kill -0 "$PID" 2>/dev/null || exit 1
  if /usr/bin/curl -fsS "http://127.0.0.1:$PORT/health" >"$HEALTH_BEFORE" 2>/dev/null; then
    READY=1
    break
  fi
  /bin/sleep .1
done
[[ $READY -eq 1 ]] || exit 1
LISTENER_PIDS=$(/usr/sbin/lsof -nP -a -p "$PID" -iTCP:$PORT -sTCP:LISTEN -t | /usr/bin/sort -u)
[[ $LISTENER_PIDS == "$PID" ]] || exit 1
verify_health "$HEALTH_BEFORE" before

if [[ $ACTION == canary ]]; then
  run_with_watchdog - \
    "$PYTHON" -I -S -B "$SCRIPT_DIR/route_canary.py" \
    --base-url "http://127.0.0.1:$PORT/v1" --output "$RESULT"
  /usr/bin/curl -fsS "http://127.0.0.1:$PORT/health" >"$HEALTH_AFTER"
  verify_health "$HEALTH_AFTER" canary-after
  verify_canary
  stop_proxy
  exit 0
fi

PYTHONPATH="$HARNESS_RUNTIME" \
OPENAI_API_KEY=local-transport \
OPENAI_BASE_URL="http://127.0.0.1:$PORT/v1" \
  run_with_watchdog "$EVALUATOR_LOG" \
  "$PYTHON" -m benchmarks.longmemeval.run \
  --project-name "$PROJECT" \
  --dataset-path "$DATASET" \
  --all-questions \
  --evaluate-only \
  --rejudge \
  --run-id v7m42a1 \
  --mode answerer \
  --provider openai \
  --judge-provider openai \
  --answerer-model "$MODEL" \
  --judge-model "$MODEL" \
  --top-k 200 \
  --top-k-cutoffs 20,50 \
  --max-workers 2 \
  --rpm 30 \
  --seed 42 \
  --output-dir "$OFFICIAL"
/usr/bin/curl -fsS "http://127.0.0.1:$PORT/health" >"$HEALTH_AFTER"
verify_health "$HEALTH_AFTER" arm-after
stop_proxy
