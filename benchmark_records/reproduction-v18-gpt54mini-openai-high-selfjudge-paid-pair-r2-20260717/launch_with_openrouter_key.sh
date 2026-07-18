#!/bin/bash -p
set -euo pipefail

[[ $# -eq 1 ]] || exit 2
ACTION=$1
case $ACTION in
  telemetry-pre|canary|primary|telemetry-between|replication|telemetry-post) ;;
  *) exit 2 ;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
[[ $(pwd -P) == "$ROOT" ]] || exit 1
PRECOMMIT="$ROOT/benchmark_records/precommits/longmemeval_dev42_v18_gpt54mini_openai_high_selfjudge_paid_pair_r2_precommit_20260717.json"
PUBLISHED=${NARRATORDB_V18_R2_GPT_SELFJUDGE_PRECOMMIT_SHA256:-}
[[ $PUBLISHED =~ ^[0-9a-f]{64}$ ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$PRECOMMIT" | /usr/bin/awk '{print $1}') == "$PUBLISHED" ]] || exit 1
(cd "$SCRIPT_DIR" && /usr/bin/shasum -a 256 -c SEALED_FILES_SHA256SUMS)
(cd "$ROOT" && /usr/bin/shasum -a 256 -c "$SCRIPT_DIR/BOUND_INPUTS_SHA256SUMS")

PYTHON="$ROOT/vendor/memory-benchmarks/.venv/bin/python"
[[ -L $PYTHON ]] || exit 1
[[ $(/usr/bin/readlink "$PYTHON") == "/Users/william/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12" ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$PYTHON" | /usr/bin/awk '{print $1}') == 7b05d803bbc1bbfc81644af4faf2b88f0a37b8de96b9f42c1e08033e2cd0848a ]] || exit 1

REPORT="$ROOT/reports/longmemeval-intelligence-dev42-v18-gpt54mini-openai-high-selfjudge-paid-pair-r2-20260717"
PROJECT=narratordb-intelligence-dev42-v18-replay-v7gpt54mini-repeat2-attempt2
DATASET="$ROOT/reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/longmemeval_s_dev42.json"
IDS="$ROOT/reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/dev42_question_ids.json"
SOURCE_ROOT="$ROOT/reports/longmemeval-intelligence-dev42-v18-replay-v7gpt54mini-repeat2-attempt2-20260717"
HARNESS_TAR="$SOURCE_ROOT/harness-source.tar"
PROXY_SCRIPT="$SCRIPT_DIR/run_openrouter_proxy_guarded.py"
ADMISSION="$REPORT/preflight/dynamic-admission.json"
PORT=8892
UPSTREAM_TIMEOUT=105
MAX_REQUEST_BYTES=20971520
MAX_RESPONSE_BYTES=4194304

case $ACTION in
  telemetry-pre) OUTPUT="$REPORT/preflight/provider-telemetry-pre.json" ;;
  telemetry-between) OUTPUT="$REPORT/between/provider-telemetry-between.json" ;;
  telemetry-post) OUTPUT="$REPORT/postrun/provider-telemetry-post.json" ;;
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
if (
    data.get("schema_version")
    != "narratordb.v18-gpt-selfjudge-campaign-admission.r2.v1"
    or data.get("admitted") is not True
    or not isinstance(checks, dict)
    or not checks
    or not all(value is True for value in checks.values())
    or data.get("credential_recorded") is not False
    or data.get("model_content_recorded") is not False
    or data.get("prior_r1_cumulative_conservative_exposure_usd")
    != "1.914605682"
    or data.get("tracked_cumulative_maximum_usd") != "6.894090250"
):
    raise SystemExit("V18 r2 dynamic admission is missing or invalid")
PY
}

