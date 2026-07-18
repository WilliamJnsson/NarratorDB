#!/bin/bash -p
set -euo pipefail
[[ $# -eq 1 ]] || exit 2
ACTION=$1
case $ACTION in
  telemetry-pre|canary|telemetry-postcanary|primary|telemetry-between|replication|telemetry-post) ;;
  *) exit 2 ;;
esac
SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
[[ $(pwd -P) == "$ROOT" ]] || exit 1
PRECOMMIT="$ROOT/benchmark_records/precommits/longmemeval_dev42_v17_gpt54mini_openai_high_selfjudge_paid_pair_r1_precommit_20260717.json"
PUBLISHED=${NARRATORDB_V17_GPT_SELFJUDGE_PRECOMMIT_SHA256:-}
[[ $PUBLISHED =~ ^[0-9a-f]{64}$ ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$PRECOMMIT" | /usr/bin/awk '{print $1}') == "$PUBLISHED" ]] || exit 1
(cd "$SCRIPT_DIR" && /usr/bin/shasum -a 256 -c SEALED_FILES_SHA256SUMS)
(cd "$ROOT" && /usr/bin/shasum -a 256 -c "$SCRIPT_DIR/BOUND_INPUTS_SHA256SUMS")
PYTHON="$ROOT/vendor/memory-benchmarks/.venv/bin/python"
[[ -L $PYTHON && $(/usr/bin/readlink "$PYTHON") == "/Users/william/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12" ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$PYTHON" | /usr/bin/awk '{print $1}') == 7b05d803bbc1bbfc81644af4faf2b88f0a37b8de96b9f42c1e08033e2cd0848a ]] || exit 1

REPORT="$ROOT/reports/longmemeval-intelligence-dev42-v17-gpt54mini-openai-high-selfjudge-paid-pair-r1-20260717"
PROJECT=narratordb-intelligence-dev42-v17-replay-v7gpt54mini-repeat2-attempt2
DATASET="$ROOT/reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/longmemeval_s_dev42.json"
HARNESS_TAR="$ROOT/reports/longmemeval-intelligence-dev42-v17-replay-v7gpt54mini-repeat2-attempt2-20260717/harness-source.tar"
PROXY_SCRIPT="$ROOT/benchmark_records/reproduction-v17-gpt54mini-openai-high-deepseek-paid-pair-r1-20260717/run_openrouter_proxy_guarded.py"
case $ACTION in
  telemetry-pre) OUTPUT="$REPORT/preflight/provider-telemetry-pre.json" ;;
  telemetry-postcanary) OUTPUT="$REPORT/preflight/provider-telemetry-postcanary.json" ;;
  telemetry-between) OUTPUT="$REPORT/between/provider-telemetry-between.json" ;;
  telemetry-post) OUTPUT="$REPORT/postrun/provider-telemetry-post.json" ;;
  canary) RUN="$REPORT/canary" ;;
  primary) RUN="$REPORT/primary" ;;
  replication) RUN="$REPORT/replication" ;;
esac
if [[ $ACTION == telemetry-* ]]; then
  [[ ! -e $OUTPUT && ! -L $OUTPUT ]] || exit 1
  /bin/mkdir -p "$(/usr/bin/dirname "$OUTPUT")"
