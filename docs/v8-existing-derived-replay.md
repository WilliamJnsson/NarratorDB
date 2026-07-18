# V8 existing-derived development replay

This protocol re-runs local intelligence retrieval and rendering from a copy of
a completed V7 development database. It deliberately reuses V7 compiler output.
It is a development diagnostic, not a fresh end-to-end V8 result or an unbiased
benchmark score.

The frozen protocol profile is
`benchmark_records/profiles/v8_existing_derived_replay_protocol_20260716.json`.
Bind every placeholder below in a per-attempt run record and hash that record
before starting the replay.

## Namespace rule

The official harness derives the database scope as
`longmemeval_<question_id>_<run_id>`. `--project-name` only selects the output
directory. Therefore:

- use a new V8 project name and a previously nonexistent output root;
- retain the exact V7 `run_id`;
- copy only V7's completed `_ingestion_*.json` checkpoints into the fresh V8
  prediction directory;
- never copy a per-question prediction JSON.

A new run ID does not create a clean replay. It points search at empty user
scopes and can make the harness attempt ingestion.

## Fail-closed server

`--existing-derived-replay-fingerprint` is valid only in intelligence mode. It
constructs no compiler, compiler cache, or usage ledger. Add, finalize, and
delete are rejected. Search is rejected unless every registered current source
in that user scope has a `complete` or `partial` compiler job under the exact
declared fingerprint. Empty scopes and any missing or nonterminal job fail.

The health response states that replay is read-only. Scoped, content-free
readiness is available from:

```sh
curl -fsS "http://127.0.0.1:$PORT/replay/diagnostics?user_id=$USER_ID"
```

The response contains only the declared fingerprint and lifecycle counts, not
messages, claims, questions, session IDs, or answers.

## Safe command sequence

Run this only after the V7 producer and its backend have stopped. Paths are
placeholders; preserve the expanded commands in the attempt record.

```sh
set -eu

test -n "$V7_DB"
test -n "$V7_PREDICTION_DIR"
test -n "$V7_RUN_ID"
test -n "$V7_COMPILER_FINGERPRINT"
test -n "$DEV42_DATASET"
test -n "$V8_SOURCE"
test -n "$HARNESS_SOURCE"
test -n "$V8_RUN_ROOT"
test -n "$V8_PROJECT"
test ! -e "$V8_RUN_ROOT"

sqlite3 "$V7_DB" "PRAGMA wal_checkpoint(TRUNCATE); PRAGMA quick_check;"
mkdir -p "$V8_RUN_ROOT/official-harness/predicted_$V8_PROJECT"
sqlite3 "$V7_DB" ".backup '$V8_RUN_ROOT/intelligence.db'"
cp "$V7_PREDICTION_DIR"/_ingestion_*.json \
  "$V8_RUN_ROOT/official-harness/predicted_$V8_PROJECT/"

test "$(find "$V8_RUN_ROOT/official-harness/predicted_$V8_PROJECT" \
  -maxdepth 1 -type f -name '_ingestion_*.json' | wc -l | tr -d ' ')" -eq 42
test "$(find "$V8_RUN_ROOT/official-harness/predicted_$V8_PROJECT" \
  -maxdepth 1 -type f -name '*.json' ! -name '_*' | wc -l | tr -d ' ')" -eq 0
sqlite3 "$V8_RUN_ROOT/intelligence.db" "PRAGMA quick_check;"
shasum -a 256 "$V7_DB" "$V8_RUN_ROOT/intelligence.db" "$DEV42_DATASET"
```

Before starting the harness, validate the 42 checkpoint records mechanically:
each must have the producer's chunk size, the exact V7 run ID, and a user ID
ending in that run ID. Do not print their content. Start the V8 backend with no
hosted credentials:

```sh
cd "$V8_SOURCE"
env -u OPENROUTER_API_KEY -u OPENAI_API_KEY -u AZURE_OPENAI_API_KEY \
  -u ANTHROPIC_API_KEY \
  uv run python -m narratordb.benchmark_server \
  --host 127.0.0.1 --port "$PORT" \
  --database "$V8_RUN_ROOT/intelligence.db" \
  --mode intelligence \
  --existing-derived-replay-fingerprint "$V7_COMPILER_FINGERPRINT" \
  --coalesce-timestamp-sessions \
  --context-token-budget 6000 \
  --merge-max-chars 1200
```

Poll `/replay/diagnostics` for every checkpoint user ID and abort unless all 42
responses say `ready: true`. Save only an aggregate of the lifecycle counts.
Snapshot content-free table counts before prediction.

Run the hash-pinned harness in predict-only mode. A dummy key satisfies the
harness client's constructor; the closed proxy blocks accidental external
HTTP(S), while `NO_PROXY` keeps the local backend reachable.

```sh
cd "$HARNESS_SOURCE"
env OPENAI_API_KEY=narratordb-predict-only-no-call \
  HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 \
  ALL_PROXY=http://127.0.0.1:9 NO_PROXY=127.0.0.1,localhost \
  python -m benchmarks.longmemeval.run \
  --project-name "$V8_PROJECT" \
  --run-id "$V7_RUN_ID" \
  --dataset-path "$DEV42_DATASET" \
  --all-questions --predict-only \
  --backend oss --mem0-host "http://127.0.0.1:$PORT" \
  --mode answerer --top-k 200 --top-k-cutoffs 20,50 \
  --max-workers 10 \
  --answerer-model no-call --judge-model no-call --provider openai \
  --output-dir "$V8_RUN_ROOT/official-harness"
```

Afterward, require exactly 42 fresh prediction JSONs, run the prediction audit,
repeat the readiness and content-free table-count checks, verify no replay cache
or usage sidecar appeared, and freeze the prediction manifest. Any optional
answerer/judge evaluation must use a copied evaluation directory so it cannot
mutate the immutable prediction archive.

Report the result as: **V8 local retrieval/render over reused V7 compiler
artifacts on consumed dev42**. This replay can select generic V8 changes, but it
cannot establish a fresh compiler score, untouched generalization, or
third-party reproducibility.
