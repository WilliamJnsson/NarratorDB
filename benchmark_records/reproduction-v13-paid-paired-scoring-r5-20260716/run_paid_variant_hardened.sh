#!/bin/bash -p

# The wrapper itself is credential-bearing only long enough to retain one
# non-exported in-memory copy for the proxy.  No command substitution,
# external program, startup hook, or verifier runs before this builtin-only
# copy-and-unset sequence.
if [[ -z ${OPENROUTER_API_KEY+x} || -z $OPENROUTER_API_KEY ]]; then
  builtin printf '%s\n' "runtime OpenRouter environment is missing" >&2
  exit 1
fi
set +a
unset RUNTIME_OPENROUTER_KEY
RUNTIME_OPENROUTER_KEY=$OPENROUTER_API_KEY
export -n RUNTIME_OPENROUTER_KEY
unset OPENROUTER_API_KEY

set -euo pipefail
unset BASH_ENV ENV CDPATH GLOBIGNORE

if [[ $# -ne 3 ]]; then
  echo "usage: $0 EXACT_VARIANT_RUN_ROOT EXACT_PROJECT_NAME EXACT_DATASET" >&2
  exit 2
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
REQUIREMENTS="$SCRIPT_DIR/execution-authorization-requirements.json"
REVISION_MANIFEST="$SCRIPT_DIR/SHA256SUMS"
BOUND_INPUTS="$SCRIPT_DIR/BOUND_INPUTS_SHA256SUMS"
RUN=$1
PROJECT=$2
DATASET=$3

V7_RUN="reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1/v7-control"
V7_PROJECT="narratordb-intelligence-dev42-v7-gpt54mini"
V13_RUN="reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1/v13-first"
V13_PROJECT="narratordb-intelligence-dev42-v13-replay-v7gpt54mini"
FIXED_DATASET="reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/longmemeval_s_dev42.json"
V7_FROZEN="reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-20260716/paired-evaluation/v7-control/evaluation/official-harness/predicted_narratordb-intelligence-dev42-v7-gpt54mini"
V13_FROZEN="reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-20260716/paired-evaluation/v13-first/evaluation/official-harness/predicted_narratordb-intelligence-dev42-v13-replay-v7gpt54mini"

case "$RUN|$PROJECT|$DATASET" in
  "$V7_RUN|$V7_PROJECT|$FIXED_DATASET")
    PHASE="before-v7"
    FROZEN_PREDICTIONS="$ROOT/$V7_FROZEN"
    ;;
  "$V13_RUN|$V13_PROJECT|$FIXED_DATASET")
    PHASE="before-v13"
    FROZEN_PREDICTIONS="$ROOT/$V13_FROZEN"
    ;;
  *)
    echo "paid evaluator arguments are not one exact precommitted tuple" >&2
    exit 1
    ;;
esac

if [[ ! ${NARRATORDB_PAID_PRECOMMIT_SHA256:-} =~ ^[0-9a-f]{64}$ ]]; then
  echo "externally published replacement-precommit SHA-256 is missing" >&2
  exit 1
fi
if [[ ! -f $REVISION_MANIFEST || ! -f $BOUND_INPUTS ]]; then
  echo "replacement precommit is not finally sealed" >&2
  exit 1
fi
COMPUTED_PRECOMMIT_SHA256=$(shasum -a 256 "$REVISION_MANIFEST" | awk '{print $1}')
if [[ $COMPUTED_PRECOMMIT_SHA256 != "$NARRATORDB_PAID_PRECOMMIT_SHA256" ]]; then
  echo "published replacement-precommit SHA-256 does not match the local seal" >&2
  exit 1
fi
(
  cd "$SCRIPT_DIR"
  shasum -a 256 -c SHA256SUMS
  if find . -mindepth 1 ! -type f -print -quit | grep -q .; then
    echo "replacement precommit must be a flat tree of regular files" >&2
    exit 1
  fi
  if find . -type f -links +1 -print -quit | grep -q .; then
    echo "replacement precommit contains a hard-linked file" >&2
    exit 1
  fi
  if find . -type f -name .DS_Store -print -quit | grep -q .; then
    echo "replacement precommit contains forbidden Finder metadata" >&2
    exit 1
  fi
  if find . -type f \( -name sitecustomize.py -o -name usercustomize.py -o -name '*.pyc' \) -print -quit | grep -q .; then
    echo "replacement precommit contains a forbidden Python startup artifact" >&2
    exit 1
  fi
  if ! diff -u \
    <(awk '{print $2}' SHA256SUMS | LC_ALL=C sort) \
    <(find . -type f ! -path './SHA256SUMS' -print | sed 's#^\./##' | LC_ALL=C sort); then
    echo "replacement precommit closed-world inventory mismatch" >&2
    exit 1
  fi
)
(
  cd "$ROOT"
  shasum -a 256 -c "$BOUND_INPUTS"
)

