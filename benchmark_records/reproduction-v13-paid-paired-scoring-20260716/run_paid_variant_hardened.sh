#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 VARIANT_RUN_ROOT PROJECT_NAME DATASET" >&2
  exit 2
fi
if [[ -z ${OPENROUTER_API_KEY:-} ]]; then
  echo "runtime OpenRouter environment is missing" >&2
  exit 1
fi
RUNTIME_OPENROUTER_KEY=$OPENROUTER_API_KEY
unset OPENROUTER_API_KEY

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
RUN=$1
PROJECT=$2
DATASET=$3
V11_SOURCE="$ROOT/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-20260716/attempt1/tools/v11-source"
HARNESS_SOURCE="$ROOT/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-20260716/attempt1/tools/harness-source"
USAGE="$RUN/evaluation/openrouter-usage.jsonl"
PROXY_LOG="$RUN/evaluation/proxy.log"
EVALUATOR_LOG="$RUN/evaluation/evaluate.log"
HEALTH="$RUN/evaluation/proxy-health-before.json"
STATUS="$RUN/evaluation/attempt-status.json"
PROXY_PID=""
EVALUATOR_STATUS="not_started"
FINAL_STATUS="failed_before_evaluator"

if [[ ! -d $V11_SOURCE/narratordb || ! -d $HARNESS_SOURCE/benchmarks ]]; then
  echo "frozen transport and harness sources must be extracted before execution" >&2
  exit 1
fi
if [[ ! -d $RUN/evaluation/official-harness/predicted_$PROJECT ]]; then
  echo "fresh working prediction copy is missing" >&2
  exit 1
fi
PYTHONPATH="$HARNESS_SOURCE" "$ROOT/vendor/memory-benchmarks/.venv/bin/python" - <<'PY'
import inspect
from benchmarks.common.llm_client import LLMClient

constructor = inspect.signature(LLMClient.__init__).parameters
generate = inspect.signature(LLMClient.generate).parameters
structured = inspect.signature(LLMClient.generate_structured).parameters
assert constructor["max_retries"].default == 5
assert constructor["timeout"].default == 120.0
assert generate["temperature"].default == 0
assert generate["max_tokens"].default == 4096
assert structured["temperature"].default == 0
assert structured["max_tokens"].default == 4096
PY
for artifact in "$USAGE" "$PROXY_LOG" "$EVALUATOR_LOG" "$HEALTH" "$STATUS"; do
  if [[ -e $artifact ]]; then
    echo "evaluator artifact must be fresh: $artifact" >&2
    exit 1
  fi
done
if lsof -nP -iTCP:8890 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port 8890 is already in use" >&2
  exit 1
fi

cleanup_proxy() {
  if [[ -n $PROXY_PID ]] && kill -0 "$PROXY_PID" 2>/dev/null; then
    kill -INT "$PROXY_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$PROXY_PID" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$PROXY_PID" 2>/dev/null; then
      kill -TERM "$PROXY_PID" 2>/dev/null || true
    fi
    wait "$PROXY_PID" 2>/dev/null || true
  fi
}

finish() {
  exit_status=$?
  trap - EXIT INT TERM
  cleanup_proxy
  mkdir -p "$(dirname -- "$STATUS")"
  printf '{"evaluator_status":"%s","exit_status":%d,"final_status":"%s","project":"%s","schema_version":"narratordb.v13-paid-variant-attempt-status.v1"}\n' \
    "$EVALUATOR_STATUS" "$exit_status" "$FINAL_STATUS" "$PROJECT" >"$STATUS"
  exit "$exit_status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# The real provider credential is inherited only by this proxy subshell.  Reset
# background signal dispositions, then replace the subshell with the proxy.
(
  trap - INT TERM
  export OPENROUTER_API_KEY="$RUNTIME_OPENROUTER_KEY"
  unset RUNTIME_OPENROUTER_KEY
  export PYTHONPATH="$V11_SOURCE"
  exec "$ROOT/.venv/bin/python" -m narratordb.benchmarks.openrouter_proxy \
    --host 127.0.0.1 \
    --port 8890 \
    --provider-allow DeepInfra,StreamLake,GMICloud,Baidu,AtlasCloud \
    --reasoning-effort high \
    --usage-log "$USAGE" \
    --max-cost-usd 1.25 \
    --request-reservation-usd 0.05 \
    --budget-safety-reserve-usd 0.01 \
    --timeout 180 \
    >"$PROXY_LOG" 2>&1
) &
PROXY_PID=$!

# Do not retain the real provider credential in the orchestration or harness
# environment after the proxy process has inherited its private copy.
unset RUNTIME_OPENROUTER_KEY OPENROUTER_API_KEY

READY=0
for _ in $(seq 1 50); do
  if curl -fsS http://127.0.0.1:8890/health >"$HEALTH" 2>/dev/null; then
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
  .usage.max_cost_usd == 1.25 and
  .usage.request_reservation_usd == 0.05 and
  .usage.safety_reserve_usd == 0.01
' "$HEALTH" >/dev/null

EVALUATOR_STATUS="running"
FINAL_STATUS="evaluator_failed"
set +e
env \
  -u OPENROUTER_API_KEY \
  -u ANTHROPIC_API_KEY \
  -u GOOGLE_API_KEY \
  -u GEMINI_API_KEY \
  PYTHON_DOTENV_DISABLED=1 \
  PYTHONPATH="$HARNESS_SOURCE" \
  OPENAI_API_KEY=local-transport \
  OPENAI_BASE_URL=http://127.0.0.1:8890/v1 \
  "$ROOT/vendor/memory-benchmarks/.venv/bin/python" \
  -m benchmarks.longmemeval.run \
  --project-name "$PROJECT" \
  --dataset-path "$DATASET" \
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
  --output-dir "$RUN/evaluation/official-harness" \
  2>&1 | tee "$EVALUATOR_LOG"
EVALUATOR_STATUS=${PIPESTATUS[0]}
set -e
if [[ $EVALUATOR_STATUS -ne 0 ]]; then
  exit "$EVALUATOR_STATUS"
fi
FINAL_STATUS="completed"
