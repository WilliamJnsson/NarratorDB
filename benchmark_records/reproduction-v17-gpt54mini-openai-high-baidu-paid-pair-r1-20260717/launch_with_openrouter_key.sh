#!/bin/bash -p

set -euo pipefail
if [[ $# -ne 1 ]]; then
  builtin printf '%s\n' "usage: $0 EXACT_ACTION" >&2
  exit 2
fi

V17_ACTION=$1
case $V17_ACTION in
  telemetry-pre | telemetry-postcanary | telemetry-between | telemetry-post | canary | primary | replication) ;;
  *)
    builtin printf '%s\n' "action is not an exact precommitted tuple" >&2
    exit 2
    ;;
esac

V17_SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
V17_ROOT=$(CDPATH= cd -- "$V17_SCRIPT_DIR/../.." && pwd -P)
if [[ $(pwd -P) != "$V17_ROOT" ]]; then
  builtin printf '%s\n' "launcher must run from the exact repository root" >&2
  exit 1
fi
V17_PRECOMMIT="$V17_ROOT/benchmark_records/precommits/longmemeval_dev42_v17_gpt54mini_openai_high_baidu_paid_pair_r1_precommit_20260717.json"
V17_PUBLISHED_PRECOMMIT=${NARRATORDB_V17_PAID_PRECOMMIT_SHA256:-}
if [[ ! $V17_PUBLISHED_PRECOMMIT =~ ^[0-9a-f]{64}$ ]]; then
  builtin printf '%s\n' "externally published V17 precommit SHA-256 is missing" >&2
  exit 1
fi
V17_COMPUTED_PRECOMMIT=$(/usr/bin/shasum -a 256 "$V17_PRECOMMIT" | /usr/bin/awk '{print $1}')
if [[ $V17_COMPUTED_PRECOMMIT != "$V17_PUBLISHED_PRECOMMIT" ]]; then
  builtin printf '%s\n' "published V17 precommit SHA-256 does not match" >&2
  exit 1
fi
(
  cd "$V17_SCRIPT_DIR"
  /usr/bin/shasum -a 256 -c SEALED_FILES_SHA256SUMS
)
(
  cd "$V17_ROOT"
  /usr/bin/shasum -a 256 -c "$V17_SCRIPT_DIR/BOUND_INPUTS_SHA256SUMS"
)

V17_PYTHON="$V17_ROOT/vendor/memory-benchmarks/.venv/bin/python"
V17_EXPECTED_PYTHON_TARGET="/Users/william/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12"
if [[ ! -L $V17_PYTHON || $(/usr/bin/readlink "$V17_PYTHON") != "$V17_EXPECTED_PYTHON_TARGET" ]]; then
  builtin printf '%s\n' "sealed Python identity changed" >&2
  exit 1
fi
if [[ $(/usr/bin/shasum -a 256 "$V17_PYTHON" | /usr/bin/awk '{print $1}') != 7b05d803bbc1bbfc81644af4faf2b88f0a37b8de96b9f42c1e08033e2cd0848a ]]; then
  builtin printf '%s\n' "sealed Python bytes changed" >&2
  exit 1
fi

V17_REPORT="$V17_ROOT/reports/longmemeval-intelligence-dev42-v17-gpt54mini-openai-high-baidu-paid-pair-r1-20260717"
V17_PROJECT="narratordb-intelligence-dev42-v17-replay-v7gpt54mini-repeat2-attempt2"
V17_DATASET="$V17_ROOT/reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/longmemeval_s_dev42.json"
V17_HARNESS_TAR="$V17_ROOT/reports/longmemeval-intelligence-dev42-v17-replay-v7gpt54mini-repeat2-attempt2-20260717/harness-source.tar"

case $V17_ACTION in
  telemetry-pre) V17_OUTPUT="$V17_REPORT/preflight/provider-telemetry-pre.json" ;;
  telemetry-postcanary) V17_OUTPUT="$V17_REPORT/preflight/provider-telemetry-postcanary.json" ;;
  telemetry-between) V17_OUTPUT="$V17_REPORT/between/provider-telemetry-between.json" ;;
  telemetry-post) V17_OUTPUT="$V17_REPORT/postrun/provider-telemetry-post.json" ;;
  canary) V17_RUN="$V17_REPORT/canary" ;;
  primary) V17_RUN="$V17_REPORT/primary" ;;
  replication) V17_RUN="$V17_REPORT/replication" ;;
esac

