#!/bin/sh
# Bounded, fail-open launcher for authenticated service lifecycle capture.

set -u

event="${1:-}"
case "$event" in
  PreCompact|Stop) ;;
  *) exit 0 ;;
esac

config="${NARRATORDB_SERVICE_HOOK_CONFIG:-${HOME:-}/.narratordb/service-hook.json}"

resolve_python() {
  for candidate in "${HOME:-}/.local/bin/python3" /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

launcher="$(resolve_python)" || exit 0

"$launcher" -c '
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys

MAX_INPUT_BYTES = 1024 * 1024
HOOK_TIMEOUT_SECONDS = 8.0

event, config_name = sys.argv[1:3]
config = Path(config_name).expanduser()
try:
    if config.is_symlink() or not config.is_file():
        raise SystemExit(0)
    if os.name != "nt" and stat.S_IMODE(config.stat().st_mode) & 0o077:
        raise SystemExit(0)
    values = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(values, dict) or set(values) != {"credentials_file", "python"}:
        raise SystemExit(0)
    python = Path(str(values["python"])).expanduser()
    credentials = Path(str(values["credentials_file"])).expanduser()
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise SystemExit(0)
    if not credentials.is_absolute() or not credentials.is_file() or credentials.is_symlink():
        raise SystemExit(0)
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    raise SystemExit(0)

payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)[:MAX_INPUT_BYTES]
allowed = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "NARRATORDB_AUTO_CAPTURE")
env = {key: os.environ[key] for key in allowed if os.environ.get(key)}
env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")

try:
    process = subprocess.Popen(
        [
            str(python),
            "-m",
            "narratordb.service_hook",
            event,
            "--credentials-file",
            str(credentials),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    try:
        process.communicate(payload, timeout=HOOK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
except (OSError, ValueError):
    pass
' "$event" "$config" || true

exit 0
