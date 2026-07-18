#!/bin/bash -p
exec </dev/null >/dev/null 2>/dev/null
set +x +v
unset BASH_ENV ENV CDPATH GLOBIGNORE BASH_XTRACEFD PROMPT_COMMAND
set -euo pipefail

root="/Users/william/Desktop/narratorDB"
recovery="$root/benchmark_records/reproduction-v13-paid-paired-scoring-r5-finalization-recovery-r3-20260716"
launcher="$recovery/run_offline_recovery.sh"

if [[ "${1-}" != "--clean-internal" ]]; then
  if (( $# != 1 )) || [[ "$1" != stage-a && "$1" != stage-b && "$1" != preflight-smoke && "$1" != stage-b-canary ]]; then
    exit 64
  fi
  if [[ "$1" == preflight-smoke || "$1" == stage-b-canary ]]; then
    exec /usr/bin/env -i \
      /bin/bash --noprofile --norc -p "$launcher" --clean-internal "$1"
  fi
  seal="${NARRATORDB_R5_RECOVERY_R3_PRECOMMIT_SHA256-}"
  exec /usr/bin/env -i \
    NARRATORDB_R5_RECOVERY_R3_PRECOMMIT_SHA256="$seal" \
    /bin/bash --noprofile --norc -p "$launcher" --clean-internal "$1"
fi
if (( $# != 2 )) || [[ "$2" != stage-a && "$2" != stage-b && "$2" != preflight-smoke && "$2" != stage-b-canary ]]; then
  exit 64
fi
stage="$2"

protocol="$recovery/recovery-protocol-r3.json"
profile="$recovery/offline-recovery.sb"
worker="$recovery/verify_finalization_recovery.py"
executable_manifest="$recovery/launcher-executables-r3.json"
output="$root/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-finalization-recovery-r3-20260716"
original_attempt="$root/reports/longmemeval-intelligence-dev42-v13-paid-paired-scoring-r5-20260716/attempt1"
python_root="/Users/william/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none"
python_real="$python_root/bin/python3.12"
seal="${NARRATORDB_R5_RECOVERY_R3_PRECOMMIT_SHA256-}"

if [[ "$stage" != preflight-smoke && "$stage" != stage-b-canary ]]; then
  if (( ${#seal} != 64 )) || [[ "$seal" == *[!0-9a-f]* ]]; then
    exit 65
  fi
elif [[ -n "$seal" ]]; then
  exit 65
fi
if [[ "$(pwd -P)" != "$root" ]]; then
  exit 66
fi

hash_file() {
  local path="$1"
  local expected="$2"
  local line
  line="$(/usr/bin/shasum -a 256 "$path")" || return 1
  [[ "${line%% *}" == "$expected" ]]
}

require_tool() {
  local path="$1"
  local expected_hash="$2"
  local expected_mode="$3"
  local expected_nlink="$4"
  local expected_size="$5"
  local require_codesign="$6"
  local metadata
  [[ -f "$path" && ! -L "$path" && -x "$path" ]] || return 1
  metadata="$(/usr/bin/stat -f '%HT|%z|%Lp|%l' "$path")" || return 1
  [[ "$metadata" == "Regular File|$expected_size|$expected_mode|$expected_nlink" ]] || return 1
  hash_file "$path" "$expected_hash" || return 1
  [[ "$(/bin/realpath "$path")" == "$path" ]] || return 1
  if [[ "$require_codesign" == true ]]; then
    /usr/bin/codesign --verify --strict "$path" || return 1
  fi
}

validate_tools() {
  local shasum_shebang
  local realpath_details

  # Bash predicates establish regular, non-symlink executables before the
  # pinned stat/hash/signature utilities are used to validate the full set.
  [[ -f /usr/bin/stat && ! -L /usr/bin/stat && -x /usr/bin/stat ]] || return 1
  [[ -f /usr/bin/shasum && ! -L /usr/bin/shasum && -x /usr/bin/shasum ]] || return 1
  [[ -f /usr/bin/perl && ! -L /usr/bin/perl && -x /usr/bin/perl ]] || return 1
  [[ -f /usr/bin/codesign && ! -L /usr/bin/codesign && -x /usr/bin/codesign ]] || return 1
  [[ -f /bin/realpath && ! -L /bin/realpath && -x /bin/realpath ]] || return 1

  require_tool /bin/bash fde343ee184953c1fa1185abddeaa8be61c6acbebae4eb54db5d6b55b09a5755 555 1 1293840 true || return 1
  require_tool /bin/chmod 8146d61f2d2c100b512e8cefc190698133889317404563bfc93aba6ab5c148e1 755 1 120656 true || return 1
  require_tool /bin/mkdir 08a20adeeff9bea14bae05c0a7f3c77c638b2b83fc3ab37e1c646e047bac7002 755 1 101472 true || return 1
  require_tool /bin/realpath 22cd7804166170874dddd490e146936b6957dbbbb93d14eacd86b8d0de3e3989 755 1 101072 true || return 1
  require_tool /bin/rm 0e7aa0987cecc8d8ca629e1c61857321e8e281a6c1d0711b21163a15e454dc9d 755 2 119184 true || return 1
  require_tool /usr/bin/codesign 214d455584d19abc0d74d02b9cbc7d3da6bdcb0596c235e6156dd9ed2f4e1ba7 755 1 459824 true || return 1
  require_tool /usr/bin/env 6e506aec3c0cff703ac1e66cedc6f1945354ad41339a38db4425c7c88227128f 755 1 102368 true || return 1
  require_tool /usr/bin/mktemp 7bb3299fdb41f16ea5d9f7748cb5cb654b93208e0a1d1d78360145dcbbfb21fe 755 1 101728 true || return 1
  require_tool /usr/bin/perl abda2bfd23a6c9a8e57adf2291f0aea4abd8faf440558ee49fe4ced55e8d9ad0 755 1 101840 true || return 1
  require_tool /usr/bin/sandbox-exec 8290e4be7387a0df83cd1559e86afd880464f269450573d012795761fe298f16 755 1 102560 true || return 1
  require_tool /usr/bin/shasum 0812595f981a26f813d98dc380af14d4af427626c9339eda29eb849ae13de1e3 755 2 9979 false || return 1
  require_tool /usr/bin/stat 934656def5cfb8e85b2e4d983bb59ba97479cec49b63b4ea2fa42d067c569242 755 2 118768 true || return 1
  require_tool /usr/bin/sw_vers f4704a35bc196e6dd101a7de40f9e9ce51dd17bdba7ef29ce465a00d123f2ec5 755 1 135536 true || return 1
  require_tool /usr/bin/uname c189136263d277786f29a16eb3137de7bcf4512d2282d0036f440022f325bfc4 755 1 101456 true || return 1
  require_tool "$python_real" 7b05d803bbc1bbfc81644af4faf2b88f0a37b8de96b9f42c1e08033e2cd0848a 755 1 18073888 true || return 1

  IFS= read -r shasum_shebang < /usr/bin/shasum || return 1
  [[ "$shasum_shebang" == '#!/usr/bin/perl' ]] || return 1
  realpath_details="$(/usr/bin/codesign -dv --verbose=4 /bin/realpath 2>&1)" || return 1
  [[ "$realpath_details" == *$'\nIdentifier=com.apple.realpath\n'* ]] || return 1
}

if ! validate_tools; then
  exit 70
fi
if [[ "$(/bin/realpath "$root")" != "$root" ]] \
  || [[ "$(/bin/realpath "$recovery")" != "$recovery" ]] \
  || [[ "$(/bin/realpath "$python_root")" != "$python_root" ]] \
  || [[ "$(/bin/realpath "$python_real")" != "$python_real" ]] \
  || [[ "$(/bin/realpath "$profile")" != "$profile" ]] \
  || [[ "$(/bin/realpath "$worker")" != "$worker" ]] \
  || [[ "$(/bin/realpath "$executable_manifest")" != "$executable_manifest" ]]; then
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
if [[ ! -f "$executable_manifest" || -L "$executable_manifest" ]] \
  || ! hash_file "$executable_manifest" 718b320ad80b37870a20421f0de654e4c8e9e74c5e5f8956cc4ff825434140d9; then
  exit 70
fi

umask 077
scratch="$(/usr/bin/mktemp -d /private/tmp/narratordb-r5-recovery-r3.XXXXXXXX)"
cleanup() {
  /bin/chmod -R u+w "$scratch" || true
  /bin/rm -rf "$scratch" || true
}
trap cleanup EXIT HUP INT TERM
/bin/mkdir -m 700 "$scratch/home" "$scratch/tmp"
if [[ "$(/bin/realpath "$scratch")" != "$scratch" ]] \
  || [[ "$(/usr/bin/stat -f '%Lp' "$scratch")" != "700" ]]; then
  exit 72
fi

if [[ "$stage" == preflight-smoke ]]; then
  smoke_output="$scratch/output"
  /bin/mkdir -m 700 "$smoke_output"
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
    -D "OUT=$smoke_output" \
    -f "$profile" \
    "$python_real" -I -S -B "$worker" --preflight-invalid
  status=$?
  set -e
  if (( status != 1 )) || [[ -e "$output" || -L "$output" ]]; then
    exit 73
  fi
  exit 0
fi

if [[ "$stage" == stage-b-canary ]]; then
  canary_output="$scratch/output"
  /bin/mkdir -m 700 "$canary_output"
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
    -D "OUT=$canary_output" \
    -f "$profile" \
    "$python_real" -I -S -B "$worker" stage-b-canary \
    --repository-root "$root" \
    --protocol "$protocol"
  status=$?
  set -e
  if (( status != 0 )) || [[ -e "$output" || -L "$output" ]]; then
    exit 74
  fi
  exit 0
fi

seal_manifest="$recovery/SHA256SUMS"
if [[ ! -f "$seal_manifest" || -L "$seal_manifest" ]] \
  || [[ "$(/usr/bin/stat -f '%Lp:%l' "$seal_manifest")" != "444:1" ]] \
  || ! hash_file "$seal_manifest" "$seal" \
  || ! (cd "$recovery" && /usr/bin/shasum -a 256 -c SHA256SUMS); then
  exit 71
fi

if [[ ! -e "$output" && ! -L "$output" ]]; then
  /bin/mkdir -m 700 "$output"
fi
output_mode="$(/usr/bin/stat -f '%Lp' "$output")"
if [[ "$(/bin/realpath "$output")" != "$output" ]]; then
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
  --published-recovery-seal-sha256 "$seal"
status=$?
set -e
exit "$status"