if [[ $V17_ACTION == telemetry-* ]]; then
  if [[ -e $V17_OUTPUT || -L $V17_OUTPUT ]]; then
    builtin printf '%s\n' "telemetry output must start absent" >&2
    exit 1
  fi
  /bin/mkdir -p "$(/usr/bin/dirname "$V17_OUTPUT")"
else
  V17_USAGE="$V17_RUN/evaluation/openrouter-usage.jsonl"
  V17_PROXY_LOG="$V17_RUN/evaluation/proxy.log"
  V17_HEALTH="$V17_RUN/evaluation/proxy-health-before.json"
  if [[ $V17_ACTION == canary ]]; then
    V17_CANARY_RESULT="$V17_RUN/evaluation/canary-result.json"
    /bin/mkdir -p "$V17_RUN/evaluation"
    for V17_PATH in "$V17_USAGE" "$V17_PROXY_LOG" "$V17_HEALTH" "$V17_CANARY_RESULT"; do
      [[ ! -e $V17_PATH && ! -L $V17_PATH ]] || { builtin printf '%s\n' "canary artifacts must start absent" >&2; exit 1; }
    done
  else
    V17_EVALUATOR_LOG="$V17_RUN/evaluation/evaluate.log"
    V17_OFFICIAL="$V17_RUN/evaluation/official-harness"
    V17_HARNESS_RUNTIME="$V17_RUN/evaluation/harness-runtime"
    [[ -d "$V17_OFFICIAL/predicted_$V17_PROJECT" ]] || { builtin printf '%s\n' "staged prediction copy is missing" >&2; exit 1; }
    for V17_PATH in "$V17_USAGE" "$V17_PROXY_LOG" "$V17_HEALTH" "$V17_EVALUATOR_LOG" "$V17_HARNESS_RUNTIME"; do
      [[ ! -e $V17_PATH && ! -L $V17_PATH ]] || { builtin printf '%s\n' "arm artifacts must start absent" >&2; exit 1; }
    done
    /bin/mkdir -p "$V17_HARNESS_RUNTIME"
    /usr/bin/tar -xf "$V17_HARNESS_TAR" -C "$V17_HARNESS_RUNTIME"
  fi
  if /usr/sbin/lsof -nP -iTCP:8890 -sTCP:LISTEN 2>/dev/null | /usr/bin/grep -q .; then
    builtin printf '%s\n' "proxy port is already in use" >&2
    exit 1
  fi
fi

V17_ENV_FILE="/Users/william/.narratordb/openrouter.env"
if [[ ! -f $V17_ENV_FILE || -L $V17_ENV_FILE || $(/usr/bin/stat -f '%Lp:%u' "$V17_ENV_FILE") != "600:501" ]]; then
  builtin printf '%s\n' "OpenRouter environment file identity or mode is unsafe" >&2
  exit 1
