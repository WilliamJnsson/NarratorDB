#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "[NarratorDB] package and syntax"
python3 -m compileall -q narratordb tests
python3 -m ruff check narratordb tests scripts
python3 -m build
python3 scripts/wheel_boundary.py dist/narratordb_memory-2.3.0-py3-none-any.whl
rm -rf /tmp/narratordb-wheel-smoke /tmp/narratordb-wheel-smoke.db*
python3 -m pip install --quiet --no-deps --target /tmp/narratordb-wheel-smoke \
  dist/narratordb_memory-2.3.0-py3-none-any.whl
(
  cd /tmp
  PYTHONPATH=/tmp/narratordb-wheel-smoke \
  NARRATORDB_PATH=/tmp/narratordb-wheel-smoke.db \
    python3 "$ROOT/scripts/wheel_smoke.py"
)

echo "[NarratorDB] API, persistence, concurrency, integrity, and protocol"
python3 -m unittest discover -v

echo "[NarratorDB] high-noise retrieval, typed records, and maintenance"
python3 -m tests.stress.provenance
python3 -m tests.stress.advanced_system
python3 -m tests.stress.code_retrieval
python3 -m tests.stress.typed_memory
python3 -m tests.stress.every_angle
python3 -m tests.stress.complex_retrieval \
  --min-custom-pass-rate 1.0 \
  --require-time-filter \
  --min-locomo-recall 0.844 \
  --min-locomo-answers 1301 \
  --max-locomo-p95-ms 250 \
  --min-scale-stored-messages 5006 \
  --min-scale-hit-rate 1.0 \
  --max-scale-p95-ms 250

echo "[NarratorDB] all standalone tests passed"