verify_canary() {
  "$PYTHON" -I -S -B - \
    "$REPORT/canary/evaluation/canary-result.json" \
    "$REPORT/canary/evaluation/openrouter-usage.jsonl" \
    "$REPORT/canary/evaluation/proxy-health-after.json" <<'PY'
import json
import sys
from decimal import Decimal
from pathlib import Path

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
events = [
    json.loads(line)
    for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
    if line.strip()
]
health = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
calls = result.get("calls")
usage = health.get("usage")
if (
    result.get("schema_version") != "narratordb.route-canary.v1"
    or result.get("complete") is not True
    or result.get("same_model_self_judge") is not True
    or result.get("prompt_or_completion_content_retained") is not False
    or not isinstance(calls, list)
    or [call.get("label") for call in calls] != ["answerer", "judge"]
    or any(
        call.get("request_model") != "openai/gpt-5.4-mini"
        or call.get("response_model") != "openai/gpt-5.4-mini"
        or call.get("finish_reason") != "stop"
        or call.get("temperature_omitted") is not True
        or call.get("content_retained") is not False
        for call in calls
    )
    or len(events) != 2
    or any(
        event.get("event") != "completion"
        or event.get("request_model") != "openai/gpt-5.4-mini"
        or event.get("response_model") != "openai/gpt-5.4-mini"
        or event.get("provider") != "OpenAI"
        or event.get("finish_reason") != "stop"
        or event.get("response_complete") is not True
        or event.get("response_forwarded") is not True
        or event.get("attempt_number") != 1
        or event.get("retryable") is not False
        or event.get("discarded_reason") is not None
        or event.get("unknown_cost") is not False
        for event in events
    )
    or sum(Decimal(str(event.get("cost_usd"))) for event in events)
    > Decimal("0.079484568")
    or not isinstance(usage, dict)
    or usage.get("calls") != 2
    or usage.get("errors") != 0
    or usage.get("discarded_transients") != 0
    or usage.get("transport_failed") is not False
    or usage.get("pending_logical_calls") != 0
    or usage.get("active_logical_calls") != 0
    or Decimal(str(usage.get("reserved_cost_usd"))) != 0
):
    raise SystemExit("V18 r2 strict route canary is incomplete or mismatched")
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
    != "narratordb.v18-gpt-selfjudge-transport-arm-audit.r2.v1"
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
    or policy.get("discarded_transients_never_used") is not True
    or policy.get("operator_selective_retries") is not False
    or not isinstance(usage, dict)
    or usage.get("successful_forwarded_openai_gpt54mini_stop_calls") != 168
    or isinstance(transients, bool)
    or not isinstance(transients, int)
    or not 0 <= transients <= 4
):
    raise SystemExit("r2 score-blind transport gate is incomplete")
PY
}

verify_health() {
  "$PYTHON" -I -S -B - \
    "$1" "$2" "$MAX" "$RESERVE" "$SAFETY" "$UPSTREAM_TIMEOUT" \
    "$MAX_REQUEST_BYTES" "$MAX_RESPONSE_BYTES" <<'PY'
import json
import sys
from decimal import Decimal
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
phase = sys.argv[2]
usage = data.get("usage")
if (
    data.get("ok") is not True
    or data.get("provider_only") != "OpenAI"
    or data.get("provider_allow") != []
    or data.get("model_routes")
    != {"openai/gpt-5.4-mini": ["OpenAI"]}
    or data.get("model_output_token_parameters") != {}
    or data.get("model_omit_temperature") != ["openai/gpt-5.4-mini"]
    or data.get("model_reasoning_efforts")
    != {"openai/gpt-5.4-mini": "high"}
    or data.get("reasoning_effort") is not None
    or data.get("public_benchmark") is not True
    or Decimal(str(data.get("upstream_timeout_seconds"))) != Decimal(sys.argv[6])
    or data.get("max_request_bytes") != int(sys.argv[7])
    or data.get("max_response_bytes") != int(sys.argv[8])
    or data.get("direct_upstream_networking") is not True
    or data.get("inbound_retry_count_policy") != "absent-or-zero-only"
    or data.get("local_caller_auth_required") is not True
    or not isinstance(usage, dict)
    or Decimal(str(usage.get("max_cost_usd"))) != Decimal(sys.argv[3])
    or Decimal(str(usage.get("request_reservation_usd"))) != Decimal(sys.argv[4])
    or Decimal(str(usage.get("safety_reserve_usd"))) != Decimal(sys.argv[5])
    or usage.get("max_discarded_transients") != 4
    or usage.get("max_logical_attempts") != 5
    or usage.get("hidden_sdk_retry_rejections") != 0
    or usage.get("transport_failed") is not False
    or usage.get("pending_logical_calls") != 0
    or usage.get("active_logical_calls") != 0
    or Decimal(str(usage.get("reserved_cost_usd"))) != 0
):
    raise SystemExit("r2 local proxy health/configuration mismatch")
if phase == "before":
    if usage.get("calls") != 0 or usage.get("errors") != 0 or usage.get("discarded_transients") != 0:
        raise SystemExit("r2 proxy was not fresh")
elif phase == "canary-after":
    if usage.get("calls") != 2 or usage.get("errors") != 0 or usage.get("discarded_transients") != 0:
        raise SystemExit("r2 canary proxy did not finish cleanly")
elif phase == "arm-after":
    discarded = usage.get("discarded_transients")
    if usage.get("calls") != 168 or isinstance(discarded, bool) or not isinstance(discarded, int) or not 0 <= discarded <= 4:
        raise SystemExit("r2 arm proxy did not finish cleanly")
else:
    raise SystemExit("unknown proxy health phase")
PY
}

if [[ $ACTION == telemetry-* ]]; then
  [[ ! -e $OUTPUT && ! -L $OUTPUT ]] || exit 1
  case $ACTION in
    telemetry-between)
      verify_transport_gate "$REPORT/primary/transport-arm-audit.json"
      ;;
    telemetry-post)
      verify_transport_gate "$REPORT/replication/transport-arm-audit.json"
      ;;
  esac
  /bin/mkdir -p "$(/usr/bin/dirname "$OUTPUT")"