RUN_ABSOLUTE="$ROOT/$RUN"
V11_SOURCE="$ROOT/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1/tools/v11-source"
HARNESS_SOURCE="$ROOT/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1/tools/harness-source"
HARNESS_GUARD="$SCRIPT_DIR/run_harness_guarded.py"
PROXY_GUARD="$SCRIPT_DIR/run_openrouter_proxy_guarded.py"
ARM_GATE_GUARD="$SCRIPT_DIR/run_arm_gate_guarded.py"
HARNESS_SITE_PACKAGES="$ROOT/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1/tools/harness-site-packages"
USAGE="$RUN_ABSOLUTE/evaluation/openrouter-usage.jsonl"
PROXY_LOG="$RUN_ABSOLUTE/evaluation/proxy.log"
EVALUATOR_LOG="$RUN_ABSOLUTE/evaluation/evaluate.log"
HEALTH="$RUN_ABSOLUTE/evaluation/proxy-health-before.json"
STATUS="$RUN_ABSOLUTE/evaluation/attempt-status.json"
ADMISSION="$RUN_ABSOLUTE/evaluation/admission-verification-$PHASE.json"
LEDGER_AUDIT="$RUN_ABSOLUTE/evaluation/ledger-identity-audit.json"
ARM_GATE="$RUN_ABSOLUTE/evaluation/arm-gate.json"
PROXY_PID=""
PROXY_DRAIN_ATTEMPTED=0
PROXY_DRAINED=0
EVALUATOR_STATUS="not_started"
FINAL_STATUS="failed_before_evaluator"

drain_proxy() {
  PROXY_DRAIN_ATTEMPTED=1
  if [[ -z $PROXY_PID ]] || ! kill -0 "$PROXY_PID" 2>/dev/null; then
    PROXY_DRAINED=1
    return 0
  fi
  for _ in $(seq 1 1900); do
    if curl --noproxy '*' -fsS http://127.0.0.1:8890/health 2>/dev/null | \
      jq -e '.usage.reserved_cost_usd == 0' >/dev/null 2>&1; then
      PROXY_DRAINED=1
      return 0
    fi
    sleep 0.1
  done
  return 1
}

cleanup_proxy() {
  if [[ -n $PROXY_PID ]] && kill -0 "$PROXY_PID" 2>/dev/null; then
    if [[ $PROXY_DRAIN_ATTEMPTED -eq 0 ]]; then
      drain_proxy || true
    fi
    kill -INT "$PROXY_PID" 2>/dev/null || true
    for _ in $(seq 1 100); do
      kill -0 "$PROXY_PID" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$PROXY_PID" 2>/dev/null; then
      kill -TERM "$PROXY_PID" 2>/dev/null || true
    fi
    wait "$PROXY_PID" 2>/dev/null || true
  fi
  PROXY_PID=""
}

