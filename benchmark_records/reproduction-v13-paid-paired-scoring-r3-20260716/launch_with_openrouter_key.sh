#!/bin/bash -p

# This launcher is the only top-level bridge from an operator's exported
# OpenRouter credential to an intended credential-bearing process.  Everything
# through the first unset is a Bash builtin: no child, command substitution,
# startup hook, or external env process can receive the key first.
if [[ -z ${OPENROUTER_API_KEY+x} || -z $OPENROUTER_API_KEY ]]; then
  builtin printf '%s\n' "runtime OpenRouter environment is missing" >&2
  exit 1
fi
set +a
unset R3_RUNTIME_OPENROUTER_KEY
R3_RUNTIME_OPENROUTER_KEY=$OPENROUTER_API_KEY
export -n R3_RUNTIME_OPENROUTER_KEY
unset OPENROUTER_API_KEY

set -euo pipefail
if [[ $# -ne 1 ]]; then
  builtin printf '%s\n' "usage: $0 EXACT_ACTION" >&2
  exit 2
fi

# The launcher is not a generic secret-forwarding utility.  The caller selects
# one of five action names; the launcher itself constructs every executable and
# argument from the verified repository root after the key has been removed
# from the exported environment.
case $1 in
  telemetry-before-v7 | telemetry-before-v13 | telemetry-after-pair | \
  evaluate-v7 | evaluate-v13)
    R3_ACTION=$1
    export -n R3_ACTION
    ;;
  *)
    builtin printf '%s\n' "credential target is not one exact precommitted tuple" >&2
    exit 1
    ;;
esac

# Preserve only the non-secret values explicitly allowed by the sealed
# protocol.  These holding variables are kept non-exported even if the caller
# enabled allexport or supplied hostile variables with the same names.
unset R3_SAFE_PRECOMMIT
R3_SAFE_PRECOMMIT=${NARRATORDB_PAID_PRECOMMIT_SHA256:-}
export -n R3_SAFE_PRECOMMIT

# Remove the inherited exported environment with Bash builtins.  The process
# substitution runs only after OPENROUTER_API_KEY has been unset and the sole
# holding variable has been made non-exported.
while IFS= read -r R3_EXPORTED_NAME; do
  unset "$R3_EXPORTED_NAME" 2>/dev/null || :
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
export PATH HOME TMPDIR LANG LC_ALL NO_PROXY no_proxy
export PYTHONDONTWRITEBYTECODE PYTHONNOUSERSITE
if [[ -n $R3_SAFE_PRECOMMIT ]]; then
  NARRATORDB_PAID_PRECOMMIT_SHA256=$R3_SAFE_PRECOMMIT
  export NARRATORDB_PAID_PRECOMMIT_SHA256
fi

if [[ ! $R3_SAFE_PRECOMMIT =~ ^[0-9a-f]{64}$ ]]; then
  builtin printf '%s\n' "externally published R3 precommit SHA-256 is missing" >&2
  exit 1
fi
R3_SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
R3_ROOT=$(CDPATH= cd -- "$R3_SCRIPT_DIR/../.." && pwd -P)
if [[ $(pwd -P) != $R3_ROOT ]]; then
  builtin printf '%s\n' "credential launcher must run from the exact repository root" >&2
  exit 1
fi
R3_COMPUTED_PRECOMMIT=$(/usr/bin/shasum -a 256 "$R3_SCRIPT_DIR/SHA256SUMS" | /usr/bin/awk '{print $1}')
if [[ $R3_COMPUTED_PRECOMMIT != $R3_SAFE_PRECOMMIT ]]; then
  builtin printf '%s\n' "published R3 precommit SHA-256 does not match" >&2
  exit 1
fi
(
  cd "$R3_SCRIPT_DIR"
  shasum -a 256 -c SHA256SUMS
  if find . -mindepth 1 ! -type f -print -quit | grep -q .; then
    builtin printf '%s\n' "R3 precommit must be a flat regular-file tree" >&2
    exit 1
  fi
  if find . -type f -links +1 -print -quit | grep -q .; then
    builtin printf '%s\n' "R3 precommit contains a hard-linked file" >&2
    exit 1
  fi
  if ! diff -u \
    <(awk '{print $2}' SHA256SUMS | LC_ALL=C sort) \
    <(find . -type f ! -path './SHA256SUMS' -print | sed 's#^\./##' | LC_ALL=C sort); then
    builtin printf '%s\n' "R3 precommit closed-world inventory mismatch" >&2
    exit 1
  fi
)
(
  cd "$R3_ROOT"
  shasum -a 256 -c "$R3_SCRIPT_DIR/BOUND_INPUTS_SHA256SUMS"
)
if [[ ! -x $R3_SCRIPT_DIR/launch_with_openrouter_key.sh || \
      ! -x $R3_SCRIPT_DIR/run_paid_variant_hardened.sh ]]; then
  builtin printf '%s\n' "R3 credential launcher and wrapper must be directly executable" >&2
  exit 1
fi

case $R3_ACTION in
  telemetry-before-v7)
    set -- "$R3_ROOT/.venv/bin/python" -I -S -B \
      "$R3_SCRIPT_DIR/capture_provider_telemetry.py" --output \
      "$R3_ROOT/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r3-20260716/attempt1/precall/provider-telemetry-before-v7.json"
    ;;
  telemetry-before-v13)
    set -- "$R3_ROOT/.venv/bin/python" -I -S -B \
      "$R3_SCRIPT_DIR/capture_provider_telemetry.py" --output \
      "$R3_ROOT/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r3-20260716/attempt1/between/provider-telemetry-before-v13.json"
    ;;
  telemetry-after-pair)
    set -- "$R3_ROOT/.venv/bin/python" -I -S -B \
      "$R3_SCRIPT_DIR/capture_provider_telemetry.py" --output \
      "$R3_ROOT/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r3-20260716/attempt1/postrun/provider-telemetry-after-pair.json"
    ;;
  evaluate-v7)
    set -- "$R3_SCRIPT_DIR/run_paid_variant_hardened.sh" \
      reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r3-20260716/attempt1/v7-control \
      narratordb-intelligence-dev42-v7-gpt54mini \
      reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/longmemeval_s_dev42.json
    ;;
  evaluate-v13)
    set -- "$R3_SCRIPT_DIR/run_paid_variant_hardened.sh" \
      reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r3-20260716/attempt1/v13-first \
      narratordb-intelligence-dev42-v13-replay-v7gpt54mini \
      reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/longmemeval_s_dev42.json
    ;;
esac
cd "$R3_ROOT"

OPENROUTER_API_KEY=$R3_RUNTIME_OPENROUTER_KEY
export OPENROUTER_API_KEY
unset R3_RUNTIME_OPENROUTER_KEY R3_SAFE_PRECOMMIT R3_EXPORTED_NAME R3_ACTION
exec "$@"