else
  verify_admission
  USAGE="$RUN/evaluation/openrouter-usage.jsonl"
  PROXY_LOG="$RUN/evaluation/proxy.log"
  HEALTH_BEFORE="$RUN/evaluation/proxy-health-before.json"
  HEALTH_AFTER="$RUN/evaluation/proxy-health-after.json"
  if [[ $ACTION == canary ]]; then
    RESULT="$RUN/evaluation/canary-result.json"
    /bin/mkdir -p "$RUN/evaluation"
    for P in "$USAGE" "$PROXY_LOG" "$HEALTH_BEFORE" "$HEALTH_AFTER" "$RESULT"; do
      [[ ! -e $P && ! -L $P ]] || exit 1
    done
  else
    EVALUATOR_LOG="$RUN/evaluation/evaluate.log"
    OFFICIAL="$RUN/evaluation/official-harness"
    HARNESS_RUNTIME="$RUN/evaluation/harness-runtime"
    COPY_MANIFEST="$OFFICIAL/frozen-copy-manifest.json"
    RAW_AUDIT="$RUN/evaluation-audit.json"
    TRANSPORT_AUDIT="$RUN/transport-arm-audit.json"
    if [[ $ACTION == primary ]]; then
      EXPECTED_COPY_SHA=846e8fa41bc5abd7de6f73b5237bb413e7db5a35330e5dd805ba4e57ac160959
    else
      EXPECTED_COPY_SHA=98866ed97c337718d32e2e18e50f22d8d0e3de58ba95b0ae0c887e4aceda1b71
      verify_transport_gate "$REPORT/primary/transport-arm-audit.json"
      [[ -f "$REPORT/between/provider-telemetry-between.json" ]] || exit 1
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
  fi
  if /usr/sbin/lsof -nP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null | /usr/bin/grep -q .; then
    exit 1
  fi
fi

ENV_FILE=/Users/william/.narratordb/openrouter.env
[[ -f $ENV_FILE && ! -L $ENV_FILE ]] || exit 1
[[ $(/usr/bin/stat -f '%Lp:%u' "$ENV_FILE") == "600:501" ]] || exit 1
SECRET=""
LINES=0
while IFS= read -r LINE || [[ -n $LINE ]]; do
  [[ -z $LINE ]] && continue
  LINES=$((LINES + 1))
  case $LINE in
    OPENROUTER_API_KEY=*) VALUE=${LINE#OPENROUTER_API_KEY=} ;;
    export\ OPENROUTER_API_KEY=*) VALUE=${LINE#export OPENROUTER_API_KEY=} ;;
    *) exit 1 ;;
  esac
  [[ $VALUE == \"*\" && $VALUE == *\" ]] && VALUE=${VALUE:1:${#VALUE}-2}
  [[ $VALUE == \'*\' && $VALUE == *\' ]] && VALUE=${VALUE:1:${#VALUE}-2}
  SECRET=$VALUE
done < "$ENV_FILE"
[[ $LINES -eq 1 && $SECRET =~ ^sk-or-[A-Za-z0-9_-]{30,}$ ]] || exit 1
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
NARRATORDB_V18_R2_GPT_SELFJUDGE_PRECOMMIT_SHA256=$PUBLISHED
export PATH HOME TMPDIR LANG LC_ALL NO_PROXY no_proxy PYTHONDONTWRITEBYTECODE PYTHONNOUSERSITE NARRATORDB_V18_R2_GPT_SELFJUDGE_PRECOMMIT_SHA256

if [[ $ACTION == telemetry-* ]]; then
  OPENROUTER_API_KEY=$SECRET
  export OPENROUTER_API_KEY
  unset SECRET LINE VALUE NAME
  exec "$PYTHON" -I -S -B "$SCRIPT_DIR/capture_provider_telemetry.py" \
    --output "$OUTPUT" --timeout 20
fi

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

if [[ $ACTION == canary ]]; then
  MAX=0.079484568
  RESERVE=0.005
  SAFETY=0.005
else
  MAX=2.45
  RESERVE=0.05
  SAFETY=0.01
fi

OPENROUTER_API_KEY=$SECRET "$PYTHON" -I -S -B "$PROXY_SCRIPT" \
  --host 127.0.0.1 --port "$PORT" --provider-only OpenAI \
  --model-route openai/gpt-5.4-mini=OpenAI \
  --model-omit-temperature openai/gpt-5.4-mini \
  --model-reasoning-effort openai/gpt-5.4-mini=high --public-benchmark \
  --usage-log "$USAGE" --max-cost-usd "$MAX" \
  --request-reservation-usd "$RESERVE" \
  --budget-safety-reserve-usd "$SAFETY" --timeout "$UPSTREAM_TIMEOUT" \
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
  --answerer-model openai/gpt-5.4-mini \
  --judge-model openai/gpt-5.4-mini \
  --top-k 200 \
  --top-k-cutoffs 20,50 \
  --max-workers 2 \
  --rpm 30 \
  --seed 42 \
  --output-dir "$OFFICIAL" \
  2>&1 | /usr/bin/tee "$EVALUATOR_LOG"
/usr/bin/curl -fsS "http://127.0.0.1:$PORT/health" >"$HEALTH_AFTER"
verify_health "$HEALTH_AFTER" arm-after
stop_proxy