finish() {
  exit_status=$?
  trap - EXIT INT TERM
  cleanup_proxy
  unset RUNTIME_OPENROUTER_KEY OPENROUTER_API_KEY
  mkdir -p "$(dirname -- "$STATUS")"
  if [[ ! -e $STATUS ]]; then
    printf '{"evaluator_status":"%s","exit_status":%d,"final_status":"%s","phase":"%s","project":"%s","schema_version":"narratordb.v13-paid-variant-attempt-status.v2"}\n' \
      "$EVALUATOR_STATUS" "$exit_status" "$FINAL_STATUS" "$PHASE" "$PROJECT" >"$STATUS"
  fi
  if [[ -d $RUN_ABSOLUTE/evaluation/official-harness ]]; then
    chmod -R a-w "$RUN_ABSOLUTE/evaluation/official-harness" || exit_status=1
  fi
  for preserved in \
    "$USAGE" "$PROXY_LOG" "$HEALTH" "$EVALUATOR_LOG" \
    "$ADMISSION" "$LEDGER_AUDIT" "$ARM_GATE"; do
    if [[ -e $preserved ]]; then
      chmod a-w "$preserved" || exit_status=1
    fi
  done
  if [[ -e $STATUS ]]; then
    chmod a-w "$STATUS" || exit_status=1
  fi
  exit "$exit_status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! -d $V11_SOURCE/narratordb || ! -d $HARNESS_SOURCE/benchmarks || ! -d $HARNESS_SITE_PACKAGES ]]; then
  echo "frozen support and harness sources must be extracted before execution" >&2
  exit 1
fi
chmod -R a-w "$V11_SOURCE" "$HARNESS_SOURCE" "$HARNESS_SITE_PACKAGES"
if [[ ! -d $RUN_ABSOLUTE/evaluation/official-harness/predicted_$PROJECT ]]; then
  echo "fresh working prediction copy is missing" >&2
  exit 1
fi
if [[ ! -f $USAGE || -L $USAGE ]]; then
  echo "predeclared initial evaluator ledger is missing or unsafe" >&2
  exit 1
fi
for artifact in "$PROXY_LOG" "$EVALUATOR_LOG" "$HEALTH" "$STATUS" "$ADMISSION" "$LEDGER_AUDIT" "$ARM_GATE"; do
  if [[ -e $artifact || -L $artifact ]]; then
    echo "paid evaluator artifact must be fresh: $artifact" >&2
    exit 1
  fi
done
if lsof -nP -iTCP:8890 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port 8890 is already in use" >&2
  exit 1
fi

env -i LANG=C LC_ALL=C PYTHONDONTWRITEBYTECODE=1 \
  "$ROOT/vendor/memory-benchmarks/.venv/bin/python" -I -S -B "$SCRIPT_DIR/verify_dynamic_admission.py" verify \
    --repository-root "$ROOT" \
    --requirements "$REQUIREMENTS" \
    --phase "$PHASE" \
    --run-root "$RUN" \
    --project-name "$PROJECT" \
    --dataset-path "$DATASET" \
    --published-precommit-sha256 "$COMPUTED_PRECOMMIT_SHA256" \
    >"$ADMISSION"
jq -e \
  --arg phase "$PHASE" \
  --arg revision "$COMPUTED_PRECOMMIT_SHA256" \
  '.ok == true and .phase == $phase and .revision_precommit_sha256 == $revision and .credential_recorded == false and .model_content_recorded == false' \
  "$ADMISSION" >/dev/null
chmod a-w "$ADMISSION"

env -i LANG=C LC_ALL=C PYTHONDONTWRITEBYTECODE=1 \
  NARRATORDB_EXPECTED_HARNESS_SOURCE="$HARNESS_SOURCE" \
  NARRATORDB_HARNESS_SITE_PACKAGES="$HARNESS_SITE_PACKAGES" \
  OPENAI_API_KEY=local-transport \
  OPENAI_BASE_URL=http://127.0.0.1:8890/v1 \
  NO_PROXY=127.0.0.1,localhost \
  no_proxy=127.0.0.1,localhost \
  "$ROOT/vendor/memory-benchmarks/.venv/bin/python" -I -S -B \
  "$HARNESS_GUARD" --narratordb-preflight >/dev/null

# The real provider credential is exported only inside this proxy subshell.
# All environment clearing is performed with Bash builtins while the retained
# key remains non-exported; there is no external env intermediary and the key
# never appears in an argument vector.
(
  trap - INT TERM
  set +a
  unset OPENROUTER_API_KEY
  while IFS= read -r EXPORTED_NAME; do
    unset "$EXPORTED_NAME" 2>/dev/null || :
  done < <(builtin compgen -e)
  unset BASH_ENV ENV CDPATH GLOBIGNORE
  LANG=C
  LC_ALL=C
  NO_PROXY='*'
  no_proxy='*'
  PYTHONDONTWRITEBYTECODE=1
  OPENROUTER_API_KEY=$RUNTIME_OPENROUTER_KEY
  export LANG LC_ALL NO_PROXY no_proxy PYTHONDONTWRITEBYTECODE OPENROUTER_API_KEY
  unset RUNTIME_OPENROUTER_KEY EXPORTED_NAME
  exec "$ROOT/vendor/memory-benchmarks/.venv/bin/python" -I -S -B "$PROXY_GUARD" \
    --host 127.0.0.1 \
    --port 8890 \
    --provider-allow DeepInfra,StreamLake,GMICloud,Baidu,AtlasCloud \
    --reasoning-effort high \
    --usage-log "$USAGE" \
    --max-cost-usd 2.50 \
    --request-reservation-usd 0.05 \
    --budget-safety-reserve-usd 0.01 \
    --timeout 180 \
    >"$PROXY_LOG" 2>&1
) &
PROXY_PID=$!
unset RUNTIME_OPENROUTER_KEY OPENROUTER_API_KEY

READY=0
for _ in $(seq 1 50); do
  if curl --noproxy '*' -fsS http://127.0.0.1:8890/health >"$HEALTH" 2>/dev/null; then
    READY=1
    break
  fi
  sleep 0.1
done
if [[ $READY -ne 1 ]]; then
  echo "evaluator proxy failed to start" >&2
  exit 1
fi
jq -e '
  .ok == true and
  .provider_allow == ["DeepInfra", "StreamLake", "GMICloud", "Baidu", "AtlasCloud"] and
  .reasoning_effort == "high" and
  .usage.cost_usd == 0 and
  .usage.max_cost_usd == 2.50 and
  .usage.request_reservation_usd == 0.05 and
  .usage.safety_reserve_usd == 0.01 and
  .usage.unknown_cost_attempts == 0
' "$HEALTH" >/dev/null

EVALUATOR_STATUS="running"
FINAL_STATUS="evaluator_failed"
set +e
env -i \
  LANG=C \
  LC_ALL=C \
  PYTHONDONTWRITEBYTECODE=1 \
  NARRATORDB_EXPECTED_HARNESS_SOURCE="$HARNESS_SOURCE" \
  NARRATORDB_HARNESS_SITE_PACKAGES="$HARNESS_SITE_PACKAGES" \
  OPENAI_API_KEY=local-transport \
  OPENAI_BASE_URL=http://127.0.0.1:8890/v1 \
  NO_PROXY=127.0.0.1,localhost \
  no_proxy=127.0.0.1,localhost \
  "$ROOT/vendor/memory-benchmarks/.venv/bin/python" \
  -I -S -B "$HARNESS_GUARD" \
  --project-name "$PROJECT" \
  --dataset-path "$ROOT/$DATASET" \
  --all-questions \
  --evaluate-only \
  --rejudge \
  --run-id v7m42a1 \
  --mode answerer \
  --provider openai \
  --judge-provider openai \
  --answerer-model z-ai/glm-5.2 \
  --judge-model deepseek/deepseek-v4-flash-20260423 \
  --top-k 200 \
  --top-k-cutoffs 20,50 \
  --max-workers 10 \
  --rpm 60 \
  --seed 42 \
  --output-dir "$RUN_ABSOLUTE/evaluation/official-harness" \
  >"$EVALUATOR_LOG" 2>&1
EVALUATOR_STATUS=$?
set -e
if [[ $EVALUATOR_STATUS -ne 0 ]]; then
  exit "$EVALUATOR_STATUS"
fi
FINAL_STATUS="post_evaluator_verification_failed"
if ! drain_proxy; then
  FINAL_STATUS="proxy_drain_timeout"
  exit 1
fi
cleanup_proxy
PROXY_PID=""
chmod -R a-w "$RUN_ABSOLUTE/evaluation/official-harness"
chmod a-w "$USAGE" "$EVALUATOR_LOG" "$PROXY_LOG" "$HEALTH"

(umask 222; set -o noclobber; env -i \
  LANG=C LC_ALL=C PYTHONDONTWRITEBYTECODE=1 \
  "$ROOT/vendor/memory-benchmarks/.venv/bin/python" -I -S -B "$SCRIPT_DIR/verify_evaluator_ledger.py" \
    --ledger "$USAGE" >"$LEDGER_AUDIT")
jq -e '
  .complete == true and
  .credential_material_recorded == false and
  .model_content_recorded == false and
  .unknown_cost_attempts == 0 and
  .request_models == [
    "deepseek/deepseek-v4-flash-20260423",
    "z-ai/glm-5.2"
  ]
' "$LEDGER_AUDIT" >/dev/null

FINAL_STATUS="arm_gate_failed"
set +e
env -i LANG=C LC_ALL=C PYTHONDONTWRITEBYTECODE=1 \
  "$ROOT/vendor/memory-benchmarks/.venv/bin/python" -I -S -B "$ARM_GATE_GUARD" \
    --evaluated-directory "$RUN_ABSOLUTE/evaluation/official-harness/predicted_$PROJECT" \
    --frozen-directory "$FROZEN_PREDICTIONS" \
    --usage-log "$USAGE" \
    --evaluator-log "$EVALUATOR_LOG" \
    --expected-questions 42 \
    --cutoffs 20,50 \
    --question-id-file "$ROOT/reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/dev42_question_ids.json" \
    --allowed-request-model z-ai/glm-5.2 \
    --allowed-request-model deepseek/deepseek-v4-flash-20260423 \
    --allowed-provider DeepInfra \
    --allowed-provider StreamLake \
    --allowed-provider GMICloud \
    --allowed-provider Baidu \
    --allowed-provider AtlasCloud \
    --max-cost-usd 2.50 \
    --output "$ARM_GATE" >/dev/null
ARM_GATE_STATUS=$?
set -e
if [[ $ARM_GATE_STATUS -ne 0 ]]; then
  exit "$ARM_GATE_STATUS"
fi
jq -e '
  .schema_version == "narratordb.arm-evaluation-gate.v1" and
  .authorized == true and
  .complete == true and
  .official_harness_score_complete == true and
  .expected_questions == 42 and
  .evaluated_questions == 42 and
  .frozen_questions == 42 and
  .cutoffs == ["top_20", "top_50"] and
  .denominators == {"top_20": 42, "top_50": 42} and
  ([.validation_counts[]] | all(. == 0)) and
  .usage.publication_ready == true and
  .usage.unknown_cost_attempts == 0 and
  .usage.invalid_completion_identities == 0
' "$ARM_GATE" >/dev/null
FINAL_STATUS="completed"
