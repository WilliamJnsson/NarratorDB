#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 RUN_ROOT PROJECT_NAME DATASET" >&2
  exit 2
fi

ROOT=$(pwd)
RUN=$1
PROJECT=$2
DATASET=$3
PROVIDER_ONLY=${NARRATORDB_EVALUATOR_PROVIDER_ONLY:-}
MAX_WORKERS=${NARRATORDB_EVALUATOR_MAX_WORKERS:-10}
RPM=${NARRATORDB_EVALUATOR_RPM:-60}
PUBLIC_BENCHMARK=${NARRATORDB_EVALUATOR_PUBLIC_BENCHMARK:-0}
ANSWERER_MODEL=${NARRATORDB_EVALUATOR_ANSWERER_MODEL:-z-ai/glm-5.2}
MODEL_ROUTE=${NARRATORDB_EVALUATOR_MODEL_ROUTE:-}
REASONING_EFFORT=${NARRATORDB_EVALUATOR_REASONING_EFFORT:-high}
MODEL_REASONING_EFFORT=${NARRATORDB_EVALUATOR_MODEL_REASONING_EFFORT:-}
MAX_COST_USD=${NARRATORDB_EVALUATOR_MAX_COST_USD:-2.5}
USAGE="$RUN/evaluation/openrouter-usage.jsonl"
PROXY_LOG="$RUN/evaluation/proxy.log"
EVALUATOR_LOG="$RUN/evaluation/evaluate.log"
HEALTH="$RUN/evaluation/proxy-health-before.json"
PROXY_PID=""

if [[ -z ${OPENROUTER_API_KEY:-} ]]; then
  echo "runtime OpenRouter environment is missing" >&2
  exit 1
fi
if [[ -n $PROVIDER_ONLY && ! $PROVIDER_ONLY =~ ^(DeepInfra|StreamLake|GMICloud|Baidu|AtlasCloud)$ ]]; then
  echo "unsupported evaluator provider route" >&2
  exit 1
fi
if [[ ! $MAX_WORKERS =~ ^[1-9][0-9]*$ || $MAX_WORKERS -gt 10 ]]; then
  echo "evaluator max workers must be an integer from 1 to 10" >&2
  exit 1
fi
if [[ ! $RPM =~ ^[1-9][0-9]*$ || $RPM -gt 60 ]]; then
  echo "evaluator rpm must be an integer from 1 to 60" >&2
  exit 1
fi
if [[ $PUBLIC_BENCHMARK != 0 && $PUBLIC_BENCHMARK != 1 ]]; then
  echo "public benchmark mode must be 0 or 1" >&2
  exit 1
fi
case "$ANSWERER_MODEL:$MODEL_ROUTE" in
  "z-ai/glm-5.2:") ;;
  "openai/gpt-5.4-mini:openai/gpt-5.4-mini=Azure") ;;
  "openai/gpt-5.4-mini:openai/gpt-5.4-mini=OpenAI") ;;
  "openai/gpt-5.6-luna-pro:openai/gpt-5.6-luna-pro=Azure") ;;
  "openai/gpt-5.6-luna-pro:openai/gpt-5.6-luna-pro=OpenAI") ;;
  *)
    echo "unsupported evaluator answerer/model-route tuple" >&2
    exit 1
    ;;
esac
if [[ $REASONING_EFFORT != high && $REASONING_EFFORT != none ]]; then
  echo "evaluator reasoning effort must be high or none" >&2
  exit 1
fi
if [[ -n $MODEL_REASONING_EFFORT && \
      $MODEL_REASONING_EFFORT != "$ANSWERER_MODEL=none" && \
      $MODEL_REASONING_EFFORT != "$ANSWERER_MODEL=low" && \
      $MODEL_REASONING_EFFORT != "$ANSWERER_MODEL=medium" && \
      $MODEL_REASONING_EFFORT != "$ANSWERER_MODEL=high" && \
      $MODEL_REASONING_EFFORT != "$ANSWERER_MODEL=xhigh" ]]; then
  echo "model reasoning effort must target the exact answerer with a supported effort" >&2
  exit 1
fi
if [[ $MAX_COST_USD != 2.5 && $MAX_COST_USD != 5.0 ]]; then
  echo "evaluator max cost must be one of the preapproved per-arm fuses: 2.5 or 5.0 USD" >&2
  exit 1
fi
if [[ -e $USAGE || -e $EVALUATOR_LOG || -e $HEALTH ]]; then
  echo "evaluator artifacts must be fresh" >&2
  exit 1
fi
if lsof -nP -iTCP:8890 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port 8890 is already in use" >&2
  exit 1
fi

cleanup() {
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
trap cleanup EXIT INT TERM

PROXY_ARGS=(--host 127.0.0.1 --port 8890)
if [[ -n $PROVIDER_ONLY ]]; then
  PROXY_ARGS+=(--provider-only "$PROVIDER_ONLY")
else
  PROXY_ARGS+=(
    --provider-allow
    DeepInfra,StreamLake,GMICloud,Baidu,AtlasCloud
  )
fi
if [[ $PUBLIC_BENCHMARK == 1 ]]; then
  PROXY_ARGS+=(--public-benchmark)
fi
if [[ -n $MODEL_ROUTE ]]; then
  PROXY_ARGS+=(--model-route "$MODEL_ROUTE")
fi
case $ANSWERER_MODEL in
  openai/gpt-5.4-mini | openai/gpt-5.6-luna-pro)
    PROXY_ARGS+=(--model-omit-temperature "$ANSWERER_MODEL")
    if [[ $MODEL_ROUTE == "$ANSWERER_MODEL=Azure" ]]; then
      PROXY_ARGS+=(
        --model-output-token-parameter
        "$ANSWERER_MODEL=max_completion_tokens"
      )
    fi
    ;;
esac
if [[ $REASONING_EFFORT != none ]]; then
  PROXY_ARGS+=(--reasoning-effort "$REASONING_EFFORT")
fi
if [[ -n $MODEL_REASONING_EFFORT ]]; then
  PROXY_ARGS+=(--model-reasoning-effort "$MODEL_REASONING_EFFORT")
fi

.venv/bin/python -m narratordb.benchmarks.openrouter_proxy \
  "${PROXY_ARGS[@]}" \
  --usage-log "$USAGE" \
  --max-cost-usd "$MAX_COST_USD" \
  --timeout 180 \
  >"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
# Only the local proxy needs the provider credential.  Remove it before any
# health-check or official-harness child is created.
unset OPENROUTER_API_KEY

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
jq '{ok,provider_allow,model_routes,model_output_token_parameters,model_omit_temperature,model_reasoning_efforts,reasoning_effort,usage}' "$HEALTH"

env \
  PYTHONPATH="$ROOT/vendor/memory-benchmarks" \
  OPENAI_API_KEY=local-transport \
  OPENAI_BASE_URL=http://127.0.0.1:8890/v1 \
  vendor/memory-benchmarks/.venv/bin/python \
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
  --answerer-model "$ANSWERER_MODEL" \
  --judge-model deepseek/deepseek-v4-flash-20260423 \
  --top-k 200 \
  --top-k-cutoffs 20,50 \
  --max-workers "$MAX_WORKERS" \
  --rpm "$RPM" \
  --seed 42 \
  --output-dir "$RUN/evaluation/official-harness" \
  2>&1 | tee "$EVALUATOR_LOG"