else
  USAGE="$RUN/evaluation/openrouter-usage.jsonl"
  PROXY_LOG="$RUN/evaluation/proxy.log"
  HEALTH="$RUN/evaluation/proxy-health-before.json"
  if [[ $ACTION == canary ]]; then
    RESULT="$RUN/evaluation/canary-result.json"
    /bin/mkdir -p "$RUN/evaluation"
    for P in "$USAGE" "$PROXY_LOG" "$HEALTH" "$RESULT"; do
      [[ ! -e $P && ! -L $P ]] || exit 1
    done
  else
    EVALUATOR_LOG="$RUN/evaluation/evaluate.log"
    OFFICIAL="$RUN/evaluation/official-harness"
    HARNESS_RUNTIME="$RUN/evaluation/harness-runtime"
    [[ -d "$OFFICIAL/predicted_$PROJECT" ]] || exit 1
    for P in "$USAGE" "$PROXY_LOG" "$HEALTH" "$EVALUATOR_LOG" "$HARNESS_RUNTIME"; do
      [[ ! -e $P && ! -L $P ]] || exit 1
    done
    /bin/mkdir -p "$HARNESS_RUNTIME"
    /usr/bin/tar -xf "$HARNESS_TAR" -C "$HARNESS_RUNTIME"
  fi
  /usr/sbin/lsof -nP -iTCP:8890 -sTCP:LISTEN 2>/dev/null | /usr/bin/grep -q . && exit 1 || :
fi

ENV_FILE=/Users/william/.narratordb/openrouter.env
[[ -f $ENV_FILE && ! -L $ENV_FILE && $(/usr/bin/stat -f '%Lp:%u' "$ENV_FILE") == "600:501" ]] || exit 1
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
export PATH HOME TMPDIR LANG LC_ALL NO_PROXY no_proxy PYTHONDONTWRITEBYTECODE PYTHONNOUSERSITE
if [[ $ACTION == telemetry-* ]]; then
  OPENROUTER_API_KEY=$SECRET
  export OPENROUTER_API_KEY
  unset SECRET LINE VALUE NAME
  exec "$PYTHON" -I -S -B "$ROOT/benchmark_records/reproduction-v13-paid-paired-scoring-r5-20260716/capture_provider_telemetry.py" --output "$OUTPUT" --timeout 20
fi
cleanup() {
  if [[ -n ${PID:-} ]] && /bin/kill -0 "$PID" 2>/dev/null; then
    /bin/kill -INT "$PID" 2>/dev/null || :
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      /bin/kill -0 "$PID" 2>/dev/null || break
      /bin/sleep .1
    done
    /bin/kill -0 "$PID" 2>/dev/null && /bin/kill -TERM "$PID" 2>/dev/null || :
    wait "$PID" 2>/dev/null || :
  fi
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
  --host 127.0.0.1 --port 8890 --provider-only OpenAI \
  --model-route openai/gpt-5.4-mini=OpenAI \
  --model-omit-temperature openai/gpt-5.4-mini \
  --model-reasoning-effort openai/gpt-5.4-mini=high --public-benchmark \
  --usage-log "$USAGE" --max-cost-usd "$MAX" --request-reservation-usd "$RESERVE" \
  --budget-safety-reserve-usd "$SAFETY" --timeout 180 >"$PROXY_LOG" 2>&1 &
PID=$!
unset SECRET LINE VALUE NAME
READY=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45 46 47 48 49 50; do
  if /usr/bin/curl -fsS http://127.0.0.1:8890/health >"$HEALTH" 2>/dev/null; then
    READY=1
    break
  fi
  /bin/sleep .1
done
[[ $READY -eq 1 ]] || exit 1
if [[ $ACTION == canary ]]; then
  "$PYTHON" -I -S -B "$SCRIPT_DIR/route_canary.py" --base-url http://127.0.0.1:8890/v1 --output "$RESULT"
  exit 0
fi
PYTHONPATH="$HARNESS_RUNTIME" OPENAI_API_KEY=local-transport OPENAI_BASE_URL=http://127.0.0.1:8890/v1 \
  "$PYTHON" -m benchmarks.longmemeval.run --project-name "$PROJECT" --dataset-path "$DATASET" \
  --all-questions --evaluate-only --rejudge --run-id v7m42a1 --mode answerer --provider openai \
  --judge-provider openai --answerer-model openai/gpt-5.4-mini \
  --judge-model openai/gpt-5.4-mini --top-k 200 --top-k-cutoffs 20,50 \
  --max-workers 2 --rpm 30 --seed 42 --output-dir "$OFFICIAL" 2>&1 | /usr/bin/tee "$EVALUATOR_LOG"
