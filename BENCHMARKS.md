# Benchmark protocol and comparability

Two systems are tested under the same rules only when all of these are pinned:

1. dataset files and hashes;
2. question selection and random seed;
3. conversation/session chunking and timestamps;
4. retrieval cutoff and context formatting;
5. answer model, version, temperature, and prompt;
6. judge model, version, temperature, and prompt;
7. abstention handling and score aggregation;
8. retries, concurrency, and failed-sample policy.

Sharing a benchmark name is not enough. NarratorDB separates exact official
harness runs from key-free retrieval diagnostics so a proxy cannot be mistaken
for a vendor-comparable headline.

## Original-author comparable QA suite and API cost model (2026-07-17)

The runnable scored memory-QA protocols published by the benchmark authors are
larger than the short diagnostics below and are not identical to Mem0's forked
headline scopes. This primary comparable planning scope is derived from the
current official
[LoCoMo](https://github.com/snap-research/locomo),
[LongMemEval](https://github.com/xiaowu0162/LongMemEval), and
[BEAM](https://github.com/mohammadtavakoli78/BEAM) repositories.

| Benchmark | Original-author system-test scope | Paid evaluation calls |
|---|---|---:|
| LoCoMo QA | 10 conversations, 1,986 questions including 446 adversarial items | 1,986 answer calls; no model judge: deterministic token/stem F1 for categories 1-4 and deterministic adversarial handling for category 5 |
| LongMemEval-S | 500 questions | 500 answer calls and 500 `gpt-4o-2024-08-06` judge calls |
| BEAM, all tiers | 100 conversations and 2,000 questions: 400 at 128K (`100K` in files), 700 at 500K, 700 at 1M, and 200 at 10M | 2,000 answer calls, 6,046 rubric calls, 200 event fact-extraction calls, and answer-dependent event-alignment calls |

With NarratorDB's declared one-query-specific-answer request per question, the
primary S-track suite produces
4,486 answers and requires `11,232 + C` logical model calls, where `C` is
BEAM's answer-dependent event-alignment count. The authors do not mandate
multiple seeds or repeated runs. LoCoMo's author script can batch up to 20
questions, so the declared 1,986 answer requests are an implementation choice,
not an author mandate. The midpoint cost below assumes `C = 700`. LoCoMo's
repository also describes event
summarization and multimodal dialogue generation, but their released README
still marks those evaluators as coming soon; they cannot be represented as a
runnable original-author score. LongMemEval-M is a separate official
500-question scale track, while the oracle file is a reference-context control
rather than an ordinary memory-system track.

The following is a planning estimate, not provider billing and not a benchmark
result. It assumes a fresh, empty Intelligence database;
[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) as
the first-party write-time compiler and answerer at `$1/M` input, `$0.10/M`
cached input, and `$6/M` output tokens; the LongMemEval-prescribed
[GPT-4o](https://developers.openai.com/api/docs/models/gpt-4o) judge at
`$2.50/M` input and `$10/M` output; and BEAM's current
[GPT-4.1 Mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
judge at `$0.40/M` input and `$1.60/M` output. GPT-5.6 Luna requests above
272K input tokens carry the model page's higher long-context rates. Prices were
checked on 2026-07-17 and can change. Compiler estimates come from audited local
GPT-5 ledgers and source/session scaling, then reprice the projected tokens at
Luna's rates; they were not measured by a Luna run. Answer estimates use the
frozen top-200 GPT-5 prompt shapes plus disclosed reasoning-token assumptions.
The midpoint assumes no individual compiler request crosses the 272K threshold,
which a paid preflight must verify. The legacy low/high bands are retained for
traceability, but their scenario multipliers were not preserved; they are not
independently auditable bounds or confidence intervals.

| Fresh model-mode run | Expected USD | Legacy reported band USD* |
|---|---:|---:|
| LoCoMo QA | 39 | 26-58 |
| LongMemEval-S | 671 | 477-1,019 |
| BEAM, all four tiers | 349 | 298-427 |
| **Primary S-track QA suite** | **1,059** | **801-1,504** |
| Add official LongMemEval-M track | 6,727 | 4,776-10,245 |
| **QA S+M suite (oracle control excluded)** | **7,786** | **5,577-11,749** |

`*` Reported during planning, but not reconstructable from retained
low/mid/high component inputs. Use the formula-backed midpoint only as a
preflight estimate, not as a guaranteed budget ceiling.

A 15% operational reserve is approximately `$1,220` for the primary suite and
`$8,950` for the S+M suite. LongMemEval dominates model-mode cost because S has
23,867 session references and M is approximately ten times larger. Compilation
is performed once per fresh database; repeating only answer/judge evaluation
against the same frozen compiled databases is much cheaper.

The benchmark rules do not require a paid write-time compiler. NarratorDB
Private and Intelligence are therefore publishable as separate configurations
when dataset, answerer, judge, retrieval cutoff, failure policy, and all other
comparison controls are held fixed. Estimated API spend is about `$160` for the
primary Private-mode suite and `$190` with the official LongMemEval-M track;
local CPU/GPU time is
not included. No full-suite spend is authorized merely by this estimate.

The formulas, source revisions, price URLs, observed ledger inputs, assumptions,
and exclusions are preserved in the
[`machine-readable cost estimate`](benchmark_records/original_author_suite_cost_estimate_20260717.json).
It is a planning record, not a benchmark-run record and not an authorization to
spend. The compact record for the completed GPT-5 short diagnostics is
[`benchmark_records/narratordb_gpt5_official_short_diagnostics_20260717.json`](benchmark_records/narratordb_gpt5_official_short_diagnostics_20260717.json).
The ignored raw bundle is integrity-bound by the hashes in that tracked record.

## 2026-07-17 Mem0-harness short diagnostics via official OpenAI API

NarratorDB completed two frozen, clean-state short diagnostics using the pinned
Mem0 `memory-benchmarks` harness at commit
`4b61c5d31b9c668a12b4f5e78064248a02c82d2b`. The configuration requested
GPT-5 through the official OpenAI API for write-time compilation, answering,
and judging. The retained compiler response identifies `gpt-5-2025-08-07`;
the pinned harness does not retain returned answerer/judge model IDs, so those
two backend snapshots cannot be independently verified from the artifacts.
Custom OpenAI base URLs, dotenv overrides, and the OpenRouter credential were
absent from both processes. The compiler had no benchmark-question access.

| Scope | Primary metric | Result | Completeness |
|---|---:|---:|---|
| LoCoMo conversation 0, balanced first 10 from categories 1–4 | Binary accuracy | **38/40 (95.0%)** | 419/419 chunks, 40/40 questions, 0 failures |
| BEAM 100K conversation 0, all 10 ability types | Average rubric score | **0.65458 (65.458%)** | 94/94 chunks, 20/20 questions, 0 errors |
| BEAM 100K conversation 0 | Pass rate at score ≥ 0.5 | **15/20 (75.0%)** | Same complete run |

LoCoMo category accuracy was 100% multi-hop, 100% temporal, 90% open-domain,
and 90% single-hop. BEAM was strongest on knowledge update and preference
following (1.0 average each) and weakest on abstention (0.0). One BEAM answer
recovered after three logged generation timeouts; no question was dropped or
rejudged.

A separate deterministic local recomputation from every per-question artifact
matched the unified results; this was not an external third-party audit. All 19
LoCoMo and 3 BEAM compiler jobs completed under fingerprint
`gpt-5:4fd7ab66b910fb7fd16b`. Both databases and compiler caches passed SQLite
integrity checks. Content-free compiler spend was $0.7860125 for LoCoMo and
$0.3192825 for BEAM. The unmodified harness discards answer/judge token usage,
request IDs, and returned model IDs, so total evaluator cost must be read from
the OpenAI account dashboard rather than reconstructed. The same requested
model served as answerer and judge, so self-judge preference risk remains.

These are deliberately labeled short diagnostics. They are not full LoCoMo or
BEAM runs, untouched holdouts, statistical replications, or direct Mem0
comparisons. The tracked compact record binds the frozen protocol, source/data
hashes, results, and the 102-file raw manifest. The content-bearing raw bundle,
including commands, per-question outputs, ledgers, databases, and audits, is
retained only in the local ignored archive at
`reports/scored-short-canaries-20260717/`; it is not part of a clean Git clone.

As checked on 2026-07-14, Mem0's official suite exposes LoCoMo, LongMemEval,
and BEAM. Supermemory's separate
[MemoryBench](https://github.com/supermemoryai/memorybench) compares
Supermemory, Mem0, and Zep on LoCoMo, LongMemEval, and ConvoMem. NarratorDB
records these as different harness families: sharing a dataset does not make
their prompts, provider adapters, models, cutoffs, or aggregation identical.

### Distinct model roles

Do not conflate the memory system's embedding model with the benchmark reader
or judge. NarratorDB 2.0 has two user-selected memory modes. Private mode stores
and retrieves original messages without an extraction LLM. Intelligence mode
runs a query-independent write-time compiler and fuses source-grounded claims
with raw hybrid hits and bounded query-anchored session neighbors. Compiler V7
may receive up to eight locally selected prior claims as untrusted memory-key
hints, but only the current session's raw messages may ground or be cited by
new output. Both modes use the small local `all-MiniLM-L6-v2` encoder for
optional semantic retrieval and leave answer generation and grading to the
models selected by the official harness. Neither mode receives ground-truth
answers, evidence labels, question types, or judge outputs. A memory-system
comparison fixes the reader and judge while allowing each database's declared
retrieval pipeline to remain the system under test; fixing the embedding or
compiler model as well would instead be a narrower component comparison.

## Third-party reproducibility policy

A new benchmark may be labeled third-party reproducible only when an
independent operator can run the disclosed protocol from public inputs and
their own provider credentials, without private NarratorDB state. A publishable
run must satisfy all of the following:

1. Start from a clean, empty database and absent compiler cache, usage ledger,
   and prediction/evaluation output directories. Do not seed claims, retrieval
   payloads, or answers. Any intentional cache must start empty, be part of the
   declared system, and report its hit/miss/write totals; hidden caches and
   pre-saved answers are forbidden.
2. Freeze the exact NarratorDB source checkout and publish its commit, source
   archive SHA-256, and file manifest. Publish the exact public dataset hash,
   materialized dataset hash when applicable, split/ID-file hash, seed, and
   complete denominator.
3. Lock the third-party harness commit and dependency environment. Retain the
   lockfile or fully resolved package versions plus the Python/runtime,
   operating system, hardware, and local embedding snapshot/hash.
4. Disclose the exact memory mode; compiler, embedding, answerer, and judge
   model identifiers/snapshots; every hosted provider route and reasoning
   effort; prompt versions/hashes; context format and budget; top-k and scored
   cutoffs; session/finalization policy; temperature; retry policy; concurrency;
   and request-rate settings.
5. Publish copyable commands in execution order for environment setup, server
   startup, ingestion/finalization, prediction, evaluation, audits, and
   manifest creation. Show environment-variable names but redact credentials.
6. Archive the raw per-question prediction and evaluation files, unified
   results, logs, content-free usage ledgers, audits, frozen source/split files,
   and SHA-256 manifests. Retain every declared question and every failed or
   empty model output; do not selectively rerun or repair samples.
7. Report compiler, answerer, and judge cost separately and in total. Report
   latency sample counts and mean/median/p95/max, define whether measurements
   are end-to-end or steady-state, and state whether ingestion, compilation,
   finalization, network time, or cache warmup is included.

Hosted compiler, answerer, and judge calls can vary even with identical prompts
and temperature zero because provider routing, backend implementations, and
serving state are not fully deterministic. Pin dated model and provider routes
where possible, record the returned model/provider, finish reasons, retries,
and token usage, and never promise byte-identical fresh hosted completions.

A per-process spend fuse is not the campaign budget. Its admission calculation
must leave room for every concurrent request reservation plus its safety margin;
otherwise a run can reject valid retries while settled spend is still below the
nominal cap. The separately enforced provider/account and governance ceilings
remain the aggregate backstop. In a paired run, each arm must pass a score-blind
scope, identity, cost, and zero-empty completeness gate before the next arm is
authorized. A zero harness exit by itself is insufficient.

| Reproducibility claim | What it establishes |
|---|---|
| **Bit-for-bit artifact verification** | A downloaded frozen archive and its raw predictions/evaluations exactly match the published SHA-256 manifests. This verifies what was scored; it does not rerun hosted models. |
| **Fresh-run statistical reproducibility** | Independent clean-state runs use the same locked protocol and complete denominator, then report repeat count and predeclared score/cost/latency tolerances or distributions. Hosted outputs may differ byte-for-byte. |

### Reproduction manifest CLI

The installed entry point is `narratordb-benchmark-reproduction` (equivalently,
`python3 -m narratordb.benchmarks.reproduction_manifest`). It binds the source
archive, clean harness checkout, dataset and question scope, run configuration,
ordered command declaration, fresh-state paths, and completed artifact hashes.
All referenced paths must stay inside `--repository-root`, and both manifest
creation commands refuse to overwrite an existing output.

After assigning the four expected hash/commit shell variables, create and then
recheck a preflight immediately before starting the declared commands:

```bash
INPUTS=benchmark_inputs/reproduction-example
RUN=reports/reproduction-example
RECORDS=benchmark_records/reproduction-example

narratordb-benchmark-reproduction preflight \
  --repository-root "$PWD" \
  --source-archive "$INPUTS/narratordb-source.tar.gz" \
  --harness-root "$INPUTS/memory-benchmarks" \
  --dataset "$INPUTS/longmemeval_s.json" \
  --question-ids "$INPUTS/question_ids.json" \
  --config-file "$INPUTS/run-config.json" \
  --commands-file "$INPUTS/commands.json" \
  --database "$RUN/intelligence.db" \
  --compiler-cache "$RUN/intelligence.db.compiler-cache.sqlite3" \
  --fresh-path output-root="$RUN" \
  --fresh-path usage-ledger="$RUN/intelligence.db.compiler-usage.jsonl" \
  --fresh-path predictions="$RUN/official-harness" \
  --expected-source-sha256 "$SOURCE_SHA256" \
  --expected-harness-commit "$HARNESS_COMMIT" \
  --expected-dataset-sha256 "$DATASET_SHA256" \
  --expected-question-ids-sha256 "$QUESTION_IDS_SHA256" \
  --output "$RECORDS/preflight.json"

narratordb-benchmark-reproduction verify-preflight \
  --repository-root "$PWD" \
  --manifest "$RECORDS/preflight.json"
```

`run-config.json` records benchmark, run ID, project, mode, compiler, answerer,
judge, and retrieval settings. Each model role requires exact `model`,
`provider`, and `reasoning` values; retrieval requires `top_k` and sorted unique
`cutoffs`. `commands.json` is an ordered JSON array of unique
`{"step":"...","argv":["..."]}` entries. Never put a credential flag, value,
or environment assignment in either file; runtime credentials remain outside
the recorded argv.

After the declared run and audits finish, seal the complete artifact root and
verify it from scratch:

```bash
narratordb-benchmark-reproduction seal \
  --repository-root "$PWD" \
  --preflight "$RECORDS/preflight.json" \
  --artifact-root "$RUN" \
  --output "$RECORDS/sealed.json"

narratordb-benchmark-reproduction verify-seal \
  --repository-root "$PWD" \
  --manifest "$RECORDS/sealed.json"
```

`verify-preflight` recomputes immutable inputs and requires every declared run
path to remain absent. `seal` allows those run paths to exist, requires all of
them to be inside the artifact root, secret-scans and hashes every artifact,
and embeds the verified preflight. `verify-seal` then recomputes the preflight,
manifest checksum, and complete artifact set. A successful seal proves artifact
provenance and integrity; it does not prove that hosted completions are
deterministic.

A single fresh hosted rerun can validate protocol execution, but it is not by
itself evidence of statistical reproducibility. V7 first froze its prediction
phase with no score, then a precommitted paired protocol evaluated a
byte-identical copy. The resulting dev-42 score is a consumed-development
diagnostic, not an untouched or statistically replicated claim.

### Fail closed after each evaluated arm

Paired runs must audit each arm before any telemetry, authorization, or model
call for the next arm. `narratordb-benchmark-arm-gate` reruns the evaluation
audit against the frozen prediction tree and returns nonzero unless question
scope, cutoffs, denominators, final answers and judges, payload integrity,
model/provider identities, known costs, and the declared arm cost cap all pass:

```bash
narratordb-benchmark-arm-gate \
  --evaluated-directory "$ARM/evaluation/official-harness/predicted_$PROJECT" \
  --frozen-directory "$FROZEN/predicted_$PROJECT" \
  --usage-log "$ARM/evaluation/openrouter-usage.jsonl" \
  --evaluator-log "$ARM/evaluation/evaluate.log" \
  --expected-questions 42 \
  --cutoffs 20,50 \
  --question-id-file "$QUESTION_IDS" \
  --allowed-request-model z-ai/glm-5.2 \
  --allowed-request-model deepseek/deepseek-v4-flash-20260423 \
  --allowed-provider DeepInfra \
  --max-cost-usd 2.50 \
  --output "$ARM/evaluation/arm-gate.json" >/dev/null
```

Repeat each allowed model and provider flag as needed. The output is created
once and made read-only. It is deliberately score-blind: it contains fixed
denominator totals, sanitized validation findings and counts, usage/route/cost
metadata, completeness booleans, content-free failure codes, and a commitment
to the internal audit, but no correct numerator, accuracy, verdict, answer,
judge text, or prompt content. A recovered malformed upstream response remains
visible as retry telemetry and is not itself a failure when the final arm is
complete.

Before-next-arm authorization must bind the gate artifact's exact SHA-256 and
require `authorized: true`, both completeness booleans, exact declared cutoffs
and denominators, every validation count zero, zero unknown costs and invalid
identities, allowlisted routes, and settled cost at or below the precommitted
arm cap. Any nonzero gate exit is terminal for that attempt: preserve it and
start a later retry only under a new precommit and attempt root.

## Exact Mem0 harness mode

`narratordb.benchmark_server` implements the OSS API consumed by
[Mem0's memory-benchmarks](https://github.com/mem0ai/memory-benchmarks). The
official repository therefore owns LongMemEval loading, pair chunking, top-k,
answer prompting, judge prompting, concurrency, and aggregation.

### Leakage and tuning guard

The compatibility endpoint receives conversation messages, timestamps, session
metadata, the user scope, the search query, and the requested limit. It does not
receive the ground-truth answer, evidence-session labels, question type, or judge
result. NarratorDB's production retrieval code must not branch on benchmark
names, question identifiers, expected answers, or dataset-specific facts.
Prior claims supplied to compiler V7 are selected locally from active memory
before the associated scope's search query is submitted. The compiler never
receives the later question, answer, evidence labels, or judge output, and
references are not valid evidence for new claims.

Once a calibration question has been inspected and used to motivate a change,
it is development data. A rerun on that same calibration can verify a regression
fix, but it is not an unbiased competitor score. A publishable comparison must
freeze the implementation first and then use the complete official set or a
predeclared, previously uninspected holdout under identical model and harness
settings.

The restored dev-42 split has now been inspected. Its frozen V6 result exposed
generic assistant-memory, ranking, and structural-excerpt failures and therefore
motivated the current V7 and fused-retrieval work. Those changes contain no
question IDs or expected answers, but they still have no unbiased score; any
rerun on dev-42 is a development regression check only.

The 2026-07-14 calibrations pinned:

- official harness commit: `4b61c5d31b9c668a12b4f5e78064248a02c82d2b`;
- cleaned LongMemEval_S SHA-256:
  `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`;
- deterministic stratified samples with seed 42: a six-question smoke and a
  30-question run with five questions per type;
- official top-k cutoffs 10, 20, 50, and 200.

Start NarratorDB with a temporary isolated database:

```bash
python3 -m narratordb.benchmark_server --host 127.0.0.1 --port 8889
```

That command tests Private mode. To test the opt-in Intelligence mode with its
default hosted compiler, supply the credential only through the process
environment and pin every compiler parameter:

```bash
export OPENROUTER_API_KEY='set-this-outside-shell-history'
python3 -m narratordb.benchmark_server \
  --host 127.0.0.1 \
  --port 8889 \
  --database /path/to/intelligence.db \
  --mode intelligence \
  --compiler openrouter \
  --model openai/gpt-5.4-mini \
  --provider Azure \
  --reasoning minimal \
  --max-output-tokens 8192 \
  --coalesce-sessions \
  --context-token-budget 6000 \
  --compiler-max-cost-usd 180
```

Hosted benchmark startup requires `--compiler-max-cost-usd`; the effective
value belongs in the run record. This is a cumulative local USD ledger stop,
not a currency converter or an account-wide budget. It is soft because a call
that starts below the threshold can finish above it, and separate fresh ledgers
do not share a total. For the historically declared full run, this was a $180
local soft USD fuse, not the account cap. The provider account cap was $250,
while the closed experiment authorization was €300 across canaries, retries,
full runs, answering, and judging. The €300 value was a governance ceiling
rather than a value this USD ledger converted or enforced; it authorizes no new
run.

The historical campaign inventory is declared in
`benchmark_records/budgets/narratordb_95_campaign_20260716.json`. Audit its
immutable records and a read-only prefix of every live usage ledger with:

```bash
narratordb-benchmark-budget-audit \
  --declaration benchmark_records/budgets/narratordb_95_campaign_20260716.json \
  --provider-cap-usd 250
```

The auditor permits usage metadata only: prompt, question, answer, request,
response, and completion content are rejected, as are credential-shaped data,
duplicate spend sources, negative/non-finite amounts, and malformed JSONL. A
live ledger can append while it is read; the report identifies that condition
and hashes the exact audited prefix. Because no FX source or timestamp is
declared, the report records the €300 ceiling with no fabricated EUR observed
spend or headroom.

When a development change affects only local retrieval/rendering, the
fail-closed existing-derived replay can isolate it without another compiler
run. It is intentionally narrower than fresh-run reproduction: it reuses a
copied compiler database, requires the producer fingerprint and unchanged
run-ID scopes, creates no compiler/cache/usage ledger, rejects all exposed
mutations, and labels any score as a consumed-development replay. See
[`docs/v8-existing-derived-replay.md`](docs/v8-existing-derived-replay.md) and
the hash-frozen protocol in
`benchmark_records/profiles/v8_existing_derived_replay_protocol_20260716.json`.

`--coalesce-sessions` is part of the adapter configuration under test. It joins
pair-level writes only when the harness supplies the same explicit session ID
or exact timestamp, compiles the prior session at a boundary, and flushes all
remaining registered sessions before search for compatibility with unchanged
harnesses. `query_debug.finalization_ms` is the synchronous pending compiler
work, while `query_debug.query_ms` and `query_debug.timings_ms.total` measure the
subsequent local retrieval/fusion. `query_debug.lazy_finalized_sessions` is the
number materialized in that compatibility step. `backend_ms` and the harness's
HTTP latency include both phases. The pinned client discards `query_debug`, so
its prediction files retain only end-to-end wall latency. Repeated timestamps
therefore coalesce by design and must be reported in the run record. Without an
explicit ID or timestamp, unrelated writes are not merged. Raw messages are
committed before compilation, and the compiler credential is never stored in
project configuration, the database, the cache, or benchmark usage logs.

Streaming clients that know ingestion is complete can keep compilation entirely
outside search by explicitly finalizing the current user scope (or one explicit
session) before querying:

```bash
curl -X POST http://127.0.0.1:8889/memories/finalize \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"benchmark-user"}'
```

Finalization is query-free and idempotent. It materializes only the current
source hash when that hash lacks a terminal job for the active compiler
fingerprint; repeating the request performs no compiler work. Appending new raw
messages changes the current source hash and deliberately reopens that session
for one new finalization lineage. Responses distinguish matched, complete,
partial, and in-progress sessions, and an unknown explicit session returns
`not_found`. The pinned official harness does not call this extension, so its
first search retains the labeled compatibility flush unless the lifecycle
protocol is explicitly extended and disclosed.

Calling finalize changes the lifecycle protocol and must be disclosed in a
comparison. It moves compiler time before search rather than removing it;
repeated clean searches should report zero lazy-finalized sessions. This is the
production-shaped path for clients that know ingestion has ended, while the
unchanged official harness remains the strict interoperability path.

### Synthetic Intelligence compiler canary

Before paying for a LongMemEval ingestion, run the isolated three-call
compiler smoke. This is separate from the official six-question benchmark
canary: it does not load LongMemEval, retrieve benchmark questions, generate
answers, invoke a judge, or produce a benchmark score.

ChatGPT-subscription Codex CLI profile:

```bash
narratordb-intelligence-canary \
  --compiler codex-cli \
  --model gpt-5.4-mini \
  --reasoning low \
  --codex-cli-version 'codex-cli 0.144.4' \
  --codex-timeout-seconds 300 \
  --codex-max-invocations 6 \
  --codex-max-concurrency 1 \
  --semantic-max-attempts 2 \
  --min-request-interval-seconds 0 \
  --report /path/to/codex-cli-canary.json
```

This backend requires `codex login status` to report ChatGPT authentication and
rejects API-key authentication. Its six-process fuse covers two semantic
attempts for each of the three sessions; a clean pass uses three. It has no
OpenRouter USD fuse and records zero marginal API cost with cost source
`subscription`, which is not a claim that subscription capacity is free or
unlimited. The exact CLI version, executable identity, model alias, reasoning
effort, policy, prompt, and schema participate in the compiler fingerprint.
See [`docs/codex-cli-compiler.md`](docs/codex-cli-compiler.md) for the staged
promotion protocol.

The initial 2026-07-17 subscription canary passed 3/3 jobs and all 17 checks on
the first attempt for every session. It made zero recall-time upstream calls,
used 24,069 input, 3,584 cached-input, and 3,078 output tokens across three
Codex tasks, and completed in 55.992 seconds. The content-safe report SHA-256 is
`0a51a4550716c988ff6d8c69aff185f6ee238c853ba497327992001f5940fc4d`;
the compact record is
[`benchmark_records/narratordb_codex_cli_canary_20260717.json`](benchmark_records/narratordb_codex_cli_canary_20260717.json).
This validates the adapter and synthetic memory behavior only. It does not
establish LongMemEval accuracy or enough subscription capacity for a development
or full build.

OpenRouter GPT-5.4 Mini canary profile:

```bash
OPENROUTER_API_KEY=... narratordb-intelligence-canary \
  --model openai/gpt-5.4-mini \
  --provider Azure \
  --reasoning minimal \
  --max-output-tokens 8192 \
  --max-cost-usd 1.00 \
  --report /path/to/gpt54mini-canary.json
```

OpenRouter Luna Pro alias canary profile:

```bash
OPENROUTER_API_KEY=... narratordb-intelligence-canary \
  --model openai/gpt-5.6-luna-pro \
  --provider Azure \
  --reasoning low \
  --max-output-tokens 8192 \
  --max-cost-usd 1.00 \
  --report /path/to/luna-pro-canary.json
```

This `openai/gpt-5.6-luna-pro` slug is the disclosed OpenRouter alias used by
the historical experiments. The first-party OpenAI model is instead
`gpt-5.6-luna` through NarratorDB's fixed `openai` compiler route; the two names
must not be conflated.

Choose a disclosed positive `--max-cost-usd` that fits the experiment budget;
`1.00` above is only an example and remains a soft per-ledger stop. The CLI
accepts no credential argument. It reads `OPENROUTER_API_KEY` at runtime and
uses a new temporary database, compiler cache, and usage ledger. Exactly three
synthetic, query-independent session compilations exercise assistant-resource
retention and grounding, prior-claim context for an explicit update, stable-key
supersession, and a long numbered-list excerpt. Recall then runs locally and
must add zero upstream usage events.

Promotion requires a zero exit status and `status: "passed"`, all three jobs
complete, every listed check true, and observed model/provider values matching
the declared route. The built-in checks cover prior-claim delivery,
old-value supersession, assistant-source grounding, numbered-item recall,
source evidence for every claim, database/index health, zero recall-time
upstream requests, fresh-cache totals of zero hits and three
misses/writes/entries, and reported cost within the declared stop. A route
mismatch or missing route
observation is a review failure even though it is surfaced separately from the
built-in boolean checks.

The JSON report is deliberately content-free. It contains configuration and
compiler fingerprint, aggregate usage/cost, separate ingestion, enrichment,
local-recall and total timings, route observations, cache/job counts, and
boolean checks. It omits credentials, prompts, source messages, derived claims,
queries, and recalled text. Archive the report with the later benchmark record;
do not present it as the official six-question canary or an accuracy result.

The first V8 GLM-5.2/DeepInfra synthetic canary is preserved as a failed report,
not overwritten by a retry. It made zero recorded usage events and cost $0.
The report at
`reports/intelligence-glm52-canaries-20260716/v8-glm52-synthetic-canary.json`
has SHA-256
`6cbd1f66fe4e2693c40c3134feb56c00a4052c637c51a65d591bd19244bda1a2`.
The original profile is frozen at
`5996e8d2664a96d22fb1373fc1e92092348fa9eebdd0a322c633a57ee831f65f`.
The pinned official ZDR endpoint listing advertised `max_tokens` for the
DeepInfra GLM-5.2 endpoint, while the prior compiler transport sent
`max_completion_tokens` with strict `require_parameters`. That evidence makes
parameter ineligibility the leading explanation for the zero-cost HTTP 404; it
does not prove a model-quality failure. The transport-only repair profile was
committed before retry at
`benchmark_records/profiles/glm52_deepinfra_transport_repair_precommit_20260716.json`
(SHA-256
`781ca48a7e54d0d7f9e83c522671559d8d667d4f8f55d968a97b1a6b075e4742`)
and selects `max_tokens` through a persisted generic allowlist rather than a
model-name branch.

The later frozen V10 fixed-FP4 canary completed all three synthetic compiler
jobs on exact model `z-ai/glm-5.2` and final provider `parasail/fp4`, but its
overall status remains permanently failed because optional internal
route-attempt metadata was absent. That false-negative report is preserved at
`reports/intelligence-v10-canaries-20260716/v10-glm52-fp4-allowlist-canary.json`
with SHA-256
`59dd27c3cd3ce34e8eb011ff58d64c8a289285156662571a9f50788a3dd80ceb`.
It cost `$0.01986928` and ran no benchmark questions.

V11 changed only the generic content-safe canary observation rule and its
tests: missing optional attempt history is disclosed but non-gating, while
every observed attempt and the exact final model/provider remain fail-closed.
The compiler, prompt, schema, retrieval, grounding, route configuration, and
scoring code are byte-identical to V10. Its independently validated source
archive is
`reports/intelligence-v11-source-20260716T014343Z/narratordb-v11-source.tar.gz`
(SHA-256
`bf0ad80f343e8621bcec47533617424831e7621c1264a66b20dc63ee738ffe97`).
The precommitted V11 canary used the endpoint snapshot's predeclared intended
allowlist `parasail/fp4,wafer/fp4,deepinfra/fp4`; this description clarifies the
immutable precommit's phrase "fixed snapshot order," which does not refer to
the snapshot's raw endpoint-array order.

That V11 canary passed 3/3 jobs and all 16 checks on exact model
`z-ai/glm-5.2` and final provider `parasail/fp4`. It made zero upstream calls
during recall, recorded zero unknown costs, and cost `$0.01658584`. The report
is
`reports/intelligence-v11-canaries-20260716/v11-glm52-fp4-allowlist-canary.json`
with SHA-256
`d79c9971b1d91a4e8ebbecc81daa2e313bb489b03c8bad636ac408626c8cba54`.
This is a synthetic compiler/transport gate, not a LongMemEval score.

Run the pinned official checkout:

```bash
python3 -m benchmarks.longmemeval.run \
  --project-name narratordb-longmemeval \
  --backend oss \
  --mem0-host http://127.0.0.1:8889 \
  --dataset-path /path/to/longmemeval_s_cleaned.json \
  --output-dir /tmp/narratordb-official
```

Use the same answerer and judge configured for the comparison target. The first
exact `--predict-only` smoke processed 1,475 official conversation pairs with
zero failures and created all six retrieval outputs. That key-free result proves
exact-harness interoperability, not superiority.

### Cost-capped OpenRouter transport

For a neutral OpenRouter run, keep the official harness unchanged and route its
OpenAI-compatible requests through NarratorDB's local transport. The transport
does not retain prompts or completions. It pins the hosting provider and
reasoning effort, writes response token/cost metadata to JSONL, and refuses new
requests after the configured soft cost cap:

```bash
export OPENROUTER_API_KEY='set-this-outside-shell-history'
python3 -m narratordb.benchmarks.openrouter_proxy \
  --provider-allow DeepInfra,StreamLake,GMICloud,Baidu \
  --reasoning-effort high \
  --max-cost-usd 1.00 \
  --usage-log reports/openrouter-v4-flash.jsonl
```

Point the unchanged harness at the local transport and use a dated OpenRouter
snapshot instead of its floating alias:

```bash
OPENAI_API_KEY=local-transport \
OPENAI_BASE_URL=http://127.0.0.1:8890/v1 \
python3 -m benchmarks.longmemeval.run \
  --project-name narratordb-v4-flash \
  --backend oss \
  --mem0-host http://127.0.0.1:8889 \
  --dataset-path /path/to/longmemeval_s_cleaned.json \
  --answerer-model deepseek/deepseek-v4-flash-20260423 \
  --judge-model deepseek/deepseek-v4-flash-20260423 \
  --top-k 200 \
  --top-k-cutoffs 10,20,50,200 \
  --output-dir reports/longmemeval-v4-flash
```

The cap is deliberately process-scoped and soft for an already-running request.
Each admitted request reserves a configured amount atomically; if upstream
billing is absent, that reservation becomes a conservative cumulative charge
and the audit remains publication-incomplete until the actual charge is
reconciled. Low-budget calibration should still use `--max-workers 1` and a
provider/account cap remains the aggregate hard backstop. A final run can use
an explicit provider allowlist to survive endpoint throttling without changing
the requested model. HTTP protocol failures are ledgered without exception
content, and successful responses with an unverified model or provider return
502 rather than reaching the scorer. Provider responses, malformed HTTP 200
responses, snapshot, reasoning effort, retries, and the usage log are part of
the benchmark record. Router-attempt history is optional upstream telemetry:
when present every observed attempt must remain within the declared allowlist,
while exact final model/provider attestation is always required. A V4 Flash
neutral score must not be presented as a
reproduction of a vendor score obtained with a different answerer or judge.

OpenRouter's V4 Flash page observed on 2026-07-14 lists DeepInfra at $0.09 per
million input tokens, $0.18 per million output tokens, and $0.018 per million
cache-read tokens; `high` and `xhigh` reasoning are supported. Prices and
provider availability are live inputs, so the final run record must capture the
returned provider and response-level cost rather than relying only on this
estimate. See the
[dated V4 Flash pricing page](https://openrouter.ai/deepseek/deepseek-v4-flash-20260423/pricing).

The clean 30-question, four-cutoff calibration cost $0.164661 in response
metadata and $0.171511 at the account level. Linear scaling to 500 questions is
approximately $2.74 and $2.86 respectively; a $3.25 soft cap leaves retry
headroom. Evaluating only top 50 and top 200 would be roughly half the model
calls, but it would no longer be the declared four-cutoff run.

### 2026-07-14 V4 Flash calibration

The untouched baseline used NarratorDB commit
`4c6c10f54b761d4eaedca28ac52688a6653a6245`, DeepSeek V4 Flash snapshot
`deepseek/deepseek-v4-flash-20260423` for both answerer and judge, DeepInfra
only, high reasoning, temperature 0, one worker, and the official five-attempt
retry policy. All 30 sampled questions remained in the denominator.

| Cutoff | Correct | Accuracy |
|---:|---:|---:|
| 10 | 26/30 | 86.7% |
| 20 | 25/30 | 83.3% |
| 50 | 26/30 | 86.7% |
| 200 | 23/30 | 76.7% |

The run ingested 7,384 official conversation pairs with zero failures, storing
14,601 messages. Search latency over the 30 questions was 554.36 ms mean,
559.4 ms median, 737.3 ms p95 by nearest rank, and 1,110.7 ms maximum. The
answer/judge phase took 50 minutes 8 seconds because it made 240 required model
calls; database search itself remained subsecond at p95.

OpenRouter returned 242 billable responses: 240 stopped normally and two hit
the length limit before succeeding under the official retry policy. Thirteen
HTTP 429 attempts also recovered. Response usage reported 1,682,954 prompt
tokens, 50,304 cached prompt tokens, 93,426 completion tokens, 89,218 reasoning
tokens, and $0.164661. Account usage rose $0.171511 during the clean-run window.

The six-question smoke scored 5/6 (83.3%) at every cutoff and reported $0.033552
in response cost. A four-worker probe was deliberately stopped after upstream
rate limiting; its 21 successful calls and $0.016320 response cost are recorded
separately rather than blended into the clean result.

The detailed record is
[`benchmark_records/longmemeval_v4flash_30_20260714.json`](benchmark_records/longmemeval_v4flash_30_20260714.json).
This sample has now been inspected and is development data. It is not a fair
head-to-head result against Mem0's full 500-question GPT-5 runs, and it does not
support a claim that NarratorDB beats Mem0.

### Frozen optimized retrieval replay

After profiling showed that repeated stemming/tokenization consumed 97% of
search CPU, schema v2 began storing normalized terms once and the pure stemmer
received a bounded cache. The same archived 30-question database was copied,
migrated, and replayed without answerer or judge calls.

| Metric | Archived baseline | Frozen optimized | Change |
|---|---:|---:|---:|
| Mean search | 554.36 ms | 44.94 ms | 12.34× faster |
| Median search | 559.40 ms | 43.53 ms | 12.85× faster |
| p95 search | 737.30 ms | 88.76 ms | 8.31× faster |
| Max search | 1,110.70 ms | 91.26 ms | 12.17× faster |
| All labeled sessions in top 10 | 30/30 | 30/30 | retained |

Only five rankings changed. The generic current-state fix moved the current
company evidence session from rank 10 to rank 1; the current six-times fact
moved from rank 5 to rank 4; and generic countable-entity semantic rescue put
the previously absent aquarium facts at ranks 3 and 9. Interval, page-count,
and monetary questions are deliberately excluded from this state-count boost,
so their multi-fact evidence is not crowded out. Production retrieval contains
no question IDs, expected answers, or benchmark-specific fact constants.

The replay cost $0 and does not replace the archived V4 Flash score. Its full
per-question content hashes, stage timings, migrated database, LoCoMo report,
and manifest are summarized in
[`benchmark_records/narratordb_optimized_retrieval_30_20260714.json`](benchmark_records/narratordb_optimized_retrieval_30_20260714.json).

### Frozen full-500 protocol

After the engine hash is frozen, the official all-question retrieval pass is:

```bash
OPENAI_API_KEY=predict-only-local-placeholder \
python3 -m benchmarks.longmemeval.run \
  --project-name narratordb-frozen-full500-20260714 \
  --backend oss \
  --mem0-host http://127.0.0.1:8889 \
  --dataset-path /path/to/longmemeval_s_cleaned.json \
  --all-questions \
  --predict-only \
  --top-k 200 \
  --top-k-cutoffs 10,20,50,200 \
  --max-workers 10 \
  --seed 42 \
  --output-dir reports/longmemeval-full500-frozen-20260714/official-harness
```

The 2026-07-14 run resolved `sentence-transformers/all-MiniLM-L6-v2` to local
Hugging Face snapshot `c9745ed1d9f207416be6d2e6f8de32d1f16199bf`, using
SentenceTransformers 2.7.0, NumPy 2.2.6, and Torch 2.10.0+cu128. The immutable
raw archive includes a dereferenced copy of that snapshot; reproductions should
use `NARRATORDB_EMBEDDING_MODEL_DIR` to target it explicitly instead of relying
on a floating model name.

The official 500-question denominator contains 42 unique questions seen in one
or more earlier development runs: the 30-question V4 calibration, six-question
smoke, and 12-question key-free adapter sample overlap but are not identical.
Consequently, the all-500 result is labeled **final frozen**, not fully unseen.
The archive also defines and audits the exact 458-question complement as the
no-leakage holdout; both views are reported, and neither question list is ever
passed into production retrieval.

The placeholder is required because the official harness constructs its LLM
client before checking `--predict-only`; that path makes no LLM request. First
archive and verify the 500 frozen retrieval files. Then copy their complete
`predicted_narratordb-frozen-full500-20260714` directory into a separate paid
evaluation output root and run `--evaluate-only` there, using run ID
`5af42210`, V4 Flash for answerer and judge, and the content-free OpenRouter
transport. The official evaluator writes judgments into each prediction JSON,
so it must never target the immutable predict-only archive. No post-run tuning
is allowed if that score is presented as the frozen full-500 result.

```bash
mkdir -p reports/longmemeval-full500-v4flash-20260714/official-harness
cp -a \
  reports/longmemeval-full500-frozen-20260714/official-harness/predicted_narratordb-frozen-full500-20260714 \
  reports/longmemeval-full500-v4flash-20260714/official-harness/

OPENAI_API_KEY=local-transport \
OPENAI_BASE_URL=http://127.0.0.1:8890/v1 \
python3 -m benchmarks.longmemeval.run \
  --project-name narratordb-frozen-full500-20260714 \
  --dataset-path /path/to/longmemeval_s_cleaned.json \
  --all-questions --evaluate-only --run-id 5af42210 \
  --answerer-model deepseek/deepseek-v4-flash-20260423 \
  --judge-model deepseek/deepseek-v4-flash-20260423 \
  --top-k 200 --top-k-cutoffs 10,20,50,200 \
  --max-workers 10 --rpm 60 --seed 42 \
  --output-dir reports/longmemeval-full500-v4flash-20260714/official-harness
```

Ten workers is the official harness default. The declared 60-RPM limit applies
independently to the answerer and judge clients and is a provider-safe request
scheduler, not a scoring change; the upstream default is 200 RPM. The frozen
dataset, prompts, temperature, top-k contexts, cutoffs, five-attempt retry
policy, concurrency, and all-question denominator remain owned by the
unmodified official harness. The different V4 Flash model and the declared
request-rate limit mean this run must not be described as an identical
reproduction of Mem0's GPT-5 publication.

After evaluation, audit all 500 files and the predeclared split against the
immutable prediction archive:

```bash
python3 -m narratordb.benchmarks.evaluation_audit \
  --evaluated-directory reports/longmemeval-full500-v4flash-20260714/official-harness/predicted_narratordb-frozen-full500-20260714 \
  --frozen-directory reports/longmemeval-full500-frozen-20260714/official-harness/predicted_narratordb-frozen-full500-20260714 \
  --usage-log reports/longmemeval-full500-v4flash-20260714/openrouter-usage.jsonl \
  --evaluator-log reports/longmemeval-full500-v4flash-20260714/evaluate.log \
  --cutoffs 10,20,50,200 \
  --expected-questions 500 --require-official-score-complete \
  --output reports/longmemeval-full500-v4flash-20260714/evaluation-audit-all500.json
```

Repeat with `--question-id-file` pointing at the archived development or
holdout ID list. The auditor recomputes every aggregate and distinguishes an
official-harness-complete score (every declared item is structurally scored)
from stricter model-output completeness (no empty generated answer or judge).
It refuses both statuses for changed retrieval payloads, missing cutoffs,
invalid verdicts, missing scoped IDs, or altered denominators; `--require-complete`
additionally rejects empty model outputs.

Audit the prediction directory without exposing it to production retrieval:

```bash
python3 -m narratordb.benchmarks.prediction_audit \
  --prediction-dir reports/longmemeval-full500-frozen-20260714/official-harness/predicted_narratordb-frozen-full500-20260714 \
  --dataset /path/to/longmemeval_s_cleaned.json \
  --require-all \
  --output reports/longmemeval-full500-frozen-20260714/retrieval-audit.json
```

This reports official-harness completeness, ingestion failures, labeled-session
coverage, returned-memory counts, and HTTP-observed latency. Backend, engine,
and stage timings are included only when the upstream harness retains optional
`query_debug`; the pinned OSS client currently normalizes that field away, so
the audit marks those measurements unavailable instead of treating them as
zero. It never produces or judges an answer.

### Frozen full-500 retrieval results

The frozen predict-only run completed all 500 questions and all 124,345
official conversation pairs in 43 minutes 26 seconds, with zero failed pairs,
zero partial progress files, and no model calls. It stored 245,780 original
messages in 500 isolated scopes. The resulting 1,927,802,880-byte database
passed SQLite's full integrity check with no missing index, embedding, or
provenance rows. Its SHA-256 is
`3cdbf7c1558838c9259ff7aba2e4eb295276e25c38a69bffb7dc13e6dca1c3e0`.

Exact-message evidence-session coverage in the official prediction files was:

| Scope | Cutoff | Any labeled session | All labeled sessions |
|---|---:|---:|---:|
| All frozen questions | 10 | 476/500 (95.2%) | 405/500 (81.0%) |
| All frozen questions | 20 | 484/500 (96.8%) | 434/500 (86.8%) |
| All frozen questions | 50 | 489/500 (97.8%) | 449/500 (89.8%) |
| Untouched holdout | 10 | 437/458 (95.41%) | 368/458 (80.35%) |
| Untouched holdout | 20 | 442/458 (96.51%) | 394/458 (86.03%) |
| Untouched holdout | 50 | 447/458 (97.60%) | 409/458 (89.30%) |

These are retrieval diagnostics, not answer accuracy. The official HTTP search
latency over all 500 questions was 769.70 ms mean, 790.45 ms median, and
1,458.8 ms p95. On the untouched 458-question holdout it was 772.62 ms mean,
796.4 ms median, and 1,486.2 ms p95. Ingestion sustained 47.71 conversation
pairs or 94.31 messages per second.

The bundled Mem0 Platform v3 top-50 artifact reports 2,381.88 ms mean,
2,468.1 ms median, and 3,353.5 ms p95, making NarratorDB's observed latency
3.09×, 3.12×, and 2.30× lower respectively. This is useful operational
evidence, but not a controlled infrastructure comparison: NarratorDB ran
locally on the declared host while Mem0 ran as a managed remote service on
undisclosed infrastructure.

The sanitized immutable record is
[`benchmark_records/narratordb_full500_predict_20260714.json`](benchmark_records/narratordb_full500_predict_20260714.json).

### Frozen full-500 GLM 5.2 answer results (NarratorDB 1.3.0, 2026-07-15)

The 1.3.0 engine (git `bf4a6b1`, engine.py SHA-256 `7bbd152a…`, adapter SHA-256
`93bf5bd5…`) was frozen after the rerank fusion, retrieval-time result shaping,
and adjacent-evidence merging work, with all regression gates green (LoCoMo
1,301/1,540 unchanged, golden-ordering parity intact, full-500 evidence
coverage at the declared cutoffs pinned to the unshaped baseline by ablation).
A fresh frozen predict-only run rebuilt all 124,345 pairs with zero failures,
then one evaluate-only pass used `z-ai/glm-5.2` (high reasoning) as answerer
and dated `deepseek/deepseek-v4-flash-20260423` as judge through the
cost-capped OpenRouter transport. Cutoffs 20 and 50 were pre-declared before
the run to halve answerer input cost. The archived record retains the older
four-cutoff auditor's expected-incomplete flag. The updated auditor can validate
a 20/50 declaration when rerun with `--cutoffs 20,50`; no supplemental audit is
claimed for that immutable record here.

| Scope | Top 20 | Top 50 |
|---|---:|---:|
| All frozen questions | 408/500 (81.6%) | 414/500 (82.8%) |

By question type at top 50: knowledge-update 91.0%, multi-session 65.4%,
single-session-assistant 96.4%, single-session-preference 83.3%,
single-session-user 97.1%, temporal-reasoning 82.0%. Total evaluation cost was
$9.9845 (2,084 LLM calls, including the $0.64 pre-freeze 30-question
pre-flight). Official-harness HTTP search latency across the run: mean
30.57 ms, median 19.05 ms, p95 86.1 ms (1.2 reference: 769.70/790.45/1,458.8;
Mem0 Platform reference: 2,381.88 ms mean). The dev-42 ID archive was not
available during this 1.3 cycle, so no predeclared per-scope score belongs to
that record; tuning used only LoCoMo, the parity corpus, and full-500 aggregate
coverage counts. The split is now restored and hash-pinned, which permits a
clearly labeled post-hoc same-ID view of the immutable outputs. This result does
not beat Mem0's published 94.8% GPT-5 top-50 number, and the non-identical
answerer/judge keeps `claim_beats_mem0_accuracy` false. Complete record:
`benchmark_records/narratordb_full500_glm52_20260715.json`.

### Frozen Intelligence dev-42 result (compiler V6, development)

The first complete Intelligence run used the restored, inspected 42-question
development split, compiler prompt V6, `openai/gpt-5.4-mini` pinned to Azure
with minimal reasoning, and the same GLM 5.2 answerer plus dated DeepSeek judge
family as the 1.3 run. All 10,375 conversation pairs and all 42 questions
completed with no ingestion failure or selective rerun.

| System on the same 42 IDs | Top 20 | Top 50 |
|---|---:|---:|
| Immutable NarratorDB 1.3 outputs, post-hoc slice | 37/42 (88.0952%) | 36/42 (85.7143%) |
| NarratorDB Intelligence, compiler V6 | 29/42 (69.0476%) | 31/42 (73.8095%) |

The complete fresh-database/compiler-cache V6 execution cost $24.408927 for
1,979 compiler calls and $0.491719 for answering/judging, or $24.900646 total.
The full 42-question denominator is retained. The engine worktree itself was
dirty; its commit, tracked diff hash, and source manifest were captured in the
frozen record. V6 is five questions and 11.9048 percentage points below the 1.3
same-ID diagnostic at top 50. This is neither a Mem0 head-to-head nor an
untouched holdout result.

Official HTTP latency was 21,490.34 ms mean, 15,376.95 ms median, 36,039.7 ms
p95, and 185,036.4 ms maximum. Those are end-to-end first-search measurements.
Prior sessions were compiled at ingestion boundaries, while the final still-open
session was synchronously lazy-finalized on the first search. They are therefore
not comparable with 1.3's 30.57 ms local/raw search path. The upstream client
discarded `query_debug`, leaving no archived steady-state `query_ms`. The legacy
exact-message evidence auditor is also invalid for Intelligence's structured
claim-plus-excerpt representation and is not used as a quality claim.

The current compiler is V7 and retrieval now uses weighted raw/claim fusion,
bounded query-anchored session evidence, and structure-preserving excerpts.
The fresh V7 dev-42 predictions and paired development evaluation are described
below. The immutable V6 run record and artifact location are indexed in
[`benchmark_records/narratordb_intelligence_dev42_gpt54mini_20260716.json`](benchmark_records/narratordb_intelligence_dev42_gpt54mini_20260716.json).
The separate
[`V6 development postmortem`](benchmark_records/narratordb_intelligence_dev42_gpt54mini_20260716_postmortem.md)
records the taxonomy of all 11 failures, no-cost replay, and predeclared V7/Luna
experiment sequence without modifying that archive.

### Frozen Intelligence dev-42 V7/V8 paired development evaluation

The fresh V7 run froze all 42 official-harness prediction files before any
answerer or judge call. At that checkpoint `run-config.json` recorded
`status: prediction_frozen`, the evaluator recorded `status: not_started`, and
the prediction audit was explicitly score-free. A paired evaluation protocol
was hash-committed before either later judged score; it evaluated fresh,
byte-identical copies rather than mutating either frozen prediction archive.

| Variant on the same consumed dev-42 IDs | Top 20 | Top 50 |
|---|---:|---:|
| Fresh V7 compiler/retrieval prediction | 36/42 (85.7143%) | 38/42 (90.4762%) |
| V8 local retrieval/render replay over V7 compiler artifacts | 34/42 (80.9524%) | 34/42 (80.9524%) |

V8 is lower by 4.7619 percentage points at top 20 and 9.5238 points at top 50
in this pair. The replay constructed no compiler or compiler cache, made zero
hosted compiler calls, and reused the three disclosed V7 partial jobs. It is a
controlled local retrieval/render diagnostic on already consumed development
data, not a fresh end-to-end V8 result, untouched holdout, third-party score, or
Mem0 comparison.

Both variants used the unchanged official harness, GLM-5.2 answerer, dated
DeepSeek judge, high reasoning, identical 20/50 cutoffs, and the same routing
policy. The content-free proxy recorded eight malformed HTTP-200 responses for
V7 and six for V8; the unchanged harness retried them under its existing policy.
There were no attempt-five failures, final empty answers or judges, missing
questions, frozen-payload mismatches, or score-dependent reruns. V7 evaluation
used 176 events and cost $0.52975236451; V8 used 174 events and cost
$0.46747280219.

The first V8 reproduction preflight is preserved under
`benchmark_records/reproduction-v8-replay-20260716` as
`superseded_before_execution`: review caught a relative `git archive` output
path defect before any replay command or run root existed. The executed replay
uses attempt 2 under
`benchmark_records/reproduction-v8-replay-20260716-attempt2`; its preflight is
copied byte-for-byte into the run and bound by the replay audit.

The compiler produced 1,976 complete jobs and three terminal partial jobs from
the Azure provider's content filter. Canonical raw memory remained searchable
for all three, no question was dropped, and no selective retry changed the
denominator. The 1,983-event ledger cost $33.14190525, with zero model/provider
route mismatches. Six ledger events had an attempt number above one. One
timed-out harness request produced a single preserved BrokenPipe request event
(two stack entries in the server log) before the harness retried normally.

`V7_INPUT_SHA256SUMS` verifies the database, compiler cache, usage ledger,
dataset, 42-ID scope, prediction manifest, audits, run configuration, source
metadata, and copied source manifest. All 42 prediction and 42 ingestion files
also pass `PREDICTION_SHA256SUMS`; both SQLite databases pass `quick_check`.
The copied `SOURCE_FREEZE_SHA256SUMS` is archive-directory-relative and is not
directly invocable from the V7 run root because the tarball was not copied
there. This is a portability/usability defect, not an observed integrity
failure: the actual archive at
`reports/intelligence-v7-source-20260716T201147Z/narratordb-v7-source.tar.gz`
independently hashes to
`181df314f0b68ac1bbd3eb3b6363dd2a6b195f7c9beb5fd0d8deec043034c85d`,
matching the source metadata and run configuration. The sealed artifacts are
under `reports/longmemeval-intelligence-dev42-v7-gpt54mini-20260716`; the V8
replay is under
`reports/longmemeval-intelligence-dev42-v8-replay-v7gpt54mini-20260716`. The
paired summary is
[`benchmark_records/narratordb_intelligence_dev42_v7_v8_paired_20260716.json`](benchmark_records/narratordb_intelligence_dev42_v7_v8_paired_20260716.json).

### Frozen Intelligence dev-42 V7-control/V11 paired development evaluation

V11 first passed its frozen synthetic canary, then replayed only local retrieval
and rendering over a copy of the sealed V7 compiler database. It constructed no
compiler or compiler cache, made no hosted compiler calls, kept the database
counts unchanged, and froze all 42 prediction payloads before a separate paired
evaluation. Two schema-invalid preflight attempts are preserved as failures
before execution; neither created a run nor incurred model cost. The executed
preflight and prediction manifests pass their audits.

| Variant on the same consumed dev-42 IDs | Top 20 | Top 50 |
|---|---:|---:|
| Contemporaneous frozen V7 control | 35/42 (83.3333%) | 38/42 (90.4762%) |
| V11 local retrieval/render replay over V7 compiler artifacts | 35/42 (83.3333%) | 37/42 (88.0952%) |

V11 is one question and 2.381 percentage points lower at top 50 and is not
promoted. Both variants used the unchanged official harness, GLM-5.2 answerer,
dated DeepSeek judge, high reasoning, identical routing, and the complete
42-question denominator. There were no final empty answers or judges, dropped
questions, frozen-payload mismatches, unknown-cost attempts, invalid completion
identities, or score-dependent reruns. V7 used 183 evaluator events and cost
$0.734585144; V11 used 181 and cost $0.61808457. The provider telemetry delta
matches the $1.352669714 paired total exactly.

The exact flip audit prevents overinterpreting the one-question difference.
Thirty-three of 42 full 200-result retrieval payloads were byte-identical.
Both top-20 flips and one top-50 flip occurred on identical contexts and are
answerer/judge variance. Of eight aggregation-pack questions, both versions
scored 5/8 at top 20 and 6/8 at top 50: V11 gained one distinct cross-session
count and lost one evolving cumulative-state count. The observed net top-50
loss came from the separate identical-context flip, so this pair neither proves
an aggregation gain nor a broad aggregation regression.

The complete negative result is retained in
[`benchmark_records/narratordb_intelligence_dev42_v7_v11_paired_20260716.json`](benchmark_records/narratordb_intelligence_dev42_v7_v11_paired_20260716.json).
Its
[`postmortem`](benchmark_records/narratordb_intelligence_dev42_v7_v11_paired_20260716_postmortem.md)
records every flip, evaluator variance, the query-relevance/cumulative-state
mechanism, exact hashes, and the generic V12 correction. This remains an
inspected consumed-development diagnostic, not a fresh V11 compiler result,
untouched holdout, third-party reproduction, Mem0 comparison, or publishable
95% claim.

V12 implements that correction without benchmark identifiers, expected
answers, judge outputs, or dataset-specific terms. Present-perfect cumulative
snapshots retain ordinary ranking; distinct-event packs put claim-FTS user
sources before dense/raw overfetch and admit claims through conservative query
overlap across text, subject, predicate, object, and memory key. Unrelated
co-located facts are excluded, while a bounded semantic/raw fallback remains
when no query-backed claim exists. The aggregation-pack identity format is 2.
This code change has no score until its frozen local replay and any separately
precommitted evaluation complete.

### Frozen Intelligence dev-42 V18 direct-official OpenAI paired evaluation

V18 is the evaluation-protocol label for this attempt series, not a NarratorDB
package version. The series preserves its incomplete predecessors instead of
silently discarding them. R1 produced a primary score of 40/42 at top 20 and
41/42 at top 50, but its transport publication gate failed after one completion
with unknown identity/cost and replication never ran. R2's clean primary scored
38/42 and 41/42, respectively, but the unconditional replication terminated on
an unforwarded completion. Neither R1 nor R2 is a paired score.

R3 moved the evaluator to the direct official OpenAI endpoint and froze the
protocol, source, dataset, 42-ID scope, evaluator, prediction bytes, exact
model snapshot, retry policy, costs, and success rule before its first canary
call. Both complete arms used the unchanged official harness at commit
`4b61c5d31b9c668a12b4f5e78064248a02c82d2b`, top-k 200, cutoffs 20/50, and
the pinned `gpt-5.4-mini-2026-03-17` snapshot with high reasoning as both
answerer and judge. The second arm was an evaluator replication over identical
frozen prediction payloads. It did not rebuild NarratorDB, recompile memory, or
rerun retrieval.

| Cutoff | Primary | Unconditional replication | Verdict agreement | Frozen target |
|---|---:|---:|---:|---:|
| Top 20 | 39/42 (92.8571%) | 38/42 (90.4762%) | 39/42 (92.8571%) | Below 95% |
| Top 50 | **42/42 (100%)** | **41/42 (97.6190%)** | 41/42 (97.6190%) | **Passed in both arms** |

The paired lower top-50 score is 41/42, or 97.6190%. The two top-50 scores
differ by one question (2.381 percentage points), and the frozen success rule
required at least 40/42 in both arms without changing execution or publication
based on the score. Top 20 did not reach 95% and is disclosed alongside the
target cutoff.

The strict canary completed two first-attempt calls. Each scored arm then
completed exactly 168 accepted official OpenAI calls: 336/336 total, all HTTP
200 with the exact endpoint, request/response model, default service tier,
nonempty visible content, and stop completion identity. There were zero
discarded transients, terminal rejections, hidden-SDK retries, unknown-cost
events, selective question reruns, score-driven route/prompt changes, or
frozen-payload mismatches. Replication began only after a score-blind primary
transport gate and was unconditional.

Local conservative token-ledger cost was $0.000237750 for the canary,
$0.691709250 for primary, and $0.428239500 for replication: $1.120186500 new
R3 spend and $3.998469932 cumulative conservative R1-R3 exposure. Provider
billing was not reconciled, provider account telemetry was not called, and the
$30 balance used for admission was user-attested rather than API-verified.

The final paired audit SHA-256 is
`4f7460c801f94ff00d536d3dedaef41e831e893e53837b6c89382b2ec96640be`;
its 37-entry manifest SHA-256 is
`773a0e14e31e51a207db9494907ddd19822b5bf8859d890d234783898f0dd167`.
The precommit SHA-256 is
`6e6abdc38508842174f8dad879a28936f4d1a656138db5628a5626553db38da5`.
The sanitized history record is
[`benchmark_records/narratordb_intelligence_dev42_v18_gpt54mini_official_r3_20260717.json`](benchmark_records/narratordb_intelligence_dev42_v18_gpt54mini_official_r3_20260717.json),
and the raw final audit is retained under
`reports/longmemeval-intelligence-dev42-v18-gpt54mini-official-openai-high-selfjudge-paid-pair-r3-20260717`.

This result is a score-exposed post-hoc consumed development-set diagnostic.
R1/R2 scores were known before R3 was sealed, although R3's own score fields
were absent from its precommit. The same GPT snapshot answered and judged, so
self-preference risk remains. It is not blind, not an untouched holdout, not an
independent-judge score, not a headline benchmark, and not a Mem0 head-to-head.

### Frozen full-500 V4 Flash answer and judge results

The evaluate-only phase completed all 500 frozen questions under the same
official harness commit, dataset, prompts, context formatting, temperature,
cutoffs, ten-worker concurrency, five-attempt retry policy, and failed-sample
denominator. Both reader and judge used the dated
`deepseek/deepseek-v4-flash-20260423` model with high reasoning. The declared
60-RPM scheduler was lower than the harness's 200-RPM default to remain inside
provider limits; it changes scheduling, not prompts or scoring. Consequently,
this is an exact official-harness score, but not an identical reproduction of
Mem0's GPT-5 run.

| Scope | Top 10 | Top 20 | Top 50 | Top 200 |
|---|---:|---:|---:|---:|
| All frozen questions | 368/500 (73.6%) | 390/500 (78.0%) | 380/500 (76.0%) | 383/500 (76.6%) |
| Known development 42 | 36/42 (85.71%) | 34/42 (80.95%) | 34/42 (80.95%) | 35/42 (83.33%) |
| Untouched holdout 458 | 332/458 (72.49%) | 356/458 (77.73%) | 346/458 (75.55%) | 348/458 (75.98%) |

The official unified result and an independent auditor agree exactly on every
aggregate and question-type count. Every question has all four valid judge
scores, so the official score is complete. One holdout temporal question had
an empty generated answer at top 50 and top 200; both nonempty judges returned
FAIL, both failures remain in the denominator, and no output was repaired or
rerun. The stricter no-empty-output integrity flag is therefore false and the
exact two exceptions are recorded.

The paid phase ran for 69 minutes 4 seconds and made 4,043 successful HTTP 200
completion calls with no upstream HTTP errors. It used 27,063,907 prompt tokens
(3,751,365 cache-read), 1,549,943 completion tokens, and 1,424,814 reasoning
tokens. Response metadata reports $2.61591663084 total, or $0.005232 per
question. The provider ledger contains 3,309 GMICloud, 414 DeepInfra, 296
StreamLake, 11 Baidu, and 13 responses without provider metadata. A discarded
canary and an aborted throttled attempt are archived separately and excluded
from this score and cost.

The result also isolates the next bottleneck. At top 50, NarratorDB retrieved
at least one exact labeled evidence session for 489/500 questions, yet V4 Flash
passed 379/489 (77.51%) of those; even when every labeled session was present,
it passed 368/449 (81.96%). This is diagnostic correlation rather than a
rescore, but it shows that reader/judge/context reasoning now limits final
accuracy in addition to the remaining retrieval misses. Top 20 was the best
V4 Flash cutoff; top 50 remains the like-for-like cutoff for Mem0's published
table.

The immutable sanitized record is
[`benchmark_records/narratordb_full500_v4flash_20260714.json`](benchmark_records/narratordb_full500_v4flash_20260714.json).
The raw local archive contains the 500 per-question files, official unified
result, exact command, usage ledger, source hashes, independent full/dev/holdout
audits, release validation, and a SHA-256 manifest.

### Current Mem0 published reference

At official harness commit `4b61c5d`, Mem0's README publishes Platform v3 at
474/500 (94.8%) for top 50 and 472/500 (94.4%) for top 200 using GPT-5. This is
a vendor-published reference, not a same-model comparison with V4 Flash.

An integrity audit found an upstream discrepancy: the two JSON artifacts
committed beside that README contain 452 PASS judgments (90.4%) at top 50 and
467 PASS judgments (93.4%) at top 200. The top-50 JSON also reports 500 search
latencies: 2,381.88 ms mean, 2,468.1 ms median, and 3,353.5 ms p95. NarratorDB
records both the published table and the artifact-derived values instead of
silently selecting one. File sizes, hashes, model metadata, and the full finding
are in
[`benchmark_records/mem0_platform_v3_longmemeval_20260407.json`](benchmark_records/mem0_platform_v3_longmemeval_20260407.json).

An exact same-model Mem0 head-to-head still requires Mem0 Platform access or a
separately pinned OSS configuration; changing only the answer model is not
enough because memory extraction and retrieval are part of the tested system.

### Immutable benchmark history

Raw outputs and databases live under ignored `reports/` directories. Selected
sanitized run summaries and their SHA-256 references are tracked in
[`benchmark_records/index.json`](benchmark_records/index.json). The full
archive verification command requires every ignored raw directory and manifest
referenced by that index, so it succeeds only on an archive host that retains
the complete `reports/` tree:

```bash
python3 -m narratordb.benchmarks.history verify-index
```

The archive command refuses duplicate run IDs, out-of-repository paths,
symlinks, key-shaped content, unmanifested files, checksum changes, and modified
tracked summaries. A clean public Git clone can verify every public summary and
its indexed hash without the content-bearing archives:

```bash
python3 -m narratordb.benchmarks.history verify-index --records-only
```

It cannot run full archive verification because the content-bearing raw
archives are intentionally not pushed to GitHub.

## Key-free LongMemEval evidence retrieval

This adapter uses the official cleaned 500-question dataset and its evidence
session labels. It reports recall-any, recall-all, MRR, and NDCG without an
answer model:

```bash
python3 -m narratordb.benchmarks.longmemeval --all \
  --dataset /path/to/longmemeval_s_cleaned.json \
  --output /tmp/narratordb-longmemeval.json
```

Adapter calibration: 12 questions, two per type, seed 42.

| Cutoff | Recall-any | Recall-all | MRR | NDCG |
|---:|---:|---:|---:|---:|
| 5 | 0.833333 | 0.666667 | 0.711111 | 0.688031 |
| 10 | 0.916667 | 0.833333 | — | — |
| 20 | 1.000000 | 0.833333 | — | — |
| 50 | 1.000000 | 0.833333 | — | — |

Mean query latency was 393.936 ms; p50 was 393.882 ms and p95 was 580.007 ms.
The calibration validates the adapter and is not a 500-question headline.

## Key-free BEAM diagnostic

The BEAM adapter loads the official `Mohammadta/BEAM` dataset. The official
pass percentage requires an answer model and rubric judge, so NarratorDB's
key-free adapter reports lexical rubric coverage and maximum embedding
similarity only:

```bash
python3 -m narratordb.benchmarks.beam --size 100K --all \
  --output /tmp/narratordb-beam-100k.json
```

The one-conversation / one-question-per-ability calibration stored 188
messages and evaluated 10 questions. Mean token coverage was 0.606936, mean
maximum semantic similarity was 0.265583, and query latency was 225.889 ms mean,
219.733 ms p50, and 271.544 ms p95. These are diagnostic metrics, not BEAM pass
rates.

## Internal Mem0 categories 1-4 LoCoMo regression

`python3 -m tests.stress.complex_retrieval` runs the 1,540 bundled categories
1-4 questions from the Mem0-derived LoCoMo fixture with a transparent answer-in-
retrieved-context heuristic. It excludes the original-author protocol's 446
category-5 adversarial items. The frozen 2026-07-14 run found the answer in
context for 1,301 questions, or 0.8448, improving the prior 1,297/1,540 result.
Query latency was 6.356 ms mean, 5.773 ms p50, 12.664 ms p95, and 164.864 ms
maximum. Its separate 5,000-noise-message scale pass was 1.388 ms mean and
4.369 ms p95, with a 100% hit rate on all six fixed queries. This detects
retrieval regressions; it is not an original-author scorer or an LLM answerer/
judge score.

## Coverage matrix

| Benchmark | Original-author scored protocol | Key-free/internal diagnostic | Current status |
|---|---|---|---|
| LoCoMo | 1,986 answer calls plus the deterministic author scorer | Mem0 categories 1-4 subset: 1,540 answer-in-context checks | Author-protocol full run not yet scored; internal subset regression complete |
| LongMemEval-S | 500 answer calls plus 500 pinned `gpt-4o-2024-08-06` judge calls | Evidence recall/MRR/NDCG | Author protocol not yet scored; the all-500 and dev-42 results above use the distinct Mem0 harness |
| BEAM 128K–10M | 2,000 answers plus the author rubric/event evaluation pipeline | Token/semantic rubric coverage | All tier files are available; only 100K calibration and short-diagnostic scopes have run |
| ConvoMem | Requires pinned MemoryBench provider/reader/judge | Not approximated | Planned, not scored |
| LongMemEval-V2 | Requires official multimodal harness | Not approximated | Planned, not scored |
| EverMemBench | Requires official generator/judge stack | Not approximated | Planned, not scored |
| CloneMem | Requires official task harness | Not approximated | Planned, not scored |
| EvoMemBench | Requires official evolving-memory harness | Not approximated | Planned, not scored |

Primary sources:

- [LoCoMo](https://github.com/snap-research/locomo)
- [Mem0 memory-benchmarks](https://github.com/mem0ai/memory-benchmarks)
- [Supermemory MemoryBench](https://github.com/supermemoryai/memorybench)
- [LongMemEval](https://github.com/xiaowu0162/LongMemEval)
- [LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2)
- [BEAM](https://github.com/mohammadtavakoli78/BEAM)

No claim that NarratorDB beats another system should be published until an
exact full Tier-1 run has the same pinned answerer and judge and includes all
failed samples in the denominator.
