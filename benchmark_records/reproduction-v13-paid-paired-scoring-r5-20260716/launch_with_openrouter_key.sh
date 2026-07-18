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
unset R5_RUNTIME_OPENROUTER_KEY
R5_RUNTIME_OPENROUTER_KEY=$OPENROUTER_API_KEY
export -n R5_RUNTIME_OPENROUTER_KEY
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
    R5_ACTION=$1
    export -n R5_ACTION
    ;;
  *)
    builtin printf '%s\n' "credential target is not one exact precommitted tuple" >&2
    exit 1
    ;;
esac

# Preserve only the non-secret values explicitly allowed by the sealed
# protocol.  These holding variables are kept non-exported even if the caller
# enabled allexport or supplied hostile variables with the same names.
unset R5_SAFE_PRECOMMIT
R5_SAFE_PRECOMMIT=${NARRATORDB_PAID_PRECOMMIT_SHA256:-}
export -n R5_SAFE_PRECOMMIT

# Remove the inherited exported environment with Bash builtins.  The process
# substitution runs only after OPENROUTER_API_KEY has been unset and the sole
# holding variable has been made non-exported.
while IFS= read -r R5_EXPORTED_NAME; do
  unset "$R5_EXPORTED_NAME" 2>/dev/null || :
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
if [[ -n $R5_SAFE_PRECOMMIT ]]; then
  NARRATORDB_PAID_PRECOMMIT_SHA256=$R5_SAFE_PRECOMMIT
  export NARRATORDB_PAID_PRECOMMIT_SHA256
fi

if [[ ! $R5_SAFE_PRECOMMIT =~ ^[0-9a-f]{64}$ ]]; then
  builtin printf '%s\n' "externally published R5 precommit SHA-256 is missing" >&2
  exit 1
fi
R5_SCRIPT_DIR=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd -P)
R5_ROOT=$(CDPATH= cd -- "$R5_SCRIPT_DIR/../.." && pwd -P)
if [[ $(pwd -P) != $R5_ROOT ]]; then
  builtin printf '%s\n' "credential launcher must run from the exact repository root" >&2
  exit 1
fi
R5_COMPUTED_PRECOMMIT=$(/usr/bin/shasum -a 256 "$R5_SCRIPT_DIR/SHA256SUMS" | /usr/bin/awk '{print $1}')
if [[ $R5_COMPUTED_PRECOMMIT != $R5_SAFE_PRECOMMIT ]]; then
  builtin printf '%s\n' "published R5 precommit SHA-256 does not match" >&2
  exit 1
fi
(
  cd "$R5_SCRIPT_DIR"
  shasum -a 256 -c SHA256SUMS
  if find . -mindepth 1 ! -type f -print -quit | grep -q .; then
    builtin printf '%s\n' "R5 precommit must be a flat regular-file tree" >&2
    exit 1
  fi
  if find . -type f -links +1 -print -quit | grep -q .; then
    builtin printf '%s\n' "R5 precommit contains a hard-linked file" >&2
    exit 1
  fi
  if ! diff -u \
    <(awk '{print $2}' SHA256SUMS | LC_ALL=C sort) \
    <(find . -type f ! -path './SHA256SUMS' -print | sed 's#^\./##' | LC_ALL=C sort); then
    builtin printf '%s\n' "R5 precommit closed-world inventory mismatch" >&2
    exit 1
  fi
)
(
  cd "$R5_ROOT"
  shasum -a 256 -c "$R5_SCRIPT_DIR/BOUND_INPUTS_SHA256SUMS"
)
if [[ ! -x $R5_SCRIPT_DIR/launch_with_openrouter_key.sh || \
      ! -x $R5_SCRIPT_DIR/run_paid_variant_hardened.sh ]]; then
  builtin printf '%s\n' "R5 credential launcher and wrapper must be directly executable" >&2
  exit 1
fi

# No credential is exported while the only Python runtime allowed to receive
# it is proven against the same platform-specific identity sealed by R5.
R5_VENDOR_PYTHON="$R5_ROOT/vendor/memory-benchmarks/.venv/bin/python"
R5_EXPECTED_PYTHON_TARGET="/Users/william/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12"
if [[ ! -L $R5_VENDOR_PYTHON || \
      $(/usr/bin/readlink "$R5_VENDOR_PYTHON") != "$R5_EXPECTED_PYTHON_TARGET" || \
      ! -x $R5_EXPECTED_PYTHON_TARGET ]]; then
  builtin printf '%s\n' "sealed vendor Python identity changed" >&2
  exit 1
fi
R5_VENDOR_PYTHON_SHA=$(/usr/bin/shasum -a 256 "$R5_VENDOR_PYTHON" | /usr/bin/awk '{print $1}')
if [[ $R5_VENDOR_PYTHON_SHA != 7b05d803bbc1bbfc81644af4faf2b88f0a37b8de96b9f42c1e08033e2cd0848a ]]; then
  builtin printf '%s\n' "sealed vendor Python bytes changed" >&2
  exit 1
fi

verify_completed_arm_before_telemetry() {
  local variant=$1
  if /usr/sbin/lsof -nP -iTCP:8890 -sTCP:LISTEN 2>/dev/null | /usr/bin/grep -q .; then
    builtin printf '%s\n' "proxy port remains live before later telemetry" >&2
    return 1
  fi
  /usr/bin/env -i LANG=C LC_ALL=C PYTHONDONTWRITEBYTECODE=1 \
    "$R5_VENDOR_PYTHON" -I -S -B \
    "$R5_SCRIPT_DIR/verify_dynamic_admission.py" verify-arm-gate \
      --repository-root "$R5_ROOT" \
      --requirements "$R5_SCRIPT_DIR/execution-authorization-requirements.json" \
      --variant "$variant" >/dev/null
}

case $R5_ACTION in
  telemetry-before-v7)
    set -- "$R5_VENDOR_PYTHON" -I -S -B \
      "$R5_SCRIPT_DIR/capture_provider_telemetry.py" --output \
      "$R5_ROOT/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1/precall/provider-telemetry-before-v7.json"
    ;;
  telemetry-before-v13)
    verify_completed_arm_before_telemetry v7-control
    set -- "$R5_VENDOR_PYTHON" -I -S -B \
      "$R5_SCRIPT_DIR/capture_provider_telemetry.py" --output \
      "$R5_ROOT/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1/between/provider-telemetry-before-v13.json"
    ;;
  telemetry-after-pair)
    verify_completed_arm_before_telemetry v13-first
    set -- "$R5_VENDOR_PYTHON" -I -S -B \
      "$R5_SCRIPT_DIR/capture_provider_telemetry.py" --output \
      "$R5_ROOT/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1/postrun/provider-telemetry-after-pair.json"
    ;;
  evaluate-v7)
    set -- "$R5_SCRIPT_DIR/run_paid_variant_hardened.sh" \
      reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1/v7-control \
      narratordb-intelligence-dev42-v7-gpt54mini \
      reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/longmemeval_s_dev42.json
    ;;
  evaluate-v13)
    set -- "$R5_SCRIPT_DIR/run_paid_variant_hardened.sh" \
      reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1/v13-first \
      narratordb-intelligence-dev42-v13-replay-v7gpt54mini \
      reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716/longmemeval_s_dev42.json
    ;;
esac
cd "$R5_ROOT"

OPENROUTER_API_KEY=$R5_RUNTIME_OPENROUTER_KEY
export OPENROUTER_API_KEY
unset R5_RUNTIME_OPENROUTER_KEY R5_SAFE_PRECOMMIT R5_EXPORTED_NAME R5_ACTION
exec "$@"
