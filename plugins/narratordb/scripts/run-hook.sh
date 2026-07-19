#!/bin/sh
# Bounded, fail-open bridge from Codex lifecycle hooks to NarratorDB.
# Hook execution is private and offline: credentials are not inherited and the
# memory command cannot download models or contact a model provider.

set -u

event="${1:-}"
case "$event" in
  SessionStart|PreCompact|Stop) ;;
  *) exit 0 ;;
esac

resolve_executable() {
  name="$1"
  shift
  resolved="$(command -v "$name" 2>/dev/null || true)"
  if [ -n "$resolved" ] && [ -x "$resolved" ]; then
    printf '%s\n' "$resolved"
    return 0
  fi
  for candidate in "$@"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

python_bin="$(resolve_executable python3 \
  /usr/bin/python3 \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3)" || exit 0
uvx_bin="$(resolve_executable uvx \
  "${HOME:-}/.local/bin/uvx" \
  /opt/homebrew/bin/uvx \
  /usr/local/bin/uvx)" || exit 0

"$python_bin" -c '
import os
import signal
import subprocess
import sys

SOURCE = "narratordb-memory[mcp] @ git+https://github.com/WilliamJnsson/NarratorDB.git@5cda4adbc5c72bec06fa5a63a81bae42369007ec"
HOOK_TIMEOUT_SECONDS = 8.0
MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024

event = sys.argv[1]
payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)[:MAX_INPUT_BYTES]

# Use an allowlist instead of inheriting the full Codex environment. In
# particular, provider tokens and unrelated application secrets never reach
# the hook subprocess.
allowed = (
    "HOME",
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "XDG_CACHE_HOME",
    "UV_CACHE_DIR",
    "NARRATORDB_DATA_DIR",
    "NARRATORDB_PATH",
    "NARRATORDB_DB_PATH",
    "NARRATORDB_USER_ID",
    "NARRATORDB_WORKSPACE_ID",
    "NARRATORDB_AUTO_CAPTURE",
    "NARRATORDB_ALLOW_PATH_FALLBACK_WRITES",
)
env = {key: os.environ[key] for key in allowed if os.environ.get(key)}
env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
env.update(
    {
        "UV_OFFLINE": "1",
        "NARRATORDB_LOCAL_ONLY": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "DO_NOT_TRACK": "1",
        "NARRATORDB_TELEMETRY": "0",
    }
)

uvx = sys.argv[2]
if not os.path.isfile(uvx) or not os.access(uvx, os.X_OK):
    raise SystemExit(0)

try:
    process = subprocess.Popen(
        [uvx, "--from", SOURCE, "narratordb-hook", event],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(payload, timeout=HOOK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise SystemExit(0)
except (OSError, ValueError):
    raise SystemExit(0)

if process.returncode == 0 and output:
    sys.stdout.buffer.write(output[:MAX_OUTPUT_BYTES])
' "$event" "$uvx_bin" || true

# Memory must never prevent Codex from continuing.
exit 0
