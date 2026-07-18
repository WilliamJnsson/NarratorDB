#!/bin/bash -p
set +x +v
unset BASH_ENV ENV CDPATH GLOBIGNORE BASH_XTRACEFD PROMPT_COMMAND
set -euo pipefail

root="/Users/william/Desktop/narratorDB"
recovery="$root/benchmark_records/reproduction-v13-paid-paired-scoring-r5-finalization-recovery-r1-20260716"
launcher="$recovery/run_offline_recovery.sh"

if [[ "${1-}" != "--clean-internal" ]]; then
  if (( $# != 1 )) || [[ "$1" != stage-a && "$1" != stage-b ]]; then
    exit 64
  fi
  seal="${NARRATORDB_R5_RECOVERY_PRECOMMIT_SHA256-}"
  exec /usr/bin/env -i \
    NARRATORDB_R5_RECOVERY_PRECOMMIT_SHA256="$seal" \
    /bin/bash --noprofile --norc -p "$launcher" --clean-internal "$1"
fi
if (( $# != 2 )) || [[ "$2" != stage-a && "$2" != stage-b ]]; then
  exit 64
fi
stage="$2"

protocol="$recovery/recovery-protocol-r1.json"
profile="$recovery/offline-recovery.sb"
worker="$recovery/verify_finalization_recovery.py"
output="$root/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-finalization-recovery-r1-20260716"
original_attempt="$root/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1"
python_root="/Users/william/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none"
python_real="$python_root/bin/python3.12"
seal="${NARRATORDB_R5_RECOVERY_PRECOMMIT_SHA256-}"

if (( ${#seal} != 64 )) || [[ "$seal" == *[!0-9a-f]* ]]; then
  exit 65
fi
if [[ "$(pwd -P)" != "$root" ]]; then
  exit 66
fi
if [[ "$(/usr/bin/realpath "$root")" != "$root" ]] \
  || [[ "$(/usr/bin/realpath "$recovery")" != "$recovery" ]] \
  || [[ "$(/usr/bin/realpath "$python_root")" != "$python_root" ]] \
  || [[ "$(/usr/bin/realpath "$python_real")" != "$python_real" ]] \
  || [[ "$(/usr/bin/realpath "$profile")" != "$profile" ]] \
  || [[ "$(/usr/bin/realpath "$worker")" != "$worker" ]]; then
  exit 67
fi
case "$output/" in
  "$original_attempt"/*) exit 68 ;;
esac
if [[ "$(/usr/bin/sw_vers -productVersion)" != "26.5.2" ]] \
  || [[ "$(/usr/bin/sw_vers -buildVersion)" != "25F84" ]] \
  || [[ "$(/usr/bin/uname -m)" != "arm64" ]]; then
  exit 69
fi
if [[ "$(/usr/bin/shasum -a 256 /usr/bin/sandbox-exec | /usr/bin/awk '{print $1}')" != "8290e4be7387a0df83cd1559e86afd880464f269450573d012795761fe298f16" ]] \
  || ! /usr/bin/codesign -v /usr/bin/sandbox-exec >/dev/null 2>&1 \
  || [[ "$(/usr/bin/shasum -a 256 "$python_real" | /usr/bin/awk '{print $1}')" != "7b05d803bbc1bbfc81644af4faf2b88f0a37b8de96b9f42c1e08033e2cd0848a" ]]; then
  exit 70
fi
seal_manifest="$recovery/SHA256SUMS"
if [[ ! -f "$seal_manifest" || -L "$seal_manifest" ]] \
  || [[ "$(/usr/bin/stat -f '%Lp:%l' "$seal_manifest")" != "444:1" ]] \
  || [[ "$(/usr/bin/shasum -a 256 "$seal_manifest" | /usr/bin/awk '{print $1}')" != "$seal" ]] \
  || ! (cd "$recovery" && /usr/bin/shasum -a 256 -c SHA256SUMS >/dev/null 2>&1); then
  exit 71
fi

umask 077
scratch="$(/usr/bin/mktemp -d /private/tmp/narratordb-r5-recovery.XXXXXXXX)"
cleanup() {
  /bin/chmod -R u+w "$scratch" >/dev/null 2>&1 || true
  /bin/rm -rf "$scratch" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM
/bin/mkdir -m 700 "$scratch/home" "$scratch/tmp"
if [[ ! -e "$output" && ! -L "$output" ]]; then
  /bin/mkdir -m 700 "$output"
fi
output_mode="$(/usr/bin/stat -f '%Lp' "$output")"
if [[ "$(/usr/bin/realpath "$scratch")" != "$scratch" ]] \
  || [[ "$(/usr/bin/stat -f '%Lp' "$scratch")" != "700" ]] \
  || [[ "$(/usr/bin/realpath "$output")" != "$output" ]]; then
  exit 72
fi
if [[ "$stage" == stage-a && "$output_mode" != "700" ]]; then
  exit 72
fi
if [[ "$stage" == stage-b && "$output_mode" != "700" && "$output_mode" != "555" ]]; then
  exit 72
fi

set +e
/usr/bin/env -i \
  HOME="$scratch/home" \
  TMPDIR="$scratch/tmp" \
  LANG=C \
  LC_ALL=C \
  PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/sandbox-exec \
  -D "ROOT=$root" \
  -D "PYROOT=$python_root" \
  -D "PYTHON_REAL=$python_real" \
  -D "SCRATCH=$scratch" \
  -D "OUT=$output" \
  -f "$profile" \
  "$python_real" -I -S -B "$worker" "$stage" \
  --repository-root "$root" \
  --protocol "$protocol" \
  --published-recovery-seal-sha256 "$seal" \
  >/dev/null 2>/dev/null
status=$?
set -e
exit "$status"