fi
V17_SECRET=""
V17_LINES=0
while IFS= read -r V17_LINE || [[ -n $V17_LINE ]]; do
  [[ -z $V17_LINE ]] && continue
  V17_LINES=$((V17_LINES + 1))
  case $V17_LINE in
    OPENROUTER_API_KEY=*) V17_VALUE=${V17_LINE#OPENROUTER_API_KEY=} ;;
    export\ OPENROUTER_API_KEY=*) V17_VALUE=${V17_LINE#export OPENROUTER_API_KEY=} ;;
    *) builtin printf '%s\n' "OpenRouter environment file has an unexpected entry" >&2; exit 1 ;;
  esac
  if [[ $V17_VALUE == \"*\" && $V17_VALUE == *\" ]]; then V17_VALUE=${V17_VALUE:1:${#V17_VALUE}-2}; fi
  if [[ $V17_VALUE == \'*\' && $V17_VALUE == *\' ]]; then V17_VALUE=${V17_VALUE:1:${#V17_VALUE}-2}; fi
  V17_SECRET=$V17_VALUE
done < "$V17_ENV_FILE"
if [[ $V17_LINES -ne 1 || ! $V17_SECRET =~ ^sk-or-[A-Za-z0-9_-]{30,}$ ]]; then
  builtin printf '%s\n' "OpenRouter environment file is malformed" >&2
  exit 1
fi
export -n V17_SECRET

V17_SAFE_ACTION=$V17_ACTION
V17_SAFE_OUTPUT=${V17_OUTPUT:-}
V17_SAFE_RUN=${V17_RUN:-}
V17_SAFE_PRECOMMIT=$V17_PUBLISHED_PRECOMMIT
export -n V17_SAFE_ACTION V17_SAFE_OUTPUT V17_SAFE_RUN V17_SAFE_PRECOMMIT
while IFS= read -r V17_EXPORTED_NAME; do
  unset "$V17_EXPORTED_NAME" 2>/dev/null || :
done < <(builtin compgen -e)
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

if [[ $V17_SAFE_ACTION == telemetry-* ]]; then
  OPENROUTER_API_KEY=$V17_SECRET
  export OPENROUTER_API_KEY
  unset V17_SECRET V17_LINE V17_VALUE V17_EXPORTED_NAME
  exec "$V17_PYTHON" -I -S -B \
    "$V17_ROOT/benchmark_records/reproduction-v13-paid-paired-scoring-r5-20260716/capture_provider_telemetry.py" \
    --output "$V17_SAFE_OUTPUT" --timeout 20
fi

cleanup_proxy() {
  if [[ -n ${V17_PROXY_PID:-} ]] && /bin/kill -0 "$V17_PROXY_PID" 2>/dev/null; then
    /bin/kill -INT "$V17_PROXY_PID" 2>/dev/null || :
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      /bin/kill -0 "$V17_PROXY_PID" 2>/dev/null || break
      /bin/sleep 0.1
    done
    /bin/kill -0 "$V17_PROXY_PID" 2>/dev/null && /bin/kill -TERM "$V17_PROXY_PID" 2>/dev/null || :
    wait "$V17_PROXY_PID" 2>/dev/null || :
  fi
}
trap cleanup_proxy EXIT INT TERM

if [[ $V17_SAFE_ACTION == canary ]]; then
  V17_MAX_COST=0.10
  V17_RESERVATION=0.01
  V17_SAFETY=0.005
else
  V17_MAX_COST=2.45
  V17_RESERVATION=0.05
  V17_SAFETY=0.01
fi

OPENROUTER_API_KEY=$V17_SECRET "$V17_PYTHON" -I -S -B \
  "$V17_SCRIPT_DIR/run_openrouter_proxy_guarded_v17.py" \
  --host 127.0.0.1 --port 8890 \
  --provider-only Baidu \
  --model-route openai/gpt-5.4-mini=OpenAI \
  --model-omit-temperature openai/gpt-5.4-mini \
  --model-reasoning-effort openai/gpt-5.4-mini=high \
  --public-benchmark \
  --usage-log "$V17_USAGE" \
  --max-cost-usd "$V17_MAX_COST" \
  --request-reservation-usd "$V17_RESERVATION" \
  --budget-safety-reserve-usd "$V17_SAFETY" \
  --timeout 180 >"$V17_PROXY_LOG" 2>&1 &
V17_PROXY_PID=$!
unset V17_SECRET V17_LINE V17_VALUE V17_EXPORTED_NAME

V17_READY=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45 46 47 48 49 50; do
  if /usr/bin/curl -fsS http://127.0.0.1:8890/health >"$V17_HEALTH" 2>/dev/null; then V17_READY=1; break; fi
  /bin/sleep 0.1
done
[[ $V17_READY -eq 1 ]] || { builtin printf '%s\n' "proxy failed to start" >&2; exit 1; }

if [[ $V17_SAFE_ACTION == canary ]]; then
  "$V17_PYTHON" -I -S -B "$V17_SCRIPT_DIR/route_canary.py" \
    --base-url http://127.0.0.1:8890/v1 \
    --output "$V17_CANARY_RESULT" --timeout 180
  exit 0
fi

PYTHONPATH="$V17_HARNESS_RUNTIME" \
OPENAI_API_KEY=local-transport \
OPENAI_BASE_URL=http://127.0.0.1:8890/v1 \
"$V17_PYTHON" -m benchmarks.longmemeval.run \
  --project-name "$V17_PROJECT" \
  --dataset-path "$V17_DATASET" \
  --all-questions \
  --evaluate-only \
  --rejudge \
  --run-id v7m42a1 \
  --mode answerer \
  --provider openai \
  --judge-provider openai \
  --answerer-model openai/gpt-5.4-mini \
  --judge-model deepseek/deepseek-v4-flash-20260423 \
  --top-k 200 \
  --top-k-cutoffs 20,50 \
  --max-workers 2 \
  --rpm 30 \
  --seed 42 \
  --output-dir "$V17_OFFICIAL" 2>&1 | /usr/bin/tee "$V17_EVALUATOR_LOG"
