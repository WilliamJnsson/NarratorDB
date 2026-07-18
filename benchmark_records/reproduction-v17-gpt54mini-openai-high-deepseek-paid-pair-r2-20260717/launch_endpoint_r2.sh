#!/bin/bash -p
set -euo pipefail
[[ $# -eq 1 && $1 == endpoint-metadata-r2 ]] || exit 2
SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
[[ $(pwd -P) == "$ROOT" ]] || exit 1
PRECOMMIT="$ROOT/benchmark_records/precommits/longmemeval_dev42_v17_gpt54mini_openai_high_deepseek_paid_pair_r2_precommit_20260717.json"
PUBLISHED=${NARRATORDB_V17_DEEPSEEK_R2_PRECOMMIT_SHA256:-}
[[ $PUBLISHED =~ ^[0-9a-f]{64}$ ]] || exit 1
[[ $(/usr/bin/shasum -a 256 "$PRECOMMIT" | /usr/bin/awk '{print $1}') == "$PUBLISHED" ]] || exit 1
(cd "$SCRIPT_DIR" && /usr/bin/shasum -a 256 -c SEALED_FILES_SHA256SUMS)
(cd "$ROOT" && /usr/bin/shasum -a 256 -c "$SCRIPT_DIR/BOUND_INPUTS_SHA256SUMS")
PYTHON="$ROOT/vendor/memory-benchmarks/.venv/bin/python"
[[ -L $PYTHON && $(/usr/bin/readlink "$PYTHON") == "/Users/william/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12" ]] || exit 1
REPORT="$ROOT/reports/longmemeval-intelligence-dev42-v17-gpt54mini-openai-high-deepseek-paid-pair-r1-20260717/preflight"
RAW="$REPORT/deepseek-endpoints-r2-raw.json"; OUTPUT="$REPORT/deepseek-endpoints-r2.json"
[[ ! -e $RAW && ! -L $RAW && ! -e $OUTPUT && ! -L $OUTPUT ]] || exit 1
ENV_FILE=/Users/william/.narratordb/openrouter.env
[[ -f $ENV_FILE && ! -L $ENV_FILE && $(/usr/bin/stat -f '%Lp:%u' "$ENV_FILE") == "600:501" ]] || exit 1
SECRET=""; LINES=0
while IFS= read -r LINE || [[ -n $LINE ]]; do
 [[ -z $LINE ]] && continue; LINES=$((LINES+1))
 case $LINE in OPENROUTER_API_KEY=*) VALUE=${LINE#OPENROUTER_API_KEY=};; export\ OPENROUTER_API_KEY=*) VALUE=${LINE#export OPENROUTER_API_KEY=};; *) exit 1;; esac
 [[ $VALUE == \"*\" && $VALUE == *\" ]] && VALUE=${VALUE:1:${#VALUE}-2}; [[ $VALUE == \'*\' && $VALUE == *\' ]] && VALUE=${VALUE:1:${#VALUE}-2}; SECRET=$VALUE
done < "$ENV_FILE"
[[ $LINES -eq 1 && $SECRET =~ ^sk-or-[A-Za-z0-9_-]{30,}$ ]] || exit 1
export -n SECRET
while IFS= read -r NAME; do unset "$NAME" 2>/dev/null || :; done < <(builtin compgen -e)
unset BASH_ENV ENV CDPATH GLOBIGNORE
PATH=/usr/bin:/bin:/usr/sbin:/sbin; HOME=/tmp; TMPDIR=/tmp; LANG=C; LC_ALL=C; NO_PROXY='*'; no_proxy='*'; PYTHONDONTWRITEBYTECODE=1; PYTHONNOUSERSITE=1
export PATH HOME TMPDIR LANG LC_ALL NO_PROXY no_proxy PYTHONDONTWRITEBYTECODE PYTHONNOUSERSITE
OPENROUTER_API_KEY=$SECRET; export OPENROUTER_API_KEY; unset SECRET LINE VALUE NAME
exec "$PYTHON" -I -S -B "$SCRIPT_DIR/capture_endpoint_metadata_r2.py" --raw-output "$RAW" --metadata-output "$OUTPUT"
