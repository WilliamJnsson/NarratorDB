#!/bin/bash -p

set -euo pipefail
[[ $# -eq 1 && $1 == canary-r2 ]] || { builtin printf '%s\n' "usage: $0 canary-r2" >&2; exit 2; }
V17_SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
V17_ROOT=$(CDPATH= cd -- "$V17_SCRIPT_DIR/../.." && pwd -P)
[[ $(pwd -P) == "$V17_ROOT" ]] || { builtin printf '%s\n' "launcher must run from repository root" >&2; exit 1; }
V17_PRECOMMIT="$V17_ROOT/benchmark_records/precommits/longmemeval_dev42_v17_gpt54mini_openai_high_baidu_paid_pair_r2_precommit_20260717.json"
V17_PUBLISHED=${NARRATORDB_V17_PAID_R2_PRECOMMIT_SHA256:-}
[[ $V17_PUBLISHED =~ ^[0-9a-f]{64}$ ]] || { builtin printf '%s\n' "published R2 precommit SHA is missing" >&2; exit 1; }
[[ $(/usr/bin/shasum -a 256 "$V17_PRECOMMIT" | /usr/bin/awk '{print $1}') == "$V17_PUBLISHED" ]] || { builtin printf '%s\n' "R2 precommit mismatch" >&2; exit 1; }
(cd "$V17_SCRIPT_DIR" && /usr/bin/shasum -a 256 -c SEALED_FILES_SHA256SUMS)
(cd "$V17_ROOT" && /usr/bin/shasum -a 256 -c "$V17_SCRIPT_DIR/BOUND_INPUTS_SHA256SUMS")

V17_PYTHON="$V17_ROOT/vendor/memory-benchmarks/.venv/bin/python"
[[ -L $V17_PYTHON && $(/usr/bin/readlink "$V17_PYTHON") == "/Users/william/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12" ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$V17_PYTHON" | /usr/bin/awk '{print $1}') == 7b05d803bbc1bbfc81644af4faf2b88f0a37b8de96b9f42c1e08033e2cd0848a ]] || exit 1

V17_RUN="$V17_ROOT/reports/longmemeval-intelligence-dev42-v17-gpt54mini-openai-high-baidu-paid-pair-r1-20260717/canary-r2/evaluation"
/bin/mkdir -p "$V17_RUN"
V17_USAGE="$V17_SCRIPT_DIR/cumulative-canary-usage.jsonl"
V17_PROXY_LOG="$V17_RUN/proxy.log"
V17_HEALTH="$V17_RUN/proxy-health-before.json"
V17_RESULT="$V17_RUN/canary-result.json"
for V17_PATH in "$V17_PROXY_LOG" "$V17_HEALTH" "$V17_RESULT"; do
  [[ ! -e $V17_PATH && ! -L $V17_PATH ]] || { builtin printf '%s\n' "R2 canary artifacts must start absent" >&2; exit 1; }
done
if /usr/sbin/lsof -nP -iTCP:8890 -sTCP:LISTEN 2>/dev/null | /usr/bin/grep -q .; then exit 1; fi

V17_ENV_FILE="/Users/william/.narratordb/openrouter.env"
[[ -f $V17_ENV_FILE && ! -L $V17_ENV_FILE && $(/usr/bin/stat -f '%Lp:%u' "$V17_ENV_FILE") == "600:501" ]] || exit 1
V17_SECRET=""
V17_LINES=0
while IFS= read -r V17_LINE || [[ -n $V17_LINE ]]; do
  [[ -z $V17_LINE ]] && continue
  V17_LINES=$((V17_LINES + 1))
  case $V17_LINE in
    OPENROUTER_API_KEY=*) V17_VALUE=${V17_LINE#OPENROUTER_API_KEY=} ;;
    export\ OPENROUTER_API_KEY=*) V17_VALUE=${V17_LINE#export OPENROUTER_API_KEY=} ;;
    *) exit 1 ;;
  esac
  [[ $V17_VALUE == \"*\" && $V17_VALUE == *\" ]] && V17_VALUE=${V17_VALUE:1:${#V17_VALUE}-2}
  [[ $V17_VALUE == \'*\' && $V17_VALUE == *\' ]] && V17_VALUE=${V17_VALUE:1:${#V17_VALUE}-2}
  V17_SECRET=$V17_VALUE
done < "$V17_ENV_FILE"
[[ $V17_LINES -eq 1 && $V17_SECRET =~ ^sk-or-[A-Za-z0-9_-]{30,}$ ]] || exit 1
export -n V17_SECRET

while IFS= read -r V17_EXPORTED_NAME; do unset "$V17_EXPORTED_NAME" 2>/dev/null || :; done < <(builtin compgen -e)
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

cleanup_proxy() {
  if [[ -n ${V17_PROXY_PID:-} ]] && /bin/kill -0 "$V17_PROXY_PID" 2>/dev/null; then
    /bin/kill -INT "$V17_PROXY_PID" 2>/dev/null || :
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do /bin/kill -0 "$V17_PROXY_PID" 2>/dev/null || break; /bin/sleep 0.1; done
    /bin/kill -0 "$V17_PROXY_PID" 2>/dev/null && /bin/kill -TERM "$V17_PROXY_PID" 2>/dev/null || :
    wait "$V17_PROXY_PID" 2>/dev/null || :
  fi
}
trap cleanup_proxy EXIT INT TERM

OPENROUTER_API_KEY=$V17_SECRET "$V17_PYTHON" -I -S -B \
  "$V17_ROOT/benchmark_records/reproduction-v17-gpt54mini-openai-high-baidu-paid-pair-r1-20260717/run_openrouter_proxy_guarded_v17.py" \
  --host 127.0.0.1 --port 8890 \
  --provider-only Baidu \
  --model-route openai/gpt-5.4-mini=OpenAI \
  --model-omit-temperature openai/gpt-5.4-mini \
  --model-reasoning-effort openai/gpt-5.4-mini=high \
  --public-benchmark \
  --usage-log "$V17_USAGE" \
  --max-cost-usd 0.10 \
  --request-reservation-usd 0.01 \
  --budget-safety-reserve-usd 0.005 \
  --timeout 180 >"$V17_PROXY_LOG" 2>&1 &
V17_PROXY_PID=$!
unset V17_SECRET V17_LINE V17_VALUE V17_EXPORTED_NAME
V17_READY=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45 46 47 48 49 50; do
  if /usr/bin/curl -fsS http://127.0.0.1:8890/health >"$V17_HEALTH" 2>/dev/null; then V17_READY=1; break; fi
  /bin/sleep 0.1
done
[[ $V17_READY -eq 1 ]] || exit 1
"$V17_PYTHON" -I -S -B "$V17_SCRIPT_DIR/route_canary_r2.py" \
  --base-url http://127.0.0.1:8890/v1 --output "$V17_RESULT"
